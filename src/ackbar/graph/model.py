"""The task graph as data.

The graph is a pure function of the merged configuration, which is what makes
healing possible: a subgraph can be regenerated at any time and resubmitted
without touching the rest. So nothing here reads the filesystem, asks Slurm
anything, or depends on when it ran.

A node is one *submission*, not one process. An ensemble member array is a
single node carrying its index set, because that is what `sbatch --array` is,
and because the elementwise `aftercorr` edge only exists between array jobs.
Flattening members into separate nodes would model something Slurm cannot
submit.

A member-level task is an array even when the set has one element, as it does
for a free run. Emitting that one as a scalar job instead would look tidier and
would silently invalidate every `aftercorr` edge into it, which Slurm reports
as a job that pends forever rather than as an error.
"""

from dataclasses import dataclass, field

#: Slurm dependency types, used exactly as Slurm spells them.
AFTEROK = "afterok"
AFTERANY = "afterany"
AFTERCORR = "aftercorr"

EDGE_KINDS = (AFTEROK, AFTERANY, AFTERCORR)


class GraphError(Exception):
    pass


@dataclass
class Node:
    cycle: int
    task: str
    #: The member index set for an array job; empty for a scalar job.
    members: tuple = ()
    #: Repository-relative path to the executable, or None for a task that is
    #: python and shell only.
    exe: str = None
    resources: dict = field(default_factory=dict)

    @property
    def id(self):
        return f"{self.cycle}.{self.task}"

    @property
    def is_array(self):
        return bool(self.members)

    @property
    def jobs(self):
        """How many jobs this node counts as against a submit limit.

        Every array task counts individually, which is the number that actually
        approaches a site's per-user limit.
        """
        return len(self.members) if self.members else 1


@dataclass(frozen=True)
class Edge:
    parent: str
    child: str
    kind: str


class Graph:
    def __init__(self, experiment, throttle=1):
        self.experiment = experiment
        self.throttle = throttle
        self._nodes = {}
        self.edges = []

    def add(self, node):
        if node.id in self._nodes:
            raise GraphError(f"duplicate node {node.id}")
        self._nodes[node.id] = node
        return node

    def link(self, parent, child, kind):
        """Add an edge, if both endpoints exist.

        Tolerating a missing endpoint is deliberate: the edge table below is
        written once for the whole workflow, and which of its tasks exist is a
        property of the configuration. An edge to a task this experiment does
        not run is not an error, it is that task being disabled.
        """
        if parent not in self._nodes or child not in self._nodes:
            return None
        if kind not in EDGE_KINDS:
            raise GraphError(f"unknown dependency type {kind!r}")
        edge = Edge(parent, child, kind)
        self.edges.append(edge)
        return edge

    def get(self, cycle, task):
        return self._nodes.get(f"{cycle}.{task}")

    def has(self, cycle, task):
        return f"{cycle}.{task}" in self._nodes

    @property
    def nodes(self):
        return list(self._nodes.values())

    @property
    def cycles(self):
        return sorted({n.cycle for n in self._nodes.values()})

    def parents(self, node_id):
        return [e for e in self.edges if e.child == node_id]

    def children(self, node_id):
        return [e for e in self.edges if e.parent == node_id]

    def cycle_nodes(self, cycle):
        return [n for n in self._nodes.values() if n.cycle == cycle]

    def order(self):
        """Node ids in topological order.

        ACKBAR sorts and detects cycles itself rather than leaving it to Slurm,
        whose own cycle detection stops at `max_depend_depth` (default 10) and
        which reports a longer cycle as a job that simply never runs.
        """
        pending = {i: 0 for i in self._nodes}
        outgoing = {i: [] for i in self._nodes}
        for edge in self.edges:
            pending[edge.child] += 1
            outgoing[edge.parent].append(edge.child)

        # Sorted, so the order is a function of the graph and not of dict
        # insertion. Goldens depend on it.
        ready = sorted(i for i, n in pending.items() if n == 0)
        out = []
        while ready:
            current = ready.pop(0)
            out.append(current)
            fresh = []
            for child in outgoing[current]:
                pending[child] -= 1
                if pending[child] == 0:
                    fresh.append(child)
            ready = sorted(ready + fresh)

        if len(out) != len(self._nodes):
            stuck = sorted(set(self._nodes) - set(out))
            raise GraphError(
                f"the task graph has a cycle involving: {', '.join(stuck)}"
            )
        return out

    def to_dict(self):
        """The serializable form, used for goldens and `ackbar graph --json`."""
        return {
            "experiment": self.experiment,
            "throttle": self.throttle,
            "nodes": [
                {
                    "id": n.id,
                    "cycle": n.cycle,
                    "task": n.task,
                    "members": list(n.members),
                    "exe": n.exe,
                    "resources": n.resources,
                }
                for n in sorted(self._nodes.values(), key=lambda n: (n.cycle, n.task))
            ],
            "edges": [
                {"from": e.parent, "to": e.child, "kind": e.kind}
                for e in sorted(self.edges, key=lambda e: (e.child, e.parent, e.kind))
            ],
        }
