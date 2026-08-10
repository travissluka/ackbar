# The background error

The static B an analysis uses is described in one place,
`config/layers/da/variational.yaml`, under `solver.background error`. It has
four parts, and they do different jobs:

| Part | Block | What it decides |
|---|---|---|
| correlation | `saber central block: diffusion` | how far an observation reaches |
| standard deviation | `SOCAParametricOceanStdDev` | how much it is allowed to move things |
| taper | `SOCABkgErrFilt` | where it is not allowed to move things at all |
| balance | `BalanceSOCA` | what else moves when temperature does |

Only the first is calibrated offline, because only the first has to be. The
diffusion operator's normalization is estimated numerically from the grid it
runs on, so it is a file rather than a number, and it takes long enough to
compute that a cycling experiment cannot pay for it.

## The offline stage

```bash
source site/activate.sh
tools/soca-diffusion.sh <domain>          # calibrate
tools/soca-dirac.sh <domain>              # check it
```

`soca-diffusion.sh` writes `corr_hz.nc`, `corr_hz_ssh.nc`, `loc_hz.nc`, `loc_hz_open.nc`
and `corr_vt.nc` into
`$ACKBAR_STATIC_ROOT/static/<domain>/diffusion`, which is what the `filepath`
entries in the variational and hybrid layers name. The list is the entries in
`config/static/diffusion.yaml` rather than a list in the script, so adding a group is
one edit. Until it has run, `ackbar validate`
step 3 reports those files as missing inputs. Keyed on domain and nothing else,
for the same reason the gridspec is: two experiments on a domain that disagree
about their background error are not comparable, and nothing downstream would
say so.

It runs in two passes.

**The lengths.** `tools/diffusion-scales.py` reads the domain's gridspec and one
restart and writes a length scale at every cell. Horizontally that is a multiple
of the local Rossby radius, floored by a multiple of the local cell size and
capped, then smoothed by itself. Vertically it is the number of model levels the
mixed layer spans at the surface, tapering to a floor below it. The numbers are
in `config/static/diffusion.yaml`, which is not per domain: everything in it is
relative to something the grid already carries, so a 4 km grid and a 25 km grid
reading the same file get different lengths in metres.

**The normalization.** `soca_error_covariance_toolbox.x` builds a diffusion
operator from those lengths and estimates, by randomization, the field that
turns it into a correlation with ones on the diagonal. This is the whole cost of
the stage and the reason it is offline.

Horizontal and vertical are two separate invocations of the toolbox. saber reads
a group's scale field by constructing an increment over the calibration's active
variables and picking one field out of it, so every scale file would otherwise
have to carry every variable, and the horizontal randomization would run over
the whole column instead of over one level.

**One of the five is not a correlation.** `loc_hz.nc` is the localization an
ensemble covariance applies, and it is here because it is the same operator
built from the same grid, not because it is the same quantity: the other three
are correlation lengths for a background error that is *modelled*, and this is
the radius beyond which a correlation *sampled* from twenty members is taken to
be noise and tapered away. It is deliberately wider than `hz`, at twice the
Rossby radius against one, and the reason is that localizing tighter than the
true correlation length throws away exactly the long range structure the
ensemble is there to provide. That failure looks like an ensemble component
which is not contributing rather than like a mistake. Read by
`config/layers/da/hybrid.yaml` and by nothing else; a static 3DVar never opens
it.

## Masking, and why the localization is not masked

Every entry under `horizontal:` in `config/static/diffusion.yaml` takes an optional
`masked`, defaulting to true, which zeroes the scale over land. A zero scale is how the
diffusion operator is told that a cell is not part of the ocean, so it is a wall: nothing
crosses it, and every cell within a scale length of a coast is normalized against a
truncated kernel.

For the three correlations that is right. The background error does not communicate
through land, and a temperature increment must not cross a peninsula.

