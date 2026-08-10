#!/usr/bin/env python
"""Build a synthetic observation archive that covers one domain.

    .venv/bin/python tools/obs-archive-osse.py --domain gom_25km \\
        --state $ACKBAR_STATIC_ROOT/ic/gom_25km/glorys-smoke/20150711T00 \\
        --start 2015-07-11T00:00:00Z --length PT12H --count 4 \\
        --platforms adt_3a sst_noaa19 \\
        --out $ACKBAR_STATIC_ROOT/obs/gom-osse-smoke/2015

`--platforms` is not optional under `--state`, and this example omitted it until
the profile platforms landed in the default set. A single state carries a two
dimensional anomaly, so there is nothing to sample down a column and the
subsurface platforms are refused; taking the default therefore fails before the
first file is written. Name the platforms the reading experiment inherits.

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

`--truth-run` is the real thing: a free run promoted by `tools/local/promote-truth.sh`,
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
    "sst_metopb": {
        "variable": "seaSurfaceTemperature",
        "error": 0.25,
        "layout": "orbit",
        "field": "sst",
        "inclination": 98.7,
        "repeat": 29.0,
        "revolutions": 412,
        "ltan": 21.5,           # 09:30 descending is 21:30 ascending
        "swath": 2900.0,
        "along": 25.0,
        "superob": 25.0,        # [km], the L3 grid it is delivered on
        "clear": 0.75,
    },
    "sst_npp": {
        "variable": "seaSurfaceTemperature",
        "error": 0.1,           # VIIRS is the better instrument of the two
        "layout": "orbit",
        "field": "sst",
        "inclination": 98.7,
        "repeat": 16.0,
        "revolutions": 227,
        "ltan": 13.5,
        "swath": 3060.0,
        "along": 25.0,
        "superob": 25.0,        # [km], the L3 grid it is delivered on
        "clear": 0.8,
    },

    # -- the surface drifter array --------------------------------------------
    #
    # Two platforms, one array: `Drifters` is built once and both of these hold
    # the same object, so a temperature report and a salinity report from the
    # same hull are at the same place at the same instant.
    #
    # **The only in situ observations in this archive, and they are surface
    # only.** That is what the Gulf actually had in 2015. Argo is thin here:
    # AOML calls the open Gulf chronically under-observed, and the twenty to
    # twenty five float arrays in the Loop Current are the UGOS campaign, from
    # 2021 and after. At Argo's design density the deep Gulf supports five to
    # eight floats on a ten day cycle, which is under one profile a day in the
    # whole basin. So the honest subsurface network for this period is nearly
    # empty, and nothing here pretends otherwise.
    #
    # `every` is the reporting interval after thinning. The array transmits
    # hourly over Iridium; an analysis is not given twenty four nearly
    # identical values a day from one hull, because consecutive reports from a
    # drifter are correlated through the same water and declaring them
    # independent would let thirty drifters outvote the satellites.
    #
    # The errors are representativeness rather than instrument. An SVP
    # thermistor is good to 0.05 K in a laboratory and the array is what the
    # satellite records are calibrated against; what is uncertain is what a
    # point measurement at 15 cm says about the mean of a 25 km cell whose top
    # layer is 2 m thick, across a diurnal cycle. Salinity is worse, because
    # the Gulf has a river plume in it and sub-grid salinity structure near one
    # is large.
    "drifter_sst": {
        "variable": "seaSurfaceTemperature",
        "error": 0.2,           # [K]
        "layout": "drifter",
        "field": "sst",
        "drifters": 25,
        "salinity fraction": 0.2,
        "every": timedelta(hours=6),
    },
    "drifter_sss": {
        "variable": "seaSurfaceSalinity",
        "error": 0.1,           # [psu]
        "layout": "drifter",
        "field": "sss",
        "salinity only": True,
        "drifters": 25,
        "salinity fraction": 0.2,
        "every": timedelta(hours=6),
    },

    # -- the L band radiometer ------------------------------------------------
    #
    # SMAP rather than SMOS. Both were flying in mid-2015, and SMAP is the
    # better of the two here: it launched in January 2015 with a real aperture
    # feed rather than SMOS's interferometric array, and it is markedly less
    # affected by radio frequency interference, which is what limits SMOS in
    # semi-enclosed basins with populated coasts. The Gulf is exactly that.
    #
    # **No `clear`.** L band sees through cloud, and that is the entire reason
    # this platform is worth having beside the infrared ones: it is the only
    # satellite in the network whose coverage does not collapse under a
    # hurricane. Giving it a cloud mask would delete the one thing it adds.
    #
    # `coast` is the land contamination instead. A 40 km footprint sitting near
    # a coast picks up land in its sidelobes, and the retrieval is discarded out
    # to about a hundred kilometres. In the Gulf that removes the whole shelf,
    # which is where the river plume is and where the salinity signal is
    # largest, so this is a real limitation of the instrument and not a
    # conservative choice here.
    "sss_smap": {
        "variable": "seaSurfaceSalinity",
        "error": 0.25,          # [psu] against Argo, 40 km, 8 day
        "layout": "orbit",
        "field": "sss",
        "inclination": 98.12,
        "repeat": 8.0,          # [days] exact repeat
        "revolutions": 117,
        "ltan": 18.0,           # 06:00 descending is 18:00 ascending
        "swath": 1000.0,
        "along": 40.0,
        "superob": 40.0,        # [km], the footprint rather than a grid choice
        "coast": 100.0,         # [km] of land contamination
    },

    # -- the subsurface float array -------------------------------------------
    #
    # Two platforms, one array, the way the drifters are: a float reports
    # temperature and salinity from the same cast.
    #
    # **This is the best observed period's network, not mid-2015's.** Asked for
    # deliberately. The Gulf in 2015 held perhaps five to eight floats on a ten
    # day cycle, under one profile a day in the whole basin, and an OSSE run
    # against that answers only how little can be done. The array here is the
    # UGOS era one: about twenty floats on a five day cycle, which is what was
    # actually flown over the Loop Current from 2021 on. So the question this
    # archive asks is what a well observed Gulf supports, and any skill number
    # from it is conditional on a network that did not exist in the year the
    # forcing came from. See `docs/observing-system.md`.
    #
    # `park` is where the float sits between casts and `profile` is the top of
    # its cast. Deployment needs water deeper than `floor`, which is why the
    # array lives in the deep basin and never on the shelf.
    "argo_t": {
        "variable": "waterPotentialTemperature",
        "error by depth": ((0, 0.30), (50, 0.60), (150, 0.80),
                           (300, 0.40), (600, 0.15), (1500, 0.05)),
        "layout": "profile",
        "field": "t",
        "floats": 20,
        "every": timedelta(days=5),
        "park": 1000.0,         # [m]
        "profile": 1500.0,      # [m], the deepest level of a cast
        "floor": 1600.0,        # [m] of water needed to deploy
    },
    "argo_s": {
        "variable": "salinity",
        "error by depth": ((0, 0.10), (50, 0.12), (150, 0.10),
                           (300, 0.05), (600, 0.02), (1500, 0.01)),
        "layout": "profile",
        "field": "s",
        "floats": 20,
        "every": timedelta(days=5),
        "park": 1000.0,
        "profile": 1500.0,
        "floor": 1600.0,
    },

    # -- the glider line ------------------------------------------------------
    #
    # Gliders are not floats with a different duty cycle. A float goes where the
    # water goes and gives isolated casts; a glider flies waypoints at about a
    # quarter of a metre a second and gives a *section*, a continuous slice
    # across whatever it is crossing. Over the Loop Current front that section
    # is the observation the front is actually in, and no amount of scattered
    # casts is the same measurement.
    #
    # The Gulf really does have this: the hurricane glider network flies the
    # basin every summer from July, which is the season this archive covers.
    # Five is the order of what is in the water at once.
    #
    # A glider dives to a thousand metres and back in about six hours, so a cast
    # every six hours is one per dive rather than a thinning of something
    # faster.
    "glider_t": {
        "variable": "waterPotentialTemperature",
        "error by depth": ((0, 0.30), (50, 0.60), (150, 0.80),
                           (300, 0.40), (600, 0.15), (1000, 0.10)),
        "layout": "glider",
        "field": "t",
        "gliders": 5,
        "every": timedelta(hours=6),
        "profile": 1000.0,      # [m]
        "floor": 1050.0,
        "speed": 0.25,          # [m s-1] through the water
    },
    "glider_s": {
        "variable": "salinity",
        "error by depth": ((0, 0.10), (50, 0.12), (150, 0.10),
                           (300, 0.05), (600, 0.02), (1000, 0.02)),
        "layout": "glider",
        "field": "s",
        "gliders": 5,
        "every": timedelta(hours=6),
        "profile": 1000.0,
        "floor": 1050.0,
        "speed": 0.25,
    },
}

#: The depths a cast reports, in metres. Close to the standard Argo set: fine
#: where the structure is and coarse where it is not, rather than uniform.
#:
#: The top four hundred metres get most of the levels because that is where the
#: Gulf's signal is: the mixed layer, the seasonal thermocline, and the
#: subtropical underwater salinity maximum around 150 to 200 m, which is the
#: feature that distinguishes Loop Current water from Gulf Common Water and is
#: therefore the one an analysis most needs to get right.
#:
#: A cast is truncated at the platform's own `profile` depth and wherever the
#: water runs out, so a glider reports the first part of this list and a float
#: reports more of it.
CAST = (5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 125, 150, 175, 200,
        250, 300, 350, 400, 500, 600, 700, 800, 900, 1000, 1200, 1400, 1500)

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


def _refuse_fixed_truth(platforms):
    """Every way a platform can be incompatible with `--state`, in one message.

    Two axes. The *layout*: a profile or a glider reads down a column and a
    drifter is advected by the truth's own velocities, none of which exists in
    one state. The *field*: `perturb` builds an anomaly for sea surface
    temperature and height and nothing else. Both say the same sentence and name
    the same fix, both are decided from the platform table rather than from a
    hardcoded list so adding a platform cannot skip the check, and both refuse
    before `truth.nc` is written.
    """
    refused = {
        name: (f"observes {PLATFORMS[name]['field']}, and a single state "
               f"carries an anomaly only for "
               f"{' and '.join(FixedTruth.FIELDS)}")
        for name in platforms
        if PLATFORMS[name]["field"] not in FixedTruth.FIELDS
    }
    refused.update({
        name: (f"is a {PLATFORMS[name]['layout']}, which a single state cannot "
               f"place: it needs the truth to evolve")
        for name in platforms
        if PLATFORMS[name]["layout"] in FixedTruth.UNSUPPORTED_LAYOUTS
    })
    if refused:
        detail = "\n".join(f"  {name} {why}" for name, why in sorted(refused.items()))
        sys.exit(f"obs-archive-osse: --state cannot produce these platforms:\n"
                 f"{detail}\nUse --truth-run against a promoted nature run, or "
                 f"drop them from --platforms.")


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

    # Everything decidable from the arguments alone, decided before a file is
    # opened. Which platforms exist and which of them a single state can produce
    # are both properties of the table above, so answering them needs no
    # gridspec, no restart and no built domain. Someone choosing a platform list
    # should get the answer on any machine, and should not be told to build a
    # domain in order to be told they picked a platform that cannot work.
    unknown = [name for name in args.platforms if name not in PLATFORMS]
    if unknown:
        sys.exit(f"obs-archive-osse: no such platform: {', '.join(unknown)}. "
                 f"It has {', '.join(sorted(PLATFORMS))}.")
    if args.state:
        _refuse_fixed_truth(args.platforms)

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
        if "coast" in spec:
            spec["_water"] = coastal(grid, spec["coast"])
        if "clear" in spec:
            # `crc32` and not `hash`, which is salted per process for strings
            # and would give a different cloud field on every run.
            spec["_cloud"] = Cloud(
                args.seed ^ zlib.crc32(platform.encode()), spec["clear"])

    # Whether any platform looks below the surface, which decides how much of
    # each truth state is read. Three fields of fifty levels against five of
    # one, on every state in the run, so it is worth not reading.
    profiling = [name for name in args.platforms
                 if PLATFORMS[name]["layout"] in ("profile", "glider")]

    if args.truth_run:
        truth = TruthRun(args.truth_run, deep=bool(profiling))
        print(f"obs-archive-osse: {truth}")
    else:
        truth = FixedTruth(grid, read_state(args.state), args.seed)
        write_truth(args.out / "truth.nc", grid, truth.anomaly, args.seed)

    # One drifter array behind every drifter platform, because they are views of
    # the same hulls and not two deployments. Built here rather than lazily in
    # `observe` so that it exists before the first window and is stepped through
    # the whole run in order: a drifter's position at cycle 20 is the result of
    # nineteen cycles of advection, which is the entire reason it is simulated.
    drifting = [name for name in args.platforms
                if PLATFORMS[name]["layout"] == "drifter"]
    if drifting:
        array = Drifters(grid, PLATFORMS[drifting[0]], args.seed)
        for name in drifting:
            PLATFORMS[name]["_array"] = array
            PLATFORMS[name]["_truth"] = truth

    # The same argument for the two subsurface arrays: `argo_t` and `argo_s` are
    # one set of hulls, and a cast reports both from one rise.
    for layout, build in (("profile", Profilers), ("glider", Gliders)):
        members = [name for name in args.platforms
                   if PLATFORMS[name]["layout"] == layout]
        if members:
            shared = build(grid, PLATFORMS[members[0]], truth, args.seed)
            for name in members:
                PLATFORMS[name]["_array"] = shared
                PLATFORMS[name]["_truth"] = truth

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
            # Draw order is load bearing: locations and times inside `observe`,
            # then errors, all off one generator per cycle. A change anywhere in
            # that sequence shifts every draw after it, so the guarantee here is
            # narrow and worth stating exactly: **the same command at the same
            # commit reproduces the same files.** It is not that an archive
            # survives a change to this tool: any such change moves tier 3's
            # pinned numbers when the smoke archive is rebuilt, which is
            # expected rather than a bug to hunt elsewhere.
            lon, lat, when, depth = observe(grid, spec, begin, begin + length, rng)
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

            sigma = errors(spec, depth) * np.ones(lon.shape)
            noise = rng.normal(0.0, sigma, size=lon.shape)
            offsets = np.array([(moment - begin).total_seconds()
                                for moment in when])
            values = truth.sample(grid, spec["field"], lon, lat, when, depth)

            # A cast runs out of water before it runs out of levels, over any
            # column shallower than the platform's own profile depth. Those
            # levels are dropped rather than written: `sample_column` refuses to
            # extrapolate, and a NaN in an ObsValue is a value the analysis
            # would try to fit. Everything else that produces a NaN here is a
            # placement disagreeing with the land mask, which is a bug, so this
            # only ever forgives the depth axis.
            if depth is not None:
                keep = np.isfinite(values)
                if not keep.all():
                    lon, lat, values, sigma = (lon[keep], lat[keep],
                                               values[keep], sigma[keep])
                    noise, offsets = noise[keep], offsets[keep]
                    depth = depth[keep]
                    when = [w for w, k in zip(when, keep) if k]
            if lon.size == 0:
                print(f"obs-archive-osse: {platform} has no level in water "
                      f"during {begin:%Y-%m-%d %H:%M}, no file written")
                continue

            write_obs(path, spec, lon, lat, values + noise, begin, offsets,
                      depth=depth, sigma=sigma)
            extent = "" if depth is None else f", {depth.min():.0f}-{depth.max():.0f} m"
            print(f"obs-archive-osse: {path} ({lon.size} locations, "
                  f"{min(when):%H:%M} to {max(when):%H:%M}{extent})")
    return 0


# --- the two truths ----------------------------------------------------------

class FixedTruth:
    """One state plus a known anomaly, valid at every time.

    `sample` ignores the times it is given, which is the whole difference from
    `TruthRun` and is stated here rather than at the call site so that the
    caller does not have to know which one it holds.
    """

    #: The fields `perturb` builds an anomaly for, and therefore the only ones
    #: this can be asked about. Named here rather than discovered from the
    #: instance because `main` refuses an incompatible platform before it
    #: constructs one, and the refusal wants to say which fields exist.
    FIELDS = ("sst", "ssh")

    #: Layouts that need the truth to be a run rather than a state, whatever
    #: field they observe. A drifter is advected by the truth's own velocities
    #: and a profile or a glider reads down a column; neither is something a
    #: single state with a two dimensional anomaly on it can answer.
    UNSUPPORTED_LAYOUTS = ("drifter", "profile", "glider")

    def __init__(self, grid, state, seed):
        self.fields, self.anomaly = perturb(grid, state, seed)

    def sample(self, grid, field, lon, lat, when, depth=None):
        # `depth` is always None here and the signature says so anyway, because
        # the caller holds one of the two truths and does not know which. A
        # subsurface platform is refused in `main` before it reaches this, since
        # a single state carries a two dimensional anomaly and there is nothing
        # to sample down a column; the assertion is what turns a change to that
        # refusal into a failure here rather than a silent surface value
        # reported at 500 m.
        assert depth is None, "FixedTruth has no vertical structure"
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

    def __init__(self, root, deep=False):
        self.root = Path(root)
        self.deep = deep
        if not self.root.is_dir():
            sys.exit(f"obs-archive-osse: {root} is not a directory. It is a "
                     f"truth archive, written by tools/local/promote-truth.sh.")
        self.times = sorted(
            datetime.strptime(match.group(1), "%Y%m%dT%H%M")
            .replace(tzinfo=timezone.utc)
            for match in (_STAMP.match(path.name) for path in self.root.iterdir())
            if match)
        if not self.times:
            sys.exit(f"obs-archive-osse: {root} holds no states named like "
                     f"20150704T0000.nc. An archive promoted before the truth "
                     f"run recorded its own trajectory held a directory per "
                     f"state instead; re-run tools/local/promote-truth.sh.")
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
                when, read_state(self.root / when.strftime("%Y%m%dT%H%M.nc"),
                                 deep=self.deep))
        return self._cached[1]

    def sample(self, grid, field, lon, lat, when, depth=None):
        """Each observation against the truth state nearest its own time.

        An observation outside the archive is refused rather than clamped to
        the nearest end of it. A clamp would produce a file that looks like
        every other file and holds a state from a different day, which is
        exactly the class of error an OSSE cannot detect in its own output.

        *depth* is given for the platforms that observe below the surface, and
        is one depth per observation rather than one per profile: ioda stores a
        profile as its levels, each an ordinary location with its own depth, and
        that is the shape everything here already carries.
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
            state = self.state(moment)
            if depth is None:
                values[picked] = sample(grid, state[field],
                                        lon[picked], lat[picked])
            else:
                values[picked] = sample_column(
                    grid, state[field], state["z"],
                    lon[picked], lat[picked], depth[picked])
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
#: Which variable and which slice of it each field comes from, per file layout.
#:
#: `sss` and the two velocity components are read for the drifters: their
#: salinity is an observation and the velocity is what moves them. The velocity
#: lives on the staggered u and v grids, which are offset from the tracer grid
#: by half a cell. That offset is not corrected, and it does not need to be: it
#: displaces a drifter by six kilometres over a day at Gulf speeds, against a
#: drogue that is itself slipping and a trajectory nothing downstream verifies.
#: What the trajectory has to be is *plausible and divergent*, so that drifters
#: gather in convergences and get swept around the Loop Current the way real
#: ones do, rather than accurate.
LAYOUTS = {
    "restart": {"sst": ("Temp", (0, 0)), "ssh": ("ave_ssh", (0,)),
                "sss": ("Salt", (0, 0)), "u": ("u", (0, 0)), "v": ("v", (0, 0))},
    "record": {"sst": ("temperature", (0,)), "ssh": ("sea_surface_height", ()),
               "sss": ("salinity", (0,)), "u": ("eastward_velocity", (0,)),
               "v": ("northward_velocity", (0,))},
}

