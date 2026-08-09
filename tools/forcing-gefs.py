#!/usr/bin/env python3
"""Build the GEFS half of the forcing archive: one `atm.nc` per member.

    tools/forcing-gefs.py 2021-06-30 2021-09-01 --lead 12 --members 20 \\
        --out $ACKBAR_STATIC_ROOT/forcing/gefs

GEFS forces every *experiment*, control and ensemble alike, while ERA5 forces the
truth. That is the fraternal twin: the experiments see a forecast of the weather
the truth actually had, wrong by the amount a real forecast is wrong.

## Lagged forecast, not lagged date

Every member here is valid at the same times as every other and as the truth.
What differs between members is which perturbed forecast they came from, and
`--lead` says how far ahead the forecast was looking. That is real forecast
uncertainty about the actual verifying date.

It is not the scheme the open boundary uses, and the difference is not cosmetic.
There, a member's field is imported from another *date*, because GLORYS has one
member and there is nothing else to draw from; the result is a plausible field
that is not an estimate of the uncertainty about this date. Here there is a
native ensemble and it would be perverse to synthesize one instead.

Three things follow, all of them simplifications:

- **No amplitude, no span, no mean preservation, no clamping.** Those exist to
  manufacture spread. This spread is measured.
- **Physical consistency is free.** A member's seven fields come out of one model
  run, so its cloud, radiation, precipitation and wind agree with each other. No
  rule about lagging every variable together, because nothing is lagged.
- **`--lead` is the one knob**, and it sets two things at once: how much spread
  the ensemble has, and how wrong the experiments' atmosphere is against the
  truth's. Both in physically calibrated units, and with the honest tradeoff a
  real forecast system has, since a longer lead buys spread and costs accuracy.

Choose it by measurement rather than taste: the smallest lead whose ensemble
spread spans the ERA5-minus-`gec00` difference over the same box and hours.

## How a member's series is stitched

A GEFS run is 16 days long and an experiment is longer, so a member is a series
at *constant lead*, taking a segment from each successive initialization. With
four initializations a day, one init contributes the six hours from `lead` to
`lead + 6`, and the next init takes over. Every record in a member's file is
therefore between `lead` and `lead + 6` hours old, which keeps its error
statistics stationary. soca-science did the same thing with a fixed six hour
lead; the joins are small jumps and they are the price of a series longer than a
forecast.

`--lead` must be a multiple of 6, because NCEP resets its averaging and
accumulation windows every six hours and this reads the reset boundary as the
segment boundary. A lead of 9 is expressible and would need a second, different
de-averaging path for no gain.

## De-averaging, which is the only place this can be quietly wrong

Radiation and precipitation arrive as windows since the last reset, so a segment
gets one window free and has to difference for the other:

    mean over [L+3, L+6]  =  2 * mean over [L, L+6]  -  mean over [L, L+3]
    accum over [L+3, L+6] =      accum over [L, L+6] -  accum over [L, L+3]

which is soca-science's `[2.0, -1.0]` and `[1.0, -1.0]`. Getting this wrong does
not crash anything: it makes precipitation too large by a factor that grows
through each reset period, and puts the shortwave's diurnal peak in the wrong
place. Every window read is checked against the range this expects.

## Volume

Whole files are fetched rather than byte ranges out of the `.idx`. Three files
per initialization per member at about 14 MB, so a sixty day twenty member
archive moves roughly 170 GB and keeps about 4 GB. Each file is deleted as soon
as it is read, so peak disk is one file per member being built. If that transfer
ever matters, the `.idx` files make a range fetch straightforward and would cut
it by about three.
"""

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

import eccodes
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ackbar.forcing import (  # noqa: E402
    FIELDS, assert_no_leap_day, clip, specific_humidity, write_atm)

BUCKET = "s3://noaa-gefs-pds"

#: Where a member's file for one initialization and lead lives. The 0.25 degree
#: `pgrb2s` subset rather than the half degree `pgrb2a`: it is smaller, finer,
#: and carries every field needed here.
LAYOUT = ("{bucket}/gefs.{day}/{hour}/atmos/pgrb2sp25/"
          "{member}.t{hour}z.pgrb2s.0p25.f{lead:03d}")

