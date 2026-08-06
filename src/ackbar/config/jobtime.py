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
from datetime import datetime, timedelta

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
    ("handoff", "valid time of the restart set the next cycle starts from"),
    ("member", "ensemble member index; the control is 0"),
    ("member_dir", "the member's directory name, mem000 and up"),
    ("seed", "random seed, derived from experiment, cycle and member"),
    ("mom6_current_date", "MOM6 input.nml date list, y,m,d,h,m,s"),
    ("mom6_hours", "whole hours of the forecast, for MOM6"),
    ("mom6_minutes", "whole minutes of the forecast past mom6_hours"),
    ("mom6_seconds", "whole seconds of the forecast past mom6_minutes"),
    ("mom6_restart_interval", "MOM6 intermediate restart cadence, y,m,d,h,m,s"),
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


#: Window types whose analysis reads a *trajectory* rather than one state, and
#: which therefore need the forecast to overshoot. `3d` compares every
#: observation in the window against a single background at the centre, so it
#: needs nothing the cycle does not already produce.
FOUR_D = ("fgat", "4d")


def window_type(config):
    """`3d`, `fgat` or `4d`, and `3d` for an experiment with no solver."""
    window = (config.get("solver") or {}).get("window") or {}
    return window.get("type", "3d")


def window_length(config):
    """The assimilation window. The cycle length unless something says otherwise.

    Equal to the cycle is the case where consecutive windows tile the
    experiment with no gap and no overlap, which is why it is the default and
    why soca-science computed it the same way (`DA_WINDOW_LEN` defaulting to
    `DA_CYCLE_LEN` in its `cycle.sh`).
    """
    declared = ((config.get("solver") or {}).get("window") or {}).get("length")
    return parse_duration(declared) if declared else cycle_length(config)


def window_bounds(config, cycle):
    """The assimilation window, centred on the analysis time.

    Centred, always. The analysis is written at the window's midpoint, not at
    its start: `CostFctFGAT::doLinearize` saves the state at
    `timeWindow_.midpoint()` and `finishLinearize` replaces the background and
    the first guess with it. A window centred anywhere else would therefore
    produce an analysis valid at a time no cycle starts from.
    """
    length = window_length(config)
    middle = cycle_time(config, cycle)
    return middle - length / 2, middle + length / 2


def forecast_overshoot(config):
    """How far past the next analysis time the cycling forecast integrates.

    Zero for a 3D window, which reads one background at the next analysis time
    and nothing after it. Half a window for FGAT and 4D, which read the whole
    of the next cycle's window, and the far half of that window lies *after*
    the time the next cycle is centred on. Nothing else can produce those
    states: the next cycle's own forecast starts from the analysis, so a state
    it wrote at the same clock time is a different trajectory.

    This is the cost of a 4D window and it is not avoidable by moving the
    window, only by shortening it: the model runs `1 + W/2C` times as long as
    the cycling itself, which at the default `W == C` is one and a half.
    """
    if window_type(config) not in FOUR_D:
        return timedelta(0)
    return window_length(config) / 2


def forecast_length(config):
    """How long one cycling forecast integrates for.

    The cycle length, so that the next cycle starts where this one is centred,
    plus whatever overshoot the window asks for. **One** model run covers the
    whole of it: FMS dumps the intermediate states in place as its clock
    reaches them, so the overshoot costs extra integration and nothing else, no
    chained short forecasts and no handoff between them to get wrong.
    """
    return cycle_length(config) + forecast_overshoot(config)


def slot_length(config):
    """How often the forecast writes a state inside its integration, or None.

    None is the ordinary case and means it writes one restart set, at the end,
    which is the whole of what the next cycle and a 3D analysis need. A 4D
    window is what asks for more. See `forecast.slots` in the schema.
    """
    declared = (config.get("forecast") or {}).get("slots")
    return parse_duration(declared) if declared else None


