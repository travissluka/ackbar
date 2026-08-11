# Moving from soca-science to ACKBAR

For someone who knows ocean DA and knows soca-science, and does not know ACKBAR.
This is what changes and what to unlearn. `README.md` is the outside view;
`docs/design.md` is the reasoning.

## The one shift that explains the rest

soca-science is one Slurm job that runs every step of every cycle in sequence
and resubmits itself when it runs low on walltime. ACKBAR is a Python program
that computes a dependency graph and hands it to Slurm, then gets out of the
way. There is no driver process, no daemon, no loop. Cycle *n*'s graph contains
a job whose only work is to submit cycle *n+1*.

Almost everything below follows from that. Steps are jobs, so they have their
own resources and their own logs. Ensemble members are array elements, so they
run concurrently instead of in a `for` loop on the whole allocation.
Post-processing has no successors, so it overlaps the next cycle instead of
blocking it. And because Slurm owns the graph, fixing a failure means
resubmitting a subgraph rather than restarting a cycle.

## Defining an experiment

soca-science: copy `scripts/workflow/exp.config`, edit roughly ninety variables,
symlink `cycle.sh` into the experiment directory (the symlink is load bearing,
it is how the script finds the repo), run it.

ACKBAR: write a short YAML file that names the layers it is built from. Here is
the whole of the 3DVar arm of the current OSSE, minus its comments:

```yaml
inherit:
  - domain/gom_25km
  - model/mom6sis2
  - da/variational
  - da/corr_vt_cycled
  - forcing/gefs
  - obs/adt_j2
  - obs/adt_saral
  # ... eleven more obs layers

experiment:
  name: osse25-3dvar

cycle:
  start: '2015-07-12T00:00:00Z'
  length: PT24H
  count: 20

vars:
  obs_dir: $(static_root)/obs/gom12-osse-2015-era5-binned-gom_25km/2015

forecast:
  extended: {length: P5D, every: PT24H, keep_states: PT24H, slots: PT6H}

cleanup:
  keep_cycles: 1

model:
  initial_condition: $(static_root)/ic/gom_25km/osse-control-25km/20150712T00
```

Layers live under `config/layers/<kind>/<name>.yaml` and deep merge in the order
listed, later winning, experiment file last. A layer may itself `inherit:`, and
there it means something narrower: not "assemble a stack" but "I am a kind of
that". `obs/adt_j2` inherits `obs/common/adt` because Jason-2 is an altimeter.
Listing a layer twice, or listing one an earlier layer already pulled in, is
refused with an explanatory error; a diamond reached through the tree is deduped
in silence.

Lists replace wholesale, except where the schema declares an identifying key.
There is exactly one today: `observations` merges on `obs space.name`, which is
what lets `da/letkf` change one observer's localization without restating all
thirteen. `$remove` deletes an inherited key or list element.

The three things soca-science's `sed` did are now three named passes with
different sigils:

| soca-science | ACKBAR | Filled |
|---|---|---|
| `__SEED__`, `__FCST_HR__` token replacement | `{{seed}}`, `{{current_cycle:%Y%m%d%H}}` | per job, from a closed set `ackbar config symbols` prints |
| `$(experiment_dir)` at setup | `$(static_root)`, `$(obs_dir)` | once at `create`, then frozen |
| `!IF_APP_letkf` line prefixes, `__OBSERVATIONS__` splicing with `sed -i "s/^/    /g"` | `$(OBSERVERS)`, `$(BACKGROUND_ERROR)` slots in `config/soca/*.yaml`, filled by `src/ackbar/soca.py` | per task |

Merge happens first, then substitution, then validation. No YAML is ever
produced by text splicing; it is emitted from data structures, and templates are
strict in both directions. An unfilled slot is an error, and so is a computed
value the template does not use, because that means the template has silently
dropped a block.

Provenance is a real feature now. `ackbar config why experiments/osse25-3dvar.yaml 'vars.ninner'`
replays the merge with the layer list truncated at each level and says which
file last changed the value. Neither prior workflow could answer that at all.

## Driving a cycle

```bash
ackbar validate experiments/osse25-3dvar.yaml   # six checks, each reported individually
ackbar create   experiments/osse25-3dvar.yaml   # freeze cfg/, emit every cycle's job script
ackbar start    osse25-3dvar                    # submit cycle 1, and only cycle 1
```

One rule worth internalising: `validate`, `create`, `graph` and `config` take a
*path to a file*; `start`, `status`, `heal`, `pause`, `resume`, `cancel` and
`harvest` take an *experiment name*. Before `create` there is nothing to name;
after it, the layer tree is never read again, so editing a layer cannot change a
run in flight.

`ackbar validate` has no soca-science counterpart and is the single biggest
quality-of-life change. It generates every job's YAML for every cycle, checks
every referenced input path exists, checks every executable runs, checks the
graph is acyclic and that every member-level array shares one index set, and
projects the job count against the site's queue limits. `--offline` skips the
three steps that touch the filesystem and says which ones it skipped. The
failure mode you are used to, discovering a bad path at cycle 3 at 04:00, mostly
goes away.

