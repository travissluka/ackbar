"""The correlation length scale fields a diffusion calibration is built from.

SOCA reads these as though they were model states, builds a diffusion operator
from each, estimates the normalization that makes the operator a correlation,
and writes *that*. So nothing here is the operator: this is its input.

**In the package rather than beside the tool, because two callers need it.**
`tools/diffusion-scales.py` is the offline half, run once per domain by
`tools/soca-diffusion.sh`, and it builds every group. `ackbar.run`'s `b.corr_vt`
task is the per-cycle half, and it builds only the vertical, against the
background the cycle is about to assimilate into. They have to agree exactly:
the normalization in a `corr_vt.nc` is computed for the scale field it was built
from, and a scale field computed two different ways is a B that is wrong by a
factor nothing reports.

Ported from soca-science v2's `tools/calc_scales.py`, which is where the
science in here comes from. Three things changed in the port and none of them
are choices about the physics:

  * it does every group in one pass over one grid, instead of being run once per
    group from a config file that repeats the grid paths three times,
  * the horizontal and the vertical scales go to separate files, because they
    are read by separate SOCA states and mixing them meant every reader carried
    a variable it ignored,
  * `np.NaN` became `np.nan`, which numpy 2 requires.

Why this and not `soca_setcorscales.x`, which exists and computes a Rossby
based scale from the same gridspec: that application applies no smoothing and
its vertical scale is a single constant times the land mask, with no mixed
layer in it at all. v2 stopped using it for those two reasons. The smoothing in
particular is not cosmetic. A diffusion coefficient with a sharper gradient
than the field it correlates produces a kernel that is not the kernel anybody
asked for, most visibly along the shelf break where the Rossby radius
collapses.
"""

import sys

import netCDF4
import numpy as np
from scipy.ndimage import distance_transform_edt, gaussian_filter

HZ_VARIABLE = "ave_ssh"
VT_VARIABLE = "Temp"
#: Below this a layer is vanished, not thin, and is no level the analysis can
#: put anything into.
#:
#: **0.1 m and not 0.01, which was sitting exactly on MOM6's own squeeze
#: value.** Z* keeps every level everywhere and presses the ones under the sea
#: floor down to a minimum thickness, which comes out around a centimetre. At
#: 0.01 those survived the test, so a 10 m shelf column of five real 2 m layers
#: counted as sixteen levels deep, and `vertical_scales` clips a fully mixed
#: column to its own level count: the scale saturated at 16 instead of 5. It
#: showed up as isolated bright cells on the Campeche Bank and the Bahamas
#: shelf, correlating three times as far as their neighbours in water a
#: hundredth as deep.
#:
#: 0.1 m is an order of magnitude above the squeeze and an order below the
#: thinnest layer any of these grids nominally carries, so there is nothing
#: real between the two values to lose.
THIN_LAYER = 0.1  # [m]


def read_gridspec(path):
    with netCDF4.Dataset(path) as src:
        grid = {name: np.asarray(src.variables[name][0])
                for name in ("rossby_radius", "dx", "dy", "area")}
        grid["mask"] = np.asarray(src.variables["mask2d"][0]) > 0.0
        # The type the scale fields are written as. Taken from the file rather
        # than fixed, so that a gridspec written in single precision does not
        # silently produce a double precision scale file that SOCA then reads
        # into a single precision state.
        grid["dtype"] = src.variables["dx"].datatype
    return grid


#: The mixed layer criterion: a density difference, and the depth it is
#: measured from.
#:
#: 0.03 kg/m^3 is the de Boyer Montegut threshold and the 10 m reference depth
#: is the other half of the same convention. **The reference depth is the part
#: that matters here and it is why this is not measured from the surface.** A
#: summer afternoon has a diurnal warm layer a few metres thick sitting on top
#: of the mixed layer, and measured from the surface the criterion finds the
#: bottom of *that*. Starting at 10 m steps over it and finds the seasonal
#: pycnocline, which is the thing a background error wants to spread inside.
#:
#: **The reference depth is also a floor on the answer**, and that is a real
#: limitation rather than an oversight: nothing above it can be returned, so a
#: genuinely shallow mixed layer comes back as 10 m. On gom_25km in July that
#: binds on about 4 per cent of ocean cells. It is the price of the paragraph
#: above and it was measured before being accepted: at a 5 m reference the
#: domain median falls from 14 m to 7 and at 3 m to 4, which is the diurnal
#: layer coming back. The floor costs less than what removing it reintroduces,
#: and it costs it where the vertical correlation is near its own floor anyway.
#:
#: The way out is not a shallower reference. It is to calibrate against a state
#: that has no diurnal layer in it: pre-dawn, when the mixed layer is deepest
#: and best defined. A 12Z restart is about 06:00 local in the Gulf and 00Z,
#: which is what every experiment here is stamped on, is the worst hour of the
#: day for this. That is the first thing to try for `b.corr_vt`, which gets to
#: choose which sub-window state it reads.
MLD_THRESHOLD = 0.03   # [kg m-3]
MLD_REFERENCE = 10.0   # [m]

