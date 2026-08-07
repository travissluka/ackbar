#!/usr/bin/env python
"""Put the GODAS sea surface temperature background error on a domain's grid.

    tools/sst-bgerr.py gom_25km
    tools/sst-bgerr.py gom_25km --source /path/to/godas_sst_bgerr.nc --plot

**What this field is.** A static, global map of the standard deviation an
analysis should assume for sea surface temperature, on the quarter degree
tripolar grid the global system runs on. It is not a model output and not a
climatology of variance; it is the number GODAS was tuned with, and it survives
into SOCA because a parametric standard deviation needs somewhere to start.

**What SOCA does with it.** `SOCAParametricOceanStdDev` builds the temperature
standard deviation from the background's own vertical temperature gradient,
`dz * |dT/dz|`, which is the right shape in the thermocline and collapses to
nothing in a mixed layer, where the gradient is zero by definition. This field
is the floor that stops it collapsing:

    localMin = min + (sst - min) * exp(-depth / efold)
    stddev   = clamp(dz * |dT/dz|, localMin, max)

So the value here is the standard deviation at the surface, decaying to the
block's own `min` with depth. Without it the block takes a single number for the
whole domain, and every mixed layer in the domain is assigned the same error
whether it is over the Loop Current or over Campeche Bank.

**Why remap rather than point SOCA at the global file.** `readNcAndInterp` would
do it: it reads the whole file on rank zero, broadcasts it to every rank, and
builds a ten nearest neighbour interpolation onto the model grid. That is one
and a half million points held per rank to use the four thousand that are over
this domain, on every cycle of every experiment, and the ten neighbour average
smooths a quarter degree field across roughly two and a half degrees. Remapping
once, offline and onto the model's own centres, makes the interpolation the
identity and the file eight hundred times smaller.

The output keeps the source's layout, an unstructured point cloud of
`longitude`, `latitude` and `sst_bgerr` over `nlocs`, because that is what
`readNcAndInterp` reads. It is not the model grid's shape and should not be
made into one.
"""

import argparse
import os
import sys
from pathlib import Path

import netCDF4
import numpy as np
from scipy.spatial import cKDTree

STATIC = Path(os.environ.get("ACKBAR_STATIC_ROOT", "/data/ackbar/static"))

#: Where the global field was found on rancor. Stated as a default rather than
#: copied into the static stage: it is somebody else's file from somebody else's
#: system, and the provenance is worth keeping visible at the point of use.
SOURCE = Path("/data/OLD/work_old/mom6sis2_global/common/soca/godas_sst_bgerr.nc")

#: The variable name, in and out. Fixed rather than an option because
#: `config/layers/da/variational.yaml` names it on the reading side and the two
#: have to agree.
VARIABLE = "sst_bgerr"

#: The source's land fill. It carries no mask and no `_FillValue`, and land is
#: this value exactly: 44% of the global grid, every point tested in the Sahara,
#: the Amazon and Antarctica, and not one point in the central Pacific.
#:
#: **Dropping it is the difference between a coastal value and a fill.** Our
#: coast is a quarter degree of a regional grid and the source's is a quarter
#: degree of a global one, so the two disagree by a cell in places, and a
#: nearest neighbour search that can see land hands a shelf cell 0.5 K because
#: the nearest source point is a beach. That drew a visible rim of exactly the
#: fill value around the whole Gulf. SOCA's own `readNcAndInterp` does not do
#: this, and averages the fill into the ten nearest neighbours instead, which is
#: a second reason to remap offline rather than let it read the global file.
LAND = 0.5


def wrap(lon):
    """Longitude into [-180, 180).

    The source runs -300 to 60, which is the global tripolar grid's own
    convention, and the Gulf sits in both halves of that range unwrapped.
    """
    return (np.asarray(lon) + 180.0) % 360.0 - 180.0


def read_source(path):
    with netCDF4.Dataset(path) as f:
        return (wrap(f["longitude"][:]), np.asarray(f["latitude"][:]),
                np.asarray(f[VARIABLE][:]))


def read_grid(path):
    """The target cell centres and ocean mask, from a SOCA gridspec."""
    with netCDF4.Dataset(path) as f:
        return (wrap(f["lon"][0]), np.asarray(f["lat"][0]),
                np.asarray(f["mask2d"][0]) > 0)


