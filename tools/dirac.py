#!/usr/bin/env python3
"""Plan a dirac test of the background error, and read back what it did.

    tools/dirac.py plan   <layers> <static> <levels> <metadata> <gridspec> \\
                          <restart> <out.yaml> <points.json> [lat,lon]... \\
                          [--full | --ensemble <dir> | --hybrid <dir>]
    tools/dirac.py report <gridspec> <static> <points.json> <outdir> \\
                          [--full | --ensemble <dir> | --hybrid <dir>]

Two verbs in one file because they have to agree about which dirac is which.
The toolbox applies the operator to a single increment holding every dirac at
once, so `points.json` written by `plan` is the only record of what was placed
where, and both halves read the node ordering of the calibration files the same
way. `tools/soca-dirac.sh` is what calls them.

## Four tests, and they answer different questions

Without a flag this is a test of the **correlation** alone: the central
diffusion block and nothing else. The response at the dirac is then exactly 1
by definition, so the peak measures the normalization's Monte Carlo error and
the radius measures whether the operator built the scale the calibration
ordered. It is a test of `tools/soca-diffusion.sh` and of nothing in
`config/layers/da/variational.yaml` below the central block.

With `--full` it is a test of the **static covariance** the analysis actually
uses: the standard deviations, the depth taper and the balance operator are all
in. Nothing is normalized to anything, and there is nothing to pass or fail.
What it shows is what one observation would do: the size of the increment in the
variable the dirac was placed in, and the size of the increment the balance
operator puts into the other two. It writes the standard deviation fields as
well, which is the only way to see what the parametric block built before it was
multiplied by anything.

`--ensemble <dir>` replaces that covariance with the **ensemble** one built from
the member restarts in `<dir>/mem*/MOM.res.nc`, localized exactly as
`config/layers/da/hybrid.yaml` says. There is no balance operator in it: an
increment in a variable the dirac was not placed in is the sample covariance and
nothing else, which is what makes this the test of whether the ensemble
component is cross-variable. `--hybrid <dir>` is both, weighted, and asks the
toolbox to apply each component separately as well, so one run writes three
increments: the hybrid and the two halves it is made of.

## It builds the covariance the workflow builds

Every mode but the first goes through `ackbar.soca.background_error`, over the
same merged layer stack `ackbar create` merges. A dirac test against a second
description of the background error would pass while the analysis used a
different operator, which is the failure this exists to catch, and the
localization variable list is exactly the sort of key that goes missing between
two descriptions: `soca._localization` is what puts it in, so this has to be
what put it in here too.
"""

import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import netCDF4
import numpy as np
import yaml

from ackbar import soca
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.diffusion import THIN_LAYER

#: Fixed by SOCA. `ifdir` indexes a hardcoded table in `soca_increment_mod`, not
#: the increment's own variable list, so these numbers are the same whatever the
#: configuration asks for. See `soca_increment_dirac`, whose table runs to nine
#: entries; these are the five an ocean analysis here carries, and the four
#: skipped between them are ice and biogeochemistry.
IFDIR = {"sea_water_potential_temperature": 1,
         "sea_water_salinity": 2,
         "sea_surface_height_above_geoid": 3,
         "eastward_sea_water_velocity": 8,
         "northward_sea_water_velocity": 9}

#: The restart variable each of those is written to, which is how the increment
#: comes back off disk. `config/model/mom6sis2/fields_metadata.yaml` owns this
#: mapping; it is repeated here because this reads the file directly.
#:
#: `u` and `v` come back on the tracer array shape rather than on a staggered
#: one, and index `i` of `u` is the face on the *low* side of tracer `i`. That is
#: not a property of the increment, it is what SOCA's reader does to every
#: staggered field and what `ackbar.gridspec` moved the gridspec's `lonu` and
#: `mask2du` to match. Anything drawing a velocity increment has to place it at
#: `lonu`/`latu` rather than at `lon`/`lat`, which is what `tools/dirac-page.py`
#: does.
IO_NAME = {"sea_water_potential_temperature": "Temp",
           "sea_water_salinity": "Salt",
           "sea_surface_height_above_geoid": "ave_ssh",
           "eastward_sea_water_velocity": "u",
           "northward_sea_water_velocity": "v"}

#: What the background carries in `correlation` mode. The three the static B's
#: central block has a group for, which is what that mode applies, and not
#: `IFDIR`: velocity is in the table above because a *covariance* can reach it,
#: and asking the background for a field no group in the correlation will touch
#: reads two more full arrays off disk to describe nothing.
CORRELATION_STATE = ("sea_water_potential_temperature",
                     "sea_water_salinity",
                     "sea_surface_height_above_geoid")

DATADIR = "out"
EXPERIMENT = "dirac_%id%"
DATE = "2000-01-01T00:00:00Z"

