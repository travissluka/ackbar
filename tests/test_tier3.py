"""Tier 3: free run cycling on the real MOM6-SIS2, at gom_25km.

The phase 4 milestone, written down so that it is a test rather than an
afternoon. Two claims, and the second is the one that costs something to check:

- **N cycles are restart continuous.** Each cycle's restart set is stamped one
  cycle length later than the last, and the model got that time from the restart
  it was handed rather than from configuration.
- **A killed cycle resumes and reproduces.** Not merely finishes: the restarts a
  healed cycle writes are bit-identical to the ones the same experiment wrote
  when nothing went wrong. Anything weaker passes for a workflow that quietly
  restarts from the wrong state.

Neither claim is about the domain, so it is asked on the cheap one: a simulated
day costs 6 seconds at `gom_25km` against 178 at `om_1deg`. The experiment is
shared with `test_tier3_gom.py`, which asks it the things only a regional domain
can be asked.

Two full runs, and the model is seconds of it. Opt in with `ACKBAR_TIER3=1`;
needs `source site/activate.sh`, a built `coupler_main`, the imported
configuration, and the offline initial condition the experiment names.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3.py
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from ackbar import ledger, slurm
from ackbar.cli import main
from ackbar.config.jobtime import member_dir
from ackbar.duration import parse_duration, parse_instant
from ackbar.paths import Paths
from ackbar.site import load_site

from test_tier2 import _purge, _rows, wait_for_quiet

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
EXPERIMENT = REPO / "tests" / "experiments" / "tier3_gom.yaml"
NAME = "tier3_gom"

#: A cycle is one 12 hour gom_25km forecast on 8 PEs, six seconds of model plus
#: the bookkeeping and scheduler latency around it. Generous, because what this
#: guards against is sitting through a hang, not a slow machine.
QUIET = 900


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real model; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")


def paths_for():
    # `Paths` needs the experiment name and the site roots, and nothing else, so
    # this works before the experiment exists as well as after.
    return Paths.of({"experiment": {"name": NAME}}, load_site())


def digests(paths, cycles):
    """md5 per restart file, for the cycles that still exist.

    Cleanup deletes as it goes, so this is asked of the cycles the experiment is
    still holding rather than of all of them, and both runs are asked the same
    question.
    """
    out = {}
    for cycle in cycles:
        directory = paths.member_out("rst", cycle, 0)
        for entry in sorted(directory.glob("*.nc")):
            out[f"{cycle}/{entry.name}"] = hashlib.md5(
                entry.read_bytes()).hexdigest()
    return out


def cycle_through(kill_cycle=None):
    """Create, start, and see it through, optionally killing one forecast."""
    paths = paths_for()
    _purge(paths)
    assert main(["create", str(EXPERIMENT)]) == 0
    assert main(["start", NAME]) == 0

    if kill_cycle is not None:
        _kill_mid_forecast(kill_cycle)
        assert wait_for_quiet(NAME, QUIET) in ("stuck", "drained")
        assert main(["heal", NAME]) == 0

    assert wait_for_quiet(NAME, QUIET) == "drained"
    return paths_for()


def _kill_mid_forecast(cycle, member=0, timeout=300):
    """Cancel the forecast once it is doing work, and before it is finished.

    Waiting a fixed number of seconds does not survive a change of domain: what
    was mid-integration at `om_1deg` is minutes after the restarts were written
    at `gom_25km`, and the kill then lands on a job that has already succeeded.
    The test does not fail *there*, it fails two assertions later with nothing
    having been healed, which is a long way from the cause.

    So this waits on the model instead of on a clock. `model.log` is MOM6's own
    stdout, so its first byte means the attempt is executing, and it is written
    a whole initialization and integration before any restart is.
    """
    job = _running_forecast(cycle)
    log = paths_for().scratch(cycle, "forecast", member) / "model.log"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.exists() and log.stat().st_size > 0:
            slurm.scancel([job])
            return
        time.sleep(0.1)
    # Scratch is deleted on success, so the other way to arrive here is a
    # forecast that finished before this loop ever saw it.
    pytest.fail(f"cycle {cycle}'s forecast never wrote {log}: {_rows(NAME)}")


def _running_forecast(cycle, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["squeue", "-h", "-n", f"{NAME}.{cycle}.forecast", "-t", "RUNNING",
             "-o", "%i"], capture_output=True, text=True,
        )
        if result.stdout.strip():
            return result.stdout.strip()
        time.sleep(5)
    pytest.fail(f"cycle {cycle}'s forecast never started: {_rows(NAME)}")


@pytest.fixture(scope="module")
def both_runs():
    """The same experiment twice: once undisturbed, once killed and healed."""
    clean = cycle_through()
    reference = digests(clean, (2, 3))
    stats = sorted(p.name for p in clean.sub("stats").glob("*.json"))
    kept = sorted(p.name for p in clean.sub("rst").iterdir())

    healed = cycle_through(kill_cycle=2)
    result = {"reference": reference, "stats": stats, "kept": kept,
              "healed": digests(healed, (2, 3)), "paths": healed}
    yield result
    _purge(healed)


# --- restart continuity ------------------------------------------------------

def test_every_cycle_hands_its_restart_set_to_the_next(both_runs):
    paths = both_runs["paths"]
    for cycle in (2, 3):
        # Line 3 of coupler.res is the model time the set is valid at, and it is
        # what the next cycle resumes from. `INPUT/coupler.res` is a hardcoded
        # path inside coupler_main, so this is also the only evidence that the
        # handoff went through the file rather than through the namelist.
        line = (paths.member_out("rst", cycle, 0) / "coupler.res"
                ).read_text().splitlines()[2].split()[:6]
        assert [int(v) for v in line] == _expected_time(cycle)


def _expected_time(cycle):
    """`cycle.start` plus one cycle length each, read from the experiment.

    Derived rather than written down, so that retiming the experiment does not
    leave a date here that quietly still passes for the wrong reason.
    """
    source = yaml.safe_load(EXPERIMENT.read_text())["cycle"]
    at = (parse_instant(source["start"])
          + cycle * parse_duration(source["length"]))
    return [at.year, at.month, at.day, at.hour, at.minute, at.second]


def test_the_restart_sets_are_not_all_the_same_state(both_runs):
    # The check above passes for a workflow that copies the initial condition
    # forward and stamps it, which is exactly what a broken handoff looks like.
    digest = both_runs["healed"]
    assert digest["2/MOM.res.nc"] != digest["3/MOM.res.nc"]


def initial_energy(paths, cycle, member=0):
    """The kinetic energy MOM6 reports for its own step zero.

    Line 3 of `ocean.stats` is the state the model started from, before it took
    a step, and `En` is a global integral of the velocity field.
    """
    stem = f"forecast.{member_dir(member)}"
    written = sorted((paths.sub("log") / str(cycle)).glob(f"{stem}*.ocean.stats"))
    assert written, f"cycle {cycle} left no ocean.stats"
    line = written[-1].read_text().splitlines()[2]
    return float(line.split("En")[1].split(",")[0])


def test_the_model_resumed_rather_than_starting_over(both_runs):
    """The failure the two checks above cannot see, and it costs everything.

    `MOM_restart::determine_is_new_run` reads one character of
    `input_filename`, and stock MOM6-examples cases ship `'n'`, meaning a new
    run. MOM6 then initializes from `INIT_LAYERS_FROM_Z_FILE` and never opens
    `INPUT/MOM.res.nc`: every cycle integrates the same cold start from a
    different date, so consecutive restart sets still *differ* and the handoff
    still looks correct in `coupler.res`. Everything upstream of the model is
    discarded, including an analysis.

    A cold start has `VELOCITY_CONFIG = zero`, so the energy MOM6 reports for
    its own step zero is exactly that: zero. A resumed run's is not.
    """
    paths = both_runs["paths"]
    for cycle in (2, 3):
        assert initial_energy(paths, cycle) > 1e-12


def test_cleanup_keeps_only_what_a_forecast_can_still_read(both_runs):
    # Cycle n's forecast reads n-1, so with the run finished at cycle 3 the sets
    # worth keeping are 2 and 3. Cheap here, but the same rule runs at OM4_025,
    # where a restart set is gigabytes and this is not tidiness.
    assert both_runs["kept"] == ["2", "3"]


def test_every_cycle_leaves_a_harvest(both_runs):
    assert both_runs["stats"] == ["1.json", "2.json", "3.json"]


# --- resuming ----------------------------------------------------------------

def test_a_killed_cycle_reproduces_rather_than_merely_finishing(both_runs):
    """The claim phase 4 is finished on.

    A workflow that resumed from the analysis instead of the background, or from
    a half-written restart, or from the initial condition again, would finish
    here and be wrong. Only the bytes say which happened.
    """
    assert both_runs["healed"] == both_runs["reference"]
    assert both_runs["reference"], "nothing was compared"


def test_the_healed_cycle_really_did_run_a_second_time(both_runs):
    """Otherwise the test above passes because nothing was rerun at all."""
    paths = both_runs["paths"]
    attempts = [r for r in ledger.read(paths)
                if r["cycle"] == 2 and r["task"] == "forecast"]
    assert len(attempts) == 2
    assert json.loads(paths.sentinel(2, "forecast", 0).read_text())["cycle"] == 2
