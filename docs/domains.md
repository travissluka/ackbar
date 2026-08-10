# Domains

What each domain is, what it costs, and what is wrong with it. A domain is a
first-class configuration axis here rather than a flag (see Domains in
`docs/design.md`), so this is the file that says which one to reach for.

## The domains

| Domain | Grid | NK | DT | One sim day, 8 PEs | Use |
|---|---|---|---|---|---|
| `om_1deg` | 360 x 320 | 75 | 1800 | 178 s | coarse global; graph fixtures only |
| `gom_25km` | 87 x 56 | 50 | 1800 | 6.3 s | regional plumbing |
| `gom_12km` | 174 x 111 | 50 | 900 | 30.5 s | regional science |
| `gom_8km` | 271 x 173 | 50 | 900 | 67 s | eddy resolving |
| `gom_4km` | 541 x 346 | 50 | 300 | 930 s | submesoscale |
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

All four Gulf domains share one vertical grid, and that is enforced structurally:
`NK`, `COORD_CONFIG` and `ALE_COORDINATE_CONFIG` live in `gom/common/MOM_input`
and are absent from every `MOM_override`, so a domain cannot restate one without
MOM6 taking the duplicate as fatal. Two resolutions that differ vertically are
not comparable, which is the whole reason the Gulf domains exist as a set.

The grid is 50 levels of Z\*, built by `FNC1:2,5500.0,4.0,0.01`: 2 m layers
through the top 20 m, 20 levels above 100 m, 35 above 1000 m, 526 m at the
bottom. Two things about it are worth knowing before reading a profile.

Z\* integrates down from the surface and squeezes the leftovers into the bottom,
so a shallow column keeps the full surface profile and collapses its deep layers
to `MIN_THICKNESS`; a 35 m shelf cell still has 2 m top layers. And `H_total` is
5500 m against a domain that reaches 7642 m in the Cayman Basin, so the deepest
0.1% of cells carry one very thick bottom layer. That is deliberate: spending
levels down there would take them from the 92% of the domain above 4000 m.

Z\* rather than the HYCOM1 hybrid these domains used to run, for two reasons.
`VERTEX_SHEAR` needs a coordinate whose layers mean the same thing everywhere to
do its job on analysis increments, and under a hybrid coordinate a diagnostic
that differences a layer index is differencing two different depths and reports
drift that is not there. Under Z\* a layer index is a depth.

**Use `gom_12km` for anything whose answer matters** unless you need the finer two. The first baroclinic
Rossby radius in the Gulf is 35 to 45 km. At 12 km the Loop Current resolves and
sheds rings, so altimetry has real mesoscale to correct, which is the signal a DA
benchmark exists to measure. At 25 km it is barely eddy-permitting and an
analysis there has very little to do. `gom_25km` is for exercising the workflow.

### What is wrong with them

Four things, all known, none of them properties of the workflow. Each is worth
fixing and none blocks development. The first has been fixed for `gom_25km` and
not for the others.

**The imported bathymetry is a cliff at the shelf break.** The steepness of a
neighbouring pair, `r = |h1-h2|/(h1+h2)`, reached 0.998 on `gom_25km` as
imported: the Cuban shelf sits at 2 to 9 m across many cells and drops to 3300
to 4400 m in one 25 km cell. 117 ocean cells were above r = 0.9.

A free forecast tolerates it. `osse25-noda` ran 45 cycles without complaint. An
analysis does not: the face between a thin column and a deep one carries
transport scaled to the deep side, so any increment error there drains the thin
one, and MOM6 reports `btstep: eta has dropped below bathyT` and then dies in
`implied h<0`. **Every forecast this workflow has lost to an analysis increment
was one of those cells**, and the workarounds that preceded finding it, relaxing
the increment, tapering it by depth, bounding its divergence, all paid for a few
hundred cells with the analysis everywhere.

