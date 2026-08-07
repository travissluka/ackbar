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
import zlib
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
#: where its observations fall.
#:
#: **`orbit` is the real layout and the two below it are not.** An `orbit`
#: platform is placed by propagating an actual repeating ground track, so its
#: observations land where that mission's observations land and carry the time
#: the satellite was there. `swath` scatters points over the domain and `track`
#: draws random straight lines across it, both with times drawn uniformly over
#: the assimilation window, and both are kept for one reason: the tier 3
#: observation archive is committed, was built with them, and its tests are
#: pinned to what it holds. Nothing new should use them. `docs/observing-system.md`
#: says what the difference costs.
#:
#: The errors are representative rather than measured: about 0.5 K for an
#: infrared SST retrieval and a few centimetres for along-track altimetry, with
#: AltiKa the best of the four because Ka band has the smallest footprint. They
#: are what the analysis weights by, so they are not free, and they have to
#: agree with the `obs error` in `config/layers/obs/<platform>.yaml`.
PLATFORMS = {
    # -- the pinned ones, for the tier 3 archive only -------------------------
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

    # -- the four altimeters flying over the Gulf in mid-2015 -----------------
    #
    # `repeat` and `revolutions` are the published pair and everything else
    # follows from them: the nodal period is one divided by the other and the
    # equatorial track spacing is 360 degrees over the revolution count. See
    # `Orbit` for the arithmetic and `docs/observing-system.md` for why these
    # four and not the ones whose names come to mind first.
    #
    # `ltan` is the local time of the ascending node in hours, and it sets the
    # phase and nothing else. For the sun synchronous platforms it is the real
    # crossing time, so a pass over the Gulf happens at the right time of day.
    # Jason-2 and CryoSat-2 are not sun synchronous and have no crossing time to
    # be right about; theirs is an arbitrary number that fixes where in its
    # cycle the mission is on the first day, which is fiction either way.
    "adt_j2": {
        "variable": "absoluteDynamicTopography",
        "error": 0.04,          # [m]
        "layout": "orbit",
        "field": "ssh",
        "inclination": 66.04,   # [deg]
        "repeat": 9.9156,       # [days]
        "revolutions": 127,
        "ltan": 0.0,            # [h] arbitrary: not sun synchronous
        "along": 25.0,          # [km] between samples along track
    },
    "adt_saral": {
        "variable": "absoluteDynamicTopography",
        "error": 0.03,
        "layout": "orbit",
        "field": "ssh",
        "inclination": 98.55,
        "repeat": 35.0,
        "revolutions": 501,
        "ltan": 22.0,           # Envisat's orbit: 10:00 descending is 22:00 here
        "along": 25.0,
    },
    "adt_c2": {
        "variable": "absoluteDynamicTopography",
        "error": 0.05,
        "layout": "orbit",
        "field": "ssh",
        "inclination": 92.0,
        "repeat": 369.0,
        "revolutions": 5344,
        "ltan": 9.0,            # arbitrary: drifting, not sun synchronous
        "along": 25.0,
    },
    "adt_hy2a": {
        "variable": "absoluteDynamicTopography",
        "error": 0.06,
        "layout": "orbit",
        "field": "ssh",
        "inclination": 99.34,
        "repeat": 14.0,
        "revolutions": 193,
        "ltan": 18.0,           # 06:00 descending node
        "along": 25.0,
    },

    # -- the infrared radiometers --------------------------------------------
    #
    # `swath` is the full cross-track width, and it is wider than the domain:
    # most passes see the whole Gulf and only the ones whose ground track runs
    # near the edge clip it. `cross` and `along` are the sampling, at twice the
    # model grid rather than at the sensor's own kilometre or so, which is the
    # thinning an operational system does before assimilating anything.
    #
    # `clear` is the fraction of the swath an infrared retrieval survives. It is
    # applied as coherent holes hundreds of kilometres across that persist from
    # one cycle to the next, not as a random thinning; see `Cloud`.
    "sst_n19": {
        "variable": "seaSurfaceTemperature",
        "error": 0.5,           # [K]
        "layout": "orbit",
        "field": "sst",
        "inclination": 99.2,
        "repeat": 9.0,          # 127 revolutions in nine days, 102.0 min each
        "revolutions": 127,
        "ltan": 14.5,           # drifted later than the 13:45 it launched into
        "swath": 2900.0,        # [km]
        "along": 50.0,          # [km]
        "cross": 50.0,          # [km]
        "clear": 0.5,
    },
    "sst_metopb": {
        "variable": "seaSurfaceTemperature",
        "error": 0.5,
        "layout": "orbit",
        "field": "sst",
        "inclination": 98.7,
        "repeat": 29.0,
        "revolutions": 412,
        "ltan": 21.5,           # 09:30 descending is 21:30 ascending
        "swath": 2900.0,
        "along": 50.0,
        "cross": 50.0,
        "clear": 0.5,
    },
    "sst_npp": {
        "variable": "seaSurfaceTemperature",
        "error": 0.4,           # VIIRS is the better instrument of the two
        "layout": "orbit",
        "field": "sst",
        "inclination": 98.7,
        "repeat": 16.0,
        "revolutions": 227,
        "ltan": 13.5,
        "swath": 3060.0,
        "along": 50.0,
        "cross": 50.0,
        "clear": 0.5,
    },
}

