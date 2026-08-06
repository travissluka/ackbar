"""Tier 3: the long forecast and its verification on the real model, gom_25km.

`tier3_var` with `forecast.extended` switched on. Four claims, and the first is
the one that needs the real model rather than the stub:

- **The long forecast does not write where the cycling one does.** Both start
  from the same analysis, and `mom6sis2.commit` deletes whatever it was not
  asked to claim, so one shared directory is the cycling forecast's restart set
  being destroyed by the long one. The stub wrote a differently named file into
  that directory and the two coexisted, which is why this never fired.
- **Lead F012 is the cycling background, not a second copy of it.** Both
  forecasts start from the same set and MOM6 is deterministic, so integrating
  two days does not change the state at twelve hours. A symlink makes that
  equality structural; this checks the state it points at really is the same
  one, because a link asserts an equality rather than testing it.
- **The trajectory is evaluated four-dimensionally**, so an observation is
  compared against the state nearest its own time rather than against one state
  standing in for two days.
- **Its departures do not overwrite the cycling ones.** The observer layer's
  `obsdataout` is `obs_out/<T>/...` rendered at the cycle being evaluated, so an
  evaluation that left it alone would overwrite the cycling departures of every
  cycle the forecast reaches.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_fcst.py
"""

import os
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import experiment_paths

from ackbar import slurm
from ackbar.cli import main
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
EXPERIMENT = REPO / "tests" / "experiments" / "tier3_fcst.yaml"
NAME = "tier3_fcst"

#: Three twelve hour cycles plus two 2 day forecasts and their evaluation.
QUIET = 1200

#: The cycles `forecast.extended.every: P1D` selects off a PT12H cycle.
EXTENDED = (1, 3)

#: The extended cycle whose *raw* trajectory still exists once the run has
#: drained. `run/<init>/fcst/` is an intermediate and is reaped on the same
#: horizon as the restarts, so only the last one survives; nothing runs after it
#: to clean it.
LIVE = 3

CYCLE = timedelta(hours=12)
LEADS = tuple(timedelta(hours=h) for h in (12, 24, 36, 48))


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real model; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")


@pytest.fixture(scope="module")
def run():
    _purge(NAME)
    assert main(["create", str(EXPERIMENT)]) == 0
    assert main(["start", NAME]) == 0
    assert wait_for_quiet(NAME, QUIET) == "drained"
    yield experiment_paths(NAME, load_site())
    _purge(experiment_paths(NAME, load_site()))


# --- the collision the stub could not see ------------------------------------

def test_the_long_forecast_leaves_the_cycling_restarts_alone(run):
    """Both forecasts run from one analysis, and both outputs survive.

    The cycling forecast's restart set is what the next cycle starts from, so if
    the long forecast had claimed that directory this experiment would not have
    reached cycle 3 at all. Asserted directly anyway: a run that failed for a
    different reason should not be able to pass this by not getting here.

    `LIVE` rather than every extended cycle, because the raw trajectory is
    reaped like any other intermediate and cycle 1's is gone by the time the run
    drains. What outlives it is `fcst/`, which the next test checks.
    """
    cycling = run.member_out("rst", LIVE, 0)
    assert (cycling / "MOM.res.nc").exists(), cycling
    assert run.cycle_out("fcst", LIVE).is_dir()
    assert run.fcst_out(LIVE, 0, LEADS[-1]) != cycling


def test_the_trajectory_has_a_state_at_every_slot(run):
    # PT3H over two days, and the last of them is the set the run ended on.
    for lead in (timedelta(hours=3), timedelta(hours=24), timedelta(hours=48)):
        state = run.fcst_out(LIVE, 0, lead) / "MOM.res.nc"
        assert state.exists(), state


def test_the_raw_trajectory_is_reaped_and_the_products_are_not(run):
    """The whole reason the two live in different tiers.

    A trajectory is a full restart set per slot per member, sixteen of them for
    two days at a three hour cadence, and it exists to be reduced. Keeping it
    would make the long forecast the largest thing an experiment holds and the
    least useful.
    """
    assert not run.cycle_out("fcst", EXTENDED[0]).is_dir(), \
        "the first extended cycle's raw states should have been reaped"
    for lead in LEADS:
        assert run.fcst_product(EXTENDED[0], lead, 0).exists()


# --- the products ------------------------------------------------------------

def test_every_kept_lead_has_a_compressed_state(run):
    for cycle in EXTENDED:
        for lead in LEADS:
            product = run.fcst_product(cycle, lead, 0)
            assert product.exists(), product


def test_the_first_lead_is_the_cycling_background_and_not_a_copy(run):
    """`fcst/<init>/F012` is a symlink into `bkg/`, and points at the right file.

    The link is what makes the equality structural: two independent reductions
    of what is supposed to be the same state can quietly disagree, over
    rounding or a `FIELDS` change made on one path only. What a link cannot do
    is say *which* state, so that is what is checked here.
    """
    for cycle in EXTENDED:
        link = run.fcst_product(cycle, CYCLE, 0)
        assert link.is_symlink(), f"{link} is a copy, not a link"
        assert not link.readlink().is_absolute(), \
            "an absolute link does not survive the experiment being moved"
        assert link.resolve() == run.product("bkg", cycle + 1, 0).resolve()


def test_the_linked_lead_really_is_the_same_state(run):
    """The claim the symlink asserts, tested once against the model.

    Both forecasts start from the same analysis and MOM6 is deterministic, so
    integrating two days writes the same fields at twelve hours as integrating
    twelve. If `forecast.ext` ever gets its own start state the link becomes a
    lie with no other symptom, which is why this exists.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    # `LIVE`, whose raw trajectory has not been reaped. The twelve hour lead is
    # in `forecast.extended.slots` even though it is not kept as a product, so
    # the model does write it and the two states can actually be compared.
    cycle = LIVE
    raw = run.fcst_out(cycle, 0, CYCLE) / "MOM.res.nc"
    assert raw.exists(), raw
    with netCDF4.Dataset(raw) as long_run, \
            netCDF4.Dataset(run.member_out("rst", cycle, 0) / "MOM.res.nc") as cycling:
        for field in ("Temp", "Salt"):
            assert numpy.allclose(long_run[field][:], cycling[field][:]), field


# --- the departures ----------------------------------------------------------

def test_the_forecast_departures_are_written_under_the_initialization(run):
    section = run.fcst_obs(EXTENDED[0], timedelta(days=2), 0)
    written = sorted(p.name for p in section.glob("*.nc4"))
    assert written, f"nothing under {section}"


def test_the_cycling_departures_were_not_overwritten(run):
    """The failure this layout exists to prevent.

    The observer layer's `obsdataout` is `obs_out/<T>/...`, rendered at the
    cycle being evaluated. A long forecast covering four cycles that left that
    alone would write over the cycling departures of all four, and the symptom
    would be an O-B statistic quietly describing a two day forecast.
    """
    netCDF4 = pytest.importorskip("netCDF4")

    for cycle in (1, 2, 3):
        written = sorted(run.cycle_out("obs_out", cycle).glob("*.nc4"))
        assert written, f"cycle {cycle} has no departures at all"
        for path in written:
            with netCDF4.Dataset(path) as data:
                # `ombg` is the variational analysis's own output and the four
                # dimensional evaluation does not write it, so its presence is
                # what says this file is still the analysis's and not a long
                # forecast's that landed on top of it.
                assert "ombg" in data.groups, f"{path} was overwritten"
        assert run.obs_summary(cycle).exists()


def test_the_experiment_finished(run):
    assert [run.stats_file(cycle).exists() for cycle in (1, 2, 3)] == \
        [True, True, True]
