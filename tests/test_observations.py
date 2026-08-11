"""Tier 0: which observers ran, and the record of the ones that did not.

The behaviour under test is a decision, not a computation: an archive with
nothing inside a window drops that observer and the cycle carries on. That is
right for any real archive and it is also how an experiment quietly assimilates
nothing, so every test here is about the record being complete and being obeyed,
and about the one case that is not a gap: a window no observer at all has an
observation in.

What the staging does to the observations themselves is `test_obsarchive.py`,
which owns the selection rule and the round trip. Here the archive is a means
rather than the subject, so its bins hold one observation each.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import PATHS_CYCLE, write_bin

from ackbar import observations, obsarchive
from ackbar.duration import parse_instant
from ackbar.paths import Paths

#: The archive's bins are days and the cycles are days, offset by half a day
#: because a window is centred on its analysis time. Cycle 1's window is
#: therefore `(day 0 12:00, day 1 12:00]` and touches two of them.
FIRST = parse_instant("2018-04-15T00:00:00Z")
DAY = timedelta(days=1)

CONFIG = {
    "experiment": {"name": "e"},
    "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H", "count": 3},
    "observations": [
        {
            "obs space": {
                "name": "adt_3a",
                "$archive": "ARCHIVE/adt_3a",
                "obsdatain": {"engine": {"obsfile": "OUT/{{cycle}}/adt.nc4"}},
                "obsdataout": {"engine": {"obsfile": "OUT/{{cycle}}/adt.nc4"}},
                "obs perturbations seed": "{{seed}}",
            },
            "obs operator": {"name": "ADT"},
        },
        {
            "obs space": {
                "name": "sst_noaa19",
                "$archive": "ARCHIVE/sst_noaa19",
                "obsdatain": {"engine": {"obsfile": "OUT/{{cycle}}/sst.nc4"}},
                "obsdataout": {"engine": {"obsfile": "OUT/{{cycle}}/sst.nc4"}},
            },
            "obs operator": {"name": "Identity"},
        },
    ],
}


@pytest.fixture
def env(tmp_path):
    """A config whose archive is a real directory, and paths beside it."""
    config = json.loads(json.dumps(CONFIG))
    for entry in config["observations"]:
        space = entry["obs space"]
        space["$archive"] = space["$archive"].replace(
            "ARCHIVE", str(tmp_path / "obs"))
        for side in ("obsdatain", "obsdataout"):
            engine = space[side]["engine"]
            engine["obsfile"] = engine["obsfile"].replace(
                "OUT", str(tmp_path / "out"))
    paths = Paths(experiment="e", output_root=tmp_path / "o",
                  scratch_root=tmp_path / "s", **PATHS_CYCLE).ensure()
    return config, paths, tmp_path / "obs"


def stage_archive(archive, cycle, names=("adt_3a", "sst_noaa19")):
    """One bin per platform, covering the given cycle's window.

    A time bin rather than a file per window: the archive knows nothing about
    the cycle, so what makes an observer present for cycle *n* is that some bin
    covers cycle *n*'s window.
    """
    start = FIRST + (cycle - 1) * DAY
    for name in names:
        write_bin(archive / name, start, [start + timedelta(hours=1)])


def test_an_observer_with_a_bin_is_present_and_one_without_is_not(env):
    config, paths, archive = env
    stage_archive(archive, 1, names=("sst_noaa19",))
    found = {r["name"]: r["present"]
             for r in observations.realize(config, paths, 1)}
    assert found == {"adt_3a": False, "sst_noaa19": True}


def test_a_bin_that_covers_the_window_but_holds_nothing_in_it_drops_the_observer(
        env):
    """Reaching back to an older bin is not the same as having observations.

    The selection rule takes the last bin at or before the window, so an
    archive that stops half way through an experiment still hands every later
    cycle a file. What settles `present` is how many of its observations are
    actually inside the window, because the alternative is a run that records a
    full observing system and assimilated nothing.
    """
    config, _, archive = env
    stage_archive(archive, 1)
    records = observations.observers(config, 6)
    assert all(record["sources"] for record in records)
    assert not any(record["present"] for record in records)


def test_the_window_and_not_the_cycle_number_decides_which_bins_are_read(env):
    # The question is which bins this cycle's window touches, so a window
    # computed from anything but the cycle's own analysis time would read an
    # archive nothing needed.
    config, paths, archive = env
    stage_archive(archive, 2)
    assert all(r["present"] for r in observations.realize(config, paths, 2))
    assert all(not r["present"] for r in observations.observers(config, 9))


def test_the_seed_reaches_the_observer_as_a_number_and_not_a_token(env):
    config, _, _ = env
    space = observations.observers(config, 1)[0]["config"]["obs space"]
    assert "{{" not in str(space["obs perturbations seed"])


def test_ackbars_own_keys_do_not_reach_jedi(env):
    """`required` is ACKBAR's, and an unknown key is a key UFO may reject.

    Losing a cycle to a value that was never meant to leave the workflow is a
    bad trade for a line of config.
    """
    config, _, _ = env
    config["observations"][0]["obs space"]["$required"] = True
    record = observations.observers(config, 1)[0]
    assert record["required"] is True
    assert "$required" not in record["config"]["obs space"]


# --- the record --------------------------------------------------------------

def test_the_list_names_every_configured_observer_including_the_dropped_ones(env):
    config, paths, archive = env
    stage_archive(archive, 1, names=("sst_noaa19",))
    observations.realize(config, paths, 1)

    written = json.loads(paths.observer_list(1).read_text())
    assert written["cycle"] == 1
    assert [r["name"] for r in written["observers"]] == ["adt_3a", "sst_noaa19"]
    assert [r["present"] for r in written["observers"]] == [False, True]


def test_the_list_records_which_bins_each_observer_read(env):
    # A comparison between two experiments is a comparison of what they read,
    # and under a window agnostic archive the configured path no longer says
    # it: the answer is which bins the window selected, so that is recorded.
    config, paths, archive = env
    stage_archive(archive, 1)
    records = observations.realize(config, paths, 1)
    assert all(record["rows"] == 1 for record in records)

    written = observations.read(paths, 1)
    assert all(str(archive) in source
               for record in written for source in record["sources"])


def test_the_staged_file_is_written_where_the_observer_will_look(env):
    config, paths, archive = env
    stage_archive(archive, 1)
    for record in observations.realize(config, paths, 1):
        assert Path(record["input"]).exists()


def test_a_required_observer_with_no_file_fails_the_cycle(env):
    config, paths, archive = env
    config["observations"][0]["obs space"]["$required"] = True
    stage_archive(archive, 1, names=("sst_noaa19",))
    with pytest.raises(observations.ObservationError, match="adt_3a"):
        observations.realize(config, paths, 1)
    assert not paths.observer_list(1).exists()


def test_an_observer_naming_no_archive_is_refused_by_name(env):
    """A layer that forgot `$archive` would otherwise stage nothing, quietly.

    Every observer would be dropped and the cycle would run to completion
    assimilating nothing, which is the most expensive way to find a missing
    line of config.
    """
    config, paths, archive = env
    stage_archive(archive, 1)
    del config["observations"][0]["obs space"]["$archive"]
    with pytest.raises(observations.ObservationError, match="adt_3a"):
        observations.realize(config, paths, 1)


def test_a_window_missing_from_a_working_archive_stops_the_cycle(env):
    """The distinction the cycle itself cannot make, made from the archive.

    One platform absent is that platform's gap and the others carry the cycle.
    Every platform absent at the same instant, in an archive that answers for
    the cycle either side of it, is a window that was never built. Nothing
    downstream would notice: the analysis is skipped, the writeback hands the
    background on, and `post.obs` reports zero of zero and passes.
    """
    config, paths, archive = env
    stage_archive(archive, 2)
    with pytest.raises(observations.ObservationError, match="cycle 2 has some"):
        observations.realize(config, paths, 1)
    assert not paths.observer_list(1).exists()


def test_an_archive_that_answers_nowhere_says_so_rather_than_blaming_the_window(env):
    # The same refusal, different cause and different fix: a path that resolves
    # to nothing is `ackbar validate`'s finding, and saying "this window was
    # never built" would send the reader to rebuild an archive that is not
    # where they think it is.
    config, paths, _ = env
    with pytest.raises(observations.ObservationError,
                       match="neither does any other cycle"):
        observations.realize(config, paths, 1)


def test_one_observer_surviving_is_enough_to_carry_the_cycle(env):
    # The gap that is genuinely data. The rule is about a cycle with nothing at
    # all, not about a cycle that lost a platform.
    config, paths, archive = env
    stage_archive(archive, 1, names=("sst_noaa19",))
    records = observations.realize(config, paths, 1)
    assert [r["present"] for r in records] == [False, True]
    assert paths.observer_list(1).exists()


# --- hofx obeys the record ---------------------------------------------------

def test_hofx_evaluates_what_was_staged_and_not_what_is_there_now(env):
    """The reason the list is read rather than recomputed.

    A file that appears between `stage.obs` and hofx would otherwise change the
    observer set without changing the record that documents it, which is the
    exact invisibility the record exists to prevent.
    """
    config, paths, archive = env
    stage_archive(archive, 1, names=("sst_noaa19",))
    observations.realize(config, paths, 1)

    stage_archive(archive, 1, names=("adt_3a",))
    assert [r["name"] for r in observations.selected(config, paths, 1)] == \
        ["sst_noaa19"]


def test_hofx_without_a_staged_list_says_which_task_should_have_written_it(env):
    config, paths, _ = env
    with pytest.raises(observations.ObservationError, match="stage.obs"):
        observations.selected(config, paths, 1)


# --- a task that stages a window which is not its own ------------------------

def test_a_lead_window_is_staged_into_the_directory_the_reader_names(env,
                                                                    tmp_path):
    """`hofx.ext` reads windows whose own `stage.obs` has not run yet.

    Cycle 1's F048 window is staged by cycle 3, so an observer left pointing at
    `obs_in/<T>` is pointing at a directory that does not exist. The reading
    task therefore names a file of its own and joins into it, and both the
    record and the document JEDI is handed have to follow, or the run reads one
    file and the log reports another.
    """
    config, _, archive = env
    stage_archive(archive, 2)
    record = observations.observers(config, 2)[0]
    target = tmp_path / "task" / "obs_in" / "20180416T000000Z" / "adt.nc4"
    observations.redirect_input(record, target)
    assert record["input"] == str(target)
    assert record["config"]["obs space"]["obsdatain"]["engine"]["obsfile"] \
        == str(target)

    begin = FIRST + DAY / 2
    observations.stage_lead(record, begin, begin + DAY)
    assert target.exists()
    assert record["rows"] == 1


def test_a_lead_window_is_cut_to_itself_and_not_left_bin_wide(env, tmp_path):
    """The cut `stage.obs` must not make and this one must.

    An application evaluating several windows at once cannot have ioda separate
    one window's rows from the next one's, so an uncut file hands two adjacent
    observers the same rows. `obsarchive` owns the rule; this is that the
    staging path uses it.
    """
    config, _, archive = env
    for cycle in (1, 2):
        stage_archive(archive, cycle)

    record = observations.observers(config, 2)[0]
    observations.redirect_input(record, tmp_path / "cut.nc4")
    begin = FIRST + DAY + timedelta(hours=-12)
    observations.stage_lead(record, begin, begin + DAY)
    # Two bins reached, one row kept: the other bin's observation is a day
    # earlier and belongs to the lead window before this one.
    assert len(record["sources"]) == 2
    assert record["rows"] == 1
    assert obsarchive.concatenate(record["sources"],
                                  tmp_path / "uncut.nc4") == 2


def test_a_lead_window_with_nothing_in_it_is_never_staged(env):
    """It drops, and it is not the refusal `realize` makes for a whole cycle.

    A cycle that assimilates nothing is indistinguishable from a healthy one
    downstream, which is why `realize` refuses it. A lead window with nothing
    costs one score at one lead, it is missing from every experiment in a
    comparison at once, and it is visible as a departure file that is not there.
    """
    config, _, archive = env
    stage_archive(archive, 1)
    assert not any(record["present"] for record in observations.observers(config, 9))
