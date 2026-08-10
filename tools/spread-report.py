#!/usr/bin/env python3
"""Ensemble spread against the control's error, for every source of spread.

    spread-report.py osse25-4dletkf osse25-4dletkf-stoch --first 6 --last 22 \
        --out $ACKBAR_SCRATCH_ROOT/spread-report --figures site/monitor/spread-report

ACKBAR generates ensemble spread three ways: an oSPPT model perturbation, a
per-member atmosphere, and a per-member open boundary. Each is one knob against
a shared baseline, so the question is not which raises the spread the most, it
is which raises it where the ensemble is under-dispersed.

**The metric is spread over error, not spread.** More spread is not better. A
filter whose prior spread sits near its own error is consistent: the weight it
gives an observation is the weight the observation deserves. Below one it is
over-confident and ignores what it is told. Above one it is over-dispersed, and
in a densely observed field that means pulling observation noise into the
analysis, which is measurable as departures getting worse while the spread looks
healthier. `experiments/osse25-4dletkf-stoch-a035.yaml` is the worked case.

Both halves are computed on the same cells at the same times, which is what
makes the ratio mean anything:

  spread   the standard deviation across mem001..mem020 of the *prior*, then
           root mean square over the wet domain. mem000 is excluded: in a pure
           LETKF it is the posterior mean and not a draw from the distribution,
           so folding it in biases the estimate toward the control.
  error    the control's own background minus the nature run, root mean square
           over the same cells. `osse-truth` at gom_12km, co-located onto the
           experiment's gom_25km centres by `osse_grid`.
  ratio    mean spread over the window divided by mean error over it. Averaging
           the two reductions and dividing, rather than averaging per-cycle
           ratios, so a cycle with a small error cannot dominate.

Reads the online `bkg/` records, which hold all 21 members at every sub-window
and survive `cleanup`. Only the analysis times (00Z here) are scored: a
4D experiment records four sub-window states per cycle, and scoring them all
would weight it four times as heavily as its own analysis.

Depths are `osse_grid.DEPTHS`, rebuilt from each state's own `h` because a Z*
coordinate's layer depths move with the free surface. The surface mechanisms
(oSPPT, atmosphere) act through the mixed layer; the boundary mechanism acts on
water that arrives at the edge and is expected below it, so a surface-only
comparison would miss the whole point of one of the three.

Everything under `--out` is derived and re-creatable from `bkg/`. The per-cycle
cache exists so that adding an experiment does not re-read the ones already
done, and it may be deleted at any time.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from osse_grid import DEPTHS, centres, read_record, sample, to_depths, wet_in_both

#: Where experiments and the offline archives live is the site layer's answer,
#: not this file's. See "The site layer" in docs/design.md.
OUTPUT_ROOT = os.environ.get("ACKBAR_OUTPUT_ROOT")
STATIC_ROOT = os.environ.get("ACKBAR_STATIC_ROOT")
SCRATCH_ROOT = os.environ.get("ACKBAR_SCRATCH_ROOT")

#: The surface fields, as the key used everywhere below, a label, and units.
#: Sea surface height first because it is the field the altimeters constrain and
#: the one that is worst under-dispersed; the velocities last because nothing
#: observes them directly here and their ratio is a diagnostic rather than a
#: score.
SURFACE = (("ssh", "SSH", "sea surface height", "m"),
           ("sst", "SST", "sea surface temperature", "degC"),
           ("sss", "SSS", "sea surface salinity", "psu"),
           ("u", "u", "eastward velocity", "m/s"),
           ("v", "v", "northward velocity", "m/s"))

#: The depths the table reports, out of the full ladder the profile figure
#: draws. Chosen for what each one is, not for even spacing:
#:   0     the surface, where oSPPT and the atmosphere act
#:   100   the seasonal thermocline, where a surface perturbation stops
#:   300   the Loop Current's core and the subtropical underwater salinity
#:         maximum, the deepest structure altimetry can plausibly reach
#:   700   the depth the extended-forecast comparisons already quote, so a
#:         number here lines up with those
#:   1500  deep water, below anything a surface mechanism reaches and inside the
#:         part of the column only the open boundary renews
REPORT_DEPTHS = (0, 100, 300, 700, 1500)

#: Nominal grid spacing in km, per domain, for the distance-from-boundary bands.
#: Keyed rather than defaulted for the reason `obc-spread.py` gives: running a
#: gom_12km experiment with gom_25km's value puts every band edge at twice its
#: true distance and the answer is wrong without being visibly wrong.
SPACING = {"gom_25km": 25.0, "gom_12km": 12.5, "gom_8km": 8.0, "gom_4km": 4.0}

#: Distance bands from the open boundary, in km.
BANDS = (0, 50, 100, 200, 400, 10000)

CACHE_VERSION = 2


def die(message):
    raise SystemExit(f"spread-report: {message}")


def surface_of(state, key):
    """The surface plane of one field, from a record read by `osse_grid`."""
    if key == "ssh":
        return state["ssh"]
    return {"sst": state["t"], "sss": state["s"],
            "u": state["u"], "v": state["v"]}[key][0]


def analysis_times(root, experiment, at_hour):
    """(stamp, datetime) for every sub-window this experiment recorded at the
    analysis hour, in order.

    A record exists only once `post.state` has committed it, so an experiment
    still cycling simply has fewer, which is the case this whole script has to
    tolerate rather than wait on.
    """
    bkg = root / experiment / "bkg"
    if not bkg.is_dir():
        die(f"{experiment} has no bkg/ records under {bkg}. Either it has not "
            f"finished a cycle yet or post.state is not in its graph.")
    found = []
    for path in sorted(bkg.iterdir()):
        if not path.is_dir():
            continue
        when = datetime.strptime(path.name, "%Y%m%dT%H%M%SZ")
        if when.hour == at_hour:
            found.append((path.name, when))
    return found


def complete(root, experiment, stamp, members):
    """Does this record hold the control and every member asked for?"""
    cycle = root / experiment / "bkg" / stamp
    if not (cycle / "mem000.nc").exists():
        return False
    return all((cycle / f"mem{m:03d}.nc").exists() for m in members)


def rms(field, wet):
    values = field[wet]
    values = values[np.isfinite(values)]
    return float(np.sqrt(np.mean(values ** 2))) if values.size else float("nan")


def one_cycle(root, truth, experiment, stamp, when, members):
    """Spread and error maps for one analysis time.

    Returned rather than reduced, because the spatial figure wants the maps and
    the table wants the reduction, and reading twenty one records is the
    expensive part either way.

    The wet mask is `wet_in_both` and is applied to spread and error alike. The
    12 km coastline resolves inlets the 25 km one does not, and a cell that is
    ocean on one and land on the other would difference a real value against a
    fill; scoring the spread on a wider set than the error would make the ratio
    a comparison between two different domains.
    """
    cycle = root / experiment / "bkg" / stamp
    nature_path = truth / f"{when:%Y%m%dT%H%M}.nc"
    if not nature_path.exists():
        return None
    control = read_record(cycle / "mem000.nc")
    nature = sample(read_record(nature_path), control)
    wet = wet_in_both(control, nature)

    planes = {key: [] for key, *_ in SURFACE}
    columns = {"t": [], "s": []}
    for m in members:
        state = read_record(cycle / f"mem{m:03d}.nc")
        for key, *_ in SURFACE:
            planes[key].append(surface_of(state, key))
        depth = centres(state["h"])
        for name in columns:
            columns[name].append(to_depths(state[name], depth))

    out = {"wet": wet, "lon": control["lon"], "lat": control["lat"],
           "spread": {}, "error": {}}
    for key, *_ in SURFACE:
        out["spread"][key] = np.std(np.array(planes[key]), axis=0, ddof=1)
        out["error"][key] = surface_of(control, key) - surface_of(nature, key)

    mine = {name: to_depths(control[name], centres(control["h"]))
            for name in columns}
    theirs = {name: to_depths(nature[name], centres(nature["h"]))
              for name in columns}
    for name in columns:
        out["spread"][name] = np.std(np.array(columns[name]), axis=0, ddof=1)
        out["error"][name] = mine[name] - theirs[name]
    return out


def cached(cache, root, truth, experiment, stamp, when, members):
    """`one_cycle`, remembered. See the module docstring: derived, deletable."""
    path = cache / experiment / f"{stamp}.npz"
    if path.exists():
        try:
            with np.load(path) as store:
                if int(store["version"]) == CACHE_VERSION:
                    got = {"wet": store["wet"], "lon": store["lon"],
                           "lat": store["lat"], "spread": {}, "error": {}}
                    for key in list(k for k, *_ in SURFACE) + ["t", "s"]:
                        got["spread"][key] = store[f"spread_{key}"]
                        got["error"][key] = store[f"error_{key}"]
                    return got
        except (OSError, KeyError, ValueError):
            # A cache written by an interrupted run is a file that exists and
            # cannot be read. Recompute rather than fail: it is derived.
            pass
    got = one_cycle(root, truth, experiment, stamp, when, members)
    if got is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    store = {"version": CACHE_VERSION, "wet": got["wet"],
             "lon": got["lon"], "lat": got["lat"]}
    for key in got["spread"]:
        store[f"spread_{key}"] = got["spread"][key]
        store[f"error_{key}"] = got["error"][key]
    # Temp then rename, the same rule every task in the workflow follows: a
    # cache half written by an interrupted run is indistinguishable from a
    # complete one.
    # The temporary name still ends in `.npz`, because `savez_compressed`
    # appends that suffix to any name that does not and then renames a file
    # that was never written.
    scratch = path.with_suffix(".tmp.npz")
    np.savez_compressed(scratch, **store)
    scratch.rename(path)
    return got


def distance_to_boundary(wet, spacing):
    """Distance in km to the nearest open boundary, per cell.

    The GoM domain opens on three sides: north (J=N), east (I=N) and south
    (J=0). The western edge is the Mexican coast and is closed, so it is not in
    the minimum. Counted in grid cells and scaled, which is honest to about the
    width of a cell and is all an axis needs. The same construction
    `obc-spread.py` uses, and deliberately the same one: two answers to "how far
    from the boundary" would be two figures that cannot be read together.
    """
    ny, nx = wet.shape
    j, i = np.mgrid[0:ny, 0:nx]
    edge = np.minimum.reduce([ny - 1 - j, nx - 1 - i, j]).astype("f8")
    return edge * spacing


def reduce_run(cycles, wet):
    """Domain reductions for one experiment over its window.

    `wet` is the intersection over the whole window rather than each cycle's
    own, so every cycle of every experiment is scored on one fixed set of cells
    and a timeseries cannot move because the mask did.
    """
    out = {"surface": {}, "profile": {}}
    for key, *_ in SURFACE:
        out["surface"][key] = {
            "spread": [rms(c["spread"][key], wet) for c in cycles],
            "error": [rms(c["error"][key], wet) for c in cycles]}
    for name in ("t", "s"):
        out["profile"][name] = {
            "spread": [[rms(c["spread"][name][k], wet) for c in cycles]
                       for k in range(len(DEPTHS))],
            "error": [[rms(c["error"][name][k], wet) for c in cycles]
                      for k in range(len(DEPTHS))]}
    return out


def mean_maps(cycles, key, depth_index=None):
    """The window-mean spread and error map for one field."""
    def plane(c, kind):
        field = c[kind][key]
        return field if depth_index is None else field[depth_index]
    return (np.nanmean([plane(c, "spread") for c in cycles], axis=0),
            np.nanmean([np.abs(plane(c, "error")) for c in cycles], axis=0))


def summarize(series):
    """spread, error and their ratio, from the two per-cycle series."""
    spread = float(np.mean(series["spread"]))
    error = float(np.mean(series["error"]))
    return {"spread": spread, "error": error,
            "ratio": spread / error if error else float("nan"),
            "cycles": len(series["spread"])}


# --------------------------------------------------------------------------
# figures

#: The width of the text column these land in: 165 mm of an A4 page, in
#: inches. Every figure is drawn at this width so that it is placed at 1:1 and
#: a 9 point label is a 9 point label on paper. A figure drawn twice this wide
#: and scaled down is where six point tick labels come from, and it is the
#: usual reason a set of plots that looked fine on screen is unreadable in the
#: PDF they were made for.
PAGE_WIDTH = 6.5

#: Panels per row where a figure has one per experiment. Five in a row at
#: 165 mm is 33 mm each, which is narrower than the Gulf needs.
PANELS_PER_ROW = 3


def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # Light and explicit. The monitor pages are dark and these are not: they
    # are printed, and a figure that inherits a dark background from a style
    # sheet somewhere costs a page of toner and loses the grid lines.
    plt.rcParams.update({
        "font.size": 8.5, "axes.titlesize": 9, "axes.labelsize": 8.5,
        "xtick.labelsize": 7.5, "ytick.labelsize": 7.5,
        "legend.fontsize": 7.5, "figure.dpi": 200, "savefig.dpi": 200,
        "axes.grid": True, "grid.alpha": 0.25,
        "figure.facecolor": "white", "axes.facecolor": "white",
        "savefig.facecolor": "white", "savefig.transparent": False,
        "text.color": "black", "axes.labelcolor": "black",
        "xtick.color": "black", "ytick.color": "black",
        "axes.edgecolor": "black",
    })
    return plt


#: One colour per experiment, assigned in the order they are named so the
#: baseline is grey and whatever is being argued for is not. The same convention
#: and the same list as `osse-compare.py` and `osse-state-error.py`, because the
#: pages are read side by side and an experiment that is blue on one and red on
#: the other is a page that will be misread.
COLOURS = ["#777777", "#1f4e79", "#a33", "#2a7", "#c80", "#75a"]


#: Caption metrics, in inches, at the 7.5 point caption size: one line's
#: height, and the width of one character. Used to wrap the caption and to
#: reserve exactly the room it needs, rather than guessing a fraction that is
#: right for one figure's aspect and wrong for the next.
CAPTION_LINE = 0.135
CAPTION_CHAR = 0.059
LEGEND_ROW = 0.28


def footer(figure, text, axis=None, columns=1):
    """A caption, and optionally the figure's one legend, below the panels.

    Every figure here has to stand alone in a PDF, which means a caption; and a
    legend drawn inside a panel covers the first cycles of a timeseries, which
    is exactly where an ensemble is spinning up. Both go in a strip the
    constrained layout engine is told to keep off, because a caption drawn over
    the x axis labels is what makes a figure look finished and unreadable at
    once.

    The strip is measured in inches and converted, not stated as a fraction: a
    fraction that leaves room on a 5 inch figure leaves none on a 12 inch one,
    and matplotlib's own `wrap` wraps to the figure width *before*
    `bbox_inches="tight"` crops, so a long caption runs off the edge.

    `rect` is (left, bottom, width, height) and the height has to lose what the
    bottom gained. Passing a full height with a raised bottom pushes the top of
    the layout off the canvas, and the suptitle lands on the panel titles.
    """
    import math
    import textwrap
    columns_of_text = max(40, int((figure.get_figwidth() - 0.3) / CAPTION_CHAR))
    wrapped = textwrap.fill(" ".join(text.split()), columns_of_text)
    inches = CAPTION_LINE * (wrapped.count("\n") + 1) + 0.12
    legend_at = inches
    handles, labels = ([], [])
    if axis is not None:
        handles, labels = axis.get_legend_handles_labels()
        # The legend's own height, not one row's: four experiments in three
        # columns wrap, and a strip sized for one row puts the second row
        # through the x axis label.
        inches += LEGEND_ROW * math.ceil(len(labels) / max(columns, 1))
    room = min(0.40, inches / figure.get_figheight())
    figure.get_layout_engine().set(rect=(0.0, room, 1.0, 1.0 - room))
    figure.text(0.01, 0.06 * CAPTION_LINE / figure.get_figheight(), wrapped,
                ha="left", va="bottom", fontsize=7.5, color="#333333")
    if handles:
        figure.legend(handles, labels, loc="lower center", ncol=columns,
                      bbox_to_anchor=(0.5, legend_at / figure.get_figheight()),
                      frameon=False, fontsize=8)


def cycles_of(run):
    """The cycle numbers a run's series is indexed by."""
    return np.arange(len(run["surface"]["ssh"]["spread"])) + run["first"]


