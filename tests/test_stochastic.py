"""Tier 0: the stochastic physics a member's forecast is given.

What the model does with it is tier 3's problem and, past that, the ensemble's.
What is checkable here in milliseconds is the part that is easy to get wrong and
silent when it is: whether the two halves agree, whether two members ever share
a draw, and whether a rerun reproduces one.
"""

from pathlib import Path

import pytest

from ackbar import stochastic
from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import GraphError, build_graph

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"
SITE = {"scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static", "root": str(REPO)}

SPPT = {"amplitude": 0.8, "length_scale": 500000.0, "timescale": "PT6H"}
BLOCK = {"seed": 20150712, "sppt": SPPT}


# --- the two halves ----------------------------------------------------------

def test_each_scheme_switches_on_in_mom6_and_in_the_generator_together():
    # `init_stochastic_physics_ocn` compares them and returns an error code that
    # `MOM_stochastics` turns into a FATAL, so half of a scheme is a run that
    # does not start rather than one that quietly does nothing.
    text = stochastic.parameters(BLOCK)
    group = stochastic.namelist(BLOCK, member=1, cycle=1)
    assert "DO_SPPT = True" in text and "ocnsppt = 0.8" in group


def test_the_two_halves_agree_about_an_empty_scheme_rather_than_disagreeing():
    # The schema rejects this, and is one `required:` away from not rejecting
    # it. Disagreeing is the failure mode that costs a whole cycle: the
    # generator refuses the run rather than skipping the scheme.
    empty = {"seed": 1, "sppt": {}}
    assert "DO_SPPT" not in stochastic.parameters(empty)
    assert "ocnsppt =" not in stochastic.namelist(empty, member=1, cycle=1)


def test_the_timescale_reaches_the_generator_in_seconds():
    # ACKBAR writes durations as ISO 8601 everywhere and the generator's
    # namelist is seconds, so this conversion is the whole interface.
    group = stochastic.namelist({"seed": 1, "sppt": SPPT}, member=1, cycle=1)
    assert "ocnsppt_tau = 21600.0" in group


def test_the_length_scale_is_an_e_folding_length_and_not_twice_one():
    """`new_lscale` is what makes `length_scale` mean what it says.

    Without it `setvarspect` puts `L**2 / 4` in the variance spectrum's exponent
    rather than `(L / 4)**2`, and the pattern comes out twice as wide as asked
    for. On a basin a few length scales across that is the difference between a
    field of errors and one multiplier with a gradient across it.
    """
    assert "new_lscale = .true." in stochastic.namelist(BLOCK, member=1, cycle=1)


def test_the_group_is_a_namelist_group_the_generator_can_read():
    group = stochastic.namelist(BLOCK, member=1, cycle=1)
    assert group.startswith("&nam_stochy\n") and group.endswith("/\n")


# --- the seed ----------------------------------------------------------------

def test_no_two_members_of_a_cycle_share_a_seed():
    seeds = {stochastic.seed(20150712, member, 7, "sppt") for member in range(21)}
    assert len(seeds) == 21


def test_no_two_cycles_of_a_member_share_a_seed():
    seeds = {stochastic.seed(20150712, 3, cycle, "sppt") for cycle in range(1, 46)}
    assert len(seeds) == 45


def test_a_seed_is_never_zero_because_zero_means_ask_the_clock():
    assert stochastic.seed(1, 0, 0, "sppt") > 0


def test_rerunning_a_cycle_reproduces_the_draw_it_had():
    # What `ackbar heal` rests on: the failed attempt and the healed one have to
    # be the same forecast.
    assert (stochastic.seed(20150712, 4, 12, "sppt")
            == stochastic.seed(20150712, 4, 12, "sppt"))


def test_two_experiments_that_differ_only_in_the_base_seed_are_independent():
    assert (stochastic.seed(20150712, 4, 12, "sppt")
            != stochastic.seed(20150713, 4, 12, "sppt"))


def test_a_member_number_the_seed_cannot_encode_is_refused():
    # Rather than silently colliding with another member's seed.
    with pytest.raises(stochastic.StochasticError):
        stochastic.seed(20150712, 1000, 1, "sppt")


# --- which members ------------------------------------------------------------

def test_every_member_but_the_control_is_perturbed():
    assert not stochastic.perturbs(0)
    assert all(stochastic.perturbs(member) for member in range(1, 21))


# --- the configuration --------------------------------------------------------

def test_an_experiment_with_no_ensemble_has_no_stochastic_physics():
    assert stochastic.settings({"model": {"name": "mom6sis2"}}) is None
    assert stochastic.settings({"ensemble": {"size": 4}}) is None


def test_stochastic_physics_on_a_model_that_integrates_nothing_is_refused(letkf):
    # `stub_letkf` runs the stub model, so this is the config as written rather
    # than one edited into an unlikely shape.
    letkf["ensemble"]["stochastic"] = BLOCK
    with pytest.raises(GraphError, match=letkf["model"]["name"]):
        build_graph(letkf)


def test_an_ensemble_of_real_forecasts_takes_it(letkf):
    letkf["model"]["name"] = "mom6sis2"
    letkf["ensemble"]["stochastic"] = BLOCK
    build_graph(letkf)


@pytest.fixture
def letkf():
    layers = resolve_layers(EXPERIMENTS / "stub_letkf.yaml", LAYERS)
    return resolve(merge_layers(layers, merge_keys(load_schema())), SITE)
