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


# --- the analysis's configuration --------------------------------------------

SOLVER = {
    "name": "variational",
    "analysis variables": ["sea_water_potential_temperature",
                           "sea_water_salinity",
                           "sea_surface_height_above_geoid"],
    "background variables": ["sea_water_potential_temperature",
                             "sea_water_salinity",
                             "sea_surface_height_above_geoid",
                             "sea_water_cell_thickness",
                             "ocean_mixed_layer_thickness"],
    "background error": {
        "covariance model": "SABER",
        "saber central block": {"saber block name": "diffusion"},
        "linear variable change": {
            "linear variable changes": [
                {"linear variable change name": "BalanceSOCA"},
            ],
        },
    },
    "variational": {
        "minimizer": {"algorithm": "RPCG"},
        "iterations": [{"ninner": 10, "gradient norm reduction": 1.0e-10}],
    },
}


@pytest.fixture
def var(config):
    return dict(config, solver=dict(SOLVER))


def test_the_analysis_reads_the_background_error_the_layer_describes(var):
    document = soca.var_config(var, 1, [observer()], background=Path("/rst/0"))
    error = document["cost function"]["background error"]
    assert error["saber central block"] == SOLVER["background error"]["saber central block"]


def test_the_balance_operators_variable_lists_are_filled_in(var):
    """Absent from the layer, and not optional.

    Without `input variables`, `oops::ModelSpaceCovarianceBase` holds a null
    pointer and dereferences it the first time it evaluates Jb, which is after
    the background has been read and every saber block reported. The answer is
    always the analysis variables, so it is built rather than stated a third
    time.
    """
    document = soca.var_config(var, 1, [observer()], background=Path("/rst/0"))
    change = document["cost function"]["background error"]["linear variable change"]
    assert change["input variables"] == SOLVER["analysis variables"]
    assert change["output variables"] == SOLVER["analysis variables"]
    # The layer's own content survives beside them.
    assert change["linear variable changes"][0]["linear variable change name"] == "BalanceSOCA"


def test_the_layer_is_not_mutated_by_being_read(var):
    """`var_config` runs once per cycle in a job that also reads the config.

    A builder that edited the config in place would leave every later cycle
    reading a document assembled from an earlier one's leftovers.
    """
    soca.var_config(var, 1, [observer()], background=Path("/rst/0"))
    assert "input variables" not in var["solver"]["background error"]["linear variable change"]
    assert "geometry" not in var["solver"]["variational"]["iterations"][0]


def test_every_inner_loop_gets_a_geometry(var):
    """`CostFunction::linearize` reads one per outer iteration and throws
    without it. It is the outer geometry: no multi-resolution analysis here."""
    var["solver"]["variational"]["iterations"] = [{"ninner": 5}, {"ninner": 3}]
    document = soca.var_config(var, 1, [observer()], background=Path("/rst/0"))
    outer = document["cost function"]["geometry"]
    assert [entry["geometry"] for entry in document["variational"]["iterations"]] == \
        [outer, outer]


def test_the_background_is_the_state_it_was_handed_not_the_analysis(var):
    document = soca.var_config(var, 2, [observer()],
                               background=Path("/out/e/rst/1/mem000"))
    background = document["cost function"]["background"]
    assert background["basename"] == "/out/e/rst/1/mem000/"
    assert background["date"] == "2018-04-16T00:00:00Z"
    # The superset, because the background error blocks read fields the analysis
    # never solves for.
    assert background["state variables"] == SOLVER["background variables"]
    assert document["cost function"]["analysis variables"] == SOLVER["analysis variables"]


