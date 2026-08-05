# ACKBAR

**A**ssimilation **C**ycling **K**it for **B**enchmarking **A**nd **R**esearch

A workflow for running cycling ocean data assimilation experiments with
[SOCA](https://github.com/JCSDA-internal/soca), driving MOM6-SIS2 as the forecast model.

Status: **early implementation.** The model and JEDI builds work. The configuration core (layer
merge, schema validation, substitution, blame), the task graph (generation, the job-time symbol
set, `ackbar validate`) and the workflow engine (job emission, submission, the ledger,
daemon-free cycling) are in, and cycle end to end against a real Slurm with a stub model. No
science runs through it yet: the forecast, analysis and observation tasks arrive with their
phases. See [`docs/build-order.md`](docs/build-order.md).

## What it is for

Running and comparing cycling marine DA experiments: 3DVar, LETKF, ensemble and hybrid
EnVar, over 3D and 4D windows, on global domains at 1 degree and quarter degree and on
regional domains at various resolutions. The comparison is the point, so experiments are meant
to be cheap to define, reproducible, and directly comparable to one another.

## Design in one page

- **Slurm is a hard dependency**, not something abstracted over. It owns the dependency graph:
  jobs are submitted with `--dependency` edges and left alone, with no daemon and no second
  state database.
- **Ensemble members are independent scheduler units**, submitted as job arrays. The previous
  workflow ran them in serial `for` loops, which made cycle wallclock linear in ensemble size
  while the allocation sat idle. This is the single biggest reason this repository exists.
- **Cycles overlap.** The only real cross-cycle dependency is `forecast(n) -> analysis(n+1)`.
  Post-processing, statistics, extended forecasts and verification are leaves with no
  successor, so they run alongside the following cycle.
- **Everything an experiment needs exists before the experiment starts.** Initial conditions,
  static background error, observations and forcing are produced by separate offline stages and
  consumed read-only. Nothing is generated on the fly during cycle one.
- **Configuration is explicit layers**, deep-merged, validated against a schema, and fully
  resolved before anything is submitted. Every job's YAML is generated and checked up front, so
  a bad path fails in seconds rather than eight hours into a run. YAML is generated from data
  structures, never templated with `sed`.
- **Two solvers, not seven modes.** Variational and LETKF, with covariance (static, ensemble,
  hybrid) and window (3D, FGAT, 4D) carried by configuration rather than by mode dispatch. The
  forecast model is a configuration axis too: MOM6-SIS2, persistence, or a stub that exercises
  the workflow at no model cost.
- **Domain is a configuration axis**, not a flag. Regional is a set of ordinary configured
  stages (open boundary forcing, grid-edge masking, domain-scoped observation culling) rather
  than a special mode, though it also carries a build-level constraint on how SOCA is compiled.
- **Everything ackbar runs is pinned by ackbar.** The JEDI bundle and the forecast model are
  submodules under `pkg/`, and each experiment records the exact commits and binaries that
  produced it. Comparisons are only meaningful if the code is accounted for.
- **Built to be monitored and healed**, because HPCs kill jobs for reasons unrelated to the
  science. Per-stage CPU and memory are harvested into the experiment directory, and a failed
  subgraph can be regenerated and resubmitted.

## Documentation

| Document | Contents |
|---|---|
| [`docs/design.md`](docs/design.md) | How the workflow is meant to work. Execution model, configuration layering, DA mode decomposition, task graph, offline stages. |
| [`docs/build-order.md`](docs/build-order.md) | What gets built in what order, what each phase is tested against, and the spikes that must land first. |
| [`docs/prior-workflows.md`](docs/prior-workflows.md) | Review of the two prior attempts and the mistakes this design exists to avoid. |
| [`docs/model-build.md`](docs/model-build.md) | Building MOM6-SIS2: repository ownership, branch choice, the submodule recipe and its traps, smoke test. |
| [`docs/model-data.md`](docs/model-data.md) | The `.datasets` mechanism and the MOM6-examples input data inventory. |
| [`docs/slurm.md`](docs/slurm.md) | The single-node Slurm used to develop against a real scheduler, and the two dependency profiles the workflow is tested under. |

## Layout

```
build-model.sh     build MOM6-SIS2
build-jedi.sh      build the JEDI bundle
config/layers/     configuration layers experiments inherit from
config/schema/     ackbar's own schema, which also declares how lists merge
docs/              design and reference documentation
pkg/jedi/          the JEDI bundle: CMakeLists plus one submodule per repo
pkg/mom6sis2/      submodule: NOAA-GFDL/MOM6-examples, branch dev/gfdl
site/              one file per machine, the only place machine paths may appear
src/ackbar/        the workflow itself
tests/             tiers 0 and 1: no scheduler, no JEDI, no model
tests/goldens/     task graphs, pinned per configuration shape
tests/test_tier2.py  tier 2: the workflow end to end on a real Slurm
tools/slurm/       local single-node Slurm configuration and its two profiles
```

Everything machine-specific (where spack-stack lives, which filesystems hold scratch and
output, partition and account, queue limits) is confined to `site/<hostname>.sh`. Porting
ackbar to a new machine is writing one of those, modelled on `site/rancor.sh`.

## Getting the source

Submodules need **full history**, not a shallow clone, so that pinned commits stay fetchable
once upstream branches move on.

```bash
git clone https://github.com/travissluka/ackbar.git
cd ackbar
git submodule update --init pkg/mom6sis2
```

See [`docs/model-build.md`](docs/model-build.md) for the nested submodules, the input data
wiring, and the build itself.

## Working on the workflow

The workflow is a Python package. It needs no scheduler, no JEDI and no model to develop
against, which is the point of the phase ordering.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest          # tiers 0 and 1, a couple of seconds
```

Tier 2 runs the whole workflow against a real Slurm with a stub model, and is skipped unless
`sbatch` exists and the site is activated. It takes a few minutes, and it is the only cheap
test that exercises arrays, dependency edges and failure recovery.

```bash
source site/activate.sh
.venv/bin/python -m pytest tests/test_tier2.py                     # about 3 minutes
ACKBAR_TIER2_FAST=1 .venv/bin/python -m pytest tests/test_tier2.py # about 1, drops requeue
```

Most of that three minutes is one thing: Slurm defers a requeued job about two minutes before
it is eligible again. `ACKBAR_TIER2_FAST=1` skips the requeue case for iterating, and is the
wrong thing to run before a commit, since requeue is the one fault the scheduler inflicts on
its own without being asked.

Anything that resolves a configuration needs the site layer, because that is where the scratch
and output roots come from:

```bash
source site/activate.sh
ackbar validate --offline tests/experiments/letkf_om1deg.yaml
ackbar graph              tests/experiments/letkf_om1deg.yaml --cycle 2
ackbar graph --dot        tests/experiments/letkf_om1deg.yaml --cycle 1 | dot -Tpng -o graph.png
ackbar config resolve     tests/experiments/letkf_om1deg.yaml --cycle 2 --member 3
ackbar config why         tests/experiments/letkf_om1deg.yaml 'vars.obs_land_mask_min'
ackbar config symbols
```

## Running an experiment

An experiment is created once and then addressed by name. Creation validates it, freezes the
merged config and the layer files verbatim, records what produced it, and emits a batch script
for every node of every cycle. After that nothing reads the layer tree again, so editing a
layer cannot change a run already in flight.

```bash
source site/activate.sh
ackbar create tests/experiments/stub_letkf.yaml   # validate, freeze, emit
ackbar start  stub_letkf                          # submit cycle 1
ackbar start  stub_letkf --dry-run                # show the edges, submit nothing
ackbar pause  stub_letkf                          # stop at the next cycle boundary
ackbar resume stub_letkf                          # clear the halt flag and re-arm
ackbar cancel stub_letkf                          # cancel everything still queued
```

There is no daemon. Cycle *n*'s graph contains a job that submits cycle *n+1*, gated `afterok`
on that cycle's forecast, so a failed cycle stops the chain rather than producing cycles of
garbage off a bad background. `ackbar run` is what those job scripts call; it is not meant to
be typed.

`tests/experiments/stub_letkf.yaml` is the workflow test case: 20 members, 3 cycles, one core
and a few seconds each, no science. `model.stub.fail` injects faults at named jobs, so a
failure is reproducible configuration rather than an afternoon nobody can repeat:

```yaml
model:
  stub:
    seconds: 3
    fail:
      exit_nonzero: ['2.forecast.7']    # <cycle>.<task>[.<member>], each field a glob
      overrun_time: ['*.da.*']
```

Configuration resolves in a fixed order: **merge, then substitute, then validate.** Merging
last would stop a layer overriding a value another layer interpolated; validating before
substitution would check `$(ntasks)` rather than the integer it stands for. `$(...)` is
experiment time and is frozen once; `{{...}}` is job time, survives that pass untouched, and
comes from a closed set that `ackbar config symbols` prints.

`validate` runs six steps and says which ones it ran. `--offline` skips the three that need the
filesystem or the site's queue limits, which is what the test tiers use and what is useful on a
machine where the input data is not staged yet. Without it the fixture experiments fail step 3,
correctly: they reference an observation archive that does not exist here.

The graph goldens under `tests/goldens/` are one line per node and per edge, so a diff is
readable. Regenerate them with `ACKBAR_UPDATE_GOLDENS=1 python -m pytest tests/test_graph.py`,
and read the diff rather than accepting it.

## Prior art

Two earlier attempts inform this one and are studied rather than inherited:

- `JCSDA-internal/soca-science`, the original bash workflow. Complete and scientifically
  proven, end of life since 2024.
- An unfinished Python and Rocoto rewrite, which had a better skeleton but never grew an
  ensemble.

[`docs/prior-workflows.md`](docs/prior-workflows.md) records what each did, what is worth
keeping, and what must not be repeated.

## Name

For Admiral Ackbar. Mon Calamari are an aquatic species, and he commands a fleet, which is the
right image for a system whose defining feature is that ensemble members sail independently.

The lineage is genuine: the original bash workflow already carried
`# "It's a trap!!" -Admiral Ackbar` above its abort handler, as a joke about the bash `trap`
builtin.
