"""Tier 0: what `tools/obs-archive-osse.py` refuses, and how it says so.

Only the refusals. Generating an archive needs a domain's gridspec and a
restart, which is tier 3's business and is covered there by every experiment
that reads one. What is covered here is the boundary between the two truth
modes, because that boundary is where this tool has actually failed.

It failed twice, in the same week and for the same reason: `--state` holds one
model state plus a two dimensional anomaly, several platforms need more than
that, and nothing checked which. The profile platforms got a clean sentence and
everything else got a traceback several hundred lines downstream, or worse a
`TypeError` about argument counts that named nothing at all. A refusal nobody
exercises is a refusal that rots, so these run without the model, without a
domain, and in under a second.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "obs-archive-osse.py"


def refuse(platforms, tmp_path):
    """Run the tool with `--state` and return what it printed.

    The state path does not exist and does not need to: every refusal here is
    decided from the platform table before anything is read, and that ordering
    is itself worth pinning. A refusal that only fired after a gridspec had been
    opened would be useless to someone choosing platforms on a machine that has
    neither.
    """
    done = subprocess.run(
        [sys.executable, str(TOOL),
         "--domain", "gom_25km",
         "--state", str(tmp_path / "there-is-no-state-here"),
         "--start", "2015-01-05T00:00:00Z", "--bin", "P1D", "--count", "1",
         "--platforms", *platforms,
         "--out", str(tmp_path / "out")],
        capture_output=True, text=True,
    )
    assert done.returncode != 0, done.stdout
    return done.stdout + done.stderr


@pytest.mark.parametrize("platform,because", [
    ("argo_t", "profile"),
    ("glider_s", "glider"),
    ("drifter_sst", "drifter"),
])
def test_a_layout_a_single_state_cannot_place_is_refused_by_name(
        platform, because, tmp_path):
    """Three layouts, one sentence, and the layout named in it.

    A drifter is advected by the truth's own velocities and a profile or glider
    reads down a column. None of that exists in one state, and the refusal has
    to name the layout rather than an implementation detail the caller has no
    way to connect to the choice they got wrong.
    """
    said = refuse([platform], tmp_path)
    assert platform in said
    assert because in said
    assert "--truth-run" in said


def test_a_field_the_anomaly_does_not_carry_is_refused_by_name(tmp_path):
    """`perturb` builds sea surface temperature and height. Salinity is neither.

    The refusal has to come before the archive directory and `truth.nc` are
    written, or a rejected request leaves a half built archive behind that looks
    like an interrupted run.
    """
    said = refuse(["sss_smap"], tmp_path)
    assert "sss_smap" in said and "sss" in said
    assert "--truth-run" in said


def test_every_refused_platform_is_listed_and_not_only_the_first(tmp_path):
    """Because choosing platforms is iterative, and one at a time is a bad loop."""
    said = refuse(["sss_smap", "argo_t", "drifter_sst"], tmp_path)
    for platform in ("sss_smap", "argo_t", "drifter_sst"):
        assert platform in said


def test_an_unknown_platform_is_refused_before_anything_else(tmp_path):
    """A typo should not be reported as a truth mode problem."""
    said = refuse(["sst_nosuchsat"], tmp_path)
    assert "sst_nosuchsat" in said
