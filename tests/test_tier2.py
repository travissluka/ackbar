"""Tier 2: the workflow end to end on a real Slurm, with no JEDI and no model.

This is the milestone the project's premise rests on. On 8 physical cores an
`--array=1-20` of 8-PE forecasts runs strictly serially, so without the stub the
property the whole project exists for cannot be demonstrated locally, and none
of the failure paths that carry the real risk can be exercised until an HPC
allocation is on the line.

Needs `source site/activate.sh` and a working `sbatch`; skipped otherwise.

**Both Slurm profiles.** With `DependencyParameters` unset, a job whose
dependency failed pends forever with reason `DependencyNeverSatisfied` and never
appears in `sacct` as failed at all. With `kill_invalid_depend` it is cancelled.
The assertions below accept either, and `tools/slurm/profile.sh` switches
between them, because depending on one of the two is exactly the assumption that
breaks on the first new machine.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from ackbar import heal, ledger, slurm, state
from ackbar.cli import _frozen, main
from ackbar.graph import build_graph
from ackbar.paths import Paths
from ackbar.site import load_site

pytestmark = pytest.mark.tier2

REPO = Path(__file__).resolve().parents[1]

#: Long enough for the whole matrix including a Slurm-enforced timeout, short
#: enough that a hang is reported rather than sat through.
QUIET_TIMEOUT = 420

#: `ACKBAR_TIER2_FAST=1` drops the requeue case, which alone is most of this
#: file's wall clock: Slurm defers a requeued job about two minutes before it is
#: eligible again, and no amount of overlapping gets under that. Everything else
#: finishes in well under a minute. Use it while iterating; do not use it as the
#: thing that runs before a commit, because requeue is the one fault the
#: scheduler inflicts on its own, without being asked.
FAST = os.environ.get("ACKBAR_TIER2_FAST") == "1"


@pytest.fixture(scope="module", autouse=True)
def require_slurm():
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")


@pytest.fixture(scope="module")
def profile():
    """Which of the two dependency behaviours this Slurm is configured for."""
    result = subprocess.run(["scontrol", "show", "config"],
                            capture_output=True, text=True)
    strict = "kill_invalid_depend" in result.stdout
    return "strict" if strict else "permissive"


# --- building and running one experiment -------------------------------------

BASE = {
    "inherit": ["domain/stub", "model/stub", "da/letkf"],
    "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H"},
    "ensemble": {"size": 4, "control": False, "source": "offline",
                 "on_missing_member": "fail_cycle"},
}


def build(tmp_path, name, *, cycles=2, members=4, seconds=2, fail=None,
          resources=None):
    """Write an experiment file. Faults are configuration, not test plumbing."""
    config = json.loads(json.dumps(BASE))
    config["experiment"] = {"name": name}
    config["cycle"]["count"] = cycles
    config["ensemble"]["size"] = members
    config["model"] = {"stub": {"seconds": seconds, "bytes": 1024}}
    if fail:
        config["model"]["stub"]["fail"] = fail
    if resources:
        config["domain"] = {"resources": resources}
    target = tmp_path / f"{name}.yaml"
    target.write_text(yaml.safe_dump(config, sort_keys=False))
    return target


#: Every experiment tier 2 needs, started together. They are independent, and
#: the scheduler is what this test is exercising, so running them one at a time
#: means the wall clock is the sum of their scheduling latencies rather than the
#: longest of them. The slowest single case is the fault matrix, which has to
#: sit through a Slurm-enforced one minute timeout.
FAULTS = {
    "exit_nonzero": ["1.forecast.1"],
    "overrun_memory": ["1.forecast.2"],
}

#: A one minute limit, because Slurm stores a time limit in whole minutes and
#: this is the floor. The stub domain's two minutes would double the wait.
SHORT = {"default": {"ntasks": 1, "time": "00:01:00", "mem": "500M"}}

#: Sized against 8 cores. What costs wall clock is the number of *stages*, not
#: the number of members: each dependency release is a few seconds of Slurm
#: scheduling latency and a cycle is nine stages deep, while twenty members at
#: one core each cost one wave. So the cycle counts are the minimum that still
#: says something (two, because one cycle cannot show cycling or cleanup) and
#: the member counts are the minimum that still makes an array an array, plus
#: the four the fault matrix needs to carry one fault each.
EXPERIMENTS = {
    "t2_clean": dict(cycles=2, members=2, seconds=1),
    "t2_matrix": dict(cycles=2, members=3, seconds=1, fail=FAULTS,
                      resources=SHORT),
    "t2_silent": dict(cycles=1, members=2, seconds=1,
                      fail={"write_nothing": ["1.writeback.*"]}),
    # Two faults, one per cycle, and the first one deliberately lands on a leaf
    # so that cycle 1 still submits cycle 2. That is what makes this a heal of
    # an experiment that has moved on rather than of one that stopped dead, and
    # it is the case where the closure has to reach across a cycle boundary
    # without dragging in the healthy cycle 1 that produced it.
    #
    # `verify` and not `post.state`, which was the obvious leaf and is not one
    # for this purpose: `post.state` reads the set `cleanup` drops, so it is in
    # cleanup's proof, and failing it makes cleanup refuse rather than makes a
    # leaf fail. That is correct behaviour and the wrong thing to test here.
    "t2_heal": dict(cycles=2, members=2, seconds=1,
                    fail={"exit_nonzero": ["1.verify", "2.writeback.2"]}),
    # Its own experiment, and not part of the matrix, purely for wall clock. A
    # Slurm-enforced timeout costs a whole minute, and inside the matrix that
    # minute would run after the other faults rather than alongside them.
    "t2_timeout": dict(cycles=1, members=2, seconds=1, resources=SHORT,
                       fail={"overrun_time": ["1.da.*"]}),
    # Its own for a sharper reason: Slurm defers a requeued job about two
    # minutes before it is eligible again, which is the single slowest thing in
    # this file. On its own that is two minutes of the suite; in the matrix it
    # would be two minutes of the matrix, in series with everything else.
    "t2_requeue": dict(cycles=1, members=3, seconds=1,
                       fail={"requeue": ["1.forecast.2"]}),
}

if FAST:
    del EXPERIMENTS["t2_requeue"]

requeue_case = pytest.mark.skipif(FAST, reason="ACKBAR_TIER2_FAST=1")


class Run:
    """One finished experiment: where it is, how it ended, and what Slurm said."""

    def __init__(self, paths, quiet, states):
        self.paths = paths
        self.quiet = quiet
        self.states = states

    def __getitem__(self, key):
        return self.states[key]

    def get(self, key, default=None):
        return self.states.get(key, default)


def _paths_for(name):
    """Paths for an experiment that has already been created.

    Out of its frozen config rather than out of its name, because every
    directory `Paths` names is keyed by a cycle date and the dates come from
    that file. `_purge` therefore cannot go through this, and takes a name.
    """
    site = load_site()
    frozen = Path(site["output_root"]) / name / "cfg" / "experiment.yaml"
    return Paths.of(yaml.safe_load(frozen.read_text()), site)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Create and start every experiment, then wait for all of them."""
    tmp_path = tmp_path_factory.mktemp("tier2")
    started = {}
    for name, options in EXPERIMENTS.items():
        _purge(name)
        assert main(["create", str(build(tmp_path, name, **options))]) == 0
        _claim(name)
        assert main(["start", name]) == 0
        started[name] = _paths_for(name)

    finished = {}
    for name, paths in started.items():
        quiet = wait_for_quiet(name)
        finished[name] = Run(paths, quiet, outcomes(paths))

    yield finished

    for name in started:
        _purge(name)


