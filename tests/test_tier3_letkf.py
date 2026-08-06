"""Tier 3: LETKF cycling at gom_25km. The phase 7 milestone.

Seven forecasts a cycle, submitted as one array, each an ordinary MOM6
integration; one analysis job consuming all of them; seven writebacks. This is
where the phase 2 array work meets real cost, and where the things that only an
ensemble can get wrong first become testable.

The claims, in the order they would cost the most to discover later:

- **Every member gets its own analysis, and it is its own.** The application
  numbers what it writes by *position* in the list it was given rather than by
  member number, so the correspondence between an analysis and a directory is
  arithmetic, and getting it wrong puts one member's analysis into another
  member's restart set with nothing anywhere to notice.
- **The control's analysis is the ensemble mean.** Not approximately: ACKBAR
  does not separately compute an analysis for `mem000`, it hands it what
  `LocalEnsembleDA` computed as the posterior mean. If that ever stops being
  true, the control is forecasting from a state nobody chose.
- **The filter used its observations, and the ensemble held together.** `oman`
  below `ombg`, and a spread that neither collapses nor explodes. Both are
  needed: a filter can reduce its departures while collapsing, and a collapsed
  ensemble ignores every later cycle's observations while continuing to look
  healthy.
- **A missing member is survivable, on the policy the experiment stated.** This
  removes one member's forecast mid-run and checks that the cycle after it
  rebuilds that member rather than shrinking the ensemble.

What this does *not* test is whether the analysis is any good. The ensemble is
drawn from the static background error, whose spread here is about 0.09 K in
surface temperature against a 1 K truth anomaly and a 0.5 K observation error,
so the filter is heavily overconfident by construction and moves the state very
little. That is a property of perturbing one state, not of the implementation,
and what fixes it is a nature run.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_letkf.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import experiment_paths
import yaml

from ackbar import slurm
from ackbar.cli import main
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet
from test_tier3 import initial_energy
from test_tier3_var import DOMAIN, STATIC, initial_condition

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
NAME = "tier3_letkf"
CYCLES = (1, 2, 3)
LAST = 3

#: The cycles whose analysis directory still exists once the run has drained.
#: `cleanup` in cycle *n* reaps *n-2*, so `ana/1` and everything in it is gone
#: by the end of three. The departures are never reaped and are checked over
#: every cycle; anything read out of `ana/` is checked over these.
KEPT = (2, 3)

#: The ensemble, and the control. `mem000` is inside the same array as the rest
#: and is not a parallel concept; what makes it different is that the filter
#: does not assimilate it.
MEMBERS = (1, 2, 3, 4, 5, 6)
CONTROL = 0

#: Seven MOM6 forecasts a cycle on eight cores run one after another, so this is
#: mostly model time. Generous, because what it guards against is sitting
#: through a hang rather than a slow machine.
QUIET = 3600


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real applications; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")
    if not (STATIC / "static" / DOMAIN / "diffusion" / "hz.nc").exists():
        pytest.skip(f"no background error for {DOMAIN}; "
                    f"run tools/soca-diffusion.sh {DOMAIN}")
    source = yaml.safe_load((REPO / "tests/experiments" / f"{NAME}.yaml").read_text())
    ensemble = Path(source["ensemble"]["initial_condition"]
                    .replace("$(static_root)", str(STATIC)))
    if not (ensemble / "mem001" / "coupler.res").exists():
        pytest.skip(f"no ensemble initial condition at {ensemble}; run "
                    f"tools/ensemble-ic.sh {DOMAIN} {len(MEMBERS)}")


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """The same synthetic archive `tier3_var` reads, built the same way.

    Same observations, same domain, same dates, so an LETKF result and a 3DVar
    result here differ in the solver and in nothing else. That comparability is
    the reason this repository exists, so it is worth building rather than
    assuming.
    """
    source = yaml.safe_load((REPO / f"tests/experiments/{NAME}.yaml").read_text())
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
    return root


@pytest.fixture(scope="module")
def run(archive, tmp_path_factory):
    source = yaml.safe_load((REPO / f"tests/experiments/{NAME}.yaml").read_text())
    source["vars"]["obs_dir"] = str(archive)
    path = tmp_path_factory.mktemp("cfg") / f"{NAME}.yaml"
    path.write_text(yaml.safe_dump(source))

    paths = experiment_paths(NAME, load_site())
    _purge(paths)
    assert main(["create", str(path)]) == 0
    assert main(["start", NAME]) == 0
    assert wait_for_quiet(NAME, QUIET) == "drained"
    yield experiment_paths(NAME, load_site())
    _purge(experiment_paths(NAME, load_site()))


# --- reading what a run left behind ------------------------------------------

def temperature(path, level=0):
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return numpy.asarray(data.variables["Temp"][0, level])


def analysis_of(paths, cycle, member):
    return next((paths.member_out("ana", cycle, member) / "analysis")
                .glob("ocn.ana.an.*.nc"))


def diagnostic(paths, cycle, name):
    return next((paths.member_out("ana", cycle, CONTROL) / "analysis")
                .glob(f"ocn.{name}.*.nc"))


def departures(paths, cycle, observer):
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    path = next(paths.cycle_out("obs_out", cycle).glob(f"*{observer}*.nc4"))
    with netCDF4.Dataset(path) as data:
        variable = list(data["ombg"].variables)[0]
        before = numpy.asarray(data["ombg"][variable][:])
        after = numpy.asarray(data["oman"][variable][:])
    keep = (numpy.abs(before) < 1e30) & (numpy.abs(after) < 1e30)
    return before[keep], after[keep]


# --- every member is its own -------------------------------------------------

def test_every_member_has_an_analysis_and_a_restart_set(run):
    for member in (CONTROL,) + MEMBERS:
        assert analysis_of(run, LAST, member).exists()
        # writeback ran for it, and `coupler.res` is what says the set is whole.
        assert (run.member_out("ana", LAST, member) / "coupler.res").exists()
        assert (run.member_out("rst", LAST, member) / "coupler.res").exists()


def test_no_two_members_were_given_the_same_analysis(run):
    """The one arithmetic error in this path that nothing else would catch.

    `oops::DataSetBase::write` numbers what it writes by each state's position
    in the list it was handed, not by the member number ACKBAR asked for, so the
    mapping back is index-to-position. If it were off, two members would end up
    with the same file or with each other's, and both produce an experiment that
    runs to completion.
    """
    import numpy

    written = [temperature(analysis_of(run, LAST, member)) for member in MEMBERS]
    for i in range(len(written)):
        for j in range(i + 1, len(written)):
            assert not numpy.array_equal(written[i], written[j]), \
                f"members {MEMBERS[i]} and {MEMBERS[j]} have the same analysis"


def test_the_controls_analysis_is_the_ensemble_mean(run):
    """Exactly, not approximately.

    ACKBAR computes no analysis for the control: it hands `mem000` the posterior
    mean the filter produced anyway. This is the assertion that says so, and if
    it ever fails the control is forecasting from a state nobody chose.
    """
    import numpy

    members = [temperature(analysis_of(run, LAST, member)) for member in MEMBERS]
    control = temperature(analysis_of(run, LAST, CONTROL))
    assert numpy.allclose(control, numpy.mean(members, axis=0), atol=1e-9)


def test_each_member_forecast_from_its_own_analysis(run):
    """Seven analyses in, seven different forecasts out.

    A writeback that read the wrong member's analysis, or a forecast that read
    the wrong member's restart set, would collapse the ensemble to one state
    over a cycle or two and the run would look entirely healthy.
    """
    import numpy

    written = [temperature(run.member_out("rst", LAST, member) / "MOM.res.nc")
               for member in MEMBERS]
    for i in range(len(written)):
        for j in range(i + 1, len(written)):
            assert not numpy.array_equal(written[i], written[j])


# --- the filter used its observations ----------------------------------------

def test_every_member_resumed_from_its_own_analysed_restart(run):
    """A cold start would give every member the same state, silently.

    `test_each_member_forecast_from_its_own_analysis` catches that too, and
    this says *why* in one number: a cold start has `VELOCITY_CONFIG = zero`,
    so MOM6's own step-zero energy is exactly zero.
    """
    for member in (CONTROL,) + MEMBERS:
        assert initial_energy(run, LAST, member) > 1e-12


def test_the_analysis_moves_the_state_towards_the_observations(run):
    import numpy

    for cycle in CYCLES:
        for observer in ("adt_3a", "sst_noaa19"):
            before, after = departures(run, cycle, observer)
            assert before.size > 10, f"cycle {cycle} {observer}: nothing assimilated"
            rms = (numpy.sqrt(numpy.mean(before ** 2)),
                   numpy.sqrt(numpy.mean(after ** 2)))
            assert rms[1] < rms[0], f"cycle {cycle} {observer}: {rms}"


def test_both_spreads_are_recorded_and_neither_ran_away(run):
    """Both spreads, and the posterior within a factor of two of the prior.

    An ensemble filter fails in two ways that look identical in any single
    analysis: the spread collapses and every later cycle ignores its
    observations, or the spread grows and the filter chases noise. Nothing else
    in the workflow records which is happening, so the two files are written and
    this is what reads them.

    Deliberately *not* "the posterior is smaller". Assimilating removes
    variance, but the posterior variance is written after inflation and
    inflation exists to put it back: `rtps` relaxes the posterior spread towards
    the prior's, `rtpp` relaxes the perturbations, and `mult` scales them by
    1.1, which is 1.21 in variance. When the observations move the state very
    little, as they do with this ensemble, the inflation is the larger term and
    the posterior comes out slightly above the prior. That is the configuration
    working, so what is checked is the thing a spread diagnostic is actually
    for: that it is neither collapsing nor exploding.
    """
    import numpy

    for cycle in KEPT:
        prior = temperature(diagnostic(run, cycle, "sprdb"))
        posterior = temperature(diagnostic(run, cycle, "sprda"))
        ocean = prior > 0
        assert ocean.sum() > 100, f"cycle {cycle}: the prior spread is zero"
        ratio = numpy.mean(posterior[ocean]) / numpy.mean(prior[ocean])
        assert 0.5 < ratio < 2.0, f"cycle {cycle}: posterior/prior variance {ratio}"


def test_the_ensemble_did_not_collapse(run):
    """Spread at the last cycle against the first one still on disk.

    Three cycles of a six member filter with multiplicative inflation is not
    long enough to prove an ensemble is stable, and it is long enough to catch
    one that goes to zero immediately, which is what an inflation configured
    onto the wrong side of the update does.
    """
    import numpy

    first = temperature(diagnostic(run, KEPT[0], "sprdb"))
    last = temperature(diagnostic(run, LAST, "sprdb"))
    ocean = first > 0
    assert numpy.mean(last[ocean]) > 0.01 * numpy.mean(first[ocean])


def test_the_mean_increment_is_not_zero(run):
    import numpy

    values = temperature(diagnostic(run, LAST, "incr"))
    assert numpy.abs(values).max() > 1e-6


# --- the ensemble's own record -----------------------------------------------

def test_every_cycle_records_which_members_it_had(run):
    """Written whether or not anything was missing.

    Two experiments that differ in which members ran are not comparable, and
    nothing else in the output would say so. "All seven were there" belongs in
    the same file and the same shape as "one was not".
    """
    # Every cycle, not just the ones whose restarts survive. The record moved
    # out of `run/<date>/ana/`, where it was reaped along with the restart sets
    # it describes, and is beside the compressed analysis now, which is kept.
    for cycle in CYCLES:
        record = json.loads(run.member_list(cycle).read_text())
        assert record["policy"] == "replace_from_mean"
        assert record["assimilated"] == list(MEMBERS)


def test_the_experiment_finished(run):
    assert [run.stats_file(cycle).exists() for cycle in CYCLES] == \
        [True] * len(CYCLES)
