#!/usr/bin/env python3
"""Build the GEFS half of the forcing archive: one `atm.nc` per member.

    tools/forcing-gefs.py 2015-07-11 2015-07-28 --leads 12,36,60,84,108 \\
        --members 21 \\
        --domain gom_25km \\
        --out $ACKBAR_STATIC_ROOT/forcing/gom_25km/gefs

**Ask for a wider span than the experiment.** `time_interp_external` refuses to
extrapolate, so a run whose first step lands before the first record dies with
"time ... is before range of list" rather than holding the first value. The gap
is real even when the dates look right: the flux fields are stamped at their
window midpoints, so the first of them sits half an output interval after the
requested start (+1.5 h for this era) and no amount of matching the start date
closes it. A day of margin at each end is the fix, and `ackbar validate` reads
the narrowest axis in the file, so an archive that is short reports itself
before anything is submitted.

GEFS forces every *experiment*, control and ensemble alike, while ERA5 forces the
truth. That is the fraternal twin: the experiments see a forecast of the weather
the truth actually had, wrong by the amount a real forecast is wrong.

## Lagged forecast, not lagged date

Every member here is valid at the same times as every other and as the truth.
What differs between members is which perturbed forecast they came from, and
`--lead` says how far ahead it was looking. That is real forecast uncertainty
about the actual verifying date.

It is not the scheme the open boundary uses, and the difference is not cosmetic.
There a member's field is imported from another *date*, because GLORYS has one
member and there is nothing else to draw from; the result is a plausible field
that is not an estimate of the uncertainty about this date. Where a native
ensemble exists it would be perverse to synthesize one instead, so where one
exists this uses it.

Where one does not, `--lead` is still the axis, and the lagged-difference scheme
builds on top of this rather than replacing it. See "What each era can supply".

## GEFS is not one archive, and this is why there is an era table

The period is a first class input, not an assumption. Two products with nothing
in common but a name cover the years anyone would want:

| era | years | members | inits/day | output | grid | one file holds |
|---|---|---|---|---|---|---|
| `reforecast` | 2000-2019 | 5 | 1 | 3 h | 0.25 deg | every lead of one field |
| `operational-1deg` | 2017 to 2018-07 | 21 | 4 | 6 h | 1.0 deg | every field at one lead |
| `operational-half` | 2018-07 to 2020-09 | 21 | 4 | 6 h | 0.5 deg | every field at one lead |
| `operational-quarter` | 2020-09 on | 31 | 4 | 3 h | 0.25 deg | every field at one lead |

They differ in the bucket, the directory shape, the file naming, how many members
exist, how often a forecast starts, how often it reports, and how humidity is
carried: the reforecast publishes specific humidity directly while the
operational archive publishes dewpoint and needs the same conversion ERA5 does.
None of that is derivable from a date, so it is stated per era and selected by
one.

Nothing is silently approximate. A period no era covers, an era that is not
implemented, a member count an era cannot supply, a lead its cadence cannot
express: each of those names itself and stops.

## What each era can supply

**The 5 member eras cannot supply a 20 member ensemble.** The reforecast has five
members on an ordinary day and eleven on a Wednesday, and eleven once a week is
not an ensemble a daily cycle can read. So on the earlier period native GEFS is
an *input to* the lagged-difference scheme rather than an ensemble in its own
right, and the lagged-difference path is first class rather than a fallback that
gets dropped if native measures better on the later period. On 2020-09 onward,
31 members means a 20 member experiment is native, exchangeable and needs no
construction at all.

## Lead

`--leads` is the one knob and it sets two things at once: how much spread the
ensemble has, and how wrong the experiments' atmosphere is against the truth's.
Both in physically calibrated units, and with the honest tradeoff a real forecast
system has, since a longer lead buys spread and costs accuracy. Choose it by
measurement rather than taste: the smallest lead whose ensemble spread spans the
ERA5-minus-control difference over the same box and hours.

A member's series is stitched at constant lead, taking the hours from `lead` to
`lead + <the era's initialization interval>` out of each successive
initialization, so every record is the same age and its error statistics are
stationary. soca-science did the same with a fixed six hour lead. The joins are
small jumps and they are the price of a series longer than a forecast.

Each lead must be a multiple of the era's reset period, because NCEP resets its
averaging and accumulation windows on that period and this reads the reset
boundary as a segment boundary.

## More members than the era has forecasts

`--leads` takes a ladder, and the ensemble is its outer product with the era's
members: five reforecast members at 12, 36, 60, 84 and 108 hours are twenty-five
members, every one of them a real forecast of these hours by a real model, and
every one of them valid at the same times as the truth. That is the only way a
2015 experiment gets more than five members, since the reforecast has five.

Count the runs rather than the members when sizing the ladder. An experiment
with `ensemble.size: 20` and a control is twenty-one runs, `mem000` through
`mem020`, so it needs twenty-one atmospheres: five rungs, not the four that
give exactly twenty.

**They are not exchangeable, and the filter assumes they are.** A member at 84
hours is drawn from a wider error distribution than one at 12, so the ensemble is
a mixture of as many distributions as there are rungs. Two consequences
worth stating rather than discovering: the spread is larger than any single
lead's and the mean is worse than the shortest lead's, and a rank histogram will
not be flat even with a perfect filter. The alternative is five members, which is
worse in a way that is harder to reason about. When the OSSE moves to a period
after 2020-09, 31 native members at one lead retire the ladder entirely.

The ladder is 24 hours apart, not 6, so that consecutive rungs come from
different initializations rather than from the same one six hours apart. Rungs
from one initialization share most of their error.

Member index runs the era's members fastest, so `mem000` is the control at the
shortest lead and the first block is a plain single-lead ensemble.

## De-averaging, which is the only place this can be quietly wrong

Radiation and precipitation arrive as windows since the last reset, so within
each reset period one window arrives whole and the rest are differences:

    mean over [a, b]  =  (b - r) * mean over [r, b]  -  (a - r) * mean over [r, a]
                         all divided by (b - a), where r is the reset

which for the common case of two three hour halves is soca-science's `[2, -1]`
for a mean and `[1, -1]` for an accumulation. Getting it wrong does not crash
anything: it makes precipitation too large by a factor that grows through each
reset period and puts the shortwave's diurnal peak in the wrong place. Every
window read is checked against the one the arithmetic assumes.

An era whose output interval equals its reset period never differences at all,
which is why the two six hourly operational eras are simpler than either three
hourly one rather than harder.

## Reading a fourteenth of the archive

A reforecast field file holds every lead of one parameter over ten days, and a
segment wants sixteen of its eighty messages. Downloading the file whole moves
451 MB per initialization to use 32 of them, and the wind files are the worst of
it: 137 MB each, of which a segment reads eight messages out of a hundred and
sixty, because they carry the 10 m and 100 m winds interleaved.

NCEP publishes a `.idx` beside every GRIB file giving each message's byte offset,
so the wanted messages can be asked for by range and concatenated.

The fourteenth is arithmetic on file sizes and message counts, not a measurement.
What was measured is the wall clock: 84 seconds a member against about nine
minutes, a factor of 6.4. The rest goes somewhere this has not profiled, most
likely the per-field loop below, which fetches seven files in sequence and only
parallelizes the ranges within each.

The index is used as a *superset* filter and nothing else: which plane is which
is still decided by eccodes in `harvest`, so a description the index parse does
not understand costs a few unread bytes rather than a wrong field, and a filter
that is too narrow is caught by `segment`'s missing check rather than silently
producing a short series.
"""

