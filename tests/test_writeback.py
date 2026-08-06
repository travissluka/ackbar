"""Tier 0: turning an analysis into a restart set.

Every way of getting this wrong produces a file the model happily integrates,
which is why the assertions here are about the *contents* of a NetCDF file
rather than about which functions were called. The failure modes, in the order
they cost the most to discover: land written over with the analysis's fill
value, a staggered variable shifted by one cell, the background's other
variables lost, a checksum left claiming data that has changed, and a restart
set whose `coupler.res` arrives before the files it vouches for.
"""

from pathlib import Path

import numpy as np
import pytest

from conftest import PATHS_CYCLE
import yaml

netCDF4 = pytest.importorskip("netCDF4")

from ackbar import writeback  # noqa: E402
from ackbar.mom6sis2 import STAMP, ModelError  # noqa: E402
from ackbar.paths import Paths  # noqa: E402
from ackbar.soca import GRIDSPEC  # noqa: E402

NX, NY, NZ = 6, 5, 3

#: The real file, not a fixture of it. The mapping from a JEDI variable name to
#: a restart variable on a staggered grid is the model layer's statement, and a
#: test that restates it here would pass while the two disagreed.
METADATA = Path(__file__).resolve().parents[1] / "config/model/mom6sis2/fields_metadata.yaml"

VARIABLES = ["sea_water_potential_temperature",
             "sea_water_salinity",
             "sea_surface_height_above_geoid"]


def mask():
    """Ocean everywhere except a two-cell island, so land is testable."""
    out = np.ones((NY, NX), dtype=bool)
    out[1, 1] = out[1, 2] = False
    return out


def write_gridspec(path):
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("y", NY)
        data.createDimension("x", NX)
        ocean = mask().astype("f8")
        for name in ("mask2d", "mask2du", "mask2dv"):
            data.createVariable(name, "f8", ("Time", "y", "x"))[:] = ocean


def write_restart(path, value=1.0):
    """A restart with the two staggered variables one cell larger, as MOM6's is."""
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("Layer", NZ)
        data.createDimension("lath", NY)
        data.createDimension("lonh", NX)
        data.createDimension("latq", NY + 1)
        data.createDimension("lonq", NX + 1)
        for name in ("Temp", "Salt", "h"):
            var = data.createVariable(name, "f8", ("Time", "Layer", "lath", "lonh"))
            var[:] = value
            var.checksum = "DEADBEEF"
        data.createVariable("u", "f8", ("Time", "Layer", "lath", "lonq"))[:] = value
        data.createVariable("v", "f8", ("Time", "Layer", "latq", "lonh"))[:] = value
        ssh = data.createVariable("ave_ssh", "f8", ("Time", "lath", "lonh"))
        ssh[:] = value
        ssh.checksum = "DEADBEEF"


def write_analysis(path, value=9.0):
    """What SOCA writes: the analysis variables, with zero under the land."""
    ocean = mask()
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("zaxis_1", NZ)
        data.createDimension("yaxis_1", NY)
        data.createDimension("xaxis_1", NX)
        for name in ("Temp", "Salt"):
            field = np.where(ocean, value, 0.0)
            data.createVariable(name, "f8", ("Time", "zaxis_1", "yaxis_1", "xaxis_1"))[:] = \
                np.broadcast_to(field, (NZ, NY, NX))
        data.createVariable("ave_ssh", "f8", ("Time", "yaxis_1", "xaxis_1"))[:] = \
            np.where(ocean, value, 0.0)


@pytest.fixture
def scene(tmp_path):
    """A background restart set, an analysis, and somewhere to put the result."""
    static = tmp_path / "static"
    static.mkdir()
    write_gridspec(static / GRIDSPEC)

    background = tmp_path / "rst"
    background.mkdir()
    write_restart(background / "MOM.res.nc")
    # A second restart file the analysis never touches. Losing it is how a
    # forecast quietly starts from a default sea ice state.
    (background / "ice_model.res.nc").write_bytes(b"sea ice\n")
    (background / STAMP).write_text("     4\n  1958 1 1 0 0 0\n  1958 1 1 12 0 0\n")

    config = {
        "model": {"name": "mom6sis2", "restart": {"ocn": "MOM.res.nc"},
                  "fields metadata": str(METADATA)},
        "solver": {"name": "variational", "analysis variables": list(VARIABLES)},
        "domain": {"name": "d", "static": str(static)},
    }
    analysis = tmp_path / "ocn.ana.an.nc"
    write_analysis(analysis)
    paths = Paths(experiment="e", output_root=tmp_path / "o",
                  scratch_root=tmp_path / "s", **PATHS_CYCLE).ensure()
    return config, paths, background, analysis, paths.member_out("ana", 1, 0)


