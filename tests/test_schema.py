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

    def test_an_experiment_name_that_would_not_survive_sacct_is_rejected(self, schema):
        config = _minimal()
        config["experiment"]["name"] = "my experiment"
        assert any(path == "experiment.name" for path, _ in validate(config, schema))


def _minimal():
    return {
        "experiment": {"name": "t"},
        "domain": {"name": "om_1deg"},
        "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H", "count": 1},
        "model": {"name": "mom6sis2"},
        "solver": {"name": "variational"},
    }
