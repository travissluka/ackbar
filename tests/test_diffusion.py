"""Tier 0: the two ends of the diffusion calibration have to describe one operator.

`config/static/diffusion.yaml` says what `tools/soca-diffusion.sh` builds.
`config/layers/da/variational.yaml` says what the analysis reads back. Nothing
at runtime compares them, and nothing can: saber reads the normalization out of
a file and applies it, and a normalization computed for one operator applied
through another is wrong by a smooth factor that looks like a tuning choice
rather than like a bug. These tests are the join.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DIFFUSION = REPO / "config" / "static" / "diffusion.yaml"
VARIATIONAL = REPO / "config" / "layers" / "da" / "variational.yaml"
HYBRID = REPO / "config" / "layers" / "da" / "hybrid.yaml"

#: The per-cycle vertical calibration, which is a *third* place the operator is
#: spelled and the one the two fixtures above do not reach. Under
#: `da/corr_vt_cycled` the normalization the analysis reads is not the offline
#: one at all: `b.corr_vt` rebuilds it every cycle through this document, whose
#: `method` and `iterations` are literals. The file says in as many words that
#: they must stay equal to the other two, which is what makes it a join rather
#: than a preference.
CYCLED_VT = REPO / "config" / "soca" / "vt.yaml"
CORR_VT_CYCLED = REPO / "config" / "layers" / "da" / "corr_vt_cycled.yaml"

#: The layer vars a `filepath` in this file's fixtures may be written in terms
#: of. Only `da/hybrid` declares any: `localization_hz` is how an experiment
#: chooses between the masked and the unmasked localization, so the stem the
#: shipped layer names is a symbol rather than a file name.
LAYER_VARS = yaml.safe_load(HYBRID.read_text()).get("vars") or {}


@pytest.fixture(scope="module")
def calibration():
    return yaml.safe_load(DIFFUSION.read_text())


@pytest.fixture(scope="module")
def groups():
    """Every diffusion `read` group any layer configures.

    Two layers, because the calibration writes for two purposes: the correlation
    of the static B, and the localization of the ensemble component. They are
    the same operator reading different scale fields out of the same directory,
    so a group that reads a file nothing calibrates is the same failure either
    way, and it is one list here.
    """
    static = yaml.safe_load(VARIATIONAL.read_text())["solver"]
    central = static["background error"]["saber central block"]
    assert central["saber block name"] == "diffusion"

    hybrid = yaml.safe_load(HYBRID.read_text())["solver"]
    localization = hybrid["ensemble error"]["localization"]["saber central block"]
    assert localization["saber block name"] == "diffusion"

    return central["read"]["groups"] + localization["read"]["groups"]


def calibrated(groups, kind):
    """Every horizontal or vertical block that reads a calibration.

    A block with no `filepath` reads no file and configures no operator, which
    is what `strategy: duplicated` is: the localization applies the same
    horizontal structure at every level and does not localize in the vertical at
    all. It is a real configuration rather than an omission, so it is skipped
    here rather than asserted about.
    """
    return [group[kind] for group in groups
            if kind in group and "filepath" in group[kind]]


def stems(groups, kind):
    """The `filepath` of every calibrated block, basename only.

    saber's `filepath` is a stem: the file it opens is this plus `.nc`. What is
    compared here is the last component, because the directory is the domain's
    and the name is the calibration's.

    A stem may be a `$(var)`, which is how the localization is selected, so the
    layer's own `vars` are applied first. Only the layer's defaults: an
    experiment may restate the var, and what is checked here is that the
    configuration as shipped names files the calibration writes.
    """
    names = set()
    for block in calibrated(groups, kind):
        name = block["filepath"].rsplit("/", 1)[-1]
        for var, value in LAYER_VARS.items():
            name = name.replace(f"$({var})", str(value))
        names.add(name)
    return names


def test_every_horizontal_file_the_analysis_reads_is_one_the_calibration_writes(
        calibration, groups):
    """A subset, not an equality, and the asymmetry is the point.

    Reading a stem the calibration does not write is a missing file discovered
    by a queued job, so it is checked. The other direction is not an error any
    more: `loc_hz` and `loc_hz_open` are two calibrations of the same
    localization differing only in whether land is masked, an experiment selects
    between them with `localization_hz`, and only the default appears in the
    shipped layers. An entry nothing selects costs a minute of calibration and
    a hundred kilobytes; `test_both_localizations_stay_calibrated` is what keeps
    the unselected one from being quietly deleted instead.
    """
    assert stems(groups, "horizontal") <= set(calibration["horizontal"])


def test_the_vertical_file_the_analysis_reads_is_the_one_the_calibration_writes(
        calibration, groups):
    assert stems(groups, "vertical") == (
        {"corr_vt"} if calibration.get("vertical") else set())


def test_the_analysis_applies_the_vertical_scheme_the_normalization_was_built_with(
        calibration, groups):
    """The failure this exists for is silent and large.

    `corr_vt.nc` holds a normalization estimated by running the configured operator
    on a dirac in every level. Read it back through the explicit scheme when it
    was written with the implicit one and every vertical increment is scaled by
    the ratio of two kernels, which is a factor of order one that varies with
    depth. Nothing reports it. `tools/soca-dirac.sh` is what would catch it
    after the fact, and it needs a calibrated domain to run.
    """
    wanted = calibration["vertical"]
    for block in calibrated(groups, "vertical"):
        assert block["method"] == wanted["method"]
        assert block["iterations"] == wanted["iterations"]


def test_the_horizontal_is_left_explicit(calibration, groups):
    """Explicit is saber's default and is stated nowhere, which is the point.

    The horizontal scales are a few grid cells, where the explicit scheme's
    iteration count is small and its kernel is the Gaussian the scales were
    derived as. Setting a method here would be a change of physics that reads
    like a change of spelling, so what is asserted is that neither end says
    anything: `config/static/diffusion.yaml` has no `method` under `horizontal`, and
    the analysis's horizontal blocks carry only a filepath.
    """
    for spec in calibration["horizontal"].values():
        assert "method" not in spec
    for group in groups:
        assert set(group.get("horizontal", {})) <= {"filepath"}


def test_the_vertical_iteration_count_is_even(calibration):
    """saber requires it, and rejects an odd one with an exception at read time.

    Which is to say: after a queued job has started, built a geometry and read a
    background.
    """
    iterations = calibration["vertical"]["iterations"]
    assert iterations > 0 and iterations % 2 == 0


def test_the_cycled_calibration_builds_the_operator_the_analysis_reads(calibration):
    """The same join as above, through the door the two fixtures do not reach.

    Under `da/corr_vt_cycled` the vertical normalization the analysis reads is
    not the offline one: `b.corr_vt` rebuilds it every cycle with
    `config/soca/vt.yaml`, whose `method` and `iterations` are literals because
    they used to be substituted from the experiment's own solver block and are
    not any more. So the pair that
    `test_the_analysis_applies_the_vertical_scheme_the_normalization_was_built_with`
    guards has a third member, and raising the scheme in the two files that test
    covers would leave every cycled experiment writing through implicit/2 and
    reading back through whatever the new value is. That is the same silent
    depth-varying factor, reintroduced by a change that the suite calls green.
    """
    document = yaml.safe_load(CYCLED_VT.read_text())
    groups = document["background error"]["saber central block"]["calibration"]["groups"]
    wanted = calibration["vertical"]
    for group in groups:
        assert group["vertical"]["method"] == wanted["method"]
        assert group["vertical"]["iterations"] == wanted["iterations"]


def test_the_cycled_floor_is_the_one_the_offline_calibration_used(calibration):
    """`corr_vt_cycled` blends against the offline scale field, so the floors must match.

    Cycle 1 seeds its rolling average from `scales_corr_vt.nc`, which the
    offline stage wrote with `vertical.min`. If the layer's own floor differs,
    the first blend mixes two fields built to different floors and carries the
    mixture forward for the rest of the experiment, with nothing reporting it.
    The layer states the requirement in its comments; this is the check.
    """
    cycled = yaml.safe_load(CORR_VT_CYCLED.read_text())["solver"]
    assert cycled["vertical correlation floor"] == calibration["vertical"]["min"]


def test_the_vertical_operator_spans_the_levels_the_model_carries(calibration):
    """`diffusion_levels` and MOM6's `NK` are two literals with nothing joining them.

    Changing one without the other is the worst failure mode in this file
    because it does not fail. saber is handed a scale field with one level count
    and a geometry with another, and what it does is spin at full CPU on every
    rank, writing nothing and raising nothing, until the job's walltime kills
    it. There is no error to read afterwards.

    Only the gom family is checked, and that is a statement about ownership
    rather than an omission: this repository carries `MOM_input` for those
    domains, so both numbers are here and can be compared. `om_1deg` takes
    upstream's MOM6-examples configuration and overrides it, so its `NK` is not
    in this tree and there is nothing here to join it to.
    """
    base = REPO / "config" / "model" / "mom6sis2" / "domain" / "gom" / "common"
    nk = None
    for line in (base / "MOM_input").read_text().splitlines():
        head = line.split("!", 1)[0]
        if head.split("=")[0].strip() == "NK":
            nk = int(head.split("=", 1)[1].strip())
    assert nk is not None, f"no NK in {base / 'MOM_input'}"

    gom = yaml.safe_load((REPO / "config" / "layers" / "domain" / "common"
                          / "gom.yaml").read_text())
    assert gom["vars"]["diffusion_levels"] == nk


def test_the_scales_are_relative_to_the_grid_and_not_to_a_domain(calibration):
    """Why this file has no per-domain section, asserted rather than commented.

    Every horizontal entry is a multiple of the Rossby radius floored by a
    multiple of the cell size. Both of those are fields the gridspec carries, so
    one file gives a 4 km grid and a 25 km grid different scales in metres
    without either being named. An absolute length appearing here without its
    relative pair is what would break that.
    """
    for spec in calibration["horizontal"].values():
        assert set(spec) - {"masked"} == {"rossby mult", "min grid mult", "max"}


def test_only_the_localization_is_unmasked(calibration):
    """Masking is per entry, and exactly one entry asks for no mask.

    A correlation with `masked: false` would be a background error that
    communicates through land, which is a change of physics that reads as a
    change of spelling: the calibration still runs, the analysis still solves,
    and increments cross a peninsula. So the *set* is pinned rather than the
    default, and adding the key to `corr_hz`, `corr_hz_ssh` or `loc_hz` is a
    failure here rather than a discovery in a dirac plot.
    """
    unmasked = {name for name, spec in calibration["horizontal"].items()
                if spec.get("masked", True) is False}
    assert unmasked == {"loc_hz_open"}


def test_both_localizations_stay_calibrated(calibration):
    """The pair exists so that masking is the only difference between them.

    `loc_hz` is what every EnVar and hybrid result on disk was run against and
    what an experiment gets back by restating `localization_hz`; `loc_hz_open`
    is the default. If they ever differ in a multiplier, a run of each stops
    being a measurement of the mask and becomes a measurement of two changes at
    once, and nothing downstream would say which.
    """
    horizontal = calibration["horizontal"]
    masked, unmasked = horizontal["loc_hz"], horizontal["loc_hz_open"]
    assert masked.get("masked", True) is True
    numbers = ("rossby mult", "min grid mult", "max")
    assert [masked[key] for key in numbers] == [unmasked[key] for key in numbers]


def test_the_localization_the_layers_ship_is_the_unmasked_one(calibration):
    """The default, asserted, because it decides what every EnVar reads.

    A localization is a taper in a Schur product: it never carries a state value
    anywhere, so masking it buys no protection and costs the ensemble's genuine
    cross-coast structure, and it truncates the kernel the normalization is
    estimated from at every cell within a scale length of a coast. That is the
    ruling `config/static/diffusion.yaml` records. Reverting it silently, by
    editing one var, would leave the layers reading a file that still exists.
    """
    selected = LAYER_VARS["localization_hz"]
    assert selected == "loc_hz_open"
    assert calibration["horizontal"][selected].get("masked", True) is False
