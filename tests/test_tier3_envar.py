"""Tier 3: an ensemble covariance with nothing maintaining the ensemble.

The other half of phase 8, and short on purpose: everything about assembling a
covariance, recentring an ensemble and writing it back is already covered by
`test_tier3_hybrid`. What is only here is the two things that experiment cannot
show.

- **A covariance with no static component at all.** `covariance: ensemble`, so
  the analysis is carried entirely by six localized members and the static B is
  configured and never opened. A cycle that quietly fell back to it would run
  normally and answer a different question.
- **A recentring whose ensemble is a set of MOM6 restarts.** With no filter in
  the cycle the members have no analyses, so the recentring reads the
  backgrounds instead: a different reader, a different variable set, and a path
  no other experiment takes.

Runs on `model/persistence`. Both claims are about what SOCA was handed and what
it wrote, and neither is about an integration; the restart sets the recentring
reads are real MOM6 restart sets either way, which is the part that matters.
`test_tier3_letkf` is where an ensemble is asked to survive being forecast.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_envar.py
"""

import os
from pathlib import Path

import pytest

from conftest import experiment_paths
import yaml

from ackbar import slurm
from ackbar.cli import main
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet
from test_tier3_hybrid import field, product, var_document
from test_tier3_var import DOMAIN, STATIC, build_archive

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
NAME = "tier3_envar"
LAST = 3
MEMBERS = (1, 2, 3, 4, 5, 6)
CONTROL = 0

#: One analysis, one recentring and seven persistence handoffs a cycle. Generous
#: against a queue that is busy rather than against the work itself.
QUIET = 900


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real applications; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")
    if not (STATIC / "static" / DOMAIN / "diffusion" / "loc_hz_open.nc").exists():
        pytest.skip(f"no localization for {DOMAIN}; "
                    f"run tools/soca-diffusion.sh {DOMAIN}")
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
    build_archive(source, root)
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


def test_the_cycle_ran_one_analysis_and_no_filter(run):
    """`source: none`, so nothing updates the members with observations.

    The graph says so and this says the graph was obeyed: there is no `da.ens`
    sentinel, and every member's only state for the cycle is the recentred one.
    """
    done = {path.stem for path in (run.done_dir(LAST)).glob("*.json")}
    assert "da" in done
    assert "da.ens" not in done
    assert "recenter" in done


def test_the_covariance_had_no_static_component(run):
    """Read off the document the application was handed.

    The static B is still configured by the layer and deliberately unread:
    `solver.covariance` is the one place that decides, and a cycle that fell
    back to it would look exactly like this one.
    """
    error = var_document(run, LAST)["cost function"]["background error"]
    assert error["covariance model"] == "ensemble"
    assert "components" not in error
    assert "saber central block" not in error
    assert len(error["members"]) == len(MEMBERS)
    assert error["localization"]["localization method"] == "SABER"
    # Every diffusion group names what it localizes. A group without its own
    # `variables:` constructs, runs, and localizes nothing, which is the one
    # failure this whole component cannot survive.
    groups = error["localization"]["saber central block"]["read"]["groups"]
    assert groups
    for group in groups:
        assert group["variables"] == \
            var_document(run, LAST)["cost function"]["analysis variables"]


def test_the_analysis_moved_the_state_towards_its_observations(run):
    """A localized six member covariance is enough to fit something.

    Not a claim that it fits it well: six members forced by one atmosphere is a
    covariance held together by its localization. What this catches is an
    ensemble component that is constructed and contributes nothing, which is
    what a mis-scaled localization produces and which nothing else reports.
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
            numpy.sqrt(numpy.mean(before[keep] ** 2))


def test_the_recentring_read_the_backgrounds_and_still_centred_the_ensemble(run):
    """The path only this experiment takes.

    With no filter there are no member analyses to recentre, so the ensemble the
    application reads is six MOM6 restart sets. The arithmetic is the same and
    the reader is not.
    """
    import numpy

    centre = field(product(run, LAST, CONTROL, "ana"))
    recentred = numpy.mean([field(product(run, LAST, member, "rcnt"))
                            for member in MEMBERS], axis=0)
    assert numpy.allclose(recentred, centre, atol=1e-5)


def test_the_free_ensemble_kept_its_spread(run):
    """Nothing in this cycle contracts the ensemble, so nothing should shrink it.

    A filter reduces spread and inflates it back; here there is neither, so the
    recentring is the only thing acting on the ensemble at all. Spread that
    collapses anyway means the recentring is overwriting members rather than
    moving them, which is the failure that looks most like success.
    """
    import numpy

    spread = numpy.std([field(product(run, LAST, member, "rcnt"))
                        for member in MEMBERS], axis=0)
    assert numpy.mean(spread[spread > 0]) > 1e-4
