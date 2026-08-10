#!/usr/bin/env python3
"""Draw what a coastal dirac does to velocity, and check where the velocity landed.

    tools/dirac-uv-page.py --gridspec <soca_gridspec.nc> --restart <MOM.res.nc> \\
        --members <dir of mem*/MOM.res.nc> --out <dir> \\
        --run north=<keepdir> --run east=<keepdir> ...

Each `--run` is a directory `tools/soca-dirac.sh --ensemble --keep` wrote: a
`points.json` holding one dirac, the increment the covariance produced, and the
increment the localization produced by itself.

## The three questions, and why velocity needs a page of its own

`tools/dirac-page.py` answers "is the increment local, cross-variable and the
right size" for a dirac in the middle of a basin. Velocity adds two questions
that a picture of a smooth blob cannot answer, and both of them are about
*where* the increment is rather than how big it is.

**Is it on the right faces?** SOCA carries `u` and `v` on the tracer array and
reads a symmetric-memory restart from the tracer origin, so index `i` of the
velocity increment is the face on the **low** side of tracer `i`, and
`ackbar.gridspec` moved the gridspec's `lonu` and `mask2du` to match. A
velocity increment displaced by one cell looks exactly like a correct one:
smooth, localized, the right amplitude, and written into faces the model treats
as land. So the check here is not a picture, it is an identity.

An ensemble covariance applied to a dirac is

    increment = L . sum_m X'_m . X'_m,T(dirac) / (N - 1)

where `L` is the localization applied to the dirac by itself, which the same run
writes out. Every term on the right can be computed from the member restarts
with numpy, and the member velocities have to be sliced out of a symmetric
restart to do it. The slice is the hypothesis: one of the two reproduces the
increment and the other does not, and the difference between them is exactly the
one cell shift this is looking for.

**Is it localized, and does the coast do anything to it?** Four diracs, one with
land close on each side of it, is what makes that answerable: a localization
whose scales are masked over land is normalized against a truncated kernel near
a coast, and one that is not masked is not. The four points are chosen so that
the nearest land is within one localization length in the named direction and
more than that away in the other three.
"""

import argparse
import glob
import html
import json
import os
import sys
from pathlib import Path

import netCDF4
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                              # noqa: E402
from matplotlib.collections import LineCollection            # noqa: E402
from matplotlib.colors import TwoSlopeNorm                   # noqa: E402
from matplotlib.patches import Rectangle                     # noqa: E402

#: The analysis variables, as the increment names them, with how to say each.
FIELDS = (("Temp", "temperature", "degC"),
          ("Salt", "salinity", "psu"),
          ("ave_ssh", "sea surface height", "m"),
          ("u", "eastward velocity", "m/s"),
          ("v", "northward velocity", "m/s"))

#: The two staggered ones, and the axis each is displaced along. `-1` is x, which
#: is `u`'s, and `-2` is y, which is `v`'s.
STAGGERED = {"u": -1, "v": -2}

#: What counts as "the increment reached here", as a fraction of the value at the
#: dirac. The same number `tools/dirac-page.py` uses, so the two pages' area
#: columns mean the same thing.
REACH = 0.05

#: How many cells either side of the dirac the face figure draws. Small enough
#: that one cell is several millimetres on the page, which is the whole point of
#: it: a half cell displacement has to be visible.
ZOOM = 6

#: How many cells either side the cross-variable maps draw.
WIDE = 18

LAND = "#d8d6d1"
INK = "#0b0b0b"
MUTED = "#52514e"
DEAD = "#e34948"

DPI = 130


# ------------------------------------------------------------------------------
# reading


def read_gridspec(path):
    """The grid, its masks, and the face coordinates the shift put where SOCA reads.

    `lonu` and `latv` are the west and south faces of each tracer cell, which is
    what `ackbar.gridspec` moved them to be. They are read here rather than
    reconstructed from `lon` and `dx` for the reason that module gives: a
    reconstruction assumes an answer to the question this page is asking.
    """
    with netCDF4.Dataset(path) as src:
        src.set_auto_mask(False)
        grid = {name: np.asarray(src.variables[name][0])
                for name in ("lon", "lat", "lonu", "latu", "lonv", "latv",
                             "dx", "dy", "distance_from_coast")}
        for name in ("mask2d", "mask2du", "mask2dv"):
            grid[name] = np.asarray(src.variables[name][0]) > 0.0
        grid["staggered"] = src.getncattr("ackbar_staggered_faces") \
            if "ackbar_staggered_faces" in src.ncattrs() else None
    return grid