import argparse
import datetime
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import eccodes
import numpy as np
import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ackbar.forcing import (  # noqa: E402
    FIELDS, assert_no_leap_day, clip, domain_box, specific_humidity,
    write_atm)


class Era(NamedTuple):
    """One GEFS product, and every period-specific fact about it.

    `implemented` is false for an era whose layout is known to differ but has not
    been read from, which is a different thing from an era that does not exist.
    A user asking for one gets told which, rather than an archive built out of
    guesses about a directory shape nobody checked.
    """
    name: str
    first: str
    last: str
    bucket: str
    members: int
    inits: tuple      # initialization hours in a day
    step: int         # hours between output records
    reset: int        # hours between accumulation resets
    humidity: str     # "spfh" or "dewpoint"
    layout: str       # "per_field" or "per_lead"
    grid: str
    implemented: bool = True
    note: str = ""


ERAS = (
    Era(name="reforecast", first="2000-01-01", last="2019-12-31",
        bucket="s3://noaa-gefs-retrospective", members=5, inits=(0,),
        step=3, reset=6, humidity="spfh", layout="per_field",
        grid="0.25 deg to day 10",
        note="five members daily and eleven on Wednesdays; the eleven are "
             "weekly and a daily cycle cannot read them"),
    Era(name="operational-1deg", first="2017-01-01", last="2018-07-26",
        bucket="s3://noaa-gefs-pds", members=21, inits=(0, 6, 12, 18),
        step=6, reset=6, humidity="dewpoint", layout="per_lead",
        grid="1.0 deg", implemented=False,
        note="the pgrb2a layout and its field names have not been read from "
             "here; six hourly shortwave on a one degree grid also defeats the "
             "point of sub-daily forcing over the Gulf"),
    Era(name="operational-half", first="2018-07-27", last="2020-09-22",
        bucket="s3://noaa-gefs-pds", members=21, inits=(0, 6, 12, 18),
        step=6, reset=6, humidity="dewpoint", layout="per_lead",
        grid="0.5 deg", implemented=False,
        note="the pgrb2a layout and its field names have not been read from "
             "here"),
    Era(name="operational-quarter", first="2020-09-23", last="2099-12-31",
        bucket="s3://noaa-gefs-pds", members=31, inits=(0, 6, 12, 18),
        step=3, reset=6, humidity="dewpoint", layout="per_lead",
        grid="0.25 deg"),
)

