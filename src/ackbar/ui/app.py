"""The app: one screen, every experiment, every verb behind a single key.

Three rules it is built to keep.

**Nothing here is load-bearing.** The console drives no part of the workflow and
holds no state that anything else reads. Closing it, losing the ssh session it
runs in, or killing it mid-refresh does nothing at all to an experiment. That is
not a nice property, it is the correction of a specific mistake: v3's driver was
a foreground curses process the workflow advanced *inside*, so a dropped
connection stalled the experiment. Viewing must never be able to do that.

**One scheduler round trip per tick, off the event loop.** Polling runs in a
thread worker, so a `sacct` that takes eight seconds makes the clock in the
corner stale and nothing else. The UI never blocks on Slurm.

**No action without its plan.** Every key that cancels or submits opens the
plan first. See `actions.py`.
"""

import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from .. import slurm, state as st
from ..site import load_site
from . import actions, theme
from .panes import (ConfigPane, FileRow, LogPane, MemberStrip, NodesPane,
                    StatsPane, candidates, members_of)
from .poll import Poller
from .widgets import Banner, CellDetail, FleetList, StatusGrid

#: Seconds between scheduler refreshes. Five is under the time it takes to read
#: the screen and well over the time a `squeue` takes, and the tick is skipped
#: while a previous one is still running, so a slow scheduler cannot pile up.
INTERVAL = 5.0

#: Seconds between frames of the running-cell animation. Purely local: it
#: repaints from the last report and asks Slurm nothing.
BREATH_INTERVAL = 0.4

#: Seconds between follow ticks on the open log file. Faster than the scheduler
#: tick because it costs one `stat` and only reads what was appended, and
#: because a log you are watching is being watched to see it move.
FOLLOW_INTERVAL = 0.5


def _first_worth_reading(status):
    """Which member's log to open on: the first that went wrong, else the first.

    A twenty member forecast is opened because one member failed, and finding
    which one by stepping through twenty logs is the work this saves.
    """
    if status is None or not status.members:
        return None
    for member in sorted(status.members):
        if status.elements.get(member) in (st.FAILED, st.STRANDED):
            return member
    return sorted(status.members)[0]


def _default_file(files):
    """Which of a node's files to open on: one with something in it.

    Slurm's capture is the best first file when it has content, because a task
    that died before writing anything of its own leaves its traceback there and
    nowhere else. But in this workflow it is *usually* empty, running or
    finished: tasks redirect their own output, so a whole successful member of a
    forecast leaves a zero byte capture beside a sixty five kilobyte model log.
    Opening on the empty one and following it looks exactly like a wedged job,
    which is the one thing a log pane must not imitate.

    So: the first file in `candidates`' order that has anything in it. That
    order is already the useful one, capture then `.log` then the rest, newest
    first within each; skipping the empty ones is the whole fix.

    Not "the most recently written file with content", which was tried against a
    real cycle and is wrong: MOM6 writes its parameter documents last, so a
    finished member's newest file is `MOM_parameter_doc.layout` and its account
    of the run is `model.log`.
    """
    if not files:
        return 0
    for index, path in enumerate(files):
        if _size(path):
            return index
    return 0


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unfocusable(widget):
    """Take a container out of the tab order. `can_focus` is a class var."""
    widget.can_focus = False
    return widget


