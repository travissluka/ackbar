#!/usr/bin/env python3
"""Build a domain's initial condition and open boundary conditions from GLORYS.

    env -u PYTHONPATH .venv-data/bin/python tools/fetch-glorys.py ic  <domain> <date>
    env -u PYTHONPATH .venv-data/bin/python tools/fetch-glorys.py obc <domain> <start> <end>

Writes `ic.nc` and `obc.nc` into `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/`,
which is where `TEMP_SALT_Z_INIT_FILE` and `OBC_SEGMENT_00N_DATA` look, unless
`--out` names somewhere else.

Everything that is about the *domain* rather than about GLORYS lives in
`tools/obc_grid.py` and is shared with `tools/fetch-hycom.py`: where the
segments are, how land is filled, what the time axis has to say, and the two
writers. Two sources that disagreed about any of those would not be two
estimates of the same boundary. What is here is what is genuinely GLORYS.

**`.venv-data/bin/python`, not `.venv/bin/python`, and with `PYTHONPATH`
unset.** The Copernicus toolbox needs a `typing_extensions` newer than the one
spack-stack puts on `PYTHONPATH`, and the project venv inherits that.
`.venv-data` is built with `env -u PYTHONPATH` for exactly that reason, and the
same has to hold when it is *run*: a virtual environment does not shadow
`PYTHONPATH`, so naming the interpreter is not by itself enough once
`site/activate.sh` has been sourced. And it has been, because that is where
`ACKBAR_STATIC_ROOT` comes from. Hence the `env -u` above.

Without it the failure is an `ImportError` on `typing_extensions.Sentinel`,
raised from inside pydantic several frames below anything recognisable, after
the argument parsing has succeeded and before anything is downloaded.

Needs `copernicusmarine login` to have been run once; it leaves credentials in
`~/.copernicusmarine/`.

## Why GLORYS, and why both halves from it

What was here before was SODA3.3.1: a single 5-day mean centred 2015-01-04, at
half a degree, for *both* files. The initial condition being a single snapshot
is normal. The boundary being one is not: `obc.nc` carried `time = 1`, so the
Yucatan Channel inflow that drives the entire Loop Current was frozen at one
January state for every run this repository has ever done, in July or any other
month.

GLORYS12V1 is 1/12 degree, 50 levels, daily, 1993 onward. It is a NEMO
reanalysis assimilating altimetry, SST and in-situ, and it is what the regional
MOM6 world nests off.

Both files come from the same product on purpose. An open boundary and an
interior drawn from different analyses disagree about sea surface height by a
constant offset, and under FLATHER that offset is a barotropic pressure gradient
the model will happily accelerate a current down. They agree here because they
are the same field sampled in two places.

That is a rule about one *experiment*, not about the repository. Building the
boundary from a second product is what `tools/fetch-hycom.py` is for, and it
carries `--match-ssh` to remove exactly this offset when the interior is still
this one's.

## What GLORYS calls things

`thetao` really is potential temperature, which is what `Z_INIT_FILE_PTEMP_VAR`
wants, and `so` really is practical salinity, so neither needs converting. That
is not true of every product: HYCOM publishes in-situ temperature and has to be
converted.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import obc_grid

WHO = "fetch-glorys"

GLORYS_IC = {"thetao": "temp", "so": "salt"}
GLORYS_OBC = {"thetao": "temp", "so": "salt", "uo": "u", "vo": "v",
              "zos": "zeta"}

DATASET = "cmems_mod_glo_phy_my_0.083deg_P1D-m"


def fetch(variables, box, start, end):
    """A GLORYS subset, loaded into memory.

    Imported here rather than at module scope so that `--help` works under a
    python that does not have the toolbox.
    """
    import copernicusmarine

    west, east, south, north = box
    data = copernicusmarine.open_dataset(
        dataset_id=DATASET,
        variables=list(variables),
        minimum_longitude=west, maximum_longitude=east,
        minimum_latitude=south, maximum_latitude=north,
        start_datetime=start.strftime("%Y-%m-%dT00:00:00"),
        end_datetime=end.strftime("%Y-%m-%dT00:00:00"),
    )
    return data.load()


def sample_segment(geo, start, end):
    """Every OBC field along one segment, for one date range.

    Fetched as a strip around the segment rather than over the whole domain.
    A season of the full box is about 8 GB in memory and 14 times more data
    than three boundaries need; a strip is a tenth of that and each segment
    is independent, so nothing is held that is not about to be used.
    """
    box = (geo["lon"].min() - obc_grid.STRIP_MARGIN,
           geo["lon"].max() + obc_grid.STRIP_MARGIN,
           geo["lat"].min() - obc_grid.STRIP_MARGIN,
           geo["lat"].max() + obc_grid.STRIP_MARGIN)
    data = fetch(GLORYS_OBC, box, start, end)
    src_lon = np.asarray(data["longitude"])
    src_lat = np.asarray(data["latitude"])

    fields = {short: np.asarray(data[source])
              for source, short in GLORYS_OBC.items() if source in data}
    sampled = obc_grid.sample_onto(geo, fields, src_lon, src_lat)
    dates = [dt.datetime.utcfromtimestamp(np.datetime64(t, "s").astype("int64"))
             for t in np.asarray(data["time"])]
    return sampled, np.asarray(data["depth"]), dates


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="what", required=True)

    one = sub.add_parser("ic", help="one snapshot, for TEMP_SALT_Z_INIT_FILE")
    one.add_argument("domain")
    one.add_argument("date", help="YYYY-MM-DD, the GLORYS day to read")
    one.add_argument("--out", type=Path,
                     help="where to write, if not the domain's own ic.nc")
    one.add_argument("--valid-at", metavar="YYYY-MM-DD",
                     help="the day this state is asserted to be an estimate "
                          "of, if that is not the day it was read from. An "
                          "OSSE control is built by reading the same season "
                          "of a different year: the ocean is real and the "
                          "mesoscale is an independent draw, so the error is "
                          "a Loop Current in the wrong place rather than a "
                          "seasonal bias. Nothing in MOM6 reads the date out "
                          "of this file, so this exists to keep the file from "
                          "claiming to be something it is not.")

    many = sub.add_parser("obc", help="a time series, for the open boundaries")
    many.add_argument("domain")
    many.add_argument("start", help="YYYY-MM-DD")
    many.add_argument("end", help="YYYY-MM-DD, inclusive")
    many.add_argument("--out", type=Path,
                      help="where to write, if not the domain's own obc.nc")

    args = parser.parse_args()
    out = obc_grid.domain_input(args.domain, WHO)
    sx, sy = obc_grid.supergrid(args.domain, WHO)

    if args.what == "ic":
        source_date = dt.datetime.strptime(args.date, "%Y-%m-%d")
        when = (dt.datetime.strptime(args.valid_at, "%Y-%m-%d")
                if args.valid_at else source_date)
        if when != source_date:
            print(f"{WHO}: {args.domain} initial condition read from "
                  f"{args.date}, asserted valid at {args.valid_at}")
        else:
            print(f"{WHO}: {args.domain} initial condition at {args.date}")

        box = (sx.min() - obc_grid.MARGIN, sx.max() + obc_grid.MARGIN,
               sy.min() - obc_grid.MARGIN, sy.max() + obc_grid.MARGIN)
        data = fetch(GLORYS_IC, box, source_date, source_date)

        # The time axis takes the source's own stamp, which for a GLORYS daily
        # mean is 00Z, the start of the day it averages rather than the middle
        # of it, unless this file is asserting a
        # different day, in which case it takes the day being asserted: the
        # assertion exists so a cycle starting at T00 can begin from another
        # day's ocean, and the hour of the day it was read from is not part of
        # the claim. See `obc_grid.write_obc` for why the hour is kept at all.
        stamp = dt.datetime.utcfromtimestamp(
            np.datetime64(np.asarray(data["time"])[0], "s").astype("int64"))
        when = when if args.valid_at else stamp

        comment = valid_at = None
        midnight = when.replace(hour=0, minute=0, second=0)
        if args.valid_at and source_date != midnight:
            # The deliberate case: this file holds one day's ocean and claims to
            # be an estimate of another.
            valid_at = when
            comment = (
                f"Read from {source_date:%Y-%m-%d} and asserted to be an "
                f"estimate of {when:%Y-%m-%d}. The difference between the two "
                "oceans is the initial error an OSSE built on this exists to "
                "correct. See tools/restamp-ic.sh, which makes the same "
                "assertion about a restart set.")
        elif source_date != midnight:
            # The accidental case, and it used to be described as the deliberate
            # one with the two dates the wrong way round: nothing was asserted
            # here, the server simply answered with a day other than the one it
            # was asked for. Said plainly, because a silent substitution is how
            # an experiment ends up initialised a day off with no record of it.
            comment = (
                f"Requested {source_date:%Y-%m-%d}; the source returned "
                f"{midnight:%Y-%m-%d}, and that is the ocean in this file. No "
                "assertion was made about the date, and none is implied.")

        target = args.out or (out / "ic.nc")
        partial = target.with_suffix(target.suffix + ".partial")
        obc_grid.write_ic(
            partial,
            np.asarray(data["longitude"]), np.asarray(data["latitude"]),
            np.asarray(data["depth"]),
            {short: np.asarray(data[source])
             for source, short in GLORYS_IC.items()},
            when,
            source=f"GLORYS12V1 {DATASET}, {source_date:%Y-%m-%d}",
            valid_at=valid_at, comment=comment)
        partial.rename(target)
        print(f"{WHO}: wrote {target}")
        return

    start = dt.datetime.strptime(args.start, "%Y-%m-%d")
    end = dt.datetime.strptime(args.end, "%Y-%m-%d")
    obc_grid.check_span(start, end, WHO)

    geometry = obc_grid.segment_geometry(args.domain, sx, sy, WHO)
    print(f"{WHO}: {args.domain} boundaries {args.start} to {args.end}, "
          f"{len(geometry)} segment(s)")

    segments, depth, dates = {}, None, None
    for name, geo in geometry.items():
        print(f"{WHO}:   segment {name}")
        segments[name], depth, dates = sample_segment(geo, start, end)

    target = args.out or (out / "obc.nc")
    partial = target.with_suffix(target.suffix + ".partial")
    obc_grid.write_obc(
        partial, segments, depth, dates, geometry,
        source=f"GLORYS12V1 {DATASET}, "
               f"{dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d}")
    partial.rename(target)
    print(f"{WHO}: wrote {target} with {len(dates)} records")


if __name__ == "__main__":
    main()
