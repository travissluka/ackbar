#!/usr/bin/env python3
"""Build a domain's initial condition and open boundary conditions from GLORYS.

    env -u PYTHONPATH .venv-data/bin/python tools/fetch-glorys.py ic  <domain> <date>
    env -u PYTHONPATH .venv-data/bin/python tools/fetch-glorys.py obc <domain> <start> <end>

Writes `ic.nc` and `obc.nc` into `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/`,
which is where `TEMP_SALT_Z_INIT_FILE` and `OBC_SEGMENT_00N_DATA` look.

**`.venv-data/bin/python`, not `.venv/bin/python`, and with `PYTHONPATH`
unset.** The Copernicus toolbox needs a `typing_extensions` newer than the one
spack-stack puts on `PYTHONPATH`, and the project venv inherits that.
`.venv-data` is built with `env -u PYTHONPATH` for exactly that reason, and the
same has to hold when it is *run*: a virtual environment does not shadow
`PYTHONPATH`, so naming the interpreter is not by itself enough once
`site/activate.sh` has been sourced. And it has been, because that is where
`ACKBAR_STATIC_ROOT` comes from. Hence the `env -u` above.

Without it the failure is an `ImportError` on `typing_extensions.Sentinel`,
raised from inside pydantic several frames below anything recognisable, after
the argument parsing has succeeded and before anything is downloaded.

Needs `copernicusmarine login` to have been run once; it leaves credentials in
`~/.copernicusmarine/`.

## Why GLORYS, and why both halves from it

What was here before was SODA3.3.1: a single 5-day mean centred 2015-01-04, at
half a degree, for *both* files. The initial condition being a single snapshot
is normal. The boundary being one is not: `obc.nc` carried `time = 1`, so the
Yucatan Channel inflow that drives the entire Loop Current was frozen at one
January state for every run this repository has ever done, in July or any other
month.

GLORYS12V1 is 1/12 degree, 50 levels, daily, 1993 onward. It is a NEMO
reanalysis assimilating altimetry, SST and in-situ, and it is what the regional
MOM6 world nests off.

Both files come from the same product on purpose. An open boundary and an
interior drawn from different analyses disagree about sea surface height by a
constant offset, and under FLATHER that offset is a barotropic pressure gradient
the model will happily accelerate a current down. They agree here because they
are the same field sampled in two places.

## Two things that would be silently wrong

**The calendar.** The model runs `NOLEAP` (`coupler_nml` in the domain's
`input.nml`) while every date ACKBAR computes is proleptic Gregorian, which
`ackbar.mom6sis2` already documents as disagreeing from March of a leap year
onwards. The same disagreement reaches the boundary file through FMS, which
converts the file's time axis using the *file's* stated calendar and then
compares against model time. A "days since 1980-01-01" axis with a real
calendar is nine leap days away from where a NOLEAP model thinks it is, and
nothing reports that: the run simply reads the wrong day's boundary.

So the axis written here is `days since <the first date requested>` with
`calendar = "NOLEAP"`, and the values are plain day counts. Within one non-leap
year the two calendars agree on day-of-year, so the counts are unambiguous, and
the epoch is close enough that no leap day falls between it and the data.

**Land.** GLORYS masks land, our bathymetry is not GLORYS's bathymetry, and the
two coastlines differ by a cell or so in places. Where the model has ocean and
GLORYS has land, an unfilled source leaves MOM6 interpolating from a missing
value. Every field is therefore flood-filled horizontally, per level, from the
nearest wet cell before anything is interpolated or written, which is what the
`cdo -fillmiss` in the old SODA file's history was doing.

## The shape of obc.nc

`BRUSHCUTTER_MODE = True` in `gom/common/MOM_input`, so MOM6 requires segment
data on the **supergrid** and dies on an even dimension. A J-segment is
therefore `(1, 2*ni+1)` and an I-segment `(2*nj+1, 1)`, taken straight off
`ocean_hgrid.nc`.

MOM6 reads `<var>_segment_00N` and `dz_<var>_segment_00N` and nothing else
(`MOM_open_boundary.F90`, `setup_segment_data`). The `vc_` variables in the
file this replaces are decorative, and are not written here: they are a third
variable of the same shape as the two that are read, so they cost a third of
the file, which is not what "free" means at a hundred days of daily boundary
data. `dz_` is the same value at every point and every time and is written in
full anyway, because MOM6 dies if its shape differs from the field's.

Data is single precision. The source is a reanalysis of the ocean, not a
checkpoint to restart from bit-for-bit, and seven significant figures is
already more than GLORYS knows. Coordinates and the time axis stay double,
where the extra bytes are negligible and a rounded longitude would move a
segment. This matters beyond disk: the file is `NETCDF3_64BIT_OFFSET`, which
caps a single variable at 4 GB, and double precision reaches that in half the
days single does.

Segments are always stored in increasing i or j order, whatever direction the
`OBC_SEGMENT_00N` string declares. Segment 001 is `I=N:0`, reversed, and its
data is still written low index first, the same as 003.
"""

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import netCDF4 as nc