def timeseries_figure(report, out):
    """The headline: spread and spread over error, per cycle, per field.

    One row per field rather than one column, because at 165 mm five panels
    across is 33 mm each and the cycle axis stops being readable.
    """
    plt = _pyplot()
    names = list(report["experiments"])
    figure, axes = plt.subplots(len(SURFACE), 2, figsize=(PAGE_WIDTH, 8.6),
                                constrained_layout=True, squeeze=False,
                                sharex=True)
    for row, (key, short, _, units) in enumerate(SURFACE):
        for colour, name in zip(COLOURS, names):
            run = report["experiments"][name]
            series = run["surface"][key]
            x = cycles_of(run)
            axes[row][0].plot(x, series["spread"], marker="o", ms=2.5,
                              lw=1.3, color=colour, label=name)
            ratio = np.array(series["spread"]) / np.array(series["error"])
            axes[row][1].plot(x, ratio, marker="o", ms=2.5, lw=1.3,
                              color=colour, label=name)
        axes[row][0].set_ylabel(f"{short} spread [{units}]")
        axes[row][0].set_ylim(bottom=0.0)
        axes[row][1].axhline(1.0, ls="--", lw=1.1, color="#a33")
        axes[row][1].set_ylabel(f"{short} spread / error")
        axes[row][1].set_ylim(bottom=0.0)
    for axis in axes[-1]:
        axis.set_xlabel("cycle")
    figure.suptitle("Prior ensemble spread, and spread against the control's "
                    "error against truth", fontsize=10)
    footer(figure,
            "Left: ensemble standard deviation across mem001-mem020, root mean "
            "square over the wet domain, at each analysis time. Right: the "
            "same divided by the control background's root mean square error "
            "against the gom_12km nature run on the same cells. The dashed "
            "line at 1 is a consistent ensemble; below it the filter is "
            "over-confident, above it observation noise enters the analysis. "
            "Any experiment perturbing its atmospheric forcing is scored "
            "against a climatology-forced truth, so its ratio is not a "
            "calibration number; read its spread and not its ratio.",
           axis=axes[0][0], columns=min(len(names), 3))
    path = out / "spread-timeseries.png"
    figure.savefig(path)
    plt.close(figure)
    return path


