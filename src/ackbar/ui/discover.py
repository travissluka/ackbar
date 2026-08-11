"""Finding the experiments, and holding what is frozen about each.

The console is the first thing in ACKBAR that answers a question about *every*
experiment rather than about a named one, so this is where "what experiments are
there" gets defined: a directory under the site's output root with a frozen
config in it. Nothing else counts. A half-created directory with no
`cfg/experiment.yaml` is not an experiment, and neither is a stray tarball
someone left in the output root.

What lives here is only the part that cannot change while the console is open:
the frozen config, the paths, the graph. State is `poll.py`'s job, refreshed on
a tick. The split matters because the graph is a pure function of a frozen
config, so building it once per experiment per session is correct, while caching
anything Slurm said for that long would not be.
"""

import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ..graph import build_graph
from ..paths import Paths


@dataclass
class Experiment:
    """One experiment on disk, with the parts of it that are frozen.

    Identified by its *directory*, not by the name inside its frozen config. The
    two are the same under `create` and come apart the moment somebody archives a
    run by renaming its directory, which is an ordinary thing to do: rename
    `osse25-3dvar` to `osse25-3dvar-ninner20`, start a fresh `osse25-3dvar`, and
    now two frozen configs on disk both say `osse25-3dvar`. A directory name is
    unique by construction and a config's name is not, so the directory is the
    identity, and `config_name` keeps the other one because that is what the
    ledger records, the job names and `sacct` all carry.
    """

    name: str
    config: dict
    paths: Paths
    #: The name inside the frozen config, which differs from `name` when the
    #: directory has been renamed. Shown in the banner where it differs, because
    #: it is the name Slurm knows this experiment's jobs by.
    config_name: str = ""
    #: The frozen config this was loaded from. The identity a rescan matches on,
    #: rather than the name: the name comes *out* of the file, so matching on it
    #: would mean reading the file to find out whether the file needs reading.
    source: Path = None
    #: `run/ledger.jsonl`'s mtime, or the directory's. Recency is "when did
    #: anything happen to this", and the ledger is appended to on every
    #: submission, so it is the cheapest honest answer.
    touched: float = 0.0
    _graph: object = field(default=None, repr=False)

    @property
    def graph(self):
        """Built once. A frozen config cannot produce two different graphs."""
        if self._graph is None:
            self._graph = build_graph(self.config)
        return self._graph

    @property
    def cycles(self):
        return self.config["cycle"]["count"]

    @property
    def halted(self):
        """Whether the halt flag is down *right now*, so not cached.

        `pause` and `cancel` both write it and only `resume` removes it, and
        `cancel` leaves it deliberately. An interactive tool has to show this
        continuously or the first thing it teaches you is that resume does
        nothing for no reason.
        """
        return self.paths.halt_flag.exists()

    @property
    def domain(self):
        return (self.config.get("domain") or {}).get("name", "?")

    @property
    def solver(self):
        """`variational/3d`, `letkf`, and so on: what kind of DA this is.

        The window type is appended only where it says something, which is why
        this is a property and not a format string at the call site: `3dvar` and
        `3dfgat` differ in nothing else, and the difference is the whole point of
        running both.
        """
        solver = self.config.get("solver") or {}
        name = solver.get("name", "noda")
        window = (solver.get("window") or {}).get("type")
        return f"{name}/{window}" if window else name

    @property
    def members(self):
        return (self.config.get("ensemble") or {}).get("size", 0) or 0

    @property
    def model(self):
        return (self.config.get("model") or {}).get("name", "?")

    @property
    def description(self):
        return (self.config.get("experiment") or {}).get("description", "")

    def age(self, now=None):
        return max(0.0, (now if now is not None else time.time()) - self.touched)


def discover(site, root=None, known=()):
    """Every experiment under the output root, most recently touched first.

    Sorted by recency rather than by name because the question a console is
    opened to answer is almost always about the thing that ran last. An
    experiment whose config will not load or whose graph is unbuildable is
    skipped rather than fatal: one broken directory must not make the console
    unable to show the other nine, and the argv commands will say what is wrong
    with it in more detail than a sidebar row could.

    *known* is any iterable of `Experiment`s a previous scan produced, and the
    ones still on disk are handed back rather than rebuilt. That is what makes a
    scan cheap enough to run on every tick, which is what makes a newly created
    experiment appear on its own instead of when the console is next restarted:
    the glob and its stats are a couple of milliseconds, while parsing ten frozen
    configs and building ten graphs is the better part of a second, and a frozen
    config cannot have changed under us.
    """
    root = Path(root or site["output_root"])
    if not root.is_dir():
        return []

    reusable = {e.source: e for e in known if e.source is not None}
    out = []
    for frozen in sorted(root.glob("*/cfg/experiment.yaml")):
        experiment = reusable.get(frozen)
        if experiment is None:
            experiment = load(frozen, site)
        else:
            # The one thing about it that does move: recency orders the sidebar
            # and decides what `--all` hides.
            experiment.touched = _touched(experiment.paths, frozen)
        if experiment is not None:
            out.append(experiment)
    out.sort(key=lambda e: (-e.touched, e.name))
    return out


def load(frozen, site):
    """One experiment from its frozen config, or None if it cannot be read."""
    frozen = Path(frozen)
    try:
        with open(frozen) as handle:
            config = yaml.safe_load(handle)
        if not isinstance(config, dict) or "experiment" not in config:
            return None
        paths = Paths.of(config, site)
    except (OSError, yaml.YAMLError, KeyError, ValueError):
        return None

    # The directory wins over the name in the config, and the paths are rooted at
    # the directory rather than at what the config calls itself. They agree under
    # `create`; where they do not, it is because a finished run was archived by
    # renaming its directory, and then the config's name belongs to whatever now
    # occupies it. Trusting the config there had two consequences, both seen:
    # the sidebar tried to give two rows the same id and Textual raised
    # `DuplicateID` on the spot, and everything the console read for the archived
    # run, its ledger, its logs and its stats, came from the live run's directory
    # instead of its own.
    directory = frozen.parent.parent.name
    if directory != paths.experiment:
        paths = replace(paths, experiment=directory)
    return Experiment(
        name=directory,
        config_name=config["experiment"]["name"],
        config=config,
        paths=paths,
        source=frozen,
        touched=_touched(paths, frozen),
    )


def _touched(paths, frozen):
    for candidate in (paths.ledger_file, frozen.parent, frozen):
        try:
            return candidate.stat().st_mtime
        except OSError:
            continue
    return 0.0


#: How old a *finished* experiment has to be before the sidebar hides it behind
#: `--all`. Anything unfinished is always shown however old it is, because an
#: experiment that stopped in the middle three weeks ago is exactly the thing
#: worth being reminded about.
RECENT_SECONDS = 14 * 24 * 3600


__all__ = ["Experiment", "RECENT_SECONDS", "discover", "load"]
