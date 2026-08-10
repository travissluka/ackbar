#!/usr/bin/env python3
"""Draw the increments a set of dirac runs produced, as one page.

    tools/dirac-page.py <gridspec> <restart> <points.json> <outdir> \\
                        <label>=<increment.nc>... [--ensemble <dir>]

Every argument after the output directory is one increment written by
`tools/soca-dirac.sh --keep`, under the name it should carry on the page. The
order is the order the panels appear in, so it is the order the argument list is
written in and not a rule inside here.

*restart* is the background the diracs were placed in. It is here for its layer
thicknesses and nothing else: an increment carries no vertical coordinate, and a
profile drawn against a level index is one nobody can read against an ocean.

## What it is for

A dirac report is a ruler: it gives the value at the point and the distance at
which the response has fallen to exp(-0.5) of it. Three questions about a
covariance are not answerable with a ruler, and they are the three that decide
whether an ensemble covariance is usable at all:

* **is the increment local**, which is a statement about the whole field rather
  than about one radius, because a sample covariance's spurious correlations are
  somewhere else in the basin rather than next to the dirac,
* **is it cross-variable**, and through what: the static B moves height through
  the balance operator and cannot move salinity at all, an ensemble moves both
  or neither and has no balance operator in it,
* **is it the right size and the right shape with depth**, which needs the
  column and not a number from one level of it.

One page with every run on it, because each of those is a comparison. A single
run's figures cannot show that a localization changed anything.

`--ensemble` is the member directory the ensemble covariance was built from. It
answers the third question and only the third: with it the page can say what the
ensemble's own spread and cross-variable correlation are at the dirac, which is
what turns "the salinity response is not zero" into a number an oceanographer
can agree or disagree with.

Nothing here reads a configuration. What a panel is called is what the caller
called it, so a mislabelled run is mislabelled on the page: the argument list is
the record of which increment came from which covariance.
"""

import html
import json
import os
import sys

import netCDF4
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                              # noqa: E402
from matplotlib.colors import TwoSlopeNorm                   # noqa: E402

#: The restart variable each analysis variable comes back as, and how to say it.
FIELDS = (("Temp", "temperature", "degC"),
          ("Salt", "salinity", "psu"),
          ("ave_ssh", "sea surface height", "m"))

#: What counts as "the increment has reached here" when measuring how much of
#: the basin one dirac moved. A fraction of the response at the dirac itself,
#: so it is scale free and comparable between a covariance and a localization.
REACH = 0.05

DPI = 130


def main(argv):
    if len(argv) < 6:
        sys.exit(__doc__.strip())
    gridspec, restart, points_path, outdir = argv[1:5]
    runs, ensemble, words = [], None, list(argv[5:])
    while words:
        word = words.pop(0)
        if word == "--ensemble":
            ensemble = words.pop(0) if words else sys.exit(
                "dirac-page: --ensemble needs a directory of members")
            continue
        if "=" not in word:
            sys.exit(f"dirac-page: {word!r} is not <label>=<increment.nc>")
        label, path = word.split("=", 1)
        runs.append((label, read(path)))

    grid = read_gridspec(gridspec)
    points = json.load(open(points_path))
    if len(points) != 1:
        sys.exit(f"dirac-page: {points_path} holds {len(points)} diracs; this "
                 f"draws the field around one and cannot tell two apart")
    point = points[0]

    os.makedirs(outdir, exist_ok=True)
    distance = separation(grid, point)
    depth = column_depth(restart, point)
    figures = [
        maps(grid, point, runs, outdir),
        decay(grid, point, runs, distance, outdir),
        cross_variable(grid, point, runs, outdir),
        vertical(grid, point, runs, depth, outdir),
    ]
    table = numbers(grid, point, runs, distance)
    write_page(outdir, point, figures, table,
               moments(ensemble, point) if ensemble else None)
    print(f"dirac-page: {outdir}/index.html")
    return 0


# ------------------------------------------------------------------------------
# reading