def slot_times(config, cycle):
    """The valid times cycle *n*'s forecast writes a sub-window state at.

    The last window's worth of the forecast, ending on its last step. That is
    the window the *next* cycle assimilates in: with an overshoot of `W/2` the
    forecast ends at `T(n+1) + W/2`, so the series runs from `T(n+1) - W/2`,
    which is where FGAT reads its background and where 4D-Ens-Var's first
    sub-window sits. With no overshoot the window is the cycle and the series
    spans it.

    Computed rather than discovered, because this is what declares the task's
    outputs *before* it has run: `ackbar validate` stats them and the skip rule
    asks whether they are all there.

    The forecast's own start time is never among them. FMS writes an
    intermediate restart when its clock *reaches* an interval, so nothing is
    written at hour zero, and the state there is the restart set the forecast
    was handed rather than one it produced. That is why the series begins one
    cadence in when the window is the whole forecast, which is the free-running
    case, and at the window's own start otherwise.
    `graph.build._check_window` is what refuses a window that reaches further
    back than that.
    """
    step = slot_length(config)
    if not step:
        return ()
    run = forecast_length(config)
    first = max(step, run - window_length(config))
    begin = cycle_time(config, cycle)
    count = int((run - first) / step) + 1
    return tuple(begin + first + index * step for index in range(count))


def handoff_time(config, cycle):
    """The valid time of the restart set cycle *n* hands to cycle *n+1*.

    The next cycle's analysis time, which is the forecast's own last step only
    when the window asks for no overshoot. With one, the set the next cycle
    starts from is written *during* the run rather than at the end of it, and
    `mom6sis2.commit` assembles it from the time-stamped files of that
    interval. Integrating past a time does not change the state at it.
    """
    return cycle_time(config, cycle) + cycle_length(config)


def mom6_interval(delta):
    """A cadence as MOM6's `restart_interval`: year, month, day, hour, min, sec.

    All zeros when nothing asked for one, which is what `coupler_main` reads as
    "write no intermediate restarts". The first two are always zero because
    `parse_duration` refuses months and years.
    """
    total = int(delta.total_seconds()) if delta else 0
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"0, 0, {days}, {hours}, {minutes}, {seconds}"


def seed_for(config, cycle, member):
    """A seed that a heal reproduces.

    Derived from experiment, cycle and member so that resubmitting a failed
    member regenerates the same perturbation it would have had. ``hash()`` is
    salted per process and would silently produce a different ensemble on the
    retry, which is the exact failure v3's ``__SEED__`` token existed to avoid.
    """
    name = config["experiment"]["name"]
    return zlib.crc32(f"{name}:{cycle}:{member}".encode()) & 0x7FFFFFFF


def extended_run(config):
    """How long the long forecast integrates for, or None if there is not one."""
    declared = ((config.get("forecast") or {}).get("extended") or {}).get("length")
    return parse_duration(declared) if declared else None


def extended_interval(config):
    """How often the long forecast writes a state, or None.

    `slots` when it is set, because that is the cadence the departures want and
    it is the finer of the two; the kept states are a subset of what it writes.
    Falls back to `interval`, so a forecast that names only a state cadence
    still writes at it. See `forecast.extended` in the schema.
    """
    extended = (config.get("forecast") or {}).get("extended") or {}
    declared = extended.get("slots") or extended.get("interval")
    return parse_duration(declared) if declared else None


def symbols(config, cycle, member=0, task="forecast"):
    """The job-time symbol table for one (cycle, member), as *task* sees it.

    *task* exists for one reason: `forecast.ext` is the same executable run for
    a different length, writing at a different cadence, and both of those reach
    the model through this table. Defaulting to the cycling forecast keeps every
    other caller, none of which is integrating anything, unchanged.
    """
    length = cycle_length(config)
    now = cycle_time(config, cycle)
    begin, end = window_bounds(config, cycle)
    if task == "forecast.ext":
        run = extended_run(config)
        interval = extended_interval(config)
    else:
        run = forecast_length(config)
        interval = slot_length(config)
    hours, rest = divmod(int(run.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)
    return {
        "cycle": cycle,
        "current_cycle": now,
        "previous_cycle": now - length,
        "next_cycle": now + length,
        "window_begin": begin,
        "window_end": end,
        "window_length": format_duration(window_length(config)),
        "forecast_begin": now,
        "forecast_end": now + run,
        "forecast_length": format_duration(run),
        "handoff": handoff_time(config, cycle),
        "member": member,
        "member_dir": member_dir(member),
        "seed": seed_for(config, cycle, member),
        "mom6_current_date": "{0.year},{0.month},{0.day},{0.hour},{0.minute},{0.second}".format(now),
        # Split three ways rather than truncated to hours. A half window of
        # overshoot need not be a whole number of them, and `coupler_nml` reads
        # each field separately, so an hours-only run length silently shortens
        # the forecast and every state it was asked for lands early.
        "mom6_hours": hours,
        "mom6_minutes": minutes,
        "mom6_seconds": seconds,
        "mom6_restart_interval": mom6_interval(interval),
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
