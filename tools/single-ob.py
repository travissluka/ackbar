#!/usr/bin/env python3
"""What one observation does to the LETKF, and how far it reaches.

    tools/single-ob.py <exp.yaml> <run-dir> [--lat 25.12] [--lon -90.0]
                       [--case sst adt argo]

Runs the real filter path on an observation set of exactly one row per case:
`ackbar.soca.letkf_config` over the layer stack `<exp.yaml>` resolves to, the
same ensemble mean, the same hofx pass over every member, the same split
between the observer's `$localization` entries and the geometry's iterator
dimension. Nothing here describes the localization a second time, which is the
point: a single observation test written against its own copy of the config
would pass while the experiments ran a different operator.

What it answers is the question a localization block cannot be read for. The
increment at the observation's own column shows the vertical reach the taper
produced, and the increment away from it shows the horizontal reach, both in
metres and kilometres rather than in the levels and Rossby multiples the config
counts in. A family with no vertical entry shows the ensemble's own response
instead, which is what an unlocalized column looks like.

It writes, under `<run-dir>`: `letkf/<case>/` holding that case's increment,
analysis and spread files plus the observer's output file, `hofx/` holding the
per-member departures, and one `<label>.yaml` per application launched. Read
the increments against `soca_gridspec.nc` and the mean's own `h` to turn level
indices into depths; `docs/analysis.md` carries what a passing result looks
like.

## Trap one: a single ADT observation assimilates nothing

`ufo::ObsADT` subtracts the mean of `H(x) - obs` over the whole observation
space before forming a departure, so a genuinely single altimeter observation
has an identically zero departure and produces an increment of exactly zero
everywhere. That reads as "no response" and means "nothing was assimilated".
The altimeter case therefore carries `FILLERS` filler rows with zero departure
of their own, placed beyond `FILLER_MIN_KM` so none of them is inside the
horizontal localization radius of the column under test and the local solve
there still sees exactly one observation. The target's realized departure is
then `(1 - 1/N)` of the one asked for and each filler takes `-1/N` of it, which
is why the printed `ombg` is not the `delta` in `CASES`.

`FILLER_MIN_KM` has to clear the *Gaspari-Cohn* radius, not the Rossby multiple:
`soca::ObsLocRossby` multiplies `mult * rossby_radius` by `2/sqrt(0.3)` = 3.65
before tapering, so `rossby mult: 1.5` cuts off at 5.5 Rossby radii, not 1.5.
That is a field rather than a number, because the radius is read at each
analysis point: on gom_25km it runs from about 150 km to about 340 km, which is
what `FILLER_MIN_KM` is set to clear. See `docs/analysis.md`.

## Trap two: the output rows are not in input order

The solver writes its observation space back in rank-major order, so row 0 of
the file in `letkf/<case>/` is not row 0 of the file this wrote. Find the
observation by its `MetaData/latitude` and `MetaData/longitude`, never by its
index.
"""

import argparse
import copy
import importlib.util
from pathlib import Path

import numpy as np
import netCDF4
import yaml

from ackbar import ensemble_hofx, observations, soca
from ackbar.config.jobtime import cycle_time, window_bounds
from ackbar.config.layers import resolve_layers, merge_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.site import load_site

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
CYCLE = 1

#: The three families the vertical half of the localization treats differently:
#: a surface platform with a vertical entry, an altimeter with none because it
#: is depth integrated, and a cast with none by decision. `delta` is the
#: departure asked for, in the observation's own units, and `depth` is metres
#: for a subsurface platform and `None` for a surface one.
CASES = {
    "sst": dict(observer="sst_metopb", variable="seaSurfaceTemperature",
                delta=2.0, error=0.5, depth=None),
    "adt": dict(observer="adt_j2", variable="absoluteDynamicTopography",
                delta=0.3, error=0.05, depth=None),
    "argo": dict(observer="argo_t", variable="waterPotentialTemperature",
                 delta=2.0, error=0.2, depth=400.0),
}

FILLERS = 99
FILLER_MIN_KM = 400.0


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archive = _load("obsarchive_osse", TOOLS / "obs-archive-osse.py")


def config(experiment):
    schema = load_schema(None)
    layers = resolve_layers(str(experiment), REPO / "config" / "layers")
    cfg = merge_layers(layers, merge_keys(schema))
    return resolve(cfg, load_site()), load_site()