**It is also not a defect in the import.** `OM4_025` is quarter degree, so at 22 N
its cells are about as wide as `gom_25km`'s, and GFDL ships it with a 3 cm wet
cell, a worst-pair steepness of 0.9993, and 1.65% of Gulf-window pairs above
r = 0.9, against our imported field's 2.09%. Their `MINIMUM_DEPTH` is 9.5 m to our
10 m. What they hand-edit is not the slope: `TOPO_EDITS_FILE = All_edits.nc` holds
80 cells globally, 30 in the Gulf window, and every one of the 30 *deepens* a cell,
in two lines, the Old Bahama Channel to 500 m and the Florida Current's path along
79.6 W to 710 to 730 m. The practice at this resolution is to leave the shelf break
as a cliff and spend the editing budget on the transport pathways.

So smoothing here is a concession to the analysis, not a correction of the grid,
and the capped field is already more conservative than a production one. Read that
as a bound on how far to take it: capping harder moves further from a sea floor
GFDL is content to integrate, to buy something the DA should be buying instead.
The durable fix is IAU.

`tools/smooth-topography.py` caps it, keeping the land mask exactly so the
mosaics and the SOCA gridspec stay true. The cap is a choice with a cost: `r`
bounds the depth ratio between neighbours at `(1+r)/(1-r)`, so a 10 m to 4000 m
shelf break is spread over `log(400)/log((1+r)/(1-r))` cells. The classic 0.2
comes from sigma-coordinate pressure gradient error, which is not the problem
here, and would spread it over thirteen cells and relocate the shelf break.

| target | cells moved > 1 m | largest move | shelf break spread over |
|---|---|---|---|
| 0.2 | - | 2566 m | 13 cells, 327 km |
| 0.6 | 753 | 1198 m | 4 cells, 96 km |
| 0.8 | 468 | 497 m | 2.4 cells, 60 km |
| **0.9** | **343** | **219 m** | **2.0 cells, 50 km** |

Which resolutions carry a smoothed field is a property of the data, not of this
file: `ocean_topog.nc.unsmoothed` sits beside the field it replaced wherever the
tool has run, and the file it writes records the cap in a `smoothed` attribute.

**The cap replaced the divergence bound rather than sharing the work with it.**
The two arrived together, which made it impossible to tell which one was keeping
members alive, so they were separated: at r <= 0.9 with `increment divergence
limit` disabled entirely, five analysed cycles ran twenty one of twenty one
forecasts with every job completing. The bound is not what the domain needs. The
sea floor is.

That matters because the bound was not cheap. It reached a sixth of wet cells,
most of them in deep water rather than the thin columns it was justified by, and
took a substantial share of the depth-weighted velocity increment every cycle
rather than only during spin-up. Contrast the per-point `increment limits`, which
stay: those touch a tenth of a percent of live velocity points and clip a tail,
which is the cost a guard should have.

One trap worth naming, because it is easy to repeat. Judging a run by counting
surviving `rst` directories reads a healthy experiment as a dead one, since
`cleanup.keep_cycles` removes an older cycle's restarts on schedule. Count
`submitted.*` and `sacct` states instead.

**`MINIMUM_DEPTH` was not changed and probably does not need to be.** It is 10 m
in `gom/common/MOM_input` for every Gulf resolution, and the imported topography
holds columns down to 2 m that MOM6 was already rounding up to it at run time.
The smoother floors at the same number, so the water the model integrates is
unchanged and the file has stopped disagreeing with it. Raising the minimum is a
separate lever, available if a domain still drains after smoothing, and worth
reaching for only then: `MASKING_DEPTH` is 0, so raising it deepens those columns
rather than drying them, and the coastline does not move either way.

**`gom_25km` is the worst case, and it is the one domain where the answer does
not matter.** `r` is a property of the grid as much as of the sea floor: the same
shelf break sampled at 12 km spans twice as many cells as at 25 km, so it is half
as steep before anything is done to it. So the coarsest domain, the one that
exists to prove plumbing rather than to compute a result, is where the cliff is
sharpest and where the increment has to be held back hardest. Expect `gom_12km`
and finer to need a looser cap, and expect the divergence limiter to barely fire
there. Run the tool with `--dry-run` on a new resolution and read its own
distribution before choosing a cap; 0.9 is what `gom_25km` needed, not a constant.