## The steps you know by name

| soca-science step | ACKBAR |
|---|---|
| `prep.forc` | gone from the cycle. Forcing is an offline archive; `ensemble.inputs` links each member's `atm.nc` into `INPUT/` at forecast time |
| `prep.obs` | `stage.obs`. Joins the archive time bins this window touches into `obs_in/<T>/<platform>.nc4` and lets ioda make the single half open cut, which is what `obs_cat.x` plus `soca_preqc.py -f time` did for you; that app is not built in this bundle, so the join is Python. Drops observers with nothing inside the window (unless `$required: true`) and writes `obs_out/<T>/observers.json` recording every configured observer and why each did or did not run. No downloading and no conversion |
| `prep.bkgrst`, `prep.bkgrst.ens` | gone. The background is the previous cycle's restart set, resolved by path. Cycle 0 is materialized from the offline initial condition at `create` time, so cycle 1 is not special |
| `prep.soca` | offline: `tools/soca-gridspec.sh`, `tools/soca-diffusion.sh`, run once per domain into `$ACKBAR_STATIC_ROOT/static/<domain>/`. The per-cycle vertical B calibration v2 did as a side effect is now an explicit task, `b.corr_vt`, opt-in via `da/corr_vt_cycled` |
| `run.var`, `run.letkf` | both are `da`. Which executable runs is `solver.name` |
| `run.hofx` | `hofx`, and only when `solver: none`. In a DA run the analysis application writes `ombg`/`oman` itself |
| `run.recenter.checkpoint` | split in two: `recenter` (one MPI job over the whole ensemble) and `writeback` (a per-member array). Writeback is a direct restart write, not the checkpoint app, which no longer exists in this SOCA |
| `run.fcst` | `forecast`, a job array. `forecast.ext` is the long forecast, a leaf on its own cadence |
| `post.obs` | `post.obs`. Still reduces the departure files, but now *computes* per-observer counts and O-B/O-A statistics into `obs_out/<T>/summary.json` rather than only running `ncks -g`. It derives departures as `ObsValue - hofx<low>` and `ObsValue - hofx<high>` rather than trusting `ombg`/`oman`, so the sign note in your `iodaplots` configs is not inherited |
| `post.state` | `post.state`, per member. Same NCO recipe you know (`-7 -L 4 --ppc default=.2`), writing `ana/<T>/mem###.nc` and `bkg/<T>/mem###.nc` |
| `post.cleanup` | `cleanup`, keyed off artifact existence rather than age. `keep_cycles` replaces `SAVE_RST_CYCLES`; `keep_every: P5D` replaces `SAVE_RST_REGEX="^......01.."`, because a regex over a date cannot express "every three days" and changes meaning if the date format does |
| (none) | `stats` (harvests `sacct` into `run/<T>/stats.json`), `verify` (declared, currently does nothing), `submit` (submits the next cycle) |
| `CUSTOM_STEPS_*` | no equivalent. Add a task to `graph/tasks.py` instead |

## DA modes

`DA_MODE`'s seven-way case statement is gone, replaced by orthogonal axes:

| `DA_MODE` | ACKBAR |
|---|---|
| `noda` | `solver: none`. Observation evaluation is a separate property, not part of the mode |
| `3dvar` | `variational` + `static` + `3d` |
| `3dfgat` | `variational` + `static` + `fgat` |
| `3denvar` | `variational` + `ensemble` + `3d` |
| `3dhyb` | `variational` + `hybrid` + `3d` |
| `4dhyb` | the `4d` column of the same table |
| `letkf` | `solver: letkf` |
| `DA_PERTURBATION_MODEL` | `ensemble.source`. `eda` is a source, not a mode |
| `DA_REGIONAL_ENABLED` | nothing. Domain is a first-class axis; the whole regional capability is in the domain layer |

Two asymmetries to know. `fgat` is variational only, and the graph refuses it on
an ensemble filter, because a filter's departures and its observation-space
perturbations come from one hofx over one set of member states and cannot
separate departure timing from covariance time. And the ensemble filter is split
internally into an observer (`soca_ensmeanandvariance.x`, then `soca_hofx.x` per
member, merged) and a solver reading `HX` from disk, which is why a 4D LETKF
costs the solver exactly what a 3D one costs it.

## Where output lands

```
$ACKBAR_OUTPUT_ROOT/<experiment>/
  cfg/                            resolved config, provenance.json, every cycle's job script
  ana/20150712T000000Z/mem000.nc  the analysis, compressed          kept forever
  bkg/20150712T000000Z/mem000.nc  the background, same names        kept forever
  obs_in/20150712T000000Z/        the archive bins each window read, joined
  obs_out/20150712T000000Z/       departures, summary.json, observers.json
  fcst/20150712T000000Z/F120/mem000.nc
  run/ledger.jsonl
  run/20150712T000000Z/log/ done/ stats.json      kept
  run/20150712T000000Z/rst/ ana/ slot/ fcst/      reaped by cleanup
```

