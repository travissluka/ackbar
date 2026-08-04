# ACKBAR

**A**ssimilation **C**ycling **K**it for **B**enchmarking **A**nd **R**esearch

A workflow for running cycling ocean data assimilation experiments with
[SOCA](https://github.com/JCSDA-internal/soca), driving MOM6-SIS2 as the forecast model.

Status: **design stage.** The model side works; no workflow code is written yet.

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
- **Configuration is explicit layers**, deep-merged, with provenance recorded so any resolved
  value can be traced to the layer that set it. YAML is generated from data structures, never
  templated with `sed`.
- **Two solvers, not seven modes.** Variational and LETKF, with covariance (static, ensemble,
  hybrid) and window (3D, FGAT, 4D) carried by configuration rather than by mode dispatch.
- **Domain is a configuration axis**, not a flag. Global and regional are the same code path,
  with regional adding open boundary forcing, grid-edge masking, and domain-scoped observation
  culling as ordinary configured stages.
- **Built to be monitored and healed**, because HPCs kill jobs for reasons unrelated to the
  science. Per-stage CPU and memory are harvested from `sacct` into the experiment directory,
  and failed subgraphs can be regenerated and resubmitted automatically.

## Documentation

| Document | Contents |
|---|---|
| [`docs/design.md`](docs/design.md) | How the workflow is meant to work. Execution model, configuration layering, DA mode decomposition, task graph, offline stages, build order. |
| [`docs/prior-workflows.md`](docs/prior-workflows.md) | Review of the two prior attempts and the mistakes this design exists to avoid. |
| [`docs/model-build.md`](docs/model-build.md) | Building MOM6-SIS2: repository ownership, branch choice, the submodule recipe and its traps, smoke test. |
| [`docs/model-data.md`](docs/model-data.md) | The `.datasets` mechanism and the MOM6-examples input data inventory. |
| [`docs/slurm.md`](docs/slurm.md) | The single-node Slurm used to develop against a real scheduler. |

## Layout

```
build-model.sh     build MOM6-SIS2
docs/              design and reference documentation
mom6sis2/          submodule: NOAA-GFDL/MOM6-examples, branch dev/gfdl
tools/slurm/       local single-node Slurm configuration
```

## Getting the source

The model is a submodule and needs **full history**, not a shallow clone, so that pinned
commits stay fetchable once upstream branches move on.

```bash
git clone https://github.com/travissluka/ackbar.git
cd ackbar
git submodule update --init mom6sis2
```

See [`docs/model-build.md`](docs/model-build.md) for the nested submodules, the input data
wiring, and the build itself.

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
