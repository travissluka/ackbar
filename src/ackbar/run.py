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

from . import (ensemble, ledger, mom6sis2, observations, persistence, post,
               soca, writeback)
from .config.jobtime import (FOUR_D, cycle_length, cycle_time,
                             forecast_overshoot, handoff_time, slot_times,
                             window_bounds, window_type)
from .duration import format_duration, format_instant, parse_duration
from .graph.build import (extended_cycles, extended_lead_cycles, extended_leads,
                          extended_length, extended_members, extended_slots,
                          member_set)
from .paths import REAPED, cycle_of, lead_name
from .graph.tasks import BY_NAME, ensemble_covariance, ensemble_source

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
    others = ensemble.ensemble_members(config, members)

    def rst(c, m):
        return paths.member_out("rst", c, m) / "restart.stub"

    def ana(c, m, name):
        return paths.member_out("ana", c, m) / name

    if task == "b.vt":
        return [rst(cycle - 1, members[0])], [paths.cycle_out("ana", cycle) / "vt.stub"]
    if task == "da":
        # One job consuming every member, which is what LETKF is, what a hybrid
        # is, and what a variational run with one member degenerates to.
        return [rst(cycle - 1, m) for m in members], \
               [ana(cycle, m, "incr.stub") for m in analysed_members(config)]
    if task == "da.ens":
        return [rst(cycle - 1, m) for m in others], \
               [ana(cycle, m, "incr.ens.stub") for m in others]
    if task == "hofx":
        return [rst(cycle - 1, m) for m in members], \
               [paths.cycle_out("obs_out", cycle) / "hofx.stub"]
    if task == "recenter":
        # One job over the whole ensemble, like the applications it stands in
        # for: the mean it subtracts belongs to every member at once.
        inputs = [ana(cycle, members[0], "incr.stub")]
        if maintains_ensemble(config):
            inputs += [ana(cycle, m, "incr.ens.stub") for m in others]
        else:
            inputs += [rst(cycle - 1, m) for m in others]
        return inputs, [ana(cycle, m, "incr.rc.stub") for m in others]
    if task == "writeback":
        recentred = recentres(config) and member != members[0]
        source = "incr.rc.stub" if recentred else "incr.stub"
        return [ana(cycle, member, source), rst(cycle - 1, member)], \
               [ana(cycle, member, "restart.stub")]
    if task == "forecast":
        start = restart_source(config, paths, cycle, member) / "restart.stub"
        # The sub-window states as well, so that tier 2 exercises `bkg/` and
        # its reaping on the scheduler rather than leaving both to first run
        # under the real model, where a cycle costs a hundred times as much.
        slots = [paths.slot_out(cycle, member, when) / "restart.stub"
                 for when in slot_times(config, cycle)]
        return [start], [rst(cycle, member)] + slots
    if task == "forecast.ext":
        start = restart_source(config, paths, cycle, member) / "restart.stub"
        # Deliberately not `rst/`, which is where this used to write. Both
        # forecasts start from the same set, so a state the long one wrote at a
        # time the cycling one also reaches is a different trajectory from that
        # point on, and `commit` deletes whatever it was not asked to claim. The
        # stub exercises the separation on the scheduler, where a cycle is
        # seconds, rather than leaving it to first run under the real model.
        return [start], [paths.fcst_out(cycle, member, lead) / "restart.stub"
                         for lead in extended_slots(config)]
    if task == "hofx.ext":
        return [paths.fcst_out(cycle, member, lead) / "restart.stub"
                for lead in extended_slots(config)], \
               [paths.fcst_obs(cycle, extended_length(config), member)
                / "hofx.stub"]
    if task == "post.fcst":
        return [paths.fcst_out(cycle, member, lead) / "restart.stub"
                for lead in stored_leads(config)], \
               [paths.fcst_product(cycle, lead, member)
                for lead in stored_leads(config)]

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
DEFERRED = ("b.vt", "verify")


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

    Both forecasts. They are the same executable run for different lengths, at
    different write cadences, into different directories, and every one of those
    three differences reaches the model through `symbols(..., task=...)` and
    `_forecast`'s target. Nothing about the model layer distinguishes them.
    """
    return config["model"]["name"] in REAL_STATE and \
        task in ("forecast", "forecast.ext")


#: Solvers with an implemented analysis. Anything else falls through to the
#: stub, which says it has no implementation rather than running the wrong one.
SOLVERS = ("variational", "letkf")

#: The two analysis nodes. `da` produces the control's answer, whichever solver
#: that is; `da.ens` maintains the ensemble a hybrid's covariance is drawn from,
#: and exists only where something has to.
ANALYSES = ("da", "da.ens")


def recentres(config):
    """Whether this experiment pulls its ensemble onto the deterministic analysis.

    True for exactly the covariances that read an ensemble, and false for an
    LETKF, where the centre of the analysis ensemble is already its own mean and
    the recentring is the identity.
    """
    return ensemble_covariance(config)


def maintains_ensemble(config):
    """Whether a filter in this cycle updates the ensemble with observations."""
    return recentres(config) and ensemble_source(config) == "letkf"


def real_analysis(config, task):
    """Whether this job runs a real analysis application."""
    return (task in ANALYSES
            and config["model"]["name"] in REAL_STATE
            and config.get("solver", {}).get("name") in SOLVERS)


def real_recenter(config, task):
    """Whether this job pulls the ensemble onto the deterministic analysis."""
    return (task == "recenter"
            and config["model"]["name"] in REAL_STATE
            and recentres(config))


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
    return task in ("hofx", "hofx.ext") and config["model"]["name"] in REAL_STATE


def real_post(config, task):
    """Whether this job runs a real post-processing body.

    Both bodies reduce files the model and the analysis wrote, so both need a
    model that writes them. Under the stub they fall through to `_stub`, which
    is the right answer and not a gap: tier 2 exists to exercise the array
    fan-out and the leaf edges of these two tasks, and a stub that summarized
    nothing would still exercise both.

    `post.obs` is not gated on the experiment having observers, because
    `BY_NAME["post.obs"].when` already is: the node does not exist without them.
    """
    return (task in ("post.obs", "post.state", "post.fcst")
            and config["model"]["name"] in REAL_STATE)


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


def analysis_trajectory(config, paths, cycle, member=0):
    """The states a 4D window compares its observations against, or None.

    Cycle *n-1*'s sub-window states, which `slot_times` computes to be exactly
    cycle *n*'s window: the forecast overshoots by half a window so the far end
    of this one exists, and writes at `forecast.slots` on the way. Nothing else
    can produce them, because this cycle's own forecast starts from the analysis
    these states are what compute.

    Keyed by valid time rather than returned as a list, so the caller that needs
    the state at the window's start can ask for it by name and not by position.

    None for cycle 1 as well as for a 3D window, and it means the same thing in
    both: there is no trajectory to read. Nothing ran before the first cycle, so
    its background is the staged initial condition and its window holds one
    state; `soca.cost_template` is what turns that into a 3D-Var, and it
    distinguishes this None from an empty mapping, which would be a predecessor
    that ran and wrote nothing.
    """
    if window_type(config) not in FOUR_D or cycle == 1:
        return None
    return {when: paths.slot_out(cycle - 1, member, when)
            for when in slot_times(config, cycle - 1)}


def analysis_first_guess(config, paths, cycle, member=0):
    """Where the analysis reads its background from.

    For a 3D window that is the previous cycle's restart set, which is valid at
    this cycle's own time. For FGAT and 4D the window starts half a window
    earlier, so the background is the slot at `window_begin` and the restart set
    is where the trajectory *ends* rather than where it starts.

    Cycle 1 takes the 3D answer whatever the window says, because `rst/0` is the
    staged initial condition and there are no slots beside it. It is valid at
    the cycle's own time rather than at the window's start, which is the other
    half of why that cycle solves 3D-Var.
    """
    trajectory = analysis_trajectory(config, paths, cycle, member)
    if trajectory is None:
        return analysis_background(paths, cycle, member)

    begin, _ = window_bounds(config, cycle)
    if begin not in trajectory:
        raise TaskError(
            f"cycle {cycle}'s window begins at {begin:%Y-%m-%dT%H:%M:%SZ} and "
            f"the previous forecast wrote no state there. `forecast.slots` has "
            f"to divide the window, which `graph.build._check_window` refuses "
            f"at build time; reaching here means the forecast did not write "
            f"what it declared."
        )
    return trajectory[begin]


def restart_stamp(config):
    """The one file whose presence means a member's restart set is whole.

    Asked by `cleanup`, which deletes restarts and therefore has to be right
    about which ones still exist, and by the skip rule. The stub's answer and
    the model's are different files, and hardcoding either is a cleanup that
    silently refuses forever under the other one.
    """
    return mom6sis2.STAMP if config["model"]["name"] in REAL_STATE else "restart.stub"


def slot_states(config, paths, cycle, member):
    """The sub-window states this cycle's forecast is asked to write.

    Empty unless `forecast.slots` is set. Each is named by its own valid time
    and holds the ocean restart under the name every other state here has, so
    it is declared exactly the way the restart set is: by the file, not by the
    directory, since a directory that exists proves nothing about what is in it.
    """
    ocean = (config["model"].get("restart") or {}).get("ocn")
    if not ocean:
        return []
    return [paths.slot_out(cycle, member, when) / ocean
            for when in slot_times(config, cycle)]


def task_io(config, paths, task, cycle, member):
    """(inputs, outputs) for one job, whatever is running it."""
    if real_model(config, task):
        stamp = restart_stamp(config)
        source = restart_source(config, paths, cycle, member)
        return [source / stamp], \
               [paths.member_out("rst", cycle, member) / stamp] \
               + slot_states(config, paths, cycle, member)
    if real_analysis(config, task):
        return _analysis_io(config, paths, cycle, task)
    if real_recenter(config, task):
        return _recenter_io(config, paths, cycle)
    if real_writeback(config, task):
        return _writeback_io(config, paths, cycle, member)
    if real_observations(config, task):
        return _observation_io(config, paths, task, cycle)
    if real_post(config, task):
        return _post_io(config, paths, task, cycle, member)
    return stub_io(config, paths, task, cycle, member)


def analysis_state(config, paths, cycle, member=0):
    """The state `writeback` applies to *member*, or None if none was written.

    None when the cycle assimilated nothing, which is a real state of a real
    experiment rather than a failure: the analysis in a window with no
    observations is the background. Answered from the realized observer list, so
    that "was there anything to assimilate" is decided once, by `stage.obs`, and
    read everywhere else.

    For every member but the control this is the *recentred* analysis wherever
    there is one, and that is the whole of what makes a hybrid's ensemble follow
    its own experiment. Skipping it would leave each member cycling around the
    ensemble filter's mean while the run being reported is the deterministic
    one, and the two would drift apart with nothing to say so.
    """
    if not _assimilated(config, paths, cycle):
        return None
    kind = soca.ANALYSIS
    if recentres(config) and member != member_set(config)[0]:
        kind = soca.RECENTERED
    return analysis_product(config, paths, cycle, member, kind)


def analysis_product(config, paths, cycle, member=0, kind=soca.ANALYSIS):
    """One of a member's own state products, by name."""
    return analysis_dir(paths, cycle, member) / soca.product_file(config, cycle, kind)