#: The same two layouts, for the platforms that observe below the surface. Read
#: only when one of those is in the platform set, because it is three fields of
#: fifty levels against five of one, and every state in the run is read.
#:
#: `h` is layer thickness and it is here because a MOM6 state has no depth axis
#: to interpolate onto. The vertical coordinate is Z* and the free surface moves
#: it, so the depth of level twenty is a property of the state rather than of
#: the grid, and it has to be read from the same file as the temperature it
#: places. See `depths`.
DEEP_LAYOUTS = {
    "restart": {"t": ("Temp", (0,)), "s": ("Salt", (0,)), "h": ("h", (0,))},
    "record": {"t": ("temperature", ()), "s": ("salinity", ()),
               "h": ("thickness", ())},
}


def read_state(path, deep=False):
    """The surface fields, and the full water column when *deep*."""
    path = Path(path)
    if path.is_dir():
        path = path / "MOM.res.nc"
    with netCDF4.Dataset(path) as data:
        layout = "restart" if "Temp" in data.variables else "record"
        if layout == "record" and "temperature" not in data.variables:
            sys.exit(f"obs-archive-osse: {path} holds neither a MOM6 restart's "
                     f"'Temp' nor a state record's 'temperature', so it is not "
                     f"a truth state this can read.")
        wanted = dict(LAYOUTS[layout])
        if deep:
            wanted.update(DEEP_LAYOUTS[layout])
        state = {}
        for field, (name, index) in wanted.items():
            if field in DEEP_LAYOUTS[layout] and name not in data.variables:
                sys.exit(f"obs-archive-osse: {path} has no {name!r}, so it "
                         f"cannot be profiled. A truth state promoted before "
                         f"the run recorded thickness carries the surface "
                         f"fields only; re-run tools/local/promote-truth.sh.")
            # Land is a fill value in a record and an arbitrary real number in a
            # restart, so it is carried through as NaN rather than as either. An
            # observation is only ever placed on `grid["open"]`, so a NaN
            # reaching an output file means the placement and the mask disagree,
            # and that is worth being loud about rather than writing 1e20 into
            # an ObsValue where it reads as an ordinary bad observation.
            state[field] = np.ma.filled(
                np.ma.masked_invalid(data.variables[name][index]).astype(float),
                np.nan)
        if deep:
            state["z"] = depths(state["h"])
        return state


