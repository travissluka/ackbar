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
| `h` `s` `r` `p` `x` `t` | heal, start, resume, pause, cancel, harvest |
| `R` `A` `P` `q` | refresh now, show all experiments, colour-blind-safe palette, quit |

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

## Queued is not blocked

`state.py` splits Slurm's `PENDING` in two, and the console is why. A job whose queue reason
starts with `Dependency` is `blocked`: the *graph* is holding it, which is most of an experiment
most of the time and is not news. Anything else pending is `queued`: eligible, waiting on the
box. So a growing queued count says the machine is the bottleneck and a queued count that never
falls says something is wrong with the partition, and neither question can be asked of a single
number. `DependencyNeverSatisfied` is recognized ahead of both and is `stranded`, as before.

The sidebar shows `N▸` running and `Nq` queued; blocked is deliberately not counted there, since
it would dwarf both and read as a problem.

## One round trip per tick

`ui/poll.py` runs one `squeue` and one batched `sacct` covering every tracked job id, then calls
`state.collect` per experiment against that single snapshot. The cost of asking Slurm is per
invocation rather than per job id, so the naive one-collect-per-experiment loop is six `sacct`
calls a second on a login node for ten experiments and five second ticks.

That is what `state.snapshot` exists for, and `state.collect(paths, graph, snap=None)` still
queries for itself when nothing passes one, which is what every argv command does.

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
