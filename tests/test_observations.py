"""Tier 0: which observers ran, and the record of the ones that did not.

The behaviour under test is a decision, not a computation: an observation file
that is not there drops its observer and the cycle carries on. That is right for
any real archive and it is also how an experiment quietly assimilates nothing,
so every test here is about the record being complete and being obeyed, and
about the one case that is not a gap: a window no observer at all has a file for.
"""

import json

import pytest

from conftest import PATHS_CYCLE

from ackbar import observations
from ackbar.paths import Paths

CONFIG = {
    "experiment": {"name": "e"},
    "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H", "count": 3},
    "observations": [
        {
            "obs space": {
                "name": "adt_3a",
                "obsdatain": {"engine": {"obsfile": "ARCHIVE/{{cycle}}/adt.nc4"}},
                "obsdataout": {"engine": {"obsfile": "OUT/{{cycle}}/adt.nc4"}},
                "obs perturbations seed": "{{seed}}",
            },
            "obs operator": {"name": "ADT"},
        },
        {
            "obs space": {
                "name": "sst_noaa19",
                "obsdatain": {"engine": {"obsfile": "ARCHIVE/{{cycle}}/sst.nc4"}},
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
        for side, root in (("obsdatain", tmp_path / "obs"),
                           ("obsdataout", tmp_path / "out")):
            engine = entry["obs space"][side]["engine"]
            engine["obsfile"] = engine["obsfile"].replace(
                "ARCHIVE", str(root)).replace("OUT", str(root))
    paths = Paths(experiment="e", output_root=tmp_path / "o",
                  scratch_root=tmp_path / "s", **PATHS_CYCLE).ensure()
    return config, paths, tmp_path / "obs"


def stage_archive(archive, cycle, names=("adt.nc4", "sst.nc4")):
    for name in names:
        target = archive / str(cycle) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()


def test_an_observer_with_a_file_is_present_and_one_without_is_not(env):
    config, _, archive = env
    stage_archive(archive, 1, names=("sst.nc4",))
    found = {r["name"]: r["present"] for r in observations.observers(config, 1)}
    assert found == {"adt_3a": False, "sst_noaa19": True}


def test_job_time_tokens_are_rendered_before_the_file_is_looked_for(env):
    # The whole question is which file this cycle needs, so an unrendered
    # `{{cycle}}` would ask about a path that no cycle ever reads.
    config, _, archive = env
    stage_archive(archive, 2)
    assert all(r["present"] for r in observations.observers(config, 2))
    assert all(not r["present"] for r in observations.observers(config, 3))


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
    stage_archive(archive, 1, names=("sst.nc4",))
    observations.realize(config, paths, 1)

    written = json.loads(paths.observer_list(1).read_text())
    assert written["cycle"] == 1
    assert [r["name"] for r in written["observers"]] == ["adt_3a", "sst_noaa19"]
    assert [r["present"] for r in written["observers"]] == [False, True]


def test_the_list_records_which_file_each_observer_read(env):
    # A comparison between two experiments is a comparison of what they read,
    # and the configured path alone does not say what it resolved to.
    config, paths, archive = env
    stage_archive(archive, 1)
    observations.realize(config, paths, 1)
    inputs = [r["input"] for r in observations.read(paths, 1)]
    assert all(str(archive / "1") in path for path in inputs)


def test_a_required_observer_with_no_file_fails_the_cycle(env):
    config, paths, archive = env
    config["observations"][0]["obs space"]["$required"] = True
    stage_archive(archive, 1, names=("sst.nc4",))
    with pytest.raises(observations.ObservationError, match="adt_3a"):
        observations.realize(config, paths, 1)
    assert not paths.observer_list(1).exists()


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
    with pytest.raises(observations.ObservationError, match="cycle 2 has one"):
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
    stage_archive(archive, 1, names=("sst.nc4",))
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
    stage_archive(archive, 1, names=("sst.nc4",))
    observations.realize(config, paths, 1)

    stage_archive(archive, 1, names=("adt.nc4",))
    assert [r["name"] for r in observations.selected(config, paths, 1)] == \
        ["sst_noaa19"]


def test_hofx_without_a_staged_list_says_which_task_should_have_written_it(env):
    config, paths, _ = env
    with pytest.raises(observations.ObservationError, match="stage.obs"):
        observations.selected(config, paths, 1)