#: Wright (1997)'s equation of state, reduced range, at the surface.
#:
#: **A linear equation of state will not do, and this is the one place in this
#: file that is not obviously local.** A density threshold is a contrast, so
#: what matters is the thermal expansion coefficient, and that is not a
#: constant: it runs from about 5e-5 per degree in polar water to 3e-4 in the
#: tropics. A linear state equation with coefficients picked near one of those
#: places puts the threshold at the wrong depth in the other by a factor of
#: several, and it fails hardest at high latitude, where the mixed layer is
#: deep and matters most. The first version of this file carried 2.0e-4, which
#: is a subtropical number, and would have been wrong the day a domain outside
#: the subtropics ran.
#:
#: Wright is what MOM6 itself ships as `EQN_OF_STATE = "WRIGHT"`, so this is the
#: model's own density rather than a second opinion about it, and it is a
#: rational polynomial rather than a table: fifteen coefficients, vectorized,
#: no dependency. Accurate to about 0.1 kg/m^3 over the oceanographic range,
#: against a threshold of 0.03 that is applied to a *difference* of two
#: densities computed the same way, where that bias cancels.
#:
#: Referenced to the surface, `p = 0`, because the criterion is on potential
#: density: pressure through the top hundred metres would otherwise contribute
#: its own stratification and find a pycnocline in an isothermal column.
WRIGHT_A = (7.057924e-4, 3.480336e-7, -1.112733e-7)
WRIGHT_B = (5.790749e8, 3.516535e6, -4.002714e4, 2.084372e2,
            5.944068e5, -9.643486e3)
WRIGHT_C = (1.704853e5, 7.904722e2, -7.984422, 5.140652e-2,
            -2.302158e2, -3.079464)


def density(temperature, salinity):
    """Potential density referenced to the surface, in kg m-3. Wright (1997)."""
    a0, a1, a2 = WRIGHT_A
    b0, b1, b2, b3, b4, b5 = WRIGHT_B
    c0, c1, c2, c3, c4, c5 = WRIGHT_C
    t, s = temperature, salinity
    alpha0 = a0 + a1 * t + a2 * s
    p0 = (b0 + b1 * t + b2 * t**2 + b3 * t**3 + b4 * s + b5 * t * s)
    lam = (c0 + c1 * t + c2 * t**2 + c3 * t**3 + c4 * s + c5 * t * s)
    return p0 / (lam + alpha0 * p0)


