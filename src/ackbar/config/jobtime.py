"""The job-time substitution pass, and the closed set of symbols it knows.

``$(...)`` is resolved once when the experiment is created and frozen
(``resolve.py``). ``{{...}}`` is what is left: the values that genuinely cannot
be known then, because they differ per cycle and per ensemble member.

v3 had this same split and it is the right one. What it lacked was a *named,
closed* set of job-time symbols, so the second pass resolved whatever happened
to be in scope at the time. Here the set is the ``SYMBOLS`` table below, an
unknown name is an error that names the config path, and `ackbar config symbols`
prints the table so that "what can I write here" has an answer that is not
"read the resolver".

A whole-token string takes the symbol's type, so ``members: {{member}}`` is an
integer. A token embedded in text interpolates as text. Either form may carry a
format spec after a colon, which goes to Python's ``format()``: dates therefore
take strftime patterns, and ``{{current_cycle:%Y%m%d%H}}`` is how a path
component gets a compact date while ``{{window_begin}}`` stays the ISO instant
JEDI parses.
"""

import re
import zlib
from datetime import datetime

from ..duration import ISO_INSTANT, format_duration, parse_duration, parse_instant

TOKEN = re.compile(r"\{\{([^{}]+)\}\}")
WHOLE = re.compile(r"^\{\{([^{}]+)\}\}$")

#: The closed set. Order is presentation order for `ackbar config symbols`.
SYMBOLS = (
    ("cycle", "cycle number, counting from 1"),
    ("current_cycle", "the cycle's analysis time"),
    ("previous_cycle", "one cycle length earlier; cycle 0 for the first cycle"),
    ("next_cycle", "one cycle length later"),
    ("window_begin", "start of the assimilation window"),
    ("window_end", "end of the assimilation window"),
    ("window_length", "assimilation window length, ISO 8601"),
    ("forecast_begin", "start of this cycle's forecast, the analysis time"),
    ("forecast_end", "end of this cycle's forecast"),
    ("forecast_length", "forecast length, ISO 8601"),
    ("member", "ensemble member index; the control is 0"),
    ("member_dir", "the member's directory name, mem000 and up"),
    ("seed", "random seed, derived from experiment, cycle and member"),
    ("mom6_current_date", "MOM6 input.nml date list, y,m,d,h,m,s"),
    ("mom6_hours", "forecast length in whole hours, for MOM6"),
)

SYMBOL_NAMES = tuple(name for name, _ in SYMBOLS)


class JobTimeError(Exception):
    def __init__(self, message, path=""):
        self.message = message
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


def cycle_length(config):
    return parse_duration(config["cycle"]["length"])


def cycle_time(config, cycle):
    """The analysis time of cycle *n*, computed from *n* alone.

    Cycle 1 is at ``cycle.start``, so cycle 0 is one length earlier: that is
    where experiment setup materializes the offline initial condition, which is
    what makes cycle 1 an ordinary cycle rather than a special case.
    """
    return parse_instant(config["cycle"]["start"]) + (cycle - 1) * cycle_length(config)


def window_bounds(config, cycle):
    """The assimilation window, centred on the analysis time.

    Centred, and as long as the cycle, so that consecutive windows tile the
    experiment without gap or overlap. soca-science computed it the same way.
    """
    length = cycle_length(config)
    middle = cycle_time(config, cycle)
    return middle - length / 2, middle + length / 2


def seed_for(config, cycle, member):
    """A seed that a heal reproduces.

    Derived from experiment, cycle and member so that resubmitting a failed
    member regenerates the same perturbation it would have had. ``hash()`` is
    salted per process and would silently produce a different ensemble on the
    retry, which is the exact failure v3's ``__SEED__`` token existed to avoid.
    """
    name = config["experiment"]["name"]
    return zlib.crc32(f"{name}:{cycle}:{member}".encode()) & 0x7FFFFFFF


def symbols(config, cycle, member=0):
    """The job-time symbol table for one (cycle, member)."""
    length = cycle_length(config)
    now = cycle_time(config, cycle)
    begin, end = window_bounds(config, cycle)
    return {
        "cycle": cycle,
        "current_cycle": now,
        "previous_cycle": now - length,
        "next_cycle": now + length,
        "window_begin": begin,
        "window_end": end,
        "window_length": format_duration(length),
        "forecast_begin": now,
        "forecast_end": now + length,
        "forecast_length": format_duration(length),
        "member": member,
        "member_dir": member_dir(member),
        "seed": seed_for(config, cycle, member),
        "mom6_current_date": "{0.year},{0.month},{0.day},{0.hour},{0.minute},{0.second}".format(now),
        "mom6_hours": int(length.total_seconds() // 3600),
    }


def member_dir(member):
    """Every member is ``mem###``, including the control, which is ``mem000``.

    No ctrl-versus-ens split anywhere in the tree: that split is what made every
    ensemble loop in v2 carry a special case.
    """
    return f"mem{member:03d}"


def render(config, table):
    """Return *config* with every ``{{...}}`` replaced from *table*."""
    return _walk(config, table, ())


def _walk(node, table, path):
    if isinstance(node, dict):
        return {k: _walk(v, table, path + (k,)) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, table, path + (i,)) for i, v in enumerate(node)]
    if isinstance(node, str):
        return _substitute(node, table, _dotted(path))
    return node


def _substitute(value, table, path):
    whole = WHOLE.match(value)
    if whole:
        return _one(whole.group(1), table, path, embedded=False)
    return TOKEN.sub(lambda m: _one(m.group(1), table, path, embedded=True), value)


def _one(token, table, path, embedded):
    name, _, spec = token.strip().partition(":")
    name = name.strip()
    if name not in table:
        raise JobTimeError(
            f"unknown job-time symbol {{{{{name}}}}}; the set is closed, see "
            f"`ackbar config symbols`",
            path,
        )

    value = table[name]
    if isinstance(value, datetime):
        return value.strftime(spec or ISO_INSTANT)
    if spec:
        return format(value, spec)
    # A whole-token string takes the symbol's type; embedded, it is text.
    return str(value) if embedded else value


def unresolved(config):
    """Any ``{{...}}`` left after a render. Should always be empty."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + (key,))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + (i,))
        elif isinstance(node, str) and TOKEN.search(node):
            found.append((_dotted(path), node))

    walk(config, ())
    return found


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
