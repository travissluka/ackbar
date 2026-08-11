"""Submitting one cycle.

This is a real program rather than an `sbatch` wrapper, and the reason is a
single Slurm behaviour: **`afterok` edges expire.** A dependency can only be
created while the target job is active or within `MinJobAge` after it ends, and
that is minutes at most. So every cross-cycle edge has to be built against the
upstream job's *current* state:

- still in the queue: keep the edge
- already `COMPLETED`: drop the edge, because the artifact is on disk and the
  edge is now redundant rather than wrong
- failed, or absent and not completed: refuse to submit and exit nonzero

A blind `--dependency=afterok:<remembered id>` gets the first case right, the
second rejected by Slurm, and the third catastrophically wrong.
"""

from . import emit, ledger, slurm
from .graph.build import build_graph


class SubmitError(Exception):
    pass


def submit_cycle(config, site, paths, cycle, *, graph=None, tasks=None,
                 dry_run=False):
    """Submit every node of one cycle. Returns the records written.

    `tasks` restricts submission to a subgraph, which is what `heal` needs and
    what makes this function the single implementation of both. Order is the
    graph's own topological order, so a node's intra-cycle parents always have
    job ids by the time it is submitted.
    """
    graph = graph or build_graph(config)
    if paths.halt_flag.exists():
        raise SubmitError(
            f"{paths.halt_flag} exists, so nothing was submitted. "
            f"`ackbar resume` clears it."
        )

    nodes = [n for n in graph.cycle_nodes(cycle)]
    if tasks is not None:
        nodes = [n for n in nodes if n.task in set(tasks)]
    if not nodes:
        raise SubmitError(f"cycle {cycle} has no tasks to submit")

    order = {node_id: i for i, node_id in enumerate(graph.order())}
    nodes.sort(key=lambda n: order[n.id])

    if not dry_run:
        _claim(paths, cycle, whole=tasks is None)

    live = _upstream_state(paths, graph, nodes, {n.id for n in nodes})
    submitted = {}
    records = []
    for node in nodes:
        dependency = _dependency(graph, node, submitted, live, paths)
        script = paths.job_script(node.cycle, node.task)
        if not script.exists():
            raise SubmitError(
                f"{script} does not exist; the experiment was created before "
                f"this task existed, so re-run `ackbar create`"
            )
        if dry_run:
            records.append({"cycle": node.cycle, "task": node.task,
                            "dependency": dependency,
                            "array": slurm.array_spec(node.members)})
            submitted[node.id] = 0
            continue

        # Slurm opens `--output` before the job script runs and will not create
        # the directory, so this is the last moment it can be made. Here rather
        # than in `emit` so that a cycle's run directory appears when the cycle
        # is submitted rather than when the experiment is created.
        paths.job_log(node.cycle, node.task, node.is_array).parent.mkdir(
            parents=True, exist_ok=True
        )
        job_id = slurm.sbatch(
            script,
            job_name=emit.job_name(paths.experiment, node.cycle, node.task),
            comment=emit.comment(paths.experiment, node.cycle, node.task),
            dependency=dependency,
            array=slurm.array_spec(node.members),
            partition=site.get("partition"),
            account=site.get("account"),
            qos=site.get("qos"),
        )
        submitted[node.id] = job_id
        records.append(ledger.append(
            paths,
            cycle=node.cycle,
            task=node.task,
            members=node.members,
            attempt=ledger.attempts(paths, node.cycle, node.task) + 1,
            job_id=job_id,
            dependency=dependency,
        ))
    return records


def _claim(paths, cycle, whole):
    """Take the per-cycle submission marker, `O_EXCL`.

    Only for a whole-cycle submission. A heal resubmits a subgraph of a cycle
    that is already marked, and refusing that would make the marker prevent the
    one operation it exists to make safe.
    """
    if not whole:
        return
    marker = paths.submit_marker(cycle)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(marker, "x") as handle:
            handle.write("")
    except FileExistsError:
        raise SubmitError(
            f"cycle {cycle} was already submitted ({marker} exists). A requeued "
            f"submitter reaching here twice is exactly what this prevents; if "
            f"the submission genuinely did not happen, remove the marker."
        ) from None


def _upstream_state(paths, graph, nodes, submitting):
    """Current state of every parent that this call is not itself submitting.

    Keyed on the submission set rather than on the cycle number, because a heal
    resubmits a subgraph *within* a cycle: its parents are in the same cycle,
    already have job ids, and have to be state-checked exactly like a
    cross-cycle parent. Reading "earlier cycle" here instead is the bug that
    makes healing work only for a whole cycle at a time.
    """
    ids = {}
    for node in nodes:
        for edge in graph.parents(node.id):
            if edge.parent in submitting:
                continue
            parent = graph.get(*_split(edge.parent))
            if parent is None:
                continue
            job_id = ledger.job_id(paths, parent.cycle, parent.task)
            if job_id is not None:
                ids[edge.parent] = job_id
    if not ids:
        return {}
    states = slurm.state_of(sorted(set(ids.values())))
    return {node_id: (job_id, states[job_id]) for node_id, job_id in ids.items()}


def _dependency(graph, node, submitted, live, paths):
    """The `--dependency` string for one node, or None."""
    terms = []
    for edge in sorted(graph.parents(node.id), key=lambda e: (e.kind, e.parent)):
        if edge.parent in submitted:
            terms.append((edge.kind, submitted[edge.parent]))
            continue

        known = live.get(edge.parent)
        if known is None:
            raise SubmitError(
                f"{node.id} depends on {edge.parent}, which has never been "
                f"submitted. Submit the earlier cycle first, or use "
                f"`ackbar heal`."
            )
        job_id, state = known
        if state == "active":
            terms.append((edge.kind, job_id))
        elif state == "completed":
            # Redundant, not missing: the artifact is on disk and Slurm would
            # reject an edge to a job it has already forgotten.
            continue
        else:
            raise SubmitError(
                f"{node.id} depends on {edge.parent} (job {job_id}), which is "
                f"{state}. Refusing to submit a cycle that would read a file "
                f"nothing produced."
            )
    if not terms:
        return None
    return ",".join(f"{kind}:{job_id}" for kind, job_id in terms)


def _split(node_id):
    cycle, _, task = node_id.partition(".")
    return int(cycle), task