def nearest(source, target):
    """Index of the source point nearest each target point.

    On the unit sphere rather than in degrees. A degree of longitude is not a
    degree of distance and the error from pretending otherwise grows with
    latitude, which for a source and target of the same resolution is the
    difference between picking the right cell and picking its neighbour.
    """
    def unit(lon, lat):
        lon, lat = np.deg2rad(lon), np.deg2rad(lat)
        return np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon),
                         np.sin(lat)], axis=-1)

    tree = cKDTree(unit(*source))
    return tree.query(unit(*target))[1]


def plot(lon, lat, values, wet, out):
    """The remapped field, ocean only.

    The file itself covers land, for the reason `main` gives, but the land
    values are the source's own fill of half a degree and drawing them puts a
    solid mid-range wash over half the figure that reads as data.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ocean = values[wet.ravel()]
    figure, axis = plt.subplots(figsize=(8.5, 5.4), constrained_layout=True)
    mesh = axis.pcolormesh(lon, lat, np.where(wet, values.reshape(lon.shape), np.nan),
                           cmap="magma_r", shading="nearest")
    axis.set_aspect(1.0 / np.cos(np.deg2rad(float(np.mean(lat)))))
    axis.set_facecolor("0.85")
    axis.set_title(f"GODAS sea surface temperature background error, {out.stem}\n"
                   f"ocean {ocean.min():.2f} to {ocean.max():.2f} K, "
                   f"median {np.median(ocean):.2f} K", fontsize=11)
    figure.colorbar(mesh, ax=axis, label="stddev [K]", shrink=0.9)
    figure.savefig(out, dpi=120)
    plt.close(figure)
    print(out)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain", help="a domain with a gridspec, e.g. gom_25km")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--gridspec", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--plot", type=Path, nargs="?", const=Path("."),
                        default=None, help="write a map beside the output")
    args = parser.parse_args()

    stage = STATIC / "static" / args.domain
    gridspec = args.gridspec or stage / "soca_gridspec.nc"
    out = args.out or stage / f"{VARIABLE}.nc"
    for path in (args.source, gridspec):
        if not path.exists():
            sys.exit(f"sst-bgerr: {path} does not exist")

    slon, slat, values = read_source(args.source)
    tlon, tlat, wet = read_grid(gridspec)
    print(f"source {values.size} points, {values.min():.3f} to {values.max():.3f} K")

    ocean_source = values != LAND
    print(f"  {(~ocean_source).sum()} at the land fill of {LAND} K, dropped")
    slon, slat, values = (slon[ocean_source], slat[ocean_source],
                          values[ocean_source])

    # Every target cell, wet and dry. The mask is read to report the ocean and
    # to draw it, not to restrict what is written: SOCA interpolates onto the
    # whole function space and masks afterwards, and a hole here would be filled
    # by whatever the nearest neighbour search reached instead.
    index = nearest((slon, slat), (tlon.ravel(), tlat.ravel()))
    picked = values[index]

    out.parent.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(out, "w") as f:
        f.createDimension("nlocs", picked.size)
        for name, data, units in (("longitude", tlon.ravel(), "degrees_east"),
                                  ("latitude", tlat.ravel(), "degrees_north"),
                                  (VARIABLE, picked, "degC")):
            variable = f.createVariable(name, "f4", ("nlocs",))
            variable.units = units
            variable[:] = data
        f[VARIABLE].long_name = "sea surface temperature background error stddev"
        f.source = str(args.source)
        f.comment = ("GODAS sea surface temperature background error, nearest "
                     "neighbour onto this domain's cell centres. Read by "
                     "SOCAParametricOceanStdDev as the surface floor on the "
                     "temperature standard deviation.")

    ocean = picked.reshape(tlon.shape)[wet]
    print(f"{out}\n  {picked.size} points, {wet.sum()} over ocean")
    print(f"  ocean {ocean.min():.3f} to {ocean.max():.3f} K, "
          f"median {np.median(ocean):.3f}, mean {ocean.mean():.3f}")

    if args.plot is not None:
        target = args.plot if args.plot.suffix == ".png" else \
            (args.plot / f"{VARIABLE}_{args.domain}.png")
        plot(tlon, tlat, picked, wet, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
