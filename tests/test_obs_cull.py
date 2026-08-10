"""Tier 0: what `tools/obs-cull-domain.py` keeps, drops and refuses.

Runs the tool for real, on files this builds, with no domain and no model: a
gridspec is three arrays and an ioda file is a handful of vectors, so the whole
suite is a fraction of a second and needs nothing staged.

The case worth stating is `test_land_inside_the_grid_survives`. Culling on the
grid's extent and not on its land mask is a decision rather than an oversight,
and it is exactly the kind of decision a later reader fixes: dropping land here
looks obviously right, since those observations are all rejected anyway. It is
not. An archive culled on extent alone survives a change to the domain's
topography or mask, where one culled on the mask would silently disagree with
the grid the next `soca-gridspec.sh` wrote, and SOCA's own `Domain Check`
rejects the land points at run time where the counts can report them honestly.
So the behaviour is pinned by a test, and the test says why.
"""

import subprocess
import sys
from pathlib import Path

import netCDF4
import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "obs-cull-domain.py"

#: The fixture domain: a degree box, well away from the dateline so that
#: wrapping is not what any of these tests is measuring.
WEST, EAST, SOUTH, NORTH = -95.0, -90.0, 20.0, 25.0


def write_gridspec(path, wet=True):
    """A gridspec with just enough in it for `ackbar.gridspec.extent`.

    `mask2d` is written even though the extent never reads it, because a test
    that put a land point inside the box would otherwise be asserting against a
    file with no land in it.
    """
    lon, lat = np.meshgrid(np.linspace(WEST, EAST, 6), np.linspace(SOUTH, NORTH, 5))
    mask = np.ones(lon.shape)
    if not wet:
        # A block of land in the middle, which is where the land observation in
        # `test_land_inside_the_grid_survives` falls.
        mask[1:4, 1:4] = 0.0
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("time", 1)
        data.createDimension("y", lon.shape[0])
        data.createDimension("x", lon.shape[1])
        for name, values in (("lon", lon), ("lat", lat), ("mask2d", mask)):
            data.createVariable(name, "f8", ("time", "y", "x"))[:] = values[None]
    return path


def write_obs(path, lon, lat, values=None):
    """One platform's file for one window, in the layout the archive holds."""
    lon = np.asarray(lon, dtype="f4")
    lat = np.asarray(lat, dtype="f4")
    if values is None:
        values = np.arange(lon.size, dtype="f4")
    path.parent.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Location", lon.size)
        data.createDimension("nvars", 1)
        data.createVariable("Location", "i4", ("Location",))[:] = np.arange(lon.size)
        data.createVariable("nvars", "f4", ("nvars",))[:] = [1.0]

        meta = data.createGroup("MetaData")
        when = meta.createVariable("dateTime", "i8", ("Location",))
        when.units = "seconds since 2015-07-12T06:00:00Z"
        when[:] = np.arange(lon.size)
        meta.createVariable("longitude", "f4", ("Location",))[:] = lon
        meta.createVariable("latitude", "f4", ("Location",))[:] = lat

        for group in ("ObsValue", "ObsError", "PreQc"):
            data.createGroup(group).createVariable(
                group_variable := "seaSurfaceTemperature", "f4", ("Location",))
            data.groups[group].variables[group_variable][:] = values

        data._ioda_layout = "ObsGroup"
        data.platform = "synthetic"
    return path


def run(gridspec, source, out, domain="gom_test"):
    return subprocess.run(
        [sys.executable, str(TOOL), domain,
         "--in", str(source), "--out", str(out), "--gridspec", str(gridspec)],
        capture_output=True, text=True,
    )


def archive(tmp_path, lon, lat, name="sst_test.2015071206.nc4"):
    source = tmp_path / "in"
    write_obs(source / "2015071212" / name, lon, lat)
    return source


def read(path):
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return {
            "locations": len(data.dimensions["Location"]),
            "index": np.asarray(data.variables["Location"][:]),
            "lon": np.asarray(data.groups["MetaData"].variables["longitude"][:]),
            "value": np.asarray(
                data.groups["ObsValue"].variables["seaSurfaceTemperature"][:]),
            "groups": sorted(data.groups),
            "attributes": {name: data.getncattr(name) for name in data.ncattrs()},
            "units": data.groups["MetaData"].variables["dateTime"].units,
        }


def test_outside_the_grid_is_dropped_and_inside_survives(tmp_path):
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = archive(tmp_path, [-92.0, -60.0, -93.5, 10.0], [22.0, 40.0, 24.0, 0.0])

    done = run(gridspec, source, tmp_path / "out")
    assert done.returncode == 0, done.stdout + done.stderr

    kept = read(tmp_path / "out" / "2015071212" / "sst_test.2015071206.nc4")
    assert kept["locations"] == 2
    assert kept["lon"] == pytest.approx([-92.0, -93.5])
    # The values travel with their own rows rather than with their positions.
    assert kept["value"] == pytest.approx([0.0, 2.0])


@pytest.mark.parametrize("lon,lat,because", [
    (-92.0, 40.0, "north of the grid, at a longitude inside it"),
    (-92.0, 5.0, "south of the grid, at a longitude inside it"),
    (-60.0, 22.0, "east of the grid, at a latitude inside it"),
    (-120.0, 22.0, "west of the grid, at a latitude inside it"),
])
def test_each_edge_of_the_box_is_an_edge(lon, lat, because, tmp_path):
    """One point outside on one side and inside on the other, four times.

    Four cases rather than one because a point outside on *both* axes is
    rejected whichever bound is broken, so a single diagonal case would pass
    with either half of the test missing. This is what a mutation of the
    latitude bound alone slipped through.
    """
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = archive(tmp_path, [-92.0, lon], [22.0, lat])

    assert run(gridspec, source, tmp_path / "out").returncode == 0

    kept = read(tmp_path / "out" / "2015071212" / "sst_test.2015071206.nc4")
    assert kept["locations"] == 1, f"a point {because} was kept"
    assert kept["lon"] == pytest.approx([-92.0])


