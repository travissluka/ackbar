"""Merge rules, in isolation.

One case per rule, on the smallest structure that shows it. The rules meet real
configs in test_port.py.
"""

import pytest

from ackbar.config.merge import MergeError, merge

KEYED = {"observations": "obs space.name"}


def test_dicts_merge_recursively_and_later_wins():
    base = {"a": 1, "nest": {"x": 1, "y": 2}}
    over = {"nest": {"y": 99, "z": 3}}
    assert merge(base, over) == {"a": 1, "nest": {"x": 1, "y": 99, "z": 3}}


def test_inputs_are_not_mutated():
    base = {"nest": {"x": 1}, "list": [1, 2]}
    over = {"nest": {"x": 2}}
    merge(base, over)
    assert base == {"nest": {"x": 1}, "list": [1, 2]}


def test_unkeyed_lists_replace_wholesale():
    # Filter chains and variable lists have no natural key. Merging them by
    # position is never what anyone meant.
    base = {"obs filters": [{"filter": "A"}, {"filter": "B"}]}
    over = {"obs filters": [{"filter": "C"}]}
    assert merge(base, over) == {"obs filters": [{"filter": "C"}]}


def test_scalars_replace_including_across_types():
    assert merge({"a": {"deep": 1}}, {"a": 5}) == {"a": 5}
    assert merge({"a": 5}, {"a": None}) == {"a": None}


class TestKeyedLists:
    def test_matching_element_merges_rather_than_replacing(self):
        base = {"observations": [
            {"obs space": {"name": "adt"}, "obs error": {"model": "diagonal"}},
            {"obs space": {"name": "sst"}, "obs error": {"model": "diagonal"}},
        ]}
        over = {"observations": [
            {"obs space": {"name": "adt"}, "obs localizations": ["rossby"]},
        ]}
        result = merge(base, over, KEYED)["observations"]

        assert [o["obs space"]["name"] for o in result] == ["adt", "sst"]
        # adt gained localization and kept everything it had
        assert result[0]["obs localizations"] == ["rossby"]
        assert result[0]["obs error"] == {"model": "diagonal"}
        # sst is untouched
        assert "obs localizations" not in result[1]

    def test_unmatched_element_is_appended_in_override_order(self):
        base = {"observations": [{"obs space": {"name": "adt"}}]}
        over = {"observations": [
            {"obs space": {"name": "sss"}},
            {"obs space": {"name": "icec"}},
        ]}
        result = merge(base, over, KEYED)["observations"]
        assert [o["obs space"]["name"] for o in result] == ["adt", "sss", "icec"]

    def test_removal_marker_deletes_an_inherited_element(self):
        base = {"observations": [
            {"obs space": {"name": "adt"}},
            {"obs space": {"name": "sst"}},
        ]}
        over = {"observations": [{"obs space": {"name": "adt"}, "$remove": True}]}
        result = merge(base, over, KEYED)["observations"]
        assert result == [{"obs space": {"name": "sst"}}]

    def test_removal_marker_never_survives_into_the_output(self):
        base = {"observations": [{"obs space": {"name": "adt"}}]}
        over = {"observations": [
            {"obs space": {"name": "adt"}, "$remove": False, "x": 1},
        ]}
        result = merge(base, over, KEYED)["observations"]
        assert result == [{"obs space": {"name": "adt"}, "x": 1}]

    def test_removing_something_that_is_not_there_is_an_error(self):
        # Almost always a typo in the observer name, and silently doing nothing
        # is how you run 30 cycles against the observer set you did not mean.
        base = {"observations": [{"obs space": {"name": "adt"}}]}
        over = {"observations": [{"obs space": {"name": "atd"}, "$remove": True}]}
        with pytest.raises(MergeError) as caught:
            merge(base, over, KEYED)
        assert "atd" in str(caught.value)

    def test_element_missing_the_key_is_an_error(self):
        base = {"observations": [{"obs space": {"name": "adt"}}]}
        over = {"observations": [{"obs operator": {"name": "Identity"}}]}
        with pytest.raises(MergeError) as caught:
            merge(base, over, KEYED)
        assert "obs space.name" in str(caught.value)
        assert caught.value.path == ("observations", 0)

    def test_duplicate_key_in_one_layer_is_an_error(self):
        base = {"observations": [
            {"obs space": {"name": "adt"}},
            {"obs space": {"name": "adt"}},
        ]}
        with pytest.raises(MergeError) as caught:
            merge(base, {"observations": []}, KEYED)
        assert "duplicate" in str(caught.value)

    def test_a_nested_list_inside_a_keyed_element_still_replaces(self):
        # observations is keyed; obs filters inside it is not. The schema path
        # of the inner list is observations.obs filters, with no index.
        base = {"observations": [
            {"obs space": {"name": "adt"}, "obs filters": [{"filter": "A"},
                                                           {"filter": "B"}]},
        ]}
        over = {"observations": [
            {"obs space": {"name": "adt"}, "obs filters": [{"filter": "C"}]},
        ]}
        result = merge(base, over, KEYED)["observations"]
        assert result[0]["obs filters"] == [{"filter": "C"}]

    def test_without_a_declared_key_the_same_list_replaces(self):
        base = {"observations": [{"obs space": {"name": "adt"}},
                                 {"obs space": {"name": "sst"}}]}
        over = {"observations": [{"obs space": {"name": "adt"}}]}
        assert len(merge(base, over, {})["observations"]) == 1
