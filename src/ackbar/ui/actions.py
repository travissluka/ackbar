"""The things the console can do to an experiment, and the gate in front of them.

Every action here is a thin wrapper over the library call the argv command makes:
`heal.heal`, `submit_cycle`, `slurm.scancel`, `harvest.write`. None of them
reimplement any part of it. That is deliberate to the point of being the design:
a console that computed its own closure would eventually disagree with
`ackbar heal` about what a failure invalidated, and the two answers would be
found to differ in the middle of a broken overnight run.

The split between a `Plan` and running it is the other half. A plan is pure with
respect to the scheduler: it computes what *would* happen, which is exactly what
`--dry-run` shows, and the modal puts that in front of you before anything is
cancelled or submitted. One keystroke is a fine way to start a heal and a
terrible way to discover it cancelled nineteen live jobs.
"""

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .. import harvest, heal, ledger, slurm
from ..submit import submit_cycle
from . import theme


#: How many node ids of a heal closure are spelled out before the rest become a
#: count. The `nodes` tab has all of them.
SHOWN_CLOSURE = 12


@dataclass
class Plan:
    """What an action would do, and the callable that does it."""

    title: str
    #: Lines to show, already styled. Empty means "nothing to do".
    lines: list = field(default_factory=list)
    #: Shown in the warning colour above the confirmation.
    warning: str = ""
    #: Called with no arguments if confirmed. Returns a summary string.
    run: object = None
    #: Whether the action cancels or submits anything. False skips the modal.
    guarded: bool = True
    #: Set when there is nothing to do; shown as a notification instead.
    nothing: str = ""


def heal_plan(view, site):
    """Cancel a failure's dependents and resubmit them with fresh job ids."""
    experiment = view.experiment
    config, paths, graph = experiment.config, experiment.paths, experiment.graph
    broken, closure, cancel = heal.plan(config, paths, graph, view.statuses)

    if not broken:
        return Plan(title="heal", nothing=f"{experiment.name}: nothing is broken")

    lines = [Text(f"{len(broken)} broken node(s):", style="bold")]
    lines += [Text(line, style=theme.INK["bad"])
              for line in heal.describe(view.statuses, broken)]

    stubborn = heal.unhealable(view.statuses, broken)
    warning = ""
    if stubborn:
        warning = ("resubmitting these unchanged will most likely fail the "
                   "same way: " + ", ".join(n for n, _ in stubborn))

    lines.append(Text(""))
    lines.append(Text(f"closure is {len(closure)} node(s)", style="bold"))
    # The count is the number that matters, and one failed forecast invalidates
    # every cycle in flight, so the full list is routinely fifty ids long and
    # pushes the line saying what will be cancelled off the modal. The head of
    # it says which cycles are involved, which is what a reader checks.
    shown = ", ".join(closure[:SHOWN_CLOSURE])
    if len(closure) > SHOWN_CLOSURE:
        shown += f", and {len(closure) - SHOWN_CLOSURE} more"
    lines.append(Text(f"  {shown}", style=theme.INK["muted"]))
    if cancel:
        lines.append(Text(
            f"will cancel {len(cancel)} live job(s): "
            f"{', '.join(str(i) for i in cancel)}",
            style=theme.INK["warn"],
        ))

    def run():
        _, _, cancelled, records = heal.heal(
            config, site, paths, graph=graph,
        )
        return (f"healed {experiment.name}: cancelled {len(cancelled)}, "
                f"resubmitted {len(records)}")

    return Plan(title=f"heal {experiment.name}", lines=lines, warning=warning,
                run=run)


def start_plan(view, site):
    experiment = view.experiment
    if ledger.submitted_cycles(experiment.paths):
        return Plan(title="start", nothing=(
            f"{experiment.name} has already been started; r resumes a paused "
            f"experiment and h heals a broken one"
        ))

    def run():
        records = submit_cycle(experiment.config, site, experiment.paths, 1)
        return f"started {experiment.name}: {len(records)} job(s) submitted"

    return Plan(
        title=f"start {experiment.name}",
        lines=[
            Text("submit cycle 1.", style="bold"),
            Text("Each cycle's graph contains the job that submits the next, "
                 "so this is the only submission made by hand.",
                 style=theme.INK["muted"]),
        ],
        run=run,
    )


