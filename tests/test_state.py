"""Tier 0: the read-only status view, with Slurm replaced.

The thing under test is a *join*, and the reason it needs four sources rather
than one is the whole subject of this file. `squeue` is the only place a reason
is ever visible and it forgets a job seconds after it ends. `sacct` outlives the
queue and is purged on a site retention. The ledger is the only thing that knows
which job id was cycle 2's writeback. And the sentinel outlives all three.

Each source is faked separately so that a test can remove one and assert the
next one down still answers.
"""

import json
import subprocess
from pathlib import Path

import pytest

from ackbar import ledger, slurm, state
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


class Slurm:
    """A fake scheduler: what is queued, what accounting remembers.

    Replaces `slurm.run`, the single point every Slurm call goes through, so
    that the command construction under test is exercised rather than bypassed.
    """

    def __init__(self, monkeypatch):
        self.queued = {}      # "123_4" or "123" -> (state, reason)
        self.accounted = {}   # same key -> state
        self.commands = []
        monkeypatch.setattr(slurm, "run", self._run)

    def _run(self, command, check=True, stdin=None):
        self.commands.append(command)
        if command[0] == "squeue":
            body = "\n".join(f"{k}|{v[0]}|{v[1]}"
                             for k, v in self.queued.items())
        elif command[0] == "sacct" and "--json" in command:
            body = json.dumps({"jobs": []})
        elif command[0] == "sacct":
            body = "\n".join(f"{k}|{v}" for k, v in self.accounted.items())
        else:
            body = ""
        return subprocess.CompletedProcess(command, 0, body + "\n", "")


@pytest.fixture
def env(tmp_path, config, monkeypatch):
    site = {"scratch_root": str(tmp_path / "s"),
            "output_root": str(tmp_path / "o")}
    paths = Paths.of(config, site).ensure()
    graph = build_graph(config)
    fake = Slurm(monkeypatch)
    return type("Env", (), {"paths": paths, "graph": graph, "slurm": fake,
                            "config": config, "site": site})


def record(paths, cycle, task, job_id, members=(), attempt=1):
    ledger.append(paths, cycle=cycle, task=task, members=tuple(members),
                  attempt=attempt, job_id=job_id, dependency="")


# --- the join ----------------------------------------------------------------

def test_a_node_with_no_ledger_record_is_unsubmitted(env):
    statuses = state.collect(env.paths, env.graph)
    assert statuses["1.da"].summary == state.UNSUBMITTED
    assert statuses["1.da"].job_id is None
    assert statuses["1.da"].elements == {}


def test_squeue_reasons_are_the_only_source_of_stranded(env):
    record(env.paths, 1, "da", 500)
    env.slurm.queued["500"] = ("PENDING", slurm.NEVER_SATISFIED)

    status = state.collect(env.paths, env.graph)["1.da"]
    assert status.summary == state.STRANDED
    assert status.reasons[None] == slurm.NEVER_SATISFIED
    assert status.broken == (None,)


def test_a_plain_dependency_reason_is_merely_pending(env):
    """The distinction the whole design rests on.

    Only the *direct* dependent of a failed job reads
    `DependencyNeverSatisfied`. Anything further down reads plain `Dependency`,
    which is indistinguishable from a job whose parent is simply still queued,
    so it must not be reported as broken.
    """
    record(env.paths, 1, "da", 500)
    env.slurm.queued["500"] = ("PENDING", "Dependency")

    status = state.collect(env.paths, env.graph)["1.da"]
    assert status.summary == state.PENDING
    assert status.broken == ()


def test_accounting_answers_once_the_queue_has_forgotten(env):
    record(env.paths, 1, "da", 500)
    env.slurm.accounted["500"] = "FAILED"

    status = state.collect(env.paths, env.graph)["1.da"]
    assert status.summary == state.FAILED
    assert status.reasons[None] == "FAILED"


def test_the_sentinel_answers_once_accounting_has_been_purged(env):
    """`sacct` rows purge on a site retention; the experiment must survive it.

    The sentinel is written last and only on success, so its presence is a
    stronger statement than an accounting row anyway.
    """
    record(env.paths, 1, "da", 500)
    sentinel = env.paths.sentinel(1, "da")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("{}")

    assert state.collect(env.paths, env.graph)["1.da"].summary == state.COMPLETE


def test_a_purged_job_with_no_sentinel_is_broken_rather_than_pending(env):
    """The job was submitted, is in no queue, and left no proof it worked.

    This used to answer PENDING, which is the one thing it cannot be: a waiting
    job is in `squeue` by definition. PENDING is not terminal and not broken, so
    `status` showed a run that would never finish and `heal` had nothing to act
    on. It has to be FAILED, which is not a claim about how it ended but a claim
    that it did, without the artifact that proves it worked.
    """
    record(env.paths, 1, "da", 500)
    status = state.collect(env.paths, env.graph)["1.da"]
    assert status.summary == state.FAILED
    assert status.broken == (None,)
    assert "no sentinel" in status.reasons[None]


