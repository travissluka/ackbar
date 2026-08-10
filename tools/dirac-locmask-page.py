#!/usr/bin/env python3
"""Measure what masking a localization costs, once the mask is actually optional.

    tools/dirac-locmask-page.py --gridspec <soca_gridspec.nc> \\
        --scales masked=<loc_hz.nc> --scales open=<loc_hz_open.nc> \\
        --before <dir of the calibration this replaced> \\
        --out <dir> --pair east=<masked dir>,<open dir> ...

Each `--pair` is two directories `tools/soca-dirac.sh --ensemble --keep` wrote at
the same point, one through each of the calibration's two localization entries.

## What this is for

`config/static/diffusion.yaml` carries `loc_hz` and `loc_hz_open` with identical
multipliers, differing in one key: whether the length scale is zeroed over land.
The pair exists so that a run through each measures the mask and nothing else.

It did not measure anything at all until the calibration stopped handing its
scale field to SOCA under a *masked* variable: `soca_fields_read` replaces every
land cell of a masked field with the fill value before saber sees it, so the
unmasked field was masked on the way in and the two files came out bit
identical. `src/ackbar/diffusion.py` carries the fix and the reasoning.

## The three questions

**Did anything about the ocean change?** It must not have. The two scale fields
have to be bit identical over water and differ only over land, or the pair is no
longer a controlled comparison. The normalization is a different matter and is
*expected* to move near a coast, because that is the operator changing.

**Does a dirac see it?** The previous run's answer was exactly zero, to every
digit, in every variable. Anything else is the fix working.

**Is it in the direction the unmasked field predicts?** More ensemble influence
across a coastline, not less. Measured by classifying each cell the dirac
reaches by whether the straight path from the dirac to it crosses land: a
masked scale is a wall at every coast, so those are the cells that should gain
and the open water ones that should barely move.
"""

import argparse
import html
import json
import os
import sys

import netCDF4
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                              # noqa: E402

#: The increment the ensemble covariance produced, and the increment the
#: localization produced by itself, as `soca-dirac.sh --ensemble` names them.
COVARIANCE = "ocn.dirac_ensemble.an.2000-01-01T00:00:00Z.nc"
LOCALIZATION = "ocn.dirac_ensemble_localization.an.2000-01-01T00:00:00Z.nc"

#: The analysis variables, as restart names, with what to call them.
VARIABLES = (("Temp", "temperature"), ("Salt", "salinity"),
             ("ave_ssh", "sea surface height"),
             ("u", "eastward velocity"), ("v", "northward velocity"))

#: Below this fraction of the peak a cell is noise rather than reach, and
#: including it turns every ratio below into a ratio of two roundings.
REACH = 1.0e-3


def read_gridspec(path):
    with netCDF4.Dataset(path) as src:
        src.set_auto_mask(False)
        return {"wet": np.asarray(src.variables["mask2d"][0]) > 0.0,
                "lon": np.asarray(src.variables["lon"][0]),
                "lat": np.asarray(src.variables["lat"][0])}


def read_calibration(path, shape):
    """`hzScales` and `hzNorm` off a calibrated file, on the model grid.

    The toolbox writes them on the atlas node ordering, which for this geometry
    is the grid read row by row, so the reshape is the whole of the mapping.
    """
    with netCDF4.Dataset(path) as src:
        src.set_auto_mask(False)
        return {name: np.asarray(src.variables[name][:])[:, 0].reshape(shape)
                for name in ("hzScales", "hzNorm")}


def read_run(directory):
    with open(f"{directory}/points.json") as stream:
        point = json.load(stream)[0]
    fields = {}
    for label, name in (("covariance", COVARIANCE),
                        ("localization", LOCALIZATION)):
        with netCDF4.Dataset(f"{directory}/{name}") as src:
            src.set_auto_mask(False)
            fields[label] = {variable: np.asarray(src.variables[variable][:])[0]
                             for variable, _ in VARIABLES}
    return point, fields


def level(field, point):
    """The dirac's own level of a field that has levels, or the field itself."""
    return field[point["level"]] if field.ndim == 3 else field


def crosses_land(wet, j0, i0, j, i):
    """Whether the straight line from (j0, i0) to (j, i) passes over land.

    Sampled at half a cell, which is fine enough that a one cell isthmus is not
    stepped over and coarse enough that a diagonal touching one corner of a land
    cell does not count as crossing it.
    """
    steps = int(max(abs(j - j0), abs(i - i0)))
    if steps == 0:
        return False
    along = np.linspace(0.0, 1.0, 2 * steps + 1)[1:-1]
    js = np.rint(j0 + (j - j0) * along).astype(int)
    is_ = np.rint(i0 + (i - i0) * along).astype(int)
    return bool((~wet[js, is_]).any())


