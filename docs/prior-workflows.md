# Prior workflows: soca-science (v2) and socasci (v3)

Notes from reading `~/work/soca-science` and `~/work/soca-science-v3`. Both are read-only
references. This records what they do, what worked, and what must not be repeated.

The short version: **v2 is the only one that ever produced science, and its fatal structural
flaw is that everything ensemble-related is a serial `for` loop.** v3 has a better skeleton
but never grew an ensemble at all, so it is not a solution to v2's problem, only a different
starting point.

## v2: soca-science

Bash. Officially end of life, last commit 2024-08. Entry point
`scripts/workflow/cycle.sh` (639 lines), run from the experiment directory, which must
contain a **symlink** to `cycle.sh` (the symlink is how the script finds the repo root) and a
copied-and-edited `exp.config`.

### Structure

- `exp.config` is sourced with `set -a`, so every setting becomes an exported environment
  variable. That, plus a few dozen derived ones, is the entire interface between steps.
- `run_step <name> [suffix]` runs `subscripts/<name>.sh` in a subshell with
  `WORK_DIR=$SCRATCH_DIR/<cycle>/<step>` wiped and recreated, output to
  `logs/<cycle>/<step>`.
- Machine abstraction: `configs/machine/machine.<name>` sets modules, `$MPIRUN`, and
  `$WORKLOAD_MANAGER`. Sourced at both build and run time, distinguished by
  `SOCA_SCIENCE_RUNTIME=T`.
- Workload manager abstraction: `workload_manager/wm.{none,slurm}.sh`, three functions
  (`wm_init`, `wm_checktime`, `wm_submitjob`). One job runs cycles in a `while true` loop;
  after each cycle it compares remaining walltime against a running average cycle runtime
  (times 1.2) and resubmits itself if short. `cycle_status` in the experiment directory holds
  the next cycle date so the new job knows where to resume.
- `DA_MODE` (`prep`, `noda`, `3dvar`, `3dfgat`, `letkf`, `3dhyb`, `4dhyb`) expands into a set
  of `DA_*_ENABLED` flags at the top of `cycle.sh`; each step is then guarded by those flags.

### Step order

```
prep.forc -> prep.bkgrst -> prep.soca -> prep.bkgrst.ens -> prep.obs
  -> run.var | run.letkf | run.recenter.checkpoint | run.hofx
  -> run.fcst (ctrl, then each ensemble member)
  -> post.obs -> post.state -> post.cleanup
```

User hook points: `CUSTOM_STEPS_{PREPREP,PREP,RUN,POST}` are arrays of shell commands run
with the same environment as a subscript.

### The serialization problem

This is the lesson to carry forward. Every place that iterates over ensemble members does so
sequentially, one MPI launch after another, with the whole node allocation held the entire
time:

| Site | What loops serially |
|---|---|
| `cycle.sh:604` | `run.fcst` per member: N sequential MOM6 runs |
| `cycle.sh:542` | EDA: N sequential `soca_var.x` runs |
| `cycle.sh:478` | `prep.forc` per member |
| `prep.bkgrst.ens.sh` | per-member checkpoint after `soca_enspert.x` |
| `run.recenter.checkpoint.sh` | per-member checkpoint, one MPI launch each |

A 20-member LETKF cycle is 21 sequential model integrations plus 21 sequential checkpoints.
Cycle wallclock is linear in ensemble size and the allocation is mostly idle.

The only concurrency anywhere in the workflow is backgrounded shell jobs in two
post-processing paths: `post.obs.sh` (`ncks` per obs type, `& ... wait`) and `prep.obs.sh`
(`soca_preqc.py` / `soca_domaincheck.py` batched at `$(nproc)`). Both use the
`( cmd || touch fail ) &` + `wait` + `[[ -e fail ]] && exit 1` idiom, since `wait` alone does
not propagate failure. Neither touches the expensive parts.

The self-resubmission heuristic compounds this: the job is sized against a measured average
cycle time, which bakes in the serial cost rather than exposing it.

**Requirement for ACKBAR: member-level work must be expressible as independent units the
scheduler can place concurrently.** Whether they actually run at once is a resource decision
(on rancor, 8 cores means something like 4 members at 2 PEs, or oversubscription), not a
structural one.

