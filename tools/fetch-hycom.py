#!/usr/bin/env python3
"""Build a domain's initial condition and open boundary conditions from HYCOM.

    env -u PYTHONPATH .venv-data/bin/python tools/fetch-hycom.py ic  <domain> <date>
    env -u PYTHONPATH .venv-data/bin/python tools/fetch-hycom.py obc <domain> <start> <end>

Same outputs as `tools/fetch-glorys.py`, from a different ocean. Writes to
`--out` rather than over the domain's own files, because the whole point of a
second source is having both.

    tools/fetch-hycom.py obc gom_25km 2015-05-28 2015-09-10 \
        --out $ACKBAR_STATIC_ROOT/domain/gom_25km/INPUT/obc.nc.hycom
    tools/fetch-hycom.py obc gom_25km 2015-05-28 2015-09-10 \
        --out ... --match-ssh $ACKBAR_STATIC_ROOT/domain/gom_25km/INPUT/obc.nc

No credentials and no toolbox: HYCOM is served over OPeNDAP from
`tds.hycom.org`, open, and read here with netCDF4 directly. It is slow. A season
of one domain's three boundary strips is tens of minutes, and the server drops
connections, so each request is retried.

---------------------------------------------------------------------------
Why HYCOM, for a fraternal twin OSSE

The OSSE as built has **no boundary error at all**: `gom_12km` (the nature run)
and `gom_25km` (the domain that assimilates) read the same GLORYS12V1 file, same
dates, same source attribute. So the analysis is handed a boundary that is exactly
truth's, which no real system ever has, and every skill number is computed with
that free constraint in it.

A fraternal twin fixes that by giving the truth and the experiments boundaries
from *different* reanalyses of the *same* days. The difference between them is
then a real boundary error, of about the size a real system has, rather than an
invented perturbation with a made-up amplitude.

GOFS 3.1 (GLBv0.08, experiment 53.X) is the closest thing to an independent
GLORYS12V1 that exists:

  - 1/12 degree, like GLORYS, so neither side is resolving eddies the other
    cannot. A coarser twin such as SODA at half a degree fails this: its
    boundary error would be dominated by not having a Loop Current rather than
    by having one in a different place.
  - 1994 to 2015, so it covers the OSSE period.
  - A different model (HYCOM, hybrid coordinate) with a different data
    assimilation (NCODA 3DVAR) under different atmospheric forcing (NAVGEM
    against GLORYS's ERA-Interim). Two runs of the same model with different
    seeds would not be a twin, they would be one solution twice.

---------------------------------------------------------------------------
Three differences from GLORYS that would be silently wrong

**`water_temp` is in-situ temperature.** HYCOM's own documentation says so:
"NRL writes out in-situ temperature, not potential temperature." MOM6 reads
potential temperature on the boundary and in `Z_INIT_FILE_PTEMP_VAR`, and
GLORYS's `thetao` already is one. Handed in-situ temperature instead, the deep
water arrives too warm by up to about 0.4 degC at 4000 m and 0.1 degC at 1000 m,
which is the same size as the differences between DA methods this project
measures. It is converted here with TEOS-10 (`gsw`), at each level's pressure and
each point's latitude, and the conversion is not optional.

**The output is three hourly snapshots, not daily means.** GLORYS `P1D-m` is a
daily mean stamped at 12Z. The 12Z snapshot is taken from HYCOM so that the two
products' records sit at the same instant, which is what makes a comparison
between them a comparison of oceans rather than of sampling. GOFS 3.1 has no
tides, so a snapshot carries no aliased signal a mean would have removed.

**Sea surface height is referenced to each model's own mean.** This is the one
that will actually break a run rather than bias it. Two analyses disagree about
absolute sea surface height by a near constant offset, and under FLATHER an
offset between the boundary and the interior is a permanent barotropic pressure
gradient the model accelerates a current down. The offset is always reported;
`--match-ssh <obc.nc>` removes it, by subtracting the difference between the two
files' mean over every segment point and every date. What survives is HYCOM's
variability, which is the independent information wanted, without its datum,
which is not information.

**And the reanalysis has missing days.** HYCOM documents gaps in the 1994-2015
record. A day the server does not have is reported by name rather than quietly
skipped, because a boundary file whose time axis has a hole in it interpolates
across the hole and the run looks healthy.

---------------------------------------------------------------------------
Any domain

Segments, their positions and the supergrid all come from `tools/obc_grid.py`,
which reads them out of the domain's own `OBC_SEGMENT_00N`. HYCOM's longitudes
run -180 to 180, the same convention GLORYS uses, so the Gulf never exercises the
wrap; `obc_grid.in_source_longitudes` handles it anyway for the first domain that
crosses the dateline.
"""

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import netCDF4 as nc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import obc_grid

