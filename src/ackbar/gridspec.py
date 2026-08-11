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

**Why a global domain gets no shift at all.** `_shift` drops the field's highest
index, which is sound only where that index is a face with no cell beyond it. On
a zonally periodic or tripolar grid it is not: `om_1deg`'s outermost `mask2du`
column holds 159 ocean faces of 320, and its outermost `mask2dv` row 226 of 360,
because the cell beyond them exists, reached by the wraparound. So this refuses
there, and such a domain records `east/north` through `record_generated`
instead, which states truthfully where its staggered fields sit.

That is a statement about this implementation, not about the defect. The
mismatch corrected here is a property of SOCA's MOM6 being built without
symmetric memory while the forecast's is built with it, and that is as true
globally as regionally. A global domain integrating the real model would have
every velocity a half cell from its mask exactly as a regional one would, so
`validate` refuses that combination rather than shipping it quietly. No global
domain runs a model today; `OM4_025` is the one that will.

Whoever implements the missing half: the x axis is the easy one and wants
`np.roll` rather than `_shift`, because on a periodic grid the west face of
tracer 0 genuinely *is* the east face of tracer nx-1, so nothing is dropped and
no edge value has to be invented. The y axis is the hard one. A tripolar grid's
northernmost row is the fold, where the grid meets itself reversed rather than
translated, and no roll expresses that.

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

#: What the attribute says on a shifted gridspec, and the whole of what it means:
#: index i of every staggered field is the face on the low side of tracer i.
STAGGER_VALUE = "west/south"

#: What it says on a gridspec left exactly as `soca_gridgen.x` wrote it. Not an
#: exemption from the shift but the honest answer where the shift cannot be
#: applied, which today is every global domain. See `record_generated`, and the
#: module docstring for why the two are different statements.
STAGGER_VALUE_GENERATED = "east/north"

#: Every face set a gridspec is allowed to claim. The domain layer's
#: `staggered_faces` is checked against this by the schema, so a typo there is a
#: schema error rather than a domain that matches no branch at build time.
STAGGER_VALUES = (STAGGER_VALUE, STAGGER_VALUE_GENERATED)

