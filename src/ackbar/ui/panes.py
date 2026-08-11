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
from textual.binding import Binding
from textual.message import Message
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
    """One log file, tailed, and followed while it grows.

    One file rather than three concatenated. A twenty member forecast cycle
    leaves a hundred and sixty eight files in one log directory, and picking
    three of them by modification time answers a question nobody asked: it
    showed some other member's stdout beside this one's model log, in an order
    that changed between refreshes. Which file is now chosen rather than
    guessed, by the `MemberStrip` and `FileRow` above the text, and this pane's
    whole job is to hold that one file and keep up with it.

    Following is an append, not a re-read. The offset of what has already been
    written is remembered, so a growing model log costs one `stat` and the new
    bytes per tick rather than a quarter megabyte, and the text on screen does
    not flicker or lose the reader's scroll position. A file that got *shorter*
    was truncated or replaced under us, so that one is read again from scratch.
    """

    #: How much of a newly opened file to show. The interesting part of a
    #: failure is always the end.
    TAIL_BYTES = LOG_TAIL_BYTES

    #: The most a single follow tick will append. A model that dumps a megabyte
    #: in one go should not stall the event loop; the rest arrives next tick.
    CHUNK_BYTES = 64 * 1024

    #: `up`/`down`/`pgup`/`pgdn`/`home`/`end` stay RichLog's own scrolling, which
    #: is what they mean in every pane. `left`/`right` step members, matching the
    #: grid, where left and right are the other axis of the same thing; with no
    #: members to step they fall back to scrolling sideways. Horizontal scrolling
    #: keeps a shifted spelling for the long lines a model log has.
    BINDINGS = [
        Binding("left", "app.member(-1)", "member", show=False),
        Binding("right", "app.member(1)", "member", show=False),
        Binding("shift+left", "scroll_left", "left", show=False),
        Binding("shift+right", "scroll_right", "right", show=False),
        Binding("left_square_bracket", "app.log_file(-1)", "file", show=False),
        Binding("right_square_bracket", "app.log_file(1)", "file", show=False),
        Binding("f", "app.follow", "follow", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(highlight=False, markup=False, wrap=False,
                         auto_scroll=True, **kwargs)
        self.path = None
        self.read_to = 0
        self.following = True

    def show(self, path, *, force=False):
        """Adopt *path*. Same path and not *force* means keep following it."""
        if path is not None and path == self.path and not force:
            return
        self.path = path
        self.read_to = 0
        self.clear()
        if path is None:
            self.write(Text("no log file for this task yet",
                            style=theme.INK["muted"]))
            return
        try:
            size = path.stat().st_size
        except OSError as error:
            self.write(Text(f"cannot read {path}: {error}",
                            style=theme.INK["bad"]))
            return
        start = max(0, size - self.TAIL_BYTES)
        if start:
            self.write(Text(f"… {start} earlier bytes not shown",
                            style=theme.INK["faint"]))
        self._append(start, size)

    def poll(self):
        """One follow tick: whatever has been appended since the last one."""
        if self.path is None or not self.following:
            return False
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size == self.read_to:
            return False
        if size < self.read_to:
            # Truncated or replaced: what is on screen is not this file's
            # beginning any more, so the honest thing is to read it again.
            self.show(self.path, force=True)
            return True
        return self._append(self.read_to, min(size, self.read_to
                                             + self.CHUNK_BYTES))

    def _append(self, start, end):
        try:
            with open(self.path, "rb") as handle:
                handle.seek(start)
                data = handle.read(max(0, end - start))
        except OSError as error:
            self.write(Text(f"cannot read {self.path}: {error}",
                            style=theme.INK["bad"]))
            return False
        self.read_to = start + len(data)
        text = data.decode("utf-8", "replace")
        if text:
            # Trailing newline stripped: `write` puts one line per call, so
            # keeping it would leave a blank line growing at the bottom.
            self.write(Text(text.rstrip("\n"), style=theme.INK["text"]))
        return bool(text)


def candidates(paths, cycle, task, job_id=None, member=None):
    """Log files for one task, best first, optionally for one member only.

    Two things about the names make this less obvious than a glob.

    Task names contain dots, so a prefix match does not respect the task
    boundary: `forecast.` is a prefix of every one of `forecast.ext`'s files,
    and the forecast node's log list used to be half its extended forecast's.
    What separates them is what follows the task name, which is either the job
    id or `mem###` and then the job id.

    Member identity is written two ways, and both are load-bearing. Slurm
    expands `%A_%a` itself, so its own capture is `<task>.<jobid>_<index>.out`,
    while a task writes its own logs as `<task>.mem###.<jobid>_<index>.…`. An
    array element's index is its member number, so either spelling answers
    "whose log is this".

    A healed attempt lands beside the failed one rather than overwriting it,
    which is the whole point of the pattern, so the newest file is not always
    the one being asked about: the job id from the ledger wins, and mtime only
    breaks the remaining ties.
    """
    directory = paths.log_dir(cycle)
    found = []
    if directory.is_dir():
        found = [p for p in directory.iterdir()
                 if p.is_file() and _belongs(p.name, task, member)]

    def rank(path):
        mine = job_id is not None and f".{job_id}" in path.name
        return (0 if mine else 1, _kind(path.name, task), -path.stat().st_mtime)

    # The live copies first, because a task that is still running is the reason
    # anybody is following a log, and the archived copy of a finished one cannot
    # move.
    return live_files(paths, cycle, task, member) + sorted(found, key=rank)


#: What counts as a log in a scratch run directory. The rest of what is in there
#: is input: namelists, parameter files, restarts, a few hundred megabytes of
#: NetCDF. Suffixes rather than a list of names, because SOCA and the model leave
#: different files and both spell them this way.
LIVE_SUFFIXES = (".log", ".out", ".stats")


def live_files(paths, cycle, task, member=None):
    """The logs a *running* task is writing, which are not in the log directory.

    This is a property of the workflow rather than a detail of this pane.
    `mom6sis2.launch` points the model's stdout at `model.log` inside its scratch
    run directory, and `keep_traces` copies that, `ocean.stats` and the rest out
    next to the job's log only once the task has finished. So while a forecast
    runs, *nothing* in `run/<date>/log` grows: the member's archived files do not
    exist yet and Slurm's own capture is empty, because the model's output was
    redirected away from it.

    A console that followed only the log directory would therefore show an empty
    file for the whole of a two minute forecast and the finished article
    afterwards, which is precisely backwards. Scratch is deleted when a task
    succeeds, so these paths exist exactly while they are the interesting ones.
    """
    directories = []
    if member is not None:
        directories.append(paths.scratch(cycle, task, member))
    else:
        directories.append(paths.scratch(cycle, task))
        # A memberless view of an array task: every member's live run directory,
        # found by name rather than by asking the graph how many there are.
        parent = paths.scratch(cycle, task).parent
        if parent.is_dir():
            directories += sorted(p for p in parent.glob(f"{task}.mem*")
                                  if p.is_dir())

    out = []
    for directory in directories:
        if not directory.is_dir():
            continue
        out += sorted((p for p in directory.iterdir()
                       if p.is_file() and p.name.endswith(LIVE_SUFFIXES)),
                      key=_live_rank)
    return out


def _live_rank(path):
    """`.log` before `.out` before `.stats`, then by name.

    The same idea as `_kind` and a different implementation because these names
    carry no task or job id to strip: MOM6's stdout is `model.log` and FMS's own
    log is `logfile.000000.out`, and alphabetical order puts the less useful one
    first.
    """
    suffix = 0 if path.name.endswith(".log") else (
        1 if path.name.endswith(".out") else 2)
    return (suffix, path.name)


def _identity(name, task):
    """(member, what follows the job id) for a log of *task*, or None.

    None means the file is not this task's: either it does not start with the
    task name, or what follows is another task's rather than a job id.
    """
    if not name.startswith(f"{task}."):
        return None
    rest = name[len(task) + 1:]
    owner, _, tail = rest.partition(".")
    member = None
    if owner.startswith("mem") and owner[3:].isdigit():
        member = int(owner[3:])
        rest = tail
    ident = rest.split(".")[0]
    base, _, element = ident.partition("_")
    if not base.isdigit():
        return None
    if member is None and element.isdigit():
        member = int(element)
    tail = rest[len(ident) + 1:]
    if member is None:
        # A third spelling, and it is not redundant: a task that loops over the
        # members itself is one job with no array index, and marks whose output
        # a file is at the end instead. `da.ens` writes
        # `da.ens.<job>.hofx_ens.mem001.log`, twenty of those in one directory,
        # and they are per member logs by any reasonable reading.
        member = _trailing_member(tail)
    return member, tail


def _trailing_member(tail):
    for part in tail.split("."):
        if part.startswith("mem") and part[3:].isdigit():
            return int(part[3:])
    return None


def _belongs(name, task, member=None):
    identity = _identity(name, task)
    if identity is None:
        return False
    return member is None or identity[0] == member


def _kind(name, task):
    """Sort key by what a file is: Slurm's capture, a log, then everything else.

    Slurm's own capture holds the traceback of a task that died before it wrote
    anything of its own, so it goes first, and a failure is the reason this pane
    is usually open. `.log` next, since that is where a model or a JEDI app puts
    its account of a run that started.
    """
    identity = _identity(name, task)
    tail = identity[1] if identity else name
    if tail == "out":
        return 0
    return 1 if tail.endswith(".log") or tail == "log" else 2


def members_of(files, task):
    """Which members the files on disk actually speak for, in order.

    Asked of the files rather than of the graph, because the two can differ in
    the direction that matters: a task that loops over members itself is one
    node with no members at all as far as the graph is concerned, and twenty per
    member logs as far as the log directory is concerned.
    """
    out = set()
    for path in files:
        identity = _identity(path.name, task)
        if identity and identity[0] is not None:
            out.add(identity[0])
            continue
        # A live file has no member in its name: it is in the member's scratch
        # run directory, which is where the name is.
        member = _trailing_member(path.parent.name)
        if member is not None and path.parent.name.startswith(f"{task}."):
            out.add(member)
    return sorted(out)


def for_member(files, task, member):
    """The subset of *files* belonging to *member*; all of them if it is None."""
    if member is None:
        return list(files)
    return [p for p in files if _belongs(p.name, task, member)]


class MemberStrip(Static):
    """One block per member of the node being read, the chosen one lit.

    The same picture the cell detail draws, made steerable. A twenty member
    forecast is one cell in the grid and one of its members is the one that
    failed, so "which member am I reading" has to be both visible and a key
    press away rather than a filename to type.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.members = []
        self._first = 0

    def show(self, members, selected, elements, palette, *, phase=0):
        from .widgets import cell_style

        self.members = list(members)
        text = Text(no_wrap=True, overflow="ellipsis")
        if len(self.members) < 2:
            self.update(text)
            return
        text.append(" member ", style=theme.INK["muted"])
        self._first = len(text.plain)
        for member in self.members:
            state = elements.get(member, st.UNSUBMITTED)
            if member == selected:
                text.append(theme.CURSOR_CELL,
                            style=f"bold #f0f6fc on {palette[state]}")
            else:
                text.append(theme.CELL, style=cell_style(state, palette, phase))
        if selected is None:
            text.append(f"  all {len(self.members)}",
                        style=theme.INK["muted"])
        else:
            text.append(f"  mem{selected:03d}",
                        style=f"bold {theme.INK['accent']}")
        text.append("   ← → ", style=theme.INK["faint"])
        self.update(text)

    def on_click(self, event):
        offset = event.get_content_offset(self)
        if offset is None or not self.members:
            return
        index = offset.x - self._first
        if 0 <= index < len(self.members):
            self.post_message(self.Picked(self.members[index]))

    class Picked(Message):
        def __init__(self, member):
            super().__init__()
            self.member = member


class FileRow(Static):
    """The task's other log files, one click or one bracket away.

    A member of a forecast leaves seven files: Slurm's capture, the model log,
    `ocean.stats`, the parameter documents. Which one answers the question
    depends on the question, so they are all listed and none of them is hidden
    behind knowing the naming scheme.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._spans = []

    #: How many names to show at once. A task that writes one log per member
    #: without an array index puts fifty five files in one directory, and a row
    #: of fifty five names is a row nobody reads; the count says what is off
    #: screen and `[` `]` reach it.
    WINDOW = 6

    def show(self, files, index, task, log_dir=None):
        text = Text(no_wrap=True, overflow="ellipsis")
        self._spans = []
        if not files:
            self.update(text)
            return
        text.append(" file   ", style=theme.INK["muted"])
        # The count and the keys that move it before the names, which are what
        # runs off the end of the line.
        text.append(f"[ ] {index + 1}/{len(files)}  ", style=theme.INK["faint"])
        first = max(0, min(len(files) - self.WINDOW, index - self.WINDOW // 2))
        if first:
            text.append("… ", style=theme.INK["faint"])
        for position in range(first, min(len(files), first + self.WINDOW)):
            path = files[position]
            label = _short(path.name, task)
            # Marked, because the difference matters: one is being written now
            # and vanishes when the task succeeds, the other is the record.
            if log_dir is not None and path.parent != log_dir:
                label = f"◂live {label}"
            start = len(text.plain)
            if position == index:
                text.append(f" {label} ", style="bold #0d1117 on #2dd4bf")
            else:
                text.append(f" {label} ", style=theme.INK["muted"])
            self._spans.append((start, len(text.plain), position))
            text.append(" ")
        self.update(text)

    def on_click(self, event):
        offset = event.get_content_offset(self)
        if offset is None:
            return
        for start, end, position in self._spans:
            if start <= offset.x < end:
                self.post_message(self.Picked(position))
                return

    class Picked(Message):
        def __init__(self, index):
            super().__init__()
            self.index = index


def _short(name, task):
    """A file's name with the part every sibling shares taken off the front."""
    identity = _identity(name, task)
    return identity[1] if identity and identity[1] else name


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


__all__ = ["ConfigPane", "FileRow", "LogPane", "MemberStrip", "NodesPane",
           "StatsPane", "candidates", "for_member", "members_of", "tail"]
