"""Tier 3: what only a regional domain can be asked, at gom_25km.

The same experiment `test_tier3.py` runs, asked different questions. That file
takes the claims any domain would have to satisfy, restart continuity and
reproduce-after-kill. Nothing here repeats them. This file asserts the things
that are only true on a regional domain, and it is cheap enough to be worth its
own run rather than an ordering dependency on the other module's.

- **A configuration whose halves live apart still runs.** Text from the repository, data
  from the static root, and neither reachable from the other.
- **ACKBAR's overrides reach the model.** Not "the file was linked": the
  parameter MOM6 reports having used is ACKBAR's value and not the base's, which
  is the only evidence that `parameter_filename` was patched.
- **The open boundary is actually being forced.** A regional run whose OBC
  configuration was silently dropped still integrates, still writes restarts,
  and is wrong.

Opt in with `ACKBAR_TIER3=1`; needs `source site/activate.sh`, a built
`coupler_main`, the imported configuration, and the offline stages the experiment names.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_gom.py
"""

import os
from pathlib import Path

import pytest

from ackbar import slurm
from ackbar.cli import main
from ackbar.paths import Paths
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
EXPERIMENT = REPO / "tests" / "experiments" / "tier3_gom.yaml"
NAME = "tier3_gom"
#: The two halves of this domain's text: what the Gulf resolutions share, and
#: what makes this one 25 km. The tests below check that what the model read is
#: the second and not the first.
BASE = REPO / "config" / "model" / "mom6sis2" / "domain" / "gom" / "common"
DOMAIN = REPO / "config" / "model" / "mom6sis2" / "domain" / "gom" / "25km"

#: A cycle here is a six second forecast, so this is almost all bookkeeping and
#: scheduler latency. Still generous: what it guards against is sitting through
#: a hang, not a slow machine.
QUIET = 900


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
    paths = Paths.of({"experiment": {"name": NAME}}, load_site())
    _purge(paths)
    assert main(["create", str(EXPERIMENT)]) == 0
    assert main(["start", NAME]) == 0
    assert wait_for_quiet(NAME, QUIET) == "drained"
    yield paths
    _purge(paths)


#: The cycles whose restart sets still exist when the run is over. `cleanup`
#: keeps only what a forecast could still read, so with three cycles done that
#: is 2 and 3, and cycle 1's set is gone by the time anything here looks. Its
#: *logs* survive, which is why the parameter docs below are still asked of it.
KEPT = (2, 3)


def parameter_doc(paths, cycle, name="MOM_parameter_doc.all"):
    """What MOM6 says it actually ran with.

    The run directory is scratch and is deleted on success, so this reads the
    copy `keep_traces` leaves beside the job's log. Anything asserted from the
    configuration instead would be asserting that ACKBAR wrote what ACKBAR
    wrote.
    """
    matches = sorted((paths.sub("log") / str(cycle)).glob(f"forecast*.{name}"))
    assert len(matches) == 1, f"expected one {name} for cycle {cycle}, got {matches}"
    return matches[0].read_text()


# --- the two halves ----------------------------------------------------------

def test_the_run_reaches_text_and_data_that_do_not_contain_each_other(run):
    # The regional split: `base` has no INPUT at all, and the data directory has
    # no MOM_input. A staging step that assumed `base/INPUT` would fail here and
    # pass at om_1deg, where the two happen to coincide.
    assert (BASE / "MOM_input").exists()
    assert not (BASE / "INPUT").exists()
    # And the resolution's own directory holds overrides only, never a base file
    # that would shadow the shared one.
    assert not (DOMAIN / "MOM_input").exists()
    data = Path(os.environ["ACKBAR_STATIC_ROOT"]) / "domain" / "gom_25km" / "INPUT"
    assert (data / "ocean_hgrid.nc").exists()
    assert not (data / "MOM_input").exists()


def test_the_regional_model_cycles_and_leaves_restart_sets(run):
    for cycle in KEPT:
        assert (run.member_out("rst", cycle, 0) / "coupler.res").exists()
    # Every cycle ran, including the one whose restarts have since been reaped.
    assert sorted(p.name for p in run.sub("stats").glob("*.json")) == \
        ["1.json", "2.json", "3.json"]


# The restart handoff was checked here as well while this experiment and
# `test_tier3.py`'s were different domains. They are one experiment now, so the
# claim is asked once, there.


# --- the overrides reach the model -------------------------------------------

def test_the_bug_flags_ackbar_turns_off_are_off_in_the_model(run):
    """The point of the whole override mechanism.

    `ENABLE_BUGS_BY_DEFAULT = False` lives in a file ACKBAR links and ACKBAR
    puts in `parameter_filename`. If either half of that failed, the model would
    run exactly as well and quietly keep fourteen bug-retention flags on.
    """
    doc = parameter_doc(run, 1)
    assert "ENABLE_BUGS_BY_DEFAULT = False" in doc
    # Split at the comment: MOM6 writes `NAME = value ! [Boolean] default = X`,
    # so a naive search for "= True" matches the default of a flag that is set
    # False, starting with ENABLE_BUGS_BY_DEFAULT itself.
    enabled = [line for line in doc.splitlines()
               if "_BUG" in line and line.split("!")[0].strip().endswith("= True")]
    assert enabled == [], f"bug flags still enabled: {enabled}"


def test_the_shared_base_is_not_what_supplied_the_override(run):
    # The imported configuration ships no MOM_override of its own, so the
    # evidence that ACKBAR's was read is the value above rather than the
    # absence of another.
    # What this checks is the other half: that ACKBAR's file was listed at all.
    assert not (BASE / "MOM_override").exists()
    assert "ENABLE_BUGS_BY_DEFAULT" in (DOMAIN / "MOM_override").read_text()


def test_no_layout_was_configured_and_mom6_chose_a_sensible_one(run):
    # 8 PEs over 87 x 56. MOM6 picks 4x2, which is what a person would write,
    # and it picks it without being told.
    layout = parameter_doc(run, 1, "MOM_parameter_doc.layout")
    assert "LAYOUT = 4, 2" in layout


# --- the open boundary --------------------------------------------------------

def test_the_open_boundary_is_configured_in_the_model(run):
    """A regional run with its OBCs dropped integrates happily and is wrong.

    The failure this guards against is not hypothetical: SOCA's MOM6 refuses
    these very segments, and the workaround for that is a file that switches
    them off. If that file ever reached the forecast, the model would close its
    boundaries and nothing else would complain.
    """
    doc = parameter_doc(run, 1)
    assert "OBC_NUMBER_OF_SEGMENTS = 3" in doc
    assert "FLATHER" in doc


def test_the_soca_only_override_never_reaches_the_forecast(run):
    soca_only = (DOMAIN / "MOM_override.soca").read_text()
    assert "#override OBC_NUMBER_OF_SEGMENTS = 0" in soca_only
    assert "OBC_NUMBER_OF_SEGMENTS = 0" not in parameter_doc(run, 1)
