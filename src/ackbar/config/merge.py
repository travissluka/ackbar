"""Deep merge of configuration layers.

Layers are merged in order, later wins. Dicts recurse. Lists either replace
wholesale or merge element by element against a declared key, depending on
whether the schema annotates that list path with ``x-ackbar-merge-key``.

The keyed case exists for ``observations``: a ``da/letkf`` layer must be able to
change one observer's localization without restating all 25 of them. Lists with
no natural key (``variables``, saber blocks, filter chains) replace wholesale,
because merging them elementwise by position is never what anyone meant.
"""

from copy import deepcopy

#: Marker key on a keyed-list element that deletes the inherited element.
REMOVE = "$remove"


class MergeError(Exception):
    """A layer could not be merged. Carries the config path, and the layer name
    once the caller that knows it has annotated the exception."""

    def __init__(self, message, path=()):
        self.message = message
        self.path = tuple(path)
        self.layer = None
        super().__init__(message)

    def __str__(self):
        where = format_path(self.path) or "<root>"
        if self.layer:
            return f"{self.layer}: {where}: {self.message}"
        return f"{where}: {self.message}"


def format_path(path):
    """Render a merge path the way an error message should show it.

    Integers are list indices, so ``('observations', 3, 'obs space')`` renders
    as ``observations[3].obs space``.
    """
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out


def schema_path(path):
    """The path with list indices dropped.

    Merge keys are declared per *schema* location, not per element, so
    ``observations[3].obs filters`` is looked up as ``observations.obs filters``.
    """
    return ".".join(str(p) for p in path if not isinstance(p, int))


def get_dotted(node, dotted):
    """Fetch ``a.b.c`` out of nested dicts, or None if any hop is missing."""
    cur = node
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def merge(base, over, merge_keys=None, path=()):
    """Merge ``over`` onto ``base`` and return a new structure.

    Neither input is mutated. ``merge_keys`` maps a schema path such as
    ``observations`` to the dotted key identifying an element, such as
    ``obs space.name``.
    """
    merge_keys = merge_keys or {}

    if isinstance(base, dict) and isinstance(over, dict):
        out = deepcopy(base)
        for key, value in over.items():
            if key in out:
                out[key] = merge(out[key], value, merge_keys, path + (key,))
            else:
                out[key] = deepcopy(value)
        return out

    if isinstance(base, list) and isinstance(over, list):
        key = merge_keys.get(schema_path(path))
        if key:
            return _merge_keyed(base, over, key, merge_keys, path)
        return deepcopy(over)

    # Scalars, and any type change, replace. A type change is legal: a layer is
    # allowed to replace a dict with a scalar, and the schema is what decides
    # whether the result makes sense.
    return deepcopy(over)


def _merge_keyed(base, over, key, merge_keys, path):
    """Merge two lists elementwise, matching elements on a dotted key.

    Base order is preserved; elements the override introduces are appended in
    the order it gives them.
    """
    result = []
    index = {}

    for i, element in enumerate(base):
        identity = _identity(element, key, path + (i,), base_side=True)
        if identity in index:
            raise MergeError(
                f"duplicate {key!r} {identity!r} in the inherited list",
                path + (i,),
            )
        index[identity] = len(result)
        result.append(deepcopy(element))

    for i, element in enumerate(over):
        identity = _identity(element, key, path + (i,), base_side=False)
        removing = isinstance(element, dict) and element.get(REMOVE) is True

        if identity in index:
            at = index[identity]
            if removing:
                result[at] = None
            else:
                result[at] = merge(result[at], element, merge_keys, path + (at,))
        elif removing:
            raise MergeError(
                f"{REMOVE} names {key} {identity!r}, which is not in the inherited list",
                path + (i,),
            )
        else:
            index[identity] = len(result)
            result.append(deepcopy(element))

    return [_strip_remove(e) for e in result if e is not None]


def _identity(element, key, path, base_side):
    if not isinstance(element, dict):
        side = "inherited" if base_side else "overriding"
        raise MergeError(
            f"{side} element of a list keyed on {key!r} is "
            f"{type(element).__name__}, not a mapping",
            path,
        )
    identity = get_dotted(element, key)
    if identity is None:
        raise MergeError(
            f"element of a list keyed on {key!r} does not set it; "
            f"every element of this list must, so that layers can address it",
            path,
        )
    if not isinstance(identity, (str, int, bool)):
        raise MergeError(
            f"{key!r} is {type(identity).__name__}, which cannot identify an element",
            path,
        )
    return identity


def _strip_remove(element):
    if isinstance(element, dict) and REMOVE in element:
        element = {k: v for k, v in element.items() if k != REMOVE}
    return element
