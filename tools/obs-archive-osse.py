#!/usr/bin/env python
"""Build a synthetic observation archive that covers one domain.

    .venv/bin/python tools/obs-archive-osse.py --domain gom_25km \\
        --state $ACKBAR_STATIC_ROOT/ic/gom_25km/hycom-smoke/20150105T01 \\
        --start 2015-01-05T01:00:00Z --length PT12H --count 3 \\
        --out $ACKBAR_STATIC_ROOT/obs/gom-osse-smoke/2015

`obs-archive-smoke.py` retimes the real ioda files the SOCA bundle ships, which
works on a global domain and does nothing on a regional one: those observations
are scattered over the world ocean and essentially none of them land in the Gulf
of Mexico. A regional analysis therefore has nothing to assimilate, and it says
so only by running successfully and producing an increment of zero.

So this generates observations *from the domain*, and it is an OSSE in
miniature. A truth state is the state given by `--state` plus a known
perturbation; observations sample the truth at plausible locations and are
given plausible errors; the perturbation is written out beside the archive so
that what an analysis recovered can be compared against what was there to
recover.

**The perturbation is the point.** Sampling the state itself and adding noise
would produce departures that are pure noise, and an analysis fitting noise
reduces its own cost function exactly as an analysis fitting signal does. With a
known anomaly in the truth, the increment can be compared against the thing it
was supposed to find. It is a sum of Gaussian bumps in the domain interior, warm
and cool, with the sea surface height signal being the steric height of the
temperature anomaly rather than an independent field, so that the two platforms
are not asking for contradictory things.

**What this is not** is an OSSE truth run. The truth here does not evolve: it is
one state plus a fixed anomaly, valid at every cycle. Observations of it pull a
cycling experiment towards a state that is not going anywhere, which is a
perfectly good test of an analysis and is not a statement about a forecast
system. The real thing is a free run promoted to a truth run, sampled at each
cycle's own time; see phase 5 in `docs/build-order.md`.

Values, errors and locations are all fiction. Nothing here should be assimilated
in support of a claim about the ocean.
"""

import argparse
import sys
from pathlib import Path

import netCDF4
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ackbar.duration import parse_duration, parse_instant  # noqa: E402

#: Where the truth's anomaly comes from. `amplitude` is the peak surface
#: temperature anomaly and `bumps` how many of them; half are cool.
#:
#: `efold` is the depth the anomaly decays over, and it appears here only
#: because the sea surface height anomaly is derived from it: a temperature
#: anomaly of `amplitude` over `efold` metres of water raises the surface by
#: `expansion * amplitude * efold`, which for the values below is about 8 cm.
#: That keeps the ADT departures inside the `absolute threshold: 0.2` the
#: observer layer's background check applies, and it keeps the two platforms
#: consistent with each other, which matters because the balance operator
#: relates them and an analysis asked for contradictory things splits the
#: difference.
TRUTH = {
    "amplitude": 1.0,      # [K]
    "efold": 400.0,        # [m]
    "expansion": 2.0e-4,   # [1/K], thermal expansion of sea water
    "bumps": 4,
    "width": 0.25,         # as a fraction of the domain's smaller side
}

#: One entry per platform: the observer name, the ioda variable, the error, and
#: how its locations are laid out. `swath` scatters points; `track` draws
#: straight lines across the domain, which is what an altimeter does.
#:
#: The errors are representative rather than measured: 0.5 K is about what an
#: L3U infrared SST retrieval carries, and 5 cm about what along-track altimetry
#: does. They are the numbers the analysis weights by, so they are not free.
PLATFORMS = {
    "sst_noaa19": {
        "variable": "seaSurfaceTemperature",
        "error": 0.5,           # [K]
        "layout": "swath",
        "count": 600,
        "field": "sst",
    },
    "adt_3a": {
        "variable": "absoluteDynamicTopography",
        "error": 0.05,          # [m]
        "layout": "track",
        "count": 3,             # tracks per window
        "spacing": 0.15,        # [degrees] along track
        "field": "ssh",
    },
}