def read(path):
    with netCDF4.Dataset(path) as src:
        fields = {io: np.asarray(src.variables[io][0])
                  for io, _, _ in FIELDS if io in src.variables}
    if not fields:
        sys.exit(f"dirac-page: {path} carries none of {[f[0] for f in FIELDS]}")
    return fields


def read_gridspec(path):
    with netCDF4.Dataset(path) as src:
        grid = {name: np.asarray(src.variables[name][0])
                for name in ("lon", "lat", "dx", "dy")}
        grid["mask"] = np.asarray(src.variables["mask2d"][0]) > 0.0
    return grid


def separation(grid, point):
    """Distance in km from the dirac to every cell, good enough for a basin.

    A local tangent plane rather than a great circle: over 20 degrees of
    longitude at 25 north the difference is under a percent, and every use of
    this is a distance axis or a radius rather than a position.
    """
    lat0 = np.radians(point["lat"])
    dlon = (grid["lon"] - point["lon"] + 180.0) % 360.0 - 180.0
    dx = np.radians(dlon) * np.cos(lat0) * 6371.0
    dy = np.radians(grid["lat"] - point["lat"]) * 6371.0
    return np.hypot(dx, dy)


def plane_of(fields, io, level):
    """One level of a field, or the field itself when it has only one."""
    data = fields.get(io)
    if data is None:
        return None
    return data[level] if data.ndim == 3 else data


def masked(grid, values):
    return np.ma.masked_where(~grid["mask"], values)


# ------------------------------------------------------------------------------
# figures


def maps(grid, point, runs, outdir):
    """The temperature increment across the domain, one panel per run.

    Every panel on the same colour scale, taken from the largest response any
    of them produced, because the question is which of them put a response
    somewhere the others did not and a per-panel scale hides exactly that.
    """
    level = point["level"] - 1
    planes = [(label, masked(grid, plane_of(fields, "Temp", level)))
              for label, fields in runs]
    limit = max(float(np.abs(plane).max()) for _, plane in planes) or 1.0

    columns = min(3, len(planes))
    rows = int(np.ceil(len(planes) / columns))
    fig, axes = plt.subplots(rows, columns, figsize=(4.6 * columns, 3.2 * rows),
                             squeeze=False, constrained_layout=True)
    for axis, (label, plane) in zip(axes.ravel(), planes):
        mesh = axis.pcolormesh(grid["lon"], grid["lat"], plane, cmap="RdBu_r",
                               norm=TwoSlopeNorm(0.0, -limit, limit),
                               shading="nearest")
        axis.plot(point["lon"], point["lat"], "k+", markersize=9, markeredgewidth=1.4)
        axis.set_title(f"{label}\npeak {plane[point['j'], point['i']]:.3f} degC",
                       fontsize=9)
        axis.set_xticks([]); axis.set_yticks([])
    for axis in axes.ravel()[len(planes):]:
        axis.axis("off")
    fig.colorbar(mesh, ax=axes, shrink=0.8, label="temperature increment (degC)")
    return save(fig, outdir, "maps.png",
                "Temperature increment at the dirac's own level, from a unit "
                "temperature dirac at the cross. One colour scale for every "
                "panel.")


