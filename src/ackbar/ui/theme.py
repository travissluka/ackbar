"""The palette, as data.

No textual and no rich import here on purpose: this is a lookup table, the rest
of the console builds styles out of it, and keeping it plain means the colours
can be asserted in a test that runs in a bare environment.

**Colour carries state, not shape.** Every cell in the status grid is the same
solid block and differs only in hue, because a grid of ticks and crosses is
something you have to read one cell at a time, while a band of colour is
something you take in at a glance. The one exception is the cursor, which swaps
to a smaller glyph so that its highlight background is visible behind it, and
that is not a symbol to learn: it is where you are.

Two palettes. `PALETTES["safe"]` exists because the default puts green and red
next to each other, which is the one pairing a sizable fraction of people cannot
separate, and a status display nobody can read is not a status display.
"""

from ..state import (BLOCKED, COMPLETE, FAILED, PENDING, RUNNING, STRANDED,
                     UNSUBMITTED)

#: The cell every state is drawn with. Adjacent cells merge into one continuous
#: bar, which is what makes a row read as progress rather than as characters.
CELL = "█"

#: The cursor's own glyph. Narrower than a full block so the highlight behind it
#: shows through on both sides.
CURSOR_CELL = "◆"

#: Drawn for a cycle the graph does not contain at all, as opposed to one that
#: exists and has not been submitted. Absent and pending-forever are different
#: facts and the grid should not merge them.
ABSENT_CELL = " "

#: The four cool states form a deliberate brightness ramp, dark to bright:
#: unsubmitted, blocked, queued, running. Most of a healthy experiment is
#: blocked on dependencies at any moment, which is the workflow working and
#: should stay quiet, while a job the scheduler has accepted is one step from
#: moving and earns some light. Read down the ramp and the leading edge of the
#: experiment is the brightest thing on the screen without anything having to
#: be labelled.
PALETTES = {
    # Dark-terminal first, in the same register as the rest of a modern CLI.
    "default": {
        COMPLETE: "#3fb950",
        RUNNING: "#2dd4bf",
        PENDING: "#6272d9",
        BLOCKED: "#414d63",
        UNSUBMITTED: "#2b3138",
        STRANDED: "#e3b341",
        FAILED: "#f85149",
    },
    # No red/green pair anywhere. Complete is blue, failed is magenta, stranded
    # is orange, and running is near-white so the leading edge stays brightest.
    "safe": {
        COMPLETE: "#58a6ff",
        RUNNING: "#f0f6fc",
        PENDING: "#a5d6ff",
        BLOCKED: "#3f4855",
        UNSUBMITTED: "#21262d",
        STRANDED: "#ffa657",
        FAILED: "#d763e8",
    },
}

#: The order states are shown in a legend, and the order a summary word is
#: chosen in. Mirrors `state.SEVERITY` deliberately: worst last, so a legend
#: reads from "fine" to "look at this".
LEGEND = (COMPLETE, RUNNING, PENDING, BLOCKED, UNSUBMITTED, STRANDED, FAILED)

#: What each state is called in prose. The grid needs no words, but the detail
#: pane and the sidebar do, and they should agree with `heal`'s output.
#: `pending` is shown as "queued", which is what it now means: eligible, and
#: waiting for the scheduler rather than for the graph. The constant keeps its
#: name because the ledger, `heal` and every test already spell it that way.
WORD = {
    COMPLETE: "complete",
    RUNNING: "running",
    PENDING: "queued",
    BLOCKED: "blocked",
    UNSUBMITTED: "unsubmitted",
    STRANDED: "stranded",
    FAILED: "failed",
}

#: Colour for an experiment's overall verdict, which is `state.finished`'s
#: vocabulary rather than a node state. `stalled` is amber rather than red
#: because nothing failed: the submitter is gone, which is a different problem
#: and has a different fix.
OVERALL = {
    "running": RUNNING,
    "finished": COMPLETE,
    "broken": FAILED,
    "stalled": STRANDED,
}

#: Chrome. Named rather than inlined in the stylesheet so that a widget drawing
#: rich markup and a stylesheet rule cannot drift apart.
INK = {
    "accent": "#2dd4bf",
    "text": "#c9d1d9",
    "muted": "#7d8590",
    "faint": "#484f58",
    "surface": "#161b22",
    "cursor": "#484f58",
    "column": "#21262d",
    "warn": "#e3b341",
    "bad": "#f85149",
}


def palette(name="default"):
    if name not in PALETTES:
        raise KeyError(
            f"unknown palette {name!r}; try {' or '.join(sorted(PALETTES))}"
        )
    return PALETTES[name]


def overall_colour(verdict, name="default"):
    """The colour of `state.finished`'s answer, via the state it stands for."""
    key = OVERALL.get(verdict)
    return palette(name)[key] if key else INK["muted"]


__all__ = [
    "ABSENT_CELL", "CELL", "CURSOR_CELL", "INK", "LEGEND", "OVERALL",
    "PALETTES", "WORD", "overall_colour", "palette",
]