#: How GEFS names an instantaneous field, as (shortName, typeOfLevel, level).
#: `2d` and `sp` have no output name: they are read for the humidity conversion,
#: which is then the same arithmetic ERA5 gets, so a truth-minus-experiment
#: difference in humidity is a difference in the atmosphere rather than in two
#: conversions.
INSTANT = {
    "T2":  ("2t",  "heightAboveGround", 2),
    "U10": ("10u", "heightAboveGround", 10),
    "V10": ("10v", "heightAboveGround", 10),
    "_2d": ("2d",  "heightAboveGround", 2),
    "_sp": ("sp",  "surface", 0),
}

#: The same for the fields that arrive as a window since the last reset.
WINDOW = {
    "DSWRF": ("dswrf", "surface", 0),
    "DLWRF": ("dlwrf", "surface", 0),
    "PRATE": ("tp",    "surface", 0),
}

#: How often GEFS initializes, and how often it reports. Both are properties of
#: the archive from 2020-09-23 onward; earlier vintages report six hourly on a
#: coarser grid and this tool does not read them.
INIT_EVERY = 6
STEP = 3


def member_name(index):
    """`mem000` is the control, which in GEFS is `gec00` and not a perturbation.

    Members above it are the perturbed ones in order. That mapping is the whole
    of "which forecast did member 7 read", and it is derived from the index
    rather than recorded, so it is reproducible from the experiment config
    alone.
    """
    return "gec00" if index == 0 else f"gep{index:02d}"


def run(*args):
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"forcing-gefs: {' '.join(args)}\n{out.stderr.strip()}")
    return out.stdout


def fetch(init, member, lead, cache):
    """One GEFS file, downloaded if it is not already here."""
    url = LAYOUT.format(bucket=BUCKET, day=f"{init:%Y%m%d}", hour=f"{init:%H}",
                        member=member, lead=lead)
    local = cache / f"{member}.{init:%Y%m%d%H}.f{lead:03d}.grib2"
    if not local.exists():
        run("aws", "s3", "cp", "--no-sign-request", "--only-show-errors",
            url, str(local) + ".part")
        Path(str(local) + ".part").rename(local)
    return local


def read(path, wanted):
    """Pull *wanted* out of one GRIB file.

    *wanted* maps an output name to (shortName, typeOfLevel, level). Returns the
    fields clipped to the box, plus each one's window as (start, end) in hours
    from the initialization, so the caller can check it got the window it meant
    to ask for.
    """
    found, spans = {}, {}
    lons = lats = None
    index = {key: name for name, key in wanted.items()}
    with open(path, "rb") as stream:
        while True:
            handle = eccodes.codes_grib_new_from_file(stream)
            if handle is None:
                break
            try:
                key = (eccodes.codes_get(handle, "shortName"),
                       eccodes.codes_get(handle, "typeOfLevel"),
                       eccodes.codes_get(handle, "level"))
                if key not in index:
                    continue
                name = index[key]
                ni = eccodes.codes_get(handle, "Ni")
                nj = eccodes.codes_get(handle, "Nj")
                if lons is None:
                    first_lon = eccodes.codes_get(
                        handle, "longitudeOfFirstGridPointInDegrees")
                    first_lat = eccodes.codes_get(
                        handle, "latitudeOfFirstGridPointInDegrees")
                    step_lon = eccodes.codes_get(
                        handle, "iDirectionIncrementInDegrees")
                    step_lat = eccodes.codes_get(
                        handle, "jDirectionIncrementInDegrees")
                    lons = (first_lon + step_lon * np.arange(ni)) % 360.0
                    # GRIB scans north to south here, so the latitude increment
                    # is a decrement. Reading it as positive flips the field
                    # about the equator, which in this box is a plausible
                    # looking wind blowing the wrong way rather than an error.
                    lats = first_lat - step_lat * np.arange(nj)
                found[name] = eccodes.codes_get_values(handle).reshape(nj, ni)
                spans[name] = (eccodes.codes_get(handle, "startStep"),
                               eccodes.codes_get(handle, "endStep"))
            finally:
                eccodes.codes_release(handle)

    missing = set(wanted) - set(found)
    if missing:
        raise SystemExit(f"forcing-gefs: {path.name} has no {sorted(missing)}")

    out = {}
    for name, plane in found.items():
        x, y, cube = clip(lons, lats, plane[None, :, :])
        out[name] = cube[0]
    return out, spans, x, y


