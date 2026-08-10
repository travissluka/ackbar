"""Putting the gridspec's staggered fields on the faces SOCA actually reads.

`soca_gridgen.x` writes `soca_gridspec.nc` from SOCA's own MOM6, which is built
without symmetric memory. In that build `lonu(i)` is the *east* face of tracer i
and `mask2du(i)` is `mask2dT(i) * mask2dT(i+1)`, because MOM6's `u(I)` always
sits between `h(I)` and `h(I+1)` and a non-symmetric grid simply has no index
for the westernmost face.

The forecast's MOM6 *is* symmetric, so the restart it writes has that extra
index: `IsdB = isd-1`, and restart column 0 of `u` is the **west** face of
tracer 0. SOCA reads a restart with `commit_reader_strided`, which starts every
variable at the tracer origin (`i_start = isc - isg + 1`) and reads a tracer
count of columns, with no branch on which grid the field is on. So SOCA loads
columns 0..nx-1, which are the **west** faces, and labels them with a `lonu` and
a `mask2du` that describe the **east** faces. Every velocity in the system is
one cell away from the mask applied to it.

This module moves the label to the data rather than the data to the label. Each
staggered field is shifted one index towards +x (`u`) or +y (`v`), so that index
i becomes the west or south face of tracer i, which is the face the reader
loads. `mask2du(i)` becomes `mask2dT(i-1) * mask2dT(i)`, which is exactly the
model's own `mask2dCu` at that face, and the outermost index, having no cell
beyond it inside the domain, becomes land.

**Why not shift the data instead.** The alternative is to strip the first column
of `u` and the first row of `v` from every restart before SOCA reads it, which is
what soca-science did in `tools/regional/soca_dynsym2dyn.sh`. It is equally
correct and it costs a converted copy of every restart the solver reads: at
gom_25km that is 39 MB times twenty members times five slots, so about 3.9 GB
per cycle against a 16 GB experiment. This costs one array shift per domain,
offline, once. The trade is that ACKBAR's velocity faces are then the west and
south set rather than MOM6's own east and north set, so the archive says which it
is: `post` writes `lon_u` and `lat_u` beside every velocity record.

**How to re-check it**, on any domain, without running anything:

    tools/uv-stagger-figures.py --gridspec <static>/soca_gridspec.nc \\
        --restart <any MOM.res.nc on that domain> --out <somewhere>

It counts, for both pairings, the faces where the gridspec's mask and the
model's own live velocity disagree. `dead-on-wet` must be zero: it is a face the
gridspec calls ocean where the model has no velocity because it is land, so an
analysis writes an increment into it and no forecast ever reads one back.

The evidence that fixes the direction, measured on gom_25km, 87x56 at 0.25
degrees:

  * `max|lonu - lonq[1:88]| = 0.0` exactly, against `0.25` for `lonq[0:87]`.
    The gridspec's u points are the restart's faces 1..87, not 0..86.
  * `mask2du == mask2d[i] * mask2d[i+1]` with 0 mismatches of 4816, against 256
    for the west-face product. The generated mask is the east-face mask.
  * Before the shift, pairing SOCA index i with restart column i leaves
    dead-on-wet at 128 u and 110 v faces, on every restart tried (a staged
    initial condition, a free-run forecast, a LETKF forecast). After it, zero.
  * `live-on-dry` does not reach zero and is not meant to: 49 u and 18 v faces
    remain, which are the boundary column that has no cell beyond it. Which edge
    they sit on is what this shift moves, from east and north to west and south.
"""

import netCDF4
import numpy as np

#: Written into the gridspec once the shift has been applied. It is the guard
#: against a second application, which would move the fields another cell and
#: leave nothing to say that it had happened.
STAGGER_ATTR = "ackbar_staggered_faces"

#: What the attribute says, and the whole of what it means: index i of every
#: staggered field is the face on the low side of tracer i.
STAGGER_VALUE = "west/south"

