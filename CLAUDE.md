# CLAUDE.md - ackbar

Guidance for Claude Code in `~/work/ackbar`.

## Name

**ACKBAR: Assimilation Cycling Kit for Benchmarking And Research.**

Named for the Mon Calamari admiral: an aquatic species, and he commands a fleet, which is the
right image for a system whose defining feature is that ensemble members run as independent
concurrent units. The lineage is real, not invented: `scripts/workflow/cycle.sh` in the v1 bash
workflow already carried `# "It's a trap!!" -Admiral Ackbar` above its abort handler, a joke
about bash's `trap` builtin. Worth putting back.

The repository, working directory, and experiment output tree are all `ackbar`.

## Goal

Build a new ocean DA workflow for running cycling SOCA experiments (3DVAR / LETKF / hybrid)
on rancor, driving MOM6-SIS2 as the forecast model.

Two prior attempts exist and are studied here as references, not as code to inherit:

1. `~/work/soca-science` (clone of `JCSDA-internal/soca-science`) - the original bash
   workflow. Officially end-of-life; last touched mid-2024. Complete and battle-tested,
   but written against a SOCA that has since moved.
2. `~/work/soca-science-v3` - an unfinished Python/rocoto rewrite (`socasci` package).
   Good bones, never finished, targets a SOCA/MOM6 vintage from 2022.

Development plan: get the latest MOM6-SIS2 from GFDL compiling first, prove the 1-degree
(`OM_1deg`) global config runs, then build the DA cycling around it. `OM4_025` (quarter
degree) is the target for real experiments; `OM_1deg` is the development and test config.

**Regional domains at various resolutions are in scope**, alongside the two global configs,
and domain is a first-class configuration axis rather than a flag. Regional pulls in
symmetric-memory restart compatibility with SOCA's own MOM6, open boundary forcing from a
parent solution, grid-edge masking, domain-scoped observation culling, and domain-specific
observer configuration. See Domains in `docs/design.md`. v2's `DA_REGIONAL_ENABLED`, described
in its own config file as "the regional hack", is not the model to follow.

## Where things live

| Path | What |
|---|---|
| `~/work/ackbar` | this project (the new workflow) |
| `~/work/jedi` | JEDI Skylab bundle; has its own `CLAUDE.md` and `claude/` doc set |
| `~/work/jedi/bundle/soca` | SOCA source (model interface for MOM6/SIS2) |
| `~/work/jedi/build/bin` | built `soca_*.x` executables |
| `~/work/soca-science` | reference: old bash workflow (read-only reference) |
| `~/work/soca-science-v3` | reference: unfinished Python rewrite |
| `~/work/soca-science-v3.test` | its test experiment dir; has a 1deg `model_data/` tree |
| `~/work/ackbar/pkg/mom6sis2` | our clone of `NOAA-GFDL/MOM6-examples` (branch `dev/gfdl`), `.datasets` wired to `/data/mom6-datasets` |
| `~/work/ackbar/pkg/jedi` | planned: the vendored JEDI bundle, one submodule per repo. Not created yet |
| `~/work/ackbar/tools/slurm` | the local Slurm install: config is the source of truth for `/etc/slurm`; see `docs/slurm.md` |
| `/data/ackbar` | our experiment and test-run output |
| `~/work/mom6sis2` | old (2022) clone with a hand-rolled mkmf `build.sh`; reference only |
| `/data/mom6-datasets` | the `.datasets` tree for MOM6-examples; see `docs/model-data.md` |
| `/data` | experiment output goes here, not `/home` (check free space with `df -h /data`) |

Detailed per-repo JEDI architecture docs: `~/work/jedi/claude/` (see `soca.md` there).

## Environment

```bash
source ~/work/env.sh    # spack-stack: gcc, mpich, netcdf, python, ecflow, fms
```

Sets `JEDI_ROOT`, `OMP_NUM_THREADS=1`, ccache/mold/Ninja build accelerators.
`$SPACK_STACK_VER` reports the stack version. spack-stack provides an `fms` module and an
`ecflow` module; both are candidates to avoid building things ourselves.

Machine budget: 8 physical cores / 16 threads. `mpiexec -n 8` for MPI runs, `make -j16`.

### Batch scheduler

