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
from pathlib import Path

from . import (ensemble, ledger, mom6sis2, observations, persistence, soca,
               writeback)
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

def restart_source(config, paths, cycle, member):
    """The directory cycle *n*'s forecast starts from.

    One definition, used by the stub and by the real model, because the two
    disagreeing is a bug that shows up as a forecast quietly starting from the
    background it was supposed to have corrected. With an analysis the forecast
    starts from the writeback's product; without one it is the previous cycle's
    restart set handed straight across.
    """
    if config.get("solver", {}).get("name", "none") != "none":
        return paths.member_out("ana", cycle, member)
    return paths.member_out("rst", cycle - 1, member)


def stub_io(config, paths, task, cycle, member):
    """(inputs, outputs) for one stub job, as absolute paths."""
    members = member_set(config)
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
        start = restart_source(config, paths, cycle, member) / "restart.stub"
        return [start], [rst(cycle, member)]
    if task == "forecast.ext":
        start = restart_source(config, paths, cycle, member) / "restart.stub"
        # Where a long forecast's output really belongs is a phase 4 question.
        # The stub needs only a file that no other task writes.
        return [start], [paths.member_out("rst", cycle, member) / "extended.stub"]

    # stage.obs, post.obs, post.state, verify: real work with no artifact worth
    # faking. They are here to exercise arrays, leaves and `afterany`.
    return [], []


# --- dispatch ----------------------------------------------------------------

#: Tasks that must run again on a resubmission even though they already
#: succeeded. Both report on or maintain the cycle as a whole rather than
#: producing an artifact of their own, so "already done" is a claim about a
#: cycle that a heal has since changed.
#:
#: `stats` harvests the accounting for its cycle; after a heal the cycle
#: contains different jobs, and skipping leaves a file describing the run that
#: was abandoned. `cleanup` refuses to delete while the cycle it is keeping is
#: incomplete, which is exactly the state a failure leaves behind: skipping
#: means the one run that refused is the only run there will ever be, and the
#: restarts leak for the life of the experiment.
RERUN_ALWAYS = ("stats", "cleanup")


#: Tasks whose bodies arrive with a later phase and which, until then, do
#: nothing at all under a real model. Delete an entry when its body lands.
#:
#: The line is not "unimplemented", it is **produces nothing anything else
#: reads**. Each of these is a leaf or a stage whose absence is visible in what
#: it did not write, so skipping it costs a diagnostic and cannot corrupt a
#: forecast. A task in the data path with no body is the opposite case and stays
#: an error: `writeback` quietly doing nothing means every cycle after it
#: forecasts from an unanalysed state and the experiment looks fine throughout.
#:
#: They run rather than being cut from the graph on purpose. The edges are the
#: part that is hard to get right, and a phase that adds a body should not also
#: be the phase that first discovers its dependencies were wrong.
#:
#: `b.vt` is here for a reason worth stating, because it is the one entry that
#: is in the data path on paper. The vertical background error scales *are*
#: calibrated, offline and once, by `tools/soca-diffusion.sh`, and the analysis
#: reads that product out of the domain's static directory. Recalibrating them
#: every cycle against the current mixed layer is a measured improvement on
#: that, not a prerequisite for it, so until it lands this task genuinely
#: produces nothing anything reads. What closes it is the vertical `filepath` in
#: `config/layers/da/variational.yaml` naming a per-cycle file instead of a
#: static one; at that moment this entry has to go, and the analysis would fail
#: on a missing input rather than silently reading last month's mixed layer.
#: `recenter` is here for a reason that is not "not written yet". Recentring an
#: ensemble onto a centre it already has is the identity, and the centre of the
#: analysis ensemble an LETKF produces *is* its own mean: `LocalEnsembleDA`
#: computes that mean and ACKBAR hands it to the control member. So for every
#: solver implemented so far the recentring is a no-op, and running
#: `soca_ensrecenter.x` to compute one would be a job per cycle to confirm it.
#: What brings a body is a centre that is not the ensemble's own mean, which is
#: a hybrid: there the deterministic analysis is the centre and the ensemble is
#: pulled onto it. At that moment this entry goes and the task gets an
#: executable in the same change.
DEFERRED = ("b.vt", "recenter", "post.obs", "post.state", "verify")


