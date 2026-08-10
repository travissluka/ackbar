"""Putting a gom_25km state and the gom_12km truth on the same footing.

Shared by `spread-report.py` here, and by `osse-ic-error.py` and
`osse-state-error.py` in the personal tree, which ask the same question at one
time and at every time. All of them have to solve the same two mismatches
first, and each would get them subtly wrong in its own way if this were
duplicated. It lives in `tools/` rather than `tools/local/` for exactly that
reason: a committed tool may not import a gitignored one, and the alternative
was a second copy of the regridding.

**Horizontally**, the truth is sampled at the coarse grid's cell centres rather
than averaged onto them. The two grids are exact factors of each other and their
centres coincide, so this is co-location and not really interpolation, and it is
what `tools/obs-archive-osse.py` does when it draws an observation: the state
space error here and the departures the run reports are then the same
measurement seen two ways.

**Vertically**, neither state has a depth axis. A Z* coordinate's layer depths
are a property of the state, because the free surface moves every one of them,
so both columns are rebuilt from their own `h` before anything is differenced.

**The velocities are on C-grid faces**, `u` half a cell east of the tracer
centre and `v` half a cell north, each with its own mask that genuinely differs
from the tracer one. They are moved onto the tracer centres as they are read, so
that everything below this sees one grid. Bilinear at the midpoint of two faces
is their average, which is the ordinary C to A regrid; doing it at read time
rather than at comparison time is what keeps `sample`, `wet_in_both` and `stats`
from each growing a branch for two fields.
"""

import numpy as np
import netCDF4
from scipy.interpolate import RegularGridInterpolator

#: Fixed depths a state is compared on. The same ladder
#: `tools/obs-archive-osse.py` casts a float on, so a row in one of these tables
#: and a float's error at that level are the same measurement.
DEPTHS = (0, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 700, 1000, 1500)

#: The velocity components, as the key they are read into, the record variable
#: they come from, and the suffix of the coordinates and mask they are written
#: on. See the module docstring: these are the fields on their own grid.
VELOCITIES = (("u", "eastward_velocity", "_u"),
              ("v", "northward_velocity", "_v"))


def read_record(path):
    """One `post.state` record, or one promoted truth state: the same schema.

    That they share a schema is the point of the `bkg/` records. A verification
    that had to know which of the two it was reading would grow a branch in
    every consumer.
    """
    with netCDF4.Dataset(path) as f:
        lon = np.asarray(f["lon"][0, :])
        lat = np.asarray(f["lat"][:, 0])
        state = {
            "lon": lon,
            "lat": lat,
            "wet": np.asarray(f["mask"][:]) > 0,
            "ssh": np.ma.filled(f["sea_surface_height"][:], np.nan),
            "t": np.ma.filled(f["temperature"][:], np.nan),
            "s": np.ma.filled(f["salinity"][:], np.nan),
            "h": np.ma.filled(f["thickness"][:], np.nan),
        }
        for key, variable, suffix in VELOCITIES:
            values = np.ma.filled(f[variable][:], np.nan)
            # Land faces are made NaN *before* the regrid rather than masked
            # after it, so that a dry face cannot contribute a value to the wet
            # tracer cell beside it. It costs the coastal ring, which is the
            # right side of that trade: `stats` drops non-finite differences
            # already, so those cells leave the score rather than corrupt it.
            values[:, ~(np.asarray(f["mask" + suffix][:]) > 0)] = np.nan
            faces = {"lon": np.asarray(f["lon" + suffix][0, :]),
                     "lat": np.asarray(f["lat" + suffix][:, 0])}
            state[key] = onto(faces, values, lon, lat)
    return state


