"""Pulling one cycle's resource usage out of Slurm before Slurm forgets it.

`sacct` rows are purged on a site retention that can be weeks, so an experiment
that wants to be answerable later has to copy them into its own directory while
they exist. Attribution comes from the job name and the `--comment`, so a
harvested row maps back to cycle, task and member without a side table.

Three things about this that the obvious implementation gets wrong:

- **`MaxRSS` and `AveCPU` exist only on step rows, never on job rows.** The
  natural query, `sacct -X` for one row per job, returns no memory data at all.
  So harvest *without* `-X` and reduce the maximum over step rows per job id.
- **The launcher changes what `MaxRSS` means**, so it is recorded next to the
  numbers. Under `srun` ranks are separate steps and `MaxRSS` is per task; under
  `mpiexec` there is only a `.batch` step whose cgroup covers every rank on the
  node, so the same field is a node aggregate. Sizing memory from one regime
  using numbers from the other is wrong by roughly ranks-per-node.
- **Sampled peaks miss short spikes.** `JobAcctGatherFrequency` is tens of
  seconds, so a peak during a restart read can fall entirely between samples.
  Any future escalation should be multiplicative rather than
  observed-peak-plus-epsilon.
- **One harvest of a cycle is never the whole story.** The task doing it is a
  leaf of the cycle it describes, so its siblings are still queued when it reads
  the accounting. `refresh_stale` is how that snapshot gets corrected, and
  `write` is what makes correcting it safe.
"""

import json
import re

from . import ledger, slurm

#: What is worth keeping per job. Everything here survives into
#: `stats/<cycle>.json` and nothing else does.
FIELDS = (
    "JobID", "JobName", "State", "ExitCode", "Elapsed", "TotalCPU", "AveCPU",
    "MaxRSS", "MaxRSSNode", "MaxRSSTask", "ReqMem", "AllocCPUS", "NNodes",
    "Submit", "Start", "End",
)

_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$")
_SCALE = {"": 1 / 1024, "K": 1, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}


def harvest_cycle(paths, cycle, launcher=""):
    """Collect one cycle's accounting into a plain dict.

    Driven off the ledger rather than a `sacct --name` query, so that a row is
    only ever attributed to the experiment that actually submitted it. Two
    experiments running at once produce identically named jobs.
    """
    wanted = {
        record["job_id"]: record
        for record in ledger.read(paths)
        if record["cycle"] == cycle
    }
    if not wanted:
        # The same shape as a cycle that ran, because every consumer reads
        # `totals`, and "no jobs" is the answer for every cycle of a running
        # experiment that has not been reached yet. A short dict here makes
        # `ackbar harvest` raise on the first such cycle, which is the state it
        # is most often run in.
        return {"cycle": cycle, "launcher": launcher, "jobs": [],
                "totals": _totals([])}

    rows = _sacct(sorted(wanted))
    jobs = {}
    for row in rows:
        base, element, step = _split(row["JobID"])
        if base is None:
            continue
        key = (base, element)
        entry = jobs.setdefault(key, {
            "job_id": base,
            "member": element,
            "task": None,
            "cycle": cycle,
            "state": "",
            "exit_code": "",
            "elapsed": "",
            "total_cpu": "",
            "max_rss_kb": None,
            "max_rss_node": "",
            "alloc_cpus": None,
            "nodes": None,
            "req_mem": "",
            "start": "",
            "end": "",
        })
        if step is None:
            # The job row: state, timings and what was requested.
            entry["task"] = _task_of(row["JobName"])
            entry["state"] = row["State"].split()[0]
            entry["exit_code"] = row["ExitCode"]
            entry["elapsed"] = row["Elapsed"]
            entry["total_cpu"] = row["TotalCPU"]
            entry["req_mem"] = row["ReqMem"]
            entry["alloc_cpus"] = _int(row["AllocCPUS"])
            entry["nodes"] = _int(row["NNodes"])
            entry["start"] = row["Start"]
            entry["end"] = row["End"]
        else:
            # A step row, and the only place memory is ever reported.
            rss = _kilobytes(row["MaxRSS"])
            if rss is not None and (entry["max_rss_kb"] is None
                                    or rss > entry["max_rss_kb"]):
                entry["max_rss_kb"] = rss
                entry["max_rss_node"] = row["MaxRSSNode"]

    ordered = sorted(jobs.values(),
                     key=lambda j: (j["task"] or "", j["member"] is None,
                                    j["member"] or 0))
    return {
        "cycle": cycle,
        "launcher": launcher,
        "jobs": ordered,
        "totals": _totals(ordered),
    }