@pytest.fixture
def experiment(tmp_path):
    """A one-off experiment, for the cases that must not share the fixture."""
    created = []

    def factory(name, **kwargs):
        _purge(name)
        created.append(name)
        assert main(["create", str(build(tmp_path, name, **kwargs))]) == 0
        _claim(name)
        return _paths_for(name)

    yield factory

    for name in created:
        _purge(name)


def _purge(target):
    """Remove an experiment, whether or not it was ever created.

    Takes a name or a `Paths`. It has to accept a name because `Paths` is built
    out of the frozen config and purging has to work before that file exists;
    it accepts a `Paths` because every tier 3 module already holds one, and
    making each of them unpack a name would be churn for nothing.
    """
    name = target if isinstance(target, str) else target.experiment
    site = load_site()
    experiment_dir = Path(site["output_root"]) / name
    ledger_file = experiment_dir / "run" / "ledger.jsonl"
    if ledger_file.exists():
        paths = _paths_for(name)
        live = set(slurm.queue()) & {r["job_id"] for r in ledger.read(paths)}
        _refuse_if_not_ours(name, live)
        slurm.scancel(sorted(live))
    shutil.rmtree(experiment_dir, ignore_errors=True)
    shutil.rmtree(Path(site["scratch_root"]) / name, ignore_errors=True)