#: Where a member's file lives, per layout. The reforecast splits by field and
#: holds every lead; the operational archive splits by lead and holds every
#: field. That is not a naming difference, it is a different number of downloads
#: for the same forcing, and it is why `plan` exists.
PER_LEAD = ("{bucket}/gefs.{day}/{hour}/atmos/pgrb2sp25/"
            "{member}.t{hour}z.pgrb2s.0p25.f{lead:03d}")
PER_FIELD = ("{bucket}/GEFSv12/reforecast/{year}/{day}{hour}/{member}/"
             "Days:1-10/{stem}_{day}{hour}_{member}.grib2")

#: How to find a field in a GRIB message, as (typeOfLevel, level). Matched on the
#: level rather than the parameter name because a reforecast field file holds one
#: parameter at more than one height: `ugrd_hgt` carries 10 m and 100 m winds
#: interleaved, and taking the first would put the ocean under a wind from ninety
#: metres up.
LEVEL = {
    "T2":    ("heightAboveGround", 2),
    "Q2":    ("heightAboveGround", 2),
    "U10":   ("heightAboveGround", 10),
    "V10":   ("heightAboveGround", 10),
    "DSWRF": ("surface", 0),
    "DLWRF": ("surface", 0),
    "PRATE": ("surface", 0),
    "_2d":   ("heightAboveGround", 2),
    "_sp":   ("surface", 0),
}

#: The GRIB `shortName` each output field has, which the per-lead layout needs
#: because one file holds every field and the level alone does not separate them.
SHORT = {
    "T2": "2t", "Q2": "q", "U10": "10u", "V10": "10v",
    "DSWRF": "dswrf", "DLWRF": "dlwrf", "PRATE": "tp",
    "_2d": "2d", "_sp": "sp",
}

#: The reforecast's file stem per output field. One parameter each, so the stem
#: is the selector and `SHORT` is not consulted.
STEM = {
    "T2": "tmp_2m", "Q2": "spfh_2m", "U10": "ugrd_hgt", "V10": "vgrd_hgt",
    "DSWRF": "dswrf_sfc", "DLWRF": "dlwrf_sfc", "PRATE": "apcp_sfc",
}

#: Fields that arrive as a window since the last reset rather than as a value.
WINDOWED = ("DSWRF", "DLWRF", "PRATE")

#: How far below zero a de-averaged field may go before it is a bug rather than
#: arithmetic on rounded numbers.
#:
#: Differencing two windows that were each packed to a few significant digits
#: leaves a residue, and scaling one of them scales it, so a night-time shortwave
#: of exactly zero comes back as a few W m-2 either side of it.
#:
#: The tolerance has to have an absolute part, and finding that out is the reason
#: these numbers are measured rather than assumed. A purely relative tolerance is
#: scaled by the field's own magnitude, which at night is the residue itself, so
#: every dark segment fails: the first version of this check rejected a shortwave
#: of -8 W m-2 "against a range of 8". The relative part still earns its place by
#: daylight, where packing precision tracks magnitude.
#:
#: Both parts are far below what a *real* misreading costs. Taking a six hour
#: window for a three hour one puts hundreds of W m-2 in the wrong place, not
#: tens, so the failure this is meant to catch is nowhere near these bounds.
#: The absolute parts are set against each field's own domain mean, which is why
#: they are not one number: 20 W m-2 is 2.4% of a 300 W m-2 shortwave, and the
#: precipitation bound has to be read the same way. At 2e-5 kg m-2 s-1 it was
#: 1.73 mm/day against a domain mean of 3.15, so a negative excursion of half
#: the climatological rainfall would have been floored to zero and reported as
#: one line of print. Flooring only ever adds water, so that is the direction
#: that matters.
NEGATIVE_ABSOLUTE = {"DSWRF": 20.0, "DLWRF": 20.0, "PRATE": 2.0e-6}
NEGATIVE_RELATIVE = 0.02