def read_restart(path, mask):
    """A MOM6 restart, as the same dictionary a record reads into.

    The initial condition is a restart and every state after it is a record.
    Only the variable names and the leading time index differ, and the mask
    comes from the domain rather than travelling with the file.
    """
    with netCDF4.Dataset(path) as f:
        state = {
            "lon": np.asarray(f["lonh"][:]),
            "lat": np.asarray(f["lath"][:]),
            "ssh": np.asarray(f["ave_ssh"][0]),
            "t": np.asarray(f["Temp"][0]),
            "s": np.asarray(f["Salt"][0]),
            "h": np.asarray(f["h"][0]),
        }
    with netCDF4.Dataset(mask) as f:
        state["wet"] = np.asarray(f["mask"][:]) > 0
    return state


def centres(thickness):
    """Depth of each layer's centre, from the thicknesses above it."""
    edges = np.cumsum(thickness, axis=0)
    return edges - thickness / 2.0


def to_depths(field, depth, wanted=DEPTHS):
    """A (z, y, x) field on its own moving levels, resampled to fixed depths.

    Holds the top layer's value above the shallowest centre, which is what a
    surface value is. **Refuses to extrapolate below the deepest centre**: a
    shelf cell has no thousand metre value, and inventing one by holding the
    bottom would put a spurious near-zero difference into the deep rows, which
    are exactly the rows these tables are read for.

    Vectorised across columns rather than looped. The truth grid is nineteen
    thousand columns and this runs once per cycle per variable.
    """
    nz, ny, nx = field.shape
    values, depths = field.reshape(nz, -1), depth.reshape(nz, -1)
    columns = np.arange(values.shape[1])
    out = np.full((len(wanted), values.shape[1]), np.nan)
    for index, target in enumerate(wanted):
        # Land columns are NaN throughout, so every comparison is False, the
        # bracket collapses to level zero, and the NaN there propagates out.
        below = np.clip((depths < target).sum(axis=0), 1, nz - 1)
        low, high = below - 1, below
        z0, z1 = depths[low, columns], depths[high, columns]
        f0, f1 = values[low, columns], values[high, columns]
        span = np.where(z1 > z0, z1 - z0, np.nan)
        got = f0 + (target - z0) / span * (f1 - f0)
        got = np.where(target <= depths[0, columns], values[0, columns], got)
        out[index] = np.where(target > depths[-1, columns], np.nan, got)
    return out.reshape(len(wanted), ny, nx)


def onto(source, field, lon, lat):
    """A field on the *source* grid, sampled at the (lon, lat) cell centres.

    Bilinear, which on gom_12km and gom_25km is exact co-location. Written as an
    interpolation anyway so it stays correct if either grid is ever changed.
    """
    grid = (source["lat"], source["lon"])
    points = np.stack(np.meshgrid(lat, lon, indexing="ij"), axis=-1)
    if field.ndim == 2:
        return RegularGridInterpolator(grid, field, bounds_error=False,
                                       fill_value=np.nan)(points)
    return np.stack([RegularGridInterpolator(grid, level, bounds_error=False,
                                             fill_value=np.nan)(points)
                     for level in field])


def sample(source, target):
    """Every field of *source* on *target*'s grid. See `onto`.

    Keyed on what the source turned out to have rather than on a fixed list,
    because the two readers above do not carry the same fields: a `post.state`
    record has the velocities and a MOM6 restart, as `read_restart` reads it,
    does not. Neither caller wants a velocity error out of an initial condition,
    so the reader is the honest place for that difference to live.
    """
    return {key: onto(source, source[key], target["lon"], target["lat"])
            for key in ("ssh", "t", "s", "h", "u", "v") if key in source}


def wet_in_both(state, sampled):
    """Cells that are ocean on both grids.

    The 12 km coastline resolves inlets the 25 km one does not, and a cell that
    is ocean on one and land on the other would difference a real value against
    a fill.
    """
    return state["wet"] & np.isfinite(sampled["ssh"])


def stats(difference, wet):
    """rms, bias and largest absolute difference, over wet cells only."""
    values = difference[wet]
    values = values[np.isfinite(values)]
    if not values.size:
        return None
    return {"rms": float(np.sqrt(np.mean(values ** 2))),
            "bias": float(np.mean(values)),
            "max": float(np.max(np.abs(values))),
            "n": int(values.size)}