def read_restart(path, grid, smoothing):
    """Layer thicknesses, and a mixed layer depth smoothed like the scales.

    **The mixed layer is computed from the restart's temperature and salinity,
    not read from its `MLD`.** MOM6 carries `MLD`, it is right there, and using
    it was wrong: its long name is "Instantaneous active mixing layer depth"
    and that is a different quantity. It is the layer being actively mixed at
    the instant the restart was written, so on a summer afternoon it is the
    diurnal warm layer and not the mixed layer under it.

    That is not a small error, and it is not particular to one domain: every
    ACKBAR restart is stamped on a synoptic hour, so somewhere in every basin a
    restart lands in the afternoon, and every summer hemisphere has a diurnal
    layer for it to find. Where it was caught, gom_25km on 2015-07-12 00Z,
    which is late afternoon at 90 W, the central profile is 29.15 C at 1 m and
    then flat at 28.4 C from 5 m to 20 m: a real 20 m mixed layer under half a
    degree of skin. MOM6 reported 3.1 m. Domain median was 2.6 m against 15.7 m
    by the criterion below, and with 2 m layers that put the vertical
    correlation on its 1.5 level floor over 62 per cent of the domain and most
    of every column. The vertical correlation was, in effect, switched
    off, and nothing reported it: the operator was correctly normalized for the
    scales it was given.

    So this is the depth at which density first exceeds its value at
    `MLD_REFERENCE` by `MLD_THRESHOLD`. Still the mixed layer of whatever state
    the restart happens to be, which is why recalibrating against a spun up
    background of the right season matters; it is now the mixed layer rather
    than the top few metres of it.
    """
    with netCDF4.Dataset(path) as src:
        missing = [name for name in ("h", "Temp", "Salt")
                   if name not in src.variables]
        if missing:
            sys.exit(f"diffusion-scales: {path} has no "
                     f"{' and no '.join(missing)}, so the mixed layer cannot "
                     f"be computed from it")
        h = np.asarray(src.variables["h"][0])
        temperature = np.asarray(src.variables["Temp"][0])
        salinity = np.asarray(src.variables["Salt"][0])

    if h.shape[1:] != grid["mask"].shape:
        sys.exit(f"diffusion-scales: the restart is {h.shape[1:]} and the "
                 f"gridspec is {grid['mask'].shape}. They are different grids.")

    mld = mixed_layer(h, temperature, salinity)

    # Fill the land with its nearest ocean value before smoothing, so that the
    # coast does not pull the mixed layer towards zero in the cells next to it.
    # `distance_transform_edt` with `return_indices` gives, for every land cell,
    # the index of the closest ocean cell.
    nearest = distance_transform_edt(~grid["mask"], return_distances=False,
                                     return_indices=True)
    filled = mld[tuple(nearest)]
    mld = np.stack([gaussian_filter(filled, sigma=s, mode="nearest")[j, :]
                    for j, s in enumerate(smoothing)])

    # The cap again, because the smoothing is what breaks it: it is a weighted
    # mean over a neighbourhood, so a shelf cell next to deep water comes back
    # with a mixed layer deeper than its own sea floor. 244 cells here. See
    # `mixed_layer`, which explains what that does downstream.
    mld = np.minimum(mld, np.sum(h, axis=0))
    return h, np.where(grid["mask"], mld, 0.0)


def mixed_layer(h, temperature, salinity):
    """Where density first exceeds its value at `MLD_REFERENCE`.

    Vectorized over the column, because the alternative is a python loop over
    every cell of every domain this is ever run on. `searchsorted` cannot be
    used: density is not monotonic in depth everywhere, and a column with an
    inversion in it would give an answer from the wrong side of it.

    A column entirely lighter than its threshold has no crossing, and the whole
    column is mixed: it gets the deepest level that is not a vanished layer,
    which is what "mixed to the bottom" means on a Z* grid.
    """
    depth = np.cumsum(h, axis=0) - h / 2.0
    sigma = density(temperature, salinity)

    # The level the reference depth falls in, per column, and the density there.
    # A column shallower than the reference depth uses its own deepest level,
    # which `argmin` gives for free and which is the only sensible answer: on a
    # shelf in five metres of water there is no 10 m to refer to.
    reference = np.argmin(np.abs(depth - MLD_REFERENCE), axis=0)
    y, x = np.indices(reference.shape)
    threshold = sigma[reference, y, x] + MLD_THRESHOLD

    # The shallowest level below the reference that is over the threshold.
    # Levels above the reference are excluded rather than merely unlikely: a
    # diurnal warm layer is lighter than the water under it, so a surface level
    # can sit above the threshold and would otherwise be found first.
    below = np.arange(sigma.shape[0])[:, None, None] >= reference[None]
    over = (sigma > threshold[None]) & below & (h > THIN_LAYER)

    deepest = np.maximum(np.sum(h > THIN_LAYER, axis=0) - 1, 0)
    crossed = over.any(axis=0)
    found = np.where(crossed, np.argmax(over, axis=0), deepest)

    # Interpolate between the level that crossed and the one above it, rather
    # than returning the crossing level's own midpoint. Near the surface the
    # levels are a couple of metres thick, so snapping quantizes the mixed
    # layer to that and steps the vertical scale by a whole level where the
    # real mixed layer deepens smoothly. Worth about a metre of the domain
    # median here and more on a coarser grid.
    above = np.maximum(found - 1, 0)
    lower, upper = sigma[found, y, x], sigma[above, y, x]
    deep, shallow = depth[found, y, x], depth[above, y, x]
    span = np.where(lower > upper, lower - upper, np.inf)
    fraction = np.clip((threshold - upper) / span, 0.0, 1.0)
    interpolated = shallow + fraction * (deep - shallow)

    # A column that never crossed is mixed to its own sea floor, and there is
    # nothing to interpolate towards.
    mld = np.where(crossed & (found > above), interpolated, deep)

    # **Never deeper than the water.** Not reachable above, where the deepest
    # level is the worst case, and very reachable after the caller smooths this
    # field: a Gaussian over a shelf break pulls a 30 m offshore mixed layer
    # onto a 10 m bank, and `vertical_scales` then saturates that column at its
    # own level count. On a Z* grid a 10 m column still holds a dozen levels,
    # so the result is isolated cells correlating their whole depth beside
    # neighbours correlating a fraction of theirs, which is exactly the sharp
    # gradient the smoothing exists to remove. Capped here so the cap survives
    # whatever the caller does afterwards, and again there.
    return np.minimum(mld, np.sum(h, axis=0))