def choose_era(start, end, named):
    """The era covering a span, or a message about why none does."""
    if named:
        for era in ERAS:
            if era.name == named:
                break
        else:
            raise SystemExit(
                f"forcing-gefs: no era called {named!r}. Known: "
                f"{', '.join(e.name for e in ERAS)}")
        # Named explicitly still has to cover the dates. Without this, --era
        # names a real product and the run walks into a 404 retried four times
        # per file rather than saying which years that product holds.
        if not (datetime.datetime.strptime(era.first, "%Y-%m-%d") <= start
                and end <= datetime.datetime.strptime(era.last, "%Y-%m-%d")
                + datetime.timedelta(hours=23)):
            raise SystemExit(
                f"forcing-gefs: --era {era.name} holds {era.first} to "
                f"{era.last}, which does not cover {start:%Y-%m-%d} to "
                f"{end:%Y-%m-%d}. Naming an era does not extend it.")
    else:
        covering = [e for e in ERAS
                    if datetime.datetime.strptime(e.first, "%Y-%m-%d") <= start
                    and end <= datetime.datetime.strptime(e.last, "%Y-%m-%d")
                    + datetime.timedelta(hours=23)]
        if not covering:
            raise SystemExit(
                f"forcing-gefs: no GEFS era covers {start:%Y-%m-%d} to "
                f"{end:%Y-%m-%d} whole. The eras are:\n" + "\n".join(
                    f"  {e.name:<22} {e.first} to {e.last}" for e in ERAS))
        if len(covering) > 1:
            # The reforecast and the operational archive overlap from 2017 to
            # 2019 and are different products over the same days, so a date is
            # not enough to choose and guessing would decide the member count
            # silently.
            raise SystemExit(
                f"forcing-gefs: {start:%Y-%m-%d} to {end:%Y-%m-%d} is covered "
                f"by more than one era, so --era is required. Candidates: "
                f"{', '.join(e.name for e in covering)}")
        era = covering[0]

    if not era.implemented:
        raise SystemExit(
            f"forcing-gefs: the {era.name} era ({era.first} to {era.last}) is "
            f"known and not implemented. {era.note}.\n"
            f"Implementing it means reading its layout and field names; the "
            f"cadence, member count and grid in this tool's era table are "
            f"already right for it.")
    return era


def member_name(era, index):
    """`mem000` is the control, which in GEFS is `gec00` and not a perturbation.

    Members above it are the perturbed ones in order, so "which forecast did
    member 7 read" is derived from the index rather than recorded, and is
    reproducible from the experiment config alone. The reforecast drops the `ge`
    prefix, which is the only naming difference between the eras that matters.
    """
    stem = "c00" if index == 0 else f"p{index:02d}"
    return stem if era.layout == "per_field" else f"ge{stem}"


def run(*args):
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"forcing-gefs: {' '.join(args)}\n{out.stderr.strip()}")
    return out.stdout


def download(url, local):
    if not local.exists():
        run("aws", "s3", "cp", "--no-sign-request", "--only-show-errors",
            url, str(local) + ".part")
        Path(str(local) + ".part").rename(local)
    return local


#: A line of a GRIB index, `number:offset:d=DATE:PARAM:LEVEL:TIMERANGE:ENS=...`.
#: The forecast hour wanted is the end of the time range, which is the second
#: number of `12-15 hour ave fcst` and the only one of `15 hour fcst`.
IDX_HOUR = re.compile(r"(?:(\d+)-)?(\d+) hour")

#: How much unwanted GRIB to swallow rather than split a range in two. Messages
#: here are a few hundred kB, so at zero the only ranges that merge are the ones
#: that were already contiguous, which is what the windowed fields do (a window
#: and the window containing it are neighbours) and what the winds do not (every
#: other message is the 100 m level, which nothing reads).
GAP = 0

#: Range requests to have in flight at once. Network concurrency, not a rank
#: count: a single range is a fraction of a file and eight of them saturate the
#: link where one does not.
JOBS = 8

RETRIES = 4

_local = threading.local()
_pool = None


def session():
    """One connection per thread, kept open across the ranges of a file."""
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def https(url):
    """The public HTTPS form of an `s3://` URL, which is what ranges need."""
    bucket, _, key = url[len("s3://"):].partition("/")
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def get(url, span=None):
    """One GET, whole or ranged, retried because S3 drops one now and then."""
    headers = {"Range": f"bytes={span}"} if span else {}
    for attempt in range(RETRIES):
        try:
            reply = session().get(url, headers=headers, timeout=300)
            if reply.status_code in (200, 206):
                return reply.content
            problem = f"HTTP {reply.status_code}"
        except requests.RequestException as error:
            problem = str(error)
        if attempt == RETRIES - 1:
            raise SystemExit(
                f"forcing-gefs: GET {url} [{span or 'whole'}]: {problem}")
        time.sleep(2 ** attempt)


def message_ranges(url, cache, steps):
    """Byte ranges of the messages in *url* whose forecast hour is wanted.

    The index is cached because the leads of a ladder read the same member's
    files over and over, and an index is six kB against the tens of MB it saves.
    """
    index = cache / (url.rsplit("/", 1)[-1] + ".idx")
    if not index.exists():
        index.write_bytes(get(https(url) + ".idx"))

    offsets, keep = [], []
    for line in index.read_text().splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        hour = IDX_HOUR.search(parts[5])
        offsets.append(int(parts[1]))
        keep.append(int(hour.group(2)) in steps if hour else False)

    ranges = []
    # The last message runs to the end of the file, which the index does not
    # say, so its range is left open.
    ends = offsets[1:] + [None]
    for offset, end, wanted in zip(offsets, ends, keep):
        if not wanted:
            continue
        if ranges and ranges[-1][1] is not None and offset - ranges[-1][1] <= GAP:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((offset, end))
    if not ranges:
        raise SystemExit(
            f"forcing-gefs: {index.name} has no message at any of "
            f"{sorted(steps)} h, so the ranges to read cannot be worked out")
    return ranges