def read_increments(directory):
    """The covariance's increment and the localization's, out of a `--keep` directory."""
    found = {}
    for path in sorted(glob.glob(f"{directory}/*.nc")):
        name = os.path.basename(path)
        key = "localization" if "_localization" in name else "covariance"
        with netCDF4.Dataset(path) as src:
            src.set_auto_mask(False)
            found[key] = {io: np.asarray(src.variables[io][0])
                          for io, _, _ in FIELDS if io in src.variables}
    for key in ("covariance", "localization"):
        if key not in found:
            sys.exit(f"dirac-uv-page: {directory} has no {key} increment in it")
    return found


def read_point(directory):
    points = json.load(open(f"{directory}/points.json"))
    if len(points) != 1:
        sys.exit(f"dirac-uv-page: {directory} holds {len(points)} diracs; this "
                 f"draws one per run")
    return points[0]


def member_fields(members, shape, shift):
    """Every member's state, with the staggered fields sliced *shift* cells in.

    *shift* is the hypothesis under test. `0` takes the low-side faces, which is
    what `ackbar.post` keeps and therefore what SOCA is being asserted to read;
    `1` takes the high-side ones, which is the pairing the gridspec described
    before `ackbar.gridspec` moved it.
    """
    ny, nx = shape
    paths = sorted(glob.glob(f"{members}/mem*/MOM.res.nc"))
    if len(paths) < 2:
        sys.exit(f"dirac-uv-page: {members} holds {len(paths)} member restarts")
    out = {io: [] for io, _, _ in FIELDS}
    for path in paths:
        with netCDF4.Dataset(path) as src:
            src.set_auto_mask(False)
            for io, _, _ in FIELDS:
                data = np.asarray(src.variables[io][0])
                if io == "u":
                    data = data[..., shift:shift + nx]
                elif io == "v":
                    data = data[..., shift:shift + ny, :]
                out[io].append(data)
    return {io: np.asarray(values) for io, values in out.items()}, len(paths)


def live_faces(restart, shape):
    """Which faces the model integrates, from a restart it wrote.

    A face is live if any level of it is nonzero: MOM6 multiplies the velocity by
    its own face mask, so a face that is land is exactly zero at every level and
    a face in the ocean is not. This recovers `mask2dCu` without reading a mask
    from anywhere, which is what makes it an independent statement about where
    the increment is allowed to be.
    """
    ny, nx = shape
    with netCDF4.Dataset(restart) as src:
        src.set_auto_mask(False)
        u = np.asarray(src.variables["u"][0])
        v = np.asarray(src.variables["v"][0])
    return ((u != 0.0).any(axis=0)[:, 0:nx], (v != 0.0).any(axis=0)[0:ny, :])


# ------------------------------------------------------------------------------
# the checks


def identity(point, run, members, count):
    """How exactly the increment is the localized sample covariance, per variable.

    The identity in the module docstring, evaluated over every cell of the
    domain. What comes back is the correlation and the largest disagreement as a
    fraction of the field's own peak, so a variable that matches reports 1 and
    something at the size of double precision rounding.
    """
    k0, j0, i0 = point["level"] - 1, point["j"], point["i"]
    localization = run["localization"]["Temp"][0]
    weight = (members["Temp"] - members["Temp"].mean(axis=0))[:, k0, j0, i0]

    rows = {}
    for io, _, _ in FIELDS:
        anomaly = members[io] - members[io].mean(axis=0)
        covariance = np.tensordot(weight, anomaly, axes=(0, 0)) / (count - 1)
        expect = covariance * (localization if covariance.ndim == 2
                               else localization[None, :, :])
        got = run["covariance"][io]
        peak = max(float(np.abs(expect).max()), 1e-30)
        big = np.abs(expect) > 1e-12 * peak
        rows[io] = {
            "r": float(np.corrcoef(got[big], expect[big])[0, 1]),
            "error": float(np.abs(got - expect).max() / peak),
        }
    return rows