def deferred_task(config, task):
    """Whether this job is one of the bodies that has not been written yet.

    Only under a real model. Under the stub every task has a body, because the
    stub *is* the body, and taking this path there would remove the fan-out that
    tier 2 exists to exercise.
    """
    return config["model"]["name"] != "stub" and task in DEFERRED


#: Models whose restart set is a real MOM6 one: written by MOM6, on the domain's
#: own grid, readable by SOCA. `persistence` is in here because it does not
#: produce a restart set of its own kind, it hands MOM6's along, which is
#: exactly what makes it useful for bringing the analysis path up without paying
#: for the model.
REAL_STATE = ("mom6sis2", "persistence")


def real_model(config, task):
    """Whether this exact job runs a forecast rather than the stub.

    Only `forecast`. `forecast.ext` is the same executable with a different
    diag_table and a different product (diagnostics to score, not a restart set
    anything reads), and it arrives with the phase that scores them; until then
    it falls through to the stub's "no implementation yet", which says so.
    """
    return config["model"]["name"] in REAL_STATE and task == "forecast"


#: Solvers with an implemented analysis. Anything else falls through to the
#: stub, which says it has no implementation rather than running the wrong one.
SOLVERS = ("variational", "letkf")


def real_analysis(config, task):
    """Whether this job runs a real analysis application."""
    return (task == "da"
            and config["model"]["name"] in REAL_STATE
            and config.get("solver", {}).get("name") in SOLVERS)


def real_writeback(config, task):
    """Whether this job builds the analysed restart set.

    Solver-independent, unlike the analysis itself: writeback reads a state and
    a background and writes a restart set, and none of that depends on how the
    state was arrived at.
    """
    return task == "writeback" and config["model"]["name"] in REAL_STATE


def real_observations(config, task):
    """Whether this job runs the real observation path rather than the stub.

    Two different answers, because the two tasks depend on different things.
    `stage.obs` is real under any model but the stub: deciding which observers
    have a file for this window is a file check and a written list, and nothing
    in it is model specific. `hofx` is real only under mom6sis2, because it
    evaluates a background that SOCA has to be able to read, and under any other
    model it falls through to the stub's "no implementation yet", which says so.
    """
    if config["model"]["name"] == "stub":
        return False
    if task == "stage.obs":
        return True
    return task == "hofx" and config["model"]["name"] in REAL_STATE


def background(config, paths, cycle):
    """The state hofx evaluates against: the previous cycle's forecast.

    `restart_source` rather than a second spelling of the same path, so that the
    state hofx reads and the state the next forecast starts from cannot drift
    apart. hofx exists only where there is no analysis, so this is always the
    restart set handed straight across, and for cycle 1 it is `rst/0`, the
    materialized initial condition. There is no member index: an experiment with
    no analysis and no ensemble has one member, and an ensemble hofx is the
    ensemble phase's problem.
    """
    return restart_source(config, paths, cycle, 0)


def analysis_background(paths, cycle, member=0):
    """The state the analysis corrects: the previous cycle's forecast.

    Always `rst/<n-1>`, and deliberately not `restart_source`. With an analysis
    configured that function answers a different question, where the *forecast*
    starts, and its answer is this task's own downstream output. Reading it here
    would make the analysis correct the analysis.
    """
    return paths.member_out("rst", cycle - 1, member)


def restart_stamp(config):
    """The one file whose presence means a member's restart set is whole.

    Asked by `cleanup`, which deletes restarts and therefore has to be right
    about which ones still exist, and by the skip rule. The stub's answer and
    the model's are different files, and hardcoding either is a cleanup that
    silently refuses forever under the other one.
    """
    return mom6sis2.STAMP if config["model"]["name"] in REAL_STATE else "restart.stub"


