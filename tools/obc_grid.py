#!/usr/bin/env python3
"""The domain half of building `ic.nc` and `obc.nc`: everything that is not the
source the numbers came from.

Imported by `fetch-glorys.py` and `fetch-hycom.py`. What lives here is what has
to be identical between them, because two boundary files that disagree about
where a segment is, how land is filled, or what calendar the time axis is in are
not two estimates of the same thing.

What each fetcher keeps for itself is small and genuinely product specific: how
to open the product, what its variables are called, and any conversion into the
units MOM6 reads. Everything after "here are the values on a source grid" is the
same problem whatever produced them.

---------------------------------------------------------------------------
Segments come from the domain, not from a constant

Where a domain's open boundaries are is stated once, by the domain, in
`OBC_SEGMENT_00N`, and read back here. The alternative, a table of segment
positions in the fetcher, is a second answer to that question and was the first
thing here: three entries, correct for the Gulf, silently wrong for a domain
whose open boundary is the west rather than the north.

Segments on a domain edge (`I=0`, `I=N`, `J=0`, `J=N`) are what this handles. A
segment MOM6 places at an interior index is refused with a message rather than
guessed at: the supergrid row it lands on is a different calculation, and no
domain here has one.

A domain with `OBC_NUMBER_OF_SEGMENTS = 0`, which is every global domain, has no
boundary to build and is told so.

---------------------------------------------------------------------------
Two things that would be silently wrong, and are handled here for both sources

**The calendar.** The model runs `NOLEAP` while every date ACKBAR computes is
proleptic Gregorian. FMS converts a boundary file's time axis using the *file's*
stated calendar and then compares against model time, so a "days since" axis in
a real calendar is a leap day out for every record after one, and nothing reports
it. Every axis written here is `NOLEAP` day counts from the first date, and a
span that would cross a 29 February is refused.

**Land.** No source's coastline is the model's, and the two differ by a cell or
so. A model cell that is ocean where the source is land would interpolate from a
missing value, so every field is flood-filled horizontally per level, from the
nearest wet cell, before it is interpolated or written.
"""

import calendar
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import netCDF4 as nc

ROOT = Path(__file__).resolve().parents[1]

#: What MOM6 is told to treat as missing. Nothing written is ever equal to it,
#: because everything is filled first, but the attribute has to exist:
#: `horiz_interp_and_extrap_tracer` reads it before it reads the data and calls
#: MOM_error(FATAL) if it is absent (`MOM_horizontal_regridding.F90:441`).
MISSING = -1.0e20

#: Degrees of source data pulled beyond the domain edge. The boundary points sit
#: exactly on the domain edge, so without a margin they land on the last source
#: cell and bilinear interpolation has nothing to lean on from outside.
MARGIN = 0.5

#: How far either side of a segment its strip reaches. Wider than MARGIN because
#: a strip is not just an interpolation stencil, it is also everything
#: `fill_horizontally` gets to see. A boundary that runs along a coast has a
#: large share of land points taking their values from the nearest wet cell, and
#: cutting the strip too close changes which cell that is, which changes the
#: answer at the wet points next to them.
STRIP_MARGIN = 2.0


def die(who, message):
    sys.exit(f"{who}: {message}")


def domain_input(domain, who="obc"):
    root = os.environ.get("ACKBAR_STATIC_ROOT")
    if not root:
        die(who, "run `source site/activate.sh` first")
    path = Path(root, "domain", domain, "INPUT")
    if not path.is_dir():
        die(who, f"{path} does not exist")
    return path


def supergrid(domain, who="obc"):
    """The domain's supergrid longitudes and latitudes, `(nyp, nxp)` each."""
    with nc.Dataset(domain_input(domain, who) / "ocean_hgrid.nc") as f:
        return np.array(f["x"][:]), np.array(f["y"][:])


