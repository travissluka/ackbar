# Domains

What each domain is, what it costs, and what is wrong with it. A domain is a
first-class configuration axis here rather than a flag (see Domains in
`docs/design.md`), so this is the file that says which one to reach for.

## The domains

| Domain | Grid | NK | DT | One sim day, 8 PEs | Use |
|---|---|---|---|---|---|
| `om_1deg` | 360 x 320 | 75 | 1800 | 178 s | global development and test |
| `gom_25km` | 87 x 56 | 36 | 1800 | 6.3 s | regional plumbing |
| `gom_12km` | 174 x 111 | 36 | 900 | 30.5 s | regional science |
| `gom_8km` | 271 x 173 | 25 | 900 | 67 s | eddy resolving |
| `gom_4km` | 541 x 346 | 36 | 300 | 930 s | submesoscale |
| `om4_025` | | | | | global production, not yet built |

Timings are wall clock on rancor, 8 MPI ranks, measured serially with nothing
else on the box. They are a ratio worth trusting and an absolute number worth
re-measuring: `tools/coldstart-ic.sh <domain> <date> 24 timing-check` reproduces
one.

`gom_25km` comes out 28x faster than `om_1deg` rather than the 38x its grid size
predicts, because 87 x 56 split eight ways leaves subdomains too small to pay
for their halo exchange. `gom_12km` hits its predicted 6.3x.

At the top of the range the halo penalty is gone and the scaling is the
arithmetic one. `gom_4km` costs 14x `gom_8km` against the 17x that 4x the cells,
1.4x the levels and 3x the steps predict, so the larger subdomains are
recovering some of what `gom_25km` loses. In absolute terms `gom_4km` is 5x
`om_1deg` per simulated day, which is what makes it a domain to reach for
deliberately: a 50 cycle experiment at 12 hour cycles is around 6.5 hours of
model alone, and that is before any analysis.

## The Gulf of Mexico domains

All four cover 98.1W to 76.4W, 18N to 32N. They came from a 2021 MOM6-SIS2
configuration and were imported by `tools/import-gom-domain.sh`.

**Names are in kilometres, and the sources they came from were not.** The
original directories were named in fractions of a degree with digits that read
as decimals:

| Source directory | Actually | Imported as |
|---|---|---|
| `04deg` | 1/4 degree, ~25 km | `gom_25km` |
| `08deg` | 1/8 degree, ~12 km | `gom_12km` |
| `012deg` | 1/12.5 degree, ~8 km | `gom_8km` |
| `025deg` | 1/25 degree, ~4 km | `gom_4km` |

Note that the digits swap between the two conventions: `08deg` is `gom_12km` and
`012deg` would be `gom_8km`. Do not infer the mapping, read it here.

`gom_8km` is the odd one out vertically: NK = 25 on `hycom1_25.nc`, where the other three use
36 on `hycom1_36.nc`. That is inherited and real, not drift, so an initial condition or a
gridspec built for it is not interchangeable with the others.

**Use `gom_12km` for anything whose answer matters** unless you need the finer two. The first baroclinic
Rossby radius in the Gulf is 35 to 45 km. At 12 km the Loop Current resolves and
sheds rings, so altimetry has real mesoscale to correct, which is the signal a DA
benchmark exists to measure. At 25 km it is barely eddy-permitting and an
analysis there has very little to do. `gom_25km` is for exercising the workflow.

### What is wrong with them

Three things, all known, none of them properties of the workflow. Each is worth
fixing and none blocks development.

**The open boundary is frozen.** Every `obc.nc` holds exactly one time record, a
SODA 3.3.1 five-day mean valid 2015-01-04 13:00, and `ic.nc` is the same
snapshot. Cycling for weeks against a stationary boundary means the interior
relaxes toward whatever that one snapshot implies. Regenerating needs
`PyCNAL_regridding`, which is defunct, and `/data/soda`, which is no longer on
this machine, so replacing this is a project rather than a rerun of
`common/gen_obc.py`. In the meantime it is a usable imperfect model: the boundary
error is real error for an analysis to work against.