def run(scene, analysis="given"):
    config, paths, background, written, target = scene
    return writeback.writeback(
        config, paths, 1, 0, background=background,
        analysis=written if analysis == "given" else None, target=target)


def field(path, name):
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return np.asarray(data.variables[name][0])


# --- what lands in the restart -----------------------------------------------

def test_the_ocean_takes_the_analysis_and_the_land_keeps_the_background(scene):
    target = run(scene)
    ocean = mask()
    for name in ("Temp", "Salt"):
        values = field(target / "MOM.res.nc", name)
        assert np.all(values[:, ocean] == 9.0)
        # The analysis carries its fill value here, and it is not a temperature.
        assert np.all(values[:, ~ocean] == 1.0)
    ssh = field(target / "MOM.res.nc", "ave_ssh")
    assert np.all(ssh[ocean] == 9.0) and np.all(ssh[~ocean] == 1.0)


def test_what_the_analysis_did_not_solve_for_is_untouched(scene):
    """`h`, `u` and `v` are in the restart and not in `analysis variables`.

    Layer thickness especially: an analysis that silently rewrote it would
    change the vertical coordinate the temperature it just wrote is defined on.
    """
    target = run(scene)
    for name in ("h", "u", "v"):
        assert np.all(field(target / "MOM.res.nc", name) == 1.0)


def test_a_staggered_variable_would_be_written_on_its_own_grid(scene):
    """`u` and `v` are one cell larger than the analysis in one direction.

    Not exercised by the default `analysis variables`, and that is exactly why
    it is tested: the day velocity is added to that list, the shape mismatch has
    to be handled rather than discovered. The extra column of `u` is the model's
    western boundary face and belongs to no tracer cell.
    """
    config, paths, background, written, target = scene
    config["solver"]["analysis variables"] = ["eastward_sea_water_velocity",
                                              "northward_sea_water_velocity"]
    with netCDF4.Dataset(written, "r+") as data:
        for name, shape in (("u", (1, NZ, NY, NX)), ("v", (1, NZ, NY, NX))):
            data.createVariable(name, "f8", ("Time", "zaxis_1", "yaxis_1", "xaxis_1"))[:] = \
                np.broadcast_to(np.where(mask(), 9.0, 0.0), shape)

    writeback.writeback(config, paths, 1, 0, background=background,
                        analysis=written, target=target)
    ocean = mask()
    u = field(target / "MOM.res.nc", "u")
    assert u.shape == (NZ, NY, NX + 1)
    assert np.all(u[:, :, :NX][:, ocean] == 9.0)
    # The face the analysis has no value for keeps the background's.
    assert np.all(u[:, :, -1] == 1.0)
    v = field(target / "MOM.res.nc", "v")
    assert v.shape == (NZ, NY + 1, NX)
    assert np.all(v[:, :NY, :][:, ocean] == 9.0)
    assert np.all(v[:, -1, :] == 1.0)


def test_the_rest_of_the_restart_set_comes_across(scene):
    target = run(scene)
    assert (target / "ice_model.res.nc").read_bytes() == b"sea ice\n"
    assert (target / STAMP).exists()
    assert not list(target.glob("*.partial"))


def test_the_background_is_not_modified(scene):
    """The one property that makes a rerun safe.

    Writeback is idempotent only because its source never changes, so a job
    killed halfway and run again reads the same background rather than an
    analysis applied twice.
    """
    _, _, background, _, _ = scene
    before = field(background / "MOM.res.nc", "Temp").copy()
    run(scene)
    assert np.array_equal(field(background / "MOM.res.nc", "Temp"), before)


# --- the claims the file makes about itself ----------------------------------

def test_the_checksum_goes_from_what_changed_and_stays_on_what_did_not(scene):
    """MOM6 aborts on a checksum that no longer matches, and should.

    Dropping the attribute for the whole file would be the easy fix and would
    also switch off the check on every variable the analysis never touched,
    which is most of them.
    """
    target = run(scene)
    with netCDF4.Dataset(target / "MOM.res.nc") as data:
        assert "checksum" not in data.variables["Temp"].ncattrs()
        assert "checksum" not in data.variables["ave_ssh"].ncattrs()
        assert data.variables["h"].checksum == "DEADBEEF"


