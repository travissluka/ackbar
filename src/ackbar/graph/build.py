"""Turning a merged configuration into a task graph.

Deterministic and side-effect free, which is a constraint and not a
description: `heal` regenerates a subgraph long after the original submission
and has to get the same answer, so nothing here may consult the clock, the
filesystem, or the scheduler.
"""

from ..config.jobtime import cycle_length, cycle_time
from ..duration import parse_duration
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
