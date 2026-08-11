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

The original development plan was to prove the 1-degree (`OM_1deg`) global config first and
build the DA cycling around it. It did not go that way: a 24 hour forecast there is slow
enough that iterating on it was the bottleneck, so the regional domains arrived early and
`gom_25km` became where everything is proved. Today `gom_25km` is the plumbing domain and the
whole of tier 3, `gom_12km` is where an answer that matters is computed, and `OM4_025`
(quarter degree) remains the target for real experiments. `om_1deg`'s only live use is in
tests that never run a model: the tier 0/1 graph and `ackbar validate` fixtures, plus the
tier 0 checks that read `config/layers/domain/om_1deg.yaml` directly. Every tier 3 fixture,
which is where a model does run, is `gom_25km`. See `docs/domains.md`.

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
| `~/work/jedi` | JEDI Skylab bundle, a separate workspace with its own `CLAUDE.md` and `claude/` doc set. ACKBAR does not read anything from it |
| `~/work/jedi/bundle/soca` | SOCA source, for reading. The copy ACKBAR builds is `pkg/jedi/soca` |
| `~/work/soca-science` | reference: old bash workflow (read-only reference) |
| `~/work/soca-science-v3` | reference: unfinished Python rewrite |
| `~/work/soca-science-v3.test` | its test experiment dir; has a 1deg `model_data/` tree |
| `~/work/ackbar/pkg/mom6sis2` | our clone of `NOAA-GFDL/MOM6-examples` (branch `dev/gfdl`), `.datasets` wired to `/data/mom6-datasets` |
| `~/work/ackbar/pkg/stochastic_physics` | `NOAA-PSL/stochastic_physics`, the pattern generator MOM6 ships only a stub of. Always compiled into `coupler_main`; see `docs/model-build.md` |
| `~/work/ackbar/pkg/jedi` | the vendored JEDI bundle, one submodule per repo. `build-jedi.sh` builds it |
| `~/work/ackbar/pkg/jedi/build/bin` | the `soca_*.x` ACKBAR runs. `graph/tasks.py` names this path and no other |
| `~/work/ackbar/tools/slurm` | the local Slurm install: config is the source of truth for `/etc/slurm`; see `docs/slurm.md` |
| `~/work/ackbar/.venv-data` | the venv the ocean-state and forcing fetchers run in, separate from the project venv and with `PYTHONPATH` cleared; see `docs/model-data.md` |
| `~/work/ackbar/experiments` | the committed experiment definitions, with their own `README.md` |
| `~/work/ackbar/site` | the site layer: `activate.sh` and `rancor.sh` own every machine-specific path, rank count and launcher |
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

Machine budget: 8 physical cores / 16 threads. `make -j16`, and `mpiexec -n 8` for a run
started by hand outside Slurm, which is what the offline `tools/` scripts do. Inside the
workflow MPI is launched by `$ACKBAR_LAUNCHER` from the site layer (`srun --mpi=pmi2` here),
never by a spelling written into a job.

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

**`src/ackbar` is an editable install, so a running experiment imports the working tree,
not a snapshot of it.** `cfg/` freezes an experiment's YAML at `create` time; the Python
is not frozen with it. Every task that starts after an edit picks the edit up, including
the moment between a new call site and the function it calls, which is a `NameError` in
a job rather than in a terminal. Finish an edit to `src/ackbar` before starting a run, or
accept that a mid-run edit can fail a cycle. `ackbar heal <experiment>` resubmits the
failed node once the cause is fixed; the failure is confined to that task, because the
graph stops at it rather than carrying a bad summary forward.

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

Under `ice_ocean_SIS2/`: `OM_1deg`, `OM4_025`, `OM4_05`, `OM4_033`,
`OM4_025.JRA`, plus small `Baltic*`/`SIS2*` cases useful for smoke tests.

Input data is not in the repo; each config's `INPUT/` is symlinks through a `.datasets`
link at the repo root. **`docs/model-data.md`** covers the whole story: how `.datasets`
works, how it is wired here, the full inventory of GFDL's FTP tarballs with what each is
for, and the SOCA-era data already sitting in `/data/OLD`. Read it before downloading
anything.

Which configurations resolve is a property of what has been downloaded, not of this file:
`docs/model-data.md` says how to check, and a broken `INPUT/` symlink is what an unresolved
one looks like.

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

Restart layout used by the *prior* workflows: `restart_input_dir='RESTART_IN'`,
`restart_output_dir='RESTART'`. ACKBAR keeps upstream's `INPUT`/`RESTART` instead, because
`coupler_main` hardcodes `INPUT/coupler.res` regardless of `restart_input_dir`, so moving the
input directory splits a restart set across two places. The long version is at the top of
`src/ackbar/mom6sis2.py`.

