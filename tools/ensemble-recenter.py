#!/usr/bin/env python3
"""Move an ensemble's mean onto a control state, leaving every perturbation.

    tools/ensemble-recenter.py <ensemble dir> <control dir> [--output DIR]
    tools/ensemble-recenter.py \\
        $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00/lagged20 \\
        $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00

Reads `<ensemble dir>/mem*/MOM.res.nc`, writes `<output>/mem*/` with the whole
restart set copied and the recentred variables replaced. Default output is the
ensemble directory with `-recentred` appended.

**This is the last step of the ensemble build, after the settle**, and the order
is load bearing: see `tools/ensemble-ic-lagged.sh` for why running it first
leaves the mean a free day ahead of the control instead of on it.

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

## What is recentred

The ocean state: `Temp`, `Salt`, `u`, `v`, and the free surface. Named here
rather than read from an experiment's `solver.analysis variables`, because at the
time an initial condition is built there is no experiment: the ensemble is an
input to one, and several experiments may share it.

**One step of memory is left alone**, and that is a decision rather than an
omission. The restart also carries `u2` and `v2`, the auxiliary velocities,
`ubtav` and `vbtav`, the time mean barotropic pair, and the accelerations `CAu`,
`CAv`, `diffu`, `diffv`. Shifting them was tried. It changes nothing that
survives a timestep, because the model rebuilds all of them from the state above,
and on this domain it changed which member fell over: the free surface fix below
and a shifted `u2`/`v2` together killed a member that either alone left standing,
at a point where the sea surface was 0.08 m above an 11 m sea floor. A recipe
whose extra terms are washed out within a step and whose only measurable effect
is on the marginal columns is a recipe with fewer terms.

`ave_ssh` is included even though MOM6 overwrites it within a timestep, because
SOCA reads it as `sea_surface_height_above_geoid`. It is the DA's sea surface
height even when it is not the model's, and an ensemble whose SSH was centred
somewhere its temperature was not would give the filter an inconsistent
background.

## The free surface is moved through `h`, not through `ave_ssh`

**Shifting `ave_ssh` alone does not move the ocean.** Under Z* the mass field
*is* the free surface: a column's sea surface height is `sum(h) - D` and nothing
else, and `ave_ssh` is a diagnostic MOM6 recomputes from `h` within a timestep.
Recentring the diagnostic therefore moves the number SOCA reads and leaves `h`
exactly where it was. The tempting argument for it, that each member should keep
its own layer structure, is right about the goal and wrong about the mechanism.

That matters most now that recentring is the last step before an experiment
starts. Nothing runs afterwards to reveal the difference, so an ensemble whose
sea surface height is centred only in the diagnostic is one whose first model
step un-centres it, in the field the altimeter constrains.

So `h` is recentred, by scaling each column by `(total + delta) / total` rather
than by adding `control - mean` layer by layer. The two differ in what they do
to the member:

- Adding the offset per layer drives 0.8% of this domain's cells negative, which
  is the `implied h<0` crash the domain is already prone to. It also rewrites the
  interface positions, which are the member's own stratification.
- Scaling the column moves exactly the column integral, which is exactly the sea
  surface height, and leaves every interface at the same fraction of the column.
  The factor is within 2% of one here and cannot change sign, so positivity is
  preserved by construction rather than by a clip.

`sfc` and `ave_ssh` then take the same `delta` additively, which keeps
`sum(h) - D = sfc` true for every member if it was true before. The residual `h`
difference from the control after this is deep layer structure, and it is the
member's own: leaving it is the point.

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

#: What gets `control - mean` added to it. See the module docstring for why this
#: is a literal, and for why the second velocity registers are not in it.
VARIABLES = ("Temp", "Salt", "u", "v")

#: The layer thicknesses, which carry the free surface and are moved by scaling
#: rather than by adding.
THICKNESS = "h"

#: The two fields that have to agree with `sum(h) - D` afterwards.
SURFACE = ("sfc", "ave_ssh")


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


def column(thickness):
    """A column's height, which under Z* is its depth plus its sea surface."""
    return thickness.sum(axis=1)


