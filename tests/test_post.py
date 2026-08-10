"""Tier 0: the two reductions that outlive the cycle they describe.

Everything here is built from synthetic files, because what these bodies do is
read a file format and reduce it. The formats are pinned by fixtures written
here to match what the real applications actually produced, and where a fixture
encodes a surprise the test says what the surprise was.
"""

import json

import numpy as np
import pytest

from ackbar import post

netCDF4 = pytest.importorskip("netCDF4")

NY, NX, NZ = 4, 5, 3


# --- fixtures that look like the real files -----------------------------------

def ioda(path, *, groups, variable="seaSurfaceTemperature"):
    """An ioda output file carrying exactly the groups asked for.

    *groups* maps a group name to the array it holds. The point of building it
    this way is that a test can create the group *names* the real applications
    write, suffix and all, which is the whole of what the first version of this
    module got wrong.
    """
    count = max(len(values) for values in groups.values())
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Location", count)
        for name, values in groups.items():
            group = data.createGroup(name)
            kind = "i4" if name.startswith("EffectiveQC") else "f4"
            written = group.createVariable(variable, kind, ("Location",))
            written[:] = np.asarray(values)
    return path


def gridspec(path, *, land=1):
    """A soca gridspec: coordinates and a mask, with `land` cells of land."""
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("time", 1)
        data.createDimension("ny", NY)
        data.createDimension("nx", NX)
        lon = data.createVariable("lon", "f8", ("time", "ny", "nx"))
        lat = data.createVariable("lat", "f8", ("time", "ny", "nx"))
        mask = data.createVariable("mask2d", "f8", ("time", "ny", "nx"))
        lon[0] = np.tile(np.linspace(-97, -78, NX), (NY, 1))
        lat[0] = np.tile(np.linspace(19, 31, NY), (NX, 1)).T
        values = np.ones((NY, NX))
        values.flat[:land] = 0.0
        mask[0] = values
    return path