def analysis_dir(paths, cycle, member=0):
    """Where the analysis application's own products go.

    Inside the member's analysed restart set rather than beside its files. See
    `soca.PRODUCTS` for why that is a directory and not a naming convention.
    """
    return paths.member_out("ana", cycle, member) / soca.PRODUCTS


def ensemble_dir(paths, cycle):
    """Where an ensemble filter's control-level products go in a hybrid.

    See `soca.ENSEMBLE_PRODUCTS`: the filter's mean is not the control's answer
    there, and two of its outputs would otherwise land on the deterministic
    analysis and its increment.
    """
    return analysis_dir(paths, cycle, 0) / soca.ENSEMBLE_PRODUCTS


def _assimilated(config, paths, cycle):
    if not paths.observer_list(cycle).exists():
        return False
    return any(record["present"] for record in observations.read(paths, cycle))


def _analysis_io(config, paths, cycle, task):
    """What an analysis reads and writes.

    The outputs come from the realized observer list for the same reason hofx's
    do: the configuration names every observer and only the staged ones ran, and
    before that list exists there is nothing to declare, which is exactly when
    the skip rule must not fire.

    An analysis that reads an ensemble declares every member's background, which
    is what makes it one job rather than an array. It is declared that way
    rather than through the control alone, so that a member whose forecast is
    missing is a missing *input* the divergence policy is asked about rather
    than a file nobody looked for.
    """
    stamp = restart_stamp(config)
    inputs = [analysis_background(paths, cycle, member) / stamp
              for member in read_members(config, task)]
    inputs.append(paths.observer_list(cycle))

    written = analysed_members(config, task)
    if not _assimilated(config, paths, cycle):
        return inputs, []

    outputs = [analysis_product(config, paths, cycle, member) for member in written]
    outputs += [Path(record["output"]) if task == "da"
                else Path(record["output"]).parent / soca.ENSEMBLE_PRODUCTS
                / Path(record["output"]).name
                for record in observations.read(paths, cycle)
                if record["present"]]
    return inputs, outputs


