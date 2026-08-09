#!/usr/bin/env python3
"""How many independent directions a perturbation ensemble actually spans.

    tools/spike-rank.py <dir-of-mem###.nc>
    tools/spike-rank.py /data/ackbar/spike/obc-lag21/obc \
        --merge '(\\w+)_segment_\\w+'
    tools/spike-rank.py $ACKBAR_STATIC_ROOT/forcing/gefs-lagged/2021070100 \
        --vars T2,Q2,U10,V10,DSWRF

Reads `mem000.nc` (the unperturbed member) and every `mem001.nc` onwards beside
it, and for each field reports the singular value spectrum of the member anomaly
matrix, the participation ratio, and the range the values reached.

---------------------------------------------------------------------------
Why the rank and not just the size

An ensemble's spread says how much uncertainty it claims. Its rank says how many
distinct things it can claim uncertainty *about*, and an ensemble filter is
limited by the second: the analysis increment lives in the span of the member
anomalies, so a direction the ensemble does not carry is a direction no
observation can correct, however large the spread is elsewhere.

That matters for any perturbation built by lagging one source. Members closer
together than the field's decorrelation time are near duplicates, so the
independent dimension is roughly `2 * span / decorrelation` whatever the member
count, and adding members past that buys forecast cost and no new structure.

It is also strongly per field rather than per ensemble, which is the thing worth
measuring rather than assuming. A field dominated by a slow seasonal ramp is
*translated* by a lag rather than restructured, and comes out close to rank one;
a field whose variance is mesoscale or synoptic, and so decorrelates inside the
span, gives a genuinely new direction per member. Measured on a 6 member, +/-21
day boundary ensemble on `gom_25km`, salinity came out at 1.8 effective
directions and velocity at 4.4 out of a possible 5.

**The ceiling is the member count minus one, not the member count**, whenever the
anomalies are centred, which mean-preserving generators like `tools/obc-lagged.py`
guarantee. A last singular value that is not zero in that case means the
generator did not centre what it claimed to.

---------------------------------------------------------------------------
The participation ratio, and why not a rank threshold

    effective = (sum s^2)^2 / sum s^4

which is the member count when every direction carries equal variance and 1 when
one direction carries all of it. A count of singular values above some cutoff
would need a cutoff, and there is no natural one: the spectrum of a lagged
ensemble decays smoothly rather than stepping.

---------------------------------------------------------------------------
The range columns are a bounds check

A mean-preserving additive anomaly can push a bounded field through its bound:
precipitation and radiation below zero, a salinity or a layer thickness below
zero. The minimum and maximum over all members are printed for every field so
that a generator that needs clamping, or a multiplicative form, says so here
rather than in a model that fails three weeks later.
"""

import argparse
import re
from pathlib import Path

import numpy as np
import netCDF4 as nc


def fields_of(path, wanted, merge):
    """`{name: [variable, ...]}`: what to score, and which variables make it up.

    Without *merge* each variable is its own field. With it, variables whose
    names match the pattern are grouped by its first capture, which is how one
    field spread over several open boundary segments becomes one row.
    """
    with nc.Dataset(path) as f:
        names = [name for name, v in f.variables.items()
                 if v.ndim > 1 and "time" in v.dimensions]
    if wanted:
        names = [name for name in names
                 if name in wanted or any(re.fullmatch(rf"{w}(_\w+)?", name)
                                          for w in wanted)]
    if not merge:
        return {name: [name] for name in names}

    grouped = {}
    for name in names:
        match = re.fullmatch(merge, name)
        grouped.setdefault(match.group(1) if match else name, []).append(name)
    return grouped


def flat(path, variables):
    with nc.Dataset(path) as f:
        return np.concatenate([np.asarray(f[name][:]).ravel()
                               for name in variables])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="directory holding mem###.nc")
    ap.add_argument("--vars", default="",
                    help="comma separated fields to score, default every "
                         "variable with a time dimension")
    ap.add_argument("--merge", metavar="REGEX",
                    help="group variables into one field by the pattern's first "
                         "capture, e.g. '(\\w+)_segment_\\w+' for a boundary "
                         "file whose fields are split across segments")
    args = ap.parse_args()

    paths = sorted(args.root.glob("mem*.nc"))
    if len(paths) < 3:
        raise SystemExit(f"spike-rank: {args.root} holds {len(paths)} mem*.nc "
                         f"files; a spectrum needs the unperturbed member plus "
                         f"at least two others")
    base, members = paths[0], paths[1:]
    wanted = [v.strip() for v in args.vars.split(",") if v.strip()]
    fields = fields_of(base, wanted, args.merge)
    if not fields:
        raise SystemExit(f"spike-rank: nothing to score in {base.name}")

    print(f"{len(members)} perturbed members against {base.name}, "
          f"ceiling {len(members) - 1} if the anomalies are centred\n")
    width = max(len(name) for name in fields)
    print(f"{'field':{width}}{'min':>10}{'max':>10}   "
          f"{'singular values, % of variance':<{6 * len(members)}}effective")
    for name, variables in fields.items():
        reference = flat(base, variables)
        anomalies = np.stack([flat(path, variables) - reference
                              for path in members])
        lo = min(float(np.nanmin(flat(path, variables))) for path in paths)
        hi = max(float(np.nanmax(flat(path, variables))) for path in paths)
        if not np.any(anomalies):
            # Not a degenerate case to hide: a field the generator copies
            # through rather than perturbs should appear, and appear as this.
            # `obc-lagged.py` leaves the source's layer thicknesses alone, so
            # every `dz_` lands here and its absence would be the surprise.
            print(f"{name:{width}}{lo:10.3f}{hi:10.3f}   "
                  f"{'identical in every member':<{6 * len(members)}}"
                  f"{'-':>9}")
            continue
        values = np.linalg.svd(anomalies, compute_uv=False)
        share = 100 * values ** 2 / (values ** 2).sum()
        effective = (values ** 2).sum() ** 2 / (values ** 4).sum()
        spectrum = " ".join(f"{s:5.1f}" for s in share)
        print(f"{name:{width}}{lo:10.3f}{hi:10.3f}   "
              f"{spectrum:<{6 * len(members)}}{effective:9.2f}")

    print("\nEffective is the participation ratio: the member count when every "
          "direction\ncarries equal variance, 1 when one direction carries all "
          "of it.")


if __name__ == "__main__":
    main()
