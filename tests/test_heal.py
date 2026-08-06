"""Tier 0: the blast radius, the cancel, and the resubmission.

Three properties are load-bearing and each has a failure mode that only shows up
on a real scheduler, hours in:

- **the closure reaches forward across cycles**, because `forecast(n)` feeds
  `da(n+1)`, so one failed forecast invalidates everything in flight;
- **the closure stops at what was actually submitted**, because a failure
  strands the `submit` task and the next cycle does not exist yet;
- **live dependents are cancelled before anything is resubmitted**, because
  unsatisfiable dependents pend rather than die, and a successful replacement
  upstream does not release them.
"""

import json
import subprocess
from pathlib import Path

import pytest

from ackbar import heal, ledger, slurm, state
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import build_graph
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


@pytest.fixture
def env(tmp_path, config, monkeypatch):
    """A part-run experiment: a ledger, a fake queue, and a counting sbatch."""
    site = {"scratch_root": str(tmp_path / "s"),
            "output_root": str(tmp_path / "o"),
            "partition": "compute", "account": "ackbar"}
    paths = Paths.of(config, site).ensure()
    graph = build_graph(config)

    queued = {}
    accounted = {}
    cancelled = []
    issued = []

    def fake_run(command, check=True, stdin=None):
        if command[0] == "squeue":
            body = "\n".join(f"{k}|{v[0]}|{v[1]}" for k, v in queued.items())
        elif command[0] == "sacct" and "--json" in command:
            body = json.dumps({"jobs": []})
        elif command[0] == "sacct":
            body = "\n".join(f"{k}|{v}" for k, v in accounted.items())
        else:
            body = ""
        return subprocess.CompletedProcess(command, 0, body + "\n", "")

    def fake_sbatch(script, **kwargs):
        issued.append(dict(kwargs, script=Path(script).name))
        return 900 + len(issued)

    def fake_state_of(job_ids):
        """What the submitter asks before rebuilding an edge onto a job.

        Derived from the same fake accounting the status view reads, because a
        fixture that answers "active" unconditionally hides the one case the
        submitter exists for: an edge onto a job that has already finished.
        """
        out = {}
        for job_id in job_ids:
            seen = {v for k, v in accounted.items()
                    if k.partition("_")[0] == str(job_id)}
            if seen and seen <= set(slurm.SUCCESS):
                out[job_id] = "completed"
            elif seen - set(slurm.SUCCESS):
                out[job_id] = "failed"
            else:
                out[job_id] = "active"
        return out

    monkeypatch.setattr(slurm, "run", fake_run)
    monkeypatch.setattr(slurm, "sbatch", fake_sbatch)
    monkeypatch.setattr(slurm, "scancel", cancelled.extend)
    monkeypatch.setattr(slurm, "state_of", fake_state_of)

    for node in graph.nodes:
        target = paths.job_script(node.cycle, node.task)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/bash\n")

    return type("Env", (), {
        "config": config, "site": site, "paths": paths, "graph": graph,
        "queued": queued, "accounted": accounted, "cancelled": cancelled,
        "issued": issued,
    })


def submitted(env, cycle, base=100):
    """Pretend a whole cycle was submitted, one job id per node."""
    ids = {}
    for i, node in enumerate(n for n in env.graph.order()
                             if n.startswith(f"{cycle}.")):
        job_id = base + i
        task = node.split(".", 1)[1]
        members = next(n.members for n in env.graph.nodes if n.id == node)
        ledger.append(env.paths, cycle=cycle, task=task, members=members,
                      attempt=1, job_id=job_id, dependency="")
        ids[node] = job_id
    return ids


def complete(env, node_id, ids, members=None):
    """Mark a node COMPLETED in accounting, with its sentinel on disk."""
    cycle, _, task = node_id.partition(".")
    node = next(n for n in env.graph.nodes if n.id == node_id)
    for member in (members if members is not None else (node.members or (None,))):
        key = f"{ids[node_id]}_{member}" if member is not None else str(ids[node_id])
        env.accounted[key] = "COMPLETED"
        sentinel = env.paths.sentinel(int(cycle), task, member)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("{}")


def fail(env, node_id, ids, member=None):
    key = f"{ids[node_id]}_{member}" if member is not None else str(ids[node_id])
    env.accounted[key] = "FAILED"