#: A cell is only usable if its whole neighbourhood is ocean. The observers
#: apply `Domain Check` on `GeoVaLs/sea_area_fraction` at 0.9, which is the
#: model's land mask interpolated to the observation, so a point one cell from
#: the coast interpolates to something under the threshold and is rejected. An
#: archive that is half rejected is not a bug, but it is a confusing way to
#: discover a real rejection later.
COAST = 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", required=True,
                        help="names the gridspec under $ACKBAR_STATIC_ROOT/static")
    parser.add_argument("--state", required=True, type=Path,
                        help="a restart set or a MOM.res.nc: the truth, before the anomaly")
    parser.add_argument("--gridspec", type=Path,
                        help="overrides the path derived from --domain")
    parser.add_argument("--start", required=True,
                        help="the first cycle's analysis time, ISO 8601")
    parser.add_argument("--length", required=True, help="cycle length, ISO 8601")
    parser.add_argument("--count", type=int, required=True, help="how many cycles")
    parser.add_argument("--out", required=True, type=Path,
                        help="the archive directory: obs/<source>/<period>")
    parser.add_argument("--platforms", nargs="*", default=sorted(PLATFORMS),
                        help=f"a subset of {sorted(PLATFORMS)}")
    parser.add_argument("--seed", type=int, default=0,
                        help="everything random here derives from this")
    args = parser.parse_args(argv)

    grid = read_grid(args.gridspec or gridspec_for(args.domain))
    state = read_state(args.state)
    truth, anomaly = perturb(grid, state, args.seed)

    args.out.mkdir(parents=True, exist_ok=True)
    write_truth(args.out / "truth.nc", grid, anomaly, args.seed)

    start = parse_instant(args.start)
    length = parse_duration(args.length)
    for index in range(args.count):
        analysis = start + index * length
        # The same window the workflow computes: centred on the analysis time
        # and as long as the cycle, so consecutive windows tile without gap.
        begin = analysis - length / 2
        target = args.out / analysis.strftime("%Y%m%d%H")
        target.mkdir(parents=True, exist_ok=True)
        # Per cycle, so that consecutive windows do not observe the same
        # points. An archive that samples one fixed set of locations lets an
        # analysis converge onto them and look better than it is.
        rng = np.random.default_rng([args.seed, index])
        for platform in args.platforms:
            spec = PLATFORMS[platform]
            lon, lat = locate(grid, spec, rng)
            values = sample(grid, truth[spec["field"]], lon, lat)
            noise = rng.normal(0.0, spec["error"], size=values.shape)
            path = target / f"{platform}.{begin.strftime('%Y%m%d%H')}.nc4"
            write_obs(path, spec, lon, lat, values + noise,
                      begin, rng.uniform(0.0, length.total_seconds(), size=lon.shape))
            print(f"obs-archive-osse: {path} ({lon.size} locations)")
    return 0


# --- the domain and the state ------------------------------------------------

def gridspec_for(domain):
    import os
    root = os.environ.get("ACKBAR_STATIC_ROOT")
    if not root:
        sys.exit("obs-archive-osse: ACKBAR_STATIC_ROOT is not set; "
                 "run `source site/activate.sh` or pass --gridspec")
    return Path(root) / "static" / domain / "soca_gridspec.nc"


def read_grid(path):
    if not Path(path).exists():
        sys.exit(f"obs-archive-osse: {path} does not exist. It is the domain's "
                 f"static stage, built by tools/soca-gridspec.sh.")
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        grid = {name: np.asarray(data.variables[name][0])
                for name in ("lon", "lat")}
        grid["mask"] = np.asarray(data.variables["mask2d"][0]) > 0.0

    # Ocean, and not next to the coast. `binary_erosion` is the neighbourhood
    # test written once rather than as four shifted comparisons.
    from scipy.ndimage import binary_erosion
    grid["open"] = binary_erosion(grid["mask"], iterations=COAST,
                                  border_value=0)
    if not grid["open"].any():
        sys.exit("obs-archive-osse: no ocean cell is more than one cell from "
                 "land, so there is nowhere to put an observation")
    return grid


