"""The widgets that carry the picture: the banner, the fleet list, the grid.

Two ideas run through all three.

**Colour is the data.** The grid draws one solid block per node and says nothing
in words, so a twenty-five cycle experiment is one glance rather than a table to
read. The sidebar draws the same blocks at one per cycle, so an experiment's
whole history is a stripe next to its name and the leading edge is visible
without opening it.

**The cursor is position, not a symbol.** It highlights its row's label and its
column's number and puts a lighter background down the column, the way a
spreadsheet does. There is exactly one glyph in the whole display, the cursor's
own, and it exists because a full block would hide the highlight behind it.
"""

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .. import state as st
from . import theme

#: Shades the running colour cycles through. A running cell breathes, which is
#: the one piece of motion in the display: a grid that is merely colourful looks
#: identical whether the experiment is advancing or wedged, and "is it actually
#: moving" is the first thing anybody wants to know.
BREATH = ("#1c8578", "#22a294", "#2dd4bf", "#5ee9d8", "#2dd4bf", "#22a294")


def cell_style(node_state, palette, phase=0, *, background=None):
    """The style one grid cell is drawn with."""
    if node_state == st.RUNNING and palette is theme.PALETTES["default"]:
        colour = BREATH[phase % len(BREATH)]
    else:
        colour = palette[node_state]
    style = colour
    if background:
        style = f"{colour} on {background}"
    return style


class Banner(Static):
    """The top line: which experiment, what it is, and whether it is halted."""

    def show(self, view, *, refreshed="", error="", loading=False):
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("ackbar", style=f"bold {theme.INK['accent']}")
        if view is None:
            # Two different facts, and the console used to report the second one
            # while the first was still true: before the first tick lands, "no
            # experiments" is not something anybody has looked yet.
            text.append("   ⟳ reading the output root…" if loading
                        else "   no experiments under this output root",
                        style=theme.INK["muted"])
            self.update(text)
            return

        experiment = view.experiment
        text.append("  ▸  ", style=theme.INK["faint"])
        text.append(experiment.name, style="bold #f0f6fc")
        if experiment.config_name and experiment.config_name != experiment.name:
            # The directory was renamed, so this is an archived run. Worth saying,
            # because its jobs, its ledger records and its `sacct` rows all carry
            # the other name, and because whatever occupies that name now is a
            # different experiment.
            text.append(f"  (jobs named {experiment.config_name})",
                        style=theme.INK["warn"])
        text.append("   ")
        text.append(f"{experiment.domain} · {experiment.solver}"
                    f" · {experiment.model}", style=theme.INK["muted"])
        if experiment.members:
            text.append(f" · {experiment.members} members",
                        style=theme.INK["muted"])
        text.append("   ")
        text.append(f" {view.overall} ",
                    style=f"bold {theme.overall_colour(view.overall)}")
        if experiment.halted:
            # Permanent, because `cancel` leaves the flag down on purpose and
            # the next thing anybody does is wonder why nothing restarts.
            text.append("  ⏸ halted ", style=f"bold {theme.INK['warn']}")
        if error:
            text.append(f"  scheduler not answering: {error.splitlines()[0]}",
                        style=f"bold {theme.INK['bad']}")
        elif refreshed:
            text.append(f"  ⟳ {refreshed}", style=theme.INK["faint"])
        self.update(text)


class FleetList(OptionList):
    """The sidebar. One row per experiment, each carrying its own history.

    An `OptionList` rather than a hand-rolled widget because up/down, wrapping,
    scrolling and mouse selection are exactly what it already does, and because
    the row content can be an arbitrary renderable, which is what the colour
    stripe needs.

    `right` and `enter` hand focus to the grid, and the grid hands it back when
    `left` runs off cycle 1. Everything is reachable on the arrow keys alone,
    which is the point: tab exists, but nobody should have to know that.
    """

    BINDINGS = [
        Binding("right,enter", "app.focus_grid", "open", show=False),
    ]

    def show(self, report, *, palette, phase=0, show_all=False, width=34):
        """Rebuild the rows, keeping the highlight on the same experiment."""
        selected = self.selected_name
        self.clear_options()

        names = []
        for name in report.order:
            view = report.get(name)
            if view is None:
                continue
            if not show_all and _is_old(view):
                continue
            # A row per *distinct* name, because `add_option` raises `DuplicateID`
            # and an exception here kills the whole app on a tick. Identity is the
            # experiment's directory, so this cannot normally happen; it did when
            # identity came from the frozen config's name and two archived runs
            # claimed one, and a display that dies on an odd output root is worse
            # than one that shows the odd root once.
            if name in names:
                continue
            names.append(name)
            self.add_option(Option(
                _fleet_row(view, palette, phase, width), id=name,
            ))

        if not names:
            return None
        target = selected if selected in names else names[0]
        self.highlighted = names.index(target)
        return target

    @property
    def selected_name(self):
        index = self.highlighted
        if index is None:
            return None
        try:
            return self.get_option_at_index(index).id
        except Exception:
            return None