def test_an_analysis_is_written_and_so_is_the_increment(var):
    """Both, and the analysis for two reasons rather than one.

    `oops::Variational` runs its final cost evaluation only when something asks
    for output, and `CostJo` saves `oman` on that evaluation and nowhere else.
    An analysis configured without an output therefore writes `ombg`, no `oman`,
    and no complaint.
    """
    document = soca.var_config(var, 1, [observer()], background=Path("/rst/0"))
    assert document["output"] == {"datadir": "out", "exp": "ana", "type": "an",
                                  "date colons": False}
    written = document["final"]["increment"]["output"]
    # `state component`, because a ControlIncrement hands each of its three
    # parts its own subsection and would otherwise find no `datadir`.
    assert written["state component"]["type"] == "incr"


def test_the_file_the_analysis_writes_is_the_file_writeback_opens(var):
    """One construction, used by both ends.

    SOCA builds the name from `datadir`, `exp`, `type` and a date format, and
    two spellings of that is a writeback that finds nothing and says the cycle
    assimilated nothing.
    """
    document = soca.var_config(var, 2, [observer()], background=Path("/rst/1"))
    assert soca.analysis_file(var, 2) == "ocn.ana.an.20180416T000000Z.nc"
    assert document["output"]["exp"] == soca.ANALYSIS[0]


def test_the_window_is_the_cycles_own(var):
    document = soca.var_config(var, 2, [observer()], background=Path("/rst/1"))
    assert document["cost function"]["time window"] == {
        "begin": "2018-04-15T12:00:00Z", "length": "P1DT0H0M0S"}


def test_a_declared_window_is_the_one_the_analysis_gets(var):
    """Still centred on the analysis time, which is where FGAT writes it."""
    var["solver"]["window"] = {"type": "3d", "length": "PT6H"}
    document = soca.var_config(var, 2, [observer()], background=Path("/rst/1"))
    assert document["cost function"]["time window"] == {
        "begin": "2018-04-15T21:00:00Z", "length": "P0DT6H0M0S"}


def test_a_window_this_document_cannot_honour_is_refused_not_ignored(var):
    """The forecast has already paid for it by the time this runs.

    A 4D window makes the cycling forecast run half a window longer and write a
    state per sub-window. Building 3D-Var over that is a 50 per cent model bill
    for a trajectory nothing reads and an analysis nobody chose, and neither
    leaves a mark in the output.
    """
    var["solver"]["window"] = {"type": "fgat"}
    with pytest.raises(ModelError, match="3D-Var"):
        soca.var_config(var, 1, [observer()], background=Path("/rst/0"))


def test_a_solver_missing_a_value_says_so_rather_than_emitting_nothing(var):
    del var["solver"]["background error"]
    with pytest.raises(ModelError, match="background error"):
        soca.var_config(var, 1, [observer()], background=Path("/rst/0"))


def test_a_variational_section_with_no_outer_loop_is_refused(var):
    # The application would run, return the background, and write an analysis
    # equal to it.
    var["solver"]["variational"] = {"minimizer": {"algorithm": "RPCG"},
                                    "iterations": []}
    with pytest.raises(ModelError, match="no outer loop"):
        soca.var_config(var, 1, [observer()], background=Path("/rst/0"))


def test_a_cycle_with_no_observers_is_not_an_analysis(var, paths, capsys):
    """The archive gap, at the analysis rather than at hofx.

    The analysis in a window with nothing in it is the background, and running
    the minimizer against an empty observer set to reach that answer is the same
    result at the price of a whole cycle's risk.
    """
    assert soca.analysis(var, {}, paths, 1, "da", background=Path("/rst/0"),
                         observers=[], target=paths.member_out("ana", 1, 0)) == []
    assert "no observers" in capsys.readouterr().out
    assert not paths.scratch(1, "da").exists()


# --- the ensemble analysis's configuration -----------------------------------

LETKF = {
    "name": "letkf",
    "analysis variables": SOLVER["analysis variables"],
    "background variables": ["sea_water_potential_temperature",
                             "sea_water_salinity",
                             "sea_surface_height_above_geoid",
                             "sea_water_cell_thickness"],
    "local ensemble DA": {"solver": "Deterministic LETKF",
                          "inflation": {"rtps": 0.5}},
    "ensemble distribution": {"name": "Halo", "halo size": 500000},
}