def decay_rates(run):
    """e-folding time of the spread, in cycles, per surface field.

    A least squares fit of `log(spread)` against cycle number. Geometric decay
    is what an ensemble filter with no external spread source does: each
    analysis removes a fixed fraction and the relaxation puts a fixed fraction
    back, so the survivor is a constant ratio per cycle rather than a constant
    subtraction. Fitting the log is therefore fitting the mechanism, and the
    residual says whether the mechanism is the right one.

    Reported as cycles per e-folding, positive meaning decay, with the standard
    error of the slope propagated through. Two experiments whose e-folding
    times differ by less than their combined standard errors have not been
    shown to differ, and saying which is which is the whole point of quoting
    the number rather than describing the picture.
    """
    out = {}
    for key, *_ in SURFACE:
        y = np.array(run["surface"][key]["spread"], dtype=float)
        x = cycles_of(run).astype(float)
        good = np.isfinite(y) & (y > 0)
        if good.sum() < 4:
            out[key] = None
            continue
        x, y = x[good], np.log(y[good])
        slope, intercept = np.polyfit(x, y, 1)
        fit = slope * x + intercept
        residual = y - fit
        dof = max(len(x) - 2, 1)
        variance = float(np.sum(residual ** 2) / dof)
        sxx = float(np.sum((x - x.mean()) ** 2))
        stderr = float(np.sqrt(variance / sxx)) if sxx else float("nan")
        total = float(np.sum((y - y.mean()) ** 2))
        out[key] = {
            "per_cycle": float(np.exp(slope)),
            "efold_cycles": float(-1.0 / slope) if slope else float("inf"),
            "efold_stderr": (float(stderr / slope ** 2) if slope
                             else float("nan")),
            "slope": float(slope), "intercept": float(intercept),
            "slope_stderr": stderr,
            "r2": float(1.0 - np.sum(residual ** 2) / total) if total else
                  float("nan"),
            "cycles": int(good.sum())}
    return out