class AckbarUI(App):
    """`ackbar-ui`."""

    CSS_PATH = "app.tcss"
    TITLE = "ackbar"
    #: Textual's own ctrl+p palette is off: it offers commands from a vocabulary
    #: that is not ACKBAR's, and the footer showing "palette" next to `heal` and
    #: `cancel` invites the guess that it is one of ours.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("q,ctrl+c", "quit", "quit"),
        Binding("question_mark", "help", "keys"),
        Binding("tab", "focus_next", "focus", show=False),
        Binding("shift+tab", "focus_previous", "focus", show=False),
        Binding("R", "refresh_now", "refresh"),
        Binding("A", "toggle_all", "all/recent"),
        Binding("h", "act('h')", "heal"),
        Binding("s", "act('s')", "start", show=False),
        Binding("r", "act('r')", "resume", show=False),
        Binding("p", "act('p')", "pause", show=False),
        Binding("x", "act('x')", "cancel"),
        Binding("t", "act('t')", "harvest", show=False),
        Binding("enter", "open_log", "log"),
        # Out of a pane and back to the grid. Backspace because the panes are
        # somewhere you went rather than somewhere you are, and because escape
        # alone would be the only way back out of the log and it is also the key
        # that dismisses a modal.
        Binding("backspace,escape", "back_to_grid", "back", show=False),
        Binding("1", "tab('grid')", "grid", show=False),
        Binding("2", "tab('nodes')", "nodes", show=False),
        Binding("3", "tab('log')", "log", show=False),
        Binding("4", "tab('stats')", "stats", show=False),
        Binding("5", "tab('config')", "config", show=False),
        Binding("P", "toggle_palette", "palette", show=False),
    ]

    def __init__(self, site=None, root=None, palette="default",
                 interval=INTERVAL, show_all=False):
        super().__init__()
        self.site = site if site is not None else load_site()
        self.poller = Poller(self.site, root)
        self.palette_name = palette
        self.palette = theme.palette(palette)
        self.interval = interval
        self.show_all = show_all
        self.report = None
        self.selected = None
        #: Which node the log pane is showing, which of its members, and which
        #: of that member's files. Held here rather than in the pane because the
        #: cell detail draws the same member choice and the two must agree.
        self.log_node = None
        self.member = None
        self.file_index = 0
        #: Whether the file on screen was picked by the reader rather than by
        #: `_default_file`. A choice sticks; a default is reconsidered.
        self.file_chosen = False
        self.phase = 0
        self._rebuilding = False
        self._focused = False
        self._last_refresh = 0.0

    # --- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("experiments", classes="pane-title")
                yield FleetList(id="fleet")
            with Vertical(id="main"):
                with TabbedContent(id="tabs"):
                    with TabPane("grid", id="grid"):
                        # Not itself a tab stop: the grid inside it is, and two
                        # stops for one pane means tab lands somewhere the arrow
                        # keys do nothing, which reads as the arrows being broken.
                        with _unfocusable(VerticalScroll(id="grid-scroll")):
                            yield StatusGrid(id="statusgrid")
                        yield CellDetail(id="detail")
                    with TabPane("nodes", id="nodes"):
                        yield NodesPane(id="nodestable")
                    with TabPane("log", id="log"):
                        # Titled, because the pane opens scrolled to the tail
                        # and the file's own header line is off the top by then.
                        yield Static(id="log-title")
                        yield MemberStrip(id="log-members")
                        yield FileRow(id="log-files")
                        yield LogPane(id="logview")
                    with TabPane("stats", id="stats"):
                        yield StatsPane(id="statstable")
                    with TabPane("config", id="config"):
                        with VerticalScroll(id="config-scroll"):
                            yield ConfigPane(id="configview")
        yield Footer()

    def on_mount(self):
        # Said before the first tick, and it is a different sentence from "there
        # are no experiments". The scan and the scheduler both take a moment on a
        # busy machine, and a display that opens by reporting an empty output root
        # is telling the reader something false for as long as it takes to find
        # out otherwise.
        self.query_one("#banner", Banner).show(None, loading=True)
        self.set_interval(self.interval, self.tick)
        self.set_interval(BREATH_INTERVAL, self.breathe)
        self.set_interval(FOLLOW_INTERVAL, self.follow)
        self.tick()

    # --- polling -------------------------------------------------------------

    def tick(self):
        self.poll()

    @work(thread=True, exclusive=True, group="poll")
    def poll(self, fresh=False):
        """One refresh, in a thread. `exclusive` skips a tick still in flight."""
        try:
            report = self.poller.refresh(fresh=fresh)
        except Exception as error:  # noqa: BLE001 - a tick must never kill the app
            self.call_from_thread(self.notify, f"refresh failed: {error}",
                                  severity="error")
            return
        self.call_from_thread(self.apply, report)

    def apply(self, report):
        self.report = report
        self._last_refresh = report.at

        fleet = self.query_one("#fleet", FleetList)
        self._rebuilding = True
        try:
            chosen = fleet.show(report, palette=self.palette,
                                show_all=self.show_all,
                                width=self._sidebar_width())
        finally:
            self._rebuilding = False
        if chosen and self.selected not in report.views:
            self.selected = chosen
        if self.selected is None:
            self.selected = chosen
        self.redraw()

        # The grid takes focus once there is something in it, because that is
        # where the work happens. Only on the first report: stealing focus on
        # every tick would move the cursor out from under whatever the reader is
        # doing every five seconds.
        if not self._focused and self.selected:
            self._focused = True
            self.action_focus_grid()

    def _sidebar_width(self):
        try:
            return max(24, self.query_one("#sidebar").size.width - 2)
        except Exception:
            return 34

    def breathe(self):
        self.phase += 1
        try:
            self.query_one("#statusgrid", StatusGrid).breathe(self.phase)
        except Exception:
            pass

    def follow(self):
        """Append whatever the open log grew by. Off the scheduler's clock.

        Only while the log pane is the one on screen: following a file nobody is
        looking at is a stat every half second for nothing, and the pane reads
        from the file rather than from a report, so there is no state to keep
        warm while it is hidden.
        """
        if self.query_one("#tabs", TabbedContent).active != "log":
            return
        self.query_one("#logview", LogPane).poll()

    # --- drawing -------------------------------------------------------------

    @property
    def view(self):
        if self.report is None:
            return None
        return self.report.get(self.selected)

    def redraw(self):
        view = self.view
        banner = self.query_one("#banner", Banner)
        banner.show(view, refreshed=self._ago(),
                    error=self.report.error if self.report else "",
                    loading=self.report is None)
        if view is None:
            self.query_one("#statusgrid", StatusGrid).blank(
                "reading the output root…" if self.report is None
                else "no experiments under this output root")
            return

        grid = self.query_one("#statusgrid", StatusGrid)
        grid.show(view, palette=self.palette)
        self.show_detail(grid.cursor_node)

        self.query_one("#nodestable", NodesPane).show(view, palette=self.palette)
        self.query_one("#statstable", StatsPane).show(view)
        self.query_one("#configview", ConfigPane).show(view)
        if self.query_one("#tabs", TabbedContent).active == "log":
            self.show_log(grid.cursor_node)

    def show_log(self, node_id, *, force=False):
        """Draw the log pane for *node_id*, its chosen member and file."""
        view = self.view
        strip = self.query_one("#log-members", MemberStrip)
        row = self.query_one("#log-files", FileRow)
        pane = self.query_one("#logview", LogPane)
        title = Text(no_wrap=True, overflow="ellipsis")

        if view is None or not node_id:
            self.log_node = None
            strip.show([], None, {}, self.palette)
            row.show([], 0, "")
            pane.show(None, force=True)
            self.query_one("#log-title", Static).update(title)
            return

        status = view.statuses.get(node_id)
        cycle, _, task = node_id.partition(".")
        arrived = node_id != self.log_node
        if arrived:
            # A different node: start from the member worth looking at rather
            # than from member zero, the same reason the grid opens on the
            # leading edge.
            self.log_node = node_id
            self.member = _first_worth_reading(status)
            self.file_index = None
            self.file_chosen = False
            force = True

        paths = view.experiment.paths
        job_id = status.job_id if status else None
        everything = candidates(paths, int(cycle), task, job_id)
        members = self._log_members(status, everything, task)
        files = candidates(paths, int(cycle), task, job_id, self.member)
        if not files:
            # Nothing carries this member's name: it died before writing
            # anything of its own, so the whole task's logs are better than a
            # blank pane, and Slurm's capture is in there.
            files = everything
        # `None` means "whichever file is worth opening", which is what arriving
        # at a node and changing member both want: the same member number of a
        # different node, or a different member of the same node, has its own set
        # of empty files.
        if arrived or self.file_index is None:
            self.file_index = _default_file(files)
        elif files:
            here = min(self.file_index, len(files) - 1)
            gone = self.file_index >= len(files) or not files[here].exists()
            # A member that has only just started has nothing but its empty
            # capture, and its model log appears a second later. Left alone the
            # reader would sit watching a file that will never grow while the
            # real one fills up beside it, so an unchosen empty file is
            # reconsidered on each scheduler tick. Only unchosen: pressing `[`
            # to look at the capture has to be allowed to stick.
            #
            # A file that has *gone* overrides even a choice, because a live log
            # in scratch is deleted the moment its task succeeds, and there is
            # nothing to be loyal to.
            if gone or (not self.file_chosen and not _size(files[here])):
                self.file_index = _default_file(files)
                self.file_chosen = False
        self.file_index = max(0, min(self.file_index, len(files) - 1))

        strip.show(members, self.member, status.elements if status else {},
                   self.palette, phase=self.phase)
        row.show(files, self.file_index, task,
                 log_dir=paths.log_dir(int(cycle)))
        pane.show(files[self.file_index] if files else None, force=force)

        # Hints first, path last. The path is the only part that can be two
        # hundred characters long, and anything after it is a key nobody can
        # see: the affordances go where the line cannot truncate them.
        title.append(node_id, style="bold #f0f6fc")
        if self.member is not None and members:
            title.append(f" mem{self.member:03d}",
                         style=f"bold {theme.INK['accent']}")
        title.append("   f follow ", style=theme.INK["faint"])
        title.append("on" if pane.following else "off",
                     style=f"bold {theme.INK['accent']}" if pane.following
                     else theme.INK["muted"])
        title.append("   ⌫ back   ", style=theme.INK["faint"])
        # The file's own directory, not the log directory: a live log is in
        # scratch, and a path that says otherwise is one nobody can `less`.
        open_file = files[self.file_index] if files else None
        title.append(f"{open_file.parent}/" if open_file
                     else f"{paths.log_dir(int(cycle))}/",
                     style=theme.INK["muted"])
        title.append(open_file.name if open_file else "nothing",
                     style=theme.INK["accent"])
        self.query_one("#log-title", Static).update(title)

    def show_detail(self, node_id):
        # The member cursor is shown here too, so the strip under the grid and
        # the log pane's own strip cannot disagree about whose log `enter` opens.
        self.query_one("#detail", CellDetail).show(
            self.view, node_id, palette=self.palette, phase=self.phase,
            member=self.member if node_id == self.log_node else None,
        )

    def _ago(self):
        if not self._last_refresh:
            return ""
        return f"{int(max(0, time.time() - self._last_refresh))}s"

    # --- events --------------------------------------------------------------

    def on_option_list_option_highlighted(self, event):
        """Sidebar selection. Ignored while the list is being rebuilt."""
        if self._rebuilding or event.option_list.id != "fleet":
            return
        name = event.option.id
        if name and name != self.selected:
            self.selected = name
            self.redraw()

    def action_focus_grid(self):
        self.query_one("#tabs", TabbedContent).active = "grid"
        self.query_one("#statusgrid", StatusGrid).focus()

    def on_status_grid_left_edge(self):
        self.query_one("#fleet", FleetList).focus()

    def on_status_grid_moved(self, event):
        self.show_detail(event.node_id)
        if self.query_one("#tabs", TabbedContent).active == "log":
            self.show_log(event.node_id)

    def on_status_grid_opened(self, event):
        self.action_tab("log")
        self.show_log(event.node_id, force=True)

    def on_tabbed_content_tab_activated(self, event):
        if event.pane.id == "log":
            self.show_log(self.query_one("#statusgrid", StatusGrid).cursor_node)

    # --- actions -------------------------------------------------------------

    def action_refresh_now(self):
        # By hand means "ask about everything", including the jobs whose outcome
        # was taken as final. See `poll.Settled`.
        self.poll(fresh=True)

    def action_back_to_grid(self):
        self.action_focus_grid()

    def action_toggle_all(self):
        self.show_all = not self.show_all
        self.notify("showing every experiment" if self.show_all
                    else "showing recent experiments")
        if self.report:
            self.apply(self.report)

    def action_toggle_palette(self):
        self.palette_name = "safe" if self.palette_name == "default" else "default"
        self.palette = theme.palette(self.palette_name)
        self.notify(f"{self.palette_name} palette")
        self.redraw()

    def action_tab(self, name):
        self.query_one("#tabs", TabbedContent).active = name

    def action_open_log(self):
        grid = self.query_one("#statusgrid", StatusGrid)
        self.action_tab("log")
        self.show_log(grid.cursor_node, force=True)
        # Focus follows, so that the member and file keys are live without a tab
        # press, and so backspace has somewhere to come back from.
        self.query_one("#logview", LogPane).focus()

    def _log_members(self, status, files, task):
        """Which members the log pane can step through for this node.

        The graph's answer when it has one, the log directory's otherwise. They
        differ for a task that loops over the members inside one job: no members
        as far as the graph is concerned, twenty per member logs on disk.
        """
        if status is not None and status.members:
            return sorted(status.members)
        return members_of(files, task)

    def action_member(self, step):
        """Step to another member's log. `left` and `right` in the log pane."""
        view = self.view
        status = view.statuses.get(self.log_node) if view else None
        cycle, _, task = (self.log_node or ".").partition(".")
        members = []
        if view is not None and self.log_node:
            members = self._log_members(
                status,
                candidates(view.experiment.paths, int(cycle), task,
                           status.job_id if status else None),
                task,
            )
        if not members:
            # Nothing to step through, so the arrows go back to being what they
            # are everywhere else in a scrolling pane.
            pane = self.query_one("#logview", LogPane)
            pane.scroll_right() if step > 0 else pane.scroll_left()
            return
        if self.member is None:
            # From "all of them" into the first or the last, depending on which
            # way you stepped.
            self.member = members[0] if step > 0 else members[-1]
        else:
            where = members.index(self.member) if self.member in members else 0
            self.member = members[max(0, min(len(members) - 1, where + step))]
        self.file_index = None
        self.file_chosen = False
        self.show_log(self.log_node, force=True)

    def action_select_member(self, member):
        self.member = member
        self.file_index = None
        self.file_chosen = False
        self.show_log(self.log_node, force=True)

    def action_log_file(self, step):
        self.file_chosen = True
        self.file_index = max(0, (self.file_index or 0) + step)
        self.show_log(self.log_node, force=True)

    def action_select_file(self, index):
        self.file_chosen = True
        self.file_index = index
        self.show_log(self.log_node, force=True)

    def action_follow(self):
        pane = self.query_one("#logview", LogPane)
        pane.following = not pane.following
        if pane.following:
            pane.poll()
            pane.scroll_end(animate=False)
        self.show_log(self.log_node)

    def on_member_strip_picked(self, event):
        self.action_select_member(event.member)

    def on_file_row_picked(self, event):
        self.action_select_file(event.index)

    def action_act(self, key):
        """Build the plan for *key*, then gate it unless it is trivial."""
        view = self.view
        if view is None:
            self.notify("no experiment selected", severity="warning")
            return
        label, builder = actions.ACTIONS[key]
        try:
            plan = builder(view, self.site)
        except (slurm.SlurmError, OSError) as error:
            self.notify(f"{label}: {error}", severity="error")
            return

        if plan.nothing:
            self.notify(plan.nothing)
            return
        if not plan.guarded:
            self.run_plan(plan)
            return
        self.push_screen(actions.ConfirmScreen(plan),
                         lambda go: self.run_plan(plan) if go else None)

    @work(thread=True, group="act")
    def run_plan(self, plan):
        """Run a confirmed plan off the event loop; `sbatch` is not instant."""
        try:
            message = plan.run()
        except Exception as error:  # noqa: BLE001 - reported, never a crash
            self.call_from_thread(self.notify, f"{plan.title}: {error}",
                                  severity="error", timeout=12)
            return
        self.call_from_thread(self.notify, message, timeout=10)
        self.call_from_thread(self.poll)

    def action_help(self):
        self.push_screen(HelpScreen())