The corollary is that the limiter's settings should not be tuned on `gom_25km` and
carried to `gom_12km`. A bound that is load bearing at 25 km may be inert at 12 km,
and a science result should not inherit a number that was chosen to keep the
plumbing domain alive.

Changing it invalidates everything downstream of the sea floor: the gridspec,
every initial condition, the diffusion calibration, and every experiment. The
observations and the nature run are not affected while only `gom_25km` changes,
because those are `gom_12km` products, and that is what keeps the rebuild
bounded. `gom_12km` needs the same treatment before it is used for DA, and doing
it means regenerating the truth and the whole observation archive with it.

**The open boundary is time varying on the two domains that run, and frozen on
the other two.** `tools/fetch-glorys.py obc` built the first pair from a daily
GLORYS12V1 reanalysis; `gom_8km` and `gom_4km` still carry the single SODA 3.3.1
five-day mean the domains were imported with, and the SODA file is kept beside
each as `obc.nc.soda`. Ask rather than trust this paragraph:

```bash
ncdump -h $ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/obc.nc | grep -E 'time = |:source'
```

A frozen boundary is usable but not neutral: cycling for weeks against one
snapshot means the interior relaxes toward whatever that snapshot implies, and
the resulting error is boundary error rather than the forecast error an analysis
exists to correct.

**A shared boundary is not neutral either, and that is what the OSSE has, on
purpose.** `gom_25km` and `gom_12km` carry the same GLORYS slice, so truth and the
assimilating system are held to the same edge, and every skill number includes a
constraint no real regional system has. Giving truth an independent boundary was
built and declined: the difference between two products is dominated by a
basin-wide sea level offset that `ObsADT` makes invisible to every solver, so it
would add an uncorrectable penalty that confounds the comparison rather than
sharpening it. `docs/osse.md` carries the reasoning.

What the second source is for instead is calibration. `tools/obc-lagged.py`
builds a per-member ensemble by lagging one product's boundary, and
`tools/fetch-hycom.py` provides the independent measurement of how far apart two
reanalyses of the same day actually are, which is what sets that ensemble's
amplitude. See `ensemble.inputs` in `docs/design.md` for how the ensemble reaches
a run.

Two things about a second source are worth knowing before using one. Its sea
surface height carries a different datum, 0.16 m between HYCOM GOFS 3.1 and
GLORYS on this boundary, which under FLATHER is a permanent head rather than a
bias and has to be removed at the source (`fetch-hycom.py --match-ssh`) rather
than left for the analysis. And HYCOM's `water_temp` is in-situ where MOM6 wants
potential, so the fetcher converts it; a source that is not converted arrives
warm at depth and looks like a model bias.

The GLORYS span is the constraint to know about, because a run outside it fails
in `time_interp_external` at the first timestep. **Every tier 3 fixture starts
2015-01-05, which is outside it, and this is not a latent problem: it is what
the gom tier 3 tests fail on.** `test_tier3.py`, `test_tier3_gom.py`,
`test_tier3_diffusion.py` and `test_tier3_gom_obc.py` all inherit `tier3_gom`,
and the model stops with

```
FATAL from PE 7: time_interp_external 2: time 735114 (20150105.010000 is before
range of list 735257-735362(20150528.000000 - 20150910.000000),file=INPUT/obc.nc
```

`tier3_gom.yaml`'s own header still describes the boundary as a SODA five-day
mean valid 2015-01-04, which it stopped being when the domain was rebuilt on
GLORYS. Reconciling it means either extending the fetch backwards or restamping
the smoke initial condition and moving the fixture dates, and either way it
touches all four modules, so it is a change of its own rather than a fix to make
in passing.