#: The fields to move, by the axis they move along. `mask` is separated from the
#: coordinates because the two need different values at the index the shift
#: vacates: a mask gains land, a coordinate gains a position.
#:
#: **Each coordinate carries its own tracer field, and that pairing is by
#: coordinate rather than by axis.** The vacated face is filled by reflecting it
#: about the tracer point, which is only meaningful between like coordinates: a
#: latitude reflected about a longitude is a number in neither. This once read
#: one tracer per axis, so `latu` was reflected about `lon` and `lonv` about
#: `lat`, which put -214 degrees of latitude and +134 degrees of longitude into
#: the vacated edge of every gridspec built with it.
STAGGERED = (
    (-1, (("lonu", "lon"), ("latu", "lat")), "mask2du"),
    (-2, (("lonv", "lon"), ("latv", "lat")), "mask2dv"),
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

        for axis, coordinates, mask in STAGGERED:
            dropped = _high(np.asarray(data.variables[mask][:]), axis)
            if np.any(dropped != 0.0):
                raise GridspecError(
                    f"the outermost {mask} in {path} has "
                    f"{int(np.sum(dropped != 0.0))} ocean faces in it, and the "
                    f"shift would drop them. A generated gridspec masks that "
                    f"column to land because it has no cell beyond it, so this "
                    f"file is not one this should be applied to."
                )
            for name, tracer in coordinates:
                centre = np.asarray(data.variables[tracer][:])
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


def record_generated(path):
    """Record that a gridspec's staggered fields are where `soca_gridgen.x` left
    them, on the east and north faces.

    For a domain `shift_staggered` cannot be applied to. The attribute is
    written for the same reason it is written after a shift: what makes a
    gridspec safe to read is that it *states* its face set, and a file stating
    nothing is indistinguishable from one built before anybody had thought about
    the question.

    This does not make the domain correct to integrate a model on. It makes the
    file honest about what it is, and `validate._gridspec_step` is where the
    consequence of that honesty is refused.
    """
    with netCDF4.Dataset(path, "r+") as data:
        if STAGGER_ATTR in data.ncattrs():
            raise GridspecError(
                f"{path} already carries {STAGGER_ATTR}="
                f"{data.getncattr(STAGGER_ATTR)!r}, so which faces its "
                f"staggered fields are on has been recorded once already. "
                f"Rebuild the gridspec with tools/soca-gridspec.sh if it needs "
                f"to be redone."
            )
        data.setncattr(STAGGER_ATTR, STAGGER_VALUE_GENERATED)


def assert_faces_recorded(path):
    """Refuse a gridspec that will not say which faces its staggered fields are on.

    The case this exists for is the one that makes this whole approach risky:
    `soca_gridgen.x` is rerun, its output lands without the post-step, and the
    defect returns with no symptom at all. Every application still starts, every
    cycle still completes, and the only evidence is a velocity increment one cell
    from where it belongs, in a system whose documentation now says that is
    fixed. A silent revert after a written fix is worse than the original bug,
    so a gridspec that will not say which faces it is on does not run.

    Presence is the check, not a particular value, the same shape as
    `forcing.assert_reference_height`. A file that records a face set records a
    decision; a file that records nothing predates the decision. Which of the
    two values it records is a separate question, answered against the domain
    layer's declaration by the caller, because this file cannot know which
    domain it was built for.
    """
    faces = staggered_faces(path)
    if faces is None:
        raise ValueError(
            f"{path} has no {STAGGER_ATTR} attribute, so which faces its "
            f"`mask2du` and `lonu` describe is unknown. A gridspec written by "
            f"`soca_gridgen.x` looks exactly like this: its staggered fields "
            f"are on the east and north faces while SOCA's reader loads the "
            f"west and south ones, which puts every velocity increment a cell "
            f"from the mask applied to it. Rebuild it with "
            f"tools/soca-gridspec.sh, which records the face set either way. "
            f"See docs/analysis.md.")
    return faces


def staggered_faces(path):
    """Which face set a gridspec's staggered fields are on, or None if unshifted.

    Everything that reads a velocity out of this domain wants this, and asking
    the file is better than inferring it from when the file was built.
    """
    with netCDF4.Dataset(path) as data:
        if STAGGER_ATTR in data.ncattrs():
            return data.getncattr(STAGGER_ATTR)
    return None


# --- what is inside the domain -----------------------------------------------
#
# Two callers, and they have to agree: `tools/obs-cull-domain.py` decides which
# observations to keep in a domain-scoped archive, and `validate` decides whether
# an experiment's archive has anything in the domain at all. Two copies of this
# that drifted would produce exactly the failure the culling stage exists to
# close, an archive `validate` calls populated and the analysis finds empty. So
# it lives here, beside the file it reads, rather than in either caller.


def wrap(lon):
    """Longitude into [-180, 180).

    Every longitude on both sides of a comparison goes through this. A gridspec
    stores whatever convention its MOM6 case used, which for the global tripolar
    grid is -300 to 60, while an ioda file is written -180 to 180. Comparing the
    two unwrapped puts an entire archive outside its own domain.
    """
    return (np.asarray(lon, dtype="f8") + 180.0) % 360.0 - 180.0


def extent(path):
    """A domain's bounding box, as (west, east, south, north).

    From the gridspec's tracer cell centres, longitude wrapped. **The mask is
    not read, on purpose.** Culling on extent alone means a domain's archive
    survives a change to its topography or its land mask, where culling on the
    mask would bake one version of it into the archive and leave the two to
    disagree silently. A land observation is rejected at run time by SOCA's own
    `Domain Check`, which reports it honestly as present-but-rejected. See
    `tools/obs-cull-domain.py`.

    A bounding box rather than the grid's true footprint: it is a necessary
    condition for being in the domain rather than a sufficient one, which is the
    right side to err on for both callers. The cull keeps a few observations the
    analysis will reject, and `validate` refuses only an archive with nothing
    even in the box.
    """
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        lon = wrap(data.variables["lon"][0])
        lat = np.asarray(data.variables["lat"][0], dtype="f8")
    return float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())


def within(lon, lat, box):
    """A boolean mask of the positions inside *box*, which is an `extent`."""
    west, east, south, north = box
    lon = wrap(lon)
    lat = np.asarray(lat, dtype="f8")
    return (lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)
