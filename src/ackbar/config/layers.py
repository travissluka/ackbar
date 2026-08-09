"""Loading configuration layers and merging them in declared order.

An experiment file names the layers it is built from, and is itself the last
layer:

.. code-block:: yaml

    inherit:
      - domain/gom_25km
      - model/mom6sis2
      - da/variational
      - obs/adt_j2
      - obs/sst_metopb
    experiment:
      name: gom-3dvar

The four kinds are the four directories under `config/layers/`: `domain`,
`model`, `da`, `obs`. Inheritance is declared in one place and read top to
bottom. v3 layered implicitly, through nearest-enclosing-dict scoping during
token resolution, which is impossible to reason about from the experiment file
alone.

A layer may inherit too, and it means something narrower than an experiment's.
It is how a layer says *what it is a kind of*, not how a stack is assembled:
`obs/adt_j2` inherits `obs/common/adt` because Jason-2 is an altimeter, and the
experiment still lists the platforms it flies. The constraint it bends is that
the whole stack should be readable from the experiment file, and it bends it for
one case only, a family of platforms that differ by name. The flattening is
depth first and a layer lands before anything that inherits it, so the flattened
list is the order things contributed in; `create` records it by name in
`provenance.json`.

Repeats are treated differently on the two sides, which looks inconsistent and
is not. An experiment naming a layer twice is refused, because order decides
precedence and a hand-written repeat is always a mistake. A layer reached twice
through the tree is deduped in silence, because four platform layers all
inheriting `obs/adt` is not a mistake, it is the whole point, and the second
arrival carries nothing the first did not.
"""

from pathlib import Path

import yaml

from .bodies import expand as expand_bodies
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
    loaded = set()
    declared = set()
    for name in names:
        if name in declared:
            raise LayerError(
                f"{experiment_path}: layer {name!r} is inherited twice; "
                f"order decides precedence, so a repeat is always a mistake"
            )
        declared.add(name)
        _load_layer(name, search_root, layers, loaded, ())

    own = {k: v for k, v in data.items() if k != INHERIT_KEY}
    layers.append(Layer(experiment_path.stem, experiment_path, own))
    return layers


def _load_layer(name, search_root, out, loaded, visiting):
    """Append ``name`` and everything it inherits to ``out``, parents first.

    ``loaded`` is every layer already placed, across the whole tree: a layer
    reached a second time is skipped rather than appended again, so `obs/adt`
    lands once no matter how many altimeters ask for it. ``visiting`` is the
    current chain, and is what tells a diamond from a cycle: the first is
    ordinary and the second is a file that cannot be loaded at all.
    """
    if name in loaded:
        return
    if name in visiting:
        chain = " -> ".join(visiting + (name,))
        raise LayerError(f"layers inherit in a cycle: {chain}")

    path = search_root / f"{name}.yaml"
    if not path.is_file():
        where = f" (inherited by {visiting[-1]!r})" if visiting else ""
        raise LayerError(f"no such layer: {name!r}{where} (looked for {path})")
    data = load_yaml(path)

    parents = data.get(INHERIT_KEY) or []
    if not isinstance(parents, list):
        raise LayerError(f"{path}: {INHERIT_KEY!r} must be a list of layer names")
    for parent in parents:
        _load_layer(parent, search_root, out, loaded, visiting + (name,))

    loaded.add(name)
    out.append(Layer(name, path, {k: v for k, v in data.items() if k != INHERIT_KEY}))


def merge_layers(layers, merge_keys=None, *, strict=True):
    """Merge layers in order and return the resolved config.

    A merge failure is annotated with the layer that caused it, because "which
    file do I edit" is the only question that matters at that moment.

    Observer bodies are expanded here, at the end, rather than by the callers.
    They were a separate step for one afternoon and it was already wrong: every
    caller that merges layers wants a config whose observers are whole, and the
    two that remembered to expand were the two the author happened to be looking
    at. `config.bodies.expand` is a no-op on a config with no `observations`, so
    the cost of doing it here is nothing and the cost of leaving it to callers is
    an observer that silently reaches UFO with no operator.

    `strict=False` tolerates an observer whose body is not declared, which only
    `config.why` wants: it replays this over deliberately truncated layer lists,
    where a platform legitimately arrives before the family layer it points at.
    """
    config = {}
    for layer in layers:
        try:
            config = merge(config, layer.data, merge_keys)
        except MergeError as error:
            error.layer = layer.name
            raise
    return expand_bodies(config, merge_keys, strict=strict)
