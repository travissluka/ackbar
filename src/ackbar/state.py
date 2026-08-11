"""What is the experiment actually doing.

Three sources, and all three are needed. The **ledger** knows identity: which
job id was cycle 7's writeback, which no scheduler query can answer. **`sacct`**
knows outcome, and is the only thing that still knows anything after a job
leaves the queue. **`squeue`** knows *reasons*, which exist only while a job is
queued and are gone forever afterward, and which are the only place
`DependencyNeverSatisfied` is ever visible.

A fourth source is consulted last: the **sentinel on disk**. `sacct` rows purge
on a site retention that can be weeks, and an experiment has to stay answerable
after that. A task's sentinel is written last and only on success, so its
presence is a stronger statement than an accounting record anyway.

Nothing here is stored. This is a read-only view computed on demand, because a
cached one would be a second state database and would be wrong the moment a job
ended.
"""

from dataclasses import dataclass, field

from . import ledger, slurm

#: What a single job element can be, worst last. Ordering matters: a node's
#: summary is the worst state among its elements, because "mostly fine" is not a
#: thing anybody can act on.
COMPLETE = "complete"
RUNNING = "running"
#: Eligible and waiting for the scheduler: Slurm could start it and has not.
PENDING = "pending"
#: Waiting for a dependency, which is the graph holding it rather than Slurm.
#:
#: Split out of PENDING because the two have opposite meanings for anyone
#: watching. Most of an experiment sits blocked at any moment, which is the
#: workflow working as designed and is not news. A job that is *queued* has had
#: its dependencies satisfied and is waiting on the box, so a growing queued
#: count is the one that says the machine is the bottleneck, and a queued count
#: that never falls is the one that says something is wrong with the partition.
#: Reported as one number, neither question can be asked.
BLOCKED = "blocked"
UNSUBMITTED = "unsubmitted"
STRANDED = "stranded"
FAILED = "failed"

#: Worst last. BLOCKED sits below PENDING so that a partly-released array
#: summarizes as queued: "some of this is eligible" is the more informative half.
SEVERITY = (COMPLETE, UNSUBMITTED, BLOCKED, PENDING, RUNNING, STRANDED, FAILED)

#: Single characters for the status grid. A grid of words does not fit on a
#: terminal at fifty cycles. `+` is eligible and `-` is held back, which is the
#: distinction BLOCKED exists to draw.
GLYPH = {
    COMPLETE: ".",
    RUNNING: ">",
    PENDING: "+",
    BLOCKED: "-",
    UNSUBMITTED: " ",
    STRANDED: "?",
    FAILED: "X",
}

#: Queue reasons that mean a dependency, not the scheduler. Matched by prefix
#: because Slurm spells the unsatisfiable case as `DependencyNeverSatisfied`,
#: which is recognized before this and is terminal rather than blocked.
BLOCKED_REASON = "Dependency"

#: States that mean the job is done for and will not become anything else.
TERMINAL = (COMPLETE, FAILED, STRANDED)


@dataclass
class NodeStatus:
    cycle: int
    task: str
    members: tuple = ()
    job_id: int = None
    attempt: int = 0
    #: member index (or None for a scalar job) -> one of the states above
    elements: dict = field(default_factory=dict)
    #: member index -> the queue reason, while there is one
    reasons: dict = field(default_factory=dict)

    @property
    def id(self):
        return f"{self.cycle}.{self.task}"

    @property
    def summary(self):
        if not self.elements:
            return UNSUBMITTED
        return max(self.elements.values(), key=SEVERITY.index)

    @property
    def broken(self):
        """Members that need a heal. Stranded counts: it never runs otherwise."""
        return tuple(sorted(
            (m for m, s in self.elements.items() if s in (FAILED, STRANDED)),
            key=lambda m: (m is None, m),
        ))

    def counts(self):
        out = {}
        for state in self.elements.values():
            out[state] = out.get(state, 0) + 1
        return out


@dataclass
class Snapshot:
    """Everything the scheduler had to say, at one instant.

    Split out of `collect` so that one caller watching several experiments can
    ask Slurm once instead of once per experiment. Three `squeue`/`sacct`
    invocations per experiment per refresh is what makes a live view expensive,
    and the cost is entirely in the number of invocations rather than in the
    number of job ids each one carries.

    Held for the length of one refresh and thrown away. It is not a cache: a
    Snapshot that outlived the tick that made it would be the second state
    database this module's docstring exists to refuse.
    """

    #: (base job id, member) -> (state, reason), from `squeue`
    live: dict = field(default_factory=dict)
    #: base job id -> accounting record, from `sacct`
    done: dict = field(default_factory=dict)
    #: (base job id, member) -> {"state": ...}, from `sacct`
    done_elements: dict = field(default_factory=dict)


def snapshot(job_ids):
    """Ask the scheduler about *job_ids*. Raises on an outage, as ever."""
    job_ids = list(job_ids)
    return Snapshot(
        live=_queue_elements(job_ids),
        done=slurm.accounting(job_ids) if job_ids else {},
        done_elements=_accounting_elements(job_ids),
    )


