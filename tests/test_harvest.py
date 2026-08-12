"""Tier 0: pulling one cycle's cost out of `sacct` before `sacct` forgets it.

The two things this has to get right are both properties of `sacct` rather than
of ACKBAR, and both are silent when wrong: memory is reported only on step rows,
so the obvious one-row-per-job query returns no memory at all; and job names are
not unique across experiments, so attribution has to come from the ledger.
"""

import json
import subprocess
from pathlib import Path

import pytest

from ackbar import harvest, ledger, slurm
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.paths import Paths

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"


@pytest.fixture(scope="module")
def config():
    layers = resolve_layers(EXPERIMENTS / "stub_letkf.yaml", LAYERS)
    keys = merge_keys(load_schema())
    return resolve(merge_layers(layers, keys),
                   {"scratch_root": "/scratch", "output_root": "/out"})


def row(job_id, name="", state="COMPLETED", exit_code="0:0", elapsed="00:01:00",
        total_cpu="00:01:00", ave_cpu="", max_rss="", node="", task="",
        req_mem="4G", cpus="8", nodes="1"):
    """One `sacct -P` line, in the column order harvest asks for."""
    values = {
        "JobID": job_id, "JobName": name, "State": state, "ExitCode": exit_code,
        "Elapsed": elapsed, "TotalCPU": total_cpu, "AveCPU": ave_cpu,
        "MaxRSS": max_rss, "MaxRSSNode": node, "MaxRSSTask": task,
        "ReqMem": req_mem, "AllocCPUS": cpus, "NNodes": nodes,
        "Submit": "", "Start": "", "End": "",
    }
    return "|".join(values[field] for field in harvest.FIELDS)


@pytest.fixture
def env(tmp_path, config, monkeypatch):
    site = {"scratch_root": str(tmp_path / "s"),
            "output_root": str(tmp_path / "o")}
    paths = Paths.of(config, site).ensure()
    lines = []
    asked = []

    def fake_run(command, check=True, stdin=None):
        asked.append(command)
        return subprocess.CompletedProcess(
            command, 0, "\n".join(lines) + "\n", "")

    monkeypatch.setattr(slurm, "run", fake_run)
    return type("Env", (), {"paths": paths, "lines": lines, "asked": asked})


def test_an_unsubmitted_cycle_harvests_to_an_empty_file(env):
    payload = harvest.harvest_cycle(env.paths, 3)
    assert payload["jobs"] == []
    assert env.asked == []      # and does not ask Slurm about nothing


def test_an_unsubmitted_cycle_still_carries_totals(env):
    """The empty answer has to be the same shape as every other one.

    `ackbar harvest` with no `--cycle` walks every cycle in the graph, and on a
    running experiment most of them have not been submitted. A short dict makes
    it raise `KeyError` at the first, which is the state the command is most
    often typed in, and leaves a `stats.json` on disk that no reader can parse
    the same way as its siblings.
    """
    payload = harvest.harvest_cycle(env.paths, 3)
    ran = harvest.harvest_cycle(env.paths, 1)
    assert payload["totals"].keys() == ran["totals"].keys()
    assert payload["totals"]["jobs"] == 0
    assert payload["totals"]["core_seconds"] == 0


def test_memory_comes_off_the_step_row_not_the_job_row(env):
    """The reason the query does not use `-X`.

    `MaxRSS` is blank on the job row and populated on the steps, so the natural
    one-row-per-job query returns a harvest with no memory in it at all, which
    reads as "nothing used any memory" rather than as a missing column.
    """
    ledger.append(env.paths, cycle=1, task="forecast", members=(1,), attempt=1,
                  job_id=77, dependency="")
    env.lines += [
        row("77", name="e.1.forecast", req_mem="4G", cpus="8"),
        row("77.batch", max_rss="1024K", node="n01"),
        row("77.0", max_rss="4096K", node="n02"),
        row("77.1", max_rss="2048K", node="n03"),
    ]

    job, = harvest.harvest_cycle(env.paths, 1)["jobs"]
    assert job["max_rss_kb"] == 4096
    assert job["max_rss_node"] == "n02"
    assert job["req_mem"] == "4G" and job["alloc_cpus"] == 8


def test_array_elements_are_kept_apart(env):
    ledger.append(env.paths, cycle=1, task="forecast", members=(1, 2), attempt=1,
                  job_id=80, dependency="")
    env.lines += [
        row("80_1", name="e.1.forecast"),
        row("80_1.batch", max_rss="100K"),
        row("80_2", name="e.1.forecast", state="FAILED", exit_code="1:0"),
        row("80_2.batch", max_rss="900K"),
    ]

    jobs = harvest.harvest_cycle(env.paths, 1)["jobs"]
    assert [j["member"] for j in jobs] == [1, 2]
    assert [j["max_rss_kb"] for j in jobs] == [100, 900]
    assert jobs[1]["state"] == "FAILED"