def strand(env, node_id, ids, member=None):
    key = f"{ids[node_id]}_{member}" if member is not None else str(ids[node_id])
    env.queued[key] = ("PENDING", slurm.NEVER_SATISFIED)


# --- the plan ----------------------------------------------------------------

def test_nothing_broken_means_nothing_to_do(env):
    ids = submitted(env, 1)
    for node in ids:
        complete(env, node, ids)

    assert heal.plan(env.config, env.paths, env.graph) == ([], [], [])


def test_the_closure_is_everything_downstream_of_the_failure(env):
    ids = submitted(env, 1)
    for node in ("1.stage.obs", "1.da", "1.writeback"):
        complete(env, node, ids)
    fail(env, "1.forecast", ids, member=1)
    strand(env, "1.submit", ids)

    broken, closure, _ = heal.plan(env.config, env.paths, env.graph)
    assert broken == ["1.forecast", "1.submit"]
    assert set(closure) >= {"1.forecast", "1.post.state", "1.submit"}
    # Upstream is untouched: a heal reruns consequences, not causes.
    assert "1.da" not in closure
    assert "1.writeback" not in closure


def test_the_closure_stops_at_what_was_actually_submitted(env):
    """The next cycle does not exist yet, and must not be conjured into one.

    A failure strands `submit`, so cycle 2 was never submitted: those nodes have
    no job ids, nothing to cancel and no edges to rebuild, and the resubmitted
    `submit` will submit them in the ordinary way. Including them makes the heal
    try to submit a cycle whose own roots have never been submitted, which is
    exactly what the submitter refuses on.
    """
    ids = submitted(env, 1)
    fail(env, "1.forecast", ids, member=1)

    _, closure, _ = heal.plan(env.config, env.paths, env.graph)
    assert not [n for n in closure if n.startswith("2.")]


def test_the_closure_reaches_the_next_cycle_once_it_is_submitted(env):
    ids = submitted(env, 1)
    ids.update(submitted(env, 2, base=200))
    fail(env, "1.forecast", ids, member=1)

    _, closure, _ = heal.plan(env.config, env.paths, env.graph)
    assert "2.da" in closure and "2.forecast" in closure
    # Cycle 2's own root is not downstream of cycle 1's forecast.
    assert "2.stage.obs" not in closure


def test_only_live_jobs_are_cancelled(env):
    """Cancelling a COMPLETED job is not an error, it just makes the log lie."""
    ids = submitted(env, 1)
    members = next(n.members for n in env.graph.nodes if n.id == "1.forecast")
    for node in ("1.stage.obs", "1.da", "1.post.obs",
                 "1.writeback", "1.stats", "1.verify"):
        complete(env, node, ids)
    # Everything but member 1 got through, which is what a per-member fault
    # leaves behind and is why the node itself has nothing left to cancel.
    complete(env, "1.forecast", ids, members=members[1:])
    complete(env, "1.post.state", ids, members=members[1:])
    fail(env, "1.forecast", ids, member=members[0])
    strand(env, "1.post.state", ids, member=members[0])
    strand(env, "1.submit", ids)

    _, _, cancel = heal.plan(env.config, env.paths, env.graph)
    assert cancel == sorted({ids["1.post.state"], ids["1.submit"]})


# --- the heal ----------------------------------------------------------------

def test_cancelling_happens_before_resubmitting(env):
    """Reversed, there is a window with two jobs claiming one directory."""
    order = []
    ids = submitted(env, 1)
    fail(env, "1.forecast", ids, member=1)
    strand(env, "1.submit", ids)

    real_scancel = slurm.scancel

    def watch_cancel(job_ids):
        order.append(("cancel", tuple(job_ids)))
        real_scancel(job_ids)

    real_sbatch = slurm.sbatch

    def watch_sbatch(script, **kwargs):
        order.append(("sbatch", kwargs["job_name"]))
        return real_sbatch(script, **kwargs)

    slurm.scancel, slurm.sbatch = watch_cancel, watch_sbatch
    try:
        heal.heal(env.config, env.site, env.paths, graph=env.graph)
    finally:
        slurm.scancel, slurm.sbatch = real_scancel, real_sbatch

    assert order[0][0] == "cancel"
    assert all(step[0] == "sbatch" for step in order[1:])