def shadow(wet, point, reached):
    """The cells the dirac reaches, split by whether land stands in the way."""
    across = np.zeros_like(wet)
    for j, i in zip(*np.nonzero(reached)):
        across[j, i] = crosses_land(wet, point["j"], point["i"], j, i)
    return reached & across, reached & ~across


# ------------------------------------------------------------------------------


def scale_rows(calibrations, wet):
    rows = []
    for name, path, fields in calibrations:
        scales = fields["hzScales"]
        rows.append((
            name, os.path.basename(path),
            "  ".join(f"{value / 1e3:.1f}" for value in
                      (scales[wet].min(), scales[wet].mean(),
                       np.median(scales[wet]), scales[wet].max())),
            "  ".join(f"{value / 1e3:.1f}" for value in
                      (scales[~wet].min(), scales[~wet].mean(),
                       scales[~wet].max()))))
    return rows


def difference_rows(masked, opened, wet):
    """Ocean identical, land not: the property the pair has to have."""
    rows = []
    for name in ("hzScales", "hzNorm"):
        a, b = masked[name], opened[name]
        rows.append((
            name,
            "yes" if np.array_equal(a[wet], b[wet]) else
            f"no, up to {np.abs(a[wet] - b[wet]).max():.3e}",
            f"{np.abs(a[~wet] - b[~wet]).max():.4g}",
            f"{np.isfinite(b).all()}"))
    return rows


def before_rows(calibrations, before, wet):
    """Each entry against the file the fix replaced.

    The masked entry is the control: nothing about it should have moved, so a
    bit identical `loc_hz.nc` says both that the change reaches only the
    unmasked field and that the randomization is repeatable at a fixed rank
    count, which is what makes the unmasked entry's normalization difference a
    measurement rather than Monte Carlo noise.
    """
    rows = []
    for name, path, fields in calibrations:
        old = read_calibration(f"{before}/{os.path.basename(path)}", wet.shape)
        for field in ("hzScales", "hzNorm"):
            a, b = old[field], fields[field]
            rows.append((name, os.path.basename(path), field,
                         "identical" if np.array_equal(a, b)
                         else f"differs, up to {np.abs(a - b).max():.4g}",
                         "identical" if np.array_equal(a[wet], b[wet])
                         else f"differs, up to {np.abs(a[wet] - b[wet]).max():.4g}"))
    return rows


def increment_rows(pairs, wet):
    rows = []
    for name, (point, masked, opened) in pairs.items():
        for variable, label in VARIABLES:
            a = level(masked["covariance"][variable], point)
            b = level(opened["covariance"][variable], point)
            peak = np.abs(a[wet]).max()
            rows.append((name, label, f"{np.abs(b - a)[wet].max() / peak:.3e}",
                         f"{peak:.4g}", f"{a[point['j'], point['i']]:.5g}",
                         f"{b[point['j'], point['i']]:.5g}"))
    return rows


def direction_rows(pairs, wet):
    """Where the change went: across a coast, or in open water."""
    rows = []
    for name, (point, masked, opened) in pairs.items():
        for variable, label in VARIABLES:
            a = level(masked["covariance"][variable], point)
            b = level(opened["covariance"][variable], point)
            reached = wet & (np.abs(a) > REACH * np.abs(a[wet]).max())
            across, clear = shadow(wet, point, reached)
            def change(where):
                if not where.any() or np.abs(a[where]).sum() == 0.0:
                    return int(where.sum()), "-"
                return (int(where.sum()),
                        f"{100 * (np.abs(b[where]).sum() / np.abs(a[where]).sum() - 1):+.1f}%")
            n_across, d_across = change(across)
            n_clear, d_clear = change(clear)
            rows.append((name, label, n_across, d_across, n_clear, d_clear))
    return rows


# ------------------------------------------------------------------------------


