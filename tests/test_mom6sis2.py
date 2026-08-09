"""Tier 0: the run directory, without the model.

Everything MOM6-SIS2 reads is a file in the directory it starts in, so
everything that can be wrong about a forecast before the model has an opinion is
checkable here in milliseconds. What the model then makes of it is tier 3's
problem, and there is nothing in between worth testing on a scheduler.

The base case is a stand-in with the same shape as a MOM6-examples directory:
the namelist groups that get patched, an override the case ships that ACKBAR
has to replace rather than inherit, and a data directory beside it rather than
inside it, because that is the shape a regional domain has.
"""

import errno
import json
import os
from datetime import datetime, timezone
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
#: Copied from MOM6-examples' OM_1deg habits rather than invented, because both
#: of them broke a patcher that looked reasonable.
#:
#: `parameter_filename` is written across two lines: namelist lists are routinely
#: continued that way, and an assignment patcher that stopped at the first
#: newline would replace the head and leave `'MOM_override'` as an orphan line,
#: producing a namelist that no longer parses.
#:
#: The SIS group closes with `/` trailing on its last value rather than alone on
#: a line, and `restart_input_dir = 'INPUT/'` puts a separator inside a quoted
#: value on the way past. A group pattern that required a bare `/` matched
#: neither, so it skipped the group in silence and left the case's own
#: `parameter_filename` in place.
INPUT_NML = """\
 &MOM_input_nml
         output_directory = '.',
         input_filename = 'n'
         restart_input_dir = 'INPUT',
         restart_output_dir = 'RESTART',
         parameter_filename = 'MOM_input',
                              'MOM_override'
/

 &SIS_input_nml
         output_directory = './',
         input_filename = 'n'
         restart_input_dir = 'INPUT/',
         parameter_filename = 'SIS_input',
                              'SIS_override' /

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
    """A stand-in for a case's text half."""
    case = tmp_path / "case"
    case.mkdir(parents=True)
    (case / "input.nml").write_text(INPUT_NML)
    (case / "MOM_input").write_text("DT = 1800.0\nUSE_GM_WORK_BUG = True\n")
    (case / "SIS_input").write_text("DT_ICE_DYNAMICS = 3600.0\n")
    (case / "field_table").write_text("# tracers\n")
    # The overrides the case itself ships. ACKBAR replaces rather than inherits
    # these, so their contents are what a passing test must *not* find.
    (case / "MOM_override").write_text("! the case's own\nVERBOSITY = 9\n")
    (case / "SIS_override").write_text("! the case's own\nADD_DIURNAL_SW = True\n")
    # Model output that MOM6-examples commits back into the case as documentation.
    (case / "MOM_parameter_doc.layout").write_text("LAYOUT = 12, 10\n")
    (case / "SIS_parameter_doc.short").write_text("DT_ICE_DYNAMICS = 3600.0\n")
    return case


@pytest.fixture
def data(tmp_path):
    """A stand-in for a case's data half, which is a separate directory."""
    target = tmp_path / "data" / "INPUT"
    target.mkdir(parents=True)
    for name in ("grid_spec.nc", "ocean_hgrid.nc", "JRA_tas.nc"):
        (target / name).write_bytes(b"netcdf\n")
    return target


@pytest.fixture
def override(tmp_path):
    """A stand-in for config/model/mom6sis2/domain/<domain>/."""
    target = tmp_path / "override"
    target.mkdir(parents=True)
    (target / "MOM_override").write_text(
        "! ackbar's\nENABLE_BUGS_BY_DEFAULT = False\n#override USE_GM_WORK_BUG = False\n")
    (target / "SIS_override").write_text("! ackbar's\nADD_DIURNAL_SW = False\n")
    return target


@pytest.fixture
def diag_tables(tmp_path):
    target = tmp_path / "diag_table.cycling"
    target.write_text(DIAG_TABLE)
    return target