#: The phase of every orbit is measured from here: revolution zero's ascending
#: node crossing. Fixed rather than derived from the first cycle, so that two
#: archives built over different periods place the same satellite in the same
#: place on the same day and can be compared.
EPOCH = datetime(2015, 1, 1, tzinfo=timezone.utc)

#: A truth archive's states: `20150704T0000.nc`, the instant the state is valid
#: at. Matched rather than parsed loosely so that the README, and the `restart/`
#: directory of complete restart sets beside them, are skipped instead of
#: crashing the run.
_STAMP = re.compile(r"^(\d{8}T\d{4})\.nc$")

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

    # One cloud field per platform, seeded by the platform's name, so that two
    # radiometers are not blind in the same places at the same times. They would
    # be correlated in reality, since they are looking through the same
    # atmosphere, and pretending otherwise makes the combined network better
    # than it is. Independent is the honest simplification here: correlated
    # would need a real cloud field, and identical would be worse than either.
    for platform in args.platforms:
        spec = PLATFORMS[platform]
        if "clear" in spec:
            # `crc32` and not `hash`, which is salted per process for strings
            # and would give a different cloud field on every run.
            spec["_cloud"] = Cloud(
                args.seed ^ zlib.crc32(platform.encode()), spec["clear"])

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
            # Draw order is load bearing: locations, then errors, all off one
            # generator per cycle. The archive's guarantee is that the same
            # command reproduces the same files, so a change here is a change to
            # every archive ever built with this tool, including the committed
            # one tier 3 reads. Sampling happens after both rather than between
            # them for exactly that reason.
            lon, lat, when = observe(grid, spec, begin, begin + length, rng)
            path = target / f"{platform}.{begin.strftime('%Y%m%d%H')}.nc4"
            if lon.size == 0:
                # A satellite that did not pass over the domain this cycle. No
                # file, rather than a file with no locations in it: ACKBAR
                # already drops an observer whose window is missing from the
                # archive, and `ackbar validate` says so in as many words, while
                # an ioda file with a zero length `Location` dimension is
                # something the observer has to survive and nothing promises it
                # will. CryoSat-2 does this about once in forty five cycles,
                # which is what a 369 day repeat over a small domain looks like.
                print(f"obs-archive-osse: {platform} does not pass over the "
                      f"domain during {begin:%Y-%m-%d %H:%M}, no file written")
                continue

            noise = rng.normal(0.0, spec["error"], size=lon.shape)
            offsets = np.array([(moment - begin).total_seconds()
                                for moment in when])
            values = truth.sample(grid, spec["field"], lon, lat, when)
            write_obs(path, spec, lon, lat, values + noise, begin, offsets)
            print(f"obs-archive-osse: {path} ({lon.size} locations, "
                  f"{min(when):%H:%M} to {max(when):%H:%M})")
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
            datetime.strptime(match.group(1), "%Y%m%dT%H%M")
            .replace(tzinfo=timezone.utc)
            for match in (_STAMP.match(path.name) for path in self.root.iterdir())
            if match)
        if not self.times:
            sys.exit(f"obs-archive-osse: {root} holds no states named like "
                     f"20150704T0000.nc. An archive promoted before the truth "
                     f"run recorded its own trajectory held a directory per "
                     f"state instead; re-run tools/promote-truth.sh.")
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
            self._cached = (
                when, read_state(self.root / when.strftime("%Y%m%dT%H%M.nc")))
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