def test_a_whole_node_is_resubmitted_not_the_failed_member(env):
    """Keeping index sets identical is what makes `aftercorr` rebuildable.

    Resubmitting `--array=1` of a twenty member forecast would be a smaller job
    and would leave the next array unable to pair with it elementwise, which
    Slurm reports as a job that pends forever rather than as an error.
    """
    ids = submitted(env, 1)
    fail(env, "1.forecast", ids, member=1)

    heal.heal(env.config, env.site, env.paths, graph=env.graph)

    forecast = next(j for j in env.issued if j["job_name"].endswith(".forecast"))
    assert forecast["array"] == slurm.array_spec(
        next(n.members for n in env.graph.nodes if n.id == "1.forecast")
    )


def test_a_dry_run_cancels_and_submits_nothing(env):
    ids = submitted(env, 1)
    fail(env, "1.forecast", ids, member=1)
    strand(env, "1.submit", ids)

    broken, closure, cancel, records = heal.heal(
        env.config, env.site, env.paths, graph=env.graph, dry_run=True)

    assert broken and closure and cancel
    assert records == []
    assert env.cancelled == [] and env.issued == []


def test_the_resubmission_is_recorded_as_a_new_attempt(env):
    ids = submitted(env, 1)
    fail(env, "1.forecast", ids, member=1)

    heal.heal(env.config, env.site, env.paths, graph=env.graph)

    rows = [r for r in ledger.read(env.paths) if r["task"] == "forecast"]
    assert len(rows) == 2
    assert rows[-1]["attempt"] == 2
    assert rows[-1]["job_id"] != ids["1.forecast"]


def test_a_completed_parent_outside_the_closure_loses_its_edge(env):
    """`afterok` edges expire, so a heal must not rebuild one on a finished job.

    Slurm accepts the dependency and then never satisfies it, which is a job
    that pends until somebody notices.
    """
    ids = submitted(env, 1)
    for node in ("1.stage.obs", "1.da", "1.writeback"):
        complete(env, node, ids)
    fail(env, "1.forecast", ids, member=1)

    heal.heal(env.config, env.site, env.paths, graph=env.graph)

    forecast = next(j for j in env.issued if j["job_name"].endswith(".forecast"))
    assert str(ids["1.writeback"]) not in (forecast["dependency"] or "")


def test_healing_twice_is_the_same_heal_again(env):
    ids = submitted(env, 1)
    fail(env, "1.forecast", ids, member=1)

    first = heal.heal(env.config, env.site, env.paths, graph=env.graph)
    # The replacement fails the same way, which is what a deterministic fault
    # does and is the case an operator actually hits.
    new_id = next(r["job_id"] for r in ledger.read(env.paths)
                  if r["task"] == "forecast" and r["attempt"] == 2)
    env.accounted[f"{new_id}_1"] = "FAILED"
    env.issued.clear()

    second = heal.heal(env.config, env.site, env.paths, graph=env.graph)
    assert second[0] == first[0]      # same broken set
    assert second[1] == first[1]      # same closure
    assert [j["job_name"] for j in env.issued]


# --- reporting ---------------------------------------------------------------

def test_a_genuine_failure_is_flagged_as_unlikely_to_heal(env):
    """Printed, never refused. The useful move is to fix the cause first."""
    ids = submitted(env, 1)
    fail(env, "1.forecast", ids, member=1)
    statuses = state.collect(env.paths, env.graph)

    assert heal.unhealable(statuses, ["1.forecast"]) == [("1.forecast", ["FAILED"])]


def test_a_stranded_node_is_not_flagged_as_unhealable(env):
    """It never ran at all, so there is nothing about it to fix."""
    ids = submitted(env, 1)
    strand(env, "1.submit", ids)
    statuses = state.collect(env.paths, env.graph)

    assert heal.unhealable(statuses, ["1.submit"]) == []


def test_describe_names_the_members_and_stops_counting_at_six(env):
    ids = submitted(env, 1)
    for member in range(1, 12):
        fail(env, "1.writeback", ids, member=member)
    statuses = state.collect(env.paths, env.graph)

    line, = heal.describe(statuses, ["1.writeback"])
    assert "mem001" in line and "and 5 more" in line
