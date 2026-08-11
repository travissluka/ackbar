"""Detect what failed and resubmit the affected subgraph.

Five steps, and the third is the one people forget:

1. identify the failed job
2. compute the transitive closure of its dependents from the regenerated graph
3. **`scancel` every id in the closure that is still queued**
4. regenerate and resubmit with fresh, state-aware edges
5. record the new ids and attempt numbers in the ledger

Step 3 is mandatory rather than tidy. Because unsatisfiable dependents pend
rather than cancel, those jobs are still holding live job ids and claimed
working directories, and because requeue poisons dependencies permanently, a
successful replacement upstream does not release them. Skipping it gives two
jobs per task, one of which will never run.

Whole nodes are resubmitted, not the failed members alone. Resubmitting
`--array=3` of a twenty member forecast would work and would be a smaller job,
and it is not worth it: every member that already succeeded skips on its
sentinel in about a second, and keeping the index sets identical across a heal
means an `aftercorr` edge can never be rebuilt between arrays that disagree.
That is the failure mode Slurm reports as a job which pends forever rather than
as an error, so it is the one to design against.
"""

from . import slurm, state
from .graph.build import build_graph
from .submit import SubmitError, submit_cycle


class HealError(Exception):
    pass


def plan(config, paths, graph=None, statuses=None):
    """What a heal would do. Returns (broken, closure, cancel).

    Pure with respect to the scheduler apart from the state it reads, so
    `--dry-run` and the real thing compute the same answer from the same code.
    """
    graph = graph or build_graph(config)
    statuses = statuses if statuses is not None else state.collect(paths, graph)

    order = {node_id: i for i, node_id in enumerate(graph.order())}
    broken = sorted(
        (node_id for node_id, status in statuses.items() if status.broken),
        key=order.get,
    )
    if not broken:
        return [], [], []

    # Only what has actually been submitted. A failure in cycle n strands its
    # `submit` task, so cycle n+1 does not exist yet: those nodes have no job
    # ids, no edges to rebuild and nothing to cancel, and the healed `submit`
    # will submit them in the ordinary way once the cycle completes. Including
    # them would make the heal try to submit a cycle whose own roots have never
    # been submitted, which is exactly the error the submitter refuses on.
    closure = _closure(graph, broken)
    ordered = sorted(
        (node_id for node_id in closure
         if statuses.get(node_id) and statuses[node_id].job_id),
        key=order.get,
    )

    # Only what is still holding a job id. A node that already reached a
    # terminal state has nothing to cancel, and cancelling a COMPLETED job is
    # not an error but does make the log lie about what happened.
    cancel = []
    for node_id in ordered:
        status = statuses.get(node_id)
        if status and status.job_id and _still_queued(status):
            cancel.append(status.job_id)
    return broken, ordered, sorted(set(cancel))


def _still_queued(status):
    return any(s in (state.PENDING, state.BLOCKED, state.RUNNING,
                     state.STRANDED)
               for s in status.elements.values())


def _closure(graph, roots):
    """The failed nodes and everything downstream of them.

    One failed forecast invalidates every cycle in flight, because
    `forecast(n) -> da(n+1)`. That is inherent to cycling and it is why the
    blast radius is worth printing before anything is cancelled.
    """
    seen = set(roots)
    frontier = list(roots)
    while frontier:
        node_id = frontier.pop()
        for edge in graph.children(node_id):
            if edge.child not in seen:
                seen.add(edge.child)
                frontier.append(edge.child)
    return seen


def heal(config, site, paths, *, graph=None, dry_run=False):
    """Run the five steps. Returns (broken, closure, cancelled, records)."""
    graph = graph or build_graph(config)
    broken, closure, cancel = plan(config, paths, graph)
    if not broken:
        return [], [], [], []

    if dry_run:
        return broken, closure, cancel, []

    # Step 3, before anything is resubmitted. Doing it afterwards would leave a
    # window in which two jobs claim the same working directory.
    slurm.scancel(cancel)

    # Step 4, cycle by cycle in order, so that a healed node in cycle n has a
    # fresh job id in the ledger before cycle n+1 tries to depend on it.
    records = []
    by_cycle = {}
    for node_id in closure:
        cycle, _, task = node_id.partition(".")
        by_cycle.setdefault(int(cycle), []).append(task)

    for cycle in sorted(by_cycle):
        try:
            records.extend(submit_cycle(
                config, site, paths, cycle, graph=graph,
                tasks=by_cycle[cycle],
            ))
        except SubmitError as error:
            raise HealError(
                f"cycle {cycle} could not be resubmitted: {error}"
            ) from None
    return broken, closure, cancel, records


def describe(statuses, broken):
    """One line per broken node, saying what is actually wrong with it."""
    lines = []
    for node_id in broken:
        status = statuses[node_id]
        members = status.broken
        detail = ", ".join(
            f"{'scalar' if m is None else f'mem{m:03d}'}"
            f" {status.elements[m]}"
            f"{' (' + status.reasons[m] + ')' if status.reasons.get(m) else ''}"
            for m in members[:6]
        )
        if len(members) > 6:
            detail += f", and {len(members) - 6} more"
        lines.append(f"  {node_id} (job {status.job_id}, attempt "
                     f"{status.attempt}): {detail}")
    return lines


def unhealable(statuses, broken):
    """Nodes whose failure a resubmission cannot plausibly fix.

    Nothing is refused on this basis. It is printed, because a genuine nonzero
    exit resubmitted unchanged will fail again in the same way, and the useful
    move then is to fix the cause rather than to heal twice.
    """
    out = []
    for node_id in broken:
        status = statuses[node_id]
        reasons = {status.reasons.get(m) for m in status.broken}
        if reasons & {"FAILED", "OUT_OF_MEMORY", "TIMEOUT"}:
            out.append((node_id, sorted(r for r in reasons if r)))
    return out


__all__ = ["HealError", "describe", "heal", "plan", "unhealable"]
