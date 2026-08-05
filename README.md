# ACKBAR

**A**ssimilation **C**ycling **K**it for **B**enchmarking **A**nd **R**esearch

A workflow for running cycling ocean data assimilation experiments with
[SOCA](https://github.com/JCSDA-internal/soca), driving MOM6-SIS2 as the forecast model.

Status: **early implementation.** The model and JEDI builds work. The configuration core
(layer merge, schema validation, substitution, and blame) is the first piece of workflow code;
nothing is submitted to a scheduler yet. See [`docs/build-order.md`](docs/build-order.md).

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
| [`docs/slurm.md`](docs/slurm.md) | The single-node Slurm used to develop against a real scheduler. |

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
tools/slurm/       local single-node Slurm configuration
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
.venv/bin/python -m pytest          # tiers 0 and 1, under a second
```

Anything that resolves a configuration needs the site layer, because that is where the scratch
and output roots come from:

```bash
source site/activate.sh
ackbar validate      tests/experiments/letkf_om1deg.yaml
ackbar config resolve tests/experiments/letkf_om1deg.yaml
ackbar config why    tests/experiments/letkf_om1deg.yaml 'vars.obs_land_mask_min'
```

Configuration resolves in a fixed order: **merge, then substitute, then validate.** Merging
last would stop a layer overriding a value another layer interpolated; validating before
substitution would check `$(ntasks)` rather than the integer it stands for. `$(...)` is
experiment time and is frozen once; `{{...}}` is job time and survives this pass untouched.

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