#: How far below zero a de-averaged field may go before it is a bug rather than
#: arithmetic on rounded numbers.
#:
#: Differencing two windows that were each packed to a few significant digits
#: leaves a residue, and doubling one of them doubles it, so a night-time
#: shortwave of exactly zero comes back as a few W m-2 either side of it.
#:
#: The tolerance has to have an absolute part, and finding that out is the
#: reason these numbers are measured rather than assumed. A purely relative
#: tolerance is scaled by the field's own magnitude, which at night is the
#: residue itself, so every dark segment fails: the first version of this check
#: rejected a shortwave of -8 W m-2 "against a range of 8". The relative part
#: still earns its place by daylight, where packing precision tracks magnitude.
#:
#: Both parts are far below what a *real* misreading costs. Taking a six hour
#: window for a three hour one puts hundreds of W m-2 in the wrong place, not
#: tens, so the failure this is meant to catch is nowhere near these bounds.
NEGATIVE_ABSOLUTE = {"DSWRF": 20.0, "DLWRF": 20.0, "PRATE": 2.0e-5}
NEGATIVE_RELATIVE = 0.02


def floor_at_zero(name, plane, scale, path):
    """Clamp a strictly positive field, refusing an excursion too big to be dust.

    *scale* is the magnitude of the window this was differenced out of, not of
    the result, because the result is what has been cancelled down to nothing.
    """
    worst = float(plane.min())
    if worst >= 0.0:
        return plane, 0.0
    allowed = max(NEGATIVE_ABSOLUTE[name], NEGATIVE_RELATIVE * scale)
    if -worst > allowed:
        raise SystemExit(
            f"forcing-gefs: de-averaging {name} from {path.name} gives "
            f"{worst:.4g}, past the {allowed:.4g} that packing residue explains "
            f"at a window magnitude of {scale:.4g}. The reset period is not "
            f"what this tool reads; see the de-averaging note in its docstring.")
    return np.maximum(plane, 0.0), worst


def expect(spans, name, window, path):
    """Fail unless a field's window is the one the de-averaging assumes."""
    if spans[name] != window:
        raise SystemExit(
            f"forcing-gefs: {path.name} has {name} over {spans[name]} where "
            f"{window} was expected. The archive's reset period is not what "
            f"this tool reads; see the de-averaging note in its docstring.")


def segment(init, member, lead, cache):
    """One initialization's six hours of forcing, as {name: [(hour, plane)]}.

    Hours are offsets from *init*. Instantaneous fields land on the hour, window
    fields at the midpoint of the window they describe.
    """
    at_lead = fetch(init, member, lead, cache)
    at_mid = fetch(init, member, lead + STEP, cache)
    at_end = fetch(init, member, lead + 2 * STEP, cache)

    first, _, x, y = read(at_lead, INSTANT)
    second, _, _, _ = read(at_mid, INSTANT)
    early, early_spans, _, _ = read(at_mid, WINDOW)
    whole, whole_spans, _, _ = read(at_end, WINDOW)

    for name in WINDOW:
        expect(early_spans, name, (lead, lead + STEP), at_mid)
        expect(whole_spans, name, (lead, lead + 2 * STEP), at_end)

    series = {}
    for name in ("T2", "U10", "V10"):
        series[name] = [(lead, first[name]), (lead + STEP, second[name])]
    series["Q2"] = [
        (lead, specific_humidity(first["_2d"], first["_sp"])),
        (lead + STEP, specific_humidity(second["_2d"], second["_sp"])),
    ]

    residue = {}
    for name in WINDOW:
        # The second half of the reset period, recovered from the window that
        # contains it and the window that precedes it. Every one of these is
        # non-negative by physics and slightly negative by arithmetic, so each
        # is floored and the worst excursion is carried out for reporting.
        if name == "PRATE":
            scale = float(np.abs(whole[name]).max()) / (STEP * 3600.0)
            late = (whole[name] - early[name]) / (STEP * 3600.0)
            first_half = early[name] / (STEP * 3600.0)
        else:
            scale = float(np.abs(whole[name]).max())
            late = 2.0 * whole[name] - early[name]
            first_half = early[name]
        first_half, low_a = floor_at_zero(name, first_half, scale, at_mid)
        late, low_b = floor_at_zero(name, late, scale, at_end)
        residue[name] = min(low_a, low_b)
        series[name] = [(lead + STEP / 2.0, first_half),
                        (lead + STEP * 1.5, late)]

    for path in (at_lead, at_mid, at_end):
        path.unlink(missing_ok=True)
    return series, x, y, residue