#: The two layouts a truth state arrives in, as {field: (variable, index)}.
#:
#: A MOM6 restart set spells its fields the model's way and carries a leading
#: `Time` axis. An ackbar state record (`ackbar/post.py`) spells them the way
#: anything downstream would guess and has no time axis, because the file *is*
#: one time. Both are truth states and the caller should not have to know which
#: one it is holding: `--state` names an offline initial condition, which is
#: always a restart, and `--truth-run` names an archive, which since the truth
#: run began recording its own trajectory is always records.
LAYOUTS = {
    "restart": {"sst": ("Temp", (0, 0)), "ssh": ("ave_ssh", (0,))},
    "record": {"sst": ("temperature", (0,)), "ssh": ("sea_surface_height", ())},
}


def read_state(path):
    """Surface temperature and sea surface height, from a restart or a record."""
    path = Path(path)
    if path.is_dir():
        path = path / "MOM.res.nc"
    with netCDF4.Dataset(path) as data:
        layout = "restart" if "Temp" in data.variables else "record"
        if layout == "record" and "temperature" not in data.variables:
            sys.exit(f"obs-archive-osse: {path} holds neither a MOM6 restart's "
                     f"'Temp' nor a state record's 'temperature', so it is not "
                     f"a truth state this can read.")
        state = {}
        for field, (name, index) in LAYOUTS[layout].items():
            # Land is a fill value in a record and an arbitrary real number in a
            # restart, so it is carried through as NaN rather than as either. An
            # observation is only ever placed on `grid["open"]`, so a NaN
            # reaching an output file means the placement and the mask disagree,
            # and that is worth being loud about rather than writing 1e20 into
            # an ObsValue where it reads as an ordinary bad observation.
            state[field] = np.ma.filled(
                np.ma.masked_invalid(data.variables[name][index]).astype(float),
                np.nan)
        return state


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

#: Constants, in kilometres, seconds and degrees.
EARTH_RADIUS = 6378.137            # [km], equatorial
EARTH_MU = 398600.4418             # [km^3/s^2]
EARTH_J2 = 1.08263e-3
SIDEREAL_DAY = 86164.0905          # [s], one rotation against the stars


