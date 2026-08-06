"""Tier 3: the observation path on the real model, at gom_25km.

The phase 5 milestone. The phase 4 free run with observers attached, and two
claims, of which the second is the one the whole design of this path turns on:

- **hofx produces ombg for every configured platform**, every cycle, evaluating
  the background the next forecast will start from. Cycle 1 included, where the
  background is the materialized initial condition and not a forecast at all.
- **A gap in the archive is survivable.** A missing observation file drops that
  observer for that cycle, the drop is recorded where a comparison can find it,
  and the cycle finishes. An archive with holes is the normal case over any real
  record, and a workflow that stops on one is a workflow that cannot be used.

One full run, and almost all of it is scheduler latency: a twelve hour
`gom_25km` forecast is six seconds and hofx is less. Opt in with
`ACKBAR_TIER3=1`; needs `source site/activate.sh`, a built `coupler_main` and
SOCA, the offline initial condition and observation archive the experiment
names, and the domain's static stage (`tools/soca-gridspec.sh gom_25km`).

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_hofx.py
"""

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from ackbar import slurm
from ackbar.cli import main
from ackbar.paths import Paths
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
EXPERIMENT = REPO / "tests" / "experiments" / "tier3_hofx.yaml"
NAME = "tier3_hofx"

#: Three cycles of one twelve hour gom_25km forecast each, plus hofx and the
#: bookkeeping. Generous, because what this guards against is sitting through a
#: hang rather than a slow machine.
QUIET = 900

#: The cycle whose ADT file is deliberately absent, and the observer that is
#: missing from it. Cycle 2 rather than 1 or 3, so that the cycle before it and
#: the cycle after it both prove the gap did not leak either way.
GAP_CYCLE = 2
GAP_OBSERVER = "adt_3a"

OBSERVERS = ("adt_3a", "sst_noaa19")


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real model; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")


def paths_for():
    return Paths.of({"experiment": {"name": NAME}}, load_site())


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """A copy of the experiment's archive that this test owns, one file short.

    Copied rather than read in place, because the gap is the point of the test
    and a hole punched in the archive every other experiment reads would be a
    hole in their results too. Copied rather than generated, because the archive
    is an offline product: what runs here should be the same bytes a real
    experiment gets, not a second recipe for them that could drift.
    """
    source = yaml.safe_load(EXPERIMENT.read_text())["vars"]["obs_dir"]
    root = tmp_path_factory.mktemp("obs") / "archive"
    shutil.copytree(resolve_static(source), root)
    # The archive is keyed by date, not by cycle number, and the cycle
    # directories sort in time order, so this is the gap cycle's file.
    missing = sorted(root.rglob(f"*/{GAP_OBSERVER}.*.nc4"))[GAP_CYCLE - 1]
    missing.unlink()
    return root


def resolve_static(value):
    """`$(static_root)/...` as written in the experiment, against this site.

    The one experiment-time symbol these paths use. Resolving it here rather
    than hardcoding the archive keeps the experiment the only place that says
    which observations this test runs on.
    """
    root = os.environ["ACKBAR_STATIC_ROOT"]
    return Path(value.replace("$(static_root)", root))


@pytest.fixture(scope="module")
def experiment(tmp_path_factory, archive):
    """The committed experiment, pointed at this test's own archive."""
    source = yaml.safe_load(EXPERIMENT.read_text())
    source["vars"]["obs_dir"] = str(archive)
    path = tmp_path_factory.mktemp("cfg") / "tier3_hofx.yaml"
    path.write_text(yaml.safe_dump(source))
    return path


@pytest.fixture(scope="module")
def run(experiment):
    paths = paths_for()
    _purge(paths)
    assert main(["create", str(experiment)]) == 0
    assert main(["start", NAME]) == 0
    assert wait_for_quiet(NAME, QUIET) == "drained"
    yield paths_for()
    _purge(paths_for())


def observers(paths, cycle):
    return {record["name"]: record
            for record in json.loads(paths.observer_list(cycle).read_text())["observers"]}


