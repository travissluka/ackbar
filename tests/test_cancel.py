"""Tier 0: what `ackbar cancel` actually stops.

Two properties, both of which fail silently on a real scheduler and only show
up as an experiment that carried on after someone stopped it:

- **the halt flag goes down before anything is cancelled**, because `submit` is
  an ordinary job in the graph and one that is running while cancel reads the
  queue arms the next cycle after cancel returns;
- **the job set is a union**, because a job somebody submitted by hand is
  nowhere in the ledger and `scancel` has no name glob to find it with.
"""

import json
import types
from pathlib import Path

import pytest

from ackbar import cli, ledger, slurm
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


@pytest.fixture
def env(tmp_path, config, monkeypatch):
    """A started experiment, a fake queue, and a record of what was cancelled."""
    site = {"scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o")}
    paths = Paths.of(config, site).ensure()
    monkeypatch.setattr(cli, "_frozen", lambda name: (config, site, paths))

    # Two jobs the ledger knows about; 11 has since left the queue.
    for job_id, task in ((10, "forecast"), (11, "da")):
        ledger.append(paths, cycle=1, task=task, members=(),
                      attempt=1, job_id=job_id, dependency="")

    state = {"queued": {10: ("RUNNING", "None")},
             "named": {}, "cancelled": [], "order": []}

    def fake_run(command, check=True, stdin=None):
        if command[0] == "squeue" and "%i|%j" in command:
            state["order"].append("scan")
            body = "\n".join(f"{k}|{v}" for k, v in state["named"].items())
        elif command[0] == "squeue":
            state["order"].append("scan")
            body = "\n".join(f"{k}|{v[0]}|{v[1]}"
                             for k, v in state["queued"].items())
        elif command[0] == "scancel":
            state["order"].append("scancel")
            state["cancelled"].extend(int(i) for i in command[1:])
            body = ""
        else:
            body = ""
        return types.SimpleNamespace(args=command, returncode=0,
                                     stdout=body, stderr="")

    monkeypatch.setattr(slurm, "run", fake_run)
    return paths, state


def cancel(paths, name="stub-letkf"):
    return cli.cmd_cancel(types.SimpleNamespace(name=name))


def test_the_halt_flag_is_set_before_anything_is_cancelled(env):
    # The race this exists to close: a `submit` job running right now sees the
    # flag and submits nothing, whether or not the scan caught it.
    paths, state = env
    state["named"] = {10: "stub-letkf.1.forecast"}
    cancel(paths)
    assert paths.halt_flag.exists()
    assert state["order"][0] != "scancel"


def test_the_halt_flag_survives_the_cancel(env):
    # An experiment that was cancelled is meant to stay stopped. Clearing the
    # flag here would let the next heal or a stray submitter restart it.
    paths, state = env
    cancel(paths)
    assert paths.halt_flag.exists()


def test_a_hand_submitted_job_is_cancelled_too(env):
    # Job 12 is in nobody's ledger. Under the old intersection this printed
    # "nothing of this experiment is in the queue" and left it running.
    paths, state = env
    state["queued"] = {12: ("RUNNING", "None")}
    state["named"] = {12: "stub-letkf.4.forecast"}
    cancel(paths)
    assert state["cancelled"] == [12]


def test_a_ledger_job_gone_from_the_queue_is_not_cancelled(env):
    # 11 is in the ledger and has left the queue. Handing it to scancel is
    # harmless but says the wrong thing about what was stopped.
    paths, state = env
    state["named"] = {}
    cancel(paths)
    assert state["cancelled"] == [10]


def test_the_two_sources_are_unioned_not_intersected(env):
    paths, state = env
    state["queued"] = {10: ("RUNNING", "None"), 12: ("PENDING", "Dependency")}
    state["named"] = {12: "stub-letkf.4.forecast"}
    cancel(paths)
    assert state["cancelled"] == [10, 12]


def test_nothing_live_still_sets_the_flag(env):
    # The flag is the half that stops the experiment; an empty queue is not a
    # reason to skip it, because a submitter can be between jobs.
    paths, state = env
    state["queued"] = {}
    state["named"] = {}
    assert cancel(paths) == 0
    assert paths.halt_flag.exists()
    assert state["cancelled"] == []
