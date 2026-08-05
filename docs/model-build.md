# Building MOM6-SIS2

Our clone: `~/work/ackbar/pkg/mom6sis2`, branch **`dev/gfdl`**, tracked as a git submodule of the
`ackbar` repo. Build driver: `~/work/ackbar/build-model.sh`.

Keep this clone and its nested submodules at **full history**, not `--depth 1`. A shallow
clone only holds commits near a branch tip, so once `dev/gfdl` (or any nested submodule's
branch) moves on, the commit we pinned is no longer fetchable and the checkout breaks. Full
history costs about 250 MB across all twelve repos, which is nothing next to the datasets.

Check with `git -C pkg/mom6sis2 rev-parse --is-shallow-repository`; repair with
`git -C <repo> fetch --unshallow`.

## Which GitHub organization owns what

MOM6 moved out from under NOAA-GFDL, but only partly, so both orgs are in play:

| Repo | Home | Notes |
|---|---|---|
| MOM6 | **`mom-ocean/MOM6`** | The root repo, community supported, single branch `main`. `NOAA-GFDL/MOM6` is a *fork* of it (`parent` = `mom-ocean/MOM6`). The two `main` branches are literally the same commit; the fork just mirrors it. GFDL's own line is the `dev/gfdl` branch, which runs ahead of `main` and flows back via periodic `gfdl-to-main-*` PRs. |
| MOM6-examples | `NOAA-GFDL/MOM6-examples` | Not a fork; `mom-ocean/MOM6-examples` does not exist. Default branch `dev/gfdl`. The only home of the coupled configs, so it is what we clone. |
| SIS2 | `NOAA-GFDL/SIS2` | Not a fork, no `mom-ocean` counterpart. |
| mkmf | `mom-ocean/mkmf` | Moved from `NOAA-GFDL/mkmf`. |
| GSW-Fortran, CVMix-src | `mom-ocean/*` | Nested submodules of MOM6, both moved. |
| FMS | `NOAA-GFDL/FMS` | `mom-ocean/FMS` exists but is a MOM5-specific fork, not what MOM6-examples uses. |

Choosing an org changes nothing about the code we build: MOM6-examples pins `src/MOM6` at
`NOAA-GFDL/MOM6` in its own `.gitmodules` on both branches. The org matters only for
knowing where upstream MOM6 issues and PRs are discussed, which is `mom-ocean/MOM6`.

## Branch choice: why dev/gfdl

We are on `dev/gfdl`, the repo's own default branch.

`main` is a trap here. MOM6-examples' `main` has not moved since 2024-07 and therefore pins
a **2024 MOM6**, while `dev/gfdl` pins a current one. SOCA links its own MOM6 from
`NOAA-EMC/MOM6`, which tracks a recent vintage, so building the forecast model off `main`
would maximize the version skew against SOCA rather than minimize it. That skew is exactly
the risk that matters for restart-file interop between the forecast model and the DA.

Check where things stand with:

```bash
git -C ~/work/ackbar/pkg/mom6sis2 log -1 --date=short --format='%h %ad %s'
git -C ~/work/jedi/bundle/soca/external/mom6/MOM6 log -1 --date=short --format='%h %ad %s'
```

### What changed in MOM6 between the two pins

Recorded because it shapes how we write model config, not as a changelog. Roughly 870
commits over ~2 years. The parts with teeth:

- **Default parameter changes, answer-changing.** `EQN_OF_STATE` default moved `WRIGHT` to
  `WRIGHT_FULL` (the old default was buggy), along with 14 other defaults from consortium
  decisions in 2024, and a further 6 in June 2025 (`VISC_REM_BUG` and `FRICTWORK_BUG` now
  default false, `MASS_WEIGHT_IN_PRESSURE_GRADIENT_TOP` and `DRAG_DIFFUSIVITY_ANSWER_DATE`
  now inherit from related parameters). Eight `*_ANSWER_DATE` parameters now derive from
  `DEFAULT_ANSWER_DATE`.
