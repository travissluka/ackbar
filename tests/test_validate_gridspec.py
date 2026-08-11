"""Tier 1: what `validate` step 3 asks of a domain's gridspec.

Three questions, and they are separate on purpose. Does the file say which faces
its staggered fields are on? Does it say what its domain layer says? And is the
face set it reports one this experiment's model can actually integrate on?

The third is the one worth stating plainly, because it is the only check in
ACKBAR that refuses a combination where both halves are individually correct. A
global domain's gridspec on the east and north faces is honest: that is where
`soca_gridgen.x` left them and the shift onto the west and south faces cannot be
expressed on a grid whose outermost row and column are open ocean. A `mom6sis2`
forecast is honest too. Together they are a velocity analysis a half cell from
its own mask, with nothing downstream that reports it, so this is where the pair
is stopped. See `src/ackbar/gridspec.py`.

Against `_gridspec_step` directly rather than through `validate_experiment`: the
step reads two keys out of the merged config and stats one file, and building a
whole experiment around it would test the fixture more than the rule.

**If you got here because a test of yours started failing on this finding**, the
seam you have hit is real and is not a bug in the check. Every `om_1deg` fixture
under `tests/experiments/` declares `model/mom6sis2`, and several exist precisely
to exercise mom6sis2-shaped config, so they are right as they are. What has never
happened until now is pointing the *six-step* validate at one of them: the
fixtures are exercised by `ackbar.config.schema.validate`, which is a different
function and checks only the shape of the config. The moment something calls
`validate_experiment` on an `om_1deg` fixture, this finding fires, correctly, and
says what it would mean to run that experiment for real.

The fix is to give that test a domain whose faces are recorded as `west/south`,
which is any of the Gulf domains, or to keep it on the schema validator if the
config's shape is what it is really about. It is not to relax the condition here
or to declare `staggered_faces` somewhere to quiet it. A velocity analysis a half
cell from its own mask produces a complete run and a wrong answer, and this is
the only thing in ACKBAR that reports it.
"""

from pathlib import Path

import pytest

netCDF4 = pytest.importorskip("netCDF4")

from ackbar.gridspec import (  # noqa: E402
    STAGGER_ATTR, STAGGER_VALUE, STAGGER_VALUE_GENERATED)
from ackbar.soca import GRIDSPEC  # noqa: E402
from ackbar.validate import _gridspec_step  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def gridspec(directory, faces):
    """An otherwise empty gridspec carrying *faces*, or none if *faces* is None.

    The step reads the attribute and nothing else in the file, so there is no
    grid here. `test_gridspec_stagger.py` is where the arrays are checked.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / GRIDSPEC
    with netCDF4.Dataset(path, "w") as data:
        if faces is not None:
            data.setncattr(STAGGER_ATTR, faces)
    return path


def config(static, faces=None, model="mom6sis2"):
    domain = {"name": "test", "static": str(static)}
    if faces is not None:
        domain["staggered_faces"] = faces
    return {"domain": domain, "model": {"name": model}}


class TestTheFaceSetIsRecorded:
    """A gridspec that will not say where its staggered fields are does not run."""

    def test_a_gridspec_with_no_attribute_is_refused(self, tmp_path):
        gridspec(tmp_path, None)
        findings = _gridspec_step(config(tmp_path))
        assert len(findings) == 1
        assert STAGGER_ATTR in findings[0].message

    def test_a_shifted_gridspec_passes(self, tmp_path):
        gridspec(tmp_path, STAGGER_VALUE)
        assert _gridspec_step(config(tmp_path)) == []

    def test_a_domain_with_no_static_is_not_this_step_s_business(self):
        assert _gridspec_step({"domain": {"name": "stub"}}) == []

    def test_a_missing_gridspec_is_left_to_the_path_check(self, tmp_path):
        # Said better by "input path does not exist" than by anything here.
        assert _gridspec_step(config(tmp_path)) == []


class TestTheFileAgreesWithTheDomainLayer:
    """The gridspec is built once per domain, so a later edit to the layer leaves
    the file behind with no other symptom."""

    def test_a_generated_file_under_a_shifting_domain_is_refused(self, tmp_path):
        # The trap the whole face-set attribute exists for, reached the other
        # way round: a regional domain whose gridspec never got the shift.
        gridspec(tmp_path, STAGGER_VALUE_GENERATED)
        findings = _gridspec_step(config(tmp_path, model="stub"))
        assert len(findings) == 1
        assert "stale" in findings[0].message

    def test_a_shifted_file_under_a_generated_domain_is_refused(self, tmp_path):
        gridspec(tmp_path, STAGGER_VALUE)
        findings = _gridspec_step(
            config(tmp_path, faces=STAGGER_VALUE_GENERATED, model="stub"))
        assert len(findings) == 1
        assert "stale" in findings[0].message

    def test_absent_means_the_shift_is_required(self, tmp_path):
        """The default is the safe one: a domain layer that has never considered
        the question must fail rather than skip the shift."""
        gridspec(tmp_path, STAGGER_VALUE_GENERATED)
        assert _gridspec_step(config(tmp_path, model="stub")) != []


class TestTheGeneratedFaceSetUnderARealModel:
    """The `om_1deg` pairing, and the reason the declaration is not an exemption."""

    def test_a_real_forecast_model_is_refused(self, tmp_path):
        gridspec(tmp_path, STAGGER_VALUE_GENERATED)
        findings = _gridspec_step(
            config(tmp_path, faces=STAGGER_VALUE_GENERATED, model="mom6sis2"))
        assert len(findings) == 1
        assert "half cell" in findings[0].message

    @pytest.mark.parametrize("model", ["stub", "persistence"])
    def test_a_model_that_reads_no_velocity_is_allowed(self, tmp_path, model):
        """What keeps `om_1deg` usable: its live work is the graph fixtures."""
        gridspec(tmp_path, STAGGER_VALUE_GENERATED)
        assert _gridspec_step(
            config(tmp_path, faces=STAGGER_VALUE_GENERATED, model=model)) == []

    def test_a_shifted_domain_under_a_real_model_is_the_normal_case(self, tmp_path):
        gridspec(tmp_path, STAGGER_VALUE)
        assert _gridspec_step(
            config(tmp_path, faces=STAGGER_VALUE, model="mom6sis2")) == []


class TestTheDomainLayersOnDisk:
    """The declarations themselves, because the schema constrains the value but
    nothing otherwise checks that the one domain that needs it has it."""

    def test_om_1deg_declares_the_generated_faces(self):
        import yaml
        layer = yaml.safe_load(
            open(REPO / "config" / "layers" / "domain" / "om_1deg.yaml"))
        assert layer["domain"]["staggered_faces"] == STAGGER_VALUE_GENERATED

    @pytest.mark.parametrize("domain", ["gom_25km", "gom_12km", "gom_8km", "gom_4km"])
    def test_a_regional_domain_declares_nothing_and_so_requires_the_shift(self, domain):
        import yaml
        layer = yaml.safe_load(
            open(REPO / "config" / "layers" / "domain" / f"{domain}.yaml"))
        assert "staggered_faces" not in (layer.get("domain") or {})
