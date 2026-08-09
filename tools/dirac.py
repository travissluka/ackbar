#!/usr/bin/env python3
"""Plan a dirac test of the background error, and read back what it did.

    tools/dirac.py plan   <layer> <static> <levels> <metadata> <gridspec> \\
                          <restart> <out.yaml> <points.json> [lat,lon]... [--full]
    tools/dirac.py report <gridspec> <static> <points.json> <outdir> [--full]

Two verbs in one file because they have to agree about which dirac is which.
The toolbox applies the operator to a single increment holding every dirac at
once, so `points.json` written by `plan` is the only record of what was placed
where, and both halves read the node ordering of the calibration files the same
way. `tools/soca-dirac.sh` is what calls them.

## Two tests, and they answer different questions

Without `--full` this is a test of the **correlation** alone: the central
diffusion block and nothing else. The response at the dirac is then exactly 1
by definition, so the peak measures the normalization's Monte Carlo error and
the radius measures whether the operator built the scale the calibration
ordered. It is a test of `tools/soca-diffusion.sh` and of nothing in
`config/layers/da/variational.yaml` below the central block.

With `--full` it is a test of the **covariance** the analysis actually uses:
the standard deviations, the depth taper and the balance operator are all in.
Nothing is normalized to anything, and there is nothing to pass or fail. What
it shows is what one observation would do: the size of the increment in the
variable the dirac was placed in, and the size of the increment the balance
operator puts into the other two. A `--full` run writes the standard deviation
fields as well, which is the only way to see what the parametric block built
before it was multiplied by anything.
"""

import json
import os
import re
import sys

import netCDF4
import numpy as np
import yaml

from ackbar.diffusion import THIN_LAYER

#: Fixed by SOCA. `ifdir` indexes a hardcoded table in `soca_increment_mod`, not
#: the increment's own variable list, so these numbers are the same whatever the
#: configuration asks for. See `soca_increment_dirac`.
IFDIR = {"sea_water_potential_temperature": 1,
         "sea_water_salinity": 2,
         "sea_surface_height_above_geoid": 3}

#: The restart variable each of those is written to, which is how the increment
#: comes back off disk. `config/model/mom6sis2/fields_metadata.yaml` owns this
#: mapping; it is repeated here because this reads the file directly.
IO_NAME = {"sea_water_potential_temperature": "Temp",
           "sea_water_salinity": "Salt",
           "sea_surface_height_above_geoid": "ave_ssh"}

DATADIR = "out"
EXPERIMENT = "dirac_%id%"
DATE = "2000-01-01T00:00:00Z"

#: Where `--full` asks `SOCAParametricOceanStdDev` to write the fields it built.
#: A stem: `util::writeFieldSet` appends `.nc`. Beside `out/` rather than in it,
#: because `read_increment` takes the one netCDF file there and a second one
#: would make it ambiguous which is the increment.
DIAGS = "stddev"

#: The units each variable's increment comes back in, for the `--full` report.
#: Only for printing; nothing computes with them.
UNITS = {"sea_water_potential_temperature": "degC",
         "sea_water_salinity": "psu",
         "sea_surface_height_above_geoid": "m"}

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
    rest = [word for word in argv[2:] if word != "--full"]
    full = "--full" in argv[2:]
    return plan(rest, full) if argv[1] == "plan" else report(rest, full)


# ------------------------------------------------------------------------------
# plan


def plan(argv, full=False):
    layer, static, levels, metadata, gridspec, restart = argv[0:6]
    out_yaml, out_points = argv[6:8]
    requested = argv[8:]

    grid = read_gridspec(gridspec)
    depth, deepest_level = read_restart(restart, grid)

    if requested:
        cells = [nearest_ocean(grid, *parse(text)) for text in requested]
    else:
        cells = default_cells(grid, depth, static)

    points = place(grid, cells, deepest_level, full)
    warn_if_crowded(grid, points, static)

    with open(out_points, "w") as stream:
        json.dump(points, stream, indent=2)
    with open(out_yaml, "w") as stream:
        yaml.safe_dump(document(layer, static, levels, metadata, restart, points,
                                full),
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
            "sea_surface_height_above_geoid": "height"}.get(variable, variable)


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