@pytest.fixture
def ens(config):
    return dict(config, solver=dict(LETKF))


def letkf_document(ens, members=(1, 2, 3)):
    return soca.letkf_config(ens, 1, [observer()],
                             backgrounds=Path("/out/e/rst/0"), members=members)


def test_the_ensemble_is_a_list_of_members_and_not_a_template(ens):
    """oops takes either, and the list is the one that cannot drift.

    A template's `%mem%` expands by *position*, so an ensemble with a gap in it
    needs an `except` and the index a member is written out as stops being its
    own number. A list is verbose in a file nobody hand-edits, and in exchange
    every member's background is a path `ackbar validate` stats up front.
    """
    document = letkf_document(ens)
    members = document["background"]["members"]
    assert [entry["basename"] for entry in members] == [
        "/out/e/rst/0/mem001/", "/out/e/rst/0/mem002/", "/out/e/rst/0/mem003/"]
    assert "members from template" not in document["background"]


def test_a_gap_in_the_ensemble_is_carried_as_a_shorter_list(ens):
    """What the divergence policy produces, and it must not renumber anything.

    The member directories are still the members' own; only the count changes.
    """
    document = letkf_document(ens, members=(1, 3))
    assert [entry["basename"] for entry in document["background"]["members"]] == [
        "/out/e/rst/0/mem001/", "/out/e/rst/0/mem003/"]


def test_every_member_reads_the_same_variables(ens):
    for entry in letkf_document(ens)["background"]["members"]:
        assert entry["state variables"] == LETKF["background variables"]
        assert entry["date"] == "2018-04-15T00:00:00Z"
        assert entry["ocn_filename"] == "MOM.res.nc"


def test_the_driver_asks_for_what_the_workflow_needs(ens):
    """Three flags that are not optional, for three different reasons.

    `do posterior observer` is what computes `oman`; without it the cycle
    produces departures against the background only. `save posterior mean` is
    what gives the control member an analysis at all. `save posterior ensemble`
    is the analysis itself.
    """
    driver = letkf_document(ens)["driver"]
    assert driver["do posterior observer"] is True
    assert driver["save posterior mean"] is True
    assert driver["save posterior ensemble"] is True


def test_every_output_the_driver_asks_for_is_configured(ens):
    """`LocalEnsembleDA` throws by name when a flag is set and its block is not.

    The one failure in this document that is loud, which is why it is worth a
    test that the two lists agree rather than a comment saying they should.
    """
    document = letkf_document(ens)
    required = {
        "save posterior mean": "output",
        "save posterior ensemble": "output",
        "save posterior mean increment": "output increment",
        "save prior variance": "output variance prior",
        "save posterior variance": "output variance posterior",
    }
    for flag, block in required.items():
        assert document["driver"].get(flag) is True, flag
        assert block in document, f"{flag} is set and {block} is missing"


def test_the_spread_diagnostics_are_named_apart(ens):
    """Prior and posterior spread and the increment cannot share a filename.

    All three are written with `member` set to zero, into one directory, so
    `exp` is the only thing that distinguishes them.
    """
    document = letkf_document(ens)
    names = {document[block]["exp"] for block in
             ("output increment", "output variance prior",
              "output variance posterior")}
    assert len(names) == 3


def test_the_solver_and_its_inflation_come_from_the_layer(ens):
    assert letkf_document(ens)["local ensemble DA"] == LETKF["local ensemble DA"]


def test_an_empty_ensemble_is_refused_rather_than_configured(ens):
    """Every member's forecast is missing and the policy let the cycle run.

    An ensemble filter with no members is not a degenerate analysis, it is an
    application that will abort somewhere less informative.
    """
    with pytest.raises(ModelError, match="ensemble is empty"):
        letkf_document(ens, members=())


