"""Tier 0: the on-disk layout.

Small tests for a small module, and worth having because every other component
asks it where things are. A second spelling of any of these answers is a bug
that surfaces only as a missing file eight hours into a run.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from ackbar.paths import Paths, cycle_of

SITE = {"scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static",
        "root": "/ackbar"}
CONFIG = {"experiment": {"name": "e"},
          # `Paths` keys every directory by a cycle date, so it needs these.
          "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H"}}


@pytest.fixture
def paths():
    return Paths.of(CONFIG, SITE)


def test_the_two_roots_stay_separate(paths):
    # Different filesystems with different purge policies on every real
    # machine, which is why the site layer names them separately.
    assert paths.experiment_dir == Path("/out/e")
    assert paths.scratch_dir == Path("/scratch/e")


def test_output_is_named_by_the_date_of_the_cycle_that_produced_it(paths):
    # Cycle 1 is at `cycle.start`, so cycle 7 is six lengths later. `rst` is the
    # one directory whose contents are valid at the *next* analysis time; it is
    # still named for the cycle that wrote it, because a cycle writes only under
    # its own directory and that is what keeps cleanup a date comparison.
    assert paths.member_out("rst", 7, 3) == \
        Path("/out/e/run/20180421T000000Z/rst/mem003")
    assert paths.cycle_out("obs_out", 7) == Path("/out/e/obs_out/20180421T000000Z")


def test_the_products_are_one_file_per_member_per_cycle(paths):
    # `ana/` and `bkg/` are what the experiment is for: compressed, kept, and
    # never touched by cleanup.
    assert paths.product("ana", 2, 0) == \
        Path("/out/e/ana/20180416T000000Z/mem000.nc")
    assert paths.product("bkg", 2, 3) == \
        Path("/out/e/bkg/20180416T000000Z/mem003.nc")
    with pytest.raises(KeyError):
        paths.product("rst", 2, 0)


def test_cycle_zero_is_one_length_before_the_start(paths):
    # Where setup materializes the offline initial condition, which is what
    # makes cycle 1 an ordinary cycle rather than a special case.
    assert paths.date(0) == "20180414T000000Z"
    assert paths.date(1) == "20180415T000000Z"


def test_a_directory_name_maps_back_to_its_cycle(paths):
    # Cleanup sweeps `run/` rather than indexing one entry, so it has to turn a
    # name back into a number, and must not guess at one it did not write.
    assert cycle_of(paths, "20180421T000000Z") == 7
    assert cycle_of(paths, "20180414T000000Z") == 0
    assert cycle_of(paths, "20180421T120000Z") is None
    assert cycle_of(paths, "ledger.jsonl") is None


def test_the_control_is_mem000_and_not_a_special_case(paths):
    assert paths.member_out("ana", 1, 0).name == "mem000"


def test_an_unknown_subdirectory_is_an_error_not_a_new_directory(paths):
    with pytest.raises(KeyError):
        paths.sub("scratch")


def test_an_array_log_keeps_the_element_and_the_attempt_apart(paths):
    # %A_%a rather than %j, so a healed attempt lands beside the failed one
    # instead of overwriting the evidence.
    assert paths.job_log(2, "forecast", array=True).name == "forecast.%A_%a.out"
    assert paths.job_log(2, "da", array=False).name == "da.%j.out"


def test_a_member_level_sentinel_names_its_member(paths):
    assert paths.sentinel(2, "forecast", 3).name == "forecast.mem003.json"
    assert paths.sentinel(2, "da").name == "da.json"


def test_scratch_is_per_cycle_per_task_and_per_member(paths):
    assert paths.scratch(2, "forecast", 3) == \
        Path("/scratch/e/20180416T000000Z/forecast.mem003")
    assert paths.scratch(2, "da") == Path("/scratch/e/20180416T000000Z/da")


def test_ensure_creates_every_subdirectory(tmp_path):
    paths = Paths.of(CONFIG, {
        "scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o"),
    }).ensure()
    for name in ("cfg", "ana", "bkg", "obs_out", "run"):
        assert paths.sub(name).is_dir()
    assert paths.scratch_dir.is_dir()
    paths.ensure()  # idempotent


# --- the long forecast, which is the one thing keyed by lead -----------------

def test_a_lead_is_named_in_hours_and_sorts_by_name(paths):
    # `F###` rather than a valid time, and this is the one place in the tree
    # that goes that way: forecast skill is read against lead, grouped across
    # initializations, and the reference is the parent directory.
    from ackbar.paths import lead_name
    assert lead_name(timedelta(hours=3)) == "F003"
    assert lead_name(timedelta(days=5)) == "F120"
    assert sorted([lead_name(timedelta(hours=h)) for h in (120, 3, 24)]) == \
        ["F003", "F024", "F120"]


def test_a_lead_that_is_not_whole_hours_has_no_spelling(paths):
    # Refused rather than truncated. `F001` for ninety minutes would put two
    # leads in one directory and let the second overwrite the first, which is
    # the same failure an hour-resolution date format would have.
    from ackbar.paths import lead_name
    with pytest.raises(ValueError):
        lead_name(timedelta(minutes=90))


def test_the_forecast_product_is_keyed_by_initialization_and_lead(paths):
    # Both, because neither alone identifies it: two forecasts started five days
    # apart pass through the same valid time and are not the same state.
    assert paths.fcst_product(7, timedelta(days=5), 3) == \
        Path("/out/e/fcst/20180421T000000Z/F120/mem003.nc")


def test_the_forecast_departures_do_not_land_in_the_cycling_ones(paths):
    # `obs_out/<T>` is what the cycling analysis saw at T. A five day forecast
    # reaches that same T with a different number, and writing it there would
    # not merely confuse the two, it would overwrite the cycling departures for
    # every cycle the forecast covers.
    assert paths.fcst_obs(7, timedelta(days=5), 3) == \
        Path("/out/e/fcst/20180421T000000Z/obs/F120/mem003")
    assert paths.sub("obs_out") not in paths.fcst_obs(7, timedelta(days=5), 3).parents


def test_the_raw_trajectory_is_reaped_and_the_product_is_not(paths):
    # The states the model wrote are under `run/`, which is the tier cleanup
    # deletes; what `post.fcst` reduces them to is at the top level, which it
    # never touches.
    raw = paths.fcst_out(7, 3, timedelta(days=5))
    assert raw == Path("/out/e/run/20180421T000000Z/fcst/mem003/F120")
    assert paths.sub("run") in raw.parents
    assert paths.sub("run") not in paths.fcst_product(7, timedelta(days=5), 3).parents


def test_the_long_forecast_no_longer_writes_where_the_cycling_one_does(paths):
    """The collision the stub could not see.

    Both forecasts start from the same set and `mom6sis2.commit` deletes
    whatever it was not asked to claim, so one directory for the two of them is
    the cycling forecast's restart set being destroyed by the long one. It never
    fired because the only experiments configuring `forecast.extended` were
    graph and stub ones, where the stub wrote a distinctly named file into the
    same directory.
    """
    cycling = paths.member_out("rst", 7, 3)
    for lead in (timedelta(hours=24), timedelta(days=5)):
        assert paths.fcst_out(7, 3, lead) != cycling
        assert cycling not in paths.fcst_out(7, 3, lead).parents
