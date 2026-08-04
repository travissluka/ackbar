"""Answering "why is this value that", by replaying the merge.

The ordered layer files are copied verbatim into the experiment directory, and
the merge is replayed with the layer list truncated at each level. The last
truncation that changed the value is the layer responsible.

This gives the same answer as threading an origin through every scalar, on plain
dicts, with nothing carried in the hot path. Neither prior workflow can answer
the question at all.
"""

import re

from .layers import merge_layers

#: Distinct from None, which is a legal config value.
MISSING = type("Missing", (), {"__repr__": lambda self: "<unset>"})()


def parse_path(dotted):
    """``a.b[3].c`` becomes ``['a', 'b', 3, 'c']``.

    Keys may contain spaces, because JEDI's do (``obs space``).
    """
    parts = []
    for chunk in dotted.split("."):
        match = re.fullmatch(r"([^\[\]]*)((?:\[\d+\])*)", chunk)
        if not match:
            raise ValueError(f"cannot parse config path: {dotted!r}")
        name, indices = match.groups()
        if name:
            parts.append(name)
        parts.extend(int(i) for i in re.findall(r"\[(\d+)\]", indices))
    if not parts:
        raise ValueError(f"empty config path: {dotted!r}")
    return parts


def lookup(config, parts):
    """Fetch a parsed path out of a config, or MISSING."""
    cur = config
    for part in parts:
        if isinstance(part, int):
            if not isinstance(cur, list) or part >= len(cur):
                return MISSING
            cur = cur[part]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return MISSING
            cur = cur[part]
    return cur


def why(layers, dotted, merge_keys=None):
    """Replay the merge, returning [(layer_name, value)] for every layer that
    changed the value at ``dotted``, in order. The last entry is the winner.
    """
    parts = parse_path(dotted)
    history = []
    previous = MISSING
    for n in range(1, len(layers) + 1):
        value = lookup(merge_layers(layers[:n], merge_keys), parts)
        if not _same(value, previous):
            history.append((layers[n - 1].name, value))
            previous = value
    return history


def responsible_layer(layers, dotted, merge_keys=None):
    """Just the name of the layer that last set a value, or None."""
    history = why(layers, dotted, merge_keys)
    return history[-1][0] if history else None


def _same(a, b):
    if a is MISSING or b is MISSING:
        return a is b
    return a == b
