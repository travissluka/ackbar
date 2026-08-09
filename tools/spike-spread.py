#!/usr/bin/env python3
"""Measure how much ensemble spread each spike group produced, and where.

    tools/spike-spread.py --root /data/ackbar/spike/params
    tools/spike-spread.py --root /data/ackbar/spike/params --json spread.json

For each group of members, the spread is the standard deviation across members
at a point, reduced over the domain by an area unweighted root mean square.
Reported per day, because the question is not how big the spread is but how
fast it grows: an ensemble has to regenerate each cycle what the analysis takes
out of it.

`--json` writes the same numbers plus the depth resolved temperature and
salinity profiles, which is what answers "where". A method that only moves the
mixed layer and a method that only moves the thermocline both show up as a
temperature spread in the table and are not interchangeable.

**A perturbed parameter group is not an ensemble and its spread is not a
forecast spread.** Five members holding five different values of one parameter
are five different models, so the standard deviation across them measures how
sensitive the solution is to that parameter over its uncertainty range. That is
the right quantity for deciding what to perturb. It is not the right quantity to
hand a filter, because the members are not exchangeable and their mean is not an
unbiased estimate of anything. The `stanley_seed` group is the exception: one
model, five draws, and its spread is a spread.
"""

import argparse
import json
from pathlib import Path

import netCDF4
import numpy

SURFACE = ("SST", "SSS", "SSH", "MLD_003")
COLUMN = ("temp", "salt")

#: Depth bands that name themselves in a report. The Gulf's thermocline sits in
#: the second and third; below the fourth is effectively the deep basin.
BANDS = ((0, 30, "0-30 m"), (30, 100, "30-100 m"), (100, 300, "100-300 m"),
         (300, 700, "300-700 m"), (700, 6000, "below 700 m"))


def members(group):
    """Every member directory under *group*, sorted, with its daily file."""
    found = []
    for entry in sorted(group.iterdir()):
        if not entry.is_dir():
            continue
        daily = sorted(entry.glob("*ocn_daily.nc"))
        if daily:
            found.append((entry.name, daily[0]))
    return found


def stack(paths, field):
    """(member, time, ...) with land as NaN."""
    planes = []
    for path in paths:
        with netCDF4.Dataset(path) as data:
            planes.append(numpy.ma.filled(data[field][:].astype("f8"), numpy.nan))
    return numpy.stack(planes)


def spread(cube):
    """Root mean square over space of the across-member standard deviation.

    ddof=1: five members estimating a population, not describing themselves.
    Without it every number here is 10% low, which is small enough to survive
    review and large enough to matter when the answer is compared against a
    filter's spread loss per cycle.
    """
    sigma = numpy.nanstd(cube, axis=0, ddof=1)
    flat = sigma.reshape(sigma.shape[0], -1)
    return numpy.sqrt(numpy.nanmean(flat ** 2, axis=1))


def profile(cube, depths):
    """Spread by depth band at the last time, as {band: value}."""
    sigma = numpy.nanstd(cube, axis=0, ddof=1)[-1]
    out = {}
    for top, bottom, name in BANDS:
        pick = (depths >= top) & (depths < bottom)
        if not pick.any():
            continue
        out[name] = float(numpy.sqrt(numpy.nanmean(sigma[pick] ** 2)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--groups", default="", help="comma separated, default all")
    ap.add_argument("--flat", action="store_true",
                    help="root is itself one ensemble, not a directory of them; "
                         "this is how the GEFS forcing suite is laid out, its "
                         "members named for the atmosphere that drove them")
    args = ap.parse_args()

    if args.flat:
        groups = [args.root]
    else:
        wanted = [g.strip() for g in args.groups.split(",") if g.strip()]
        groups = sorted(e for e in args.root.iterdir()
                        if e.is_dir() and e.name != "namelists"
                        and (not wanted or e.name in wanted))

    report = {}
    for group in groups:
        found = members(group)
        if len(found) < 2:
            continue
        paths = [path for _, path in found]
        with netCDF4.Dataset(paths[0]) as data:
            depths = data["zl"][:].astype("f8")
            days = data["time"][:].astype("f8")

        entry = {"members": [name for name, _ in found], "days": days.tolist(),
                 "surface": {}, "column": {}, "profile": {}}
        for field in SURFACE:
            entry["surface"][field] = spread(stack(paths, field)).tolist()
        for field in COLUMN:
            cube = stack(paths, field)
            sigma = numpy.nanstd(cube, axis=0, ddof=1)
            flat = sigma.reshape(sigma.shape[0], -1)
            entry["column"][field] = numpy.sqrt(numpy.nanmean(flat ** 2, axis=1)).tolist()
            entry["profile"][field] = profile(cube, depths)
        report[group.name] = entry

    order = sorted(report, key=lambda g: -report[g]["surface"]["SST"][-1])
    print(f"{'group':14s} {'n':>2s}  "
          + "  ".join(f"{f:>9s}" for f in ("SST d1", "SST d5", "d5-d4",
                                           "SSH d5", "MLD d5", "T d5", "S d5")))
    print("-" * 92)
    for name in order:
        e = report[name]
        sst, ssh, mld = e["surface"]["SST"], e["surface"]["SSH"], e["surface"]["MLD_003"]
        temp, salt = e["column"]["temp"], e["column"]["salt"]
        print(f"{name:14s} {len(e['members']):2d}  "
              f"{sst[0]:9.4f}  {sst[-1]:9.4f}  {sst[-1] - sst[-2]:9.4f}  "
              f"{ssh[-1]:9.4f}  {mld[-1]:9.3f}  {temp[-1]:9.4f}  {salt[-1]:9.4f}")
    print("\nSST and T in degC, SSS and S in psu, SSH in m, MLD in m. "
          "'d5-d4' is the spread added by the fifth day.")

    print(f"\n{'group':14s} " + "  ".join(f"{n:>12s}" for _, _, n in BANDS))
    print("-" * 92)
    for name in order:
        row = report[name]["profile"]["temp"]
        print(f"{name:14s} "
              + "  ".join(f"{row.get(n, float('nan')):12.4f}" for _, _, n in BANDS))
    print("temperature spread by depth at day 5, degC")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\n{args.json}")


if __name__ == "__main__":
    main()
