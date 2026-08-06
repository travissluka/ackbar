"""Turning a merged configuration into a task graph.

Deterministic and side-effect free, which is a constraint and not a
description: `heal` regenerates a subgraph long after the original submission
and has to get the same answer, so nothing here may consult the clock, the
filesystem, or the scheduler.
"""

from ..config.jobtime import (FOUR_D, cycle_length, cycle_time,
                              forecast_overshoot, slot_length, window_length,
                              window_type)
from ..duration import format_duration, parse_duration
from .model import AFTERCORR, Graph, GraphError, Node
from .tasks import (
    CROSS_CYCLE,
    CROSS_CYCLE_NO_ANALYSIS,
    EDGES,
    ROOTS,
    SUPPRESSED_BY,
    TASKS,
    ensemble_covariance,
    ensemble_source,
)

#: Ways of maintaining an ensemble across cycles that ACKBAR can actually run.
#: `letkf` puts an ensemble filter in the cycle beside the deterministic
#: analysis; `none` lets the members run free and only pulls them back onto that
#: analysis, which is a cheaper and genuinely different experiment rather than a
#: degraded version of the first.
#:
#: `eda` and the rest are in the schema because they are the vocabulary, and
#: refused here because a covariance drawn from an ensemble nothing maintains is
#: not an under-specified experiment, it is one whose spread decays to nothing
#: over a few cycles with no error to say so.
ENSEMBLE_SOURCES = ("letkf", "none")


def member_set(config):
    """The canonical member index set.

    Pinned in one place and asserted everywhere, because `aftercorr` behaviour
    on mismatched index ranges is undocumented and a mismatch produces an
    invisible forever-pending job rather than an error.

    Every experiment has one, even a free run, whose single member is the
    control. That is what keeps `mem000` from being a special case: there is no
    ctrl-versus-ens split in the paths, the arrays, or the graph.
    """
    ensemble = config.get("ensemble")
    if not ensemble:
        return (0,)
    size = ensemble["size"]
    if ensemble.get("control", True):
        return tuple(range(0, size + 1))
    return tuple(range(1, size + 1))


def extended_members(config, canonical):
    """Which members get a long forecast. Defaults to the control alone."""
    declared = config.get("forecast", {}).get("extended", {}).get("members")
    if declared is None:
        return (canonical[0],)
    unknown = sorted(set(declared) - set(canonical))
    if unknown:
        raise GraphError(
            f"forecast.extended.members names members that do not exist: "
            f"{unknown}; the ensemble is {canonical[0]} to {canonical[-1]}"
        )
    return tuple(sorted(set(declared)))


