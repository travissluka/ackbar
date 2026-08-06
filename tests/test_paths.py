"""Tier 0: the on-disk layout.

Small tests for a small module, and worth having because every other component
asks it where things are. A second spelling of any of these answers is a bug
that surfaces only as a missing file eight hours into a run.
"""

from pathlib import Path

import pytest

from ackbar.paths import Paths

SITE = {"scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static",
        "root": "/ackbar"}
CONFIG = {"experiment": {"name": "e"}}


@pytest.fixture
def paths():
    return Paths.of(CONFIG, SITE)


def test_the_two_roots_stay_separate(paths):
    # Different filesystems with different purge policies on every real
    # machine, which is why the site layer names them separately.
    assert paths.experiment_dir == Path("/out/e")
    assert paths.scratch_dir == Path("/scratch/e")


def test_output_is_named_by_the_cycle_that_produced_it(paths):
    assert paths.member_out("rst", 7, 3) == Path("/out/e/rst/7/mem003")


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
    assert paths.scratch(2, "forecast", 3) == Path("/scratch/e/2/forecast.mem003")
    assert paths.scratch(2, "da") == Path("/scratch/e/2/da")


def test_ensure_creates_every_subdirectory(tmp_path):
    paths = Paths.of(CONFIG, {
        "scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o"),
    }).ensure()
    for name in ("cfg", "ledger", "stats", "log", "rst", "bkg", "ana",
                 "obs_out", "done"):
        assert paths.sub(name).is_dir()
    assert paths.scratch_dir.is_dir()
    paths.ensure()  # idempotent
