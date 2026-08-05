"""A fixed site for the whole suite.

Tiers 0 and 1 need python and nothing else, which has to include not needing
`source site/activate.sh` first. It also has to include not depending on which
machine the suite runs on: a test that passes on rancor because rancor's
scratch root happens to be where it is would be a test of rancor.

`site.load_site` takes an explicit environment, so the tests that are about the
site layer itself are unaffected by this.
"""

import pytest

#: Deliberately not any real machine's roots.
SITE = {
    "ACKBAR_SITE": "test",
    "ACKBAR_SCRATCH_ROOT": "/scratch",
    "ACKBAR_OUTPUT_ROOT": "/out",
    "ACKBAR_MAX_SUBMIT_JOBS": "10000",
    "ACKBAR_MAX_ARRAY_SIZE": "1000",
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "tier2: needs a real Slurm and the real site; see docs/build-order.md",
    )


@pytest.fixture(autouse=True)
def fixed_site(request, monkeypatch):
    if request.node.get_closest_marker("tier2"):
        # Tier 2 submits to a real scheduler, and the jobs it submits read the
        # site the same way any other job does. Pinning a fake site here would
        # give the test one set of roots and its own jobs another.
        return
    for key, value in SITE.items():
        monkeypatch.setenv(key, value)
    # ACKBAR_ROOT decides where executables are looked for. Leaving whatever
    # the shell exported would make the executable check depend on which
    # checkout was activated last.
    monkeypatch.delenv("ACKBAR_ROOT", raising=False)
