"""Merge-key extraction from the schema, and validation of merged configs."""

import pytest

from ackbar.config.schema import load_schema, merge_keys, validate


class TestMergeKeyExtraction:
    def test_annotation_on_a_top_level_array(self):
        schema = {"properties": {"observations": {
            "type": "array", "x-ackbar-merge-key": "obs space.name",
        }}}
        assert merge_keys(schema) == {"observations": "obs space.name"}

    def test_items_adds_no_path_component(self):
        # A schema path has no indices, so a list inside a list element is
        # addressed as parent.child, matching merge.schema_path.
        schema = {"properties": {"observations": {
            "type": "array",
            "x-ackbar-merge-key": "obs space.name",
            "items": {"properties": {"chain": {
                "type": "array", "x-ackbar-merge-key": "id",
            }}},
        }}}
        assert merge_keys(schema) == {
            "observations": "obs space.name",
            "observations.chain": "id",
        }

    def test_an_unannotated_schema_declares_no_keys(self):
        assert merge_keys({"properties": {"a": {"type": "array"}}}) == {}

    def test_combinators_are_searched(self):
        schema = {"allOf": [{"properties": {"a": {
            "type": "array", "x-ackbar-merge-key": "name",
        }}}]}
        assert merge_keys(schema) == {"a": "name"}


@pytest.fixture(scope="module")
def schema():
    return load_schema()


class TestRealSchema:
    def test_declares_the_observations_key(self, schema):
        assert merge_keys(schema)["observations"] == "obs space.name"

    def test_the_merge_key_annotation_does_not_break_validation(self, schema):
        # jsonschema must treat x- keywords as annotations, not constraints.
        config = _minimal()
        config["observations"] = [{"obs space": {"name": "adt"}}]
        assert validate(config, schema) == []

    def test_a_missing_required_key_is_reported(self, schema):
        config = _minimal()
        del config["cycle"]
        errors = validate(config, schema)
        assert any("cycle" in message for _, message in errors)

    def test_a_wrong_type_is_reported_with_its_path(self, schema):
        config = _minimal()
        config["ensemble"] = {"size": "twenty"}
        errors = validate(config, schema)
        assert ("ensemble.size", "'twenty' is not of type 'integer'") in errors

    def test_an_unknown_key_in_an_ackbar_owned_block_is_rejected(self, schema):
        config = _minimal()
        config["solver"]["algorithm"] = "RPCG"
        assert any(path == "solver" for path, _ in validate(config, schema))

    def test_an_unknown_key_inside_an_observer_is_allowed(self, schema):
        # Below `obs space` the config is UFO's, and ACKBAR does not model it.
        config = _minimal()
        config["observations"] = [{
            "obs space": {"name": "adt", "anything UFO accepts": [1, 2]},
            "obs operator": {"name": "ADT"},
        }]
        assert validate(config, schema) == []

    def test_an_observer_without_a_name_is_rejected(self, schema):
        # The merge key must be present, and the schema says so too.
        config = _minimal()
        config["observations"] = [{"obs space": {}}]
        assert any("name" in message for _, message in validate(config, schema))

    def test_every_error_is_returned_not_just_the_first(self, schema):
        config = _minimal()
        config["ensemble"] = {"size": "twenty"}
        config["solver"]["name"] = "nonsense"
        assert len(validate(config, schema)) >= 2

    def test_the_stub_model_must_declare_its_cost(self, schema):
        config = _minimal()
        config["model"] = {"name": "stub"}
        assert validate(config, schema) != []
        config["model"]["stub"] = {"seconds": 30}
        assert validate(config, schema) == []

    def test_a_variational_solver_must_state_its_b_and_its_minimizer(self, schema):
        # An analysis with no background error and no minimizer is not an
        # under-specified experiment, it is a different one: the application
        # takes OOPS defaults for both and produces an analysis nobody chose.
        config = _minimal()
        config["solver"] = {"name": "variational"}
        missing = {message for _, message in validate(config, schema)}
        for key in ("analysis variables", "background variables",
                    "background error", "variational"):
            assert f"'{key}' is a required property" in missing

        config["solver"].update({
            "analysis variables": ["sea_water_potential_temperature"],
            "background variables": ["sea_water_potential_temperature",
                                     "sea_water_cell_thickness"],
            "background error": {"covariance model": "SABER"},
            "variational": {"minimizer": {"algorithm": "RPCG"}},
        })
        assert validate(config, schema) == []

    def test_the_saber_config_below_the_solver_is_not_modelled(self, schema):
        # Same rule as the body of an observer: ACKBAR names the block and does
        # not describe its insides, because that is SABER's config and not ours.
        config = _minimal()
        config["solver"] = {
            "name": "variational",
            "analysis variables": ["sea_water_salinity"],
            "background variables": ["sea_water_salinity"],
            "background error": {
                "saber central block": {"anything SABER accepts": [1, 2]},
            },
            "variational": {"iterations": [{"ninner": 5, "whatever": True}]},
        }
        assert validate(config, schema) == []

    def test_an_experiment_name_that_would_not_survive_sacct_is_rejected(self, schema):
        config = _minimal()
        config["experiment"]["name"] = "my experiment"
        assert any(path == "experiment.name" for path, _ in validate(config, schema))

    def test_a_stochastic_timescale_that_is_not_a_duration_is_rejected(self, schema):
        # It was `{type: string}`, so `banana` passed all six validate steps and
        # `parse_duration` raised inside the forecast job instead, hours after
        # submission and once per member.
        config = _stochastic("banana")
        assert any(path == "ensemble.stochastic.sppt.timescale"
                   for path, _ in validate(config, schema))

    def test_a_real_duration_still_passes(self, schema):
        # The guard is a pattern, not a parser, so this is what says it did not
        # simply reject everything.
        assert validate(_stochastic("PT6H"), schema) == []


def _stochastic(timescale):
    config = _minimal()
    config["ensemble"] = {
        "size": 2,
        "stochastic": {
            "seed": 20150712,
            "sppt": {
                "amplitude": 0.35,
                "length_scale": 500000.0,
                "timescale": timescale,
            },
        },
    }
    return config


def _minimal():
    return {
        "experiment": {"name": "t"},
        "domain": {"name": "om_1deg"},
        "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H", "count": 1},
        "model": {"name": "mom6sis2"},
        # A free run, which is the only solver that needs nothing else stated.
        # `variational` deliberately does not belong here: it requires a B and a
        # minimizer, and `test_a_variational_solver_must_state_its_b_and_its_
        # minimizer` is what says so.
        "solver": {"name": "none"},
    }