def decay_figure(report, out):
    """Is the spread collapsing, and does any mechanism change the slope?

    Log axis, so geometric decay is a straight line and two runs that decay at
    the same rate are two parallel lines whatever their level. That is the
    distinction the whole figure exists to make: a mechanism that raises the
    level and a mechanism that arrests the collapse are different things, and
    on a linear axis they look alike.
    """
    plt = _pyplot()
    names = list(report["experiments"])
    figure, axes = plt.subplots(2, 3, figsize=(PAGE_WIDTH, 4.6),
                                constrained_layout=True, squeeze=False)
    flat = [axis for row in axes for axis in row]
    for axis, (key, short, _, units) in zip(flat, SURFACE):
        for colour, name in zip(COLOURS, names):
            run = report["experiments"][name]
            x = cycles_of(run)
            axis.semilogy(x, run["surface"][key]["spread"], marker="o", ms=2.5,
                          lw=1.3, color=colour, label=name)
            fit = run.get("decay", {}).get(key)
            if fit:
                axis.semilogy(x, np.exp(fit["slope"] * x + fit["intercept"]),
                              ls=":", lw=1.0, color=colour)
        axis.set_title(f"{short} [{units}]")
        axis.set_xlabel("cycle")
    axes[0][0].set_ylabel("spread")
    axes[1][0].set_ylabel("spread")
    # The sixth cell stays empty. It used to carry the fitted rates as a
    # monospace block, on the reasoning that a reader shown two nearly parallel
    # lines wants the number immediately. At print size it was unreadable, it
    # clipped at the right edge, and it printed a large negative e-folding for a
    # slope indistinguishable from zero, which reads as a contradiction against
    # prose that calls that field arrested. The rates belong in a table, which
    # `decay.csv` and the report both carry.
    flat[len(SURFACE)].axis("off")
    figure.suptitle("Does the spread hold? Spread per cycle on a log axis",
                    fontsize=10)
    footer(figure,
            "Domain root mean square ensemble spread against cycle, log scale, "
            "so a constant fraction lost per cycle is a straight line. Dotted "
            "lines are the least squares fit; the fitted e-folding times and "
            "their standard errors are in decay.csv beside this figure. Two "
            "experiments whose lines are parallel differ in where the spread "
            "sits, not in whether it is held.",
           axis=flat[0], columns=min(len(names), 3))
    path = out / "spread-decay.png"
    figure.savefig(path)
    plt.close(figure)
    return path


