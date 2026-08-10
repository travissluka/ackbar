#!/usr/bin/env python3
"""Where a velocity face lives, and which one ACKBAR hands SOCA.

    uv-stagger-figures.py --gridspec DIR/soca_gridspec.nc \
        --restart DIR/MOM.res.nc --out site/monitor/uv-stagger

The forecast's MOM6 is built with symmetric memory, so its restart carries `u`
one column wider than the tracer grid and `v` one row taller. The extra face is
at the *west* and *south*: MOM6 sets `IsdB = isd-1` when the grid is symmetric,
so restart column 0 of `u` is the west face of tracer 0. SOCA's MOM6 is not
symmetric, so its `lonu(i)` is the *east* face of tracer i, and one of the two
ends of the restart array has to be dropped to line them up.

Which end is not a matter of taste and this is the figure that says so. Two
panels of the same stretch of coastline, one per pairing, with the model's own
live velocity against the gridspec's `mask2du`. The pairing that is right makes
every face SOCA calls ocean a face the model actually integrates; the pairing
that is wrong leaves a trail of disagreements along every coast.

The two counts the figure turns on, and what each means:

  dead-on-wet   the gridspec calls the face ocean and the model has no velocity
                there, because it is land. An analysis writes an increment into
                it. This is the count that must be zero.
  live-on-dry   the model integrates the face and the gridspec calls it land, so
                no increment ever reaches it. Zero is not achievable here: the
                east and north boundary columns are masked off by non-symmetric
                MOM6 for want of a cell beyond them, and those faces are real.

Which pairing ACKBAR uses is not written into this file. It is probed from
`ackbar.post` so that the figure is a reading of the code rather than a second
opinion about it, and so that regenerating it after a change to the slice is a
proof rather than a redrawing.
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

#: Slot 1 and slot 2 of the categorical palette, plus its status red. `u` and
#: `v` are never on screen as a pair that has to be told apart at a glance, so
#: the two colors here carry the two *failure kinds* instead, which is what a
#: reader has to distinguish.
DEAD_ON_WET = "#e34948"   # land the analysis would write into
LIVE_ON_DRY = "#2a78d6"   # ocean the analysis never reaches
LIVE = "#2a78d6"
LAND = "#d8d6d1"
OCEAN = "#eef4fa"
INK = "#0b0b0b"
MUTED = "#52514e"

#: The stretch drawn in the zoom. A row with several land/ocean transitions,
#: because one transition is an anecdote and the shift shows up at every one.
ZOOM_ROW = 49
ZOOM_COLS = (33, 51)


def face_axes(restart):
    """The restart's own staggered longitudes and latitudes.

    Read rather than reconstructed. An earlier version of this built the face
    axis by prepending half a cell to the gridspec's `lonu`, which silently
    assumed the gridspec was on the east faces and drew the zoom a cell out once
    that stopped being true. The restart states its own axes; use them.

    Rectilinear only, which every domain here is. A curvilinear grid would need
    the two dimensional face coordinates and this refuses rather than drawing
    something plausible and wrong.
    """
    with netCDF4.Dataset(restart) as data:
        data.set_auto_mask(False)
        for name in ("lonq", "latq", "lonh", "lath"):
            if name not in data.variables:
                raise SystemExit(f"{restart} has no {name} axis to read")
        return {name: np.asarray(data.variables[name][:])
                for name in ("lonq", "latq", "lonh", "lath")}


def live_faces(restart):
    """Which faces the model actually integrates, from a restart it wrote.

    A face is live if any level of it is nonzero. Land faces are exactly zero at
    every level, because MOM6 multiplies the velocity by the face mask, so this
    recovers the model's own `mask2dCu` without reading a mask from anywhere.
    """
    with netCDF4.Dataset(restart) as data:
        data.set_auto_mask(False)
        u = np.asarray(data.variables["u"][0])
        v = np.asarray(data.variables["v"][0])
    return (u != 0.0).any(axis=0), (v != 0.0).any(axis=0)


def grids(gridspec):
    with netCDF4.Dataset(gridspec) as data:
        data.set_auto_mask(False)
        return {name: np.asarray(data.variables[name][0])
                for name in ("lon", "lat", "lonu", "latv",
                             "mask2d", "mask2du", "mask2dv")}


def ackbar_offset():
    """Which end of the staggered axis ACKBAR keeps, asked of ACKBAR.

    Returns 0 for the leading slice and 1 for the trailing one. A probe rather
    than a constant: the answer has to be whatever the code does today, or
    regenerating this figure after a change proves nothing.
    """
    from ackbar import post

    probe = np.arange(4, dtype="f4").reshape(1, 4) * np.ones((2, 1), dtype="f4")
    kept = post._tracer_corner(probe, (2, 3), Path("probe"), "u", "u")
    return int(kept[0, 0])


def _slice(live, mask, offset, axis):
    height, width = mask.shape
    if axis == 1:
        return live[:, offset:offset + width]
    return live[offset:offset + height, :]


def zoom(ax, g, faces, ulive, offset, title):
    """One pairing, drawn over a stretch of coast.

    Three rows: the tracer cells the model integrates, the restart's `u`
    columns at the longitudes the restart's own `lonq` axis gives them, and
    SOCA's `u` indices at the longitudes `lonu` gives them. A connector joins
    each restart column to the SOCA index ACKBAR pairs it with, so the pairing
    is the line and a one cell error is a line that leans.
    """
    c0, c1 = ZOOM_COLS
    j = ZOOM_ROW
    lon = g["lon"][j]
    lonu = g["lonu"][j]
    mask = g["mask2d"][j]
    mu = g["mask2du"][j]
    dx = float(lon[1] - lon[0])
    lonq = faces["lonq"]
    if not np.allclose(lon, faces["lonh"]):
        raise SystemExit(
            "the gridspec's tracer longitudes are not the restart's `lonh`, so "
            "this grid is not rectilinear and the zoom cannot place its faces")

    for i in range(c0, c1):
        wet = mask[i] > 0
        ax.add_patch(Rectangle((lon[i] - dx / 2, 0.55), dx, 0.9,
                               facecolor=OCEAN if wet else LAND,
                               edgecolor="#b8b6b1", linewidth=0.6))
        ax.text(lon[i], 1.0, str(i), ha="center", va="center",
                fontsize=6.5, color=MUTED)

    for i in range(c0, c1 + 1):
        ax.plot(lonq[i], 0.2, marker="o", markersize=6,
                markerfacecolor=LIVE if ulive[j, i] else "none",
                markeredgecolor=LIVE if ulive[j, i] else "#9a9892",
                markeredgewidth=1.2)

    for i in range(c0, c1):
        wet = mu[i] > 0
        ax.plot(lonu[i], -0.6, marker="s", markersize=6,
                markerfacecolor=LIVE if wet else "none",
                markeredgecolor=LIVE if wet else "#9a9892",
                markeredgewidth=1.2)

    # The pairing itself. SOCA index i is fed restart column i + offset.
    for i in range(c0, c1):
        col = i + offset
        if col > len(lonq) - 1:
            continue
        agree = bool(ulive[j, col]) == bool(mu[i] > 0)
        ax.plot([lonq[col], lonu[i]], [0.2, -0.6], "-",
                color="#c9c7c2" if agree else DEAD_ON_WET,
                linewidth=0.9 if agree else 1.8, zorder=1)
        if not agree:
            ax.plot(np.mean([lonq[col], lonu[i]]), -0.2, marker="x",
                    markersize=7, color=DEAD_ON_WET, markeredgewidth=2)

    ax.set_xlim(lon[c0] - dx, lon[c1 - 1] + dx)
    ax.set_ylim(-1.2, 1.9)
    ax.set_yticks([1.0, 0.2, -0.6])
    ax.set_yticklabels(["tracer cell\n(model)", "restart u column\n(lonq)",
                        "SOCA u index\n(lonu, mask2du)"], fontsize=7.5)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("longitude", fontsize=8)
    ax.tick_params(axis="x", labelsize=7.5)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_title(title, fontsize=9.5, color=INK)


def domain(ax, g, ulive, vlive, offset, title):
    """Every disagreeing face on the domain, for one pairing."""
    lon, lat = g["lon"], g["lat"]
    ax.pcolormesh(lon, lat, np.where(g["mask2d"] > 0, np.nan, 1.0),
                  cmap=matplotlib.colors.ListedColormap([LAND]), shading="auto")

    counts = {}
    found = []
    for key, live, mask, axis, lonf, latf in (
            ("u", ulive, g["mask2du"], 1, g["lonu"], g["lat"]),
            ("v", vlive, g["mask2dv"], 0, g["lon"], g["latv"])):
        sliced = _slice(live, mask, offset, axis)
        dead_on_wet = (~sliced) & (mask > 0)
        live_on_dry = sliced & (mask == 0)
        counts[key] = (int(dead_on_wet.sum()), int(live_on_dry.sum()))
        # Which edge the residual sits on, read off the indices rather than
        # asserted, because the shift is exactly the thing that moves it.
        where = np.argwhere(live_on_dry)
        if where.size:
            # `axis` is the direction the face set is staggered along, so it is
            # also the index that is constant on the edge the residual sits on:
            # a `u` residual is a column, a `v` residual is a row.
            index = where[:, axis]
            limit = live_on_dry.shape[axis] - 1
            if np.all(index == 0):
                found.append("west" if key == "u" else "south")
            elif np.all(index == limit):
                found.append("east" if key == "u" else "north")
            else:
                found.append(f"{key} interior")
        for flags, color, size in ((dead_on_wet, DEAD_ON_WET, 11),
                                   (live_on_dry, LIVE_ON_DRY, 7)):
            ax.scatter(lonf[flags], latf[flags], s=size, c=color,
                       marker="|" if key == "u" else "_", linewidths=1.1)
    edges = " and ".join(found) if found else "no"

    ax.set_title(f"{title}\ndead-on-wet: {counts['u'][0]} u, {counts['v'][0]} v"
                 f"   |   live-on-dry: {counts['u'][1]} u, {counts['v'][1]} v",
                 fontsize=9)
    ax.set_xlabel("longitude", fontsize=8)
    ax.set_ylabel("latitude", fontsize=8)
    ax.tick_params(labelsize=7.5)
    ax.set_xlim(lon.min(), lon.max())
    ax.set_ylim(lat.min(), lat.max())
    if counts["u"][0] == 0 and counts["v"][0] == 0:
        ax.text(0.5, 0.02,
                f"the remaining live-on-dry faces are the {edges} boundary "
                f"columns,\nwhich have no cell beyond them for the mask to be "
                f"formed from",
                transform=ax.transAxes, ha="center", va="bottom", fontsize=7.5,
                color=MUTED)
    return counts


def figure(gridspec, restart, out, offset):
    g = grids(gridspec)
    faces = face_axes(restart)
    ulive, vlive = live_faces(restart)

    fig, axes = plt.subplots(2, 2, figsize=(14.5, 10.6),
                             gridspec_kw={"height_ratios": [1.0, 1.45]})
    fig.patch.set_facecolor("#fcfcfb")

    marks = ["leading slice: SOCA index i gets restart column i",
             "trailing slice: SOCA index i gets restart column i+1"]
    for k, ax in enumerate(axes[0]):
        used = " (ACKBAR uses this)" if k == offset else ""
        zoom(ax, g, faces, ulive, k, marks[k] + used)

    counts = {}
    for k, ax in enumerate(axes[1]):
        used = " (ACKBAR uses this)" if k == offset else ""
        counts[k] = domain(ax, g, ulive, vlive, k,
                           ["leading slice", "trailing slice"][k] + used)

    legend = [
        Line2D([], [], marker="o", color="none", markerfacecolor=LIVE,
               markeredgecolor=LIVE, markersize=6,
               label="restart face the model integrates"),
        Line2D([], [], marker="o", color="none", markerfacecolor="none",
               markeredgecolor="#9a9892", markersize=6,
               label="restart face that is land (velocity exactly zero)"),
        Line2D([], [], marker="s", color="none", markerfacecolor=LIVE,
               markeredgecolor=LIVE, markersize=6,
               label="gridspec mask2du calls the face ocean"),
        Line2D([], [], marker="x", color=DEAD_ON_WET, linestyle="none",
               markersize=7, markeredgewidth=2,
               label="the pair disagrees"),
        Line2D([], [], color=DEAD_ON_WET, linewidth=2,
               label="dead-on-wet: land given an increment"),
        Line2D([], [], color=LIVE_ON_DRY, linewidth=2,
               label="live-on-dry: ocean never analysed"),
    ]
    fig.legend(handles=legend, loc="lower center", ncol=3, frameon=False,
               fontsize=8, bbox_to_anchor=(0.5, 0.012))
    fig.text(0.5, 0.002,
             "dead-on-wet is the count that must be zero: it is a land face the "
             "gridspec calls ocean, so every analysis writes an increment into it.",
             ha="center", fontsize=8.5, color=INK)

    fig.suptitle("Which restart column is SOCA's u index i?  "
                 f"{Path(gridspec).parent.name}", fontsize=12)
    fig.tight_layout(rect=(0, 0.095, 1, 0.965))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)
    return counts


PAGE = """<!doctype html><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>ackbar: u/v stagger</title><link rel='stylesheet' href='/style.css'>
<main><h1>Which restart column is SOCA's u index i?</h1>
<p class='sub'>{domain}, from {restart}</p>
<p>The forecast's MOM6 is built with symmetric memory, so its restart carries
<code>u</code> one column wider than the tracer grid and <code>v</code> one row
taller. The extra face is at the <b>west</b> and <b>south</b>: MOM6 sets
<code>IsdB = isd-1</code> on a symmetric grid, so restart column 0 of
<code>u</code> is the west face of tracer 0. SOCA's MOM6 is not symmetric, so
its <code>lonu(i)</code> is the <b>east</b> face of tracer i. One end of the
restart array has to be dropped, and which end is what this figure settles.</p>
<p>ACKBAR keeps the <b>{used}</b> slice.</p>
<img src='uv-stagger.png' style='width:100%;max-width:1450px'>
<table><thead><tr><th>pairing</th><th>dead-on-wet u</th><th>dead-on-wet v</th>
<th>live-on-dry u</th><th>live-on-dry v</th></tr></thead><tbody>{rows}</tbody>
</table>
<p><b>dead-on-wet</b> is the count that must be zero. It is a face the gridspec
calls ocean and the model has no velocity on, because it is land, so every
analysis writes an increment into it and no forecast ever reads one back.
<b>live-on-dry</b> cannot reach zero: the east and north boundary columns are
masked off by non-symmetric MOM6 for want of a cell beyond them, and those faces
are real ocean.</p>
<p>Re-check with <code>tools/uv-stagger-figures.py</code>, which probes
<code>ackbar.post</code> for the slice rather than being told it, so this page is
a reading of the code. <code>tests/test_uv_stagger.py</code> asserts the same
zero without needing a domain on disk.</p>
</main>"""


def page(out, gridspec, restart, offset, counts):
    rows = ""
    for k in (0, 1):
        name = ["leading", "trailing"][k]
        used = " (ACKBAR uses this)" if k == offset else ""
        c = counts[k]
        rows += (f"<tr><td>{name}{used}</td>"
                 f"<td class='num'>{c['u'][0]}</td>"
                 f"<td class='num'>{c['v'][0]}</td>"
                 f"<td class='num'>{c['u'][1]}</td>"
                 f"<td class='num'>{c['v'][1]}</td></tr>")
    (out / "index.html").write_text(PAGE.format(
        domain=Path(gridspec).parent.name, restart=restart,
        used=["leading", "trailing"][offset], rows=rows))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gridspec", required=True, type=Path)
    p.add_argument("--restart", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    offset = ackbar_offset()
    counts = figure(args.gridspec, args.restart,
                    args.out / "uv-stagger.png", offset)
    page(args.out, args.gridspec, args.restart, offset, counts)
    print(f"ackbar keeps the {'trailing' if offset else 'leading'} slice")
    for k in (0, 1):
        name = ["leading", "trailing"][k]
        print(f"  {name:9s} dead-on-wet u={counts[k]['u'][0]:4d} "
              f"v={counts[k]['v'][0]:4d}   live-on-dry u={counts[k]['u'][1]:4d} "
              f"v={counts[k]['v'][1]:4d}")
    print(f"wrote {args.out / 'uv-stagger.png'}")


if __name__ == "__main__":
    main()