def fetch_messages(url, steps, local, cache):
    """Write just the wanted messages of *url* to *local*, as one GRIB file.

    Concatenated GRIB is GRIB: a reader walks message to message and neither
    knows nor cares that the ones between were never fetched.
    """
    ranges = message_ranges(url, cache, steps)
    target = https(url)
    spans = [f"{start}-" if end is None else f"{start}-{end - 1}"
             for start, end in ranges]
    local.write_bytes(b"".join(_pool.map(lambda s: get(target, s), spans)))
    return local


def harvest(path, specs, steps):
    """Pull the wanted (field, step) planes out of one GRIB file.

    *specs* maps an output name to `(shortName or None, typeOfLevel, level)`, and
    *steps* is the set of `endStep` values to keep. Returns `{(name, step):
    plane}` plus the window each carries and the grid.

    A `shortName` of None means the file holds one parameter and the level alone
    identifies it, which is the reforecast's case.
    """
    found, spans = {}, {}
    lons = lats = None
    with open(path, "rb") as stream:
        while True:
            handle = eccodes.codes_grib_new_from_file(stream)
            if handle is None:
                break
            try:
                end = eccodes.codes_get(handle, "endStep")
                if end not in steps:
                    continue
                level = (eccodes.codes_get(handle, "typeOfLevel"),
                         eccodes.codes_get(handle, "level"))
                short = eccodes.codes_get(handle, "shortName")
                for name, (want_short, want_type, want_level) in specs.items():
                    if (want_type, want_level) != level:
                        continue
                    if want_short is not None and want_short != short:
                        continue
                    ni = eccodes.codes_get(handle, "Ni")
                    nj = eccodes.codes_get(handle, "Nj")
                    if lons is None:
                        first_lon = eccodes.codes_get(
                            handle, "longitudeOfFirstGridPointInDegrees")
                        first_lat = eccodes.codes_get(
                            handle, "latitudeOfFirstGridPointInDegrees")
                        step_lon = eccodes.codes_get(
                            handle, "iDirectionIncrementInDegrees")
                        step_lat = eccodes.codes_get(
                            handle, "jDirectionIncrementInDegrees")
                        lons = (first_lon + step_lon * np.arange(ni)) % 360.0
                        # GRIB scans north to south here, so the latitude
                        # increment is a decrement. Reading it as positive flips
                        # the field about the equator, which in this box is a
                        # plausible looking wind blowing the wrong way rather
                        # than an error.
                        # The sign is read rather than assumed. GEFS scans
                        # north to south, so the latitude increment is a
                        # decrement, and reading it the other way flips the
                        # field about the equator: over this box that is a
                        # plausible looking wind blowing the wrong way, not an
                        # error anything would raise.
                        down = not eccodes.codes_get(handle,
                                                     "jScansPositively")
                        lats = (first_lat + (-step_lat if down else step_lat)
                                * np.arange(nj))
                    found[(name, end)] = eccodes.codes_get_values(
                        handle).reshape(nj, ni)
                    spans[(name, end)] = (
                        eccodes.codes_get(handle, "startStep"), end)
            finally:
                eccodes.codes_release(handle)
    return found, spans, lons, lats


def collect(era, init, member, steps, cache):
    """Every needed (field, step) for one initialization, whichever layout."""
    specs_instant = {name: (SHORT.get(name), *LEVEL[name])
                     for name in instant_names(era)}
    specs_window = {name: (SHORT.get(name), *LEVEL[name])
                    for name in WINDOWED}

    found, spans = {}, {}
    lons = lats = None
    if era.layout == "per_lead":
        specs = {**specs_instant, **specs_window}
        for lead in sorted(steps["all"]):
            url = PER_LEAD.format(bucket=era.bucket, day=f"{init:%Y%m%d}",
                                  hour=f"{init:%H}", member=member, lead=lead)
            local = download(url, cache / f"{member}.{init:%Y%m%d%H}.f{lead:03d}")
            try:
                part, span, x, y = harvest(local, specs, {lead})
            finally:
                local.unlink(missing_ok=True)
            found.update(part)
            spans.update(span)
            lons = lons if lons is not None else x
            lats = lats if lats is not None else y
    else:
        for name in list(specs_instant) + list(specs_window):
            want = steps["window"] if name in WINDOWED else steps["instant"]
            url = PER_FIELD.format(bucket=era.bucket, year=f"{init:%Y}",
                                   day=f"{init:%Y%m%d}", hour=f"{init:%H}",
                                   member=member, stem=STEM[name])
            local = fetch_messages(
                url, want, cache / f"{member}.{init:%Y%m%d%H}.{STEM[name]}",
                cache)
            try:
                # One parameter per file, so the shortName is not consulted and
                # only the level separates the 10 m wind from the 100 m one.
                part, span, x, y = harvest(
                    local, {name: (None, *LEVEL[name])}, want)
            finally:
                local.unlink(missing_ok=True)
            found.update(part)
            spans.update(span)
            lons = lons if lons is not None else x
            lats = lats if lats is not None else y

    return found, spans, lons, lats