def analysed_members(config, task="da"):
    """Which members this analysis writes an analysis for.

    An ensemble filter produces one for every member, plus the mean, which in a
    pure LETKF is the control's. Everything else is one deterministic analysis
    of one background, and that background is the control's.

    One function rather than a condition at each of the places that need it,
    because a hybrid is the case where a variational analysis reads every member
    and still writes only one, and that asymmetry has to arrive in a single
    place or not at all.
    """
    members = member_set(config)
    if task == "da.ens":
        return ensemble.ensemble_members(config, members)
    if config.get("solver", {}).get("name") == "letkf":
        return members
    return members[:1]


def read_members(config, task="da"):
    """Which members' backgrounds this analysis reads.

    Not the same question as which it writes, and the gap between the two is
    exactly what a hybrid is: it reads the whole ensemble, because that ensemble
    is half of its covariance, and it writes one state.
    """
    members = member_set(config)
    if task == "da.ens":
        return ensemble.ensemble_members(config, members)
    if config.get("solver", {}).get("name") == "letkf" or recentres(config):
        return members
    return members[:1]


def _recenter_io(config, paths, cycle):
    """What the recentring reads and writes.

    Its centre is the deterministic analysis and its ensemble is whatever the
    cycle last did to the members: their own analyses where a filter produced
    some, and their backgrounds where nothing did. The second is not a
    degenerate case of the first. It is an ensemble that carries no observation
    information of its own and exists to give the covariance flow dependence,
    which is a cheaper experiment rather than a broken one.
    """
    members = ensemble.ensemble_members(config, member_set(config))
    if not _assimilated(config, paths, cycle):
        return [], []

    inputs = [analysis_product(config, paths, cycle, member_set(config)[0])]
    if maintains_ensemble(config):
        inputs += [analysis_product(config, paths, cycle, member)
                   for member in members]
    else:
        inputs += [analysis_background(paths, cycle, member) / restart_stamp(config)
                   for member in members]
    outputs = [analysis_product(config, paths, cycle, member, soca.RECENTERED)
               for member in members]
    return inputs, outputs


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


