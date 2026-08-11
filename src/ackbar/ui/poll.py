"""One refresh, for every experiment at once.

The console shows several experiments live, and the naive way to do that is to
call `state.collect` per experiment on a timer. That runs three scheduler
commands per experiment per tick, and the cost of asking Slurm is almost
entirely per invocation rather than per job id, so ten experiments on a five
second tick is six `sacct` calls a second on a login node for no reason.

So: one `squeue` and one batched `sacct` covering every tracked job id, then
`state.collect` per experiment against that single snapshot. Same answers, one
round trip.

No state is cached. A `Report` is what was true at one instant and is replaced
whole by the next tick. Two things are carried across a tick and neither is a
state cache: the *previous* report, kept so that a scheduler outage can leave the
last known grid on screen instead of blanking it, and `Settled`, which remembers
accounting rows that can no longer change. The first is the distinction
`slurm.accounting` already insists on, "Slurm could not answer" is not "none of
these jobs exist", and a display that erased itself on a slurmdbd restart would
be asserting the second; the other is argued at `Settled` itself.
"""

import time
from dataclasses import dataclass, field

from .. import ledger, slurm, state
from .discover import discover


class Settled:
    """Job ids whose accounting row can no longer change, and what it said.

    The one thing in the console worth remembering across a tick, and it is not
    a cache of state: a job that has left the queue and reached a terminal
    accounting state has *no* further states to reach. Asking again is not
    checking, it is re-reading an immutable record, and the record is the
    expensive part. A twenty-five cycle ensemble experiment accumulates upwards
    of a thousand job ids and all but a dozen of them are ancient history, so
    the cost of a tick otherwise grows with the length of the run rather than
    with how much is happening, which is the wrong way round for a live display.

    Two facts have to hold together before an id is kept, and both are checked:
    nothing of it is in the queue, and every accounting row it has is terminal.
    An array with one element still running is not settled, however many of its
    siblings have finished.

    The one way this can be wrong is a scheduler whose job ids restart, which
    means a slurmctld rebuilt from nothing mid-session. `forget` exists for that
    and `R` calls it, so a refresh asked for by hand asks about everything.
    """

    def __init__(self):
        self.done = {}
        self.elements = {}

    def forget(self):
        self.done.clear()
        self.elements.clear()

    def unasked(self, job_ids):
        """The ids still worth a scheduler round trip."""
        return [i for i in job_ids if i not in self.done]

    def fill(self, snap):
        """Put the remembered rows back into a fresh snapshot."""
        for job_id, record in self.done.items():
            snap.done.setdefault(job_id, record)
        for key, record in self.elements.items():
            snap.done_elements.setdefault(key, record)
        snap.asked = snap.asked | frozenset(self.done)
        return snap

    def absorb(self, snap):
        """Keep whatever this snapshot proved is over."""
        queued = {base for base, _member in snap.live}
        for job_id, record in snap.done.items():
            if job_id in queued or job_id in self.done:
                continue
            elements = {key: row for key, row in snap.done_elements.items()
                        if key[0] == job_id}
            if not _terminal(record) or not all(
                    _terminal(row) for row in elements.values()):
                continue
            self.done[job_id] = record
            self.elements.update(elements)


def _terminal(record):
    return record["state"] not in slurm.ACTIVE


@dataclass
class View:
    """What one experiment is doing, at the instant of one refresh."""

    experiment: object
    #: node id -> NodeStatus
    statuses: dict = field(default_factory=dict)
    #: `state.finished`'s verdict: running, finished, broken or stalled
    overall: str = "stalled"
    #: node ids that need a heal, in graph order
    broken: tuple = ()
    #: cycle -> the worst state among that cycle's nodes, for the sidebar bar
    by_cycle: dict = field(default_factory=dict)
    #: Jobs in the queue, split the way the queue itself splits them. `running`
    #: plus `queued` is what the box is doing; `blocked` is what the graph is
    #: holding, and it is normal for it to dwarf the other two.
    running_jobs: int = 0
    queued_jobs: int = 0
    blocked_jobs: int = 0
    #: how many cycles are complete in full
    done_cycles: int = 0

    @property
    def name(self):
        return self.experiment.name

    @property
    def progress(self):
        """Complete cycles over total, as a fraction for a progress bar."""
        total = len(self.experiment.graph.cycles) or 1
        return self.done_cycles / total


