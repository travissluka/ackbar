"""Tier 0: what a job does, without a job.

Every body here is python, so the interesting parts run in-process: the skip
rule, the input check, commit by rename, the cleanup gate, and the halt flag.
What Slurm makes of the results is tier 2's problem.
"""

import json
from pathlib import Path

import pytest

from ackbar import ledger, run
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.paths import Paths

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"


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
    assert run.selector_matches("*.b.vt.*", 3, "b.vt", 0)


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


def test_a_task_with_no_body_yet_says_so_plainly(env):
    """A task in the data path with no implementation is an error, not a no-op.

    `forecast.ext` is the one left: it is the same executable as the forecast
    with a different diag_table and a different product, and it arrives with the
    phase that scores those diagnostics. A stub body standing in for it would
    write a file claiming to be a long forecast. The deferred leaves say so
    through DEFERRED instead, and `recenter` is among them for a reason that is
    not "unimplemented"; see the comment there.
    """
    config, site, paths = env
    config = json.loads(json.dumps(config))
    config["model"] = {"name": "mom6sis2"}
    with pytest.raises(run.TaskError, match="phase"):
        run.run_task(config, site, paths, 1, "forecast.ext", 1)


# --- cleanup -----------------------------------------------------------------

def _complete_cycle(env, cycle):
    _, _, paths = env
    for member in (1, 2):
        target = paths.member_out("rst", cycle, member) / "restart.stub"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


def test_cleanup_drops_only_what_no_forecast_can_still_read(env):
    _, _, paths = env
    _complete_cycle(env, 1)
    _complete_cycle(env, 2)
    do(env, 3, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()
    assert paths.cycle_out("rst", 2).exists()


def test_cleanup_reaps_the_analysis_on_the_same_rule_as_the_restarts(env):
    """`ana/` grows at the same rate as `rst/` and used to grow forever.

    In a DA run the forecast starts from `ana/<n>/mem###`, so writeback leaves a
    whole restart set there per member per cycle. Reaping `rst/` alone is an
    experiment that cycles happily for a week and fills the disk in the second.
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
    # And it is the same rule, not a second one: the cycle whose restarts are
    # kept keeps its analysis too.
    assert paths.cycle_out("ana", 2).exists()


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
    # file being tidied away by hand.
    _, _, paths = env
    ledger.append(paths, cycle=2, task="da", members=(), attempt=1, job_id=1,
                  dependency=None)
    assert do(env, 1, "submit") == 0
    assert len(ledger.read(paths)) == 1


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