def maps_figure(report, data, out, key, short, units, depth=None):
    """Where the spread is: one panel per experiment, one colour scale."""
    plt = _pyplot()
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    tag = key if depth is None else f"{key}{depth}"
    store = np.load(data / f"maps-{tag}.npz")
    names = [n for n in report["experiments"] if f"spread_{n}" in store]
    lon, lat, wet = store["lon"], store["lat"], store["wet"]
    planes = [np.where(wet, store[f"spread_{n}"], np.nan) for n in names]
    top = float(np.nanpercentile(np.concatenate([p[wet] for p in planes]), 99))

    projection = ccrs.PlateCarree()
    panels = len(names) + 1
    columns = min(PANELS_PER_ROW, panels)
    rows = int(np.ceil(panels / columns))
    # The Gulf is about 1.4 times as wide as it is tall on this projection;
    # the extra half inch per row is the title and the colour bar.
    figure, grid = plt.subplots(rows, columns,
                                figsize=(PAGE_WIDTH,
                                         rows * (PAGE_WIDTH / columns / 1.4 + 0.5)),
                                constrained_layout=True, squeeze=False,
                                subplot_kw={"projection": projection})
    axes = [axis for row in grid for axis in row]
    for axis, name, plane in zip(axes, names, planes):
        mesh = axis.pcolormesh(lon, lat, plane, vmin=0.0, vmax=top,
                               cmap="viridis", shading="auto",
                               transform=projection, rasterized=True)
        axis.set_title(name, fontsize=7.5)
    # The error, once, on its own scale: the spread panels are being read
    # against it and a reader who cannot see it is reading half the figure.
    error = np.where(wet, store["error"], np.nan)
    error_axis = axes[len(names)]
    mesh_e = error_axis.pcolormesh(lon, lat, error, vmin=0.0,
                                   vmax=float(np.nanpercentile(error[wet], 99)),
                                   cmap="magma_r", shading="auto",
                                   transform=projection, rasterized=True)
    error_axis.set_title(f"{names[0]}\ncontrol error against truth", fontsize=7.5)
    for axis in axes[:panels]:
        axis.add_feature(cfeature.LAND, facecolor="0.85", zorder=2)
        axis.coastlines(resolution="50m", linewidth=0.4, zorder=3)
        axis.set_extent([lon.min(), lon.max(), lat.min(), lat.max()], projection)
        axis.gridlines(draw_labels=False, linewidth=0.3, alpha=0.3)
    for axis in axes[panels:]:
        axis.set_visible(False)
    # The shared scale goes underneath everything, the error's beside its own
    # panel. Two bottom colour bars on a wrapped grid are laid out against the
    # same bounding box and are drawn on top of each other.
    figure.colorbar(mesh, ax=axes[:len(names)], shrink=0.7, aspect=40,
                    location="bottom", label=f"spread [{units}]")
    figure.colorbar(mesh_e, ax=error_axis, shrink=0.9, location="right",
                    label=f"|error| [{units}]")
    where = "surface" if depth is None else f"{depth} m"
    figure.suptitle(f"{short} at the {where}: window-mean prior spread, "
                    f"one colour scale across experiments", fontsize=10)
    footer(figure,
            f"Ensemble standard deviation of {short} at the {where}, averaged "
            f"over the scored cycles of each experiment. All spread panels "
            f"share one scale so the experiments are comparable at a glance; "
            f"the last panel is the baseline control's mean absolute error "
            f"against truth on its own scale, which is what the spread is "
            f"trying to match.")
    path = out / f"spread-map-{tag}.png"
    figure.savefig(path)
    plt.close(figure)
    return path


def profile_figure(report, out):
    """Spread against depth for T and S, and the ratio beside it.

    The depth axis is the reason the boundary experiment is in this report at
    all: oSPPT and the atmosphere act through the mixed layer, and water
    arriving at the open boundary does not.
    """
    plt = _pyplot()
    depths = np.array(DEPTHS, dtype=float)
    figure, axes = plt.subplots(2, 2, figsize=(PAGE_WIDTH, 7.4),
                                constrained_layout=True, sharey=True)
    for row, (name, label, units) in enumerate((("t", "temperature", "degC"),
                                                ("s", "salinity", "psu"))):
        for colour, exp in zip(COLOURS, report["experiments"]):
            run = report["experiments"][exp]["profile"][name]
            spread = np.array([np.mean(level) for level in run["spread"]])
            error = np.array([np.mean(level) for level in run["error"]])
            axes[row][0].plot(spread, depths, lw=1.5, marker="o", ms=2.5,
                              color=colour, label=exp)
            axes[row][1].plot(spread / error, depths, lw=1.5, marker="o",
                              ms=2.5, color=colour, label=exp)
        axes[row][0].set_xlabel(f"spread [{units}]")
        axes[row][1].set_xlabel("spread / error")
        axes[row][1].axvline(1.0, ls="--", lw=1.1, color="#a33")
        axes[row][0].set_ylabel(f"depth [m]  ({label})")
        axes[row][0].set_xlim(left=0.0)
        axes[row][1].set_xlim(left=0.0)
    axes[0][0].invert_yaxis()
    axes[0][0].set_yscale("symlog", linthresh=100)
    figure.suptitle("Spread and spread over error through the water column",
                    fontsize=10)
    footer(figure,
            "Window-mean ensemble spread (left) and its ratio to the control's "
            "error (right), on the fixed depth ladder, rebuilt from each "
            "state's own layer thicknesses. Depth is on a symmetric log axis "
            "linear to 100 m, because the thermocline is where the surface "
            "mechanisms stop and drawn to scale it is a strip. The dashed "
            "line at 1 is a consistent ensemble.",
           axis=axes[0][0], columns=min(len(report["experiments"]), 3))
    path = out / "spread-profile.png"
    figure.savefig(path)
    plt.close(figure)
    return path