def _post_io(config, paths, task, cycle, member):
    """What the post-processing tasks read and write.

    `post.obs` declares no inputs, for the reason `stage.obs` does not: it hangs
    off `afterany` precisely so that it runs when the analysis has failed, and
    the document it writes in that case is the one that says which observer has
    no output. A declared input would make the missing file a refusal instead of
    the finding.

    `post.state` reads this cycle's own forecast output and declares the
    compressed background as its output. The analysis is genuinely optional (a
    free run has none) and is discovered rather than required, so naming either
    side of it here would make every free run's post.state look unsatisfiable.
    """
    if task == "post.obs":
        return [], [paths.obs_summary(cycle)]
    if task == "post.fcst":
        # The kept leads, minus the one `bkg/` already holds: that lead's
        # product is a symlink this task creates, and a symlink whose target has
        # not landed yet does not `exists()`, so declaring it here would make the
        # task look unfinished for as long as `post.state` is still running.
        return [paths.fcst_out(cycle, member, lead) / restart_stamp(config)
                for lead in stored_leads(config)], \
               [paths.fcst_product(cycle, lead, member)
                for lead in stored_leads(config)]
    return [paths.member_out("rst", cycle, member) / restart_stamp(config)], \
           [paths.product("bkg", cycle + 1, member)]


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
    elif real_recenter(config, task):
        _recenter(config, site, paths, cycle, task)
    elif real_writeback(config, task):
        _writeback(config, paths, cycle, task, member)
    elif task == "stage.obs" and real_observations(config, task):
        _stage_obs(config, paths, cycle)
    elif task == "hofx.ext" and real_observations(config, task):
        _hofx_ext(config, site, paths, cycle, task, member)
    elif task == "hofx" and real_observations(config, task):
        _hofx(config, site, paths, cycle, task)
    elif real_post(config, task):
        _post(config, paths, cycle, task, member)
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
    # Only the cycling forecast. An extended forecast integrates past the
    # window on its own cadence, so a state it wrote at the same clock time is
    # a different trajectory, and putting it in the same directory would give
    # the analysis two answers for one slot. That applies to the restart set it
    # ends on as much as to the states along the way, which is why the target
    # moves too: both forecasts start from the same set, and `commit` deletes
    # whatever it was not asked to claim, so one directory for the two of them
    # is the cycling forecast's output being destroyed by the long one.
    slots, handoff = None, None
    if task == "forecast.ext":
        start = cycle_time(config, cycle)
        length = extended_length(config)
        if length is None:
            # Unreachable through the graph, which only creates this node when
            # `forecast.extended` is set. Said plainly anyway, because without
            # it the symptom is an AttributeError from inside the path layer.
            raise TaskError(
                f"{cycle}.{task}: forecast.extended is not configured, so there "
                f"is no length to integrate for and nowhere for the result to go"
            )
        target = paths.fcst_out(cycle, member, length)
        # Every state but the last, which is the unstamped set at the end of the
        # run and is what `target` claims. No handoff: nothing resumes from a
        # long forecast, which is what makes it a leaf.
        slots = {start + lead: paths.fcst_out(cycle, member, lead)
                 for lead in extended_slots(config) if lead != length} or None
    else:
        target = paths.member_out("rst", cycle, member)
    if task == "forecast":
        slots = {when: paths.slot_out(cycle, member, when)
                 for when in slot_times(config, cycle)} or None
        # Only where the window makes the forecast overshoot. Without one the
        # set the next cycle starts from is the last thing the run wrote, and
        # naming a time for it would send `commit` looking for an interval at
        # an hour the model had no reason to write one.
        if forecast_overshoot(config):
            handoff = handoff_time(config, cycle)
    try:
        if config["model"]["name"] == "persistence":
            persistence.forecast(config, paths, cycle, task, member,
                                 source=source, target=target)
        else:
            mom6sis2.forecast(config, site, paths, cycle, task, member,
                              source=source, target=target, slots=slots,
                              handoff=handoff)
    except mom6sis2.ModelError as error:
        raise TaskError(f"{cycle}.{task} member {member}: {error}") from error


# --- the analysis ------------------------------------------------------------

def _analysis(config, site, paths, cycle, task):
    """Solve for this cycle's analysis and its departures."""
    try:
        observers = observations.selected(config, paths, cycle)
        if task == "da.ens" or config["solver"]["name"] == "letkf":
            written = _ensemble_analysis(config, site, paths, cycle, task,
                                         observers)
        else:
            member = member_set(config)[0]
            written = soca.analysis(
                config, site, paths, cycle, task,
                background=analysis_first_guess(config, paths, cycle, member),
                observers=observers,
                ensemble=_covariance_ensemble(config, paths, cycle),
                trajectory=analysis_trajectory(config, paths, cycle, member),
                target=analysis_dir(paths, cycle, member),
            )
    except (mom6sis2.ModelError, observations.ObservationError) as error:
        raise TaskError(f"{cycle}.{task}: {error}") from error

    for path in written:
        print(f"ackbar: wrote {path}")


def _covariance_ensemble(config, paths, cycle):
    """The member backgrounds a hybrid or ensemble covariance is drawn from.

    The *backgrounds*, not the analyses, and that is what a B is: the covariance
    of the forecast error, sampled by an ensemble of forecasts. Reading the
    members' analyses instead would sample an error the assimilation has already
    removed.

    Which members those are is read from the record `da.ens` wrote rather than
    resolved again, wherever there is a `da.ens`. Two jobs applying the
    divergence policy independently is how one half of a hybrid ends up with a
    member the other half rebuilt.
    """
    if not recentres(config):
        return None

    members = ensemble.ensemble_members(config, member_set(config))
    if maintains_ensemble(config):
        record = ensemble.read(paths, cycle)
        if record is None:
            raise TaskError(
                f"cycle {cycle} has no {ensemble.LEDGER}, so which members the "
                f"filter assimilated was never recorded. That file is "
                f"`da.ens`'s and this analysis has to read the same ensemble it "
                f"did."
            )
        members = tuple(record["assimilated"])
    else:
        members = ensemble.resolve(config, paths, cycle, members,
                                   stamp=restart_stamp(config))

    restart = _require_restart(config)
    variables = config["solver"]["background variables"]

    # 4D-Ens-Var reads every member as a trajectory, because its covariance at
    # each sub-window is the ensemble's spread *there*. The states are the ones
    # the previous cycle's forecast already wrote for every member: `forecast`
    # is a member-level array and `mom6_restart_interval` is the slot cadence,
    # so the ensemble half of a 4D window costs storage rather than model.
    #
    # Cycle 1 has no trajectory and solves 3D, so it takes the branch below and
    # reads the staged ensemble initial condition, one state per member.
    if window_type(config) == "4d" and cycle > 1:
        return soca.member_trajectories(
            lambda member, when: paths.slot_out(cycle - 1, member, when) / restart,
            members,
            slots=slot_times(config, cycle - 1),
            variables=variables,
        )

    return soca.member_states(
        lambda member: analysis_background(paths, cycle, member) / restart,
        members,
        date=cycle_time(config, cycle).strftime("%Y-%m-%dT%H:%M:%SZ"),
        variables=variables,
    )