### What v2 got right and is worth keeping

- **Idempotent steps.** Nearly every subscript opens with "does my output already exist? then
  `exit 0`". That property is what makes crash-and-resubmit survivable, and it should be
  designed in rather than bolted on.
- **Input resolution with fallbacks.** `prep.bkgrst` tries, in order: existing output, a
  `.zip` archive, several candidate directory layouts, then a CFSv2 coldstart. `prep.obs`
  does the same with per-type and per-platform overrides (`OBS_SRC`, `OBS_<ob>_SRC`,
  `OBS_<ob>_<plat>_SRC`), plus `%Y%m%d`-style date templating and a `#ob#` placeholder.
- **Observation configs per platform.** `configs/soca/obs/<ob>_<plat>.yaml`, about 25 of them,
  each a complete observer block with operator, error model, and filter chain. Only those with
  a present input file get spliced in. This inventory is the single most reusable asset in the
  repo.
- **Compression as a first-class post step.** `post.state.sh` is entirely NCO:
  `ncks -7 -L 4 --ppc default=.2` for states and increments, `nces`/`ncbo` for ensemble mean
  and spread. No python, no new dependency.
- **Retention policy.** `post.cleanup.sh` deletes restart cycles older than
  `SAVE_RST_CYCLES` with a regex escape hatch (`SAVE_RST_REGEX="^......01.."` keeps the first
  of each month).
- **Experiment comparison is first class.** `tools/obs_plot.sh` computes the overlapping date
  range across N experiments, merges per-platform binned stats, and drives `iodaplots` for
  single, all-experiment, and difference plots. `configs/iodaplots/` holds the binning and
  plotting configs.
- **Templated `input.nml` only.** `configs/model/mom6sis2/1deg/input.nml.sh` is a bash heredoc
  taking `FCST_START_TIME`, `FCST_LEN`, `FCST_RESTART`, `FCST_RST_OFST`. All other model
  config is symlinked unchanged.
- **The non-model checkpoint path.** `tools/regional/soca_domom6_action.py checkpoint` copies
  `RESTART_IN/MOM.res.nc`, then writes `Temp`, `Salt`, `u`, `v`, `ave_ssh` in place from the
  SOCA analysis using `mask2d`/`mask2du`/`mask2dv` from `soca_gridspec.nc`, clamping T to
  `[-1.9, 33]` and S to `[0.1, 38]`. Pure `netCDF4`, needs IO layout `1,1`. Since
  `soca_checkpoint_model.x` no longer exists, this is the path to build on.
- **Static B via diffusion.** `prep.soca.sh` runs `calc_scales.py` then
  `soca_error_covariance_toolbox.x` on `soca_diffusion_calibrate_hz.yaml` once, and on
  `soca_diffusion_calibrate_vt.yaml` **every cycle**.

### What v2 got wrong

- **Env vars as the only interface.** Roughly 90 exported variables. Every subscript
  re-declares an `envars=()` list and hand-validates it, about 20 lines of identical
  boilerplate per file. It still goes wrong: `prep.bkgrst.coldstart.sh` does `envars+=(...)`
  without initializing the array first, so it silently inherits the caller's list.
- **YAML by `sed`.** Configs are copied and then patched three ways:
  - token replacement (`__DA_VARIABLES__`, `__DOMAINS__`, `__FCST_HR__`, `__SEED__`, ...)
  - conditional lines: `!IF_APP_var`, `!IF_DA_3dhyb`, `!IF_EDA` prefixes. The matching prefix
    is stripped, all remaining `!IF_` become `#IF_` comments.
  - block injection: a fragment is built line by line, indented with `sed "s/^/    /g"`, then
    spliced over `__OBSERVATIONS__`, `__ENSEMBLE__`, `__PSEUDOMODEL_STATES__`,
    `__ENSEMBLE_4D__` with `sed $'/TOKEN/{r file\nd}'`.

  The indentation is hardcoded per injection site, and the injected fragments reference YAML
  anchors (`*ens_member`, `*obs_land_mask`, `*obs_distribution`) that must be defined in the
  parent file. `common.sh` itself carries the comment "who put this messy subsitution here??
  clean it up". Generate YAML from a data structure instead.
