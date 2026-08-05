"""Tier 0: the run directory, without the model.

Everything MOM6-SIS2 reads is a file in the directory it starts in, so
everything that can be wrong about a forecast before the model has an opinion is
checkable here in milliseconds. What the model then makes of it is tier 3's
problem, and there is nothing in between worth testing on a scheduler.

The base case is a stand-in with the same shape as a MOM6-examples directory:
the namelist groups that get patched, a placeholder layout with the same trap in
it as the real one, and an INPUT directory.
"""

import json
import os
from pathlib import Path

import pytest

from ackbar import mom6sis2, run
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.paths import Paths

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

#: Trimmed to the groups that matter, but with the real file's habits: a group
#: ACKBAR patches, a group it must not touch, and `days`/`months` set to
#: something non-zero so that failing to zero them shows up as a longer run.
INPUT_NML = """\
 &MOM_input_nml
         output_directory = '.',
         restart_input_dir = 'INPUT',
         restart_output_dir = 'RESTART',
/

 &coupler_nml
            months = 1,
            days   = 5,
            hours  = 0,
            current_date = 1958,1,1,0,0,0
            calendar = 'julian',
            dt_cpld = 7200,
            do_ocean = .true.,
/

 &fms_nml
            clock_grain='ROUTINE'
/
"""

DIAG_TABLE = """\
a title nobody reads
1958 1 1 0 0 0
"ocean_daily", 1, "days", 1, "days", "time"
"""


@pytest.fixture
def base(tmp_path):
    """A stand-in for pkg/mom6sis2/ice_ocean_SIS2/<case>."""
    case = tmp_path / "case"
    (case / "INPUT").mkdir(parents=True)
    (case / "input.nml").write_text(INPUT_NML)
    (case / "MOM_input").write_text("DT = 1800.0\n")
    (case / "SIS_input").write_text("DT_ICE_DYNAMICS = 3600.0\n")
    (case / "field_table").write_text("# tracers\n")
    # The placeholder layouts that ship with the real cases, PE counts and all.
    (case / "MOM_layout").write_text("LAYOUT = 12,10\n")
    (case / "SIS_layout").write_text("LAYOUT = 32,18\n")
    # Model output that MOM6-examples commits back into the case as documentation.
    (case / "MOM_parameter_doc.layout").write_text("LAYOUT = 12, 10\n")
    (case / "SIS_parameter_doc.short").write_text("DT_ICE_DYNAMICS = 3600.0\n")
    for name in ("grid_spec.nc", "ocean_hgrid.nc", "JRA_tas.nc"):
        (case / "INPUT" / name).write_bytes(b"netcdf\n")
    return case


@pytest.fixture
def diag_tables(tmp_path):
    target = tmp_path / "diag_table.cycling"
    target.write_text(DIAG_TABLE)
    return target


@pytest.fixture
def config(base, diag_tables):
    layers = resolve_layers(EXPERIMENTS / "free_om1deg.yaml", LAYERS)
    keys = merge_keys(load_schema())
    merged = resolve(merge_layers(layers, keys), {
        "scratch_root": "/scratch", "output_root": "/out", "root": str(REPO),
    })
    merged["model"].update({
        "base": str(base),
        "executable": str(base / "coupler_main"),
        "diag_table": {"forecast": str(diag_tables)},
    })
    return merged


@pytest.fixture
def env(tmp_path, config):
    site = {"scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o"),
            "launcher": ""}
    paths = Paths.of(config, site).ensure()
    return config, site, paths


