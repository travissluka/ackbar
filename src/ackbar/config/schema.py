"""ACKBAR's own schema, and the merge keys declared inside it.

One document describes both the shape of the config and how its lists merge, so
the two cannot drift apart. The merge keys are carried on array nodes as
``x-ackbar-merge-key``, which JSON Schema validators ignore as an unknown
annotation.

The schema is deliberately strict about keys ACKBAR owns and permissive about
subtrees that are verbatim JEDI config. ACKBAR's schema describes ACKBAR's
config; it is not a model of OOPS, and trying to make it one is how a workflow
ends up reimplementing the thing it drives.
"""

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

MERGE_KEY = "x-ackbar-merge-key"

DEFAULT_SCHEMA = Path(__file__).resolve().parents[3] / "config" / "schema" / "experiment.yaml"


def load_schema(path=None):
    path = Path(path) if path else DEFAULT_SCHEMA
    with open(path) as f:
        return yaml.safe_load(f)


def merge_keys(schema, path=(), found=None):
    """Collect every ``x-ackbar-merge-key`` in the schema, by schema path.

    Returns a mapping such as ``{"observations": "obs space.name"}``. List
    indices are not part of a schema path, so descending through ``items`` adds
    no component.
    """
    found = {} if found is None else found
    if not isinstance(schema, dict):
        return found

    if MERGE_KEY in schema:
        found[".".join(path)] = schema[MERGE_KEY]

    for name, sub in (schema.get("properties") or {}).items():
        merge_keys(sub, path + (name,), found)

    items = schema.get("items")
    if isinstance(items, dict):
        merge_keys(items, path, found)

    for combinator in ("allOf", "anyOf", "oneOf"):
        for sub in schema.get(combinator) or []:
            merge_keys(sub, path, found)

    # `then` and `else` carry `properties` and the schema already uses `if` in
    # five places. A merge key added inside one and not collected here would be
    # read as `None`, and the list it keys would replace wholesale instead of
    # merging, which for `observations` means an experiment that quietly ships
    # with only the observers its last layer restated.
    for branch in ("then", "else"):
        sub = schema.get(branch)
        if isinstance(sub, dict):
            merge_keys(sub, path, found)

    return found


def validate(config, schema):
    """Validate a merged config. Returns a list of (dotted_path, message),
    sorted, empty when the config is good.

    Every error is returned rather than only the first, because the point of
    validating up front is to fix everything in one pass instead of discovering
    the next problem on the next submit.
    """
    validator = Draft202012Validator(schema)
    errors = []
    for error in validator.iter_errors(config):
        errors.append((_dotted(error.absolute_path), error.message))
    return sorted(errors)


def _dotted(deque_path):
    out = ""
    for part in deque_path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out
