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

from .. import slurm
from ..site import load_site
from . import actions, theme
from .panes import (ConfigPane, LogPane, NodesPane, StatsPane,
                    candidates)
from .poll import Poller
from .widgets import Banner, CellDetail, FleetList, StatusGrid

#: Seconds between scheduler refreshes. Five is under the time it takes to read
#: the screen and well over the time a `squeue` takes, and the tick is skipped
#: while a previous one is still running, so a slow scheduler cannot pile up.
INTERVAL = 5.0

#: Seconds between frames of the running-cell animation. Purely local: it
#: repaints from the last report and asks Slurm nothing.
BREATH_INTERVAL = 0.4


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
                        with VerticalScroll(id="grid-scroll"):
                            yield StatusGrid(id="statusgrid")
                        yield CellDetail(id="detail")
                    with TabPane("nodes", id="nodes"):
                        yield NodesPane(id="nodestable")
                    with TabPane("log", id="log"):
                        # Titled, because the pane opens scrolled to the tail
                        # and the file's own header line is off the top by then.
                        yield Static(id="log-title")
                        yield LogPane(id="logview")
                    with TabPane("stats", id="stats"):
                        yield StatsPane(id="statstable")
                    with TabPane("config", id="config"):
                        with VerticalScroll(id="config-scroll"):
                            yield ConfigPane(id="configview")
        yield Footer()

    def on_mount(self):
        self.set_interval(self.interval, self.tick)
        self.set_interval(BREATH_INTERVAL, self.breathe)
        self.tick()

    # --- polling -------------------------------------------------------------

    def tick(self):
        self.poll()

    @work(thread=True, exclusive=True, group="poll")
    def poll(self):
        """One refresh, in a thread. `exclusive` skips a tick still in flight."""
        try:
            report = self.poller.refresh()
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
                    error=self.report.error if self.report else "")
        if view is None:
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
        view = self.view
        self.query_one("#logview", LogPane).show(view, node_id, force=force)
        title = Text(no_wrap=True, overflow="ellipsis")
        if view is not None and node_id:
            cycle, _, task = node_id.partition(".")
            status = view.statuses.get(node_id)
            files = candidates(view.experiment.paths, int(cycle), task,
                               status.job_id if status else None)
            title.append(node_id, style="bold #f0f6fc")
            title.append(f"   {view.experiment.paths.log_dir(int(cycle))}/",
                         style=theme.INK["muted"])
            title.append(", ".join(p.name for p in files[:3]) or "nothing",
                         style=theme.INK["accent"])
        self.query_one("#log-title", Static).update(title)

    def show_detail(self, node_id):
        self.query_one("#detail", CellDetail).show(
            self.view, node_id, palette=self.palette, phase=self.phase,
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

    def on_tabbed_content_tab_activated(self, event):
        if event.pane.id == "log":
            self.show_log(self.query_one("#statusgrid", StatusGrid).cursor_node)

    # --- actions -------------------------------------------------------------

    def action_refresh_now(self):
        self.poll()

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
