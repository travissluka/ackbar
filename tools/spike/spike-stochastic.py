#!/usr/bin/env python3
"""Sweep the NOAA-PSL ocean stochastic physics schemes, as seed ensembles.

    tools/spike/spike-stochastic.py --out /data/ackbar/spike/stochastic
    tools/spike/spike-stochastic.py --list

Runs against the checkout's own `coupler_main`, which carries the pattern
generator: `build-model.sh` compiles `pkg/stochastic_physics` into it, and with
no scheme switched on the executable is bit for bit a stock build.

**Every group here is five seeds of one configuration, not five values of one
parameter.** That is the difference between this and `tools/spike/spike-sweep.py` and
it is the whole point: a stochastic scheme run five times with different random
seeds is an ensemble in the sense a filter means, five draws from one
distribution, so the standard deviation across members is a forecast spread and
not a sensitivity. Comparing a number from here against a number from the
parameter sweep is comparing a spread against a sensitivity, and only the
former is what an ensemble filter actually gets.

Amplitude, length scale and decorrelation time are covered by running several
such ensembles rather than by varying them within one, for the same reason.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORECAST = ROOT / "tools" / "spike" / "spike-forecast.sh"

DOMAIN = "gom_25km"
DAYS = 5
SEEDS = (101, 202, 303, 404, 505)


def nam_stochy(entries):
    """The `&nam_stochy` group, as text to append to `input.nml`.

    `ntrunc`, `lon_s` and `lat_s` are deliberately absent. Left unset the
    generator derives the spectral truncation and its Gaussian grid from the
    smallest length scale asked for, which is the right answer and one fewer
    number to get wrong.
    """
    body = "\n".join(f"  {key} = {value}" for key, value in entries.items())
    return f"&nam_stochy\n{body}\n/\n"


def scheme_sppt(amp, lscale, tau):
    return ({"DO_SPPT": "True"},
            {"ocnsppt": amp, "ocnsppt_lscale": lscale, "ocnsppt_tau": tau})


def scheme_epbl(amp, lscale, tau):
    return ({"PERT_EPBL": "True"},
            {"epbl": amp, "epbl_lscale": lscale, "epbl_tau": tau})


def scheme_skeb(amp, lscale, tau):
    # Without a dissipation source the backscatter amplitude is identically
    # zero and the scheme runs and does nothing. GM is not a source here
    # (`KHTH` is zero on an eddy permitting domain), so the friction rate is
    # the only one available, and it has to be asked for.
    return ({"DO_SKEB": "True", "SKEB_USE_FRICT": "True", "SKEB_FRICT_COEF": "1.0"},
            {"ocnskeb": amp, "ocnskeb_lscale": lscale, "ocnskeb_tau": tau})


def combine(*parts):
    override, namelist = {}, {}
    for one, two in parts:
        override.update(one)
        namelist.update(two)
    return override, namelist


#: group -> (what it is, MOM6 parameters, nam_stochy entries).
#:
#: Length scales in metres and decorrelation times in seconds, as the generator
#: wants them. 500 km and six hours are the middle of the range the UFS runs
#: these at; the variants around them are there to find out whether the ocean
#: cares about the scale of the noise or only its size.
MATRIX = {
    "sppt_a02": ("oSPPT, amplitude 0.2", *scheme_sppt(0.2, 500e3, 21600)),
    "sppt_a04": ("oSPPT, amplitude 0.4", *scheme_sppt(0.4, 500e3, 21600)),
    "sppt_a08": ("oSPPT, amplitude 0.8", *scheme_sppt(0.8, 500e3, 21600)),
    "sppt_short": ("oSPPT, 150 km", *scheme_sppt(0.4, 150e3, 21600)),
    "sppt_long": ("oSPPT, 1500 km", *scheme_sppt(0.4, 1500e3, 21600)),
    "sppt_slow": ("oSPPT, 24 h decorrelation", *scheme_sppt(0.4, 500e3, 86400)),

    "epbl_a02": ("ePBL perturbation, amplitude 0.2", *scheme_epbl(0.2, 500e3, 21600)),
    "epbl_a05": ("ePBL perturbation, amplitude 0.5", *scheme_epbl(0.5, 500e3, 21600)),
    "epbl_a10": ("ePBL perturbation, amplitude 1.0", *scheme_epbl(1.0, 500e3, 21600)),

    "both": ("oSPPT 0.4 + ePBL 0.5",
             *combine(scheme_sppt(0.4, 500e3, 21600),
                      scheme_epbl(0.5, 500e3, 21600))),

    # oSKEB is defined and not in DEFAULT. It crashes this domain at every
    # amplitude tried, down to 0.05 with SKEB_FRICT_COEF=0.01 and an eight cell
    # taper, always in `MOM_neutral_diffusion::interpolate_for_nondim_position`.
    # That the failure does not soften with amplitude says it is not a CFL
    # violation being provoked by too large an increment: something about the
    # velocity increment on this regional configuration corrupts the density
    # column outright. Left runnable with `--only` for whoever picks that up.
    "skeb_a02": ("oSKEB, amplitude 0.2", *scheme_skeb(0.2, 500e3, 21600)),
    "skeb_a05": ("oSKEB, amplitude 0.5", *scheme_skeb(0.5, 500e3, 21600)),
    "skeb_a10": ("oSKEB, amplitude 1.0", *scheme_skeb(1.0, 500e3, 21600)),
}

#: What runs when `--only` is not given.
DEFAULT = tuple(g for g in MATRIX if not g.startswith("skeb"))

#: Which seed each scheme reads, so a member varies every pattern it runs.
SEED_KEYS = {"DO_SPPT": "iseed_ocnsppt", "PERT_EPBL": "iseed_epbl",
             "DO_SKEB": "iseed_ocnskeb"}


def run(out, group, seed, override, namelist, ic, ntasks, exe, days, scratch):
    target = out / group / f"seed{seed}"
    if list(target.glob("*ocn_daily.nc")):
        return None

    entries = dict(namelist)
    for flag, key in SEED_KEYS.items():
        if flag in override:
            entries[key] = seed
    written = scratch / f"nam_stochy.{group}.{seed}"
    written.write_text(nam_stochy(entries))

    env = dict(os.environ, NTASKS=str(ntasks), SPIKE_NAMELIST=str(written))
    if exe:
        env["SPIKE_EXE"] = str(exe)
    began = time.time()
    done = subprocess.run(
        ["bash", str(FORECAST), DOMAIN, str(ic), str(days), str(target)]
        + [f"{k}={v}" for k, v in override.items()],
        env=env, capture_output=True, text=True,
    )
    entry = {"group": group, "seed": seed, "override": override,
             "nam_stochy": entries, "seconds": round(time.time() - began, 1),
             "ok": done.returncode == 0}
    if done.returncode != 0:
        entry["stderr"] = done.stderr[-2000:]
        print(f"  {group}/seed{seed} FAILED", flush=True)
        print(done.stderr[-1200:], file=sys.stderr, flush=True)
    else:
        print(f"  {group}/seed{seed} {entry['seconds']:.0f}s", flush=True)
    return entry


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--exe", type=Path,
                    default=os.environ.get("SPIKE_EXE",
                                           str(ROOT / "pkg/mom6sis2/ice_ocean_SIS2/build/coupler_main")))
    ap.add_argument("--ic", type=Path,
                    default=Path(os.environ.get("ACKBAR_STATIC_ROOT", "/data/ackbar/static"))
                    / "ic" / DOMAIN / "osse-control-25km" / "20150712T00")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--ntasks", type=int, default=8)
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    groups = [g.strip() for g in args.only.split(",") if g.strip()] or list(DEFAULT)
    unknown = [g for g in groups if g not in MATRIX]
    if unknown:
        sys.exit(f"spike-stochastic: no such group: {', '.join(unknown)}")

    if args.list:
        for group in groups:
            what, override, namelist = MATRIX[group]
            print(f"{group:12s} {what}")
            print(f"    MOM6      " + " ".join(f"{k}={v}" for k, v in override.items()))
            print(f"    nam_stochy " + " ".join(f"{k}={v}" for k, v in namelist.items()))
        print(f"\n{len(groups)} ensembles of {len(SEEDS)}, {len(groups) * len(SEEDS)} members")
        return

    if not args.out:
        sys.exit("spike-stochastic: --out is required unless --list")
    if not Path(args.exe).exists():
        sys.exit(f"spike-stochastic: {args.exe} does not exist. The stock "
                 f"coupler_main cannot run these; build it with "
                 f"./build-model.sh")

    args.out.mkdir(parents=True, exist_ok=True)
    scratch = args.out / "namelists"
    scratch.mkdir(exist_ok=True)
    manifest = args.out / "manifest.json"
    entries = json.loads(manifest.read_text()) if manifest.exists() else []

    for group in groups:
        what, override, namelist = MATRIX[group]
        print(f"{group}: {what}", flush=True)
        for seed in SEEDS:
            entry = run(args.out, group, seed, override, namelist,
                        args.ic, args.ntasks, args.exe, args.days, scratch)
            if entry is None:
                continue
            entries.append(entry)
            manifest.write_text(json.dumps(entries, indent=2) + "\n")

    bad = [f"{e['group']}/seed{e['seed']}" for e in entries if not e["ok"]]
    print(f"\n{len(entries)} members run, {len(bad)} failed"
          + (": " + ", ".join(bad) if bad else ""))


if __name__ == "__main__":
    main()
