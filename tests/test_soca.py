"""Tier 0: the run directory and the configuration a SOCA application reads.

Everything here is what `ackbar validate` cannot check, because it is assembled
by the job rather than written by a layer. JEDI has no parse-and-exit, so a
wrong value in this document is discovered by an application that has already
been allocated eight nodes.
"""

from pathlib import Path

import pytest
import yaml

from ackbar import soca
from ackbar.mom6sis2 import ModelError
from ackbar.paths import Paths

CONFIG = {
    "experiment": {"name": "e"},
    "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H", "count": 3},
    "domain": {"name": "d", "resources": {"hofx": {"ntasks": 4}}},
    "model": {
        "name": "mom6sis2",
        "namelist": "/ackbar/config/model/mom6sis2/mom_input.nml",
        "fields metadata": "/ackbar/config/model/mom6sis2/fields_metadata.yaml",
        "restart": {"ocn": "MOM.res.nc"},
        "state variables": ["sea_water_potential_temperature",
                            "sea_surface_height_above_geoid"],
    },
}


def observer(name="adt_3a", output="/out/e/obs_out/1/e.adt.nc4"):
    return {
        "name": name,
        "output": output,
        "config": {
            "obs space": {
                "name": name,
                "obsdataout": {"engine": {"type": "H5File", "obsfile": output}},
            },
            "obs operator": {"name": "ADT"},
        },
    }


@pytest.fixture
def config(tmp_path):
    """The config with a real case, a real static stage, and real overrides.

    The case's three parts are three directories here, as they are for a
    regional domain, because that is the shape that catches a path assumed
    rather than read.
    """
    base = tmp_path / "case"
    base.mkdir(parents=True)
    (base / "MOM_input").write_text("! MOM_input\n")
    # The case's own override, which SOCA must not read: it has to see the same
    # file the forecast sees, or the two build different geometries.
    (base / "MOM_override").write_text("! the case's own\n")

    data = tmp_path / "data" / "INPUT"
    data.mkdir(parents=True)
    (data / "ocean_hgrid.nc").write_bytes(b"netcdf\n")

    override = tmp_path / "override"
    override.mkdir()
    (override / "MOM_override").write_text("! ackbar's\nENABLE_BUGS_BY_DEFAULT = False\n")
    (override / "SIS_override").write_text("! ackbar's\n")
    # The SOCA-only override, which the forecast never links. On a regional
    # domain this is what keeps MOM6 from refusing the case's Flather
    # boundaries in SOCA's non-symmetric build.
    (override / "MOM_override.soca").write_text("#override OBC_NUMBER_OF_SEGMENTS = 0\n")

    static = tmp_path / "static"
    static.mkdir()
    (static / soca.GRIDSPEC).write_text("not really netcdf\n")

    merged = {k: dict(v) if isinstance(v, dict) else v for k, v in CONFIG.items()}
    merged["model"] = dict(CONFIG["model"], base=str(base), input=str(data),
                           override={n: str(override / n) for n in
                                     ("MOM_override", "SIS_override",
                                      "MOM_override.soca")})
    merged["domain"] = dict(CONFIG["domain"], static=str(static))
    return merged


@pytest.fixture
def paths(tmp_path):
    return Paths(experiment="e", output_root=tmp_path / "o",
                 scratch_root=tmp_path / "s").ensure()


# --- the configuration -------------------------------------------------------

def test_the_geometry_names_what_the_model_layer_names(config):
    document = soca.hofx_config(config, 1, [observer()], background=Path("/rst/0"))
    geometry = document["geometry"]
    assert geometry["mom6_input_nml"] == config["model"]["namelist"]
    assert geometry["fields metadata"] == config["model"]["fields metadata"]
    # The gridspec is the one relative name, because it is linked into the run
    # directory rather than opened where it lives.
    assert geometry["geom_grid_file"] == soca.GRIDSPEC