def _fillers(cfg, lat, lon):
    """Deep open-water points at least `FILLER_MIN_KM` from the column tested.

    Only the altimeter needs them, and only because of the operator's global
    offset removal; see trap one at the top of this file.
    """
    grid_file = Path(cfg["domain"]["static"]) / soca.GRIDSPEC
    with netCDF4.Dataset(grid_file) as grid:
        glon = grid["lon"][0].filled(np.nan)
        glat = grid["lat"][0].filled(np.nan)
        mask = grid["mask2d"][0].filled(0)
        coast = grid["distance_from_coast"][0].filled(0)
    km = 111.0 * np.hypot(glat - lat, (glon - lon) * np.cos(np.radians(lat)))
    good = (mask > 0) & (coast > 100e3) & (km > FILLER_MIN_KM)
    j, i = np.nonzero(good)
    if j.size < FILLERS:
        raise SystemExit(
            f"{grid_file} has only {j.size} deep open-water point(s) more than "
            f"{FILLER_MIN_KM} km from {lat},{lon}, and {FILLERS} are needed")
    order = np.argsort(-km[j, i])[:FILLERS]
    return glon[j, i][order], glat[j, i][order]


def positions(cfg, case, lat, lon):
    spec = CASES[case]
    lons = np.array([lon], dtype="f4")
    lats = np.array([lat], dtype="f4")
    if case == "adt":
        more_lon, more_lat = _fillers(cfg, lat, lon)
        lons = np.concatenate([lons, more_lon]).astype("f4")
        lats = np.concatenate([lats, more_lat]).astype("f4")
    depth = None
    if spec["depth"] is not None:
        depth = np.full(lons.shape, spec["depth"], dtype="f4")
    return lons, lats, depth


def write_obs(cfg, case, values, obs, when, lat, lon):
    spec = CASES[case]
    obs.mkdir(parents=True, exist_ok=True)
    path = obs / f"{spec['observer']}.nc4"
    lons, lats, depth = positions(cfg, case, lat, lon)
    values = np.broadcast_to(np.asarray(values, dtype="f4"), lons.shape)
    archive.write_obs(
        str(path),
        {"variable": spec["variable"], "error": spec["error"]},
        lons, lats, values.astype("f4"), when, np.zeros(lons.shape, dtype="i8"),
        depth=depth,
        sigma=np.full(lons.shape, spec["error"], dtype="f4"))
    return path


def records(cfg, cases, obs):
    """One observer record per case, reading this test's own single-ob files."""
    by_name = {r["name"]: r for r in observations.observers(cfg, CYCLE)}
    out = []
    for case in cases:
        spec = CASES[case]
        record = copy.deepcopy(by_name[spec["observer"]])
        body = record["config"]
        body["obs space"]["obsdatain"] = {
            "engine": {"type": "H5File",
                       "obsfile": str(obs / f"{spec['observer']}.nc4")}}
        record["config"] = body
        out.append(record)
    return out


def launch(cfg, site, run, document, label, task="da"):
    run.mkdir(parents=True, exist_ok=True)
    soca.stage(cfg, run, CYCLE)
    (run / "out").mkdir(exist_ok=True)
    (run / f"{label}.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    app = {"ensmean": "ensmean", "letkf": "letkf"}.get(label.split(".")[0],
                                                       "hofx_ens")
    print(f"--- {label} ({soca.APPLICATIONS[app]})", flush=True)
    soca.launch(cfg, site, run, task, soca.APPLICATIONS[app],
                f"{label}.yaml", f"{label}.log")