**The atmosphere is a climatology.** NCAR/CORE, staged once under
`$ACKBAR_STATIC_ROOT/forcing/ncar-clim` and shared by every resolution. It has no
synoptic variability at all, so forecast error on these domains is dominated by
missing weather rather than by anything an analysis can correct. Replacing it
with ERA5, JRA-55 or GEFS lands beside it under a new source name.

**SOCA cannot configure their open boundaries.** SOCA links a MOM6 built without
symmetric memory, and MOM6 refuses Flather OBCs in such a build:

    FATAL: MOM_open_boundary, open_boundary_config:
           Symmetric memory must be used when using Flather OBCs.

which aborts inside `soca_geom_init`, so every SOCA application on a regional
domain dies during geometry construction. The forecast model is built symmetric
and runs the same boundaries fine, so this is the two builds disagreeing rather
than a configuration error. Worked around by
`config/model/mom6sis2/domain/<domain>/MOM_override.soca`, which switches the
segments off for SOCA and only for SOCA; the grid with three Flather segments and
the grid with none are the same grid, and the grid is all SOCA wants. The real
fix is building SOCA's MOM6 with symmetric memory, which is a build-level
decision open since soca-science.

## How a domain is put together

Four things, in three places, and the split is deliberate.

| Part | Where | Named by |
|---|---|---|
| the stock MOM6-SIS2 text | `config/model/mom6sis2/domain/gom/common/`, or upstream | `mom6_base_dir` |
| its data | `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/` | `mom6_input_dir` |
| ACKBAR's overrides | `config/model/mom6sis2/domain/gom/<res>/` | `mom6_override_dir` |
| SOCA geometry | `$ACKBAR_STATIC_ROOT/static/<domain>/` | `domain.static` |

A family of domains that share a base configuration nests under one directory,
which is why the Gulf resolutions are `gom/25km` and not `gom_25km`: the shared
files have somewhere to be that is obviously theirs, and adding a fifth
resolution cannot put a file where four domains will read it by accident.
`om_1deg` has no family and sits flat at `domain/om_1deg/`.

The three paths are stated per domain rather than derived from its name, and the
offline stages read them back out of the layer (`tools/domain-paths.sh`) instead
of rebuilding the mapping. No rule over the name is right for all of them: four
domains share one base directory and `om_1deg`'s is upstream's.

Text in git, data out of it. That is MOM6-examples' own convention: it tracks
`MOM_input` and the tables while `INPUT/` holds symlinks into a gitignored
`.datasets` mirror. What decides the answer belongs under review, and a 300 MB
grid file does not belong in a clone. ACKBAR differs on one detail, naming the
data directory as an absolute path rather than reaching it through a symlink, so
that `ackbar validate` stats it before submission instead of FMS failing on a
dangling link mid-job.

`om_1deg` is the exception that proves the split: its configuration is
upstream's, inside the MOM6-examples submodule, so `mom6_base_dir` points there
and its data comes with it. Only its overrides are ACKBAR's.

### One configuration, four resolutions

The four Gulf domains share a single base directory, `domain/gom/common`. They arrived as four
independent copies: 77 identical MOM6 parameters apiece plus 26 that differed, four `SIS_input`
files differing only in `NIGLOBAL`/`NJGLOBAL`, and four `input.nml` files differing only in
groups ACKBAR rewrites. Keeping four copies of the 77 is how the other 26 drifted apart in the
first place, so there is now one copy and each domain overrides what makes it itself.

Each domain's `MOM_override` therefore has three sections, and the second one is the interesting
one:

- **the resolution**, the 11 parameters with an actual reason to differ: `NIGLOBAL`, `NJGLOBAL`,
  `NK`, `DT`, `DT_THERM`, the ALE coordinate and the z-level init files.
