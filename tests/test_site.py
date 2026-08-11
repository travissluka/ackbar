"""Reading the site layer out of the environment site/activate.sh exports."""

import pytest

from ackbar.site import SiteError, load_site


def test_required_roots_are_read():
    site = load_site({
        "ACKBAR_SCRATCH_ROOT": "/scratch/ackbar",
        "ACKBAR_OUTPUT_ROOT": "/project/ackbar",
    })
    assert site["scratch_root"] == "/scratch/ackbar"
    assert site["output_root"] == "/project/ackbar"


def test_optional_settings_are_carried_when_present():
    site = load_site({
        "ACKBAR_SCRATCH_ROOT": "/s",
        "ACKBAR_OUTPUT_ROOT": "/o",
        "ACKBAR_SITE": "rancor",
        "ACKBAR_LAUNCHER": "mpiexec",
        "ACKBAR_QOS": "batch",
    })
    assert site["site"] == "rancor"
    assert site["launcher"] == "mpiexec"
    assert site["qos"] == "batch"


def test_optional_settings_are_absent_rather_than_empty():
    site = load_site({"ACKBAR_SCRATCH_ROOT": "/s", "ACKBAR_OUTPUT_ROOT": "/o"})
    assert "launcher" not in site


def test_a_missing_root_says_how_to_fix_it():
    with pytest.raises(SiteError) as caught:
        load_site({"ACKBAR_SCRATCH_ROOT": "/s"})
    message = str(caught.value)
    assert "ACKBAR_OUTPUT_ROOT" in message
    assert "site/activate.sh" in message


def test_an_empty_value_counts_as_unset():
    with pytest.raises(SiteError):
        load_site({"ACKBAR_SCRATCH_ROOT": "/s", "ACKBAR_OUTPUT_ROOT": ""})
