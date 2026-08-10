#!/usr/bin/env python3
"""Build the ERA5 half of the forcing archive: one `atm.nc` over a domain's box.

    tools/forcing-era5.py 2021-06-25 2021-09-05 --domain gom_25km \\
        --out $ACKBAR_STATIC_ROOT/forcing/gom_25km/era5

ERA5 forces the *truth* run of the OSSE and nothing else. The experiments read a
different source, which is the whole point of a fraternal twin: an OSSE whose
truth and whose experiments share an atmosphere is measuring the analysis against
a nature run that had no weather the analysis did not already know.

**Ask for a wider span than the experiment.** `time_interp_external` refuses to
extrapolate, so a run whose first step lands before the first record dies with
"time ... is before range of list" rather than holding the first value. A day of
margin at each end costs about 15 MB.

## Where it comes from

NCAR's mirror of ERA5 on S3, `nsf-ncar-era5`, anonymous and about 56 MB/s from
rancor. Two families, because ERA5 splits them and they are honestly different
things:

- `e5.oper.an.sfc`, hourly analysis, one file per variable per month. Values are
  instantaneous and stamped at the hour they are valid.
- `e5.oper.fc.sfc.meanflux`, hourly *mean rates* over the preceding hour, one
  file per variable per half month, shaped (forecast_initial_time, forecast_hour)
  from the 06Z and 18Z forecast legs at steps 1 to 12. Those tile the calendar
  exactly once, and each value is stamped here at its interval midpoint, half
  past the hour.

Mean rates rather than accumulations is worth noticing: there is no
de-accumulation step and therefore no way to get one wrong, which is the failure
that makes precipitation too large by the number of intervals since the last
reset. GEFS is not so kind.

## Why the download is global and the archive is not

The mirror is one file per variable per month for the whole globe, with no server
side subsetting, so the box is cut here. Each file is downloaded, its box read
out, and the file deleted before the next one starts: peak disk is one file, and
what lands in the archive is about a thousandth of what crossed the network. Two
months of all eight variables is roughly 14 GB transferred and 200 MB kept.

## What is not written

Surface pressure, which is fetched because the humidity conversion needs it and
then dropped because nothing in MOM6 here reads a real one. See
`src/ackbar/forcing.py`.
"""

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

import netCDF4
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ackbar.forcing import (  # noqa: E402
    FIELDS, PRODUCT_HEIGHT, REFERENCE_HEIGHT, assert_no_leap_day, box_slices,
    domain_box, shift_to_10m, specific_humidity, write_atm)

BUCKET = "s3://nsf-ncar-era5"

#: Output name -> the ERA5 parameter code that carries it, for the hourly
#: analysis family. `sp` has no output name of its own: it is read for the
#: humidity conversion and dropped.
INSTANT = {
    "T2":  "128_167_2t",
    "U10": "128_165_10u",
    "V10": "128_166_10v",
    "_2d": "128_168_2d",
    "_sp": "128_134_sp",
}

#: Skin temperature, read only for the height shift and dropped like `_sp`.
#:
#: Skin rather than an ocean analysis, and ERA5's own rather than GLORYS, for
#: the reason `shift_to_10m` gives: the 2 m fields were produced by ERA5's
#: surface layer over this surface, so inverting that profile is self-consistent
#: only against it. `sst` is not in this family in any case; `skt` is.
SURFACE = "128_235_skt"

#: The same for the hourly mean flux family.
MEANFLUX = {
    "DSWRF": "235_035_msdwswrf",
    "DLWRF": "235_036_msdwlwrf",
    "PRATE": "235_055_mtpr",
}

#: The two timestamps every file in this mirror carries, as `YYYYMMDDHH`.
SPAN = re.compile(r"(\d{10})_(\d{10})\.nc$")


def run(*args):
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"forcing-era5: {' '.join(args)}\n{out.stderr.strip()}")
    return out.stdout


def month_keys(start, end):
    """`YYYYMM` for every month the span touches, plus the one before it.

    The month before is not defensive: the mean flux file that carries the first
    six hours of a month begins on the sixteenth of the month before, because the
    06Z leg that covers 00Z to 06Z was initialised the previous evening.
    """
    keys, when = [], datetime.datetime(start.year, start.month, 1)
    when = (when - datetime.timedelta(days=1)).replace(day=1)
    while when <= end:
        keys.append(f"{when:%Y%m}")
        when = (when.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)
    return keys


def listing(family, key, code):
    """Files of one variable in one month, as (name, first hour, last hour)."""
    found = []
    for line in run("aws", "s3", "ls", "--no-sign-request",
                    f"{BUCKET}/{family}/{key}/").splitlines():
        name = line.split()[-1]
        if f".{code}." not in name:
            continue
        span = SPAN.search(name)
        if span is None:
            raise SystemExit(f"forcing-era5: cannot read a span from {name}")
        first, last = (datetime.datetime.strptime(s, "%Y%m%d%H")
                       for s in span.groups())
        found.append((name, first, last))
    return found