#: The layer stack each mode merges to get its solver. The same names an
#: experiment writes under `inherit:`, so the covariance built here is the one
#: `ackbar create` would freeze rather than a second spelling of it. `static`
#: and `correlation` share a stack because they differ in how much of the same
#: block is kept, not in which block it is.
STACK = {"correlation": ["da/variational"],
         "static": ["da/variational"],
         "ensemble": ["da/envar"],
         "hybrid": ["da/hybrid"]}

#: Which modes read an ensemble. The two that do refuse to run without one, and
#: the two that do not refuse to be given one, which is `background_error`'s own
#: rule rather than a second one written here.
ENSEMBLE_MODES = ("ensemble", "hybrid")

#: How a member's restart is found under the directory `--ensemble` names. The
#: layout `tools/ensemble-recenter.py` writes and `ackbar` reads.
MEMBER_GLOB = "mem*"
MEMBER_RESTART = "MOM.res.nc"

#: Where `--full` asks `SOCAParametricOceanStdDev` to write the fields it built.
#: A stem: `util::writeFieldSet` appends `.nc`. Beside `out/` rather than in it,
#: because `read_increment` takes the one netCDF file there and a second one
#: would make it ambiguous which is the increment.
DIAGS = "stddev"

#: The units each variable's increment comes back in, for the `--full` report.
#: Only for printing; nothing computes with them.
UNITS = {"sea_water_potential_temperature": "degC",
         "sea_water_salinity": "psu",
         "sea_surface_height_above_geoid": "m",
         "eastward_sea_water_velocity": "m/s",
         "northward_sea_water_velocity": "m/s"}

#: The fraction of the peak whose distance is reported. For a Gaussian kernel of
#: Daley length L the response is exp(-0.5) at exactly L, which makes the
#: reported radius directly comparable to the scale the calibration was given.
E_HALF = float(np.exp(-0.5))

#: How far the measured radius may sit from the requested scale before this
#: calls it a disagreement. Generous, because the reported radius is measured
#: along a grid line by linear interpolation between two cells, and on a coarse
#: grid the scale is only a couple of cells wide.
RADIUS_TOLERANCE = 0.35

#: How far the peak may sit from 1. This is the normalization's Monte Carlo
#: error and nothing else, so it is the number that says whether
#: `normalization iterations` was high enough.
PEAK_TOLERANCE = 0.05


def main(argv):
    if len(argv) < 2 or argv[1] not in ("plan", "report"):
        sys.exit(__doc__.strip())
    rest, mode, ensemble, overrides = options(argv[2:])
    return (plan(rest, mode, ensemble, overrides) if argv[1] == "plan"
            else report(rest, mode))


def options(argv):
    """Split the positional arguments from the one flag that picks a mode.

    One flag and not several: the modes are alternatives, and a run that was
    given two of them is a run whose author believes it applied a covariance it
    did not. Refusing here is cheaper than reading a report of the wrong B.

    `--localization <name>` is the one layer *var* this can override, and it
    exists so that the masked and the unmasked localization can be applied to
    the same dirac on the same day. `config/layers/da/hybrid.yaml` declares
    `localization_hz`, an experiment overrides it by restating it in its own
    `vars:`, and this is that same override for a run with no experiment behind
    it. Anything else in the layer stack is read as written.
    """
    rest, mode, ensemble, overrides = [], "correlation", None, {}
    words = list(argv)
    while words:
        word = words.pop(0)
        if word in ("--full", "--ensemble", "--hybrid"):
            if mode != "correlation":
                sys.exit("dirac: --full, --ensemble and --hybrid are alternatives")
            mode = "static" if word == "--full" else word[2:]
            if mode in ENSEMBLE_MODES:
                if not words:
                    sys.exit(f"dirac: {word} needs a directory of members")
                ensemble = words.pop(0)
        elif word == "--localization":
            if not words:
                sys.exit("dirac: --localization needs a calibration name, such "
                         "as loc_hz or loc_hz_open")
            overrides["localization_hz"] = words.pop(0)
        elif word.startswith("-"):
            sys.exit(f"dirac: unknown option {word}")
        else:
            rest.append(word)
    return rest, mode, ensemble, overrides


# ------------------------------------------------------------------------------
# plan


def plan(argv, mode="correlation", ensemble=None, overrides=None):
    layers, static, levels, metadata, gridspec, restart = argv[0:6]
    out_yaml, out_points = argv[6:8]
    requested = argv[8:]

    grid = read_gridspec(gridspec)
    depth, deepest_level = read_restart(restart, grid)

    if requested:
        cells = [nearest_ocean(grid, *parse(text)) for text in requested]
    else:
        cells = default_cells(grid, depth, static)

    points = place(grid, cells, deepest_level, mode != "correlation")
    warn_if_crowded(grid, points, static)

    with open(out_points, "w") as stream:
        json.dump(points, stream, indent=2)
    with open(out_yaml, "w") as stream:
        yaml.safe_dump(document(layers, static, levels, metadata, restart, points,
                                mode, ensemble, overrides),
                       stream, sort_keys=False, default_flow_style=False)

    for point in points:
        print(f"dirac: {point['what']:26s} {point['lat']:8.3f} {point['lon']:9.3f}"
              f"  i={point['ixdir']:4d} j={point['iydir']:4d} k={point['izdir']:3d}"
              f"   {point['why']}")