def smoothing_scale(grid):
    """The smoothing width for each row, in grid cells.

    Derived from the Rossby radius itself rather than from a group's scaled
    version of it, so that every group is smoothed identically and two groups
    differing only by a multiplier stay proportional to each other.
    """
    cells = np.where(grid["mask"], grid["rossby_radius"] / np.sqrt(grid["area"]),
                     np.nan)
    with np.errstate(invalid="ignore"):
        scale = np.nanmean(cells, axis=1)
    # A row that is entirely land has no mean. Rows at the poles usually are,
    # and a nan sigma makes `gaussian_filter` return a field of nans over the
    # whole grid rather than failing anywhere near where the problem is.
    scale = fill_nan_rows(scale)
    scale[0] = scale[1]
    scale[-1] = scale[-2]
    return scale


def fill_nan_rows(scale):
    good = ~np.isnan(scale)
    if not good.any():
        sys.exit("diffusion-scales: the mask is empty, so this is all land")
    index = np.arange(len(scale))
    return np.interp(index, index[good], scale[good])


def horizontal_scales(grid, spec, smoothing):
    """Rossby radius, floored by the grid, capped, and smoothed by itself."""
    scales = grid["rossby_radius"] * float(spec["rossby mult"])
    floor = float(spec["min grid mult"])
    ceiling = float(spec["max"])
    scales = np.clip(scales, grid["dx"] * floor, ceiling)
    scales = np.clip(scales, grid["dy"] * floor, ceiling)

    # Smoothed row by row rather than cell by cell: `gaussian_filter` is
    # separable and takes one sigma for the whole field, so this runs it once per
    # distinct sigma and keeps the row that sigma belongs to. The alternative is
    # a two deep python loop over every cell.
    smoothed = np.stack([gaussian_filter(scales, sigma=s, mode="nearest")[j, :]
                         for j, s in enumerate(smoothing)])
    return np.where(grid["mask"], smoothed, 0.0)


def vertical_scales(h, mld, spec):
    """Levels in the mixed layer at the surface, tapering to `min` below it.

    The scale is in levels, and it is a function of depth: at the surface it is
    the number of levels the mixed layer spans, and it falls linearly to `min`
    at the level where the mixed layer ends. Below that the analysis correlates
    over `min` levels, which is what keeps an Argo profile from smearing across
    the thermocline.

    `max` is optional and is normally absent. It was a cost control for the
    explicit scheme, whose iteration count grows with the square of the scale,
    and the implicit scheme does not have that problem. Capping under implicit
    would be flattening a deep mixed layer's correlation for no reason.
    """
    nz = h.shape[0]
    vt_min = float(spec["min"])
    vt_max = float(spec["max"]) if "max" in spec else float(nz)

    # Depth of the middle of each layer, and how many layers there are before
    # the bottom. The vanished layers MOM6 keeps at the sea floor are not
    # levels the analysis can put anything in.
    depth = np.cumsum(h, axis=0) - h / 2.0
    levels_to_bottom = np.sum(h > THIN_LAYER, axis=0)

    # The last level whose midpoint is still inside the mixed layer, clipped so
    # that there is always a level below it to interpolate towards.
    inside = np.where(depth < mld[np.newaxis, :, :], 1, 0)
    inside[h <= THIN_LAYER] = 0
    last = np.clip(np.sum(inside, axis=0) - 1, 0, nz - 2)

    # Plus the fraction of the next layer the mixed layer reaches into, so that
    # a mixed layer that deepens smoothly gives a scale that deepens smoothly
    # rather than one that steps by a whole level.
    y, x = np.indices(last.shape)
    above = depth[last, y, x]
    below = depth[last + 1, y, x]
    span = np.where(below > above, below - above, 1.0)
    ml_levels = np.clip(last + (mld - above) / span, 1.0, levels_to_bottom)

    level = np.indices(h.shape)[0]
    scales = np.clip(ml_levels[np.newaxis, :, :] - level, vt_min, vt_max)
    return np.where(h > THIN_LAYER, scales, 0.0)


