"""Tier 0: which members a cycle has, and what the policy does about the rest.

Three policies, three different experiments. The one that carries real risk is
`replace_from_mean`, because it writes a restart set: a member rebuilt wrongly
is a member that forecasts and assimilates for the rest of the run while looking
exactly like one that arrived.
"""

from pathlib import Path

import numpy as np
import pytest

from conftest import PATHS_CYCLE

netCDF4 = pytest.importorskip("netCDF4")

from ackbar import ensemble  # noqa: E402
from ackbar.mom6sis2 import STAMP, ModelError  # noqa: E402
from ackbar.paths import Paths  # noqa: E402
from ackbar.soca import GRIDSPEC  # noqa: E402

from test_writeback import METADATA, NX, NY, NZ, mask, write_gridspec  # noqa: E402

MEMBERS = (0, 1, 2, 3)
ENSEMBLE = (1, 2, 3)


def write_restart(path, value):
    """A restart whose analysis variables all carry one value."""
    ocean = mask()
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("Layer", NZ)
        data.createDimension("lath", NY)
        data.createDimension("lonh", NX)
        for name in ("Temp", "Salt", "h"):
            var = data.createVariable(name, "f8", ("Time", "Layer", "lath", "lonh"))
            var[:] = np.broadcast_to(np.where(ocean, value, -99.0), (NZ, NY, NX))
            var.checksum = "DEADBEEF"
        data.createVariable("ave_ssh", "f8", ("Time", "lath", "lonh"))[:] = \
            np.where(ocean, value, -99.0)


@pytest.fixture
def scene(tmp_path):
    """A cycle-1 ensemble whose cycle-0 backgrounds all exist, each distinct."""
    static = tmp_path / "static"
    static.mkdir()
    write_gridspec(static / GRIDSPEC)

    config = {
        "model": {"name": "mom6sis2", "restart": {"ocn": "MOM.res.nc"},
                  "fields metadata": str(METADATA)},
        "solver": {"name": "letkf",
                   "analysis variables": ["sea_water_potential_temperature",
                                          "sea_water_salinity",
                                          "sea_surface_height_above_geoid"]},
        "domain": {"name": "d", "static": str(static)},
        "ensemble": {"size": 3, "on_missing_member": "fail_cycle"},
    }
    paths = Paths(experiment="e", output_root=tmp_path / "o",
                  scratch_root=tmp_path / "s", **PATHS_CYCLE).ensure()
    for member in ENSEMBLE:
        target = paths.member_out("rst", 0, member)
        target.mkdir(parents=True)
        write_restart(target / "MOM.res.nc", float(member))
        (target / "ice_model.res.nc").write_bytes(b"sea ice\n")
        (target / STAMP).write_text("     4\n  2015 1 4 13 0 0\n  2015 1 5 1 0 0\n")
    return config, paths


def lose(paths, member):
    """A member whose forecast never finished: no `coupler.res`."""
    (paths.member_out("rst", 0, member) / STAMP).unlink()


def field(path, name, level=0):
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        values = np.asarray(data.variables[name][0])
    return values[level] if values.ndim == 3 else values


# --- what the ensemble is ----------------------------------------------------

def test_the_control_is_not_part_of_the_ensemble(scene):
    """`mem000` is in the member array and is not assimilated.

    Its analysis is the posterior mean, which the filter computes anyway, so
    including it would assimilate the mean of the ensemble as a member of it.
    """
    config, _ = scene
    assert ensemble.ensemble_members(config, MEMBERS) == ENSEMBLE
    # An experiment configured without a control has no member 0 to drop.
    assert ensemble.ensemble_members(config, ENSEMBLE) == ENSEMBLE


def test_a_member_without_a_coupler_res_is_not_available(scene):
    config, paths = scene
    lose(paths, 2)
    assert ensemble.available(paths, 1, ENSEMBLE) == (1, 3)


# --- the policies ------------------------------------------------------------

def test_a_complete_ensemble_is_recorded_as_one(scene):
    """The record is written when nothing is missing, too.

    "All three were there" belongs in the same file and the same shape as "one
    was not", because the question a comparison asks is which members ran and it
    should not have to know whether anything went wrong to find the answer.
    """
    config, paths = scene
    assert ensemble.resolve(config, paths, 1, ENSEMBLE) == ENSEMBLE
    record = ensemble.read(paths, 1)
    assert record["assimilated"] == [1, 2, 3]
    assert record["missing"] == [] and record["rebuilt"] == []