- **Idempotency was already decaying.** The "skip if already done" blocks in `run.var.sh` and
  `run.fcst.sh` are commented out, with notes that they broke under EDA. Half the property was
  gone by the end.
- **Leaky abstractions.** `concatenate_obs` in `prep.obs.sh` calls `srun -n $i` directly,
  bypassing `$MPIRUN`, so it only works under SLURM. `ln -s $MODEL_DATA_DIR/../soca/*` with
  "TODO use a proper path" appears in five scripts.
- **4D bolted onto a 3D design.** Restarts live in `f###` subdirectories per forecast hour; for
  a 3D window `convert3Dbkg` builds a symlink farm faking the slots so the yaml can address
  them uniformly.
- **Regional is a declared hack.** `DA_REGIONAL_ENABLED` "triggers the regional hack": symmetric
  restart conversion (`soca_dynsym2dyn.sh`), grid edge masking, obs domain checks, an
  `OBC_NUMBER_OF_SEGMENTS` rewrite of `MOM_input`.
- **R2D2.** A 115-line early-exit fork at the top of `prep.obs.sh`. Dead, do not carry forward.

## v3: socasci

Python package, `pip install -e .`, click CLI. Abandoned mid-development; the `TODO` file at
the end reads: real obs dates, save obs stats, ensemble coldstart.

### Structure

- `socasci create <exp_dir> <input.yaml>` resolves the user config and writes
  `<exp_dir>/cfg/exp_config.yaml`. `socasci run <exp_dir>` builds the suite and drives Rocoto.
  `socasci job <exp_dir> <group> <cycle>` is hidden and is what Rocoto actually invokes.
- **Suite / RuntimeGroup / Task.** `DefaultSuite` wires groups `cold_prep`, `coldstart`,
  `prep`, `da_init`, `da`, `forecast`, `forecast_ext`, `post` according to `do_*` booleans in
  the config. One group is one batch job is one Rocoto task; tasks inside a group run
  sequentially in-process under `<work_dir>/<YYYYMMDDHH>/<group>/<task>`.
- **Dependencies as data.** `Dependency(group, first_cycle_only, prev_cycle)`. The Rocoto
  writer translates those flags into the right XML: `prev_cycle` emits
  `<taskdep cycle_offset="-&CYCLE_PERIOD;">` wrapped in an `<or>` with
  `<not><cycleexistdep .../></not>` so the first cycle is exempt; `first_cycle_only` emits the
  mirror image. Writing that dependency logic once and generating it is the best idea in the
  repo, and it generalizes to any engine.
- **Two-pass config substitution.** `$(var)` is resolved at create time and baked into
  `exp_config.yaml`; `{{var}}` is resolved per cycle at job time. Resolution is a fixed-point
  loop over the config tree, and tokens resolve against the **nearest enclosing dict** that
  defines them (the resolver walks the parent chain in reverse), so settings can be
  overridden at any level. Per-cycle symbols supplied by the suite: `exp_dir`,
  `cycle_current`, `cycle_next`, `cycle_previous`, `window_length`, `window_start`.
- **`!include` and `!env` YAML tags.** `!include` resolves inside the same fixed-point loop, so
  the path itself can contain `$(...)`:
  `!include $(default_soca_config)/obs/adt/coperl4.yaml`.
- **Observation assembly.** `Task.generate_obs_yaml` walks the `observations` list, drops any
  whose `obs space.obsdatain.obsfile` does not exist (error instead if `_required: true`), and
  projects each surviving entry down to the sections that app needs, listed per app as
  `_obs_yaml_sections: [obs space, obs operator, obs error, obs filters]`. The result lands in
  `_valid_observations`, which the app yaml references as `'{{_valid_observations}}'`. This is
  v2's `process_OBSERVATIONS` done properly: a list comprehension over parsed YAML instead of
  `sed`.
- **Model staging split.** Files needing per-cycle values get `{{token}}` substitution,
  everything else is symlinked. `configs/momsis2_1deg/stage.yaml` declares that split as
  `link:` / `substitute:` lists, though `Mom6Sis2` hardcodes `subfiles = ['mom_input.nml']`
  and never reads it. The mechanism was designed and not wired up.
