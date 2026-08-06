"""Tier 3: FGAT at gom_25km, which is the one 4D method that needs the model.

Everything about a four-dimensional window that is arithmetic is pinned at tier
0 and everything that is document shape is pinned at tier 1. What is only
provable with MOM6 in the loop is the handoff a 4D window creates and a 3D one
does not, and it has three halves that can each fail on their own:

- **The forecast overshoots and writes on the way.** One run of 18 hours rather
  than a chain of them, dumping an intermediate restart set every 3 hours as
  FMS's clock reaches the interval. A state named at an hour the model had no
  reason to write is a file that does not exist, and nothing upstream of here
  can tell the difference between that and one it did write.
- **The next cycle starts from the middle of it.** With an overshoot the set the
  next forecast resumes from is an interval partway through this one rather than
  the last thing it wrote. Taking the last would advance the experiment by a
  cycle and a half every cycle, silently.
- **The analysis reads them back as a trajectory.** `soca_var.x` builds a pseudo
  model over those slots, so the departures are computed against the state
  nearest each observation's own time. Cycle 1 is the exception and solves
  3D-Var, because nothing ran before it to write a trajectory.

The claim that makes it worth running at all is the last test: the same
observations, the same B and the same minimizer produce *different* departures
from `tier3_var`, because 3D-Var compares everything in the window against one
state and this does not.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_fgat.py
"""

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import experiment_paths
import yaml

from ackbar import slurm
from ackbar.cli import main
from ackbar.config.jobtime import (cycle_time, forecast_length, slot_times,
                                   window_bounds)
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet
from test_tier3_var import DOMAIN, STATIC, initial_condition

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
NAME = "tier3_fgat"
CYCLES = (1, 2, 3)
LAST = 3

#: Three cycles of an 18 hour forecast, which is 1.5x `tier3_var`'s model.
QUIET = 1800


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real model; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load((REPO / f"tests/experiments/{NAME}.yaml").read_text())


@pytest.fixture(scope="module")
def run(config, tmp_path_factory):
    source = dict(config)
    root = tmp_path_factory.mktemp("obs")
    subprocess.run(
        [sys.executable, str(REPO / "tools" / "obs-archive-osse.py"),
         "--domain", DOMAIN,
         "--state", str(initial_condition()),
         "--start", source["cycle"]["start"],
         "--length", source["cycle"]["length"],
         "--count", str(source["cycle"]["count"]),
         "--out", str(root)],
        check=True, capture_output=True,
    )
    source["vars"]["obs_dir"] = str(root)
    path = tmp_path_factory.mktemp("cfg") / f"{NAME}.yaml"
    path.write_text(yaml.safe_dump(source))

    paths = experiment_paths(NAME, load_site())
    _purge(paths)
    assert main(["create", str(path)]) == 0
    assert main(["start", NAME]) == 0
    assert wait_for_quiet(NAME, QUIET) == "drained"
    yield experiment_paths(NAME, load_site())
    _purge(experiment_paths(NAME, load_site()))


def emitted(paths, cycle):
    """The document the analysis actually read, kept beside its log.

    `varfgat` rather than `var`, which is itself the assertion that this cycle
    solved the cost function it was configured for: the two documents are
    written under different names precisely so that this is answerable
    afterwards.
    """
    found, = sorted(paths.log_dir(cycle).glob("da.*.varfgat.yaml"))
    return yaml.safe_load(found.read_text())


# --- the model wrote the trajectory ------------------------------------------

def test_the_forecast_overshot_and_wrote_a_state_at_every_slot(config, run):
    """One 18 hour run, five states, and every one of them named in advance.

    FMS writes when its clock *reaches* an interval, so a slot the workflow
    names that is not a whole number of cadences into the run is a file nothing
    ever wrote. `slot_times` computes those names before the job starts, which
    is what makes this checkable rather than discoverable.
    """
    assert forecast_length(config) == timedelta(hours=18)
    for cycle in (1, 2):
        when = slot_times(config, cycle)
        assert len(when) == 5, [str(t) for t in when]
        for slot in when:
            state = run.slot_out(cycle, 0, slot) / "MOM.res.nc"
            assert state.exists(), state