def surface_offset(paths, control):
    """How far each column has to move for the ensemble's free surface to centre.

    Taken from the mass field rather than from `sfc`, because the mass field is
    the one MOM6 believes. Anything that disagrees with it is overwritten on the
    first step.
    """
    return column(read(control, THICKNESS)) - mean_column(paths)


def mean_column(paths):
    total = None
    for path in paths:
        values = column(read(path, THICKNESS))
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
    # Refused before the rmtree, because `--output <the ensemble>` is the
    # obvious way to ask for an edit in place and this is not that: it would
    # delete the members it is about to read, then die in `copytree`. Same for
    # the control.
    for name, taken in (("--ensemble", args.ensemble), ("--control", args.control)):
        if out.resolve() == taken.resolve():
            sys.exit(f"ensemble-recenter: --output is the same directory as "
                     f"{name}, and this rebuilds its output from scratch; "
                     f"name somewhere new")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print(f"ensemble-recenter: {len(sources)} member(s) onto {args.control.name}")
    restarts = [p / RESTART for p in sources]
    offsets = {}
    for name in VARIABLES:
        offsets[name] = read(control, name) - mean(restarts, name)
        size = float(np.sqrt(np.mean(offsets[name] ** 2)))
        print(f"  {name:8} shifted by rms {size:.5g}")

    delta = surface_offset(restarts, control)
    print(f"  {'surface':8} shifted by rms {float(np.sqrt(np.mean(delta ** 2))):.5g}"
          f", through {THICKNESS} and {' and '.join(SURFACE)}")

    for source in sources:
        target = out / source.name
        shutil.copytree(source, target)
        with netCDF4.Dataset(target / RESTART, "r+") as data:
            data.set_auto_mask(False)

            def put(name, values):
                data.variables[name][:] = values
                # The file's claim about itself is now false for this variable.
                # Same rule as `ackbar.writeback.place`, and for the same reason:
                # MOM6 refuses a restart whose checksum disagrees with its data.
                if "checksum" in data.variables[name].ncattrs():
                    data.variables[name].delncattr("checksum")

            for name in VARIABLES:
                put(name, read(source / RESTART, name) + offsets[name])

            thickness = read(source / RESTART, THICKNESS)
            height = column(thickness)
            scale = (height + delta) / height
            # A guard rather than a clip. A non-positive factor means this
            # sample's mean column is further from the control than the column
            # is deep, and a state built by flooring it would be a state no
            # member had, silently, in the one variable the model is least
            # forgiving about.
            if not np.all(scale > 0):
                sys.exit(f"ensemble-recenter: {source.name} would recentre to a "
                         f"column of zero or negative height (factor "
                         f"{float(scale.min()):.4g}); the sample is not one this "
                         f"control sits inside")
            put(THICKNESS, thickness * scale[:, np.newaxis])
            for name in SURFACE:
                put(name, read(source / RESTART, name) + delta)

    (out / "README.md").write_text(
        f"# Recentred ensemble\n\n"
        f"`{args.ensemble.name}` with its mean moved onto `{args.control.name}`,\n"
        f"by `tools/ensemble-recenter.py`. Every perturbation, and therefore the\n"
        f"whole sample covariance, is unchanged: only the state the ensemble is\n"
        f"an ensemble around is different.\n\n"
        f"Recentred: {', '.join(VARIABLES)}, and the free surface, which moves\n"
        f"by scaling each column of `{THICKNESS}` and shifting\n"
        f"{' and '.join(f'`{name}`' for name in SURFACE)} to match. Under Z* the\n"
        f"mass field is the sea surface height, so recentring the diagnostic\n"
        f"alone is undone by the model on the first step; see the tool's\n"
        f"docstring for the measurement.\n\n"
        f"Rebuild with:\n\n"
        f"    tools/ensemble-recenter.py {args.ensemble} {args.control}\n")
    print(f"ensemble-recenter: wrote {len(sources)} member(s) to {out}")


if __name__ == "__main__":
    main()