def build(index, start, end, lead, cache, out):
    """One member's whole `atm.nc`."""
    member = member_name(index)
    origin = start
    gathered = {name: ([], []) for name in FIELDS}
    grid = None

    worst = {name: 0.0 for name in WINDOW}
    init = start - datetime.timedelta(hours=lead)
    while init + datetime.timedelta(hours=lead) <= end:
        series, x, y, residue = segment(init, member, lead, cache)
        for name, low in residue.items():
            worst[name] = min(worst[name], low)
        if grid is None:
            grid = (x, y)
        base = (init - origin).total_seconds() / 3600.0
        for name, records in series.items():
            hours, planes = gathered[name]
            for offset, plane in records:
                hours.append(base + offset)
                planes.append(plane)
        init += datetime.timedelta(hours=INIT_EVERY)

    packed = {}
    for name, (hours, planes) in gathered.items():
        hours = np.array(hours, dtype="f8")
        cube = np.stack(planes).astype("f4")
        order = np.argsort(hours, kind="stable")
        packed[name] = (hours[order], cube[order])

    x, y = grid
    target = out / f"mem{index:03d}.nc"
    write_atm(target, x, y, origin, packed,
              source=f"GEFS {member} at {lead} h lead, noaa-gefs-pds")
    return target, packed, worst


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="first hour to cover, YYYY-MM-DD")
    ap.add_argument("end", help="last day to cover, YYYY-MM-DD, inclusive")
    ap.add_argument("--out", type=Path, required=True,
                    help="archive directory for this source")
    ap.add_argument("--lead", type=int, default=12,
                    help="forecast lead in hours, a multiple of 6. Every record "
                         "is between this and this plus six hours old")
    ap.add_argument("--members", type=int, default=20,
                    help="how many members, counting the control as the first. "
                         "GEFS has 31 from 2020-09-23, so above that a second "
                         "lead is needed and the members stop being "
                         "exchangeable")
    ap.add_argument("--cache", type=Path,
                    help="where downloads land before being read and deleted. "
                         "Default: <out>/.cache")
    args = ap.parse_args()

    if args.lead % (2 * STEP) != 0:
        raise SystemExit(
            f"forcing-gefs: --lead {args.lead} is not a multiple of "
            f"{2 * STEP}; see the de-averaging note in this tool's docstring")
    if not 1 <= args.members <= 31:
        raise SystemExit("forcing-gefs: --members outside 1 to 31")

    start = datetime.datetime.strptime(args.start, "%Y-%m-%d")
    end = (datetime.datetime.strptime(args.end, "%Y-%m-%d")
           + datetime.timedelta(hours=23))
    if end <= start:
        raise SystemExit("forcing-gefs: end is not after start")
    assert_no_leap_day(start, end)
    if start.hour % INIT_EVERY:
        raise SystemExit("forcing-gefs: start must land on an initialization")

    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.cache or (args.out / ".cache")
    cache.mkdir(parents=True, exist_ok=True)

    for index in range(args.members):
        target, packed, worst = build(index, start, end, args.lead, cache,
                                      args.out)
        hours, cube = packed["DSWRF"]
        residue = " ".join(f"{name} {low:.3g}" for name, low in worst.items()
                           if low < 0.0)
        print(f"forcing-gefs: {target}  {cube.shape[0]} records, "
              f"{hours[0]:+.1f} to {hours[-1]:+.1f} h"
              + (f", floored at zero from {residue}" if residue else ""),
              flush=True)

    if not any(cache.iterdir()):
        cache.rmdir()


if __name__ == "__main__":
    main()