def restart(directory, *, offset=0.0, fields=("Temp", "Salt", "h", "ave_ssh")):
    """A MOM6 restart holding the fields the state record looks for."""
    directory.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(directory / "MOM.res.nc", "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("zl", NZ)
        data.createDimension("lath", NY)
        data.createDimension("lonh", NX)
        for name in fields:
            dims = ("Time", "lath", "lonh") if name == "ave_ssh" \
                else ("Time", "zl", "lath", "lonh")
            shape = (1, NY, NX) if name == "ave_ssh" else (1, NZ, NY, NX)
            written = data.createVariable(name, "f8", dims)
            written.units = "degC" if name == "Temp" else "m"
            written[:] = np.full(shape, 10.0 + offset)
    return directory


# --- observation space --------------------------------------------------------

def test_the_qc_group_carries_an_outer_iteration_index(tmp_path):
    """The bug that made this module wrong on its first real file.

    A variational analysis writes `EffectiveQC0` and `EffectiveQC1`, not
    `EffectiveQC`. Code looking for the bare name finds nothing, concludes no
    filter ran, and reports every observation as assimilated. On the file that
    caught it three of six hundred SSTs had been rejected by the background
    check and the summary said none had.
    """
    path = ioda(tmp_path / "sst.nc4", groups={
        "ObsValue": [10, 11, 12, 13, 14, 15],
        "EffectiveQC0": [0, 0, 0, 13, 13, 0],
        "EffectiveQC1": [0, 0, 0, 13, 13, 0],
        "hofx0": [10, 11, 12, 13, 14, 15],
    })
    stats = post.obs_stats([{"name": "sst", "output": str(path)}],
                           tmp_path / "obs.json")
    variable = stats["observers"][0]["variables"]["seaSurfaceTemperature"]
    assert variable["count"] == 6
    assert variable["assimilated"] == 4
    assert variable["qc_group"] == "EffectiveQC0"
    assert variable["rejected"] == {"13": 2}


def test_the_solve_s_own_qc_decides_what_was_assimilated(tmp_path):
    # The lowest index is the evaluation the minimization saw. A later one can
    # reject more, and reporting that as what was assimilated understates what
    # the analysis actually fitted.
    path = ioda(tmp_path / "sst.nc4", groups={
        "ObsValue": [10, 11, 12, 13],
        "EffectiveQC0": [0, 0, 0, 13],
        "EffectiveQC1": [0, 0, 13, 13],
        "hofx0": [10, 11, 12, 13],
    })
    stats = post.obs_stats([{"name": "sst", "output": str(path)}],
                           tmp_path / "obs.json")
    variable = stats["observers"][0]["variables"]["seaSurfaceTemperature"]
    assert variable["qc_group"] == "EffectiveQC0"
    assert variable["assimilated"] == 3


def test_departures_are_derived_from_the_forward_operator(tmp_path):
    """O-B and O-A from `hofx<first>` and `hofx<last>`, and the file says so.

    Deriving rather than reading `ombg` is what makes the sign unambiguous. It
    also means the two ends of the outer loop are found by their index rather
    than by two group names that could be anything.
    """
    path = ioda(tmp_path / "sst.nc4", groups={
        "ObsValue": [10.0, 12.0],
        "hofx0": [9.0, 10.0],
        "hofx1": [9.5, 11.5],
    })
    stats = post.obs_stats([{"name": "sst", "output": str(path)}],
                           tmp_path / "obs.json")
    variable = stats["observers"][0]["variables"]["seaSurfaceTemperature"]
    assert variable["omb"]["mean"] == pytest.approx(1.5)
    assert variable["omb_from"] == "ObsValue - hofx0"
    assert variable["oma"]["mean"] == pytest.approx(0.5)
    assert variable["oma_from"] == "ObsValue - hofx1"
    assert variable["omb"]["rms"] == pytest.approx(np.sqrt((1.0 ** 2 + 2.0 ** 2) / 2))


def test_a_file_without_a_forward_operator_falls_back_and_names_the_group(tmp_path):
    # An application that writes only `ombg` still gets summarized, and the
    # summary says where the number came from rather than claiming a sign for
    # it. `omb_from` is the whole difference between the two paths.
    path = ioda(tmp_path / "adt.nc4", groups={
        "ObsValue": [1.0, 2.0],
        "ombg": [0.2, 0.4],
    })
    stats = post.obs_stats([{"name": "adt", "output": str(path)}],
                           tmp_path / "obs.json")
    variable = stats["observers"][0]["variables"]["seaSurfaceTemperature"]
    assert variable["omb_from"] == "ombg"
    assert variable["omb"]["mean"] == pytest.approx(0.3)
    assert "oma" not in variable


def test_no_qc_group_is_reported_rather_than_assumed(tmp_path):
    path = ioda(tmp_path / "sst.nc4", groups={
        "ObsValue": [1.0, 2.0],
        "hofx0": [1.0, 1.0],
    })
    stats = post.obs_stats([{"name": "sst", "output": str(path)}],
                           tmp_path / "obs.json")
    variable = stats["observers"][0]["variables"]["seaSurfaceTemperature"]
    assert variable["qc_group"] is None
    assert "no EffectiveQC group" in variable["note"]


def test_an_observer_with_no_output_is_the_finding_and_not_a_failure(tmp_path):
    """This body runs under `afterany`, so a missing file is what it is for.

    A cycle whose analysis died leaves no observation output, and the document
    that says which observer has none is exactly the diagnostic wanted at that
    moment. Refusing would delete it.
    """
    stats = post.obs_stats([{"name": "gone", "output": str(tmp_path / "nope.nc4")}],
                           tmp_path / "obs.json")
    assert "no output file" in stats["observers"][0]["error"]
    assert stats["totals"] == {"observers": 1, "failed": 1,
                               "count": 0, "assimilated": 0}


def test_an_empty_observation_space_is_recorded_as_one(tmp_path):
    """"Considered and saw nothing" is a different fact from a blank record.

    A domain-scoped archive produces empty observation spaces routinely, and the
    application writes back a file holding only a zero length `Location`,
    because `put_db` does nothing when there are no rows. Without the marker
    that record is indistinguishable from one for a file that arrived damaged,
    and the two want opposite reactions.
    """
    path = tmp_path / "sst.nc4"
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Location", 0)

    stats = post.obs_stats([{"name": "sst", "output": str(path)}],
                           tmp_path / "obs.json")
    record = stats["observers"][0]
    assert record["empty"] is True
    assert "had nothing inside the domain" in record["note"]
    # It is not an error, and it contributes nothing to the counts, which is
    # what keeps `post.obs`'s "read some and assimilated none" refusal from
    # firing on a cycle that simply had no observations.
    assert "error" not in record
    assert stats["totals"]["count"] == 0


def test_a_populated_observer_is_not_marked_empty(tmp_path):
    path = ioda(tmp_path / "sst.nc4", groups={"ObsValue": [1.0], "hofx0": [1.0]})
    stats = post.obs_stats([{"name": "sst", "output": str(path)}],
                           tmp_path / "obs.json")
    assert "empty" not in stats["observers"][0]


def test_the_summary_is_written_where_it_was_asked_for(tmp_path):
    target = tmp_path / "post" / "3" / "obs.json"
    path = ioda(tmp_path / "sst.nc4", groups={"ObsValue": [1.0], "hofx0": [1.0]})
    post.obs_stats([{"name": "sst", "output": str(path)}], target)
    assert json.loads(target.read_text())["totals"]["count"] == 1


# --- state space --------------------------------------------------------------

def record(tmp_path, *, state="bkg", land=1, **kwargs):
    """One state written the way `post.state` writes it, and its file."""
    grid = gridspec(tmp_path / "soca_gridspec.nc", land=land)
    source = restart(tmp_path / state, offset=0.0, **kwargs)
    target = tmp_path / f"{state}.nc"
    written = post.state_record(state, source, grid, target, cycle=7, member=2,
                                valid="2015-01-05T01:00:00Z")
    return target, written


def test_one_file_holds_one_state_and_says_which(tmp_path):
    """`ana/` and `bkg/` are separate products, so they are separate files.

    A comparison between two experiments is a comparison of their analyses or of
    their backgrounds, never of a pair, and a free run writes one of them and not
    the other.
    """
    target, written = record(tmp_path, state="ana")
    assert set(written) == {"temperature", "salinity", "thickness",
                            "sea_surface_height"}
    with netCDF4.Dataset(target) as data:
        assert data.state == "analysis"
        assert data.cycle == 7
        assert data.member == 2
        assert data.valid_time == "2015-01-05T01:00:00Z"
        assert data.variables["temperature"].source == "Temp"


def test_a_state_that_is_not_there_is_refused_rather_than_written_empty(tmp_path):
    # The caller decides whether a state exists; a `bkg/` file that quietly
    # failed to appear cannot be told from a cycle that had no background.
    grid = gridspec(tmp_path / "soca_gridspec.nc")
    (tmp_path / "bkg").mkdir()
    with pytest.raises(post.PostError, match="has no bkg state"):
        post.state_record("bkg", tmp_path / "bkg", grid, tmp_path / "bkg.nc",
                          cycle=1, member=0, valid="2015-01-05T01:00:00Z")


def test_land_is_the_fill_value_and_not_whatever_the_restart_had(tmp_path):
    """A restart carries a real number under the mask and MOM6 mostly does not
    care what it is. Averaged into a domain statistic it silently poisons it, so
    it is replaced once here rather than by every reader remembering to.
    """
    target, _ = record(tmp_path, land=3)
    with netCDF4.Dataset(target) as data:
        data.set_auto_mask(False)
        mask = np.asarray(data.variables["mask"][:]) > 0
        values = np.asarray(data.variables["temperature"][:])
        assert np.all(values[:, ~mask] > 1e30)
        assert np.all(values[:, mask] < 1e30)


def test_the_grid_travels_with_the_record(tmp_path):
    # A MOM6 restart carries indices and no coordinates, so a record that took
    # its fields and not the gridspec could not say where its own values are.
    target, _ = record(tmp_path)
    with netCDF4.Dataset(target) as data:
        assert data.variables["lon"].shape == (NY, NX)
        assert data.variables["lat"].units == "degrees_north"
        assert data.variables["mask"].shape == (NY, NX)


def test_the_record_is_quantized_and_that_is_what_makes_it_small(tmp_path):
    """Deflate on raw float32 buys about three; deflate after quantization buys
    about nine, and nine is the difference between keeping every member of every
    cycle and keeping a sample. The quantization is therefore part of what this
    file is, and it is recorded in the variable so a reader knows its precision.
    """
    target, _ = record(tmp_path)
    with netCDF4.Dataset(target) as data:
        variable = data.variables["temperature"]
        filters = variable.filters()
        digits = variable.getncattr(
            "_QuantizeBitGroomNumberOfSignificantDigits")
    assert filters["shuffle"] is True
    assert filters["complevel"] == post.ENCODING["complevel"]
    assert digits == post.ENCODING["significant_digits"]


def _widen(source, extra, name="ave_ssh"):
    """Replace *name* with a version *extra* columns wider, numbered by column.

    Numbered so that which corner was taken is visible in the values rather than
    inferred from a shape that would match either way.
    """
    with netCDF4.Dataset(source / "MOM.res.nc", "a") as data:
        data.createDimension(f"lonq{extra}", NX + extra)
        wide = data.createVariable("widened", "f8",
                                   ("Time", "lath", f"lonq{extra}"))
        wide[:] = np.arange(NX + extra, dtype="f8")[None, None, :]
        data.renameVariable(name, f"{name}_original")
        data.renameVariable("widened", name)


def test_a_symmetric_memory_restart_is_read_on_its_tracer_corner(tmp_path):
    """A regional MOM6 writes `u` a column wider, and this takes the right one.

    Symmetric memory is what the Flather open boundaries need, so every regional
    domain's restart carries `u` on `lonq` and `v` on `latq`. The corner taken
    has to be the one `writeback._place` writes an analysis into, or the record
    and the state the model actually integrates are a column apart and every
    velocity in the archive is shifted.
    """
    grid = gridspec(tmp_path / "soca_gridspec.nc")
    source = restart(tmp_path / "bkg")
    _widen(source, 1)

    post.state_record("bkg", source, grid, tmp_path / "bkg.nc",
                      cycle=1, member=0, valid="2015-01-05T01:00:00Z")
    with netCDF4.Dataset(tmp_path / "bkg.nc") as out:
        kept = out["sea_surface_height"][:]
    assert kept.shape[-1] == NX
    # The leading corner: columns 0..NX-1, not 1..NX.
    assert np.allclose(kept[0], np.arange(NX))


def test_a_shape_that_is_not_a_staggered_edge_is_refused_rather_than_dropped(tmp_path):
    """Two columns wide is not a staggering, it is a different grid.

    Skipping it silently is how a field disappears from an archive without
    anything saying so, and taking a corner of it anyway is how a record ends up
    describing cells it does not name.
    """
    grid = gridspec(tmp_path / "soca_gridspec.nc")
    source = restart(tmp_path / "bkg")
    _widen(source, 2)

    with pytest.raises(post.PostError, match="staggered grid's edge"):
        post.state_record("bkg", source, grid, tmp_path / "bkg.nc",
                          cycle=1, member=0, valid="2015-01-05T01:00:00Z")


def test_a_missing_gridspec_says_what_builds_it(tmp_path):
    source = restart(tmp_path / "bkg")
    with pytest.raises(post.PostError, match="soca-gridspec.sh"):
        post.state_record("bkg", source, tmp_path / "absent.nc",
                          tmp_path / "bkg.nc", cycle=1, member=0,
                          valid="2015-01-05T01:00:00Z")


def test_the_record_is_committed_by_rename(tmp_path):
    # Written to `.partial` and renamed, like every other output here, so a
    # killed job leaves no file rather than a truncated one that the skip rule
    # would then accept.
    target, _ = record(tmp_path)
    assert target.exists()
    assert not target.with_name(target.name + ".partial").exists()
