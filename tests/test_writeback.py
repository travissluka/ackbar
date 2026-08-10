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

#: Cell side, in metres. A kilometre, so a divergence works out by hand.
CELL = 1000.0

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
        # Square cells a kilometre on a side, so a divergence works out by hand.
        for name, value in (("dx", CELL), ("dy", CELL), ("area", CELL * CELL)):
            data.createVariable(name, "f8", ("Time", "y", "x"))[:] = value


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


def test_a_vanished_layer_takes_the_increment_from_above(scene):
    """A collapsed Z* layer gets the increment of the last live layer over it.

    Under Z* every column carries all `NK` levels whether or not the seafloor
    leaves room, and the ones below the bottom hold a residual thickness. A
    third of the cells on gom_25km are in that state. Nothing constrains what an
    ensemble covariance says about them, so its increment there is meaningless
    and must not be used.

    Leaving the cell at its background value is *not* the right way to not use
    it. The live layer above moves and the dead one does not, which puts a step
    into the column, and a step in temperature is a density inversion the model
    convects away. Copying the increment down keeps the background's own
    vertical gradient through the dead part instead, so nothing is destabilised.
    """
    config, paths, background, analysis, target = scene
    with netCDF4.Dataset(background / "MOM.res.nc", "r+") as data:
        thickness = np.asarray(data.variables["h"][:])
        thickness[0, -1, 2, 3] = writeback.VANISHED / 10.0
        data.variables["h"][:] = thickness

    # The analysis has to vary by level or the fill is invisible: give the dead
    # cell an absurd value and the live cell above it an ordinary one.
    with netCDF4.Dataset(analysis, "r+") as data:
        for name in ("Temp", "Salt"):
            values = np.asarray(data.variables[name][:])
            values[0, -1, 2, 3] = 100.0
            values[0, -2, 2, 3] = 5.0
            data.variables[name][:] = values

    run(scene)
    for name in ("Temp", "Salt"):
        values = field(target / "MOM.res.nc", name)
        # Background 1.0, live layer above analysed to 5.0, so the increment
        # carried down is +4.0 and the dead cell lands on 5.0. Neither its own
        # analysed 100.0 nor the untouched background 1.0.
        assert values[-1, 2, 3] == 5.0, f"{name}: dead cell took the wrong value"
        # The same column at the surface is ordinary ocean.
        assert values[0, 2, 3] == 9.0
        # And a neighbouring column at the same level is live, so it keeps its own.
        assert values[-1, 2, 4] == 9.0

    # `ave_ssh` has no level to vanish, so the rule cannot reach it.
    assert field(target / "MOM.res.nc", "ave_ssh")[2, 3] == 9.0


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

    The extra column of `u` belongs to no tracer cell, so the analysis has no
    value for it. It takes the increment of the face beside it rather than
    keeping the background, and the difference is not cosmetic: every face
    inside moving while the outermost stays put is a step in the velocity field
    at the domain edge, and on a regional domain that edge is an open boundary
    where the OBC is imposing its own solution.

    Copying the neighbour is the weakest assumption that does not invent a
    gradient. Leaving the face alone is not neutral, it asserts the increment
    falls to zero over half a cell.
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
    # The outer face takes the increment of the column beside it, so it lands
    # where that column landed: background 1.0 plus an increment of 8.0. The
    # last tracer column is ocean for every row, the island being inland.
    assert np.all(u[:, :, -1] == 9.0)
    v = field(target / "MOM.res.nc", "v")
    assert v.shape == (NZ, NY + 1, NX)
    assert np.all(v[:, :NY, :][:, ocean] == 9.0)
    assert np.all(v[:, -1, :] == 9.0)


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
    for solver in ("variational", "letkf", "hybrid"):
        layer = yaml.safe_load(
            (repo / f"config/layers/da/{solver}.yaml").read_text())
        for name in layer["solver"]["analysis variables"]:
            assert name in fields, f"{solver}: {name}"
            assert fields[name]["io file"] in writeback.IO_FILES
            assert fields[name]["grid"] in writeback.MASKS