def test_the_shipped_letkf_layers_produce_a_document_soca_would_accept(tmp_path):
    repo, merged = shipped("tier3_letkf.yaml")
    document = soca.letkf_config(merged, 1, [observer()],
                                 backgrounds=Path("/out/e/rst/0"),
                                 members=(1, 2, 3, 4, 5, 6))
    reread = yaml.safe_load(yaml.safe_dump(document))
    assert reread["geometry"]["fields metadata"].startswith(str(repo))
    assert len(reread["background"]["members"]) == 6
    assert reread["local ensemble DA"]["solver"] == "Deterministic LETKF"
    # The halo is the distribution's own parameter, not an `options` bag: ioda
    # reads it directly under `distribution` and refuses to construct without it.
    space = reread["observations"]["observers"][0]["obs space"]
    assert space.get("distribution", {}).get("name", "RoundRobin")


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
    with pytest.raises(ModelError, match="wrote no "):
        soca.commit([(local, paths.cycle_out("obs_out", 1) / "e.adt.nc4")])


def test_no_observers_is_not_a_failure(config, paths, capsys):
    # An archive gap wide enough to drop everything. The realized list already
    # records it, and there is nothing to evaluate.
    assert soca.hofx(config, {}, paths, 1, "hofx",
                     background=Path("/rst/0"), observers=[]) == []
    assert "no observers" in capsys.readouterr().out
    assert not paths.scratch(1, "hofx").exists()


# --- the real layers ---------------------------------------------------------

def shipped(name):
    """One of the committed experiments, resolved the way `create` resolves it."""
    from ackbar.config.layers import merge_layers, resolve_layers
    from ackbar.config.resolve import resolve
    from ackbar.config.schema import load_schema, merge_keys

    repo = Path(__file__).resolve().parents[1]
    layers = resolve_layers(repo / "tests/experiments" / name, repo / "config/layers")
    return repo, resolve(merge_layers(layers, merge_keys(load_schema())), {
        "scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static", "root": str(repo),
    })


def test_the_shipped_layers_produce_an_analysis_soca_would_accept(tmp_path):
    """The variational layer, not a fixture, through the whole builder.

    Everything `soca_var.x` reads is either here or in that layer, so a key
    neither of them sets is discovered by an application that has already
    allocated eight PEs and read a background.
    """
    repo, merged = shipped("tier3_var.yaml")
    document = soca.var_config(merged, 1, [observer()], background=Path("/rst/0"))
    reread = yaml.safe_load(yaml.safe_dump(document))

    cost = reread["cost function"]
    assert cost["cost type"] == "3D-Var"
    assert cost["geometry"]["fields metadata"].startswith(str(repo))
    assert cost["background"]["date"] == "2015-01-05T01:00:00Z"
    # The B is read from the domain's static stage, and `filepath` is a stem.
    groups = cost["background error"]["saber central block"]["read"]["groups"]
    assert groups[0]["horizontal"]["filepath"] == "/static/static/gom_25km/diffusion/hz"
    assert groups[0]["vertical"]["levels"] == 36
    # An integer, not the string the substitution pass started with: eckit does
    # not coerce, and `levels: "75"` is a type error inside saber.
    assert isinstance(groups[0]["vertical"]["levels"], int)
    assert reread["variational"]["iterations"][0]["geometry"] == cost["geometry"]


def test_the_shipped_layers_produce_a_document_soca_would_accept(tmp_path):
    """The config files, not a fixture: every key SOCA needs is set by a layer.

    A missing one is caught here rather than by an application that has already
    read a grid.
    """
    repo, merged = shipped("tier3_hofx.yaml")
    document = soca.hofx_config(merged, 1, [observer()], background=Path("/rst/0"))
    # Round trip, because what the application reads is the file and not the
    # dictionary, and an unserializable value fails at the last moment.
    reread = yaml.safe_load(yaml.safe_dump(document))
    assert reread["geometry"]["fields metadata"].startswith(str(repo))
    assert reread["state"]["date"] == "2015-01-05T01:00:00Z"


