#!/usr/bin/env python3
"""Figures for the spread spike: growth curves, depth profiles, and maps.

    tools/spike-figures.py --out /home/tsluka/work/ackbar/site/monitor/spread

Reads the member directories directly rather than the JSON `spike-spread.py`
writes, because the maps need the fields and not the reduction.

Every panel answers one of the two questions the spike exists for: how much
spread a method makes, and where it puts it. Nothing is plotted that answers
neither.
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4
import numpy

#: Where each suite lives and how its groups should read on a chart. Parameter
#: groups are drawn thin and grey as a family, because the point they make is
#: collective: none of them grows. The stochastic and forcing ensembles are
#: drawn individually because each is a candidate.
SUITES = (("params", "perturbed parameter", "0.55", 1.0),
          ("stochastic", "stochastic physics", None, 2.0),
          ("forcing", "GEFS forcing", "#c2410c", 2.6),
          ("combined", "all three", "#0f766e", 3.2))

#: Suites laid out as one ensemble, their member directories directly inside.
#: The other suites hold many ensembles and are a level deeper.
FLAT = ("forcing", "combined")


def members(group):
    out = []
    for entry in sorted(group.iterdir()):
        if entry.is_dir():
            daily = sorted(entry.glob("*ocn_daily.nc"))
            if daily:
                out.append(daily[0])
    return out


def field(paths, name):
    return numpy.stack([
        numpy.ma.filled(netCDF4.Dataset(p)[name][:].astype("f8"), numpy.nan)
        for p in paths])


def sigma(cube):
    return numpy.nanstd(cube, axis=0, ddof=1)


def reduce_rms(s):
    flat = s.reshape(s.shape[0], -1)
    return numpy.sqrt(numpy.nanmean(flat ** 2, axis=1))


def collect(root):
    """{(suite, group): [paths]} for every group with at least two members."""
    found = {}
    for suite, _, _, _ in SUITES:
        base = root / suite
        if not base.is_dir():
            continue
        if suite in FLAT:
            paths = members(base)
            if len(paths) >= 2:
                found[(suite, suite)] = paths
            continue
        for group in sorted(base.iterdir()):
            if not group.is_dir() or group.name == "namelists":
                continue
            paths = members(group)
            if len(paths) >= 2:
                found[(suite, group.name)] = paths
    return found


def growth(found, out):
    """Spread against lead time, SST and temperature, every group."""
    figure, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    colours = plt.cm.viridis(numpy.linspace(0.05, 0.85, 12))
    used = 0
    for (suite, group), paths in found.items():
        style = next(s for s in SUITES if s[0] == suite)
        _, label, colour, width = style
        if colour is None:
            colour = colours[used % len(colours)]
            used += 1
        days = netCDF4.Dataset(paths[0])["time"][:].astype("f8")
        for axis, name in zip(axes, ("SST", "temp")):
            axis.plot(days, reduce_rms(sigma(field(paths, name))),
                      color=colour, lw=width, alpha=0.85 if width > 1 else 0.55,
                      label=group if width > 1 else None)
    for axis, name, units in zip(axes, ("sea surface temperature", "temperature, whole column"),
                                 ("degC", "degC")):
        axis.set_xlabel("forecast day")
        axis.set_ylabel(f"ensemble spread [{units}]")
        axis.set_title(name)
        axis.grid(alpha=0.25)
    axes[1].legend(fontsize=7, ncol=2, loc="upper left", frameon=False)
    axes[0].text(0.98, 0.03, "grey: perturbed parameters", transform=axes[0].transAxes,
                 ha="right", fontsize=8, color="0.4")
    figure.tight_layout()
    figure.savefig(out / "growth.png", dpi=130)
    plt.close(figure)


def profiles(found, out, top=9):
    """Temperature spread against depth at the last day."""
    depths = None
    rows = []
    for (suite, group), paths in found.items():
        with netCDF4.Dataset(paths[0]) as data:
            depths = data["zl"][:].astype("f8")
        s = sigma(field(paths, "temp"))[-1]
        column = numpy.sqrt(numpy.nanmean(s.reshape(s.shape[0], -1) ** 2, axis=1))
        rows.append((float(numpy.nanmean(column)), suite, group, column))
    rows.sort(reverse=True)

    figure, axis = plt.subplots(figsize=(5.6, 6.4))
    colours = plt.cm.plasma(numpy.linspace(0.05, 0.8, top))
    for n, (_, suite, group, column) in enumerate(rows[:top]):
        axis.plot(column, depths, lw=2, color=colours[n], label=f"{group}")
    axis.invert_yaxis()
    axis.set_yscale("log")
    axis.set_xlabel("temperature spread at day 5 [degC]")
    axis.set_ylabel("depth [m]")
    axis.set_title("where the spread sits")
    axis.grid(alpha=0.25)
    axis.legend(fontsize=8, frameon=False)
    figure.tight_layout()
    figure.savefig(out / "profiles.png", dpi=130)
    plt.close(figure)


def maps(found, out, want):
    """Spatial pattern of SST spread at day 5 for a chosen few.

    The Gulf is forced through prescribed open boundaries, so a method whose
    spread hugs the interior and vanishes at the Yucatan Channel and the
    Florida Strait is telling you the boundary is holding the ensemble
    together, not that the method is weak.
    """
    picked = [(k, v) for k, v in found.items() if k[1] in want]
    if not picked:
        return
    figure, axes = plt.subplots(1, len(picked), figsize=(4.6 * len(picked), 4.0),
                               squeeze=False)
    top = 0.0
    panels = []
    for (suite, group), paths in picked:
        with netCDF4.Dataset(paths[0]) as data:
            lon, lat = data["xh"][:], data["yh"][:]
        s = sigma(field(paths, "SST"))[-1]
        panels.append((group, lon, lat, s))
        top = max(top, float(numpy.nanpercentile(s, 99)))
    for axis, (group, lon, lat, s) in zip(axes[0], panels):
        art = axis.pcolormesh(lon, lat, s, cmap="magma", vmin=0, vmax=top)
        axis.set_title(group, fontsize=10)
        axis.set_xlabel("longitude")
        figure.colorbar(art, ax=axis, label="SST spread [degC]")
    axes[0][0].set_ylabel("latitude")
    figure.tight_layout()
    figure.savefig(out / "maps.png", dpi=130)
    plt.close(figure)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("/data/ackbar/spike"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--maps", default="kv,sppt_a04,both,gefs",
                    help="comma separated group names to draw as maps")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    found = collect(args.root)
    if not found:
        raise SystemExit(f"spike-figures: no member groups under {args.root}")
    print(f"{len(found)} groups")
    growth(found, args.out)
    profiles(found, args.out)
    maps(found, args.out, {w.strip() for w in args.maps.split(",")})
    for name in ("growth.png", "profiles.png", "maps.png"):
        path = args.out / name
        if path.exists():
            print(f"{path}  {path.stat().st_size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