def depths(thickness):
    """Depth of each layer's centre, from the layer thicknesses above it.

    A MOM6 state has no depth axis. Z* keeps the layers close to fixed levels in
    the interior and stretches them all with the free surface, so the depth of
    level twenty is a property of the state and not of the grid, and the only
    way to get it is to add up what is above.

    Depths are positive downward and measured from the instantaneous surface,
    which is what an instrument on a float measures against: its pressure sensor
    reads the water above it. That is a slightly different reference from the
    resting sea surface, by the sea surface height itself, and the difference is
    tens of centimetres against a top layer that is metres thick.
    """
    # NaN over land, and `nancumsum` would turn that into a plausible depth. The
    # column is left NaN so that a profile placed on land is loud rather than
    # deep. See the note on fill values in `read_state`.
    edges = np.cumsum(thickness, axis=0)
    return edges - thickness / 2.0


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
    #: *against each other*. The field travels a bit over one correlation
    #: length between cycles, so a hole is recognisably the same hole tomorrow
    #: and is gone in three days, which is how a summer Gulf convective system
    #: behaves. An early attempt used two degrees and six a day, which is three
    #: correlation lengths a cycle: coherent within a pass and completely
    #: redrawn by the next one, which is half of the point and looks like the
    #: whole of it in a snapshot.
    #:
    #: **Two and a half degrees rather than four, and the reason is the size of
    #: the basin.** At four degrees the Gulf held only a handful of independent
    #: cloud cells, so coverage over the domain was close to all or nothing:
    #: the delivered count ran from 21 observations in a cycle to 2246, around
    #: a long-run mean that was correct. A calibrated mean over a domain that
    #: spends whole cycles blind is not a calibrated cloud mask, and a blind
    #: cycle is not a harder analysis problem but an absent one. Two and a half
    #: degrees is still several times the correlation length of the background
    #: error, so a hole is still a region the analysis has to reach across,
    #: which is the property this class exists for.
    #:
    #: The drift is westward because the flow that steers them is, and it moves
    #: with the scale so that the one-length-per-cycle property is preserved.
    STEP = 0.25          # [deg]
    SCALE = 2.5          # [deg], the smoothing radius
    DRIFT = -3.0         # [deg/day], westward, about four metres a second

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

    Returns (longitude, latitude, times, depths). The first three come back
    together because for a real satellite they are one thing: a pass is a place
    *and* an instant, and the times cannot be drawn afterwards. *depths* is None
    for everything that observes the surface, and for the profiling platforms it
    is one depth per entry, a cast having already been flattened into its
    levels.
    """
    if spec["layout"] == "orbit":
        lon, lat, when = _pass(grid, spec, begin, end, rng)
        return lon, lat, when, None
    if spec["layout"] == "drifter":
        lon, lat, when, salty = spec["_array"].report(
            grid, spec["_truth"], begin, end, spec["every"])
        if spec.get("salinity only") and lon.size:
            # The hulls that carry conductivity, which is a minority of them.
            lon, lat = lon[salty], lat[salty]
            when = [moment for moment, keep in zip(when, salty) if keep]
        return lon, lat, when, None
    if spec["layout"] in ("profile", "glider"):
        return spec["_array"].report(grid, spec["_truth"], spec, begin, end)
    # The pinned layouts, whose times are drawn by the caller.
    if spec["layout"] == "swath":
        lon, lat = _swath(grid, spec["count"], rng)
    else:
        lon, lat = _tracks(grid, spec["count"], spec["spacing"], rng)
    seconds = rng.uniform(0.0, (end - begin).total_seconds(), size=lon.shape)
    return (lon, lat, [begin + timedelta(seconds=float(s)) for s in seconds],
            None)


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

    # A scanning radiometer is asked for its footprint, which needs `near` to
    # still index the untrimmed track: `_passes` cuts it into overflights and
    # slices these arrays with the result.
    if half > 0.0:
        return _footprint(grid, spec, near, lon, lat, when, half, rng)

    lon, lat = lon[near], lat[near]
    when = [moment for moment, keep in zip(when, near) if keep]

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


def _unit(lon, lat):
    """Points on the unit sphere, so distances can be compared without a map.

    A tree in (lon, lat) degrees would measure a degree of longitude as a
    degree of latitude, which at 25N is an error of ten per cent, and would
    have to be told what to do at the date line. Neither problem exists in
    three dimensions.
    """
    phi, theta = np.radians(np.asarray(lon)), np.radians(np.asarray(lat))
    return np.column_stack([np.cos(theta) * np.cos(phi),
                            np.cos(theta) * np.sin(phi),
                            np.sin(theta)])


def _passes(near):
    """Index ranges of the contiguous runs of *near*, one per overflight.

    A window holds two or three passes and they must not be merged: a cell can
    be seen twice in a day, at two different times, and treating the whole
    window as one track would give it one observation at whichever time
    happened to be nearest.
    """
    edges = np.diff(near.astype(int))
    starts = list(np.flatnonzero(edges == 1) + 1)
    stops = list(np.flatnonzero(edges == -1) + 1)
    if near[0]:
        starts.insert(0, 0)
    if near[-1]:
        stops.append(len(near))
    return list(zip(starts, stops))


def _footprint(grid, spec, near, lon, lat, when, half, rng):
    """The cells a scanning radiometer sees, one observation each, per pass.

    **This replaced a ray cast and the reason was an artifact.** The swath used
    to be built by firing a fan of cross-track great-circle rays from every
    along-track point, at a fixed spacing, out to the half swath width. Rays
    from successive track points are not parallel: the track curves, so the
    fans converge on the inside of the curve and diverge on the outside, and at
    fifteen hundred kilometres of range they cross. Where they cross, points
    pile up. The archive came out carrying rings of concentrated observations
    with clear ground between them, which is a caustic and not a scan pattern,
    and no amount of care in the interpolation afterwards could undo it because
    it was in the locations rather than in the values.

    So the footprint is asked for directly instead: which cells of the domain
    lie within the half swath width of this pass's ground track. One
    observation per cell, jittered inside it so the archive is not a lattice,
    carrying the time of the nearest point on the track. That is a superobbed
    L3 product, which is what an analysis is given in practice and is what
    justifies observation errors of a quarter of a degree rather than the
    instrument's own. It cannot produce a caustic, because the locations come
    from the domain and never from an intersection.
    """
    # Superob spacing, as a stride over the truth grid. The cells are the truth
    # run's, at 12 km, and one observation per cell would file the archive at
    # the resolution of the ocean it was sampled from rather than the one the
    # analysis runs at: four times more observations than the analysis grid can
    # hold distinct information about, every one of them correlated with its
    # neighbour through the same truth cell and declared independent. The
    # spacing an L3 product is delivered on is a property of the product, so it
    # is stated per platform rather than derived from the domain.
    stride = max(1, int(round(spec["superob"] / _kilometres(grid))))
    keep = np.zeros_like(grid["open"])
    keep[::stride, ::stride] = True
    # `_water` is the platform's own mask where it has one, which for the L band
    # radiometer is the open ocean minus a hundred kilometres of land
    # contamination. See `coastal` and the note on `sss_smap`.
    cells = np.argwhere(spec.get("_water", grid["open"]) & keep)
    if not len(cells):
        return np.array([]), np.array([]), []
    j, i = cells[:, 0], cells[:, 1]
    clon, clat = grid["lon"][j, i], grid["lat"][j, i]
    points = _unit(clon, clat)

    # Chord length subtending the half swath width, since the tree measures
    # straight lines through the sphere rather than arcs along it.
    reach = 2.0 * np.sin(half / (2.0 * EARTH_RADIUS))

    from scipy.spatial import cKDTree
    seen_lon, seen_lat, seen_when = [], [], []
    for start, stop in _passes(near):
        track = _unit(lon[start:stop], lat[start:stop])
        if not len(track):
            continue
        distance, index = cKDTree(track).query(points)
        inside = distance <= reach
        if not inside.any():
            continue
        moments = [when[start:stop][k] for k in index[inside]]
        seen_lon.append(clon[inside])
        seen_lat.append(clat[inside])
        seen_when.extend(moments)
    if not seen_lon:
        return np.array([]), np.array([]), []

    out_lon = np.concatenate(seen_lon)
    out_lat = np.concatenate(seen_lat)
    out_lon, out_lat = _jitter(grid, out_lon, out_lat, rng)

    if "clear" in spec and out_lon.size:
        clear = spec["_cloud"].clear(out_lon, out_lat, seen_when)
        out_lon, out_lat = out_lon[clear], out_lat[clear]
        seen_when = [m for m, keep in zip(seen_when, clear) if keep]
    return out_lon, out_lat, seen_when


class Drifters:
    """A Global Drifter Program array, advected by the truth's own currents.

    Every other platform here is placed by geometry: an orbit is where it is
    whatever the ocean does. A drifter is placed by the ocean, and that is the
    reason it is worth simulating rather than scattering. Real drifters are
    swept into convergences and wound around the Loop Current, so the array
    thins where the water diverges and piles up where it does not, and the
    places it stops sampling are exactly the places an analysis would like it
    to. Scattering points at random every cycle would produce the same count
    and none of that.

    So the positions are stepped with the truth's surface velocity, read at
    each drifter's own place and time. They are the only observations in this
    archive whose *locations* depend on the truth as well as their values,
    which also means a drifter cannot be placed without reading the state it
    will be sampled from.

    **Temperature on every drifter, salinity on a minority.** An SVP hull
    carries a thermistor at about 15 cm depth as standard; conductivity is an
    add-on and most of the array does not have it. Reporting both from every
    drifter would roughly quintuple the surface salinity observations an
    analysis sees, and salinity is the variable the network is otherwise
    thinnest in, so the mistake would flatter exactly the thing it is least
    entitled to.

    Drifters die: they ground on a shelf, they are picked up, they leave
    through the Straits of Florida. One that leaves the domain is replaced by a
    new deployment rather than clamped to the boundary, which keeps the array
    size steady and is what the program does.
    """

    #: Integration step for the trajectory. Well inside the time a drifter
    #: takes to cross a 12 km truth cell at Gulf speeds, so the path is
    #: resolved rather than jumped along.
    STEP = timedelta(hours=1)

    def __init__(self, grid, spec, seed):
        self.rng = np.random.default_rng([seed, 0xD819])
        self.count = spec["drifters"]
        self.salty = spec["salinity fraction"]
        self.lon, self.lat = self._deploy(grid, self.count)
        # Which hulls carry conductivity. Fixed for the life of a hull rather
        # than redrawn per report, because it is a property of the instrument.
        self.has_salinity = self.rng.random(self.count) < self.salty
        self.when = None
        self._cached = None

    def _deploy(self, grid, count):
        """*count* new drifters, in open water away from the coast."""
        cells = np.argwhere(grid["open"])
        picked = cells[self.rng.choice(len(cells), count, replace=True)]
        j, i = picked[:, 0], picked[:, 1]
        dlat, dlon = _spacing(grid)
        return (grid["lon"][j, i] + self.rng.uniform(-0.5, 0.5, count) * dlon,
                grid["lat"][j, i] + self.rng.uniform(-0.5, 0.5, count) * dlat)

    def advance(self, grid, truth, until):
        """Step every drifter forward to *until*, through the truth's currents."""
        if self.when is None:
            self.when = until
            return
        while self.when < until:
            step = min(self.STEP, until - self.when)
            state = truth.state(truth.nearest(self.when))
            east = _interpolate(grid, self.lon, self.lat, state["u"])
            north = _interpolate(grid, self.lon, self.lat, state["v"])
            # A hull sitting over land has no velocity to be moved by. It stays
            # put for this step and `_replace` retires it at the end of the
            # window, which is the same treatment as one that grounds.
            east = np.where(np.isfinite(east), east, 0.0)
            north = np.where(np.isfinite(north), north, 0.0)
            seconds = step.total_seconds()
            # Metres per second to degrees, with the longitude scaled by the
            # latitude the drifter is actually at.
            scale = 180.0 / (np.pi * EARTH_RADIUS * 1000.0)
            self.lat = self.lat + north * seconds * scale
            self.lon = self.lon + east * seconds * scale / np.maximum(
                np.cos(np.radians(self.lat)), 0.1)
            self.when += step
        self._replace(grid)

    def _replace(self, grid):
        """Redeploy any drifter that has run aground or left the domain."""
        wet = _nearest(grid, self.lon, self.lat, grid["open"]).astype(bool)
        inside = ((self.lon >= grid["lon"].min()) & (self.lon <= grid["lon"].max())
                  & (self.lat >= grid["lat"].min()) & (self.lat <= grid["lat"].max()))
        lost = ~(wet & inside)
        if lost.any():
            fresh_lon, fresh_lat = self._deploy(grid, int(lost.sum()))
            self.lon[lost], self.lat[lost] = fresh_lon, fresh_lat
            self.has_salinity[lost] = self.rng.random(int(lost.sum())) < self.salty

    def report(self, grid, truth, begin, end, every):
        """Positions and times of every report in the window.

        Returns (lon, lat, when, salinity), the last a boolean saying whether
        that report carries conductivity as well as temperature.

        **Cached on the window, because the array is shared and stateful.** The
        temperature platform and the salinity platform are two views of one set
        of hulls, and the caller reaches them one after the other. Advancing
        the trajectory twice would step the array through the window twice and
        file the salinity a day downstream of the temperature it was measured
        with, which is a thing no comparison would notice: both files would
        look entirely ordinary.
        """
        if self._cached and self._cached[0] == (begin, end, every):
            return self._cached[1]

        lon, lat, when, salty = [], [], [], []
        moment = begin
        while moment < end:
            self.advance(grid, truth, moment)
            lon.append(self.lon.copy())
            lat.append(self.lat.copy())
            when.extend([moment] * self.count)
            salty.append(self.has_salinity.copy())
            moment += every
        if not lon:
            answer = (np.array([]), np.array([]), [], np.array([], dtype=bool))
        else:
            answer = (np.concatenate(lon), np.concatenate(lat), when,
                      np.concatenate(salty))
        self._cached = ((begin, end, every), answer)
        return answer


