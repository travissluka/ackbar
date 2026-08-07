"""Shared observer bodies: what expands, what wins, and what is refused.

The mechanism exists because `observations` merges keyed on `obs space.name`, so
a layer holding the common half of a platform family has no element to merge
into. These pin the properties that make it a safe substitute for having typed
the body out in each platform layer: the platform wins, the key does not survive,
and a body that is not there is an error rather than a silently thin observer.
"""

import pytest

from ackbar.config.bodies import BodyError, expand
from ackbar.config.layers import merge_layers, resolve_layers

KEYED = {"observations": "obs space.name"}

BODIES = {
    "adt": {
        "obs operator": {"name": "ADT"},
        "obs error": {"covariance model": "diagonal"},
        "obs filters": [{"filter": "Background Check", "absolute threshold": 10.0}],
    }
}


def config(*observers, bodies=BODIES):
    return {"observation bodies": bodies, "observations": list(observers)}


def test_a_body_fills_in_what_the_platform_does_not_state():
    out = expand(config({"obs space": {"name": "adt_j2"}, "$inherit": "adt"}), KEYED)
    observer = out["observations"][0]
    assert observer["obs operator"] == {"name": "ADT"}
    assert observer["obs space"] == {"name": "adt_j2"}


def test_the_key_does_not_survive_expansion():
    # It is ACKBAR's, not UFO's, and UFO rejects keys it does not know.
    out = expand(config({"obs space": {"name": "adt_j2"}, "$inherit": "adt"}), KEYED)
    assert "$inherit" not in out["observations"][0]


def test_the_platform_overrides_the_body():
    """A platform that restates something the body sets means to change it."""
    out = expand(config({
        "obs space": {"name": "adt_j2"},
        "$inherit": "adt",
        "obs operator": {"name": "Identity"},
    }), KEYED)
    assert out["observations"][0]["obs operator"] == {"name": "Identity"}


def test_an_override_merges_rather_than_replaces_the_dict():
    out = expand(config({
        "obs space": {"name": "adt_j2"},
        "$inherit": "adt",
        "obs error": {"random amplitude": 0.5},
    }), KEYED)
    assert out["observations"][0]["obs error"] == {
        "covariance model": "diagonal", "random amplitude": 0.5,
    }


def test_a_filter_chain_replaces_wholesale():
    # Filter chains carry no merge key, so a platform that wants a different
    # chain states the whole chain. Pinned because the alternative, merging
    # elementwise by position, silently produces a chain nobody wrote.
    out = expand(config({
        "obs space": {"name": "adt_j2"},
        "$inherit": "adt",
        "obs filters": [{"filter": "Domain Check"}],
    }), KEYED)
    assert out["observations"][0]["obs filters"] == [{"filter": "Domain Check"}]


def test_an_observer_with_no_body_is_untouched():
    entry = {"obs space": {"name": "adt_3a"}, "obs operator": {"name": "ADT"}}
    assert expand(config(entry), KEYED)["observations"][0] == entry


def test_an_undeclared_body_is_refused_and_lists_what_is_declared():
    with pytest.raises(BodyError) as caught:
        expand(config({"obs space": {"name": "x"}, "$inherit": "sst"}), KEYED)
    assert "sst" in str(caught.value)
    assert "declared here: adt" in str(caught.value)


def test_an_undeclared_body_is_tolerated_when_not_strict():
    # `config.why` replays the merge over truncated layer lists, so it sees an
    # observer before the layer declaring its body has been reached. Refusing
    # there would break the tool on exactly the configs it explains.
    entry = {"obs space": {"name": "x"}, "$inherit": "sst"}
    assert expand(config(entry), KEYED, strict=False)["observations"][0] == entry


def test_a_list_of_bodies_is_refused_with_the_distinction_spelled_out():
    # `inherit:` one line up in the same file is a list, so this is the mistake
    # the shared word invites.
    with pytest.raises(BodyError, match="one body"):
        expand(config({"obs space": {"name": "x"}, "$inherit": ["adt"]}), KEYED)


def test_a_config_with_no_observations_passes_through():
    assert expand({"solver": {"name": "none"}}, KEYED) == {"solver": {"name": "none"}}


def test_the_end_to_end_shape_matches_a_layer_that_states_everything(tmp_path):
    """The property the whole change rests on.

    A platform layer that inherits a body and one that carries the same text
    must produce the same observer, or the refactor that moved six of them onto
    shared bodies changed the science.
    """
    root = tmp_path / "layers"
    (root / "obs").mkdir(parents=True)
    (root / "obs" / "adt.yaml").write_text(
        "observation bodies:\n"
        "  adt:\n"
        "    obs operator: {name: ADT}\n"
        "    obs error: {covariance model: diagonal}\n"
    )
    (root / "obs" / "adt_j2.yaml").write_text(
        "inherit: [obs/adt]\n"
        "observations:\n"
        "- obs space: {name: adt_j2, simulated variables: [absoluteDynamicTopography]}\n"
        "  $inherit: adt\n"
    )
    (root / "obs" / "spelled_out.yaml").write_text(
        "observations:\n"
        "- obs space: {name: adt_j2, simulated variables: [absoluteDynamicTopography]}\n"
        "  obs operator: {name: ADT}\n"
        "  obs error: {covariance model: diagonal}\n"
    )

    def resolved(inherit):
        # No `expand` call: `merge_layers` does it, which is the other half of
        # what this asserts. A caller that has to remember an extra step is a
        # caller that will forget it.
        path = tmp_path / f"{inherit.replace('/', '_')}.yaml"
        path.write_text(f"inherit: [{inherit}]\n")
        return merge_layers(resolve_layers(path, root), KEYED)

    inherited = resolved("obs/adt_j2")
    literal = resolved("obs/spelled_out")
    assert inherited["observations"] == literal["observations"]