def instant_names(era):
    """The fields read directly, which depends on how the era carries humidity."""
    if era.humidity == "spfh":
        return ["T2", "Q2", "U10", "V10"]
    return ["T2", "U10", "V10", "_2d", "_sp"]


def floor_at_zero(name, plane, scale, where):
    """Clamp a strictly positive field, refusing an excursion too big to be dust.

    *scale* is the magnitude of the window this was differenced out of, not of
    the result, because the result is what has been cancelled down to nothing.
    """
    worst = float(plane.min())
    if worst >= 0.0:
        return plane, 0.0
    allowed = max(NEGATIVE_ABSOLUTE[name], NEGATIVE_RELATIVE * scale)
    if -worst > allowed:
        raise SystemExit(
            f"forcing-gefs: de-averaging {name} at {where} gives {worst:.4g}, "
            f"past the {allowed:.4g} that packing residue explains at a window "
            f"magnitude of {scale:.4g}. The reset period is not what this tool "
            f"reads; see the de-averaging note in its docstring.")
    return np.maximum(plane, 0.0), worst


def segment(era, init, member, lead, cache, box):
    """One initialization's contribution: `era.inits` spacing of forcing.

    Returns `{name: [(hours after init, plane)]}`, the grid, and the worst
    negative excursion the de-averaging produced.
    """
    span = 24 // len(era.inits)
    instants = list(range(lead, lead + span, era.step))
    # Every window boundary inside the segment, plus the reset each belongs to.
    ends = list(range(lead + era.step, lead + span + era.step, era.step))
    steps = {"instant": set(instants), "window": set(ends)}
    steps["all"] = steps["instant"] | steps["window"]

    found, spans, lons, lats = collect(era, init, member, steps, cache)
    missing = [key for key in
               [(n, s) for n in instant_names(era) for s in instants]
               + [(n, s) for n in WINDOWED for s in ends]
               if key not in found]
    if missing:
        raise SystemExit(
            f"forcing-gefs: {member} at {init:%Y-%m-%d %H} is missing "
            f"{missing[:6]}{' and more' if len(missing) > 6 else ''}")

    # Clipped before anything is computed, not after. Two reasons, and the second
    # is the one that bit: the arithmetic below is a hundredth of the work on a
    # box, and the de-averaging tolerance is meant to be about the water this
    # archive forces rather than about the globe. Flooring globally and clipping
    # afterwards gives the same numbers in the file and a different, larger,
    # residue to judge, because the largest packing residue on Earth is not in
    # the Gulf of Mexico.
    x = y = None
    for key, plane in list(found.items()):
        x, y, cube = clip(lons, lats, plane[None, :, :], box)
        found[key] = cube[0]

    series = {name: [] for name in FIELDS}
    for step in instants:
        if era.humidity == "spfh":
            series["Q2"].append((step, found[("Q2", step)]))
        else:
            series["Q2"].append((step, specific_humidity(
                found[("_2d", step)], found[("_sp", step)])))
        for name in ("T2", "U10", "V10"):
            series[name].append((step, found[(name, step)]))

    records, worst = deaverage(era, found, spans, ends, lead,
                               f"{member} {init:%Y-%m-%d %H}")
    for name, entries in records.items():
        series[name].extend(entries)

    return series, x, y, worst