class Profilers:
    """An array of profiling floats, drifting at depth between casts.

    A float is a drifter that spends nearly all of its time a kilometre down.
    It is advected there, not at the surface, and that matters for where the
    array ends up: the flow at a thousand metres is weaker than the surface
    flow and pointed differently, so a float does not get wound around the Loop
    Current the way a drifter does. It is stepped with a fraction of the
    surface velocity here, which is a simplification and is stated rather than
    hidden: the truth archive records the surface currents and not the whole
    velocity field, so a real parking-depth trajectory is not available to it.
    What the trajectory has to be is slow, divergent, and not the surface's,
    and this is all three.

    **The casts are what the platform is for, and they are staggered.** Every
    float carries its own phase, so twenty floats on a five day cycle surface
    about four times a day between them rather than all at once every fifth
    day. An array in phase would give an analysis a flood of profiles in one
    cycle and nothing in the next four, and the cycles either side of the flood
    would differ for a reason that has nothing to do with the ocean.

    Floats are deployed only where the water is deeper than `floor`, which
    keeps the array in the deep basin, and one that drifts onto the shelf or
    out of the domain is redeployed rather than clamped.
    """

    #: Integration step, and the same argument as `Drifters.STEP`: inside the
    #: time it takes to cross a truth cell at the speeds involved.
    STEP = timedelta(hours=3)

    #: Parking depth flow as a fraction of the surface flow. The Gulf's deep
    #: circulation is order a few centimetres a second against a surface Loop
    #: Current of over a metre, and the ratio is what keeps the array from
    #: being flushed through the Straits of Florida in a fortnight.
    DEEP = 0.05

    def __init__(self, grid, spec, truth, seed):
        self.rng = np.random.default_rng([seed, 0xA260])
        self.count = spec["floats"]
        self.every = spec["every"]
        self.deep = deep_enough(grid, truth, spec["floor"])
        self.lon, self.lat = self._deploy(grid, self.count)
        # Each float's own phase through the cycle, as a fraction of it. Drawn
        # once for the life of the hull, so a float keeps its slot.
        self.phase = self.rng.random(self.count)
        self.when = None
        self._cached = None

    def _deploy(self, grid, count):
        cells = np.argwhere(self.deep)
        if not len(cells):
            sys.exit("obs-archive-osse: no column in the domain is deep enough "
                     "for a profiling float. Lower the platform's 'floor'.")
        picked = cells[self.rng.choice(len(cells), count, replace=True)]
        j, i = picked[:, 0], picked[:, 1]
        dlat, dlon = _spacing(grid)
        return (grid["lon"][j, i] + self.rng.uniform(-0.5, 0.5, count) * dlon,
                grid["lat"][j, i] + self.rng.uniform(-0.5, 0.5, count) * dlat)

    def advance(self, grid, truth, until):
        if self.when is None:
            self.when = until
            return
        while self.when < until:
            step = min(self.STEP, until - self.when)
            state = truth.state(truth.nearest(self.when))
            east = _interpolate(grid, self.lon, self.lat, state["u"]) * self.DEEP
            north = _interpolate(grid, self.lon, self.lat, state["v"]) * self.DEEP
            east = np.where(np.isfinite(east), east, 0.0)
            north = np.where(np.isfinite(north), north, 0.0)
            seconds = step.total_seconds()
            scale = 180.0 / (np.pi * EARTH_RADIUS * 1000.0)
            self.lat = self.lat + north * seconds * scale
            self.lon = self.lon + east * seconds * scale / np.maximum(
                np.cos(np.radians(self.lat)), 0.1)
            self.when += step
        self._replace(grid)

    def _replace(self, grid):
        """Redeploy any float that has left the deep basin or the domain."""
        ok = _nearest(grid, self.lon, self.lat, self.deep).astype(bool)
        inside = ((self.lon >= grid["lon"].min()) & (self.lon <= grid["lon"].max())
                  & (self.lat >= grid["lat"].min()) & (self.lat <= grid["lat"].max()))
        lost = ~(ok & inside)
        if lost.any():
            fresh_lon, fresh_lat = self._deploy(grid, int(lost.sum()))
            self.lon[lost], self.lat[lost] = fresh_lon, fresh_lat
            self.phase[lost] = self.rng.random(int(lost.sum()))

    def report(self, grid, truth, spec, begin, end):
        """Every cast in the window, flattened to one entry per level.

        Cached on the window for the reason `Drifters.report` is: the
        temperature platform and the salinity platform are two views of one
        array, and stepping it twice would file a cast's salinity from a
        different day than its temperature.
        """
        if self._cached and self._cached[0] == (begin, end):
            return self._cached[1]

        period = self.every.total_seconds()
        lon, lat, when, depth = [], [], [], []
        # A cast is an instant here: a real float takes about six hours to rise
        # through fifteen hundred metres, and the archive samples the truth at
        # the nearest recorded state anyway, which is coarser than that.
        moment = begin
        step = timedelta(hours=1)
        while moment < end:
            self.advance(grid, truth, moment)
            # Whose cycle comes due in this hour. `since` is measured from the
            # epoch every other platform's phase is measured from, so two
            # archives built over different periods put a float in the same
            # part of its cycle on the same day.
            since = (moment - EPOCH).total_seconds()
            due = (np.floor((since / period) - self.phase)
                   != np.floor(((since - 3600.0) / period) - self.phase))
            for index in np.flatnonzero(due):
                levels = [z for z in CAST if z <= spec["profile"]]
                lon.extend([self.lon[index]] * len(levels))
                lat.extend([self.lat[index]] * len(levels))
                when.extend([moment] * len(levels))
                depth.extend(levels)
            moment += step

        answer = (np.array(lon), np.array(lat), when, np.array(depth, dtype=float))
        self._cached = ((begin, end), answer)
        return answer