#: The three open segments, as `gom/common/MOM_input` declares them. `edge`
#: says which supergrid row or column each one lies on, and `axis` which of the
#: two supergrid dimensions runs along it.
#:
#:   001  "J=N,I=N:0"   northern boundary, top supergrid row
#:   002  "I=N,J=0:N"   eastern boundary, last supergrid column
#:   003  "J=0,I=0:N"   southern boundary, bottom supergrid row
#:
#: The western boundary is closed; it is the Mexican and Texan coast.
SEGMENTS = {
    "001": {"axis": "i", "edge": -1},
    "002": {"axis": "j", "edge": -1},
    "003": {"axis": "i", "edge": 0},
}

#: GLORYS names against MOM6's. `thetao` really is potential temperature, which
#: is what `Z_INIT_FILE_PTEMP_VAR` wants, and `so` really is practical
#: salinity, so neither needs converting.
GLORYS_IC = {"thetao": "temp", "so": "salt"}
GLORYS_OBC = {"thetao": "temp", "so": "salt", "uo": "u", "vo": "v", "zos": "zeta"}

DATASET = "cmems_mod_glo_phy_my_0.083deg_P1D-m"

#: What MOM6 is told to treat as missing. Nothing in either file is ever set
#: to it, because everything is filled before it is written, but the attribute
#: has to exist: see `write_ic`.
MISSING = -1.0e20

#: Degrees of GLORYS pulled beyond the domain edge. The boundary points sit
#: exactly on the domain edge, so without a margin they land on the last source
#: cell and bilinear interpolation has nothing to lean on from outside.
MARGIN = 0.5

#: How far either side of a segment its strip reaches. Wider than MARGIN
#: because a strip is not just an interpolation stencil, it is also everything
#: `fill_horizontally` gets to see. The northern boundary runs along 32N,
#: which is mostly Texas, Louisiana and Georgia, so a large share of its points
#: are land and get their values from the nearest wet cell. Cut the strip too
#: close and that donor changes, which changes the answer at the wet points
#: next to them. Two degrees is 24 GLORYS cells, and holds peak memory to
#: about a quarter of the full box since segments are processed one at a time.
STRIP_MARGIN = 2.0


def domain_input(domain):
    root = os.environ.get("ACKBAR_STATIC_ROOT")
    if not root:
        sys.exit("fetch-glorys: run `source site/activate.sh` first")
    path = Path(root, "domain", domain, "INPUT")
    if not path.is_dir():
        sys.exit(f"fetch-glorys: {path} does not exist")
    return path


def supergrid(domain):
    """The domain's supergrid longitudes and latitudes, `(nyp, nxp)` each."""
    with nc.Dataset(domain_input(domain) / "ocean_hgrid.nc") as f:
        return np.array(f["x"][:]), np.array(f["y"][:])


def fetch(variables, box, start, end):
    """A GLORYS subset, loaded into memory.

    Imported here rather than at module scope so that `--help` works under a
    python that does not have the toolbox.
    """
    import copernicusmarine

    west, east, south, north = box
    data = copernicusmarine.open_dataset(
        dataset_id=DATASET,
        variables=list(variables),
        minimum_longitude=west, maximum_longitude=east,
        minimum_latitude=south, maximum_latitude=north,
        start_datetime=start.strftime("%Y-%m-%dT00:00:00"),
        end_datetime=end.strftime("%Y-%m-%dT00:00:00"),
    )
    return data.load()


