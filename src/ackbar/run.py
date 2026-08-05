"""What a job actually does.

Three tasks are real from the first day, because they *are* the machinery being
tested rather than science standing in for it: `submit`, `cleanup` and `stats`.
Everything else is the stub, which burns wallclock, requires the inputs its real
counterpart would require, writes correctly sized output, and can be told to
fail in a specific way.

The stub exists because on 8 physical cores an `--array=1-20` of 8-PE forecasts
runs strictly serially, so without it the property the whole project exists for
cannot be demonstrated locally, and none of the failure paths that carry the
real risk can be exercised until an HPC allocation is on the line.

Idempotency is not optional here. Requeue on node failure is on by default and
reruns a batch script from its beginning, so every body below writes to a
temporary path, commits by rename, and writes its sentinel last.
"""

import fnmatch
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

from . import ledger
from .graph.build import member_set

#: Faults the stub can inject, each named for the terminal state it produces.
#: `impossible memory request` is deliberately absent: that is a resources
#: value, not a fault, and an experiment tests it by asking for more memory than
#: the partition has. Likewise "a member that never starts" is not injected, it
#: is what `exit_nonzero` on one array element does to its `aftercorr` child.
FAULTS = ("exit_nonzero", "overrun_time", "overrun_memory", "write_nothing",
          "requeue")


class TaskError(Exception):
    pass


# --- the stub's data flow ----------------------------------------------------
#
# Scoped to the stub on purpose. A real forecast's inputs are a restart set, a
# forcing archive, a diag_table and a namelist, and they arrive when the real
# task does. What the stub needs is the *shape*: one file per member per cycle,
# so that restart continuity across a heal is a thing a test can assert.

def stub_io(config, paths, task, cycle, member):
    """(inputs, outputs) for one stub job, as absolute paths."""
    members = member_set(config)
    analysis = config.get("solver", {}).get("name", "none") != "none"
    recentres = config.get("solver", {}).get("name") == "letkf" and config.get("ensemble")

    def rst(c, m):
        return paths.member_out("rst", c, m) / "restart.stub"

    def ana(c, m, name):
        return paths.member_out("ana", c, m) / name

    if task == "b.vt":
        return [rst(cycle - 1, members[0])], [paths.cycle_out("ana", cycle) / "vt.stub"]
    if task == "da":
        # One job consuming every member, which is what LETKF is and what a
        # variational run with one member degenerates to.
        return [rst(cycle - 1, m) for m in members], \
               [ana(cycle, m, "incr.stub") for m in members]
    if task == "hofx":
        return [rst(cycle - 1, m) for m in members], \
               [paths.cycle_out("obs_out", cycle) / "hofx.stub"]
    if task == "recenter":
        return [ana(cycle, member, "incr.stub")], [ana(cycle, member, "incr.rc.stub")]
    if task == "writeback":
        source = "incr.rc.stub" if recentres else "incr.stub"
        return [ana(cycle, member, source), rst(cycle - 1, member)], \
               [ana(cycle, member, "restart.stub")]
    if task == "forecast":
        start = ana(cycle, member, "restart.stub") if analysis else rst(cycle - 1, member)
        return [start], [rst(cycle, member)]
    if task == "forecast.ext":
        start = ana(cycle, member, "restart.stub") if analysis else rst(cycle - 1, member)
        # Where a long forecast's output really belongs is a phase 4 question.
        # The stub needs only a file that no other task writes.
        return [start], [paths.member_out("rst", cycle, member) / "extended.stub"]

    # stage.obs, post.obs, post.state, verify: real work with no artifact worth
    # faking. They are here to exercise arrays, leaves and `afterany`.
    return [], []


# --- dispatch ----------------------------------------------------------------

def run_task(config, site, paths, cycle, task, member=None):
    """Run one job to completion. Raises TaskError on anything unrecoverable."""
    sentinel = paths.sentinel(cycle, task, member)
    inputs, outputs = stub_io(config, paths, task, cycle, member)

    if sentinel.exists() and all(p.exists() for p in outputs):
        # The only safe skip. Output-exists alone is what v2 had, and a job
        # killed during a restart write leaves an output that exists and is
        # truncated.
        print(f"ackbar: {cycle}.{task} already complete, skipping")
        return 0

    scratch = paths.scratch(cycle, task, member)
    scratch.mkdir(parents=True, exist_ok=True)

    started = time.time()
    if task == "submit":
        _submit_next(config, site, paths, cycle)
    elif task == "cleanup":
        _cleanup(config, paths, cycle)
    elif task == "stats":
        _stats(paths, cycle)
    else:
        _stub(config, paths, cycle, task, member, inputs, outputs)

    _write_sentinel(sentinel, cycle, task, member, started)

    # Scratch is deleted by the task itself on success and kept on failure, so
    # a failed cycle leaves everything needed to debug it and a successful one
    # leaves nothing.
    shutil.rmtree(scratch, ignore_errors=True)
    return 0