def restart_set(directory, *, stamp=True):
    """A previous cycle's output, or something that looks like one."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "MOM.res.nc").write_bytes(b"ocean\n")
    (directory / "ice_model.res.nc").write_bytes(b"ice\n")
    if stamp:
        (directory / mom6sis2.STAMP).write_text("2 (calendar type)\n")
    return directory


def staged(env, cycle=1, task="forecast", member=0, source=None):
    config, _, paths = env
    run_dir = paths.scratch(cycle, task, member)
    source = source or restart_set(paths.member_out("rst", cycle - 1, member))
    mom6sis2.stage(config, run_dir, cycle, task, source=source)
    return run_dir


# --- the run directory -------------------------------------------------------

def test_the_base_case_arrives_by_symlink_and_is_never_copied(env):
    # A case directory is mostly gigabytes of forcing, and the run directory is
    # rebuilt from nothing on every attempt.
    run_dir = staged(env)
    assert (run_dir / "MOM_input").is_symlink()
    assert (run_dir / "MOM_input").read_text() == "DT = 1800.0\n"
    assert (run_dir / "INPUT" / "grid_spec.nc").is_symlink()


def test_the_files_ackbar_owns_are_real_files_not_links(env):
    run_dir = staged(env)
    for name in mom6sis2.OWNED:
        assert (run_dir / name).is_file() and not (run_dir / name).is_symlink()


def test_what_the_model_writes_is_not_linked_back_into_the_shared_case(env, base):
    # MOM6-examples commits the model's own parameter dumps into the case, so
    # they are outputs living in an input directory. Linked, the model opens the
    # symlink for writing and edits the shared case underneath every other
    # experiment. Absent, it writes its own.
    run_dir = staged(env)
    for name in ("MOM_parameter_doc.layout", "SIS_parameter_doc.short"):
        assert not (run_dir / name).exists(), f"{name} would be written through"
    assert (base / "MOM_parameter_doc.layout").read_text() == "LAYOUT = 12, 10\n"


def test_the_incoming_restarts_land_in_input_where_the_coupler_looks(env):
    # `INPUT/coupler.res` is a hardcoded string in coupler_main, so this is the
    # only place the date can come from, whatever restart_input_dir says.
    run_dir = staged(env)
    assert (run_dir / "INPUT" / mom6sis2.STAMP).is_symlink()
    assert (run_dir / "INPUT" / "MOM.res.nc").read_bytes() == b"ocean\n"
    # and the static archive is still there underneath them
    assert (run_dir / "INPUT" / "grid_spec.nc").exists()


def test_a_second_attempt_does_not_resume_from_the_first_ones_leftovers(env):
    """The failure this costs the most to find: a stale `INPUT/coupler.res`.

    A rebuilt run directory that kept the old link would start the model from
    the wrong date with the right state, and the model would run to completion
    without a word about it.
    """
    _, _, paths = env
    run_dir = staged(env)
    (run_dir / "INPUT" / "leftover.res.nc").write_bytes(b"from the last attempt\n")

    source = restart_set(paths.member_out("rst", 0, 0))
    (source / mom6sis2.STAMP).write_text("2 (calendar type)\nhealed\n")
    staged(env, source=source)

    assert not (run_dir / "INPUT" / "leftover.res.nc").exists()
    assert "healed" in (run_dir / "INPUT" / mom6sis2.STAMP).read_text()


def test_a_second_attempt_does_not_commit_the_first_ones_half_written_restarts(env):
    # Scratch is kept on failure, so a healed attempt starts on top of whatever
    # the killed one had written, and a set assembled from two attempts is a
    # state no forecast ever produced.
    run_dir = staged(env)
    (run_dir / "RESTART" / "MOM.res.nc").write_bytes(b"half of an attempt\n")
    staged(env)
    assert list((run_dir / "RESTART").iterdir()) == []


def test_a_source_without_a_coupler_res_is_not_a_restart_set(env):
    _, _, paths = env
    source = restart_set(paths.member_out("rst", 0, 0), stamp=False)
    with pytest.raises(mom6sis2.ModelError, match="not a restart set"):
        staged(env, source=source)


# --- the layout --------------------------------------------------------------

def test_the_layout_comes_from_the_domain_and_never_from_the_case(env):
    config, _, _ = env
    run_dir = staged(env)
    layout = config["domain"]["resources"]["forecast"]["layout"]
    assert f"LAYOUT = {layout[0]},{layout[1]}" in (run_dir / "MOM_layout").read_text()
    assert "12,10" not in (run_dir / "MOM_layout").read_text()


def test_a_layout_that_does_not_match_ntasks_is_caught_before_the_model_sees_it(env):
    # FMS reports this as a domain decomposition error from inside the model,
    # tens of seconds and one queued job later.
    config, _, _ = env
    config["domain"]["resources"]["forecast"]["layout"] = [4, 4]
    with pytest.raises(mom6sis2.ModelError, match="16 PEs but ntasks is 8"):
        staged(env)


def test_a_missing_layout_names_the_key_rather_than_using_the_placeholder(env):
    config, _, _ = env
    del config["domain"]["resources"]["forecast"]["layout"]
    with pytest.raises(mom6sis2.ModelError, match="domain.resources.forecast.layout"):
        staged(env)


# --- the namelist ------------------------------------------------------------

def test_the_run_length_is_the_cycle_length_and_nothing_is_left_over(env):
    # PT24H, and the base case's `months = 1, days = 5` must not survive.
    run_dir = staged(env)
    text = (run_dir / "input.nml").read_text()
    assert "hours = 24" in text
    assert "months = 0" in text and "days = 0" in text


def test_the_fallback_date_is_this_cycle_and_not_the_case_authors(env):
    run_dir = staged(env, cycle=2)
    # cycle.start is 2018-04-15 and the length is a day, so cycle 2 is the 16th.
    assert "current_date = 2018,4,16,0,0,0" in (run_dir / "input.nml").read_text()
    assert "1958,1,1" not in (run_dir / "input.nml").read_text()


def test_namelist_groups_ackbar_does_not_own_come_through_untouched(env):
    run_dir = staged(env)
    text = (run_dir / "input.nml").read_text()
    assert "clock_grain='ROUTINE'" in text
    assert "restart_input_dir = 'INPUT'" in text
    assert "calendar = 'julian'" in text


def test_a_namelist_with_no_coupler_group_is_an_error_not_a_silent_default(env, base):
    (base / "input.nml").write_text(" &fms_nml\n  x = 1\n/\n")
    with pytest.raises(mom6sis2.ModelError, match="no &coupler_nml"):
        staged(env)


# --- the diag_table ----------------------------------------------------------

def test_the_diag_table_base_date_is_rewritten_to_the_cycle(env):
    run_dir = staged(env, cycle=3)
    lines = (run_dir / "diag_table").read_text().splitlines()
    assert lines[1] == "2018 4 17 0 0 0"
    assert lines[0] == "a title nobody reads"
    assert lines[2].startswith('"ocean_daily"')


def test_a_task_with_no_diag_table_configured_is_an_error(env):
    config, _, _ = env
    config["model"]["diag_table"] = {}
    with pytest.raises(mom6sis2.ModelError, match="no entry for 'forecast'"):
        staged(env)


def test_the_shipped_cycling_table_writes_nothing(env):
    """The cycling forecast's product is the restart set, not model history."""
    table = REPO / "config" / "model" / "mom6sis2" / "diag_table.cycling"
    body = [line for line in table.read_text().splitlines()[2:]
            if line.strip() and not line.lstrip().startswith("#")]
    assert body == []


