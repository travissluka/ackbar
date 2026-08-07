"""Tier 1: `ackbar validate`, the whole of the early warning system.

There is no step that asks JEDI whether it likes its own configuration, because
JEDI has no reliable parse-and-exit at the application level. That makes these
checks the only ones there are between a config and an eight hour run, and step
3 in particular is what catches the failure that actually happens: a path that
is not there.
"""

import stat
from pathlib import Path

import pytest
import yaml

from ackbar.cli import main
from ackbar.config.jobtime import render, symbols
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import build_graph, job_time_context
from ackbar.validate import validate_experiment

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

SITE = {
    "scratch_root": "/scratch",
    "output_root": "/out",
    "static_root": "/static",
    "max_submit_jobs": "10000",
    "max_array_size": "1000",
    "root": str(REPO),
}

FIXTURES = [
    "free_om1deg",
    "var_om1deg",
    # Here rather than deferred, since FGAT landed. What it pins is the
    # four-dimensional *arithmetic*, the overshoot and the slot cadence, which
    # `fgat` and `4d` share and which is computed from the window's length.
    "fourd_om1deg",
    # And 4D-Ens-Var, which was the deferred one until its document landed.
    # There is no deferred fixture any more: every window type in the schema
    # builds. What used to be a phase gate here is now a permanent refusal in
    # `graph.build._check_four_d_covariance`, and it is about a combination that
    # cannot exist rather than one that is not written yet, so it is pinned in
    # `test_graph.py` beside the other graph checks.
    "fourdenvar_om1deg",
    "letkf_om1deg",
    "envar_om1deg",
    "hybrid_om1deg",
]


@pytest.fixture(scope="module")
def keys():
    return merge_keys(load_schema())


@pytest.fixture(scope="module")
def schema():
    return load_schema()


def load(name, keys, path=None, site=None):
    layers = resolve_layers(path or EXPERIMENTS / f"{name}.yaml", LAYERS)
    return resolve(merge_layers(layers, keys), site or SITE)


def steps(findings):
    return sorted({f.step for f in findings})


def offline(config, schema, site=None):
    findings, _, _ = validate_experiment(
        config, schema, site or SITE, str(REPO), offline=True
    )
    return findings


def full(config, schema, site=None, root=None):
    findings, _, _ = validate_experiment(
        config, schema, site or SITE, root or str(REPO)
    )
    return findings


def stage(findings):
    """Create whatever step 3 said was missing.

    Directories first: `obs_dir` itself is a referenced input, and touching it
    as a file would make its children impossible.

    Observation files are `stage_observations`'s job and are skipped here, both
    the ones reported by name and the ones a required observer reports by path.
    A test that means to leave a gap in the archive and has it filled in by the
    generic stager passes for the wrong reason.
    """
    for path in sorted(f.where for f in findings):
        if not path.startswith("/") or path.endswith(".nc4"):
            continue
        target = Path(path)
        if target.suffix:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        else:
            target.mkdir(parents=True, exist_ok=True)


def observation_files(config):
    """Every observation file the experiment will look for, over every cycle.

    Rendered the way validate renders it rather than globbed off disk, because
    the point of these tests is what the configuration asks for.
    """
    found = set()
    for cycle, member in job_time_context(config, build_graph(config)):
        rendered = render(config, symbols(config, cycle, member))
        for entry in rendered.get("observations") or ():
            space = entry.get("obs space") or {}
            engine = (space.get("obsdatain") or {}).get("engine") or {}
            if engine.get("obsfile"):
                found.add(Path(engine["obsfile"]))
    return sorted(found)


def stage_observations(config, skip=()):
    """Create the archive, optionally leaving some files out.

    A skip that matches nothing is an error rather than a no-op: a test that
    means to leave a gap and leaves none passes for the wrong reason, and the
    windows here are dates that are easy to mistype.
    """
    left_out = 0
    for path in observation_files(config):
        if any(part in str(path) for part in skip):
            left_out += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert not skip or left_out, f"nothing matched {skip}"


