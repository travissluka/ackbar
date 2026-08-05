"""ISO 8601 durations, restricted to the ones a cycling experiment can mean.

Cycle length, window length and forecast length are all written as ISO 8601
durations: ``PT24H``, ``P7D``. There is no such parser in the standard library
and pulling in a dependency for it is not worth it, so this is that parser.

Months and years are rejected rather than approximated. Their length depends on
which month it is, so ``start + n * P1M`` is not a function of ``n`` alone, and
every cycle date in an experiment would then depend on the path taken to reach
it rather than on the cycle number. That breaks the property the whole graph
rests on: cycle *n* is computable without simulating cycles 1 through *n-1*.
"""

import re
from datetime import datetime, timedelta, timezone

#: Weeks, days, hours, minutes, seconds. The negative lookaheads reject the
#: degenerate forms "P" and "PT", which are valid-looking and mean nothing.
_DURATION = re.compile(
    r"^P(?!$)(?:(\d+)W)?(?:(\d+)D)?(?:T(?!$)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)

#: What JEDI expects to read back, and what ACKBAR writes everywhere.
ISO_INSTANT = "%Y-%m-%dT%H:%M:%SZ"


class DurationError(ValueError):
    pass


def parse_duration(text):
    """``PT24H`` -> ``timedelta(hours=24)``."""
    if not isinstance(text, str):
        raise DurationError(f"duration must be a string, got {type(text).__name__}")

    match = _DURATION.match(text)
    if not match:
        # Before the T, M means months; after it, minutes. So look only at the
        # date part to tell a rejected P1M from an accepted PT30M.
        if re.search(r"\d+[YM]", text.split("T", 1)[0]):
            raise DurationError(
                f"{text!r}: years and months are not accepted, because their "
                f"length depends on which month it is. Use days or hours."
            )
        raise DurationError(f"{text!r} is not an ISO 8601 duration (expected e.g. PT6H, P1D)")

    weeks, days, hours, minutes, seconds = match.groups()
    return timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=float(seconds or 0),
    )


def format_duration(delta):
    """``timedelta(hours=24)`` -> ``P1DT0H0M0S``, canonically.

    Always fully spelled out. A canonical form matters more than a short one
    here, because these strings end up in goldens and in generated JEDI config,
    where a value that renders two different ways for the same duration is a
    diff that means nothing.
    """
    total = int(delta.total_seconds())
    if total < 0:
        raise DurationError("negative durations are not meaningful here")
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"P{days}DT{hours}H{minutes}M{seconds}S"


def parse_instant(text):
    """An ISO 8601 instant, always returned as an aware UTC datetime.

    ``datetime.fromisoformat`` learned to read a trailing ``Z`` only in 3.11,
    and the configs are full of them.
    """
    if isinstance(text, datetime):
        value = text
    else:
        if not isinstance(text, str):
            raise DurationError(f"date must be a string, got {type(text).__name__}")
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise DurationError(
                f"{text!r} is not an ISO 8601 date-time (expected e.g. "
                f"2018-04-15T00:00:00Z)"
            )
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_instant(value):
    return value.strftime(ISO_INSTANT)
