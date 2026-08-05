"""The append-only record of what was submitted.

Slurm answers "how did job 12345 end". It cannot answer "what is the job id of
cycle 7's writeback", which is exactly what `heal`, `cancel` and `status` need,
and its records are purged on a site retention that can be weeks.

So this holds **identity only**. It is written once at submit, is never updated,
and is never a source of truth about outcome: Slurm stays authoritative for
state. Retry counters live here because they cannot live anywhere else, since
job names are deliberately identical across attempts and `sacct` rows purge.

One JSON object per line, opened `O_APPEND` and flushed. That is the only write
mode where two writers (the submitter job and a healer run by hand) cannot
interleave a partial record, and where a torn tail costs one line rather than
the file.
"""

import json
import os
from datetime import datetime, timezone

#: Fields every record carries. Written in this order so a human reading the
#: file sees identity before bookkeeping.
FIELDS = (
    "experiment",
    "cycle",
    "task",
    "members",
    "attempt",
    "job_id",
    "array",
    "dependency",
    "submitted",
)


def append(paths, *, cycle, task, members, attempt, job_id, dependency):
    """Record one submission. Returns the record."""
    record = {
        "experiment": paths.experiment,
        "cycle": cycle,
        "task": task,
        "members": list(members),
        "attempt": attempt,
        "job_id": job_id,
        "array": bool(members),
        "dependency": dependency or "",
        "submitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    line = json.dumps({field: record[field] for field in FIELDS}) + "\n"

    paths.ledger_file.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(paths.ledger_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(handle, line.encode())
        os.fsync(handle)
    finally:
        os.close(handle)
    return record


def read(paths):
    """Every record, in submission order.

    A trailing partial line is dropped rather than raising: a ledger torn by a
    node failure should still answer what it can, since the alternative is that
    `heal` cannot run at exactly the moment it is needed.
    """
    if not paths.ledger_file.exists():
        return []
    records = []
    for line in paths.ledger_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def latest(paths):
    """The most recent record per (cycle, task). Keyed by the tuple."""
    out = {}
    for record in read(paths):
        out[(record["cycle"], record["task"])] = record
    return out


def attempts(paths, cycle, task):
    """How many times this node has been submitted."""
    return sum(
        1 for r in read(paths) if r["cycle"] == cycle and r["task"] == task
    )


def job_id(paths, cycle, task):
    """The job id of the latest attempt, or None."""
    record = latest(paths).get((cycle, task))
    return record["job_id"] if record else None


def submitted_cycles(paths):
    return sorted({r["cycle"] for r in read(paths)})