class HelpScreen(ModalScreen):
    """Every key, in one place."""

    BINDINGS = [("escape,q,question_mark", "dismiss_help", "close")]

    def compose(self) -> ComposeResult:
        rows = [
            ("↑ ↓", "choose an experiment (sidebar) or a task (grid)"),
            ("← →", "move a cycle at a time; home and end jump to the ends"),
            ("pgup pgdn", "ten cycles at a time"),
            ("tab", "move between the sidebar, the grid and the panes"),
            ("1 - 5", "grid, nodes, log, stats, config"),
            ("enter", "open the log of the node under the cursor"),
            ("backspace", "back to the grid from any pane"),
            ("click", "put the cursor on a cell; twice opens its log"),
            ("", ""),
            ("← →", "in the log: which member. It opens on the one that failed"),
            ("[ ]", "in the log: which of that member's files"),
            ("f", "in the log: follow the file as it grows"),
            ("", ""),
            ("h", "heal: cancel a failure's dependents and resubmit them"),
            ("s", "start the first cycle"),
            ("r", "resume: clear the halt flag and re-arm"),
            ("p", "pause at the next cycle boundary"),
            ("x", "cancel every live job, and hold the halt flag down"),
            ("t", "harvest sacct into each cycle's stats.json"),
            ("", ""),
            ("R", "refresh now"),
            ("A", "show every experiment, not only the recent ones"),
            ("P", "switch to the colour-blind-safe palette"),
            ("q", "quit; this does nothing to any experiment"),
        ]
        with Vertical(id="confirm"):
            yield Static(Text("keys", style="bold #f0f6fc"), id="confirm-title")
            with VerticalScroll(id="confirm-body"):
                for key, what in rows:
                    yield Static(Text.assemble(
                        (f"  {key:<11}", f"bold {theme.INK['accent']}"),
                        (what, theme.INK["text"]),
                    ))
            yield Static(Text("h, s, r, x and t always ask before they touch "
                              "the queue.", style=theme.INK["muted"]),
                         id="confirm-keys")

    def action_dismiss_help(self):
        self.dismiss(None)


__all__ = ["AckbarUI", "HelpScreen"]