#: The fields to move, by the axis they move along. `mask` is separated from the
#: coordinates because the two need different values at the index the shift
#: vacates: a mask gains land, a coordinate gains a position.
STAGGERED = (
    (-1, "lon", ("lonu", "latu"), "mask2du"),
    (-2, "lat", ("lonv", "latv"), "mask2dv"),
)


class GridspecError(Exception):
    pass


def shift_staggered(path):
    """Move every staggered field one index up, in place.

    A shift towards +x drops the field's highest index off the end. On a
    generated gridspec that index is the east or north face of the last tracer,
    which non-symmetric MOM6 has already masked to land for want of a cell
    beyond it, so nothing analysable is lost. That is checked rather than
    assumed: if the column being dropped has any ocean in it, this refuses,
    because the shift would be discarding faces the analysis was reaching.
    """
    with netCDF4.Dataset(path, "r+") as data:
        data.set_auto_mask(False)
        if STAGGER_ATTR in data.ncattrs():
            raise GridspecError(
                f"{path} already carries {STAGGER_ATTR}="
                f"{data.getncattr(STAGGER_ATTR)!r}, so its staggered fields "
                f"have been shifted once. Shifting again would move them "
                f"another cell. Rebuild the gridspec with "
                f"tools/soca-gridspec.sh if it needs to be redone."
            )
        for name in ("lon", "lat", "lonu", "latu", "lonv", "latv",
                     "mask2du", "mask2dv"):
            if name not in data.variables:
                raise GridspecError(
                    f"{path} has no {name}, so it is not a soca gridspec this "
                    f"can shift"
                )

        for axis, tracer, coordinates, mask in STAGGERED:
            dropped = _high(np.asarray(data.variables[mask][:]), axis)
            if np.any(dropped != 0.0):
                raise GridspecError(
                    f"the outermost {mask} in {path} has "
                    f"{int(np.sum(dropped != 0.0))} ocean faces in it, and the "
                    f"shift would drop them. A generated gridspec masks that "
                    f"column to land because it has no cell beyond it, so this "
                    f"file is not one this should be applied to."
                )
            centre = np.asarray(data.variables[tracer][:])
            for name in coordinates:
                values = np.asarray(data.variables[name][:])
                # The vacated face is outside the domain, so there is no grid
                # point to copy. Reflecting the old outermost face about its
                # tracer point puts it half a cell the other side, which is
                # where that face is on any grid smooth at the scale of a cell.
                # Its mask is zero, so nothing reads it; it exists so the
                # coordinate is monotonic rather than repeated.
                edge = 2.0 * _face(centre, axis) - _face(values, axis)
                data.variables[name][:] = _shift(values, axis, edge)
            values = np.asarray(data.variables[mask][:])
            data.variables[mask][:] = _shift(values, axis, 0.0)

        data.setncattr(STAGGER_ATTR, STAGGER_VALUE)


def _face(values, axis):
    """The lowest row or column, which the shift is about to vacate."""
    return values[..., :, 0] if axis == -1 else values[..., 0, :]


def _high(values, axis):
    """The highest row or column, which the shift drops off the end."""
    return values[..., :, -1] if axis == -1 else values[..., -1, :]


def _shift(values, axis, edge):
    """*values* moved one index towards +x or +y, with *edge* filling index 0."""
    out = np.empty_like(values)
    if axis == -1:
        out[..., :, 1:] = values[..., :, :-1]
        out[..., :, 0] = edge
    else:
        out[..., 1:, :] = values[..., :-1, :]
        out[..., 0, :] = edge
    return out


def staggered_faces(path):
    """Which face set a gridspec's staggered fields are on, or None if unshifted.

    Everything that reads a velocity out of this domain wants this, and asking
    the file is better than inferring it from when the file was built.
    """
    with netCDF4.Dataset(path) as data:
        if STAGGER_ATTR in data.ncattrs():
            return data.getncattr(STAGGER_ATTR)
    return None