def deaverage(era, found, spans, ends, lead, where=""):
    """Windows since a reset, turned into a mean over each output interval.

    Pure: planes in, planes out, no network and no era beyond its arithmetic, so
    the one part of this tool that can be quietly wrong is the part that can be
    tested from synthetic numbers. *found* maps `(name, end step)` to a plane and
    *spans* to the `(start, end)` that plane actually covers.

    Returns `{name: [(hours after init, plane)]}` and the worst negative
    excursion each field produced, which the caller reports.

    The stamp is the window midpoint, `end - step/2`, not the end: these are
    means over an interval and the model interpolates between stamps.
    """
    if era.reset % era.step:
        # Otherwise `start - reset` can go negative and the weighted difference
        # below adds the earlier window where it should subtract it, with the
        # span check still passing because both windows are the ones expected.
        raise SystemExit(
            f"forcing-gefs: the {era.name} era resets every {era.reset} h and "
            f"reports every {era.step} h, which do not divide. The de-averaging "
            f"assumes each reset period is a whole number of output intervals.")

    records = {name: [] for name in WINDOWED}
    worst = {name: 0.0 for name in WINDOWED}
    for name in WINDOWED:
        previous = {}
        for end in ends:
            # Relative to `lead` rather than to forecast hour zero, which is
            # only the same grid because `main` refuses a lead that is not a
            # multiple of the reset period. The two are load bearing on each
            # other.
            reset = lead + (end - 1 - lead) // era.reset * era.reset
            start = end - era.step
            whole = found[(name, end)]
            if spans[(name, end)] != (reset, end):
                raise SystemExit(
                    f"forcing-gefs: {name} at step {end} covers "
                    f"{spans[(name, end)]} where ({reset}, {end}) was expected. "
                    f"The reset period is not what this tool reads; see the "
                    f"de-averaging note in this tool's docstring.")
            if start == reset:
                # The first window of a reset period arrives whole.
                plane = whole
            else:
                # Everything after it is what the containing window holds minus
                # what the previous one already accounted for.
                earlier = previous[start]
                if name == "PRATE":
                    plane = whole - earlier
                else:
                    plane = ((end - reset) * whole
                             - (start - reset) * earlier) / era.step
            previous[end] = whole
            scale = float(np.abs(whole).max())
            if name == "PRATE":
                plane = plane / (era.step * 3600.0)
                scale = scale / (era.step * 3600.0)
            plane, low = floor_at_zero(name, plane, scale, f"{where} +{end}")
            worst[name] = min(worst[name], low)
            records[name].append((end - era.step / 2.0, plane))
    return records, worst


