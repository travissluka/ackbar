# ACKBAR

**A**ssimilation **C**ycling **K**it for **B**enchmarking **A**nd **R**esearch

A workflow for running cycling ocean data assimilation experiments with
[SOCA](https://github.com/JCSDA-internal/soca), driving MOM6-SIS2 as the forecast model.

The point is comparison. Experiments are defined as a short stack of configuration layers, so
two of them differ only in the thing being compared, and every experiment records the code that
produced it.

## Requirements

- **Slurm.** It owns the dependency graph: jobs carry `--dependency` edges and are then left
  alone. There is no daemon and no second state database, and there is no non-Slurm path. See
  [`docs/slurm.md`](docs/slurm.md), which also installs a single-node Slurm if you need one.
- **spack-stack**, for the compilers and the JEDI dependency chain.
- **JCSDA-internal access**, with a GitHub SSH key. The JEDI half of this (`oops`, `saber`,
  `ioda`, `ufo`, `vader`, `soca`) is private, so without it you can run the workflow's own tests
  and nothing else.
- Python 3.9 or newer.

Only one machine currently has a site file. If you are not on it, read **Setting up on a new
host** below before running anything.

## What you can run

| | |
|---|---|
| Solver | `da/none` (free run), `da/variational`, `da/letkf`, `da/eakf` |
| Covariance | `da/variational` alone is static; `+ da/hybrid` adds an ensemble term, `+ da/envar` makes it fully ensemble. Both also need an `ensemble:` block in the experiment file |
| Window | `solver.window.type`: `3d`, `fgat`, `4d` |
| Forecast model | `model/mom6sis2`, `model/persistence` |
| Domain | `domain/gom_{25,12,8,4}km`. `domain/om_1deg` exists for the graph fixtures and is not a domain to run |
| Observers | one layer per platform under `config/layers/obs/`; list as many as you fly |

The named DA methods fall out of the first three rather than from a mode flag: 3DVar is
variational with the static B in a `3d` window, 3DEnVar swaps the covariance, FGAT and 4DEnVar
swap the window, and a hybrid weights two covariances and recentres the ensemble on the
deterministic analysis each cycle.

Alongside the analysis, an experiment can run extended forecasts on a cadence, evaluate
observations against them, and reduce both to compressed per-cycle products that survive after
the model output is reaped.

## Installing

The site file comes first: `build-model.sh` and `build-jedi.sh` both source it, so on a machine
without one they fail before compiling anything.

```bash
git clone https://github.com/travissluka/ackbar.git
cd ackbar

# Full history, not a shallow clone, so pinned commits stay fetchable once
# upstream branches move on. The nested MOM6 and JEDI sets are in docs/.
git submodule update --init --recursive pkg/mom6sis2 pkg/jedi

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

cp site/rancor.sh site/$(hostname -s).sh   # then edit it: see the next section
source site/activate.sh

./build-model.sh     # MOM6-SIS2
./build-jedi.sh      # the JEDI bundle
```

`source site/activate.sh` exports the `ACKBAR_*` environment and nothing else. It does **not**
activate the venv, so commands below are spelled `.venv/bin/ackbar`; put `.venv/bin` on your
`PATH` if you would rather not.

[`docs/model-build.md`](docs/model-build.md) covers the nested submodules and the input data
wiring.

## Setting up on a new host

One file per machine under `site/`, and the only place a machine-specific path may appear.
`ACKBAR_SCRATCH_ROOT` and `ACKBAR_OUTPUT_ROOT` are the two required variables; roughly a dozen
others cover the launcher, the queue limits, the spack-stack environment and the data roots.
**[`site/README.md`](site/README.md) is the reference**: what each one does, and the four traps
that surface as a job behaving oddly rather than as a command that says no.

Work up in this order; each step needs strictly more of the machine than the last, so stop at
the first that fails.

```bash
.venv/bin/python -m pytest -q                                    # no site, no Slurm, no JEDI
.venv/bin/ackbar validate tests/experiments/stub_letkf.yaml --offline
tools/slurm/smoke-test.sh                                        # the scheduler behaviour relied on
.venv/bin/python -m pytest -q -m tier2                           # real Slurm, no JEDI, ~3 min
ACKBAR_TIER3=1 .venv/bin/python -m pytest -q -m tier3            # the lot, ~15 min
```

Tier 2 is the first thing that submits to Slurm, and exercises the whole graph without needing
either build. Tier 3 also needs both builds and the per-domain offline products below.

## Defining an experiment

An experiment file is an `inherit:` list and the handful of keys that make this experiment this
experiment. Everything else comes from the layers.

```yaml
inherit:
  - domain/gom_25km       # grid, bathymetry, forcing, per-task resources
  - model/mom6sis2        # the forecast model
  - da/variational        # the solver, and its background error
  - obs/adt_j2            # one layer per platform
  - obs/sst_metopb

experiment:
  name: osse25-3dvar

vars:
  # Layers interpolate this; every observer layer needs it.
  obs_dir: $(static_root)/obs/gom12-osse-2015-era5-gom_25km/2015

cycle:
  start: '2015-01-05T00:00:00Z'   # must equal the initial condition's valid time
  length: PT24H
  count: 21

model:
  initial_condition: $(static_root)/ic/gom_25km/spinup/20150105T00
```

An ensemble solver additionally needs an `ensemble:` block. `size`, `control` and `source`
decide its shape; `initial_condition` is what gives the members spread, and an ensemble filter
without it starts every member from the same state and has nothing to work with.
`tools/ensemble-ic.sh` builds one. A `fgat` or `4d` window needs `solver.window.type` and
`forecast.slots`. The authority on every key is
[`config/schema/experiment.yaml`](config/schema/experiment.yaml), which is commented.

`experiments/` holds the studies that have actually been run, with an
[index](experiments/README.md). `osse25-3dvar.yaml` is the nearest thing to the above and is
the one to copy.

### How layers combine

This is the mechanism worth understanding, because it is what lets two experiments differ by
exactly one thing.

Layers live under `config/layers/<kind>/<name>.yaml` and are **deep-merged in the order
listed**: a later layer wins over an earlier one, and the experiment file wins over all of them.
([`config/README.md`](config/README.md) says what else is under `config/` and who reads it.)
Lists replace wholesale, except where the schema declares an identifying key (`observations`
merges on `obs space.name`, so a layer can reach one observer without restating the others).

```yaml
# domain/common/gom.yaml           # domain/gom_4km.yaml, which inherits it
domain:                            domain:
  resources:                         resources:
    forecast:                          forecast:
      ntasks: 8                          time: '04:00:00'
      time: '00:20:00'                   mem: 32G
      mem: 8G

# merged: time and mem take the 4 km values, ntasks: 8 survives from the base,
# and every other task's resources come through untouched
```

A layer may inherit too, meaning something narrower than an experiment's list: not "build a
stack" but "I am a kind of that". `obs/adt_j2` inherits `obs/common/adt` because Jason-2 is an
altimeter; `da/hybrid` inherits `da/variational`. A `<kind>/common/` directory holds layers only
ever inherited, never listed, none of them a complete anything on its own.

A `$` prefix marks a key ACKBAR reads and JEDI never sees, which matters because most of an
observer is verbatim UFO configuration. `$remove` deletes an inherited key or list element,
`$inherit` names a shared observer body, `$required: true` makes a missing input file fail the
cycle rather than drop the observer, and `$localization` is the observation-space localization
an ensemble filter applies, which is rendered for the filters and dropped for everything else.

Configuration resolves in a fixed order: **merge, then substitute, then validate.** Three
substitution syntaxes, each naming who fills it and when:

| Syntax | Filled | By what |
|---|---|---|
| `$(lowercase)` | once, at `create` | the layer stack's `vars`, and the site |
| `{{lowercase}}` | per job | a closed set that `ackbar config symbols` prints |
| `$(UPPERCASE)` | per task | `src/ackbar/soca.py`, into a `config/soca/` document template |

One CLI rule worth knowing before the commands below: **`validate`, `create`, `graph` and
`config` take a path to an experiment file; everything else takes an experiment name.** Before
`create` there is nothing to name, and after it the layer tree is never read again.

Inspect any of it before running anything:

```bash
.venv/bin/ackbar validate       experiments/osse25-3dvar.yaml            # six checks, says which ran
.venv/bin/ackbar validate       experiments/osse25-3dvar.yaml --offline  # skip the three touching disk
.venv/bin/ackbar config resolve experiments/osse25-3dvar.yaml --cycle 2 --member 3
.venv/bin/ackbar config why     experiments/osse25-3dvar.yaml 'vars.ninner'
.venv/bin/ackbar graph --dot    experiments/osse25-3dvar.yaml --cycle 1 | dot -Tpng -o graph.png
```

### The pieces an experiment does not build

**Experiments never generate their own inputs.** Initial conditions, the static background
error, observations and forcing are offline products consumed read-only, so nothing is generated
on the fly during cycle one. On a new machine `$ACKBAR_STATIC_ROOT` is empty and all of these
must run first. Per domain, once:

```bash
tools/soca-gridspec.sh  gom_25km      # SOCA's geometry. Also after a bundle bump
tools/soca-diffusion.sh gom_25km      # correlation lengths for the static B, and localization
tools/soca-dirac.sh     gom_25km      # check that calibration with a dirac through B
tools/coldstart-ic.sh   gom_25km 2015-07-10T00 24 glorys-smoke  # a first restart set
tools/ensemble-ic.sh    gom_25km 20                             # one restart set per member
```

They depend on each other in that order: the diffusion stage reads the gridspec, and ensemble
initial conditions are drawn from the B the diffusion stage built. See
[`docs/domains.md`](docs/domains.md) and [`docs/background-error.md`](docs/background-error.md).

**Observations are not on that list, and the difference matters.** The five commands above are
one command each and are all a domain needs in order to *run*. An observation archive is
sampled from a nature run, so it comes after a spinup, a truth run and a promotion step, and
`tools/obs-archive-osse.py` is the last stage of that rather than a thing you invoke cold.
[`docs/osse.md`](docs/osse.md) is the recipe end to end and the `experiments/osse-*` files are
its stages in order. Read it before copying an experiment file, because every shipped
experiment names an archive that recipe produces.

An archive of real observations, or one borrowed from a wider domain, is culled to the domain
once with `tools/obs-cull-domain.py` before an experiment reads it. That is offline too, so
nothing stops an experiment naming an unculled archive, and two checks make that loud rather
than silent: `validate` refuses an experiment with no observations inside the domain at all,
and `post.obs` fails a cycle that read observations and assimilated none of them.

## Running an experiment

An experiment is created once and then addressed by name. Creation validates it, freezes the
resolved config with the commit that produced it, and emits every cycle's job scripts, after
which nothing reads the layer tree again: editing a layer cannot change a run already in flight.

```bash
.venv/bin/ackbar create experiments/osse25-3dvar.yaml
.venv/bin/ackbar start  osse25-3dvar
.venv/bin/ackbar start  osse25-3dvar --dry-run     # show the edges, submit nothing
.venv/bin/ackbar pause  osse25-3dvar               # stop at the next cycle boundary
.venv/bin/ackbar resume osse25-3dvar               # clear the halt flag and re-arm
.venv/bin/ackbar cancel osse25-3dvar               # cancel everything still queued
```

There is no daemon: cycle *n*'s graph contains a job that submits cycle *n+1*, gated `afterok`,
so a failed cycle stops the chain rather than producing cycles of garbage off a bad background.
`ackbar run` and `ackbar submit` are what the job scripts call; neither is meant to be typed.

## Watching it, and fixing it

```bash
.venv/bin/ackbar status  osse25-3dvar            # a grid of tasks by cycle, and what is broken
.venv/bin/ackbar status  osse25-3dvar --verbose  # which job id was cycle 7's writeback
.venv/bin/ackbar heal    osse25-3dvar --dry-run  # the blast radius and what would be cancelled
.venv/bin/ackbar heal    osse25-3dvar            # cancel the stranded closure, resubmit it
.venv/bin/ackbar harvest osse25-3dvar --cycle 7  # pull sacct into that cycle's stats.json
```

`status` is read-only and holds nothing, so closing it does nothing. `heal` cancels the
transitive closure of a failure's dependents and resubmits it with fresh edges; members that
already succeeded skip on their sentinels in about a second. A heal does not fix the cause, and
a genuine nonzero exit resubmitted unchanged fails the same way. Both are detailed in
[`docs/design.md`](docs/design.md).

## What an experiment produces

Two tiers, and the split is the retention policy. The top level holds what the experiment is
*for*, and nothing there is ever deleted. `run/` holds what it took to get there, and `cleanup`
reaps it on the schedule the experiment sets.

```
<output_root>/<experiment>/
  cfg/                                   resolved config, provenance, job scripts
  ana/20150105T000000Z/mem000.nc         the analysis, compressed
  bkg/20150105T000000Z/mem000.nc         the background, compressed
  corr_vt/20150105T000000Z/              the vertical correlation, if rebuilt per cycle
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
files of the same name. Kept products are float32, quantized to five significant digits and
deflated, which is what makes keeping every cycle of every member affordable. Two keys decide
what survives:

```yaml
cleanup:
  keep_cycles: 1     # how many completed cycles behind the current one keep their model state
  keep_every: P5D    # pin a restart set this often, so a variant can branch from mid-run
```

`keep_cycles: 1` is the tightest correct answer: cycle *n*'s forecast reads cycle *n-1*'s
restarts and nothing reads further back. `keep_every` is what makes a long run branchable, since
`model.initial_condition` can name another experiment's `run/<date>/rst/mem000`.

## Seeing whether it worked

`status` and `heal` are about jobs. The science is in two places.

**Departures**, per cycle, under `obs_out/<T>/`: one ioda file per observer carrying `ObsValue`,
`hofx<n>` and `EffectiveQC<n>`, plus a `summary.json` that `post.obs` writes with per-observer
counts and O-B and O-A statistics. The group names carry an outer-iteration index, so the lowest
is the background evaluation and the highest the analysis: `ObsValue - hofx<low>` is O-B and
`ObsValue - hofx<high>` is O-A.

**States**, under `ana/<T>/` and `bkg/<T>/`, on the model grid and compressed. Same instant, same
filename, so an increment is a subtraction.

Two things that are *not* there. The `verify` task is in the graph and does nothing: it is
declared, writes a deferred sentinel, and produces no product, so a green `verify` row in
`ackbar status` means only that the job ran. And comparing two experiments is `tools/local/`,
which is not in the repository (see the note at the end of this file).

## Where to read next

[`docs/design.md`](docs/design.md) is the reference for how the workflow works and why: the
execution model, configuration layering, the task graph, healing, and the offline stages.
[`docs/domains.md`](docs/domains.md) says what each domain is and what a day of it costs.
[`docs/testing.md`](docs/testing.md) explains the test tiers.
[`docs/ensemble-spread.md`](docs/ensemble-spread.md) covers the three ways an ensemble gets its
spread, the YAML for each, and the offline archive each needs.
[`docs/osse.md`](docs/osse.md) is a worked end-to-end study, and
[`docs/model-build.md`](docs/model-build.md) is where to go when a build fails.
[`docs/background-error.md`](docs/background-error.md) describes the static B and which part of
it is calibrated offline, [`docs/analysis.md`](docs/analysis.md) the analysis tasks and what
each writes, [`docs/forcing.md`](docs/forcing.md) the atmospheric forcing archives, and
[`docs/observing-system.md`](docs/observing-system.md) what the OSSE observing system imitates.
`ackbar --help` and `ackbar <command> --help` are current by construction.

`tools/local/` is not in the repository. It holds personal plotting and monitoring, and the
docs occasionally reference it.

## Name

Admiral Ackbar commanded the fleet at Endor and is remembered for one line about a trap. A
workflow for detecting when your experiment is one seemed close enough.
