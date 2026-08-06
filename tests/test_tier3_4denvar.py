"""Tier 3: 4D-Ens-Var at gom_25km, where the covariance has a time dimension.

The second of the two new methods that earns a real model, and for a different
reason than `test_tier3_fgat`. There the model writes the trajectory the
analysis reads. Here the *ensemble* is a set of trajectories, one per member,
and there is no standing in for them: `model/persistence` writes nothing partway
through a forecast, so a persistence ensemble has no sub-window states for a
covariance to be sampled at.

What is only provable here:

- **Every member is read as a trajectory, and they line up.**
  `oops::DataSetBase` expands each member's `states` list and asserts that all
  members have the same number of times. A member short one slot fails inside
  the application, which nothing upstream of a real run can see.
- **The states named are the ones on disk.** Four members plus a control, each
  writing five restart sets a cycle, is where a path built from the wrong member
  index or the wrong cycle stops being a typo and becomes a covariance sampled
  from somebody else's forecast.
- **The document is accepted.** A cost type oops does not register, a background
  that is a state where a list was wanted, a `subwindow` that disagrees with the
  spacing of the states: each of those passes every golden and aborts here.

What this does *not* test is whether a 4D-Ens-Var analysis is better than a 3D
one. Four members forced by one atmosphere is a rank three covariance held
together by its localization, exactly as `test_tier3_letkf` says at length.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_4denvar.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import experiment_paths
import yaml

from ackbar import slurm
from ackbar.cli import main
from ackbar.config.jobtime import slot_times, window_bounds
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet
from test_tier3_var import DOMAIN, STATIC, initial_condition

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
NAME = "tier3_4denvar"
CYCLES = (1, 2, 3)
LAST = 3
MEMBERS = (1, 2, 3, 4)
CONTROL = 0

#: Five 18 hour forecasts a cycle, which is the 1.5x a four-dimensional window
#: costs times a small ensemble.
QUIET = 2400


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real model; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")
    if not (STATIC / "static" / DOMAIN / "diffusion" / "loc_hz.nc").exists():
        pytest.skip(f"no localization for {DOMAIN}; "
                    f"run tools/soca-diffusion.sh {DOMAIN}")
    source = yaml.safe_load((REPO / "tests/experiments" / f"{NAME}.yaml").read_text())
    ensemble = Path(source["ensemble"]["initial_condition"]
                    .replace("$(static_root)", str(STATIC)))
    if not (ensemble / "mem001" / "coupler.res").exists():
        pytest.skip(f"no ensemble initial condition at {ensemble}; run "
                    f"tools/ensemble-ic.sh {DOMAIN} 6")


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
    found, = sorted(paths.log_dir(cycle).glob("da.*.var4d.yaml"))
    return yaml.safe_load(found.read_text())


def test_the_cycle_solved_4d_ens_var(run):
    cost = emitted(run, LAST)["cost function"]
    assert cost["cost type"] == "4D-Ens-Var"
    assert cost["subwindow"] == "P0DT3H0M0S"
    assert cost["parallel subwindows"] is False


def test_the_first_cycle_solved_3dvar_because_it_had_no_predecessor(run):
    """The same spin-up rule FGAT has, and it matters more here.

    A 4D-Ens-Var over a single sub-window is a 3D-EnVar spelled at three times
    the cost, and the ensemble would have no time dimension to be sampled over.
    Cycle 1 reads the staged ensemble initial condition, one state per member.
    """
    assert not list(run.log_dir(1).glob("da.*.var4d.yaml"))
    found, = sorted(run.log_dir(1).glob("da.*.var.yaml"))
    assert yaml.safe_load(found.read_text())["cost function"]["cost type"] == \
        "3D-Var"


def test_the_background_is_one_state_per_subwindow(config, run):
    """A list, including the window's own start.

    Unlike the pseudo model's list in FGAT: nothing is being stepped here, and
    the first sub-window's observations are compared against the state at the
    start, so it has to be present.
    """
    cost = emitted(run, LAST)["cost function"]
    expected = slot_times(config, LAST - 1)
    dates = [state["date"] for state in cost["background"]["states"]]

    assert len(dates) == 5
    assert dates == [when.strftime("%Y-%m-%dT%H:%M:%SZ") for when in expected]
    assert dates[0] == window_bounds(config, LAST)[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    for state in cost["background"]["states"]:
        assert (Path(state["basename"]) / state["ocn_filename"]).exists()


def test_every_member_is_a_trajectory_over_the_same_times(config, run):
    """What oops asserts, asserted here where the message can name the member.

    A member short one slot is a covariance sampled over a shorter window than
    the rest, and `DataSetBase` catches it with an `ASSERT` rather than a
    message. Better to know which member and which time.
    """
    error = emitted(run, LAST)["cost function"]["background error"]
    members = error["members"]
    assert len(members) == len(MEMBERS)

    expected = [when.strftime("%Y-%m-%dT%H:%M:%SZ")
                for when in slot_times(config, LAST - 1)]
    for index, member in enumerate(members):
        dates = [state["date"] for state in member["states"]]
        assert dates == expected, f"member {index + 1}"


def test_each_members_trajectory_is_its_own_and_exists_on_disk(config, run):
    """The path is built from the member index and the previous cycle.

    Four members each writing five restart sets a cycle is where an index built
    one off, or a cycle built one off, stops being a typo and becomes a
    covariance sampled from a forecast that belongs to someone else.
    """
    members = emitted(run, LAST)["cost function"]["background error"]["members"]
    for number, member in zip(MEMBERS, members):
        for state, when in zip(member["states"], slot_times(config, LAST - 1)):
            expected = run.slot_out(LAST - 1, number, when)
            assert state["basename"].rstrip("/") == str(expected)
            assert (Path(state["basename"]) / state["ocn_filename"]).exists()


def test_the_covariance_had_no_static_component(run):
    # `covariance: ensemble`. A 4D window with a static B is refused at graph
    # build, so this is the only shape this experiment can have, and it is
    # asserted anyway: the layer configures a static B and leaves it unread.
    error = emitted(run, LAST)["cost function"]["background error"]
    assert error["covariance model"] == "ensemble"
    assert "components" not in error


def test_the_analysis_moved_the_state_towards_its_observations(run):
    """A rank three localized covariance is enough to fit something.

    Not a claim that it fits it well. What this catches is a covariance that is
    constructed and contributes nothing, which is what a trajectory read at the
    wrong times produces and which nothing else reports.
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
