"""The YAML 1.1 / 1.2 numeric hazard.

PyYAML is YAML 1.1 and requires an explicit exponent sign; eckit is YAML 1.2 and
does not. So a threshold written the way soca-science wrote it is a string to
ACKBAR and a number to JEDI.
"""

import pytest
import yaml

from ackbar.config.lint import ambiguous_numbers


@pytest.mark.parametrize("value", ["500e3", "5.0e5", "1e-3", "+2e10", ".5e3"])
def test_flags_values_pyyaml_reads_as_strings(value):
    assert isinstance(yaml.safe_load(value), str), "premise of the test"
    assert ambiguous_numbers({"k": value}) == [("k", value)]


@pytest.mark.parametrize("value", ["5.0e+5", "500000", "0.5", "-2.0", ".inf"])
def test_ignores_values_pyyaml_already_reads_as_numbers(value):
    assert not isinstance(yaml.safe_load(value), str), "premise of the test"
    assert ambiguous_numbers({"k": value}) == []


@pytest.mark.parametrize("value", [
    "RoundRobin", "PT24H", "00:30:00", "2018-04-15T00:00:00Z",
    "$(obs_land_mask_min)", "MOM.res.nc", "16G",
])
def test_leaves_ordinary_config_strings_alone(value):
    assert ambiguous_numbers({"k": value}) == []


def test_reports_the_path_through_lists_and_dicts():
    config = {"observations": [
        {"obs space": {"name": "adt"}},
        {"obs space": {"name": "sst", "distribution": {"halo size": "500e3"}}},
    ]}
    assert ambiguous_numbers(config) == [
        ("observations[1].obs space.distribution.halo size", "500e3"),
    ]


def test_finds_every_occurrence():
    assert len(ambiguous_numbers({"a": "1e5", "b": {"c": "2e5"}})) == 2


def test_numbers_that_are_already_numbers_are_not_strings_and_are_ignored():
    assert ambiguous_numbers({"a": 500000, "b": 0.5, "c": True, "d": None}) == []
