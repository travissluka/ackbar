"""Tier 0: what a job does, without a job.

Every body here is python, so the interesting parts run in-process: the skip
rule, the input check, commit by rename, the cleanup gate, and the halt flag.
What Slurm makes of the results is tier 2's problem.
"""

import json
from pathlib import Path

import netCDF4
import numpy as np
import pytest

from ackbar import ledger, run, soca, submit
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph.build import build_graph
from ackbar.graph.tasks import BY_NAME
from ackbar.paths import Paths

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

#: The simulated variable in the observation files the `post.obs` tests write.
#: The fixture experiment flies altimeters, and `post` reads whatever variable
#: it finds rather than a name it was told, so only the two have to agree.
VARIABLE = "absoluteDynamicTopography"


@pytest.fixture(scope="module")
def base():
    layers = resolve_layers(EXPERIMENTS / "stub_letkf.yaml", LAYERS)
    keys = merge_keys(load_schema())
    return resolve(merge_layers(layers, keys),
                   {"scratch_root": "/scratch", "output_root": "/out"})


@pytest.fixture
def env(tmp_path, base):
    """A created experiment directory and a config that runs instantly."""
    config = json.loads(json.dumps(base))
    config["model"]["stub"]["seconds"] = 0
    config["ensemble"]["size"] = 2
    site = {"scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o")}
    paths = Paths.of(config, site).ensure()
    for member in (1, 2):
        target = paths.member_out("rst", 0, member) / "restart.stub"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ic\n")
    return config, site, paths


def do(env, cycle, task, member=None):
    config, site, paths = env
    return run.run_task(config, site, paths, cycle, task, member)


# --- fault selectors ---------------------------------------------------------

@pytest.mark.parametrize("pattern,cycle,task,member,expected", [
    ("1.forecast.1", 1, "forecast", 1, True),
    ("1.forecast.1", 1, "forecast", 2, False),
    ("1.forecast.1", 2, "forecast", 1, False),
    ("*.da.*", 7, "da", 0, True),
    ("*.da", 7, "da", 0, True),
    ("2.*.*", 2, "post.state", 3, True),
    ("1.forecast.*", 1, "forecast", 9, True),
])
def test_a_selector_addresses_exactly_the_jobs_it_names(
        pattern, cycle, task, member, expected):
    assert run.selector_matches(pattern, cycle, task, member) is expected


def test_a_dotted_task_name_is_a_task_and_not_a_member():
    # `2.forecast.ext` names a task. Splitting on every dot would read `ext` as
    # a member index and silently match nothing.
    assert run.selector_matches("2.forecast.ext", 2, "forecast.ext", 0)
    assert not run.selector_matches("2.forecast", 2, "forecast.ext", 0)
    assert run.selector_matches("*.b.corr_vt.*", 3, "b.corr_vt", 0)


def test_a_selector_with_no_task_is_rejected_rather_than_guessed():
    with pytest.raises(run.TaskError, match="at least"):
        run.selector_matches("2", 2, "da", 0)


# --- the stub's data flow ----------------------------------------------------

def test_the_forecast_chain_is_what_makes_restart_continuity_testable(env):
    config, _, paths = env
    inputs, outputs = run.stub_io(config, paths, "forecast", 2, 1)
    # With an analysis in the cycle the forecast starts from the writeback's
    # product, not from the background it was computed against.
    assert inputs == [paths.member_out("ana", 2, 1) / "restart.stub"]
    assert outputs == [paths.member_out("rst", 2, 1) / "restart.stub"]


def test_a_free_run_hands_restarts_straight_from_one_cycle_to_the_next(env):
    config, _, paths = env
    free = json.loads(json.dumps(config))
    free["solver"]["name"] = "none"
    inputs, _ = run.stub_io(free, paths, "forecast", 4, 1)
    assert inputs == [paths.member_out("rst", 3, 1) / "restart.stub"]


def test_the_analysis_consumes_every_member(env):
    config, _, paths = env
    inputs, outputs = run.stub_io(config, paths, "da", 2, None)
    assert len(inputs) == 2 and len(outputs) == 2


def test_a_task_with_nothing_to_fake_declares_no_artifacts(env):
    config, _, paths = env
    assert run.stub_io(config, paths, "verify", 1, None) == ([], [])


# --- running one job ---------------------------------------------------------

def test_a_completed_task_writes_its_outputs_and_its_sentinel(env):
    _, _, paths = env
    do(env, 1, "da")
    assert (paths.member_out("ana", 1, 1) / "incr.stub").exists()
    sentinel = json.loads(paths.sentinel(1, "da").read_text())
    assert sentinel["task"] == "da" and sentinel["restarts"] == 0


def test_nothing_partial_is_left_behind(env):
    _, _, paths = env
    do(env, 1, "da")
    assert not list(paths.experiment_dir.rglob("*.partial"))


def test_a_missing_input_names_the_file_its_producer_should_have_written(env):
    # The only thing that can catch a producer which exited 0 having written
    # nothing. Slurm sees a COMPLETED job either way.
    with pytest.raises(run.TaskError, match="missing 2 input"):
        do(env, 3, "da")


def test_a_body_that_writes_nothing_is_a_failure_not_a_success(env, monkeypatch):
    # The mirror of the input check above, on the other side of the body. A
    # producer that exits 0 having written nothing is caught here, in the job
    # that did it, rather than one edge later in whatever tried to read it.
    _, _, paths = env
    monkeypatch.setattr(run, "_stub", lambda *a, **k: None)
    with pytest.raises(run.TaskError, match="without writing what it declares"):
        do(env, 1, "da")
    assert not paths.sentinel(1, "da").exists()


