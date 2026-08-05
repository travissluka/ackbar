"""Tier 0: the submitter, with Slurm replaced.

The submitter is a real program rather than an `sbatch` wrapper for one reason:
`afterok` edges expire. A dependency can only be created while the target job is
active or within `MinJobAge` after it ends, so every cross-cycle edge has to be
built against the upstream job's current state. Those three cases are what most
of this file is about.
"""

from pathlib import Path

import pytest

from ackbar import ledger, slurm, submit
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
    """A created experiment, a graph, and an sbatch that hands out job ids."""
    site = {"scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o"),
            "partition": "compute", "account": "ackbar"}
    paths = Paths.of(config, site).ensure()
    graph = build_graph(config)

    issued = []

    def sbatch(script, **kwargs):
        issued.append(dict(kwargs, script=Path(script).name))
        return 1000 + len(issued)

    monkeypatch.setattr(slurm, "sbatch", sbatch)
    monkeypatch.setattr(slurm, "state_of", lambda ids: {i: "active" for i in ids})

    # The emitter is tested separately; here the scripts only have to exist.
    for node in graph.nodes:
        target = paths.job_script(node.cycle, node.task)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/bash\n")

    return type("Env", (), {
        "config": config, "site": site, "paths": paths, "graph": graph,
        "issued": issued,
    })


def by_task(records):
    return {r["task"]: r for r in records}


def kwargs_for(env, task):
    return next(k for k in env.issued if k["job_name"].endswith(f".{task}"))


# --- the first cycle ---------------------------------------------------------

def test_a_whole_cycle_is_submitted_in_topological_order(env):
    records = submit.submit_cycle(env.config, env.site, env.paths, 1,
                                  graph=env.graph)
    tasks = [r["task"] for r in records]
    assert tasks.index("da") < tasks.index("recenter") < tasks.index("writeback")
    assert tasks.index("forecast") < tasks.index("submit")


def test_every_submission_lands_in_the_ledger(env):
    records = submit.submit_cycle(env.config, env.site, env.paths, 1,
                                  graph=env.graph)
    assert len(ledger.read(env.paths)) == len(records)
    assert ledger.job_id(env.paths, 1, "da") == by_task(records)["da"]["job_id"]


def test_a_member_task_is_an_array_over_its_index_set(env):
    submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph)
    assert kwargs_for(env, "forecast")["array"] == "1-20"
    # A member-level task is an array even when the set has one element;
    # emitting it as a scalar would silently invalidate every aftercorr edge.
    assert kwargs_for(env, "da")["array"] is None


def test_array_to_array_edges_are_elementwise(env):
    records = by_task(submit.submit_cycle(env.config, env.site, env.paths, 1,
                                          graph=env.graph))
    assert records["forecast"]["dependency"] == \
        f"aftercorr:{records['writeback']['job_id']}"


def test_a_leaf_tolerates_a_failed_member(env):
    # afterok on an array is all-or-nothing, which is wrong for the harvest:
    # it is exactly the task most wanted when a member has failed.
    records = by_task(submit.submit_cycle(env.config, env.site, env.paths, 1,
                                          graph=env.graph))
    assert records["stats"]["dependency"].startswith("afterany:")
    assert "afterok" not in records["stats"]["dependency"]


def test_the_partition_and_account_come_from_the_site(env):
    submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph)
    assert kwargs_for(env, "da")["partition"] == "compute"
    assert kwargs_for(env, "da")["account"] == "ackbar"


# --- cross-cycle edges, which are the reason this is a program ---------------

def _first_cycle(env):
    return by_task(submit.submit_cycle(env.config, env.site, env.paths, 1,
                                       graph=env.graph))


def test_a_still_running_upstream_keeps_its_edge(env, monkeypatch):
    first = _first_cycle(env)
    monkeypatch.setattr(slurm, "state_of", lambda ids: {i: "active" for i in ids})
    second = by_task(submit.submit_cycle(env.config, env.site, env.paths, 2,
                                         graph=env.graph))
    assert f"afterok:{first['forecast']['job_id']}" in second["da"]["dependency"]


def test_a_completed_upstream_loses_its_edge_as_redundant(env, monkeypatch):
    # Not missing: the artifact is on disk, and Slurm would reject an edge to a
    # job it has already forgotten. The edges inside the new cycle are
    # untouched by this.
    first = _first_cycle(env)
    monkeypatch.setattr(slurm, "state_of", lambda ids: {i: "completed" for i in ids})
    second = by_task(submit.submit_cycle(env.config, env.site, env.paths, 2,
                                         graph=env.graph))
    assert str(first["forecast"]["job_id"]) not in second["da"]["dependency"]
    assert second["da"]["dependency"] == \
        f"afterok:{second['stage.obs']['job_id']}"


def test_a_failed_upstream_stops_the_submission(env, monkeypatch):
    _first_cycle(env)
    monkeypatch.setattr(slurm, "state_of", lambda ids: {i: "failed" for i in ids})
    with pytest.raises(submit.SubmitError, match="failed"):
        submit.submit_cycle(env.config, env.site, env.paths, 2, graph=env.graph)


def test_an_upstream_slurm_has_forgotten_stops_the_submission(env, monkeypatch):
    _first_cycle(env)
    monkeypatch.setattr(slurm, "state_of", lambda ids: {i: "unknown" for i in ids})
    with pytest.raises(submit.SubmitError, match="unknown"):
        submit.submit_cycle(env.config, env.site, env.paths, 2, graph=env.graph)


def test_a_cycle_whose_parent_was_never_submitted_is_refused(env):
    with pytest.raises(submit.SubmitError, match="never been submitted"):
        submit.submit_cycle(env.config, env.site, env.paths, 2, graph=env.graph)


# --- the protections against submitting twice --------------------------------

def test_a_cycle_cannot_be_submitted_twice(env):
    submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph)
    with pytest.raises(submit.SubmitError, match="already submitted"):
        submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph)


def test_the_marker_does_not_block_healing_a_subgraph(env):
    # A heal resubmits part of a cycle that is already marked. Refusing that
    # would make the marker prevent the one operation it exists to make safe.
    submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph)
    records = submit.submit_cycle(env.config, env.site, env.paths, 1,
                                  graph=env.graph, tasks=["verify"])
    assert [r["task"] for r in records] == ["verify"]
    assert ledger.attempts(env.paths, 1, "verify") == 2


def test_the_halt_flag_stops_everything_before_any_sbatch(env):
    env.paths.halt_flag.write_text("")
    with pytest.raises(submit.SubmitError, match="HALT"):
        submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph)
    assert env.issued == []


def test_a_dry_run_writes_no_ledger_and_takes_no_marker(env):
    submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph,
                        dry_run=True)
    assert env.issued == []
    assert ledger.read(env.paths) == []
    assert not env.paths.submit_marker(1).exists()


def test_a_missing_job_script_is_caught_by_name(env):
    env.paths.job_script(1, "da").unlink()
    with pytest.raises(submit.SubmitError, match="ackbar create"):
        submit.submit_cycle(env.config, env.site, env.paths, 1, graph=env.graph)