# --- committing the result ---------------------------------------------------

def test_the_restart_set_is_moved_and_the_stamp_goes_last(env, tmp_path):
    written = tmp_path / "run" / "RESTART"
    restart_set(written)
    target = tmp_path / "rst" / "mem000"

    mom6sis2.commit(written.parent, target)

    assert (target / "MOM.res.nc").read_bytes() == b"ocean\n"
    assert (target / mom6sis2.STAMP).exists()
    # Moved, not copied: at a gigabyte a member a copy is the cycle's biggest
    # single cost and buys nothing.
    assert not (written / "MOM.res.nc").exists()


def test_a_model_that_exits_zero_having_written_no_restarts_still_fails(env, tmp_path):
    # Nothing in Slurm notices this, and the next cycle is where it surfaces.
    written = tmp_path / "run" / "RESTART"
    written.mkdir(parents=True)
    with pytest.raises(mom6sis2.ModelError, match="wrote no"):
        mom6sis2.commit(written.parent, tmp_path / "rst")


# --- launching ---------------------------------------------------------------

def test_the_launcher_and_the_task_size_come_from_config(env, tmp_path, monkeypatch):
    config, site, _ = env
    site["launcher"] = "srun --mpi=pmi2"
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["cwd"] = kwargs.get("cwd")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(mom6sis2.subprocess, "run", fake_run)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mom6sis2.launch(config, site, run_dir, "forecast")

    assert seen["command"] == ["srun", "--mpi=pmi2", "-n", "8", "./coupler_main"]
    assert seen["cwd"] == run_dir


def test_what_the_model_said_about_itself_outlives_the_run_directory(env, tmp_path):
    """Scratch is deleted on success, and `ocean.stats` lives in scratch.

    One line per timestep, and where an ocean that is blowing up says so. A
    forecast that leaves only a restart set leaves nothing to answer "did that
    look right" with.
    """
    _, _, paths = env
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ocean.stats").write_text("En 4.3e-04, CFL 0.03\n")
    (run_dir / "model.log").write_text("NOTE: ...\n")

    mom6sis2.keep_traces(run_dir, paths.sub("log") / "2", "forecast", 0)

    kept = sorted(p.name for p in (paths.sub("log") / "2").iterdir())
    assert kept == ["forecast.mem000.model.log", "forecast.mem000.ocean.stats"]


def test_a_failed_run_keeps_its_trace_too(env, tmp_path, monkeypatch):
    # The failed attempt's trace is the one worth having, so the copy happens
    # whatever the model exited with.
    config, site, paths = env
    source = restart_set(paths.member_out("rst", 0, 0))

    def fake_run(command, cwd=None, **kwargs):
        (Path(cwd) / "ocean.stats").write_text("CFL 8.0, and then NaN\n")
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(mom6sis2.subprocess, "run", fake_run)
    with pytest.raises(mom6sis2.ModelError):
        mom6sis2.forecast(config, site, paths, 1, "forecast", 0,
                          source=source, target=paths.member_out("rst", 1, 0))
    assert (paths.sub("log") / "1" / "forecast.mem000.ocean.stats").exists()