@pytest.fixture
def config(base, data, override, diag_tables):
    layers = resolve_layers(EXPERIMENTS / "free_om1deg.yaml", LAYERS)
    keys = merge_keys(load_schema())
    merged = resolve(merge_layers(layers, keys), {
        "scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static", "root": str(REPO),
    })
    merged["model"].update({
        "base": str(base),
        "input": str(data),
        "override": {
            "MOM_override": str(override / "MOM_override"),
            "SIS_override": str(override / "SIS_override"),
        },
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
    mom6sis2.stage(config, run_dir, cycle, task, source=source, member=member)
    return run_dir


# --- the run directory -------------------------------------------------------

def test_the_base_case_arrives_by_symlink_and_is_never_copied(env):
    # A case directory is mostly gigabytes of forcing, and the run directory is
    # rebuilt from nothing on every attempt.
    run_dir = staged(env)
    assert (run_dir / "MOM_input").is_symlink()
    assert (run_dir / "MOM_input").read_text().startswith("DT = 1800.0\n")
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


# --- the overrides -----------------------------------------------------------

def test_the_override_is_ackbars_and_never_the_cases(env, override):
    # The whole point. A case that ships its own MOM_override, as every
    # MOM6-examples case does, must not have it linked through: that is how
    # bug-retention flags survive into a run nobody meant to enable them in.
    run_dir = staged(env)
    for name in mom6sis2.OVERRIDE:
        assert (run_dir / name).resolve() == (override / name).resolve()
    assert "ENABLE_BUGS_BY_DEFAULT = False" in (run_dir / "MOM_override").read_text()
    assert "VERBOSITY = 9" not in (run_dir / "MOM_override").read_text()


def test_a_missing_override_file_is_named_before_the_model_runs(env, override):
    (override / "MOM_override").unlink()
    with pytest.raises(mom6sis2.ModelError, match="MOM_override"):
        staged(env)


def test_an_unconfigured_override_says_what_it_would_have_changed(env):
    config, _, _ = env
    del config["model"]["override"]["SIS_override"]
    with pytest.raises(mom6sis2.ModelError, match="model.override.SIS_override"):
        staged(env)


def test_the_override_is_read_because_ackbar_puts_it_in_the_parameter_list(env):
    # An override the case does not list is an override MOM6 never opens. The
    # imported regional cases name only `MOM_input`, so inheriting the case's
    # list would silently drop every setting in the file above.
    run_dir = staged(env)
    text = (run_dir / "input.nml").read_text()
    assert "parameter_filename = 'MOM_input', 'MOM_override'" in text
    assert "parameter_filename = 'SIS_input', 'SIS_override'" in text


# --- per-member inputs -------------------------------------------------------

@pytest.fixture
def boundaries(tmp_path):
    """What `tools/obc-lagged.py` leaves behind: one boundary file per member."""
    target = tmp_path / "obc-lagged"
    target.mkdir(parents=True)
    for member in range(3):
        (target / f"mem{member:03d}.nc").write_bytes(f"boundary {member}\n".encode())
    return target


def with_inputs(config, boundaries, name="obc.nc"):
    config["ensemble"] = {
        "size": 2, "control": True,
        "inputs": {name: str(boundaries / "{{member_dir}}.nc")},
    }


def test_a_member_reads_its_own_input_and_not_the_domains(env, boundaries, data):
    # The whole point: the run directory is identical for every member except
    # for which file this name resolves to.
    config, _, _ = env
    (data / "obc.nc").write_bytes(b"the domain's shared boundary\n")
    with_inputs(config, boundaries)
    for member in (0, 1, 2):
        run_dir = staged(env, member=member)
        link = run_dir / "INPUT" / "obc.nc"
        assert link.is_symlink()
        assert link.read_text() == f"boundary {member}\n"


def test_the_control_gets_one_too(env, boundaries, data):
    # mem000 is a member, not a parallel concept, and its boundary is the
    # unperturbed one rather than the domain's by accident.
    config, _, _ = env
    with_inputs(config, boundaries)
    assert (staged(env, member=0) / "INPUT" / "obc.nc").read_text() == "boundary 0\n"


def test_an_experiment_with_no_inputs_configured_stages_what_it_always_did(env, data):
    (data / "obc.nc").write_bytes(b"the domain's shared boundary\n")
    run_dir = staged(env, member=1)
    assert (run_dir / "INPUT" / "obc.nc").read_text() == "the domain's shared boundary\n"


def test_a_member_the_offline_stage_never_built_stops_the_run(env, boundaries):
    # The alternative is falling back to the domain's copy, which is a member
    # with no perturbation whose only symptom is slightly less spread.
    config, _, _ = env
    with_inputs(config, boundaries)
    with pytest.raises(mom6sis2.ModelError) as error:
        staged(env, member=7)
    assert "mem007" in str(error.value)


def test_an_input_the_restart_set_would_overwrite_is_refused(env, boundaries):
    # Restarts are staged last so that `INPUT/coupler.res` comes from the set
    # being resumed. A per-member file by the same name would be built, linked,
    # overwritten and never read.
    config, _, _ = env
    with_inputs(config, boundaries, name="MOM.res.nc")
    with pytest.raises(mom6sis2.ModelError) as error:
        staged(env, member=1)
    assert "MOM.res.nc" in str(error.value)


def test_the_restart_set_still_wins_the_files_it_owns(env, boundaries, data):
    # The overlay sits between the domain and the restarts, not on top of them.
    config, _, _ = env
    (data / "coupler.res").write_bytes(b"the domain's cold start date\n")
    with_inputs(config, boundaries)
    source = restart_set(Path(env[2].member_out("rst", 0, 1)))
    (source / "coupler.res").write_text("the previous cycle's date\n")
    run_dir = staged(env, member=1, source=source)
    assert (run_dir / "INPUT" / "coupler.res").read_text() == "the previous cycle's date\n"


# --- stochastic physics ------------------------------------------------------

SPPT = {"seed": 20150712,
        "sppt": {"amplitude": 0.8, "length_scale": 500000.0, "timescale": "PT6H"}}


def test_a_run_with_no_stochastic_physics_configured_writes_none_of_it(env):
    # Every experiment written before this existed, and the reason the schemes
    # can be compiled into one executable rather than two: no parameter file, no
    # namelist group, and the same bytes MOM6 read before.
    run_dir = staged(env, member=1)
    assert not (run_dir / mom6sis2.STOCHASTIC).exists()
    assert "nam_stochy" not in (run_dir / "input.nml").read_text()


def test_a_perturbed_member_gets_both_halves_or_the_generator_refuses_the_run(env):
    # `init_stochastic_physics_ocn` compares the two and fails when they
    # disagree, so writing either alone is a run that does not start.
    config, _, paths = env
    config["ensemble"] = {"size": 2, "control": True, "stochastic": SPPT}
    run_dir = staged(env, member=1)
    assert "DO_SPPT = True" in (run_dir / mom6sis2.STOCHASTIC).read_text()
    assert "ocnsppt = 0.8" in (run_dir / "input.nml").read_text()


def test_the_perturbation_file_is_read_because_it_is_in_the_parameter_list(env):
    # Last, so it wins, and only for the member that has one: a run staged
    # without the file must not name it, or MOM6 fails on the missing file.
    config, _, paths = env
    config["ensemble"] = {"size": 2, "control": True, "stochastic": SPPT}
    perturbed = (staged(env, member=1) / "input.nml").read_text()
    control = (staged(env, member=0) / "input.nml").read_text()
    assert ("parameter_filename = 'MOM_input', 'MOM_override', "
            "'MOM_stochastic'") in perturbed
    assert "parameter_filename = 'MOM_input', 'MOM_override'\n" in control


def test_the_control_is_not_perturbed_because_it_is_what_is_scored(env):
    config, _, paths = env
    config["ensemble"] = {"size": 2, "control": True, "stochastic": SPPT}
    run_dir = staged(env, member=0)
    assert not (run_dir / mom6sis2.STOCHASTIC).exists()
    assert "nam_stochy" not in (run_dir / "input.nml").read_text()


def test_two_members_of_one_cycle_draw_different_patterns(env):
    config, _, paths = env
    config["ensemble"] = {"size": 2, "control": True, "stochastic": SPPT}
    first = (staged(env, member=1) / "input.nml").read_text()
    second = (staged(env, member=2) / "input.nml").read_text()
    assert _seed_line(first) != _seed_line(second)


def test_one_member_draws_a_different_pattern_each_cycle(env):
    # Otherwise every cycle perturbs a member the same way, which is a fixed
    # offset rather than a random walk, and the ensemble stops growing.
    config, _, paths = env
    config["ensemble"] = {"size": 2, "control": True, "stochastic": SPPT}
    first = (staged(env, cycle=1, member=1) / "input.nml").read_text()
    second = (staged(env, cycle=2, member=1) / "input.nml").read_text()
    assert _seed_line(first) != _seed_line(second)


def test_the_long_forecast_continues_the_cycle_rather_than_re_drawing(env):
    # Both start from the same state, so a different seed would make the long
    # forecast's first day a different trajectory from the cycle it extends.
    config, _, paths = env
    config["ensemble"] = {"size": 2, "control": True, "stochastic": SPPT}
    config["model"]["diag_table"]["forecast.ext"] = \
        config["model"]["diag_table"]["forecast"]
    config.setdefault("forecast", {})["extended"] = {"length": "P2D"}
    cycling = (staged(env, member=1) / "input.nml").read_text()
    extended = (staged(env, task="forecast.ext", member=1) / "input.nml").read_text()
    assert _seed_line(cycling) == _seed_line(extended)


def _seed_line(text):
    return next(line for line in text.splitlines() if "iseed_ocnsppt" in line)


def test_the_model_is_told_to_resume_rather_than_start_new(env):
    """The single most consequential value in the emitted namelist.

    `MOM_restart::determine_is_new_run` reads one character. Stock
    MOM6-examples cases ship `input_filename = 'n'`, which means a new run:
    MOM6 then initializes temperature and salinity from
    `INIT_LAYERS_FROM_Z_FILE` and never opens `INPUT/MOM.res.nc` at all. Every
    cycle integrates the same cold start and every analysis is discarded, while
    the workflow reports nothing, because the model runs and writes a restart
    set exactly as it should.

    Both components. An ice state silently reset every cycle is the same
    failure with a smaller blast radius.
    """
    text = (staged(env) / "input.nml").read_text()
    assert text.count("input_filename = 'r'") == 2
    assert "input_filename = 'n'" not in text


def test_patching_a_continued_assignment_does_not_orphan_its_tail(env):
    # `parameter_filename` arrives written across two lines. Replacing only the
    # first would leave `'MOM_override'` behind as a line of its own, and the
    # namelist would no longer parse.
    run_dir = staged(env)
    text = (run_dir / "input.nml").read_text()
    assert "\n                              'MOM_override'" not in text
    assert text.count("parameter_filename") == 2


def test_the_coupling_timestep_comes_from_the_domain(env):
    # The one resolution-dependent value that lives in `input.nml` rather than
    # in `MOM_input`, and therefore the one a shared case directory cannot
    # carry. Without this the four Gulf resolutions would each need their own
    # copy of a file they otherwise agree on completely.
    config, _, _ = env
    config["model"]["coupling_seconds"] = 1800
    text = (staged(env) / "input.nml").read_text()
    assert "dt_cpld = 1800" in text
    assert "dt_atmos = 1800" in text
    assert "dt_cpld = 7200" not in text


def test_a_domain_that_states_no_coupling_timestep_keeps_the_cases(env):
    # Absent rather than zero: a model layer that does not set it leaves the
    # case's own value alone, which is what `om_1deg` relied on before the Gulf
    # domains needed this at all.
    config, _, _ = env
    config["model"].pop("coupling_seconds", None)
    assert "dt_cpld = 7200" in (staged(env) / "input.nml").read_text()


def test_no_layout_is_written_because_mom6_decomposes_for_itself(env):
    # MOM6 picks 4x2 at 8 PEs, 3x2 at 6, 1x5 at 5. A layout in configuration
    # would be a second home for the PE count and a thing to get wrong on every
    # machine with a different core count.
    run_dir = staged(env)
    assert not (run_dir / "MOM_layout").exists()
    assert not (run_dir / "SIS_layout").exists()
    assert "MOM_layout" not in (run_dir / "input.nml").read_text()


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


def test_a_forecast_with_no_slots_is_told_to_write_no_intermediate_restarts(env):
    """Written as zeros rather than omitted.

    A base case that set a `restart_interval` of its own would otherwise decide
    how much a cycling forecast writes, and the states would land in a run
    directory nothing sorts, which is the same class of surprise
    `input_filename` was.
    """
    run_dir = staged(env)
    assert "restart_interval = 0, 0, 0, 0, 0, 0" in (run_dir / "input.nml").read_text()


def test_a_cadence_reaches_the_model_as_one_run_that_dumps_as_it_goes(env):
    """The whole mechanism, in one namelist value.

    `coupler_main` compares its clock against the next interval on each coupled
    step and writes in place, so a window of states costs one model run and an
    extra restart write per slot. The alternative shape, a chain of short
    forecasts, pays a model initialization per slot and puts a restart handoff
    between each pair of them.
    """
    config, _, _ = env
    config["forecast"] = {"slots": "PT6H"}
    run_dir = staged(env)
    assert "restart_interval = 0, 0, 0, 6, 0, 0" in (run_dir / "input.nml").read_text()
    # And the run is still the whole cycle, not a slot of it.
    assert "hours = 24" in (run_dir / "input.nml").read_text()


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


# --- the sub-window states ---------------------------------------------------

#: The two sub-window times the fixture below writes a state at, and the names
#: the two writers actually give them. Both are transcribed from a real
#: `RESTART/` after a 12 hour gom_25km forecast with `restart_interval` set to
#: three hours, because the point of this fixture is that the two conventions
#: are genuinely unalike: MOM6 stamps its own restart with a year-day and a
#: second of the day, and FMS appends `YYYYMMDD.HHMMSS` to everything else while
#: `coupler_main` prepends it to its own clock files.
SLOTS = {
    datetime(2015, 1, 5, 4, tzinfo=timezone.utc):
        ("MOM.res_Y2015_D005_S14400.nc", "20150105.040000"),
    datetime(2015, 1, 5, 7, tzinfo=timezone.utc):
        ("MOM.res_Y2015_D005_S25200.nc", "20150105.070000"),
}


def written_restart(directory):
    """`RESTART/` as one run of the model leaves it with a cadence configured."""
    restart_set(directory)
    # Only written once an interval has actually passed, and it carries no
    # stamp, so it travels with the set rather than with the states.
    (directory / "coupler.intermediate.res").write_text("2015 1 5 13 0 0\n")
    for state, stamp in SLOTS.values():
        (directory / state).write_bytes(f"ocean {state}\n".encode())
        (directory / f"ice_model.res.nc{stamp}.nc").write_bytes(b"ice\n")
        (directory / f"{stamp}.coupler.res").write_text("2 (calendar type)\n")
        (directory / f"{stamp}.coupler.intermediate.res").write_text("2015 1 5\n")
    return directory


def slot_map(tmp_path):
    return {when: tmp_path / "bkg" / when.strftime("%Y%m%dT%H%M%SZ")
            for when in SLOTS}


def test_each_interval_state_lands_in_the_slot_directory_for_its_own_time(tmp_path):
    written = written_restart(tmp_path / "run" / "RESTART")

    mom6sis2.commit(written.parent, tmp_path / "rst",
                    slots=slot_map(tmp_path), restart="MOM.res.nc")

    # Named by the state's own valid time, and under the name every other state
    # in the experiment has, so `model.restart.ocn` keeps one spelling. MOM6's
    # own year-day stamp does not survive into the layout: it is how the file
    # was found, not what it is called.
    assert (tmp_path / "bkg" / "20150105T040000Z" / "MOM.res.nc").read_bytes() \
        == b"ocean MOM.res_Y2015_D005_S14400.nc\n"
    assert (tmp_path / "bkg" / "20150105T070000Z" / "MOM.res.nc").exists()


def test_a_state_the_cycle_asked_for_and_did_not_get_stops_the_cycle(tmp_path):
    """The failure `RESTART_CONTROL` at its default produces.

    `ocean_model_restart` writes nothing unless a bit is set, and the default
    bit overwrites `MOM.res.nc` rather than time-stamping it, so the model runs
    the interval, reports writing an intermediate restart, and leaves no state.
    Claiming each slot by the name it should have is what turns that into a
    stopped cycle instead of a `bkg/` that is quietly empty.
    """
    written = written_restart(tmp_path / "run" / "RESTART")
    (written / SLOTS[datetime(2015, 1, 5, 7, tzinfo=timezone.utc)][0]).unlink()

    with pytest.raises(mom6sis2.ModelError, match="RESTART_CONTROL"):
        mom6sis2.commit(written.parent, tmp_path / "rst",
                        slots=slot_map(tmp_path), restart="MOM.res.nc")


def test_the_restart_set_the_next_cycle_reads_holds_none_of_them(tmp_path):
    written = written_restart(tmp_path / "run" / "RESTART")
    target = tmp_path / "rst"

    mom6sis2.commit(written.parent, target, slots=slot_map(tmp_path),
                    restart="MOM.res.nc")

    # A stamped file left here is inert to the model and is then carried
    # forward by every cycle after it, one more each time, because the next
    # forecast links the whole directory into its own INPUT. The unstamped
    # `coupler.intermediate.res` is not one of those: `coupler_main` reads it
    # back to know when the last interval fell.
    assert sorted(p.name for p in target.iterdir()) == [
        "MOM.res.nc", "coupler.intermediate.res", "coupler.res", "ice_model.res.nc"]


def test_the_state_a_slot_keeps_is_the_ocean_and_nothing_else(tmp_path):
    """The ice restart at an interval is written by SIS2 and read by nothing.

    The fields metadata's ice section describes CICE's `cice.res.nc`, not SIS2's
    `ice_model.res.nc`, so an ice file in a slot directory is a file SOCA cannot
    open and disk that `cleanup` still has to carry.
    """
    written = written_restart(tmp_path / "run" / "RESTART")

    mom6sis2.commit(written.parent, tmp_path / "rst",
                    slots=slot_map(tmp_path), restart="MOM.res.nc")

    assert sorted(p.name for p in
                  (tmp_path / "bkg" / "20150105T040000Z").iterdir()) == ["MOM.res.nc"]


def test_a_forecast_that_was_asked_for_no_slots_keeps_none_of_them(tmp_path):
    """An extended forecast integrates past the window on its own cadence, so a
    state it wrote at the same clock time is a different trajectory."""
    written = written_restart(tmp_path / "run" / "RESTART")
    target = tmp_path / "rst"

    mom6sis2.commit(written.parent, target, restart="MOM.res.nc")

    assert sorted(p.name for p in target.iterdir()) == [
        "MOM.res.nc", "coupler.intermediate.res", "coupler.res", "ice_model.res.nc"]
    assert not [p for p in written.iterdir()]


def test_the_numbered_halves_of_a_split_restart_are_all_kept(tmp_path):
    """MOM6 writes `MOM.res.nc`, `MOM.res_1.nc` and up when a restart carries
    enough fields, and a slot missing one is a state SOCA reads as far as it
    goes and then reports a field it could not find."""
    when = datetime(2015, 1, 5, 4, tzinfo=timezone.utc)
    written = tmp_path / "run" / "RESTART"
    restart_set(written)
    for part in ("", "_1", "_2"):
        (written / f"MOM.res_Y2015_D005_S14400{part}.nc").write_bytes(b"ocean\n")

    mom6sis2.commit(written.parent, tmp_path / "rst",
                    slots={when: tmp_path / "one"}, restart="MOM.res.nc")

    assert sorted(p.name for p in (tmp_path / "one").iterdir()) == [
        "MOM.res.nc", "MOM.res_1.nc", "MOM.res_2.nc"]


def test_slots_with_no_restart_name_is_an_error_rather_than_a_guess(tmp_path):
    written = written_restart(tmp_path / "run" / "RESTART")
    with pytest.raises(mom6sis2.ModelError, match="model.restart.ocn"):
        mom6sis2.commit(written.parent, tmp_path / "rst", slots=slot_map(tmp_path))


# --- the handoff, when the forecast overshoots -------------------------------
#
# A four-dimensional window makes the forecast run half a window past the next
# analysis time, so the set the next cycle starts from is no longer the last
# thing the run wrote. It is one of the intervals, and every one of those is a
# complete set: `coupler_restart` writes the calendar and the model time at the
# interval, FMS writes each component's restart under the same stamp, and MOM6
# writes the ocean under its own. Integrating past a time does not change the
# state at it.

HANDOFF = datetime(2015, 1, 5, 7, tzinfo=timezone.utc)


def test_the_next_cycle_starts_from_the_interval_and_not_the_end_of_the_run(tmp_path):
    written = written_restart(tmp_path / "run" / "RESTART")
    target = tmp_path / "rst"

    mom6sis2.commit(written.parent, target, restart="MOM.res.nc", handoff=HANDOFF)

    # The same four files a restart set always holds, under the same names, so
    # nothing downstream can tell which one of the run's sets this was.
    assert sorted(p.name for p in target.iterdir()) == [
        "MOM.res.nc", "coupler.intermediate.res", "coupler.res", "ice_model.res.nc"]
    # And it is the state at the handoff, not the one at the end of the run.
    assert (target / "MOM.res.nc").read_bytes() \
        == b"ocean MOM.res_Y2015_D005_S25200.nc\n"


def test_the_sets_the_handoff_did_not_claim_are_discarded(tmp_path):
    written = written_restart(tmp_path / "run" / "RESTART")

    mom6sis2.commit(written.parent, tmp_path / "rst", restart="MOM.res.nc",
                    handoff=HANDOFF)

    # Including the unstamped one at the end of the run, which describes a time
    # half a window past anything this experiment resumes from.
    assert not [p for p in written.iterdir()]


def test_the_slot_at_the_handoff_is_the_restart_sets_own_file(tmp_path):
    """The handoff time is a sub-window time too: it is the centre of the next
    cycle's window, and MOM6 writes the state there once.

    A hard link rather than a copy, because it is the same state and a copy is
    a gigabyte, and rather than a symlink because `bkg/` and `rst/` are reaped
    independently and a link that outlives its target reads as a missing
    background only when something opens it.
    """
    written = written_restart(tmp_path / "run" / "RESTART")
    target = tmp_path / "rst"

    mom6sis2.commit(written.parent, target, slots=slot_map(tmp_path),
                    restart="MOM.res.nc", handoff=HANDOFF)

    slot = tmp_path / "bkg" / "20150105T070000Z" / "MOM.res.nc"
    assert slot.read_bytes() == (target / "MOM.res.nc").read_bytes()
    assert slot.stat().st_ino == (target / "MOM.res.nc").stat().st_ino
    assert not slot.is_symlink()
    # The slots that are not the handoff are moved as they always were.
    assert (tmp_path / "bkg" / "20150105T040000Z" / "MOM.res.nc").exists()


def test_a_handoff_the_run_never_wrote_a_set_at_stops_the_cycle(tmp_path):
    """`restart_interval` not dividing the cycle length produces exactly this.

    The model runs, exits zero, and leaves a `RESTART/` full of sets at times
    nothing asked about. Handing one of those forward would start the next cycle
    from the wrong hour, which nothing downstream can detect: `coupler.res`
    carries whatever date it carries and the model resumes happily.
    """
    written = written_restart(tmp_path / "run" / "RESTART")
    missed = datetime(2015, 1, 5, 10, tzinfo=timezone.utc)

    with pytest.raises(mom6sis2.ModelError, match="restart_interval"):
        mom6sis2.commit(written.parent, tmp_path / "rst", restart="MOM.res.nc",
                        handoff=missed)


def test_an_interval_with_a_clock_and_no_ocean_is_refused_not_handed_forward(
        tmp_path):
    """What a domain missing `RESTART_CONTROL = 2` actually leaves behind.

    MOM6 takes the default bit and overwrites one unstamped file per interval
    instead of stamping anything, while `coupler_main` and FMS stamp theirs
    regardless. So the interval looks present and is missing the only part that
    matters. Unrefused, `commit` deletes the unstamped ocean state as unclaimed
    and hands forward a clock, an ice state and nothing else, which every check
    ackbar makes accepts because they all key on `coupler.res`.
    """
    written = written_restart(tmp_path / "run" / "RESTART")
    for state, _ in SLOTS.values():
        (written / state).unlink()

    with pytest.raises(mom6sis2.ModelError, match="RESTART_CONTROL"):
        mom6sis2.commit(written.parent, tmp_path / "rst", restart="MOM.res.nc",
                        handoff=HANDOFF)

    # And nothing was committed on the way to finding out.
    assert not (tmp_path / "rst").exists()


def test_a_restart_set_commits_across_a_filesystem_boundary(tmp_path, monkeypatch):
    """Scratch and output are meant to be two filesystems.

    `paths.py` and `run._commit` both say so, and the first site file that
    honours it points scratch at a Lustre scratch and output at a project
    directory. A rename between two of them raises EXDEV, which would fail every
    forecast at its last step with the model run already paid for. It works on
    rancor only because both roots are under `/data`.
    """
    written = written_restart(tmp_path / "run" / "RESTART")
    real = os.replace
    crossed = []

    def one_filesystem_per_root(source, target):
        if Path(source).is_relative_to(tmp_path / "run") \
                and not Path(target).is_relative_to(tmp_path / "run"):
            crossed.append(Path(target).name)
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(source, target)

    monkeypatch.setattr(mom6sis2.os, "replace", one_filesystem_per_root)
    mom6sis2.commit(written.parent, tmp_path / "rst",
                    slots=slot_map(tmp_path), restart="MOM.res.nc")

    assert crossed                      # the boundary was actually reached
    assert (tmp_path / "rst" / "MOM.res.nc").exists()
    assert (tmp_path / "rst" / mom6sis2.STAMP).exists()
    assert (tmp_path / "bkg" / "20150105T040000Z" / "MOM.res.nc").read_bytes() \
        == b"ocean MOM.res_Y2015_D005_S14400.nc\n"
    # Nothing half-written is left under either name.
    assert not list((tmp_path / "rst").glob("*.partial"))


def test_a_set_with_no_ocean_in_it_is_refused_before_the_model_starts(
        env, tmp_path):
    """The other end of the same hole.

    A set that reached `rst/` without an ocean would otherwise be staged, and
    MOM6 would fail on the missing file a whole cycle after the thing that
    produced it reported success.
    """
    config, _, _ = env
    source = tmp_path / "rst"
    restart_set(source)
    (source / "MOM.res.nc").unlink()

    with pytest.raises(mom6sis2.ModelError, match="clock without an ocean"):
        mom6sis2.stage(config, tmp_path / "run", 1, "forecast", source=source)


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

    mom6sis2.keep_traces(run_dir, paths.log_dir(2), "forecast", 0)

    kept = sorted(p.name for p in (paths.log_dir(2)).iterdir())
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
    assert (paths.log_dir(1) / "forecast.mem000.ocean.stats").exists()


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

def test_both_forecasts_run_the_real_model(config):
    """One model layer, two tasks, and no branch in it.

    They differ in how long they integrate, how often they write and where the
    result goes. All three reach the model through `symbols(..., task=...)` and
    the target `_forecast` computes, so nothing below that point knows which of
    the two it is running.
    """
    assert run.real_model(config, "forecast")
    assert run.real_model(config, "forecast.ext")
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
    # A free run evaluates observations against the set being dropped, and that
    # `hofx` is a leaf, so `cleanup` waits for it as well as for the restarts.
    # `post.state` reads the same dropped set and is a leaf on the same event,
    # so it is in the proof for the same reason.
    for task in ("hofx", "post.state"):
        member = None if task == "hofx" else 0
        proof = paths.sentinel(2, task, member)
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text("{}")

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
        "scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static", "root": str(REPO),
    })
    model = merged["model"]
    assert os.path.isdir(model["base"])
    assert os.path.isfile(model["base"] + "/input.nml")
    assert os.path.isfile(model["diag_table"]["forecast"])