Three differences from `ana/{ctrl,ens}` and `rst/<YYYYMMDDHH>/`:

**Directories are named for the valid time, ISO 8601 basic to the second, not
for the cycle.** `ana/<T>` and `bkg/<T>` are two readings of the same instant
with the same filename, so an increment is a subtraction. Cycle numbers survive
only in the interface, where a dependency or a heal is computed.

**Every member is `mem###`, and the control is `mem000`.** There is no `ctrl`
versus `ens` split anywhere. This is what removes the special case from every
ensemble loop, and the array index maps directly to a path.

**The top level is the retention policy.** Nothing above `run/` is ever deleted;
`run/` is what it took to get there and is reaped. That is your split, made
structural.

Scratch is a separate root, deleted by the task on success and kept on failure,
so a failed cycle leaves everything needed to debug it and a successful one
leaves nothing.

## Habits that are now errors

- **Do not edit an experiment directory's config.** `cfg/` is frozen at
  `create`. Change the source YAML and `create` a new experiment.
- **Do not expect a missing input to be generated.** Initial conditions, static
  B, forcing, boundary conditions and observations are offline products consumed
  read only. On a new machine `$ACKBAR_STATIC_ROOT` is empty and several
  `tools/` commands must run first, per domain, in order.
- **Do not put a machine path anywhere but `site/<host>.sh`.** That file is the
  only one allowed to name one. There is no `MACHINE` file, no `MODEL_SCRIPT`
  indirection, and no abstraction over Slurm.
- **Do not `scancel -u`.** Use `ackbar cancel`.
- **Do not assume skip-if-exists.** ACKBAR skips only when the committed
  artifact *and* its sentinel both exist, because a `TIMEOUT` during a restart
  write leaves a truncated one gigabyte file that exists.
- **Do not `scontrol requeue`.** Requeue poisons a dependency permanently, even
  if the job later succeeds. Healing always means fresh job ids.
- **Watch the observer list by hand across a comparison.** An observer whose file
  is absent is dropped and recorded, not failed. Two experiments differing in
  which observers ran are not comparable, and the comparison tooling scores only
  the platforms every arm has.

## When something fails

```bash
ackbar status osse25-3dvar            # grid of tasks by cycle, one glyph per cell
ackbar status osse25-3dvar --verbose  # which job id was cycle 7's writeback
ackbar heal   osse25-3dvar --dry-run  # the blast radius, and what would be cancelled
ackbar heal   osse25-3dvar
```

`status` reads the ledger for identity, `sacct` for outcome and `squeue` for
pending reasons, and holds nothing, so closing it does nothing. Glyphs are `.`
complete, `-` pending, `>` running, `?` stranded, `X` failed, blank unsubmitted;
the last line says `running`, `finished`, `stalled` or `broken`. `stalled` is
the state that means a killed submitter, which in soca-science was
indistinguishable from a completed run.

`heal` does five things: identify the failure, compute the transitive closure of
its dependents, `scancel` everything in the closure still holding a job id,
resubmit with fresh state-aware edges, and record the new attempt numbers. The
third step is not tidiness: unsatisfiable dependents pend forever rather than
being cancelled, so skipping it gives two jobs per task. Members that already
succeeded skip on their sentinels in about a second, which is why whole nodes
are resubmitted rather than individual array indices.

A heal fixes consequences, never causes. It prints the failures that look
genuine (`FAILED`, `TIMEOUT`, `OUT_OF_MEMORY`) and resubmits them anyway; if the
same thing fails twice, read the log.

Because `forecast(n) -> da(n+1)` is a real edge and only one cycle is ever in
flight, a failure stops the chain rather than producing cycles of garbage off a
bad background. That is deliberate and it is the opposite of soca-science's
behaviour, where a step that half worked could carry forward.

## What is not there yet

Three things to be honest with yourself about before planning work around
ACKBAR.

**`verify` does nothing.** It is in the graph, it writes a sentinel, and it
produces no product. A green `verify` row means the job ran.

**Comparing two experiments is not in the repository.** The scoring, plotting,
promotion and comparison scripts live in `tools/local/`, which is deliberately
untracked, and some committed experiment files reference them by name. If you
need to reproduce a published comparison from the repo alone, you currently
cannot.

**Observations are synthetic.** There is an OSSE generator and a domain culler,
and no downloaders, no ioda converters, no concatenation across a window, and no
superobbing or thinning. Your `scripts/obs/*.sh` and the per-platform observer
configs remain the most reusable thing either prior repo has; the observers have
been ported and revalidated, the ingest half has not. Nor has sea ice: the model
runs SIS2 and carries the ice restart through writeback untouched, but nothing
analyses or scores it.

None of these will stop a Gulf of Mexico OSSE. All three are between you and the
first real-observation experiment on a global domain.