def test_the_state_is_the_background_it_was_handed(config):
    document = soca.hofx_config(
        config, 2, [observer()], background=Path("/out/e/rst/1/mem000"))
    state = document["state"]
    # The trailing separator is load-bearing: SOCA concatenates basename and
    # filename without inserting one, so its absence reads a file called
    # `mem000MOM.res.nc` in the parent directory.
    assert state["basename"] == "/out/e/rst/1/mem000/"
    assert state["ocn_filename"] == "MOM.res.nc"
    assert state["state variables"] == config["model"]["state variables"]


def test_the_date_and_the_window_are_the_cycles_own(config):
    document = soca.hofx_config(config, 2, [observer()], background=Path("/rst/1"))
    # Cycle 2 of a daily experiment starting 2018-04-15, with the window centred
    # on the analysis time so that consecutive windows tile.
    assert document["state"]["date"] == "2018-04-16T00:00:00Z"
    # The length is ACKBAR's own `window_length` symbol, spelled the fully
    # expanded way `oops::util::Duration` prints rather than the compact way an
    # experiment writes it. Both parse; only one is what a job actually sees.
    assert document["time window"] == {"begin": "2018-04-15T12:00:00Z",
                                       "length": "P1DT0H0M0S"}


def test_the_observers_go_where_oops_looks_for_them(config):
    document = soca.hofx_config(
        config, 1, [observer("adt_3a"), observer("sst_noaa19")],
        background=Path("/rst/0"))
    names = [o["obs space"]["name"] for o in document["observations"]["observers"]]
    assert names == ["adt_3a", "sst_noaa19"]


def test_a_model_layer_missing_a_value_says_so_rather_than_emitting_nothing(config):
    del config["model"]["state variables"]
    with pytest.raises(ModelError, match="state variables"):
        soca.hofx_config(config, 1, [observer()], background=Path("/rst/0"))


# --- the run directory -------------------------------------------------------

def test_the_run_directory_gets_the_cases_own_grid_files(config, paths, tmp_path):
    run = paths.scratch(1, "hofx")
    soca.stage(config, run, 1)
    for name in soca.CASE_FILES:
        assert (run / name).is_symlink()
        assert (run / name).resolve() == (Path(config["model"]["base"]) / name)
    assert (run / soca.GRIDSPEC).is_symlink()


def test_soca_reads_the_same_override_the_forecast_does(config, paths):
    """The analysis and the model have to agree about MOM6's parameters.

    SOCA initializes a real MOM6 geometry from `MOM_input` and `MOM_override`.
    If it read the case's override while the forecast read ACKBAR's, the two
    would agree about the grid only for as long as no override touched it, and
    the day one did the disagreement would surface as an interpolation that is
    quietly slightly wrong rather than as an error.
    """
    run = paths.scratch(1, "hofx")
    soca.stage(config, run, 1)
    expected = Path(config["model"]["override"]["MOM_override"])
    assert (run / "MOM_override").resolve() == expected.resolve()
    assert "ENABLE_BUGS_BY_DEFAULT = False" in (run / "MOM_override").read_text()
    assert "the case's own" not in (run / "MOM_override").read_text()


def test_the_data_half_of_the_case_is_a_path_and_not_base_input(config, paths):
    # A regional case keeps its grids under the static root, not inside the
    # text directory, so `base/INPUT` does not exist for it at all.
    run = paths.scratch(1, "hofx")
    soca.stage(config, run, 1)
    assert (run / "INPUT").resolve() == Path(config["model"]["input"]).resolve()
    assert not (Path(config["model"]["base"]) / "INPUT").exists()


def test_a_diag_table_exists_even_though_nothing_writes_diagnostics(config, paths):
    # FMS reads one inside the geometry constructor and its absence is a
    # segfault there rather than a message about a missing file.
    run = paths.scratch(1, "hofx")
    soca.stage(config, run, 1)
    lines = (run / "diag_table").read_text().splitlines()
    assert lines[1] == "2018 4 15 0 0 0"