def document(layer, static, levels, metadata, restart, points, full=False):
    """The experiment's own background error, lifted out of the layer.

    Lifted rather than written here: a dirac test against a second description
    of the background error would pass while the analysis used a different
    operator, which is the failure this exists to catch.

    Without *full*, everything outside the central block is dropped, because the
    standard deviations and the balance operator turn a correlation into a
    covariance and would leave the peak value meaning nothing. With *full* they
    are all kept and the peak means something else instead; see the module
    docstring.
    """
    solver = yaml.safe_load(open(layer))["solver"]
    background_error = solver["background error"]
    wanted = {"covariance model": background_error["covariance model"],
              "saber central block": background_error["saber central block"]}
    analysis_variables = solver["analysis variables"]
    state_variables = list(IFDIR)

    if full:
        wanted["saber outer blocks"] = with_diagnostics(
            background_error["saber outer blocks"])
        # `input variables` and `output variables` are absent from the layer on
        # purpose, because `ackbar/soca.py` fills them in from the analysis
        # variables. Nothing here goes through that, so this is the second place
        # that has to know it, and omitting them is not a configuration error
        # that anything reports: oops holds a null pointer and dereferences it
        # the first time it evaluates Jb.
        change = dict(background_error["linear variable change"])
        change["input variables"] = analysis_variables
        change["output variables"] = analysis_variables
        wanted["linear variable change"] = change
        # The outer blocks read fields the central block does not: thickness is
        # what makes anything addressable by depth, and the parametric standard
        # deviations are built out of the mixed layer and the depth.
        state_variables = solver["background variables"]

    covariance = substitute(
        wanted, {"domain_static": static, "diffusion_levels": levels})

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
        # the background's own variable list, which in `--full` is six fields
        # rather than three because the outer blocks read thickness, the mixed
        # layer and the depth. The increment would then carry three fields the
        # linear variable change does not, and SOCA fails an assertion on
        # `Increment::operator=` rather than saying so.
        **({"increment variables": analysis_variables} if full else {}),
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

    Only those two. Anything else surviving is a token this does not know how to
    resolve, and it fails rather than handing `$(...)` to eckit, which would
    read it as a filename.
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


def report(argv, full=False):
    gridspec, static, points_path, outdir = argv[0:4]
    grid = read_gridspec(gridspec)
    points = json.load(open(points_path))
    increment = read_increment(outdir)
    if full:
        return report_full(grid, static, points, increment)

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


def report_full(grid, static, points, increment):
    """What one observation would do, through the covariance as configured.

    Nothing here passes or fails. A correlation returns 1 at its own dirac and
    that is a statement about the operator; a covariance returns the variance,
    and whether that number is right is a question about the ocean that no
    single run can answer. What this is for is seeing the three things a
    configuration change was meant to do:

    * the size of the increment in the variable the dirac went into, which is
      the standard deviation squared where the correlation is 1,
    * the size of the increment the balance operator put into the other two,
      which is the whole of the ssh response once unbalanced ssh error is zero,
    * the standard deviation fields themselves, which the parametric block
      wrote on the way past.
    """
    print()
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
            # The response of a variable the dirac did not go into is the
            # balance operator's, and it can be either sign. The radius is only
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

    print("  * is the variable the dirac was placed in. The other rows are the")
    print("    balance operator, which is the only thing that moves them.")
    diagnostics(static, grid)
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


def read_increment(outdir):
    """The one increment every dirac was applied in, whatever it got called."""
    files = [name for name in sorted(os.listdir(outdir)) if name.endswith(".nc")]
    if len(files) != 1:
        sys.exit(f"dirac: expected one increment in {outdir}, found {len(files)}")
    with netCDF4.Dataset(f"{outdir}/{files[0]}") as src:
        return {name: np.asarray(src.variables[name][0])
                for name in IO_NAME.values() if name in src.variables}


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