def decay(grid, point, runs, distance, outdir):
    """How the response falls away with distance, over every cell in the domain.

    A cloud rather than a line along a grid row, because the failure being
    looked for is a response at a place the dirac has no business reaching, and
    a transect through the dirac would miss it. The vertical axis is the
    magnitude as a fraction of the response at the dirac, so runs of different
    amplitude are comparable.
    """
    level = point["level"] - 1
    fig, axis = plt.subplots(figsize=(7.6, 4.4), constrained_layout=True)
    wet = grid["mask"] & (distance > 0)
    for label, fields in runs:
        plane = plane_of(fields, "Temp", level)
        peak = abs(float(plane[point["j"], point["i"]]))
        if not peak:
            continue
        away = np.abs(plane[wet]) / peak
        if not away.any():
            # Nothing to draw, and saying so is the point: a covariance that
            # moved no cell but the dirac's own has no decay to look at, and a
            # legend entry with no dots under it reads as a plotting failure.
            axis.plot([], [], ".", markersize=1.6,
                      label=f"{label} (nothing away from the dirac)")
            continue
        axis.plot(distance[wet], away, ".", markersize=1.6, alpha=0.45, label=label)
    axis.axhline(REACH, color="0.4", linewidth=0.8, linestyle="--")
    axis.text(0.01, REACH * 1.2, f"{REACH:.0%} of the response at the dirac",
              transform=axis.get_yaxis_transform(), ha="left", fontsize=8,
              color="0.35")
    axis.set_yscale("log")
    axis.set_ylim(1e-5, 2.0)
    axis.set_xlabel("distance from the dirac (km)")
    axis.set_ylabel("|temperature increment| / value at the dirac")
    legend = axis.legend(markerscale=8, fontsize=8.5, loc="lower right")
    for handle in legend.legend_handles:
        handle.set_alpha(1.0)
    return save(fig, outdir, "decay.png",
                "Every wet cell at the dirac's level, as a fraction of the "
                "response at the dirac. A localized covariance leaves this "
                "plot at the bottom well before the far side of the basin.")


def cross_variable(grid, point, runs, outdir):
    """The three analysis variables, for each run: what one dirac reached.

    Per-panel colour scales here, unlike the map figure, because the three
    variables are in different units and the question is the shape and the sign
    of each rather than which is biggest.
    """
    level = point["level"] - 1
    fig, axes = plt.subplots(len(runs), len(FIELDS),
                             figsize=(4.2 * len(FIELDS), 3.3 * len(runs)),
                             squeeze=False, constrained_layout=True)
    for row, (label, fields) in enumerate(runs):
        for column, (io, name, unit) in enumerate(FIELDS):
            axis = axes[row][column]
            plane = plane_of(fields, io, level)
            if plane is None:
                axis.axis("off")
                continue
            plane = masked(grid, plane)
            limit = float(np.abs(plane).max()) or 1.0
            mesh = axis.pcolormesh(grid["lon"], grid["lat"], plane, cmap="RdBu_r",
                                   norm=TwoSlopeNorm(0.0, -limit, limit),
                                   shading="nearest")
            axis.plot(point["lon"], point["lat"], "k+", markersize=8,
                      markeredgewidth=1.2)
            axis.set_xticks([]); axis.set_yticks([])
            axis.set_title(f"{label}: {name}\n"
                           f"{plane[point['j'], point['i']]:.4g} {unit} at the dirac",
                           fontsize=8.5)
            fig.colorbar(mesh, ax=axis, shrink=0.85)
    return save(fig, outdir, "crossvar.png",
                "The same temperature dirac, in each analysis variable. The "
                "static B has no temperature to salinity jacobian, so its "
                "salinity panel is identically zero; an ensemble has no balance "
                "operator, so everything in its other two panels is the sample "
                "covariance.")


