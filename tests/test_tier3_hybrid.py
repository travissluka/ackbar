"""Tier 3: hybrid covariance cycling at gom_25km. The phase 8 milestone.

Two analyses in one cycle and a recentring joining them. `da.ens` is an LETKF
over the six members, `da` is a variational solve for the control whose
background error is half the static B and half that ensemble, and `recenter`
pulls every member onto the control's answer before its writeback.

The claims, in the order they would cost the most to discover later:

- **The covariance really is hybrid.** Both components are in the document the
  application read, both carry the weight the experiment stated, and the
  ensemble component names the previous cycle's member forecasts. The failure
  this guards against is a cycle that runs a static 3DVar under a hybrid's name,
  which produces a completely healthy experiment and a wrong answer to the only
  question the experiment was asked.
- **The two analyses do not overwrite each other.** They run over the same
  observers with the same configured output name, and the filter's mean and
  increment share a filename with the deterministic analysis and its own. Every
  one of those collisions produces a file that exists and is the wrong state.
- **Every member ends the cycle centred on the control's analysis.** That is
  what the recentring is, and it is checkable exactly: member minus recentred
  member is the same field for every member, because what changed is the centre
  they share.
- **The ensemble the covariance read is the ensemble the filter assimilated.**
  One job applies the divergence policy and the other reads its record. Two
  independent applications of a `replace_from_mean` policy is how one half of a
  hybrid ends up with a member the other half rebuilt.

What this does *not* test is whether a hybrid analysis is any better than the
static one. Everything `test_tier3_letkf` says about this ensemble applies here
and applies harder: six members forced by one atmosphere, perturbed out of the
static B, giving about 0.09 K of surface spread. The weights are not tuned and
nothing here claims they are.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_hybrid.py
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
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet
from test_tier3 import initial_energy
from test_tier3_var import DOMAIN, STATIC, initial_condition

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
NAME = "tier3_hybrid"
CYCLES = (1, 2, 3)
LAST = 3

#: The cycles whose analysis directory still exists once the run has drained.
#: `cleanup` in cycle *n* reaps *n-2*.
KEPT = (2, 3)

MEMBERS = (1, 2, 3, 4, 5, 6)
CONTROL = 0

#: Seven forecasts, two analyses and a recentring a cycle, on eight cores.
QUIET = 3600


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real applications; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")
    diffusion = STATIC / "static" / DOMAIN / "diffusion"
    for name in ("hz.nc", "loc_hz.nc"):
        if not (diffusion / name).exists():
            pytest.skip(f"no {name} for {DOMAIN}; "
                        f"run tools/soca-diffusion.sh {DOMAIN}")
    source = yaml.safe_load((REPO / "tests/experiments" / f"{NAME}.yaml").read_text())
    ensemble = Path(source["ensemble"]["initial_condition"]
                    .replace("$(static_root)", str(STATIC)))
    if not (ensemble / "mem001" / "coupler.res").exists():
        pytest.skip(f"no ensemble initial condition at {ensemble}; run "
                    f"tools/ensemble-ic.sh {DOMAIN} {len(MEMBERS)}")


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """The same synthetic archive the other two tier 3 experiments read."""
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

def field(path, name="Temp", level=0):
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        values = data.variables[name][0]
        return numpy.asarray(values[level] if values.ndim == 3 else values)


def product(paths, cycle, member, stem):
    return next((paths.member_out("ana", cycle, member) / "analysis")
                .glob(f"ocn.{stem}.*.nc"))


def document(paths, cycle, task, name):
    """The YAML a cycle's application actually read, kept beside its log.

    Read rather than rebuilt, because what is being checked is that the job
    assembled the right document and not that this test can assemble one. The
    file is stamped with the job id, so that a healed attempt lands beside the
    failed one instead of overwriting the evidence.
    """
    found, = sorted((paths.log_dir(cycle)).glob(f"{task}.*.{name}.yaml"))
    return yaml.safe_load(found.read_text())


def var_document(paths, cycle):
    return document(paths, cycle, "da", "var")


def letkf_document(paths, cycle):
    return document(paths, cycle, "da.ens", "letkf")


def departures(paths, cycle, observer, subdir=""):
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    where = paths.cycle_out("obs_out", cycle)
    path = next((where / subdir).glob(f"*{observer}*.nc4"))
    with netCDF4.Dataset(path) as data:
        variable = list(data["ombg"].variables)[0]
        before = numpy.asarray(data["ombg"][variable][:])
        after = numpy.asarray(data["oman"][variable][:])
    keep = (numpy.abs(before) < 1e30) & (numpy.abs(after) < 1e30)
    return before[keep], after[keep]


# --- the covariance really is hybrid -----------------------------------------

def test_the_analysis_read_a_two_component_covariance(run):
    """Read off the document the application was handed, not off the config.

    Everything between a layer and a running solve is assembled by the job, and
    a static 3DVar running under a hybrid's name is an experiment that completes
    normally and answers a different question.
    """
    error = var_document(run, LAST)["cost function"]["background error"]
    assert error["covariance model"] == "hybrid"
    static, ensemble = error["components"]
    assert static["covariance"]["covariance model"] == "SABER"
    assert ensemble["covariance"]["covariance model"] == "ensemble"
    assert static["weight"]["value"] == 0.5
    assert ensemble["weight"]["value"] == 0.5


def test_the_ensemble_component_read_the_previous_cycles_forecasts(run):
    """The *backgrounds*, which is what a B is: the covariance of forecast error.

    Reading the members' analyses instead would sample an error the assimilation
    has already removed, which is a smaller covariance every cycle and no
    message about it.
    """
    error = var_document(run, LAST)["cost function"]["background error"]
    members = error["components"][1]["covariance"]["members"]
    assert len(members) == len(MEMBERS)
    for member, entry in zip(MEMBERS, members):
        assert entry["basename"].rstrip("/") == \
            str(run.member_out("rst", LAST - 1, member))
        assert entry["ocn_filename"] == "MOM.res.nc"


def test_the_localization_read_the_domains_own_calibration(run):
    error = var_document(run, LAST)["cost function"]["background error"]
    localization = error["components"][1]["covariance"]["localization"]
    groups = localization["saber central block"]["read"]["groups"]
    assert groups[0]["horizontal"]["filepath"] == \
        str(STATIC / "static" / DOMAIN / "diffusion" / "loc_hz")
    assert localization["localization variables"]


def test_the_two_applications_read_their_observations_differently(run):
    """The reason a distribution cannot be a substituted value.

    One merged configuration, one observer list, two applications that need
    different decompositions. soca-science patched around this with `sed`
    markers keyed on whether the LETKF was running solo or inside a `3dhyb`.
    """
    var = var_document(run, LAST)["cost function"]["observations"]["observers"]
    letkf = letkf_document(run, LAST)["observations"]["observers"]
    assert var[0]["obs space"]["distribution"] == {"name": "RoundRobin"}
    assert letkf[0]["obs space"]["distribution"]["name"] == "Halo"


# --- the two analyses do not overwrite each other -----------------------------

def test_the_control_has_both_answers_and_they_are_not_the_same_file(run):
    """The deterministic analysis, and the filter's mean beside it.

    In a pure LETKF the posterior mean *is* the control's analysis. Here it is
    not, and it shares a filename with the one that is.
    """
    import numpy

    deterministic = product(run, LAST, CONTROL, "ana")
    mean = next((run.member_out("ana", LAST, CONTROL) / "analysis" / "ensemble")
                .glob("ocn.ana.*.nc"))
    assert deterministic != mean
    assert not numpy.array_equal(field(deterministic), field(mean))


def test_the_filters_increment_did_not_land_on_the_deterministic_one(run):
    import numpy

    where = run.member_out("ana", LAST, CONTROL) / "analysis"
    solve = next(where.glob("ocn.incr.*.nc"))
    filtered = next((where / "ensemble").glob("ocn.incr.*.nc"))
    assert not numpy.array_equal(field(solve), field(filtered))


def test_both_analyses_wrote_departures_and_the_controls_kept_the_name(run):
    """The control's are the experiment's product; the ensemble's are a
    diagnostic and go one level down."""
    import numpy

    for cycle in CYCLES:
        for observer in ("adt_3a", "sst_noaa19"):
            before, after = departures(run, cycle, observer)
            assert before.size > 10, f"cycle {cycle} {observer}: nothing assimilated"
            rms = (numpy.sqrt(numpy.mean(before ** 2)),
                   numpy.sqrt(numpy.mean(after ** 2)))
            assert rms[1] < rms[0], f"cycle {cycle} {observer}: {rms}"
            # And the filter's own, which are a different solve of the same
            # observations and so are not the same numbers.
            ens_before, _ = departures(run, cycle, observer, subdir="ensemble")
            assert ens_before.size > 10


# --- the recentring ------------------------------------------------------------

def test_every_member_was_moved_by_the_same_field(run):
    """What a recentring is, checked exactly.

    Each member keeps its own perturbation about the ensemble mean and is given
    the deterministic analysis as its centre, so what every member gains is the
    same field: the centre minus the mean they shared. If any member were
    recentred onto something else, or onto nothing, this is where it shows.
    """
    import numpy

    shifts = [field(product(run, LAST, member, "rcnt"))
              - field(product(run, LAST, member, "ana"))
              for member in MEMBERS]
    for shift in shifts[1:]:
        assert numpy.allclose(shift, shifts[0], atol=1e-5)


def test_the_recentred_ensemble_is_centred_on_the_deterministic_analysis(run):
    import numpy

    centre = field(product(run, LAST, CONTROL, "ana"))
    recentred = numpy.mean([field(product(run, LAST, member, "rcnt"))
                            for member in MEMBERS], axis=0)
    assert numpy.allclose(recentred, centre, atol=1e-5)


def test_the_recentring_kept_the_spread_it_was_given(run):
    """Recentring moves the mean and must not touch the perturbations.

    An implementation that overwrote each member with the centre would satisfy
    the test above and collapse the ensemble, which is the failure that looks
    most like success.
    """
    import numpy

    def spread(stem):
        return numpy.std([field(product(run, LAST, member, stem))
                          for member in MEMBERS], axis=0)

    before, after = spread("ana"), spread("rcnt")
    ocean = before > 0
    assert ocean.sum() > 100
    assert numpy.allclose(before[ocean], after[ocean], rtol=1e-4)


def test_writeback_applied_the_recentred_state_and_not_the_filters(run):
    """The step that makes a hybrid's ensemble follow its own experiment.

    Applying the filter's analysis instead leaves every member cycling around
    the filter's mean while the run being reported is the deterministic one, and
    the two drift apart with nothing to say so.
    """
    import numpy

    for member in MEMBERS[:2]:
        written = field(run.member_out("ana", LAST, member) / "MOM.res.nc")
        recentred = field(product(run, LAST, member, "rcnt"))
        filtered = field(product(run, LAST, member, "ana"))
        ocean = ~numpy.isclose(recentred, filtered)
        assert ocean.sum() > 100
        assert numpy.allclose(written[ocean], recentred[ocean], atol=1e-5)


# --- the cycle held together ---------------------------------------------------

def test_one_record_says_which_members_both_halves_used(run):
    """`da.ens` applies the divergence policy and `da` reads what it decided.

    Two independent applications of `replace_from_mean` is how one half of a
    hybrid ends up reading a member the other half rebuilt, and neither would
    report anything.
    """
    import json

    ledger = json.loads(run.member_list(LAST).read_text())
    assert ledger["assimilated"] == list(MEMBERS)

    error = var_document(run, LAST)["cost function"]["background error"]
    used = [entry["basename"].rstrip("/")[-3:]
            for entry in error["components"][1]["covariance"]["members"]]
    assert used == [f"{member:03d}" for member in ledger["assimilated"]]


def test_every_member_resumed_from_its_own_analysed_restart(run):
    """A cold start would give every member the same state, silently.

    `VELOCITY_CONFIG = zero` on a cold start, so MOM6's own step-zero kinetic
    energy is exactly zero and is not on a resumed run.
    """
    for member in (CONTROL,) + MEMBERS:
        assert initial_energy(run, LAST, member) > 1e-12


def test_no_two_members_forecast_the_same_state(run):
    import numpy

    written = [field(run.member_out("rst", LAST, member) / "MOM.res.nc")
               for member in MEMBERS]
    for i in range(len(written)):
        for j in range(i + 1, len(written)):
            assert not numpy.array_equal(written[i], written[j])


def test_the_ensemble_did_not_collapse(run):
    """Three cycles is not long enough to prove an ensemble is stable, and is
    long enough to catch one that goes to zero immediately."""
    import numpy

    def spread(cycle):
        return numpy.std([field(product(run, cycle, member, "rcnt"))
                          for member in MEMBERS], axis=0)

    first, last = spread(KEPT[0]), spread(LAST)
    ocean = first > 0
    assert numpy.mean(last[ocean]) > 0.01 * numpy.mean(first[ocean])
