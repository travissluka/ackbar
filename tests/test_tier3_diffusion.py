"""Tier 3: the diffusion calibration, against the real SOCA, at gom_25km.

The background error's correlation is the one part of the analysis whose
correctness is not visible from its output. An under-normalized operator returns
increments that are smoothly a few percent small; a mis-scaled one returns
increments that spread over the wrong distance. Both look like tuning. So the
test is a dirac: a correlation applied to a delta returns exactly one at the
delta's own point, and falls to exp(-0.5) of that at the length scale it was
built with. Neither of those is a matter of taste.

What is asserted here is that `tools/soca-diffusion.sh` and `tools/soca-dirac.sh`
agree end to end on the real thing: python writes length scale fields, SOCA
builds an operator and normalizes it by randomization, and reading the result
back through the block in `config/layers/da/variational.yaml` reproduces the
scales that went in. `tests/test_diffusion.py` covers the two configurations
agreeing on paper and needs no model at all.

This leaves a real calibration behind in $ACKBAR_STATIC_ROOT/static/gom_25km,
and that is deliberate rather than tolerated. It runs at the iteration count in
`config/static/diffusion.yaml`, so what it writes is what the tool writes, and gom_25km
is the domain the suite already uses for plumbing. A test that calibrated
somewhere else would be exercising a path nothing else uses.

Opt in with `ACKBAR_TIER3=1`; needs `source site/activate.sh`, a built SOCA, the
imported gom_25km configuration and its gridspec.

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_diffusion.py
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
DOMAIN = "gom_25km"

#: The state the diracs are placed in, named rather than discovered. Left to
#: itself `soca-dirac.sh` searches the domain's initial conditions and refuses
#: to guess between several, which is right for a person at a terminal and wrong
#: here: what is under test is the operator, and a second product or a perturbed
#: ensemble appearing beside this one is not a change to it.
EXPERIMENT = REPO / "tests" / "experiments" / "tier3_gom.yaml"

#: Generous. The calibration is a quarter of a minute on this domain and the
#: point of the limit is not to sit through a hang.
TIMEOUT = 1800


def restart():
    """The experiment's initial condition, with this site's static root in it."""
    named = yaml.safe_load(EXPERIMENT.read_text())["model"]["initial_condition"]
    root = os.environ["ACKBAR_STATIC_ROOT"]
    return Path(named.replace("$(static_root)", root)) / "MOM.res.nc"


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real SOCA; set ACKBAR_TIER3=1")
    static = os.environ.get("ACKBAR_STATIC_ROOT")
    if not static:
        pytest.skip("run `source site/activate.sh` first")
    if not Path(static, "static", DOMAIN, "soca_gridspec.nc").exists():
        pytest.skip(f"run tools/soca-gridspec.sh {DOMAIN} first")
    if not Path(REPO, "pkg/jedi/build/bin/soca_error_covariance_toolbox.x").exists():
        pytest.skip("SOCA is not built")
    if not restart().exists():
        pytest.skip(f"{restart()} does not exist")


def run(tool, *args):
    done = subprocess.run(
        [str(REPO / "tools" / tool), DOMAIN, *args],
        capture_output=True, text=True, timeout=TIMEOUT,
        env={**os.environ, "ACKBAR_DIRAC_RESTART": str(restart())},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


@pytest.fixture(scope="module")
def calibrated():
    """The three files an analysis on this domain reads.

    The restart is named rather than left to the tool's discovery, and that is
    not a preference. Discovery refuses when the domain has more than one staged
    initial condition, which is correct of it and is the *normal* state of a
    domain anything real has been run on: the OSSE alone stages a synoptic cold
    start and a time-lagged control beside the one this experiment uses. A test
    that depended on there being exactly one was a test of the static root's
    contents, which every other experiment on the machine is entitled to change.
    """
    run("soca-diffusion.sh", str(restart()))
    return Path(os.environ["ACKBAR_STATIC_ROOT"], "static", DOMAIN, "diffusion")


def test_the_calibration_writes_what_the_analysis_names(calibrated):
    # `filepath` in saber is a stem and the file is the stem plus `.nc`, which
    # is the one thing about this stage that cannot be read off the config.
    for name in ("hz.nc", "hz_ssh.nc", "vt.nc"):
        assert (calibrated / name).stat().st_size > 0


def test_it_records_what_it_was_normalized_with(calibrated):
    """The generated documents are copied next to the output on purpose.

    A calibration built at a low iteration count is indistinguishable from a
    real one by inspecting the parameter files, and `--iterations` exists to
    build exactly that. These copies are the only record of which one a given
    `hz.nc` is.
    """
    hz = (calibrated / "calibrate_hz.yaml").read_text()
    assert "iterations:" in hz
    assert "implicit" in (calibrated / "calibrate_vt.yaml").read_text()


def test_the_operator_is_a_correlation_and_has_the_scales_it_was_given(calibrated):
    """The whole test. A dirac through B, read back and measured.

    `tools/soca-dirac.sh` exits non-zero if any peak is off 1 by more than its
    tolerance or any measured radius is off the requested scale by more than
    its own, so `run` asserting the exit status is most of the assertion. What
    is checked here on top of that is that it actually placed diracs and
    actually measured them, because a report over an empty point set would
    exit zero.
    """
    out = run("soca-dirac.sh")
    assert "the operator is normalized and the scales are the ones asked for" in out

    peaks = [float(value) for value in re.findall(r"^  \S.*?\s+([01]\.\d{4})\s", out,
                                                  re.MULTILINE)]
    assert len(peaks) >= 4, out
    assert all(abs(peak - 1.0) < 0.05 for peak in peaks), peaks


def test_a_named_location_is_honoured(calibrated):
    """The default pair is chosen from the grid; a stated pair must not be.

    Somewhere in the middle of the Gulf, deep, and far enough from the two
    cells the tool picks by itself that a match here is not a coincidence.
    """
    out = run("soca-dirac.sh", "25.0,-90.0")
    placed = re.findall(r"nearest wet cell to 25\.0,-90\.0", out)
    assert len(placed) == 2, out