- **Flat dated file naming.** Forecast output is renamed to `ocn.<YYYYMMDDHH>.f###.nc` and
  `ice.<YYYYMMDDHH>.f###.nc` in a per-cycle directory, rather than v2's
  `rst/<date>/ctrl/f###/MOM.res.nc` tree.

### How far it actually got

Working end to end for a deterministic 3DVAR: coldstart, forecast cycling, gridgen, static B
init, hofx, var, checkpoint. Everything else is a stub or absent:

- `tasks/letkf.py` is `class Letkf(Task): pass`. `tasks/stage.py` likewise. `groups/prep.py`
  and `groups/post.py` are `pass`.
- **No ensemble dimension anywhere.** No ensemble group, no member iteration, no recenter, no
  perturbation. v3 therefore says nothing about the parallel-ensemble problem; it did not get
  far enough to hit it.
- Observation file paths are hardcoded literals inside `tasks/var.py` and `tasks/hofx.py`
  (`gdas_marine.s2s_v1.ob.P1D.insitu_profile_fnmoc.2019-01-01T00:00:00Z.nc4`), with a TODO to
  move them to the prep group that never happened.
- `Task.mpirun` is `subprocess.run(['mpirun', exe])` with a TODO to pass correct args: no PE
  count, no machine abstraction, no per-task log file.
- No forcing preparation, no compression, no obs statistics.
- Rocoto settings hardcoded in `rocoto.py`: `MAX_CYCLES=3`, `MAX_TRIES=2`,
  `SCHEDULER="slurm"`.
- `Task.run()` raises if its working directory already exists, so a group cannot resume: it
  must start clean. The exact opposite of v2's idempotency, and a step backwards for cycling.
- Rocoto is not installed on rancor; `test/exp.yaml` points `rocoto.bin_path` at a
  `~/work/rocoto` that does not exist.

### Stale against current SOCA

`configs/soca/` targets a 2022 SOCA and JEDI:

- `covariance model: SocaError` with a BUMP NICAS block, versus today's SABER diffusion.
- `soca_staticbinit.x` and `soca_checkpoint_model.x`, neither of which is built any more.
- Old variable naming: `sea_area_fraction@GeoVaLs` rather than `GeoVaLs/sea_area_fraction`.
- `window begin` / `window length` rather than `time window: {begin, length}`.
- Only two observation types configured (`adt/coperl4`, `insitu/profile`) against v2's ~25.

For SOCA configuration content, port from v2 and re-check against current SOCA. Take v3 for
structure only.

## Implications for ACKBAR

1. **Ensemble members are independent scheduler units.** Non-negotiable, and the single
   biggest reason not to inherit v2. Design it in from the first ensemble step, not after the
   deterministic path works.
2. **Steps must be resumable.** v2's "check for output, exit early" property, but declared
   once rather than reimplemented per script, and not allowed to decay the way it did.
3. **Generate YAML from data structures.** v3's include-plus-substitute-plus-filter pipeline,
   never v2's `sed` splicing.
4. **Dependencies as data, engine as a backend.** v3's `Dependency` flags are correct
   regardless of whether the backend ends up being Slurm directly, ecflow, or a local runner.
5. **Port the assets, not the code.** From v2: the ~25 per-platform observation configs, the
   `iodaplots` binning and plotting configs, the NCO compression recipes, the retention
   policy, the download scripts under `scripts/obs/` and `scripts/forc/`, and
   `soca_domom6_action.py` as the basis for analysis-to-restart.
6. **Drop on sight:** R2D2, the regional hack, the `MODEL_SCRIPT` and `MACHINE`
   indirection layers (one machine, one model here), and the 3D-to-4D symlink farm.

## Where v2's configuration ended up

The observer configs, the background error settings and the diffusion scales were ported from
v2 rather than reinvented. The configuration files themselves say what they are and what to
change; this is the provenance, kept here so that the question "why is this number this
number" has an answer without putting archaeology in front of every reader of a config file.

### The port of an observer config

`configs/soca/obs/*.yaml` became `config/layers/obs/*.yaml`. Four things changed, and each was
a case of a value living somewhere it could not be correct:

| v2 | ACKBAR | Why |
|---|---|---|
| `*obs_distribution`, `*obs_distribution_opt` | removed | YAML anchors defined in the *solver* file, so the same observer got RoundRobin under 3DVar and Halo under LETKF. That breaks the moment a cycle contains two applications, which a hybrid does; v2 patched around it with `sed` markers keyed on whether the LETKF was running solo or inside a `3dhyb`. It is a property of the application, so `ackbar/soca.py` sets it. |
| `*obs_land_mask` | a filter with `$(obs_land_mask_min)` | Same story: 0.9 under 3DVar, 0.5 under LETKF. Filter chains have no natural merge key, so they replace wholesale and the varying part has to be substituted rather than merged. |
| `!IF_APP_letkf obs localizations` | supplied by `da/letkf` | The original guarded this block with `!IF_APP_letkf` while `sst_noaa19` used `!IF_letkf`, so one of the two never fired. That bug is only possible because the mechanism was `sed`. |
| `__SEED__` | `{{seed}}` | Job time, and derived from experiment, cycle and member so that a heal reproduces the original ensemble. |

One spelling change is not cosmetic: ioda reads a distribution's parameters directly under
`distribution` rather than under an `options` key. v2's spelling was for an older ioda, and
against the pinned one the Halo distribution finds no `halo size` there and refuses to
construct.

### The background error

Two sources, used for different things. The pinned bundle's `soca/test/testinput/3dvar.yml`
supplies the *schema*, which blocks exist and where they sit, because it is CI-verified against
this exact SOCA and v2's config is not. v2 supplies the *science* where the two agree.

They do not agree everywhere, and the largest gap is the standard deviations. v2's came from
`BkgErrGODAS`, a linear variable change that no longer exists in SOCA: there is no `godas`
anywhere in the source tree and `LinearVariableChange/` holds only `Balance` and
`LinearModel2GeoVaLs`. Its replacement, the saber outer block `SOCAParametricOceanStdDev`, is
not a rename. v2 tuned `t_min`/`t_max`/`t_dz`/`t_efold` and `s_min`/`s_max`; the parametric
block takes an SST error field, defaults for unbalanced salinity and ssh, and a per-variable
minimum or fraction of background. Carrying v2's tuning across is a science decision rather
than a transcription, so `config/layers/da/variational.yaml` took the bundle's own defaults
for most of them. They are known to run and are not known to be right.

Two settings there are deliberate departures rather than inheritance:

- **`ocean_depth_min: 0`** against the bundle example's 1000. v2 had already started moving:
  `soca_3dhyb.yaml` and `soca_4dhyb.yaml` set 0 while the older `soca_3dvar.yaml` kept 1000.
  The reason is in the config file, and it is that the parameter deletes the background error
  rather than tapering it.
- **`ksshts.nlayers: 2`**, which is v2's rather than the bundle example's 10. The example is a
  25 level toy and v2 is a real configuration.
- **`ninner: 20`** against v2's 50, and `gradient norm reduction: 1.0e-10` against its 1e-3.
  v2's solves stopped on the target; ACKBAR's stop on the count.

The vertical correlation scales are calibrated once by default rather than every cycle as v2
did; `da/corr_vt_cycled` opts into v2's behaviour. What makes it optional is that the mixed
layer moves slowly compared to a DA cycle.

### The diffusion scales

`config/static/diffusion.yaml` is v2's `configs/soca/saber_init/soca_diffusion_scales_*.yaml`
and `tools/calc_scales.py`, with the horizontal numbers unchanged. Three departures:

- **`hz_ssh` rossby mult 1.5** rather than 2.0. The floor and the ceiling are still v2's.
- **No vertical `max`.** v2 capped it at 10 levels, which was a cost control rather than a
  statement about the ocean: the explicit scheme's iteration count grows with the square of the
  scale in levels. The implicit scheme removes the cost, so the ceiling has nothing left to
  protect.
- **Implicit rather than explicit** in the vertical, which is what makes the above free.

### The filter

`rtps: 0.95` in `config/layers/da/letkf.yaml` is v2's. `rossby mult: 1.5` is not: v2 used 1.0,
and the reason for widening it is in the config file. The bundle's LETKF example switches
`rtps`, `rtpp` and `mult` on at once because it is a unit test exercising three code paths, and
inheriting that would give an ensemble whose spread is set by three interacting knobs none of
which can be attributed afterwards.