def collect(paths, graph, snap=None):
    """A NodeStatus for every node in the graph, keyed by node id.

    *snap* is an optional pre-fetched `Snapshot`. It has to cover at least this
    experiment's job ids; covering more is harmless, because every lookup in it
    is by job id. Passing none queries the scheduler for exactly this
    experiment, which is what every command line caller wants.
    """
    records = ledger.latest(paths)
    attempts = _attempt_counts(paths)

    if snap is None:
        snap = snapshot([r["job_id"] for r in records.values()])
    live, done, done_elements = snap.live, snap.done, snap.done_elements

    out = {}
    for node in graph.nodes:
        record = records.get((node.cycle, node.task))
        status = NodeStatus(
            cycle=node.cycle,
            task=node.task,
            members=node.members,
            job_id=record["job_id"] if record else None,
            attempt=attempts.get((node.cycle, node.task), 0),
        )
        if record:
            for member in (node.members or (None,)):
                state, reason = _element_state(
                    paths, node, member, record["job_id"], live, done,
                    done_elements,
                )
                status.elements[member] = state
                if reason:
                    status.reasons[member] = reason
        out[node.id] = status
    return out


def _element_state(paths, node, member, job_id, live, done, done_elements):
    key = (job_id, member)

    row = live.get(key) or live.get((job_id, None))
    if row:
        state, reason = row
        if reason == slurm.NEVER_SATISFIED:
            return STRANDED, reason
        if state == "PENDING":
            if reason.startswith(BLOCKED_REASON):
                return BLOCKED, reason
            return PENDING, reason
        return RUNNING, reason

    record = done_elements.get(key) or done.get(job_id)
    if record:
        state = record["state"]
        if state in slurm.SUCCESS:
            return COMPLETE, ""
        if state in slurm.ACTIVE:
            return RUNNING, ""
        return FAILED, state

    # Purged, or never seen. The sentinel outlives both and is written last, so
    # if it is there the task finished no matter what accounting remembers.
    if paths.sentinel(node.cycle, node.task, member).exists():
        return COMPLETE, ""

    # Submitted, absent from the queue, absent from accounting, and it left no
    # sentinel. This used to answer PENDING, which is the one thing it cannot
    # be: a job that is waiting to run is in `squeue` by definition, and this
    # job was submitted, because it has a ledger record with an id. So the
    # answer meant "still queued" about something that had already stopped, and
    # PENDING is not in TERMINAL and not in `broken`, so `status` showed a run
    # that would never finish and `heal` found nothing to fix.
    #
    # FAILED is what it is: not a claim about how it ended, a claim that it did,
    # without leaving the artifact that proves it worked. That puts it in
    # `broken`, which is right, because the only way forward is to run it again.
    #
    # Safe only because `slurm.accounting` now raises when it cannot answer
    # rather than returning nothing. Under the old behaviour a slurmdbd blip
    # emptied `done` and this would have called every job in the experiment
    # failed at once.
    return FAILED, "no queue row, no accounting row, and no sentinel"


def _queue_elements(job_ids):
    """{(base id, member): (state, reason)} from squeue, per array element.

    `slurm.queue` collapses elements onto the base id, which is the right answer
    for "may I still depend on this" and the wrong one here: a heal has to know
    *which* members are stranded.
    """
    if not job_ids:
        return {}
    result = slurm.run(
        ["squeue", "-h", "-o", "%i|%T|%r", "-j",
         ",".join(str(i) for i in job_ids)],
        check=False,
    )
    out = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        raw, state, reason = parts
        base, _, element = raw.partition("_")
        if not base.isdigit():
            continue
        if element.startswith("["):
            # The un-started remainder of an array, "123_[5-20]". Expand it, so
            # that members nobody has looked at yet are not reported missing.
            for member in _expand(element.strip("[]")):
                out.setdefault((int(base), member), (state, reason))
            continue
        member = int(element) if element.isdigit() else None
        out[(int(base), member)] = (state, reason)
    return out


def _accounting_elements(job_ids):
    """{(base id, member): {"state": ...}} from sacct, per array element."""
    if not job_ids:
        return {}
    result = slurm.run(
        ["sacct", "-n", "-X", "-P", "-o", "JobID,State", "-j",
         ",".join(str(i) for i in job_ids)],
        check=False,
    )
    out = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 2:
            continue
        raw, state = parts
        base, _, element = raw.partition("_")
        if not base.isdigit() or element.startswith("["):
            continue
        member = int(element) if element.isdigit() else None
        out[(int(base), member)] = {"state": state.split()[0]}
    return out


def _expand(spec):
    """"1-3,7" -> [1, 2, 3, 7]. Slurm's own array notation, in reverse."""
    out = []
    for part in spec.split(","):
        part = part.split("%")[0]
        if "-" in part:
            low, _, high = part.partition("-")
            if low.isdigit() and high.isdigit():
                out.extend(range(int(low), int(high) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def _attempt_counts(paths):
    out = {}
    for record in ledger.read(paths):
        key = (record["cycle"], record["task"])
        out[key] = out.get(key, 0) + 1
    return out


def finished(statuses, graph):
    """Is the experiment finished, stalled, or still going.

    The distinction the design insists on: a killed submitter is otherwise
    indistinguishable from a completed experiment, because nothing is FAILED,
    the queue drains, and it looks done.
    """
    if any(s.summary in (RUNNING, PENDING, BLOCKED) for s in statuses.values()):
        return "running"
    terminal = max(graph.cycles) if graph.cycles else 0
    last = [s for s in statuses.values() if s.cycle == terminal]
    if last and all(s.summary == COMPLETE for s in last):
        return "finished"
    if any(s.summary in (FAILED, STRANDED) for s in statuses.values()):
        return "broken"
    return "stalled"