def scale_figure(path, calibrations, grid):
    """The two scale fields side by side, which is where the moat is visible."""
    wet = grid["wet"]
    figure, axes = plt.subplots(1, len(calibrations), figsize=(11, 4.0),
                                constrained_layout=True)
    top = max(fields["hzScales"].max() for _, _, fields in calibrations) / 1e3
    for axis, (name, _, fields) in zip(np.atleast_1d(axes), calibrations):
        image = axis.pcolormesh(grid["lon"], grid["lat"],
                                fields["hzScales"] / 1e3,
                                vmin=0.0, vmax=top, cmap="viridis")
        axis.contour(grid["lon"], grid["lat"], wet.astype(float), [0.5],
                     colors="white", linewidths=0.6)
        axis.set_title(f"{name}: localization length (km)")
        axis.set_xlabel("longitude")
        figure.colorbar(image, ax=axis)
    np.atleast_1d(axes)[0].set_ylabel("latitude")
    figure.savefig(path, dpi=120)
    plt.close(figure)


def ratio_figure(path, pairs, grid, span=14):
    """Open over masked, cell by cell, around each dirac.

    Land is drawn in grey and the cells whose path to the dirac crosses it are
    outlined, because those are the ones the argument is about.
    """
    wet = grid["wet"]
    figure, axes = plt.subplots(1, len(pairs), figsize=(5.4 * len(pairs), 4.6),
                                constrained_layout=True)
    for axis, (name, (point, masked, opened)) in zip(np.atleast_1d(axes),
                                                     pairs.items()):
        a = level(masked["localization"]["Temp"], point)
        b = level(opened["localization"]["Temp"], point)
        reached = wet & (a > REACH * a.max())
        across, _ = shadow(wet, point, reached)
        ratio = np.where(reached, b / np.where(a == 0.0, 1.0, a), np.nan)

        j, i = point["j"], point["i"]
        js = slice(max(j - span, 0), min(j + span + 1, wet.shape[0]))
        is_ = slice(max(i - span, 0), min(i + span + 1, wet.shape[1]))
        axis.pcolormesh(grid["lon"][js, is_], grid["lat"][js, is_],
                        np.where(wet[js, is_], np.nan, 1.0),
                        cmap="Greys", vmin=0.0, vmax=1.4, shading="nearest")
        image = axis.pcolormesh(grid["lon"][js, is_], grid["lat"][js, is_],
                                ratio[js, is_], cmap="RdBu_r",
                                vmin=0.5, vmax=1.5, shading="nearest")
        axis.contour(grid["lon"][js, is_], grid["lat"][js, is_],
                     across[js, is_].astype(float), [0.5],
                     colors="black", linewidths=1.2)
        axis.plot(point["lon"], point["lat"], "kx", markersize=9)
        axis.set_title(f"{name}: localization, open / masked")
        axis.set_xlabel("longitude")
        figure.colorbar(image, ax=axis)
    np.atleast_1d(axes)[0].set_ylabel("latitude")
    figure.savefig(path, dpi=120)
    plt.close(figure)


# ------------------------------------------------------------------------------


def write_page(outdir, sections, figures):
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
<title>dirac: the localization mask, now that it is one</title>
<link rel='stylesheet' href='/style.css'>
<style>
figure {{ margin: 1.6rem 0; }}
figure img {{ max-width: 100%; height: auto; display: block; }}
figcaption {{ font-size: 0.9rem; opacity: 0.75; margin-top: 0.4rem; }}
table {{ font-variant-numeric: tabular-nums; }}
li {{ margin: 0.4rem 0; }}
</style>
<main>
<h1>dirac: the localization mask, now that it is one</h1>
<p class='sub'>the same two coastal diracs through <code>loc_hz</code> and
<code>loc_hz_open</code>, after the calibration stopped masking the field it was
told not to mask</p>

<h2>what was wrong</h2>
<p>The calibration handed its length scale field to SOCA as a state named
<code>sea_surface_height_above_geoid</code>, whose entry in
<code>config/model/mom6sis2/fields_metadata.yaml</code> is masked, so
<code>soca_fields_read</code> replaced every land cell with the fill value
before saber saw the array. <code>tools/diffusion-scales.py</code> wrote the
land values correctly and the read discarded them, so <code>masked: false</code>
did nothing, <code>loc_hz_open.nc</code> came out bit identical to
<code>loc_hz.nc</code>, and every localization in use was the masked one. The
scale file is now written and named as
<code>friction_velocity_over_water</code>, one of the handful of entries the
metadata marks <code>masked: false</code>, and read out of the
<code>sfc</code> restart slot, which is where every such entry lives.</p>

