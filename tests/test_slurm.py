"""Tier 0: the Slurm wrapper, with Slurm replaced.

Every call ACKBAR makes goes through `slurm.run`, so replacing that one
function is enough to exercise the parsing and the state logic on a machine
with no scheduler. What Slurm actually does when handed these arguments is
tier 2's problem, in `test_tier2.py`.
"""

import subprocess
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


# --- finding an experiment's jobs by name -------------------------------------

def test_a_hand_submitted_job_is_found_by_name(monkeypatch):
    # The whole reason this exists: nothing in the ledger knows about it.
    monkeypatch.setattr(slurm, "run", fake("41|osse.3.forecast\n"))
    assert slurm.named("osse") == {41}


def test_another_experiment_sharing_a_prefix_is_not_swept_up(monkeypatch):
    # `osse-truth` and `osse-truth.presmooth` both exist here. A prefix match
    # would cancel the second while cancelling the first, and the names differ
    # only after the dot the cycle number is supposed to follow.
    monkeypatch.setattr(slurm, "run", fake(
        "41|osse-truth.3.forecast\n"
        "42|osse-truth.presmooth.3.forecast\n"
        "43|osse-truth-v2.1.forecast\n"
    ))
    assert slurm.named("osse-truth") == {41}


def test_array_elements_collapse_here_too(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake(
        "50_0|e.1.forecast\n50_1|e.1.forecast\n"))
    assert slurm.named("e") == {50}


def test_an_empty_queue_is_not_an_error(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake("", returncode=1))
    assert slurm.named("e") == set()


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


def _array(*elements):
    """`sacct --json` for one array: an object per element, each with its own id.

    Only the element that happened to be allocated the base id is reported under
    it, which is what makes keying on `job_id` wrong.
    """
    jobs = ",".join(
        f'{{"job_id": {job_id}, "name": "e.1.forecast", '
        f'"array": {{"job_id": 4, "task_id": {task}}}, '
        f'"comment": {{"job": "ackbar:e:1:forecast"}}, '
        f'"state": {{"current": ["{state}"], "reason": "None"}}}}'
        for job_id, task, state in elements
    )
    return f'{{"jobs": [{jobs}]}}'


def _flaky(failures, then, returncode=1):
    """A `run` that fails *failures* times before answering with *then*."""
    calls = []

    def run(command, check=True, stdin=None):
        calls.append(command)
        if len(calls) <= failures:
            return types.SimpleNamespace(args=command, returncode=returncode,
                                         stdout="", stderr="down")
        return types.SimpleNamespace(args=command, returncode=0,
                                     stdout=then, stderr="")

    run.calls = calls
    return run


def test_a_blip_is_retried_rather_than_reported_as_a_missing_job(monkeypatch):
    # The failure this closes: a slurmdbd restart inside a submitter's window
    # made `state_of` answer `unknown`, which the submitter turns into a refusal
    # to submit, stopping an overnight experiment over five seconds.
    monkeypatch.setattr(slurm, "QUERY_BACKOFF", 0)
    run = _flaky(2, _array((4, 1, "COMPLETED")))
    monkeypatch.setattr(slurm, "run", run)
    assert slurm.accounting([4])[4]["state"] == "COMPLETED"
    assert len(run.calls) == 3


def test_undecodable_output_is_retried_too(monkeypatch):
    # A truncated response is the same kind of event as a refused one.
    monkeypatch.setattr(slurm, "QUERY_BACKOFF", 0)
    run = _flaky(1, _array((4, 1, "COMPLETED")), returncode=0)
    monkeypatch.setattr(slurm, "run", run)
    assert slurm.accounting([4])[4]["state"] == "COMPLETED"


def test_an_outage_is_an_error_rather_than_an_empty_answer(monkeypatch):
    # Silence and "these jobs do not exist" are different statements, and
    # returning {} made them one.
    monkeypatch.setattr(slurm, "QUERY_BACKOFF", 0)
    monkeypatch.setattr(slurm, "run", fake("", returncode=1))
    with pytest.raises(slurm.SlurmError, match="outage"):
        slurm.accounting([4])


def test_no_job_ids_is_still_not_a_query(monkeypatch):
    # An empty result for an empty question, without touching Slurm at all.
    def explode(*a, **k):
        raise AssertionError("should not have run a command")
    monkeypatch.setattr(slurm, "run", explode)
    assert slurm.accounting([]) == {}


def test_a_command_that_never_answers_is_not_waited_on_forever(monkeypatch):
    def hang(command, capture_output, text, input, timeout):
        raise subprocess.TimeoutExpired(command, timeout)
    monkeypatch.setattr(slurm.subprocess, "run", hang)
    with pytest.raises(slurm.SlurmError, match="did not answer"):
        slurm.run(["squeue"])


def test_an_array_is_keyed_on_its_base_and_not_on_whichever_element_got_that_id(
        monkeypatch):
    monkeypatch.setattr(slurm, "run", fake(
        _array((8, 1, "COMPLETED"), (9, 2, "COMPLETED"), (4, 3, "COMPLETED"))))
    assert set(slurm.accounting([4])) == {4}


def test_one_failed_element_makes_the_whole_array_failed(monkeypatch):
    """The element allocated the base id is not the array's answer.

    Keyed on the element, a member array whose base-id element succeeded reports
    `completed`, `submit._dependency` drops the edge as redundant rather than
    missing, and a consumer of a member that was never written is released.
    """
    monkeypatch.setattr(slurm, "run", fake(
        _array((8, 1, "FAILED"), (9, 2, "COMPLETED"), (4, 3, "COMPLETED"))))
    assert slurm.accounting([4])[4]["state"] == "FAILED"
    assert slurm.state_of([4]) == {4: "failed"}


def test_an_element_still_running_outranks_one_that_finished(monkeypatch):
    monkeypatch.setattr(slurm, "run", fake(
        _array((8, 1, "COMPLETED"), (4, 2, "RUNNING"))))
    assert slurm.state_of([4]) == {4: "active"}


def test_a_dependency_that_can_never_be_satisfied_is_not_hidden_by_a_sibling(
        monkeypatch):
    monkeypatch.setattr(slurm, "run", fake(
        f"4_1|PENDING|Priority\n4_2|PENDING|{slurm.NEVER_SATISFIED}\n"))
    assert slurm.queue([4])[4][1] == slurm.NEVER_SATISFIED


def test_unparseable_accounting_is_an_outage_rather_than_no_data(monkeypatch):
    """Not a traceback: `cli` catches SlurmError and prints it.

    This used to return {}, which reads as "Slurm has never heard of job 5" and
    reaches the submitter as a refusal to submit. Retried first, so a truncated
    response costs seconds rather than the run.
    """
    monkeypatch.setattr(slurm, "QUERY_BACKOFF", 0)
    run = fake("not json")
    monkeypatch.setattr(slurm, "run", run)
    with pytest.raises(slurm.SlurmError, match="outage"):
        slurm.accounting([5])
    assert len(run.calls) == slurm.QUERY_ATTEMPTS


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