def _require_restart(config):
    restart = (config["model"].get("restart") or {}).get("ocn")
    if not restart:
        raise TaskError("model.restart.ocn is not set, so no member's state "
                        "can be named")
    return restart


def _ensemble_analysis(config, site, paths, cycle, task, observers):
    """One LETKF over the whole ensemble.

    The divergence policy is applied here rather than inside the application,
    because a member with no background is a fact about the workflow and not
    about the filter: SOCA handed a path that does not exist aborts on the read,
    which is the right answer for exactly one of the three policies.

    Where this *is* the experiment's analysis its posterior mean is the
    control's, and where it is a hybrid's ensemble the mean is a diagnostic of
    the ensemble instead. That is the whole of the difference, and it is one
    directory.
    """
    members = ensemble.resolve(
        config, paths, cycle,
        ensemble.ensemble_members(config, member_set(config)),
        stamp=restart_stamp(config))

    beside = task == "da.ens"
    return soca.letkf(
        config, site, paths, cycle, task,
        backgrounds=paths.cycle_out("rst", cycle - 1),
        observers=observers,
        members=members,
        departures=soca.ENSEMBLE_PRODUCTS if beside else None,
        target=lambda member: (ensemble_dir(paths, cycle) if beside and not member
                               else analysis_dir(paths, cycle, member)),
    )


def _recenter(config, site, paths, cycle, task):
    """Pull the ensemble onto the deterministic analysis."""
    if not _assimilated(config, paths, cycle):
        print(f"ackbar: {cycle}.{task} has nothing to recentre; the cycle "
              f"assimilated no observations, so every member's analysis is its "
              f"own background")
        return

    control = member_set(config)[0]
    members = ensemble.ensemble_members(config, member_set(config))
    record = ensemble.read(paths, cycle)
    if record is not None:
        members = tuple(record["assimilated"])

    if maintains_ensemble(config):
        def locate(member):
            return analysis_product(config, paths, cycle, member)
    else:
        restart = _require_restart(config)

        def locate(member):
            return analysis_background(paths, cycle, member) / restart

    try:
        written = soca.recenter(
            config, site, paths, cycle, task,
            center=analysis_product(config, paths, cycle, control),
            ensemble=locate,
            members=members,
            target=lambda member: analysis_dir(paths, cycle, member),
        )
    except mom6sis2.ModelError as error:
        raise TaskError(f"{cycle}.{task}: {error}") from error

    for path in written:
        print(f"ackbar: wrote {path}")


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