def wanted(family, code, start, end):
    """Every file of one variable whose span meets [*start*, *end*]."""
    files = []
    for key in month_keys(start, end):
        for name, first, last in listing(family, key, code):
            if last >= start and first <= end:
                files.append((f"{BUCKET}/{family}/{key}/{name}", name))
    if not files:
        raise SystemExit(
            f"forcing-era5: {code} has no file covering {start:%Y-%m-%d} to "
            f"{end:%Y-%m-%d}")
    return files


def payload(dataset):
    """The one variable in an ERA5 file that is not a coordinate.

    By shape rather than by name: the mirror names the analysis variables
    `VAR_2T`, `VAR_10U` and so on but the pressure one plainly `SP`, and the
    mean flux ones `MSDWSWRF`, so a table of names is a table that will be wrong
    the first time a new variable is added.
    """
    named = [v for v in dataset.variables.values() if v.ndim >= 3]
    if len(named) != 1:
        raise SystemExit(
            f"forcing-era5: expected one field, found {[v.name for v in named]}")
    return named[0]


def read(url, name, cache, origin, box):
    """Download one file, read the box out of it, delete it.

    Returns `(hours since origin, cube)` with *cube* (time, lat, lon) on an
    ascending grid, and the grid itself.
    """
    local = cache / name
    if not local.exists():
        run("aws", "s3", "cp", "--no-sign-request", "--only-show-errors",
            url, str(local) + ".part")
        Path(str(local) + ".part").rename(local)
    try:
        with netCDF4.Dataset(local) as source:
            lons = source["longitude"][:].astype("f8")
            lats = source["latitude"][:].astype("f8")
            sy, sx, y, x, flip_y = box_slices(lons, lats, box)
            field = payload(source)

            if "forecast_initial_time" in field.dimensions:
                # A mean rate over the hour ending at `forecast_hour`, so it
                # belongs at the midpoint of that hour and not at its end.
                # Stamping an interval mean at its end shifts the diurnal cycle
                # of shortwave by half an hour, which over two months is a
                # systematic bias in the mixed layer rather than noise.
                init = netCDF4.num2date(
                    source["forecast_initial_time"][:],
                    source["forecast_initial_time"].units,
                    source["forecast_initial_time"].calendar,
                    only_use_cftime_datetimes=False)
                step = source["forecast_hour"][:].astype("f8")
                cube = field[:, :, sy, sx]
                offsets = np.array(
                    [(t - origin).total_seconds() / 3600.0 for t in init])
                hours = (offsets[:, None] + step[None, :] - 0.5).ravel()
                cube = cube.reshape((-1,) + cube.shape[2:])
            else:
                when = netCDF4.num2date(
                    source["time"][:], source["time"].units,
                    source["time"].calendar, only_use_cftime_datetimes=False)
                cube = field[:, sy, sx]
                hours = np.array(
                    [(t - origin).total_seconds() / 3600.0 for t in when])

            cube = np.ma.filled(cube, np.nan).astype("f4")
            if flip_y:
                cube = cube[:, ::-1, :]
            return hours, cube, x, y
    finally:
        # The archive is the product; the download is scratch. Two months of all
        # eight variables is 14 GB that has no reason to survive the run that
        # read it.
        local.unlink(missing_ok=True)