def test_the_ensemble_solvers_analyse_the_same_variables():
    """`da/hybrid` and `da/letkf` correct the same fields, and have to.

    `osse-letkf`, `osse-envar` and `osse-hybrid` are meant to differ by their
    covariance and by nothing else, and `da/envar` is `da/hybrid` with one key
    changed. A pair that analyses different variables is a pair whose velocity
    scores are not comparable, and nothing downstream would say so: the run with
    the shorter list simply leaves those fields at the background and reports a
    healthy cycle.

    Every analysis variable also has to be a background variable, on both sides.
    A field the solver writes and the background never read is one the increment
    has nowhere to come from, and for the ensemble half it is a variable the
    localization names in its group and the member states do not carry.
    """
    repo = Path(__file__).resolve().parents[1]
    layers = {name: yaml.safe_load(
        (repo / f"config/layers/da/{name}.yaml").read_text())["solver"]
        for name in ("letkf", "hybrid")}
    assert (layers["hybrid"]["analysis variables"]
            == layers["letkf"]["analysis variables"])
    for name, solver in layers.items():
        assert set(solver["analysis variables"]) <= set(
            solver["background variables"]), name


def velocities(analysis, u, v):
    """Give the analysis a velocity pair, on the tracer grid SOCA writes."""
    with netCDF4.Dataset(analysis, "r+") as data:
        for name, values in (("u", u), ("v", v)):
            data.createVariable(name, "f8",
                                ("Time", "zaxis_1", "yaxis_1", "xaxis_1"))[:] = \
                np.where(mask(), values, 0.0)


def test_a_divergent_velocity_increment_is_scaled_by_what_the_column_can_take(scene):
    """The velocity increment is bounded by how fast it drains a column.

    Every forecast this workflow has lost to an analysis increment died in
    `btstep: eta has dropped below bathyT`, and always in a thin column. Damping
    the increment in shallow water treats the symptom: depth is a proxy, and it
    also throws away a strong alongshore increment the column could carry
    perfectly well because it moves no water in or out.

    What empties the column is the divergence of the transport the increment
    implies, `d(eta)/dt = -div(sum_k h_k du_k)/A`, so that is what is bounded, as
    a fraction of the column's own depth per hour. One number then covers a 10 m
    shelf cell and a 3000 m basin.
    """
    config, paths, background, analysis, target = scene
    config["solver"]["analysis variables"] = ["eastward_sea_water_velocity",
                                              "northward_sea_water_velocity"]
    config["solver"]["increment divergence limit"] = 0.05

    # A jet that stops dead partway across: the faces west of column 3 all carry
    # the same increment and the ones east of it carry none, so column 3 is
    # purely convergent. Everywhere else the two faces cancel exactly.
    jet = np.zeros((NZ, NY, NX))
    jet[:, :, :3] = 5.0
    velocities(analysis, jet, np.ones((NZ, NY, NX)))

    run(scene)
    u = field(target / "MOM.res.nc", "u")
    ocean = mask()
    # Background 1.0, so the untouched increment at the convergent face is +4.0.
    # A column NZ metres deep may lose 5% of NZ metres an hour, which is orders
    # of magnitude less than a 4 m/s jet across a kilometre delivers, so what
    # survives is a small fraction of what was asked for.
    moved = (u[:, :, :NX] - 1.0)[:, ocean]
    assert np.abs(moved).max() < 0.1, f"the divergent face kept {moved.max()}"
    assert np.all(np.isfinite(u))