#: Where the per-domain overrides live, and the shared base they must not
#: duplicate. Two spellings of one parameter is a fatal in MOM6, so `common/`
#: deliberately ships no `MOM_override` at all.
DOMAINS = REPO / "config" / "model" / "mom6sis2" / "domain"


def test_every_domain_keeps_the_back_compatibility_bugs_off():
    """Guarded here rather than only where a real model reports what it ran.

    Dropping the back-compatibility pins is a closed decision whose reversal
    invalidates every initial condition already generated, and it was checked
    only by a tier 3 assertion: delete the line from a domain and tiers 0 to 2
    stay green, on a machine with no model built at all.
    """
    overrides = sorted(DOMAINS.glob("*/*/MOM_override")) \
        + sorted(DOMAINS.glob("*/MOM_override"))
    assert overrides, f"no MOM_override found under {DOMAINS}"
    for path in overrides:
        assert "ENABLE_BUGS_BY_DEFAULT = False" in path.read_text(), path


def test_every_domain_asks_mom6_for_a_time_stamped_restart():
    """What makes a sub-window state and an interval handoff exist at all.

    Bit 0 is the default and overwrites one unstamped file, so a forecast given
    a `restart_interval` on a domain missing this ends holding the state it
    started from, having reported success at every step.
    """
    for path in sorted(DOMAINS.glob("*/*/MOM_override")) \
            + sorted(DOMAINS.glob("*/MOM_override")):
        assert "RESTART_CONTROL = 2" in path.read_text(), path


def test_the_shared_base_ships_no_override_to_be_overridden_twice():
    assert not (DOMAINS / "gom" / "common" / "MOM_override").exists()