# --- the covariance ----------------------------------------------------------
#
# `solver.covariance` was validated and never read until phase 8. These are the
# three shapes it now assembles, and the failures they guard against are all of
# the same kind: a document that constructs, runs, and describes a different
# background error than the one the experiment asked for.

ENSEMBLE_ERROR = {
    "localization": {
        "localization method": "SABER",
        "saber central block": {
            "saber block name": "diffusion",
            "read": {"groups": [{"multivariate strategy": "duplicated",
                                 "horizontal": {"filepath": "/static/loc_hz"},
                                 "vertical": {"strategy": "duplicated"}}]},
        },
    },
}


def members_at(cycle, members=(1, 2, 3)):
    return soca.member_states(
        lambda member: Path(f"/out/e/rst/{cycle}/mem{member:03d}/MOM.res.nc"),
        members,
        date="2018-04-15T00:00:00Z",
        variables=SOLVER["background variables"],
    )


@pytest.fixture
def hybrid(config):
    solver = dict(SOLVER, covariance="hybrid",
                  **{"ensemble error": ENSEMBLE_ERROR,
                     "hybrid weights": {"static": 0.4, "ensemble": 0.6}})
    return dict(config, solver=solver)


def test_a_static_covariance_is_the_layers_block_and_nothing_else(var):
    error = soca.background_error(var["solver"], SOLVER["analysis variables"])
    assert error["covariance model"] == "SABER"
    assert "components" not in error


def test_a_hybrid_is_two_weighted_components(hybrid):
    error = soca.background_error(hybrid["solver"], SOLVER["analysis variables"],
                                  ensemble=members_at(0))
    assert error["covariance model"] == "hybrid"
    static, ens = error["components"]
    assert static["covariance"]["covariance model"] == "SABER"
    assert ens["covariance"]["covariance model"] == "ensemble"
    # Weights as stated, and floats: eckit does not coerce a string.
    assert static["weight"] == {"value": 0.4}
    assert ens["weight"] == {"value": 0.6}


def test_an_ensemble_covariance_has_no_static_component_at_all(hybrid):
    hybrid["solver"]["covariance"] = "ensemble"
    error = soca.background_error(hybrid["solver"], SOLVER["analysis variables"],
                                  ensemble=members_at(0))
    assert error["covariance model"] == "ensemble"
    assert "components" not in error
    # The static B is still configured by the layer and deliberately unread:
    # `solver.covariance` is the one place that decides.
    assert "saber central block" not in error


def test_the_ensemble_component_carries_the_members_it_was_given(hybrid):
    error = soca.background_error(hybrid["solver"], SOLVER["analysis variables"],
                                  ensemble=members_at(1, members=(1, 3)))
    ens = error["components"][1]["covariance"]
    assert [entry["basename"] for entry in ens["members"]] == [
        "/out/e/rst/1/mem001/", "/out/e/rst/1/mem003/"]
    assert [entry["ocn_filename"] for entry in ens["members"]] == \
        ["MOM.res.nc", "MOM.res.nc"]


def test_the_localization_is_the_layers_with_the_variables_filled_in(hybrid):
    """The same omission as the balance operator's, from the other side.

    `localization variables` is what the localization applies to, it is the
    analysis variables, and they are stated once.
    """
    error = soca.background_error(hybrid["solver"], SOLVER["analysis variables"],
                                  ensemble=members_at(0))
    localization = error["components"][1]["covariance"]["localization"]
    assert localization["localization variables"] == SOLVER["analysis variables"]
    assert localization["localization method"] == "SABER"
    assert localization["saber central block"]["saber block name"] == "diffusion"