def build(era, index, start, end, leads, cache, out, box, domain):
    """One member's whole `atm.nc`.

    *index* is the ensemble member, which the ladder resolves into a GEFS
    member and a lead. The era's members run fastest, so `mem000` is the
    control at the shortest lead.
    """
    lead = leads[index // era.members]
    member = member_name(era, index % era.members)
    origin = start
    gathered = {name: ([], []) for name in FIELDS}
    grid = None
    worst = {name: 0.0 for name in WINDOWED}

    # The initializations are evenly spaced through the day from midnight, so
    # walking by the segment length from a midnight visits exactly them, for one
    # a day and for four alike.
    #
    # The series is *not* required to begin on an initialization plus the lead.
    # It cannot be: the reforecast starts once a day at 00Z, so demanding that
    # alignment would restrict a midnight start to leads that are whole
    # multiples of a day, and the shortest of those is 24 hours. Segments are
    # generated wherever they fall and the result is trimmed, which is what the
    # ERA5 tool does with its monthly files for the same reason.
    span = 24 // len(era.inits)
    # A midnight far enough back that the first segment cannot be missed, and a
    # midnight rather than `start - lead - span` because only the former is
    # guaranteed to sit on the initialization grid: subtracting 36 hours from
    # midnight lands on 12Z, which the reforecast never initializes at, and every
    # step of 24 from there stays wrong.
    cursor = (datetime.datetime(start.year, start.month, start.day)
              - datetime.timedelta(hours=((lead + span) // 24 + 1) * 24))
    while cursor + datetime.timedelta(hours=lead) <= end:
        if cursor + datetime.timedelta(hours=lead + span) > start:
            series, x, y, low = segment(era, cursor, member, lead, cache,
                                        box)
            for name, value in low.items():
                worst[name] = min(worst[name], value)
            if grid is None:
                grid = (x, y)
            base = (cursor - origin).total_seconds() / 3600.0
            for name, records in series.items():
                hours, planes = gathered[name]
                for offset, plane in records:
                    hours.append(base + offset)
                    planes.append(plane)
        cursor += datetime.timedelta(hours=span)

    if grid is None:
        raise SystemExit(
            f"forcing-gefs: no {era.name} initialization at a {lead} h lead "
            f"reaches {start:%Y-%m-%d %H}")

    horizon = (end - origin).total_seconds() / 3600.0
    packed = {}
    for name, (hours, planes) in gathered.items():
        hours = np.array(hours, dtype="f8")
        cube = np.stack(planes).astype("f4")
        order = np.argsort(hours, kind="stable")
        hours, cube = hours[order], cube[order]
        inside = (hours >= 0.0) & (hours <= horizon)
        packed[name] = (hours[inside], cube[inside])

    x, y = grid
    target = out / f"mem{index:03d}.nc"
    write_atm(target, x, y, origin, packed,
              source=f"GEFS {era.name} {member} at {lead} h lead, "
                     f"{era.bucket}", domain=domain)
    return target, packed, worst


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="first hour to cover, YYYY-MM-DD")
    ap.add_argument("end", help="last day to cover, YYYY-MM-DD, inclusive")
    ap.add_argument("--out", type=Path, required=True,
                    help="archive directory for this source")
    ap.add_argument("--leads", default="12",
                    help="comma separated forecast leads in hours, each a "
                         "multiple of the era's reset period. Every record is "
                         "between its lead and its lead plus the "
                         "initialization interval old. More than one lead is "
                         "how an era with few members reaches a larger "
                         "ensemble: members are the outer product of the "
                         "ladder with the era's members, all at one valid "
                         "time")
    ap.add_argument("--members", type=int, default=20,
                    help="how many members, counting the control as the first. "
                         "Bounded by what the era has")
    ap.add_argument("--domain", required=True,
                    help="clip to this domain's grid, plus a margin. Reads "
                         "$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/"
                         "ocean_hgrid.nc, so the domain must already have one")
    ap.add_argument("--era", help="which GEFS product, when the dates alone do "
                                  "not decide it")
    ap.add_argument("--cache", type=Path,
                    help="where downloads land before being read and deleted. "
                         "Default: <out>/.cache")
    ap.add_argument("--jobs", type=int, default=JOBS,
                    help="byte ranges to request at once. Network concurrency, "
                         "not a rank count")
    args = ap.parse_args()

    start = datetime.datetime.strptime(args.start, "%Y-%m-%d")
    end = (datetime.datetime.strptime(args.end, "%Y-%m-%d")
           + datetime.timedelta(hours=23))
    if end <= start:
        raise SystemExit("forcing-gefs: end is not after start")
    assert_no_leap_day(start, end)

    era = choose_era(start, end, args.era)
    try:
        leads = [int(part) for part in args.leads.split(",")]
    except ValueError:
        raise SystemExit(f"forcing-gefs: --leads {args.leads} is not a comma "
                         f"separated list of hours")
    if len(set(leads)) != len(leads):
        raise SystemExit(
            f"forcing-gefs: --leads {args.leads} repeats a lead, which would "
            f"make two members the same forecast and give the ensemble a "
            f"duplicate it cannot see")
    leads.sort()
    for lead in leads:
        if lead < era.step:
            raise SystemExit(
                f"forcing-gefs: the {era.name} era's first output is at "
                f"{era.step} h, so a lead of {lead} has nothing to read")
        if lead % era.reset:
            raise SystemExit(
                f"forcing-gefs: lead {lead} is not a multiple of the "
                f"{era.name} era's {era.reset} h reset period; see the "
                f"de-averaging note in this tool's docstring")
    capacity = era.members * len(leads)
    if not 1 <= args.members <= capacity:
        rungs = ", ".join(str(lead) for lead in leads)
        raise SystemExit(
            f"forcing-gefs: the {era.name} era has {era.members} members and "
            f"the ladder [{rungs}] has {len(leads)} rungs, which is "
            f"{capacity} members; {args.members} were asked for."
            + (f" {era.note}." if era.note else "")
            + " Add a rung to the ladder, or above what a ladder can hold "
              "take members from the lagged-difference scheme.")
    if args.members > era.members and len(leads) > 1:
        print(f"forcing-gefs: {args.members} members from {era.members} "
              f"forecasts on a {len(leads)} rung ladder, so the ensemble is a "
              f"mixture of {len(leads)} lead distributions and is not "
              f"exchangeable. See this tool's docstring.")

    box = domain_box(args.domain)
    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.cache or (args.out / ".cache")
    cache.mkdir(parents=True, exist_ok=True)

    global _pool
    _pool = ThreadPoolExecutor(max_workers=args.jobs)

    print(f"forcing-gefs: {era.name}, {era.members} members, "
          f"{len(era.inits)} init/day, {era.step} h output, {era.grid}")
    print(f"forcing-gefs: {args.domain} box {box['west']:.3f} to "
          f"{box['east']:.3f} east, {box['south']:.3f} to "
          f"{box['north']:.3f} north")
    for index in range(args.members):
        target, packed, worst = build(era, index, start, end, leads, cache,
                                      args.out, box, args.domain)
        hours, cube = packed["DSWRF"]
        residue = " ".join(f"{name} {low:.3g}" for name, low in worst.items()
                           if low < 0.0)
        print(f"forcing-gefs: {target}  {cube.shape[0]} records, "
              f"{hours[0]:+.1f} to {hours[-1]:+.1f} h"
              + (f", floored at zero from {residue}" if residue else ""),
              flush=True)

    # The indices are kept for the whole run because every rung of the ladder
    # reads the same member's files again, and are the only thing left in the
    # cache once the last member is written.
    for index in cache.glob("*.idx"):
        index.unlink()
    # A `.part` is what an interrupted whole-file download leaves. Temp then
    # rename means it is never mistaken for a finished file, but it is also
    # never cleaned up, and one of them keeps the cache directory alive forever.
    for part in cache.glob("*.part"):
        part.unlink(missing_ok=True)
    if not any(cache.iterdir()):
        cache.rmdir()


if __name__ == "__main__":
    main()