class TestTheFixturesAreClean:
    @pytest.mark.parametrize("name", FIXTURES)
    def test_config_and_graph_checks_pass(self, name, keys, schema):
        assert offline(load(name, keys), schema) == []

    @pytest.mark.parametrize("name", FIXTURES)
    def test_the_cli_agrees(self, name, capsys):
        assert main(["validate", "--offline", str(EXPERIMENTS / f"{name}.yaml")]) == 0
        assert "ok:" in capsys.readouterr().out

    def test_a_four_dimensional_letkf_is_clean(self, keys, schema):
        # There was a phase gate here refusing four-dimensional windows while
        # no document built one, and it had to exempt the LETKF because it is
        # `config/soca/var.yaml` that could not do it and the LETKF does not
        # read that file. Every window type builds now and the gate is gone;
        # this stays as the assertion that nothing refuses the combination on
        # the window type alone.
        config = load("letkf_om1deg", keys)
        config["solver"]["window"] = {"type": "4d"}
        config["forecast"] = {"slots": "PT12H"}
        assert offline(config, schema) == []


class TestStep1Configuration:
    def test_a_schema_violation_is_reported(self, keys, schema):
        config = load("var_om1deg", keys)
        config["cycle"]["count"] = "thirty"
        assert steps(offline(config, schema)) == [1]

    def test_a_yaml_version_ambiguous_number_is_reported(self, keys, schema):
        # '500e3' is a string to PyYAML and the number 500000 to eckit, and the
        # re-emitted file still looks right, so nothing else would catch it.
        config = load("letkf_om1deg", keys)
        config["vars"]["obs_distribution"] = {"name": "Halo",
                                              "halo size": "500e3"}
        found = offline(config, schema)
        assert [f.step for f in found] == [1]
        assert "500e3" in found[0].message

    @pytest.mark.parametrize("where,value", [
        ("start", "not-a-date"),
        ("length", "Potato"),
    ])
    def test_a_date_the_schema_cannot_describe_is_reported_and_not_raised(
            self, where, value, keys, schema):
        """`format: date-time` is annotation-only and `pattern: '^P'` takes `Potato`.

        Left to the parser downstream, a one-character typo in a date raised out
        of the middle of `validate` with no config path and skipped every step
        after it, which is the silently-partial report the command exists to
        prevent.
        """
        config = load("var_om1deg", keys)
        config["cycle"][where] = value
        found = offline(config, schema)
        assert [f.where for f in found if f.step == 1] == [f"cycle.{where}"]


class TestStep2EveryJobsConfig:
    def test_an_unknown_job_time_symbol_is_caught_before_submission(self, keys, schema):
        config = load("var_om1deg", keys)
        config["observations"][0]["obs space"]["obsdatain"]["engine"]["obsfile"] = \
            "/archive/{{windo_begin}}.nc4"
        found = offline(config, schema)
        assert steps(found) == [2]
        assert "windo_begin" in found[0].message

    def test_the_same_bad_symbol_is_reported_once_and_not_once_per_cycle(
        self, keys, schema
    ):
        config = load("hybrid_om1deg", keys)
        config["vars"]["broken"] = "{{nope}}"
        assert len(offline(config, schema)) == 1