## SOCA (the DA)

Built by `./build-jedi.sh` from the vendored bundle in `pkg/jedi`. Executables land in
`pkg/jedi/build/bin/soca_*.x`, which is the only place ACKBAR looks: `SOCA_BIN` in
`src/ackbar/graph/tasks.py` is that path, relative to the checkout, so an experiment records
which build it ran against by recording the checkout. SOCA links MOM6 as a *library* from its own
`external/mom6/MOM6` submodule, which tracks **NOAA-EMC/MOM6**, not NOAA-GFDL. The
standalone forecast executable and SOCA's internal MOM6 are therefore two different MOM6
versions. Keeping them compatible (restart file layout, parameter names) is a standing
risk for this project.

Check what SOCA is checked out before trusting anything:
`git -C pkg/jedi/soca status`.

### Executables the workflow needs

`ls pkg/jedi/build/bin` is the inventory; `ls pkg/jedi/soca/src/mains` is what could be built.
Do not keep a list here, it goes stale silently and the answer is one command away.

**Two apps both prior workflows call do not exist in the pinned SOCA.**
`soca_checkpoint_model.x` has no `Checkpoint.cc` in `src/mains` and no CMake target, and
`soca_staticbinit.x` is not built either, though `src/mains/StaticBInit.cc` still exists. Both
old workflows call the checkpoint app to write the analysis back into MOM6 restarts, so that
path is closed rather than merely unfashionable: the writeback has to be a direct restart
write, modelled on `~/work/soca-science/tools/regional/soca_domom6_action.py checkpoint` (plus
`socaincr2mom6` if IAU is ever wanted).

**A third is gone and matters as much.** `BkgErrGODAS`, the linear variable change v2 built its
background error standard deviations with, is not in this SOCA: `LinearVariableChange/` holds
only `Balance` and `LinearModel2GeoVaLs`. Its replacement is the SABER outer block
`SOCAParametricOceanStdDev`, whose parameter tree is not a rename of the old one. So a ported
v2 analysis yaml fails on a config key, and the rule when porting is: science values from
soca-science, schema from `pkg/jedi/soca/test/testinput/`, which is CI-verified against this
exact bundle.

One trap in the departures, which is where the analysis is read from afterwards.
`oops::CostJo` saves `ombg` on the first outer loop and `oman` on the last, into whatever
`obsdataout` the obs space names, so an observer without that key writes nothing at all and
an analysis without a top-level `output:` writes `ombg` and no `oman`: `oops::Variational`
only runs the final cost evaluation when something asks for one. Either way post-processing
has nothing to read and the run looks healthy the whole way through. ACKBAR builds both keys
itself rather than leaving them to a copied config; `config/soca/var.yaml` carries the
comment.

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

## Where the build has got to

There is no status list here on purpose: a phase list in a file nobody re-reads is how a
project ends up documenting a state it left months ago. `docs/build-order.md` carries the
phases and the test tier each is verified at, and `git log --oneline` says which have landed.

## Design

**`docs/design.md`** is the reference for how the workflow is meant to work: Slurm as a hard
dependency owning the dependency graph, offline stages producing every experiment input,
layered configuration, the DA mode decomposition, cross-cycle overlap, resource accounting,
and monitoring and healing. **`docs/build-order.md`** carries the implementation phases, the
test tier each is verified at, and the spikes that must land before particular phases.
**`docs/prior-workflows.md`** records what the two prior attempts did and which of their
mistakes the design exists to avoid. **`docs/forcing.md`** covers atmospheric forcing: what
each source and each GEFS era can supply, why the archive is keyed by domain, the
de-averaging that is the one place a fetch can be quietly wrong, and how a source reaches
the model.

The rest of `docs/` is narrower and worth knowing exists rather than reading up front:
`analysis.md` (the analysis tasks and what each produces), `background-error.md` (the four
parts of B and which one is calibrated offline), `domains.md` (what each domain is and costs),
`ensemble-spread.md` (the three spread mechanisms and the offline archive each needs),
`forcing-reference-height.md` (the deferred `z_bot` question), `from-soca-science.md` (what
changes and what to unlearn, for someone who knows the old bash workflow), `model-build.md`
and `model-data.md` (the model and its input data), `observing-system.md` (what the OSSE archive
imitates and why), `osse.md` (the worked end-to-end study), `slurm.md`, and `testing.md` (the
tiers, and what each can and cannot catch). `README.md` is the outside view of the same
material.

