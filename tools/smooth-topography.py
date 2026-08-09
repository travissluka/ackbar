#!/usr/bin/env python3
"""Cap a domain's bathymetric steepness, in place, keeping its coastline.

    tools/smooth-topography.py <domain> [--target R] [--minimum DEPTH] [--dry-run]
    tools/smooth-topography.py gom_25km --target 0.6

Rewrites `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/ocean_topog.nc`, keeping the
original beside it as `ocean_topog.nc.unsmoothed` the first time it runs, so the
change is reversible and a second run does not smooth an already smoothed field.

## What is being capped, and why it is not cosmetic

The steepness of a cell pair, the Beckmann and Haidvogel number:

    r  =  |h1 - h2| / (h1 + h2)

At r = 1 one of the two columns has no water in it relative to the other. The
classic target is 0.2, which comes from sigma-coordinate pressure gradient
error and is stricter than this needs: ACKBAR runs Z*, where the problem is not
the pressure gradient but the thin column. gom_25km as imported has 27% of its
ocean cells above 0.3, 3% above 0.9, and pairs where a 10.4 m column sits
against a 4084 m one, which is the Cuban shelf break resolved as a cliff one
grid cell wide.

0.6 is the default here rather than 0.2 for that reason, and because of what the
cap costs: r bounds the depth ratio between neighbours at (1+r)/(1-r), so a shelf
break from 10 m to 4000 m is spread over `log(400)/log((1+r)/(1-r))` cells. At
0.2 that is thirteen cells, 327 km, which relocates the shelf break; at 0.6 it is
four cells, and a 25 km grid cannot honestly resolve one sharper than that
anyway.

**A free forecast tolerates this and an analysis does not.** `osse25-noda` ran
45 cycles on it without complaint. Add an increment and the shallow side of one
of those pairs drains: the face between them carries a transport scaled to the
deep column, any error in it lands on the thin one, and MOM6 reports
`btstep: eta has dropped below bathyT` and then dies in `implied h<0` a few
steps later. Every forecast this workflow has lost to an analysis increment was
one of those cells.

So the workarounds all had the same shape, and all of them paid for a few
hundred cells with the analysis everywhere: relaxing the whole increment,
tapering it by depth, bounding its divergence. The last is worth keeping as a
guard against an ensemble outlier. None of them is the fix, because the sea
floor is what is wrong.

## The method

Martinho and Batteen (2006), which is the standard one: sweep the grid, and
wherever a neighbouring pair is over the target, move the smallest amount of
depth between them that brings it back. Iterate until nothing is over or the
pass limit is reached. It is deliberately local: a Gaussian filter over the
whole field would move depth in the deep basin, where nothing is wrong, and
would round off the shelf break everywhere rather than only where it is too
sharp to carry.

Two things are held fixed:

- **The land mask.** A cell that is land stays land and a cell that is ocean
  stays ocean, so `ocean_mask.nc`, the mosaics and the SOCA gridspec all remain
  true. Smoothing that wets or dries a cell would need the whole domain rebuilt.
- **The minimum depth.** Nothing is left shallower than `--minimum`, which is the
  domain's own `MINIMUM_DEPTH`: the model would round it up anyway, and doing it
  here means the file says what the model will actually run.

The volume is not conserved and is not meant to be. Depth moved out of a deep
column is a rounding of a cliff, and constraining the total would push the error
somewhere else on the same slope.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

import netCDF4
import numpy as np

TOPOG = "ocean_topog.nc"
ORIGINAL = TOPOG + ".unsmoothed"

#: How many sweeps. Each one only ever reduces the worst pair, so the sequence
#: is monotone; the cap is a backstop, and gom_25km converges well inside it.
PASSES = 200


def steepness(depth, ocean):
    """`r` for every pair of neighbouring *ocean* cells.

    Pairs against land are excluded rather than counted as r = 1. A wet cell
    beside a dry one is trivially at the maximum and there is nothing to do
    about it: no water crosses that face, so it cannot drain the column. Counting
    them was hiding whether the smoothing was converging.
    """
    rx = np.zeros_like(depth)
    ry = np.zeros_like(depth)
    rx[:, :-1] = _ratio(depth[:, :-1], depth[:, 1:]) * (ocean[:, :-1] & ocean[:, 1:])
    ry[:-1, :] = _ratio(depth[:-1, :], depth[1:, :]) * (ocean[:-1, :] & ocean[1:, :])
    return rx, ry


def _ratio(one, two):
    total = one + two
    return np.where(total > 0, np.abs(one - two) / np.maximum(total, 1e-9), 0.0)


def smooth(depth, ocean, target, minimum, passes=PASSES):
    """Move depth between neighbours until no pair is steeper than *target*.

    Returns `(smoothed, passes used, worst r remaining)`.
    """
    out = np.where(ocean, np.maximum(depth, minimum), depth).astype("f8")
    used = 0
    for used in range(1, passes + 1):
        # Accumulated rather than applied pair by pair. A cell belongs to up to
        # four pairs, so writing each pair's answer straight back means the last
        # one written wins and the other three are silently discarded.
        delta = np.zeros_like(out)
        worst = 0.0
        for axis in (0, 1):
            lo = _slice(out, axis, 0)
            hi = _slice(out, axis, 1)
            wet = _slice(ocean, axis, 0) & _slice(ocean, axis, 1)
            r = _ratio(lo, hi) * wet
            worst = max(worst, float(r.max()) if r.size else 0.0)
            bad = wet & (r > target)
            if not bad.any():
                continue
            # The pair's mean is held and its difference is pulled back to what
            # the target allows, which is the smallest move that fixes the pair.
            mean = 0.5 * (lo + hi)
            allowed = target * (lo + hi)
            sign = np.sign(hi - lo)
            _add(delta, axis, 0, np.where(bad, mean - 0.5 * allowed * sign - lo, 0.0))
            _add(delta, axis, 1, np.where(bad, mean + 0.5 * allowed * sign - hi, 0.0))
        if worst <= target:
            break
        # Under-relaxed, because a cell pulled in two directions at once by two
        # different pairs would otherwise overshoot and oscillate.
        out = np.where(ocean, np.maximum(out + 0.5 * delta, minimum), out)
    rx, ry = steepness(out, ocean)
    return out, used, float(np.maximum(rx, ry).max())


def _slice(array, axis, side):
    return array[:-1, :] if axis == 0 and side == 0 else \
           array[1:, :] if axis == 0 else \
           array[:, :-1] if side == 0 else array[:, 1:]


def _add(array, axis, side, values):
    if axis == 0:
        if side == 0:
            array[:-1, :] += values
        else:
            array[1:, :] += values
    else:
        if side == 0:
            array[:, :-1] += values
        else:
            array[:, 1:] += values


def report(name, depth, ocean, target):
    rx, ry = steepness(depth, ocean)
    pairs = np.maximum(rx, ry)[ocean]
    # A tolerance, because the smoothing leaves pairs sitting exactly at the
    # target and a bare `>` counts every one of them as a failure.
    over = [(t, int((pairs > t + 1e-9).sum())) for t in (target, 0.5, 0.9)]
    print(f"  {name:9} min {depth[ocean].min():7.1f} m  max {depth[ocean].max():7.1f} m  "
          f"worst r {pairs.max():.4f}")
    for t, n in over:
        print(f"    {'':7} r > {t:.2f}: {n:5d} cell(s) ({100.0 * n / pairs.size:.1f}%)")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain")
    parser.add_argument("--target", type=float, default=0.6,
                        help="the largest steepness to allow (default 0.6)")
    parser.add_argument("--minimum", type=float, default=10.0,
                        help="the domain's MINIMUM_DEPTH, in metres (default 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args()

    static = os.environ.get("ACKBAR_STATIC_ROOT")
    if not static:
        sys.exit("smooth-topography: run `source site/activate.sh` first")
    directory = Path(static) / "domain" / args.domain / "INPUT"
    path = directory / TOPOG
    if not path.exists():
        sys.exit(f"smooth-topography: {path} does not exist")

    # The original, once. A second run must smooth the imported field again
    # rather than smooth its own output, which would keep eroding the shelf.
    keep = directory / ORIGINAL
    source = keep if keep.exists() else path
    with netCDF4.Dataset(source) as data:
        data.set_auto_mask(False)
        depth = np.asarray(data.variables["depth"][:], dtype="f8")
    ocean = depth > 0.0

    print(f"smooth-topography: {args.domain}, {int(ocean.sum())} ocean cell(s), "
          f"target r {args.target}, minimum depth {args.minimum} m")
    report("before", depth, ocean, args.target)
    out, used, worst = smooth(depth, ocean, args.target, args.minimum)
    report("after", out, ocean, args.target)

    moved = np.abs(out - depth)[ocean]
    print(f"  {used} pass(es), worst r now {worst:.4f}; "
          f"{int((moved > 1.0).sum())} cell(s) moved by more than a metre, "
          f"largest move {moved.max():.1f} m")
    assert np.all((out > 0.0) == ocean), "the coastline moved, which it must not"

    if args.dry_run:
        print("smooth-topography: --dry-run, nothing written")
        return
    if not keep.exists():
        shutil.copy2(path, keep)
        print(f"smooth-topography: kept the imported field as {ORIGINAL}")
    with netCDF4.Dataset(path, "r+") as data:
        data.set_auto_mask(False)
        data.variables["depth"][:] = out
        data.smoothed = (f"Martinho and Batteen steepness cap r <= {args.target}, "
                         f"minimum depth {args.minimum} m, by tools/smooth-topography.py")
    print(f"smooth-topography: wrote {path}")


if __name__ == "__main__":
    main()
