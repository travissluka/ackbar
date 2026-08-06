"""A forecast that does not integrate: the state, handed forward.

`model: persistence` runs the whole workflow with the model taken out of it. The
forecast reads the restart set its cycle starts from, writes it out again as the
next cycle's, and advances the clock. Everything else in the experiment is
unchanged: the analysis reads a real background off a real grid, writeback edits
a real restart, `cleanup` reaps real directories.

It exists because the DA loop and the model are two independent things that fail
in the same place. A three-cycle 3DVar experiment at `gom_25km` spends most of
its wall clock in MOM6, and every one of those minutes is spent re-proving
something phase 4 already proved. With persistence the loop closes in seconds,
so the handoff, the writeback and the departures can be got right before the
model is put back in.

It is also a baseline. Persistence is the forecast to beat, and an experiment
whose analysis does not score better than one is an experiment with a problem
that is not in the model.

What it is not is a model. The state it hands forward is dynamically stale the
moment the clock moves: nothing mixes, nothing advects, and the analysis is
therefore correcting a background that gets worse in a way no ocean does. Read
any result from a persistence experiment as a statement about the workflow.
"""

import re
import shutil
from pathlib import Path

from .config.jobtime import cycle_time
from .mom6sis2 import STAMP, ModelError

#: The line of `coupler.res` that says what time it is, and the six integers on
#: it. FMS writes three lines: the calendar, the time the run started from, and
#: the time the state is valid at. Only the third moves.
#:
#: Read by `coupler_main` out of the literal path `INPUT/coupler.res`, which is
#: why this is the file that has to be right rather than the namelist. A restart
#: set carrying the wrong time integrates the right state from the wrong date
#: and says nothing at all about it.
CURRENT_TIME_LINE = 2
DATE_FIELDS = re.compile(r"^(\s*\d+){6}")


def forecast(config, paths, cycle, task, member, *, source, target):
    """Hand *source*'s restart set to *target*, valid one cycle later."""
    if not (source / STAMP).exists():
        raise ModelError(
            f"{source} is not a restart set: no {STAMP}. Persistence has "
            f"nothing to hand forward, and the cycle it came from either never "
            f"ran or exited without finishing."
        )

    # Copied rather than linked, all of it. A restart set that shares storage
    # with the set it was made from stops being a restart set the moment
    # `cleanup` reaps the other one, and the failure surfaces as a forecast that
    # cannot open its own input two cycles later.
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.name == STAMP or not entry.is_file():
            continue
        _commit(entry, target / entry.name)

    _commit(advance(source / STAMP, cycle_time(config, cycle + 1)),
            target / STAMP)
    print(f"ackbar: {cycle}.{task} member {member}: persisted {source} -> "
          f"{target}, valid {cycle_time(config, cycle + 1)}")
    return target


def advance(stamp, when):
    """`coupler.res` with its current model time set to *when*.

    Rewritten rather than regenerated: the calendar on line 1 and the model
    start time on line 2 are facts about the run this state came from, and
    inventing them here would produce a file that parses and lies.
    """
    lines = Path(stamp).read_text().splitlines()
    if len(lines) <= CURRENT_TIME_LINE:
        raise ModelError(
            f"{stamp} has {len(lines)} line(s); a coupler.res has three, and "
            f"the third is the one that says what time it is"
        )

    line = lines[CURRENT_TIME_LINE]
    if not DATE_FIELDS.match(line):
        raise ModelError(
            f"{stamp} line {CURRENT_TIME_LINE + 1} does not start with six "
            f"integers, so it is not the current model time: {line!r}"
        )
    fields = "".join(f"{value:6d}" for value in
                     (when.year, when.month, when.day,
                      when.hour, when.minute, when.second))
    lines[CURRENT_TIME_LINE] = DATE_FIELDS.sub(fields, line, count=1)
    return "\n".join(lines) + "\n"


def _commit(payload, target):
    """Write through a temporary name in the destination, then rename.

    *payload* is either a path to copy or the text to write, because the only
    file whose contents change is `coupler.res` and it is the small one. A
    restart file is a gigabyte and is never read into memory to be written back
    out.
    """
    temp = target.with_name(target.name + ".partial")
    if isinstance(payload, Path):
        shutil.copyfile(payload, temp)
    else:
        temp.write_text(payload)
    temp.replace(target)
    return target