def gather(family, code, start, end, cache, origin, box):
    """One variable over the whole span, ordered, trimmed, duplicates dropped."""
    hours, cubes, grid = [], [], None
    for url, name in sorted(wanted(family, code, start, end), key=lambda f: f[1]):
        h, cube, x, y = read(url, name, cache, origin, box)
        if grid is None:
            grid = (x, y)
        elif not (np.array_equal(grid[0], x) and np.array_equal(grid[1], y)):
            raise SystemExit(f"forcing-era5: {name} is on a different grid")
        hours.append(h)
        cubes.append(cube)
    hours = np.concatenate(hours)
    cube = np.concatenate(cubes)
    order = np.argsort(hours, kind="stable")
    hours, cube = hours[order], cube[order]
    # Consecutive half month files share their joining hour, and the analysis
    # family can be asked for a month twice by a span that starts and ends in it.
    keep = np.concatenate([[True], np.diff(hours) > 0])
    hours, cube = hours[keep], cube[keep]
    # The mirror's granularity is a month and the request's is a day, so a two
    # day span arrives holding two months. Trimmed here rather than left in:
    # what is written should be what was asked for, or the archive's span stops
    # being readable from the command that built it.
    span = (end - origin).total_seconds() / 3600.0
    inside = (hours >= 0.0) & (hours <= span)
    if not inside.any():
        raise SystemExit(f"forcing-era5: {code} has no record inside the span")
    return hours[inside], cube[inside], grid


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="first day to cover, YYYY-MM-DD")
    ap.add_argument("end", help="last day to cover, YYYY-MM-DD, inclusive")
    ap.add_argument("--domain", required=True,
                    help="clip to this domain's grid, plus a margin. Reads "
                         "$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/"
                         "ocean_hgrid.nc, so the domain must already have one")
    ap.add_argument("--out", type=Path, required=True,
                    help="archive directory for this source")
    ap.add_argument("--member", default="mem000",
                    help="which member this is. ERA5 has one, so the default is "
                         "the only value that means anything until a "
                         "deterministic source is fanned out across an "
                         "ensemble, which is symlinks rather than fetches")
    ap.add_argument("--cache", type=Path,
                    help="where downloads land before being read and deleted. "
                         "Default: <out>/.cache")
    ap.add_argument("--no-height-shift", action="store_true",
                    help="archive T2 and Q2 at the 2 m ERA5 publishes them at, "
                         "instead of shifting them to the 10 m the data table "
                         "declares. The archive records which it is. Carries a "
                         "turbulent flux bias of about ten per cent in one "
                         "direction: see docs/forcing-reference-height.md")
    args = ap.parse_args()

    start = datetime.datetime.strptime(args.start, "%Y-%m-%d")
    end = (datetime.datetime.strptime(args.end, "%Y-%m-%d")
           + datetime.timedelta(hours=23))
    if end <= start:
        raise SystemExit("forcing-era5: end is not after start")
    assert_no_leap_day(start, end)
    box = domain_box(args.domain)
    print(f"forcing-era5: {args.domain} box {box['west']:.3f} to "
          f"{box['east']:.3f} east, {box['south']:.3f} to "
          f"{box['north']:.3f} north", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.cache or (args.out / ".cache")
    cache.mkdir(parents=True, exist_ok=True)

    origin = start
    series, grid = {}, None
    raw = {}
    shift = not args.no_height_shift
    reading = dict(INSTANT)
    if shift:
        reading["_TS"] = SURFACE
    for name, code in reading.items():
        print(f"forcing-era5: {name} from {code}", flush=True)
        hours, cube, grid = gather("e5.oper.an.sfc", code, start, end, cache,
                                   origin, box)
        raw[name] = (hours, cube)

    if not np.array_equal(raw["_2d"][0], raw["_sp"][0]):
        raise SystemExit("forcing-era5: dewpoint and pressure disagree on time")
    q2 = specific_humidity(raw["_2d"][1], raw["_sp"][1])
    t2 = raw["T2"][1]
    if shift:
        # Every field here is hourly instantaneous analysis out of one family,
        # so they share a time axis. Checked rather than assumed, because the
        # shift combines four of them pointwise and a silent offset between two
        # would be a plausible looking field rather than an error.
        for name in ("U10", "V10", "_TS", "_2d"):
            if not np.array_equal(raw[name][0], raw["T2"][0]):
                raise SystemExit(
                    f"forcing-era5: {name} and T2 disagree on time, so the "
                    f"height shift would combine two different hours")
        t2, q2 = shift_to_10m(t2, q2, np.hypot(raw["U10"][1], raw["V10"][1]),
                              raw["_TS"][1])
    series["T2"] = (raw["T2"][0], t2)
    series["Q2"] = (raw["_2d"][0], q2)
    for name in ("U10", "V10"):
        series[name] = raw[name]

    for name, code in MEANFLUX.items():
        print(f"forcing-era5: {name} from {code}", flush=True)
        hours, cube, grid = gather("e5.oper.fc.sfc.meanflux", code, start, end,
                                   cache, origin, box)
        series[name] = (hours, cube)

    x, y = grid
    # One file per member in one directory, `mem000.nc` upward, which is the
    # layout the boundary archive uses. What the file is called inside the run
    # directory is the stager's business, not the archive's.
    target = args.out / f"{args.member}.nc"
    write_atm(target, x, y, origin, series,
              source="ERA5, NCAR mirror ds633.0", domain=args.domain,
              scalar_height=REFERENCE_HEIGHT if shift else PRODUCT_HEIGHT)
    if not any(cache.iterdir()):
        cache.rmdir()

    print(f"forcing-era5: {target}")
    for name in FIELDS:
        hours, cube = series[name]
        print(f"  {name:<6} {cube.shape[0]:5d} records, "
              f"{hours[0]:+.1f} to {hours[-1]:+.1f} h, "
              f"min {np.nanmin(cube):.4g} max {np.nanmax(cube):.4g}")


if __name__ == "__main__":
    main()