def test_the_location_index_is_renumbered(tmp_path):
    """It numbers the rows of the file it is in, so it cannot be subset.

    Carrying the source numbering into a file with different rows would leave an
    index that skips, which is not what any reader of it expects.
    """
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = archive(tmp_path, [-60.0, -92.0, -60.0, -93.5], [40.0, 22.0, 40.0, 24.0])

    assert run(gridspec, source, tmp_path / "out").returncode == 0

    kept = read(tmp_path / "out" / "2015071212" / "sst_test.2015071206.nc4")
    assert list(kept["index"]) == [0, 1]


def test_land_inside_the_grid_survives(tmp_path):
    """Extent only, never the mask. See this module's docstring.

    The observation sits in the middle of the fixture's block of land and is
    kept regardless. SOCA's `Domain Check` is what rejects it, at run time,
    where the rejection is counted and reported.
    """
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc", wet=False)
    source = archive(tmp_path, [-92.5], [22.5])

    assert run(gridspec, source, tmp_path / "out").returncode == 0

    kept = read(tmp_path / "out" / "2015071212" / "sst_test.2015071206.nc4")
    assert kept["locations"] == 1


def test_a_window_with_nothing_in_it_is_an_empty_file_not_a_missing_one(tmp_path):
    """The pinned ioda reads this; an absent file would mean something else.

    An absent file is how `stage.obs` learns of a gap in the archive, which is
    not what "this platform was considered and saw nothing" means.
    """
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    # One file with nothing in the domain, one with something, so the run as a
    # whole still finds observations and does not take the refusal path.
    source = tmp_path / "in"
    write_obs(source / "2015071212" / "sst_test.2015071206.nc4", [-92.0], [22.0])
    write_obs(source / "2015071312" / "sst_test.2015071306.nc4", [10.0, 11.0], [0.0, 1.0])

    done = run(gridspec, source, tmp_path / "out")
    assert done.returncode == 0, done.stdout + done.stderr

    empty = tmp_path / "out" / "2015071312" / "sst_test.2015071306.nc4"
    assert empty.exists(), "an empty window must still produce a file"

    held = read(empty)
    assert held["locations"] == 0
    assert held["value"].size == 0
    # Every group survives at length zero rather than the file collapsing to the
    # barest thing ioda will accept.
    assert held["groups"] == ["MetaData", "ObsError", "ObsValue", "PreQc"]
    assert held["attributes"]["platform"] == "synthetic"
    assert held["units"] == "seconds since 2015-07-12T06:00:00Z"


def test_structure_and_attributes_are_carried_through(tmp_path):
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = archive(tmp_path, [-92.0, -60.0], [22.0, 40.0])

    assert run(gridspec, source, tmp_path / "out").returncode == 0

    kept = read(tmp_path / "out" / "2015071212" / "sst_test.2015071206.nc4")
    assert kept["groups"] == ["MetaData", "ObsError", "ObsValue", "PreQc"]
    assert kept["attributes"]["_ioda_layout"] == "ObsGroup"
    assert kept["units"] == "seconds since 2015-07-12T06:00:00Z"
    with netCDF4.Dataset(tmp_path / "out" / "2015071212" / "sst_test.2015071206.nc4") as data:
        # `nvars` is not indexed by Location and is copied whole.
        assert len(data.dimensions["nvars"]) == 1


def test_no_partial_file_is_left_behind(tmp_path):
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = archive(tmp_path, [-92.0], [22.0])

    assert run(gridspec, source, tmp_path / "out").returncode == 0

    assert not list((tmp_path / "out").rglob("*.partial"))


def test_an_archive_entirely_outside_the_domain_is_refused(tmp_path):
    """Not a cull, a mismatch. Every experiment reading the result would run to
    completion and assimilate nothing, so it is said at the point it is known.
    """
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = archive(tmp_path, [10.0, 11.0], [0.0, 1.0])

    done = run(gridspec, source, tmp_path / "out")
    assert done.returncode != 0
    assert "do not overlap" in done.stdout + done.stderr


def test_a_missing_gridspec_is_a_sentence(tmp_path):
    source = archive(tmp_path, [-92.0], [22.0])
    done = run(tmp_path / "there-is-no-gridspec.nc", source, tmp_path / "out")
    assert done.returncode != 0
    assert "does not exist" in done.stdout + done.stderr
    assert "Traceback" not in done.stderr


def test_culling_in_place_is_refused(tmp_path):
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = archive(tmp_path, [-92.0], [22.0])
    done = run(gridspec, source, source)
    assert done.returncode != 0
    assert "same directory" in done.stdout + done.stderr


def test_a_file_without_coordinates_is_a_sentence(tmp_path):
    gridspec = write_gridspec(tmp_path / "soca_gridspec.nc")
    source = tmp_path / "in"
    target = source / "2015071212" / "sst_test.2015071206.nc4"
    target.parent.mkdir(parents=True)
    with netCDF4.Dataset(target, "w") as data:
        data.createDimension("Location", 3)
        data.createVariable("Location", "i4", ("Location",))[:] = [0, 1, 2]

    done = run(gridspec, source, tmp_path / "out")
    assert done.returncode != 0
    assert "MetaData" in done.stdout + done.stderr
    assert "Traceback" not in done.stderr