def read_state(path):
    """Surface temperature and sea surface height, from a restart."""
    path = Path(path)
    if path.is_dir():
        path = path / "MOM.res.nc"
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return {"sst": np.asarray(data.variables["Temp"][0, 0]),
                "ssh": np.asarray(data.variables["ave_ssh"][0])}


def perturb(grid, state, seed):
    """The truth: the state plus a known anomaly. Returns (truth, anomaly)."""
    rng = np.random.default_rng(seed)
    lon, lat, open_ocean = grid["lon"], grid["lat"], grid["open"]

    # Bump centres, drawn from the open ocean cells so that no anomaly sits
    # mostly over land where nothing can observe it.
    candidates = np.argwhere(open_ocean)
    picked = candidates[rng.choice(len(candidates), TRUTH["bumps"], replace=False)]

    span = min(lon.max() - lon.min(), lat.max() - lat.min())
    width = TRUTH["width"] * span
    shape = np.zeros_like(lon)
    for index, (j, i) in enumerate(picked):
        # Alternating sign, so the domain mean anomaly is near zero and the
        # ADT operator's own offset removal has nothing to bite on.
        sign = 1.0 if index % 2 == 0 else -1.0
        distance = np.hypot(lon - lon[j, i], lat - lat[j, i])
        shape += sign * np.exp(-0.5 * (distance / width) ** 2)

    anomaly = {
        "sst": TRUTH["amplitude"] * shape,
        # Steric height of the same anomaly, rather than an independent field.
        "ssh": TRUTH["expansion"] * TRUTH["amplitude"] * TRUTH["efold"] * shape,
    }
    truth = {name: state[name] + anomaly[name] for name in anomaly}
    for name in anomaly:
        inside = anomaly[name][grid["open"]]
        print(f"obs-archive-osse: {name} anomaly min {inside.min():+.4g} "
              f"max {inside.max():+.4g}")
    return truth, anomaly


def write_truth(path, grid, anomaly, seed):
    """What the observations know that the background does not.

    Written because an OSSE whose truth is not recorded can only be scored
    against itself. With this, "did the analysis find the anomaly" is a
    correlation rather than an opinion.
    """
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("y", grid["lon"].shape[0])
        data.createDimension("x", grid["lon"].shape[1])
        for name, field in (("lon", grid["lon"]), ("lat", grid["lat"]),
                            ("sst", anomaly["sst"]), ("ssh", anomaly["ssh"])):
            data.createVariable(name, "f8", ("y", "x"))[:] = field
        data.createVariable("open", "i1", ("y", "x"))[:] = grid["open"]
        data.seed = seed
        for key, value in TRUTH.items():
            setattr(data, key, value)
    print(f"obs-archive-osse: {path}")


# --- where the observations are ----------------------------------------------

def locate(grid, spec, rng):
    """Observation positions for one platform and one window."""
    if spec["layout"] == "swath":
        return _swath(grid, spec["count"], rng)
    return _tracks(grid, spec["count"], spec["spacing"], rng)


def _swath(grid, count, rng):
    """Scattered points, one per drawn ocean cell, jittered inside it."""
    cells = np.argwhere(grid["open"])
    picked = cells[rng.choice(len(cells), min(count, len(cells)), replace=False)]
    j, i = picked[:, 0], picked[:, 1]
    # Half a cell of jitter, so the locations are not exactly the model's own
    # points and the interpolation the operator does is a real interpolation.
    step = _spacing(grid)
    return (grid["lon"][j, i] + rng.uniform(-0.5, 0.5, j.shape) * step[1],
            grid["lat"][j, i] + rng.uniform(-0.5, 0.5, j.shape) * step[0])