def distance_figure(report, out):
    """Spread in bands of distance from the open boundary.

    The one figure that separates a mechanism which raised the spread
    everywhere from one which raised it at the edge, which is the only claim
    the boundary ensemble makes.
    """
    if not any(run.get("by_distance") for run in report["experiments"].values()):
        return None
    plt = _pyplot()
    keys = [(key, short, units) for key, short, _, units in SURFACE
            if key in ("ssh", "sst", "sss")]
    figure, axes = plt.subplots(1, len(keys), figsize=(PAGE_WIDTH, 2.9),
                                constrained_layout=True, squeeze=False)
    for axis, (key, short, units) in zip(axes[0], keys):
        for colour, exp in zip(COLOURS, report["experiments"]):
            rows = report["experiments"][exp].get("by_distance", {}).get(key)
            if not rows:
                continue
            x = [0.5 * (r["from"] + min(r["to"], 600)) for r in rows]
            axis.plot(x, [r["spread"] for r in rows], marker="o", ms=3,
                      lw=1.4, color=colour, label=exp)
        axis.set_title(short)
        axis.set_xlabel("distance from open boundary [km]")
        axis.set_ylabel(f"spread [{units}]")
        axis.set_ylim(bottom=0.0)
    figure.suptitle("Prior spread against distance from the open boundary",
                    fontsize=10)
    footer(figure,
            "Window-mean surface spread reduced in bands of distance from the "
            "three open edges (north, east and south; the western edge is the "
            "Mexican coast and is closed). A mechanism that raises the spread "
            "by the same factor everywhere is doing something, but not the "
            "thing the boundary ensemble was built for.",
           axis=axes[0][0], columns=min(len(report["experiments"]), 3))
    path = out / "spread-distance.png"
    figure.savefig(path)
    plt.close(figure)
    return path


def ratio_figure(report, out):
    """Every ratio in the report, on one axis, as the summary a reader keeps."""
    plt = _pyplot()
    rows = [(key, short) for key, short, _, _ in SURFACE]
    rows += [(("t", d), f"T {d} m") for d in REPORT_DEPTHS if d]
    rows += [(("s", d), f"S {d} m") for d in REPORT_DEPTHS if d]
    names = list(report["experiments"])
    figure, axis = plt.subplots(figsize=(PAGE_WIDTH, 4.6),
                                constrained_layout=True)
    width = 0.8 / len(names)
    y = np.arange(len(rows))
    for index, (colour, exp) in enumerate(zip(COLOURS, names)):
        run = report["experiments"][exp]
        values = []
        for key, _ in rows:
            if isinstance(key, tuple):
                name, depth = key
                cell = run["summary"]["profile"][name].get(str(depth))
            else:
                cell = run["summary"]["surface"][key]
            values.append(cell["ratio"] if cell else np.nan)
        axis.barh(y + index * width, values, height=width, color=colour,
                  label=exp)
    axis.axvline(1.0, ls="--", lw=1.2, color="#a33")
    axis.set_yticks(y + 0.4 - width / 2)
    axis.set_yticklabels([label for _, label in rows])
    axis.invert_yaxis()
    axis.set_xlabel("spread / error")
    figure.suptitle("Ensemble consistency, every field and depth", fontsize=10)
    footer(figure,
            "Window-mean spread divided by window-mean control error. One is a "
            "consistent ensemble. Each experiment is scored over its own "
            "available cycles, which differ; the exact windows are in "
            "spread.json and spread.csv beside this figure.",
           axis=axis, columns=min(len(names), 3))
    path = out / "spread-ratio.png"
    figure.savefig(path)
    plt.close(figure)
    return path


# --------------------------------------------------------------------------

def write_csv(report, path):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["experiment", "field", "depth_m", "first_cycle",
                         "last_cycle", "first_time", "last_time", "n_cycles",
                         "spread", "error", "ratio", "units"])
        for name, run in report["experiments"].items():
            common = [name]
            tail = [run["first"], run["last"], run["times"][0],
                    run["times"][-1]]
            for key, short, _, units in SURFACE:
                cell = run["summary"]["surface"][key]
                writer.writerow(common + [short, 0] + tail +
                                [cell["cycles"], f"{cell['spread']:.6g}",
                                 f"{cell['error']:.6g}", f"{cell['ratio']:.4f}",
                                 units])
            for field, short, units in (("t", "T", "degC"), ("s", "S", "psu")):
                for depth in REPORT_DEPTHS:
                    cell = run["summary"]["profile"][field].get(str(depth))
                    if not cell:
                        continue
                    writer.writerow(common + [short, depth] + tail +
                                    [cell["cycles"], f"{cell['spread']:.6g}",
                                     f"{cell['error']:.6g}",
                                     f"{cell['ratio']:.4f}", units])


