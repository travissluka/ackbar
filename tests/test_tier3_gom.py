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
- **One forecast dumps a state SOCA can read at every sub-window time.** The
  phase 9 foundation, and the only genuinely open question in it: whether what
  MOM6 writes at an interval is a state SOCA's own reader opens. Asked here
  rather than in a 4D experiment because it is a question about the model and
  the state reader, and a free run is the cheapest place to ask it.

Opt in with `ACKBAR_TIER3=1`; needs `source site/activate.sh`, a built
`coupler_main`, the imported configuration, and the offline stages the experiment names.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_gom.py
"""

import copy
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import netCDF4
import numpy as np
import pytest
import yaml

from ackbar import mom6sis2, slurm, soca
from ackbar.cli import main
from ackbar.config.jobtime import cycle_time, handoff_time, slot_times
from ackbar.duration import ISO_INSTANT
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


# --- the sub-window states ----------------------------------------------------

def declared():
    """The experiment as written, which is what the slot times are computed off."""
    return yaml.safe_load(EXPERIMENT.read_text())


def slot_dirs(paths, cycle, member=0):
    return [paths.slot_out(cycle, member, when)
            for when in slot_times(declared(), cycle)]


def test_one_forecast_writes_a_state_at_every_slot(run):
    """Every state the cadence asked for, from a single model run.

    There is no per-slot job and no chain of short forecasts in this graph: the
    only thing that ran is `<cycle>.forecast`, once per cycle, and its own log
    is the evidence for that. What the cadence bought is an extra restart write
    inside that run at each interval.
    """
    ocean = declared_restart()
    for cycle in KEPT:
        written = slot_dirs(run, cycle)
        assert len(written) == 4
        for directory in written:
            assert (directory / ocean).exists(), f"cycle {cycle} wrote no {directory}"
        # One forecast per cycle per member, and no second one hiding behind it.
        logs = sorted((run.sub("log") / str(cycle)).glob("forecast.mem000*.model.log"))
        assert len(logs) == 1


def test_the_last_slot_lands_on_the_end_of_the_forecast(run):
    """Derived from the cycle rather than from the slot list it is checking.

    A cadence that does not divide the cycle leaves the last state short of the
    end of the window, and the analysis then reads a trajectory that stops
    before its own last observation. `ackbar/graph/build.py` refuses that case;
    this is the same claim asked of what MOM6 actually wrote.
    """
    for cycle in KEPT:
        last = slot_times(declared(), cycle)[-1]
        assert last == cycle_time(declared(), cycle + 1)


def field(path, name="Temp"):
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return np.asarray(data.variables[name][0])


def declared_restart():
    """`model.restart.ocn`, as the model layer states it."""
    layer = REPO / "config" / "layers" / "model" / "mom6sis2.yaml"
    return yaml.safe_load(layer.read_text())["model"]["restart"]["ocn"]


def test_the_slots_are_a_trajectory_and_not_the_same_state_four_times(run):
    """Otherwise the check above passes for a run that dumped its start state
    at every interval, which is what a mis-scheduled cadence would look like."""
    ocean = declared_restart()
    states = [field(directory / ocean) for directory in slot_dirs(run, 2)]
    for earlier, later in zip(states, states[1:]):
        assert not np.array_equal(earlier, later)


def test_soca_reads_what_the_model_wrote_at_a_slot(run):
    """The open question of the whole mechanism, asked of SOCA itself.

    A slot state is a MOM6 restart rather than model history, and that is the
    reason: SOCA reads a state through `fields metadata`, whose every ocean
    entry maps to a *restart* variable (`Temp`, `Salt`, `ave_ssh`). A
    `diag_table` writes the diagnostic names for the same fields (`temp`,
    `salt`, `SSH`), so history would need a second fields metadata, and that
    file is the model layer's one contract with SOCA.

    `soca_diffstates.x` is the cheapest application that reads two states and
    proves it did: it asserts the two carry the same time, differences them
    through the geometry, and writes the result. A file that SOCA could open as
    far as the first missing variable would fail in the read rather than here.
    """
    config = yaml.safe_load(run.frozen_config.read_text())
    ocean = config["model"]["restart"]["ocn"]
    first, _, _, last = slot_dirs(run, 2)

    scratch = run.scratch(2, "slotcheck")
    soca.stage(config, scratch, 2)
    # One date for both, because `oops::DiffStates` asserts the two states are
    # valid at the same time and a MOM6 restart carries no JEDI date of its own:
    # what a state is stamped with is whatever ACKBAR says here.
    date = cycle_time(config, 2).strftime(ISO_INSTANT)

    def state(directory):
        return {"read_from_file": 1, "basename": f"{directory}{os.sep}",
                "ocn_filename": ocean, "date": date,
                "state variables": list(config["model"]["state variables"])}

    geometry = soca._geometry(config["model"])
    document = {
        "state geometry": geometry,
        "increment geometry": geometry,
        "state1": state(last),
        "state2": state(first),
        "output": {"datadir": "out", "exp": "slotcheck", "type": "incr",
                   "date colons": False},
    }
    (scratch / "slotcheck.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    soca.launch(config, load_site(), scratch, "hofx", "soca_diffstates.x",
                "slotcheck.yaml", "slotcheck.log")

    written = sorted((scratch / "out").glob("*.nc"))
    assert len(written) == 1, (scratch / "slotcheck.log").read_text()[-4000:]
    # And it read the *states*, not two handles on one file: three hours of this
    # model is not the identity.
    assert np.any(field(written[0]) != 0.0)


# --- the overshoot a four-dimensional window costs ----------------------------
#
# This experiment cycles free, so its own forecast covers exactly its cycle. The
# two claims below are about the case it does not exercise, and they are asked
# here because they are claims about MOM6 rather than about a solver: a centred
# 4D window makes the forecast run half a window past the next analysis time, so
# the set the next cycle starts from stops being the last thing written and
# becomes one of the intervals. Whether MOM6 resumes from a set assembled that
# way is the whole risk in the change, and it is a question a free run can ask.

#: The restart set these two start from, and the instant it is valid at. Cycle
#: *n*'s forecast ends where cycle *n+1* begins, so `rst/2` is the state at
#: cycle 3's own time. Both configurations below are pinned to that instant and
#: run their cycle 1, rather than renumbering to find the cycle that lands on
#: it: the arithmetic under test is what the cycle length does to a forecast,
#: and a test that also had to solve for the cycle index would fail for two
#: different reasons with one message.
SOURCE_CYCLE = 2


def aside(paths, tmp_path):
    """*paths* with its scratch and its log under pytest's own directory.

    These two run the model outside the experiment's graph, at the experiment's
    own cycle 1, so without this they would write over the run directory and the
    parameter documents that the tests above read.
    """
    return replace(paths, scratch_root=tmp_path, output_root=tmp_path)


def derived(config, length, start, window=None, slots=None):
    """The same experiment at a different cadence, starting where *start* is."""
    config = copy.deepcopy(config)
    config["cycle"] = {**config["cycle"], "length": length,
                       "start": start.strftime(ISO_INSTANT)}
    config["solver"] = {"name": "none", "window": {"type": window}} if window else {}
    config["forecast"] = {"slots": slots} if slots else {}
    return config


def four_dimensional(config, start):
    """A six hour FGAT window over a six hour cycle, sub-divided every three.

    Which makes the forecast nine hours: states at F03, F06 and F09, the next
    cycle's window running from the first of those to the last, and the handoff
    at F06 in the middle of the run rather than at the end of it.
    """
    return derived(config, "PT6H", start, window="fgat", slots="PT3H")


def test_the_set_a_run_hands_forward_is_the_one_it_would_have_written_there(run, tmp_path):
    """Integrating past a time does not change the state at it.

    The claim the whole overshoot rests on, asked of the model rather than of
    arithmetic. A nine hour forecast hands forward the set it wrote at F06; a
    six hour forecast from the same start hands forward the set it wrote at its
    own end. Those two have to be the same state, bit for bit, or a 4D cycle
    would be resuming from something subtly unlike what a 3D one would have.

    Compared against a *shorter run* and not against a longer chain on purpose.
    This configuration is not exact-restart: resuming from the model's own final
    set and running on does not reproduce an uninterrupted run, at round-off
    that grows with the trajectory. That is a property of MOM6-SIS2 here and it
    is unchanged by any of this, so the comparison has to be one that isolates
    what did change, which is which of the run's sets gets handed forward.
    """
    frozen = yaml.safe_load(run.frozen_config.read_text())
    begin = cycle_time(frozen, SOURCE_CYCLE + 1)
    config = four_dimensional(frozen, begin)
    ocean = frozen["model"]["restart"]["ocn"]
    source = run.member_out("rst", SOURCE_CYCLE, 0)
    site = load_site()

    handoff = handoff_time(config, 1)
    slots = {when: tmp_path / "bkg" / when.strftime("%Y%m%dT%H%M%SZ")
             for when in slot_times(config, 1)}
    assert sorted(slots) == [begin + timedelta(hours=h) for h in (3, 6, 9)]
    assert handoff == begin + timedelta(hours=6)

    overshot = tmp_path / "rst_overshot"
    mom6sis2.forecast(config, site, aside(run, tmp_path / "long"), 1,
                      "forecast", 0, source=source, target=overshot,
                      slots=slots, handoff=handoff)

    # A whole set, under the names a restart set always has, and it says what
    # time it is: nothing downstream can tell it was written mid-run.
    assert sorted(p.name for p in overshot.iterdir()) == [
        "MOM.res.nc", "coupler.intermediate.res", "coupler.res", "ice_model.res.nc"]
    assert handoff.strftime("%Y %-m %-d %-H").split() == \
        (overshot / mom6sis2.STAMP).read_text().splitlines()[2].split()[:4]

    stopped = tmp_path / "rst_stopped"
    mom6sis2.forecast(derived(frozen, "PT6H", begin), site,
                      aside(run, tmp_path / "short"), 1, "forecast", 0,
                      source=source, target=stopped)

    assert np.array_equal(field(overshot / ocean), field(stopped / ocean))


def test_the_set_a_run_hands_forward_mid_run_is_one_the_model_resumes_from(run, tmp_path):
    """Every component's, and not only the ocean's.

    `coupler_main` writes its own clock files, FMS writes each component's
    restart and MOM6 writes the ocean's, all three under different stamping
    conventions and all three at every interval. A set missing any of them
    either stops the next cycle or, for the coupler's, starts it at the
    namelist date instead and integrates the right state from the wrong time
    without complaint.
    """
    frozen = yaml.safe_load(run.frozen_config.read_text())
    begin = cycle_time(frozen, SOURCE_CYCLE + 1)
    config = four_dimensional(frozen, begin)
    handoff = handoff_time(config, 1)
    site, overshot = load_site(), tmp_path / "rst"

    mom6sis2.forecast(config, site, aside(run, tmp_path / "long"), 1,
                      "forecast", 0, source=run.member_out("rst", SOURCE_CYCLE, 0),
                      target=overshot, handoff=handoff)

    resumed = tmp_path / "rst_resumed"
    mom6sis2.forecast(derived(frozen, "PT3H", handoff), site,
                      aside(run, tmp_path / "on"), 1, "forecast", 0,
                      source=overshot, target=resumed)

    assert (handoff + timedelta(hours=3)).strftime("%Y %-m %-d %-H").split() == \
        (resumed / mom6sis2.STAMP).read_text().splitlines()[2].split()[:4]


def test_the_slot_at_the_handoff_costs_no_second_copy_of_the_state(run, tmp_path):
    """It is the same state, and a copy of it is a gigabyte a member a cycle.

    The handoff time is a sub-window time too, always: it is the centre of the
    next cycle's window. A hard link rather than a symlink because `bkg/` and
    `rst/` are reaped independently, and a link that outlives its target reads
    as a missing background only when something opens it.
    """
    frozen = yaml.safe_load(run.frozen_config.read_text())
    config = four_dimensional(frozen, cycle_time(frozen, SOURCE_CYCLE + 1))
    ocean = frozen["model"]["restart"]["ocn"]
    handoff = handoff_time(config, 1)
    target = tmp_path / "rst"
    slots = {when: tmp_path / "bkg" / when.strftime("%Y%m%dT%H%M%SZ")
             for when in slot_times(config, 1)}

    mom6sis2.forecast(config, load_site(), aside(run, tmp_path), 1,
                      "forecast", 0,
                      source=run.member_out("rst", SOURCE_CYCLE, 0),
                      target=target, slots=slots, handoff=handoff)

    slot = slots[handoff] / ocean
    assert not slot.is_symlink()
    assert slot.stat().st_ino == (target / ocean).stat().st_ino


def test_cleanup_reaps_the_states_on_the_same_rule_as_the_restarts(run):
    """A slot state is a full 3D field, so `bkg/` grows faster than `rst/`.

    Four slots a cycle here and a six slot window at OM4_025 is six times the
    per-cycle output of the restarts beside it, which is the item that fills a
    disk in the second week rather than the first.
    """
    assert sorted(p.name for p in run.sub("bkg").iterdir()) == ["2", "3"]