WHO = "fetch-hycom"

#: GOFS 3.1 reanalysis, which is one dataset per year: `expt_53.X` is the whole
#: 1994-2015 record and the year selects within it.
#:
#: **This is the right default only while the OSSE stays in 2015.** GOFS 3.1
#: splits into two families and the reanalysis is the earlier one:
#:
#:   1994-01 to 2015-12   GLBv0.08/expt_53.X   the reanalysis, this default
#:   2014-07 to 2018-12   GLBv0.08/expt_5*,9*  the analysis, split by date into
#:                                             a series of short experiments
#:   2018-12 onwards      GLBy0.08/expt_93.0   the analysis, one dataset
#:
#: A period after 2015 is therefore a different URL, and after 2018 a different
#: grid as well. `--dataset` takes the whole template rather than an experiment
#: name, and `{year}` in it is optional, because the later families are not
#: split by year. The catalogue at https://www.hycom.org/dataserver/gofs-3pt1
#: is the authority on which experiment covers which dates; a table of them
#: here would be a second answer, free to go stale.
URL = "https://tds.hycom.org/thredds/dodsC/GLBv0.08/expt_53.X/data/{year}"

#: HYCOM names against MOM6's. `water_temp` is in-situ and is converted; see the
#: module docstring. `salinity` is practical salinity, which is what MOM6 wants.
HYCOM_IC = {"water_temp": "temp", "salinity": "salt"}
HYCOM_OBC = {"water_temp": "temp", "salinity": "salt",
             "water_u": "u", "water_v": "v", "surf_el": "zeta"}

DATASET_HELP = (
    "OPeNDAP URL template for the GOFS 3.1 experiment to read. `{year}` in it "
    "is substituted and is optional. The default is the 1994-2015 reanalysis; "
    "a period after 2015 needs an analysis experiment instead, and after 2018 "
    "the GLBy0.08 grid. See the note on URL in the source.")

#: Which of the eight daily snapshots to take, to sit where GLORYS's daily mean
#: is stamped. See the module docstring.
HOUR = 12

#: The server drops connections under a large request. Each read is retried
#: rather than failing a fetch that is otherwise half an hour in.
ATTEMPTS = 4
BACKOFF = 20


def die(message):
    obc_grid.die(WHO, message)


def experiment_of(template):
    """Which HYCOM experiment a URL template names, for the `:source` stamp.

    Read from the template rather than written as a constant, because the whole
    purpose of this tool is provable independence from GLORYS, and a file that
    names the 1994-2015 reanalysis while holding 2017 from the analysis family
    is worse than one with no provenance at all.
    """
    parts = [p for p in template.split("/") if p.startswith("expt_") or p.startswith("GLB")]
    return " ".join(parts) or template


def open_dataset(template, year):
    url = template.format(year=year)
    for attempt in range(ATTEMPTS):
        try:
            print(f"{WHO}: {url}")
            return nc.Dataset(url)
        except OSError as problem:
            if attempt == ATTEMPTS - 1:
                die(f"cannot open {url}: {problem}. Check that this experiment "
                    f"covers {year}; the reanalysis stops at 2015 and the "
                    f"analysis is a different family. See --dataset.")
            print(f"{WHO}:   retrying the catalogue ({problem})")
            time.sleep(BACKOFF * (attempt + 1))


def read(dataset, name, when, ybox, xbox):
    """One variable over a time and space box, retried, as a masked array."""
    for attempt in range(ATTEMPTS):
        try:
            variable = dataset[name]
            if variable.ndim == 4:
                return variable[when, :, ybox, xbox]
            return variable[when, ybox, xbox]
        except (OSError, RuntimeError) as problem:
            if attempt == ATTEMPTS - 1:
                die(f"reading {name} failed after {ATTEMPTS} attempts: {problem}")
            print(f"{WHO}:   retrying {name} ({problem})")
            time.sleep(BACKOFF * (attempt + 1))