def test_a_nonzero_exit_names_the_log_that_explains_it(env, tmp_path, monkeypatch):
    config, site, _ = env
    monkeypatch.setattr(
        mom6sis2.subprocess, "run",
        lambda command, **kwargs: type("R", (), {"returncode": 137})(),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(mom6sis2.ModelError, match="model.log"):
        mom6sis2.launch(config, site, run_dir, "forecast")


# --- how run.py sees it ------------------------------------------------------

def test_only_the_cycling_forecast_runs_the_real_model_so_far(config):
    assert run.real_model(config, "forecast")
    # Same executable, different product, and nothing reads it yet.
    assert not run.real_model(config, "forecast.ext")
    assert not run.real_model(config, "da")


def test_the_stub_and_the_model_agree_on_where_a_forecast_starts(env):
    """One rule, in one place.

    The two disagreeing would show up as a forecast that quietly started from
    the background it was supposed to have corrected, which is the kind of thing
    a whole experiment gets written up before anybody notices.
    """
    config, _, paths = env
    free = json.loads(json.dumps(config))
    free["solver"]["name"] = "none"
    assert run.restart_source(free, paths, 4, 1) == paths.member_out("rst", 3, 1)

    analysed = json.loads(json.dumps(config))
    analysed["solver"]["name"] = "variational"
    assert run.restart_source(analysed, paths, 4, 1) == paths.member_out("ana", 4, 1)


def test_a_forecast_is_skipped_on_its_stamp_and_its_sentinel(env):
    config, site, paths = env
    _, outputs = run.task_io(config, paths, "forecast", 2, 0)
    assert outputs == [paths.member_out("rst", 2, 0) / mom6sis2.STAMP]

    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("2 (calendar type)\n")
    paths.sentinel(2, "forecast", 0).parent.mkdir(parents=True, exist_ok=True)
    paths.sentinel(2, "forecast", 0).write_text("{}")

    # No base case is reachable from this config's scratch, so anything other
    # than a skip would raise rather than return.
    assert run.run_task(config, site, paths, 2, "forecast", 0) == 0


def test_a_leaf_with_no_body_yet_runs_and_says_it_did_nothing(env):
    """`verify` has no implementation and produces nothing anything reads.

    Failing it would mean a free run reports broken on every cycle for the whole
    of phase 4, and faking it would mean a run with no diagnostics is
    indistinguishable from one whose diagnostics came out empty. So it runs, and
    the sentinel carries the difference.
    """
    config, site, paths = env
    assert run.deferred_task(config, "verify")
    assert run.run_task(config, site, paths, 1, "verify") == 0
    assert json.loads(paths.sentinel(1, "verify").read_text())["deferred"] is True


def test_the_stub_still_runs_every_task_itself(env):
    # Under the stub the fan-out these leaves create is the thing tier 2 exists
    # to exercise, so nothing there may take the deferred path.
    config, _, _ = env
    stub = json.loads(json.dumps(config))
    stub["model"] = {"name": "stub", "stub": {"seconds": 0}}
    assert not any(run.deferred_task(stub, task) for task in run.DEFERRED)


def test_cleanup_looks_for_the_model_s_own_restarts_and_not_the_stub_s(env):
    """The proof file differs by model, and getting it wrong is invisible.

    `cleanup` refuses when the cycle it is keeping looks incomplete, and a
    refusal is a log line rather than a failure. Keyed off the stub's file name
    under a real model it refuses on every cycle of every experiment, and the
    restarts pile up until the disk fills, which is where anyone finds out.
    """
    config, site, paths = env
    assert run.restart_stamp(config) == mom6sis2.STAMP
    for cycle in (1, 2):
        restart_set(paths.member_out("rst", cycle, 0))

    run.run_task(config, site, paths, 3, "cleanup")
    assert not paths.cycle_out("rst", 1).exists()
    assert paths.cycle_out("rst", 2).exists()


def test_the_config_layer_points_at_a_case_that_is_actually_there(config):
    """The layer names paths in this checkout, and `validate` stats them.

    Asserted here as well because a broken path in the model layer takes down
    every mom6sis2 experiment at once, and this is the cheap place to find out.
    """
    layers = resolve_layers(EXPERIMENTS / "free_om1deg.yaml", LAYERS)
    merged = resolve(merge_layers(layers, merge_keys(load_schema())), {
        "scratch_root": "/scratch", "output_root": "/out", "root": str(REPO),
    })
    model = merged["model"]
    assert os.path.isdir(model["base"])
    assert os.path.isfile(model["base"] + "/input.nml")
    assert os.path.isfile(model["diag_table"]["forecast"])
