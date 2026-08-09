#!/usr/bin/env python3
"""Figures for the boundary ensemble comparison.

    obcfigs.py --data DIR --json DIR/spread.json --out site/monitor/osse/obc

Three figures per run of this, and the order matters because they answer
progressively narrower questions:

  spread-<field>.png   where the spread is, both runs, and the ratio
  spread-distance.png  spread against distance from the open boundary
  spread-series.png    domain spread per cycle, both runs

The distance figure is the one to read first. The maps show that something
changed; the distance figure shows that what changed is the boundary, which is
the only claim the feature makes.
"""

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm

LABEL = {"sea_surface_height": "SSH spread (m)",
         "temperature": "SST spread (°C)",
         "salinity": "SSS spread (psu)"}
SHORT = {"sea_surface_height": "SSH", "temperature": "SST",
         "salinity": "SSS"}


def maps(data, field, out):
    """Control, test, and the ratio, on the domain."""
    d = np.load(data / f"{field}.npz")
    lon, lat, mask = d["lon"], d["lat"], d["mask"]
    control = np.where(mask, d["control"], np.nan)
    test = np.where(mask, d["test"], np.nan)
    top = np.nanpercentile(np.concatenate([control[mask], test[mask]]), 98)

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2), constrained_layout=True)
    for ax, plane, title in ((axes[0], control, "control (shared boundary)"),
                             (axes[1], test, "per-member boundary")):
        m = ax.pcolormesh(lon, lat, plane, vmin=0, vmax=top, cmap="viridis",
                          shading="auto")
        ax.set_title(title, fontsize=10)
        fig.colorbar(m, ax=ax, label=LABEL[field])

    ratio = np.where(control > 0, test / control, np.nan)
    # Centered at 1 so "no change" is the neutral color and the eye is not
    # asked to compare two saturations.
    m = axes[2].pcolormesh(lon, lat, ratio, cmap="RdBu_r",
                           norm=TwoSlopeNorm(vmin=0.5, vcenter=1.0, vmax=2.5),
                           shading="auto")
    axes[2].set_title("ratio, per-member / control", fontsize=10)
    fig.colorbar(m, ax=axes[2], label="×")
    for ax in axes:
        ax.set_xlabel("longitude")
    axes[0].set_ylabel("latitude")
    fig.suptitle(f"{SHORT[field]}: prior ensemble spread", fontsize=12)
    fig.savefig(out / f"spread-maps-{SHORT[field].lower()}.png", dpi=120)
    plt.close(fig)


def distance(report, out):
    """The claim, plotted: spread against distance from the open boundary."""
    fields = [f for f in report["fields"] if report["fields"][f]["by_distance"]]
    fig, axes = plt.subplots(1, len(fields), figsize=(4.6 * len(fields), 4.0),
                             constrained_layout=True, squeeze=False)
    for ax, field in zip(axes[0], fields):
        rows = report["fields"][field]["by_distance"]
        x = [0.5 * (r["from"] + min(r["to"], 600)) for r in rows]
        ax.plot(x, [r["control"] for r in rows], "o-", label="control",
                color="#555555")
        ax.plot(x, [r["test"] for r in rows], "s-", label="per-member boundary",
                color="#c1440e")
        ax.set_title(SHORT[field], fontsize=11)
        ax.set_xlabel("distance from open boundary (km)")
        ax.set_ylabel(LABEL[field])
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.3)
    axes[0][0].legend(fontsize=9)
    fig.suptitle("Prior spread against distance from the open boundary",
                 fontsize=12)
    fig.savefig(out / "spread-distance.png", dpi=120)
    plt.close(fig)


def series(report, out):
    """Domain spread per cycle. Ten cycles is a trend at best, so it is drawn
    as points joined by a line and not fitted."""
    fields = list(report["fields"])
    stamps = report["cycles"]
    fig, axes = plt.subplots(1, len(fields), figsize=(4.6 * len(fields), 3.8),
                             constrained_layout=True, squeeze=False)
    for ax, field in zip(axes[0], fields):
        s = report["fields"][field]["series"]
        n = range(1, len(s["control"]) + 1)
        ax.plot(n, s["control"], "o-", color="#555555", label="control")
        ax.plot(n, s["test"], "s-", color="#c1440e",
                label="per-member boundary")
        ax.set_title(SHORT[field], fontsize=11)
        ax.set_xlabel("cycle")
        ax.set_ylabel(LABEL[field])
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.3)
    axes[0][0].legend(fontsize=9)
    fig.suptitle(f"Domain prior spread, {stamps[0][:8]} onward", fontsize=12)
    fig.savefig(out / "spread-series.png", dpi=120)
    plt.close(fig)


def column(report, out):
    """Does the boundary signal reach the thermocline, or only the skin?"""
    if "column" not in report:
        return
    fields = list(report["column"])
    fig, axes = plt.subplots(1, len(fields), figsize=(4.0 * len(fields), 4.6),
                             constrained_layout=True, squeeze=False)
    for ax, field in zip(axes[0], fields):
        d = report["column"][field]
        levels = range(len(d["control"]))
        ax.plot(d["control"], levels, color="#555555", label="control")
        ax.plot(d["test"], levels, color="#c1440e",
                label="per-member boundary")
        ax.invert_yaxis()
        ax.set_title(field, fontsize=11)
        ax.set_xlabel("spread")
        ax.set_ylabel("model level")
        ax.grid(alpha=0.3)
    axes[0][0].legend(fontsize=9)
    fig.suptitle("Spread through the column, last cycle", fontsize=12)
    fig.savefig(out / "spread-column.png", dpi=120)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--json", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = json.loads((args.json or args.data / "spread.json").read_text())

    for field in report["fields"]:
        if (args.data / f"{field}.npz").exists():
            maps(args.data, field, args.out)
    distance(report, args.out)
    series(report, args.out)
    column(report, args.out)
    print(f"wrote figures to {args.out}")


if __name__ == "__main__":
    main()