def test_fail_cycle_stops_and_names_the_member(scene):
    config, paths = scene
    lose(paths, 2)
    with pytest.raises(ModelError, match="mem002"):
        ensemble.resolve(config, paths, 1, ENSEMBLE)


def test_run_degraded_assimilates_what_arrived_and_records_the_rest(scene):
    """A smaller ensemble is a different analysis, so it is written down.

    Lower rank, more sampling noise and less spread, and the effect outlives the
    cycle that caused it. Two experiments differing in this are not comparable
    and nothing else would say so.
    """
    config, paths = scene
    config["ensemble"]["on_missing_member"] = "run_degraded"
    lose(paths, 2)
    assert ensemble.resolve(config, paths, 1, ENSEMBLE) == (1, 3)
    record = ensemble.read(paths, 1)
    assert record["assimilated"] == [1, 3]
    assert record["missing"] == [2]


def test_replace_from_mean_rebuilds_the_member(scene):
    """The ensemble stays the size it was configured with.

    The rebuilt member carries no independent information, so the rank does not
    really come back; what it buys is that the member exists, forecasts, and is
    at full strength again one cycle later rather than leaving a hole forever.
    """
    config, paths = scene
    config["ensemble"]["on_missing_member"] = "replace_from_mean"
    lose(paths, 2)
    assert ensemble.resolve(config, paths, 1, ENSEMBLE) == (1, 2, 3)

    rebuilt = paths.member_out("rst", 0, 2) / "MOM.res.nc"
    ocean = mask()
    # Members 1 and 3 carry 1.0 and 3.0, so the mean is 2.0.
    assert np.allclose(field(rebuilt, "Temp")[ocean], 2.0)
    assert np.allclose(field(rebuilt, "ave_ssh")[ocean], 2.0)
    assert ensemble.read(paths, 1)["rebuilt"] == [2]


def test_a_rebuilt_member_is_a_whole_restart_set(scene):
    """Not just the ocean file. A forecast handed a directory missing the ice
    state starts from a default and says nothing about it."""
    config, paths = scene
    config["ensemble"]["on_missing_member"] = "replace_from_mean"
    lose(paths, 2)
    ensemble.resolve(config, paths, 1, ENSEMBLE)

    target = paths.member_out("rst", 0, 2)
    assert (target / STAMP).exists()
    assert (target / "ice_model.res.nc").read_bytes() == b"sea ice\n"
    assert not list(target.glob("*.partial"))


def test_a_rebuilt_member_keeps_the_land_and_drops_its_checksum(scene):
    """It goes through the same writer an analysis does, and for the same
    reasons: the land mask is not the model's business to rediscover, and a
    checksum that no longer describes the data aborts the next forecast."""
    config, paths = scene
    config["ensemble"]["on_missing_member"] = "replace_from_mean"
    lose(paths, 2)
    ensemble.resolve(config, paths, 1, ENSEMBLE)

    rebuilt = paths.member_out("rst", 0, 2) / "MOM.res.nc"
    assert np.all(field(rebuilt, "Temp")[~mask()] == -99.0)
    with netCDF4.Dataset(rebuilt) as data:
        assert "checksum" not in data.variables["Temp"].ncattrs()
        # `h` was not rebuilt, so its claim about itself is still true.
        assert "checksum" in data.variables["h"].ncattrs()


def test_rebuilding_with_nothing_to_average_is_refused(scene):
    config, paths = scene
    config["ensemble"]["on_missing_member"] = "replace_from_mean"
    for member in ENSEMBLE:
        lose(paths, member)
    with pytest.raises(ModelError, match="nothing to build a mean from"):
        ensemble.resolve(config, paths, 1, ENSEMBLE)


def test_a_policy_nothing_implements_is_refused(scene):
    """Rather than silently taking the strictest one.

    The schema and this module both list the policies, and a value one allows
    and the other does not is a cycle that fails on a policy nobody wrote.
    """
    config, paths = scene
    config["ensemble"]["on_missing_member"] = "carry_on_regardless"
    with pytest.raises(ModelError, match="fail_cycle"):
        ensemble.resolve(config, paths, 1, ENSEMBLE)


def test_the_schema_and_this_module_list_the_same_policies():
    import yaml

    repo = Path(__file__).resolve().parents[1]
    schema = yaml.safe_load((repo / "config/schema/experiment.yaml").read_text())
    declared = schema["properties"]["ensemble"]["properties"]["on_missing_member"]["enum"]
    assert sorted(declared) == sorted(ensemble.POLICIES)
