# The console

`ackbar-ui` is the interactive view of every experiment under `$ACKBAR_OUTPUT_ROOT`, with the
running-experiment verbs behind single keys. `ackbar ui` and a bare `ackbar` are the same thing.

```bash
source site/activate.sh
pip install -e '.[ui]'      # once; textual is an optional dependency
ackbar-ui                   # or: ackbar-ui osse25-3dvar
```

## What it is not

**It drives nothing.** The console holds no state anything else reads, and no part of the
workflow advances inside it. Closing it, losing the ssh session, or killing it mid-refresh does
nothing to any experiment. That is the correction of a specific mistake rather than a nicety:
v3's driver was a foreground curses process the workflow ran *inside*, so a dropped connection
stalled the run. `tests/test_ui.py` asserts that a session of keypresses leaves the experiment
directory byte for byte unchanged.

**It reimplements nothing.** Every action calls the library function the argv command calls:
`heal.heal`, `submit_cycle`, `slurm.scancel`, `harvest.write`. A console that computed its own
failure closure would eventually disagree with `ackbar heal` about what a failure invalidated,
and the disagreement would surface in the middle of a broken overnight run.

**It is not the whole CLI.** `create`, `validate`, `graph` and `config` stay argv-only: they take
a path to an experiment file, run before an experiment exists, and belong somewhere you can pipe
them. `run` and `submit` are what job scripts call and are not for typing. The console covers
what a *running* experiment needs.

## Keys

Everything is reachable on the arrow keys. The footer shows the bindings that are live, and `?`
opens the full map.

| | |
|---|---|
| `↑` `↓` | choose an experiment (sidebar) or a task (grid) |
| `←` `→` | a cycle at a time. `←` on cycle 1 steps back to the sidebar; `→` or `enter` there returns |
| `home` `end` `pgup` `pgdn` | the ends, and ten cycles at a time |
| `tab` | sidebar, grid, panes |
| `1`-`5` | grid, nodes, log, stats, config |
| `enter` | open the log of the node under the cursor |
| `backspace` | back to the grid from any pane |
| `← →` | in the log: which member. `[` `]`: which file. `f`: follow |
| `h` `s` `r` `p` `x` `t` | heal, start, resume, pause, cancel, harvest |
| `R` `A` `P` `q` | refresh now, show all experiments, colour-blind-safe palette, quit |

The mouse works where pointing is the natural thing to do: a click puts the cursor on the cell
under it, a double click opens that cell's log, and the sidebar and the tabs are clickable. "Why
did *that* one fail" is a question you ask by pointing, and counting twenty arrow presses to a
cell already on screen is the kind of friction that sends a reader back to `sacct`.

The grid's two margins are its two axes, and clicking either asks for that axis alone: a cycle
number along the top moves to that cycle whatever row you were on, and a task name down the side
moves to that task in the cycle you were already looking at.

Which region the arrows move is drawn rather than remembered: a teal rail down the left edge of
whichever region has the keyboard, the grid cursor bright and its column lit only while the grid
has it, and the sidebar's highlight dim while it does not. The rail is always there and only
changes colour, so focus moving never shifts the layout by a column.

`h`, `s`, `r`, `x` all open the plan before they touch the queue: heal shows its broken nodes,
its closure, and the job ids it will cancel, and `enter` confirms. `p` and `t` are not gated,
because the halt flag is trivially reversible and a harvest only writes `stats.json`.

## Colour is the data

Every grid cell is the same block and differs only in hue, so a twenty-five cycle experiment is
one glance rather than a table to read. The four cool states form a brightness ramp,
`unsubmitted → blocked → queued → running`, which puts the leading edge of the experiment as the
brightest thing on screen without anything being labelled. `tests/test_ui.py` pins the ramp.

There is exactly one glyph, the cursor's, drawn near-white on the cell's own colour so it is
findable on an unsubmitted cell as well as a complete one. The cursor also lights its row's task
name and its column, the way a spreadsheet does.

`--palette safe`, or `P` in the app, swaps the red/green pair out. Red beside green is the one
combination a sizable fraction of people cannot separate, and a status display nobody can read
is not one.

## The log pane: whose log, which file, and where it is right now

A cycle of a twenty member forecast leaves 168 files in one log directory, so "show the log"
is not a well posed request. Three things resolve it, and each of them is a fact about this
workflow rather than a preference.

**Whose.** Member identity is written three ways and all three are real. Slurm expands `%A_%a`
itself, so its capture is `forecast.59089_7.out`; a task writes its own logs as
`forecast.mem007.59089_7.model.log`; and a task that loops over the members inside one job has no
array index at all, so `da.ens` marks them at the end, `da.ens.59084.hofx_ens.mem001.log`. The
member strip lists whichever members the graph or the directory knows about, `←` `→` step it, and
clicking a block picks it. The pane opens on the first member that *failed*, since that is why a
twenty member cell gets opened.

Task names contain dots, which is why this is not a glob: `forecast.` is a prefix of every one of
`forecast.ext`'s files, and both write a `model.log`. What separates them is what follows the task
name, either the job id or `mem###` and then the job id.

**Which file.** One file at a time, with the rest a bracket or a click away, rather than three
concatenated by modification time. It opens on the first file with anything *in* it: Slurm's
capture is right when it has content, because a task that died before writing anything of its own
leaves its traceback there and nowhere else, but in this workflow it is usually empty either way,
since tasks redirect their output. A successful forecast member leaves a zero byte capture beside
a sixty five kilobyte model log.