def vertical(grid, point, runs, depth, outdir):
    """The column at the dirac, and a section through it.

    The section is the last run given: one section is enough to say what the
    vertical structure looks like and several would be a wall of panels saying
    the same thing, so which one it is belongs to the caller's argument order.
    """
    level = point["level"] - 1

    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.6), constrained_layout=True,
                             gridspec_kw={"width_ratios": [1, 1, 1.5]})
    for axis, (io, name, unit) in zip(axes[:2], FIELDS[:2]):
        for label, fields in runs:
            data = fields.get(io)
            if data is None or data.ndim != 3:
                continue
            axis.plot(data[:, point["j"], point["i"]], depth, label=label,
                      linewidth=1.4)
        axis.axhline(depth[level], color="0.5", linewidth=0.8, linestyle=":")
        axis.invert_yaxis()
        axis.set_ylim(1200.0, 0.0)
        axis.set_xlabel(f"{name} increment ({unit})")
        axis.set_ylabel("depth (m)")
        axis.grid(alpha=0.25)
    axes[0].legend(fontsize=8)

    label, fields = runs[-1]
    data = fields["Temp"]
    section = np.ma.masked_where(
        ~np.broadcast_to(grid["mask"][point["j"]], data.shape[:1] + (data.shape[2],)),
        data[:, point["j"], :])
    limit = float(np.abs(section).max()) or 1.0
    lon = grid["lon"][point["j"]]
    mesh = axes[2].pcolormesh(np.broadcast_to(lon, section.shape),
                              np.broadcast_to(depth[:, None], section.shape),
                              section, cmap="RdBu_r",
                              norm=TwoSlopeNorm(0.0, -limit, limit),
                              shading="nearest")
    axes[2].plot(point["lon"], depth[level], "k+", markersize=9, markeredgewidth=1.4)
    axes[2].set_ylim(1200.0, 0.0)
    axes[2].set_xlabel("longitude")
    axes[2].set_ylabel("depth (m)")
    axes[2].set_title(f"{label}: temperature increment along {point['lat']:.1f} N",
                      fontsize=9)
    fig.colorbar(mesh, ax=axes[2], label="degC")
    return save(fig, outdir, "vertical.png",
                "The column at the dirac and a west to east section through it. "
                "The dotted line is the level the dirac was placed in.")


def column_depth(restart, point):
    """Depth of each level centre in the dirac's own column.

    The model's own thicknesses, cumulated: under Z* they differ from column to
    column and from any nominal table, so this is the only depth axis that is
    the one the increment is actually on.
    """
    with netCDF4.Dataset(restart) as src:
        thickness = np.asarray(src.variables["h"][0])[:, point["j"], point["i"]]
    return np.cumsum(thickness) - 0.5 * thickness


def save(fig, outdir, name, caption):
    fig.savefig(f"{outdir}/{name}", dpi=DPI)
    plt.close(fig)
    return name, caption


# ------------------------------------------------------------------------------
# the numbers beside them


def numbers(grid, point, runs, distance):
    """One row per run: the value in each variable, and how far it reached."""
    level = point["level"] - 1
    rows = []
    for label, fields in runs:
        plane = plane_of(fields, "Temp", level)
        peak = abs(float(plane[point["j"], point["i"]]))
        reached = (grid["mask"] & (np.abs(plane) > REACH * peak)) if peak else None
        rows.append({
            "label": label,
            "values": [(f"{plane_of(fields, io, level)[point['j'], point['i']]:.4g}"
                        f" {unit}") if fields.get(io) is not None else "-"
                       for io, _, unit in FIELDS],
            "area": ("-" if reached is None
                     else f"{100.0 * reached.sum() / grid['mask'].sum():.1f}%"),
            "far": ("-" if reached is None or not reached.any()
                    else f"{distance[reached].max():.0f} km"),
        })
    return rows


def moments(directory, point):
    """The ensemble's own spread and correlations at the dirac.

    The same numbers the covariance is made of, computed here from the member
    files rather than read out of any increment, so they are an independent
    reading of what the localized increment should have come back as. A
    localization is 1 at zero separation, so the temperature entry should match
    the temperature increment at the dirac to whatever the normalization's Monte
    Carlo error is, and a disagreement larger than that is the interesting kind.
    """
    import glob
    level, j, i = point["level"] - 1, point["j"], point["i"]
    columns = {"Temp": [], "Salt": [], "ave_ssh": []}
    for path in sorted(glob.glob(f"{directory}/mem*/MOM.res.nc")):
        with netCDF4.Dataset(path) as src:
            for io in columns:
                data = np.asarray(src.variables[io][0])
                columns[io].append(float(data[level, j, i] if data.ndim == 3
                                         else data[j, i]))
    if len(columns["Temp"]) < 2:
        sys.exit(f"dirac-page: {directory} holds fewer than two members")
    stack = np.vstack([columns[io] for io, _, _ in FIELDS])
    covariance = np.cov(stack)
    spread = np.sqrt(np.diag(covariance))
    return {
        "members": len(columns["Temp"]),
        "spread": [(name, f"{value:.4g} {unit}")
                   for (_, name, unit), value in zip(FIELDS, spread)],
        "covariance": [(name, f"{value:.4g}")
                       for (_, name, _), value in zip(FIELDS, covariance[0])],
        "correlation": [(name, f"{value:.2f}") for (_, name, _), value
                        in zip(FIELDS, covariance[0] / (spread[0] * spread))],
    }