def faces(point, run, grid, live):
    """Where the velocity increment landed, against where the model has a face.

    Three counts. `moved` is every face the increment is not exactly zero at,
    which is the whole footprint rather than a thresholded one, because the
    question here is placement and not size. `dead` is the part of that footprint
    the model does not integrate: an increment written into land, spent by the
    analysis and never read back by a forecast, and it is the count that has to be
    zero. `blank` is the opposite failure, a live face inside the localization's
    own reach that the increment left at exactly zero, which is what a footprint
    displaced off the coastline would leave behind.
    """
    out = {}
    for io in STAGGERED:
        got = run["covariance"][io][0]
        moved = got != 0.0
        out[io] = {
            "moved": int(moved.sum()),
            "dead": int((moved & ~live[io]).sum()),
            "blank": int((live[io] & near(point, run) & ~moved).sum()),
        }
    return out


def near(point, run):
    """Cells the localization still reaches, as the localization itself defines it."""
    localization = run["localization"]["Temp"][0]
    return localization > REACH * localization[point["j"], point["i"]]


def reach(point, run, grid, distance):
    """How much of the ocean one dirac moved, per variable, and how far away."""
    rows = {}
    level = point["level"] - 1
    for io, _, _ in FIELDS:
        data = run["covariance"][io]
        plane = data[level] if data.ndim == 3 else data
        peak = float(np.abs(plane).max())
        wet = grid["mask2du" if io == "u" else
                   "mask2dv" if io == "v" else "mask2d"]
        moved = wet & (np.abs(plane) > REACH * peak) if peak else None
        rows[io] = {
            "at dirac": float(plane[point["j"], point["i"]]),
            "peak": peak,
            "area": (0.0 if moved is None
                     else 100.0 * moved.sum() / wet.sum()),
            "far": (0.0 if moved is None or not moved.any()
                    else float(distance[moved].max())),
        }
    return rows


def scale_summary(path, grid):
    """A calibration's length scale and its normalization, over ocean and over land.

    `hzScales` is what `tools/diffusion-scales.py` asked for and `hzNorm` is what
    the randomization made of it. Both are read here because the pair
    `loc_hz`/`loc_hz_open` is supposed to differ in the first and only in the
    first: they carry identical multipliers and differ by whether the scale is
    zeroed over land.
    """
    shape = grid["mask2d"].shape
    with netCDF4.Dataset(path) as src:
        src.set_auto_mask(False)
        fields = {name: np.asarray(src.variables[name][:])[:, 0].reshape(shape)
                  for name in ("hzScales", "hzNorm")}
    wet = grid["mask2d"]
    return {
        "scales": fields["hzScales"],
        "norm": fields["hzNorm"],
        "ocean": (fields["hzScales"][wet].min() / 1e3,
                  fields["hzScales"][wet].mean() / 1e3,
                  np.median(fields["hzScales"][wet]) / 1e3,
                  fields["hzScales"][wet].max() / 1e3),
        "land": (fields["hzScales"][~wet].min() / 1e3,
                 fields["hzScales"][~wet].mean() / 1e3,
                 fields["hzScales"][~wet].max() / 1e3),
    }


def separation(grid, point):
    """Distance in km from the dirac to every tracer cell."""
    lat0 = np.radians(point["lat"])
    dlon = (grid["lon"] - point["lon"] + 180.0) % 360.0 - 180.0
    return np.hypot(np.radians(dlon) * np.cos(lat0) * 6371.0,
                    np.radians(grid["lat"] - point["lat"]) * 6371.0)


def geometry(point, grid, live, restart_levels):
    """What the point is: its depth, and how far the land is in each direction."""
    j, i = point["j"], point["i"]
    runs = {}
    for name, (dj, di) in (("north", (1, 0)), ("south", (-1, 0)),
                           ("east", (0, 1)), ("west", (0, -1))):
        for step in range(1, max(grid["mask2d"].shape)):
            y, x = j + dj * step, i + di * step
            if not (0 <= y < grid["mask2d"].shape[0]
                    and 0 <= x < grid["mask2d"].shape[1]) \
                    or not grid["mask2d"][y, x]:
                runs[name] = step
                break
        else:
            runs[name] = None
    return {
        "depth": float(restart_levels["depth"][j, i]),
        "levels": int(restart_levels["levels"][j, i]),
        "coast": float(grid["distance_from_coast"][j, i]) / 1e3,
        "runs": runs,
        "cell": float(grid["dx"][j, i]) / 1e3,
    }


