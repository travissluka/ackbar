"""Tier 3: the EAKF solver runs, at gom_25km, with no model in the loop.

Short on purpose. Everything structural about this experiment is settled at tier
1: the graph is the LETKF's graph
(`test_the_two_ensemble_filters_build_the_same_graph`) and the document differs
from the LETKF's in exactly three keys
(`test_the_shipped_eakf_layers_change_the_solver_and_nothing_structural`).
Everything about writing an ensemble analysis back into restart sets is
`test_tier3_letkf`'s, and none of it changes with the solver.

What is only here is the thing no golden can say: that `soca_letkf.x` accepts
this document and produces an analysis from it. Three ways it could not, all of
which pass validation and every tier 1 test:

- **`EAKF` is not registered.** It is, in
  `oops/assimilation/instantiateLocalEnsembleSolverFactory.h`, but the name is a
  string in a YAML file and the factory is the only thing that checks it.
- **The localization is one the sequential solver rejects.** SOCA's own
  `eakf.yml` carries the note that Rossby localization does not work with EAKF,
  which is why `da/eakf` replaces it. A layer that failed to would abort here.
- **The distribution is one it cannot solve in.** The halo an LETKF needs is
  removed, and removing the wrong key, or leaving `halo size` behind on a round
  robin, is a document ioda reads differently than it looks.

Runs on `model/persistence`: all three failures are inside SOCA, and none of
them is about an integration.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_eakf.py
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
from test_tier3_hybrid import field, product
from test_tier3_var import DOMAIN, STATIC, initial_condition

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
NAME = "tier3_eakf"
CYCLES = (1, 2, 3)
LAST = 3
MEMBERS = (1, 2, 3, 4, 5, 6)
CONTROL = 0

#: One filter and seven persistence handoffs a cycle. Generous against a busy
#: queue rather than against the work.
QUIET = 900


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real applications; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")
    source = yaml.safe_load((REPO / "tests/experiments" / f"{NAME}.yaml").read_text())
    ensemble = Path(source["ensemble"]["initial_condition"]
                    .replace("$(static_root)", str(STATIC)))
    if not (ensemble / "mem001" / "coupler.res").exists():
        pytest.skip(f"no ensemble initial condition at {ensemble}; run "
                    f"tools/ensemble-ic.sh {DOMAIN} {len(MEMBERS)}")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
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


def test_the_document_the_filter_read_named_the_sequential_solver(run):
    """Read off what the job assembled, not off the layer.

    The layer is checked at tier 1. What this adds is that the document the
    application was handed on this machine is the one that layer describes, and
    that the halo really is absent rather than present and empty.
    """
    found, = sorted(run.log_dir(LAST).glob("da.*.letkf.yaml"))
    document = yaml.safe_load(found.read_text())

    assert document["local ensemble DA"]["solver"] == "EAKF"
    for observer in document["observations"]["observers"]:
        assert observer["obs space"].get("distribution") == {"name": "RoundRobin"}
        methods = [entry["localization method"]
                   for entry in observer["obs localizations"]]
        assert methods == ["Horizontal Gaspari-Cohn"], observer["obs space"]["name"]


def test_every_member_has_its_own_analysis(run):
    """The filter ran and wrote six distinct states.

    A solver that constructed and did nothing would leave every member holding
    the state it came in with, which is six identical files rather than none.
    """
    import numpy

    written = [field(product(run, LAST, member, "ana")) for member in MEMBERS]
    for i in range(len(written)):
        for j in range(i + 1, len(written)):
            assert not numpy.array_equal(written[i], written[j]), (i, j)


def test_the_analysis_moved_the_state_towards_the_observations(run):
    """`oman` below `ombg`, which is the filter having used its observations.

    Not a claim about how much: the ensemble is perturbed out of the static B
    and is overconfident by construction, exactly as `test_tier3_letkf` says at
    length. What this catches is a solver that ran and changed nothing.
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


def test_the_ensemble_did_not_collapse(run):
    """A sequential filter updates the ensemble once per observation.

    Which is the failure mode this solver has and the LETKF does not: an
    inflation that is right for a single simultaneous update can be far too weak
    when the same spread is contracted a few hundred times in sequence.
    `rtps: 0.95` is inherited from `da/letkf` and is not retuned, deliberately,
    so this is the assertion that says whether that was survivable at all.
    """
    import numpy

    def spread(cycle):
        return numpy.std([field(product(run, cycle, member, "ana"))
                          for member in MEMBERS], axis=0)

    first, last = spread(2), spread(LAST)
    ocean = first > 0
    assert ocean.sum() > 100
    assert numpy.mean(last[ocean]) > 0.01 * numpy.mean(first[ocean])


def test_every_cycle_records_which_members_it_had(run):
    for cycle in CYCLES:
        ledger = json.loads(run.member_list(cycle).read_text())
        assert ledger["assimilated"] == list(MEMBERS)


def test_the_experiment_finished(run):
    assert [run.stats_file(cycle).exists() for cycle in CYCLES] == [True] * 3