def mom6_parameters(domain, who="obc"):
    """`MOM_input` under `MOM_override`, flattened to what MOM6 resolves.

    The two directories come from `tools/domain-paths.sh` rather than from a
    rule over the domain name, because that is where a domain says what it is
    made of and a second answer here would be free to disagree with the one the
    workflow uses.
    """
    script = (f'source "{ROOT}/tools/domain-paths.sh" && domain_paths "{domain}"'
              ' && printf "%s\\n%s\\n" "$BASE" "$OVERRIDE"')
    # `PYTHON` is this interpreter, not whatever `python3` resolves to. The
    # fetchers run under `.venv-data` with `PYTHONPATH` unset, which the
    # Copernicus toolbox requires and which also takes spack-stack's `yaml` off
    # the search path of the `python3` on PATH. `domain-paths.sh` honours the
    # variable for this one caller.
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=dict(os.environ, ACKBAR_ROOT=str(ROOT),
                                   PYTHON=sys.executable))
    if done.returncode != 0:
        die(who, f"domain-paths failed for {domain}: {done.stderr.strip()}")
    base, override = done.stdout.split()

    values = {}
    for path in (Path(base, "MOM_input"), Path(override, "MOM_override")):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            match = re.match(r"\s*(?:#override\s+)?(\w+)\s*=\s*(.*?)\s*(?:!.*)?$",
                             line)
            if match:
                values[match.group(1)] = match.group(2).strip('"')
    return values


def segment_geometry(domain, sx, sy, who="obc"):
    """Where each of the domain's open segments sits on the supergrid.

    `{name: {"lon": ..., "lat": ..., "shape": (ny, nx)}}`, the names being the
    `00N` MOM6 numbers them with, which is what the variables in `obc.nc` are
    suffixed by.

    A J segment is a line of constant j, so it runs along i and is one supergrid
    row: shape `(1, nxp)`. An I segment is one column: `(nyp, 1)`.
    """
    parameters = mom6_parameters(domain, who)
    count = int(parameters.get("OBC_NUMBER_OF_SEGMENTS", 0))
    if count == 0:
        die(who, f"{domain} sets OBC_NUMBER_OF_SEGMENTS = 0, so it has no open "
                 f"boundary and no obc.nc to build. A global domain is closed; "
                 f"its ensemble spread comes from the atmosphere, not an edge.")

    geometry = {}
    for index in range(1, count + 1):
        name = f"{index:03d}"
        spec = parameters.get(f"OBC_SEGMENT_{name}")
        if not spec:
            die(who, f"{domain} declares {count} segments but no "
                     f"OBC_SEGMENT_{name}")
        match = re.fullmatch(r"([IJ])=(\w+)", spec.split(",")[0].strip())
        if not match:
            die(who, f"OBC_SEGMENT_{name} of {domain} starts with "
                     f"{spec.split(',')[0]!r}, which is not I=<pos> or J=<pos>")
        axis, position = match.groups()
        if position not in ("0", "N"):
            die(who, f"OBC_SEGMENT_{name} of {domain} is at {axis}={position}, "
                     f"an interior index. Only segments on a domain edge (0 or "
                     f"N) are handled here, because an interior segment lands "
                     f"on a supergrid row this does not compute.")
        edge = -1 if position == "N" else 0

        if axis == "J":
            lon, lat, shape = sx[edge, :], sy[edge, :], (1, sx.shape[1])
        else:
            lon, lat, shape = sx[:, edge], sy[:, edge], (sx.shape[0], 1)
        geometry[name] = {"lon": lon, "lat": lat, "shape": shape}
    return geometry


def check_span(start, end, who="obc"):
    """Refuse a span the NOLEAP time axis cannot represent.

    `write_obc` stamps day counts under `calendar = "NOLEAP"`, which agrees with
    the real calendar only while the span contains no 29 February. Crossing one
    shifts every record after it by a day, for the rest of the file, with nothing
    to report it.
    """
    if start.year != end.year:
        die(who, "keep a boundary file inside one year")
    if calendar.isleap(start.year):
        leap_day = dt.datetime(start.year, 2, 29)
        if start <= leap_day <= end:
            die(who, f"{start.year} is a leap year and this span crosses 29 "
                     f"February, which the NOLEAP time axis cannot represent; "
                     f"split it either side of the leap day")


def in_source_longitudes(box, src_lon):
    """A `(west, east, south, north)` box moved onto the source's convention.

    Sources disagree about whether longitude runs -180 to 180 or 0 to 360, and a
    domain in the western hemisphere asks for -98 of a grid that starts at 0 and
    silently gets nothing back. Nothing in the Gulf exercises this and the first
    Pacific or Indian Ocean domain does.
    """
    west, east, south, north = box
    if float(np.min(src_lon)) >= 0.0 and west < 0.0:
        west, east = west % 360.0, east % 360.0
    if float(np.max(src_lon)) <= 180.0 and west > 180.0:
        west, east = west - 360.0, east - 360.0
    return west, east, south, north