The decisions those docs rest on, in one place, because they are the ones an agent is most
likely to relitigate by accident: Slurm is assumed rather than abstracted over, and rancor's
single-node install (`docs/slurm.md`) is the development target; observations are never
downloaded in-cycle, and spinup, static B, observations and forcing are all offline stages;
the cycle throttle is 1; the healer is a manual command, not a recurring job; config
provenance is layer replay rather than wrapped values; scratch and output are separate roots
named by the site layer; every member including the control is `mem###`; task completion is
temp-then-rename plus a sentinel, never skip-if-exists; cleanup keys off artifact existence
rather than job state; the whole config is schema-validated and every job's YAML is generated
up front before anything is submitted; the model is a config axis (`mom6sis2`, `persistence`,
`stub`); and a stub model plus fault injection came before the real one, because 8 cores
cannot demonstrate member parallelism with MOM6.

Four Slurm behaviors the design now handles explicitly, all easy to get wrong: unsatisfiable
dependents pend forever rather than being cancelled unless the site sets
`kill_invalid_depend`; requeue poisons dependencies permanently, so healing always means fresh
job ids; a requeued batch script reruns from its first line, so the submitter needs
`--no-requeue` plus an `O_EXCL` marker; and `afterok` edges cannot be attached to a job that
ended more than `MinJobAge` ago.

**`pkg/` is where everything ackbar builds lives**: `pkg/mom6sis2` for the forecast model,
`pkg/stochastic_physics` for the pattern generator compiled into it, and `pkg/jedi` for the
JEDI bundle, one submodule per repo. Nothing in ACKBAR reaches outside the checkout for an
executable, which is what makes "which build produced this experiment" answerable from the
experiment.

## Open decisions

- Ensemble geometry on rancor: 8 cores against an 8-PE model run means parallel members need
  fewer PEs each, or oversubscription.
- The background error numbers in `config/layers/da/variational.yaml`. The structure is right
  and the values are mostly the pinned bundle's defaults, because v2's tuning lived in
  `BkgErrGODAS` and does not map onto `SOCAParametricOceanStdDev` one for one. Sea surface
  temperature is the exception, derived for the domain by `tools/sst-bgerr.py`; see
  `docs/background-error.md`. A science call, and results should not be believed until it is
  made.

Closed, and recorded here because the reasoning is easy to reopen by mistake:

- **Writeback is a direct restart write.** Not a preference: the checkpoint app does not exist
  in this SOCA (see above). The contract is "produce the restart set the next forecast reads",
  so IAU stays a later alternate implementation rather than a second graph shape. On a regional
  domain the writeback must copy the background and overwrite variables in place, never let
  SOCA's MOM6 author the file, because it runs with `OBC_NUMBER_OF_SEGMENTS = 0` and would
  drop the open-boundary fields the forecast restarts from.
- **`srun` launches MPI here.** `site/rancor.sh` sets `ACKBAR_LAUNCHER="srun --mpi=pmi2"`, so
  job steps and per-task accounting match production. `docs/slurm.md` has the how.
- **Ensemble spread does not come from perturbed parameters.** A member given its own
  parameter values is a different model from every other member, so the ensemble covariance
  stops being the covariance of anything and the mean carries whatever bias the offsets
  produce. It was measured as well as argued: seventeen parameter groups swept five ways each,
  and all of them produce a fixed offset that does not grow, most of it sitting on the model's
  own divergence floor. The full measurement is `site/monitor/spread/report.html`. Three
  sources are implemented instead: `ensemble.stochastic` (oSPPT), per-member open boundaries
  and per-member atmospheric forcing, the last two through `ensemble.inputs`. Forcing is the
  largest of them by a wide margin and stochastic physics the smallest;
  `docs/ensemble-spread.md` is the reference for all three, and `docs/forcing.md` and
  `docs/domains.md` say how each archive is built.
- **The back-compat pins are dropped.** Every domain's `MOM_override` sets
  `ENABLE_BUGS_BY_DEFAULT = False`, so the model runs the corrected physics and no state spun
  up under the old defaults is reusable. `docs/model-build.md` and `docs/domains.md` have the
  reasoning and the flag count.
- **Truth and the experiments share the open boundary on purpose.** Giving truth its own is
  the obvious next proposal and it was built and declined: the difference between two products
  on the same boundary is dominated by a basin-wide sea level offset, and `ufo::ObsADT`
  subtracts the domain mean before forming departures, so that offset is invisible to every
  altimeter and shows up as a permanent barotropic pressure gradient under FLATHER instead.
  `docs/osse.md` and `docs/domains.md` carry the measurement.