For the localization it is wrong, which is why `loc_hz_open` exists with `masked: false`.
The localization is a taper applied as a Schur product against a covariance *sampled* from
the ensemble. It carries no state value anywhere, so an unmasked localization cannot leak
an increment onto land or across it: what it changes is only how far away a sampled
correlation is still believed. Masking it discards the ensemble's genuine cross-coast
structure, in the shallow water where the ensemble has the most to say and the observations
are densest. There is nothing degenerate to include either: `min grid mult` floors every
cell before the mask is consulted, and SOCA's gridspec carries a real Rossby radius over
land, so the unmasked field is continuous across the coast rather than a moat of floor
values.

The two are separate entries and separate files rather than one entry that changed its
mind, because every EnVar and hybrid result already on disk was run against `loc_hz.nc` and
has to keep naming the file it read.

**Which variable the scale field travels under is load-bearing, and it is the one thing
here that has already gone wrong once.** `tools/diffusion-scales.py` writes the land values,
and `tests/test_diffusion_mask.py` holds it to that. What used to lose them was the read:
the calibration handed the scale field to SOCA as an ordinary state named
`sea_surface_height_above_geoid`, whose entry in `config/model/mom6sis2/fields_metadata.yaml`
is masked, and `soca_fields_read` replaces every masked cell with the field's fill value
before saber sees it. So `loc_hz_open.nc` and `loc_hz.nc` came out of the toolbox carrying
bit identical `hzScales` and bit identical `hzNorm`, a dirac through each returned the same
increment to the last bit, and `masked: false` was a statement with no effect: every
localization in use was the masked one.

The scale field now travels as `friction_velocity_over_water`, which the metadata marks
`masked: false`, out of the `sfc` restart slot, because every unmasked entry lives there.
`src/ackbar/diffusion.py` owns the pair of names and `tools/soca-diffusion.sh` writes the
document from them, so the io name in the file and the JEDI name in the configuration cannot
drift apart. The name means nothing about the field: it holds a length in metres.
`tests/test_diffusion_mask.py` now fails if that variable is a masked entry, which is the
assertion that was missing.

What the fix is worth, on `gom_25km` at two coastal diracs: the two calibrations' scales are
bit identical over water and differ over land only, 60 to 118 km against zero; a dirac
through each differs by 2 to 19 per cent of its peak where it used to differ by exactly
nothing; and the difference sits where the argument says it should, +51 to +69 per cent of
the increment for the cells whose path to the dirac crosses land against +0.1 to +2.4 per
cent for cells in clear water. `site/monitor/dirac-locmask/` carries the tables and the
figures, and `site/monitor/dirac-velocity/` is the run that found the bug.

Both entries were recalibrated together. `loc_hz` came back bit identical to the file it
replaced, which says the change reaches only the field that asked not to be masked and that
the randomization is repeatable at a fixed rank count. Every EnVar and hybrid result
produced before that recalibration was localized by the masked field whatever its `cfg/`
says it read.

**How an experiment chooses.** `da/hybrid` declares a var:

```yaml
vars:
  localization_hz: loc_hz_open      # the default
```

and the layer's `filepath` is `$(domain_static)/diffusion/$(localization_hz)`. An
experiment that wants the masked field restates the var in its own `vars:` block. The
choice is frozen into the experiment's `cfg/` at create time, so what a run read is
answerable from the run.

`tools/soca-dirac.sh --localization <name>` overrides the var for one run, which is how the
pair is compared without an experiment behind it.

Reading the wrong one does not fail. Both files exist on a fully calibrated domain, both
are correctly normalized for their own scales, and the analysis solves either way. A run
that reads `loc_hz` by mistake produces localization lengths that are shorter than the file
says wherever a coast is within a scale length, so the ensemble contributes less than it
should on the shelf, and it does it smoothly, which reads as an ensemble component that is
merely disappointing. The pair is only interpretable as a measurement of the mask because
the two entries carry identical multipliers; `tests/test_diffusion.py` is what holds that
true.

## Reading it back