class Gliders:
    """Gliders flying waypoints, giving sections rather than casts.

    The difference from a float is the whole point. A float goes where the
    water goes; a glider is steered, and it holds its line against the current
    at about a quarter of a metre a second through the water. What comes back
    is a continuous slice across whatever the line crosses, sampled every few
    kilometres, and over a front that slice is the measurement. A scattering of
    independent casts at the same total count is not the same observation, and
    it is the reason this is a separate layout rather than a fast float.

    The lines are fixed for the run and drawn once, each a great circle segment
    between two points in water deep enough to dive in. Real deployments fly
    repeat sections for exactly this reason: a line reoccupied is a line whose
    changes mean something. A glider that reaches the end of its line turns
    round and flies it back.

    Advected as well as steered: the along-line progress is the glider's own
    speed and the cross-line displacement is the current's, which is what makes
    a glider line in a strong current bow rather than stay straight.
    """

    def __init__(self, grid, spec, truth, seed):
        self.rng = np.random.default_rng([seed, 0x91D3])
        self.count = spec["gliders"]
        self.every = spec["every"]
        self.speed = spec["speed"]
        self.deep = deep_enough(grid, truth, spec["floor"])
        self.ends = self._lines(grid, self.count)
        # Position along each line as a fraction, and which way it is flying.
        self.along = self.rng.random(self.count)
        self.forward = np.ones(self.count)
        self.lon, self.lat = self._position()
        self.when = None
        self._cached = None

    def _lines(self, grid, count):
        """*count* line segments, both ends in water deep enough to dive in."""
        cells = np.argwhere(self.deep)
        if len(cells) < 2:
            sys.exit("obs-archive-osse: fewer than two columns in the domain "
                     "are deep enough to fly a glider line in.")
        ends = []
        for _ in range(count):
            picked = cells[self.rng.choice(len(cells), 2, replace=False)]
            (j0, i0), (j1, i1) = picked
            ends.append(((grid["lon"][j0, i0], grid["lat"][j0, i0]),
                         (grid["lon"][j1, i1], grid["lat"][j1, i1])))
        return ends

    def _position(self):
        """Where each glider is now, linearly along its own line."""
        lon = np.empty(self.count)
        lat = np.empty(self.count)
        for n, ((lon0, lat0), (lon1, lat1)) in enumerate(self.ends):
            lon[n] = lon0 + (lon1 - lon0) * self.along[n]
            lat[n] = lat0 + (lat1 - lat0) * self.along[n]
        return lon, lat

    def advance(self, grid, truth, until):
        if self.when is None:
            self.when = until
            return
        while self.when < until:
            step = min(timedelta(hours=1), until - self.when)
            seconds = step.total_seconds()
            for n, ((lon0, lat0), (lon1, lat1)) in enumerate(self.ends):
                span = _haversine(lon0, lat0, lon1, lat1)
                if span <= 0:
                    continue
                moved = self.speed * seconds / 1000.0 / span
                self.along[n] += moved * self.forward[n]
                # Turn at the end of the line and fly it back.
                if self.along[n] > 1.0:
                    self.along[n] = 2.0 - self.along[n]
                    self.forward[n] = -1.0
                elif self.along[n] < 0.0:
                    self.along[n] = -self.along[n]
                    self.forward[n] = 1.0
            self.lon, self.lat = self._position()
            self.when += step

    def report(self, grid, truth, spec, begin, end):
        if self._cached and self._cached[0] == (begin, end):
            return self._cached[1]

        levels = [z for z in CAST if z <= spec["profile"]]
        lon, lat, when, depth = [], [], [], []
        moment = begin
        while moment < end:
            self.advance(grid, truth, moment)
            for n in range(self.count):
                lon.extend([self.lon[n]] * len(levels))
                lat.extend([self.lat[n]] * len(levels))
                when.extend([moment] * len(levels))
                depth.extend(levels)
            moment += self.every

        answer = (np.array(lon), np.array(lat), when, np.array(depth, dtype=float))
        self._cached = ((begin, end), answer)
        return answer