rancor runs a real single-node Slurm so the workflow can be developed against `sbatch`,
job arrays, `afterok` dependencies, and `sacct` polling before it ever sees an HPC.
**`docs/slurm.md`** is the reference: install/reconfigure procedure, cluster shape, the
portable command subset the workflow should limit itself to, the traps (`mpiexec`/hydra
needs `HYDRA_LAUNCHER=fork` inside an allocation), and how to switch to two fake nodes.

The essentials: `tools/slurm/` holds the config, `sudo tools/slurm/install.sh config svc`
applies a change (never edit `/etc/slurm` directly), partitions are `debug` (30 min) and
`compute` (8 h, default), and job outcome comes from `sacct`, never from a job's absence
from `squeue`.

## MOM6-SIS2 (the model)

`./build-model.sh` builds it. **`docs/model-build.md`** is the reference: repo/org
ownership (MOM6 root moved to `mom-ocean`, examples and SIS2 did not), why we are on
`dev/gfdl` rather than `main`, what changed in MOM6 between the 2024 and 2026 pins and how
that affects config we port from soca-science, the clone/submodule recipe and its traps,
and the `OM_1deg` smoke-test recipe.

The essentials: the build is autoconf now, not the mkmf recipe in the stale
`~/work/mom6sis2` clone; FMS comes from the `src/FMS2` submodule, not the spack-stack
module; and the executable is **`ice_ocean_SIS2/build/coupler_main`**, not `MOM6`.

### Configurations

Under `ice_ocean_SIS2/`: `OM_1deg` (development target), `OM4_025`, `OM4_05`, `OM4_033`,
`OM4_025.JRA`, plus small `Baltic*`/`SIS2*` cases useful for smoke tests.

Input data is not in the repo; each config's `INPUT/` is symlinks through a `.datasets`
link at the repo root. **`docs/model-data.md`** covers the whole story: how `.datasets`
works, how it is wired here, the full inventory of GFDL's FTP tarballs with what each is
for, and the SOCA-era data already sitting in `/data/OLD`. Read it before downloading
anything.

`OM_1deg` and `OM4_05` resolve fully today; `OM4_025` resolves except for its
`mask_table.*` files.

### SOCA-specific model config

Both prior workflows carry a copy of the GFDL config plus overrides. See
`~/work/soca-science-v3/configs/momsis2_1deg/` and
`~/work/soca-science/configs/model/mom6sis2/1deg/`. What the overrides do:

- `MOM_override`: Z* vertical coordinate (not hybrid), `DT_THERM=3600`,
  `RESTART_CHECKSUMS_REQUIRED=False`, `RESTART_CONTROL=-2`, single diag coordinate,
  cold-start IC read from `ic.nc`, and a block of `*_BUG = False` flags (we do not need
  bit reproducibility with old GFDL runs).
- `SIS_override`: the same bug-flag disabling.
- `input.nml` is templated per cycle (forecast length, current date, restart interval,
  whether to read a restart), not copied verbatim. The rest of the config files are
  symlinked in.

Restart layout used by the workflow: `restart_input_dir='RESTART_IN'`,
`restart_output_dir='RESTART'` (upstream default is `INPUT`/`RESTART`).

## SOCA (the DA)

Built as part of the JEDI bundle in `~/work/jedi`. Executables land in
`~/work/jedi/build/bin/soca_*.x`. SOCA links MOM6 as a *library* from its own
`external/mom6/MOM6` submodule, which tracks **NOAA-EMC/MOM6**, not NOAA-GFDL. The
standalone forecast executable and SOCA's internal MOM6 are therefore two different MOM6
versions. Keeping them compatible (restart file layout, parameter names) is a standing
risk for this project.

Check what SOCA branch is checked out before trusting anything:
`git -C ~/work/jedi/bundle/soca status`.

### Executables the workflow will need

Present today: `soca_gridgen.x`, `soca_var.x`, `soca_letkf.x`, `soca_hofx.x`,
`soca_hofx3d.x`, `soca_error_covariance_toolbox.x`, `soca_setcorscales.x`,
`soca_enspert.x`, `soca_ensrecenter.x`, `soca_ensmeanandvariance.x`, `soca_forecast.x`,
`soca_postproc.x`, `soca_addincrement.x`, `soca_convertstate.x`, `soca_hybridgain.x`.

