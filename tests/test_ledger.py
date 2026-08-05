"""Tier 0: the append-only submission record.

It holds identity and nothing else. The tests below are mostly about what it
must *not* do: update a record, lose the file to a torn write, or claim to know
an outcome.
"""

import pytest

from ackbar import ledger
from ackbar.paths import Paths


@pytest.fixture
def paths(tmp_path):
    return Paths.of(
        {"experiment": {"name": "e"}},
        {"scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o")},
    ).ensure()


def add(paths, cycle, task, job_id, members=(), dependency=None):
    return ledger.append(
        paths, cycle=cycle, task=task, members=members,
        attempt=ledger.attempts(paths, cycle, task) + 1,
        job_id=job_id, dependency=dependency,
    )


def test_a_record_carries_the_identity_slurm_will_forget(paths):
    record = add(paths, 1, "forecast", 42, members=(1, 2, 3), dependency="afterok:41")
    assert record["experiment"] == "e"
    assert record["members"] == [1, 2, 3]
    assert record["array"] is True
    assert record["dependency"] == "afterok:41"
    assert record["submitted"].endswith("+00:00")


def test_nothing_records_an_outcome(paths):
    # Slurm stays authoritative for state. A "status" field here would be a
    # second state database, and it would be wrong the moment a job ends.
    record = add(paths, 1, "da", 1)
    assert not set(record) & {"state", "status", "exit", "outcome"}


def test_a_resubmission_appends_rather_than_replaces(paths):
    add(paths, 1, "da", 10)
    add(paths, 1, "da", 11)
    assert ledger.attempts(paths, 1, "da") == 2
    assert len(ledger.read(paths)) == 2
    # Retry counters live here because they cannot live anywhere else: job
    # names are identical across attempts and sacct rows purge.
    assert [r["attempt"] for r in ledger.read(paths)] == [1, 2]


def test_the_latest_attempt_is_the_one_heal_and_cancel_mean(paths):
    add(paths, 1, "da", 10)
    add(paths, 1, "da", 11)
    assert ledger.job_id(paths, 1, "da") == 11


def test_an_unsubmitted_node_has_no_job_id(paths):
    assert ledger.job_id(paths, 3, "forecast") is None


def test_submitted_cycles_is_what_the_submitter_checks_itself_against(paths):
    add(paths, 1, "da", 1)
    add(paths, 2, "da", 2)
    assert ledger.submitted_cycles(paths) == [1, 2]


def test_a_torn_tail_costs_one_line_rather_than_the_file(paths):
    add(paths, 1, "da", 1)
    with open(paths.ledger_file, "a") as handle:
        handle.write('{"cycle": 2, "task": "for')
    # heal has to be able to run at exactly the moment the ledger was torn.
    assert len(ledger.read(paths)) == 1


def test_reading_a_ledger_that_does_not_exist_yet_is_empty_not_an_error(paths):
    assert ledger.read(paths) == []