def _write_sentinel(sentinel, cycle, task, member, started):
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cycle": cycle,
        "task": task,
        "member": member,
        "job_id": os.environ.get("SLURM_JOB_ID", ""),
        "array_job_id": os.environ.get("SLURM_ARRAY_JOB_ID", ""),
        "restarts": int(os.environ.get("SLURM_RESTART_COUNT", "0") or 0),
        "seconds": round(time.time() - started, 3),
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _commit(sentinel, json.dumps(payload, indent=2).encode() + b"\n")


def _commit(target, payload):
    """Write to a temporary path in the same directory, then rename.

    Same directory because rename is only atomic within a filesystem, and
    scratch and output are different filesystems on every real machine.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    with open(temp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


# --- the stub ----------------------------------------------------------------

def _stub(config, paths, cycle, task, member, inputs, outputs):
    stub = config.get("model", {}).get("stub")
    if stub is None:
        raise TaskError(
            f"{task} has no implementation yet and the model is "
            f"{config['model']['name']!r} rather than 'stub'. Phase 2 runs the "
            f"stub; the real task bodies arrive with their science phases."
        )

    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        raise TaskError(
            f"{cycle}.{task} is missing {len(missing)} input(s) its producer "
            f"should have written: {', '.join(sorted(missing)[:4])}"
        )

    fault = _fault_for(stub, cycle, task, member)
    if fault == "requeue":
        _requeue(cycle, task)
    if fault == "exit_nonzero":
        raise TaskError(f"{cycle}.{task} member {member}: injected failure")
    if fault == "overrun_memory":
        _burn_memory()

    seconds = float(stub.get("seconds", 0))
    if fault == "overrun_time":
        # Slurm kills this at the job's --time. Sleeping a fixed hour rather
        # than reading the limit keeps the injector independent of the
        # resources the experiment happens to declare.
        seconds = 3600
    _burn_time(seconds)

    if fault == "write_nothing":
        # Exit 0 having produced nothing. Nothing in Slurm notices, which is
        # the point: the consumer's input check is the only thing that can.
        print(f"ackbar: {cycle}.{task} injecting write_nothing")
        return

    payload = b"\0" * int(stub.get("bytes", 0))
    header = f"ackbar stub {task} cycle={cycle} member={member}\n".encode()
    for target in outputs:
        _commit(target, header + payload)


def _fault_for(stub, cycle, task, member):
    """Which fault, if any, this exact job is told to inject.

    Deterministic by construction: the same job on a heal injects the same
    fault, which is what makes the fault matrix a regression test rather than a
    story about one afternoon.
    """
    fail = stub.get("fail") or {}
    for name in FAULTS:
        for pattern in fail.get(name) or ():
            if selector_matches(pattern, cycle, task, member):
                print(f"ackbar: {cycle}.{task} member {member} injecting "
                      f"{name} (matched {pattern!r})")
                return name
    return None


def selector_matches(pattern, cycle, task, member):
    """`<cycle>.<task>[.<member>]`, each field a shell glob.

    Split from the ends rather than on every dot, because task names contain
    dots themselves: `2.forecast.ext` names a task, not member `ext`. The
    trailing field is read as a member only when it is a number or `*`, which
    is unambiguous as long as no task name ends in a numeric component.
    """
    fields = pattern.split(".")
    if len(fields) < 2:
        raise TaskError(
            f"fault selector {pattern!r} needs at least <cycle>.<task>"
        )
    cycle_pattern, rest = fields[0], fields[1:]
    if len(rest) > 1 and (rest[-1] == "*" or rest[-1].isdigit()):
        task_pattern, member_pattern = ".".join(rest[:-1]), rest[-1]
    else:
        task_pattern, member_pattern = ".".join(rest), "*"

    return (
        fnmatch.fnmatchcase(str(cycle), cycle_pattern)
        and fnmatch.fnmatchcase(task, task_pattern)
        and fnmatch.fnmatchcase(str(0 if member is None else member),
                                member_pattern)
    )


def _burn_time(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def _burn_memory():
    """Allocate past the job's own request until the cgroup kills it.

    `SLURM_MEM_PER_NODE` is in megabytes. Touching every page matters: an
    untouched allocation is virtual and the cgroup never sees it.
    """
    limit = int(os.environ.get("SLURM_MEM_PER_NODE", "512") or 512)
    print(f"ackbar: injecting overrun_memory against {limit}M")
    chunks = []
    for _ in range(2 * limit + 64):
        chunks.append(bytearray(1024 * 1024))
        chunks[-1][::4096] = b"\1" * len(chunks[-1][::4096])


def _this_job():
    """How Slurm wants *this* job named, which for an array element is
    `<array id>_<index>` and not `$SLURM_JOB_ID`.

    `SLURM_JOB_ID` inside an array element is that element's own id, and passing
    it to `scontrol requeue` takes the **whole array** with it: every sibling is
    killed and rerun, including the ones that had already finished. That is not
    a hypothetical. It is what turned a one member fault into a three member
    one, and it cost two minutes of Slurm's post-requeue deferral each time.
    """
    array, index = (os.environ.get("SLURM_ARRAY_JOB_ID"),
                    os.environ.get("SLURM_ARRAY_TASK_ID"))
    if array and index:
        return f"{array}_{index}"
    return os.environ.get("SLURM_JOB_ID")


def _requeue(cycle, task):
    """Requeue this job once, mid-task, and let the rerun finish it.

    Guarded on `SLURM_RESTART_COUNT` so the fault fires exactly once; without
    that it is an infinite loop, which is a fault of the test rather than of
    the workflow.
    """
    if int(os.environ.get("SLURM_RESTART_COUNT", "0") or 0):
        print(f"ackbar: {cycle}.{task} resumed after requeue")
        return
    job_id = _this_job()
    if not job_id:
        raise TaskError("requeue was injected outside Slurm")
    print(f"ackbar: {cycle}.{task} requeueing job {job_id}")
    subprocess.run(["scontrol", "requeue", job_id], check=False)
    # Slurm signals the job asynchronously; wait rather than racing ahead and
    # writing the outputs the requeue is supposed to interrupt.
    time.sleep(120)
    raise TaskError("requeue did not take effect")


# --- the tasks that are real from day one ------------------------------------

def _submit_next(config, site, paths, cycle):
    """Cycle n's job that submits cycle n+1. This is what makes cycling work.

    Fail-stop by construction: this job is `afterok` on the forecast, so a
    failed cycle stops the chain rather than producing cycles of garbage off a
    bad background.
    """
    from .submit import submit_cycle

    if paths.halt_flag.exists():
        # The normal way to stop. Exit 0, so the graph drains and the
        # experiment ends at a clean cycle boundary rather than looking failed.
        print(f"ackbar: {paths.halt_flag} exists, stopping after cycle {cycle}")
        return

    nxt = cycle + 1
    if nxt > config["cycle"]["count"]:
        print(f"ackbar: cycle {cycle} is the last, nothing to submit")
        return
    if nxt in ledger.submitted_cycles(paths):
        # The ledger is the authority on "was this already submitted", and it
        # is the check that survives a marker file being cleaned up by hand.
        print(f"ackbar: cycle {nxt} is already in the ledger, not submitting")
        return

    records = submit_cycle(config, site, paths, nxt)
    for record in records:
        print(f"ackbar: submitted {record['cycle']}.{record['task']} "
              f"as {record['job_id']}")


def _cleanup(config, paths, cycle):
    """Remove restarts nothing can still need, gated on artifact existence.

    Keyed off artifacts rather than job state on purpose: keying off job state
    means a retried cleanup evaluates a regenerated subgraph with new job ids,
    concludes the old consumers are gone, and deletes restarts a resubmitted
    consumer is about to read.

    Cycle n's forecast reads cycle n-1's restarts, so with one cycle in flight
    the earliest set nothing can need is n-2, and it goes only once n-1 is
    complete for every member.
    """
    members = member_set(config)
    keep = cycle - 1
    drop = cycle - 2
    if drop < 0:
        return

    proof = [paths.member_out("rst", keep, m) / "restart.stub" for m in members]
    absent = [str(p) for p in proof if not p.exists()]
    if absent:
        print(f"ackbar: not cleaning cycle {drop}, cycle {keep} is incomplete "
              f"({len(absent)} restart(s) missing)")
        return

    target = paths.cycle_out("rst", drop)
    if target.exists():
        shutil.rmtree(target)
        print(f"ackbar: removed {target}")


def _stats(paths, cycle):
    """The per-cycle resource harvest.

    A placeholder in phase 2: the `sacct --json` join lands in phase 3, and
    what this proves now is that an `afterany` leaf runs when its parents
    failed, which is exactly when the harvest is most wanted.
    """
    _commit(paths.stats_file(cycle), json.dumps({
        "cycle": cycle,
        "harvested": False,
        "note": "sacct harvest arrives in phase 3; see docs/build-order.md",
    }, indent=2).encode() + b"\n")