def extended_cycles(config, count):
    """The cycles a long forecast runs off.

    Cadence is a setting rather than the cycle period, because a 7 day forecast
    off every 24 hour cycle is seven times the model cost of the cycling itself.
    """
    extended = config.get("forecast", {}).get("extended")
    if not extended:
        return ()
    every = parse_duration(extended["every"]).total_seconds()
    if every <= 0:
        raise GraphError("forecast.extended.every must be a positive duration")
    step = cycle_length(config).total_seconds()
    if every % step:
        raise GraphError(
            f"forecast.extended.every ({extended['every']}) is not a whole "
            f"number of cycles ({config['cycle']['length']}), so the cadence "
            f"would drift"
        )
    stride = int(every // step)
    return tuple(range(1, count + 1, stride))


def build_graph(config):
    """The whole experiment: every cycle, every task, every edge."""
    _check_ensemble_source(config)
    _check_window(config)
    count = config["cycle"]["count"]
    canonical = member_set(config)
    graph = Graph(
        experiment=config["experiment"]["name"],
        throttle=config["cycle"].get("throttle", 1),
    )

    enabled = [task for task in TASKS if task.when(config)]
    long_forecast = set(extended_cycles(config, count))
    long_members = extended_members(config, canonical) if long_forecast else ()
    resources = config.get("domain", {}).get("resources", {}) or {}

    for cycle in range(1, count + 1):
        for task in enabled:
            if not _runs_in_cycle(task.name, cycle, count, long_forecast):
                continue
            if task.name == "forecast.ext":
                members = long_members
            elif task.member_level:
                members = canonical
            else:
                members = ()
            graph.add(Node(
                cycle=cycle,
                task=task.name,
                members=members,
                exe=task.exe(config),
                resources=resources.get(task.name, resources.get("default", {})),
            ))

        for parent, child, kind in EDGES:
            suppressor = SUPPRESSED_BY.get((parent, child))
            if suppressor and graph.has(cycle, suppressor):
                continue
            graph.link(
                f"{cycle}.{parent}",
                f"{cycle}.{child}",
                _kind(graph, cycle, parent, child, kind),
            )

    _cross_cycle(graph, count)
    _check_roots(graph)
    graph.order()  # fail here rather than leaving Slurm to not notice
    return graph


def _check_ensemble_source(config):
    """A covariance drawn from an ensemble needs that ensemble maintained.

    Here rather than in the schema because it is a relation between two
    subtrees, `solver.covariance` and `ensemble.source`, and a schema that
    expressed it would be read by nobody. It is checked at graph build time
    because that is what every command does first.
    """
    if not ensemble_covariance(config):
        return
    source = ensemble_source(config)
    if source not in ENSEMBLE_SOURCES:
        raise GraphError(
            f"solver.covariance is "
            f"{config['solver'].get('covariance')!r}, so the analysis draws its "
            f"background error from the ensemble, and ensemble.source is "
            f"{source!r}, which nothing in this cycle implements. Use one of "
            f"{', '.join(ENSEMBLE_SOURCES)}: an ensemble that nothing updates "
            f"loses its spread over a few cycles and reports no error while it "
            f"does."
        )
    if not config["ensemble"].get("control", True):
        raise GraphError(
            "solver.covariance draws its background error from the ensemble, "
            "and ensemble.control is false. The centre every member is "
            "recentred onto is the deterministic analysis, which is the "
            "control's; without one there is nothing for the recentring to be "
            "about and the first ensemble member would be treated as the "
            "answer."
        )
    if source != "letkf":
        return
    # `da.ens` is an ensemble filter, so it needs what any ensemble filter
    # needs. The schema asks for these when the *solver* is an LETKF and cannot
    # ask for them here, where what calls for one is a value in another subtree.
    missing = [key for key in ("local ensemble DA", "ensemble distribution")
               if not config["solver"].get(key)]
    if missing:
        raise GraphError(
            f"ensemble.source is 'letkf', so this cycle runs an ensemble "
            f"filter beside the variational analysis, and solver states no "
            f"{' and no '.join(missing)}. Inherit a layer that configures one."
        )


def _check_window(config):
    """What the window, the cycle and the sub-window cadence imply about each other.

    Checked here, with the rest of what one subtree implies about another,
    rather than in the schema, which can say `forecast.slots` is a duration and
    not that it is *this* experiment's duration.

    Three relations, and none of them is a rounding question. FMS writes a state
    each time its clock *reaches* the next interval, counting from the start of
    the run, so any time the workflow names that is not a whole number of
    cadences in is a file the model never wrote. Two of the three are the same
    invariant soca-science checked, `(DA_TIMESLOTS-1)*DA_SUBWINDOW_LEN ==
    DA_WINDOW_LEN` in its `cycle.sh`; the third is ACKBAR's, and it exists
    because the forecast here is one run rather than a chain of them, so the
    state the next cycle resumes from is an interval of this one.

    A window that is not shorter than two cycles is refused outright: its own
    start would fall at or before the forecast's, and nothing is written at hour
    zero. That is also what keeps every state one cycle's analysis reads inside
    one forecast, which is what lets `cleanup` reap on the cycle before last.
    """
    step = slot_length(config)
    window = window_length(config)
    cycle = cycle_length(config)
    overshoot = forecast_overshoot(config)

    if window.total_seconds() <= 0:
        raise GraphError("solver.window.length must be a positive duration")
    if window_type(config) in FOUR_D and step is None:
        raise GraphError(
            f"solver.window.type is {window_type(config)!r}, which compares each "
            f"observation against the state nearest its own time, and "
            f"forecast.slots is not set, so the forecast writes no states for it "
            f"to read. Set a sub-window cadence that divides the window "
            f"({_spell(window)})."
        )
    # The run length is constrained by the model's clock whether or not any
    # states are asked of it, so this half is checked before the cadence
    # relations, which only exist once there are slots.
    _check_coupling(config, "the forecast", cycle + overshoot)

    if step is None:
        return
    if step.total_seconds() <= 0:
        raise GraphError("forecast.slots must be a positive duration")

    # Persistence hands one restart set forward and writes nothing in between,
    # so it has nowhere to put a sub-window state. Refused rather than ignored:
    # the forecast would exit 0 having written none of the states it declared,
    # and the skip rule needs the sentinel *and* the outputs, so the task would
    # be rerun forever instead of failing. That is the one configuration
    # `model: persistence` exists to make cheap.
    if (config.get("model") or {}).get("name") == "persistence":
        raise GraphError(
            f"forecast.slots ({config['forecast']['slots']}) asks for a state "
            f"partway through a forecast, and model.persistence does not "
            f"integrate: it hands one restart set forward unchanged. Bring the "
            f"sub-window states up on a real model, or drop forecast.slots and "
            f"the four-dimensional window with it."
        )

    # The window has to fit inside the forecast that covers it, and a window
    # whose analysis reads a state *at* its own start needs it to fit with a
    # cadence to spare: nothing is written at hour zero, so the state there is
    # the set the forecast was handed rather than one it produced. A free
    # running forecast reads nothing and is allowed the flush fit, which is what
    # `W == C` with no overshoot is.
    lead = cycle + overshoot - window
    if lead.total_seconds() < 0 or (overshoot and not lead.total_seconds()):
        raise GraphError(
            f"the window ({_spell(window)}) begins at or before the start of the "
            f"forecast that has to cover it, which runs {_spell(cycle)} plus "
            f"{_spell(overshoot)} of overshoot. The state at hour zero is the "
            f"set that forecast was handed, not one it wrote. Shorten "
            f"solver.window.length or lengthen the cycle."
        )

    for name, quantity, why in (
        ("the window", window,
         "so the sub-window states would not tile it and the analysis would "
         "read a trajectory that stops before its own last observation"),
        ("the cycle length", cycle,
         "so the forecast writes nothing at the time the next cycle starts "
         "from"),
        ("the forecast's lead-in to the window", lead,
         "so the window would begin between two of the forecast's writes"),
    ):
        if quantity.total_seconds() % step.total_seconds():
            raise GraphError(
                f"forecast.slots ({config['forecast']['slots']}) does not divide "
                f"{name} ({_spell(quantity)}), {why}"
            )

    # Last, because a cadence that fails one of the relations above fails it for
    # a reason the experiment's author can act on, and this one would otherwise
    # report a coupling step they never chose.
    _check_coupling(config, "forecast.slots", step)


def _check_coupling(config, name, quantity):
    """What the model's own clock will accept, asked before the job is written.

    `coupler_main` refuses outright a run length that is not a whole number of
    coupled steps, so a window whose half reaches past one lands as a FATAL at
    job start, on every cycle. It is a quieter failure for the states:
    intervals are tested at the bottom of the coupled loop and stamped with the
    step time actually reached, so a cadence finer than the coupling writes on
    the coupling step instead and the times asked for never exist at all. That
    one is caught eventually, by `_claim_slot`, after the whole run has been
    paid for, and blamed on `restart_interval`.

    Only for a model that declares a coupling step, which leaves the stub and
    persistence layers alone.
    """
    seconds = (config.get("model") or {}).get("coupling_seconds")
    if not seconds or quantity is None:
        return
    if quantity.total_seconds() % seconds:
        raise GraphError(
            f"model.coupling_seconds ({seconds}) does not divide {name} "
            f"({_spell(quantity)}). `coupler_main` runs a whole number of "
            f"coupled steps and nothing else, so this is a FATAL at job start "
            f"on every cycle rather than a slow drift."
        )


def _spell(delta):
    """A duration as the configuration would have written it."""
    return format_duration(delta)


def _runs_in_cycle(name, cycle, count, long_forecast):
    if name == "submit":
        # The last cycle has nothing to submit. Without this the experiment
        # never ends, which is how v2's resubmission loop behaved.
        return cycle < count
    if name == "cleanup":
        # Nothing to clean before the first cycle has produced anything.
        return cycle > 1
    if name == "forecast.ext":
        return cycle in long_forecast
    return True


def _kind(graph, cycle, parent, child, kind):
    """Upgrade an array-to-array edge to the elementwise form.

    Only `afterok` upgrades. `aftercorr` requires the corresponding task to
    have *succeeded*, so an `afterany` leaf that upgraded would stop tolerating
    the member failure it exists to report.
    """
    if kind != "afterok":
        return kind
    up, down = graph.get(cycle, parent), graph.get(cycle, child)
    if up and down and up.members and up.members == down.members:
        return AFTERCORR
    return kind


def _cross_cycle(graph, count):
    """The only genuine cross-cycle edges: this cycle's forecast feeds the next.

    Everything else is intra-cycle or a leaf with no successor, which is what
    lets post-processing, long forecasts and verification overlap the following
    cycle instead of holding it up.
    """
    for cycle in range(1, count):
        nxt = cycle + 1
        consumers = list(CROSS_CYCLE)
        if not graph.has(nxt, "da"):
            # No analysis, so the restart handoff is forecast to forecast.
            consumers += list(CROSS_CYCLE_NO_ANALYSIS)
        for task in consumers:
            graph.link(
                f"{cycle}.forecast",
                f"{nxt}.{task}",
                _cross_kind(graph, cycle, nxt, task),
            )


def _cross_kind(graph, cycle, nxt, task):
    up, down = graph.get(cycle, "forecast"), graph.get(nxt, task)
    if up and down and up.members and up.members == down.members:
        return AFTERCORR
    return "afterok"


def _check_roots(graph):
    """Every node either has a parent or is a declared root.

    A node with no parent starts as soon as its cycle is submitted. That is
    right for the tasks in ROOTS and for the first cycle, whose inputs are all
    offline, and a bug anywhere else, where it means an edge the table forgot
    and a job that runs before its input exists.
    """
    first = min(graph.cycles) if graph.cycles else None
    stray = []
    for node in graph.nodes:
        if graph.parents(node.id):
            continue
        if node.task in ROOTS or node.cycle == first:
            continue
        stray.append(node.id)
    if stray:
        raise GraphError(
            f"these tasks have no dependency and would be submitted "
            f"immediately: {', '.join(sorted(stray))}"
        )


def job_time_context(config, graph):
    """Every (cycle, member) pair the experiment will ever render config for.

    Distinct pairs rather than distinct nodes: the job-time symbols depend on
    the cycle and the member and on nothing else, so rendering per node would
    repeat identical work once per task.
    """
    pairs = set()
    for node in graph.nodes:
        for member in (node.members or (member_set(config)[0],)):
            pairs.add((node.cycle, member))
    return sorted(pairs)


__all__ = [
    "build_graph",
    "cycle_time",
    "extended_cycles",
    "extended_members",
    "job_time_context",
    "member_set",
]
