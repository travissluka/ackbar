#!/usr/bin/env python
"""Build a small observation archive so that the observation path can be tested.

    .venv/bin/python tools/obs-archive-smoke.py \
        --start 1958-01-01T12:00:00Z --length PT12H --count 3 \
        --out $ACKBAR_STATIC_ROOT/obs/soca-test-smoke/1958

The archive an experiment reads is an offline product, and the real one is
either OSSE observations generated from a truth run or converted real
observations. Neither exists before hofx does, and hofx cannot be brought up
without observations to evaluate, so this breaks the circle: it takes the ioda
files the SOCA bundle ships as test data and files them under the layout ACKBAR
expects, at the dates an experiment asks for.

**What it changes is one attribute.** ioda stores observation time as an offset
in seconds against an epoch in `MetaData/dateTime:units`, so moving a file to a
different date is rewriting that string. Positions, values and errors are
untouched, which is the point: these are real observation locations with real
error estimates, and only their date is a fiction.

**What it is not.** The values were observed in 2018 and the background they
will be evaluated against is not, so every departure this archive produces is
meaningless as science. It exists to make hofx run end to end over a real
archive layout with real ioda files, including the cycles where a file is
missing. Delete it and regenerate rather than accumulating trust in it.
"""

import argparse
import shutil
import sys
from pathlib import Path

import netCDF4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ackbar.duration import parse_duration, parse_instant  # noqa: E402

#: Which bundle test file stands in for which platform. The names on the left
#: are the observer names in `config/layers/obs/`, and they are what the archive
#: is keyed on; the files on the right are whatever the bundle has of that kind.
PLATFORMS = {
    "adt_3a": "adt.nc",
    "sst_noaa19": "sst.nc",
}

SOURCE_DIR = REPO / "pkg/jedi/soca/test/Data/obs"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True,
                        help="the first cycle's analysis time, ISO 8601")
    parser.add_argument("--length", required=True, help="cycle length, ISO 8601")
    parser.add_argument("--count", type=int, required=True, help="how many cycles")
    parser.add_argument("--out", required=True, type=Path,
                        help="the archive directory: obs/<source>/<period>")
    parser.add_argument("--platforms", nargs="*", default=sorted(PLATFORMS),
                        help=f"a subset of {sorted(PLATFORMS)}")
    args = parser.parse_args(argv)

    start = parse_instant(args.start)
    length = parse_duration(args.length)

    for index in range(args.count):
        analysis = start + index * length
        # The same window the workflow computes: centred on the analysis time
        # and as long as the cycle, so consecutive windows tile without gap.
        begin = analysis - length / 2
        target = args.out / analysis.strftime("%Y%m%d%H")
        target.mkdir(parents=True, exist_ok=True)
        for platform in args.platforms:
            written = write_one(platform, target, analysis, begin)
            print(f"obs-archive-smoke: {written}")
    return 0


def write_one(platform, target, analysis, begin):
    """One platform's file for one window, retimed onto the analysis time."""
    source = SOURCE_DIR / PLATFORMS[platform]
    if not source.exists():
        raise SystemExit(f"obs-archive-smoke: {source} does not exist")

    path = target / f"{platform}.{begin:%Y%m%d%H}.nc4"
    shutil.copyfile(source, path)
    with netCDF4.Dataset(path, "a") as data:
        # Offsets are kept and the epoch is moved, so the observations stay
        # spread across the window the way they were spread across the original
        # one. Any that fall outside are dropped by the time window, which is
        # ioda doing its job rather than a defect in the archive.
        data["MetaData"]["dateTime"].units = (
            f"seconds since {analysis:%Y-%m-%dT%H:%M:%SZ}"
        )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
