"""Atmospheric forcing: the one file shape every source is normalized into.

ACKBAR's forcing archive holds one `atm.nc` per member per source, clipped to a
padded box around the domain's own grid and carrying the seven fields
MOM6-SIS2's data table reads. Sources differ in almost everything upstream of
this file and in
nothing downstream of it, which is the point: **the data table does not vary by
source.** One `data_table` names `INPUT/atm.nc` and the seven variables below,
and swapping ERA5 for GEFS is swapping which file the symlink points at.

That normalization is what keeps the per-member staging mechanism dumb, and it
is why there is no `data_table.<source>`: one `data_table.atm` serves every
source, and a member selects a *file* through `ensemble.inputs` rather than a
config variant. What a source does still set is `model.override.SIS_forcing`, a
fourth SIS parameter file holding `ADD_DIURNAL_SW = False`, because how often a
source reports shortwave is a property of the source. It is a separate file
rather than a line in the domain's `SIS_override` because that file carries
`NIGLOBAL` and `NJGLOBAL`, which must not be replaced.

## The field names are soca-science's

`T2 Q2 U10 V10 DSWRF DLWRF PRATE`, which is what both prior workflows used and
what `config/model/mom6sis2/domain/gom/common/data_table.atm` already names. No
reason to rename them, and one reason not to: a data table copied from either
prior workflow keeps working.

## Surface pressure is fetched and not written

MOM6 here applies no atmospheric pressure load. The data table sets `p_surf` and
`p_bot` to a constant 1e5, both prior workflows did the same, and nothing in the
gom `MOM_input` reads a real one. So surface pressure is read from the source
where a humidity conversion needs it and then dropped rather than archived. If a
pressure load is ever wanted, that is a refetch, and the refetch is cheap.

## The calendar is the model's, not the source's

`coupler_nml` runs `NOLEAP`. FMS resolves a forcing file's time axis with the
calendar the file names, so a file stamped `julian` against a `NOLEAP` model is
offset by every leap day since year zero, which is about five hundred days and
fails loudly rather than subtly. Both prior workflows ran `julian` on both sides
and were consistent for that reason, not because `julian` is right here.

`NOLEAP` has no 29 February, so an archive spanning one is a real hazard: the
source has a day the model cannot name. `assert_no_leap_day` refuses to build one
rather than letting the dates slide by a day from March onwards.

## Every field carries its own time axis

`time_T2`, `time_DSWRF`, and so on, each an unlimited dimension of its own, which
netCDF4 permits and netCDF3 does not. That is soca-science's shape and it ran
that way for years, so it is proven against FMS rather than inferred.

It matters because instantaneous fields and interval means are honestly stamped
at different times. Temperature at 12:00 is the temperature at 12:00; downwelling
shortwave "at 12:00" is the mean over an hour and belongs at its midpoint. Forced
onto one axis, one of the two has to be interpolated, and it is always the
shortwave that pays: `tools/spike-gefs-atm.py` says so in its own docstring and
smears the diurnal cycle it was built to resolve. With per-field axes neither is
interpolated and neither is wrong.
"""

import datetime
import os
from pathlib import Path

import netCDF4
import numpy as np

#: Degrees of forcing pulled beyond the domain edge.
#:
#: The margin is not decoration. FMS interpolates the forcing onto the model grid
#: with `bilinear` and `bicubic`, and a bicubic stencil reaches two source cells
#: past the point it is filling, so a box ending at the domain edge leaves the
#: outermost row of the model reading from nothing. Four degrees is sixteen ERA5
#: cells and four cells of the coarsest GEFS era, which is the one that sets the
#: floor. `fetch-glorys.py` makes the same argument for the same reason and calls
#: it `MARGIN` too.
MARGIN = 4.0

#: Output name -> (units, long name). The order is the order they are written.
FIELDS = {
    "T2":    ("K",             "2 metre temperature"),
    "Q2":    ("kg kg-1",       "2 metre specific humidity"),
    "U10":   ("m s-1",         "10 metre eastward wind"),
    "V10":   ("m s-1",         "10 metre northward wind"),
    "DSWRF": ("W m-2",         "surface downwelling shortwave radiation flux"),
    "DLWRF": ("W m-2",         "surface downwelling longwave radiation flux"),
    "PRATE": ("kg m-2 s-1",    "total precipitation rate"),
}

#: What the model's `coupler_nml` runs. See the module docstring.
CALENDAR = "NOLEAP"


def assert_no_leap_day(start, end):
    """Refuse a span containing 29 February, which the model cannot name."""
    for year in range(start.year, end.year + 1):
        try:
            leap = datetime.datetime(year, 2, 29)
        except ValueError:
            continue
        if start <= leap <= end:
            raise SystemExit(
                f"forcing: {start:%Y-%m-%d} to {end:%Y-%m-%d} contains "
                f"{leap:%Y-%m-%d}, which does not exist in the model's NOLEAP "
                f"calendar. Choose a span inside one non-leap year, or teach "
                f"the model a calendar that has the day.")