- **`ENABLE_BUGS_BY_DEFAULT`**, a new master switch that sets the defaults for 16
  bug-retention flags at once. It defaults True so existing answers are preserved; upstream
  says setting it false "is probably the right choice for new runs". This supersedes
  soca-science's hand-maintained list of `#override *_BUG = False` lines, which should
  collapse into this one setting when we port the SOCA overrides.
- **Seven parameters obsoleted** (`BETTER_BOUND_KH`, `BETTER_BOUND_AH`,
  `USE_DIABATIC_TIME_BUG`, `FIX_UNSPLIT_DT_VISC_BUG`, `CFL_BASED_TRUNCATIONS`,
  `KD_BACKGROUND_VIA_KDML_BUG`), plus `INTERNAL_TIDE_CORNER_ADVECT`. Referencing them is a
  **fatal error**, not a warning. Config inherited from the old workflow may refuse to
  start for this reason alone.
- **Restart and I/O rework**: `MOM_restart.F90` and `MOM_io.F90` each grew by ~550 lines,
  and EMC's "flexible restart" work merged. This is the code path SOCA's analysis-to-restart
  interop depends on, and the strongest argument for staying close to SOCA's MOM6 vintage.
- **A very large open-boundary-condition overhaul.** Irrelevant to the global configs, and
  directly relevant to the regional domains ACKBAR is meant to support (see Domains in
  `docs/design.md`). Treat any OBC configuration ported from a soca-science-era regional setup
  as stale until checked against this MOM6.
- Bulk churn not relevant to a global ocean-ice config: ALE/regridding, ice shelf, CVMix and
  MARBL updates, new Zanna-Bolton and ML-diffusivity parameterizations, Apache-2 relicense.

Note that the `OM_1deg` *configuration* barely differs between the branches: `MOM_override`,
`SIS_input`, `input.nml`, `data_table` and `diag_table` are identical, and `MOM_input` gains
only 28 lines, all of which are back-compat pins for the defaults listed above
(`EQN_OF_STATE = "WRIGHT"`, `VISC_REM_BUG = True`,
`MASS_WEIGHT_IN_PRESSURE_GRADIENT_TOP = False`, `DRAG_DIFFUSIVITY_ANSWER_DATE`,
`LOTW_BBL_ANSWER_DATE`, `NDIFF_ANSWER_DATE`).

Those pins are effective: the `OM_1deg` smoke test below gives a bit-identical `ocean.stats`
whether built from the `main` pin (2024 MOM6) or the `dev/gfdl` pin (2026 MOM6). So the
branch choice buys us newer code without changing answers, and any answer change we do see
later will be one we asked for.

**Open question for later:** whether our SOCA configs keep those pins or drop them and run
the corrected physics. For a new system dropping them is probably right, consistent with
soca-science's existing habit of disabling bug-retention flags. Dropping them is a deliberate
one-way step, so do it before spinning up, not mid-experiment.

## Cloning

Normally you get this for free by cloning the `ackbar` repo, which pins `mom6sis2` as a
submodule:

```bash
git -C ~/work/ackbar submodule update --init pkg/mom6sis2
cd ~/work/ackbar/pkg/mom6sis2
git submodule update --init \
    src/MOM6 src/SIS2 src/FMS2 src/coupler src/atmos_null src/land_null \
    src/ice_param src/icebergs src/mkmf
git -C src/MOM6 submodule update --init pkg/CVMix-src pkg/GSW-Fortran
```

Starting from scratch instead, the first line is
`git clone -b dev/gfdl https://github.com/NOAA-GFDL/MOM6-examples.git mom6sis2`.

Two things bite here:

- **Only these submodules.** A plain `--recursive` also drags in `tools/matlab/gtools`,
  `tools/python/MIDAS` and `tools/analysis/mpl-cmocean`, none of which we need.
- **MOM6's own nested submodules are required.** `src/equation_of_state/TEOS10/*.f90` are
  symlinks into `pkg/GSW-Fortran/toolbox/`, and MOM6's `ac/makedep` follows them. Without
  `pkg/GSW-Fortran` initialized the build dies with a bare `FileNotFoundError` on
  `gsw_chem_potential_water_t_exact.f90`, naming nothing about the real problem.