def column_depths(restart, shape):
    with netCDF4.Dataset(restart) as src:
        src.set_auto_mask(False)
        h = np.asarray(src.variables["h"][0])
    live = h > 1e-2
    return {"depth": (h * live).sum(axis=0), "levels": live.sum(axis=0)}


# ------------------------------------------------------------------------------
# figures


def edges(grid):
    """Cell edges, built out of the face coordinates rather than out of the centres.

    The west edge of tracer `i` is `lonu[i]` and the south edge of tracer `j` is
    `latv[j]`, which is exactly the claim this page is checking, so the mesh is
    drawn from them: if that claim were wrong the mesh would not line up with the
    land and the figure would say so. The last edge on each axis has no face to
    read and is reflected from the centre.
    """
    x = np.empty(grid["lon"].shape[1] + 1)
    x[:-1] = grid["lonu"][0]
    x[-1] = 2.0 * grid["lon"][0, -1] - grid["lonu"][0, -1]
    y = np.empty(grid["lat"].shape[0] + 1)
    y[:-1] = grid["latv"][:, 0]
    y[-1] = 2.0 * grid["lat"][-1, 0] - grid["latv"][-1, 0]
    return x, y


def face_figure(runs, grid, live, outdir):
    """One panel per dirac, zoomed until a single cell is centimetres across.

    The temperature increment is shaded on the tracer cells. The velocity
    increment is drawn where it is: `u` as a bar on the west edge of its cell and
    `v` as a bar on the south edge, both at the dirac's own level. What the
    picture is for is the offset: the velocity response is centred half a cell
    west and half a cell south of the temperature response, because that is where
    those faces are, and not a whole cell away in either direction.
    """
    x, y = edges(grid)
    fig, axes = plt.subplots(1, len(runs), figsize=(4.4 * len(runs), 4.6),
                             squeeze=False, constrained_layout=True)
    for axis, (name, point, run) in zip(axes[0], runs):
        j, i = point["j"], point["i"]
        js = slice(max(j - ZOOM, 0), min(j + ZOOM + 1, grid["lon"].shape[0]))
        is_ = slice(max(i - ZOOM, 0), min(i + ZOOM + 1, grid["lon"].shape[1]))
        level = point["level"] - 1

        temp = np.ma.masked_where(~grid["mask2d"][js, is_],
                                  run["covariance"]["Temp"][level][js, is_])
        limit = float(np.abs(temp).max()) or 1.0
        axis.pcolormesh(x[is_.start:is_.stop + 1], y[js.start:js.stop + 1],
                        temp, cmap="RdBu_r",
                        norm=TwoSlopeNorm(0.0, -limit, limit), zorder=1)
        for row in range(js.start, js.stop):
            for col in range(is_.start, is_.stop):
                if not grid["mask2d"][row, col]:
                    axis.add_patch(Rectangle(
                        (x[col], y[row]), x[col + 1] - x[col],
                        y[row + 1] - y[row], facecolor=LAND, edgecolor="none",
                        zorder=2))

        speed = max(float(np.abs(run["covariance"]["u"][level][js, is_]).max()),
                    float(np.abs(run["covariance"]["v"][level][js, is_]).max()),
                    1e-12)
        segments, colors = [], []
        for row in range(js.start, js.stop):
            for col in range(is_.start, is_.stop):
                if live["u"][row, col]:
                    segments.append([(x[col], y[row] + 0.15 * (y[row + 1] - y[row])),
                                     (x[col], y[row + 1] - 0.15 * (y[row + 1] - y[row]))])
                    colors.append(run["covariance"]["u"][level][row, col] / speed)
                if live["v"][row, col]:
                    segments.append([(x[col] + 0.15 * (x[col + 1] - x[col]), y[row]),
                                     (x[col + 1] - 0.15 * (x[col + 1] - x[col]), y[row])])
                    colors.append(run["covariance"]["v"][level][row, col] / speed)
        bars = LineCollection(segments, cmap="PuOr_r", linewidths=3.4, zorder=4)
        bars.set_array(np.asarray(colors))
        bars.set_clim(-1.0, 1.0)
        axis.add_collection(bars)

        axis.add_patch(Rectangle((x[i], y[j]), x[i + 1] - x[i], y[j + 1] - y[j],
                                 fill=False, edgecolor=INK, linewidth=1.8,
                                 zorder=5))
        axis.set_xlim(x[is_.start], x[is_.stop])
        axis.set_ylim(y[js.start], y[js.stop])
        axis.set_xticks([]); axis.set_yticks([])
        axis.set_title(f"{name}: {point['lat']:.2f} N, {abs(point['lon']):.2f} W\n"
                       f"level {point['level']}, cell {grid['dx'][j, i] / 1e3:.0f} km",
                       fontsize=9)
    handle = fig.colorbar(bars, ax=axes, shrink=0.75)
    handle.set_label("velocity increment, as a fraction of the largest in the panel")
    return save(fig, outdir, "faces.png",
                "The temperature increment shaded on the tracer cells, land in "
                "grey, and the velocity increment drawn on the faces it occupies: "
                "u on the west edge of its cell, v on the south edge. The heavy "
                "outline is the cell the dirac was placed in. Only faces the model "
                "itself integrates are drawn.")


