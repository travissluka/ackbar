#!/usr/bin/env python3
"""Sweep MOM6 parameters from one initial condition and record what each does.

    tools/spike/spike-sweep.py --out /data/ackbar/spike/params
    tools/spike/spike-sweep.py --out /data/ackbar/spike/params --only kd,mstar
    tools/spike/spike-sweep.py --list

Every member integrates the same restart set for the same length through
`tools/spike/spike-forecast.sh`, differing only in the parameters named below. The
control is a member too, perturbing nothing, so a difference between it and any
other member is the parameter and not the staging.

**The values are not a uniform fraction of the default.** Each range is the
range that parameter is genuinely uncertain over, which for a background
diffusivity is most of an order of magnitude and for a bottom drag coefficient
is a factor of two. A sweep built on `default * [0.5, 0.75, 1, 1.5, 2]` would
answer a question nobody asked: what matters is how much spread a *defensible*
value produces, because that is the spread an ensemble is entitled to claim.

Parameters that this configuration computes but does not use are absent on
purpose, and the absences are worth as much as the entries:

  - MEKE: `USE_MEKE` is true, but `MEKE_KHTH_FAC`, `MEKE_KHTR_FAC`,
    `MEKE_KHMEKE_FAC` and `MEKE_VISCOSITY_COEFF_KU` are all zero, so the eddy
    energy field feeds back into nothing. Perturbing it moves a diagnostic.
  - Shortwave penetration: `PEN_SW_SCALE` and `PEN_SW_FRAC` are both zero, so
    all shortwave is absorbed in the top cell. Optical parameters have nothing
    to scale, and turning penetration on is a change of configuration rather
    than a perturbation of one.
  - `KHTH` is zero, correct for an eddy-permitting domain, so the thickness
    diffusion coefficients have no term to multiply.
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

#: What every member starts from, and how far it runs.
#:
#: Five days rather than one. Members start from an identical state, so day one
#: measures how fast a perturbation is *introduced* and is dominated by the
#: parameter's direct local effect. What an ensemble needs is the spread that
#: survives a cycle once the perturbation has projected onto the flow, which is
#: what the later days show. The growth between day four and day five is the
#: number to compare against a filter's per-cycle spread loss.
DOMAIN = "gom_25km"
DAYS = 5

#: Each entry: group -> (what it controls, [(label, {KEY: VALUE}), ...]).
#:
#: The default is included as a labelled member rather than assumed equal to the
#: control. It should reproduce the control bit for bit, and a sweep in which it
#: does not has something wrong with it that no amount of interpreting the other
#: members will reveal.
MATRIX = {
    # --- lateral momentum: eddies, the Loop Current, sea level -----------
    "smag_lap": ("Laplacian viscosity (Smagorinsky)", [
        ("0.05", {"SMAG_LAP_CONST": "0.05"}),
        ("0.10", {"SMAG_LAP_CONST": "0.10"}),
        ("0.15", {"SMAG_LAP_CONST": "0.15"}),
        ("0.22", {"SMAG_LAP_CONST": "0.22"}),
        ("0.35", {"SMAG_LAP_CONST": "0.35"}),
    ]),
    "smag_bi": ("biharmonic viscosity (Smagorinsky)", [
        ("0.02", {"SMAG_BI_CONST": "0.02"}),
        ("0.04", {"SMAG_BI_CONST": "0.04"}),
        ("0.06", {"SMAG_BI_CONST": "0.06"}),
        ("0.09", {"SMAG_BI_CONST": "0.09"}),
        ("0.14", {"SMAG_BI_CONST": "0.14"}),
    ]),
    # The constant viscosity floor. Large enough here that it may swamp the
    # Smagorinsky terms above, which is itself worth measuring: if this moves
    # the solution and they do not, the floor is what sets eddy vigour.
    "kh_sin_lat": ("constant lateral viscosity floor", [
        ("0", {"KH_SIN_LAT": "0.0"}),
        ("500", {"KH_SIN_LAT": "500.0"}),
        ("1000", {"KH_SIN_LAT": "1000.0"}),
        ("2000", {"KH_SIN_LAT": "2000.0"}),
        ("4000", {"KH_SIN_LAT": "4000.0"}),
    ]),
    "kv": ("background vertical viscosity", [
        ("1e-5", {"KV": "1.0E-05"}),
        ("3e-5", {"KV": "3.0E-05"}),
        ("1e-4", {"KV": "1.0E-04"}),
        ("3e-4", {"KV": "3.0E-04"}),
        ("1e-3", {"KV": "1.0E-03"}),
    ]),

    # --- bottom drag: barotropic dissipation, the shelf ------------------
    "cdrag": ("bottom drag coefficient", [
        ("0.0015", {"CDRAG": "0.0015"}),
        ("0.0022", {"CDRAG": "0.0022"}),
        ("0.003", {"CDRAG": "0.003"}),
        ("0.0042", {"CDRAG": "0.0042"}),
        ("0.006", {"CDRAG": "0.006"}),
    ]),
    "hbbl": ("bottom boundary layer thickness", [
        ("2", {"HBBL": "2.0"}),
        ("5", {"HBBL": "5.0"}),
        ("10", {"HBBL": "10.0"}),
        ("20", {"HBBL": "20.0"}),
        ("40", {"HBBL": "40.0"}),
    ]),
    # Drag felt under weak flow, which is unresolved tidal and wave motion
    # standing in for itself. Zero here, and zero is a choice rather than a
    # measurement.
    "drag_bg_vel": ("background velocity for bottom drag", [
        ("0.00", {"DRAG_BG_VEL": "0.0"}),
        ("0.02", {"DRAG_BG_VEL": "0.02"}),
        ("0.05", {"DRAG_BG_VEL": "0.05"}),
        ("0.10", {"DRAG_BG_VEL": "0.10"}),
        ("0.20", {"DRAG_BG_VEL": "0.20"}),
    ]),

    # --- interior diapycnal mixing: thermocline, deep temperature --------
    "kd": ("background diapycnal diffusivity", [
        ("3e-6", {"KD": "3.0E-06"}),
        ("7e-6", {"KD": "7.0E-06"}),
        ("1.5e-5", {"KD": "1.5E-05"}),
        ("3e-5", {"KD": "3.0E-05"}),
        ("7e-5", {"KD": "7.0E-05"}),
    ]),
    "rino_crit": ("critical Richardson number, shear mixing", [
        ("0.15", {"RINO_CRIT": "0.15"}),
        ("0.20", {"RINO_CRIT": "0.20"}),
        ("0.25", {"RINO_CRIT": "0.25"}),
        ("0.35", {"RINO_CRIT": "0.35"}),
        ("0.50", {"RINO_CRIT": "0.50"}),
    ]),

    # --- the mixed layer: SST, SSS, MLD ----------------------------------
    "mstar": ("ePBL wind energy conversion efficiency", [
        ("0.6", {"MSTAR": "0.6"}),
        ("0.9", {"MSTAR": "0.9"}),
        ("1.2", {"MSTAR": "1.2"}),
        ("1.7", {"MSTAR": "1.7"}),
        ("2.4", {"MSTAR": "2.4"}),
    ]),
    "nstar": ("ePBL convective efficiency", [
        ("0.10", {"NSTAR": "0.10"}),
        ("0.15", {"NSTAR": "0.15"}),
        ("0.20", {"NSTAR": "0.20"}),
        ("0.30", {"NSTAR": "0.30"}),
        ("0.40", {"NSTAR": "0.40"}),
    ]),
    "tke_decay": ("ePBL turbulent decay scale", [
        ("1.0", {"TKE_DECAY": "1.0"}),
        ("1.7", {"TKE_DECAY": "1.7"}),
        ("2.5", {"TKE_DECAY": "2.5"}),
        ("3.5", {"TKE_DECAY": "3.5"}),
        ("5.0", {"TKE_DECAY": "5.0"}),
    ]),
    "mix_len": ("ePBL mixing length exponent", [
        ("1.0", {"MIX_LEN_EXPONENT": "1.0"}),
        ("1.5", {"MIX_LEN_EXPONENT": "1.5"}),
        ("2.0", {"MIX_LEN_EXPONENT": "2.0"}),
        ("2.5", {"MIX_LEN_EXPONENT": "2.5"}),
        ("3.0", {"MIX_LEN_EXPONENT": "3.0"}),
    ]),

    # --- restratification and tracer stirring ----------------------------
    # The least constrained number in the configuration.
    "mle_front": ("mixed layer eddy front length", [
        ("50", {"MLE_FRONT_LENGTH": "50.0"}),
        ("100", {"MLE_FRONT_LENGTH": "100.0"}),
        ("200", {"MLE_FRONT_LENGTH": "200.0"}),
        ("400", {"MLE_FRONT_LENGTH": "400.0"}),
        ("800", {"MLE_FRONT_LENGTH": "800.0"}),
    ]),
    "khtr": ("along-isopycnal tracer diffusivity", [
        ("0", {"KHTR": "0.0"}),
        ("25", {"KHTR": "25.0"}),
        ("50", {"KHTR": "50.0"}),
        ("100", {"KHTR": "100.0"}),
        ("200", {"KHTR": "200.0"}),
    ]),

    # --- the one genuinely stochastic scheme that needs no external library
    #
    # `MOM_stoch_eos` draws a new random field every step from `MOM_random`, so
    # unlike everything above it perturbs the model rather than replacing it
    # with a different one. Two sweeps: the amplitude, and then five members at
    # one amplitude differing only by seed, which is the only member set here
    # that is an ensemble in the sense a filter means.
    "stanley": ("stochastic EOS amplitude (Stanley SGS variance)", [
        ("0.10", {"STOCH_EOS": "True", "STANLEY_COEFF": "0.10", "USE_STANLEY_PGF": "True"}),
        ("0.24", {"STOCH_EOS": "True", "STANLEY_COEFF": "0.24", "USE_STANLEY_PGF": "True"}),
        ("0.50", {"STOCH_EOS": "True", "STANLEY_COEFF": "0.50", "USE_STANLEY_PGF": "True"}),
        ("0.75", {"STOCH_EOS": "True", "STANLEY_COEFF": "0.75", "USE_STANLEY_PGF": "True"}),
        ("1.00", {"STOCH_EOS": "True", "STANLEY_COEFF": "1.00", "USE_STANLEY_PGF": "True"}),
    ]),
    "stanley_seed": ("stochastic EOS, one amplitude, five seeds", [
        (f"seed{n}", {"STOCH_EOS": "True", "STANLEY_COEFF": "0.24",
                      "USE_STANLEY_PGF": "True", "SEED_STOCH_EOS": str(n)})
        for n in (1, 2, 3, 4, 5)
    ]),
}


def run(out, group, label, overrides, ic, ntasks, exe, days):
    """One member. Returns the manifest entry, or None if it was already there."""
    target = out / group / label
    # Existence of the output, not a marker file, because the output is what the
    # analysis reads. A sweep interrupted halfway resumes by re-running.
    if list(target.glob("*ocn_daily.nc")):
        return None
    env = dict(os.environ, NTASKS=str(ntasks))
    if exe:
        env["SPIKE_EXE"] = str(exe)
    began = time.time()
    done = subprocess.run(
        ["bash", str(FORECAST), DOMAIN, str(ic), str(days), str(target)]
        + [f"{k}={v}" for k, v in overrides.items()],
        env=env, capture_output=True, text=True,
    )
    entry = {"group": group, "label": label, "overrides": overrides,
             "seconds": round(time.time() - began, 1), "ok": done.returncode == 0}
    if done.returncode != 0:
        entry["stderr"] = done.stderr[-2000:]
        print(f"  {group}/{label} FAILED", flush=True)
        print(done.stderr[-1500:], file=sys.stderr, flush=True)
    else:
        print(f"  {group}/{label} {entry['seconds']:.0f}s", flush=True)
    return entry


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, help="where members are written")
    ap.add_argument("--ic", type=Path,
                    default=Path(os.environ.get("ACKBAR_STATIC_ROOT", "/data/ackbar/static"))
                    / "ic" / DOMAIN / "osse-control-25km" / "20150712T00")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--ntasks", type=int, default=8)
    ap.add_argument("--exe", type=Path, default=os.environ.get("SPIKE_EXE"))
    ap.add_argument("--only", default="", help="comma separated group names")
    ap.add_argument("--list", action="store_true", help="print the matrix and stop")
    args = ap.parse_args()

    groups = [g.strip() for g in args.only.split(",") if g.strip()] or list(MATRIX)
    unknown = [g for g in groups if g not in MATRIX]
    if unknown:
        sys.exit(f"spike-sweep: no such group: {', '.join(unknown)}. "
                 f"Known: {', '.join(MATRIX)}")

    if args.list:
        total = 0
        for group in groups:
            what, members = MATRIX[group]
            print(f"{group:14s} {what}")
            for label, overrides in members:
                print(f"    {label:10s} "
                      + " ".join(f"{k}={v}" for k, v in overrides.items()))
            total += len(members)
        print(f"\n{len(groups)} groups, {total} members, plus one control")
        return

    if not args.out:
        sys.exit("spike-sweep: --out is required unless --list")
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "manifest.json"
    entries = json.loads(manifest.read_text()) if manifest.exists() else []

    def record(entry):
        if entry is None:
            return
        entries.append(entry)
        # Rewritten after every member. A sweep that dies at member 60 still
        # documents the 59 that ran.
        manifest.write_text(json.dumps(entries, indent=2) + "\n")

    print(f"control ({args.days} days from {args.ic.name})", flush=True)
    record(run(args.out, "control", "control", {}, args.ic, args.ntasks, args.exe, args.days))

    for group in groups:
        what, members = MATRIX[group]
        print(f"{group}: {what}", flush=True)
        for label, overrides in members:
            record(run(args.out, group, label, overrides,
                       args.ic, args.ntasks, args.exe, args.days))

    bad = [f"{e['group']}/{e['label']}" for e in entries if not e["ok"]]
    print(f"\n{len(entries)} members run, {len(bad)} failed"
          + (": " + ", ".join(bad) if bad else ""))


if __name__ == "__main__":
    main()
