"""The third substitution pass: slots a task fills in.

Three passes now, and each one is a different syntax because each one is filled
by a different thing at a different time.

``$(name)``
    Experiment time, lowercase. Resolved once by `resolve.py` when the
    experiment is created, from the layer stack's ``vars`` and a closed set of
    built-ins, and frozen.

``{{name}}``
    Job time, lowercase. Cycle date, window bounds, member index. Resolved by
    the running job from `jobtime.py`'s closed table.

``$(NAME)``
    Task time, uppercase. Resolved by *one particular function* from values only
    that function can compute: the observer list `stage.obs` realized, the
    member states that exist on disk, the background error assembled from the
    covariance the experiment asked for.

The uppercase spelling shares the experiment-time sigil on purpose. A template
under ``config/soca/`` is not a layer and must never be merged as one, and if
one ever is, `resolve.py` meets ``$(OBSERVERS)`` and refuses it as an unknown
symbol rather than quietly resolving it to nothing. Two syntaxes that fail into
each other loudly beat three that do not.

**What may be written in a template, and what may not.** A template holds the
shape of a JEDI document: which blocks exist, what they are called, where they
sit, and why. It holds constants only when nothing in Python reads them. The
moment a value is also read on the Python side, it becomes a slot, because two
spellings of a filename field is a `writeback` that opens the wrong name and
reports that the analysis produced nothing. That rule is the whole of what
separates this from the templated workflows that came before: there is nothing
in these files for a configuration to disagree with.

Filling is strict in both directions. An unfilled slot is an error, and so is a
slot the caller computed that the template never used: the second one is a
template that has quietly dropped a block, which is exactly the failure this
module's callers spend their comments warning about. A missing ``output:`` in
the variational document costs every ``oman`` in the experiment and JEDI says
nothing about it.
"""

import re

#: A slot: uppercase, so it cannot be confused with an experiment-time symbol.
SLOT = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
WHOLE = re.compile(r"^\$\(([A-Za-z_][A-Za-z0-9_]*)\)$")


class TemplateError(Exception):
    def __init__(self, message, path=""):
        self.message = message
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


def fill(document, slots, *, source=""):
    """Return *document* with every ``$(NAME)`` replaced by ``slots[NAME]``.

    *source* names the template file, so a message about a slot says which file
    to open.
    """
    used = set()
    filled = _walk(document, slots, used, (), source)

    unused = sorted(set(slots) - used)
    if unused:
        raise TemplateError(
            f"{', '.join(unused)} was computed for this document and the "
            f"template does not use it. Either the template has dropped a "
            f"block, which JEDI will not report, or the value is no longer "
            f"needed and should stop being computed",
            source,
        )
    return filled


def slots_of(document):
    """Every slot name a template refers to. For checking one without filling it."""
    found = set()
    _walk(document, None, found, (), "")
    return found


def _walk(node, slots, used, path, source):
    if isinstance(node, dict):
        return {k: _walk(v, slots, used, path + (k,), source)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, slots, used, path + (i,), source)
                for i, v in enumerate(node)]
    if isinstance(node, str):
        return _substitute(node, slots, used, _dotted(path), source)
    return node


def _substitute(value, slots, used, path, source):
    whole = WHOLE.match(value)
    if whole:
        return _one(whole.group(1), slots, used, path, source, embedded=False)

    def replace(match):
        return str(_one(match.group(1), slots, used, path, source, embedded=True))

    return SLOT.sub(replace, value)


def _one(name, slots, used, path, source, embedded):
    if not name.isupper():
        raise TemplateError(
            f"$({name}) is lowercase, which is an experiment-time symbol, and "
            f"this file is never read by that pass. A slot a task fills is "
            f"uppercase",
            path,
        )
    used.add(name)
    if slots is None:
        return ""
    if name not in slots:
        raise TemplateError(
            f"$({name}) is not a slot this document has a value for. The slots "
            f"are set where the document is built, so this is either a typo or "
            f"a block that belongs in a different template",
            path,
        )

    value = slots[name]
    if embedded and isinstance(value, (dict, list)):
        raise TemplateError(
            f"$({name}) is a {type(value).__name__} and cannot be interpolated "
            f"into surrounding text; give it a line of its own to use it whole",
            path,
        )
    return value


def _dotted(path):
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out