def test_attribution_comes_from_the_ledger_not_the_job_name(env):
    """Two experiments running at once produce identically named jobs.

    `sacct --name` would harvest the other run's rows into this run's stats and
    nothing downstream could tell.
    """
    ledger.append(env.paths, cycle=1, task="da", members=(), attempt=1,
                  job_id=90, dependency="")
    ledger.append(env.paths, cycle=2, task="da", members=(), attempt=1,
                  job_id=91, dependency="")

    harvest.harvest_cycle(env.paths, 1)
    command, = env.asked
    assert command[command.index("-j") + 1] == "90"


def test_every_attempt_is_kept(env):
    """A healed cycle cost what all of its attempts cost, not what the last did."""
    for attempt, job_id in enumerate((70, 71, 72), start=1):
        ledger.append(env.paths, cycle=1, task="forecast", members=(1,),
                      attempt=attempt, job_id=job_id, dependency="")
    env.lines += [
        row("70_1", name="e.1.forecast", state="FAILED"),
        row("71_1", name="e.1.forecast", state="FAILED"),
        row("72_1", name="e.1.forecast", state="COMPLETED"),
    ]

    payload = harvest.harvest_cycle(env.paths, 1)
    assert len(payload["jobs"]) == 3
    assert payload["totals"]["failed"] == 2


def test_a_dotted_task_name_survives_the_round_trip(env):
    ledger.append(env.paths, cycle=1, task="post.state", members=(1,),
                  attempt=1, job_id=95, dependency="")
    env.lines.append(row("95_1", name="exp.1.post.state"))

    job, = harvest.harvest_cycle(env.paths, 1)["jobs"]
    assert job["task"] == "post.state"


def test_totals_answer_what_the_cycle_cost(env):
    ledger.append(env.paths, cycle=1, task="forecast", members=(1, 2), attempt=1,
                  job_id=60, dependency="")
    env.lines += [
        row("60_1", name="e.1.forecast", elapsed="00:00:30", cpus="8"),
        row("60_1.batch", max_rss="500K"),
        row("60_2", name="e.1.forecast", elapsed="00:01:00", cpus="8"),
        row("60_2.batch", max_rss="1500K"),
    ]

    totals = harvest.harvest_cycle(env.paths, 1)["totals"]
    assert totals == {"jobs": 2, "failed": 0, "unfinished": 0,
                      "max_rss_kb": 1500, "core_seconds": 720.0}


def test_the_cycle_this_runs_inside_is_unfinished_rather_than_failed(env):
    """This task is an `afterany` leaf off its own cycle's forecast.

    So it reads the accounting while its siblings are still running, every time.
    Counting a RUNNING row as failed on the usual not-COMPLETED rule would make
    every healthy cycle report most of itself as a failure.
    """
    ledger.append(env.paths, cycle=1, task="forecast", members=(), attempt=1,
                  job_id=70, dependency="")
    ledger.append(env.paths, cycle=1, task="verify", members=(), attempt=1,
                  job_id=71, dependency="")
    ledger.append(env.paths, cycle=1, task="submit", members=(), attempt=1,
                  job_id=72, dependency="")
    env.lines += [
        row("70", name="e.1.forecast", state="COMPLETED"),
        row("71", name="e.1.verify", state="RUNNING"),
        row("72", name="e.1.submit", state="FAILED"),
    ]

    totals = harvest.harvest_cycle(env.paths, 1)["totals"]
    assert (totals["jobs"], totals["failed"], totals["unfinished"]) == (3, 1, 1)


def test_the_launcher_is_recorded_next_to_the_numbers(env):
    """`MaxRSS` means a different thing under each, by ranks-per-node.

    Under `srun` ranks are separate steps and it is per task; under `mpiexec`
    there is one `.batch` step whose cgroup covers every rank on the node.
    """
    payload = harvest.harvest_cycle(env.paths, 1, launcher="mpiexec")
    assert payload["launcher"] == "mpiexec"


def test_write_commits_the_file_by_rename(env):
    ledger.append(env.paths, cycle=1, task="da", members=(), attempt=1,
                  job_id=50, dependency="")
    env.lines.append(row("50", name="e.1.da"))

    harvest.write(env.paths, 1, launcher="srun")

    target = env.paths.stats_file(1)
    assert target.exists()
    assert not list(target.parent.glob("*.partial"))


def test_a_harvest_never_shrinks_the_file_it_already_wrote(env):
    """The purge this whole module exists to outrun.

    `sacct` drops rows on a site retention, and `harvest_cycle` builds its
    entries out of the rows it got back, so re-harvesting a cycle old enough to
    have been purged returns a payload short a job or empty. Committing that
    destroys the copy taken while the rows existed, which is the one thing here
    that cannot be recovered.
    """
    ledger.append(env.paths, cycle=1, task="da", members=(), attempt=1,
                  job_id=50, dependency="")
    ledger.append(env.paths, cycle=1, task="verify", members=(), attempt=1,
                  job_id=51, dependency="")
    env.lines += [row("50", name="e.1.da"), row("51", name="e.1.verify")]
    harvest.write(env.paths, 1)

    env.lines.clear()               # the retention window has closed
    kept = harvest.write(env.paths, 1)

    assert len(kept["jobs"]) == 2
    assert json.loads(env.paths.stats_file(1).read_text())["totals"]["jobs"] == 2


