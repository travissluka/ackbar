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

So this generates observations *from the domain*. In both modes below the
observations sample a truth at plausible locations, with plausible errors, and
what differs is where the truth comes from.

**`--state`: one state plus a known anomaly.** The truth does not evolve; it is
valid at every cycle. The anomaly is the point, because sampling a state and
adding noise would produce departures that are pure noise, and an analysis
fitting noise reduces its own cost function exactly as one fitting signal does.
With a known anomaly, the increment can be compared against the thing it was
supposed to find, and it is written to `truth.nc` beside the archive so that it
can be. It is a sum of Gaussian bumps in the domain interior, warm and cool,
with the sea surface height signal being the steric height of the temperature
anomaly rather than an independent field, so the two platforms are not asking
for contradictory things.

Observations of a truth that is not going anywhere are a good test of an
analysis and are not a statement about a forecast system. This mode is what the
tier 3 archive is built with, and it stays because those tests are pinned to it.

    .venv/bin/python tools/obs-archive-osse.py --domain gom_25km \\
        --truth-run $ACKBAR_STATIC_ROOT/truth/gom_25km/osse-2015 \\
        --start 2015-07-14T00:00:00Z --length PT24H --count 21 \\
        --out $ACKBAR_STATIC_ROOT/obs/gom-osse-2015/2015

`--truth-run` is the real thing: a free run promoted by `tools/promote-truth.sh`,
sampled at **each observation's own time** rather than at the analysis time.
That distinction is the reason the truth run writes sub-window states at all.
An observation an hour before the analysis time compared against the
analysis-time truth carries an hour of model evolution as an error nobody
declared, and it is an error that grows with the window, so a 4D experiment
would be scored against a truth less four-dimensional than itself.

There is no anomaly in this mode and no `truth.nc`: the archive is the truth,
and what an analysis recovered is compared against it directly by `verify`.

Values, errors and locations are all fiction. Nothing here should be assimilated
in support of a claim about the ocean.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
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

#: A truth archive's state directories: `20150704T0000`, the instant the state
#: is valid at. Matched rather than parsed loosely so that a README or a stray
#: directory beside them is skipped instead of crashing the run.
_STAMP = re.compile(r"^\d{8}T\d{4}$")

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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--state", type=Path,
                        help="a restart set or a MOM.res.nc: the truth, before the anomaly")
    source.add_argument("--truth-run", type=Path,
                        help="a promoted truth archive, sampled at each observation's own time")
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
    args.out.mkdir(parents=True, exist_ok=True)

    if args.truth_run:
        truth = TruthRun(args.truth_run)
        print(f"obs-archive-osse: {truth}")
    else:
        truth = FixedTruth(grid, read_state(args.state), args.seed)
        write_truth(args.out / "truth.nc", grid, truth.anomaly, args.seed)

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
            # Draw order is load bearing: locations, then errors, then times,
            # all off one generator per cycle. The archive's guarantee is that
            # the same command reproduces the same files, so a change here is a
            # change to every archive ever built with this tool, including the
            # committed one tier 3 reads. Sampling happens after all three
            # rather than between them for exactly that reason.
            lon, lat = locate(grid, spec, rng)
            noise = rng.normal(0.0, spec["error"], size=lon.shape)
            offsets = rng.uniform(0.0, length.total_seconds(), size=lon.shape)

            when = [begin + timedelta(seconds=float(offset))
                    for offset in offsets]
            values = truth.sample(grid, spec["field"], lon, lat, when)
            path = target / f"{platform}.{begin.strftime('%Y%m%d%H')}.nc4"
            write_obs(path, spec, lon, lat, values + noise, begin, offsets)
            print(f"obs-archive-osse: {path} ({lon.size} locations)")
    return 0


# --- the two truths ----------------------------------------------------------

class FixedTruth:
    """One state plus a known anomaly, valid at every time.

    `sample` ignores the times it is given, which is the whole difference from
    `TruthRun` and is stated here rather than at the call site so that the
    caller does not have to know which one it holds.
    """

    def __init__(self, grid, state, seed):
        self.fields, self.anomaly = perturb(grid, state, seed)

    def sample(self, grid, field, lon, lat, when):
        return sample(grid, self.fields[field], lon, lat)


class TruthRun:
    """A promoted free run, sampled at the state nearest each observation.

    Nearest rather than interpolated between the two bracketing states, and the
    reason is the same one behind sampling the nearest *cell*: the interpolation
    an observation operator does is what is under test, and generating with an
    interpolation of ACKBAR's own would hide an error in it. What it costs is a
    representativeness error of at most half the slot cadence, which is declared
    here and is smaller than the observation errors below at any cadence worth
    running.

    States are read on demand and one is held, because observations arrive
    sorted into the two or three states a window spans and reading a restart set
    per observation would read the same file six hundred times.
    """

    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_dir():
            sys.exit(f"obs-archive-osse: {root} is not a directory. It is a "
                     f"truth archive, written by tools/promote-truth.sh.")
        self.times = sorted(
            datetime.strptime(path.name, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
            for path in self.root.iterdir()
            if path.is_dir() and _STAMP.match(path.name))
        if not self.times:
            sys.exit(f"obs-archive-osse: {root} holds no state directories "
                     f"named like 20150704T0000.")
        self._cached = (None, None)

    def __str__(self):
        cadence = "one state"
        if len(self.times) > 1:
            gaps = {(b - a).total_seconds()
                    for a, b in zip(self.times, self.times[1:])}
            cadence = (f"{len(self.times)} states every "
                       f"{min(gaps) / 3600:g}h" if len(gaps) == 1 else
                       f"{len(self.times)} states, irregular")
        return (f"truth run {self.root.name}: {cadence}, "
                f"{self.times[0]:%Y-%m-%d %H:%M} to {self.times[-1]:%Y-%m-%d %H:%M}")

    def nearest(self, when):
        return min(self.times, key=lambda moment: abs(moment - when))

    def state(self, when):
        if self._cached[0] != when:
            self._cached = (when,
                            read_state(self.root / when.strftime("%Y%m%dT%H%M")))
        return self._cached[1]

    def sample(self, grid, field, lon, lat, when):
        """Each observation against the truth state nearest its own time.

        An observation outside the archive is refused rather than clamped to
        the nearest end of it. A clamp would produce a file that looks like
        every other file and holds a state from a different day, which is
        exactly the class of error an OSSE cannot detect in its own output.
        """
        outside = [t for t in when
                   if t < self.times[0] or t > self.times[-1]]
        if outside:
            sys.exit(
                f"obs-archive-osse: {len(outside)} observation(s) fall outside "
                f"the truth archive, the first at {min(outside):%Y-%m-%d %H:%M}. "
                f"It covers {self.times[0]:%Y-%m-%d %H:%M} to "
                f"{self.times[-1]:%Y-%m-%d %H:%M}. Move --start, shorten "
                f"--count, or promote more of the truth run.")

        groups = {}
        for index, moment in enumerate(when):
            groups.setdefault(self.nearest(moment), []).append(index)

        values = np.empty(lon.shape)
        for moment, indices in sorted(groups.items()):
            picked = np.array(indices)
            values[picked] = sample(grid, self.state(moment)[field],
                                    lon[picked], lat[picked])
        return values


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
