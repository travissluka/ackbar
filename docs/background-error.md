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

`soca-diffusion.sh` writes `hz.nc`, `hz_ssh.nc` and `vt.nc` into
`$ACKBAR_STATIC_ROOT/static/<domain>/diffusion`, which is what the `filepath`
entries in the variational layer name. Until it has run, `ackbar validate`
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
in `config/diffusion.yaml`, which is not per domain: everything in it is
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

## Reading it back

The analysis reads what the calibration wrote, and two things about the read
have to match the write or the result is wrong without being visibly wrong.

The **vertical scheme** is one. The vertical operator is implicit, a fixed count
of tridiagonal solves down each column, rather than explicit, whose iteration
count grows with the square of the scale in levels. `vt.nc` holds a
normalization computed with the implicit operator; read back through the
explicit one, every vertical increment is scaled by the ratio of two kernels.
`method` and `iterations` therefore appear in both `config/diffusion.yaml` and
the variational layer, and `tests/test_diffusion.py` exists to hold them
together. The horizontal is explicit, which is saber's default and is stated in
neither place.

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

## Iteration count

`normalization iterations` in `config/diffusion.yaml` is the number of
randomizations the horizontal normalization is estimated from, and its error
falls as one over the square root of it. `tools/soca-diffusion.sh --iterations N`
overrides it downwards, and exists for one purpose: proving the stage runs, in
seconds rather than in minutes. **A calibration built that way is not one to
assimilate through.** Nothing downstream can tell the two apart, which is why
the generated documents are copied next to the output: they are the only record
of what a given `hz.nc` was normalized with.

The dirac report is how to tell whether the count was high enough. Run it and
read the peak column.

## What is not calibrated

The vertical scales come from the `MLD` field of whichever restart the
calibration was given, so they describe that state's mixed layer. A cold start's
mixed layer is thinner than a spun up one, and the resulting B is correspondingly
tight in the vertical. soca-science v2 recalibrated the vertical every cycle,
which is the measured improvement to make here; what makes it optional rather
than required is that the mixed layer moves slowly compared to a DA cycle.

The standard deviations in the variational layer are the bundle's defaults, not
chosen values, and the sea surface height multiplier in `config/diffusion.yaml`
is soca-science v2's. Both are decisions waiting to be made rather than settings
that have been made.

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