def task_io(config, paths, task, cycle, member):
    """(inputs, outputs) for one job, whatever is running it."""
    if real_model(config, task):
        stamp = restart_stamp(config)
        source = restart_source(config, paths, cycle, member)
        return [source / stamp], [paths.member_out("rst", cycle, member) / stamp]
    if real_analysis(config, task):
        return _analysis_io(config, paths, cycle)
    if real_writeback(config, task):
        return _writeback_io(config, paths, cycle, member)
    if real_observations(config, task):
        return _observation_io(config, paths, task, cycle)
    return stub_io(config, paths, task, cycle, member)


def analysis_state(config, paths, cycle, member=0):
    """The analysis `da` writes and `writeback` reads, or None if it wrote none.

    None when the cycle assimilated nothing, which is a real state of a real
    experiment rather than a failure: the analysis in a window with no
    observations is the background. Answered from the realized observer list, so
    that "was there anything to assimilate" is decided once, by `stage.obs`, and
    read everywhere else.
    """
    if not _assimilated(config, paths, cycle):
        return None
    return analysis_dir(paths, cycle, member) / soca.analysis_file(config, cycle)


def analysis_dir(paths, cycle, member=0):
    """Where the analysis application's own products go.

    Inside the member's analysed restart set rather than beside its files. See
    `soca.PRODUCTS` for why that is a directory and not a naming convention.
    """
    return paths.member_out("ana", cycle, member) / soca.PRODUCTS


def _assimilated(config, paths, cycle):
    if not paths.observer_list(cycle).exists():
        return False
    return any(record["present"] for record in observations.read(paths, cycle))


def _analysis_io(config, paths, cycle):
    """What the analysis reads and writes.

    The outputs come from the realized observer list for the same reason hofx's
    do: the configuration names every observer and only the staged ones ran, and
    before that list exists there is nothing to declare, which is exactly when
    the skip rule must not fire.

    An ensemble analysis reads every member's background and writes every
    member's analysis, which is what makes it one job rather than an array. It
    is declared that way rather than through the control alone, so that a member
    whose forecast is missing is a missing *input* the divergence policy is
    asked about rather than a file nobody looked for.
    """
    members = analysed_members(config)
    stamp = restart_stamp(config)
    inputs = [analysis_background(paths, cycle, member) / stamp
              for member in members]
    inputs.append(paths.observer_list(cycle))

    if analysis_state(config, paths, cycle, members[0]) is None:
        return inputs, []

    outputs = [analysis_state(config, paths, cycle, member) for member in members]
    outputs += [Path(record["output"])
                for record in observations.read(paths, cycle)
                if record["present"]]
    return inputs, outputs


def analysed_members(config):
    """Which members this cycle's analysis reads and writes.

    An ensemble filter consumes every member and produces an analysis for each,
    plus the mean, which is the control's. Everything else is one deterministic
    analysis of one background, and that background is the control's.

    One function rather than a condition at each of the places that need it,
    because a hybrid is the case where a variational analysis reads every member
    and still writes only one, and that asymmetry has to arrive in a single
    place or not at all.
    """
    members = member_set(config)
    if config.get("solver", {}).get("name") == "letkf":
        return members
    return members[:1]


def _writeback_io(config, paths, cycle, member):
    """What writeback reads and writes.

    Its output is a whole restart set and its proof is the same file the model's
    own is: `coupler.res`, written last. Declaring the directory instead would
    make a half-copied set look finished.
    """
    stamp = restart_stamp(config)
    inputs = [analysis_background(paths, cycle, member) / stamp]
    state = analysis_state(config, paths, cycle, member)
    if state is not None:
        inputs.append(state)
    return inputs, [paths.member_out("ana", cycle, member) / stamp]


def _observation_io(config, paths, task, cycle):
    """What the observation tasks read and write.

    `stage.obs` deliberately declares no inputs. Its whole job is that an
    observation file may or may not be there, so a missing one is the normal
    case rather than a reason to refuse to start, and it is the realized list
    that records which way it went.

    hofx's outputs come from that list rather than from the configuration,
    because the configuration names every observer and only the staged ones ran.
    Before the list exists there is nothing to declare, which is exactly when
    the skip rule must not fire.
    """
    if task == "stage.obs":
        return [], [paths.observer_list(cycle)]

    inputs = [background(config, paths, cycle) / restart_stamp(config),
              paths.observer_list(cycle)]
    if not paths.observer_list(cycle).exists():
        return inputs, []
    outputs = [Path(record["output"])
               for record in observations.read(paths, cycle)
               if record["present"]]
    return inputs, outputs


