"""The merge rules against the real ported soca-science observer configs.

The keyed-list rule is the one that is expensive to change later, because all
~27 observer configs bake it in. Toy fixtures are not convincing evidence that
it is right; these are the actual files.
"""

from pathlib import Path

import pytest
import yaml

from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.lint import ambiguous_numbers
from ackbar.config.resolve import resolve, unresolved
from ackbar.config.schema import load_schema, merge_keys, validate
from ackbar.config.why import responsible_layer, why

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

#: A fixed site, so the tests do not depend on which machine they run on.
SITE = {"scratch_root": "/scratch", "output_root": "/out"}


@pytest.fixture(scope="module")
def keys():
    return merge_keys(load_schema())


@pytest.fixture
def letkf(keys):
    layers = resolve_layers(EXPERIMENTS / "letkf_om1deg.yaml", LAYERS)
    return layers, merge_layers(layers, keys)


def _strings(node, path="") -> list:
    """Every string in a config, as (path, value). Keys count too: the sed era
    put tokens in key position, as in `!IF_APP_letkf obs localizations:`."""
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                out.append((here, key))
            out += _strings(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += _strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        out.append((path, node))
    return out


def observer(config, name):
    for entry in config["observations"]:
        if entry["obs space"]["name"] == name:
            return entry
    raise AssertionError(f"no observer {name!r}")


def test_the_declared_merge_key_comes_from_the_schema(keys):
    # One document declares both the shape and how it merges, so they cannot
    # drift apart.
    assert keys == {"observations": "obs space.name"}


class TestLetkfLayerAddressesOneObserverAtATime:
    """The case the whole keyed-merge design exists for."""

    def test_no_observer_is_duplicated(self, letkf):
        _, config = letkf
        names = [o["obs space"]["name"] for o in config["observations"]]
        assert names == ["adt_3a", "sst_noaa19"]

    def test_letkf_adds_localization_without_restating_the_observer(self, letkf):
        _, config = letkf
        adt = observer(config, "adt_3a")
        assert adt["obs localizations"][0]["localization method"] == "Rossby"
        # everything the obs layer supplied survived the overlay
        assert adt["obs operator"]["name"] == "ADT"
        assert adt["obs space"]["simulated variables"] == ["absoluteDynamicTopography"]

    def test_each_observer_keeps_its_own_filter_chain(self, letkf):
        _, config = letkf
        assert len(observer(config, "adt_3a")["obs filters"]) == 5
        assert len(observer(config, "sst_noaa19")["obs filters"]) == 4


class TestValuesThatUsedToBeYamlAnchors:
    """soca-science varied these by redefining anchors in each solver file.

    They are ordinary layered values now, which is what they always were.
    """

    def test_solver_layer_owns_the_distribution(self, letkf):
        _, config = letkf
        assert config["vars"]["obs_distribution"] == "Halo"
        assert config["vars"]["obs_distribution_options"] == {"halo size": 500000}

    def test_land_mask_threshold_differs_by_solver(self, keys):
        def land_mask(experiment):
            layers = resolve_layers(EXPERIMENTS / experiment, LAYERS)
            return merge_layers(layers, keys)["vars"]["obs_land_mask_min"]

        assert land_mask("letkf_om1deg.yaml") == 0.5
        assert land_mask("var_om1deg.yaml") == 0.9

    def test_the_observer_references_the_threshold_rather_than_carrying_it(self, letkf):
        # Filter chains replace wholesale, so the varying part has to be
        # substituted, not merged. This is why substitution runs after merge.
        _, config = letkf
        mask = observer(config, "adt_3a")["obs filters"][0]
        assert mask["where"][0]["minvalue"] == "$(obs_land_mask_min)"


class TestBlame:
    def test_why_names_the_layer_that_set_a_value(self, letkf, keys):
        layers, _ = letkf
        assert responsible_layer(layers, "vars.obs_distribution", keys) == "da/letkf"
        assert responsible_layer(layers, "domain.name", keys) == "domain/om_1deg"
        assert responsible_layer(layers, "ensemble.size", keys) == "letkf_om1deg"

    def test_why_shows_every_layer_that_changed_a_value(self, keys):
        layers = resolve_layers(EXPERIMENTS / "letkf_om1deg.yaml", LAYERS)
        history = why(layers, "solver.name", keys)
        assert history == [("da/letkf", "letkf")]

    def test_why_reports_nothing_for_a_value_no_layer_sets(self, letkf, keys):
        layers, _ = letkf
        assert responsible_layer(layers, "vars.nonexistent", keys) is None


class TestPortedFilesAreClean:
    def test_every_layer_file_parses_standalone(self):
        # The originals do not: they reference anchors defined in the solver
        # file, so they are only valid once sed has concatenated them.
        for path in sorted(LAYERS.rglob("*.yaml")):
            with open(path) as f:
                assert isinstance(yaml.safe_load(f), dict), path

    def test_no_sed_era_tokens_survive_the_port(self):
        # Checked against the parsed data, not the file text: the port notes
        # name the tokens they removed, and that is documentation.
        tokens = ("!IF_APP_", "!IF_", "__SEED__", "__DA_", "__OBSERVATIONS__")
        for path in sorted(LAYERS.rglob("*.yaml")):
            with open(path) as f:
                for where, text in _strings(yaml.safe_load(f)):
                    for token in tokens:
                        assert token not in text, f"{path}: {where} contains {token}"

    def test_no_layer_carries_a_yaml_version_ambiguous_number(self, keys):
        for path in sorted(LAYERS.rglob("*.yaml")):
            with open(path) as f:
                found = ambiguous_numbers(yaml.safe_load(f))
            assert found == [], f"{path}: {found}"


class TestResolvedPort:
    """The ported observers after substitution, which is what JEDI would see."""

    @pytest.fixture
    def resolved(self, letkf):
        _, config = letkf
        return resolve(config, SITE)

    def test_the_land_mask_threshold_becomes_a_number(self, resolved):
        mask = observer(resolved, "adt_3a")["obs filters"][0]
        value = mask["where"][0]["minvalue"]
        assert value == 0.5
        assert isinstance(value, float), "a string here is rejected by UFO"

    def test_the_distribution_resolves_from_the_solver_layer(self, resolved):
        space = observer(resolved, "sst_noaa19")["obs space"]
        assert space["distribution"]["name"] == "Halo"
        assert space["distribution"]["options"] == {"halo size": 500000}

    def test_input_paths_come_from_the_archive_and_output_from_the_layout(self, resolved):
        space = observer(resolved, "adt_3a")["obs space"]
        assert space["obsdatain"]["engine"]["obsfile"].startswith(
            "/archive/obs/osse_2018/"
        )
        assert space["obsdataout"]["engine"]["obsfile"].startswith(
            "/out/letkf_om1deg/obs_out/"
        )

    def test_job_time_tokens_survive_the_experiment_time_pass(self, resolved):
        space = observer(resolved, "adt_3a")["obs space"]
        assert "{{current_cycle}}" in space["obsdatain"]["engine"]["obsfile"]
        assert "{{window_begin}}" in space["obsdatain"]["engine"]["obsfile"]
        assert space["obs perturbations seed"] == "{{seed}}"

    def test_nothing_is_left_unsubstituted(self, resolved):
        assert unresolved(resolved) == []


class TestSchema:
    def test_the_fixture_experiments_validate(self, keys):
        schema = load_schema()
        for name in ("letkf_om1deg.yaml", "var_om1deg.yaml"):
            layers = resolve_layers(EXPERIMENTS / name, LAYERS)
            config = resolve(merge_layers(layers, keys), SITE)
            assert validate(config, schema) == [], name