def wanted_times(dataset, start, end):
    """Indices of the HOUR snapshot on each requested day, and the days missing.

    HYCOM's own time axis is the authority on which days exist: the reanalysis
    has documented gaps, and a day that is absent has to be named rather than
    left as a shorter file than was asked for.
    """
    units = dataset["time"].units
    stamps = nc.num2date(dataset["time"][:], units,
                         only_use_cftime_datetimes=False)
    # Keyed by day, but the value keeps the full stamp: the hour is what makes
    # this a 12Z snapshot rather than a daily mean, and `obc_grid.write_obc`
    # writes it into the time axis so MOM6 applies the boundary when it is
    # actually valid. Keying by day is only so that a requested date can be
    # looked up.
    by_day = {}
    for index, stamp in enumerate(stamps):
        if stamp.hour == HOUR:
            when = dt.datetime(stamp.year, stamp.month, stamp.day,
                               stamp.hour, stamp.minute, stamp.second)
            by_day[when.date()] = (when, index)

    days = [start + dt.timedelta(days=n)
            for n in range((end - start).days + 1)]
    found = [by_day[day.date()] for day in days if day.date() in by_day]
    missing = [day for day in days if day.date() not in by_day]

    # A dataset that publishes nothing at HOUR is not a dataset with gaps, it is
    # a dataset on a different cadence, and every day would come back missing.
    # Told apart here rather than at the call sites, because "documented gaps,
    # pick a neighbouring day" is advice that can never work when the answer is
    # that no day has this hour at all.
    if missing and not by_day:
        die(f"nothing in this dataset is stamped {HOUR:02d}Z. Its time axis "
            f"carries {sorted({s.hour for s in stamps})} as hours, so it "
            f"publishes on a different cadence than the 3-hourly snapshots "
            f"this tool reads. That is a property of the experiment behind "
            f"--dataset, not a gap in the record.")
    return found, missing


def source_box(dataset, box):
    """Index slices covering *box*, plus the coordinates they select."""
    lon = np.asarray(dataset["lon"][:])
    lat = np.asarray(dataset["lat"][:])
    west, east, south, north = obc_grid.in_source_longitudes(box, lon)

    xi = np.nonzero((lon >= west) & (lon <= east))[0]
    yi = np.nonzero((lat >= south) & (lat <= north))[0]
    if len(xi) < 2 or len(yi) < 2:
        die(f"the box {west:.2f} to {east:.2f}, {south:.2f} to {north:.2f} "
            f"selects {len(xi)} by {len(yi)} HYCOM cells, which is not enough "
            f"to interpolate from. A domain crossing the dateline needs its "
            f"strip fetched in two pieces, which this does not do.")
    return (slice(yi[0], yi[-1] + 1), slice(xi[0], xi[-1] + 1),
            lon[xi[0]:xi[-1] + 1], lat[yi[0]:yi[-1] + 1])


def to_potential(in_situ, salinity, depth, lat):
    """In-situ temperature to potential temperature, TEOS-10.

    *in_situ* and *salinity* are `(..., depth, lat, lon)`. Pressure is taken
    from depth and latitude, and absolute salinity from practical salinity, so
    both conversions see the geometry rather than a nominal column.
    """
    import gsw

    shape = in_situ.shape
    z = np.broadcast_to(np.asarray(depth)[:, None, None], shape[-3:])
    y = np.broadcast_to(np.asarray(lat)[None, :, None], shape[-3:])
    # gsw wants height, negative downward, and returns pressure in dbar.
    pressure = gsw.p_from_z(-z, y)

    pressure = np.broadcast_to(pressure, shape)
    y = np.broadcast_to(y, shape)
    absolute = gsw.SA_from_SP(salinity, pressure, 0.0, y)
    return gsw.pt0_from_t(absolute, in_situ, pressure)


def sample_segment(dataset, geo, when, depth):
    """Every OBC field along one segment, for the chosen times.

    Fetched as a strip around the segment rather than over the whole domain, for
    the same reason `fetch-glorys.py` does it: each segment is independent and
    nothing is held that is not about to be used.
    """
    box = (geo["lon"].min() - obc_grid.STRIP_MARGIN,
           geo["lon"].max() + obc_grid.STRIP_MARGIN,
           geo["lat"].min() - obc_grid.STRIP_MARGIN,
           geo["lat"].max() + obc_grid.STRIP_MARGIN)
    ybox, xbox, src_lon, src_lat = source_box(dataset, box)
    indices = [index for _, index in when]

    raw = {}
    for name in HYCOM_OBC:
        print(f"{WHO}:     {name}")
        # One day at a time. A single request for a season of one strip is tens
        # of millions of points and the server times out on it; a day is small
        # enough to always come back and to retry cheaply when it does not.
        raw[name] = np.stack([np.ma.filled(
            np.ma.masked_invalid(read(dataset, name, index, ybox, xbox)
                                 .astype("f8")), np.nan)
            for index in indices])

    raw["water_temp"] = to_potential(raw["water_temp"], raw["salinity"],
                                     depth, src_lat)
    fields = {HYCOM_OBC[name]: values for name, values in raw.items()}
    return obc_grid.sample_onto(geo, fields, src_lon, src_lat)


