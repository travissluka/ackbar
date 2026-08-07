"""Layer loading, inheritance, and the errors each failure should produce."""

import pytest

from ackbar.config.layers import (
    LayerError, merge_layers, resolve_layers,
)
from ackbar.config.merge import MergeError

KEYED = {"observations": "obs space.name"}


@pytest.fixture
def tree(tmp_path):
    """A small layer tree plus a place to write experiment files."""
    root = tmp_path / "layers"
    (root / "da").mkdir(parents=True)
    (root / "obs").mkdir(parents=True)
    (root / "da" / "letkf.yaml").write_text(
        "solver: {name: letkf}\nvars: {dist: Halo}\n"
    )
    (root / "da" / "variational.yaml").write_text(
        "solver: {name: variational}\nvars: {dist: RoundRobin}\n"
    )
    (root / "obs" / "adt.yaml").write_text(
        "observations:\n- obs space: {name: adt}\n  obs operator: {name: ADT}\n"
    )
    return root, tmp_path


def experiment(tmp_path, text, name="exp.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_layers_apply_in_declared_order_and_the_last_wins(tree):
    root, tmp = tree
    path = experiment(tmp, "inherit: [da/variational, da/letkf]\n")
    config = merge_layers(resolve_layers(path, root))
    assert config["vars"]["dist"] == "Halo"


def test_the_experiment_file_is_the_final_layer(tree):
    root, tmp = tree
    path = experiment(tmp, "inherit: [da/letkf]\nvars: {dist: Mine}\n")
    layers = resolve_layers(path, root)
    assert [l.name for l in layers] == ["da/letkf", "exp"]
    assert merge_layers(layers)["vars"]["dist"] == "Mine"


def test_inherit_is_not_itself_a_config_key(tree):
    root, tmp = tree
    path = experiment(tmp, "inherit: [da/letkf]\n")
    assert "inherit" not in merge_layers(resolve_layers(path, root))


def test_an_experiment_with_no_inherit_is_just_one_layer(tree):
    root, tmp = tree
    path = experiment(tmp, "vars: {dist: Only}\n")
    layers = resolve_layers(path, root)
    assert [l.name for l in layers] == ["exp"]


def test_a_missing_layer_names_the_path_it_looked_for(tree):
    root, tmp = tree
    path = experiment(tmp, "inherit: [da/enkf]\n")
    with pytest.raises(LayerError) as caught:
        resolve_layers(path, root)
    assert "da/enkf" in str(caught.value)
    assert "da/enkf.yaml" in str(caught.value)


def test_inheriting_the_same_layer_twice_is_an_error(tree):
    # Order decides precedence, so a repeat is always a mistake and is never
    # what the author meant.
    root, tmp = tree
    path = experiment(tmp, "inherit: [da/letkf, da/variational, da/letkf]\n")
    with pytest.raises(LayerError, match="inherited twice"):
        resolve_layers(path, root)


def test_a_layer_may_declare_its_own_inheritance(tree):
    """And what it inherits lands before it, so the layer still wins."""
    root, tmp = tree
    (root / "da" / "hybrid.yaml").write_text("inherit: [da/letkf]\nsolver: {name: x}\n")
    layers = resolve_layers(experiment(tmp, "inherit: [da/hybrid]\n"), root)
    assert [layer.name for layer in layers] == ["da/letkf", "da/hybrid", "exp"]
    assert merge_layers(layers)["solver"] == {"name": "x"}


def test_inherit_is_stripped_from_a_layer_as_well_as_an_experiment(tree):
    # It is ACKBAR's key at both scopes, and a layer leaking it into the merged
    # config would put a list of filenames where the schema expects config.
    root, tmp = tree
    (root / "da" / "hybrid.yaml").write_text("inherit: [da/letkf]\nsolver: {name: x}\n")
    config = merge_layers(resolve_layers(experiment(tmp, "inherit: [da/hybrid]\n"), root))
    assert "inherit" not in config


def test_a_layer_reached_twice_through_the_tree_is_loaded_once(tree):
    """Four altimeters inheriting one body is the case this exists for.

    Deduped rather than refused, unlike a repeat in an experiment's own list:
    there the repeat is hand-written and always a mistake, here it is the whole
    point, and the second arrival carries nothing the first did not.
    """
    root, tmp = tree
    for name in ("j2", "saral"):
        (root / "obs" / f"adt_{name}.yaml").write_text(
            f"inherit: [obs/adt]\nobservations:\n- obs space: {{name: adt_{name}}}\n"
        )
    layers = resolve_layers(experiment(tmp, "inherit: [obs/adt_j2, obs/adt_saral]\n"), root)
    assert [layer.name for layer in layers] == [
        "obs/adt", "obs/adt_j2", "obs/adt_saral", "exp",
    ]


def test_a_layer_lands_before_the_layer_that_inherits_it(tree):
    # Depth first and parents first, so the inheriting layer overrides what it
    # inherited rather than the other way round.
    root, tmp = tree
    (root / "obs" / "adt_j2.yaml").write_text(
        "inherit: [obs/adt]\n"
        "observations:\n- obs space: {name: adt}\n  obs operator: {name: Identity}\n"
    )
    config = merge_layers(
        resolve_layers(experiment(tmp, "inherit: [obs/adt_j2]\n"), root), KEYED
    )
    assert config["observations"][0]["obs operator"] == {"name": "Identity"}


def test_layers_that_inherit_in_a_cycle_are_refused(tree):
    root, tmp = tree
    (root / "da" / "a.yaml").write_text("inherit: [da/b]\n")
    (root / "da" / "b.yaml").write_text("inherit: [da/a]\n")
    with pytest.raises(LayerError, match="cycle"):
        resolve_layers(experiment(tmp, "inherit: [da/a]\n"), root)


def test_a_layer_missing_from_a_parents_list_names_who_wanted_it(tree):
    # The experiment does not mention `obs/nope`, so without this the error
    # names a file nothing in front of the reader asked for.
    root, tmp = tree
    (root / "obs" / "adt_j2.yaml").write_text("inherit: [obs/nope]\n")
    with pytest.raises(LayerError, match="inherited by 'obs/adt_j2'"):
        resolve_layers(experiment(tmp, "inherit: [obs/adt_j2]\n"), root)


def test_inherit_must_be_a_list(tree):
    root, tmp = tree
    path = experiment(tmp, "inherit: da/letkf\n")
    with pytest.raises(LayerError, match="must be a list"):
        resolve_layers(path, root)


def test_a_layer_that_is_not_a_mapping_is_an_error(tree):
    root, tmp = tree
    (root / "da" / "bad.yaml").write_text("- just\n- a list\n")
    with pytest.raises(LayerError, match="must be a mapping"):
        resolve_layers(experiment(tmp, "inherit: [da/bad]\n"), root)


def test_an_empty_layer_file_is_legal(tree):
    root, tmp = tree
    (root / "da" / "empty.yaml").write_text("# nothing yet\n")
    config = merge_layers(resolve_layers(experiment(tmp, "inherit: [da/empty]\n"), root))
    assert config == {}


def test_a_merge_failure_names_the_layer_that_caused_it(tree):
    root, tmp = tree
    (root / "obs" / "broken.yaml").write_text(
        "observations:\n- obs operator: {name: Identity}\n"
    )
    path = experiment(tmp, "inherit: [obs/adt, obs/broken]\n")
    with pytest.raises(MergeError) as caught:
        merge_layers(resolve_layers(path, root), KEYED)
    assert caught.value.layer == "obs/broken"
    assert "obs/broken" in str(caught.value)
    assert "observations[0]" in str(caught.value)