class TestStep3InputPaths:
    """The step that does most of the real work.

    Malformed JEDI YAML is rare here because it is generated from data
    structures rather than templated. Missing files are the common failure.
    """

    @pytest.fixture
    def local_site(self, tmp_path):
        """A site whose offline stages are under `tmp_path`, so they can be made.

        The module's `/static` is deliberately not any real machine's root,
        which also means nothing can create it. These tests are about staging
        the inputs an experiment names, so they need somewhere to stage them.
        """
        return dict(SITE, static_root=str(tmp_path / "static"))

    @pytest.fixture
    def staged(self, tmp_path, local_site, keys, schema):
        """An experiment whose observation archive is a real directory."""
        source = yaml.safe_load((EXPERIMENTS / "var_om1deg.yaml").read_text())
        source["vars"]["obs_dir"] = str(tmp_path / "obs")
        path = tmp_path / "staged.yaml"
        path.write_text(yaml.safe_dump(source))
        return load(None, keys, path, site=local_site)

    def test_the_experiment_passes_once_everything_it_names_is_staged(
        self, staged, schema, local_site
    ):
        stage(full(staged, schema, site=local_site))
        stage_observations(staged)
        assert full(staged, schema, site=local_site) == []

    def test_a_missing_grid_file_is_rejected(self, staged, schema, local_site):
        stage_observations(staged)
        found = full(staged, schema, site=local_site)
        assert steps(found) == [3]
        assert all("does not exist" in f.message for f in found)

    def test_an_unreadable_input_is_rejected_too(self, staged, schema, local_site):
        stage(full(staged, schema, site=local_site))
        stage_observations(staged)
        target = observation_files(staged)[0]
        target.chmod(0)
        try:
            found = full(staged, schema, site=local_site)
            assert [f.message for f in found] == ["observation file is not readable"]
        finally:
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_an_archive_with_a_gap_is_data_and_not_a_finding(
        self, staged, schema, local_site
    ):
        """The rule the whole observation path is built around.

        A platform goes down for a week and the windows it covers have no file.
        `stage.obs` drops the observer for those cycles and records it, and
        refusing to create the experiment over it would make ACKBAR unusable on
        any real record.
        """
        stage(full(staged, schema, site=local_site))
        stage_observations(staged, skip=("2018041600",))
        assert full(staged, schema, site=local_site) == []

    def test_an_observer_with_no_file_in_any_cycle_is_a_typo_and_is_reported(
        self, staged, schema, local_site
    ):
        # The same absence, all the way through, is a wrong path rather than a
        # gap, and it produces an experiment that runs to completion
        # assimilating nothing.
        stage(full(staged, schema, site=local_site))
        stage_observations(staged, skip=("adt_3a",))
        found = full(staged, schema, site=local_site)
        assert [f.where for f in found] == ["observations.adt_3a"]
        assert "wrong path" in found[0].message

    def test_a_required_observer_is_reported_for_every_window_it_is_missing(
        self, staged, schema, local_site
    ):
        # `required` is how an experiment says its own gap is not acceptable, so
        # the check reverts to file by file.
        staged["observations"][0]["obs space"]["$required"] = True
        stage(full(staged, schema, site=local_site))
        stage_observations(staged, skip=("2018041600",))
        found = full(staged, schema, site=local_site)
        assert [f.where.endswith("2018041512.nc4") for f in found] == [True]
        assert "required" in found[0].message

    def test_a_saber_filepath_is_a_stem_and_the_file_it_names_ends_in_nc(
        self, keys, schema
    ):
        """saber's `filepath` is not the name of anything on disk.

        It reads and writes parameter files through `util::readFieldSet`, which
        takes a stem and appends `.nc`. Checked literally, the diffusion
        calibration would report as a missing input on a domain where it is
        present, forever, which is the fastest way to teach a reader that step 3
        is noise.
        """
        found = [
            f.where for f in full(load("var_om1deg", keys), schema)
            if "/diffusion/" in f.where
        ]
        assert found, "the variational fixture names no diffusion parameters"
        assert all(where.endswith(".nc") for where in found)

    def test_output_paths_are_not_mistaken_for_inputs(self, keys, schema):
        # The experiment is about to create them; that is the point of it.
        config = load("var_om1deg", keys)
        outputs = [
            f.where for f in full(config, schema)
            if f.where.startswith(("/out", "/scratch"))
        ]
        assert outputs == []


class TestStep2Templates:
    """The SOCA document templates, checked before one is frozen.

    Everything here is a property of the file alone, which is why it needs no
    cycle and no observer list. Whether the slots a template declares match what
    the task computes needs both halves and is pinned by
    `tests/test_templates.py`.
    """

    def rooted(self, tmp_path, text, name="var.yaml"):
        (tmp_path / "config" / "soca").mkdir(parents=True)
        (tmp_path / "config" / "soca" / name).write_text(text)
        return tmp_path

    def found(self, tmp_path, text, keys, schema, name="var.yaml"):
        root = self.rooted(tmp_path, text, name)
        return [f for f in full(load("var_om1deg", keys), schema, root=str(root))
                if f.step == 2]

    def test_a_template_that_is_not_yaml_is_rejected(self, tmp_path, keys, schema):
        found = self.found(tmp_path, "a: [1,\nb: 2\n", keys, schema)
        assert len(found) == 1
        assert "not parseable YAML" in found[0].message

    def test_a_lowercase_slot_is_rejected(self, tmp_path, keys, schema):
        """It would be an experiment-time symbol, and this file never sees that pass."""
        found = self.found(tmp_path, "output: $(analysis_output)\n", keys, schema)
        assert len(found) == 1
        assert "lowercase" in found[0].message

    def test_an_unknown_job_time_symbol_is_rejected(self, tmp_path, keys, schema):
        found = self.found(tmp_path, "when: '{{analysis_time}}'\n", keys, schema)
        assert len(found) == 1
        assert "unknown job-time symbol {{analysis_time}}" in found[0].message

    def test_a_known_job_time_symbol_with_a_format_spec_is_accepted(
            self, tmp_path, keys, schema):
        found = self.found(tmp_path, "when: 'd{{current_cycle:%Y%m%d}}'\n",
                           keys, schema)
        assert found == []

    def test_the_real_templates_pass(self, keys, schema):
        found = [f for f in full(load("var_om1deg", keys), schema) if f.step == 2]
        assert found == []