<h2>the ocean did not move, and the land did</h2>
{table(("calibration", "file", "ocean min / mean / median / max (km)",
        "land min / mean / max (km)"), sections["scales"])}
{table(("field", "identical over ocean", "max difference over land",
        "unmasked field finite everywhere"), sections["difference"])}
<p>The scales are the controlled part of the comparison and are bit identical
over water. <code>hzNorm</code> is not, and must not be: it is the
normalization the randomization computed <em>from</em> those scales, so a
coastal cell whose kernel is no longer truncated gets a different one. That is
the operator changing, which is the point.</p>

<h2>and the masked entry did not move at all</h2>
{table(("calibration", "file", "field", "against the file this replaced",
        "over ocean"), sections["before"])}
<p>Both entries were recalibrated, because both are read through the changed
path. <code>loc_hz</code> coming back bit identical is two statements at once:
the fix reaches only the field that asked not to be masked, and the
randomization is repeatable at a fixed rank count, so
<code>loc_hz_open</code>'s normalization difference above is the operator
rather than the estimator.</p>

<h2>a dirac now sees it</h2>
{table(("dirac", "variable", "max |open - masked| / peak", "peak",
        "at the dirac, masked", "at the dirac, open"),
       sections["increments"])}
<p>Every one of these was exactly <code>0.00e+00</code> on the previous run,
which is what said the pair was measuring nothing.</p>

<h2>and it goes the way the unmasked field predicts</h2>
<p>Each cell the dirac reaches is classified by whether the straight line from
the dirac to it crosses land. A masked scale is a wall at every coast, so those
are the cells an unmasked localization should hand influence back to, and open
water should barely move.</p>
{table(("dirac", "variable", "cells across land", "change in |increment|",
        "cells in clear water", "change in |increment|"),
       sections["direction"])}
<p>The eastern point has no cell across land within the localization's reach,
and that is the domain rather than the measurement: the land east of it is the
Florida peninsula and what lies behind it is outside <code>gom_25km</code>
altogether, so there is no water on the far side to hand anything back to. Its
row is the null case, and the whole of its change is the renormalization along
the coast. The velocity rows are the same null for a different reason: a face
adjacent to land is dead, so it carries no increment either way.</p>

{panels}
</main>
<footer>made by <code>tools/dirac-locmask-page.py</code> from
<code>tools/soca-dirac.sh --ensemble --localization &lt;name&gt; --keep</code>
output</footer>
""")


# ------------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gridspec", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--scales", action="append", required=True,
                        metavar="NAME=FILE")
    parser.add_argument("--pair", action="append", required=True,
                        metavar="NAME=MASKEDDIR,OPENDIR")
    parser.add_argument("--before", required=True,
                        metavar="DIR",
                        help="the calibrations this run replaced, same names")
    args = parser.parse_args()

    grid = read_gridspec(args.gridspec)
    wet = grid["wet"]

    calibrations = []
    for entry in args.scales:
        if "=" not in entry:
            sys.exit(f"dirac-locmask-page: {entry!r} is not NAME=FILE")
        name, path = entry.split("=", 1)
        calibrations.append((name, path, read_calibration(path, wet.shape)))

    pairs = {}
    for entry in args.pair:
        name, directories = entry.split("=", 1)
        masked_dir, open_dir = directories.split(",", 1)
        point, masked = read_run(masked_dir)
        _, opened = read_run(open_dir)
        pairs[name] = (point, masked, opened)

    os.makedirs(args.out, exist_ok=True)
    scale_figure(f"{args.out}/scales.png", calibrations, grid)
    ratio_figure(f"{args.out}/ratio.png", pairs, grid)

    sections = {
        "scales": scale_rows(calibrations, wet),
        "difference": difference_rows(calibrations[0][2], calibrations[1][2],
                                      wet),
        "before": before_rows(calibrations, args.before, wet),
        "increments": increment_rows(pairs, wet),
        "direction": direction_rows(pairs, wet),
    }
    write_page(args.out, sections, (
        ("scales.png", "The calibrated localization length, masked and open. "
                       "The masked field is zero over land, which is a wall the "
                       "diffusion cannot carry anything across; the open one "
                       "runs continuously across the coast."),
        ("ratio.png", "The localization applied to the dirac by itself, open "
                      "over masked, around each point. Land is grey, the cells "
                      "whose path to the dirac crosses land are outlined, and "
                      "the cross is the dirac."),
    ))
    print(f"dirac-locmask-page: wrote {args.out}/index.html")


if __name__ == "__main__":
    main()