def fill_horizontally(field):
    """Replace every masked value with its nearest wet neighbour, per level.

    `field` is `(..., lat, lon)` and may be a masked array. Land is filled
    rather than left missing because the model's coastline is not GLORYS's, and
    a model cell that is ocean where GLORYS is land would otherwise interpolate
    from nothing. The fill is nearest rather than smooth on purpose: it exists
    to make the interpolation well posed a cell or two inside the coast, not to
    invent an estuary.
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
            #   An entirely land level, which is not hypothetical: GLORYS's
            # deepest level is 5727 m and our bathymetry reaches 7642 m in the
            # Cayman Basin, so the bottom level of this box has no wet cell in
            # it at all while the model very much wants a value there.
            #
            #   It takes the level above rather than a constant. Zero was the
            # first thing here and it is badly wrong: 0 psu under 35 psu is a
            # density inversion the model would convect away on the first
            # step. Copying down extends the deepest real water instead, which
            # is what any sane extrapolation of an abyssal profile gives.
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

    GLORYS publishes centres. MOM6 wants a `dz_` beside every segment field and
    remaps the source column onto its own coordinate with it, so the numbers
    have to be thicknesses that stack up to the centres they came from.
    """
    interfaces = np.empty(len(depths) + 1)
    interfaces[0] = 0.0
    interfaces[1:-1] = 0.5 * (depths[:-1] + depths[1:])
    interfaces[-1] = depths[-1] + (depths[-1] - interfaces[-2])
    return np.diff(interfaces)


def interpolate_to(values, src_lon, src_lat, lon, lat):
    """Bilinear from the GLORYS grid onto a list of points.

    `values` is `(..., lat, lon)` already filled, `lon`/`lat` are flat arrays of
    target points. Returns `(..., npoints)`.
    """
    from scipy.interpolate import RegularGridInterpolator

    points = np.column_stack([np.asarray(lat).ravel(), np.asarray(lon).ravel()])
    flat = values.reshape(-1, *values.shape[-2:])
    out = np.empty((flat.shape[0], points.shape[0]))
    for k, plane in enumerate(flat):
        interp = RegularGridInterpolator(
            (src_lat, src_lon), plane, method="linear",
            bounds_error=False, fill_value=None,  # extrapolate at the edge
        )
        out[k] = interp(points)
    return out.reshape(*values.shape[:-2], points.shape[0])


def write_ic(path, data, when):
    """`ic.nc`: temperature and salinity on GLORYS's own grid and levels.

    Deliberately *not* regridded to the model grid. MOM6 does its own
    horizontal interpolation and extrapolation during
    `initialize_temp_salt_from_Z_file`, and doing it twice only adds a
    smoothing pass. What matters is that nothing is missing, which
    `fill_horizontally` guarantees.
    """
    lon = np.asarray(data["longitude"])
    lat = np.asarray(data["latitude"])
    depth = np.asarray(data["depth"])

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
        z.units = "meters"
        z.positive = "down"
        z.cartesian_axis = "Z"
        z[:] = depth

        y = f.createVariable("lat", "f8", ("lat",))
        y.units = "degrees_north"
        y.cartesian_axis = "Y"
        y[:] = lat

        x = f.createVariable("lon", "f8", ("lon",))
        x.units = "degrees_east"
        x.cartesian_axis = "X"
        x[:] = lon

        for source, name in GLORYS_IC.items():
            field = fill_horizontally(np.asarray(data[source]))
            # `_FillValue` is not optional and its absence is not a warning:
            # `horiz_interp_and_extrap_tracer` reads it before it reads the
            # data and calls MOM_error(FATAL) if the attribute is missing
            # (MOM_horizontal_regridding.F90:441). Nothing here is ever equal
            # to it, since the field is already filled, which is the point:
            # MOM6 finds no missing data and does no second extrapolation.
            v = f.createVariable(name, "f4", ("time", "depth", "lat", "lon"),
                                 fill_value=MISSING)
            v.missing_value = MISSING
            v.units = "degrees C" if name == "temp" else "psu"
            v.long_name = ("Potential temperature" if name == "temp"
                           else "Practical Salinity")
            v[:] = field

        f.source = f"GLORYS12V1 {DATASET}, {when:%Y-%m-%d}"
        f.comment = ("Land flood-filled horizontally per level so that MOM6 "
                     "never interpolates from a missing value.")