class TestStep4Executables:
    def test_a_missing_executable_is_rejected(self, tmp_path, keys, schema):
        found = full(load("var_om1deg", keys), schema, root=str(tmp_path))
        assert steps(found) == [3, 4]
        missing = sorted(f.where for f in found if f.step == 4)
        assert missing == [
            "pkg/jedi/build/bin/soca_var.x",
            "pkg/mom6sis2/ice_ocean_SIS2/build/coupler_main",
        ]

    def test_a_file_that_is_not_runnable_is_rejected(self, tmp_path, keys, schema):
        for relative in (
            "pkg/jedi/build/bin/soca_var.x",
            "pkg/mom6sis2/ice_ocean_SIS2/build/coupler_main",
        ):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o644)
        found = [f for f in full(load("var_om1deg", keys), schema, root=str(tmp_path))
                 if f.step == 4]
        assert len(found) == 2
        assert all("not runnable" in f.message for f in found)

    def test_the_real_tree_has_the_executables_the_graph_names(self, keys, schema):
        # Which is also a check that the names in tasks.py are the ones the
        # bundle actually builds.
        found = [f for f in full(load("letkf_om1deg", keys), schema) if f.step == 4]
        assert found == []


class TestStep5QueueLimits:
    def test_a_projected_job_count_over_the_limit_is_refused(self, keys, schema):
        # Every array task counts individually, which is what makes a large
        # ensemble approach a per-user cap.
        site = {**SITE, "max_submit_jobs": "40"}
        found = [f for f in full(load("letkf_om1deg", keys), schema, site) if f.step == 5]
        assert len(found) == 1
        assert "max_submit_jobs" in found[0].message

    def test_the_throttle_multiplies_the_projection(self, keys, schema):
        config = load("letkf_om1deg", keys)
        site = {**SITE, "max_submit_jobs": "100"}
        assert [f for f in full(config, schema, site) if f.step == 5] == []
        config["cycle"]["throttle"] = 4
        assert [f for f in full(config, schema, site) if f.step == 5] != []

    def test_an_array_over_the_sites_max_array_size_is_refused(self, keys, schema):
        site = {**SITE, "max_array_size": "10"}
        found = [f for f in full(load("letkf_om1deg", keys), schema, site) if f.step == 5]
        assert "max_array_size" in found[0].message

    def test_a_site_that_declares_no_limits_is_not_second_guessed(self, keys, schema):
        site = {"scratch_root": "/scratch", "output_root": "/out"}
        assert [f for f in full(load("letkf_om1deg", keys), schema, site)
                if f.step == 5] == []


class TestStep6Graph:
    def test_a_task_with_no_resources_is_reported(self, keys, schema):
        # Otherwise it is submitted with whatever sbatch defaults to, which on
        # a real machine is a single core.
        config = load("var_om1deg", keys)
        del config["domain"]["resources"]["default"]
        del config["domain"]["resources"]["da"]
        found = offline(config, schema)
        assert steps(found) == [6]
        assert any(f.where == "domain.resources.da" for f in found)

    def test_a_broken_graph_stops_the_later_steps(self, keys, schema):
        # Nothing downstream of a graph that will not build is meaningful.
        config = load("hybrid_om1deg", keys)
        config["forecast"]["extended"]["every"] = "PT7H"
        found = offline(config, schema)
        assert steps(found) == [6]


class TestOfflineIsAStatedSubset:
    def test_offline_skips_exactly_the_filesystem_steps(self, keys, schema):
        config = load("var_om1deg", keys)
        assert steps(full(config, schema)) == [3]
        assert steps(offline(config, schema)) == []

    def test_the_cli_says_which_steps_it_skipped(self, capsys):
        main(["validate", "--offline", str(EXPERIMENTS / "var_om1deg.yaml")])
        out = capsys.readouterr().out
        assert out.count("not run (--offline)") == 3
        assert "1. the merged config" in out
