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

from ackbar import ledger, slurm
from ackbar.cli import main
from ackbar.paths import Paths
from ackbar.site import load_site

pytestmark = pytest.mark.tier2

REPO = Path(__file__).resolve().parents[1]

#: Long enough for the whole matrix including a Slurm-enforced timeout, short
#: enough that a hang is reported rather than sat through.
QUIET_TIMEOUT = 420


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


@pytest.fixture
def experiment(tmp_path):
    """Create, run, and clean up. Yields a factory."""
    created = []

    def factory(name, **kwargs):
        site = load_site()
        paths = Paths(experiment=name,
                      output_root=Path(site["output_root"]),
                      scratch_root=Path(site["scratch_root"]))
        _purge(paths)
        created.append(paths)
        source = build(tmp_path, name, **kwargs)
        assert main(["create", str(source)]) == 0
        return paths

    yield factory

    for paths in created:
        _purge(paths)


def _purge(paths):
    if paths.ledger_file.exists():
        live = set(slurm.queue()) & {r["job_id"] for r in ledger.read(paths)}
        slurm.scancel(sorted(live))
    shutil.rmtree(paths.experiment_dir, ignore_errors=True)
    shutil.rmtree(paths.scratch_dir, ignore_errors=True)


def wait_for_quiet(name, timeout=QUIET_TIMEOUT):
    """Wait until nothing of this experiment can make further progress.

    Not "until the queue drains": on a permissive Slurm the dependents of a
    failed job pend forever, so a drain never happens and waiting for one is how
    a test hangs instead of reporting.

    Only the *direct* dependent of a failed job reads `DependencyNeverSatisfied`.
    A job further down the chain reads plain `Dependency`, indistinguishable
    from one whose parent is merely queued, so "stuck" is the combination: not
    one job running, and every pending job waiting on a dependency. That is the
    same reasoning `status` will need, and it is why the queue reason cannot be
    read one row at a time.
    """
    deadline = time.time() + timeout
    quiet, previous = 0, None
    while time.time() < deadline:
        rows = _rows(name)
        if not rows:
            return "drained"
        moving = any(state != "PENDING" for _, state, _ in rows)
        blocked = all(reason.startswith("Dependency") for _, _, reason in rows)
        # Three conditions, all needed. Slurm's own scheduling loop leaves gaps
        # of tens of seconds during which every job of a perfectly healthy
        # experiment is pending on a dependency that has in fact been
        # satisfied, so neither "nothing running" nor "everything blocked" is
        # evidence on its own. What distinguishes a stalled experiment is that
        # the queue stops *changing*: a healthy one loses rows as jobs finish.
        unchanged = rows == previous
        previous = rows
        quiet = quiet + 1 if (blocked and not moving and unchanged) else 0
        if quiet >= 20:
            return "stuck"
        time.sleep(3)
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


def outcomes(paths, settle=60):
    """{(cycle, task, member or None): state}, from sacct via the ledger.

    Polled until nothing is still moving, because `sacct` lags the queue: the
    accounting database is written asynchronously, so for a few seconds after a
    job leaves `squeue` its final state has not landed and it still reads
    RUNNING. Reading once is how this test flakes.
    """
    deadline = time.time() + settle
    while True:
        states = _outcomes_now(paths)
        if not any(s in slurm.ACTIVE for s in states.values()):
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

def test_a_clean_run_cycles_without_a_daemon(experiment):
    paths = experiment("t2_clean", cycles=2, members=4, seconds=2)
    assert main(["start", "t2_clean"]) == 0
    assert wait_for_quiet("t2_clean") == "drained"

    states = outcomes(paths)
    assert states, "nothing ran"
    assert set(states.values()) == {"COMPLETED"}, \
        f"not every job completed: {sorted(states.items())}"

    # Cycle 2 exists at all only because cycle 1's submitter put it there.
    assert ledger.submitted_cycles(paths) == [1, 2]
    for member in (1, 2, 3, 4):
        assert (paths.member_out("rst", 2, member) / "restart.stub").exists()


def test_cleanup_removes_only_what_nothing_can_still_need(experiment):
    paths = experiment("t2_cleanup", cycles=2, members=2, seconds=1)
    assert main(["start", "t2_cleanup"]) == 0
    wait_for_quiet("t2_cleanup")
    # cleanup(2) drops cycle 0, gated on cycle 1 being complete for every
    # member. Cycle 1 is what cycle 2's writeback reads, so it stays.
    assert not paths.cycle_out("rst", 0).exists()
    assert paths.cycle_out("rst", 1).exists()


def test_every_cycle_leaves_a_ledger_record_per_node(experiment):
    paths = experiment("t2_ledger", cycles=2, members=2, seconds=1)
    main(["start", "t2_ledger"])
    wait_for_quiet("t2_ledger")
    records = ledger.read(paths)
    assert {r["cycle"] for r in records} == {1, 2}
    assert all(r["attempt"] == 1 for r in records)


# --- the fault matrix --------------------------------------------------------

