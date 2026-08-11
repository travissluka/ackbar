"""The panes behind the grid: nodes, log, stats, config.

Each answers a question the grid cannot. The grid says *that* cycle 16's
forecast failed; the log says why, the nodes table says which job id and which
attempt, the stats table says what it cost, and the config pane says what the
experiment was actually asked to do, read from the frozen copy rather than from
the layer tree.

That last point is not a detail. `cfg/experiment.yaml` is what every job of this
experiment read, and the layer tree it came from may have been edited twenty
times since. A console that showed the live layers would answer a different
question than the one being asked.
"""

import json

import yaml
from rich.syntax import Syntax
from rich.text import Text
from textual.widgets import DataTable, RichLog, Static

from .. import state as st
from . import theme

#: How much of a log to read. A model log can be tens of megabytes and the
#: interesting part of a failure is always the end, so this reads the tail
#: rather than the file.
LOG_TAIL_BYTES = 256 * 1024


class NodesPane(DataTable):
    """Every submitted node, with the identity only the ledger knows."""

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("node", "date", "job", "try", "state", "detail")

    def show(self, view, *, palette):
        self.clear()
        if view is None:
            return
        for node_id in _reading_order(view):
            status = view.statuses.get(node_id)
            if status is None or not status.job_id:
                continue
            # Only where it says something. "1 complete" beside a state column
            # already reading "complete" is a column of noise, and the cases
            # worth seeing are an array that disagrees with itself and a queue
            # reason.
            detail = ""
            if len(status.elements) > 1:
                counts = sorted(status.counts().items(),
                                key=lambda kv: st.SEVERITY.index(kv[0]))
                detail = ", ".join(f"{n} {theme.WORD[s]}" for s, n in counts)
            reason = next((r for r in status.reasons.values() if r), "")
            if reason:
                detail = f"{detail}  ({reason})".strip()
            self.add_row(
                Text(node_id, style="bold"),
                Text(view.experiment.paths.date(status.cycle),
                     style=theme.INK["muted"]),
                str(status.job_id),
                Text(str(status.attempt),
                     style=theme.INK["warn"] if status.attempt > 1 else ""),
                Text(theme.WORD[status.summary],
                     style=f"bold {palette[status.summary]}"),
                Text(detail, style=theme.INK["muted"]),
                key=node_id,
            )


def _reading_order(view):
    """Node ids by cycle, and within a cycle in the order they run.

    Not `graph.order()`, which is a topological sort broken by name: that is the
    right order for submitting and a bad one to read, because it emits cycle 1
    in full and then interleaves `10.cleanup` with `11.stage.obs` for the rest of
    the experiment. A table is read down the cycles.
    """
    pipeline = {}
    for node_id in view.experiment.graph.order():
        _, _, task = node_id.partition(".")
        pipeline.setdefault(task, len(pipeline))
    def key(node_id):
        cycle, _, task = node_id.partition(".")
        return (int(cycle), pipeline.get(task, 0))
    return sorted(view.statuses, key=key)


class LogPane(RichLog):
    """The tail of one task's job log, and its model log beside it.

    Slurm expands `%A_%a` itself and ACKBAR never globs the pattern, so finding
    the file is this pane's own problem. A healed attempt lands beside the failed
    one rather than overwriting it, which is the whole point of the pattern, so
    the newest is not always the one being asked about: the job id from the
    ledger picks the right file, and mtime only breaks the tie.
    """

    def __init__(self, **kwargs):
        super().__init__(highlight=False, markup=False, wrap=False, **kwargs)
        self.showing = None

    def show(self, view, node_id, *, force=False):
        if view is None or node_id is None:
            return
        if not force and self.showing == node_id:
            return
        self.showing = node_id
        self.clear()

        status = view.statuses.get(node_id)
        cycle, _, task = node_id.partition(".")
        files = candidates(view.experiment.paths, int(cycle), task,
                           status.job_id if status else None)
        if not files:
            self.write(Text(
                f"no log for {node_id} under "
                f"{view.experiment.paths.log_dir(int(cycle))}",
                style=theme.INK["muted"],
            ))
            return

        for path in files[:3]:
            self.write(Text(f"── {path.name} ", style=theme.INK["accent"]))
            self.write(Text(tail(path), style=theme.INK["text"]))
            self.write("")


def candidates(paths, cycle, task, job_id=None):
    """Log files for one task, the ones belonging to *job_id* first."""
    directory = paths.log_dir(cycle)
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir()
             if p.is_file() and (p.name.startswith(f"{task}.")
                                 or p.name == f"{task}.out")]

    def rank(path):
        mine = job_id is not None and f".{job_id}" in path.name
        # `.out` is Slurm's own capture, which holds the traceback of a task
        # that died before it wrote anything of its own. It goes first.
        return (0 if mine else 1, 0 if path.suffix == ".out" else 1,
                -path.stat().st_mtime)

    return sorted(found, key=rank)


def tail(path, limit=LOG_TAIL_BYTES):
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
                handle.readline()
            data = handle.read()
    except OSError as error:
        return f"cannot read {path}: {error}"
    return data.decode("utf-8", "replace")


class StatsPane(DataTable):
    """What a cycle cost, out of `run/<date>/stats.json`.

    Read rather than computed: `ackbar harvest` writes the file from `sacct`, and
    the console showing a number the file does not contain would be a second
    accounting path to disagree with the first.
    """

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        # Exactly the keys `harvest._totals` writes. A column for a key the file
        # does not have is a column of dashes that reads as a measurement of
        # zero.
        self.add_columns("cycle", "jobs", "failed", "unfinished", "core s",
                         "peak RSS")

    def show(self, view):
        self.clear()
        if view is None:
            return
        found = False
        for cycle in view.experiment.graph.cycles:
            path = view.experiment.paths.stats_file(cycle)
            try:
                with open(path) as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            found = True
            totals = payload.get("totals", {})
            failed = totals.get("failed", 0)
            unfinished = totals.get("unfinished", 0)
            self.add_row(
                Text(view.experiment.paths.date(cycle), style="bold"),
                str(totals.get("jobs", "-")),
                Text(str(failed),
                     style=f"bold {theme.INK['bad']}" if failed else ""),
                Text(str(unfinished),
                     style=theme.INK["warn"] if unfinished else ""),
                str(totals.get("core_seconds", "-")),
                f"{(totals.get('max_rss_kb') or 0) // 1024}M",
            )
        if not found:
            self.add_row(Text("nothing harvested yet; press t",
                              style=theme.INK["muted"]), "", "", "", "", "")


class ConfigPane(Static):
    """The frozen config, syntax highlighted. What the jobs actually read."""

    def show(self, view):
        if view is None:
            self.update("")
            return
        path = view.experiment.paths.frozen_config
        try:
            text = path.read_text()
        except OSError as error:
            self.update(Text(f"cannot read {path}: {error}",
                             style=theme.INK["bad"]))
            return
        # Re-dumped rather than shown verbatim: `create` writes it in merge
        # order, which is not the order anybody reads it in.
        try:
            text = yaml.safe_dump(yaml.safe_load(text), sort_keys=True,
                                  default_flow_style=False, width=100)
        except yaml.YAMLError:
            pass
        self.update(Syntax(text, "yaml", theme="github-dark",
                           background_color="#0d1117", word_wrap=False))


__all__ = ["ConfigPane", "LogPane", "NodesPane", "StatsPane", "candidates",
           "tail"]
