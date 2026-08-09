#!/usr/bin/env python3
"""Prior ensemble spread, with a per-member open boundary against without.

    obccompare.py --control osse25-4dletkf --test osse25-4dletkf-obc --out DIR

The question this answers is narrow on purpose. Ten cycles cannot say whether
the boundary ensemble improves skill, because the boundary needs about three
weeks to mature in a free forecast. What ten cycles can say is whether the
filter's *prior spread* moved, and specifically whether it moved at the open
boundary, which is where it was zero before by construction: every member read
the same `obc.nc`, so every member had identical water arriving at the edge and
the ensemble had no opinion about it.

So the money plot is not the map, it is spread against distance from the open
boundary. A method that raises spread everywhere by the same factor has done
something, but not the thing it was built for.

Spread is the standard deviation across mem001..mem020 at a point. mem000 is
the control and is excluded: it is not a draw from the distribution, and folding
it in biases the estimate toward whatever the control happens to be doing.
"""

import argparse
import json
import os
from pathlib import Path

import netCDF4
import numpy as np

#: Where experiments live is the site layer's answer, not this file's.
EXP = Path(os.environ.get("ACKBAR_OUTPUT_ROOT", ""))

#: Surface fields worth a map. `sea_surface_height` first because ADT is the
#: observation the boundary most plausibly reaches, and because it is the field
#: whose basin-wide component we know is unobservable.
FIELDS = ("sea_surface_height", "temperature", "salinity")

#: Grid spacing, km. gom_25km is nominal 25 km, which is close enough for an
#: axis label; nothing downstream divides by it.
DX = 25.0


def cycles(exp):
    """Sub-window stamps this experiment wrote a full ensemble for."""
    if not str(EXP):
        raise SystemExit("ACKBAR_OUTPUT_ROOT is unset; source site/activate.sh")
    root = EXP / exp / "bkg"
    if not root.is_dir():
        raise SystemExit(f"{root} does not exist")
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def analysis_times(stamps):
    """Just the 00Z sub-windows, which are the window centers here."""
    return [s for s in stamps if s.endswith("T000000Z")]


def spread(exp, stamp, field, members=range(1, 21)):
    """Standard deviation across members at each point, land as NaN.

    Returns the surface plane: a 3D field is taken at its top level, because a
    map of the whole column is not a map. `--depth` on the profile side is where
    the column gets looked at.
    """
    root = EXP / exp / "bkg" / stamp
    stack = []
    for m in members:
        path = root / f"mem{m:03d}.nc"
        if not path.exists():
            return None
        with netCDF4.Dataset(path) as f:
            if field not in f.variables:
                return None
            v = f[field]
            plane = v[0] if v.ndim == 3 else v[:]
            stack.append(np.ma.filled(plane.astype("f8"), np.nan))
    return np.std(np.array(stack), axis=0, ddof=1)


def column_spread(exp, stamp, field, members=range(1, 21)):
    """Standard deviation across members, kept depth resolved."""
    root = EXP / exp / "bkg" / stamp
    stack = []
    for m in members:
        path = root / f"mem{m:03d}.nc"
        if not path.exists():
            return None
        with netCDF4.Dataset(path) as f:
            if field not in f.variables or f[field].ndim != 3:
                return None
            stack.append(np.ma.filled(f[field][:].astype("f8"), np.nan))
    return np.std(np.array(stack), axis=0, ddof=1)


def geometry(exp, stamp):
    """lon, lat, ocean mask, and distance to the nearest open boundary in km.

    The GoM domain opens on three sides: north (J=N), east (I=N), and south
    (J=0). The western edge is the Mexican coast and is closed, so it is not in
    the minimum. Distance is counted in grid cells and scaled, which is honest
    to about the width of a cell and is all an axis needs.
    """
    root = EXP / exp / "bkg" / stamp
    with netCDF4.Dataset(root / "mem001.nc") as f:
        lon = np.ma.filled(f["lon"][:].astype("f8"), np.nan)
        lat = np.ma.filled(f["lat"][:].astype("f8"), np.nan)
        mask = np.ma.filled(f["mask"][:].astype("f8"), 0.0) > 0.5
    ny, nx = mask.shape
    j, i = np.mgrid[0:ny, 0:nx]
    edge = np.minimum.reduce([ny - 1 - j, nx - 1 - i, j]).astype("f8")
    return lon, lat, mask, edge * DX


def rms(field, mask):
    """Domain reduced spread: root mean square over ocean points."""
    values = field[mask]
    values = values[np.isfinite(values)]
    return float(np.sqrt(np.mean(values ** 2))) if values.size else float("nan")


def profile(control, test, distance, mask, edges=(0, 50, 100, 200, 400, 10000)):
    """Spread in bands of distance from the open boundary."""
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        band = mask & (distance >= lo) & (distance < hi)
        if not band.any():
            continue
        c, t = rms(control, band), rms(test, band)
        rows.append({"from": lo, "to": hi, "cells": int(band.sum()),
                     "control": c, "test": t,
                     "ratio": t / c if c else float("nan")})
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--control", default="osse25-4dletkf")
    p.add_argument("--test", default="osse25-4dletkf-obc")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--json", type=Path)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    shared = sorted(set(analysis_times(cycles(args.control)))
                    & set(analysis_times(cycles(args.test))))
    if not shared:
        raise SystemExit("the two experiments share no analysis time")
    print(f"{len(shared)} shared analysis times: {shared[0]} .. {shared[-1]}")

    lon, lat, mask, distance = geometry(args.test, shared[-1])
    report = {"control": args.control, "test": args.test,
              "cycles": shared, "fields": {}}

    for field in FIELDS:
        maps, series = {}, []
        for exp in (args.control, args.test):
            got = [spread(exp, s, field) for s in shared]
            got = [g for g in got if g is not None]
            if not got:
                print(f"  {field}: no data for {exp}, skipped")
                break
            maps[exp] = np.nanmean(np.array(got), axis=0)
            series.append([rms(g, mask) for g in got])
        else:
            c, t = maps[args.control], maps[args.test]
            report["fields"][field] = {
                "control_rms": rms(c, mask), "test_rms": rms(t, mask),
                "ratio": rms(t, mask) / rms(c, mask) if rms(c, mask) else None,
                "series": {"control": series[0], "test": series[1]},
                "by_distance": profile(c, t, distance, mask),
            }
            np.savez(args.out / f"{field}.npz", control=c, test=t,
                     lon=lon, lat=lat, mask=mask, distance=distance)
            print(f"  {field}: control {rms(c, mask):.4g}  "
                  f"test {rms(t, mask):.4g}  "
                  f"ratio {rms(t, mask) / rms(c, mask):.2f}")

    # The column, at the last shared time only. A depth profile averaged over
    # cycles hides the growth this is meant to show, and one time is enough to
    # say whether the boundary signal is a surface skin or reaches the
    # thermocline.
    for field in ("temperature", "salinity"):
        out = {}
        for exp in (args.control, args.test):
            col = column_spread(exp, shared[-1], field)
            if col is None:
                break
            out[exp] = [rms(level, mask) for level in col]
        else:
            report.setdefault("column", {})[field] = {
                "control": out[args.control], "test": out[args.test]}

    text = json.dumps(report, indent=2)
    (args.json or args.out / "spread.json").write_text(text)
    print(f"wrote {args.json or args.out / 'spread.json'}")


if __name__ == "__main__":
    main()
