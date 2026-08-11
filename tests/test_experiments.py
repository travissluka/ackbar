"""Tier 1: the committed experiment definitions in `experiments/`.

**Nothing loaded these until this file existed**, which is how three of them
came to be uncreatable at once. Every other test in the suite resolves a fixture
from `tests/experiments/`, so the twelve definitions that are the actual
deliverable were the one part of the configuration nothing exercised: a layer
edit could change the merged shape of an experiment and be found by running it,
weeks later, or not at all.

The failure that prompted this is worth naming, because it is the shape the rest
will take. A forcing source stages its atmosphere through `ensemble.inputs`, so
inheriting one introduces an `ensemble` block, and the schema requires `size`
inside it. The three deterministic arms inherited a source and declared no size,
so `ackbar validate` refused them and `ackbar graph` died on a raw `KeyError`.
Nothing was wrong with any layer in isolation.

These are cheap: no site, no Slurm, no data, no bundle. They resolve the layer
tree and read the merged result, so they run in seconds and are worth keeping
even as the set of experiments changes.

The assertions past the first two are the invariants that have so far been
established by editing every file by hand and hoping. Each one names the
comparison it protects, because an invariant nothing can state is one the next
sweep silently drops.
"""

from pathlib import Path

import pytest
import yaml

from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import build_graph
from ackbar.validate import validate_experiment

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = REPO / "experiments"

SITE = {
    "scratch_root": "/scratch",
    "output_root": "/out",
    "static_root": "/static",
    "max_submit_jobs": "10000",
    "max_array_size": "1000",
    "root": str(REPO),
}

#: The eight arms of the comparison matrix. Named rather than discovered,
#: because "every experiment that is not a staging run" is a rule that quietly
#: absorbs the next experiment somebody adds for an unrelated reason, and the
#: paired-difference invariants below only hold across a matrix somebody meant
#: to compare.
ARMS = (
    "osse25-noda",
    "osse25-3dvar",
    "osse25-3dfgat",
    "osse25-4dletkf",
    "osse25-envar",
    "osse25-4denvar",
    "osse25-hybrid",
    "osse25-4dhybrid",
)


def definitions():
    return sorted(EXPERIMENTS.glob("*.yaml"))


def load(path, keys):
    return resolve(merge_layers(resolve_layers(path, LAYERS), keys), SITE)


@pytest.fixture(scope="module")
def keys():
    return merge_keys(load_schema())


@pytest.fixture(scope="module")
def schema():
    return load_schema()


@pytest.fixture(scope="module")
def configs(keys):
    """Every committed definition, merged and resolved once."""
    return {path.stem: load(path, keys) for path in definitions()}


def test_there_are_experiments_to_check():
    """The glob finding nothing would make every test below vacuously true."""
    assert len(definitions()) >= 8


@pytest.mark.parametrize("path", definitions(), ids=lambda p: p.stem)
def test_a_committed_experiment_validates_and_builds_a_graph(path, keys, schema):
    """The whole of what `ackbar create` will refuse on, minus the filesystem.

    `offline=True` skips the three steps that read paths, executables and
    observation files, because those depend on what has been staged on this
    machine and this is a tier 1 test. What is left is the schema, every job's
    config over every cycle and member, and the graph, which is where a layer
    edit lands.
    """
    config = load(path, keys)
    findings, _, _ = validate_experiment(
        config, schema, SITE, str(REPO), offline=True
    )
    assert not findings, "\n".join(
        f"  [{f.step}] {f.where}: {f.message}" for f in findings
    )
    assert build_graph(config).nodes


def test_every_ensemble_fails_a_cycle_rather_than_shrinking(configs):
    """`replace_from_mean` is undetectable after the fact, so this is the check.

    A member rebuilt from the mean of the survivors adds a column with no
    independent information, so the rank falls while the count still reads
    twenty, and under RTPS the deflation feeds back through a relaxation target
    that is itself shrinking. `ana/<date>/members.json` records what happened
    and nothing reads it. This invariant was established by editing twenty three
    files by hand.
    """
    for name, config in configs.items():
        ensemble = config.get("ensemble") or {}
        if not ensemble.get("size"):
            continue
        assert ensemble.get("on_missing_member") == "fail_cycle", (
            f"{name} would continue with a degraded ensemble"
        )