def run_task(config, site, paths, cycle, task, member=None):
    """Run one job to completion. Raises TaskError on anything unrecoverable."""
    sentinel = paths.sentinel(cycle, task, member)
    inputs, outputs = task_io(config, paths, task, cycle, member)

    if task not in RERUN_ALWAYS and sentinel.exists() and all(p.exists() for p in outputs):
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
        _stats(site, paths, cycle)
    elif real_model(config, task):
        _forecast(config, site, paths, cycle, task, member)
    elif real_analysis(config, task):
        _analysis(config, site, paths, cycle, task)
    elif real_writeback(config, task):
        _writeback(config, paths, cycle, task, member)
    elif task == "stage.obs" and real_observations(config, task):
        _stage_obs(config, paths, cycle)
    elif task == "hofx" and real_observations(config, task):
        _hofx(config, site, paths, cycle, task)
    elif deferred_task(config, task):
        print(f"ackbar: {cycle}.{task} has no body yet and writes nothing that "
              f"anything reads; see DEFERRED in ackbar/run.py")
    else:
        _stub(config, paths, cycle, task, member, inputs, outputs)

    _write_sentinel(sentinel, cycle, task, member, started,
                    deferred=deferred_task(config, task))

    # Scratch is deleted by the task itself on success and kept on failure, so
    # a failed cycle leaves everything needed to debug it and a successful one
    # leaves nothing.
    shutil.rmtree(scratch, ignore_errors=True)
    return 0


def _write_sentinel(sentinel, cycle, task, member, started, deferred=False):
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cycle": cycle,
        "task": task,
        "member": member,
        # Recorded, because a run whose diagnostics were never computed looks
        # exactly like one whose diagnostics were computed and came out empty.
        "deferred": deferred,
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


# --- the real model ----------------------------------------------------------

def _forecast(config, site, paths, cycle, task, member):
    """One integration, or one state handed forward.

    The input check the stub does is inside the model module instead, because a
    restart *set* is complete or not, which is a different question from whether
    a list of files exists, and the model has to answer it before it builds a
    run directory around them.
    """
    source = restart_source(config, paths, cycle, member)
    target = paths.member_out("rst", cycle, member)
    try:
        if config["model"]["name"] == "persistence":
            persistence.forecast(config, paths, cycle, task, member,
                                 source=source, target=target)
        else:
            mom6sis2.forecast(config, site, paths, cycle, task, member,
                              source=source, target=target)
    except mom6sis2.ModelError as error:
        raise TaskError(f"{cycle}.{task} member {member}: {error}") from error


# --- the analysis ------------------------------------------------------------

def _analysis(config, site, paths, cycle, task):
    """Solve for this cycle's analysis and its departures."""
    try:
        observers = observations.selected(config, paths, cycle)
        if config["solver"]["name"] == "letkf":
            written = _ensemble_analysis(config, site, paths, cycle, task,
                                         observers)
        else:
            member = member_set(config)[0]
            written = soca.analysis(
                config, site, paths, cycle, task,
                background=analysis_background(paths, cycle, member),
                observers=observers,
                target=analysis_dir(paths, cycle, member),
            )
    except (mom6sis2.ModelError, observations.ObservationError) as error:
        raise TaskError(f"{cycle}.{task}: {error}") from error

    for path in written:
        print(f"ackbar: wrote {path}")


def _ensemble_analysis(config, site, paths, cycle, task, observers):
    """One LETKF over the whole ensemble.

    The divergence policy is applied here rather than inside the application,
    because a member with no background is a fact about the workflow and not
    about the filter: SOCA handed a path that does not exist aborts on the read,
    which is the right answer for exactly one of the three policies.
    """
    members = ensemble.resolve(
        config, paths, cycle,
        ensemble.ensemble_members(config, member_set(config)),
        stamp=restart_stamp(config))

    return soca.letkf(
        config, site, paths, cycle, task,
        backgrounds=paths.cycle_out("rst", cycle - 1),
        observers=observers,
        members=members,
        target=lambda member: analysis_dir(paths, cycle, member),
    )