def test_a_hybrid_with_no_weights_is_refused(hybrid):
    """0.5/0.5 is the textbook answer and therefore not any ocean's answer.

    An experiment that did not state them is one whose result cannot be
    attributed to either component.
    """
    del hybrid["solver"]["hybrid weights"]
    with pytest.raises(ModelError, match="hybrid weights"):
        soca.background_error(hybrid["solver"], SOLVER["analysis variables"],
                              ensemble=members_at(0))


def test_a_weight_that_is_not_a_number_is_refused(hybrid):
    hybrid["solver"]["hybrid weights"]["static"] = "0.5"
    with pytest.raises(ModelError, match="needs a number"):
        soca.background_error(hybrid["solver"], SOLVER["analysis variables"],
                              ensemble=members_at(0))


def test_a_static_covariance_handed_an_ensemble_is_refused(var):
    """Not ignored. An experiment configured as static and supplied with an
    ensemble is one whose author believes it is doing something it is not."""
    with pytest.raises(ModelError, match="static covariance reads none"):
        soca.background_error(var["solver"], SOLVER["analysis variables"],
                              ensemble=members_at(0))


def test_an_ensemble_covariance_with_no_ensemble_is_refused(hybrid):
    with pytest.raises(ModelError, match="reads none and the others"):
        soca.background_error(hybrid["solver"], SOLVER["analysis variables"])


def test_the_layer_is_not_mutated_by_assembling_a_hybrid(hybrid):
    soca.background_error(hybrid["solver"], SOLVER["analysis variables"],
                          ensemble=members_at(0))
    assert "localization variables" not in \
        hybrid["solver"]["ensemble error"]["localization"]
    assert "input variables" not in \
        hybrid["solver"]["background error"]["linear variable change"]


def test_the_analysis_document_carries_the_hybrid_it_was_handed(hybrid):
    document = soca.var_config(hybrid, 1, [observer()], background=Path("/rst/0"),
                               ensemble=members_at(0))
    assert document["cost function"]["background error"]["covariance model"] == "hybrid"


# --- the distribution --------------------------------------------------------

def test_a_variational_analysis_reads_its_observations_round_robin(var):
    document = soca.var_config(var, 1, [observer()], background=Path("/rst/0"))
    space = document["cost function"]["observations"]["observers"][0]["obs space"]
    assert space["distribution"] == {"name": "RoundRobin"}


def test_a_filter_reads_the_same_observers_through_a_halo(ens):
    """The reason a distribution is not a layered value.

    A hybrid cycle runs both applications over one merged configuration, so a
    substituted `$(obs_distribution)` could only ever be one of these two.
    soca-science patched around that with `sed` markers keyed on whether the
    LETKF was running solo.
    """
    document = letkf_document(ens)
    space = document["observations"]["observers"][0]["obs space"]
    assert space["distribution"] == {"name": "Halo", "halo size": 500000}


def test_hofx_takes_the_serial_distribution_too(config):
    document = soca.hofx_config(config, 1, [observer()], background=Path("/rst/0"))
    space = document["observations"]["observers"][0]["obs space"]
    assert space["distribution"] == {"name": "RoundRobin"}


def test_setting_the_distribution_does_not_edit_the_observer_record(var):
    record = observer()
    soca.var_config(var, 1, [record], background=Path("/rst/0"))
    assert "distribution" not in record["config"]["obs space"]


# --- the recentring ----------------------------------------------------------

def recenter_document(hybrid, members=(1, 2, 3)):
    return soca.recenter_config(
        hybrid, 1,
        center=Path("/out/e/ana/1/mem000/analysis/ocn.ana.an.20180415T000000Z.nc"),
        ensemble=lambda m: Path(f"/out/e/ana/1/mem{m:03d}/analysis/x.nc"),
        members=members)