def write(path, grid, hz=None, vt=None):
    """One scale field, in a file SOCA's ordinary state reader can open.

    The dimension names are this script's own and nothing reads them: SOCA finds
    the field by variable name. What does matter is that the shape matches the
    geometry, which is why `read_restart` refuses a restart on a different grid.
    """
    with netCDF4.Dataset(path, "w") as dst:
        dst.createDimension("Time", None)
        dst.createDimension("y", grid["mask"].shape[0])
        dst.createDimension("x", grid["mask"].shape[1])
        dst.createVariable("Time", grid["dtype"], ("Time",))
        if hz is not None:
            dst.createVariable(HZ_VARIABLE, grid["dtype"], ("y", "x"))[:] = hz
        if vt is not None:
            dst.createDimension("z", vt.shape[0])
            dst.createVariable(VT_VARIABLE, grid["dtype"],
                               ("z", "y", "x"))[:] = vt


def read_vertical(path, grid):
    """A vertical scale field back off disk, from a file `write` produced.

    The offline stage's `scales_corr_vt.nc` and every cycle's own record are the same
    file in the same layout, which is what lets a cycled experiment seed itself
    from the domain's calibration and then carry its own forward.
    """
    with netCDF4.Dataset(path) as src:
        if VT_VARIABLE not in src.variables:
            raise OSError(f"{path} has no {VT_VARIABLE}, so it is not a "
                          f"vertical scale file")
        field = np.asarray(src.variables[VT_VARIABLE][:])
    if field.shape[1:] != grid["mask"].shape:
        raise OSError(f"{path} is {field.shape[1:]} and the gridspec is "
                      f"{grid['mask'].shape}. They are different grids.")
    return field


def blend(new, carried, memory):
    """`memory` of this cycle's scales and the rest of the one carried forward.

    **On the finished scales rather than on the mixed layer they came from.**
    Every source of cycle to cycle noise in this field arrives through a
    different intermediate: the mixed layer estimate, the layer thicknesses the
    free surface moves, and the analysis increment that the next background
    carries. Smoothing one of them leaves the others, and smoothing the output
    catches all three at once with one number to explain.

    **Before the normalization, not after.** `vtNorm` in the calibrated file is
    computed *from* these scales, so a blended normalization beside an
    unblended scale, or the reverse, is a background error wrong by a factor
    nothing reports. This runs on the calibration's input and the calibration
    then runs on what it produced.

    A cell that is land or a vanished layer in either field stays zero. The
    column depth moves with the free surface, so the deepest live level is not
    the same every cycle, and a weighted mean against a zero would taper the
    bottom level towards nothing over a run.
    """
    if new.shape != carried.shape:
        raise OSError(f"the carried scales are {carried.shape} and this "
                      f"cycle's are {new.shape}")
    live = (new > 0.0) & (carried > 0.0)
    return np.where(live, memory * new + (1.0 - memory) * carried, new)


def report(name, field, mask, units):
    """What was written, in the units it was written in.

    Printed rather than merely returned because this is the only place the
    numbers are legible. Everything downstream is a normalization coefficient,
    and a horizontal scale that came out as a hundred metres because a
    multiplier was mistyped is indistinguishable from a correct one by then.
    """
    ocean = field[mask]
    print(f"diffusion-scales: {name:8s} min {ocean.min():10.4g} "
          f"mean {ocean.mean():10.4g} max {ocean.max():10.4g}  [{units}]")