def ssh_offset(built, reference, geometry, dates):
    """Mean `zeta` here minus mean `zeta` in *reference*, over the whole boundary.

    One number for the file rather than one per segment: the thing being removed
    is the two products' different datum, which is global to each, and a per
    segment correction would put a step in sea surface height at the corner
    where two segments meet.

    **Over the same days on both sides**, which is the part that is easy to get
    wrong and expensive when it is. A reference covering a year while this fetch
    covers a summer would otherwise contribute its own seasonal cycle to the
    "datum": the Gulf's steric range is about 10 cm against a 16 cm datum
    difference, so most of what got removed would be real signal the interior
    still has, and it would be removed as a constant.

    The same segments on both sides too. A reference missing one of three is a
    comparison between different geography, and a mean over a different piece of
    coastline is not a datum.
    """
    with nc.Dataset(reference) as f:
        carried = [name for name in geometry
                   if f"zeta_segment_{name}" in f.variables]
        if sorted(carried) != sorted(geometry):
            die(f"{reference} carries zeta for segment(s) "
                f"{sorted(carried) or 'none'} where this domain has "
                f"{sorted(geometry)}. Matching against a different piece of "
                f"the boundary would produce a number, and it would not be a "
                f"datum.")
        when = nc.num2date(f["time"][:], f["time"].units, f["time"].calendar,
                           only_use_cftime_datetimes=False)
        # Matched on the calendar day itself rather than by differencing. The
        # reference is written with `calendar = NOLEAP`, so `num2date` hands back
        # cftime objects, and subtracting one of those from a python datetime is
        # a hard error rather than an approximation. Two files that disagree
        # about leap days should also not be silently reconciled by nearest
        # neighbour: the same date is the only sound pairing.
        day_of = lambda d: (d.year, d.month, d.day)
        theirs_by_day = {day_of(d): i for i, d in enumerate(when)}
        pairs = [(ours, theirs_by_day[day_of(day)])
                 for ours, day in enumerate(dates)
                 if day_of(day) in theirs_by_day]
        if not pairs:
            die(f"{reference} covers {when[0]:%Y-%m-%d} to {when[-1]:%Y-%m-%d}, "
                f"which does not overlap {dates[0]:%Y-%m-%d} to "
                f"{dates[-1]:%Y-%m-%d}. An offset taken across that gap is a "
                f"season, not a datum.")
        if len(pairs) < len(dates):
            print(f"{WHO}:   matching on the {len(pairs)} of {len(dates)} "
                  f"day(s) {reference.name} also covers")
        here = [ours for ours, _ in pairs]
        there = [theirs for _, theirs in pairs]
        reference_ssh = np.concatenate(
            [np.asarray(f[f"zeta_segment_{name}"][there]).ravel()
             for name in geometry])

    mine = np.concatenate([built[name]["zeta"][here].ravel() for name in geometry])
    return float(np.nanmean(mine) - np.nanmean(reference_ssh))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="what", required=True)

    one = sub.add_parser("ic", help="one snapshot, for TEMP_SALT_Z_INIT_FILE")
    one.add_argument("domain")
    one.add_argument("date", help="YYYY-MM-DD, the HYCOM day to read")
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--valid-at", metavar="YYYY-MM-DD",
                     help="the day this state is asserted to be an estimate of, "
                          "if that is not the day it was read from")
    one.add_argument("--dataset", default=URL, help=DATASET_HELP)

    many = sub.add_parser("obc", help="a time series, for the open boundaries")
    many.add_argument("domain")
    many.add_argument("start", help="YYYY-MM-DD")
    many.add_argument("end", help="YYYY-MM-DD, inclusive")
    many.add_argument("--out", type=Path, required=True)
    many.add_argument("--dataset", default=URL, help=DATASET_HELP)
    many.add_argument("--match-ssh", type=Path, metavar="OBC.NC",
                      help="remove the mean sea surface height difference "
                           "against this boundary file, so that a domain whose "
                           "interior came from the other product does not sit "
                           "in a permanent barotropic pressure gradient. The "
                           "offset is reported either way.")
    args = parser.parse_args()

    sx, sy = obc_grid.supergrid(args.domain, WHO)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    partial = args.out.with_suffix(args.out.suffix + ".partial")

    if args.what == "ic":
        when = dt.datetime.strptime(args.date, "%Y-%m-%d")
        asserted = (dt.datetime.strptime(args.valid_at, "%Y-%m-%d")
                    if args.valid_at else when)
        box = (sx.min() - obc_grid.MARGIN, sx.max() + obc_grid.MARGIN,
               sy.min() - obc_grid.MARGIN, sy.max() + obc_grid.MARGIN)

        dataset = open_dataset(args.dataset, when.year)
        found, missing = wanted_times(dataset, when, when)
        if missing:
            die(f"HYCOM has no {HOUR:02d}Z snapshot on {args.date}. The GOFS "
                f"3.1 reanalysis has documented gaps; pick a neighbouring day.")
        depth = np.asarray(dataset["depth"][:])
        ybox, xbox, src_lon, src_lat = source_box(dataset, box)

        print(f"{WHO}: {args.domain} initial condition at {args.date}")
        raw = {}
        for name in HYCOM_IC:
            print(f"{WHO}:   {name}")
            raw[name] = np.ma.filled(np.ma.masked_invalid(
                read(dataset, name, found[0][1], ybox, xbox).astype("f8")),
                np.nan)
        raw["water_temp"] = to_potential(raw["water_temp"], raw["salinity"],
                                         depth, src_lat)

        # As in `fetch-glorys.py`: the source's own stamp, which is the 12Z
        # snapshot this tool reads by choice, unless the file is asserting a
        # different day, in which case the day asserted.
        stamp = found[0][0]
        comment = valid_at = None
        if asserted != when:
            valid_at = asserted
            comment = (f"Read from {when:%Y-%m-%d} and asserted to be an "
                       f"estimate of {asserted:%Y-%m-%d}.")
        obc_grid.write_ic(
            partial, src_lon, src_lat, depth,
            {HYCOM_IC[name]: values for name, values in raw.items()},
            asserted if args.valid_at else stamp,
            source=f"HYCOM GOFS 3.1 {experiment_of(args.dataset)}, "
                   f"{when:%Y-%m-%d} {HOUR:02d}Z, in-situ temperature "
                   f"converted to potential",
            valid_at=valid_at, comment=comment)
        partial.rename(args.out)
        print(f"{WHO}: wrote {args.out}")
        return

    start = dt.datetime.strptime(args.start, "%Y-%m-%d")
    end = dt.datetime.strptime(args.end, "%Y-%m-%d")
    obc_grid.check_span(start, end, WHO)

    dataset = open_dataset(args.dataset, start.year)
    found, missing = wanted_times(dataset, start, end)
    if missing:
        die(f"HYCOM has no {HOUR:02d}Z snapshot on "
            f"{', '.join(d.strftime('%Y-%m-%d') for d in missing[:8])}"
            f"{' and more' if len(missing) > 8 else ''}. The GOFS 3.1 "
            f"reanalysis has documented gaps, and a boundary file with a hole "
            f"in its time axis interpolates across the hole while the run looks "
            f"healthy. Choose a span without them.")

    depth = np.asarray(dataset["depth"][:])
    geometry = obc_grid.segment_geometry(args.domain, sx, sy, WHO)
    dates = [day for day, _ in found]
    print(f"{WHO}: {args.domain} boundaries {args.start} to {args.end}, "
          f"{len(found)} days, {len(geometry)} segment(s), {len(depth)} levels")

    segments = {}
    for name, geo in geometry.items():
        print(f"{WHO}:   segment {name}")
        segments[name] = sample_segment(dataset, geo, found, depth)

    offset = None
    if args.match_ssh:
        offset = ssh_offset(segments, args.match_ssh, geometry, dates)
        print(f"{WHO}: mean sea surface height is {offset:+.4f} m against "
              f"{args.match_ssh.name}, removing it")
        for name in segments:
            segments[name]["zeta"] = segments[name]["zeta"] - offset

    source = (f"HYCOM GOFS 3.1 {experiment_of(args.dataset)} {HOUR:02d}Z "
              f"snapshots, {dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d}, in-situ "
              f"temperature converted to potential")
    if offset is not None:
        source += (f", sea surface height shifted by {-offset:+.4f} m to match "
                   f"{args.match_ssh.name}")
    obc_grid.write_obc(partial, segments, depth, dates, geometry, source)
    partial.rename(args.out)
    print(f"{WHO}: wrote {args.out} with {len(dates)} records")


if __name__ == "__main__":
    main()