def test_the_centre_is_the_deterministic_analysis(hybrid):
    document = recenter_document(hybrid)
    assert document["center"]["basename"] == "/out/e/ana/1/mem000/analysis/"
    assert document["center"]["ocn_filename"] == "ocn.ana.an.20180415T000000Z.nc"


def test_the_recentring_touches_only_the_analysis_variables(hybrid):
    """`x = x_center; x += pert` replaces every field it is given.

    Naming a field the analysis never solved for would hand every member the
    control's layer thicknesses, which is a different vertical grid under the
    same water.
    """
    document = recenter_document(hybrid)
    assert document["recenter variables"] == SOLVER["analysis variables"]
    for entry in document["ensemble"]["members"]:
        assert entry["state variables"] == SOLVER["analysis variables"]
    assert document["center"]["state variables"] == SOLVER["analysis variables"]


def test_the_recentred_members_are_named_apart_from_the_analyses(hybrid):
    """Both states are kept, because the recentring is the step that decides how
    much of a hybrid's answer the ensemble keeps and nothing else records it."""
    document = recenter_document(hybrid)
    assert document["recentered output"]["exp"] == soca.RECENTERED[0]
    assert soca.product_file(hybrid, 1, soca.RECENTERED) == \
        "ocn.rcnt.an.20180415T000000Z.nc"
    assert soca.product_file(hybrid, 1, soca.ANALYSIS) == \
        "ocn.ana.an.20180415T000000Z.nc"


def test_a_gap_in_the_ensemble_recentres_a_shorter_list(hybrid):
    document = recenter_document(hybrid, members=(1, 3))
    assert [entry["basename"] for entry in document["ensemble"]["members"]] == [
        "/out/e/ana/1/mem001/analysis/", "/out/e/ana/1/mem003/analysis/"]


def test_the_shipped_hybrid_layers_produce_a_document_soca_would_accept():
    """The committed layers, through the whole builder, both applications.

    Everything `soca_var.x` and `soca_letkf.x` read in a hybrid cycle is either
    built here or stated by a layer, so a key neither sets is otherwise
    discovered by an application that has already read an ensemble.
    """
    repo, merged = shipped("tier3_hybrid.yaml")
    document = yaml.safe_load(yaml.safe_dump(soca.var_config(
        merged, 1, [observer()], background=Path("/rst/0"),
        ensemble=members_at(0))))

    error = document["cost function"]["background error"]
    assert error["covariance model"] == "hybrid"
    static, ens = error["components"]
    assert static["weight"]["value"] + ens["weight"]["value"] == 1.0
    groups = ens["covariance"]["localization"]["saber central block"]["read"]["groups"]
    # The localization scales are the domain's static stage, like the
    # correlation, and `filepath` is a stem.
    assert groups[0]["horizontal"]["filepath"] == \
        "/static/static/gom_25km/diffusion/loc_hz"

    filter_document = yaml.safe_load(yaml.safe_dump(soca.letkf_config(
        merged, 1, [observer()], backgrounds=Path("/out/e/rst/0"), members=(1, 2))))
    assert filter_document["local ensemble DA"]["solver"] == "Deterministic LETKF"
    space = filter_document["observations"]["observers"][0]["obs space"]
    assert space["distribution"]["halo size"] == 500000


def test_a_per_member_writer_is_told_the_type_that_carries_an_index(hybrid, ens):
    """`soca_genfilename` puts the member index in the name only for `ens`.

    With any other type the six members write one filename in turn and the
    application exits 0, leaving a single file that is the last member's. So the
    type asked for and the type the committed file is named with are different,
    and both are stated here rather than in two places that can drift.
    """
    document = recenter_document(hybrid)
    assert document["recentered output"]["type"] == soca.ENSEMBLE_TYPE
    assert letkf_document(ens)["output"]["type"] == soca.ENSEMBLE_TYPE
    # And the diagnostics, which are one file each, are not told that.
    assert letkf_document(ens)["output increment"]["type"] == soca.INCREMENT[1]