class Orbit:
    """A repeating ground track, from the two numbers a mission publishes.

    An exact repeat orbit is specified by how many days it takes to return to
    its own tracks and how many revolutions that is. Everything else follows,
    and deriving it rather than storing it is what keeps the platform table
    from accumulating numbers that can disagree with each other:

        nodal period        `repeat / revolutions`
        semi-major axis     Kepler's third law from the period
        nodal precession    the J2 secular rate at that axis and inclination
        track spacing       360 degrees over the revolution count

    The last one is the check worth doing by hand. Jason-2 is 127 revolutions,
    so 360/127 = 2.835 degrees, which at the equator is the 315 km AVISO
    publishes; SARAL is 501, so 0.719 degrees and 80 km against a published 75.
    Neither figure is in the table.

    **The node drift is snapped to an exact repeat.** The J2 rate is accurate to
    a few thousandths of a degree per revolution, which is nothing over one pass
    and is a quarter of a degree over a 35 day cycle: enough that the tracks
    would not close, and a ground track that does not close is not a repeat
    orbit, which is the entire property being modelled. So the drift computed
    from physics is replaced by the nearest value that divides 360 degrees a
    whole number of times over the cycle, and the physics is what chooses which
    whole number.

    The phase is fiction. Revolution zero is declared to cross the equator
    northward at `EPOCH`, at whatever longitude puts the sun where *ltan* says,
    and no two-line elements are read. See `docs/observing-system.md`.
    """

    def __init__(self, inclination, repeat, revolutions, ltan):
        self.inclination = np.radians(inclination)
        self.period = repeat * 86400.0 / revolutions
        self.axis = (EARTH_MU * (self.period / (2 * np.pi)) ** 2) ** (1 / 3)

        # The J2 secular drift of the ascending node, in degrees per second.
        # Positive for a retrograde orbit, which is why every sun synchronous
        # satellite is inclined past 90 degrees: that is what makes the node
        # keep up with the sun.
        mean_motion = 2 * np.pi / self.period
        precession = np.degrees(
            -1.5 * EARTH_J2 * (EARTH_RADIUS / self.axis) ** 2
            * mean_motion * np.cos(self.inclination))

        # How far the node moves between one crossing and the next: the drift
        # above, less the Earth turning underneath. Then snapped, as above.
        drift = (precession - 360.0 / SIDEREAL_DAY) * self.period
        turns = round(-revolutions * drift / 360.0)
        self.drift = -360.0 * turns / revolutions

        # Where revolution zero crosses. Local solar time is UTC plus the
        # longitude in hours, and `EPOCH` is midnight UTC, so the longitude
        # whose local time is *ltan* at that instant is fifteen degrees an hour.
        self.node = _wrap(15.0 * ltan)

    def at(self, when):
        """Sub-satellite (longitude, latitude) at each of *when*, in degrees.

        Circular orbit, spherical Earth, and the node held fixed within a
        revolution. The first two are worth about ten kilometres against a real
        ground track and the third about ten metres, all of which is well inside
        the fiction the phase already is.
        """
        seconds = np.array([(moment - EPOCH).total_seconds() for moment in when])
        revolution, into = np.divmod(seconds, self.period)

        # Argument of latitude: the angle travelled since the ascending node.
        angle = 2 * np.pi * into / self.period
        latitude = np.arcsin(np.sin(self.inclination) * np.sin(angle))
        # The longitude gained along the orbit, plus the node's own position,
        # less the rotation of the Earth during this revolution so far.
        along = np.degrees(np.arctan2(
            np.cos(self.inclination) * np.sin(angle), np.cos(angle)))
        longitude = (self.node + revolution * self.drift + along
                     - into * 360.0 / SIDEREAL_DAY)
        return _wrap(longitude), np.degrees(latitude)


def _wrap(degrees):
    """Longitude into [-180, 180)."""
    return (np.asarray(degrees) + 180.0) % 360.0 - 180.0


def _bearing(lon, lat):
    """The heading of a track at each point, in radians east of north.

    Taken forward from each point to the next, and the last point reuses the
    one before it. A track sampled every few tens of kilometres is straight at
    that scale, so the one repeated value is not worth a special case.
    """
    lon, lat = np.radians(lon), np.radians(lat)
    ahead = slice(1, None)
    behind = slice(0, -1)
    delta = lon[ahead] - lon[behind]
    heading = np.arctan2(
        np.sin(delta) * np.cos(lat[ahead]),
        np.cos(lat[behind]) * np.sin(lat[ahead])
        - np.sin(lat[behind]) * np.cos(lat[ahead]) * np.cos(delta))
    return np.append(heading, heading[-1] if heading.size else 0.0)


def _offset(lon, lat, bearing, distance):
    """The point *distance* km from each (lon, lat) along *bearing*.

    The spherical destination formula rather than a flat-earth offset, because
    a 2900 km swath is twenty-six degrees of arc and treating that as flat
    misplaces its edges by enough to matter at the domain boundary.
    """
    angular = np.asarray(distance) / EARTH_RADIUS
    phi, lam = np.radians(lat), np.radians(lon)
    out_phi = np.arcsin(np.sin(phi) * np.cos(angular)
                        + np.cos(phi) * np.sin(angular) * np.cos(bearing))
    out_lam = lam + np.arctan2(
        np.sin(bearing) * np.sin(angular) * np.cos(phi),
        np.cos(angular) - np.sin(phi) * np.sin(out_phi))
    return _wrap(np.degrees(out_lam)), np.degrees(out_phi)