The analysis reads what the calibration wrote, and two things about the read
have to match the write or the result is wrong without being visibly wrong.

The **vertical scheme** is one. The vertical operator is implicit, a fixed count
of tridiagonal solves down each column, rather than explicit, whose iteration
count grows with the square of the scale in levels. `corr_vt.nc` holds a
normalization computed with the implicit operator; read back through the
explicit one, every vertical increment is scaled by the ratio of two kernels.
`method` and `iterations` therefore appear in both `config/static/diffusion.yaml` and
the variational layer, and `tests/test_diffusion.py` exists to hold them
together, across both layers that read a calibration. The horizontal is
explicit, which is saber's default and is stated in neither place. The
localization's vertical block is `strategy: duplicated`, which reads no file and
configures no operator: the same horizontal localization applies at every level
and nothing localizes in the vertical. That is the bundle example's arrangement
and the honest starting point, since vertical localization for an ensemble
covariance is a different quantity again, computed by `soca_sqrtvertloc.x`.

The **filepath** is the other. saber's `filepath` is a stem: the file it opens
is the stem plus `.nc`. `ackbar validate` knows this, which is why a calibrated
domain does not report three permanent missing inputs.

## Checking it

```bash
tools/soca-dirac.sh <domain>                     # two locations picked from the grid
tools/soca-dirac.sh <domain> 26.5,-90.0 28.9,-88.4
```

A correlation applied to a delta function returns exactly one at the delta's own
point. That is what normalizing it means, so the peak value in the dirac report
is a direct measurement of the normalization's error and of nothing else. The
report also gives the distance at which the response falls to `exp(-0.5)` of its
peak, which for a Gaussian kernel is the length scale itself, beside the length
the calibration was asked for.

Both are things an analysis cannot tell you. An under-normalized B gives
increments that are smoothly a few percent small; a mis-scaled one spreads them
over the wrong distance. Both look like tuning.

`soca-dirac.sh` builds its document from the variational layer's own central
block rather than from a copy, so it tests the operator the analysis will use.
It drops everything outside that block, because the standard deviations and the
balance operator turn a correlation into a covariance and the peak value would
then mean nothing.

With no locations it picks two: the deepest ocean cell, and the cell with the
shortest horizontal scale. Those are the two regimes the calibration has, open
ocean where the scale is the Rossby radius and shelf or high latitude where it
is the grid floor, and a calibration that is wrong is usually wrong in one and
not the other.

## Checking the other two covariances

`--full`, `--ensemble` and `--hybrid` apply a covariance rather than a
correlation, so nothing passes or fails and what comes back is the increment one
observation would produce. Every one of them is assembled by
`ackbar.soca.background_error` over the merged layer stack, which is the same
call an analysis job makes.

```bash
E=/data/ackbar/static/ic/gom_25km/osse-control-25km/20150712T00/ensemble20
export ACKBAR_DIRAC_RESTART=${E%/*}/MOM.res.nc
tools/soca-dirac.sh gom_25km 25.5,-90.0 --full     --keep /data/ackbar/test/dirac/static
tools/soca-dirac.sh gom_25km 25.5,-90.0 --ensemble $E --keep /data/ackbar/test/dirac/ensemble
tools/soca-dirac.sh gom_25km 25.5,-90.0 --hybrid   $E --keep /data/ackbar/test/dirac/hybrid
tools/dirac-page.py $STATIC/soca_gridspec.nc $ACKBAR_DIRAC_RESTART \
    /data/ackbar/test/dirac/ensemble/points.json site/monitor/dirac-localization \
    "static B=/data/ackbar/test/dirac/static/<increment>.nc" \
    "ensemble=/data/ackbar/test/dirac/ensemble/<increment>.nc" \
    "hybrid=/data/ackbar/test/dirac/hybrid/<increment>.nc" --ensemble $E
```

The velocity page is four of those runs, one per coast, and the tool reads the `--keep`
directories rather than a list of files, because it needs each run's `points.json` and its
localization increment as well as its increment:

```bash
for side in "north 28.37,-89.25" "east 27.62,-83.50" \
            "south 22.12,-89.25" "west 20.12,-95.75"; do
    set -- $side
    tools/soca-dirac.sh gom_25km "$2" --ensemble $E --keep /data/ackbar/test/dirac-uv/$1
done
tools/dirac-uv-page.py --gridspec $STATIC/soca_gridspec.nc \
    --restart $ACKBAR_DIRAC_RESTART --members $E \
    --out site/monitor/dirac-velocity \
    --run north=/data/ackbar/test/dirac-uv/north ...
```

An ensemble run writes two increments and a hybrid run writes four, because the
toolbox applies each component of a hybrid separately and applies a
localization by itself as well. So one hybrid run reports the hybrid, the static
half, the ensemble half, and the localization, which is the only arrangement in
which "the ensemble half is contributing" and "the weights are what was asked
for" are separately visible.

**What one temperature dirac in the Gulf thermocline says**, at 25.6 N, 90.0 W,
138 m, against the twenty member `osse-control-25km` ensemble:

| covariance | temperature | salinity | height | ocean area moved by 5% of the peak |
|---|---|---|---|---|
| static B | 1.396 degC | 0 psu | 0.040 m | 1.3% |
| ensemble, localized | 3.213 degC | 0.178 psu | 0.188 m | 1.8% |
| hybrid, 0.5 and 0.5 | 2.304 degC | 0.089 psu | 0.114 m | 1.6% |
| ensemble, unlocalized | 3.207 degC | 0.178 psu | 0.188 m | 65.7% |
| ensemble, localization group with no `variables:` | 3.207 degC | 0 psu | 0 m | 0% |

The static B's salinity is exactly zero because `kst` is off, and its height is
the balance operator. The ensemble has no balance operator, so both of its other
columns are the sample covariance: at that point the ensemble's spread is 1.79
degC and its correlation with salinity is 0.78 and with sea surface height 0.90,
which is the subtropical underwater and a thermocline displacement, and is what
the increment is a picture of.

**The vertical is where the two covariances differ most**, and it is a
consequence of a decision rather than a fault. The static B's vertical
correlation is calibrated against the mixed layer, so the same dirac is down to
0.38 of its peak by 195 m and to zero by 400 m. The localization has no vertical
part at all (`vertical: strategy: duplicated` in `da/hybrid.yaml`, which is a
localization of one at every separation in the vertical), so the ensemble
component is at 0.66 of its peak at 371 m and still 0.09 at 1028 m: whatever
vertical structure the members have, an observation sees all of it. That is the
honest starting point recorded in the layer, and it is the first thing to
measure if a hybrid analysis turns out to be moving the deep ocean.

**Velocity is in the EnVar's and the hybrid's analysis variables and not in the pure
variational one's**, which is `da/hybrid` restating both variable lists. The ensemble carries a
sampled covariance between the flow and everything else, so those two can correct velocity
directly; the static B cannot, since `BalanceSOCA` has no velocity row and
`SOCAParametricOceanStdDev` builds no velocity standard deviation. The asymmetry costs nothing
and needs no repair. There are no velocity observations, so Jo has no velocity sensitivity, the
gradient in those control components is zero, and the minimizer never moves them through the
static half; the static central block simply has no group naming them, and
`saber::DiffusionImpl` leaves a variable no group names alone. The list is `da/letkf`'s to the
entry, so the three solvers analyse the same variables and their velocity scores are
comparable.

