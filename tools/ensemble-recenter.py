#!/usr/bin/env python3
"""Move an ensemble's mean onto a control state, leaving every perturbation.

    tools/ensemble-recenter.py <ensemble dir> <control dir> [--output DIR]
    tools/ensemble-recenter.py \\
        $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00/lagged20 \\
        $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00

Reads `<ensemble dir>/mem*/MOM.res.nc`, writes `<output>/mem*/` with the whole
restart set copied and the recentred variables replaced. Default output is the
ensemble directory with `-recentred` appended.

## What it does, in one line

`member += control - mean`, per variable, over every point.

That is the entire operation and the reason it is arithmetic here rather than a
call to `soca_ensrecenter.x`: the offset is one field, the same field for every
member, and building a run directory and an MPI launch around one subtraction
buys nothing an offline initial condition needs. The in-cycle path uses the app,
which is the right choice there because the cycle already has the machinery.

**The sample covariance is untouched.** Adding a common field to every member
moves the mean and leaves every deviation from it exactly as it was, so the
spread, the correlations and everything an ensemble filter reads from them are
the same before and after. What changes is only which state the ensemble is an
ensemble *around*. This is worth being explicit about, because "recentring" is
easy to hear as something that damages the ensemble.

## What is recentred, and what is not

`Temp`, `Salt`, `ave_ssh`, `u`, `v`. Named here rather than read from an
experiment's `solver.analysis variables`, because at the time an initial
condition is built there is no experiment: the ensemble is an input to one, and
several experiments may share it.

`ave_ssh` is included even though MOM6 overwrites it within a timestep, because
SOCA reads it as `sea_surface_height_above_geoid`. It is the DA's sea surface
height even when it is not the model's, and an ensemble whose SSH was centred
somewhere its temperature was not would give the filter an inconsistent
background.

**`h` is not recentred**, so each member keeps its own layer structure. Under
Z* that is the member's own free surface, which is what the model will actually
integrate. Shifting the mass field to match a shifted `ave_ssh` the model
discards would be inventing a column no member had, and it is the same choice
`ensemble.replace_from_mean` makes for the same reason.

## Every point, including land and the staggered edge

Unlike `ackbar.writeback`, which writes ocean cells only and crops `u` and `v`
to the tracer grid, this shifts the full array. Two reasons, and they are both
about this being a whole state rather than an analysis increment: a shift
applied to the interior of `u` but not to its outermost column would put a step
into the velocity field at the domain edge, and land values in a restart are
carried along by the model rather than ignored by it. The offset is defined
everywhere because both operands are, so there is nothing to restrict it to.
"""

import argparse
import shutil
import sys
from pathlib import Path

import netCDF4
import numpy as np

#: The restart this touches. One file, unlike an analysis writeback, because an
#: ensemble around a control differs from it in the ocean and the ice cover is
#: whatever each member's own year had.
RESTART = "MOM.res.nc"

#: What gets shifted. See the module docstring for why this is a literal.
VARIABLES = ("Temp", "Salt", "ave_ssh", "u", "v")


def members(root):
    found = sorted(p for p in root.glob("mem*") if (p / RESTART).exists())
    if not found:
        sys.exit(f"ensemble-recenter: no mem*/{RESTART} under {root}")
    return found


def read(path, name):
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return np.asarray(data.variables[name][:], dtype="f8")


def mean(paths, name):
    """One variable, averaged over the members.

    Accumulated one member at a time rather than stacked, because an ensemble is
    twenty full model states and holding them all at once to take a mean is how
    a one-node job runs out of memory doing arithmetic. The same reasoning, and
    the same shape, as `ackbar.ensemble._mean`.
    """
    total = None
    for path in paths:
        values = read(path, name)
        total = values if total is None else total + values
    return total / len(paths)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ensemble", type=Path)
    parser.add_argument("control", type=Path)
    parser.add_argument("--output", type=Path, default=None,
                        help="where to write; defaults to <ensemble>-recentred")
    args = parser.parse_args()

    control = args.control / RESTART
    if not control.exists():
        sys.exit(f"ensemble-recenter: {control} does not exist")

    sources = members(args.ensemble)
    # A new directory rather than an edit in place, so that re-running is a
    # replacement and not a second shift. Recentring twice is silent: the states
    # stay plausible and the ensemble is centred somewhere no state ever was.
    out = args.output or args.ensemble.with_name(args.ensemble.name + "-recentred")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print(f"ensemble-recenter: {len(sources)} member(s) onto {args.control.name}")
    offsets = {}
    for name in VARIABLES:
        offsets[name] = read(control, name) - mean([p / RESTART for p in sources], name)
        size = float(np.sqrt(np.mean(offsets[name] ** 2)))
        print(f"  {name:8} shifted by rms {size:.5g}")

    for source in sources:
        target = out / source.name
        shutil.copytree(source, target)
        with netCDF4.Dataset(target / RESTART, "r+") as data:
            data.set_auto_mask(False)
            for name in VARIABLES:
                data.variables[name][:] = read(source / RESTART, name) + offsets[name]
                # The file's claim about itself is now false for this variable.
                # Same rule as `ackbar.writeback.place`, and for the same reason:
                # MOM6 refuses a restart whose checksum disagrees with its data.
                if "checksum" in data.variables[name].ncattrs():
                    data.variables[name].delncattr("checksum")

    (out / "README.md").write_text(
        f"# Recentred ensemble\n\n"
        f"`{args.ensemble.name}` with its mean moved onto `{args.control.name}`,\n"
        f"by `tools/ensemble-recenter.py`. Every perturbation, and therefore the\n"
        f"whole sample covariance, is unchanged: only the state the ensemble is\n"
        f"an ensemble around is different.\n\n"
        f"Recentred: {', '.join(VARIABLES)}. Not `h`, so each member keeps its\n"
        f"own layer structure; see the tool's docstring.\n\n"
        f"Rebuild with:\n\n"
        f"    tools/ensemble-recenter.py {args.ensemble} {args.control}\n")
    print(f"ensemble-recenter: wrote {len(sources)} member(s) to {out}")


if __name__ == "__main__":
    main()