def crossvar_figure(runs, grid, outdir):
    """Every analysis variable at each dirac's level, over the coast it sits on."""
    columns = [(io, name) for io, name, _ in FIELDS if io != "Salt"]
    fig, axes = plt.subplots(len(runs), len(columns),
                             figsize=(3.5 * len(columns), 2.9 * len(runs)),
                             squeeze=False, constrained_layout=True)
    for row, (name, point, run) in enumerate(runs):
        j, i = point["j"], point["i"]
        js = slice(max(j - WIDE, 0), min(j + WIDE + 1, grid["lon"].shape[0]))
        is_ = slice(max(i - WIDE, 0), min(i + WIDE + 1, grid["lon"].shape[1]))
        level = point["level"] - 1
        for column, (io, label) in enumerate(columns):
            axis = axes[row][column]
            data = run["covariance"][io]
            plane = (data[level] if data.ndim == 3 else data)[js, is_]
            wet = grid["mask2du" if io == "u" else
                       "mask2dv" if io == "v" else "mask2d"][js, is_]
            plane = np.ma.masked_where(~wet, plane)
            limit = float(np.abs(plane).max()) or 1.0
            mesh = axis.pcolormesh(plane, cmap="RdBu_r",
                                   norm=TwoSlopeNorm(0.0, -limit, limit))
            axis.plot(i - is_.start + 0.5, j - js.start + 0.5, "k+",
                      markersize=8, markeredgewidth=1.2)
            axis.set_xticks([]); axis.set_yticks([])
            axis.set_aspect("equal")
            axis.set_title(f"{name}: {label}\npeak {limit:.4g}", fontsize=8)
            fig.colorbar(mesh, ax=axis, shrink=0.85)
    return save(fig, outdir, "crossvar.png",
                "Each dirac's own level, in every analysis variable but salinity, "
                "over the 37 cells around the point. A cross marks the dirac. "
                "There is no balance operator in an ensemble covariance, so every "
                "panel but the temperature one is the sample covariance.")


def decay_figure(runs, grid, outdir):
    """How far the velocity response reaches, against the temperature response."""
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4), constrained_layout=True)
    for axis, (io, label) in zip(axes, (("Temp", "temperature"),
                                        ("u", "eastward velocity"))):
        for name, point, run in runs:
            level = point["level"] - 1
            plane = run["covariance"][io][level]
            distance = separation(grid, point)
            wet = grid["mask2du" if io == "u" else "mask2d"] & (distance > 0)
            peak = float(np.abs(plane).max())
            if not peak:
                continue
            axis.plot(distance[wet], np.abs(plane[wet]) / peak, ".",
                      markersize=1.7, alpha=0.45, label=name)
        axis.axhline(REACH, color="0.4", linewidth=0.8, linestyle="--")
        axis.set_yscale("log")
        axis.set_ylim(1e-5, 2.0)
        axis.set_xlabel("distance from the dirac (km)")
        axis.set_ylabel(f"|{label} increment| / its peak")
        legend = axis.legend(markerscale=8, fontsize=8.5, loc="lower right")
        for handle in legend.legend_handles:
            handle.set_alpha(1.0)
    return save(fig, outdir, "decay.png",
                "Every wet cell at the dirac's level, as a fraction of the peak "
                "response. The velocity increment is localized exactly as the "
                "temperature one is, because the localization is a single scalar "
                "field applied to every analysis variable at once.")