def test_the_arms_share_their_valid_times(configs):
    """A paired difference at each lead needs the same valid times everywhere.

    `osse-compare.py` scores only what every experiment has, so an arm that
    cycles differently does not fail, it drops itself out of the comparison at
    the leads it does not share.
    """
    spans = {
        name: (configs[name]["cycle"]["start"], configs[name]["cycle"]["count"])
        for name in ARMS
    }
    assert len(set(spans.values())) == 1, spans


def test_every_arm_reads_a_real_atmosphere(configs):
    """The launch blocker this file exists for, in its other half.

    An experiment inheriting no forcing source runs on the model's built-in
    climatology. That is not a small difference from an arm that reads GEFS: it
    is a common bias against a nature run driven by real weather, owing nothing
    to any solver, and it is invisible in the output.
    """
    for name in ARMS:
        inputs = (configs[name].get("ensemble") or {}).get("inputs") or {}
        assert "atm.nc" in inputs, f"{name} inherits no forcing source"


def test_the_truth_and_the_arms_read_different_weather(configs):
    """The fraternal twin, asserted on the thing that actually makes it one.

    The truth reads the weather that happened and the arms read a forecast of
    it, so the analysis has to cope with forcing error rather than being handed
    the nature run's own atmosphere. That difference is carried by the *source*,
    ERA5 against GEFS, and not by `forcing_purpose`, which keys the archives
    apart so they can be built over different boxes and spans without
    colliding.

    Asserting the two read different *paths* would be weaker than it looks and
    was the first version of this test: source and purpose are two segments of
    one path, so a truth mistakenly keyed to the experiment archive still reads
    a different file and passes. The source is the claim worth making.
    """
    def source(name):
        atmosphere = ((configs[name].get("ensemble") or {}).get("inputs")
                      or {}).get("atm.nc")
        # `.../forcing/<purpose>/<source>/mem###.nc`
        return Path(atmosphere).parent.name if atmosphere else None

    truth = source("osse-truth")
    assert truth, "the nature run inherits no forcing source"
    for name in ARMS:
        # An arm with no atmosphere at all is the other test's finding, and
        # raising here would send a reader to the wrong file.
        arm = source(name)
        if arm is None:
            continue
        assert arm != truth, (
            f"{name} reads {arm}, the same source as the nature run, so the "
            f"analysis is handed the weather it is scored against"
        )


def test_a_solver_analyses_no_more_than_it_reads(configs):
    """Per experiment, not per layer, so an override cannot escape it.

    A solver that analyses a variable its background never read writes a field
    with nothing behind it, and the run reports healthy.
    """
    for name, config in configs.items():
        solver = config.get("solver") or {}
        analysis = solver.get("analysis variables")
        background = solver.get("background variables")
        if not analysis or not background:
            continue
        assert set(analysis) <= set(background), (
            f"{name} analyses {sorted(set(analysis) - set(background))}, "
            f"which its background does not carry"
        )


def test_every_definition_is_reachable_from_the_readme():
    """A file nobody lists is one nobody maintains.

    `experiments/README.md` is the index a reader arrives at, and the set of
    definitions has been cut and extended twice; both times a name survived in
    prose after its file went, or arrived without being listed.
    """
    readme = (EXPERIMENTS / "README.md").read_text()
    for path in definitions():
        assert path.name in readme or path.stem in readme, (
            f"{path.stem} is committed but not mentioned in experiments/README.md"
        )


#: Experiments that have been deleted, kept by name because a reference to one
#: is how a reader is sent to a file that is not there. Each is either a whole
#: name or long enough not to be a prefix of a committed one, which is checked
#: below rather than assumed.
RETIRED = (
    "osse25-letkf",
    "osse25-4dletkf-atm",
    "osse25-4dletkf-obc",
    "osse25-4dletkf-all",
    "osse25-4dletkf-stoch",
    "osse25-letkf-smoke",
    "osse-free",
    "osse25-3dvar-bal",
    "osse-settle",
)


def test_the_readme_names_no_experiment_that_is_gone(configs):
    """The other direction, which is how the dangling references accumulated."""
    for token in RETIRED:
        assert not any(name.startswith(token) for name in configs), (
            f"{token} is listed as retired but a committed experiment starts "
            f"with it, so the check below would be a false positive"
        )
    readme = (EXPERIMENTS / "README.md").read_text()
    for token in RETIRED:
        assert token not in readme, (
            f"experiments/README.md names {token}, which is deleted"
        )


def test_a_definition_parses_as_yaml_before_anything_else():
    """A syntax error should say so rather than surface as a merge failure."""
    for path in definitions():
        yaml.safe_load(path.read_text())