def test_the_skip_rule_and_the_completion_rule_agree(env, monkeypatch):
    # The specific corruption: a task marked complete in a state where the skip
    # rule would refuse to skip it. Downstream reads the sentinel and runs, so
    # the disagreement is only ever resolved the wrong way.
    _, _, paths = env
    monkeypatch.setattr(run, "_stub", lambda *a, **k: None)
    with pytest.raises(run.TaskError):
        do(env, 1, "da")
    monkeypatch.undo()
    # Nothing was recorded, so the retry does the work rather than skipping it.
    do(env, 1, "da")
    assert (paths.member_out("ana", 1, 1) / "incr.stub").exists()


def test_a_deferred_task_declares_nothing_and_so_still_completes(env):
    # `verify` has no body yet. It must stay a success: it is a leaf that writes
    # nothing anything reads, and failing it would stop every cycle.
    config, _, paths = env
    config["model"]["name"] = "mom6sis2"
    assert run.deferred_task(config, "verify")
    assert run.kind_of(config, "verify").name == "deferred"
    assert run.kind_of(config, "verify").io(config, paths, "verify", 1, None) \
        == ([], [])


# --- the dispatch table ------------------------------------------------------

@pytest.mark.parametrize("model", ["stub", "mom6sis2"])
def test_every_task_the_graph_can_build_has_a_kind(env, model):
    # What the table exists for: one row decides a job's predicate, its declared
    # artifacts and its body together, so a task cannot be one kind to `task_io`
    # and another to `run_task`. A task nothing matches would have fallen
    # through to the stub silently.
    config, _, _ = env
    config["model"]["name"] = model
    for task in BY_NAME:
        assert run.kind_of(config, task).name


def test_the_body_a_task_runs_is_the_one_that_declared_its_outputs(env):
    # The pairing itself, asserted rather than assumed: `task_io` answers out of
    # the same row `run_task` takes its body from.
    config, _, paths = env
    for task in ("da", "forecast", "recenter", "cleanup"):
        kind = run.kind_of(config, task)
        assert kind.io(config, paths, task, 2, 1) \
            == run.task_io(config, paths, task, 2, 1)


def test_a_finished_task_is_skipped_rather_than_repeated(env):
    _, _, paths = env
    do(env, 1, "da")
    do(env, 1, "recenter", 1)
    output = paths.member_out("ana", 1, 1) / "incr.rc.stub"
    stamp = output.stat().st_mtime_ns
    do(env, 1, "recenter", 1)
    assert output.stat().st_mtime_ns == stamp


def test_an_output_without_its_sentinel_is_redone(env):
    # The weaker rule, skip if the output exists, is what v2 had. A kill during
    # a restart write leaves a truncated file that exists, and under
    # skip-if-exists the retry declines to redo the task it was launched for.
    _, _, paths = env
    do(env, 1, "da")
    do(env, 1, "recenter", 1)
    paths.sentinel(1, "recenter", 1).unlink()
    output = paths.member_out("ana", 1, 1) / "incr.rc.stub"
    output.write_bytes(b"truncated")
    do(env, 1, "recenter", 1)
    assert output.read_bytes() != b"truncated"


def test_scratch_is_removed_on_success_and_kept_on_failure(env):
    _, _, paths = env
    do(env, 1, "da")
    assert not paths.scratch(1, "da").exists()

    with pytest.raises(run.TaskError):
        do(env, 3, "da")
    assert paths.scratch(3, "da").exists()


def test_a_deferred_task_says_so_rather_than_pretending(env):
    """A declared task with no body announces itself instead of exiting 0 quietly.

    `DEFERRED` is down to `b.corr_vt` and `verify`. `forecast.ext` used to be here and
    is not any more: it runs the real model, for `forecast.extended.length`,
    writing at `forecast.extended.slots` into `run/<init>/fcst/`.
    """
    config, site, paths = env
    config = json.loads(json.dumps(config))
    config["model"] = {"name": "mom6sis2"}
    assert run.deferred_task(config, "verify")
    assert not run.deferred_task(config, "forecast.ext")


def test_a_long_forecast_with_nothing_configured_is_an_error_not_a_traceback(env):
    """Unreachable through the graph, which is exactly why it needs a message.

    The node only exists when `forecast.extended` is set, so this can only be
    reached by hand or by a heal against an edited config. Without the guard the
    symptom is an AttributeError from inside the path layer, which says nothing
    about what is actually wrong.
    """
    config, site, paths = env
    config = json.loads(json.dumps(config))
    config["model"] = {"name": "mom6sis2"}
    config.pop("forecast", None)
    with pytest.raises(run.TaskError, match="forecast.extended is not configured"):
        run.run_task(config, site, paths, 1, "forecast.ext", 1)


# --- cleanup -----------------------------------------------------------------

def _complete_cycle(env, cycle, *, reduced=True):
    """The restart set *and* the proof every declared consumer is done with it.

    `post.state` reduces `ana/<n>`, is a leaf off the same forecast cleanup keys
    on, and is therefore in cleanup's proof for exactly the reason hofx is. A
    helper that writes the restarts alone would describe a cycle that has not
    finished, so every test built on it would be testing the refusal.

    *reduced* is how the one test that wants that refusal asks for it.
    """
    _, _, paths = env
    for member in (1, 2):
        target = paths.member_out("rst", cycle, member) / "restart.stub"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        if reduced:
            proof = paths.sentinel(cycle, "post.state", member)
            proof.parent.mkdir(parents=True, exist_ok=True)
            proof.write_text("{}")


