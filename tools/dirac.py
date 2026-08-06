#!/usr/bin/env python3
"""Plan a dirac test of the diffusion operator, and read back what it did.

    tools/dirac.py plan   <layer> <static> <levels> <metadata> <gridspec> \\
                          <restart> <out.yaml> <points.json> [lat,lon]...
    tools/dirac.py report <gridspec> <static> <points.json> <outdir>

Two verbs in one file because they have to agree about which dirac is which.
The toolbox applies the correlation to a single increment holding every dirac
at once, so `points.json` written by `plan` is the only record of what was
placed where, and both halves read the node ordering of the calibration files
the same way. `tools/soca-dirac.sh` is what calls them.
"""

import json
import os
import re
import sys

import netCDF4
import numpy as np
import yaml

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
    return plan(argv[2:]) if argv[1] == "plan" else report(argv[2:])


# ------------------------------------------------------------------------------
# plan


def plan(argv):
    layer, static, levels, metadata, gridspec, restart = argv[0:6]
    out_yaml, out_points = argv[6:8]
    requested = argv[8:]

    grid = read_gridspec(gridspec)
    depth, deepest_level = read_restart(restart, grid)

    if requested:
        cells = [nearest_ocean(grid, *parse(text)) for text in requested]
    else:
        cells = default_cells(grid, depth, static)

    points = place(grid, cells, deepest_level)
    warn_if_crowded(grid, points, static)

    with open(out_points, "w") as stream:
        json.dump(points, stream, indent=2)
    with open(out_yaml, "w") as stream:
        yaml.safe_dump(document(layer, static, levels, metadata, restart, points),
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
    thick = h > 0.01
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
        read_nodes(f"{static}/diffusion/hz.nc", "hzScales", grid)[..., 0],
        read_nodes(f"{static}/diffusion/hz_ssh.nc", "hzScales", grid)[..., 0],
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


def place(grid, cells, deepest_level):
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
    """
    points = []
    for j, i, why in cells:
        nk = int(deepest_level[j, i])
        if nk < 4:
            sys.exit(f"dirac: the cell chosen for '{why}' is only {nk} levels deep")
        for variable, level in (("sea_water_potential_temperature", nk // 2),
                                ("sea_surface_height_above_geoid", 1)):
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
            "sea_surface_height_above_geoid": "height"}.get(variable, variable)


def warn_if_crowded(grid, points, static):
    """Two diracs in one field close enough to overlap make the report wrong.

    Not fatal: the locations may have been asked for deliberately, and the
    overlap shows up in the report as a peak above 1. Worth saying out loud
    though, because otherwise it reads as a normalization failure.
    """
    scales = read_nodes(f"{static}/diffusion/hz.nc", "hzScales", grid)[..., 0]
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


def document(layer, static, levels, metadata, restart, points):
    """The experiment's own central block, and nothing else from its B.

    Lifted out of the layer rather than written here: a dirac test against a
    second description of the background error would pass while the analysis
    used a different operator. What is dropped is everything outside the central
    block, because the standard deviations and the balance operator turn a
    correlation into a covariance and would leave the peak value meaning
    nothing.
    """
    background_error = yaml.safe_load(open(layer))["solver"]["background error"]
    covariance = substitute({
        "covariance model": background_error["covariance model"],
        "saber central block": background_error["saber central block"],
    }, {"domain_static": static, "diffusion_levels": levels})

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
            "state variables": list(IFDIR),
        },
        "background error": covariance,
        # The toolbox prints the value at every dirac itself, before anything
        # here reads a file. Two independent readings of the peak are worth the
        # one line of output.
        "dirac": {key: [point[key] for point in points]
                  for key in ("ixdir", "iydir", "izdir", "ifdir")},
        "output dirac": {"datadir": DATADIR, "date": DATE,
                         "exp": EXPERIMENT, "type": "an"},
    }


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


def report(argv):
    gridspec, static, points_path, outdir = argv[0:4]
    grid = read_gridspec(gridspec)
    points = json.load(open(points_path))
    increment = read_increment(outdir)

    requested = {
        "sea_water_potential_temperature":
            read_nodes(f"{static}/diffusion/hz.nc", "hzScales", grid)[..., 0],
        "sea_surface_height_above_geoid":
            read_nodes(f"{static}/diffusion/hz_ssh.nc", "hzScales", grid)[..., 0],
    }
    vertical = read_nodes(f"{static}/diffusion/vt.nc", "vtScales", grid)

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