def fill_horizontally(field):
    """Replace every masked value with its nearest wet neighbour, per level.

    `field` is `(..., lat, lon)` and may be a masked array. The fill is nearest
    rather than smooth on purpose: it exists to make the interpolation well
    posed a cell or two inside the coast, not to invent an estuary.
    """
    from scipy import ndimage

    values = np.ma.filled(np.ma.masked_invalid(field), np.nan)
    flat = values.reshape(-1, *values.shape[-2:])
    out = np.empty_like(flat)
    for k, plane in enumerate(flat):
        missing = np.isnan(plane)
        if not missing.any():
            out[k] = plane
            continue
        if missing.all():
            #   An entirely land level, which is not hypothetical: a source's
            # deepest level is often shallower than the model's deepest column,
            # so the bottom level of a box can have no wet cell in it at all
            # while the model very much wants a value there.
            #
            #   It takes the level above rather than a constant. Zero was the
            # first thing here and it is badly wrong: 0 psu under 35 psu is a
            # density inversion the model would convect away on the first step.
            # Copying down extends the deepest real water instead, which is what
            # any sane extrapolation of an abyssal profile gives.
            #
            #   `flat` runs depth fastest inside each time, so `out[k - 1]` is
            # the level above. The surface is never all land, so k == 0 here
            # would mean the whole box is dry.
            out[k] = out[k - 1] if k else 0.0
            continue
        _, nearest = ndimage.distance_transform_edt(missing, return_indices=True)
        out[k] = plane[tuple(nearest)]
    return out.reshape(values.shape)


def layer_thicknesses(depths):
    """Thicknesses from level centres, by putting interfaces at the midpoints.

    Both sources publish centres. MOM6 wants a `dz_` beside every segment field
    and remaps the source column onto its own coordinate with it, so the numbers
    have to be thicknesses that stack up to the centres they came from.
    """
    interfaces = np.empty(len(depths) + 1)
    interfaces[0] = 0.0
    interfaces[1:-1] = 0.5 * (depths[:-1] + depths[1:])
    interfaces[-1] = depths[-1] + (depths[-1] - interfaces[-2])
    return np.diff(interfaces)


def interpolate_to(values, src_lon, src_lat, lon, lat):
    """Bilinear from the source grid onto a list of points.

    `values` is `(..., lat, lon)` already filled, `lon`/`lat` are flat arrays of
    target points. Returns `(..., npoints)`.
    """
    from scipy.interpolate import RegularGridInterpolator

    src_lon = np.asarray(src_lon, float)
    lon = np.asarray(lon, float).ravel()
    # Match the target onto whichever branch cut the source uses, for the same
    # reason `in_source_longitudes` moves the request box.
    if src_lon.min() >= 0.0:
        lon = lon % 360.0
    elif src_lon.max() <= 180.0:
        lon = ((lon + 180.0) % 360.0) - 180.0

    points = np.column_stack([np.asarray(lat, float).ravel(), lon])
    flat = values.reshape(-1, *values.shape[-2:])
    out = np.empty((flat.shape[0], points.shape[0]))
    for k, plane in enumerate(flat):
        interp = RegularGridInterpolator(
            (np.asarray(src_lat, float), src_lon), plane, method="linear",
            bounds_error=False, fill_value=None,  # extrapolate at the edge
        )
        out[k] = interp(points)
    return out.reshape(*values.shape[:-2], points.shape[0])


def sample_onto(geo, fields, src_lon, src_lat):
    """Fill and interpolate every field of one strip onto one segment's points."""
    return {name: interpolate_to(fill_horizontally(np.asarray(values)),
                                 src_lon, src_lat, geo["lon"], geo["lat"])
            for name, values in fields.items()}