def _totals(jobs):
    """Enough to answer "what did this cycle cost" without reopening the file.

    `failed` and `unfinished` are separate because this task runs *inside* the
    cycle it is describing: it is an `afterany` leaf off the forecast, so most
    of its own cycle is still running when it reads the accounting, and every
    one of those rows is `RUNNING`. Folding them into `failed` on the usual
    "anything that is not COMPLETED" rule makes a perfectly healthy cycle report
    that four jobs out of five failed. The rule is right when a job has stopped
    and wrong before then, which is the whole difference.
    """
    return {
        "jobs": len(jobs),
        "failed": sum(1 for j in jobs
                      if j["state"] not in slurm.SUCCESS
                      and j["state"] not in slurm.ACTIVE),
        "unfinished": sum(1 for j in jobs if j["state"] in slurm.ACTIVE),
        "max_rss_kb": max((j["max_rss_kb"] or 0 for j in jobs), default=0),
        "core_seconds": round(sum(
            (j["alloc_cpus"] or 0) * _seconds(j["elapsed"]) for j in jobs
        ), 1),
    }


def _sacct(job_ids):
    result = slurm.run(
        ["sacct", "-n", "-P", "-o", ",".join(FIELDS), "-j",
         ",".join(str(i) for i in job_ids)],
        check=False,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) != len(FIELDS):
            continue
        rows.append(dict(zip(FIELDS, parts)))
    return rows


def _split(job_id):
    """"123_4.batch" -> (123, 4, "batch"). Step is None for the job row.

    `123_[5-20]`, the collapsed row for array elements that have not started, is
    dropped rather than parsed. Its member reads as absent, which would file it
    alongside the scalar jobs as a phantom entry in a state no element is
    actually in. Cycle stats are harvested by an `afterany` leaf while the rest
    of the cycle is still queued, so this row is present most times this runs.
    """
    head, _, step = job_id.partition(".")
    base, _, element = head.partition("_")
    if not base.isdigit():
        return None, None, None
    if element and not element.isdigit():
        return None, None, None
    member = int(element) if element else None
    return int(base), member, (step or None)


def _task_of(job_name):
    """`<exp>.<cycle>.<task>` back to the task, which may itself contain dots."""
    parts = job_name.split(".")
    return ".".join(parts[2:]) if len(parts) > 2 else job_name


def _kilobytes(value):
    match = _SIZE.match((value or "").strip())
    if not match:
        return None
    number, unit = match.groups()
    return int(float(number) * _SCALE[unit])


def _seconds(elapsed):
    """Slurm's `[DD-[HH:]]MM:SS` into seconds."""
    text = (elapsed or "").strip()
    if not text:
        return 0
    days, _, rest = text.rpartition("-")
    parts = [float(p) for p in rest.split(":") if p]
    total = 0.0
    for part in parts:
        total = total * 60 + part
    if days:
        total += int(days) * 86400
    return total


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def write(paths, cycle, launcher=""):
    """Harvest and commit `stats/<cycle>.json`. Returns the payload.

    **A harvest never shrinks a file it already wrote.** This whole module exists
    because `sacct` rows are purged on a site retention, so re-harvesting a cycle
    old enough to have been purged returns nothing for jobs that are no longer
    there: `harvest_cycle` builds its entries out of the rows it got back, so the
    payload would be short a job, or empty, and committing it would destroy the
    copy that was taken while the rows existed. The count is the test because it
    is the one thing a purge can only ever reduce.

    Refusing is safe in the other direction too. Nothing legitimately drops a job
    from a cycle: the ledger only grows, and a heal that resubmits adds job ids
    rather than replacing them.
    """
    payload = harvest_cycle(paths, cycle, launcher)
    target = paths.stats_file(cycle)
    kept = _existing(target)
    if kept is not None and len(kept.get("jobs", [])) > len(payload["jobs"]):
        return kept
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    temp.replace(target)
    return payload


def _existing(target):
    try:
        with open(target) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def refresh_stale(paths, before, launcher=""):
    """Re-harvest the earlier cycles whose recorded harvest is a stale snapshot.

    A cycle's harvest is taken by a task inside that cycle, so it necessarily
    reads its own siblings while they are still queued and records them as
    `unfinished`. Nothing used to revisit that, so a cycle that went on to finish
    cleanly kept a permanent count of jobs that had merely not started yet, and
    the stats page showed a wall of them for a run with nothing left to do.

    So each cycle's harvest repairs the ones behind it. By the time cycle *n*
    runs, the leaves of cycle *n-1* have finished and `sacct` has their real
    states. Only files that claim an unfinished job are re-read, so the steady
    cost is one extra query for the cycle just behind, and a cycle whose count
    reaches zero is never queried again.

    This is deliberately not a graph edge. Making `stats` wait on the leaves
    would make it wait on `post.state`, which hangs off the forecast by
    `aftercorr`, and a stranded element there never terminates: the harvest most
    wanted when a member failed would be the one thing that never ran. That is
    the same trap `post.state -> verify` was removed for, and `tasks.py` records
    it as an invariant.

    Returns the cycles it rewrote.
    """
    repaired = []
    for cycle in range(before):
        payload = _existing(paths.stats_file(cycle))
        if payload is None:
            continue
        if not payload.get("totals", {}).get("unfinished"):
            continue
        fresh = write(paths, cycle, launcher)
        if fresh["totals"] != payload["totals"]:
            repaired.append(cycle)
    return repaired