#: Written into an experiment by the process that created it. `_purge` deletes
#: by a *fixed* name, so without a mark it cannot tell its own leftovers from
#: another suite run's live experiment. A pid is enough: the two runs that
#: collide are two pytest processes on one machine.
OWNER = "tier2-owner"
_ME = str(os.getpid())


def _claim(name):
    """Mark an experiment as this process's, as soon as it exists."""
    site = load_site()
    mark = Path(site["output_root"]) / name / OWNER
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(_ME + "\n")


def _refuse_if_not_ours(name, live):
    """Stop rather than delete a run this process did not start.

    Two suites at once was a slow, wrong failure rather than a refusal, and it
    took a reviewer twenty five minutes on fourteen seconds of CPU to find out
    why. Three things combine. `_purge` rmtree's a *fixed* name in the shared
    output root, so it deletes the other run's frozen config and ledger out from
    under it. `_rows` filters a global `squeue` by job name prefix with no user
    or session scope, so each run counts the other's jobs as its own. And
    `wait_for_quiet` therefore reaches neither "drained", because the other
    run's rows are there, nor "stuck", because they are healthy, so it spins to
    its timeout and fails naming job ids belonging to a run the reader cannot
    see.

    The cheapest place to break that chain is here, before anything is deleted.
    A live job in this experiment's ledger that this process did not submit is
    somebody else's run, and there is no repair for having deleted it.
    """
    if not live:
        return
    mark = Path(load_site()["output_root"]) / name / OWNER
    owner = mark.read_text().strip() if mark.exists() else "unknown"
    if owner == _ME:
        return
    raise pytest.UsageError(
        f"experiment {name!r} has live job(s) {sorted(live)} and belongs to "
        f"process {owner}, not to this one ({_ME}), so another tier 2 run is "
        f"using it. These tests share fixed experiment names in one output "
        f"root and cannot run twice at once. Refusing to delete it: wait for "
        f"the other run to finish, or give this one its own "
        f"ACKBAR_OUTPUT_ROOT."
    )


def wait_for_quiet(name, timeout=QUIET_TIMEOUT):
    """Wait until nothing of this experiment can make further progress.

    Not "until the queue drains": on a permissive Slurm the dependents of a
    failed job pend forever, so a drain never happens and waiting for one is how
    a test hangs instead of reporting.

    Only the *direct* dependent of a failed job reads `DependencyNeverSatisfied`.
    A job further down the chain reads plain `Dependency`, indistinguishable
    from one whose parent is merely queued, so no single row settles the
    question. What does settle it is that at least one row is definitively dead
    while nothing at all is running: a healthy experiment never produces a
    `DependencyNeverSatisfied` row, so its presence is the evidence, and waiting
    for the queue to stop changing is not needed on top of it.
    """
    deadline = time.time() + timeout
    quiet = 0
    while time.time() < deadline:
        rows = _rows(name)
        if not rows:
            return "drained"
        running = any(state != "PENDING" for _, state, _ in rows)
        blocked = all(reason.startswith("Dependency") for _, _, reason in rows)
        dead = any(reason == slurm.NEVER_SATISFIED for _, _, reason in rows)
        # Two polls rather than one, only because squeue and the scheduler are
        # not sampled at the same instant.
        quiet = quiet + 1 if (dead and blocked and not running) else 0
        if quiet >= 2:
            return "stuck"
        time.sleep(2)
    pytest.fail(f"{name} was still moving after {timeout}s: {_rows(name)}")