def outputs(paths, cycle):
    return sorted(p.name for p in paths.cycle_out("obs_out", cycle).glob("*.nc4"))


def emitted(paths, cycle):
    """The config the application actually read, kept out of scratch.

    Named with the job id, like every other trace, so that a healed attempt
    lands beside the failed one instead of overwriting the evidence.
    """
    document = sorted((paths.sub("log") / str(cycle)).glob("hofx.*.hofx.yaml"))
    assert len(document) == 1, f"cycle {cycle}: {document}"
    return yaml.safe_load(document[0].read_text())


# --- hofx ran, and ran on the right state ------------------------------------

def test_every_cycle_evaluated_every_observer_it_had(run):
    for cycle in (1, 2, 3):
        expected = [name for name in OBSERVERS
                    if not (cycle == GAP_CYCLE and name == GAP_OBSERVER)]
        written = outputs(run, cycle)
        assert len(written) == len(expected), f"cycle {cycle}: {written}"
        for name in expected:
            assert any(name in f for f in written), f"cycle {cycle} missing {name}"


def test_the_output_is_a_departure_and_not_an_empty_file(run):
    """`hofx` present, finite, and not equal to the observation.

    A file of the right name containing fill values is what an operator that
    interpolated nothing produces, and it is indistinguishable from success
    everywhere except here.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    path = next(run.cycle_out("obs_out", 1).glob("*sst_noaa19*.nc4"))
    with netCDF4.Dataset(path) as data:
        hofx = data["hofx"]["seaSurfaceTemperature"][:]
        observed = data["ObsValue"]["seaSurfaceTemperature"][:]
    finite = numpy.abs(hofx) < 1e30
    assert finite.sum() > 10, "almost nothing was simulated"
    assert not numpy.allclose(hofx[finite], observed[finite])


def test_the_first_cycle_evaluates_the_initial_condition(run):
    """Cycle 1's background is `rst/0`, which no forecast wrote.

    The claim the whole "cycle 1 is not special" design rests on, asked of the
    one task that would most easily get it wrong: hofx in the first cycle has no
    forecast parent at all, so nothing about the graph would notice if it read
    the wrong state.
    """
    document = emitted(run, 1)
    assert document["state"]["basename"].rstrip("/").endswith("rst/0/mem000")
    assert document["state"]["date"] == \
        yaml.safe_load(EXPERIMENT.read_text())["cycle"]["start"]


def test_each_cycle_evaluates_its_own_background(run):
    # Cycle n reads what cycle n-1 wrote. A workflow that reads `rst/0` every
    # time passes the test above and is wrong from cycle 2 on.
    for cycle in (2, 3):
        assert emitted(run, cycle)["state"]["basename"].rstrip("/").endswith(
            f"rst/{cycle - 1}/mem000")


# --- the gap -----------------------------------------------------------------

def test_the_missing_file_dropped_its_observer_and_not_the_cycle(run):
    record = observers(run, GAP_CYCLE)[GAP_OBSERVER]
    assert record["present"] is False
    assert record["required"] is False
    # The cycle finished anyway, which is what the sentinel means.
    assert run.sentinel(GAP_CYCLE, "hofx").exists()


def test_the_drop_is_recorded_where_a_comparison_can_find_it(run):
    """Every configured observer appears in every cycle's list, present or not.

    A list that only names what ran is a list in which a drop looks like an
    observer that was never configured, and two experiments differing in which
    observers actually ran is the difference that most distorts a comparison
    between them.
    """
    for cycle in (1, 2, 3):
        assert sorted(observers(run, cycle)) == sorted(OBSERVERS)


def test_the_cycles_either_side_of_the_gap_are_untouched(run):
    for cycle in (GAP_CYCLE - 1, GAP_CYCLE + 1):
        assert all(record["present"] for record in observers(run, cycle).values())


def test_the_experiment_finished(run):
    # Nothing above proves the run got past the gap: a workflow that stopped at
    # cycle 2 would satisfy every assertion about cycles 1 and 2.
    assert sorted(p.name for p in run.sub("stats").glob("*.json")) == [
        "1.json", "2.json", "3.json"
    ]