def parse(text):
    try:
        lat, lon = (float(part) for part in text.split(","))
    except ValueError:
        sys.exit(f"dirac: '{text}' is not <lat>,<lon>")
    return lat, lon


def read_gridspec(path):
    with netCDF4.Dataset(path) as src:
        grid = {name: np.asarray(src.variables[name][0])
                for name in ("lon", "lat", "dx", "dy")}
        grid["mask"] = np.asarray(src.variables["mask2d"][0]) > 0.0
        # Used to keep a default dirac far enough from land that its kernel is
        # measurable. gridgen computes it; it knows about land and not about a
        # regional grid's open boundary, which is what EDGE is for.
        grid["coast"] = np.asarray(src.variables["distance_from_coast"][0])
    return grid


def read_restart(path, grid):
    """Water column depth, and how many levels a dirac can go into."""
    with netCDF4.Dataset(path) as src:
        h = np.asarray(src.variables["h"][0])
    if h.shape[1:] != grid["mask"].shape:
        sys.exit("dirac: the restart and the gridspec are different grids")
    # `THIN_LAYER`, not a literal, and not `writeback.VANISHED`. This counts
    # levels, which is what `ackbar.diffusion` uses it for: at a centimetre the
    # Z* layers pressed under the sea floor still count, so a 10 m shelf column
    # reads as fifty levels deep and a dirac aimed at its middle lands in a
    # collapsed layer. `judge` then reports the operator returning zero and
    # blames the calibration.
    thick = h > THIN_LAYER
    return np.where(grid["mask"], (thick * h).sum(axis=0), 0.0), thick.sum(axis=0)


def nearest_ocean(grid, lat, lon):
    """The closest wet cell, by an approximation good enough to pick a cell.

    Longitude is folded into the same branch as the grid's before differencing,
    so that a request at -90 finds a cell the gridspec calls 270.
    """
    dlon = (grid["lon"] - lon + 180.0) % 360.0 - 180.0
    distance = np.hypot(dlon * np.cos(np.radians(lat)), grid["lat"] - lat)
    distance = np.where(grid["mask"], distance, np.inf)
    if not np.isfinite(distance).any():
        sys.exit("dirac: this domain has no ocean in it")
    j, i = np.unravel_index(np.argmin(distance), distance.shape)
    return int(j), int(i), f"nearest wet cell to {lat},{lon}"


#: How many latitude bands the default locations span the domain with.
BANDS = 3

#: How much clear water a default location needs around it, as a multiple of the
#: longest scale in play there. Two is enough for the response to have fallen
#: well past exp(-0.5) before it reaches anything.
ROOM = 2.0

#: Cells this close to the edge of the array are not eligible, however much
#: water `distance_from_coast` thinks is around them. On a regional grid the
#: array edge is an open boundary rather than a coast and the gridspec does not
#: know the difference, so a dirac there has a kernel that leaves the domain
#: through a side nothing has masked. Whether it *should* be masked is the open
#: question in Domains, docs/design.md; either way it is not where to measure a
#: correlation length.
EDGE = 2


def default_cells(grid, depth, static):
    """Open ocean across the domain's latitudes, plus the shortest scale in it.

    Picked from data rather than named here, so that they move with the domain.

    The Rossby radius varies with latitude more than with anything else, so one
    location says nothing about whether the calibration is right anywhere but
    there. The domain is divided into equal latitude bands and the deepest
    eligible cell in each is taken, which keeps them in open water without
    naming a place.

    The last is the cell with the shortest horizontal scale, wherever it is.
    That is the other regime the calibration has: where the Rossby radius has
    fallen below `min grid mult` times the cell size and the grid, rather than
    the ocean, is setting the correlation length. On a coarse grid that is most
    of the domain and on a fine grid it is the shelf. A calibration that is
    wrong is usually wrong in one regime and not the other.
    """
    scales = np.maximum(
        read_nodes(f"{static}/diffusion/corr_hz.nc", "hzScales", grid)[..., 0],
        read_nodes(f"{static}/diffusion/corr_hz_ssh.nc", "hzScales", grid)[..., 0],
    )

    inside = np.zeros_like(grid["mask"])
    inside[EDGE:-EDGE, EDGE:-EDGE] = True
    eligible = grid["mask"] & inside & (grid["coast"] >= ROOM * scales)

    cells = []
    latitudes = grid["lat"][grid["mask"]]
    edges = np.linspace(latitudes.min(), latitudes.max(), BANDS + 1)
    for low, high in zip(edges[:-1], edges[1:]):
        band = eligible & (grid["lat"] >= low) & (grid["lat"] <= high)
        if not band.any():
            print(f"dirac: nothing between {low:.1f} and {high:.1f} has room for "
                  f"its own correlation length, so no dirac goes there")
            continue
        deep = np.where(band, depth, -np.inf)
        j, i = np.unravel_index(np.argmax(deep), deep.shape)
        cells.append((int(j), int(i),
                      f"deepest with room, {low:.0f} to {high:.0f} lat, "
                      f"{deep[j, i]:.0f} m"))

    shortest = np.where(grid["mask"] & (scales > 0), scales, np.inf)
    j, i = np.unravel_index(np.argmin(shortest), shortest.shape)
    cells.append((int(j), int(i), f"shortest scale, {shortest[j, i] / 1e3:.1f} km"))
    return cells