def test_the_queue_wins_over_accounting(env):
    """A requeued job is RUNNING again while `sacct` still says it failed."""
    record(env.paths, 1, "da", 500)
    env.slurm.accounted["500"] = "FAILED"
    env.slurm.queued["500"] = ("RUNNING", "None")

    assert state.collect(env.paths, env.graph)["1.da"].summary == state.RUNNING


# --- arrays ------------------------------------------------------------------

def test_array_elements_are_tracked_separately(env):
    record(env.paths, 1, "writeback", 600, members=range(1, 21))
    env.slurm.accounted["600_1"] = "FAILED"
    for member in range(2, 21):
        env.slurm.accounted[f"600_{member}"] = "COMPLETED"

    status = state.collect(env.paths, env.graph)["1.writeback"]
    assert status.broken == (1,)
    assert status.counts() == {state.FAILED: 1, state.COMPLETE: 19}
    assert status.summary == state.FAILED


def test_the_unstarted_remainder_of_an_array_is_expanded(env):
    """squeue collapses what has not started onto one row, "600_[5-20]".

    Left unexpanded, sixteen members would fall through to the sentinel check
    and be reported as pending-with-no-record, which reads as a heal being
    needed when nothing is wrong.
    """
    record(env.paths, 1, "writeback", 600, members=range(1, 21))
    env.slurm.queued["600_[5-20]"] = ("PENDING", "Resources")
    for member in range(1, 5):
        env.slurm.queued[f"600_{member}"] = ("RUNNING", "None")

    status = state.collect(env.paths, env.graph)["1.writeback"]
    assert status.counts() == {state.RUNNING: 4, state.PENDING: 16}


def test_one_stranded_element_makes_the_node_stranded(env):
    """A summary is the worst element, because "mostly fine" is not actionable.

    Every element is accounted for, which is what a real array looks like and
    what this test is about. Leaving eighteen of them modelled by nothing at all
    would make the answer come from the fallback for a job nothing remembers,
    and this would pass or fail for a reason that has nothing to do with
    severity ordering.
    """
    record(env.paths, 1, "writeback", 600, members=range(1, 21))
    env.slurm.queued["600_3"] = ("PENDING", slurm.NEVER_SATISFIED)
    for member in range(1, 21):
        if member != 3:
            env.slurm.accounted[f"600_{member}"] = "COMPLETED"

    status = state.collect(env.paths, env.graph)["1.writeback"]
    assert status.summary == state.STRANDED
    assert status.counts() == {state.STRANDED: 1, state.COMPLETE: 19}


def test_attempts_count_ledger_rows_not_the_latest_one(env):
    record(env.paths, 1, "da", 500)
    record(env.paths, 1, "da", 501, attempt=2)
    record(env.paths, 1, "da", 502, attempt=3)

    status = state.collect(env.paths, env.graph)["1.da"]
    assert status.attempt == 3
    assert status.job_id == 502  # the live one, not the first


# --- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("1-3", [1, 2, 3]),
    ("1-3,7", [1, 2, 3, 7]),
    ("5", [5]),
    ("1-4%2", [1, 2, 3, 4]),   # a throttle suffix is not an index
    ("", []),
])
def test_expand_reverses_slurms_array_notation(spec, expected):
    assert state._expand(spec) == expected


# --- overall verdict ---------------------------------------------------------

def test_a_drained_queue_with_a_failure_is_broken_not_finished(env):
    record(env.paths, 1, "da", 500)
    env.slurm.accounted["500"] = "FAILED"

    statuses = state.collect(env.paths, env.graph)
    assert state.finished(statuses, env.graph) == "broken"


def test_an_experiment_is_finished_when_its_last_cycle_is_complete(env):
    for node in env.graph.nodes:
        record(env.paths, node.cycle, node.task, 500 + node.cycle,
               members=node.members)
        for member in (node.members or (None,)):
            sentinel = env.paths.sentinel(node.cycle, node.task, member)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("{}")

    statuses = state.collect(env.paths, env.graph)
    assert state.finished(statuses, env.graph) == "finished"


def test_a_killed_submitter_is_stalled_rather_than_finished(env):
    """The case a drained queue cannot distinguish on its own.

    Nothing failed, nothing is queued, and the experiment stopped early. Calling
    that "finished" is how a run quietly delivers half an experiment.
    """
    record(env.paths, 1, "stage.obs", 500)
    sentinel = env.paths.sentinel(1, "stage.obs")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("{}")

    statuses = state.collect(env.paths, env.graph)
    assert state.finished(statuses, env.graph) == "stalled"