def test_a_missing_gridspec_names_the_tool_that_writes_one(config, paths):
    """The failure a new domain hits first.

    "no such file" would send someone looking for a bug in the workflow; the
    answer is that an offline stage has not been run for this domain.
    """
    (Path(config["domain"]["static"]) / soca.GRIDSPEC).unlink()
    with pytest.raises(ModelError, match="soca-gridspec"):
        soca.stage(config, paths.scratch(1, "hofx"), 1)


def test_a_case_without_the_grid_files_is_an_error(config, paths):
    (Path(config["model"]["base"]) / "MOM_input").unlink()
    with pytest.raises(ModelError, match="MOM_input"):
        soca.stage(config, paths.scratch(1, "hofx"), 1)


# --- output ------------------------------------------------------------------

def test_the_application_writes_to_scratch_and_not_to_the_experiment(config, paths):
    """Otherwise a killed hofx leaves a truncated file where the next job looks.

    Every other task here commits by rename. This is the same rule applied to a
    value in a generated config rather than to a path in code.
    """
    records = [observer(output=str(paths.cycle_out("obs_out", 1) / "e.adt.nc4"))]
    staging = paths.scratch(1, "hofx") / "out"
    products = soca._redirect_output(records, staging)

    written = records[0]["config"]["obs space"]["obsdataout"]["engine"]["obsfile"]
    assert written == str(staging / "e.adt.nc4")
    assert products == [(staging / "e.adt.nc4",
                         paths.cycle_out("obs_out", 1) / "e.adt.nc4")]


def test_output_reaches_the_experiment_once_the_application_has_exited(config, paths):
    local = paths.scratch(1, "hofx") / "out" / "e.adt.nc4"
    final = paths.cycle_out("obs_out", 1) / "e.adt.nc4"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"ombg")

    assert soca.commit([(local, final)]) == [final]
    assert final.read_bytes() == b"ombg"
    assert not list(final.parent.glob("*.partial"))


def test_an_application_that_exits_zero_writing_nothing_still_fails(config, paths):
    local = paths.scratch(1, "hofx") / "out" / "e.adt.nc4"
    with pytest.raises(ModelError, match="no ombg"):
        soca.commit([(local, paths.cycle_out("obs_out", 1) / "e.adt.nc4")])


def test_no_observers_is_not_a_failure(config, paths, capsys):
    # An archive gap wide enough to drop everything. The realized list already
    # records it, and there is nothing to evaluate.
    assert soca.hofx(config, {}, paths, 1, "hofx",
                     background=Path("/rst/0"), observers=[]) == []
    assert "no observers" in capsys.readouterr().out
    assert not paths.scratch(1, "hofx").exists()


# --- the real layers ---------------------------------------------------------

def test_the_shipped_layers_produce_a_document_soca_would_accept(tmp_path):
    """The config files, not a fixture: every key SOCA needs is set by a layer.

    A missing one is caught here rather than by an application that has already
    read a grid.
    """
    from ackbar.config.layers import merge_layers, resolve_layers
    from ackbar.config.resolve import resolve
    from ackbar.config.schema import load_schema, merge_keys

    repo = Path(__file__).resolve().parents[1]
    layers = resolve_layers(repo / "tests/experiments/tier3_hofx.yaml",
                            repo / "config/layers")
    merged = resolve(merge_layers(layers, merge_keys(load_schema())), {
        "scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static", "root": str(repo),
    })
    document = soca.hofx_config(merged, 1, [observer()], background=Path("/rst/0"))
    # Round trip, because what the application reads is the file and not the
    # dictionary, and an unserializable value fails at the last moment.
    reread = yaml.safe_load(yaml.safe_dump(document))
    assert reread["geometry"]["fields metadata"].startswith(str(repo))
    assert reread["state"]["date"] == "1958-01-01T12:00:00Z"