def place(grid, cells, deepest_level, full=False):
    """Two diracs per location: temperature at mid-depth, and height.

    Two rather than three, and neither of them a surface temperature, because
    every dirac goes into one increment and one application of the operator.
    Two diracs in the same field and the same column would have their vertical
    responses added together, and the report would read the sum as one kernel.
    Temperature and height are separate fields and cannot interfere.

    Mid-depth rather than the surface for temperature: the horizontal scales are
    a single level and give the same kernel at every depth, so the surface buys
    nothing, while a dirac at the top of the column has half its vertical kernel
    cut off by the boundary.

    **`full` places the temperature dirac alone**, because the argument above
    stops holding once the balance operator is in. A correlation is block
    diagonal in the variables, so a temperature dirac cannot reach the height
    field and the two never interfere. A covariance is not: the height a
    temperature dirac produces through balance lands in the same cell as the
    height dirac's own response, they add, and no reading of the sum separates
    them. One dirac per location, in temperature, makes every height and
    salinity number in the report the balance operator's and nothing else.
    """
    points = []
    for j, i, why in cells:
        nk = int(deepest_level[j, i])
        if nk < 4:
            sys.exit(f"dirac: the cell chosen for '{why}' is only {nk} levels deep")
        placed = (("sea_water_potential_temperature", nk // 2),)
        if not full:
            placed += (("sea_surface_height_above_geoid", 1),)
        for variable, level in placed:
            points.append({
                # Fortran indexing, and global rather than per task: SOCA
                # compares these against each rank's `isc`/`iec` directly and
                # silently skips any dirac outside.
                "ixdir": i + 1, "iydir": j + 1, "izdir": level,
                "ifdir": IFDIR[variable],
                "variable": variable,
                "j": j, "i": i, "level": level,
                "lat": float(grid["lat"][j, i]), "lon": float(grid["lon"][j, i]),
                "why": why,
                "what": f"{short(variable)} at level {level}",
            })
    return points


def short(variable):
    return {"sea_water_potential_temperature": "temperature",
            "sea_water_salinity": "salinity",
            "sea_surface_height_above_geoid": "height",
            "eastward_sea_water_velocity": "eastward velocity",
            "northward_sea_water_velocity": "northward velocity"}.get(
                variable, variable)


def warn_if_crowded(grid, points, static):
    """Two diracs in one field close enough to overlap make the report wrong.

    Not fatal: the locations may have been asked for deliberately, and the
    overlap shows up in the report as a peak above 1. Worth saying out loud
    though, because otherwise it reads as a normalization failure.
    """
    scales = read_nodes(f"{static}/diffusion/corr_hz.nc", "hzScales", grid)[..., 0]
    for a in points:
        for b in points:
            if a is b or a["variable"] != b["variable"]:
                continue
            span = np.hypot((a["i"] - b["i"]) * grid["dx"][a["j"], a["i"]],
                            (a["j"] - b["j"]) * grid["dy"][a["j"], a["i"]])
            reach = 3.0 * scales[a["j"], a["i"]]
            if span < reach:
                print(f"dirac: WARNING two {short(a['variable'])} diracs are "
                      f"{span / 1e3:.0f} km apart and the scale there reaches "
                      f"{reach / 1e3:.0f} km. Their responses overlap.")
                return


def solver_of(layers, mode):
    """The solver block and the `vars` of the stack *mode* names, merged as ackbar merges it.

    Merged rather than read out of one file, because a hybrid's solver is
    `da/variational` with `da/hybrid` on top of it and reading either alone
    gives a covariance nothing would ever run. `resolve_layers` wants an
    experiment file, so it gets one: two lines in a temporary directory,
    holding nothing but the `inherit:` list.

    Merged with no merge keys, which is not a shortcut: the schema declares
    exactly one, `observations`, and no `da/*` layer carries an observer. Doing
    it properly would mean importing `ackbar.config.schema` and therefore
    jsonschema, which is in the project venv and not in the interpreter
    `tools/soca-dirac.sh` runs under, to resolve a key that cannot appear in
    what is being merged.

    The `vars` come back beside the solver because a layer is allowed to select
    a file through one: `da/hybrid` names its localization as
    `$(domain_static)/diffusion/$(localization_hz)`. Resolving that here rather
    than hardcoding a filename is what makes this read the localization the
    experiment reads, which is the whole rule this file is built on.
    """
    if mode not in STACK:
        sys.exit(f"dirac: {mode!r} is not a covariance this builds")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dirac.yaml"
        with open(path, "w") as stream:
            yaml.safe_dump({"inherit": STACK[mode]}, stream)
        stack = resolve_layers(path, layers)
    merged = merge_layers(stack)
    return merged["solver"], dict(merged.get("vars") or {})


def members_of(directory):
    """One state description per member, in the order ackbar would read them."""
    root = Path(directory)
    names = sorted(entry.name for entry in root.glob(MEMBER_GLOB)
                   if (entry / MEMBER_RESTART).exists())
    if len(names) < 2:
        sys.exit(f"dirac: {root} holds {len(names)} member restart(s); an "
                 f"ensemble covariance needs at least two")
    print(f"dirac: {len(names)} members from {root}")
    return soca.member_states(lambda name: root / name / MEMBER_RESTART, names,
                              date=DATE, variables=list(IFDIR))


def document(layers, static, levels, metadata, restart, points, mode="correlation",
             ensemble=None, overrides=None):
    """The experiment's own background error, lifted out of the layer stack.

    Lifted rather than written here: a dirac test against a second description
    of the background error would pass while the analysis used a different
    operator, which is the failure this exists to catch.

    In `correlation` everything outside the central block is dropped, because
    the standard deviations and the balance operator turn a correlation into a
    covariance and would leave the peak value meaning nothing. Every other mode
    hands the whole solver to `ackbar.soca.background_error`, which is the same
    call the analysis job makes, and the peak means something else instead; see
    the module docstring.
    """
    solver, variables = solver_of(layers, mode)
    variables.update(overrides or {})
    analysis_variables = solver["analysis variables"]
    state_variables = list(CORRELATION_STATE)

    if mode == "correlation":
        background_error = solver["background error"]
        wanted = {"covariance model": background_error["covariance model"],
                  "saber central block": background_error["saber central block"]}
    else:
        # `save diagnostics` goes into the layer's own block rather than the
        # assembled document, because a hybrid's assembled document holds that
        # block one level down inside a component and finding it again there
        # would be a second piece of knowledge about the shape of a hybrid.
        solver = copy.deepcopy(solver)
        if "saber outer blocks" in solver["background error"]:
            solver["background error"]["saber outer blocks"] = with_diagnostics(
                solver["background error"]["saber outer blocks"])
        if (mode in ENSEMBLE_MODES) != bool(ensemble):
            sys.exit(f"dirac: {mode} reads an ensemble and none was given")
        wanted = soca.background_error(
            solver, analysis_variables,
            ensemble=members_of(ensemble) if ensemble else None)
        if mode == "hybrid":
            # Apply the two components separately as well as together. The
            # toolbox writes one increment per component, named by `%id%`, and
            # a hybrid whose ensemble half is doing nothing is otherwise
            # indistinguishable from one whose weights are wrong.
            wanted["run components recursively"] = True
        # The outer blocks read fields the central block does not: thickness is
        # what makes anything addressable by depth, and the parametric standard
        # deviations are built out of the mixed layer and the depth.
        state_variables = solver["background variables"]

    covariance = substitute(
        wanted, dict(variables,
                     **{"domain_static": static, "diffusion_levels": levels}))

    return {
        "geometry": {
            "geom_grid_file": "soca_gridspec.nc",
            "mom6_input_nml": "mom_input.nml",
            "fields metadata": metadata,
        },
        "background": {
            "read_from_file": 1,
            "basename": os.path.dirname(restart) + "/",
            "ocn_filename": os.path.basename(restart),
            "date": DATE,
            # Salinity is here although no dirac goes into it: it shares a
            # diffusion group with temperature, and leaving it out would change
            # which fields the block is built over.
            "state variables": state_variables,
        },
        "background error": covariance,
        # What the dirac increment is built over. Without it the toolbox takes
        # the background's own variable list, which outside `correlation` is six
        # fields rather than three because the outer blocks read thickness, the
        # mixed layer and the depth. The increment would then carry three fields
        # the linear variable change does not, and SOCA fails an assertion on
        # `Increment::operator=` rather than saying so.
        **({} if mode == "correlation"
           else {"increment variables": analysis_variables}),
        # The toolbox prints the value at every dirac itself, before anything
        # here reads a file. Two independent readings of the peak are worth the
        # one line of output.
        "dirac": {key: [point[key] for point in points]
                  for key in ("ixdir", "iydir", "izdir", "ifdir")},
        "output dirac": {"datadir": DATADIR, "date": DATE,
                         "exp": EXPERIMENT, "type": "an"},
    }


def with_diagnostics(blocks):
    """The layer's outer blocks, with the parametric one asked to show its work.

    `SOCAParametricOceanStdDev` builds the standard deviations out of the
    background and then hands them to the next block, and nothing downstream
    reports what they were. `save diagnostics` writes them, along with the
    intermediate fields they were built from: `dtdz`, the mixed layer, the
    depth, and the sst floor as it landed on this grid after interpolation.
    That last one is the only direct check that `tools/sst-bgerr.py` produced a
    file SOCA can read.

    Added here rather than in the layer because it is a per cycle write of every
    field in the covariance, and an experiment that turned it on would spend
    more disk on the diagnostic than on the analysis. An experiment that wants
    it can add the same two lines.
    """
    out = []
    for block in blocks:
        if block.get("saber block name") == "SOCAParametricOceanStdDev":
            block = dict(block, **{"save diagnostics": {"filepath": DIAGS}})
        out.append(block)
    return out


def substitute(node, values):
    """Resolve the experiment-time tokens a background error block can contain.

    *values* is the layer stack's own `vars` with the two paths ACKBAR supplies
    at create time laid over them, so a token resolves here to what it would
    resolve to in a frozen `cfg/`. Anything left unresolved is a token nothing
    in the stack declares, and it fails rather than handing `$(...)` to eckit,
    which would read it as a filename.
    """
    if isinstance(node, dict):
        return {key: substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [substitute(value, values) for value in node]
    if not isinstance(node, str):
        return node

    def one(match):
        if match.group(1) not in values:
            sys.exit(f"dirac: nothing here can resolve $({match.group(1)})")
        return str(values[match.group(1)])

    text = re.sub(r"\$\((\w+)\)", one, node)
    # `levels:` has to come back as an integer. eckit will not coerce it.
    return int(text) if text.isdigit() else text


# ------------------------------------------------------------------------------
# report


def report(argv, mode="correlation"):
    gridspec, static, points_path, outdir = argv[0:4]
    grid = read_gridspec(gridspec)
    points = json.load(open(points_path))
    increments = read_increments(outdir)
    if mode != "correlation":
        for name, increment in increments.items():
            report_full(grid, points, increment, name)
        # Only the modes with a static component have a parametric block to
        # have written any. Asking for them after a pure ensemble run would
        # report their absence as though it were a misconfiguration.
        if mode in ("static", "hybrid"):
            diagnostics(static, grid)
        return 0
    if len(increments) != 1:
        sys.exit(f"dirac: expected one increment in {outdir}, "
                 f"found {len(increments)}")
    increment = next(iter(increments.values()))

    requested = {
        "sea_water_potential_temperature":
            read_nodes(f"{static}/diffusion/corr_hz.nc", "hzScales", grid)[..., 0],
        "sea_surface_height_above_geoid":
            read_nodes(f"{static}/diffusion/corr_hz_ssh.nc", "hzScales", grid)[..., 0],
    }
    vertical = read_nodes(f"{static}/diffusion/corr_vt.nc", "vtScales", grid)

    print()
    print("  field                       peak    east    west   north   south "
          "    want      up    down    want")
    print("  " + "-" * 96)

    bad = []
    for point in points:
        field = increment[IO_NAME[point["variable"]]]
        j, i, k = point["j"], point["i"], point["level"] - 1
        plane = field[k] if field.ndim == 3 else field
        peak = float(plane[j, i])

        along = {
            "east": radius(plane[j, i:], grid["dx"][j, i:], peak),
            "west": radius(plane[j, i::-1], grid["dx"][j, i::-1], peak),
            "north": radius(plane[j:, i], grid["dy"][j:, i], peak),
            "south": radius(plane[j::-1, i], grid["dy"][j::-1, i], peak),
        }
        want = requested[point["variable"]][j, i]

        if field.ndim == 3:
            column = field[:, j, i]
            up = radius(column[k::-1], np.ones(k + 1), peak)
            down = radius(column[k:], np.ones(len(column) - k), peak)
            want_z = vertical[j, i, k]
            depth_part = (f"{up:7.2f} {down:7.2f} {want_z:7.2f}"
                          if np.isfinite(want_z) else "      -       -       -")
        else:
            up = down = want_z = np.nan
            depth_part = "      -       -       -"

        print(f"  {point['what']:22s} {peak:8.4f} "
              + " ".join(f"{along[side] / 1e3:7.1f}" for side in
                         ("east", "west", "north", "south"))
              + f" {want / 1e3:7.1f}   {depth_part}")

        bad += judge(point, peak, along, want, (up, down, want_z))

    print()
    for message in bad:
        print(f"dirac: {message}")
    if bad:
        return 1
    print("dirac: the operator is normalized and the scales are the ones asked for.")
    return 0


def report_full(grid, points, increment, name):
    """What one observation would do, through the covariance as configured.

    Nothing here passes or fails. A correlation returns 1 at its own dirac and
    that is a statement about the operator; a covariance returns the variance,
    and whether that number is right is a question about the ocean that no
    single run can answer. What this is for is seeing the three things a
    configuration change was meant to do:

    * the size of the increment in the variable the dirac went into, which is
      the standard deviation squared where the correlation is 1,
    * the size of the increment in the other two, which through the static B is
      the balance operator's and through an ensemble is the sample covariance's,
    * the standard deviation fields themselves, which the parametric block
      wrote on the way past.

    *name* is the toolbox's own id for the covariance that produced this
    increment: `SABER`, `ensemble`, `hybrid`, or a hybrid's numbered component.
    A hybrid run reports three of these, and which is which is the whole point
    of running it that way.
    """
    print()
    print(f"  covariance: {name}")
    print(f"  {'dirac':<26}{'variable':>13}{'value':>11} {'units':<6}"
          f"{'east':>8}{'west':>8}{'north':>8}{'south':>8}")
    print("  " + "-" * 90)
    for point in points:
        j, i, k = point["j"], point["i"], point["level"] - 1
        for variable, io in IO_NAME.items():
            field = increment.get(io)
            if field is None:
                continue
            plane = field[k] if field.ndim == 3 else field
            value = float(plane[j, i])
            # The response of a variable the dirac did not go into is
            # cross-variable and can be either sign. The radius is only
            # meaningful where there is something to measure a radius of.
            spread = ("      -       -       -       -" if abs(value) < 1e-12
                      else " ".join(
                          f"{radius(line, spacing, value) / 1e3:7.1f}" for line, spacing in (
                              (plane[j, i:], grid["dx"][j, i:]),
                              (plane[j, i::-1], grid["dx"][j, i::-1]),
                              (plane[j:, i], grid["dy"][j:, i]),
                              (plane[j::-1, i], grid["dy"][j::-1, i]))))
            mark = "*" if variable == point["variable"] else " "
            label = f"{point['what']} {mark}" if mark == "*" else point["what"]
            print(f"  {label if variable == point['variable'] else '':<26}"
                  f"{short(variable):>13}{value:11.4f} {UNITS[variable]:<6}"
                  f"{spread}")
        print(f"  {'':<26}{point['lat']:.2f}, {point['lon']:.2f}   {point['why']}")
        print()

    print("  * is the variable the dirac was placed in. Through the static B the")
    print("    other rows are the balance operator; through an ensemble there is")
    print("    no balance operator and they are the sample covariance.")
    return 0


def diagnostics(static, grid):
    """The standard deviations the parametric block wrote, as a summary.

    Domain medians and the range, per field, over ocean. The fields themselves
    are in the file for anything that wants to look at where they are, which is
    the question a median cannot answer.
    """
    path = f"{DIAGS}.nc"
    if not os.path.exists(path):
        print(f"\ndirac: no {path}; the parametric block did not write its "
              f"diagnostics, which means it is not in this experiment's outer "
              f"blocks.")
        return
    print(f"\n  the standard deviations this B was built from ({path}), "
          f"over ocean")
    print(f"  {'field':<34}{'median':>10}{'10th':>10}{'90th':>10}{'max':>10}")
    print("  " + "-" * 74)
    with netCDF4.Dataset(path) as src:
        for name in sorted(src.variables):
            data = src.variables[name]
            if data.ndim < 1 or name in ("lon", "lat"):
                continue
            values = np.ma.filled(np.ma.masked_invalid(data[:]), np.nan).ravel()
            values = values[np.isfinite(values)]
            if not values.size:
                continue
            if not np.any(values != 0.0):
                # Said rather than dropped. A field of zeros is usually a
                # decision (`unbalanced ssh` is set to zero in the layer) and
                # occasionally a mistake, and a missing row reads as neither.
                print(f"  {name:<34}{'zero everywhere':>40}")
                continue
            # Land is zero in these and would drag every percentile down.
            values = values[values != 0.0]
            print(f"  {name:<34}{np.median(values):>10.4f}"
                  f"{np.percentile(values, 10):>10.4f}"
                  f"{np.percentile(values, 90):>10.4f}{values.max():>10.4f}")


def read_increments(outdir):
    """Every increment the toolbox wrote, by the covariance that produced it.

    One file in every mode but `--hybrid`, which asks for its components as
    well and gets three. SOCA names them `ocn.ice.dirac_<id>.an.<date>.nc`,
    where `<id>` is the toolbox's `%id%`: the covariance model, with a component
    index appended for each half of a hybrid.
    """
    found = {}
    for name in sorted(os.listdir(outdir)):
        if not name.endswith(".nc"):
            continue
        with netCDF4.Dataset(f"{outdir}/{name}") as src:
            found[increment_id(name)] = {
                io: np.asarray(src.variables[io][0])
                for io in IO_NAME.values() if io in src.variables}
    if not found:
        sys.exit(f"dirac: no increment in {outdir}; the toolbox wrote nothing")
    return found


def increment_id(filename):
    """`hybrid1_SABER` out of `ocn.dirac_hybrid1_SABER.an.<date>.nc`.

    The prefix and the two trailing fields are `soca_genfilename`'s, and what is
    between them is what `dirac.py` asked to be there. The prefix is `ocn.` or
    `ocn.ice.` depending on which domain type the increment was written for, so
    both are accepted. Anything this does not recognise comes back whole rather
    than being renamed into something tidier, because a filename it cannot parse
    is a filename worth seeing.

    One id is not a covariance at all: a localized ensemble covariance writes
    `<id>_localization` beside its own increment, which is the localization
    applied to the dirac by itself. It comes through with that name, and it is
    the most direct picture there is of the taper the ensemble is multiplied by.
    """
    match = re.match(r"^ocn(?:\.ice)?\.dirac_(.+)\.an\..*\.nc$", filename)
    return match.group(1) if match else filename


def read_nodes(path, name, grid):
    """A calibration file, reshaped from atlas node order onto the model grid.

    The calibration writes one value per atlas node with no dimension names that
    say how they are laid out, so the reshape is checked rather than assumed:
    the file carries the longitude and latitude of each node, and if those do
    not come back as the gridspec's own the ordering is not what this thinks.
    """
    shape = grid["mask"].shape
    with netCDF4.Dataset(path) as src:
        field = np.asarray(src.variables[name][:])
        lon = np.asarray(src.variables["lon"][:])
        lat = np.asarray(src.variables["lat"][:])

    if field.shape[0] != shape[0] * shape[1]:
        sys.exit(f"dirac: {path} has {field.shape[0]} nodes and the grid has "
                 f"{shape[0] * shape[1]}")
    dlon = (lon.reshape(shape) - grid["lon"] + 180.0) % 360.0 - 180.0
    if np.abs(dlon).max() > 1e-6 or np.abs(lat.reshape(shape) - grid["lat"]).max() > 1e-6:
        sys.exit(f"dirac: {path} is not in the gridspec's node order")
    return field.reshape(shape + (field.shape[1],))


def radius(values, spacing, peak):
    """Distance from the dirac at which the response falls to exp(-0.5) of it.

    Walks outward one cell at a time and interpolates linearly across the cell
    where the crossing happens.

    Returns nan for a direction that has no scale to measure, which is not a
    failure: it is what a coast, a domain edge or the sea floor does. Two ways
    that happens, and both have to be nan rather than a number. Running out of
    cells before the crossing is the obvious one. The other is reaching land
    first, which shows up as an abrupt fall to exactly zero and would otherwise
    be read as a very short correlation length. A dirac on a shelf next to a
    coastline has a kernel that is genuinely truncated in that direction, and
    averaging the truncation into the measured radius says the calibration is
    wrong when what is wrong is the ruler.
    """
    if peak <= 0:
        return np.nan
    target = E_HALF * peak
    distance = 0.0
    for step in range(1, len(values)):
        # SOCA writes exactly zero on land, and the field decays smoothly
        # everywhere else, so an exact zero before the crossing is a boundary.
        if values[step] == 0.0:
            return np.nan
        # The width between two cell centres is the mean of their own widths.
        width = 0.5 * (spacing[step - 1] + spacing[step])
        if values[step] < target:
            span = values[step - 1] - values[step]
            fraction = (values[step - 1] - target) / span if span > 0 else 0.0
            return distance + fraction * width
        distance += width
    return np.nan


def judge(point, peak, along, want, depth):
    """What is worth stopping for, as opposed to worth printing."""
    problems = []
    what = f"{point['what']} at {point['lat']:.2f},{point['lon']:.2f}"

    if not np.isfinite(peak) or abs(peak - 1.0) > PEAK_TOLERANCE:
        problems.append(
            f"{what}: the operator returned {peak:.4f} at the dirac and a "
            f"correlation returns 1. Either the normalization was estimated "
            f"from too few iterations or the operator is not the one the "
            f"calibration wrote.")

    measured = [value for value in along.values() if np.isfinite(value)]
    if not measured:
        problems.append(
            f"{what}: the response never falls to exp(-0.5) in any direction, "
            f"so there is no scale to compare. A dirac in an enclosed basin "
            f"smaller than its own correlation length does this.")
    else:
        mean = float(np.mean(measured))
        if abs(mean - want) > RADIUS_TOLERANCE * want:
            problems.append(
                f"{what}: the response has a radius of {mean / 1e3:.1f} km and "
                f"the calibration asked for {want / 1e3:.1f} km.")

    up, down, want_z = depth
    if np.isfinite(want_z):
        measured = [value for value in (up, down) if np.isfinite(value)]
        if measured and abs(np.mean(measured) - want_z) > RADIUS_TOLERANCE * want_z:
            problems.append(
                f"{what}: the vertical response has a radius of "
                f"{np.mean(measured):.2f} levels and the calibration asked for "
                f"{want_z:.2f}.")
    return problems


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