def write_decay_csv(report, path):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["experiment", "field", "first_cycle", "last_cycle",
                         "n_cycles", "fraction_per_cycle", "efold_cycles",
                         "efold_stderr", "r2"])
        for name, run in report["experiments"].items():
            for key, short, *_ in SURFACE:
                fit = run.get("decay", {}).get(key)
                if not fit:
                    continue
                writer.writerow([name, short, run["first"], run["last"],
                                 fit["cycles"], f"{fit['per_cycle']:.5f}",
                                 f"{fit['efold_cycles']:.3f}",
                                 f"{fit['efold_stderr']:.3f}",
                                 f"{fit['r2']:.4f}"])


def decay_table(report):
    lines = ["\nSpread decay: cycles per e-folding, with the fit's standard "
             "error. Larger is slower.",
             f"  {'experiment':<28}" + "".join(f"{short:>16}"
                                               for _, short, *_ in SURFACE)]
    for name, run in report["experiments"].items():
        row = ""
        for key, *_ in SURFACE:
            fit = run.get("decay", {}).get(key)
            row += (f"{fit['efold_cycles']:>9.1f}+-{fit['efold_stderr']:<5.1f}"
                    if fit else f"{'-':>16}")
        lines.append(f"  {name:<28}{row}")
    return "\n".join(lines)


def table(report):
    """The same numbers as the csv, for a terminal."""
    lines = []
    for name, run in report["experiments"].items():
        lines.append(f"\n{name}: cycles {run['first']}..{run['last']} "
                     f"({run['times'][0]} .. {run['times'][-1]}), "
                     f"{len(run['times'])} analysis times")
        lines.append(f"  {'field':<12}{'spread':>12}{'error':>12}{'ratio':>9}")
        for key, short, _, units in SURFACE:
            cell = run["summary"]["surface"][key]
            lines.append(f"  {short + ' (' + units + ')':<12}"
                         f"{cell['spread']:>12.4g}{cell['error']:>12.4g}"
                         f"{cell['ratio']:>9.3f}")
        for field, short in (("t", "T"), ("s", "S")):
            for depth in REPORT_DEPTHS:
                cell = run["summary"]["profile"][field].get(str(depth))
                if cell:
                    lines.append(f"  {short + ' @ ' + str(depth) + 'm':<12}"
                                 f"{cell['spread']:>12.4g}{cell['error']:>12.4g}"
                                 f"{cell['ratio']:>9.3f}")
    return "\n".join(lines)