def _hofx_ext(config, site, paths, cycle, task, member):
    """Evaluate the long forecast's trajectory, over every cycle it reaches.

    The observers are every covered cycle's own, which is what makes the result
    comparable: a lead is scored against exactly the observations the cycling
    background was scored against at that time. Their output is redirected under
    `fcst/`, and that is not tidiness. The observer layer's `obsdataout` is
    `obs_out/<T>/...`, rendered at the cycle being evaluated, so leaving it
    alone would have a five day forecast overwrite the cycling departures of
    every cycle it reaches.

    Departures are not split by lead here. Each observation carries its own
    time, so lead is `time - initialized` and the split is a grouping the
    comparison does, not a set of files this has to produce.
    """
    start = cycle_time(config, cycle)
    length = extended_length(config)
    section = paths.fcst_obs(cycle, length, member)

    observers = []
    for lead in extended_lead_cycles(config):
        for record in observations.observers(config, cycle + int(
                lead.total_seconds() // cycle_length(config).total_seconds())):
            if not record["present"]:
                continue
            record["output"] = str(section / Path(record["output"]).name)
            observers.append(record)

    states = {start + lead: paths.fcst_out(cycle, member, lead)
              for lead in extended_slots(config)}
    try:
        written = soca.hofx4d(
            config, site, paths, cycle, task,
            initial=restart_source(config, paths, cycle, member),
            states=states, observers=observers,
            tstep=extended_slots(config)[0], begin=start, length=length,
        )
    except (mom6sis2.ModelError, observations.ObservationError) as error:
        raise TaskError(f"{cycle}.{task} member {member}: {error}") from error

    print(f"ackbar: {cycle}.{task} member {member}: {len(observers)} observer(s) "
          f"over {len(states)} state(s) -> {section}")
    for path in written:
        print(f"ackbar: wrote {path}")


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


def _post(config, paths, cycle, task, member):
    """Reduce what this cycle produced to something that outlives it.

    Neither body raises on a missing input. They run under `afterany` and their
    whole purpose at that moment is to record what did and did not appear, so a
    refusal here would delete the diagnostic instead of writing it. What they
    do raise on is a state that exists and is not the shape it claims to be,
    which is a different thing entirely.
    """
    if task == "post.obs":
        # Every configured observer, not only the staged ones: an observer that
        # had no file for this window is a fact about the cycle, and the
        # document that omits it cannot be told from one where it assimilated
        # nothing.
        payload = post.obs_stats(observations.observers(config, cycle),
                                 paths.obs_summary(cycle))
        totals = payload["totals"]
        print(f"ackbar: {cycle}.post.obs: {totals['assimilated']} of "
              f"{totals['count']} observations assimilated across "
              f"{totals['observers']} observer(s), {totals['failed']} with no "
              f"output -> {paths.obs_summary(cycle)}")
        return

    if task == "post.fcst":
        _post_fcst(config, paths, cycle, member)
        return

    # **This cycle records only what this cycle produced.** Its forecast wrote a
    # state valid at the *next* analysis time, which is the next cycle's
    # background, so that is what goes to `bkg/<T(n+1)>`; its analysis is valid
    # now and goes to `ana/<T(n)>`.
    #
    # Reading the previous cycle's restart instead would put the same numbers in
    # the same files, and it cost two things. It made this task a consumer of
    # the set `cleanup` drops, so the two raced and the proof that settles the
    # race made the last cleanup of a run refuse, permanently, because nothing
    # comes after it to try again. And the final cycle's forecast was recorded
    # by nobody: `post.state` only ever looked backwards, so the last state the
    # experiment produced had no `bkg/` file and its restart was reaped.
    #
    # The cost is that `bkg/` starts one cycle in. The state at the first
    # analysis time is the offline initial condition, which is read-only in the
    # static root and is not this experiment's output to record.
    gridspec = Path(config["domain"]["static"]) / soca.GRIDSPEC
    end = cycle_time(config, cycle + 1)

    # (source directory, valid time, product kind), in the order they are
    # written. The restart first, so that if a slot at the same instant slipped
    # through the filter below the complete state would still be the one on
    # disk.
    records = [(paths.member_out("rst", cycle, member), end, "bkg")]

    analysed = paths.member_out("ana", cycle, member)
    if (analysed / "MOM.res.nc").exists():
        # A free run has no analysis, and that is a complete record of a free
        # run rather than half a record of an experiment.
        records.append((analysed, cycle_time(config, cycle), "ana"))

    # **Every sub-window state, not just the one that becomes a background at an
    # analysis time.** The forecast writes at `forecast.slots` on its way, and
    # until this reduced them the only way to keep them was to keep the restart
    # sets they came in, which is a hundred and fifty megabytes per slot against
    # eight for the record. That is the difference between a truth run costing
    # forty gigabytes and costing two, and it is what lets `cleanup` reap `slot/`
    # on its ordinary schedule instead of the experiment pinning every cycle.
    #
    # The last slot is dropped: the forecast ends on an analysis time, so FMS
    # writes an intermediate restart there *and* the final one, and they are the
    # same state byte for byte. Reducing both would write the same numbers to
    # the same path twice. This is the same rule `stored_leads` applies to the
    # extended forecast, for the same reason.
    for when in slot_times(config, cycle):
        if when == end:
            continue
        records.append((paths.slot_out(cycle, member, when), when, "bkg"))

    for source, valid, kind in records:
        target = paths.product(kind, cycle, member, at=valid)
        try:
            written = post.state_record(
                kind, source, gridspec, target, cycle=cycle, member=member,
                valid=format_instant(valid))
        except post.PostError as error:
            raise TaskError(f"{cycle}.{task}: {error}") from error
        print(f"ackbar: {cycle}.post.state: {kind} {format_instant(valid)} "
              f"{len(written)} field(s), "
              f"{target.stat().st_size / 1e6:.1f} MB -> {target}")


def stored_leads(config):
    """The leads the long forecast keeps a state of its own for.

    Every kept lead except the cycle length, which `bkg/` already holds. Both
    forecasts start from the same set and the model is deterministic, so
    integrating five days does not change the state at twelve hours: that lead
    is the cycling forecast's background, recorded by `post.state` and linked
    here rather than reduced a second time.

    A link rather than a duplicate because it makes the equality *structural*.
    Two independent reductions of what is supposed to be the same state can
    quietly disagree, over rounding or a `FIELDS` change made on one path only,
    and a link cannot.
    """
    step = cycle_length(config)
    return tuple(lead for lead in extended_leads(config) if lead != step)


def linked_lead(config):
    """The lead `bkg/` supplies, or None when the cadences do not produce one."""
    step = cycle_length(config)
    return step if step in extended_leads(config) else None


def _post_fcst(config, paths, cycle, member):
    """Reduce the long forecast's states, and link the one `bkg/` already has."""
    gridspec = Path(config["domain"]["static"]) / soca.GRIDSPEC
    start = cycle_time(config, cycle)
    for lead in stored_leads(config):
        target = paths.fcst_product(cycle, lead, member)
        try:
            written = post.state_record(
                "fcst", paths.fcst_out(cycle, member, lead), gridspec, target,
                cycle=cycle, member=member,
                valid=format_instant(start + lead),
                extra={"initialized": format_instant(start),
                       "lead": format_duration(lead)})
        except post.PostError as error:
            raise TaskError(f"{cycle}.post.fcst: {error}") from error
        print(f"ackbar: {cycle}.post.fcst: {lead_name(lead)} {len(written)} "
              f"field(s), {target.stat().st_size / 1e6:.1f} MB -> {target}")

    lead = linked_lead(config)
    if lead is None:
        return
    link = paths.fcst_product(cycle, lead, member)
    target = paths.product("bkg", cycle + 1, member)
    # Relative, so the experiment survives being moved, copied or rsynced whole,
    # and so the link may be made before `post.state` has written its target:
    # the two hang off different forecasts and race, and a relative link
    # resolves whenever the file lands.
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(os.path.relpath(target, link.parent))
    print(f"ackbar: {cycle}.post.fcst: {lead_name(lead)} -> {link.readlink()} "
          f"(the cycling background, not a second copy of it)")


# --- the tasks that are real from day one ------------------------------------

def _submit_next(config, site, paths, cycle):
    """Cycle n's job that submits cycle n+1. This is what makes cycling work.

    Fail-stop by construction: this job is `afterok` on the forecast, so a
    failed cycle stops the chain rather than producing cycles of garbage off a
    bad background.
    """
    from .graph.build import build_graph
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

    # The ledger is the authority on "was this already submitted", and it is
    # the check that survives a marker file being cleaned up by hand. Asked per
    # task rather than per cycle, because a submitter that dies partway through
    # leaves some of the cycle in the ledger and the rest nowhere at all. Asked
    # of the cycle as a whole, this would report the cycle done and exit 0; the
    # unsubmitted nodes have no job id, so nothing reports them failed, no heal
    # finds them broken, and the experiment wedges in the one state that reads
    # as healthy. `tasks=` is the path `heal` already uses, and it leaves the
    # submission marker alone for exactly this reason.
    graph = build_graph(config)
    done = {r["task"] for r in ledger.read(paths) if r["cycle"] == nxt}
    missing = [n.task for n in graph.cycle_nodes(nxt) if n.task not in done]
    if not missing:
        print(f"ackbar: cycle {nxt} is already in the ledger, not submitting")
        return
    if done:
        print(f"ackbar: cycle {nxt} is partly in the ledger, submitting the "
              f"{len(missing)} task(s) that are not: {', '.join(missing)}")

    records = submit_cycle(config, site, paths, nxt, graph=graph,
                           tasks=missing if done else None)
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

    n-2 and not "however far back the longest window reaches", because it
    cannot reach further. Every state a cycle's analysis reads comes out of one
    forecast, including the sub-window states of a 4D window: that forecast runs
    from the cycle before last and overshoots by half a window, and
    `graph.build._check_window` refuses a window that would need it to start
    earlier still. So the horizon here is a property of the cycle rather than of
    the solver, and stays one setting the moment a window grows.

    All three of `REAPED` go, not just `rst/`. In a DA run the forecast starts
    from `run/<n>/ana/mem###`, which `writeback` fills with a whole restart set
    per member per cycle, so it grows at the same rate as `rst/` and for the same
    reason. `slot/` grows *faster* than either once `forecast.slots` is set: it
    is a full 3D state per slot per member per cycle, so a six slot window is six
    times the per-cycle output of the restarts it sits beside. Reaping one and
    not the others is how an experiment that cycles happily for a week fills the
    disk in the second week.

    They do not go at the same time, though, and the offsets in `REAPED` are why.
    `ana/<n>` is consumed by `forecast(n)` inside its own cycle, so `rst/<n>`
    existing, which is this task's proof, already says nothing can read it again.
    `rst/<n>` and `slot/<n>` are read by cycle *n+1* and need `rst/<n+1>`. So
    `ana` goes one cycle earlier than the other two, which is the earliest this
    task ever sees it.

    Nothing outside `run/<date>/` is ever touched. The compressed states under
    `ana/` and `bkg/`, the departures under `obs_out/`, and the log, sentinel and
    accounting inside the cycle's own run directory are the record of what
    happened, and they are kilobytes where the states are gigabytes.

    Everything at or below the horizon goes, not only the horizon cycle itself.
    A refusal has to be a delay rather than a leak: this task considers one
    cycle, runs once, and nothing revisits it, so indexing a single directory
    means one incomplete cycle strands its predecessor's state for the life of
    the experiment. Sweeping the range instead collects on the next pass whatever
    the last one declined to touch.
    """
    members = member_set(config)
    keep = cycle - 1
    drop = keep - keep_cycles(config)
    if drop < 0:
        return

    stamp = restart_stamp(config)
    proof = [paths.member_out("rst", keep, m) / stamp for m in members]

    # The restart sets prove the *cycling* forecast is past this state, and for
    # a long time that was read as proof that everything was. It is not. The
    # rule is the design's: a cycle's inputs may go once every declared
    # consumer's output exists, and two consumers sit outside the chain that
    # ends at `rst/<keep>`.
    #
    # `hofx(keep)` reads `rst/<drop>` and is a leaf: nothing requires it to
    # finish before the forecast that releases the next cycle, so it and this
    # task are released by the same event and race. Its sentinel is the right
    # artifact rather than an output path, because a cycle whose observers all
    # realize empty legitimately writes no observation file.
    if BY_NAME["hofx"].when(config):
        proof.append(paths.sentinel(keep, "hofx"))

    # `post.state` is deliberately *not* in this proof, and that is a property
    # of what it reads rather than an omission. It reduces its own cycle's
    # output, so by the time this task considers that cycle the reduction ran
    # two cycles ago and cannot still be pending on it. It was a consumer of the
    # dropped set once, and the entry that settled that race also made the last
    # cleanup of a run refuse permanently, because nothing comes after it to try
    # again. See `_post`.

    # A long forecast outlives the cycle it started from by construction, which
    # is the whole reason its cadence is a setting. It starts from the same
    # state as the cycling forecast (`ana/<n>` with an analysis, `rst/<n-1>`
    # without), so the one integrating out of the dropped tree is `drop` in a DA
    # run and `keep` in a free one; wait for both rather than work out which
    # shape this is. A running model has already opened its restarts, but a
    # requeued one rebuilds `INPUT/` from scratch and would find nothing.
    if BY_NAME["forecast.ext"].when(config):
        long_forecast = extended_cycles(config, config["cycle"]["count"])
        for when in (drop, keep):
            if when not in long_forecast:
                continue
            # The long forecast itself, and both things that reduce it. Its
            # trajectory now lives in `run/<n>/fcst/`, which is reaped, and
            # `hofx.ext` and `post.fcst` run *after* it: waiting only on the
            # forecast would delete the states between the forecast finishing
            # and its own consumers reading them. They are leaves, so nothing
            # else in the graph orders this task after them.
            for task in ("forecast.ext", "hofx.ext", "post.fcst"):
                if not BY_NAME[task].when(config):
                    continue
                proof.extend(paths.sentinel(when, task, m)
                             for m in extended_members(config, members))

    absent = [str(p) for p in proof if not p.exists()]
    if absent:
        print(f"ackbar: not cleaning cycle {drop} or earlier, cycle {keep} is "
              f"incomplete ({len(absent)} artifact(s) missing)")
        return

    # `ana` at the horizon is safe under the same proof: it is what that cycle's
    # forecast started from, and its `rst` existing is what says that forecast
    # ran. `slot` is safe under it too, and this is the one that needs saying:
    # those are the sub-window states of the horizon cycle's forecast, read by
    # the analysis one cycle later, and `rst/<keep>` existing means that analysis
    # and the forecast after it are both done with them.
    for target, kind in _reapable(paths, drop, config):
        shutil.rmtree(target)
        print(f"ackbar: removed {target}")

    # Scratch too, and this is the one nothing else was collecting. A task
    # deletes its own scratch on success and *keeps* it on failure, which is
    # right: it is the whole debugging trace. But nothing then removed it, so
    # every failed attempt of a long experiment left a full model run directory
    # behind for the life of the run. By the time a cycle is this far behind the
    # horizon its logs have been kept by `keep_traces` and its trace is no
    # longer what anyone is reading.
    for target in _reapable_scratch(paths, drop):
        shutil.rmtree(target, ignore_errors=True)
        print(f"ackbar: removed {target}")


def keep_cycles(config):
    """How many completed cycles behind the current one keep their state.

    One by default, which is the tightest correct answer: cycle n's forecast
    reads cycle n-1's restarts, so n-2 is the earliest set nothing can need. It
    is a setting because that is also the tightest *possible* answer, and an
    experiment being healed repeatedly, or one whose analysis is being compared
    against a rerun, wants more headroom than the minimum.
    """
    return (config.get("cleanup") or {}).get("keep_cycles", 1)


def pinned(config, paths, cycle):
    """Whether a keep rule holds this cycle's restarts open forever.

    Without one an experiment holds only its last two cycles the moment it
    finishes, so branching a variant off the middle of a fifty cycle run, or
    re-running a segment after a mistake found at cycle forty, means starting
    again from cycle zero. The deletion happens in-cycle and is not reversible,
    so noticing afterwards is too late.

    Stated as a duration rather than as v2's `SAVE_RST_REGEX="^......01.."`,
    which pinned the first of each month by matching the directory name. A regex
    over a date is a rule that changes meaning when the date format does, and it
    cannot express "every three days" at all. A duration divides: cycle *n* is
    pinned when its analysis time is an exact multiple of `keep_every` after
    `cycle.start`.
    """
    every = (config.get("cleanup") or {}).get("keep_every")
    if not every:
        return False
    if cycle <= 0:
        # The materialized initial condition, and the branch point every other
        # one is measured from. An experiment that kept a pin every week and
        # dropped the state cycle 1 started from could be re-run from anywhere
        # except its own beginning.
        return True
    offset = (cycle - 1) * paths.length
    return not offset.total_seconds() % parse_duration(every).total_seconds()


def _reapable(paths, drop, config):
    """Every state directory at or below the horizon, oldest first.

    Sweeps `run/` rather than indexing the horizon cycle, so that a cycle the
    last pass declined to touch is collected by the next one.

    Each kind has its own horizon, `drop` plus its offset in `REAPED`, because
    each is released by a different event. `rst` and `slot` are read by the cycle
    after them and go at `drop`; `ana` is read only by its own cycle's forecast
    and goes one cycle later, which is as soon as this task can see it at all.

    The keep rule pins `rst` alone. A pinned cycle exists so an experiment can be
    branched or rerun from it, and that needs the restart set the next forecast
    would start from; `ana` and `slot` at the same date carry nothing the pinned
    `rst` and the compressed products do not already hold, and they are the two
    that grow fastest.
    """
    root = paths.sub("run")
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        number = cycle_of(paths, entry.name)
        if number is None:
            continue
        held = pinned(config, paths, number)
        for kind, offset in REAPED.items():
            if number > drop + offset:
                continue
            if held and kind == "rst":
                continue
            target = entry / kind
            if target.is_dir():
                yield target, kind


def _reapable_scratch(paths, drop):
    """Scratch directories of cycles at or below the horizon.

    Never pinned. A keep rule is about branch points, and a branch starts from a
    restart set; the working directory a task was killed in is not something an
    experiment is ever resumed from.
    """
    root = paths.scratch_dir
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        number = cycle_of(paths, entry.name)
        if entry.is_dir() and number is not None and number <= drop:
            yield entry


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