- **Removing the boundary ensemble's basin-wide `zeta` does not work.** Same reasoning, and it
  was built, measured and reverted: interior spread did not fall, and each member's boundary
  anomaly correlates with its basin-mean sea level at -0.23, no relationship and the wrong
  sign. `tools/obc-lagged.py` and Domains in `docs/design.md` carry the full account.
- **The mass-field writeback is split by solver, not generalized.** The tempting fix, adding
  `sea_water_cell_thickness` to every solver's writeback, is wrong; `docs/osse.md` and
  `docs/design.md` say which solver needs what and why.

## Parallel agents and git

Several agents work this repo at once, each in its own worktree under `.claude/worktrees/`,
on its own branch. That is the intended shape. What follows is how their work reaches `main`,
because the failure modes are not obvious and the history has already been made unreadable
by all three of them at least once.

**`main` is fast-forward only.** `git config merge.ff only` is set locally. An agent rebases
its branch onto `main` before handing off, and the merge is then a fast-forward. The log gets
one entry per unit of work, no merge bubbles, and the shape of history stops depending on
which agent happened to land first. Do not reach for `--no-ff` to defeat this; if a merge will
not fast-forward, the branch is stale and wants a rebase.

**A merge never restates its branch's commit subject.** The prose belongs in the branch
commit. A merge that needs a message at all takes `Merge <topic>`, never a copy of the tip.
Reusing the subject is what puts every feature in `git log --oneline` twice, once as the
branch commit and once as the merge, and the reader cannot tell the pair from a real revert
and reland.

**A branch is merged when its agent has finished, not while it is still working.** Merging a
running agent lands a partial change under a subject that then reappears when the rest
arrives, and the intervening merge carries nothing a reader can act on.

**Merge and cleanup are one action**: fast-forward `main`, `git worktree remove`,
`git branch -d`, and delete the remote branch if it was pushed. A merged branch left behind
is indistinguishable from an unfinished one a week later.

**Branch names carry a topic slug**, `worktree-<topic>`. A name like
`worktree-agent-a2f2092c880882cb8` says nothing about what is in it once the session is gone.

Two hazards that follow from the worktrees rather than from git. Agents on separate worktrees
can edit the same file; a branch that has not rebased since the last merge is the one that
finds out by conflict, so rebase before continuing rather than before handing off. And
`src/ackbar` is an editable install shared by every worktree's runs (see Environment above),
so a fast-forward lands under any experiment that is mid-flight. The same sharing runs the
other way, and that direction is silent: the venv's editable install names the shared
checkout's `src/` by absolute path and no worktree has a venv of its own, so `ackbar` run
from a worktree imports the shared checkout's code and defaults to the shared checkout's
`config/layers/`. A worktree's own edits to either do not apply to a run launched from it,
and nothing warns. `--layers` can be pointed at the branch's copy, but a code change has no
such escape: verify one by merging it, not by running from the branch.

### Which kind of agent gets which work

The choice is not about the size of the task. It is about whether the work writes, and about
where the session doing the routing is standing.

**Read-only work goes to a subagent.** Searching, auditing, reviewing, answering a question
about the code: a subagent inherits its parent's working directory, needs no branch of its
own, and returns a conclusion rather than a commit. Nothing has to be merged afterwards,
which is what makes it the cheap option.

**Writing work that needs its own branch goes to a peer session.** A peer is a separate
Claude session, so it takes its own worktree and its own branch, and its work reaches `main`
by the fast-forward route above. A subagent inherits its parent's working directory, so one
spawned from a session pinned to the shared checkout is pinned there too and cannot write
either. The Agent tool advertises an `isolation: "worktree"` option that would give a
subagent a worktree of its own; that is untested here, so do not route writing work through
it without checking it first.

**A session driving experiments cannot write at all.** This is the case that forces the rule
rather than merely recommending it. `ackbar create` and `ackbar start` have to run from the
shared checkout, for the reason in the paragraph above, so a session running experiments
sends every edit it wants to a peer, however small. A one line README fix is a peer's job
under those conditions, and that is the process working rather than failing.

**So the routing session stays out of a worktree while it is driving.** Entering one to make
a quick edit takes the whole session, and its subagents, out of the checkout the experiments
need.

## Conventions

- Do not modify `~/work/jedi/bundle/*` from this project; SOCA changes belong in the JEDI
  workspace.
- Reference clones (`~/work/soca-science`, `~/work/soca-science-v3`) are read-only
  references. Copy from them, do not edit them.
- Experiment output on `/data`, never in the repo or `/home`.