def save(fig, outdir, name, caption):
    fig.savefig(f"{outdir}/{name}", dpi=DPI)
    plt.close(fig)
    return name, caption


# ------------------------------------------------------------------------------
# the page


def write_page(outdir, sections, figures, staggered, shifted):
    def table(head, rows):
        return ("<table><thead><tr>"
                + "".join(f"<th>{html.escape(str(cell))}</th>" for cell in head)
                + "</tr></thead><tbody>"
                + "".join("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>"
                                           for cell in row) + "</tr>"
                          for row in rows)
                + "</tbody></table>")

    panels = "\n".join(
        f"<figure><img src='{name}' alt='{html.escape(caption)}'>"
        f"<figcaption>{html.escape(caption)}</figcaption></figure>"
        for name, caption in figures)

    with open(f"{outdir}/index.html", "w") as stream:
        stream.write(f"""<!doctype html><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>dirac: velocity, and four coasts</title>
<link rel='stylesheet' href='/style.css'>
<style>
figure {{ margin: 1.6rem 0; }}
figure img {{ max-width: 100%; height: auto; display: block; }}
figcaption {{ font-size: 0.9rem; opacity: 0.75; margin-top: 0.4rem; }}
table {{ font-variant-numeric: tabular-nums; }}
li {{ margin: 0.4rem 0; }}
</style>
<main>
<h1>dirac: velocity, and four coasts</h1>
<p class='sub'>a unit temperature dirac at four points, one with land close on
each side, applied to the localized ensemble covariance</p>

<h2>the points</h2>
{table(("dirac", "position", "depth", "live levels", "cell", "land is",
        "distance to coast"), sections["points"])}

<h2>the velocity increment is the sample covariance, on the faces SOCA reads</h2>
<p>An ensemble covariance applied to a dirac is
<code>L . sum_m X'_m . X'_m,T(dirac) / (N-1)</code>, with <code>L</code> the
localization applied to the dirac by itself, which the same run writes out. Every
term is computable from the member restarts, and the velocities have to be sliced
out of a symmetric-memory restart to do it. The slice is the hypothesis. The
gridspec says its staggered fields are on the <b>{html.escape(str(staggered))}</b>
faces.</p>
{table(("dirac", "variable", "correlation", "max error / peak",
        "correlation, shifted one cell", "max error / peak, shifted"),
       sections["identity"])}
<p>The shifted columns are the same computation with the member velocities taken
from the other end of the staggered axis, which is the pairing the gridspec
described before <code>ackbar.gridspec</code> moved it. Tracers have no staggered
axis, so their two columns are the same number twice and are omitted.</p>

<h2>and none of it lands on model land</h2>
<p>A face is live if the model's own restart has a nonzero velocity anywhere down
it, which recovers MOM6's face mask without reading a mask. <b>dead</b> is a face
the increment moved that the model does not integrate: an increment written into
land, spent by the analysis and never read back by a forecast.</p>
{table(("dirac", "field", "faces moved", "of those, dead",
        "live faces in reach left at exactly zero"), sections["faces"])}

<h2>what one temperature dirac reached</h2>
{table(("dirac", "variable", "at the dirac", "peak", "ocean above 5%",
        "furthest cell above it"), sections["reach"])}

{panels}

<h2>the masked localization, and what it is not doing</h2>
<p><code>config/static/diffusion.yaml</code> carries two localization entries with
identical multipliers, differing only in whether the length scale is zeroed over
land, so a run through each is meant to be a measurement of the mask and of
nothing else. It is not one yet. The calibration reads its scale field back
through SOCA's ordinary state reader, and that reader replaces every masked cell
with the field's fill value, so the unmasked scale field is masked on the way in
and the two calibrations come out carrying the same scales.</p>
{table(("calibration", "file", "ocean min / mean / median / max (km)",
        "land min / mean / max (km)"), sections["scales"])}
{table(("dirac", "variable", "max |open - masked| / peak",
        "correlation"), sections["masked"])}
<p>{html.escape(shifted)}</p>
</main>
<footer>made by <code>tools/dirac-uv-page.py</code> from
<code>tools/soca-dirac.sh --ensemble --keep</code> output</footer>
""")


