#!/usr/bin/env python
"""Build a small observation archive so that the observation path can be tested.

    .venv/bin/python tools/obs-archive-smoke.py \
        --start 1958-01-01T00:00:00Z --bin P1D --count 3 \
        --out $ACKBAR_STATIC_ROOT/obs/soca-test-smoke/1958

The archive an experiment reads is an offline product, and the real one is
either OSSE observations generated from a truth run or converted real
observations. Neither exists before hofx does, and hofx cannot be brought up
without observations to evaluate, so this breaks the circle: it takes the ioda
files the SOCA bundle ships as test data and files them under the layout ACKBAR
expects, at the dates an experiment asks for.

**The layout is `<platform>/<bin start>.nc4`**, fixed time bins that know
nothing about any assimilation cycle, the same as `obs-archive-osse.py` writes.
`src/ackbar/obsarchive.py` is the reference. The bin has to be at least as long
as the source file's own span, or the retimed observations would fall outside
the bin they are filed in and the archive would no longer be contiguous, which
is the one property `stage.obs` relies on. That is checked rather than assumed:
the bundle's files span a little under a day, so `P1D` works and `PT12H` is
refused.

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
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ackbar import obsarchive  # noqa: E402
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
                        help="the first bin's start, ISO 8601")
    parser.add_argument("--bin", required=True, dest="span",
                        help="time bin the archive is filed in, ISO 8601. At "
                             "least the source's own span, so P1D here")
    parser.add_argument("--count", type=int, required=True, help="how many bins")
    parser.add_argument("--out", required=True, type=Path,
                        help="the archive directory: obs/<source>/<period>")
    parser.add_argument("--platforms", nargs="*", default=sorted(PLATFORMS),
                        help=f"a subset of {sorted(PLATFORMS)}")
    args = parser.parse_args(argv)

    # Before the output directory is touched, so a typo is a rejected request
    # rather than a half-built archive plus a KeyError from inside the loop.
    unknown = sorted(set(args.platforms) - set(PLATFORMS))
    if unknown:
        parser.error(f"no such platform: {', '.join(unknown)}. "
                     f"The platforms are {', '.join(sorted(PLATFORMS))}")

    start = parse_instant(args.start)
    span = parse_duration(args.span)

    # Before anything is written: a bin shorter than the source's own span would
    # file observations outside the bin they are named for, and `stage.obs`
    # would then stage a window and miss some of what fell in it. The archive
    # this builds is contiguous by construction only if every retimed
    # observation lands inside its own bin.
    for platform in args.platforms:
        _check_span(platform, span)

    for index in range(args.count):
        begin = start + index * span
        for platform in args.platforms:
            written = write_one(platform, args.out / platform, begin, span)
            print(f"obs-archive-smoke: {written}")
    return 0


def _offsets(platform):
    """The source file's own time offsets, in seconds about its own epoch."""
    source = SOURCE_DIR / PLATFORMS[platform]
    if not source.exists():
        raise SystemExit(f"obs-archive-smoke: {source} does not exist")
    with netCDF4.Dataset(source) as data:
        data.set_auto_mask(False)
        values = np.asarray(data["MetaData"]["dateTime"][:]).ravel()
    return float(values.min()), float(values.max())


def _check_span(platform, span):
    """Refuse a bin the source does not fit in, and say by how much."""
    low, high = _offsets(platform)
    seconds = span.total_seconds()
    # The epoch goes at the bin's centre, so the source's own centre lands
    # there and its extremes land `seconds/2 + offset` from the bin's start.
    first, last = seconds / 2.0 + low, seconds / 2.0 + high
    if first < 0.0 or last > seconds:
        raise SystemExit(
            f"obs-archive-smoke: {platform} spans {(high - low) / 3600.0:.1f} h "
            f"and the bin is {seconds / 3600.0:.1f} h, so retiming it would put "
            f"observations outside the bin they are filed in. Use a longer "
            f"--bin; the bundle's files want P1D.")


def write_one(platform, target, begin, span):
    """One platform's file for one bin, retimed onto the bin's centre."""
    source = SOURCE_DIR / PLATFORMS[platform]
    if not source.exists():
        raise SystemExit(f"obs-archive-smoke: {source} does not exist")

    target.mkdir(parents=True, exist_ok=True)
    path = target / obsarchive.name(begin)
    shutil.copyfile(source, path)
    with netCDF4.Dataset(path, "a") as data:
        # Offsets are kept and the epoch is moved, so the observations stay
        # spread the way they were spread in the original file. The epoch is the
        # bin's centre, which is where the source's own centre belongs and is
        # what keeps its extremes inside the bin.
        centre = begin + span / 2
        data["MetaData"]["dateTime"].units = (
            f"seconds since {centre:%Y-%m-%dT%H:%M:%SZ}"
        )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