def test_the_divergence_limit_leaves_a_uniform_increment_alone(scene):
    """A velocity increment that moves no water in or out is not touched.

    This is the whole reason the bound is on divergence and not on depth: a
    uniform along-shelf increment in 10 m of water is harmless, and a depth
    taper would throw it away along with the dangerous one.
    """
    config, paths, background, analysis, target = scene
    config["solver"]["analysis variables"] = ["eastward_sea_water_velocity",
                                              "northward_sea_water_velocity"]
    config["solver"]["increment divergence limit"] = 0.05

    velocities(analysis, np.full((NZ, NY, NX), 9.0), np.ones((NZ, NY, NX)))

    run(scene)
    u = field(target / "MOM.res.nc", "u")
    # Every face in a row takes the same increment, so nothing diverges and
    # nothing is cut, at 9.0 against a background of 1.0 in three metres of
    # water. Row 1 is excluded because the island is in it: an increment that
    # stops at a coast is divergent there, and the limiter is right to cut it.
    rows = [j for j in range(NY) if j != 1]
    assert np.all(u[:, rows, :NX] == 9.0)


# --- the per-variable increment limit ----------------------------------------
#
# `place`'s soft bound runs inside every analysis of every member of every
# cycle, and `config/layers/da/letkf.yaml` sets it with values tuned against
# measurements quoted in that file. Until these tests it had never executed
# here: `apply_analysis` passes `limit=limits.get(...)`, no test config set
# `increment limits`, so every call in this file ran with `limit=None`.
#
# It fails quietly by construction. Nothing crashes, the restart integrates, and
# the only evidence is one log line that reads plausibly whatever the arithmetic
# did. So these assert the arithmetic by hand rather than through a scenario.


class FakeVariable:
    """The little of a NetCDF variable `place` uses, over a plain array.

    A real file would do, and these assertions are about arithmetic rather than
    about NetCDF, so the shape of the input is worth having in the test body
    instead of in a fixture three screens away.
    """

    def __init__(self, values):
        self.values = np.array(values, dtype="f8")[None, ...]

    def __getitem__(self, index):
        return self.values[index]

    def __setitem__(self, index, value):
        self.values[index] = value

    def ncattrs(self):
        return []


class FakeRestart:
    def __init__(self, values):
        self.variables = {"Temp": FakeVariable(values)}


TEMP = {"name": "sea_water_potential_temperature", "io name": "Temp",
        "grid": "h"}


def bounded(background, analysis, limit, relaxation=1.0):
    """`place` over a row of all-ocean cells. Returns (written, report)."""
    background = np.atleast_2d(background)
    target = FakeRestart(background)
    report = writeback.place(
        target, TEMP, np.ones(background.shape, dtype=bool),
        np.atleast_2d(analysis).astype("f8"),
        limit=limit, relaxation=relaxation)
    return target.variables["Temp"].values[0], report


def test_the_bound_is_on_the_increment_and_not_on_the_state():
    """A temperature is not wrong for being 30 degrees. It is wrong for having
    moved 30 degrees in one analysis.

    The likeliest way this breaks: a refactor reaching for the state instead of
    the increment passes every other assertion in this block.
    """
    written, _ = bounded(30.0, 31.0, limit=0.5)
    assert written[0, 0] == pytest.approx(30.0 + 0.5 * np.tanh(2.0))
    assert written[0, 0] == pytest.approx(30.4820, abs=1e-4)


def test_the_count_is_taken_before_the_damping():
    """After `tanh` nothing exceeds the bound, so a count taken one line later
    is permanently zero.

    That count is the number `place`'s own comment says to watch: a handful of
    points is a filter meeting the bound at the edges, a large fraction is an
    analysis not to be trusted. Its failure mode is a plausible zero.
    """
    # Three of the four move past the bound; the fourth stays well inside it.
    _, report = bounded([0.0, 0.0, 0.0, 0.0], [2.0, 3.0, 4.0, 0.01], limit=0.5)
    assert "4 ocean point(s)" in report
    assert "3 point(s) over the 0.5 limit" in report


def test_the_bound_is_soft_so_two_points_past_it_stay_ordered():
    """What a hard clip would destroy, and why the comment argues for tanh.

    `np.clip` returns the bound for both, flattening real structure into a
    plateau and manufacturing a gradient at its edge. Spurious gradients driving
    spurious dynamics is the failure the limiter exists to prevent, so an
    instrument that makes more of them is the wrong one.
    """
    written, _ = bounded([0.0, 0.0], [4 * 0.5, 8 * 0.5], limit=0.5)
    near, far = written[0]
    assert near < 0.5 and far < 0.5
    assert near < far, "a hard clip would return the bound for both"


