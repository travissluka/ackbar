"""Tier 0: the gridspec's staggered fields against the faces the model has.

The assertion that matters is not "the arrays moved". It is that after the
shift, every face the gridspec calls ocean is a face the model actually
integrates a velocity on, and the other way round for the outermost column,
which has no cell beyond it. That invariant is checked here against a synthetic
symmetric restart built from a tracer mask by the same rule MOM6 uses,
`mask2dCu(I) = mask2dT(I) * mask2dT(I+1)`, so the test is about the convention
rather than about a particular domain.

`tools/uv-stagger-figures.py` makes the same measurement on a real domain and
draws it. This is the version that runs without a grid on disk.
"""

from pathlib import Path

import numpy as np
import pytest

netCDF4 = pytest.importorskip("netCDF4")

from ackbar.gridspec import (  # noqa: E402
    STAGGER_ATTR, GridspecError, assert_shifted, shift_staggered,
    staggered_faces)

NX, NY = 8, 6

#: A tracer mask with several coastlines in both directions, because one
#: transition is an anecdote and the shift shows up at every one of them.
LAND = ((0, 0), (0, 1), (1, 1), (3, 4), (4, 4), (5, 0), (2, 6), (3, 6), (5, 7))


def tracer_mask():
    out = np.ones((NY, NX))
    for j, i in LAND:
        out[j, i] = 0.0
    return out


def model_faces(mask):
    """The `u` and `v` masks MOM6 builds from a tracer mask, symmetric.

    A symmetric grid carries one extra face at the low side, so `u` is
    `(NY, NX+1)` and `u[:, i]` is the face between tracer `i-1` and tracer `i`.
    Outside the domain is land, which is what makes both outermost faces dry.
    """
    wide = np.zeros((NY, NX + 2))
    wide[:, 1:-1] = mask
    u = wide[:, :-1] * wide[:, 1:]
    tall = np.zeros((NY + 2, NX))
    tall[1:-1, :] = mask
    v = tall[:-1, :] * tall[1:, :]
    return u, v


def write_gridspec(path, mask):
    """A gridspec as `soca_gridgen.x` writes one: staggered fields on the *east*
    and *north* faces, which is all a non-symmetric MOM6 can express."""
    lon = np.broadcast_to(np.arange(NX, dtype="f8"), (NY, NX)).copy()
    lat = np.broadcast_to(np.arange(NY, dtype="f8")[:, None], (NY, NX)).copy()
    u, v = model_faces(mask)
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("y", NY)
        data.createDimension("x", NX)
        for name, values in (("lon", lon), ("lat", lat),
                             ("lonu", lon + 0.5), ("latu", lat),
                             ("lonv", lon), ("latv", lat + 0.5),
                             ("mask2d", mask),
                             # east face of tracer i, so u[:, i+1] of the model
                             ("mask2du", u[:, 1:]),
                             ("mask2dv", v[1:, :])):
            var = data.createVariable(name, "f8", ("Time", "y", "x"))
            var[:] = values[None]


@pytest.fixture
def grid(tmp_path):
    mask = tracer_mask()
    path = tmp_path / "soca_gridspec.nc"
    write_gridspec(path, mask)
    return path, mask


def read(path, name):
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return np.asarray(data.variables[name][0])


def counts(path, mask):
    """dead-on-wet and live-on-dry, for the slice SOCA's reader takes.

    SOCA reads a tracer count of columns from the tracer origin, so it holds
    `u[:, :NX]` of the symmetric array however the gridspec is labelled. That is
    the whole point: the reader cannot be told which columns to take, so the
    gridspec has to describe the ones it takes.
    """
    u, v = model_faces(mask)
    out = {}
    for key, live, mu in (("u", u[:, :NX] > 0, read(path, "mask2du") > 0),
                          ("v", v[:NY, :] > 0, read(path, "mask2dv") > 0)):
        out[key] = (int(((~live) & mu).sum()), int((live & ~mu).sum()))
    return out


def test_a_generated_gridspec_disagrees_with_the_model(grid):
    """The failure this exists for, stated as a test so it cannot come back."""
    path, mask = grid
    before = counts(path, mask)
    assert before["u"][0] > 0 and before["v"][0] > 0, (
        "the fixture stopped reproducing the defect, so the test below proves "
        "nothing")


def test_after_the_shift_no_land_face_is_called_ocean(grid):
    """dead-on-wet is zero. This is the assertion the whole change is for.

    A face the gridspec calls ocean where the model has no velocity is a land
    face that every analysis writes an increment into and no forecast ever reads
    one back from.
    """
    path, mask = grid
    shift_staggered(path)
    after = counts(path, mask)
    assert after["u"][0] == 0, f"{after['u'][0]} u faces are land called ocean"
    assert after["v"][0] == 0, f"{after['v'][0]} v faces are land called ocean"