def resume_plan(view, site):
    """Clear the halt flag and submit the cycle the experiment stopped before."""
    experiment = view.experiment
    paths, config = experiment.paths, experiment.config
    cycles = ledger.submitted_cycles(paths)
    if not cycles:
        return Plan(title="resume", nothing=(
            f"{experiment.name} has never been started; press s"
        ))
    nxt = max(cycles) + 1
    if nxt > config["cycle"]["count"]:
        return Plan(title="resume", nothing=(
            f"cycle {max(cycles)} is the last; nothing to re-arm"
        ))

    lines = []
    if paths.halt_flag.exists():
        lines.append(Text(f"remove {paths.halt_flag}",
                          style=theme.INK["muted"]))
    lines.append(Text(f"submit cycle {nxt}", style="bold"))

    def run():
        removed = ""
        if paths.halt_flag.exists():
            paths.halt_flag.unlink()
            removed = "cleared the halt flag, "
        records = submit_cycle(config, site, paths, nxt)
        return (f"{removed}resumed {experiment.name} at cycle {nxt}: "
                f"{len(records)} job(s)")

    return Plan(title=f"resume {experiment.name}", lines=lines, run=run)


def pause_plan(view, site=None):
    """Set the halt flag. Reversible, cheap, and so not gated behind a modal."""
    experiment = view.experiment

    def run():
        experiment.paths.halt_flag.write_text("paused from the ackbar console\n")
        return (f"{experiment.name} will drain and stop at a cycle boundary "
                f"(press r to re-arm)")

    return Plan(title="pause", run=run, guarded=False)


def cancel_plan(view, site=None):
    """Cancel every live job, and hold the halt flag down while doing it.

    The flag goes first and stays down, exactly as `ackbar cancel` does it:
    `submit` is an ordinary job in the graph, so one already running would arm
    the next cycle behind the scancel and the experiment would carry on with a
    hole in it.
    """
    experiment = view.experiment
    paths = experiment.paths

    known = {r["job_id"] for r in ledger.read(paths)}
    live = (set(slurm.queue()) & known) | slurm.named(experiment.name)
    live = sorted(live)

    lines = [
        Text(f"write {paths.halt_flag.name}, so a submitter that is already "
             f"running cannot re-arm behind this", style=theme.INK["muted"]),
    ]
    if live:
        lines.append(Text(f"scancel {len(live)} job(s): "
                          f"{', '.join(str(i) for i in live)}",
                          style=theme.INK["warn"]))
    else:
        lines.append(Text("nothing of this experiment is in the queue",
                          style=theme.INK["muted"]))
    lines.append(Text("artifacts already written are left alone; press r to "
                      "start again", style=theme.INK["muted"]))

    def run():
        paths.halt_flag.write_text("cancelled from the ackbar console\n")
        if live:
            slurm.scancel(live)
        return (f"cancelled {experiment.name}: {len(live)} job(s), halt flag "
                f"left down")

    return Plan(title=f"cancel {experiment.name}", lines=lines,
                warning="this stops the experiment", run=run)


def harvest_plan(view, site):
    """Pull `sacct` into each cycle's stats.json. Writes nothing to the queue."""
    experiment = view.experiment

    def run():
        cycles = experiment.graph.cycles
        jobs = 0
        for cycle in cycles:
            payload = harvest.write(experiment.paths, cycle,
                                    launcher=site.get("launcher", ""))
            jobs += payload["totals"]["jobs"]
        return f"harvested {len(cycles)} cycle(s), {jobs} job(s)"

    return Plan(title="harvest", run=run, guarded=False)


#: Key -> (label, plan builder). The app builds its bindings and its help from
#: this, so a key and its help text cannot drift apart.
ACTIONS = {
    "h": ("heal", heal_plan),
    "s": ("start", start_plan),
    "r": ("resume", resume_plan),
    "p": ("pause", pause_plan),
    "x": ("cancel", cancel_plan),
    "t": ("harvest", harvest_plan),
}


class ConfirmScreen(ModalScreen):
    """The gate. Enter runs the plan, Escape does not, and nothing else does."""

    BINDINGS = [
        ("enter", "confirm", "confirm"),
        ("escape,q", "dismiss_plan", "cancel"),
    ]

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(Text(self.plan.title, style="bold #f0f6fc"),
                         id="confirm-title")
            with VerticalScroll(id="confirm-body"):
                for line in self.plan.lines:
                    yield Static(line)
            if self.plan.warning:
                yield Static(Text(self.plan.warning,
                                  style=f"bold {theme.INK['warn']}"),
                             id="confirm-warning")
            yield Static(Text.assemble(
                ("enter", f"bold {theme.INK['accent']}"), (" run    ",
                                                           theme.INK["muted"]),
                ("esc", f"bold {theme.INK['accent']}"), (" leave it alone",
                                                         theme.INK["muted"]),
            ), id="confirm-keys")

    def action_confirm(self):
        self.dismiss(True)

    def action_dismiss_plan(self):
        self.dismiss(False)


__all__ = ["ACTIONS", "ConfirmScreen", "Plan", "cancel_plan", "harvest_plan",
           "heal_plan", "pause_plan", "resume_plan", "start_plan"]