def test_relaxation_runs_before_the_bound():
    """The two orders differ, and the call site is explicit about which.

    Relaxation is the shape-preserving part and acts on what the filter
    produced; the bound is the tail guard and acts on what is about to be
    written.
    """
    written, _ = bounded(0.0, 2 * 0.5, limit=0.5, relaxation=0.5)
    assert written[0, 0] == pytest.approx(0.5 * np.tanh(1.0))
    assert written[0, 0] != pytest.approx(0.5 * 0.5 * np.tanh(2.0)), \
        "the bound ran before the relaxation"


def test_no_limit_leaves_the_increment_exactly_alone():
    """The path stays opt-in: an experiment that sets nothing gets the analysis
    it was given, to the bit."""
    written, report = bounded(30.0, 31.0, limit=None)
    assert written[0, 0] == 31.0
    assert "limit" not in report


def test_the_limit_reaches_the_staggered_outer_face(scene):
    """The outer face takes its neighbour's increment *after* the bound.

    Every other staggered test here runs with no limit, so nothing pinned that
    the value written to that column is the damped one rather than the raw one.
    On a regional domain that face is the open boundary.
    """
    config, paths, background, analysis, target = scene
    config["solver"]["analysis variables"] = ["eastward_sea_water_velocity"]
    config["solver"]["increment limits"] = {
        "eastward_sea_water_velocity": 0.5}
    velocities(analysis, np.full((NZ, NY, NX), 9.0), np.zeros((NZ, NY, NX)))

    run(scene)
    u = field(target / "MOM.res.nc", "u")
    wet = mask()[:, -1]
    outer = u[:, wet, NX]
    assert np.all(np.abs(outer - 1.0) <= 0.5 + 1e-12), \
        "the outer face took the raw increment rather than the bounded one"
    assert np.any(np.abs(outer - 1.0) > 0.4), \
        "the outer face took no increment at all, so this proves nothing"


# --- the mass field ----------------------------------------------------------

THICKNESS = "sea_water_cell_thickness"


def thicknesses(path, values):
    """Give the analysis file an `h`, which `write_analysis` does not write.

    Only an ensemble filter's analysis carries one: it is a background variable
    everywhere, and an analysis variable only where the increment to it exists.
    """
    with netCDF4.Dataset(path, "r+") as data:
        var = data.variables.get("h")
        if var is None:
            var = data.createVariable(
                "h", "f8", ("Time", "zaxis_1", "yaxis_1", "xaxis_1"))
        var[:] = values


def layered(base):
    """A background thickness that differs by level, so a scaling is visible."""
    column = base * (1.0 + np.arange(NZ, dtype="f8"))
    return np.broadcast_to(column.reshape(NZ, 1, 1), (NZ, NY, NX)).copy()


def with_thickness(scene, analysis_h, background_h=None):
    """Run with `h` among the analysis variables. Returns the written field."""
    config, paths, background, written, target = scene
    config["solver"]["analysis variables"] = list(VARIABLES) + [THICKNESS]
    if background_h is not None:
        with netCDF4.Dataset(background / "MOM.res.nc", "r+") as data:
            data.variables["h"][:] = background_h
    thicknesses(written, analysis_h)
    run(scene)
    return field(target / "MOM.res.nc", "h")


def test_the_column_takes_the_analysis_total(scene):
    """What the write is for: the sea level the filter inferred.

    Under `BOUSSINESQ = True` a column's free surface is `sum(h) - D`, so the
    column integral *is* the sea level, and moving it is the entire purpose.
    """
    background = layered(1.0)
    analysis = background * 1.004
    written = with_thickness(scene, analysis, background)
    ocean = mask()
    assert written.sum(axis=0)[ocean] == pytest.approx(
        analysis.sum(axis=0)[ocean])