def specific_humidity(dewpoint, pressure):
    """Specific humidity from dewpoint temperature and surface pressure.

    ECMWF's own saturation formulation over water (IFS documentation, the Tetens
    form Buck fitted), applied at the dewpoint, where by definition the vapour
    pressure equals the saturation vapour pressure.

    Over water rather than the mixed water-and-ice form on purpose: the box is
    the Gulf of Mexico in summer and the ice branch would never be taken, so the
    simpler expression is the honest one to read.

    *dewpoint* in K, *pressure* in Pa, result in kg/kg.
    """
    vapour = 611.21 * np.exp(17.502 * (dewpoint - 273.16) / (dewpoint - 32.19))
    ratio = 0.621981  # Rdry / Rvap
    return ratio * vapour / (pressure - (1.0 - ratio) * vapour)


def domain_box(domain, margin=MARGIN):
    """The clip box for *domain*, read from its supergrid.

    Not a constant, because a constant is a second statement of where a domain is
    and the two drift. `ocean_hgrid.nc` is where a domain says what it covers,
    every offline stage that needs an extent already reads it (`fetch-glorys.py`
    builds its GLORYS request the same way), and a domain whose grid moves gets a
    forcing box that moves with it.

    The corollary is that **the box belongs to a domain, not to a family**, so the
    archive is keyed by domain: the four Gulf resolutions do not have quite the
    same footprint (`gom_25km` starts at 17.995N, `gom_12km` at 18.058N) and
    pretending one fetch serves all four means one of them silently reads a box
    that stops inside it.

    Returns the box in the source's own coordinates: degrees east on [0, 360) and
    degrees north.
    """
    root = os.environ.get("ACKBAR_STATIC_ROOT")
    if not root:
        raise SystemExit("forcing: run `source site/activate.sh` first")
    path = Path(root, "domain", domain, "INPUT", "ocean_hgrid.nc")
    if not path.exists():
        raise SystemExit(
            f"forcing: {path} does not exist, so {domain}'s extent is unknown. "
            f"The domain's grid is built by its own offline stage and has to "
            f"exist before forcing can be clipped to it.")
    with netCDF4.Dataset(path) as f:
        return box_around(np.asarray(f["x"][:]), np.asarray(f["y"][:]), margin)


def lon_extent(lon):
    """(west, east) in [0, 360) covering *lon*, going the short way round.

    `min` and `max` cannot do this, and the difference is not academic. A grid
    that wraps through zero holds both 0.1 and 359.9, so its min/max extent is
    the whole globe: a Gulf of Guinea domain from 350E to 10E, or any domain
    straddling the antimeridian on a [-180, 180) grid, would be classified as
    global and fetched as one, which is 290 MB of ERA5 becoming about 100 GB.

    The extent is the complement of the largest gap between adjacent
    longitudes. For a grid that genuinely covers the globe that gap is one cell
    and the extent is everything; for a wrapped regional grid it is the ocean of
    longitudes the domain does not touch, and the returned west is greater than
    the returned east.
    """
    values = np.unique(np.asarray(lon).ravel() % 360.0)
    if values.size < 2:
        return float(values[0]), float(values[0])
    gaps = np.diff(values)
    across_zero = 360.0 - (values[-1] - values[0])
    if across_zero >= gaps.max():
        return float(values[0]), float(values[-1])
    cut = int(np.argmax(gaps))
    return float(values[cut + 1]), float(values[cut])


def box_around(lon, lat, margin=MARGIN):
    """A padded box around supergrid coordinates *lon*, *lat*, in [0, 360).

    Takes the extent rather than the corners, so a curvilinear or rotated grid
    gets the box that contains it rather than a box through four of its points.
    """
    west, east = lon_extent(lon)
    covered = (east - west) % 360.0
    south, north = float(lat.min()) - margin, float(lat.max()) + margin
    if covered + 2.0 * margin >= 360.0:
        west, east = 0.0, 360.0
    else:
        west, east = (west - margin) % 360.0, (east + margin) % 360.0
        if west >= east:
            # A box straddling where the source's longitude axis wraps is two
            # slices, not one, and every caller here reads one. Refused rather
            # than half-handled: a global domain takes the branch above, and no
            # regional domain in this repository crosses it.
            raise SystemExit(
                f"forcing: the box {west:.3f} to {east:.3f} east crosses the "
                f"source's longitude seam, which the single-slice clip cannot "
                f"express. A domain that needs this needs two reads joined.")
    south, north = max(south, -90.0), min(north, 90.0)
    return {"west": west, "east": east, "south": south, "north": north}