- **inherited drift**, the 15 with no reason to differ. `FRAZIL`, `THERMO_SPANS_COUPLING`,
  `MAXTRUNC`, `DIABATIC_FIRST`, `VERTEX_SHEAR`, `SAVE_INITIAL_CONDS`, `DAYMAX`,
  `ENERGYSAVEDAYS`, the OBC nudging timescales, and a few that differ only in spelling (`3e3`
  against `3000`). These are 2021 hand edits nobody reconciled. They are kept per domain so
  that the split changed no answers, which means each is a decision waiting to be made:
  delete the line from all four overrides and set the value once in `gom/common/MOM_input`.
- **ackbar**, the bug-retention flags.

The restructure was checked rather than trusted: every parameter of every domain resolves to the
value it resolved to before.

`dt_cpld` is the one resolution-dependent value that could not move into the shared base,
because it lives in `input.nml` rather than in `MOM_input`. The domain layer states it as
`mom6_dt_cpld` and ACKBAR patches it in alongside the run length.

### The overrides

`MOM_override` and `SIS_override` replace whatever the base configuration ships
under those names rather than adding to them, and ACKBAR puts them in
`parameter_filename` itself so that a configuration which never mentions them
still reads them.

They exist mostly to turn off bug-retention flags. MOM6-examples is a regression
suite and its cases hold those on to protect historical answers, which is right
for a regression suite and wrong for a testbed whose entire output is an error
statistic: a bug left enabled shows up as forecast error the DA is then scored on
correcting.

- `om_1deg` needs a list. Its `MOM_input` states seven flags explicitly and its
  `SIS_input` four, and an explicit setting beats `ENABLE_BUGS_BY_DEFAULT`.
- The Gulf configuration states none, so one line reaches all four domains. What that line
  turns off there is mostly different: twelve of the fourteen are in the open
  boundary code, which a global domain never executes.
- `DTBT_RESTART_BUG` matters on every domain. It is the barotropic timestep not
  surviving a restart, and a cycling system restarts every cycle.

Re-check after any MOM6 bump, because a bump can add flags and a flag added
upstream will not appear in an override until someone looks:

```bash
grep -E '_BUG[A-Z_]* = True' MOM_parameter_doc.all SIS_parameter_doc.all
```

### There is no layout

`ntasks` is the only thing a domain says about PEs. MOM6 decomposes for itself
and picks what a person would: 4x2 at 8 PEs, 3x2 at 6, 4x3 at 12, 1x5 at 5. A
layout in configuration would be a second home for the PE count and one more
thing to get wrong on every machine with a different core count.

## Adding a domain

```bash
tools/import-gom-domain.sh <source-dir> <domain>   # text and data
$EDITOR config/model/mom6sis2/domain/gom/<res>/MOM_override  # reduce the import against
                                                            # gom/common; and SIS_override,
                                                            # and MOM_override.soca
$EDITOR config/layers/domain/<domain>.yaml                  # the three vars, resources
tools/soca-gridspec.sh <domain>                                # the static stage
tools/coldstart-ic.sh <domain> <YYYY-MM-DDThh> <hours> <slug>  # the IC stage
tools/soca-diffusion.sh <domain>                               # for a DA experiment
tools/soca-dirac.sh <domain>                                   # and check it
tools/obs-archive-osse.py --domain <domain> ...                # observations that reach it
tools/ensemble-ic.sh <domain> <members>                        # for an ensemble experiment
ackbar validate <experiment>.yaml
```

The background error and the observations are only needed by an experiment that
assimilates; a free run names no B and reads no observations. The ensemble
initial condition is only needed by one that carries an ensemble, and it depends
on the background error, since that is what its perturbations are drawn from.
See [`background-error.md`](background-error.md) and
[`analysis.md`](analysis.md).

**Observations have to be built for the domain, and this is quiet when it is
wrong.** A global observation file handed to a regional domain does not fail:
SOCA runs, every observation outside the grid fails its `Domain Check`, and the
cycle completes with an analysis that assimilated nothing. The only symptom is
an increment of zero. `obs-archive-osse.py` generates from the domain's own
gridspec; a real archive needs the domain-scoped culling stage described in
Domains in [`design.md`](design.md).

`ackbar validate` step 3 stats every one of those paths, so a stage that has not
been run is a message naming the directory rather than a job that fails an hour
later.