def test_a_non_finite_analysis_is_refused(scene):
    """A diverged minimization, caught here rather than by the forecast.

    A NaN in a restart is read without complaint and shows up as a model that
    fails several timesteps later somewhere unrelated.
    """
    config, paths, background, written, target = scene
    with netCDF4.Dataset(written, "r+") as data:
        values = data.variables["Temp"][:]
        values[0, 0, 0, 0] = np.nan
        data.variables["Temp"][:] = values
    with pytest.raises(ModelError, match="not finite"):
        writeback.writeback(config, paths, 1, 0, background=background,
                            analysis=written, target=target)


# --- the cycle that assimilated nothing --------------------------------------

def test_no_analysis_hands_the_background_across_and_says_so(scene, capsys):
    """A window with no observations. The analysis is the background.

    Silently correct rather than silently wrong, which is the reason it is
    printed: a cycle that did nothing and a cycle that did nothing *because
    there was nothing to do* look identical in every artifact except this line.
    """
    target = run(scene, analysis=None)
    assert np.all(field(target / "MOM.res.nc", "Temp") == 1.0)
    assert (target / STAMP).exists()
    assert "no analysis for this cycle" in capsys.readouterr().out


# --- refusals ----------------------------------------------------------------

def test_a_background_that_is_not_a_restart_set_is_refused(scene):
    config, paths, background, written, target = scene
    (background / STAMP).unlink()
    with pytest.raises(ModelError, match="not a restart set"):
        writeback.writeback(config, paths, 1, 0, background=background,
                            analysis=written, target=target)


def test_an_analysis_variable_the_model_has_no_name_for_is_refused(scene):
    config, paths, background, written, target = scene
    config["solver"]["analysis variables"] = ["sea_floor_temperature"]
    with pytest.raises(ModelError, match="fields metadata"):
        writeback.writeback(config, paths, 1, 0, background=background,
                            analysis=written, target=target)


def test_an_ice_analysis_variable_says_what_is_missing(scene):
    """Not "no such variable": the ice restart is a different file.

    Whoever adds the first ice observer arrives here, and the message is what
    tells them the work is a mask and a file rather than a bug.
    """
    config, paths, background, written, target = scene
    config["solver"]["analysis variables"] = ["sea_ice_area_fraction"]
    with pytest.raises(ModelError, match="only writes the ocean one"):
        writeback.writeback(config, paths, 1, 0, background=background,
                            analysis=written, target=target)


def test_a_missing_gridspec_names_the_tool_that_writes_one(scene):
    config, paths, background, written, target = scene
    (Path(config["domain"]["static"]) / GRIDSPEC).unlink()
    with pytest.raises(ModelError, match="soca-gridspec"):
        writeback.writeback(config, paths, 1, 0, background=background,
                            analysis=written, target=target)


def test_an_analysis_on_a_different_grid_is_refused(scene):
    """The failure a domain change produces, and it is otherwise silent.

    An analysis on the wrong grid has the wrong shape, and numpy would happily
    broadcast some of those.
    """
    config, paths, background, written, target = scene
    other = Path(written).with_name("other.nc")
    with netCDF4.Dataset(other, "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("z", NZ)
        data.createDimension("y", NY + 3)
        data.createDimension("x", NX + 3)
        data.createVariable("Temp", "f8", ("Time", "z", "y", "x"))[:] = 9.0
    with pytest.raises(ModelError, match="different grids"):
        writeback.writeback(config, paths, 1, 0, background=background,
                            analysis=other, target=target)


# --- the metadata this depends on --------------------------------------------

def test_the_model_layer_still_names_every_analysis_variable(scene):
    """Holds `fields_metadata.yaml` and every solver layer together.

    The analysis variables are stated in `config/layers/da/*.yaml` and the
    restart names for them in the model layer, and nothing else compares the
    two. Dropping an entry from the metadata is a writeback that fails in the
    middle of a cycle rather than a configuration that fails to load.
    """
    repo = Path(__file__).resolve().parents[1]
    fields = writeback.fields_of({"model": {"fields metadata": str(METADATA)}})
    for solver in ("variational", "letkf"):
        layer = yaml.safe_load(
            (repo / f"config/layers/da/{solver}.yaml").read_text())
        for name in layer["solver"]["analysis variables"]:
            assert name in fields, f"{solver}: {name}"
            assert fields[name]["io file"] in writeback.IO_FILES
            assert fields[name]["grid"] in writeback.MASKS
