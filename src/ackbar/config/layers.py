"""Loading configuration layers and merging them in declared order.

An experiment file names the layers it is built from, and is itself the last
layer:

.. code-block:: yaml

    inherit:
      - model/mom6sis2_om_1deg
      - da/variational
      - covariance/hybrid
      - window/3d
      - obs/osse
    ens_size: 20

Inheritance is declared in one place and read top to bottom. v3 layered
implicitly, through nearest-enclosing-dict scoping during token resolution,
which is impossible to reason about from the experiment file alone.
"""

from pathlib import Path

import yaml

from .merge import MergeError, merge

INHERIT_KEY = "inherit"


class LayerError(Exception):
    pass


class Layer:
    """One named configuration layer and the file it came from."""

    def __init__(self, name, path, data):
        self.name = name
        self.path = Path(path)
        self.data = data

    def __repr__(self):
        return f"Layer({self.name!r})"


def load_yaml(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise LayerError(f"{path}: a layer must be a mapping, got {type(data).__name__}")
    return data


def resolve_layers(experiment_path, search_root):
    """Return the ordered layers for an experiment, experiment file last."""
    experiment_path = Path(experiment_path)
    search_root = Path(search_root)

    data = load_yaml(experiment_path)
    names = data.get(INHERIT_KEY) or []
    if not isinstance(names, list):
        raise LayerError(
            f"{experiment_path}: {INHERIT_KEY!r} must be a list of layer names"
        )

    layers = []
    seen = set()
    for name in names:
        if name in seen:
            raise LayerError(
                f"{experiment_path}: layer {name!r} is inherited twice; "
                f"order decides precedence, so a repeat is always a mistake"
            )
        seen.add(name)
        layers.append(_load_layer(name, search_root))

    own = {k: v for k, v in data.items() if k != INHERIT_KEY}
    layers.append(Layer(experiment_path.stem, experiment_path, own))
    return layers


def _load_layer(name, search_root):
    path = search_root / f"{name}.yaml"
    if not path.is_file():
        raise LayerError(f"no such layer: {name!r} (looked for {path})")
    data = load_yaml(path)
    if INHERIT_KEY in data:
        raise LayerError(
            f"{path}: layers may not inherit; only the experiment file declares "
            f"its layer list, so that the whole stack is readable in one place"
        )
    return Layer(name, path, data)


def merge_layers(layers, merge_keys=None):
    """Merge layers in order and return the resolved config.

    A merge failure is annotated with the layer that caused it, because "which
    file do I edit" is the only question that matters at that moment.
    """
    config = {}
    for layer in layers:
        try:
            config = merge(config, layer.data, merge_keys)
        except MergeError as error:
            error.layer = layer.name
            raise
    return config
