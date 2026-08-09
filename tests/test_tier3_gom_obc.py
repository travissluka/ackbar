"""Tier 3: does a per-member open boundary reach the model, or only the disk?

`test_mom6sis2.py` proves `ensemble.inputs` builds the run directory correctly,
and it proves it with symlinks to files holding the word "boundary". What it
cannot prove is the half that matters: that `INPUT/obc.nc` is the name MOM6 opens
for its open boundary, that the model is not reaching the domain's shared copy by
some other path, and that a staged file therefore changes the ocean rather than
only the directory listing. Every one of those is a claim about MOM6, so this is
tier 3 by `docs/testing.md`'s own rule.

The experiment is `tier3_gom` with two keys added and nothing else, run twice:
once staging the domain's own boundary per member, once staging a lagged one.
Two runs rather than one because the assertion is a difference, and a single run
can only say what it did, not what it would have done otherwise. It is affordable
because a cycle here is a six second forecast, so both runs together are mostly
scheduler latency.

**Why the archive is built by `tools/obc-lagged.py` rather than written here.**
The producer and the consumer are joined by a filename convention, `mem000.nc`
and up, which lives in the tool at one end and in an experiment's
`ensemble.inputs` template at the other. A fixture that hand-wrote that layout
would keep passing if either end renamed, which is the failure
`tests/test_templates.py` exists for elsewhere.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import netCDF4
import numpy as np
import pytest
import yaml

from ackbar import slurm
from ackbar.cli import main
from ackbar.paths import Paths
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "tests" / "experiments" / "tier3_gom.yaml"
QUIET = 900

#: Large enough that the difference is unmistakably the boundary rather than
#: roundoff, and still a real lagged ocean rather than noise. The window this
#: domain's `obc.nc` covers is short, so the span is days rather than weeks.
SPAN, MEMBERS, AMPLITUDE = 2, 2, 4.0


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real model; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")
    covered_or_skip()


def covered_or_skip():
    """Skip, loudly, if `tier3_gom`'s dates are outside the domain's boundary.

    This is not a check about the boundary ensemble; it is a check about the
    fixture this module inherits. `tier3_gom` starts at 2015-01-05 because that
    is where `ic/gom_25km/hycom-smoke` leaves off, and the domain's `obc.nc` is
    GLORYS covering 2015-05-28 to 2015-09-10. MOM6 stops on that, and it stops
    with `time_interp_external ... is before range of list`, four minutes into a
    job, from whichever PE owns a segment.

    Skipping rather than failing because a red test here would be reporting
    someone else's bug in this module's name: `test_tier3.py`,
    `test_tier3_gom.py` and `test_tier3_diffusion.py` inherit the same fixture
    and are blocked on the same thing. The message names the real blocker so it
    cannot be read as a boundary-ensemble problem.
    """
    config = yaml.safe_load(SOURCE.read_text())
    boundary = Path(os.environ["ACKBAR_STATIC_ROOT"]) / "domain" / "gom_25km" \
        / "INPUT" / "obc.nc"
    if not boundary.exists():
        pytest.skip(f"{boundary} is not staged")
    with netCDF4.Dataset(boundary) as f:
        units, t = f["time"].units, f["time"]
        calendar = getattr(t, "calendar", "standard")
        # Built from the fields rather than formatted. A NOLEAP axis comes back
        # as a cftime object, and the one pinned here implements neither
        # `__format__` nor `isoformat`; year, month and day it does have.
        def day(value):
            d = netCDF4.num2date(value, units, calendar)
            return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"

        first, last = day(t[0]), day(t[-1])
    start = str(config["cycle"]["start"])[:10]
    if not first <= start <= last:
        pytest.skip(
            f"tier3_gom starts {start}, outside the gom_25km boundary's "
            f"{first} to {last}. Every tier 3 test on this fixture is blocked "
            f"on it, not just this one.")


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """A boundary ensemble, built by the tool an experiment would use."""
    out = tmp_path_factory.mktemp("obc-lagged")
    done = subprocess.run(
        [sys.executable, str(REPO / "tools" / "obc-lagged.py"),
         "--span", str(SPAN), "--members", str(MEMBERS),
         "--amplitude", str(AMPLITUDE), "--out", str(out), "gom_25km"],
        capture_output=True, text=True,
        env=dict(os.environ, PYTHON=sys.executable))
    if done.returncode != 0:
        pytest.skip(f"could not build a boundary ensemble: {done.stderr.strip()}")
    assert (out / "mem000.nc").exists(), done.stdout
    return out


def experiment(tmp_path, name, boundary):
    """`tier3_gom` with `ensemble.inputs` pointing at *boundary*."""
    config = yaml.safe_load(SOURCE.read_text())
    config["experiment"]["name"] = name
    config["ensemble"] = {"size": 1, "control": True,
                          "inputs": {"obc.nc": str(boundary)}}
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def cycled(path, name):
    # `Paths` from the generated config rather than `conftest.experiment_paths`,
    # which resolves a name against `tests/experiments/`. These two experiments
    # are written per run into a tmp dir, because the boundary archive they point
    # at is built per run and its path cannot be committed.
    paths = Paths.of(yaml.safe_load(path.read_text()), load_site())
    _purge(paths)
    assert main(["create", str(path), "--force"]) == 0
    assert main(["start", name]) == 0
    assert wait_for_quiet(name, QUIET) == "drained"
    return paths


@pytest.fixture(scope="module")
def runs(tmp_path_factory, archive):
    """The same experiment against the unperturbed boundary and a lagged one."""
    tmp = tmp_path_factory.mktemp("obc-experiments")
    out = {}
    for name, member in (("tier3_gom_obc_base", "mem000.nc"),
                         ("tier3_gom_obc_lagged", "mem001.nc")):
        out[name] = cycled(experiment(tmp, name, archive / member), name)
    yield out, archive
    for paths in out.values():
        _purge(paths)


def final_state(paths, cycle=3):
    with netCDF4.Dataset(paths.member_out("rst", cycle, 0) / "MOM.res.nc") as f:
        return np.ma.filled(f["Temp"][:].astype("f8"), np.nan)


# --- the mechanism -----------------------------------------------------------

def test_a_staged_boundary_is_what_the_model_integrated(runs):
    """The whole point, and the only assertion here that MOM6 has to answer.

    Two runs identical in every input but `INPUT/obc.nc`, from the same restart
    set, over the same three cycles. If the staged file is what MOM6 opened for
    its open boundary, the oceans differ. If the model reached the domain's
    shared copy by any other path, or ignored the link, they are bit identical,
    which is exactly what a staging bug looks like from every other test.
    """
    outputs, _ = runs
    base = final_state(outputs["tier3_gom_obc_base"])
    lagged = final_state(outputs["tier3_gom_obc_lagged"])
    difference = np.nanmax(np.abs(base - lagged))
    assert difference > 0, (
        "the two runs are bit identical, so INPUT/obc.nc is not what forced "
        "this domain's open boundary and ensemble.inputs stages a file nothing "
        "reads")


def test_the_difference_is_the_boundary_and_not_the_whole_ocean(runs):
    """It arrives at the edge, not everywhere at once.

    A difference that filled the domain in three six second cycles would not be
    the boundary; it would be a different initial condition or a different
    parameter file, which is the other way this test could pass for the wrong
    reason.
    """
    outputs, _ = runs
    base = final_state(outputs["tier3_gom_obc_base"])
    lagged = final_state(outputs["tier3_gom_obc_lagged"])
    changed = np.abs(base - lagged) > 0
    fraction = changed.sum() / np.isfinite(base).sum()
    assert 0 < fraction < 0.5, (
        f"{100 * fraction:.0f}% of the ocean moved in three short cycles, which "
        f"is too much of it to have come from the boundary")


def test_the_member_read_the_archive_and_not_the_domains_own_copy(runs):
    """`readlink` answers what a member read, which is the design's own claim."""
    outputs, archive = runs
    paths = outputs["tier3_gom_obc_lagged"]
    linked = sorted((paths.log_dir(3)).glob("forecast*.model.log"))
    assert linked, "no forecast log for cycle 3"
    # The run directory is scratch and is gone on success, so the evidence that
    # survives is the parameter document MOM6 itself wrote.
    docs = sorted((paths.log_dir(3)).glob("forecast*.MOM_parameter_doc.all"))
    assert len(docs) == 1, docs
    text = docs[0].read_text()
    assert "OBC_SEGMENT_001" in text, (
        "cycle 3 ran without an open boundary configured, so nothing here is "
        "about a boundary at all")