## Building

```bash
./build-model.sh          # TARGET=ice_ocean_SIS2 by default
```

The script sources `site/activate.sh`, which loads the site file for this machine
(`docs/design.md`, The site layer). That supplies the spack-stack toolchain, the job count,
and `ACKBAR_DATASETS_ROOT`, which the script wires to `pkg/mom6sis2/.datasets` so the input
data symlink is not something you maintain by hand.

On top of that it sets `CC/MPICC=mpicc`, `FC/MPIFC=mpif90`, and `FCFLAGS` with
`-fallow-argument-mismatch -fallow-invalid-boz` (needed for gcc 13 against this vintage of
FMS).

Build layout, autoconf under the hood:

- dependency libraries land in `shared/{fms,atmos_null,land_null,ice_param,icebergs}/build/lib*.a`
- the model links in `ice_ocean_SIS2/build/`
- **the executable is `ice_ocean_SIS2/build/coupler_main`**, not `MOM6`. The old mkmf build
  produced `MOM6`; anything carried over from soca-science that names a `MOM6` executable
  needs updating.

Each `build/` directory holds a `config.cache`, and the generated makefiles carry **absolute**
paths into the source tree. After changing branch, compiler, or modules, or after the tree
moves on disk, delete the `build/` directories (`rm -rf shared/*/build ice_ocean_SIS2/build`)
and reconfigure rather than trusting the cache. An already-linked `coupler_main` keeps working
after a move; only incremental rebuilds break. Check with `make -n coupler_main` in the build
directory, which names the stale path if there is one.

## Smoke test

A 12-hour `OM_1deg` cold start on 8 PEs, which exercises the executable, the `.datasets`
wiring and restart writing together.

```bash
RD=$ACKBAR_TEST_ROOT/om_1deg_smoke   # source site/activate.sh first
mkdir -p $RD/RESTART
cd ~/work/ackbar/pkg/mom6sis2/ice_ocean_SIS2/OM_1deg
cp input.nml MOM_input MOM_override SIS_input SIS_override \
   data_table diag_table diag_table.MOM6 diag_table.SIS field_table $RD/
ln -s $PWD/INPUT $RD/INPUT          # symlink, do NOT cp -rL (dereferences ~1GB of WOA13)
printf 'LAYOUT = 4,2\nIO_LAYOUT = 1,1\n' > $RD/MOM_layout
printf 'LAYOUT = 4,2\nIO_LAYOUT = 1,1\n' > $RD/SIS_layout
ln -sf ~/work/ackbar/pkg/mom6sis2/ice_ocean_SIS2/build/coupler_main $RD/coupler_main
cd $RD && source ~/work/ackbar/site/activate.sh
srun --mpi=pmi2 -p compute -n 8 -t 30 ./coupler_main > run.log 2>&1
```

Gotchas:

- `srun --mpi=pmi2` because that is what the workflow launches with (`docs/slurm.md`, srun and
  PMI). If Slurm is down and the question is only whether the executable links and runs,
  `mpiexec -n 8 ./coupler_main` gives bit-identical restarts. Dropping the `--mpi` flag does
  not: eight rank-0s each write the whole domain over each other and the job still says
  `COMPLETED`.

- The committed `MOM_layout` / `SIS_layout` are placeholders with huge PE counts
  (`12,10` and `32,18`) and carry a "should not be used in production" comment. Always
  override them. The 1deg grid is 360x320, so `4,2` divides cleanly for 8 PEs.
- `OM_1deg` cold starts from WOA13 monthly T/S (`TEMP_Z_INIT_FILE` / `SALT_Z_INIT_FILE`),
  so no restart is needed to get going.
- `input.nml` ships with `hours = 12` and `current_date = 1958,1,1`; the JRA55-do short
  sample covers that period.

What success looks like: `ocean.stats` advancing with `En` around 1e-4 and `CFL` well under
1, and `RESTART/` containing `MOM.res.nc` (~1 GB), `ice_model.res.nc`, `calving.res.nc`,
`icebergs.res.nc` and `coupler.res`.