def _writeback(config, paths, cycle, task, member):
    """Turn this cycle's analysis into the restart set the forecast reads."""
    try:
        writeback.writeback(
            config, paths, cycle, member,
            background=analysis_background(paths, cycle, member),
            analysis=analysis_state(config, paths, cycle, member),
            target=paths.member_out("ana", cycle, member),
        )
    except mom6sis2.ModelError as error:
        raise TaskError(f"{cycle}.{task} member {member}: {error}") from error


# --- observations ------------------------------------------------------------

def _stage_obs(config, paths, cycle):
    """Decide this cycle's observer set and write the list that records it.

    Exits 0 having dropped every observer if the archive has nothing for this
    window. That is deliberate: a gap is a property of the archive, the cycle
    after it is unaffected, and failing here would stop a fifty cycle experiment
    over a real record. An observer marked `required` is how an experiment says
    that its own gap is not acceptable.
    """
    try:
        records = observations.realize(config, paths, cycle)
    except observations.ObservationError as error:
        raise TaskError(f"{cycle}.stage.obs: {error}") from error

    dropped = [r["name"] for r in records if not r["present"]]
    print(f"ackbar: cycle {cycle} staged "
          f"{sum(1 for r in records if r['present'])}/{len(records)} observer(s) "
          f"-> {paths.observer_list(cycle)}")
    for name in dropped:
        print(f"ackbar:   dropped {name}, no input file for this window")


def _hofx(config, site, paths, cycle, task):
    """Evaluate the staged observers against this cycle's background."""
    try:
        written = soca.hofx(
            config, site, paths, cycle, task,
            background=background(config, paths, cycle),
            observers=observations.selected(config, paths, cycle),
        )
    except (mom6sis2.ModelError, observations.ObservationError) as error:
        raise TaskError(f"{cycle}.{task}: {error}") from error

    for path in written:
        print(f"ackbar: wrote {path}")


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

    Both restart directories go, not just `rst/`. In a DA run the forecast
    starts from `ana/<n>/mem###`, which `writeback` fills with a whole restart
    set per member per cycle, so `ana/` grows at the same rate as `rst/` and for
    the same reason. Reaping one and not the other is how an experiment that
    cycles happily for a week fills the disk in the second week. `obs_out/` is
    deliberately not here: the departures are the experiment's product rather
    than an intermediate, and they are kilobytes where these are gigabytes.
    """
    members = member_set(config)
    keep = cycle - 1
    drop = cycle - 2
    if drop < 0:
        return

    stamp = restart_stamp(config)
    proof = [paths.member_out("rst", keep, m) / stamp for m in members]
    absent = [str(p) for p in proof if not p.exists()]
    if absent:
        print(f"ackbar: not cleaning cycle {drop}, cycle {keep} is incomplete "
              f"({len(absent)} restart(s) missing)")
        return

    # `ana/<drop>` is safe under the same proof: it is what `forecast(drop)`
    # started from, and `rst/<drop>` existing is what says that forecast ran.
    for kind in ("rst", "ana"):
        target = paths.cycle_out(kind, drop)
        if target.exists():
            shutil.rmtree(target)
            print(f"ackbar: removed {target}")


def _stats(site, paths, cycle):
    """The per-cycle resource harvest.

    An `afterany` leaf, so it runs whatever happened, which is the point: this
    is the task most wanted exactly when something failed. It harvests its own
    cycle including the jobs that are still running alongside it, so a row can
    be incomplete; `ackbar status` reads the scheduler, not this file.
    """
    from .harvest import write

    payload = write(paths, cycle, launcher=site.get("launcher", ""))
    totals = payload.get("totals", {})
    print(f"ackbar: harvested {totals.get('jobs', 0)} job(s) for cycle {cycle}, "
          f"{totals.get('core_seconds', 0)} core seconds, "
          f"peak RSS {totals.get('max_rss_kb', 0)}K")
