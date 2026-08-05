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
        return {"cycle": cycle, "launcher": launcher, "jobs": []}

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
    """Enough to answer "what did this cycle cost" without reopening the file."""
    return {
        "jobs": len(jobs),
        "failed": sum(1 for j in jobs if j["state"] not in slurm.SUCCESS),
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
    """Harvest and commit `stats/<cycle>.json`. Returns the payload."""
    payload = harvest_cycle(paths, cycle, launcher)
    target = paths.stats_file(cycle)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    temp.replace(target)
    return payload