**Gone since both prior workflows were written**: `soca_checkpoint_model.x` (and
`soca_staticbinit.x` is not built either, though `src/mains/StaticBInit.cc` still exists).
Both old workflows call `soca_checkpoint_model.x` to write the analysis back into MOM6
restarts. The replacement path needs to be decided; the old workflow's non-model
alternative was a pure-Python dump
(`~/work/soca-science/tools/regional/soca_domom6_action.py checkpoint`, plus
`socaincr2mom6` for IAU increments), which is probably the better model to follow anyway.

Static B is now built with `soca_error_covariance_toolbox.x` running SABER diffusion
calibration (`configs/soca/saber_init/soca_diffusion_calibrate_{hz,vt}.yaml` in the old
workflow), not the retired `staticbinit` app.

## What the old bash workflow (soca-science) did

Single job runs every step of a cycle sequentially; at the end of a cycle it checks
remaining walltime and resubmits itself if short. Entry point `scripts/workflow/cycle.sh`,
symlinked into the experiment directory (the symlink is load-bearing, it is how the script
finds the repo root). Experiment settings in a copied-and-edited `exp.config`.

Step order per cycle (`run_step <name>` calls into `scripts/workflow/subscripts/`):

```
prep.forc -> prep.bkgrst -> prep.soca -> prep.bkgrst.ens -> prep.obs
  -> run.var | run.letkf | run.recenter.checkpoint | run.hofx
  -> run.fcst (ctrl + each ensemble member)
  -> post.obs -> post.state -> post.cleanup
```

Modes (`DA_MODE`): `prep`, `noda`, `3dvar`, `3dfgat`, `letkf`, `3dhyb`, `4dhyb`.
Perturbation models for the ensemble: `none`, `eda`, `letkf`.

Experiment output layout worth keeping: `ana/{ctrl,ens}`, `bkg/`, `rst/`, `forc/`, `obs/`,
`obs_out/`, `static/` (grid and B files, made once), `logs/`, and a `cycle_status` file
holding the current cycle date so a resubmitted job knows where it left off.

Other reusable pieces: `scripts/obs/*.sh` (downloaders for ADT/RADS, in-situ FNMOC, SSS
JPL, ocean color, sea ice), `scripts/forc/forc_{gfs,gefs}.py` (atmospheric forcing),
`configs/soca/obs/*.yaml` (~25 per-platform observation configs), `configs/iodaplots/`.
R2D2 was optional and is dead; do not carry it forward.

## What the unfinished Python rewrite (socasci v3) did

`socasci` Python package, `pip install -e .`, CLI `socasci {create,run,job}`. Rocoto as the
workflow manager (`workflow/workflow_managers/rocoto.py`). Rocoto is **not** installed on
rancor; the test config points at a `~/work/rocoto` that does not exist.

Design worth stealing:

- **Suite / Group / Task** hierarchy. `DefaultSuite` (`workflow/default_suite.py`) wires
  groups: `prep`, `coldstart`, `da_init`, `da`, `forecast`, `forecast_ext`, `post`, with
  explicit dependency edges including `first_cycle_only` and `prev_cycle` flags. Each
  group is one batch job; tasks inside it run in sequence in a per-cycle working dir.
- **Layered YAML config** with two substitution passes: `$(var)` resolved at config-load
  time, `{{var}}` resolved per cycle. Predefined per-cycle symbols: `exp_dir`,
  `cycle_current`, `cycle_next`, `cycle_previous`, `window_length`, `window_start`.
- A custom `!include` YAML tag so per-observation and per-app YAML fragments compose into
  the experiment config (`test/exp.yaml` is the worked example).
- Per-task `stage.yaml` describing which model config files are symlinked vs templated.
- Observations with no input file present are dropped from the generated YAML unless
  marked `_required` (`Task.generate_obs_yaml`).

Its `TODO` when abandoned: real obs dates, saving obs stats, ensemble coldstart.

Its config references are stale in the ways described above (`soca_staticbinit.x`,
`soca_checkpoint_model.x`, 2022-era SOCA YAML schema).

## Status

Model side is up:

