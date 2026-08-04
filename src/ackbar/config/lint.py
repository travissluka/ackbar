"""Checks that are not about shape, and so do not belong in the schema.

Currently one: scalars that PyYAML read as strings but that JEDI will read as
numbers.

PyYAML implements YAML 1.1, whose float resolver requires an explicit sign on
the exponent. eckit and yaml-cpp implement the YAML 1.2 core schema, which does
not. So ``halo size: 500e3`` is the string ``'500e3'`` to ACKBAR and the number
500000 to JEDI, and ``1e-3`` and ``5.0e5`` are strings too.

The re-emitted YAML is usually still correct, because PyYAML writes these back
unquoted. The damage is internal: ACKBAR's own schema rejects a string where it
wants a number, comparisons against numeric limits are wrong, and the error
message points at a value that looks fine. The soca-science observer configs
that this workflow inherits are full of thresholds written this way, so the
check pays for itself on the port.
"""

import re

import yaml

#: Numerics under the YAML 1.2 core schema, which is what JEDI reads.
YAML12_NUMBER = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")


def ambiguous_numbers(config, path=()):
    """Find string scalars that JEDI will read as numbers.

    Returns [(dotted_path, value)].
    """
    found = []
    if isinstance(config, dict):
        for key, value in config.items():
            found += ambiguous_numbers(value, path + (key,))
    elif isinstance(config, list):
        for i, value in enumerate(config):
            found += ambiguous_numbers(value, path + (i,))
    elif isinstance(config, str):
        if _is_ambiguous(config):
            found.append((_dotted(path), config))
    return found


def _is_ambiguous(value):
    """True when JEDI reads this string as a number and PyYAML did not.

    The YAML 1.1 side is answered by asking PyYAML rather than by restating its
    resolver here. Its float rule has more corners than it looks (a decimal
    point is required in every case, sexagesimals resolve, ``1e-3`` does not),
    and a second copy of it would only drift.
    """
    if not YAML12_NUMBER.match(value):
        return False
    try:
        return isinstance(yaml.safe_load(value), str)
    except yaml.YAMLError:
        return False


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