def _is_old(view):
    from .discover import RECENT_SECONDS
    return (view.overall == "finished"
            and view.experiment.age() > RECENT_SECONDS)


def _fleet_row(view, palette, phase, width):
    experiment = view.experiment
    text = Text(no_wrap=True, overflow="ellipsis")

    text.append("● ", style=theme.overall_colour(view.overall))
    text.append(experiment.name[:width - 10], style="bold")
    text.append("\n  ")

    # The whole experiment as one stripe, one cell per cycle. Compressed to the
    # available width by taking the worst state in each bucket, so the stripe
    # never lies in the optimistic direction.
    cycles = experiment.graph.cycles
    room = max(8, width - 10)
    for bucket in _buckets(cycles, room):
        worst = _worst(view.by_cycle.get(c, st.UNSUBMITTED) for c in bucket)
        text.append(theme.CELL, style=cell_style(worst, palette, phase))

    text.append(f" {view.done_cycles}/{len(cycles)}", style=theme.INK["muted"])
    if view.running_jobs:
        text.append(f"  {view.running_jobs}▸", style=theme.INK["accent"])
    if view.queued_jobs:
        text.append(f" {view.queued_jobs}q", style=palette[st.PENDING])
    if experiment.halted:
        text.append("  halted", style=theme.INK["warn"])
    return text