- `mom6sis2` cloned on `dev/gfdl` with the needed submodules, `.datasets` wired, builds
  clean to `ice_ocean_SIS2/build/coupler_main`.
- `OM_1deg` runs: a 12-hour cold start on 8 PEs from WOA13 completes and writes a full
  restart set. Built from either the `main` or the `dev/gfdl` MOM6 pin it gives identical
  `ocean.stats`, because the config carries back-compat pins for every answer-changing
  default that moved in between.
- `OM_1deg` and `OM4_05` input data resolves fully; `OM4_025` is short only its
  `mask_table.*` files (see `docs/model-data.md`).

Not started: anything SOCA-side or workflow-side.

## Design

**`docs/design.md`** is the reference for how the workflow is meant to work: Slurm as a hard
dependency owning the dependency graph, offline stages producing every experiment input,
layered configuration, the DA mode decomposition, cross-cycle overlap, resource accounting,
and monitoring and healing. **`docs/build-order.md`** carries the implementation phases, the
test tier each is verified at, and the spikes that must land before particular phases.
**`docs/prior-workflows.md`** records what the two prior attempts did and which of their
mistakes the design exists to avoid.

Settled since those docs were written: Slurm is assumed rather than abstracted over (rancor's
single-node install, `docs/slurm.md`, is the development target); observations are never
downloaded in-cycle and OSSE is the first obs source; spinup, static B, observations and
forcing are all offline stages.

Settled by the design review: the cycle throttle is 1; the healer is a manual command, not a
recurring job; config provenance is layer replay rather than wrapped values; scratch and output
are separate roots named by the site layer; every member including the control is `mem###`;
task completion is temp-then-rename plus a sentinel, never skip-if-exists; cleanup keys off
artifact existence rather than job state; the whole config is schema-validated and every job's
YAML is generated up front before anything is submitted; the model is a config axis
(`mom6sis2`, `persistence`, `stub`); a stub model plus fault injection is milestone 0, because
8 cores cannot demonstrate member parallelism with the real model.

Four Slurm behaviors the design now handles explicitly, all easy to get wrong: unsatisfiable
dependents pend forever rather than being cancelled unless the site sets
`kill_invalid_depend`; requeue poisons dependencies permanently, so healing always means fresh
job ids; a requeued batch script reruns from its first line, so the submitter needs
`--no-requeue` plus an `O_EXCL` marker; and `afterok` edges cannot be attached to a job that
ended more than `MinJobAge` ago.

**`pkg/` is where everything ackbar builds lives.** `pkg/mom6sis2` is there now; `pkg/jedi`,
the vendored bundle with one submodule per JEDI repo, is not created yet. Until it is,
`~/work/jedi` is what SOCA executables come from.

## Open decisions

- How to write the analysis back into MOM6 restarts now that the checkpoint app is gone.
  Direct restart write is first, behind a single writeback contract ("produce the restart set
  the next forecast reads") so IAU is later an alternate implementation rather than a second
  graph shape. Investigate what SOCA offers before writing anything; spike before milestone 3.
- What a solver does with a missing or diverged ensemble member: fail the cycle, run degraded,
  or replace from the mean. Different graph shapes and different science, so it is per
  experiment.
- Whether getting `srun` to launch MOM6 on rancor (a PMI plugin Slurm can talk to) is worth the
  effort. Decided yes; not done. Without it, job steps and per-task accounting differ between
  rancor and production.
- Whether the SOCA model config keeps MOM6's back-compat parameter pins
  (`EQN_OF_STATE = "WRIGHT"` and friends) or drops them for the corrected physics with
  `ENABLE_BUGS_BY_DEFAULT = False`. See `docs/model-build.md`. Decide before generating an
  initial condition, since dropping them invalidates any state spun up under the old physics.
- How much of the workflow lives in a Python package versus in the job scripts it emits.
- Ensemble geometry on rancor: 8 cores against an 8-PE model run means parallel members need
  fewer PEs each, or oversubscription.

## Conventions

- Do not modify `~/work/jedi/bundle/*` from this project; SOCA changes belong in the JEDI
  workspace.
- Reference clones (`~/work/soca-science`, `~/work/soca-science-v3`) are read-only
  references. Copy from them, do not edit them.
- Experiment output on `/data`, never in the repo or `/home`.