def coastal(grid, kilometres):
    """Ocean at least *kilometres* from any land, as a mask on the truth grid.

    For the L band radiometer, whose 40 km footprint picks up land in its
    sidelobes and whose retrieval is discarded well before the coast is
    reached. In a basin the size of the Gulf that is not a detail: a hundred
    kilometres removes the entire shelf, and with it the river plume, which is
    where the salinity signal this instrument exists to measure is largest.

    **The edge of the array is not land**, which is what `border_value=1` says.
    The alternative is not a conservative choice, it is a second coastline drawn
    wherever the domain happens to have been cut: the Gulf grid ends in open
    water on its eastern and southeastern sides, at the Florida Straits and
    across the Caribbean, and eroding inward from there removed 989 water cells
    with no coast within a hundred kilometres of them, seven percent of the
    basin. The northern and western edges really are land, but they are land
    *inside* the mask, so the erosion still finds them.
    """
    from scipy.ndimage import binary_erosion
    cells = max(1, int(round(kilometres / _kilometres(grid))))
    return binary_erosion(grid["mask"], iterations=cells, border_value=1)


def deep_enough(grid, truth, floor):
    """Where the water column reaches *floor* metres, from one truth state.

    Read once from a single state rather than per cycle: the free surface moves
    the deepest layer centre by centimetres and this is a deployment mask
    thresholded at a kilometre and a half.

    Bathymetry rather than the land mask, because these platforms are the only
    ones in the archive that a shallow column is *wrong* for rather than merely
    unlikely. A float cannot park at a thousand metres over the Texas shelf.
    """
    if not hasattr(truth, "state"):
        sys.exit("obs-archive-osse: the profiling platforms need --truth-run. "
                 "A single --state has no trajectory for a float to drift "
                 "along and no second cast to compare the first against.")
    state = truth.state(truth.nearest(truth.times[0]))
    if "z" not in state:
        sys.exit("obs-archive-osse: the truth was opened without its water "
                 "column, which is a bug: --platforms includes a profiling "
                 "platform, so TruthRun should have been built with deep=True.")
    with np.errstate(invalid="ignore"):
        bottom = np.nanmax(np.where(np.isfinite(state["z"]), state["z"], -1.0),
                           axis=0)
    return grid["open"] & (bottom > floor)


