"""Deep merge of configuration layers.

Layers are merged in order, later wins. Dicts recurse. Lists either replace
wholesale or merge element by element against a declared key, depending on
whether the schema annotates that list path with ``x-ackbar-merge-key``.

The keyed case exists for ``observations``: a layer must be able to reach one
observer without restating the other twenty-four. (Setting an observer's
localization from a solver layer is the anti-pattern, not the example:
``da/letkf.yaml`` says why.) Lists with no natural
key (``variables``, saber blocks, filter chains) replace wholesale, because
merging them elementwise by position is never what anyone meant.
"""

from copy import deepcopy

#: Deletes what a layer inherited. Two forms, because there are two things to
#: delete: as a key on a keyed-list element (``$remove: true`` beside the
#: element's identity) it drops that element, and as a *value* on any dict key
#: (``halo size: $remove``) it drops that key. Both refuse to name something the
#: layer did not inherit.
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
            if value == REMOVE:
                # The only way a layer can make a key *absent*. Dicts merge, so
                # a layer that inherits `{name: Halo, halo size: 500000}` and
                # writes `{name: RoundRobin}` gets a round robin distribution
                # that still carries a halo size: a key the application will
                # ignore, in a document that now describes a decomposition
                # nothing is using. See `da/eakf.yaml`, which is what this is
                # for.
                #
                # Absent rather than null, because the emitted document is read
                # by eckit and a null is a value. Refused when the key is not
                # inherited, matching the keyed-list form above: a removal that
                # names nothing is either a typo or a leftover from a base layer
                # that has already dropped the key, and both are worth saying.
                if key not in out:
                    raise MergeError(
                        f"{REMOVE} names {key!r}, which is not inherited here",
                        path + (key,),
                    )
                del out[key]
            elif key in out:
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

    seen = set()
    for i, element in enumerate(over):
        identity = _identity(element, key, path + (i,), base_side=False)
        # Checked on this side too. Without it the second element with an
        # identity already used merges onto the first, so writing one observer's
        # name twice by mistake silently folds two blocks into one and leaves
        # the observer that was meant to get the second block with nothing. That
        # is the class of typo the strict schema exists to catch, and there is
        # no uniqueness constraint on the key to catch it later. The `$remove`
        # form is worse: adding an element and removing it in the same list
        # deletes what it just added.
        if identity in seen:
            raise MergeError(
                f"duplicate {key!r} {identity!r} in the overriding list",
                path + (i,),
            )
        seen.add(identity)

        removing = _removing(element, key, path + (i,))

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


def _removing(element, key, path):
    """Whether this element is a removal, refusing a marker that is not a bool.

    `false` is allowed and means what it says.
    """
    if not isinstance(element, dict) or REMOVE not in element:
        return False
    value = element[REMOVE]
    if not isinstance(value, bool):
        raise MergeError(
            f"{REMOVE} is {value!r}, and only a boolean removes. Anything else "
            f"here reads as a removal and does nothing",
            path,
        )
    return value


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
