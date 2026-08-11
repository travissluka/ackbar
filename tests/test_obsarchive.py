"""Tier 0: the archive layout, and the round trip a window makes through it.

The bug this module exists for was not a wrong number anywhere. The archive was
written one file per assimilation window, cut by the generator, and an
assimilation window is half open, `(begin, end]`, so the observation stamped at
the instant a window opens was dropped by that window and was in no other
window's file. About a quarter of every six hourly platform's observations were
lost in every cycle, in silence: the file held the row, the observer read the
file, and no departure was ever formed from it.

So the test that matters here is not "the archive has N observations" and not
"the staged file has N rows". It is the round trip: take the times an archive
holds, stage a window out of it, apply the half open cut the observer applies,
and assert that what survives is exactly the observations whose time is in the
window. A count test passes on an archive that files the right number of
observations at the wrong instants, which is the archive that was built.

`covering` is checked separately from the round trip because the three
degenerate windows are easier to state than to construct: a window opening
exactly on a bin boundary, which is the original bug in its general form, a
window shorter than one bin, and a window spanning many.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import write_bin

from ackbar import obsarchive

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
START = datetime(2015, 7, 16, tzinfo=timezone.utc)


def when(*offsets):
    return [START + timedelta(hours=hours) for hours in offsets]


@pytest.fixture
def archive(tmp_path):
    """Four daily bins of a six hourly platform, sampled on the hour grid.

    Six hourly and anchored on the day is the cadence the loss was measured on,
    and it is the one that puts an observation exactly on a bin boundary, which
    is exactly where a 24 hour window opens.
    """
    directory = tmp_path / "glider_t"
    for day in range(4):
        start = START + day * DAY
        write_bin(directory, start,
                  [start + timedelta(hours=h) for h in (0, 6, 12, 18)])
    return directory


# --- the selection rule ------------------------------------------------------

def test_a_window_opening_on_a_bin_boundary_takes_that_bin_and_the_next(archive):
    """The original bug, generalized: the seam and the window edge coincide.

    The window `(day 1 00:00, day 2 00:00]` opens exactly where a bin does. Its
    observations are the ones after midnight on day 1 and the one at midnight
    on day 2, and that last one is in the bin *starting* at day 2. A rule that
    stopped at `start < end` would leave it in a file nothing staged.
    """
    begin = START + DAY
    picked = obsarchive.window(archive, begin, begin + DAY)
    assert [path.name for path in picked] == [
        obsarchive.name(START + DAY), obsarchive.name(START + 2 * DAY)]


def test_a_window_inside_one_bin_takes_only_that_bin(archive):
    begin = START + timedelta(hours=3)
    picked = obsarchive.window(archive, begin, begin + timedelta(hours=6))
    assert [path.name for path in picked] == [obsarchive.name(START)]


def test_a_window_shorter_than_the_time_between_two_bins_still_finds_one(archive):
    """A ten minute window in a daily archive is not an empty answer.

    The rule reaches back to the bin the window opens inside rather than
    looking for one that starts in it, which is what makes the bin duration
    something the staging code never has to know.
    """
    begin = START + timedelta(hours=13)
    picked = obsarchive.window(archive, begin, begin + timedelta(minutes=10))
    assert [path.name for path in picked] == [obsarchive.name(START)]


def test_a_window_spanning_many_bins_takes_all_of_them(archive):
    picked = obsarchive.window(archive, START + HOUR, START + 3 * DAY)
    assert len(picked) == 4


def test_a_window_before_the_archive_takes_nothing(archive):
    picked = obsarchive.window(archive, START - 2 * DAY, START - DAY)
    assert picked == []


def test_a_window_after_the_archive_takes_the_last_bin(archive):
    """Not a gap: the last bin is the one that instant falls in.

    Whether it holds anything in the window is ioda's question, and it answers
    it by reading the file rather than by the file's name.
    """
    picked = obsarchive.window(archive, START + 10 * DAY, START + 11 * DAY)
    assert [path.name for path in picked] == [obsarchive.name(START + 3 * DAY)]


def test_a_missing_bin_is_reached_past_rather_than_stopping_the_search(tmp_path):
    """A bin with no observations has no file, and that is not a hole.

    The rule needs the last bin at or before an instant, so an absent one costs
    nothing: it was empty, and reaching further back stages a file that
    contributes no rows in the window rather than missing rows that exist.
    """
    directory = tmp_path / "adt_c2"
    write_bin(directory, START, when(1))
    write_bin(directory, START + 2 * DAY, when(49))
    picked = obsarchive.window(directory, START + DAY + HOUR, START + DAY + 2 * HOUR)
    assert [path.name for path in picked] == [obsarchive.name(START)]


def test_a_directory_that_is_not_there_is_an_empty_archive_and_not_a_crash(tmp_path):
    assert obsarchive.window(tmp_path / "nothing", START, START + DAY) == []


def test_a_file_that_is_not_a_bin_is_ignored(tmp_path):
    """An archive carries a README, and a README is not a time bin."""
    directory = tmp_path / "argo_t"
    write_bin(directory, START, when(1))
    (directory / "README.md").write_text("the layout\n")
    assert len(obsarchive.window(directory, START, START + DAY)) == 1


# --- the round trip ----------------------------------------------------------

def stage(archive, begin, end, target):
    """What `stage.obs` does, and then what the observer does to the result."""
    obsarchive.concatenate(obsarchive.window(archive, begin, end), target)
    times = obsarchive.read_times(target)
    return times, [moment for moment in times if begin < moment <= end]


def test_the_staged_file_holds_every_observation_in_the_window(archive, tmp_path):
    """The test the original bug would have failed.

    Every six hourly sample of the day, including the one at the instant the
    window opens, which belongs to the window that ends there, and the one at
    the instant it closes, which belongs to this one.
    """
    begin = START + DAY
    _, kept = stage(archive, begin, begin + DAY, tmp_path / "staged.nc4")
    assert kept == [begin + timedelta(hours=h) for h in (6, 12, 18, 24)]


def test_no_observation_is_read_twice_and_none_is_read_by_nobody(archive, tmp_path):
    """Three consecutive windows tile the archive, and so must their contents.

    This is the property the per window archive broke and the one no count test
    notices: the window that lost an observation and the window that should
    have had it were both the right size.
    """
    seen = []
    for index in range(3):
        begin = START + index * DAY
        _, kept = stage(archive, begin, begin + DAY,
                        tmp_path / f"staged{index}.nc4")
        seen += kept

    # Everything the three windows span, which reaches the fourth bin's first
    # observation: the instant the third window closes is inside it.
    everything = [START + timedelta(hours=h) for h in range(0, 73, 6)]
    # The archive's first observation is at `START`, which is the instant the
    # first window opens: no window contains it, the same way no cycle contains
    # the instant before the experiment begins. Everything after it is read
    # exactly once.
    assert sorted(seen) == everything[1:]
    assert len(set(seen)) == len(seen)


def test_the_join_rebases_the_times_rather_than_repeating_an_epoch(archive,
                                                                  tmp_path):
    """Each bin is written against its own start, and a naive join would lie.

    Two files whose offsets both begin at zero mean two different instants.
    Concatenating the numbers and keeping the first file's `units` would file
    the second day's observations on the first day, which is a shift of exactly
    one bin: every observation still lands in the window, in the wrong place.
    """
    begin = START + DAY
    times, _ = stage(archive, begin, begin + DAY, tmp_path / "staged.nc4")
    assert len(times) == 8
    assert times == sorted(times)
    assert times[0] == START + DAY
    assert times[-1] == START + 2 * DAY + timedelta(hours=18)


def test_the_join_keeps_every_group_variable_and_attribute(archive, tmp_path):
    import netCDF4

    target = tmp_path / "staged.nc4"
    obsarchive.concatenate(obsarchive.window(archive, START, START + DAY), target)
    with netCDF4.Dataset(target) as data:
        assert set(data.groups) == {"MetaData", "ObsValue", "ObsError", "PreQc"}
        assert set(data.groups["MetaData"].variables) == {
            "dateTime", "longitude", "latitude"}
        assert data.getncattr("_ioda_layout") == "ObsGroup"
        assert len(data.dimensions["Location"]) == 8
        # ioda's row index numbers the file it is in, so a joined file has to be
        # renumbered rather than carrying two copies of 0..3.
        assert list(data.variables["Location"][:]) == list(range(8))


def test_an_empty_bin_joins_to_nothing_rather_than_failing(tmp_path):
    """A file with no rows is a platform that saw nothing, which is data.

    `tools/obs-cull-domain.py` writes exactly this whenever a bin holds nothing
    inside the domain, so the join meets it routinely.
    """
    directory = tmp_path / "sss_smap"
    write_bin(directory, START, [])
    write_bin(directory, START + DAY, when(30))
    target = tmp_path / "staged.nc4"
    rows = obsarchive.concatenate(
        obsarchive.window(directory, START + HOUR, START + DAY + 12 * HOUR), target)
    assert rows == 1
    assert obsarchive.read_times(target) == when(30)


def test_files_of_different_shapes_are_refused_rather_than_joined(tmp_path):
    """Two files of one platform disagreeing about their variables is a broken
    archive, and the staged file would be silently short of a variable."""
    directory = tmp_path / "mixed"
    write_bin(directory, START, when(1))
    write_bin(directory, START + DAY, when(25), variable="seaSurfaceSalinity")
    with pytest.raises(obsarchive.ArchiveError, match="one platform"):
        obsarchive.concatenate(
            obsarchive.window(directory, START, START + DAY), tmp_path / "x.nc4")
