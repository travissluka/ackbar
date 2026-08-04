"""Path parsing and the merge replay that answers "why is this value that"."""

import pytest

from ackbar.config.layers import Layer
from ackbar.config.why import MISSING, lookup, parse_path, why

KEYED = {"observations": "obs space.name"}


def layer(name, data):
    return Layer(name, f"{name}.yaml", data)


class TestParsePath:
    def test_plain_dotted_path(self):
        assert parse_path("a.b.c") == ["a", "b", "c"]

    def test_list_index(self):
        assert parse_path("observations[3]") == ["observations", 3]

    def test_index_in_the_middle(self):
        assert parse_path("observations[3].obs space.name") == [
            "observations", 3, "obs space", "name",
        ]

    def test_keys_may_contain_spaces_because_jedi_keys_do(self):
        assert parse_path("obs error.covariance model") == [
            "obs error", "covariance model",
        ]

    def test_consecutive_indices(self):
        assert parse_path("a[1][2]") == ["a", 1, 2]

    def test_empty_path_is_rejected(self):
        with pytest.raises(ValueError):
            parse_path("")


class TestLookup:
    config = {"a": {"b": [{"c": 1}, {"c": 2}]}, "null": None}

    def test_finds_a_nested_value(self):
        assert lookup(self.config, parse_path("a.b[1].c")) == 2

    def test_missing_key_is_MISSING(self):
        assert lookup(self.config, parse_path("a.z")) is MISSING

    def test_index_past_the_end_is_MISSING(self):
        assert lookup(self.config, parse_path("a.b[9]")) is MISSING

    def test_descending_into_a_scalar_is_MISSING(self):
        assert lookup(self.config, parse_path("a.b[0].c.deeper")) is MISSING

    def test_an_explicit_null_is_a_value_not_MISSING(self):
        # None is a legal config value, so it must be distinguishable from
        # unset. Getting this wrong makes `why` claim a layer set nothing.
        assert lookup(self.config, parse_path("null")) is None


class TestWhy:
    def test_reports_only_layers_that_changed_the_value(self):
        layers = [
            layer("base", {"x": 1}),
            layer("noop", {"y": 2}),
            layer("change", {"x": 3}),
            layer("same", {"x": 3}),
        ]
        assert why(layers, "x") == [("base", 1), ("change", 3)]

    def test_the_last_entry_is_the_winner(self):
        layers = [layer("a", {"x": 1}), layer("b", {"x": 2})]
        assert why(layers, "x")[-1] == ("b", 2)

    def test_a_value_nobody_sets_has_no_history(self):
        assert why([layer("a", {"x": 1})], "z") == []

    def test_a_layer_setting_null_is_reported(self):
        layers = [layer("a", {"x": 1}), layer("b", {"x": None})]
        assert why(layers, "x") == [("a", 1), ("b", None)]

    def test_works_through_a_keyed_list(self):
        layers = [
            layer("obs", {"observations": [{"obs space": {"name": "adt"},
                                            "error": 0.1}]}),
            layer("tune", {"observations": [{"obs space": {"name": "adt"},
                                             "error": 0.2}]}),
        ]
        history = why(layers, "observations[0].error", KEYED)
        assert history == [("obs", 0.1), ("tune", 0.2)]

    def test_a_structural_change_counts_as_a_change(self):
        layers = [
            layer("a", {"d": {"x": 1}}),
            layer("b", {"d": {"y": 2}}),
        ]
        assert why(layers, "d") == [("a", {"x": 1}), ("b", {"x": 1, "y": 2})]