FAULTS = {
    "exit_nonzero": ["1.forecast.1"],
    "overrun_time": ["1.forecast.2"],
    "overrun_memory": ["1.forecast.3"],
    "requeue": ["1.forecast.4"],
}

#: One minute, so a Slurm-enforced timeout costs a minute rather than the two
#: the stub domain declares.
SHORT = {"default": {"ntasks": 1, "time": "00:01:00", "mem": "500M"}}


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    """One run carrying four independent faults, one per array element.

    Independent on purpose: each fault lands on a different member of the same
    forecast array, so one run answers four questions instead of four runs
    answering one each, and the elementwise `aftercorr` edges are what keep them
    from masking each other.
    """
    name = "t2_matrix"
    site = load_site()
    paths = Paths(experiment=name,
                  output_root=Path(site["output_root"]),
                  scratch_root=Path(site["scratch_root"]))
    _purge(paths)
    source = build(tmp_path_factory.mktemp("matrix"), name, cycles=2, members=4,
                   seconds=2, fail=FAULTS, resources=SHORT)
    assert main(["create", str(source)]) == 0
    assert main(["start", name]) == 0
    state = wait_for_quiet(name)
    yield paths, outcomes(paths), state
    _purge(paths)


def test_a_nonzero_exit_is_reported_as_failed(matrix):
    _, states, _ = matrix
    assert states[(1, "forecast", 1)] == "FAILED"


def test_running_past_the_time_limit_is_reported_as_timeout(matrix):
    _, states, _ = matrix
    assert states[(1, "forecast", 2)] == "TIMEOUT"


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
    _, states, _ = matrix
    assert states[(1, "forecast", 3)] in ("OUT_OF_MEMORY", "FAILED")


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
    return "0B" in subprocess.run(
        ["swapon", "--show=SIZE", "--noheadings", "--bytes"],
        capture_output=True, text=True,
    ).stdout or not subprocess.run(
        ["swapon", "--show"], capture_output=True, text=True,
    ).stdout.strip()


def test_a_requeued_member_reruns_from_the_top_and_still_finishes(matrix):
    # Requeue on node failure is on by default and reruns the batch script from
    # its beginning, so every task has to be safe to run twice.
    paths, states, _ = matrix
    assert states[(1, "forecast", 4)] == "COMPLETED"
    sentinel = json.loads(paths.sentinel(1, "forecast", 4).read_text())
    assert sentinel["restarts"] >= 1
    assert (paths.member_out("rst", 1, 4) / "restart.stub").exists()


def test_a_healthy_member_is_untouched_by_the_others(matrix):
    # aftercorr, elementwise: member 4 proceeds without waiting for member 1,
    # and member 1's failure does not cancel it.
    paths, states, _ = matrix
    assert states[(1, "post.state", 4)] == "COMPLETED"


def test_a_failed_member_strands_exactly_its_own_successor(matrix, profile):
    paths, states, _ = matrix
    stranded = states.get((1, "post.state", 1))
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
    _, states, _ = matrix
    assert states[(1, "stats", None)] == "COMPLETED"
    assert states[(1, "verify", None)] == "COMPLETED"


def test_a_failed_cycle_stops_the_chain_rather_than_outrunning_it(matrix):
    # Fail-stop is the right default for science: one transient node failure
    # needs a heal, which is much better than cycling off a bad background.
    paths, states, _ = matrix
    assert ledger.submitted_cycles(paths) == [1]
    assert states.get((1, "submit", None)) in (None, "PENDING", "CANCELLED")


def test_nothing_is_left_pending_and_undiagnosed(matrix, profile):
    _, _, state = matrix
    assert state == ("drained" if profile == "strict" else "stuck")


# --- faults that are not job states ------------------------------------------

def test_exit_zero_having_written_nothing_is_caught_by_the_consumer(experiment):
    """The fault Slurm cannot see, so ACKBAR has to.

    The producer reports success and writes no output. Nothing in the scheduler
    notices; the consumer's input check is the only thing that can.
    """
    paths = experiment("t2_silent", cycles=1, members=2, seconds=1,
                       fail={"write_nothing": ["1.recenter.*"]})
    main(["start", "t2_silent"])
    wait_for_quiet("t2_silent")

    states = outcomes(paths)
    assert states[(1, "recenter", 1)] == "COMPLETED"
    assert states[(1, "writeback", 1)] == "FAILED"

    log = next(paths.sub("log").joinpath("1").glob("writeback.*_1.out"))
    assert "missing" in log.read_text()


def test_an_impossible_request_is_refused_before_anything_is_submitted(
        experiment, tmp_path):
    """Not a fault injector: a resources value the partition cannot satisfy.

    `sbatch` rejects it outright, and the point of the test is that the
    rejection propagates instead of leaving half a cycle in the queue.
    """
    experiment(
        "t2_toobig", cycles=1, members=2, seconds=1,
        resources={"default": {"ntasks": 1, "time": "00:01:00", "mem": "9000G"}},
    )
    # The CLI turns it into exit 2 rather than a traceback, which is what the
    # submitter job inside a running experiment would report.
    assert main(["start", "t2_toobig"]) == 2
    assert wait_for_quiet("t2_toobig") == "drained"