**A lagged boundary ensemble is shorter than the boundary it came from, and by
enough to matter.** `tools/obc-lagged.py` cuts the largest lag off each end,
because a member lagged 21 days forward has nothing to read in the last 21 days
of the file. So a span 21 ladder built from a boundary covering 2015-05-28 to
2015-09-10 covers 2015-06-18 to 2015-08-20, while the `osse25-*` experiments run
45 daily cycles from 2015-07-12, to 2015-08-26, and their extended forecasts
reach about 2015-08-31. Eleven days short. `ackbar validate` reports that at
step 3, by reading each member's own time axis and comparing it against the span
the graph will ask for, which is the only chance anything has to catch it: the
run would otherwise fail in `time_interp_external` around cycle 40, and healing
cannot recover, because fixing it means re-fetching and rebuilding the ladder,
which changes the boundary every earlier cycle already integrated against. Fetch
the source long enough for the experiment *plus twice the span* before building
an ensemble from it.

Each member says what it covers, so this is answerable from the file rather than
from the ladder arithmetic:

```bash
ncdump -h $ACKBAR_STATIC_ROOT/obc/<domain>/<archive>/mem000.nc \
  | grep -E ':time_coverage|:perturbation ='
```

`perturbation` names the member's own lag and the amplitude, and the source's
`:source` carries through, so an archive records which pull it was built from
without depending on its directory name.

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

- **the resolution**, the parameters with an actual reason to differ: `NIGLOBAL`, `NJGLOBAL`,
  `DT`, `DT_THERM` and the z-level init file. `NK` and the ALE coordinate used to be here and
  are not any more; see the vertical grid above.
- **inherited drift**, the ones with no reason to differ. `FRAZIL`, `THERMO_SPANS_COUPLING`,
  `MAXTRUNC`, `DIABATIC_FIRST`, `SAVE_INITIAL_CONDS`, `DAYMAX`, `ENERGYSAVEDAYS`, the OBC
  nudging timescales, and a few that differ only in spelling (`3e3` against `3000`). These are
  2021 hand edits nobody reconciled. They are kept per domain so that the split changed no
  answers, which means each is a decision waiting to be made: delete the line from all four
  overrides and set the value once in `gom/common/MOM_input`.

  `VERTEX_SHEAR` was the first one collapsed, and it is the model for the rest. Only `gom_4km`
  set it, which read as a resolution choice and was not one: it is what stops an analysis
  increment leaving a grid-scale checkerboard the model then carries forward, and every domain
  running DA wants it. It now sits in `gom/common/MOM_input` for all four, and in `om_1deg`.

  `BUOY_CONFIG` and `WIND_CONFIG` are listed as drift here and are not drift. soca-science
  documents both as required by SOCA's MOM6 solo rather than by the coupled forecast, so they
  are deliberate and belong in the shared base with that reason attached.
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
env -u PYTHONPATH .venv-data/bin/python \
    tools/fetch-glorys.py obc <domain> <start> <end>            # the boundary, if it has one
tools/coldstart-ic.sh <domain> <YYYY-MM-DDThh> <hours> <slug>  # the IC stage
tools/soca-diffusion.sh <domain>                               # for a DA experiment
tools/soca-dirac.sh <domain>                                   # and check it
tools/obs-archive-osse.py --domain <domain> ...                # observations that reach it
tools/ensemble-ic.sh <domain> <members>                        # for an ensemble experiment
env -u PYTHONPATH .venv-data/bin/python tools/obc-lagged.py \
    --span 21 --members <members> \
    --out $ACKBAR_STATIC_ROOT/obc/<domain>/<product>-lag21 \
    <domain>                                                   # a boundary ensemble, if wanted
ackbar validate <experiment>.yaml
```

**The boundary is fetched once per domain and the ensemble is built once per
experiment family, both offline, both before anything is submitted.** Neither is
a cycle task and neither reaches the network from inside a job: `fetch-glorys.py`
writes `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/obc.nc`, which every experiment
on that domain shares read-only, and `obc-lagged.py` reads that file and writes
`mem000.nc` and up into a directory an experiment then names in
`ensemble.inputs`. The order matters only in that the second reads the first.
Fetch the source long enough for the experiment plus twice the span, for the
reason below.

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