def test_cleanup_drops_only_what_no_forecast_can_still_read(env):
    _, _, paths = env
    _complete_cycle(env, 1)
    _complete_cycle(env, 2)
    do(env, 3, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()
    assert paths.cycle_out("rst", 2).exists()


def test_cleanup_reaps_the_analysis_a_cycle_before_the_restarts(env):
    """`ana/` grows at the same rate as `rst/` and is released a cycle sooner.

    In a DA run the forecast starts from `ana/<n>/mem###`, so writeback leaves a
    whole restart set there per member per cycle. Reaping `rst/` alone is an
    experiment that cycles happily for a week and fills the disk in the second.

    The horizon is its own, though, and that is the point of this test. `ana/n`
    is read by `forecast(n)` and by nothing else, so `rst/n` existing, which is
    the proof this task already requires, says nothing can read it again.
    `rst/n` and `slot/n` are read by cycle *n+1* and have to wait a cycle
    longer. On a twenty member run that difference is a whole cycle of member
    restart sets held for nothing.
    """
    _, _, paths = env
    for cycle in (1, 2):
        _complete_cycle(env, cycle)
        for member in (1, 2):
            analysis = paths.member_out("ana", cycle, member) / "MOM.res.nc"
            analysis.parent.mkdir(parents=True, exist_ok=True)
            analysis.write_bytes(b"x")

    do(env, 3, "cleanup")
    assert not paths.cycle_out("ana", 1).exists()
    # Cycle 2 is the horizon cycle: its restarts are kept, because cycle 3 has
    # still to read them, and its analysis is not, because cycle 2's own
    # forecast is the only thing that ever wanted it and has demonstrably run.
    assert not paths.cycle_out("ana", 2).exists()
    assert paths.cycle_out("rst", 2).is_dir()


def test_cleanup_reaps_the_sub_window_states_too(env):
    """`slot/` grows faster than either of the other two.

    A slot state is a full 3D field, so a six slot window is six times the
    per-cycle output of the restarts it sits beside. It is safe under the same
    proof: those are `forecast(drop)`'s states, read by `da(drop + 1)`, and the
    horizon cycle's `rst` existing means that analysis and the forecast after it
    are both done with them.
    """
    _, _, paths = env
    for cycle in (1, 2):
        _complete_cycle(env, cycle)
        for member in (1, 2):
            for hour in ("20180415T060000Z", "20180415T120000Z"):
                slot = paths.member_out("slot", cycle, member) / hour / "MOM.res.nc"
                slot.parent.mkdir(parents=True, exist_ok=True)
                slot.write_bytes(b"x")

    do(env, 3, "cleanup")
    assert not paths.cycle_out("slot", 1).exists()
    assert paths.cycle_out("slot", 2).exists()


def test_a_forecast_declares_every_state_it_was_asked_to_write(env):
    """The states are declared by file rather than by directory.

    A slot directory that exists proves nothing about what is in it, and the
    skip rule and `ackbar validate` both read this list: an output declared as a
    directory is a cycle that skips itself after writing nothing.
    """
    config, _, paths = env
    config["forecast"] = {"slots": "PT6H"}
    _, outputs = run.task_io(config, paths, "forecast", 2, 1)

    assert len(outputs) == 5           # the restart set, plus four slots
    assert outputs[0].name == "restart.stub"
    assert [p.parent.name for p in outputs[1:]] == [
        "20180416T060000Z", "20180416T120000Z",
        "20180416T180000Z", "20180417T000000Z"]


def test_a_forecast_that_was_asked_for_no_states_declares_none(env):
    config, _, paths = env
    _, outputs = run.task_io(config, paths, "forecast", 2, 1)
    assert len(outputs) == 1


def test_cleanup_keeps_everything_when_the_proof_is_incomplete(env):
    # Keyed off artifacts rather than job state, so a retried cleanup cannot
    # conclude that a resubmitted consumer is gone and delete what it is about
    # to read.
    _, _, paths = env
    _complete_cycle(env, 1)
    _complete_cycle(env, 2)
    (paths.member_out("rst", 2, 2) / "restart.stub").unlink()
    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists()


def test_a_cleanup_that_refused_once_tries_again(env):
    """Refusing writes a sentinel, and skipping on it leaks for good.

    A cleanup declines while the cycle it is keeping is incomplete, which is
    exactly the state a failure leaves behind. If that run were allowed to count
    as done, the restarts it declined to delete would never be revisited.
    """
    _, _, paths = env
    _complete_cycle(env, 1)
    _complete_cycle(env, 2)
    missing = paths.member_out("rst", 2, 2) / "restart.stub"
    missing.unlink()
    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists()

    missing.write_bytes(b"healed")
    do(env, 3, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()


def test_a_cleanup_that_refused_collects_the_arrears_on_its_next_pass(env):
    """A refusal has to be a delay and not a leak.

    Each cleanup runs once and nothing revisits its cycle, so indexing a single
    directory means one incomplete cycle strands its predecessor's state for the
    life of the experiment. At gom_4km with twenty members that is tens of
    gigabytes a cycle, and it accumulates in exactly the situation that produced
    it: a disk too full to finish a forecast.
    """
    _, _, paths = env
    for cycle in (1, 2, 3):
        _complete_cycle(env, cycle)

    # Cycle 2 is incomplete when cleanup(3) looks, so cycle 1 survives it.
    missing = paths.member_out("rst", 2, 2) / "restart.stub"
    missing.unlink()
    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists()

    # By the time cleanup(4) runs the cycle has been healed. Cycle 1 is below
    # its horizon rather than at it, and is collected anyway.
    missing.write_bytes(b"healed")
    do(env, 4, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()
    assert not paths.cycle_out("rst", 2).exists()
    assert paths.cycle_out("rst", 3).exists()


def test_cleanup_reaps_behind_the_long_forecast_that_is_never_finished(env):
    """The refusal that never ends, which is what the walk-back is for.

    `submit` is released by the *cycling* forecast, while `forecast.ext`,
    `hofx.ext` and `post.fcst` run on past it as leaves. So cycle n's cleanup
    starts while cycle n-1's long forecast is still integrating, and it is not
    bad luck: the two are released by the same event and one of them runs for
    days. A horizon fixed at `cycle - 1` therefore never proves, and the sweep
    that is supposed to collect the arrears has no pass that reaches it.

    Measured on `osse25-4dletkf` before this: twenty one cycles, twenty one
    refusals with the same three sentinels missing, and 5.9 GB per cycle held
    for the life of the run. The only successful pass in the experiment was the
    one after it had been paused long enough for the leaves to land.
    """
    config, _, paths = env
    config["forecast"] = {"extended": {"length": "P7D", "every": "PT24H"}}
    for cycle in (1, 2, 3):
        _complete_cycle(env, cycle)

    # Every cycle but the newest has finished its long forecast, which is what
    # a run in flight looks like at every moment of its life.
    for cycle in (1, 2):
        for task in ("forecast.ext", "hofx.ext", "post.fcst"):
            sentinel = paths.sentinel(cycle, task, 1)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("{}")

    do(env, 4, "cleanup")

    # Cycle 3 cannot be proven, so nothing is reaped behind it. Cycle 2 can, so
    # everything at or below cycle 1 goes: one cycle of state held for the delay
    # rather than every cycle of it held for good.
    assert not paths.cycle_out("rst", 1).exists()
    assert paths.cycle_out("rst", 2).exists()
    assert paths.cycle_out("rst", 3).exists()


def test_cleanup_leaves_an_old_cycle_whose_long_forecast_is_still_reading_it(env):
    """The arrears are proved one at a time, not by the horizon alone.

    Extended forecasts are leaves on their own cadence, so several are in flight
    at once and they finish out of order. A horizon at cycle 4 says the chain is
    past cycle 3; it says nothing about whether cycle 1's `post.fcst` is still
    reducing `run/1/fcst/`. The sweep reaches every cycle at or below the drop,
    and `_reapable` uses `shutil.rmtree` with no `ignore_errors`, so without a
    per-cycle proof this deletes a trajectory out from under a running job.

    Not reachable before the walk-back landed: cleanup refused on every cycle of
    every experiment with an extended forecast, so the arrears sweep never ran.
    """
    config, _, paths = env
    config["forecast"] = {"extended": {"length": "P7D", "every": "PT24H"}}
    for cycle in (1, 2, 3, 4):
        _complete_cycle(env, cycle)

    # Cycle 1's long forecast has integrated and been observed, but its
    # reduction has not finished. Every later cycle's chain is done, so the
    # horizon is well past it and the sweep reaches it.
    for cycle in (1, 2, 3):
        for task in ("forecast.ext", "hofx.ext", "post.fcst"):
            if cycle == 1 and task == "post.fcst":
                continue
            sentinel = paths.sentinel(cycle, task, 1)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("{}")

    do(env, 5, "cleanup")

    assert paths.cycle_out("rst", 1).exists(), (
        "cycle 1's states went while its own post.fcst was still reading them")
    assert not paths.cycle_out("rst", 2).exists()


def test_a_keep_rule_pins_a_restart_every_so_often(env):
    """What makes a long experiment branchable.

    Without it the states left when a fifty cycle run finishes are cycles 49 and
    50, so re-running a segment after a mistake found at cycle 40 means starting
    from cycle 0.

    A duration rather than a cycle count, so the rule survives a change of cycle
    length and can say "every three days", which v2's `SAVE_RST_REGEX` over the
    directory name could not.
    """
    config, _, paths = env
    config["cleanup"] = {"keep_every": "P2D"}   # cycles are PT24H here
    for cycle in (1, 2, 3, 4):
        _complete_cycle(env, cycle)

    do(env, 4, "cleanup")
    assert not paths.cycle_out("rst", 2).exists()
    assert paths.cycle_out("rst", 1).exists()
    assert paths.cycle_out("rst", 3).exists()
    # And the initial condition, which is cycle 0 and is what a variant would
    # actually be branched from.
    assert paths.cycle_out("rst", 0).exists()


def test_a_pin_holds_the_restart_and_not_the_states_beside_it(env):
    """`ana` and `slot` at a pinned date carry nothing the pinned `rst` does not.

    A pinned cycle exists so an experiment can be branched from it, and that
    needs the set the next forecast would start from. Pinning the other two as
    well would hold the fastest-growing directories open for the life of the run
    and buy nothing.
    """
    config, _, paths = env
    config["cleanup"] = {"keep_every": "P1D"}   # every cycle is pinned
    for cycle in (1, 2, 3):
        _complete_cycle(env, cycle)
        for member in (1, 2):
            target = paths.member_out("ana", cycle, member) / "restart.stub"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")

    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists()
    assert not paths.cycle_out("ana", 1).exists()


def test_cleanup_collects_the_scratch_a_failure_left_behind(env):
    """The one thing nothing else was collecting.

    A task deletes its own scratch on success and keeps it on failure, which is
    right: it is the whole debugging trace. Nothing then removed it, so every
    failed attempt of a long experiment left a full model run directory behind
    for the life of the run.
    """
    _, _, paths = env
    for cycle in (1, 2):
        _complete_cycle(env, cycle)
    for cycle in (1, 2):
        stranded = paths.scratch(cycle, "forecast", 1)
        stranded.mkdir(parents=True, exist_ok=True)
        (stranded / "MOM_input").write_bytes(b"x")

    do(env, 3, "cleanup")
    assert not paths.scratch(1, "forecast", 1).exists()
    assert paths.scratch(2, "forecast", 1).exists()


def test_the_rolling_window_is_a_setting(env):
    # One completed cycle behind the current one is the tightest correct answer
    # and the default; an experiment being healed repeatedly wants headroom.
    config, _, paths = env
    config["cleanup"] = {"keep_cycles": 2}
    for cycle in (1, 2, 3):
        _complete_cycle(env, cycle)

    do(env, 4, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()
    assert paths.cycle_out("rst", 2).exists()


def test_cleanup_waits_for_the_hofx_reading_the_set_it_would_delete(env):
    """`hofx` is a leaf, so nothing upstream of the next cycle waits for it.

    `hofx(n)` reads `rst/<n-1>` and `cleanup(n+1)` drops exactly that. Both are
    released by the same forecast completing, and on one node they cannot even
    run at once, so one of them runs second by construction. This is the free
    run shape, which is also the OSSE truth run.
    """
    config, _, paths = env
    config["solver"] = {"name": "none", "window": {"type": "3d"}}
    _complete_cycle(env, 1)
    _complete_cycle(env, 2)

    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists()

    paths.sentinel(2, "hofx").parent.mkdir(parents=True, exist_ok=True)
    paths.sentinel(2, "hofx").write_text("{}")
    do(env, 3, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()


def test_cleanup_waits_for_the_reduction_of_the_analysis_it_would_delete(env):
    """The `ana` offset puts this task and `post.state` on the same cycle.

    `ana` is reaped a cycle earlier than the rest of `REAPED`, which makes
    `cleanup(n)` the one that deletes `run/<n-1>/ana` and `post.state(n-1)` the
    one that reduces it. Both are released by `forecast(n-1)`, so nothing orders
    them.

    Losing that race is the reason this is a test rather than a comment: `_post`
    reads a missing `ana/mem###/MOM.res.nc` as "this cycle had no analysis",
    which is how a free run is spelled, so the cycle's headline product would go
    missing with nothing raised and no heal able to rebuild it.
    """
    _, _, paths = env
    _complete_cycle(env, 1)
    _complete_cycle(env, 2, reduced=False)
    for cycle in (1, 2):
        for member in (1, 2):
            analysis = paths.member_out("ana", cycle, member) / "MOM.res.nc"
            analysis.parent.mkdir(parents=True, exist_ok=True)
            analysis.write_bytes(b"x")

    do(env, 3, "cleanup")
    assert paths.cycle_out("ana", 2).is_dir()
    # And nothing else went either, because the proof is all or nothing.
    assert paths.cycle_out("rst", 1).is_dir()

    for member in (1, 2):
        proof = paths.sentinel(2, "post.state", member)
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text("{}")
    do(env, 3, "cleanup")
    assert not paths.cycle_out("ana", 2).exists()


def test_cleanup_waits_for_the_long_forecast_still_integrating(env):
    """A long forecast outlives the cycle it started from by construction.

    That is the whole reason its cadence is a setting. A running model has
    already opened its restarts, but a requeued one rebuilds `INPUT/` from
    scratch and would find the tree gone.

    And not only the forecast: `post.fcst` reduces its trajectory afterwards,
    out of `run/<n>/fcst/`, which this task reaps. Both are leaves, so nothing
    in the graph orders cleanup after either of them, and waiting on the
    forecast alone would delete the states in the window between the model
    exiting and its own reduction reading them.
    """
    config, _, paths = env
    config["forecast"] = {"extended": {"length": "P7D", "every": "PT24H"}}
    _complete_cycle(env, 1)
    _complete_cycle(env, 2)

    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists()

    def finish(task):
        for cycle in (1, 2):
            sentinel = paths.sentinel(cycle, task, 1)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("{}")

    finish("forecast.ext")
    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists(), \
        "the model exited but nothing has reduced its trajectory yet"

    finish("post.fcst")
    do(env, 3, "cleanup")
    assert paths.cycle_out("rst", 1).exists(), \
        "the departures have still to be computed from the same trajectory"

    finish("hofx.ext")
    do(env, 3, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()


def test_stats_reruns_rather_than_reporting_the_run_it_replaced(env):
    """The harvest describes a cycle, and a heal changes what the cycle is."""
    _, _, paths = env
    do(env, 1, "stats")
    paths.stats_file(1).write_text('{"cycle": 1, "stale": true}')
    do(env, 1, "stats")
    assert "stale" not in json.loads(paths.stats_file(1).read_text())


# --- the submitter body ------------------------------------------------------

def test_the_halt_flag_stops_cycling_without_looking_like_a_failure(env):
    # `pause` is the normal way to stop, so the submitter exits 0 and the graph
    # drains. Failing here would make a deliberate stop indistinguishable from
    # a broken one.
    _, _, paths = env
    paths.halt_flag.write_text("")
    assert do(env, 1, "submit") == 0
    assert ledger.read(paths) == []


def test_the_last_cycle_submits_nothing(env):
    config, _, paths = env
    assert do(env, config["cycle"]["count"], "submit") == 0
    assert ledger.read(paths) == []


def test_a_cycle_already_in_the_ledger_is_not_submitted_again(env):
    # The ledger is the authority, and it is the check that survives a marker
    # file being tidied away by hand. A requeued submitter reaching here twice
    # is what this prevents, and what it left behind is the whole cycle.
    config, _, paths = env
    tasks = [n.task for n in build_graph(config).cycle_nodes(2)]
    for task in tasks:
        ledger.append(paths, cycle=2, task=task, members=(), attempt=1,
                      job_id=1, dependency=None)
    assert do(env, 1, "submit") == 0
    assert len(ledger.read(paths)) == len(tasks)


def test_a_cycle_only_partly_in_the_ledger_is_finished_rather_than_skipped(
        env, monkeypatch):
    """The state a submitter that died partway through leaves behind.

    Asked of the cycle as a whole, this reads as done: the submitter exits 0,
    the unsubmitted nodes have no job id, so nothing reports them failed and no
    heal finds them broken. The experiment wedges while reading as healthy, and
    no command repairs it: `resume` submits the cycle after this one, whose
    parents were never submitted, and re-submitting the cycle by hand duplicates
    the tasks that did go.
    """
    config, _, paths = env
    ledger.append(paths, cycle=2, task="da", members=(), attempt=1, job_id=1,
                  dependency=None)

    asked = {}

    def record(config, site, paths, cycle, *, graph=None, tasks=None, **kw):
        asked.update(cycle=cycle, tasks=tasks)
        return []

    monkeypatch.setattr(submit, "submit_cycle", record)
    do(env, 1, "submit")

    tasks = {n.task for n in build_graph(config).cycle_nodes(2)}
    assert asked["cycle"] == 2
    # Everything but the one already there, and `da` is not submitted twice.
    assert set(asked["tasks"]) == tasks - {"da"}


# --- the harvest -------------------------------------------------------------

def test_every_cycle_leaves_a_stats_file(env):
    _, _, paths = env
    do(env, 2, "stats")
    assert json.loads(paths.stats_file(2).read_text())["cycle"] == 2


# --- which body runs, and what it reads --------------------------------------
#
# The dispatch predicates, and `analysis_state` in particular. Everything about
# a cycle that assimilated nothing runs through it, and it is the one place
# where "produced nothing" and "had nothing to do" are told apart.

@pytest.fixture
def var(base, tmp_path):
    """A variational experiment on a real model, with one staged observer."""
    config = json.loads(json.dumps(base))
    config["model"] = {"name": "persistence"}
    config["solver"] = {"name": "variational"}
    config.pop("ensemble", None)
    site = {"scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o")}
    return config, Paths.of(config, site).ensure()


def stage(paths, cycle, present):
    from ackbar import observations
    observations.write(paths, cycle, [
        {"name": "adt_3a", "required": False, "input": "/in.nc4",
         "output": str(paths.cycle_out("obs_out", cycle) / "adt.nc4"),
         "present": present},
    ])


@pytest.mark.parametrize("model,solver,task,expected", [
    ("mom6sis2", "variational", "da", True),
    ("persistence", "variational", "da", True),
    # LETKF is a different application with a different document. Until it
    # lands it has to reach the stub's "no implementation yet" rather than run
    # the variational one.
    ("mom6sis2", "letkf", "da", True),
    ("mom6sis2", "variational", "writeback", False),
    ("stub", "variational", "da", False),
])
def test_only_the_analyses_that_exist_are_dispatched(base, model, solver, task, expected):
    config = json.loads(json.dumps(base))
    config["model"] = {"name": model}
    config["solver"] = {"name": solver}
    assert run.real_analysis(config, task) is expected


@pytest.mark.parametrize("model,expected", [
    ("mom6sis2", True), ("persistence", True), ("stub", False),
])
def test_writeback_is_solver_independent(base, model, expected):
    """It reads a state and a background and writes a restart set.

    None of that depends on how the state was arrived at, so unlike the
    analysis this asks about the model and nothing else.
    """
    for solver in ("variational", "letkf"):
        config = json.loads(json.dumps(base))
        config["model"] = {"name": model}
        config["solver"] = {"name": solver}
        assert run.real_writeback(config, "writeback") is expected
        assert run.real_writeback(config, "forecast") is False


def test_the_analysis_corrects_the_previous_cycles_forecast(var):
    """Not `restart_source`, which with a solver names this task's own output."""
    _, paths = var
    assert run.analysis_background(paths, 3).name == "mem000"
    assert run.analysis_background(paths, 3).parent == paths.cycle_out("rst", 2)


def test_a_cycle_with_a_staged_observer_expects_an_analysis(var):
    config, paths = var
    stage(paths, 1, present=True)
    state = run.analysis_state(config, paths, 1)
    assert state.parent == paths.member_out("ana", 1, 0) / "analysis"
    assert state.name.startswith("ocn.ana.an.")
    # Writeback reads it, and its own output is the restart set's stamp.
    inputs, outputs = run.task_io(config, paths, "writeback", 1, 0)
    assert state in inputs
    assert outputs == [paths.member_out("ana", 1, 0) / "coupler.res"]


def test_a_cycle_that_assimilated_nothing_expects_no_analysis(var):
    """The archive gap. Writeback hands the background across unchanged.

    Declaring an output the analysis will never write would leave the skip rule
    unable to tell a finished cycle from an unfinished one, forever.
    """
    config, paths = var
    stage(paths, 1, present=False)
    assert run.analysis_state(config, paths, 1) is None
    assert run.task_io(config, paths, "da", 1, None)[1] == []
    inputs, outputs = run.task_io(config, paths, "writeback", 1, 0)
    assert inputs == [paths.member_out("rst", 0, 0) / "coupler.res"]
    assert outputs == [paths.member_out("ana", 1, 0) / "coupler.res"]


def test_before_the_observers_are_staged_nothing_is_declared(var):
    """The skip rule must not fire on a task whose outputs are not yet knowable.

    `da`'s outputs come from the realized list, and before `stage.obs` has run
    there is no list. An empty declaration plus an existing sentinel is what
    makes a healed cycle skip the analysis it was healed to redo.
    """
    config, paths = var
    assert run.analysis_state(config, paths, 1) is None
    assert run.task_io(config, paths, "da", 1, None)[1] == []


def test_persistence_hands_a_real_restart_set_forward(var):
    """`coupler.res` proves a restart set for persistence too.

    Keyed off the stub's file name it would be `restart.stub`, and `cleanup`
    would then refuse to reap on every cycle of every experiment, which is a log
    line rather than a failure and a disk that fills over days.
    """
    config, _ = var
    assert run.restart_stamp(config) == "coupler.res"
    assert run.real_model(config, "forecast") is True


# --- a hybrid's two analyses and the recentring -------------------------------
#
# The asymmetry phase 8 introduces: an analysis that reads the whole ensemble
# and writes one state, beside a filter that reads the ensemble and writes every
# member, with a recentring between them and `writeback`.

@pytest.fixture
def hyb(base, tmp_path):
    """A hybrid experiment: four members plus a control, on a real model."""
    config = json.loads(json.dumps(base))
    config["model"] = {"name": "persistence"}
    config["solver"] = {"name": "variational", "covariance": "hybrid"}
    config["ensemble"] = {"size": 4, "control": True, "source": "letkf",
                          "on_missing_member": "fail_cycle"}
    site = {"scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o")}
    return config, Paths.of(config, site).ensure()


def test_both_analyses_run_a_real_application(hyb):
    config, _ = hyb
    assert run.real_analysis(config, "da") is True
    assert run.real_analysis(config, "da.ens") is True
    assert run.real_recenter(config, "recenter") is True


def test_an_letkf_does_not_recentre_onto_its_own_mean(base):
    config = json.loads(json.dumps(base))
    config["model"] = {"name": "persistence"}
    config["solver"] = {"name": "letkf"}
    assert run.recentres(config) is False
    assert run.real_recenter(config, "recenter") is False


def test_the_hybrid_reads_every_member_and_writes_one(hyb):
    """The asymmetry, in one place.

    The whole ensemble is half of the covariance, so every member's background
    is an input; the answer is the control's, so there is one output state.
    """
    config, _ = hyb
    assert run.read_members(config, "da") == (0, 1, 2, 3, 4)
    assert run.analysed_members(config, "da") == (0,)


def test_the_filter_reads_and_writes_the_ensemble_and_not_the_control(hyb):
    config, _ = hyb
    assert run.read_members(config, "da.ens") == (1, 2, 3, 4)
    assert run.analysed_members(config, "da.ens") == (1, 2, 3, 4)


def test_the_two_analyses_declare_the_backgrounds_they_read(hyb):
    config, paths = hyb
    stage(paths, 2, present=True)
    var_in, _ = run.task_io(config, paths, "da", 2, None)
    ens_in, _ = run.task_io(config, paths, "da.ens", 2, None)
    assert paths.member_out("rst", 1, 0) / "coupler.res" in var_in
    assert paths.member_out("rst", 1, 4) / "coupler.res" in var_in
    # The filter never assimilates the control.
    assert paths.member_out("rst", 1, 0) / "coupler.res" not in ens_in
    assert paths.member_out("rst", 1, 4) / "coupler.res" in ens_in


def test_the_filters_departures_do_not_overwrite_the_controls(hyb):
    """Two applications, the same observers, one configured output name.

    The control's are the experiment's product and keep that name. The
    ensemble's are a diagnostic and go one level down, which is the same
    arrangement v2 reached with OBS_OUT_CTRL_DIR and OBS_OUT_ENS_DIR.
    """
    config, paths = hyb
    stage(paths, 2, present=True)
    _, var_out = run.task_io(config, paths, "da", 2, None)
    _, ens_out = run.task_io(config, paths, "da.ens", 2, None)
    departures = paths.cycle_out("obs_out", 2) / "adt.nc4"
    assert departures in var_out
    assert departures not in ens_out
    assert paths.cycle_out("obs_out", 2) / "ensemble" / "adt.nc4" in ens_out


def test_the_filters_mean_is_a_diagnostic_and_not_the_controls_analysis(hyb):
    """In a pure LETKF the posterior mean *is* the control's answer.

    Here it is not: the control's came from the variational solve. Two of the
    filter's control-level products share a filename with that answer and its
    increment, so they go in a subdirectory of it.
    """
    config, paths = hyb
    assert run.ensemble_dir(paths, 2) == \
        paths.member_out("ana", 2, 0) / "analysis" / "ensemble"


def test_writeback_reads_the_recentred_state_for_every_member_but_the_control(hyb):
    """What makes a hybrid's ensemble follow its own experiment.

    Without it each member cycles around the filter's mean while the run being
    reported is the deterministic one, and the two drift apart silently.
    """
    config, paths = hyb
    stage(paths, 2, present=True)
    assert run.analysis_state(config, paths, 2, 0).name.startswith("ocn.ana.an.")
    assert run.analysis_state(config, paths, 2, 3).name.startswith("ocn.rcnt.an.")


def test_the_recentring_reads_the_analyses_a_filter_produced(hyb):
    config, paths = hyb
    stage(paths, 2, present=True)
    inputs, outputs = run.task_io(config, paths, "recenter", 2, None)
    assert run.analysis_product(config, paths, 2, 0) in inputs
    assert run.analysis_product(config, paths, 2, 3) in inputs
    assert outputs == [run.analysis_product(config, paths, 2, m, soca.RECENTERED)
                       for m in (1, 2, 3, 4)]


def test_the_recentring_reads_the_backgrounds_when_nothing_updated_them(hyb):
    """`source: none`: the members run free and are only pulled back.

    Not a degenerate case of the other. It is an ensemble that carries no
    observation information of its own and exists to give the covariance flow
    dependence, which is a cheaper experiment rather than a broken one.
    """
    config, paths = hyb
    config["ensemble"]["source"] = "none"
    stage(paths, 2, present=True)
    inputs, _ = run.task_io(config, paths, "recenter", 2, None)
    assert run.analysis_product(config, paths, 2, 0) in inputs
    assert paths.member_out("rst", 1, 3) / "coupler.res" in inputs
    assert run.analysis_product(config, paths, 2, 3) not in inputs


def test_a_cycle_that_assimilated_nothing_recentres_nothing(hyb):
    config, paths = hyb
    stage(paths, 2, present=False)
    assert run.task_io(config, paths, "recenter", 2, None) == ([], [])


# --- post.obs, and the cycle that assimilated nothing -------------------------
#
# `_post` directly rather than through `run_task`, because what is under test is
# the reduction's own refusal rather than the sentinel and skip machinery it
# runs inside.


def observation_output(path, *, observed, qc):
    """An analysis's observation output, carrying the QC the solve ended on."""
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Location", len(observed))
        data.createGroup("ObsValue").createVariable(
            VARIABLE, "f4", ("Location",))[:] = np.asarray(observed)
        data.createGroup("EffectiveQC").createVariable(
            VARIABLE, "i4", ("Location",))[:] = np.asarray(qc)
    return path


def departures_at(config, path):
    """Point every observer's output at one file, so a test can write it."""
    for entry in config["observations"]:
        entry["obs space"]["obsdataout"]["engine"]["obsfile"] = str(path)
    return config


def test_reading_observations_and_assimilating_none_fails_the_cycle(env, tmp_path):
    """The one failure a regional analysis has no other way to notice.

    Every observation rejected is what a global archive on a regional domain
    produces: SOCA runs, `Domain Check` throws all of them out, the increment is
    zero, and nothing else in the cycle is unhappy about any of it. It was a
    printed line for as long as that was possible.
    """
    config, _, paths = env
    departures_at(config, observation_output(
        tmp_path / "adt.nc4", observed=[1.0, 2.0], qc=[1, 1]))

    with pytest.raises(run.TaskError, match="not one survived"):
        run._post(config, None, paths, 1, "post.obs", 0)

    # Written before the raise, because it carries which filter did the
    # rejecting and that is wanted at exactly this moment.
    assert paths.obs_summary(1).exists()
    assert json.loads(paths.obs_summary(1).read_text())["totals"]["assimilated"] == 0


def test_a_cycle_that_assimilated_some_of_them_is_fine(env, tmp_path):
    config, _, paths = env
    departures_at(config, observation_output(
        tmp_path / "adt.nc4", observed=[1.0, 2.0], qc=[0, 1]))

    run._post(config, None, paths, 1, "post.obs", 0)

    assert json.loads(paths.obs_summary(1).read_text())["totals"]["assimilated"] == 2


def test_a_cycle_with_no_observations_at_all_is_not_a_failure(env, tmp_path):
    """A count of zero is not "all rejected", and both are normal outcomes.

    This is what the `count > 0` half of the condition is for. A domain-scoped
    archive produces empty observation spaces routinely, for a platform that did
    not pass over the domain, and a cycle of them must not fail.
    """
    config, _, paths = env
    empty = tmp_path / "adt.nc4"
    with netCDF4.Dataset(empty, "w") as data:
        data.createDimension("Location", 0)
    departures_at(config, empty)

    run._post(config, None, paths, 1, "post.obs", 0)

    summary = json.loads(paths.obs_summary(1).read_text())
    assert summary["totals"] == {"observers": 2, "failed": 0,
                                 "count": 0, "assimilated": 0}
    assert all(record["empty"] for record in summary["observers"])