**Where it is.** `mom6sis2.launch` points the model's stdout at `model.log` inside the *scratch*
run directory, and `keep_traces` copies that out next to the job's log only once the task has
finished. So while a forecast runs, nothing under `run/<date>/log` grows at all. A pane that
watched only the log directory would show an empty file for the whole of a run and the finished
article afterwards, which is exactly backwards, so `candidates` includes the live files from
`paths.scratch(cycle, task, member)` and puts them first, marked `◂live`. Scratch is deleted when
a task succeeds, so those paths exist exactly while they are the interesting ones, and the pane
hands over to the archived copy when they go.

A member with no logs of its own gets an empty pane saying so, and the state it is in: "mem004
has not run yet: queued". It used to fall back to the whole task's file list, on the theory that a
member which died before writing anything still has Slurm's capture in there. But the capture is
per array element, so it is already in the member's own list whenever it exists, and the fallback's
real effect was to leave the *previous* member's text on screen under this member's name, which is
worse than an empty pane by the amount a confident wrong answer is worse than no answer.

Following is an append, not a re-read: the pane remembers how far it has read and each half second
costs one `stat` plus whatever was added, so the scroll position survives and a growing log does
not flicker. `f` toggles it. Measured against a live forecast member: 33034 bytes at open, rising
to 35554 over ten seconds, then the member finished and the pane was reading the 65 kilobyte
archived copy.

## Queued is not blocked

`state.py` splits Slurm's `PENDING` in two, and the console is why. A job whose queue reason
starts with `Dependency` is `blocked`: the *graph* is holding it, which is most of an experiment
most of the time and is not news. Anything else pending is `queued`: eligible, waiting on the
box. So a growing queued count says the machine is the bottleneck and a queued count that never
falls says something is wrong with the partition, and neither question can be asked of a single
number. `DependencyNeverSatisfied` is recognized ahead of both and is `stranded`, as before.

The sidebar shows `N▸` running and `Nq` queued; blocked is deliberately not counted there, since
it would dwarf both and read as a problem.

## What a tick costs

Three things keep a refresh at a couple of hundred milliseconds over ten experiments and a
thousand job ids. Each was a real cost, measured on rancor rather than guessed at, and the first
two were what made the whole tick take longer than the interval between ticks.

**One round trip, not one per experiment.** `ui/poll.py` runs one `squeue` and one batched
`sacct` covering every tracked job id, then calls `state.collect` per experiment against that
single snapshot. The cost of asking Slurm is per invocation rather than per job id, so the naive
one-collect-per-experiment loop is six `sacct` calls a second on a login node for ten experiments
and five second ticks. That is what `state.snapshot` exists for, and
`state.collect(paths, graph, snap=None)` still queries for itself when nothing passes one, which
is what every argv command does.

**Two columns, not every field.** `sacct --json` serializes everything Slurm knows for every job:
at a thousand ids that is fifteen megabytes to produce and parse, and it measured ten seconds
against three hundred milliseconds for `-P -o JobID,State`. So `state.snapshot` asks through
`slurm.accounting_states`, which reads per array element, and `slurm.collapse_elements` does the
worst-element collapse that `accounting` did internally. The hardened `accounting` is unchanged
and still what the submitter and healer use, and the cheap call falls through to it when it comes
back with nothing at all, which is the one case an exit code cannot tell from a purge.

**A finished job is asked about once.** `poll.Settled` remembers accounting rows that can no
longer change: nothing of the job in the queue, every row it has terminal. Asking again is not
checking, it is re-reading an immutable record, and without it the cost of a tick grows with how
long an experiment has run rather than with how much is happening. `R` forgets the lot and asks
about everything, which is the escape hatch for the one way it can be wrong, a scheduler whose
job ids restart.

A refresh also rescans the output root, so an experiment created while the console is open
appears within a tick instead of on the next restart. That is affordable because `discover` hands
back the `Experiment` objects it was given rather than re-parsing ten frozen configs and
rebuilding ten graphs: the config is frozen, so there is nothing to re-read.

Until the first tick lands the banner says it is reading the output root. "No experiments under
this output root" is a finding, and stating it before anything has been looked at is stating
something false for as long as the first scan takes.

Polling runs in a thread worker, so a slow `sacct` makes the clock in the corner stale and
nothing else, and a tick still in flight is skipped rather than queued. When Slurm cannot be
reached at all the last grid stays on screen under a banner, because "Slurm could not answer" is
not "none of these jobs exist" and a display that blanked itself would be asserting the second.

## Why textual is optional

Every compute job imports `ackbar`, and a terminal UI framework has no business being installed
on a node that only runs a model. So `textual` is the `ui` extra, `src/ackbar/ui/` is the only
package allowed to import it, and nothing outside that package imports `ackbar.ui` at module
scope: `cli.py` reaches it from inside the command function. `ui/theme.py` and
`ui/discover.py` import neither textual nor rich, so the palette and the experiment scan stay
testable in a bare environment.

## Testing it

```bash
.venv/bin/python -m pytest -q tests/test_ui.py
```

Textual's `run_test` drives the real app headlessly with a fake scheduler, so these are ordinary
tier 0 tests needing no terminal and no Slurm. They are marked `ui` and skip where textual is
absent. The two properties worth the trouble: no key reaches the queue without the modal being
accepted, and quitting changes nothing on disk.