def box_slices(lons, lats, box):
    """Where *box* sits in a source grid, as slices, plus the axes it selects.

    Returns `(slice_y, slice_x, y, x, flip_y)`. Slices rather than index arrays
    so that a caller can read the box straight out of a netCDF variable instead
    of reading the globe and throwing it away: an ERA5 month is 1.5 GB globally
    and 15 MB over the Gulf, and the difference is whether this runs in memory.

    *flip_y* says the source hands latitude down from the pole. Sources do that
    about as often as not, and reading a descending axis as ascending flips the
    field about the equator, which in this box is a plausible looking wind
    blowing the wrong way rather than an error.
    """
    keep_x = np.where((lons >= box["west"]) & (lons <= box["east"]))[0]
    keep_y = np.where((lats >= box["south"]) & (lats <= box["north"]))[0]
    if keep_x.size == 0 or keep_y.size == 0:
        raise SystemExit(
            f"forcing: the source grid does not reach {box}; it spans "
            f"{lons.min():.2f} to {lons.max():.2f} east and "
            f"{lats.min():.2f} to {lats.max():.2f} north")
    # Contiguous by construction here: the box does not cross the prime meridian
    # in a 0-360 source, and no source in use hands out a scrambled axis. A gap
    # would silently widen the box, so it is checked rather than assumed.
    for keep, name in ((keep_x, "longitude"), (keep_y, "latitude")):
        if keep.size > 1 and np.any(np.diff(keep) != 1):
            raise SystemExit(f"forcing: the box is not contiguous in {name}")
    sy = slice(keep_y[0], keep_y[-1] + 1)
    sx = slice(keep_x[0], keep_x[-1] + 1)
    y, x = lats[sy], lons[sx]
    flip_y = y.size > 1 and y[0] > y[-1]
    if flip_y:
        y = y[::-1]
    return sy, sx, y, x, flip_y


def clip(lons, lats, cube, box):
    """Cut *cube* to *box*, returning `(x, y, cube)` with coordinates ascending.

    For a source already wholly in memory, which is what reading GRIB gives.
    Anything reading netCDF should use `box_slices` and never hold the globe.
    """
    sy, sx, y, x, flip_y = box_slices(lons, lats, box)
    out = cube[:, sy, sx]
    if flip_y:
        out = out[:, ::-1, :]
    return x, y, out


def write_atm(path, x, y, origin, series, source, domain):
    """Write one member's `atm.nc`.

    *series* maps a name in `FIELDS` to `(hours, cube)`, where *hours* are hours
    since *origin* and *cube* is (time, lat, lon) on the *x*, *y* grid. Every
    field in `FIELDS` must be present: a data table that names a variable the
    file does not carry fails inside `time_interp_external` with a message about
    the file rather than about the variable, and half an hour is lost to it.

    *source* and *domain* are recorded as global attributes, because the archive
    path says which source and domain a file came from and a file that has been
    moved does not. The box is recorded from the axes actually written rather
    than from the request, so the attribute says what is in the file and not what
    was asked for; the two differ by up to one source cell on every edge.
    """
    missing = [name for name in FIELDS if name not in series]
    if missing:
        raise SystemExit(f"forcing: {path} would be missing {missing}")

    with netCDF4.Dataset(path, "w", format="NETCDF4") as out:
        out.createDimension("LON", len(x))
        out.createDimension("LAT", len(y))
        v = out.createVariable("LON", "f8", ("LON",))
        v.units, v.long_name, v.axis = "degrees_east", "longitude", "X"
        v[:] = x
        v = out.createVariable("LAT", "f8", ("LAT",))
        v.units, v.long_name, v.axis = "degrees_north", "latitude", "Y"
        v[:] = y

        for name, (units, long_name) in FIELDS.items():
            hours, cube = series[name]
            hours = np.asarray(hours, dtype="f8")
            if cube.shape[0] != hours.size:
                raise SystemExit(
                    f"forcing: {name} has {cube.shape[0]} records against "
                    f"{hours.size} times")
            if np.any(np.diff(hours) <= 0):
                raise SystemExit(f"forcing: {name}'s times are not increasing")
            # A missing value in forcing is not a gap the model works around:
            # `data_override` interpolates it into the flux and the ocean
            # carries NaN from the first timestep. Sources mask land in some
            # fields and not others, so this is a real possibility rather than
            # a formality, and it has to fail here where the source is still
            # known.
            if not np.isfinite(cube).all():
                bad = int((~np.isfinite(cube)).sum())
                raise SystemExit(
                    f"forcing: {name} has {bad} non-finite values of "
                    f"{cube.size}; the source masks something this box needs")

            axis = f"time_{name}"
            out.createDimension(axis, None)
            t = out.createVariable(axis, "f8", (axis,))
            # `axis` and `calendar` are both load bearing. soca-science set them
            # with an `ncatted` call after the fact rather than at creation,
            # which is how they are easy to lose.
            t.units = f"hours since {origin:%Y-%m-%d %H:%M:%S}"
            t.calendar = CALENDAR
            t.axis = "T"
            t.long_name = "time"
            t[:] = hours

            f = out.createVariable(name, "f4", (axis, "LAT", "LON"),
                                   zlib=True, complevel=4)
            f.units = units
            f.long_name = long_name
            f[:] = cube

        out.source = source
        out.domain = domain
        out.box = (f"{x[0]:.3f} to {x[-1]:.3f} east, "
                   f"{y[0]:.3f} to {y[-1]:.3f} north")
