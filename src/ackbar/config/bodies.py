"""Shared observer bodies, and the pass that expands them.

`observations` merges keyed on `obs space.name`, which is what lets a `da/letkf`
layer change one observer's localization without restating the other
twenty-four. It also means a layer reaches an observer only by naming it, so a
layer holding the *common* half of a platform, with no platform of its own, has
nothing to merge into. That is the gap this closes.

A family layer declares one body under a name:

.. code-block:: yaml

    observation bodies:
      adt:
        obs operator: {name: ADT}
        obs error: {covariance model: diagonal}
        obs filters: [...]

and a platform layer states what is its own and points at it:

.. code-block:: yaml

    inherit: [obs/adt]
    observations:
    - obs space:
        name: adt_j2
        obsdatain: {engine: {type: H5File, obsfile: .../adt_j2....nc4}}
      $inherit: adt

The same word at two scopes, deliberately: the layer inherits the file, the
observer inherits the body out of it. One is a list of layer names at the top of
a document ACKBAR owns outright, the other is one body name inside an observer
that is otherwise verbatim UFO configuration, and that is what the sigil is for.
`$remove` wears it for the same reason.

The body merges *under* the element, so anything the platform states wins, and
`$inherit` is gone by the time anything downstream sees the observer. What is left
is a complete observer, identical to the one the platform layer would have
carried if it had been typed out, which is the property that made this
replaceable at all: the expansion changes where the text lives and nothing else.

Expansion runs after the merge and before substitution, in that order and for
the same reason the merge runs before substitution: a later layer has to be able
to override a value the body supplied, and it can only do that while the two are
still separate documents.

`config.layers.merge_layers` calls this, so nothing else has to. That is not
where it started, and the reason it moved is worth keeping: as a step callers
performed themselves it was already missing from three of five call sites within
a day, and the symptom of missing it is an observer that reaches UFO with no
operator and no error model.
"""

from .merge import merge

#: Where a layer declares its bodies, and what an observer calls one.
#:
#: `$inherit` carries the sigil `merge.REMOVE` established, and for the same
#: reason: an observer is verbatim UFO configuration apart from the two keys
#: ACKBAR reads out of it, and a bare `inherit:` sitting among `obs operator`
#: and `obs filters` reads like something SOCA is going to act on. The sigil
#: says it is not. `observation bodies` needs no sigil, being a root key in a
#: document whose root keys are all ACKBAR's.
BODIES = "observation bodies"
BODY = "$inherit"


class BodyError(Exception):
    def __init__(self, message, path=""):
        self.message = message
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


def expand(config, merge_keys=None, strict=True):
    """Return the config with every observer's inherited body merged in.

    ``strict=False`` leaves an observer whose body is not declared alone instead
    of refusing it. That is for `config.why`, which replays the merge over
    truncated layer lists and so legitimately sees an observer before the layer
    that declares its body has been reached.
    """
    observers = config.get("observations")
    if not isinstance(observers, list):
        return config

    bodies = config.get(BODIES) or {}
    out = dict(config)
    out["observations"] = [
        _one(entry, bodies, merge_keys, index, strict)
        for index, entry in enumerate(observers)
    ]
    return out


def _one(entry, bodies, merge_keys, index, strict):
    if not isinstance(entry, dict) or BODY not in entry:
        return entry

    name = entry[BODY]
    where = f"observations[{index}].{BODY}"
    if not isinstance(name, str):
        # A list is the plausible mistake, `inherit:` being a list one line up in
        # the same file. One body, because two would need an order between them
        # and no platform has ever wanted a second.
        raise BodyError(
            f"{name!r} is not a body name; an observer inherits one body, "
            f"by name, unlike a layer's {BODY!r}, which is a list",
            where,
        )

    if name not in bodies:
        if not strict:
            return entry
        known = ", ".join(sorted(bodies)) or "none"
        raise BodyError(
            f"no observer body named {name!r} is declared. A body comes from a "
            f"layer this one inherits, under {BODIES!r}; declared here: {known}",
            where,
        )

    body = bodies[name]
    if not isinstance(body, dict):
        raise BodyError(
            f"body {name!r} is a {type(body).__name__}, and only a mapping can "
            f"merge into an observer",
            f"{BODIES}.{name}",
        )

    rest = {k: v for k, v in entry.items() if k != BODY}
    # `rest` over `body`: the platform layer is the more specific statement, and
    # a platform that restates something the body also sets means to change it.
    return merge(body, rest, merge_keys, ("observations", index))