def write_page(outdir, point, figures, rows, ensemble=None):
    def cells(values):
        return "".join(f"<td>{html.escape(value)}</td>" for value in values)

    head = "".join(f"<th>{html.escape(name)}</th>" for _, name, _ in FIELDS)
    body = "\n".join(
        f"<tr><th>{html.escape(row['label'])}</th>{cells(row['values'])}"
        f"<td>{row['area']}</td><td>{row['far']}</td></tr>" for row in rows)
    panels = "\n".join(
        f"<figure><img src='{name}' alt='{html.escape(caption)}'>"
        f"<figcaption>{html.escape(caption)}</figcaption></figure>"
        for name, caption in figures)

    if ensemble is None:
        aside = ""
    else:
        def line(name, pairs):
            return (f"<tr><th>{name}</th>"
                    + "".join(f"<td>{html.escape(value)}</td>"
                              for _, value in pairs) + "</tr>")
        aside = f"""
<h2>the ensemble at the dirac</h2>
<p>Computed from the {ensemble['members']} member files themselves, not from any
increment. The temperature row of the covariance is what an unlocalized ensemble
covariance returns at the dirac, so the increment above should match it to the
localization's value at zero separation.</p>
<table>
<thead><tr><th></th>{head}</tr></thead>
<tbody>
{line('spread', ensemble['spread'])}
{line('covariance with temperature', ensemble['covariance'])}
{line('correlation with temperature', ensemble['correlation'])}
</tbody></table>
"""

    with open(f"{outdir}/index.html", "w") as stream:
        stream.write(f"""<!doctype html><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>dirac: the ensemble covariance</title>
<link rel='stylesheet' href='/style.css'>
<style>
figure {{ margin: 1.6rem 0; }}
figure img {{ max-width: 100%; height: auto; display: block; }}
figcaption {{ font-size: 0.9rem; opacity: 0.75; margin-top: 0.4rem; }}
li {{ margin: 0.4rem 0; }}
</style>
<main>
<h1>dirac: the ensemble covariance</h1>
<p class='sub'>a unit temperature dirac at
{point['lat']:.2f} N, {abs(point['lon']):.2f} W, level {point['level']},
applied to each covariance</p>
<p>Three claims, each of them checkable against what is below.</p>
<ol>
<li><b>The increment is local.</b> The response falls away with distance from
the dirac and is at the bottom of the decay plot well before the far side of the
basin. The table says how much of the ocean it moved by more than
{REACH:.0%} of the value at the dirac, and how far the furthest such cell is.</li>
<li><b>It is cross-variable.</b> A temperature dirac produces a coherent
salinity and height increment. Through the static B that can only be the balance
operator, which carries no temperature to salinity jacobian, so its salinity
column is exactly zero. An ensemble covariance has no balance operator in it at
all: everything outside its temperature panel is the sample covariance.</li>
<li><b>The magnitudes and the vertical structure are physical.</b> The value at
the dirac is a covariance, so temperature returns the ensemble variance there
and the other two return the covariance with it. The column and the section show
what one observation reaches in the vertical.</li>
</ol>
<table>
<thead><tr><th>covariance</th>{head}
<th>ocean area above {REACH:.0%}</th><th>furthest cell above it</th></tr></thead>
<tbody>
{body}
</tbody></table>
{aside}
{panels}
</main>
<footer>made by <code>tools/dirac-page.py</code> from
<code>tools/soca-dirac.sh --keep</code> output</footer>
""")


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
