#!/usr/bin/env python3
"""Write the correlation length scale fields the diffusion calibration reads.

    tools/diffusion-scales.py <gridspec> <restart> <config> <outdir>

Called by `tools/soca-diffusion.sh` and not otherwise useful on its own. It
writes one file per entry under `horizontal:` in the config, named
`scales_<entry>.nc`, plus `scales_vt.nc` if `vertical:` is present.

The files it writes are not the operator's parameters. They are the *input* to
the calibration: SOCA reads them as though they were model states, builds the
diffusion operator from them, estimates its normalization, and writes that. See
`tools/soca-diffusion.sh` for the shape of the whole stage.

Ported from soca-science v2's `tools/calc_scales.py`, which is where the science
in here comes from. Three things changed in the port and none of them are
choices about the physics:

  * it does every group in one pass over one grid, instead of being run once per
    group from a config file that repeats the grid paths three times,
  * the horizontal and the vertical scales go to separate files, because they
    are read by separate SOCA states and mixing them meant every reader carried
    a variable it ignored,
  * `np.NaN` became `np.nan`, which numpy 2 requires.

Why a python script and not `soca_setcorscales.x`, which exists and computes a
Rossby-based scale from the same gridspec: that application applies no smoothing
and its vertical scale is a single constant times the land mask, with no mixed
layer in it at all. v2 stopped using it for those two reasons. The smoothing in
particular is not cosmetic. A diffusion coefficient with a sharper gradient than
the field it correlates produces a kernel that is not the kernel anybody asked
for, most visibly along the shelf break where the Rossby radius collapses.
"""

import sys

import netCDF4
import numpy as np
import yaml
from scipy.ndimage import distance_transform_edt, gaussian_filter

#: What the scale fields are called inside the files this writes. They are MOM6
#: restart variable names rather than anything descriptive, because SOCA reads
#: these files with its ordinary state reader: the horizontal field arrives as
#: `sea_surface_height_above_geoid` and the vertical one as
#: `sea_water_potential_temperature`, which is what the calibration configuration
#: names in `model variable`. Nothing about either field is a height or a
#: temperature. See `config/model/mom6sis2/fields_metadata.yaml` for the mapping.
HZ_VARIABLE = "ave_ssh"
VT_VARIABLE = "Temp"

#: Layers thinner than this are MOM6's vanished bottom layers. They are excluded
#: from the mixed layer count and given a zero vertical scale, which is what
#: stops the operator diffusing through the sea floor.
THIN_LAYER = 0.01  # [m]


def main(argv):
    if len(argv) != 5:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    gridspec_path, restart_path, config_path, outdir = argv[1:5]

    with open(config_path) as stream:
        config = yaml.safe_load(stream)

    grid = read_gridspec(gridspec_path)

    # The scale over which the scales themselves are smoothed, in grid cells.
    # Zonally averaged, on the assumption that the Rossby radius varies with
    # latitude far more than with longitude, which is true everywhere the
    # smoothing matters and cheap where it does not.
    smoothing = smoothing_scale(grid)

    for name, spec in (config.get("horizontal") or {}).items():
        scales = horizontal_scales(grid, spec, smoothing)
        write(f"{outdir}/scales_{name}.nc", grid, hz=scales)
        report(f"{name}", scales, grid["mask"], "m")

    vertical = config.get("vertical")
    if vertical:
        h, mld = read_restart(restart_path, grid, smoothing)
        scales = vertical_scales(h, mld, vertical)
        write(f"{outdir}/scales_vt.nc", grid, vt=scales)
        report("vt", scales[0], grid["mask"], "levels")


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


def read_restart(path, grid, smoothing):
    """Layer thicknesses, and a mixed layer depth smoothed like the scales.

    `MLD` is a diagnostic MOM6 carries in its restart, so this is the mixed
    layer of whatever state the restart happens to be, not a climatology. On a
    cold start it is the mixed layer the initialization produced, which is
    thinner than a spun up one and gives a correspondingly tighter vertical
    correlation. That is a reason to recalibrate against a spun up background
    before believing an analysis, and not a reason for this to invent a number.
    """
    with netCDF4.Dataset(path) as src:
        if "MLD" not in src.variables:
            sys.exit(f"diffusion-scales: {path} has no MLD, so the vertical "
                     f"scales cannot be built from it")
        h = np.asarray(src.variables["h"][0])
        mld = np.asarray(src.variables["MLD"][0])

    if h.shape[1:] != grid["mask"].shape:
        sys.exit(f"diffusion-scales: the restart is {h.shape[1:]} and the "
                 f"gridspec is {grid['mask'].shape}. They are different grids.")

    # Fill the land with its nearest ocean value before smoothing, so that the
    # coast does not pull the mixed layer towards zero in the cells next to it.
    # `distance_transform_edt` with `return_indices` gives, for every land cell,
    # the index of the closest ocean cell.
    nearest = distance_transform_edt(~grid["mask"], return_distances=False,
                                     return_indices=True)
    filled = mld[tuple(nearest)]
    mld = np.stack([gaussian_filter(filled, sigma=s, mode="nearest")[j, :]
                    for j, s in enumerate(smoothing)])
    return h, np.where(grid["mask"], mld, 0.0)


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


if __name__ == "__main__":
    main(sys.argv)
