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


class TestRemovingADictKey:
    """The only way a layer can make an inherited key *absent*.

    `da/eakf` is what this is for. It inherits `ensemble distribution` as
    `{name: Halo, halo size: 500000}` and needs a round robin decomposition with
    no halo at all; dicts merge, so naming the distribution alone leaves the
    halo size behind, and the application is then handed a document describing a
    decomposition nothing is using.
    """

    def test_the_key_is_gone_rather_than_null(self):
        # Absent, not None. The document is read by eckit, where a null is a
        # value and not the absence of one.
        base = {"d": {"name": "Halo", "halo size": 500000}}
        over = {"d": {"name": "RoundRobin", "halo size": "$remove"}}
        assert merge(base, over) == {"d": {"name": "RoundRobin"}}

    def test_it_removes_a_whole_subtree_not_only_a_scalar(self):
        base = {"error": {"model": "SABER", "localization": {"method": "Rossby"}}}
        over = {"error": {"localization": "$remove"}}
        assert merge(base, over) == {"error": {"model": "SABER"}}

    def test_removing_something_that_is_not_inherited_is_an_error(self):
        # Same rule as the keyed-list form, and for the same reason: a removal
        # that names nothing is a typo or a leftover from a base layer that has
        # already dropped the key, and doing nothing quietly is how a layer ends
        # up not doing what it says.
        with pytest.raises(MergeError) as caught:
            merge({"d": {"name": "Halo"}}, {"d": {"halo sze": "$remove"}})
        assert "halo sze" in str(caught.value)
        assert caught.value.path == ("d", "halo sze")

    def test_a_key_whose_value_merely_looks_like_the_marker_is_untouched(self):
        # The marker is the exact string. Nothing else is a removal, including a
        # dict that happens to contain the word.
        base = {"a": {"x": 1}}
        over = {"a": {"x": "$removed", "y": {"$remove": True}}}
        assert merge(base, over) == \
            {"a": {"x": "$removed", "y": {"$remove": True}}}


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

    def test_duplicate_key_in_the_overriding_layer_is_an_error_too(self):
        """The asymmetry that used to make a typo silently drop an observer.

        The second element with an identity already used merged onto the first,
        so naming one observer twice folded two blocks into one and left the
        observer meant to get the second block with nothing. No error from the
        merge, and none from the schema either: there is no uniqueness
        constraint on the key.
        """
        base = {"observations": [
            {"obs space": {"name": "adt"}},
            {"obs space": {"name": "sst"}},
        ]}
        over = {"observations": [
            {"obs space": {"name": "adt"}, "obs localizations": [{"a": 1}]},
            {"obs space": {"name": "adt"}, "obs localizations": [{"b": 2}]},
        ]}
        with pytest.raises(MergeError) as caught:
            merge(base, over, KEYED)
        assert "duplicate" in str(caught.value)
        assert caught.value.path == ("observations", 1)

    def test_a_removal_marker_that_is_not_a_boolean_is_refused(self):
        # `$remove: 'true'` merged normally and then had its marker stripped, so
        # the output carried no trace of a line meant to delete something.
        base = {"observations": [{"obs space": {"name": "adt"}}]}
        over = {"observations": [{"obs space": {"name": "adt"}, "$remove": "true"}]}
        with pytest.raises(MergeError, match="only a boolean removes"):
            merge(base, over, KEYED)

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