class Cloud:
    """Where an infrared retrieval fails, as coherent holes that move.

    Cloud is the dominant limitation on infrared sea surface temperature and it
    is not a thinning. What makes it hard for an analysis is that it removes
    *regions* for *days*, so the background inside a hole ages while everything
    around it is corrected, and the analysis has to carry information across a
    boundary. Thinning at random removes the same fraction of observations and
    poses none of that: every gap is one grid cell wide and the next pass fills
    it.

    So: a random field smoothed to a few hundred kilometres, thresholded at the
    clear sky fraction, and translated westward at about seven metres a second,
    which is a plausible steering flow and is what makes a hole persist from one
    cycle to the next rather than being redrawn. The field is defined around the
    whole globe and indexed modularly, so the drift never runs off the end of it
    and the archive can be any length.

    Not a cloud model. It has the right length scale, the right persistence and
    the right coverage, and no relationship whatever to the weather the
    atmospheric forcing is imposing on the ocean below it.
    """

    #: Resolution of the field, its smoothing, and how fast it moves.
    #:
    #: The two that matter are the scale and the drift, and they matter
    #: *against each other*. A field smoothed to four degrees and moved five a
    #: day travels a bit over one correlation length between cycles, so a hole
    #: is recognisably the same hole tomorrow and is gone in three days, which
    #: is how a summer Gulf convective system behaves. The first attempt used
    #: two degrees and six a day, which is three correlation lengths a cycle:
    #: coherent within a pass and completely redrawn by the next one, which is
    #: half of the point and looks like the whole of it in a snapshot.
    #:
    #: The drift is westward because the flow that steers them is.
    STEP = 0.25          # [deg]
    SCALE = 4.0          # [deg], the smoothing radius
    DRIFT = -5.0         # [deg/day], westward, about six metres a second

    def __init__(self, seed, clear):
        from scipy.ndimage import gaussian_filter
        self.columns = int(round(360.0 / self.STEP))
        rows = int(round(180.0 / self.STEP))
        field = np.random.default_rng([seed, 0xC10D]).normal(size=(rows, self.columns))
        # Wrapped in longitude so the seam at the date line is not a permanent
        # feature of the cloud field, and reflected in latitude where there is
        # no wrap to be had.
        field = gaussian_filter(field, self.SCALE / self.STEP, mode=("reflect", "wrap"))
        self.field = field
        self.threshold = np.quantile(field, 1.0 - clear)

    def clear(self, lon, lat, when):
        """True where a retrieval succeeds."""
        days = np.array([(moment - EPOCH).total_seconds() / 86400.0
                         for moment in when])
        shifted = (np.asarray(lon) - self.DRIFT * days + 180.0) % 360.0
        column = np.floor(shifted / self.STEP).astype(int) % self.columns
        row = np.clip(np.floor((np.asarray(lat) + 90.0) / self.STEP).astype(int),
                      0, self.field.shape[0] - 1)
        return self.field[row, column] > self.threshold


def observe(grid, spec, begin, end, rng):
    """Where and when one platform observes, over one window.

    Returns (longitude, latitude, times). The three come back together because
    for a real satellite they are one thing: a pass is a place *and* an instant,
    and the times cannot be drawn afterwards.
    """
    if spec["layout"] == "orbit":
        return _pass(grid, spec, begin, end, rng)
    # The pinned layouts, whose times are drawn by the caller.
    if spec["layout"] == "swath":
        lon, lat = _swath(grid, spec["count"], rng)
    else:
        lon, lat = _tracks(grid, spec["count"], spec["spacing"], rng)
    seconds = rng.uniform(0.0, (end - begin).total_seconds(), size=lon.shape)
    return lon, lat, [begin + timedelta(seconds=float(s)) for s in seconds]