def _tracks(grid, count, spacing, rng):
    """Straight lines across the domain, sampled evenly, ocean points kept.

    An altimeter measures along a ground track, and the difference from a
    scatter is not cosmetic: a track gives long thin runs of correlated
    coverage with wide gaps between them, which is what makes the horizontal
    correlation length in B matter at all.
    """
    lon, lat = grid["lon"], grid["lat"]
    west, east = lon.min(), lon.max()
    south, north = lat.min(), lat.max()

    points = []
    for _ in range(count):
        # A line from a random point on the southern edge to a random point on
        # the northern one, which crosses the whole domain at an angle.
        start = (rng.uniform(west, east), south)
        end = (rng.uniform(west, east), north)
        steps = max(2, int(np.hypot(end[0] - start[0], north - south) / spacing))
        along = np.linspace(0.0, 1.0, steps)
        points.append((start[0] + along * (end[0] - start[0]),
                       start[1] + along * (north - south)))

    track_lon = np.concatenate([p[0] for p in points])
    track_lat = np.concatenate([p[1] for p in points])
    keep = _nearest(grid, track_lon, track_lat, grid["open"])
    return track_lon[keep], track_lat[keep]


def _spacing(grid):
    """Mean cell size in degrees, (dlat, dlon). Only used to jitter."""
    return (float(np.abs(np.diff(grid["lat"], axis=0)).mean()),
            float(np.abs(np.diff(grid["lon"], axis=1)).mean()))


def _nearest(grid, lon, lat, field):
    """*field* at the grid cell nearest each point.

    A brute-force search over the whole grid, which is fine at the sizes here
    (a few hundred points against a few thousand cells) and is one expression
    rather than an interpolator to configure. The archive is built once.
    """
    flat_lon = grid["lon"].ravel()
    flat_lat = grid["lat"].ravel()
    index = np.array([
        np.argmin((flat_lon - x) ** 2 + (flat_lat - y) ** 2)
        for x, y in zip(lon, lat)
    ])
    return field.ravel()[index]


def sample(grid, field, lon, lat):
    """The truth at each observation, from the nearest cell.

    Nearest rather than interpolated on purpose: the observation operator
    interpolates, and generating with the same interpolation would hide any
    error in it. The half-cell offset this introduces is a representativeness
    error, which is what an observation has anyway.
    """
    return _nearest(grid, lon, lat, field)


# --- the file ----------------------------------------------------------------

def write_obs(path, spec, lon, lat, values, begin, offsets):
    """One platform's file for one window, in the ioda layout SOCA reads."""
    variable = spec["variable"]
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Location", lon.size)
        data.createDimension("nvars", 1)
        data.createVariable("Location", "i4", ("Location",))[:] = np.arange(lon.size)
        data.createVariable("nvars", "f4", ("nvars",))[:] = [1.0]

        meta = data.createGroup("MetaData")
        # ioda stores time as an offset in seconds against an epoch named in
        # the units string, which is why retiming a file is an attribute edit.
        when = meta.createVariable("dateTime", "i8", ("Location",))
        when.units = f"seconds since {begin.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        when[:] = offsets.astype("i8")
        meta.createVariable("longitude", "f4", ("Location",))[:] = lon
        meta.createVariable("latitude", "f4", ("Location",))[:] = lat

        for group, payload in (("ObsValue", values),
                               ("ObsError", np.full(values.shape, spec["error"])),
                               ("PreQc", np.zeros(values.shape))):
            data.createGroup(group).createVariable(
                variable, "f4", ("Location",))[:] = payload

        data._ioda_layout = "ObsGroup"
        data._ioda_layout_version = 0
        data.converter = "ackbar tools/obs-archive-osse.py"
        data.platform = "synthetic"
    return path


if __name__ == "__main__":
    sys.exit(main())