def segment_geometry(sx, sy):
    """Where each segment's points sit on the supergrid."""
    geometry = {}
    for name, spec in SEGMENTS.items():
        if spec["axis"] == "i":
            lon, lat = sx[spec["edge"], :], sy[spec["edge"], :]
            shape = (1, len(lon))
        else:
            lon, lat = sx[:, spec["edge"]], sy[:, spec["edge"]]
            shape = (len(lon), 1)
        geometry[name] = {"lon": lon, "lat": lat, "shape": shape}
    return geometry


def sample_segment(geo, start, end):
    """Every OBC field along one segment, for one date range.

    Fetched as a strip around the segment rather than over the whole domain.
    A season of the full box is about 8 GB in memory and 14 times more data
    than three boundaries need; a strip is a tenth of that and each segment
    is independent, so nothing is held that is not about to be used.
    """
    box = (geo["lon"].min() - STRIP_MARGIN, geo["lon"].max() + STRIP_MARGIN,
           geo["lat"].min() - STRIP_MARGIN, geo["lat"].max() + STRIP_MARGIN)
    data = fetch(GLORYS_OBC, box, start, end)
    src_lon = np.asarray(data["longitude"])
    src_lat = np.asarray(data["latitude"])

    sampled = {}
    for source, short in GLORYS_OBC.items():
        if source not in data:
            continue
        filled = fill_horizontally(np.asarray(data[source]))
        sampled[short] = interpolate_to(filled, src_lon, src_lat,
                                        geo["lon"], geo["lat"])
    dates = [dt.datetime.utcfromtimestamp(np.datetime64(t, "s").astype("int64"))
             for t in np.asarray(data["time"])]
    return sampled, np.asarray(data["depth"]), dates


def write_obc(path, segments, depth, dates, geometry):
    """`obc.nc`: every segment, every field, on the supergrid.

    One `time` axis shared by all of them, in `NOLEAP` days from the first
    date, for the reason in the module docstring.
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
                thickness = f.createVariable(f"dz_{short}_segment_{name}", "f4", full)
                thickness.units = "meters"
                thickness[:] = np.broadcast_to(
                    dz[None, :, None, None], block.shape)

        f.source = (f"GLORYS12V1 {DATASET}, "
                    f"{dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d}")
        f.comment = ("Supergrid, as BRUSHCUTTER_MODE requires. Time axis is "
                     "NOLEAP to match the model's coupler_nml calendar.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="what", required=True)

    one = sub.add_parser("ic", help="one snapshot, for TEMP_SALT_Z_INIT_FILE")
    one.add_argument("domain")
    one.add_argument("date", help="YYYY-MM-DD")

    many = sub.add_parser("obc", help="a time series, for the open boundaries")
    many.add_argument("domain")
    many.add_argument("start", help="YYYY-MM-DD")
    many.add_argument("end", help="YYYY-MM-DD, inclusive")

    args = parser.parse_args()
    out = domain_input(args.domain)
    sx, sy = supergrid(args.domain)
    box = (sx.min() - MARGIN, sx.max() + MARGIN,
           sy.min() - MARGIN, sy.max() + MARGIN)

    if args.what == "ic":
        when = dt.datetime.strptime(args.date, "%Y-%m-%d")
        print(f"fetch-glorys: {args.domain} initial condition at {args.date}")
        data = fetch(GLORYS_IC, box, when, when)
        target = out / "ic.nc"
        write_ic(target.with_suffix(".nc.partial"), data, when)
        target.with_suffix(".nc.partial").rename(target)
        print(f"fetch-glorys: wrote {target}")
    else:
        start = dt.datetime.strptime(args.start, "%Y-%m-%d")
        end = dt.datetime.strptime(args.end, "%Y-%m-%d")
        if start.year != end.year:
            # The NOLEAP epoch trick in write_obc holds within one non-leap
            # year and stops holding across a February.
            sys.exit("fetch-glorys: keep a boundary file inside one year")
        print(f"fetch-glorys: {args.domain} boundaries {args.start} to {args.end}")
        geometry = segment_geometry(sx, sy)
        segments, depth, dates = {}, None, None
        for name, geo in geometry.items():
            print(f"fetch-glorys:   segment {name}")
            segments[name], depth, dates = sample_segment(geo, start, end)
        target = out / "obc.nc"
        write_obc(target.with_suffix(".nc.partial"),
                  segments, depth, dates, geometry)
        target.with_suffix(".nc.partial").rename(target)
        print(f"fetch-glorys: wrote {target} with {len(dates)} records")


if __name__ == "__main__":
    main()