def hofx(cfg, site, run, recs, state, tag, outdir, bounds):
    begin, end = bounds
    local = copy.deepcopy(recs)
    outdir.mkdir(parents=True, exist_ok=True)
    staged = soca._redirect_output(local, outdir)
    written = {r["name"]: p for r, (p, _) in zip(local, staged)}
    launch(cfg, site, run,
           soca.member_hofx_config(cfg, CYCLE, local,
                                   initial=Path(state),
                                   states={end: Path(state)},
                                   tstep=end - begin,
                                   templates=REPO / "config" / "soca"),
           f"hofx.{tag}", task="hofx")
    for name, path in written.items():
        if not path.exists():
            raise SystemExit(f"{tag}: observer {name} wrote nothing")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("experiment", help="an experiment yaml, cycle 1 of it")
    parser.add_argument("run", help="the run directory to build and launch in")
    parser.add_argument("--lat", type=float, default=25.12)
    parser.add_argument("--lon", type=float, default=-90.0)
    parser.add_argument("--obs", default=None,
                        help="where to write the observation files "
                             "(default <run>/obs)")
    parser.add_argument("--case", nargs="+", choices=sorted(CASES),
                        default=sorted(CASES))
    args = parser.parse_args()

    run = Path(args.run)
    obs = Path(args.obs) if args.obs else run / "obs"
    lat, lon = args.lat, args.lon

    cfg, site = config(args.experiment)
    when = cycle_time(cfg, CYCLE)
    bounds = window_bounds(cfg, CYCLE)
    members = list(range(1, int(cfg["ensemble"]["size"]) + 1))
    ens = Path(cfg["ensemble"]["initial_condition"])
    restart = soca._restart(cfg["model"])
    states = {m: ens / f"mem{m:03d}" / restart for m in members}

    # 1. the ensemble mean, which is what quality control is evaluated against
    mean_out = run / "out" / soca.prior_mean_name(when)
    if not mean_out.exists():
        launch(cfg, site, run,
               soca.ensemble_mean_config(
                   cfg, CYCLE, when=when, templates=REPO / "config" / "soca",
                   states=soca.member_states(
                       lambda m: states[m], members,
                       date=soca.format_instant(when),
                       variables=cfg["solver"]["background variables"])),
               "ensmean", task="da")
    if not mean_out.exists():
        raise SystemExit(f"no ensemble mean at {mean_out}")

    # 2. a placeholder value, so the mean's H(x) can be read and the departure
    #    set exactly rather than guessed. An offset common to every row cancels
    #    in the altimeter's own offset removal, so the placeholder need not be
    #    the raw geoval.
    for case in args.case:
        write_obs(cfg, case, 0.0, obs, when, lat, lon)
    recs = records(cfg, args.case, obs)
    probe = hofx(cfg, site, run, recs, mean_out, "probe",
                 run / "hofx" / "probe", bounds)
    background = {}
    for case in args.case:
        spec = CASES[case]
        with netCDF4.Dataset(probe[spec["observer"]]) as data:
            background[case] = data["hofx"][spec["variable"]][:].filled(np.nan)
        print(f"{case}: H(mean(Xb))[0] = {background[case][0]:.4f} "
              f"over {background[case].size} row(s)")

    # 3. the real observation: the mean's own H(x) plus the departure, and the
    #    departure is on the first row only
    for case in args.case:
        delta = np.zeros(background[case].shape)
        delta[0] = CASES[case]["delta"]
        write_obs(cfg, case, background[case] + delta, obs, when, lat, lon)
    recs = records(cfg, args.case, obs)

    # 4. the reference run and every member
    reference = hofx(cfg, site, run, recs, mean_out, "mean",
                     run / "hofx" / "mean", bounds)
    member_hofx = [hofx(cfg, site, run, recs, states[m], f"mem{m:03d}",
                        run / "hofx" / f"mem{m:03d}", bounds)
                   for m in members]

    merged = {}
    for name, path in reference.items():
        merged[name] = ensemble_hofx.merge(
            path, [w[name] for w in member_hofx], run / "departures" / path.name)
    print("merged:", {k: str(v) for k, v in merged.items()}, flush=True)

    # 5. one LETKF per case, each with exactly one observer
    for case in args.case:
        spec = CASES[case]
        one = records(cfg, [case], obs)
        out = run / "letkf" / case
        out.mkdir(parents=True, exist_ok=True)
        soca._redirect_output(one, out)
        document = soca.letkf_config(
            cfg, CYCLE, one, backgrounds=ens, members=members,
            departures={spec["observer"]: merged[spec["observer"]]},
            templates=REPO / "config" / "soca")
        # Products land in `out/`, which is shared between the cases, so each
        # case gets its own directory under it.
        for key in ("output", "output increment", "output variance prior",
                    "output variance posterior"):
            document[key]["datadir"] = f"letkf/{case}"
            document[key]["exp"] = f"{case}"
        launch(cfg, site, run, document, f"letkf.{case}", task="da")
        print(f"{case}: wrote {sorted(p.name for p in out.glob('*.nc'))}",
              flush=True)


if __name__ == "__main__":
    main()