@dataclass
class Report:
    """Every experiment, one instant, plus whether the scheduler answered."""

    views: dict = field(default_factory=dict)
    order: tuple = ()
    at: float = 0.0
    #: The outage message, if the scheduler could not be asked at all. When this
    #: is set the views are the *previous* tick's, deliberately.
    error: str = ""
    #: True when `views` came from an earlier tick than `at`.
    stale: bool = False

    def get(self, name):
        return self.views.get(name)


class Poller:
    """Holds the experiment list, and produces a `Report` per refresh.

    `rescan` and `refresh` are still separate calls, but a refresh rescans:
    `discover` hands back the experiments it was given, so a scan is a glob and a
    handful of stats, and an experiment created while the console is open shows up
    within a tick instead of on the next restart. It did not, and that was the
    wrong trade made for the right reason: the scan was expensive because it
    parsed every frozen config, so it ran once, so `create` was invisible.
    """

    def __init__(self, site, root=None):
        self.site = site
        self.root = root
        self.experiments = []
        self.previous = None
        self.settled = Settled()

    def rescan(self):
        self.experiments = discover(self.site, self.root,
                                    known=self.experiments)
        return self.experiments

    def refresh(self, fresh=False):
        """One tick. Never raises for a scheduler problem; reports it instead.

        A failure to reach Slurm is a fact about the tick, not about the
        experiments, and the console has to keep drawing either way. Anything
        else that goes wrong with a single experiment is confined to that
        experiment's view, for the same reason `discover` skips an unreadable
        config: nine working experiments must stay visible past one broken one.
        """
        if fresh:
            self.settled.forget()
        self.rescan()

        wanted = {}
        for experiment in self.experiments:
            try:
                wanted[experiment.name] = ledger.latest(experiment.paths)
            except OSError:
                wanted[experiment.name] = {}

        job_ids = sorted({
            record["job_id"]
            for records in wanted.values() for record in records.values()
            if record.get("job_id")
        })

        try:
            snap = state.snapshot(self.settled.unasked(job_ids))
        except slurm.SlurmError as error:
            return self._outage(str(error))
        self.settled.absorb(snap)
        self.settled.fill(snap)

        views = {}
        for experiment in self.experiments:
            views[experiment.name] = self._view(
                experiment, snap, wanted.get(experiment.name) or {},
            )

        report = Report(
            views=views,
            order=tuple(e.name for e in self.experiments),
            at=time.time(),
        )
        self.previous = report
        return report

    def _view(self, experiment, snap, records):
        graph = experiment.graph
        try:
            # The same ledger read the snapshot was built from. Re-reading it
            # here would let the submitter slip a whole cycle in between, and
            # every one of those nodes would be a job the scheduler was never
            # asked about; see `state.collect`.
            statuses = state.collect(experiment.paths, graph, snap, records)
        except (OSError, slurm.SlurmError):
            return View(experiment=experiment)

        by_cycle = {}
        for status in statuses.values():
            worst = by_cycle.get(status.cycle)
            if worst is None or _worse(status.summary, worst):
                by_cycle[status.cycle] = status.summary

        order = {node_id: i for i, node_id in enumerate(graph.order())}
        broken = tuple(sorted(
            (i for i, s in statuses.items() if s.broken),
            key=lambda i: order.get(i, 0),
        ))

        counted = {state.RUNNING: 0, state.PENDING: 0, state.BLOCKED: 0}
        for status in statuses.values():
            for element in status.elements.values():
                if element in counted:
                    counted[element] += 1
        done = sum(
            1 for cycle, worst in by_cycle.items() if worst == state.COMPLETE
        )

        return View(
            experiment=experiment,
            statuses=statuses,
            overall=state.finished(statuses, graph),
            broken=broken,
            by_cycle=by_cycle,
            running_jobs=counted[state.RUNNING],
            queued_jobs=counted[state.PENDING],
            blocked_jobs=counted[state.BLOCKED],
            done_cycles=done,
        )

    def _outage(self, message):
        previous = self.previous
        return Report(
            views=previous.views if previous else {},
            order=previous.order if previous else
            tuple(e.name for e in self.experiments),
            at=time.time(),
            error=message,
            stale=previous is not None,
        )


def _worse(this, than):
    return state.SEVERITY.index(this) > state.SEVERITY.index(than)


__all__ = ["Poller", "Report", "View"]
