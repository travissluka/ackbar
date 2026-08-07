#!/usr/bin/env python
"""Plot a free run's state record, which is how the spinup's length is decided.

    tools/osse-plots.py osse-spinup
    tools/osse-plots.py osse-spinup --state ana

Reads `<experiment>/bkg/<date>/mem000.nc`, the compressed per-cycle record
`post.state` writes, and nothing else. That is deliberate: those files are what
survives `cleanup`, so this works on an experiment whose restarts are long gone
and on one that is still running.

**What decides the spinup is the first two figures.** Domain kinetic energy and
the Loop Current's northern extent are the two things that are still moving
after the thermodynamics have settled, and the run is over when their trend is
gone. Not when they are flat: the atmospheric forcing is a climatology with a
seasonal cycle in it, so a flat line would mean something had died.

The rest are there to catch a spinup that is settling towards the wrong thing.
A drift plot that walks steadily in one direction at depth is a model finding a
new equilibrium rather than approaching the one it was initialized to.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import netCDF4
import numpy as np

#: Where the figures go, and why they go somewhere served rather than beside the
#: experiment: a plot nobody can open is a plot nobody reads. The monitor's
#: docroot is already published, and it writes one directory per *experiment*,
#: so `osse/` cannot collide with anything it owns.
DOCROOT = Path(__file__).resolve().parents[1] / "site" / "monitor" / "osse"

#: The Loop Current metric: the northernmost latitude reached by this contour of
#: sea surface height anomaly, inside the eastern Gulf box below.
#:
#: 17 cm is the conventional value and it is a contour of *anomaly*, not of the
#: model's own `ave_ssh`, whose zero is wherever the model's mass field put it.
#: The anomaly here is against the domain area mean at the same time, so the
#: metric does not move when the basin as a whole warms or when the free surface
#: drifts, both of which a spinup does.
LOOP_CONTOUR = 0.17
LOOP_BOX = {"lon": (-92.0, -83.0), "lat": (21.0, 30.0)}

#: A reference density, for kinetic energy per unit area. The record holds no
#: density and computing one from T and S would be a state equation this file
#: has no business owning; KE is being read for its *trend*, so a constant is
#: the honest simplification and 1025 is the number to state.
RHO0 = 1025.0

#: The depths the drift figures are sampled at, in metres. Fixed, and shared by
#: every cycle and every experiment, because the whole point is that two states
#: are compared at the same depth rather than at the same layer index. Spaced
#: to resolve the mixed layer and the thermocline and to say something about the
#: abyss without pretending to resolve it.
DEPTHS = np.array([
    0., 5., 10., 20., 30., 50., 75., 100., 125., 150., 200., 250., 300.,
    400., 500., 600., 800., 1000., 1250., 1500., 2000., 2500., 3000.,
])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("experiment")
    parser.add_argument("--state", default="bkg", choices=("bkg", "ana"),
                        help="which per-cycle record to read (default bkg)")
    parser.add_argument("--member", type=int, default=0)
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: the published one)")
    args = parser.parse_args(argv)

    root = os.environ.get("ACKBAR_OUTPUT_ROOT")
    if not root:
        sys.exit("osse-plots: no ACKBAR_OUTPUT_ROOT; source site/activate.sh")

    records = sorted((Path(root) / args.experiment / args.state).glob(
        f"*/mem{args.member:03d}.nc"))
    if not records:
        sys.exit(f"osse-plots: no {args.state} records under "
                 f"{Path(root) / args.experiment}; has a cycle finished?")

    out = args.out or (DOCROOT / args.experiment)
    out.mkdir(parents=True, exist_ok=True)

    grid = read_grid(records[0], Path(root) / args.experiment)
    series = [read_record(path, grid) for path in records]
    times = [entry["time"] for entry in series]

    figures = []
    figures += departures(Path(root) / args.experiment, out)
    figures.append(kinetic_energy(series, times, out))
    figures.append(loop_current(series, times, grid, out))
    figures.append(volume_means(series, times, out))
    figures += drift(series, times, out)
    figures += snapshots(series, grid, out)

    (out / "index.html").write_text(page(args.experiment, args.state, times,
                                         figures))
    print(f"osse-plots: {len(records)} cycles -> {out}/index.html")


# --- reading -----------------------------------------------------------------

def domain_of(experiment):
    """The domain name, out of the experiment's own frozen config.

    This used to be `$ACKBAR_OSSE_DOMAIN`, defaulting to `gom_25km`. That was
    right for exactly as long as every OSSE experiment was at a quarter degree,
    and the day one was not it read a gridspec belonging to a different grid.
    Here that happened to be caught by a shape mismatch on the mask; a domain
    with the same shape and different areas would have produced weighted means
    that were quietly wrong.

    `tools/promote-truth.sh` reads it the same way and for the same reason: the
    domain is a property of the experiment being read, and a second place to
    state it is a second thing that can disagree.
    """
    frozen = experiment / "cfg" / "experiment.yaml"
    if not frozen.exists():
        sys.exit(f"osse-plots: {frozen} does not exist, so there is no way to "
                 f"know which domain this experiment ran on. It is written by "
                 f"`ackbar create`.")
    import yaml
    return yaml.safe_load(frozen.read_text())["domain"]["name"]


def read_grid(path, experiment):
    """Coordinates, masks and cell area, which are the same in every record.

    Area comes from the domain's gridspec rather than from the record, which
    does not carry it. Without it every domain mean is a mean over grid cells
    rather than over ocean, and on a domain spanning 14 degrees of latitude
    those differ by enough to matter to a trend.
    """
    with netCDF4.Dataset(path) as ds:
        grid = {"lon": ds["lon"][:], "lat": ds["lat"][:],
                "mask": ds["mask"][:].astype(bool),
                "mask_u": ds["mask_u"][:].astype(bool),
                "mask_v": ds["mask_v"][:].astype(bool)}

    static = os.environ.get("ACKBAR_STATIC_ROOT")
    gridspec = (Path(static or "") / "static" / domain_of(experiment)
                / "soca_gridspec.nc")
    if gridspec.exists():
        with netCDF4.Dataset(gridspec) as ds:
            grid["area"] = np.asarray(ds["area"][:]).squeeze()
    else:
        # cos(lat) on a regular grid, which is what area is proportional to.
        # Stated rather than silently substituted: every mean below is then a
        # relative weight and the absolute KE is off by a constant.
        print(f"osse-plots: no gridspec at {gridspec}, weighting by cos(lat)")
        grid["area"] = np.cos(np.deg2rad(grid["lat"]))

    grid["weight"] = np.where(grid["mask"], grid["area"], 0.0)
    return grid


def to_center(field, axis):
    """A face-centred velocity averaged onto the tracer cell centres.

    The face at index `i` is the *east* or *north* one, so cell `i` is the mean
    of faces `i-1` and `i`, and the first row or column has no stored neighbour
    on its far side. It takes the one face it has, which is a half cell offset
    on one line of the domain edge and nothing anywhere else.

    Masked points are filled with zero rather than propagated. A masked land
    velocity next to an ocean cell would otherwise mask the ocean cell too, and
    the land is already excluded by the area weights.
    """
    filled = np.ma.filled(field, 0.0).astype(np.float64)
    shifted = np.roll(filled, 1, axis=axis)
    edge = [slice(None)] * filled.ndim
    edge[axis] = 0
    shifted[tuple(edge)] = filled[tuple(edge)]
    return 0.5 * (filled + shifted)


def _at_depths(field, thick, weight, ocean):
    """Area-mean of *field* on `DEPTHS`, interpolated per column first.

    Per column and then averaged, rather than averaging the layers and
    interpolating the mean profile. The two differ wherever the layer
    interfaces are not flat, which on a hybrid coordinate is everywhere the
    density structure varies, and the second is the one that reintroduces the
    artefact this function exists to remove.

    Below a column's own bottom the profile is not extended: those columns drop
    out of the average at that depth, so a deep level is a mean over the ocean
    deep enough to have one. The alternative, holding the bottom value, invents
    an abyss under the shelf.
    """
    values = np.ma.filled(field, np.nan)
    thickness = np.ma.filled(thick, 0.0)
    # Layer centres, which is where a layer's mean value actually sits.
    edges = np.cumsum(thickness, axis=0)
    centres = edges - 0.5 * thickness

    levels, rows, cols = values.shape
    out = np.full((DEPTHS.size, rows, cols), np.nan)
    for j in range(rows):
        for i in range(cols):
            if weight[j, i] == 0.0:
                continue
            column = centres[:, j, i]
            good = np.isfinite(values[:, j, i]) & (thickness[:, j, i] > 0)
            if good.sum() < 2:
                continue
            inside = DEPTHS <= column[good][-1]
            out[inside, j, i] = np.interp(
                DEPTHS[inside], column[good], values[good, j, i])

    mask = np.isfinite(out)
    scaled = np.where(mask, out, 0.0) * weight
    covered = np.where(mask, weight, 0.0).sum(axis=(1, 2))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(covered > 0, scaled.sum(axis=(1, 2)) / covered, np.nan)


def read_record(path, grid):
    """One cycle, reduced to the handful of numbers and fields plotted."""
    date = path.parent.name
    with netCDF4.Dataset(path) as ds:
        temp = np.ma.masked_invalid(ds["temperature"][:])
        salt = np.ma.masked_invalid(ds["salinity"][:])
        thick = np.ma.masked_invalid(ds["thickness"][:])
        ssh = np.ma.masked_invalid(ds["sea_surface_height"][:])
        u = np.ma.masked_invalid(ds["eastward_velocity"][:])
        v = np.ma.masked_invalid(ds["northward_velocity"][:])

    weight = grid["weight"]
    ocean = weight.sum()

    # u and v are on the cell faces: `lon_u` sits half a cell east of `lon` and
    # `lat_v` half a cell north of `lat`. They are averaged onto the tracer grid
    # before anything is integrated, because the weights and the thickness are
    # the tracer grid's and a domain integral that mixed the three would be
    # weighting each velocity by the wrong cell.
    uc = to_center(u, axis=2)
    vc = to_center(v, axis=1)

    # Depth-integrated kinetic energy per unit area, J/m2, then area averaged.
    column = (0.5 * RHO0 * (uc**2 + vc**2) * thick.filled(0.0)).sum(axis=0)

    volume = thick * weight

    # **On fixed depths, not on layer index.** The vertical coordinate is
    # HYCOM1: z* near the surface and isopycnal below, so a layer moves, and
    # layer 30 at the end of a run holds different water than layer 30 at the
    # start. Differencing by index reports that migration as a temperature
    # change, and it does it in the most misleading possible form, a smooth
    # anomaly of one sign spread over the whole lower column.
    #
    # Measured coordinate-free on a 60 day nature run, that artefact read as
    # +0.29 K of warming from 100 m to 1100 m where the actual heat change below
    # 200 m was -0.0015 K. This is what `h` is kept in the record for.
    layer_temp = _at_depths(temp, thick, weight, ocean)
    layer_salt = _at_depths(salt, thick, weight, ocean)

    surface = np.hypot(uc[0], vc[0])

    return {
        "time": datetime.strptime(date, "%Y%m%dT%H%M%SZ"),
        "ke": float((column * weight).sum() / ocean),
        "ssh": ssh,
        "ssh_mean": float((ssh * weight).sum() / ocean),
        "sst": temp[0],
        "speed": surface,
        "layer_temp": layer_temp,
        "layer_salt": layer_salt,
        "depth": DEPTHS,
        "temp_mean": float((temp * volume).sum() / volume.sum()),
        "salt_mean": float((salt * volume).sum() / volume.sum()),
    }


# --- the two that decide the spinup ------------------------------------------

def kinetic_energy(series, times, out):
    figure, axes = plt.subplots(figsize=(9, 4))
    axes.plot(times, [entry["ke"] for entry in series], marker="o", ms=3,
              color="#1f4e79")
    axes.set_ylabel("depth-integrated KE  [J m$^{-2}$]")
    axes.set_title("Domain kinetic energy")
    return save(figure, axes, out / "kinetic-energy.png",
                "Kinetic energy. The spinup is over when the trend is gone, "
                "not when the line is flat: the forcing climatology has a "
                "seasonal cycle and a flat line would mean the currents had "
                "died.")


def loop_current(series, times, grid, out):
    """Northernmost latitude of the 17 cm SSH anomaly contour, eastern Gulf.

    Computed by masking rather than by contouring, because a contour tracer
    returns a set of paths and the metric wants one number: the highest latitude
    of any ocean point inside the box whose anomaly exceeds the threshold. That
    is the same quantity for a Loop Current that has shed a ring and one that
    has not, which a path-following definition is not.
    """
    lon, lat = grid["lon"], grid["lat"]
    box = ((lon >= LOOP_BOX["lon"][0]) & (lon <= LOOP_BOX["lon"][1])
           & (lat >= LOOP_BOX["lat"][0]) & (lat <= LOOP_BOX["lat"][1])
           & grid["mask"])

    extent, area = [], []
    for entry in series:
        anomaly = entry["ssh"] - entry["ssh_mean"]
        inside = box & (anomaly >= LOOP_CONTOUR)
        extent.append(float(lat[inside].max()) if inside.any() else np.nan)
        area.append(float(grid["area"][inside].sum()) / 1e9)

    figure, axes = plt.subplots(figsize=(9, 4))
    axes.plot(times, extent, marker="o", ms=3, color="#a33", label="latitude")
    axes.set_ylabel("northern extent  [$^\\circ$N]", color="#a33")

    # The latitude alone is quantized to the grid, which at a quarter degree is
    # three or four distinct values across a whole intrusion cycle, so it reads
    # as a staircase whether or not anything is happening. The enclosed area is
    # the same metric without the quantization and is what the trend should be
    # read off.
    twin = axes.twinx()
    twin.plot(times, area, marker="s", ms=3, color="#36c", alpha=0.8,
              label="enclosed area")
    twin.set_ylabel("enclosed area  [10$^3$ km$^2$]", color="#36c")
    twin.grid(False)

    axes.set_title(f"Loop Current, {LOOP_CONTOUR:.2f} m SSH anomaly contour")
    return save(figure, axes, out / "loop-current.png",
                "Loop Current intrusion, as the northernmost latitude of the "
                "contour and as the area it encloses. It should extend, shed "
                "and retreat rather than sit still. A trace that does not move "
                "at 25 km is the domain barely permitting the eddy, which is a "
                "known limit of gom_25km rather than a spinup that has "
                "finished.")


# --- the ones that catch a spinup settling on the wrong thing -----------------

def departures(experiment, out):
    """Observation space, from the per-cycle summaries `post.obs` writes.

    First rather than last on the page, whenever there are any. On a free run
    these are the whole result: with no analysis, the O-B is the model's own
    distance from the observations and it is the number every assimilating
    experiment has to beat.

    Reads the summaries rather than the ioda files, so this works on a cycle
    whose `obs_out/` has been reduced and on an experiment that is still
    running. A cycle where an observer was dropped is a gap in the line rather
    than a zero, because a zero would read as a perfect fit.
    """
    summaries = sorted((experiment / "obs_out").glob("*/summary.json"))
    if not summaries:
        return []

    series = {}
    for path in summaries:
        when = datetime.strptime(path.parent.name, "%Y%m%dT%H%M%SZ")
        for record in json.loads(path.read_text()).get("observers", []):
            for variable, stats in record.get("variables", {}).items():
                entry = series.setdefault((record["name"], variable), [])
                entry.append((when, stats))

    figure, axes = plt.subplots(len(series), 1, figsize=(9, 3.2 * len(series)),
                                squeeze=False)
    for row, ((name, variable), entries) in zip(axes[:, 0], sorted(series.items())):
        times = [when for when, _ in entries]
        row.plot(times, [stats.get("omb", {}).get("rms") for _, stats in entries],
                 marker="o", ms=3, color="#a33", label="O-B rms")
        if any("oma" in stats for _, stats in entries):
            row.plot(times, [stats.get("oma", {}).get("rms") for _, stats in entries],
                     marker="s", ms=3, color="#1f4e79", label="O-A rms")
        row.axhline(0.0, color="#999", lw=0.8)

        kept = [stats.get("assimilated") or 0 for _, stats in entries]
        count = row.twinx()
        count.plot(times, kept, color="#999", lw=0.8, ls="--")
        count.set_ylabel("assimilated", color="#999", fontsize=8)
        count.grid(False)
        # Explicit, because the default leaves the line against the frame or
        # off the top of it, and a count that is clipped reads as a count that
        # fell.
        count.set_ylim(0, max(kept) * 1.25 or 1)

        row.set_title(f"{name}: {variable}", fontsize=10)
        row.set_ylabel("departure rms")
        row.grid(alpha=0.3)
        row.legend(fontsize=8, loc="upper left")

    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(out / "departures.png", dpi=120)
    plt.close(figure)
    return [("departures.png",
             "Observation space, per platform. The dashed grey line is how many "
             "observations survived QC, on the right axis: an rms that improves "
             "while that line falls is a filter rejecting what it cannot fit "
             "rather than an analysis fitting it.")]


def volume_means(series, times, out):
    figure, axes = plt.subplots(3, 1, figsize=(9, 7), sharex=True)
    axes[0].plot(times, [e["temp_mean"] for e in series], color="#1f4e79")
    axes[0].set_ylabel("T  [$^\\circ$C]")
    axes[1].plot(times, [e["salt_mean"] for e in series], color="#1f4e79")
    axes[1].set_ylabel("S  [PPT]")
    axes[2].plot(times, [e["ssh_mean"] for e in series], color="#1f4e79")
    axes[2].set_ylabel("SSH  [m]")
    axes[0].set_title("Volume mean temperature and salinity, and mean SSH")
    for ax in axes:
        ax.grid(alpha=0.3)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(out / "volume-means.png", dpi=120)
    plt.close(figure)
    return ("volume-means.png",
            "Whole-domain conservation. A steady walk in either of the top two "
            "is the model finding a different equilibrium from the one it was "
            "initialized to, which no amount of further spinup fixes.")


def drift(series, times, out):
    """Area-mean T and S at fixed depths against time, as anomalies from cycle 1.

    Anomalies rather than values because the signal is a fraction of a degree
    against a 25 degree range, and the question is whether it is still moving.

    Depths, not layers: see `_at_depths`. A version of this figure that
    differenced layer index showed a smooth 0.3 K warming reaching the abyss
    that was entirely the hybrid coordinate migrating.
    """
    figures = []
    depth = series[0]["depth"]
    for key, label, unit, cmap in (
            ("layer_temp", "temperature", "$^\\circ$C", "RdBu_r"),
            ("layer_salt", "salinity", "PPT", "BrBG_r")):
        field = np.array([entry[key] for entry in series])
        anomaly = field - field[0]
        limit = float(np.nanmax(np.abs(anomaly))) or 1e-9

        figure, axes = plt.subplots(figsize=(9, 4.5))
        mesh = axes.pcolormesh(times, depth, anomaly.T, cmap=cmap,
                               vmin=-limit, vmax=limit, shading="nearest")
        axes.set_yscale("symlog", linthresh=100)
        axes.invert_yaxis()
        # Ticks stated rather than left to symlog, which labels the decades and
        # nothing in the linear part, so the mixed layer came out unlabelled.
        axes.set_yticks([0, 25, 50, 100, 300, 1000, 3000])
        axes.get_yaxis().set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
        axes.set_ylabel("depth  [m]")
        axes.set_title(f"Area mean {label} anomaly from the first cycle, "
                       "at fixed depth")
        figure.colorbar(mesh, ax=axes, label=unit)
        figure.autofmt_xdate()
        figure.tight_layout()
        name = f"drift-{label}.png"
        figure.savefig(out / name, dpi=120)
        plt.close(figure)
        figures.append((name, f"{label.capitalize()} drift at fixed depths, "
                              "interpolated per column. Depth is log below "
                              "100 m so the mixed layer is readable next to the "
                              "abyss. A deep level averages only the columns "
                              "deep enough to have one."))
    return figures


def snapshots(series, grid, out):
    """First, middle and last cycle, for three surface fields.

    Three columns rather than an animation: what is being asked is whether the
    character of the flow changed, and two states far apart answer that better
    than sixty states in sequence.
    """
    picks = [0, len(series) // 2, len(series) - 1]
    lon, lat = grid["lon"], grid["lat"]
    figures = []

    for key, label, unit, cmap, anomaly in (
            ("sst", "sea surface temperature", "$^\\circ$C", "turbo", False),
            ("ssh", "sea surface height anomaly", "m", "RdBu_r", True),
            ("speed", "surface speed", "m s$^{-1}$", "magma", False)):
        fields = []
        for index in picks:
            field = np.ma.masked_where(~grid["mask"], series[index][key])
            if anomaly:
                field = field - series[index]["ssh_mean"]
            fields.append(field)

        low = min(float(f.min()) for f in fields)
        high = max(float(f.max()) for f in fields)
        if anomaly:
            high = max(abs(low), abs(high))
            low = -high

        figure, axes = plt.subplots(1, 3, figsize=(13, 3.4), sharey=True)
        for ax, index, field in zip(axes, picks, fields):
            mesh = ax.pcolormesh(lon, lat, field, cmap=cmap, vmin=low,
                                 vmax=high, shading="auto")
            ax.set_title(series[index]["time"].strftime("%Y-%m-%d"), fontsize=9)
            ax.set_xlabel("longitude")
        axes[0].set_ylabel("latitude")
        figure.colorbar(mesh, ax=axes, label=f"{label}  [{unit}]")
        name = f"map-{key}.png"
        figure.savefig(out / name, dpi=120, bbox_inches="tight")
        plt.close(figure)
        figures.append((name, f"{label.capitalize()} at the first, middle and "
                              "last cycle."))
    return figures


# --- output ------------------------------------------------------------------

def save(figure, axes, path, caption):
    axes.grid(alpha=0.3)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)
    return (path.name, caption)


def page(experiment, state, times, figures):
    span = (f"{times[0]:%Y-%m-%d} to {times[-1]:%Y-%m-%d}, "
            f"{len(times)} cycles, {(times[-1] - times[0]).days} days")
    blocks = "\n".join(
        f'  <figure><img src="{name}" alt="{name}">'
        f'<figcaption>{caption}</figcaption></figure>'
        for name, caption in figures)
    return f"""<!doctype html>
<meta charset="utf-8">
<title>{experiment}</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 60rem;
         margin: 2rem auto; padding: 0 1rem; color: #222; }}
 h1 {{ font-size: 1.4rem; margin-bottom: 0.2rem; }}
 p.span {{ color: #666; margin-top: 0; }}
 figure {{ margin: 2rem 0; }}
 img {{ width: 100%; border: 1px solid #ddd; }}
 figcaption {{ color: #444; font-size: 0.9rem; margin-top: 0.4rem; }}
</style>
<h1>{experiment}</h1>
<p class="span">{state} states, {span}</p>
{blocks}
"""


if __name__ == "__main__":
    main()