A velocity increment is the one field that can be wrong in a way no picture of it shows.
SOCA carries `u` and `v` on the tracer array and reads a symmetric-memory restart from the
tracer origin, so index `i` is the face on the *low* side of tracer `i`, and
`ackbar.gridspec` moved the gridspec's `lonu` and `mask2du` to match; a velocity increment
displaced by one cell is smooth, localized, the right size and written into faces the model
treats as land. `tools/dirac-uv-page.py` is what settles it, and it does not settle it with a
picture: an ensemble covariance applied to a dirac is exactly
`L . sum_m X'_m . X'_m,T(dirac) / (N-1)`, with `L` the localization increment the same run
writes, every term of it computable from the member restarts, and the slice taken out of the
symmetric velocity array is the hypothesis under test. On `gom_25km` at four coastal points the
low-side slice reproduces the increment to 1e-15 of its peak in nineteen of the twenty
variable and point pairs, and to 1e-11 in the twentieth, and the
high-side slice is wrong by 36 to 110 per cent of it. `site/monitor/dirac-velocity/` is the
page.

The last row is the configuration `8b71c18` fixed, and it is not "unlocalized":
a localization over an empty variable list is the identity, which collapses
`sum_m X_m . L(X_m . dx)` to the ensemble variance times the input. One cell, one
level, one variable. Every EnVar and hybrid result produced before that commit
had a diagonal ensemble component.

## Iteration count

`normalization iterations` in `config/static/diffusion.yaml` is the number of
randomizations the horizontal normalization is estimated from, and its error
falls as one over the square root of it. `tools/soca-diffusion.sh --iterations N`
overrides it downwards, and exists for one purpose: proving the stage runs, in
seconds rather than in minutes. **A calibration built that way is not one to
assimilate through.** Nothing downstream can tell the two apart, which is why
the generated documents are copied next to the output: they are the only record
of what a given `corr_hz.nc` was normalized with.

The dirac report is how to tell whether the count was high enough. Run it and
read the peak column.

## What is not calibrated

The vertical scales are computed by `ackbar.diffusion` from the density profile
of whichever restart the calibration was given, so they describe that state's
mixed layer. Not from the restart's own `MLD` field: MOM6's instantaneous `MLD`
carries the diurnal layer, so on a summer afternoon it reports a few metres and
the vertical correlation collapses to the floor over most of the domain. A cold
start's mixed layer is thinner than a spun up one, and the resulting B is
correspondingly tight in the vertical.

Calibrated once by default. `config/layers/da/corr_vt_cycled.yaml` rebuilds the
vertical every cycle from that cycle's own background and blends it into a
rolling average, which is what soca-science v2 did; what makes it optional rather
than required is that the mixed layer moves slowly compared to a DA cycle.

The standard deviations in the variational layer are mostly the bundle's
defaults rather than chosen values, and the sea surface height multiplier in
`config/static/diffusion.yaml` is soca-science v2's. Both are decisions waiting to
be made. The exception is sea surface temperature, which reads a field derived
for the domain by `tools/sst-bgerr.py`.

## The depth filter, and why it is off

`SOCABkgErrFilt` carries two mechanisms with one name. `efold_z` is a taper:
the background error decays with depth, which is what anyone would want. But
`ocean_depth_min` is not a taper, and reading `BkgErrFilt.cc` is the only way to
find that out:

```cpp
if (depth <= params.oceanDepthMin) continue;
```

The multiplier is left at zero, so wherever the total water column is shallower
than that value the background error is not reduced, it is **deleted**. At the
1000 m both sources use, that removes every continental shelf. On the Gulf
domains it is the Louisiana-Texas shelf, the West Florida shelf and Campeche
Bank, which between them are most of the domain's area and exactly where the
SST observations are densest. Those observations are read, evaluated, given a
departure, and then multiplied by an error of zero.

It is set to 0 here. soca-science had already moved the same way, though not
consistently: its hybrid configurations (`soca_3dhyb.yaml`, `soca_4dhyb.yaml`)
use 0 while the older `soca_3dvar.yaml` and the hat10 regional copy of it kept
1000.

What it was for is real. A sea surface height increment over a shelf is not
very meaningful, and neither is a temperature increment through a ten metre
water column. A hard cutoff on total depth is a blunt way to say so, and it says
it for every variable at once. Something better belongs here and belongs per
variable.