def test_the_column_keeps_its_shape(scene):
    """Only the integral moves; the layer proportions are the background's.

    The filter also redistributed mass between layers and that part is dropped
    rather than approximated. Under Z* the model regrids on its first step, so
    the interfaces are the coordinate's business and the integral is the state.
    """
    background = layered(1.0)
    # An analysis that inverts the profile while barely changing the total. A
    # per-layer write would follow it; a column scaling must not.
    analysis = background[::-1] * 1.001
    written = with_thickness(scene, analysis, background)
    ocean = mask()
    ratio = written[:, ocean] / background[:, ocean]
    assert ratio.max() - ratio.min() < 1e-12, \
        "the layers moved relative to each other, so this is not a scaling"


def test_an_analysis_layer_at_zero_does_not_reach_the_restart(scene):
    """The reason for the scaling, and the crash it exists to avoid.

    Writing `h_ana` cell by cell is the obvious form. On `gom_25km` the analysis
    itself puts a fraction of a percent of cells at or below zero thickness, and
    MOM6 reports that several steps downstream as `adjust_interface_motion:
    implied h<0`. The scaling cannot produce one while the column total is
    positive, whatever the individual levels say.
    """
    background = layered(1.0)
    analysis = background.copy()
    analysis[1, 2, 3] = -0.5
    written = with_thickness(scene, analysis, background)
    assert np.all(written[:, mask()] > 0.0)
    # And the column still took the total it was asked for, so the guard is the
    # form of the write rather than the analysis having been ignored.
    assert written[:, 2, 3].sum() == pytest.approx(analysis[:, 2, 3].sum())


def test_a_column_the_analysis_empties_is_refused(scene):
    """A negative total is a state the model has no representation for, and it
    is refused rather than written as something else."""
    background = layered(1.0)
    analysis = background.copy()
    analysis[:, 2, 3] = -50.0
    config, paths, _, written_file, _ = scene
    with pytest.raises(ModelError, match="negative"):
        with_thickness(scene, analysis, background)


def test_the_land_keeps_its_background_thickness(scene):
    """The analysis carries a fill value on land, and a scale factor built from
    one is not a number the restart should take."""
    background = layered(1.0)
    analysis = np.where(mask(), background * 1.004, 0.0)
    written = with_thickness(scene, analysis, background)
    assert np.all(written[:, ~mask()] == background[:, ~mask()])


def test_relaxation_reaches_the_column_delta(scene):
    """Written weaker on the same rule as every other field."""
    config = scene[0]
    config["solver"]["increment relaxation"] = 0.5
    background = layered(1.0)
    analysis = background * 1.004
    written = with_thickness(scene, analysis, background)
    ocean = mask()
    moved = written.sum(axis=0)[ocean] - background.sum(axis=0)[ocean]
    whole = analysis.sum(axis=0)[ocean] - background.sum(axis=0)[ocean]
    assert moved == pytest.approx(0.5 * whole)


def test_the_thickness_checksum_is_dropped(scene):
    """The file's claim about `h` is false once it has been scaled."""
    background = layered(1.0)
    with_thickness(scene, background * 1.004, background)
    config, paths, _, _, target = scene
    with netCDF4.Dataset(target / "MOM.res.nc") as data:
        assert "checksum" not in data.variables["h"].ncattrs()


def test_thickness_is_untouched_when_it_is_not_an_analysis_variable(scene):
    """The split by solver, enforced where it is decided.

    A variational analysis over the static B loses nothing by skipping this: its
    SSH increment is steric by construction, so the temperature and salinity
    increment already *is* the sea level increment and the model realizes the
    height over the following hours. Writing `h` as well would apply the same
    information twice. So the mass field is written when, and only when, the
    experiment names it.
    """
    background = layered(1.0)
    config, paths, rst, written_file, target = scene
    with netCDF4.Dataset(rst / "MOM.res.nc", "r+") as data:
        data.variables["h"][:] = background
    thicknesses(written_file, background * 1.5)
    run(scene)
    assert np.all(field(target / "MOM.res.nc", "h") == background)