def _rows(name):
    result = subprocess.run(
        ["squeue", "-h", "-o", "%i|%j|%T|%r"], capture_output=True, text=True,
    )
    out = []
    for line in result.stdout.splitlines():
        job_id, job_name, state, reason = line.split("|")
        if job_name.startswith(f"{name}."):
            out.append((job_id, state, reason))
    return out


def outcomes(paths, settle=30):
    """{(cycle, task, member or None): state}, from sacct via the ledger.

    Polled while anything is still running, because `sacct` lags the queue: the
    accounting database is written asynchronously, so for a few seconds after a
    job leaves `squeue` its final state has not landed and it still reads
    RUNNING. Reading once is how this test flakes.

    PENDING is deliberately not waited on. A stranded job pends forever by
    design on a permissive Slurm, so treating it as "not settled yet" would
    spend the whole timeout on every fault case.
    """
    moving = ("RUNNING", "COMPLETING", "REQUEUED", "RESIZING", "SUSPENDED")
    deadline = time.time() + settle
    while True:
        states = _outcomes_now(paths)
        if not any(s in moving for s in states.values()):
            return states
        if time.time() > deadline:
            return states
        time.sleep(2)


def _outcomes_now(paths):
    records = ledger.read(paths)
    if not records:
        return {}
    raw = subprocess.run(
        ["sacct", "-n", "-X", "-P", "-o", "JobIDRaw,JobID,State",
         "-j", ",".join(str(r["job_id"]) for r in records)],
        capture_output=True, text=True,
    ).stdout

    by_base = {}
    for line in raw.splitlines():
        _, job_id, state = line.split("|")
        base, _, element = job_id.partition("_")
        if not base.isdigit():
            continue
        if element.startswith("["):
            continue  # the un-started remainder of an array
        member = int(element) if element.isdigit() else None
        by_base[(int(base), member)] = state.split()[0]

    out = {}
    for record in records:
        for member in (record["members"] or [None]):
            state = by_base.get((record["job_id"], member))
            if state is not None:
                out[(record["cycle"], record["task"], member)] = state
    return out


# --- the clean run -----------------------------------------------------------
#
# One experiment, several questions. Cycling, cleanup and the ledger are all
# properties of the same clean run, and asking each of them its own 40 second
# run would triple the wall clock to learn nothing extra.

@pytest.fixture(scope="module")
def clean(runs):
    return runs["t2_clean"]


def test_a_clean_run_cycles_without_a_daemon(clean):
    assert clean.quiet == "drained"
    assert clean.states, "nothing ran"
    assert set(clean.states.values()) == {"COMPLETED"}, \
        f"not every job completed: {sorted(clean.states.items())}"

    # Cycle 2 exists at all only because cycle 1's submitter put it there.
    assert ledger.submitted_cycles(clean.paths) == [1, 2]
    for member in (1, 2):
        assert (clean.paths.member_out("rst", 2, member) / "restart.stub").exists()


def test_cleanup_removes_only_what_nothing_can_still_need(clean):
    # cleanup(2) drops cycle 0, gated on cycle 1 being complete for every
    # member. Cycle 1 is what cycle 2's writeback reads, so it stays.
    assert not clean.paths.cycle_out("rst", 0).exists()
    assert clean.paths.cycle_out("rst", 1).exists()


def test_every_cycle_leaves_a_ledger_record_per_node(clean):
    records = ledger.read(clean.paths)
    assert {r["cycle"] for r in records} == {1, 2}
    assert all(r["attempt"] == 1 for r in records)


def test_every_cycle_leaves_a_stats_file(clean):
    # An afterany leaf, so it runs whatever happened. Here nothing did.
    for cycle in (1, 2):
        assert clean.paths.stats_file(cycle).exists()


# --- the fault matrix --------------------------------------------------------
#
# One run carrying four independent faults, one per array element. Independent
# on purpose: each lands on a different member of the same forecast array, so
# one run answers four questions instead of four runs answering one each, and
# the elementwise `aftercorr` edges are what keep them from masking each other.

@pytest.fixture(scope="module")
def matrix(runs):
    return runs["t2_matrix"]