def _haversine(lon0, lat0, lon1, lat1):
    """Great circle distance in kilometres."""
    phi0, phi1 = np.radians(lat0), np.radians(lat1)
    dphi = phi1 - phi0
    dlam = np.radians(lon1 - lon0)
    a = (np.sin(dphi / 2.0) ** 2
         + np.cos(phi0) * np.cos(phi1) * np.sin(dlam / 2.0) ** 2)
    return 2.0 * EARTH_RADIUS * np.arcsin(np.sqrt(a))


def _kilometres(grid):
    """The domain's grid spacing in kilometres, as one number.

    A median over the whole grid rather than a nominal figure, because the
    spacing is a property of the file and a domain regridded later would leave
    a constant here silently wrong. Longitude differences are scaled by the
    cosine of the latitude they sit at, without which a quarter degree at 25N
    reads ten per cent too wide.

    Distinct from `_spacing`, which answers in degrees and as a (dlat, dlon)
    pair and is what the jitter wants.
    """
    step = np.abs(np.diff(grid["lon"], axis=1)) * np.cos(
        np.radians(grid["lat"][:, :-1]))
    return float(np.median(step)) * np.pi * EARTH_RADIUS / 180.0


#: Jitter applied to a superob, as a fraction of one *truth* cell.
#:
#: Enough to break the lattice and no further. The job is only to stop an
#: archive whose locations are exactly a model's grid points, which would let
#: an observation operator that happens to collocate look better than one that
#: interpolates. Scattering across the whole superob cell instead, which is
#: twice as wide, does that job no better and moves the value away from the
#: water it is an average over: a superob is already a claim about a 25 km
#: cell, and displacing it a further 12 km adds a representativeness error on
#: top of the one it honestly has.
JITTER = 0.5