def write_ic(path, lon, lat, depth, fields, when, source, valid_at=None,
             comment=None):
    """`ic.nc`: temperature and salinity on the source's own grid and levels.

    Deliberately *not* regridded to the model grid. MOM6 does its own horizontal
    interpolation and extrapolation during `initialize_temp_salt_from_Z_file`,
    and doing it twice only adds a smoothing pass. What matters is that nothing
    is missing, which `fill_horizontally` guarantees.

    *when* is the date the state is asserted to be an estimate of. *fields* is
    `{"temp": ..., "salt": ...}` already in degrees C and psu.
    """
    lon, lat, depth = (np.asarray(a) for a in (lon, lat, depth))
    with nc.Dataset(path, "w", format="NETCDF3_64BIT_OFFSET") as f:
        f.createDimension("time", None)
        f.createDimension("depth", len(depth))
        f.createDimension("lat", len(lat))
        f.createDimension("lon", len(lon))

        t = f.createVariable("time", "f8", ("time",))
        t.units = f"days since {when:%Y-%m-%d} 00:00:00"
        t.calendar = "NOLEAP"
        t.cartesian_axis = "T"
        t[0] = 0.0

        z = f.createVariable("depth", "f8", ("depth",))
        z.units, z.positive, z.cartesian_axis = "meters", "down", "Z"
        z[:] = depth

        y = f.createVariable("lat", "f8", ("lat",))
        y.units, y.cartesian_axis = "degrees_north", "Y"
        y[:] = lat

        x = f.createVariable("lon", "f8", ("lon",))
        x.units, x.cartesian_axis = "degrees_east", "X"
        x[:] = lon

        for name, values in fields.items():
            v = f.createVariable(name, "f4", ("time", "depth", "lat", "lon"),
                                 fill_value=MISSING)
            v.missing_value = MISSING
            v.units = "degrees C" if name == "temp" else "psu"
            v.long_name = ("Potential temperature" if name == "temp"
                           else "Practical Salinity")
            v[:] = fill_horizontally(np.asarray(values))

        f.source = source
        # `valid_at` is read by people rather than by code, and
        # `tools/ensemble-ic-lagged.sh` names the `ncdump | grep valid_at` that
        # tells you whether a domain is holding the initial condition it should
        # be. Written only when it differs from the day the state came from, so
        # its presence is itself the signal that the file is asserting something.
        if valid_at:
            f.valid_at = f"{valid_at:%Y-%m-%d}"
        if comment:
            f.comment_on_date = comment
        f.comment = ("Land flood-filled horizontally per level so that MOM6 "
                     "never interpolates from a missing value.")


def write_obc(path, segments, depth, dates, geometry, source):
    """`obc.nc`: every segment, every field, on the supergrid.

    One `time` axis shared by all of them, in `NOLEAP` days from the first date.
    `BRUSHCUTTER_MODE` requires segment data on the supergrid and dies on an
    even dimension, so a J segment is `(1, 2*ni+1)` and an I segment
    `(2*nj+1, 1)`, taken straight off `ocean_hgrid.nc`.
    """
    dz = layer_thicknesses(depth)
    nz = len(depth)

    with nc.Dataset(path, "w", format="NETCDF3_64BIT_OFFSET") as f:
        f.createDimension("time", None)
        t = f.createVariable("time", "f8", ("time",))
        t.units = f"days since {dates[0]:%Y-%m-%d} 00:00:00"
        t.calendar = "NOLEAP"
        t.cartesian_axis = "T"
        t[:] = [(d - dates[0]).days for d in dates]

        for name, geo in geometry.items():
            ny, nx = geo["shape"]
            f.createDimension(f"nx_segment_{name}", nx)
            f.createDimension(f"ny_segment_{name}", ny)
            dims = (f"ny_segment_{name}", f"nx_segment_{name}")

            for axis, values in (("lon", geo["lon"]), ("lat", geo["lat"])):
                v = f.createVariable(f"{axis}_segment_{name}", "f8", dims)
                v.units = "degrees_east" if axis == "lon" else "degrees_north"
                v[:] = values.reshape(ny, nx)

            for short, sampled in segments[name].items():
                if short == "zeta":
                    v = f.createVariable(f"zeta_segment_{name}", "f4",
                                         ("time",) + dims)
                    v.units = "meters"
                    v[:] = sampled.reshape(len(dates), ny, nx)
                    continue

                zdim = f"nz_segment_{name}_{short}"
                if zdim not in f.dimensions:
                    f.createDimension(zdim, nz)
                    index = f.createVariable(zdim, "f8", (zdim,))
                    index.cartesian_axis = "Z"
                    index[:] = np.arange(nz)

                full = ("time", zdim) + dims
                block = sampled.reshape(len(dates), nz, ny, nx)

                v = f.createVariable(f"{short}_segment_{name}", "f4", full)
                v.units = {"temp": "degrees C", "salt": "psu"}.get(short, "m s-1")
                v[:] = block

                # MOM6 reads this one to remap the source column onto its own
                # coordinate, and dies if its shape differs from the field's.
                thickness = f.createVariable(f"dz_{short}_segment_{name}", "f4",
                                             full)
                thickness.units = "meters"
                thickness[:] = np.broadcast_to(dz[None, :, None, None],
                                               block.shape)

        f.source = source
        f.comment = ("Supergrid, as BRUSHCUTTER_MODE requires. Time axis is "
                     "NOLEAP to match the model's coupler_nml calendar.")