def test_a_nonzero_exit_is_reported_as_failed(matrix):
    assert matrix[(1, "forecast", 1)] == "FAILED"


def test_running_past_the_time_limit_is_reported_as_timeout(runs):
    assert runs["t2_timeout"][(1, "da", None)] == "TIMEOUT"


def test_blowing_the_memory_request_is_reported_as_out_of_memory(matrix):
    # This one is a property of the cluster as much as of ACKBAR. With
    # ConstrainSwapSpace unset, memory.max is applied but memory.swap.max is
    # left unlimited, so the job swaps its way past the limit and finishes
    # COMPLETED. Skip loudly rather than assert something weaker: a silently
    # relaxed assertion here would report that ACKBAR handles OUT_OF_MEMORY on
    # a machine that has never produced one.
    if not _enforces_memory():
        pytest.skip(
            "this Slurm does not kill a job that exceeds --mem; set "
            "ConstrainSwapSpace=yes in tools/slurm/cgroup.conf and run "
            "`sudo tools/slurm/install.sh config svc`"
        )
    assert matrix[(1, "forecast", 2)] in ("OUT_OF_MEMORY", "FAILED")


def _enforces_memory():
    conf = Path("/etc/slurm/cgroup.conf")
    if not conf.exists():
        return False
    settings = dict(
        line.split("=", 1) for line in conf.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    if settings.get("ConstrainRAMSpace", "").lower() != "yes":
        return False
    if settings.get("ConstrainSwapSpace", "").lower() == "yes":
        return True
    # Unlimited swap saves the job unless there is no swap to use.
    return not subprocess.run(
        ["swapon", "--show"], capture_output=True, text=True,
    ).stdout.strip()


@requeue_case
def test_a_requeued_member_reruns_from_the_top_and_still_finishes(runs):
    # Requeue on node failure is on by default and reruns the batch script from
    # its beginning, so every task has to be safe to run twice.
    run = runs["t2_requeue"]
    assert run[(1, "forecast", 2)] == "COMPLETED"
    sentinel = json.loads(run.paths.sentinel(1, "forecast", 2).read_text())
    assert sentinel["restarts"] >= 1
    assert (run.paths.member_out("rst", 1, 2) / "restart.stub").exists()


@requeue_case
def test_a_requeue_takes_only_the_element_that_asked_for_it(runs):
    """`scontrol requeue $SLURM_JOB_ID` inside an array element requeues the
    whole array, siblings and all, and each of them then waits out Slurm's two
    minute post-requeue deferral. The element has to be named
    `<array id>_<index>` instead.
    """
    run = runs["t2_requeue"]
    requeued = json.loads(run.paths.sentinel(1, "forecast", 2).read_text())
    for member in (1, 3):
        sibling = json.loads(run.paths.sentinel(1, "forecast", member).read_text())
        assert sibling["restarts"] == 0, \
            f"member {member} was requeued along with member 2"
    assert requeued["restarts"] >= 1


def test_a_healthy_member_is_untouched_by_the_others(matrix):
    # aftercorr, elementwise: member 3 proceeds without waiting for member 1,
    # and member 1's failure does not cancel it.
    assert matrix[(1, "post.state", 3)] == "COMPLETED"


@requeue_case
def test_a_requeue_does_not_stall_the_rest_of_its_cycle(runs):
    # The siblings finish on their own schedule while the requeued element
    # waits out Slurm's deferral.
    run = runs["t2_requeue"]
    assert run[(1, "post.state", 1)] == "COMPLETED"
    assert run[(1, "post.state", 3)] == "COMPLETED"


def test_a_failed_member_strands_exactly_its_own_successor(matrix, profile):
    stranded = matrix.get((1, "post.state", 1))
    if profile == "strict":
        assert stranded == "CANCELLED"
    else:
        # Never runs, never fails, never appears in sacct as anything. This is
        # why `status` and `heal` read the queue reason rather than trusting
        # sacct alone.
        assert stranded in (None, "PENDING")


def test_the_leaves_still_run_when_members_have_failed(matrix):
    # afterany, and the whole reason for it: the harvest and the observation
    # statistics are exactly what is most wanted when something failed.
    assert matrix[(1, "stats", None)] == "COMPLETED"
    assert matrix[(1, "verify", None)] == "COMPLETED"


def test_a_failed_cycle_stops_the_chain_rather_than_outrunning_it(matrix):
    # Fail-stop is the right default for science: one transient node failure
    # needs a heal, which is much better than cycling off a bad background.
    assert ledger.submitted_cycles(matrix.paths) == [1]
    assert matrix.get((1, "submit", None)) in (None, "PENDING", "CANCELLED")


def test_nothing_is_left_pending_and_undiagnosed(matrix, profile):
    assert matrix.quiet == ("drained" if profile == "strict" else "stuck")


# --- faults that are not job states ------------------------------------------

def test_exit_zero_having_written_nothing_is_caught_by_the_consumer(runs):
    """The fault Slurm cannot see, so ACKBAR has to.

    The producer reports success and writes no output. Nothing in the scheduler
    notices; the consumer's input check is the only thing that can.
    """
    silent = runs["t2_silent"]
    assert silent[(1, "writeback", 1)] == "COMPLETED"
    assert silent[(1, "forecast", 1)] == "FAILED"

    log = next(silent.paths.log_dir(1).glob("forecast.*_1.out"))
    assert "missing" in log.read_text()


def test_an_impossible_request_is_refused_before_anything_is_submitted(
        experiment):
    """Not a fault injector: a resources value the partition cannot satisfy.

    `sbatch` rejects it outright, and the point of the test is that the
    rejection propagates instead of leaving half a cycle in the queue. Its own
    experiment rather than the shared fixture, because it never starts.
    """
    experiment(
        "t2_toobig", cycles=1, members=2, seconds=1,
        resources={"default": {"ntasks": 1, "time": "00:01:00", "mem": "9000G"}},
    )
    # The CLI turns it into exit 2 rather than a traceback, which is what the
    # submitter job inside a running experiment would report.
    assert main(["start", "t2_toobig"]) == 2
    assert wait_for_quiet("t2_toobig") == "drained"


# --- healing -----------------------------------------------------------------
#
# The phase's done-criterion, written so that it fails if it needed help: every
# recovery below goes through `ackbar heal` and there is no `scancel` or
# `sbatch` anywhere in this section.

@pytest.fixture(scope="module")
def healed(runs, profile):
    """One broken experiment taken all the way back to finished.

    Shares the module's run fixture rather than starting its own, so the first
    two cycles are already broken by the time this begins and only the heal
    rounds cost wall clock.
    """
    name = "t2_heal"
    paths = runs[name].paths
    config, _, _ = _frozen(name)
    graph = build_graph(config)

    before = state.collect(paths, graph)
    plan = heal.plan(config, paths, graph, before)

    if not FAST:
        # Heal without fixing the cause first. The fault is deterministic, so
        # the experiment has to come back broken in exactly the same place: a
        # heal that appeared to work here would mean the second attempt ran
        # something other than what failed.
        assert main(["heal", name]) == 0
        # Under `kill_invalid_depend` the dependents are cancelled and the
        # queue drains; under the default they pend forever. Either way nothing
        # can make further progress, which is the property being waited on.
        assert wait_for_quiet(name) in ("stuck", "drained")
        repeat = heal.plan(config, paths, graph)
    else:
        repeat = None

    # Fixing the cause is an edit to the frozen config, which is what an
    # operator would do: the experiment is resolved once and the jobs read that
    # file, so this is the only place a fix can go.
    frozen = yaml.safe_load(paths.frozen_config.read_text())
    frozen["model"]["stub"].pop("fail", None)
    paths.frozen_config.write_text(yaml.safe_dump(frozen, sort_keys=False))

    assert main(["heal", name]) == 0
    assert wait_for_quiet(name) == "drained"

    after = state.collect(paths, graph)
    return type("Healed", (), {
        "paths": paths, "graph": graph, "before": before, "plan": plan,
        "repeat": repeat, "after": after, "profile": profile,
    })


def test_a_leaf_failure_does_not_stop_the_next_cycle(healed):
    """Cycle 1's verify failed and cycle 2 ran anyway.

    A leaf has no dependents, so fail-stop does not apply to it, and this is the
    case where a heal has to work on an experiment that has moved on rather than
    on one that stopped dead.
    """
    assert healed.before["1.verify"].broken == (None,)
    assert healed.before["2.da"].summary == state.COMPLETE


def test_the_blast_radius_is_the_failure_and_its_dependents(healed):
    broken, closure, cancel = healed.plan
    # The two injected faults, plus however much of the tail the profile has
    # already condemned. On a permissive Slurm that is the direct dependent,
    # which reads `DependencyNeverSatisfied` and so is broken in its own right
    # rather than merely waiting, while anything further down reads plain
    # `Dependency` and is only pending. Under `kill_invalid_depend` the whole
    # tail has been cancelled and every one of them is broken.
    assert {"1.verify", "2.writeback"} <= set(broken)
    assert not {"1.forecast", "1.da", "2.da", "2.stage.obs"} & set(broken)

    # Downstream of the cycle 2 failure, all of it.
    assert {"2.writeback", "2.forecast", "2.post.state"} <= set(closure)
    # Not upstream, and not the healthy cycle that produced the input.
    assert not {"1.forecast", "1.da", "2.da", "2.stage.obs"} & set(closure)
    # On a permissive Slurm cancelling is not optional: those jobs pend forever
    # holding job ids, and a successful replacement upstream does not release
    # them. Under `kill_invalid_depend` Slurm has already cancelled them, so
    # there is nothing live left and an empty list is the right answer rather
    # than a missed step.
    if healed.profile == "permissive":
        assert cancel
    else:
        assert cancel == []


def test_healing_twice_lands_in_the_same_place(healed):
    """A deterministic fault, resubmitted unchanged, fails the same way."""
    if FAST:
        pytest.skip("ACKBAR_TIER2_FAST=1")
    assert healed.repeat[0] == healed.plan[0]
    assert set(healed.repeat[1]) == set(healed.plan[1])


def test_heal_alone_takes_the_experiment_to_finished(healed):
    assert state.finished(healed.after, healed.graph) == "finished"
    for member in (1, 2):
        assert (healed.paths.member_out("rst", 2, member) / "restart.stub").exists()


def test_a_healed_node_is_a_new_attempt_in_the_ledger(healed):
    attempts = ledger.attempts(healed.paths, 2, "writeback")
    assert attempts >= 2
    assert healed.after["2.writeback"].attempt == attempts


def test_members_that_already_succeeded_skip_rather_than_rerun(healed):
    """The whole node is resubmitted, not the failed member alone.

    Keeping the index set identical is what lets an `aftercorr` edge be rebuilt
    between two arrays; the cost of it is paid back by the sentinel, which is
    the only thing that makes rerunning a finished member free.
    """
    log = sorted(healed.paths.log_dir(2).glob("writeback.*_1.out"))
    assert len(log) >= 2, "member 1 was not resubmitted with the array"
    assert "already complete, skipping" in log[-1].read_text()


def test_every_cycle_leaves_a_stats_file_after_a_heal(healed):
    for cycle in (1, 2):
        assert healed.paths.stats_file(cycle).exists()


def test_the_stats_file_describes_the_healed_run_not_the_abandoned_one(healed):
    """`stats` reruns on a heal even though it already succeeded.

    It reports on a cycle rather than producing an artifact of its own, and a
    heal changes which jobs that cycle consists of. Skipping on its sentinel
    leaves a file describing the run that was thrown away.
    """
    records = [r for r in ledger.read(healed.paths)
               if r["cycle"] == 2 and r["task"] == "writeback"]
    assert len(records) >= 2
    harvested = json.loads(healed.paths.stats_file(2).read_text())
    assert records[-1]["job_id"] in {j["job_id"] for j in harvested["jobs"]}


def test_cleanup_still_runs_on_an_experiment_that_needed_healing(healed):
    # Gated on artifacts rather than job state, so a heal cannot talk it into
    # deleting a restart that a resubmitted consumer is about to read.
    assert not healed.paths.cycle_out("rst", 0).exists()
    assert healed.paths.cycle_out("rst", 1).exists()