# ------------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gridspec", required=True)
    parser.add_argument("--restart", required=True)
    parser.add_argument("--members", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run", action="append", required=True,
                        metavar="NAME=DIR")
    parser.add_argument("--masked", action="append", default=[],
                        metavar="NAME=DIR",
                        help="the same diracs through the masked localization")
    parser.add_argument("--scales", action="append", default=[],
                        metavar="NAME=FILE",
                        help="a calibration to summarize, as loc_hz_open.nc")
    parser.add_argument("--note", default="",
                        help="the paragraph closing the page")
    args = parser.parse_args()

    grid = read_gridspec(args.gridspec)
    shape = grid["mask2d"].shape
    columns = column_depths(args.restart, shape)
    ulive, vlive = live_faces(args.restart, shape)
    live = {"u": ulive, "v": vlive}

    runs = []
    for entry in args.run:
        if "=" not in entry:
            sys.exit(f"dirac-uv-page: {entry!r} is not NAME=DIR")
        name, directory = entry.split("=", 1)
        runs.append((name, read_point(directory), read_increments(directory)))

    members = {shift: member_fields(args.members, shape, shift)
               for shift in (0, 1)}

    masked = {}
    for entry in args.masked:
        name, directory = entry.split("=", 1)
        masked[name] = read_increments(directory)

    sections = {"points": [], "identity": [], "faces": [], "reach": [],
                "scales": [], "masked": []}
    for entry in args.scales:
        name, path = entry.split("=", 1)
        summary = scale_summary(path, grid)
        sections["scales"].append((
            name, os.path.basename(path),
            "  ".join(f"{value:.1f}" for value in summary["ocean"]),
            "  ".join(f"{value:.1f}" for value in summary["land"])))
        sections["scales"] = sections["scales"]
    if len(args.scales) == 2:
        first, second = (scale_summary(entry.split("=", 1)[1], grid)
                         for entry in args.scales)
        sections["scales"].append((
            "difference", "",
            f"max |scale difference| {np.abs(first['scales'] - second['scales']).max():.3e} m",
            f"max |normalization difference| "
            f"{np.abs(first['norm'] - second['norm']).max():.3e}"))

    for name, point, run in runs:
        about = geometry(point, grid, live, columns)
        side = min(about["runs"], key=lambda key: about["runs"][key] or 10 ** 6)
        sections["points"].append((
            name,
            f"{point['lat']:.2f} N, {abs(point['lon']):.2f} W",
            f"{about['depth']:.0f} m",
            about["levels"],
            f"{about['cell']:.0f} km",
            f"{side}, {about['runs'][side]} cells",
            f"{about['coast']:.0f} km",
        ))

        straight = identity(point, run, *members[0])
        shifted = identity(point, run, *members[1])
        for io, label, _ in FIELDS:
            sections["identity"].append((
                name, label,
                f"{straight[io]['r']:.9f}", f"{straight[io]['error']:.1e}",
                f"{shifted[io]['r']:.6f}" if io in STAGGERED else "-",
                f"{shifted[io]['error']:.2f}" if io in STAGGERED else "-",
            ))

        counts = faces(point, run, grid, live)
        for io in STAGGERED:
            sections["faces"].append((name, io, counts[io]["moved"],
                                      counts[io]["dead"], counts[io]["blank"]))

        if name in masked:
            for io, label, _ in FIELDS:
                got = run["covariance"][io]
                other = masked[name]["covariance"][io]
                peak = max(float(np.abs(got).max()), 1e-30)
                sections["masked"].append((
                    name, label,
                    f"{np.abs(got - other).max() / peak:.2e}",
                    f"{np.corrcoef(got.ravel(), other.ravel())[0, 1]:.9f}"))

        distance = separation(grid, point)
        for io, label, unit in FIELDS:
            row = reach(point, run, grid, distance)[io]
            sections["reach"].append((
                name, label, f"{row['at dirac']:.4g} {unit}",
                f"{row['peak']:.4g} {unit}", f"{row['area']:.1f}%",
                f"{row['far']:.0f} km"))

    os.makedirs(args.out, exist_ok=True)
    figures = [face_figure(runs, grid, live, args.out),
               crossvar_figure(runs, grid, args.out),
               decay_figure(runs, grid, args.out)]
    write_page(args.out, sections, figures, grid["staggered"], args.note)
    print(f"dirac-uv-page: {args.out}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