def _buckets(cycles, room):
    """Split *cycles* into exactly *room* groups, stretching or compressing.

    Always the full width, whatever the cycle count. A one cycle experiment and
    a sixty cycle one then draw stripes of the same length, so the sidebar
    compares *fraction done* down the column, which is the thing worth comparing.
    Drawing one cell per cycle instead would make a short experiment a stub and
    a long one a bar, and the eye would read the length as progress.
    """
    if not cycles:
        return []
    out = [[] for _ in range(room)]
    if len(cycles) >= room:
        for index, cycle in enumerate(cycles):
            out[min(room - 1, index * room // len(cycles))].append(cycle)
    else:
        for index in range(room):
            out[index] = [cycles[index * len(cycles) // room]]
    return [b for b in out if b]


def _worst(states):
    worst = st.COMPLETE
    for value in states:
        if st.SEVERITY.index(value) > st.SEVERITY.index(worst):
            worst = value
    return worst


class StatusGrid(Static):
    """Tasks down, cycles across, one block per node, a cursor you drive.

    Arrow keys only, no vim aliases: `h` and `j` are wanted for heal and for
    jumping, and a movement key that shadows an action key depending on which
    widget has focus is the kind of interface that gets one of the two wrong at
    the worst moment.

    A click puts the cursor on the cell under the pointer, and a double click
    opens its log, which is the mouse spelling of `enter`. Worth having even in a
    keyboard-first display: "why did *that* one fail" is a question you ask by
    pointing at it, and counting twenty arrow presses to the cell you can already
    see is the kind of friction that makes a reader go back to `sacct`.
    """

    can_focus = True

    BINDINGS = [
        Binding("up", "move(-1, 0)", "up", show=False),
        Binding("down", "move(1, 0)", "down", show=False),
        Binding("left", "move(0, -1)", "left", show=False),
        Binding("right", "move(0, 1)", "right", show=False),
        Binding("home", "edge(-1)", "first cycle", show=False),
        Binding("end", "edge(1)", "last cycle", show=False),
        Binding("pageup", "move(0, -10)", "back 10", show=False),
        Binding("pagedown", "move(0, 10)", "on 10", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.view = None
        self.palette = theme.palette()
        self.tasks = []
        self.cycles = []
        self.row = 0
        self.column = 0
        self.phase = 0
        self._showing = None
        # What to say when there is nothing to draw. "Nothing submitted yet" is
        # a claim about an experiment, and before the first tick there is no
        # experiment to make a claim about.
        self._message = "reading the output root…"
        #: (label width, cell width, visible cycles) from the last draw. Kept so
        #: a click can be turned back into a cell; the layout is a function of
        #: the pane width and the cycle count, so it is not derivable without it.
        self._layout = (0, 1, [])

    def blank(self, message):
        """Draw *message* instead of a grid: no experiment, or not yet."""
        self.view = None
        self._showing = None
        self.tasks = []
        self._message = message
        self.draw()

    def show(self, view, *, palette=None):
        """Adopt a new view. Keeps the cursor where it was on the same one."""
        if palette is not None:
            self.palette = palette
        arrived = view.name != self._showing
        self._showing = view.name
        self.view = view
        self.tasks = _task_order(view)
        self.cycles = view.experiment.graph.cycles

        if arrived:
            self._to_leading_edge()
        else:
            self.row = max(0, min(self.row, len(self.tasks) - 1))
            self.column = max(0, min(self.column, len(self.cycles) - 1))
        self.draw()

    def draw(self):
        self.update(self.picture())

    def on_resize(self):
        # The window of visible cycles is a function of the width, so a resize
        # is a redraw rather than a reflow of the same text.
        self.draw()

    def _to_leading_edge(self):
        """Open on the cycle something is actually happening in.

        Cycle 1 of a finished experiment is the least interesting cell on the
        screen. The leading edge is where a failure will be and where the
        running jobs are, so that is where the cursor starts.
        """
        self.row = 0
        self.column = max(0, len(self.cycles) - 1)
        if self.view is None:
            return
        for index, cycle in enumerate(self.cycles):
            worst = self.view.by_cycle.get(cycle, st.UNSUBMITTED)
            if worst != st.COMPLETE:
                self.column = index
                break
        broken = self.view.broken
        if broken:
            # A failure outranks the leading edge: it is the thing to look at.
            cycle, _, task = broken[0].partition(".")
            if int(cycle) in self.cycles:
                self.column = self.cycles.index(int(cycle))
            if task in self.tasks:
                self.row = self.tasks.index(task)

    @property
    def cursor_node(self):
        if not self.tasks or not self.cycles:
            return None
        row = min(self.row, len(self.tasks) - 1)
        column = min(self.column, len(self.cycles) - 1)
        return f"{self.cycles[column]}.{self.tasks[row]}"

    def action_move(self, down, across):
        if across < 0 and self.column == 0:
            # Off the left edge and back to the experiment list, the way a file
            # manager walks out of a directory.
            self.post_message(self.LeftEdge())
            return
        if self.tasks:
            self.row = max(0, min(len(self.tasks) - 1, self.row + down))
        if self.cycles:
            self.column = max(0, min(len(self.cycles) - 1,
                                     self.column + across))
        self._moved()

    def action_edge(self, across):
        self.column = 0 if across < 0 else max(0, len(self.cycles) - 1)
        self._moved()

    def on_click(self, event):
        """Put the cursor where the pointer is; twice opens the log."""
        if self.view is None or not self.tasks:
            return
        offset = event.get_content_offset(self)
        if offset is None:
            return
        label, width, window = self._layout
        if not window:
            return

        # The two edges of the grid are the two axes, and clicking either asks
        # for that axis alone. Line 0 is the header of cycle numbers, so clicking
        # a number is a request for that cycle whatever row you were on; the left
        # margin is the task names, so clicking a name is a request for that task
        # in the cycle you were already looking at. Below the last task is the
        # legend, which is not a cell at all.
        column = self.column if offset.x < label else (offset.x - label) // width
        if offset.x >= label:
            if column < 0 or column >= len(window):
                return
            column = self.cycles.index(window[column])
        row = self.row if offset.y == 0 else offset.y - 1
        if not 0 <= row < len(self.tasks) or not 0 <= column < len(self.cycles):
            return
        if offset.x < label and offset.y == 0:
            # The corner is neither axis.
            return

        self.row = row
        self.column = column
        self._moved()
        if event.chain >= 2:
            self.post_message(self.Opened(self.cursor_node))

    def _moved(self):
        self.draw()
        self.post_message(self.Moved(self.cursor_node))

    def on_focus(self):
        # The cursor is drawn differently depending on whether this widget has
        # the keyboard, so focus is a repaint.
        self.draw()

    def on_blur(self):
        self.draw()

    class Moved(Message):
        """The cursor landed on a node."""

        def __init__(self, node_id):
            super().__init__()
            self.node_id = node_id

    class Opened(Message):
        """A node was double clicked: the mouse spelling of `enter`."""

        def __init__(self, node_id):
            super().__init__()
            self.node_id = node_id

    class LeftEdge(Message):
        """`left` was pressed on cycle 1; the sidebar should take focus."""

    def breathe(self, phase):
        """Advance the running-cell animation without recomputing anything."""
        self.phase = phase
        if self.view is not None:
            self.draw()

    def picture(self):
        if self.view is None or not self.tasks:
            return Text(self._message, style=theme.INK["muted"])

        label = max(len(t) for t in self.tasks) + 2
        room = max(4, self.size.width - label - 2)
        width, window = _fit(self.cycles, self.column, room)
        cursor_cycle = self.cycles[min(self.column, len(self.cycles) - 1)]
        self._layout = (label, width, window)
        # Focus is drawn, not merely tracked. With five focusable regions on the
        # screen, "which one do the arrow keys move" has to be visible without
        # pressing one to find out, so the cursor here is bright and its column
        # lit when this widget has the keyboard, and both go quiet when it does
        # not. The stylesheet lights the rail down the left edge to match.
        active = self.has_focus

        text = Text(no_wrap=True)
        text.append_text(_header(window, width, cursor_cycle, label))
        text.append("\n")

        for index, task in enumerate(self.tasks):
            here = index == min(self.row, len(self.tasks) - 1)
            text.append(
                f" {task:<{label - 1}}",
                style=f"bold {theme.INK['accent']}" if here
                else theme.INK["muted"],
            )
            # A gutter once there is room for one. Without it the row is a slab
            # of colour that cannot be counted against the tick marks, and
            # "which cycle is that" is most of what the grid is asked.
            ink = width - 1 if width > 1 else 1
            gutter = " " * (width - ink)
            for cycle in window:
                status = self.view.statuses.get(f"{cycle}.{task}")
                if status is None:
                    text.append(theme.ABSENT_CELL * width)
                    continue
                if here and cycle == cursor_cycle:
                    # Near-white on the state's own colour, rather than the
                    # state's colour on a grey. The cursor has to be findable on
                    # an unsubmitted cell too, and those are nearly black: a
                    # dark glyph on a dark highlight was invisible on exactly
                    # the half of the grid nothing has happened in yet.
                    text.append(
                        theme.CURSOR_CELL * ink,
                        style=(f"bold #f0f6fc on {self.palette[status.summary]}"
                               if active else
                               f"{theme.INK['muted']} on "
                               f"{self.palette[status.summary]}"),
                    )
                    text.append(gutter, style=f"on {theme.INK['cursor']}"
                                if active else f"on {theme.INK['column']}")
                elif cycle == cursor_cycle and active:
                    text.append(theme.CELL * ink, style=cell_style(
                        status.summary, self.palette, self.phase,
                        background=theme.INK["column"],
                    ))
                    text.append(gutter, style=f"on {theme.INK['column']}")
                else:
                    text.append(theme.CELL * ink, style=cell_style(
                        status.summary, self.palette, self.phase))
                    text.append(gutter)
            text.append("\n")

        text.append_text(_legend(self.palette, self.phase))
        return text


#: The widest a cell is allowed to get. Two characters is enough to read as a
#: block rather than as a character, and wider makes a twenty cycle experiment
#: look like a bar chart of nothing.
MAX_CELL_WIDTH = 2


def _fit(cycles, column, room):
    """How wide a cell can be, and which cycles fit. Returns (width, window).

    Cells widen to fill the pane rather than sitting in a thin strip with the
    rest of the terminal empty, and narrow to one character when there are more
    cycles than columns. Only when even one character each will not fit does the
    grid start showing a window instead of the whole experiment, because a
    partial view is worse than a dense one: the question "how far has it got" is
    about the whole run.
    """
    if not cycles:
        return 1, []
    width = max(1, min(MAX_CELL_WIDTH, room // len(cycles)))
    fits = room // width
    if len(cycles) <= fits:
        return width, list(cycles)
    start = max(0, min(len(cycles) - fits, column - fits // 2))
    return width, list(cycles[start:start + fits])


def _header(window, width, cursor_cycle, label):
    """Sparse cycle numbers over the columns they belong to.

    One row of numbers at intervals, rather than the argv command's stack of
    one digit per row. The stacked form exists because a column there is one
    character wide and cannot hold "20"; here a label is allowed to overhang the
    columns to its right, which reads immediately and costs nothing, since the
    cycle under the cursor is spelled out in full in the detail line anyway.
    """
    if not window:
        return Text("", no_wrap=True)

    total = width * len(window)
    buffer = [" "] * total
    # Ticks every fifth cycle at one character per cell, every second when
    # there is room for the digits. Both leave at least three columns between
    # labels, which is what stops "15" and "20" from colliding.
    step = 5 if width == 1 else 2
    edges = (0, len(window) - 1)
    for index, cycle in enumerate(window):
        # The ends are always labelled, whatever the interval: the first and last
        # cycle on screen are the two a reader orients by, and a one cycle
        # experiment would otherwise get no label at all.
        if cycle % step and index not in edges:
            continue
        label_text = str(cycle)
        # Whole label or none. Half of "60" at the right edge reads as cycle 6.
        if index * width + len(label_text) > total:
            continue
        for offset, digit in enumerate(label_text):
            buffer[index * width + offset] = digit

    line = Text(" " * label, no_wrap=True)
    cursor_column = window.index(cursor_cycle) if cursor_cycle in window else -1
    for position, char in enumerate(buffer):
        here = position // width == cursor_column
        line.append(char, style=f"bold {theme.INK['accent']}" if here
                    else theme.INK["faint"])
    return line


def _legend(palette, phase):
    text = Text("\n ", no_wrap=True)
    for node_state in theme.LEGEND:
        text.append(theme.CELL, style=cell_style(node_state, palette, phase))
        text.append(f" {theme.WORD[node_state]}   ", style=theme.INK["muted"])
    return text


def _task_order(view):
    """Task names in pipeline order, not alphabetical.

    `graph.order()` is a topological sort of the whole experiment, so the tasks
    of its first cycle come out in the order they actually run. That makes a
    column readable top to bottom as a cycle's progress, which alphabetical
    never is: `da` before `forecast` before `post` says something, `b.corr_vt`
    before `cleanup` before `da` does not.
    """
    seen = []
    for node_id in view.experiment.graph.order():
        _, _, task = node_id.partition(".")
        if task not in seen:
            seen.append(task)
    present = {s.task for s in view.statuses.values()}
    ordered = [t for t in seen if t in present or not present]
    return ordered or sorted(present)


class CellDetail(Static):
    """Everything about the node under the cursor, including its members.

    The member strip is the reason this is not just a line of text. A twenty
    member forecast is one cell in the grid, and "one of them failed" is the
    interesting case: the strip draws every member so the failure is located
    rather than merely announced.
    """

    def show(self, view, node_id, *, palette, phase=0, member=None):
        if view is None or node_id is None:
            self.update(Text("", no_wrap=True))
            return
        status = view.statuses.get(node_id)
        if status is None:
            self.update(Text(f"{node_id}: not in this graph",
                             style=theme.INK["muted"]))
            return

        text = Text(no_wrap=True)
        text.append(node_id, style="bold #f0f6fc")
        text.append("  ")
        text.append(f" {theme.WORD[status.summary]} ",
                    style=f"bold {palette[status.summary]}")

        date = view.experiment.paths.date(status.cycle)
        text.append(f"  {date}", style=theme.INK["muted"])
        if status.job_id:
            text.append(f"  job {status.job_id}", style=theme.INK["muted"])
        if status.attempt > 1:
            text.append(f"  attempt {status.attempt}",
                        style=f"bold {theme.INK['warn']}")

        counts = status.counts()
        if len(status.elements) > 1:
            text.append("   " + ", ".join(
                f"{n} {theme.WORD[s]}" for s, n in
                sorted(counts.items(), key=lambda kv: st.SEVERITY.index(kv[0]))
            ), style=theme.INK["muted"])

        if status.members and len(status.members) > 1:
            text.append("\n ")
            for index in status.members:
                member_state = status.elements.get(index, st.UNSUBMITTED)
                if index == member:
                    text.append(theme.CURSOR_CELL,
                                style=f"bold #f0f6fc on {palette[member_state]}")
                else:
                    text.append(theme.CELL,
                                style=cell_style(member_state, palette, phase))
            if member is None:
                text.append(f"  mem000-{max(status.members):03d}",
                            style=theme.INK["faint"])
            else:
                text.append(f"  mem{member:03d}",
                            style=f"bold {theme.INK['accent']}")

        reasons = {m: r for m, r in status.reasons.items() if r}
        if reasons:
            first = sorted(reasons.items(), key=lambda kv: (kv[0] is None,
                                                            kv[0]))[:3]
            text.append("\n ")
            text.append("  ".join(
                f"{'' if m is None else f'mem{m:03d} '}{r}" for m, r in first
            ), style=theme.INK["warn"])
        self.update(text)


__all__ = ["Banner", "CellDetail", "FleetList", "StatusGrid", "cell_style"]
