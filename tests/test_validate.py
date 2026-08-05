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
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.validate import validate_experiment

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

SITE = {
    "scratch_root": "/scratch",
    "output_root": "/out",
    "max_submit_jobs": "10000",
    "max_array_size": "1000",
    "root": str(REPO),
}

FIXTURES = [
    "free_om1deg",
    "var_om1deg",
    "fourd_om1deg",
    "letkf_om1deg",
    "hybrid_om1deg",
]


@pytest.fixture(scope="module")
def keys():
    return merge_keys(load_schema())


@pytest.fixture(scope="module")
def schema():
    return load_schema()


def load(name, keys, path=None):
    layers = resolve_layers(path or EXPERIMENTS / f"{name}.yaml", LAYERS)
    return resolve(merge_layers(layers, keys), SITE)


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
    """
    for path in sorted(f.where for f in findings):
        target = Path(path)
        if target.suffix:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        else:
            target.mkdir(parents=True, exist_ok=True)


class TestTheFixturesAreClean:
    @pytest.mark.parametrize("name", FIXTURES)
    def test_config_and_graph_checks_pass(self, name, keys, schema):
        assert offline(load(name, keys), schema) == []

    @pytest.mark.parametrize("name", FIXTURES)
    def test_the_cli_agrees(self, name, capsys):
        assert main(["validate", "--offline", str(EXPERIMENTS / f"{name}.yaml")]) == 0
        assert "ok:" in capsys.readouterr().out


class TestStep1Configuration:
    def test_a_schema_violation_is_reported(self, keys, schema):
        config = load("var_om1deg", keys)
        config["cycle"]["count"] = "thirty"
        assert steps(offline(config, schema)) == [1]

    def test_a_yaml_version_ambiguous_number_is_reported(self, keys, schema):
        # '500e3' is a string to PyYAML and the number 500000 to eckit, and the
        # re-emitted file still looks right, so nothing else would catch it.
        config = load("letkf_om1deg", keys)
        config["vars"]["obs_distribution_options"] = {"halo size": "500e3"}
        found = offline(config, schema)
        assert [f.step for f in found] == [1]
        assert "500e3" in found[0].message


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
    def staged(self, tmp_path, keys, schema):
        """An experiment whose observation archive is a real directory."""
        source = yaml.safe_load((EXPERIMENTS / "var_om1deg.yaml").read_text())
        source["vars"]["obs_dir"] = str(tmp_path / "obs")
        path = tmp_path / "staged.yaml"
        path.write_text(yaml.safe_dump(source))
        return path

    def test_a_missing_observation_file_is_rejected(self, staged, keys, schema):
        found = full(load(None, keys, staged), schema)
        assert steps(found) == [3]
        assert all("does not exist" in f.message for f in found)

    def test_the_same_experiment_passes_once_the_archive_is_staged(
        self, staged, keys, schema
    ):
        config = load(None, keys, staged)
        stage(full(config, schema))
        assert full(config, schema) == []

    def test_an_unreadable_input_is_rejected_too(self, staged, keys, schema):
        config = load(None, keys, staged)
        stage(full(config, schema))
        target = next(Path(config["vars"]["obs_dir"]).rglob("*.nc4"))
        target.chmod(0)
        try:
            found = full(config, schema)
            assert [f.message for f in found] == ["input path is not readable"]
        finally:
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_output_paths_are_not_mistaken_for_inputs(self, keys, schema):
        # The experiment is about to create them; that is the point of it.
        config = load("var_om1deg", keys)
        outputs = [
            f.where for f in full(config, schema)
            if f.where.startswith(("/out", "/scratch"))
        ]
        assert outputs == []


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