def page(report, figures, written, data):
    """One served page per run, so the figures have a URL and a caption.

    Written into the figures directory rather than the data one: the site
    serves `site/monitor`, and a page whose images sit on another filesystem
    is a page that renders as broken boxes.
    """
    body = ["<!doctype html><meta charset=utf-8>",
            "<title>Ensemble spread against error</title>",
            "<link rel=stylesheet href=/style.css>",
            "<h1>Ensemble spread against error</h1>",
            "<p>Prior ensemble spread and the control's error against the "
            "nature run, for every source of spread ACKBAR implements. A "
            "consistent ensemble has a ratio near one; below it the filter is "
            "over-confident, above it observation noise enters the analysis.</p>",
            "<h2>Cycles scored</h2><ul>"]
    for name, run in report["experiments"].items():
        body.append(f"<li><b>{name}</b>: cycles {run['first']}..{run['last']}, "
                    f"{len(run['times'])} analysis times, "
                    f"{run['times'][0]} to {run['times'][-1]}</li>")
    body.append("</ul>")
    body.append(f"<p>Numbers: <code>{data / 'spread.csv'}</code> and "
                f"<code>{data / 'spread.json'}</code>.</p>")
    body.append("<h2>Table</h2><pre>" + table(report) + "\n"
                + decay_table(report) + "</pre>")
    body.append("<h2>Figures</h2>")
    for path in written:
        if path is None:
            continue
        body.append(f"<h3>{path.stem}</h3>")
        body.append(f"<img src='{path.name}' style='max-width:100%'>")
    (figures / "index.html").write_text("\n".join(body))
    return figures / "index.html"


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiments", nargs="+",
                        help="the baseline first, as in osse-compare.py")
    parser.add_argument("--first", type=int, default=1,
                        help="first cycle to score, 1-based over each "
                             "experiment's own analysis times")
    parser.add_argument("--last", type=int, default=0,
                        help="last cycle to score; 0 means whatever has landed")
    parser.add_argument("--members", type=int, default=20,
                        help="perturbed members, not counting the control")
    parser.add_argument("--domain", default="gom_25km",
                        help="which domain, for the distance bands' scale")
    parser.add_argument("--truth", type=Path,
                        help="the promoted nature run; defaults to "
                             "$ACKBAR_STATIC_ROOT/truth/gom_12km/osse-2015")
    parser.add_argument("--analysis-hour", type=int, default=0,
                        help="the sub-window hour that is a window centre")
    parser.add_argument("--out", type=Path,
                        help="where the table, the json and the map arrays go; "
                             "defaults to $ACKBAR_SCRATCH_ROOT/spread-report")
    parser.add_argument("--figures", type=Path,
                        help="where the png files go; defaults to --out")
    parser.add_argument("--cache", type=Path,
                        help="per-cycle reductions; defaults to <out>/cache")
    args = parser.parse_args()

    if not OUTPUT_ROOT or not STATIC_ROOT:
        die("ACKBAR_OUTPUT_ROOT or ACKBAR_STATIC_ROOT is unset; "
            "run `source site/activate.sh`")
    root = Path(OUTPUT_ROOT)
    truth = args.truth or Path(STATIC_ROOT) / "truth" / "gom_12km" / "osse-2015"
    if not truth.is_dir():
        die(f"no truth archive at {truth}")
    if args.domain not in SPACING:
        die(f"no grid spacing known for {args.domain}; the distance bands are "
            f"in km and would silently be wrong. Add it to SPACING. Known: "
            f"{', '.join(sorted(SPACING))}")
    out = args.out or (Path(SCRATCH_ROOT or ".") / "spread-report")
    figures = args.figures or out
    cache = args.cache or out / "cache"
    for path in (out, figures, cache):
        path.mkdir(parents=True, exist_ok=True)

    members = range(1, args.members + 1)
    report = {"truth": str(truth), "members": args.members,
              "domain": args.domain, "depths": list(DEPTHS),
              "requested": {"first": args.first, "last": args.last},
              "experiments": {}}
    loaded, wet = {}, None

    for name in args.experiments:
        times = analysis_times(root, name, args.analysis_hour)
        last = args.last or len(times)
        chosen = [(stamp, when) for index, (stamp, when)
                  in enumerate(times, start=1) if args.first <= index <= last]
        chosen = [(s, w) for s, w in chosen
                  if complete(root, name, s, members)]
        if not chosen:
            print(f"{name}: no complete ensemble in cycles "
                  f"{args.first}..{last or '-'} of {len(times)} recorded; "
                  f"skipped", file=sys.stderr)
            continue
        first_index = [s for s, _ in times].index(chosen[0][0]) + 1
        last_index = [s for s, _ in times].index(chosen[-1][0]) + 1
        print(f"{name}: cycles {first_index}..{last_index} of {len(times)} "
              f"recorded ({chosen[0][0]} .. {chosen[-1][0]})", file=sys.stderr)

        cycles = []
        for stamp, when in chosen:
            got = cached(cache, root, truth, name, stamp, when, members)
            if got is None:
                print(f"  {stamp}: no truth state, skipped", file=sys.stderr)
                continue
            cycles.append(got)
        if not cycles:
            print(f"{name}: no cycle the truth archive covers; skipped",
                  file=sys.stderr)
            continue
        loaded[name] = {"cycles": cycles, "times": [s for s, _ in chosen],
                        "first": first_index, "last": last_index}
        here = np.logical_and.reduce([c["wet"] for c in cycles])
        wet = here if wet is None else (wet & here)

    if not loaded:
        die("nothing to report on")

    distance = distance_to_boundary(wet, SPACING[args.domain])
    for name, run in loaded.items():
        cycles = run["cycles"]
        entry = reduce_run(cycles, wet)
        entry["first"], entry["last"] = run["first"], run["last"]
        entry["times"] = run["times"]
        entry["summary"] = {
            "surface": {key: summarize(entry["surface"][key])
                        for key, *_ in SURFACE},
            "profile": {name_: {str(DEPTHS[k]): summarize(
                {"spread": entry["profile"][name_]["spread"][k],
                 "error": entry["profile"][name_]["error"][k]})
                for k in range(len(DEPTHS))} for name_ in ("t", "s")}}
        entry["by_distance"] = {}
        for key, *_ in SURFACE:
            mean_spread, _ = mean_maps(cycles, key)
            rows = []
            for low, high in zip(BANDS[:-1], BANDS[1:]):
                band = wet & (distance >= low) & (distance < high)
                if band.any():
                    rows.append({"from": low, "to": high,
                                 "cells": int(band.sum()),
                                 "spread": rms(mean_spread, band)})
            entry["by_distance"][key] = rows
        entry["decay"] = decay_rates(entry)
        report["experiments"][name] = entry

    # The map arrays, one file per field, holding every experiment's window-mean
    # spread on the shared wet mask plus the baseline's error for the scale.
    names = list(report["experiments"])
    lon, lat = loaded[names[0]]["cycles"][0]["lon"], loaded[names[0]]["cycles"][0]["lat"]
    lon2, lat2 = np.meshgrid(lon, lat)
    wanted = [(key, short, units, None) for key, short, _, units in SURFACE]
    wanted += [("t", "temperature", "degC", d) for d in (100, 300, 700)]
    wanted += [("s", "salinity", "psu", d) for d in (100, 300)]
    for key, short, units, depth in wanted:
        index = DEPTHS.index(depth) if depth is not None else None
        store = {"lon": lon2, "lat": lat2, "wet": wet}
        for name in names:
            spread, error = mean_maps(loaded[name]["cycles"], key, index)
            store[f"spread_{name}"] = spread
            if name == names[0]:
                store["error"] = error
        tag = key if depth is None else f"{key}{depth}"
        np.savez_compressed(out / f"maps-{tag}.npz", **store)

    (out / "spread.json").write_text(json.dumps(report, indent=2))
    write_csv(report, out / "spread.csv")
    write_decay_csv(report, out / "decay.csv")
    print(table(report))
    print(decay_table(report))
    print(f"\nwrote {out / 'spread.json'}, {out / 'spread.csv'} and "
          f"{out / 'decay.csv'}")

    written = [timeseries_figure(report, figures),
               decay_figure(report, figures),
               profile_figure(report, figures),
               ratio_figure(report, figures)]
    written.append(distance_figure(report, figures))
    for key, short, units, depth in wanted:
        written.append(maps_figure(report, out, figures, key, short, units, depth))
    written.append(page(report, figures, written, out))
    for path in written:
        if path is not None:
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
