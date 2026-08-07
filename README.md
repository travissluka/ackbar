# ACKBAR

**A**ssimilation **C**ycling **K**it for **B**enchmarking **A**nd **R**esearch

A workflow for running cycling ocean data assimilation experiments with
[SOCA](https://github.com/JCSDA-internal/soca), driving MOM6-SIS2 as the forecast model.

The point is comparison. Experiments are defined as a short stack of configuration layers, so
two of them differ only in the thing being compared, and every experiment records the code that
produced it.

## What you can run

An experiment picks one value from each axis. Everything else follows.

| Axis | Values |
|---|---|
| Solver | `none` (free run), `variational`, `letkf` |
| Covariance | `static`, `ensemble`, `hybrid` |
| Window | `3d`, `fgat`, `4d` |
| Ensemble solver | LETKF, EAKF |
| Forecast model | `mom6sis2`, `persistence`, `stub` |
| Domain | `om_1deg`, `gom_25km`, `gom_12km`, `gom_8km`, `gom_4km` |
| Observers | `sst_noaa19`, `adt_3a` |

The named DA methods fall out of the first three: 3DVar is variational + static + `3d`, 3DEnVar
swaps the covariance, FGAT and 4DEnVar swap the window, a hybrid weights two covariances and
recentres the ensemble on the deterministic analysis each cycle. There is no mode flag.

Alongside the analysis, an experiment can run extended forecasts on a cadence, evaluate
observations against them, and reduce both to compressed per-cycle products that survive after
the model output is reaped.

## Installing

Submodules need **full history**, not a shallow clone, so that pinned commits stay fetchable
once upstream branches move on.

```bash
git clone https://github.com/travissluka/ackbar.git
cd ackbar
git submodule update --init pkg/mom6sis2

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

./build-model.sh     # MOM6-SIS2
./build-jedi.sh      # the JEDI bundle
```

Everything machine-specific (where spack-stack lives, which filesystems hold scratch and
output, partition and account, queue limits) is confined to `site/<hostname>.sh`. Porting ackbar
to a new machine is writing one of those, modelled on `site/rancor.sh`. Every command below
needs it loaded first:

```bash
source site/activate.sh
```

**Slurm is required.** It owns the dependency graph: jobs are submitted with `--dependency`
edges and left alone. There is no daemon and no second state database.

See [`docs/model-build.md`](docs/model-build.md) for the nested submodules, the input data
wiring, and the build itself.

## Your first experiment

```bash
source site/activate.sh
ackbar create tests/experiments/stub_letkf.yaml   # validate, freeze, emit
ackbar start  stub_letkf                          # submit cycle 1
ackbar status stub_letkf                          # watch it
```

That one uses the stub model, so it needs no MOM6, no JEDI and no input data, and it finishes
in a couple of minutes. `tests/experiments/tier3_gom.yaml` is the smallest complete example
with the real model, and `experiments/` holds the science runs.

## Defining an experiment

An experiment file is an `inherit:` list and the handful of keys that make this experiment this
experiment. Everything else comes from the layers.

```yaml
inherit:
  - domain/gom_25km       # grid, bathymetry, forcing, per-task resources
  - model/mom6sis2        # the forecast model
  - da/variational        # the solver, and its background error
  - obs/sst_noaa19        # one layer per observer
  - obs/adt_3a

experiment:
  name: gom-3dvar

cycle:
  start: '2015-01-05T00:00:00Z'   # must equal the initial condition's valid time
  length: PT24H
  count: 21

model:
  initial_condition: $(static_root)/ic/gom_25km/spinup/20150105T00
```

Layers live under `config/layers/<kind>/<name>.yaml` and are deep-merged in the order listed,
so a later layer overrides an earlier one and the experiment file overrides all of them. Lists
merge by an identifying key where the schema declares one (`observations` merges on
`obs space.name`, so a `da/letkf` layer can change one observer's localization without
restating the others) and otherwise replace wholesale. `$remove` deletes an inherited key.

Configuration resolves in a fixed order: **merge, then substitute, then validate.** Merging last
would stop a layer overriding a value another layer interpolated; validating before substitution
would check `$(ntasks)` rather than the integer it stands for.

Three substitution syntaxes, and each names who fills it and when:

| Syntax | Filled | By what |
|---|---|---|
| `$(lowercase)` | once, at `create` | the layer stack's `vars`, and the site |
| `{{lowercase}}` | per job | a closed set that `ackbar config symbols` prints |
| `$(UPPERCASE)` | per task | `ackbar/soca.py`, into a `config/soca/` document template |

Inspect any of it before running anything:

```bash
ackbar validate       experiments/gom-3dvar.yaml            # six checks, says which ran
ackbar validate       experiments/gom-3dvar.yaml --offline  # skip the three that touch disk
ackbar config resolve experiments/gom-3dvar.yaml --cycle 2 --member 3
ackbar config why     experiments/gom-3dvar.yaml 'vars.obs_land_mask_min'   # which layer set it
ackbar config symbols
ackbar graph          experiments/gom-3dvar.yaml --cycle 2
ackbar graph --dot    experiments/gom-3dvar.yaml --cycle 1 | dot -Tpng -o graph.png
```

`validate` runs before every `create`, so a bad path fails in seconds rather than eight hours
into a run.

### The pieces an experiment does not build

**Experiments never generate their own inputs.** Initial conditions, the static background
error, observations and forcing are produced by separate offline stages and consumed read-only,
so nothing is generated on the fly during cycle one. Per domain, once:

```bash
tools/soca-gridspec.sh  gom_25km      # SOCA's geometry. Also after a bundle bump
tools/soca-diffusion.sh gom_25km      # correlation lengths for the static B, and localization
tools/soca-dirac.sh     gom_25km      # check that calibration with a dirac through B
tools/coldstart-ic.sh   gom_25km 2015-01-04T12 12 hycom-smoke   # a first restart set
tools/ensemble-ic.sh    ...           # one restart set per ensemble member
tools/obs-archive-osse.py ...         # a synthetic observation archive covering a domain
```

`create` materializes the initial condition into the experiment's own cycle-0 restart location
as symlinks, which is what makes cycle 1 an ordinary cycle: `forecast(1)` finds its background
exactly where `forecast(50)` does, and nothing in the graph, the model, or healing carries a
notion of a first cycle.

## Running an experiment

An experiment is created once and then addressed by name. Creation validates it, freezes the
merged config and the layer files verbatim, records the ackbar commit that produced it, and
emits a batch script for every node of every cycle. After that nothing reads the layer tree
again, so editing a layer cannot change a run already in flight.

```bash
ackbar create experiments/gom-3dvar.yaml
ackbar start  gom-3dvar
ackbar start  gom-3dvar --dry-run     # show the edges, submit nothing
ackbar pause  gom-3dvar               # stop at the next cycle boundary
ackbar resume gom-3dvar               # clear the halt flag and re-arm
ackbar cancel gom-3dvar               # cancel everything still queued
```

There is no daemon. Cycle *n*'s graph contains a job that submits cycle *n+1*, gated `afterok`
on that cycle's forecast, so a failed cycle stops the chain rather than producing cycles of
garbage off a bad background. Cycles overlap: the only real cross-cycle dependency is
`forecast(n) -> analysis(n+1)`, so post-processing, statistics, extended forecasts and
verification run alongside the following cycle. Ensemble members are job array elements rather
than a serial loop, so a cycle's wallclock does not grow with the ensemble.

`ackbar run` and `ackbar submit` are what those job scripts call. Neither is meant to be typed.

## Watching it, and fixing it

Three commands cover a failure, and none of them needs `squeue` or `scancel` by hand.

```bash
ackbar status  gom-3dvar            # a grid of tasks by cycle, and what is broken
ackbar status  gom-3dvar --verbose  # which job id was cycle 7's writeback
ackbar heal    gom-3dvar --dry-run  # the blast radius and what would be cancelled
ackbar heal    gom-3dvar            # cancel the stranded closure, resubmit it
ackbar harvest gom-3dvar --cycle 7  # pull sacct into stats/7.json
```

`status` is read-only and holds nothing: closing it does nothing, because a view that has to
stay open for the workflow to advance is a view that stalls the experiment when an ssh session
drops. It joins four sources and needs all four. The ledger knows which job id was cycle 7's
writeback. `sacct` knows the outcome, and is all that is left once a job leaves the queue.
`squeue` knows the *reason*, which exists only while a job is queued and is the only place
`DependencyNeverSatisfied` is ever visible. The sentinel on disk outlives all three, because
`sacct` rows purge on a site retention and an experiment has to stay answerable after that.

`heal` identifies the failure, takes the transitive closure of its dependents from the
regenerated graph, **cancels every job in that closure that is still queued**, and resubmits
with fresh state-aware edges. The cancel is the step that is easy to skip and cannot be:
unsatisfiable dependents pend rather than die, so they are still holding job ids and claimed
working directories, and a successful replacement upstream does not release them. Whole nodes
are resubmitted rather than the failed members alone, so an `aftercorr` edge is never rebuilt
between arrays whose index sets disagree; members that already succeeded skip on their
sentinels in about a second.

A heal does not fix the cause. A genuine nonzero exit resubmitted unchanged fails the same way,
and `heal` says so and resubmits anyway rather than refusing.

Per-stage CPU and memory are harvested into the experiment directory, because HPCs kill jobs
for reasons unrelated to the science and the next thing anyone asks is what it was using.

## What an experiment produces

Two tiers, and the split is the retention policy. The top level holds what the experiment is
*for*, and nothing there is ever deleted. `run/` holds what it took to get there, and `cleanup`
reaps it on the schedule the experiment sets.

```
<output_root>/<experiment>/
  cfg/                                   frozen config, layers and job scripts
  ana/20150105T000000Z/mem000.nc         the analysis, compressed
  bkg/20150105T000000Z/mem000.nc         the background, compressed
  obs_out/20150105T000000Z/              departures, and a per-cycle summary
  fcst/20150105T000000Z/F120/mem000.nc   an extended forecast at that lead
  run/
    ledger.jsonl
    20150105T000000Z/
      log/  done/  stats.json            kept
      rst/  ana/  slot/  fcst/           reaped
```

Directories are named by the date a state is valid at, not by cycle number, so `ana/<T>` and
`bkg/<T>` are two readings of the same instant and an increment is a subtraction between two
files with the same name. Cycle numbers survive in the interface, where a scheduler dependency
needs them.

The kept products are float32 quantized to five significant digits and deflated, about a ninth
of the restart set they came from, which is what makes it affordable to keep every cycle of
every member of every experiment.

Two keys decide what survives:

```yaml
cleanup:
  keep_cycles: 1     # how many completed cycles behind the current one keep their model state
  keep_every: P5D    # pin a restart set this often, so a variant can branch from mid-run
```

`keep_cycles: 1` is the tightest correct answer, since cycle *n*'s forecast reads cycle *n-1*'s
restarts and nothing reads further back. `keep_every` is what makes a long experiment
branchable: `model.initial_condition` can name another experiment's `run/<date>/rst/mem000`.

## Documentation

| Document | Contents |
|---|---|
| [`docs/design.md`](docs/design.md) | How the workflow is meant to work. Execution model, configuration layering, DA mode decomposition, task graph, offline stages. |
| [`docs/analysis.md`](docs/analysis.md) | The analysis and writeback tasks: what each solver is handed, what comes back, how it reaches a restart, and what a missing ensemble member does. |
| [`docs/background-error.md`](docs/background-error.md) | The static B: what is calibrated offline, how, and how to check it with a dirac. |
| [`docs/domains.md`](docs/domains.md) | What each domain is, what a day of it costs, how one is added, and what is wrong with each. |
| [`docs/osse.md`](docs/osse.md) | The Gulf of Mexico OSSE: nature run, synthetic observations, the experiment matrix, verification. |
| [`docs/model-build.md`](docs/model-build.md) | Building MOM6-SIS2: repository ownership, branch choice, the submodule recipe and its traps, smoke test. |
| [`docs/model-data.md`](docs/model-data.md) | The `.datasets` mechanism and the MOM6-examples input data inventory. |
| [`docs/slurm.md`](docs/slurm.md) | The single-node Slurm used to develop against a real scheduler, and the two dependency profiles the workflow is tested under. |
| [`docs/testing.md`](docs/testing.md) | The test tiers, what each is for, and how to run them. |
| [`docs/build-order.md`](docs/build-order.md) | What gets built in what order and what each phase is tested against. |
| [`docs/prior-workflows.md`](docs/prior-workflows.md) | The two prior attempts, what is inherited from them, and the mistakes this design exists to avoid. |

## Layout

```
build-model.sh     build MOM6-SIS2
build-jedi.sh      build the JEDI bundle
config/layers/     configuration layers experiments inherit from
config/model/      files a model needs that are ackbar's rather than the case's
config/obs/        files the observers need, shared across observer layers
config/schema/     ackbar's own schema, which also declares how lists merge
config/soca/       one JEDI document template per SOCA application
config/static/     parameters for the offline per-domain stages
docs/              design and reference documentation
experiments/       science experiments
pkg/jedi/          the JEDI bundle: CMakeLists plus one submodule per repo
pkg/mom6sis2/      submodule: NOAA-GFDL/MOM6-examples, branch dev/gfdl
site/              one file per machine, the only place machine paths may appear
src/ackbar/        the workflow itself
tests/             the test suite and its experiment fixtures
tools/             the offline stages, and the domain import
```

A template under `config/soca/` holds the *shape* of a JEDI document and holds a value only
when nothing in Python reads it. Anything Python also reads stays a slot, because two spellings
of a filename field is a writeback that opens a name nothing wrote.

## Name

For Admiral Ackbar. Mon Calamari are an aquatic species, and he commands a fleet, which is the
right image for a system whose defining feature is that ensemble members sail independently.

The lineage is genuine: the original bash workflow already carried
`# "It's a trap!!" -Admiral Ackbar` above its abort handler, as a joke about the bash `trap`
builtin.