def _pass(grid, spec, begin, end, rng):
    """Every point a satellite observes inside the domain, during one window.

    The orbit is stepped at the platform's own along-track sampling, converted
    to a time step through the ground speed, so a point every 25 km means a
    point every three and a half seconds and the times are the satellite's.
    That is the whole difference from the pinned layouts: a pass crosses the
    Gulf in about two minutes and arrives as one snapshot, rather than as a
    scattering of observations spread across a 24 hour window and compared
    against a different model state each.
    """
    orbit = Orbit(spec["inclination"], spec["repeat"], spec["revolutions"],
                  spec["ltan"])
    # Ground speed, from the time to go once around a sphere of Earth's radius.
    speed = 2 * np.pi * EARTH_RADIUS / orbit.period          # [km/s]
    step = timedelta(seconds=spec["along"] / speed)

    ticks = int(np.ceil((end - begin) / step))
    when = [begin + index * step for index in range(ticks)]
    lon, lat = orbit.at(when)

    west, east = grid["lon"].min(), grid["lon"].max()
    south, north = grid["lat"].min(), grid["lat"].max()
    half = spec.get("swath", 0.0) / 2.0

    # Ground track points near enough that some part of the swath could reach
    # the domain. `half` is zero for an altimeter, which is nadir looking, so
    # this is the domain itself and the expansion below does nothing.
    margin = np.degrees(half / EARTH_RADIUS) + 1.0
    near = ((lon > west - margin) & (lon < east + margin)
            & (lat > south - margin) & (lat < north + margin))
    if not near.any():
        return np.array([]), np.array([]), []

    lon, lat = lon[near], lat[near]
    when = [moment for moment, keep in zip(when, near) if keep]

    if half > 0.0:
        bearing = _bearing(lon, lat) + np.pi / 2      # to the right of the track
        # One row of cross-track samples per offset, so the swath is built as
        # whole scan lines and every point keeps the time of the line it is on.
        offsets = np.arange(-half, half + spec["cross"] / 2, spec["cross"])
        rows = [_offset(lon, lat, bearing, distance) for distance in offsets]
        lon = np.concatenate([row[0] for row in rows])
        lat = np.concatenate([row[1] for row in rows])
        when = when * len(offsets)

    inside = ((lon >= west) & (lon <= east) & (lat >= south) & (lat <= north))
    lon, lat = lon[inside], lat[inside]
    when = [moment for moment, keep in zip(when, inside) if keep]
    if lon.size == 0:
        return lon, lat, when

    # Ocean, and not against the coast, which is the same test the scattered
    # layout applies by only ever drawing from `open` in the first place.
    wet = _nearest(grid, lon, lat, grid["open"]).astype(bool)
    lon, lat = lon[wet], lat[wet]
    when = [moment for moment, keep in zip(when, wet) if keep]

    if "clear" in spec and lon.size:
        seen = spec["_cloud"].clear(lon, lat, when)
        lon, lat = lon[seen], lat[seen]
        when = [moment for moment, keep in zip(when, seen) if keep]
    return lon, lat, when


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

    A tree over the grid, built once and cached on it. This was a brute force
    argmin per point, which was fine at the few hundred points a random scatter
    produced and is not: a 2900 km swath sampled every 50 km puts tens of
    thousands of points in the domain per pass, against tens of thousands of
    cells, and the product of those two is not a thing to loop over in Python
    once per platform per cycle.

    Distances are in degrees, on the plain (lon, lat) plane. Over one regional
    domain the meridional convergence is a few per cent and the nearest cell is
    the nearest cell either way.
    """
    if "_tree" not in grid:
        from scipy.spatial import cKDTree
        grid["_tree"] = cKDTree(
            np.column_stack([grid["lon"].ravel(), grid["lat"].ravel()]))
    _, index = grid["_tree"].query(np.column_stack([lon, lat]))
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