def test_a_stale_unfinished_count_is_repaired_by_a_later_cycle(env):
    """The bug the stats page showed: a wall of unfinished jobs on a done run.

    A cycle's harvest is taken by a task inside that cycle, so it reads its own
    leaves while they are still queued. Nothing revisited that, so a cycle that
    went on to finish cleanly kept the count forever.
    """
    ledger.append(env.paths, cycle=1, task="forecast", members=(), attempt=1,
                  job_id=60, dependency="")
    ledger.append(env.paths, cycle=1, task="verify", members=(), attempt=1,
                  job_id=61, dependency="")
    env.lines += [
        row("60", name="e.1.forecast", state="COMPLETED"),
        row("61", name="e.1.verify", state="PENDING"),
    ]
    assert harvest.write(env.paths, 1)["totals"]["unfinished"] == 1

    # Cycle 2 runs, by which time cycle 1's leaf has finished.
    env.lines[-1] = row("61", name="e.1.verify", state="COMPLETED")
    assert harvest.refresh_stale(env.paths, 2) == [1]

    totals = json.loads(env.paths.stats_file(1).read_text())["totals"]
    assert (totals["unfinished"], totals["failed"], totals["jobs"]) == (0, 0, 2)


def test_a_settled_cycle_is_never_queried_again(env):
    """What keeps the repair from costing a query per cycle for the whole run."""
    ledger.append(env.paths, cycle=1, task="da", members=(), attempt=1,
                  job_id=70, dependency="")
    env.lines.append(row("70", name="e.1.da", state="COMPLETED"))
    harvest.write(env.paths, 1)
    env.asked.clear()

    assert harvest.refresh_stale(env.paths, 5) == []
    assert env.asked == []


def test_a_repair_that_finds_nothing_leaves_the_count_alone(env):
    """A purge must not turn `unfinished` into `failed`.

    Both are wrong, but this one is wrong in the alarming direction: it invents
    failures in a run that had none, out of rows Slurm has merely forgotten.
    """
    ledger.append(env.paths, cycle=1, task="verify", members=(), attempt=1,
                  job_id=80, dependency="")
    env.lines.append(row("80", name="e.1.verify", state="PENDING"))
    harvest.write(env.paths, 1)

    env.lines.clear()
    assert harvest.refresh_stale(env.paths, 2) == []

    totals = json.loads(env.paths.stats_file(1).read_text())["totals"]
    assert (totals["unfinished"], totals["failed"]) == (1, 0)


def test_the_cycle_being_harvested_is_not_its_own_repair(env):
    """`refresh_stale` is exclusive: the current cycle is legitimately unfinished.

    Re-reading it in the same job would query `sacct` twice for the same rows and
    still record the same snapshot, because the siblings that make it unfinished
    are waiting on this very task's cycle to drain.
    """
    ledger.append(env.paths, cycle=1, task="verify", members=(), attempt=1,
                  job_id=90, dependency="")
    env.lines.append(row("90", name="e.1.verify", state="RUNNING"))
    harvest.write(env.paths, 1)
    env.asked.clear()

    assert harvest.refresh_stale(env.paths, 1) == []
    assert env.asked == []


@pytest.mark.parametrize("value,expected", [
    ("1024K", 1024),
    ("2M", 2048),
    ("1.5G", 1572864),
    ("512", 0),        # bytes, and rounding down is honest at this scale
    ("", None),
    ("n/a", None),
])
def test_memory_units_are_normalised_to_kilobytes(value, expected):
    assert harvest._kilobytes(value) == expected


@pytest.mark.parametrize("elapsed,expected", [
    ("00:00:30", 30),
    ("00:01:00", 60),
    ("01:00:00", 3600),
    ("2-01:00:00", 176400),
    ("", 0),
])
def test_slurms_elapsed_format_becomes_seconds(elapsed, expected):
    assert harvest._seconds(elapsed) == expected


@pytest.mark.parametrize("job_id,expected", [
    ("123", (123, None, None)),
    ("123_4", (123, 4, None)),
    ("123_4.batch", (123, 4, "batch")),
    ("123.extern", (123, None, "extern")),
    # The collapsed row for elements that have not started. Dropped, not parsed:
    # it has no member, and filing it as one would invent a scalar job.
    ("123_[5-20]", (None, None, None)),
])
def test_job_ids_split_into_job_element_and_step(job_id, expected):
    assert harvest._split(job_id) == expected


def test_the_unstarted_remainder_of_an_array_is_not_a_job(env):
    ledger.append(env.paths, cycle=1, task="forecast", members=(1, 2, 3),
                  attempt=1, job_id=40, dependency="")
    env.lines += [
        row("40_1", name="e.1.forecast"),
        row("40_[2-3]", name="e.1.forecast", state="PENDING"),
    ]

    jobs = harvest.harvest_cycle(env.paths, 1)["jobs"]
    assert [j["member"] for j in jobs] == [1]
