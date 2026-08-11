"""Tier 1: every observation file is staged before something reads it.

The gap this module closes cost a whole experiment. The observation archive was
moved from per window files to calendar time bins joined at stage time, which is
the right shape, and the join was given to `stage.obs`: cycle T's observers read
`obs_in/T/`, which cycle T's own `stage.obs` builds. Every tier 0 and tier 1
test of that passed, because every one of them was about a task reading its own
cycle's window.

`hofx.ext` is not. It scores a five day forecast against the observations at its
lead valid times, which fall in the windows of cycles that have not run: cycle
1's F048 window is staged by cycle 3. So every `hofx.ext` failed on an input
file that does not exist yet, on every cycle, and nothing in the suite noticed
because nothing in the suite asked where `hofx.ext`'s observations come from.

So the test here is not about `hofx.ext`. It is the property that would have
caught it and will catch the next one: **for every task in the graph, every
observation file it hands to an observer is staged by a task that is an ancestor
of it, or by itself.** A task that reads a file no task stages fails it, and so
does a task that reads a file staged by a task Slurm has no reason to run first.

`run.observation_inputs` and `run.observation_staging` are the two halves, and
they answer out of the same calls the bodies make, so a body that changes where
it reads from changes what this test checks.
"""

from pathlib import Path

import pytest

from ackbar import run
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import build_graph
from ackbar.paths import Paths

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

SITE = {"scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static", "root": str(REPO)}

#: One per shape that reads observations. `hybrid_om1deg` is the one that
#: carries an extended forecast, and therefore the only one whose tasks read a
#: window that is not their own cycle's; the others are here so that the check
#: is a property of the workflow rather than a test of one experiment.
FIXTURES = ["hybrid_om1deg", "var_om1deg", "free_om1deg", "fourd_om1deg"]


@pytest.fixture(scope="module")
def keys():
    return merge_keys(load_schema())


def load(name, keys):
    layers = resolve_layers(EXPERIMENTS / f"{name}.yaml", LAYERS)
    return resolve(merge_layers(layers, keys), SITE)


def ancestors(graph):
    """Every node id that must have run before each node, by id.

    Transitive, because the ordering that matters is Slurm's: an edge chain
    through three tasks orders the ends of it just as firmly as one edge would.
    """
    parents = {node.id: set() for node in graph.nodes}
    for edge in graph.edges:
        parents[edge.child].add(edge.parent)
    reach = {}

    def walk(node_id):
        if node_id in reach:
            return reach[node_id]
        reach[node_id] = set()          # guards against a cycle; `order` refuses one
        found = set()
        for parent in parents[node_id]:
            found.add(parent)
            found |= walk(parent)
        reach[node_id] = found
        return found

    for node in graph.nodes:
        walk(node.id)
    return reach


def elements(node):
    """The member indices of a node, or a single None for a scalar job."""
    return node.members or (None,)


def staged(config, paths, graph):
    """Every observation file the graph stages, and which nodes stage it."""
    producers = {}
    for node in graph.nodes:
        for member in elements(node):
            for path in run.observation_staging(config, paths, node.cycle,
                                                node.task, member):
                producers.setdefault(path, set()).add(node.id)
    return producers


@pytest.mark.parametrize("name", FIXTURES)
def test_every_observation_file_is_staged_before_it_is_read(name, keys):
    config = load(name, keys)
    paths = Paths.of(config, SITE)
    graph = build_graph(config)
    producers = staged(config, paths, graph)
    reach = ancestors(graph)

    # One writer per file, asserted before the ordering is. Two tasks building
    # the same observation file is the shared staging area this workflow does
    # not have: it races, and it makes "who produced this" unanswerable, which
    # is the question the ordering below is asked about.
    shared = {path: sorted(nodes) for path, nodes in producers.items()
              if len(nodes) > 1}
    assert not shared, (
        "these observation files are staged by more than one task, so two jobs "
        "write one path:\n"
        + "\n".join(f"  {path}: {', '.join(nodes)}"
                    for path, nodes in sorted(shared.items())))

    read = 0
    for node in graph.nodes:
        for member in elements(node):
            for path in run.observation_inputs(config, paths, node.cycle,
                                               node.task, member):
                read += 1
                assert path in producers, (
                    f"{node.id} reads {path}, which no task in the graph "
                    f"stages")
                producer = sorted(producers[path])[0]
                assert producer == node.id or producer in reach[node.id], (
                    f"{node.id} reads {path}, which {producer} stages, and "
                    f"{producer} is not an ancestor of {node.id}: nothing "
                    f"orders the two, so the file may not exist when it is "
                    f"read")

    # Not a vacuous pass. Every fixture here carries observers, so a run that
    # checked nothing means the two halves stopped seeing the tasks that read.
    assert read, f"{name} carries observers and no task was found reading one"


def test_the_extended_forecast_reads_windows_no_cycle_of_its_own_has_staged(keys):
    """The specific shape, stated so the general test above cannot go quiet.

    A `hofx.ext` in cycle 1 reads the windows of cycles 2 and later. If those
    reads ever stop appearing, the invariant test still passes and stops being
    about anything.
    """
    config = load("hybrid_om1deg", keys)
    paths = Paths.of(config, SITE)
    windows = {record["window"]
               for record in run._hofx_ext_observers(config, paths, 1, 0)}
    assert len(windows) > 1
    own = run.window_bounds(config, 1)
    assert own not in windows
    assert all(begin >= own[1] for begin, _ in windows)


def test_the_extended_forecast_reads_no_other_cycles_staging_directory(keys):
    """Where the fix is, said as a path rather than as an ordering.

    `obs_in/<T>` belongs to cycle T's `stage.obs`, and a task that reaches into
    another cycle's is reading a directory that does not exist until that cycle
    runs. `hofx.ext` stages its own, into its own scratch, so nothing it reads
    is under the shared tree at all.
    """
    config = load("hybrid_om1deg", keys)
    paths = Paths.of(config, SITE)
    reads = run.observation_inputs(config, paths, 1, "hofx.ext", 0)
    assert reads
    shared = paths.sub("obs_in")
    assert not [path for path in reads if shared in path.parents]
    assert all(paths.scratch(1, "hofx.ext", 0) in path.parents for path in reads)
