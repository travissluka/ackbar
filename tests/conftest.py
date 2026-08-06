"""A fixed site for the whole suite.

Tiers 0 and 1 need python and nothing else, which has to include not needing
`source site/activate.sh` first. It also has to include not depending on which
machine the suite runs on: a test that passes on rancor because rancor's
scratch root happens to be where it is would be a test of rancor.

`site.load_site` takes an explicit environment, so the tests that are about the
site layer itself are unaffected by this.
"""

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from ackbar.duration import parse_instant
from ackbar.paths import Paths

#: The committed experiment definitions tiers 2 and 3 run.
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

#: Deliberately not any real machine's roots.
SITE = {
    "ACKBAR_SITE": "test",
    "ACKBAR_SCRATCH_ROOT": "/scratch",
    "ACKBAR_OUTPUT_ROOT": "/out",
    # What the offline stages produce. Here because the domain layers name it
    # for their static stage, so without it an experiment on a real domain fails
    # to resolve rather than failing to find a file.
    "ACKBAR_STATIC_ROOT": "/static",
    "ACKBAR_MAX_SUBMIT_JOBS": "10000",
    "ACKBAR_MAX_ARRAY_SIZE": "1000",
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "tier2: needs a real Slurm and the real site; see docs/build-order.md",
    )
    config.addinivalue_line(
        "markers",
        "tier3: needs the built model and ACKBAR_TIER3=1; takes a quarter hour",
    )


@pytest.fixture(autouse=True)
def fixed_site(request, monkeypatch):
    if any(request.node.get_closest_marker(t) for t in ("tier2", "tier3")):
        # These submit to a real scheduler, and the jobs they submit read the
        # site the same way any other job does. Pinning a fake site here would
        # give the test one set of roots and its own jobs another.
        return
    for key, value in SITE.items():
        monkeypatch.setenv(key, value)
    # ACKBAR_ROOT decides where executables are looked for. Leaving whatever
    # the shell exported would make the executable check depend on which
    # checkout was activated last.
    monkeypatch.delenv("ACKBAR_ROOT", raising=False)


#: The cycle a bare `Paths` is built against, for the tests that construct one
#: directly rather than out of a config.
#:
#: `Paths` needs `start` and `length` because every directory it names is keyed
#: by a date computed from them. A test that only cares about the *shape* of a
#: path still has to supply them, so it supplies these, and a test that cares
#: which date builds its own.
PATHS_CYCLE = {
    "start": parse_instant("2018-04-15T00:00:00Z"),
    "length": timedelta(hours=24),
}


def experiment_paths(name, site):
    """`Paths` for a committed test experiment, created or not.

    `Paths` is keyed by cycle dates, so it needs a config. The frozen one is
    authoritative once the experiment exists; before that the fixture it will be
    created from says the same thing, which is what lets a purge run against an
    experiment that was never created.
    """
    frozen = Path(site["output_root"]) / name / "cfg" / "experiment.yaml"
    source = frozen if frozen.exists() else EXPERIMENTS / f"{name}.yaml"
    return Paths.of(yaml.safe_load(source.read_text()), site)