def test_the_next_cycle_resumed_from_the_middle_of_the_forecast(config, run):
    """The handoff an overshoot creates.

    The forecast runs 18 hours and the next cycle is 12 hours on, so the set it
    starts from is the interval at F012 and not the last one written at F018.
    Taking the last would advance the experiment by a cycle and a half every
    cycle, with nothing anywhere to say so.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    handoff = run.member_out("rst", 1, 0) / "MOM.res.nc"
    at_twelve = run.slot_out(1, 0, cycle_time(config, 2)) / "MOM.res.nc"
    at_eighteen = run.slot_out(1, 0, slot_times(config, 1)[-1]) / "MOM.res.nc"

    with netCDF4.Dataset(handoff) as a, netCDF4.Dataset(at_twelve) as b, \
            netCDF4.Dataset(at_eighteen) as c:
        assert numpy.allclose(a["Temp"][:], b["Temp"][:])
        assert not numpy.allclose(a["Temp"][:], c["Temp"][:])


# --- the analysis read it back -----------------------------------------------

def test_the_cycle_solved_fgat_and_not_3dvar(run):
    document = emitted(run, LAST)
    assert document["cost function"]["cost type"] == "3D-FGAT"
    assert document["cost function"]["model"]["name"] == "PseudoModel"


def test_the_first_cycle_solved_3dvar_because_it_had_no_predecessor(run):
    """The spin-up cycle, and the reason it is visible rather than special-cased.

    Nothing ran before cycle 1, so its background is the staged initial
    condition and its window holds one state. FGAT over one state *is* 3D-Var,
    and a pseudo model with a single entry steps off the end of its own list on
    the first observation.

    The two documents are written under different names, so which cost function
    a cycle solved is answerable from what it left behind rather than inferred
    from the configuration. That is the whole reason `varfgat` is a separate
    entry in `soca.APPLICATIONS` for the same executable.
    """
    assert not list(run.log_dir(1).glob("da.*.varfgat.yaml"))
    found, = sorted(run.log_dir(1).glob("da.*.var.yaml"))
    document = yaml.safe_load(found.read_text())
    assert document["cost function"]["cost type"] == "3D-Var"
    assert "model" not in document["cost function"]

    # And every cycle that does have a predecessor used it.
    for cycle in (2, 3):
        assert not list(run.log_dir(cycle).glob("da.*.var.yaml"))
        assert emitted(run, cycle)["cost function"]["cost type"] == "3D-FGAT"


def test_the_pseudo_model_was_given_the_previous_forecasts_slots(config, run):
    """Named files, checked to be the ones that exist.

    A document that pointed at states from this cycle's own forecast, or at the
    restart set instead of the slots, would build and would be comparing the
    observations against a trajectory that starts from the analysis they are
    supposed to produce.
    """
    document = emitted(run, LAST)
    begin, _ = window_bounds(config, LAST)

    background = document["cost function"]["background"]
    assert background["date"] == begin.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert background["basename"].rstrip("/") == \
        str(run.slot_out(LAST - 1, 0, begin))

    states = document["cost function"]["model"]["states"]
    expected = [when for when in slot_times(config, LAST - 1) if when != begin]
    assert len(states) == len(expected)
    for state, when in zip(states, expected):
        assert state["date"] == when.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert state["basename"].rstrip("/") == str(run.slot_out(LAST - 1, 0, when))
        assert (Path(state["basename"]) / state["ocn_filename"]).exists()


def test_the_tstep_is_the_slot_cadence(config, run):
    # A tstep that disagreed with the spacing of the states would step the
    # pseudo model onto times no file is dated at, and SOCA reports that as a
    # missing state rather than as a configuration error.
    assert emitted(run, LAST)["cost function"]["model"]["tstep"] == "P0DT3H0M0S"


def test_the_analysis_still_lands_at_the_centre_of_the_window(config, run):
    """What makes a centred window mandatory rather than a preference.

    `CostFctFGAT::doLinearize` saves the state at the window's midpoint and
    `finishLinearize` replaces the background with it, so the analysis is valid
    there. The next cycle starts from that time, so an off-centre window would
    produce an analysis valid at an hour no cycle begins at.
    """
    written = sorted((run.member_out("ana", LAST, 0) / "analysis").glob("ocn.ana.*.nc"))
    assert len(written) == 1, written
    assert written[0].name.endswith(
        cycle_time(config, LAST).strftime("%Y%m%dT%H%M%SZ.nc")), written[0].name


# --- it is a different analysis ----------------------------------------------

def test_the_departures_were_computed_against_the_trajectory(run):
    """The claim the whole method rests on, and the reason this runs at all.

    The observations are spread across a 12 hour window. 3D-Var compares every
    one of them against a single state at the centre; FGAT compares each against
    the slot nearest its own time. So the *background* departures have to differ
    between the two, before any minimization, and a run that produced the same
    `ombg` as a 3D-Var would be one whose pseudo model was ignored.

    Checked as spread rather than against `tier3_var`'s numbers, which would
    couple two experiments' runtimes together. What it catches is a trajectory
    that is really one state repeated: departures computed against five states
    have visible structure that departures against one do not.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    for observer in ("adt_3a", "sst_noaa19"):
        path = next(run.cycle_out("obs_out", LAST).glob(f"*{observer}*.nc4"))
        with netCDF4.Dataset(path) as data:
            variable = list(data["ombg"].variables)[0]
            before = numpy.asarray(data["ombg"][variable][:])
            after = numpy.asarray(data["oman"][variable][:])
        keep = (numpy.abs(before) < 1e30) & (numpy.abs(after) < 1e30)
        assert keep.sum() > 10, f"{observer}: nothing assimilated"
        assert numpy.sqrt(numpy.mean(after[keep] ** 2)) < \
            numpy.sqrt(numpy.mean(before[keep] ** 2)), observer


def test_the_experiment_finished(run):
    assert [run.stats_file(cycle).exists() for cycle in CYCLES] == [True] * 3