def _jitter(grid, lon, lat, rng):
    """A little scatter, so the archive is not the domain's own lattice."""
    dlat, dlon = _spacing(grid)
    return (lon + rng.uniform(-JITTER, JITTER, size=lon.shape) * dlon,
            lat + rng.uniform(-JITTER, JITTER, size=lat.shape) * dlat)


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


#: Cells combined per interpolated point, and the floor on a distance before it
#: is treated as a coincidence rather than divided by. The floor is in degrees
#: and is far below one cell of any domain here, so it fires only when an
#: observation lands exactly on a grid point.
NEIGHBOURS = 4
COINCIDENT = 1.0e-9


def _interpolate(grid, lon, lat, field):
    """*field* at each point, inverse distance weighted over `NEIGHBOURS` cells.

    Land is excluded from the average rather than blended into it. A cell under
    the coast holds whatever the model left there, and letting it contribute to
    an observation a few kilometres offshore is how a synthetic archive grows a
    cold bias along every coastline. Where all four neighbours are dry the
    nearest wet cell is used, which is the only answer available and is what
    the caller's own `open` test has already made rare.
    """
    if "_tree" not in grid:
        from scipy.spatial import cKDTree
        grid["_tree"] = cKDTree(
            np.column_stack([grid["lon"].ravel(), grid["lat"].ravel()]))

    points = np.column_stack([lon, lat])
    distance, index = grid["_tree"].query(points, k=NEIGHBOURS)
    values = field.ravel()[index]
    # Land is dry whether the mask says so or the value does. `read_state`
    # carries land through as NaN, and a NaN multiplied by a zero weight is
    # still a NaN, so excluding a cell by weighting it out is not enough: the
    # value has to leave the sum as well. Without this a drifter that passes
    # near the coast picks up a NaN velocity and its position becomes NaN,
    # which surfaces much later as an unrelated failure in the tree.
    usable = grid["mask"].ravel()[index] & np.isfinite(values)
    values = np.where(usable, values, 0.0)

    weight = usable.astype(float) / np.maximum(distance, COINCIDENT)
    total = weight.sum(axis=1)

    out = np.empty(len(points))
    any_wet = total > 0.0
    out[any_wet] = ((values * weight)[any_wet].sum(axis=1) / total[any_wet])
    # Every neighbour dry: fall back to the nearest cell of any kind, which is
    # what the whole function replaced and is right for exactly this case.
    out[~any_wet] = field.ravel()[index[~any_wet, 0]]
    return out


def sample(grid, field, lon, lat):
    """The truth at each observation, interpolated from the surrounding cells.

    **This used to take the nearest cell, and that was an aliasing bug.** The
    argument for nearest was that the observation operator interpolates and
    generating with an interpolation of ACKBAR's own would hide an error in it.
    That argument does not survive the two grids being different: the truth is
    at 12 km and the analysis at 25 km, so the operator's interpolation is over
    a different mesh and there is nothing to hide.

    What nearest actually did was quantize every observation onto the truth
    grid. A swath samples on its own regular lattice, and a regular lattice
    read through a nearest-cell lookup on a second regular lattice beats
    against it: the observations acquire a moire pattern with a wavelength set
    by the ratio of the two spacings, which is visible in a map of them and is
    a spatially correlated error nothing declares. It is worst exactly where
    the two spacings are close, which is where a well designed observing system
    puts them.

    Inverse distance over the four nearest cells, rather than bilinear in index
    space, because the model grid is curvilinear and there is no separable
    (i, j) to interpolate along. The half-cell offset nearest introduced was
    defended as a representativeness error, and a representativeness error is
    fine; a representativeness error organised into stripes is not.
    """
    return _interpolate(grid, lon, lat, field)


def sample_column(grid, field, z, lon, lat, depth):
    """The truth at each observation's own depth, for the profiling platforms.

    Horizontally first, then vertically, and in that order for a reason. Doing
    it the other way round means interpolating each of the four surrounding
    columns onto the target depth and then blending, which is the same thing
    only where the layers of those columns are at the same depths. Across the
    Loop Current front they are not, and blending four columns that have been
    resampled onto a common depth smears the front over the interpolation
    stencil. Blending the layers first keeps the level structure the model had.

    Linear in depth, and the vertical grid is fine enough that this is honest:
    the top of the water column is metres thick and an interpolation across two
    of those levels is not where the error in a profile comes from.

    Below the deepest layer centre the profile is refused rather than
    extrapolated. An extrapolated value at 2000 m sits in an ioda file looking
    exactly like a measured one, and it is a value the analysis will fit.
    """
    levels = field.shape[0]
    column = np.empty((levels, lon.size))
    axis = np.empty((levels, lon.size))
    for k in range(levels):
        column[k] = _interpolate(grid, lon, lat, field[k])
        axis[k] = _interpolate(grid, lon, lat, z[k])

    values = np.full(lon.size, np.nan)
    for n in range(lon.size):
        good = np.isfinite(column[:, n]) & np.isfinite(axis[:, n])
        if good.sum() < 2:
            continue
        # `np.interp` needs an increasing x and clamps outside its range rather
        # than saying so, so the range is checked here and the clamp never runs.
        deep, shallow = axis[good, n].max(), axis[good, n].min()
        if shallow <= depth[n] <= deep:
            values[n] = np.interp(depth[n], axis[good, n], column[good, n])
    return values


# --- the file ----------------------------------------------------------------

def errors(spec, depth):
    """The declared error of each observation.

    A scalar for the platforms whose error does not depend on where in the water
    column they are looking, which is all of the surface ones.

    **`error by depth` is not a refinement, it is the difference between a
    usable profile and an unusable one.** What an in situ error mostly measures
    here is representativeness: what a point cast says about the mean of a 25 km
    cell. In the seasonal thermocline the vertical temperature gradient is a
    tenth of a degree per metre, so an eddy displacing an isotherm by ten metres
    moves the value by a degree, and a profile declared accurate to the
    instrument's own 0.002 K would be given a weight in the cost function
    hundreds of times what it has earned. Below the thermocline the same
    argument runs the other way and a uniform error would throw away the deep
    part of the cast, which is the part that is actually well measured.
    """
    if "error by depth" not in spec:
        return np.full(np.shape(depth) if depth is not None else (), spec["error"])
    table = np.array(spec["error by depth"], dtype=float)
    return np.interp(depth, table[:, 0], table[:, 1])


def write_obs(path, spec, lon, lat, values, begin, offsets, depth=None,
              sigma=None):
    """One platform's file for one window, in the ioda layout SOCA reads."""
    variable = spec["variable"]
    if sigma is None:
        sigma = np.full(values.shape, spec["error"])
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
        if depth is not None:
            # Positive downward in metres, which is what SOCA's profile
            # operator reads and the sign the model's own vertical coordinate
            # is not in. See `depths`.
            below = meta.createVariable("depth", "f4", ("Location",))
            below.units = "m"
            below.positive = "down"
            below[:] = depth

        for group, payload in (("ObsValue", values),
                               ("ObsError", sigma),
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