def test_the_shift_leaves_only_the_boundary_column_unanalysed(grid):
    """live-on-dry does not reach zero, and the count says which faces remain.

    They are the low-side boundary column, which has no cell beyond it inside
    the domain. Pinning the number keeps a future change honest: a shift that
    quietly stopped analysing an interior face would raise it.
    """
    path, mask = grid
    shift_staggered(path)
    after = counts(path, mask)
    u, v = model_faces(mask)
    assert after["u"][1] == int((u[:, 0] > 0).sum())
    assert after["v"][1] == int((v[0, :] > 0).sum())


def test_the_mask_becomes_the_low_side_face_product(grid):
    """`mask2du(i)` is `mask2dT(i-1) * mask2dT(i)`, which is MOM6's own rule."""
    path, mask = grid
    shift_staggered(path)
    mu = read(path, "mask2du")
    assert np.array_equal(mu[:, 1:], mask[:, :-1] * mask[:, 1:])
    assert np.all(mu[:, 0] == 0.0)
    mv = read(path, "mask2dv")
    assert np.array_equal(mv[1:, :], mask[:-1, :] * mask[1:, :])
    assert np.all(mv[0, :] == 0.0)


def test_the_coordinates_move_with_the_mask(grid):
    """A face's position and its wetness are one statement, so they move together.

    If only the mask moved, the archive would label a velocity with a longitude
    a cell away from the mask that decided whether it was written.
    """
    path, _ = grid
    before = read(path, "lonu")
    shift_staggered(path)
    after = read(path, "lonu")
    assert np.array_equal(after[:, 1:], before[:, :-1])
    # The vacated face is outside the domain, reflected about its tracer point.
    assert np.allclose(after[:, 0], 2.0 * read(path, "lon")[:, 0] - before[:, 0])


def test_every_coordinate_is_reflected_about_its_own_kind(grid):
    """The assertion above, for the three coordinates it does not cover.

    `lonu` is the one staggered field whose vacated edge is right whichever
    tracer fills it, because the u faces share their latitude with the tracer
    row. So checking it alone passed while `latu` was being reflected about a
    longitude and `lonv` about a latitude, which put a latitude of -214 and a
    longitude of +134 into the edge of every gridspec on disk.
    """
    path, _ = grid
    before = {name: read(path, name)
              for name in ("lonu", "latu", "lonv", "latv")}
    shift_staggered(path)
    lon, lat = read(path, "lon"), read(path, "lat")

    for name, tracer, axis in (("lonu", lon, -1), ("latu", lat, -1),
                               ("lonv", lon, -2), ("latv", lat, -2)):
        after = read(path, name)
        if axis == -1:
            assert np.array_equal(after[:, 1:], before[name][:, :-1])
            assert np.allclose(after[:, 0],
                               2.0 * tracer[:, 0] - before[name][:, 0])
        else:
            assert np.array_equal(after[1:, :], before[name][:-1, :])
            assert np.allclose(after[0, :],
                               2.0 * tracer[0, :] - before[name][0, :])

        # The reflection is half a cell, so a filled edge that leaves the
        # coordinate's own range is a sign it was reflected about the wrong
        # field. This is what fails loudly on the bug above; the assertions
        # over the tracer are what say which field was wrong.
        limit = 90.0 if name.startswith("lat") else 180.0
        assert np.all(np.abs(after) <= limit)


def test_shifting_twice_is_refused(grid):
    """The guard, because a second shift moves everything another cell and
    leaves nothing behind to say that it happened."""
    path, _ = grid
    shift_staggered(path)
    assert staggered_faces(path) == "west/south"
    with pytest.raises(GridspecError, match="already carries"):
        shift_staggered(path)


def test_an_unshifted_gridspec_says_so(grid):
    path, _ = grid
    assert staggered_faces(path) is None


def test_a_freshly_generated_gridspec_is_refused(grid):
    """The failure mode this whole approach introduces, held shut.

    `soca_gridgen.x` rerun without the post-step puts the defect back with no
    symptom: every application starts, every cycle completes, and the only
    evidence is a velocity a cell out, in a system whose docs say it is fixed.
    """
    path, _ = grid
    with pytest.raises(ValueError, match=STAGGER_ATTR):
        assert_shifted(path)


def test_a_shifted_gridspec_passes_and_says_which_faces(grid):
    path, _ = grid
    shift_staggered(path)
    assert assert_shifted(path) == "west/south"


def test_dropping_an_ocean_column_is_refused(grid, tmp_path):
    """The shift drops the highest index. On a generated gridspec that column is
    land; if it is not, this is not a file to apply the shift to."""
    path, _ = grid
    with netCDF4.Dataset(path, "r+") as data:
        data.variables["mask2du"][0, :, -1] = 1.0
    with pytest.raises(GridspecError, match="ocean faces in it"):
        shift_staggered(path)
    assert STAGGER_ATTR not in netCDF4.Dataset(path).ncattrs()
