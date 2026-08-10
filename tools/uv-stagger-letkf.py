#!/usr/bin/env python3
"""What the staggered-face shift does to a real LETKF velocity increment.

    uv-stagger-letkf.py --before EXP --after EXP --out site/monitor/uv-stagger

Two experiments that differ in one thing, the domain's gridspec: the control
reads one with the staggered fields as `soca_gridgen.x` writes them, on the east
and north faces, and the test reads one shifted onto the west and south faces,
which are the faces SOCA's reader actually loads. Same domain, dates, initial
conditions, observations, ensemble size and seed.

The increment is `ana - bkg` from the archived records, per face, for one
member. Both arms archive the *same* restart columns, because `post` and
`writeback` take the leading tracer-sized corner in both and only the mask and
the coordinates moved, so the two increments are comparable index by index.
What changes between them is which faces were eligible for an increment at all.

**Two cycles cannot show a skill difference and this does not claim one.** What
it shows is where the increments move. A skill statement needs the cycling
experiments in `experiments/`, run long enough for the difference to separate
from sampling noise, and scored against the truth run.
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4
import numpy as np

MOVED = "#e34948"
KEPT = "#2a78d6"
LAND = "#d8d6d1"
MUTED = "#52514e"

FIELDS = {"eastward_velocity": "u", "northward_velocity": "v"}


def record(experiment, kind, when, member="mem000"):
    path = Path(experiment) / kind / when / f"{member}.nc"
    if not path.exists():
        raise SystemExit(f"{path} does not exist")
    return path


def increment(experiment, when, name, member="mem000"):
    """`ana - bkg` for one field, with land as NaN rather than as zero.

    A fill value is not a small increment and must not average as one, which is
    the whole reason this reads the fill rather than trusting the mask.
    """
    out = []
    for kind in ("ana", "bkg"):
        with netCDF4.Dataset(record(experiment, kind, when, member)) as data:
            data.set_auto_mask(False)
            values = np.asarray(data.variables[name][:], dtype="f8")
            fill = data.variables[name]._FillValue
            out.append(np.where(values == fill, np.nan, values))
    return out[0] - out[1]


def analysed(experiment, when, name, member="mem000"):
    """Which faces the arm could write an increment into at all."""
    with netCDF4.Dataset(record(experiment, "ana", when, member)) as data:
        data.set_auto_mask(False)
        values = np.asarray(data.variables[name][:])
        return values != data.variables[name]._FillValue


def coordinates(experiment, when, key, member="mem000"):
    with netCDF4.Dataset(record(experiment, "bkg", when, member)) as data:
        data.set_auto_mask(False)
        return (np.asarray(data.variables[f"lon_{key}"]),
                np.asarray(data.variables[f"lat_{key}"]),
                np.asarray(data.variables["mask"]) if "mask" in data.variables
                else None)


def depth_max(values):
    """The largest magnitude down each column, with a land column left as NaN.

    Written out rather than `np.nanmax`, which warns on an all-NaN column and
    a land column is exactly that. The warning is not the problem; being asked
    to ignore a warning that fires on every land point is.
    """
    finite = np.isfinite(values)
    out = np.where(finite, np.abs(values), -np.inf).max(axis=0)
    return np.where(finite.any(axis=0), out, np.nan)


def figure(before, after, when, out):
    fig, axes = plt.subplots(2, 3, figsize=(15.5, 9.0))
    fig.patch.set_facecolor("#fcfcfb")
    summary = {}

    for row, (name, key) in enumerate(FIELDS.items()):
        db = increment(before, when, name)
        da = increment(after, when, name)
        lon, lat, _ = coordinates(before, when, key)

        eligible_b = analysed(before, when, name).any(axis=0)
        eligible_a = analysed(after, when, name).any(axis=0)
        gained = eligible_a & ~eligible_b
        lost = eligible_b & ~eligible_a
        both = eligible_a & eligible_b

        # Depth-maximum increment magnitude, so a change anywhere in the column
        # is visible rather than averaged away by fifty quiet levels.
        mb, ma = depth_max(db), depth_max(da)
        top = float(np.nanpercentile(np.concatenate(
            [mb[np.isfinite(mb)], ma[np.isfinite(ma)]]), 99)) or 1e-3

        for ax, plane, title in ((axes[row, 0], mb, "control (east/north faces)"),
                                 (axes[row, 1], ma, "shifted (west/south faces)")):
            m = ax.pcolormesh(lon, lat, plane, vmin=0, vmax=top,
                              cmap="viridis", shading="auto")
            ax.set_title(f"{key}: |increment| max over depth, {title}",
                         fontsize=9)
            fig.colorbar(m, ax=ax, label="m/s")

        ax = axes[row, 2]
        ax.scatter(lon[both], lat[both], s=3, c=LAND, marker=".",
                   label=f"analysed by both ({int(both.sum())})")
        ax.scatter(lon[lost], lat[lost], s=14, c=MOVED, marker="|",
                   label=f"control only, model land ({int(lost.sum())})")
        ax.scatter(lon[gained], lat[gained], s=14, c=KEPT, marker="|",
                   label=f"shifted only, real ocean ({int(gained.sum())})")
        ax.set_title(f"{key}: which faces are eligible for an increment",
                     fontsize=9)
        ax.legend(fontsize=7, loc="lower left", frameon=False)

        interior = both & np.isfinite(mb) & np.isfinite(ma)
        summary[key] = {
            "lost": int(lost.sum()), "gained": int(gained.sum()),
            "both": int(both.sum()),
            "interior_max": float(np.nanmax(np.abs(ma - mb)[interior]))
            if interior.any() else float("nan"),
            "interior_median": float(np.nanmedian(np.abs(ma - mb)[interior]))
            if interior.any() else float("nan"),
            "control_max_on_lost": float(np.nanmax(mb[lost]))
            if lost.any() else 0.0,
        }
        for ax in axes[row]:
            ax.set_xlabel("longitude", fontsize=8)
            ax.tick_params(labelsize=7.5)
        axes[row, 0].set_ylabel("latitude", fontsize=8)

    fig.suptitle(f"LETKF velocity increment, {when}, mem000: "
                 f"gridspec on the east/north faces against the west/south ones",
                 fontsize=11.5)
    fig.text(0.5, 0.012,
             "Two cycles show where the increments move. They cannot show a "
             "skill difference and none is claimed here.",
             ha="center", fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0, 0.035, 1, 0.965))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=125, facecolor=fig.get_facecolor())
    plt.close(fig)
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--when", required=True, help="an archived time, e.g. 20150711T120000Z")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    summary = figure(args.before, args.after, args.when,
                     args.out / "uv-stagger-letkf.png")
    for key, s in summary.items():
        print(f"{key}: {s['both']} faces analysed by both, "
              f"{s['lost']} only by the control (model land), "
              f"{s['gained']} only by the shifted arm (real ocean)")
        print(f"    largest control increment on a land face: "
              f"{s['control_max_on_lost']:.4g} m/s")
        print(f"    |difference| on shared faces: median "
              f"{s['interior_median']:.3g}, max {s['interior_max']:.3g} m/s")
    print(f"wrote {args.out / 'uv-stagger-letkf.png'}")


if __name__ == "__main__":
    main()
