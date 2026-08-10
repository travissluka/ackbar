"""Tier 0: the emitted batch script.

The script is a header carrier, not generated code, so these tests are about
the `#SBATCH` block and about the two things deliberately *missing* from it.
"""

import time
from pathlib import Path
from unittest import mock

import pytest

from ackbar import emit
from ackbar.graph.model import Node
from ackbar.paths import Paths

CONFIG = {"experiment": {"name": "e"}, "model": {"name": "stub"},
          "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H"}}


@pytest.fixture
def paths(tmp_path):
    return Paths.of(CONFIG, {
        "scratch_root": str(tmp_path / "s"), "output_root": str(tmp_path / "o"),
    }).ensure()


def text(paths, node):
    return emit.script_text(
        CONFIG, paths, node, root="/repo", python="/venv/bin/python",
    )


ARRAY = Node(cycle=2, task="forecast", members=(1, 2, 3),
             resources={"ntasks": 8, "time": "00:30:00", "mem": "16G"})
SCALAR = Node(cycle=2, task="da", resources={"ntasks": 8})
SUBMITTER = Node(cycle=2, task="submit", resources={"ntasks": 1})


def test_identity_appears_twice_because_sacct_truncates_the_name(paths):
    script = text(paths, ARRAY)
    assert "#SBATCH --job-name=e.2.forecast" in script
    assert "#SBATCH --comment=ackbar:e:2:forecast" in script


def test_the_experiment_name_is_part_of_the_identity(paths):
    # Without it, sacct --name attributes rows to the wrong experiment and
    # --dependency=singleton silently couples unrelated ones.
    assert emit.job_name("other", 2, "forecast") == "other.2.forecast"
    assert emit.comment("other", 2, "forecast") == "ackbar:other:2:forecast"


def test_resources_are_written_and_never_defaulted(paths):
    script = text(paths, ARRAY)
    assert "#SBATCH --ntasks=8" in script
    assert "#SBATCH --time=00:30:00" in script
    assert "#SBATCH --mem=16G" in script


def test_a_task_with_no_resources_emits_none_rather_than_a_guess(paths):
    # validate step 6 is what catches this. Inventing a default here would turn
    # a caught error into a silently wrong allocation.
    script = text(paths, Node(cycle=1, task="verify"))
    assert "--ntasks" not in script
    assert "--mem" not in script


def test_the_array_and_the_dependency_are_not_in_the_script(paths):
    # They are the two values that differ between a first attempt and a healed
    # one, so they belong on the sbatch command line where a heal can change
    # them without rewriting history.
    script = text(paths, ARRAY)
    assert "--array" not in script
    assert "--dependency" not in script


def test_a_member_job_takes_its_member_from_the_array_index(paths):
    assert '--member "${SLURM_ARRAY_TASK_ID}"' in text(paths, ARRAY)


def test_a_scalar_job_is_given_no_member_at_all(paths):
    assert "--member" not in text(paths, SCALAR)


def test_only_the_submitter_refuses_requeue(paths):
    # A requeued submitter reruns its script from the beginning and submits a
    # whole cycle twice.
    assert "#SBATCH --no-requeue" in text(paths, SUBMITTER)
    assert "--no-requeue" not in text(paths, ARRAY)


def test_the_environment_comes_from_the_site_file(paths):
    script = text(paths, ARRAY)
    assert "export ACKBAR_ROOT=/repo" in script
    assert 'source "$ACKBAR_ROOT/site/activate.sh"' in script


def test_strict_mode_starts_after_the_toolchain_is_sourced(paths):
    # spack's own shell code is not written to survive `set -u`. Guarding
    # ACKBAR's lines is the point; failing inside somebody else's is not.
    script = text(paths, ARRAY)
    assert script.index("activate.sh") < script.index("set -euo pipefail")


def test_the_interpreter_is_frozen_rather_than_found_on_path(paths):
    assert '"/venv/bin/python" -m ackbar.cli run e' in text(paths, ARRAY)


def test_writing_a_script_does_not_create_the_cycle_directory(paths):
    # The log directory Slurm will not create is made by `submit`, immediately
    # before `sbatch`, and not here. Making it here meant `ackbar create` laid
    # down `run/<date>/log/` for every cycle at once, so a sixty cycle run
    # showed sixty run directories before it had run anything.
    target = emit.write_script(
        CONFIG, paths, ARRAY, root="/repo", python="/venv/bin/python",
    )
    assert target.exists()
    assert not Path(paths.job_log(2, "forecast", True)).parent.exists()
    assert target.stat().st_mode & 0o100  # executable


def test_the_script_is_a_pure_function_of_the_node(paths):
    # Healing regenerates it long after submission and must get the same
    # answer, so nothing here may consult the clock or the filesystem.
    #
    # Two calls microseconds apart do not show that: a second-resolution
    # timestamp in the script satisfies them and they still agree. Moving the
    # clock a year and asking again is what shows it, and it is the situation
    # healing is actually in.
    assert text(paths, ARRAY) == text(paths, ARRAY)

    before = text(paths, ARRAY)
    with mock.patch("time.time", return_value=time.time() + 31_536_000), \
         mock.patch("time.localtime",
                    return_value=time.localtime(time.time() + 31_536_000)), \
         mock.patch("time.gmtime",
                    return_value=time.gmtime(time.time() + 31_536_000)):
        assert text(paths, ARRAY) == before
