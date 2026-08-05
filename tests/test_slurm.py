"""Tier 0: the Slurm wrapper, with Slurm replaced.

Every call ACKBAR makes goes through `slurm.run`, so replacing that one
function is enough to exercise the parsing and the state logic on a machine
with no scheduler. What Slurm actually does when handed these arguments is
tier 2's problem, in `test_tier2.py`.
"""

import types

import pytest

from ackbar import slurm


def fake(stdout="", returncode=0, stderr=""):
    calls = []

    def run(command, check=True, stdin=None):
        calls.append(command)
        result = types.SimpleNamespace(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr,
        )
        if check and returncode:
            raise slurm.SlurmError(f"{' '.join(command)} exited {returncode}")
        return result

    run.calls = calls
    return run


# --- array specs -------------------------------------------------------------

@pytest.mark.parametrize("members,expected", [
    ((), None),
    ((0,), "0"),
    ((1, 2, 3), "1-3"),
    ((0, 1, 2, 5), "0-2,5"),
    ((3, 1, 2), "1-3"),
    ((1, 3, 5), "1,3,5"),
])
def test_an_index_set_becomes_compact_ranges(members, expected):
    assert slurm.array_spec(members) == expected


def test_a_throttle_is_appended_the_way_slurm_spells_it():
    assert slurm.array_spec((1, 2, 3), throttle=2) == "1-3%2"


# --- queue -------------------------------------------------------------------

def test_array_elements_collapse_onto_their_base_job(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake(
        "18_1|RUNNING|None\n18_2|PENDING|Resources\n19|PENDING|Dependency\n"
    ))
    assert slurm.queue() == {18: ("RUNNING", "None"), 19: ("PENDING", "Dependency")}


def test_a_job_id_that_has_left_the_queue_is_not_an_error(monkeypatch):
    # squeue exits nonzero for an unknown job id, which is the ordinary case
    # here rather than a failure.
    monkeypatch.setattr(slurm, "run", fake("", returncode=1))
    assert slurm.queue([1234]) == {}


def test_unparseable_lines_are_ignored_rather_than_fatal(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake("garbage\n\n7|RUNNING|None\n"))
    assert slurm.queue() == {7: ("RUNNING", "None")}


# --- accounting --------------------------------------------------------------

def _sacct(state, comment="ackbar:e:1:da"):
    return (
        '{"jobs": [{"job_id": 5, "name": "e.1.da", '
        f'"comment": {{"job": "{comment}"}}, '
        f'"state": {{"current": {state}, "reason": "None"}}}}]}}'
    )


def test_sacct_state_is_read_whether_it_is_a_list_or_a_string(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake(_sacct('["COMPLETED"]')))
    assert slurm.accounting([5])[5]["state"] == "COMPLETED"

    monkeypatch.setattr(slurm, "run", fake(_sacct('"COMPLETED"')))
    assert slurm.accounting([5])[5]["state"] == "COMPLETED"


def test_the_comment_carries_identity_back_out_of_accounting(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake(_sacct('["FAILED"]')))
    assert slurm.accounting([5])[5]["comment"] == "ackbar:e:1:da"


def test_unparseable_accounting_is_no_data_rather_than_a_crash(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake("not json"))
    assert slurm.accounting([5]) == {}


# --- the state the submitter actually asks for -------------------------------

def _states(monkeypatch, queue, accounting):
    monkeypatch.setattr(slurm, "queue", lambda ids=None: queue)
    monkeypatch.setattr(slurm, "accounting", lambda ids: accounting)


def test_a_job_in_the_queue_is_active(monkeypatch):
    _states(monkeypatch, {1: ("RUNNING", "None")}, {})
    assert slurm.state_of([1]) == {1: "active"}


def test_a_dependency_that_can_never_be_satisfied_is_terminally_failed(monkeypatch):
    # It never appears in sacct as failed at all, and unless the site sets
    # kill_invalid_depend it pends forever. The queue reason is the only place
    # this is visible.
    _states(monkeypatch, {1: ("PENDING", slurm.NEVER_SATISFIED)}, {})
    assert slurm.state_of([1]) == {1: "failed"}


def test_completed_and_failed_come_from_accounting(monkeypatch):
    _states(monkeypatch, {}, {
        1: {"state": "COMPLETED", "reason": "", "comment": "", "name": ""},
        2: {"state": "TIMEOUT", "reason": "", "comment": "", "name": ""},
    })
    assert slurm.state_of([1, 2]) == {1: "completed", 2: "failed"}


def test_a_state_nobody_recognizes_is_failure_not_success(monkeypatch):
    _states(monkeypatch, {}, {
        1: {"state": "SOMETHING_NEW", "reason": "", "comment": "", "name": ""},
    })
    assert slurm.state_of([1]) == {1: "failed"}


def test_a_job_slurm_has_forgotten_is_unknown(monkeypatch):
    # Purged by the site retention. Not the same as failed, and the submitter
    # treats it differently.
    _states(monkeypatch, {}, {})
    assert slurm.state_of([9999]) == {9999: "unknown"}


# --- sbatch ------------------------------------------------------------------

def test_identity_is_carried_twice_and_parsable_gives_the_id(monkeypatch):
    run = fake("4242;rancor\n")
    monkeypatch.setattr(slurm, "run", run)
    job_id = slurm.sbatch(
        "/tmp/x.sh", job_name="e.1.da", comment="ackbar:e:1:da",
        dependency="afterok:1", array="1-20", partition="compute",
        account="ackbar",
    )
    assert job_id == 4242
    command = run.calls[0]
    assert "--job-name=e.1.da" in command
    assert "--comment=ackbar:e:1:da" in command
    assert "--dependency=afterok:1" in command
    assert "--array=1-20" in command
    assert command[-1] == "/tmp/x.sh"
