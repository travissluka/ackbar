"""Tier 3: 3DVar cycling on the real model, at gom_25km. The phase 6 milestone.

Two experiments, and the pair is the point. `tier3_var_persist` runs the whole
DA loop with `model: persistence`, so a failure there is in the analysis, the
writeback or the handoff. `tier3_var` is the same experiment with MOM6 back in
the loop, so a failure there is in the one thing the first cannot test: whether
the model reads what writeback wrote.

The claims, in the order they would cost the most to discover later:

- **The analysis moves the state towards the observations.** `oman` is smaller
  than `ombg` in RMS. That is one number and it is the only assertion here that
  covers the background error, the observation operators, the minimizer and the
  departure bookkeeping at once. Everything else can be right while this is
  wrong.
- **What the analysis solved for reaches the restart, and nothing else does.**
  Ocean cells carry the increment, land carries the background, and the fields
  the analysis never touched are byte for byte the background's.
- **The forecast starts from the analysis.** Not from the background it
  corrected, which is the failure that leaves an experiment looking healthy
  while it cycles a free run.
- **A window with no observations is survivable.** The archive has a gap in it,
  and the cycle over that gap produces a restart set equal to its background and
  says so.

Regional rather than global, because that is what tier 3 is *for*: gom_25km
costs 6 seconds a simulated day against 178 for om_1deg, and the analysis path
has no global-only behaviour left in it, while the domain does have
regional-only behaviour in `MOM_override.soca` and the open boundaries.

The observations are synthetic and this test builds them, because the bundle's
own test files are scattered over the world ocean and essentially none of them
land in the Gulf. They observe a known anomaly, which is what makes the last
assertion here possible: not just that the analysis reduced its departures, but
that what it put into the ocean is the thing that was there to find. See
`tools/obs-archive-osse.py`.

Opt in with `ACKBAR_TIER3=1`; needs `source site/activate.sh`, a built
`coupler_main` and SOCA, the offline initial condition, the domain's static
stage (`tools/soca-gridspec.sh gom_25km`) and its background error
(`tools/soca-diffusion.sh gom_25km`).

    ACKBAR_TIER3=1 .venv/bin/python -m pytest tests/test_tier3_var.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import experiment_paths
import yaml

from ackbar import slurm
from ackbar.cli import main
from ackbar.site import load_site

from test_tier2 import _purge, wait_for_quiet
from test_tier3 import initial_energy

pytestmark = pytest.mark.tier3

REPO = Path(__file__).resolve().parents[1]
CYCLES = (1, 2, 3)
OBSERVERS = ("adt_3a", "sst_noaa19")
DOMAIN = "gom_25km"
STATIC = Path(os.environ.get("ACKBAR_STATIC_ROOT", "/nonexistent"))

#: The last cycle, and the only one whose restarts and background both still
#: exist when the run has drained. `cleanup` in cycle *n* reaps *n-2*, so by the
#: end of three cycles `rst/0`, `rst/1` and `ana/1` are gone. Everything about a
#: restart is therefore asserted here; the departures and the emitted documents
#: are never reaped and are asserted over every cycle.
LAST = 3
KEPT = (2, 3)

#: The cycle whose ADT file is deliberately absent. Cycle 2, so that the cycles
#: either side of it prove the gap did not leak in either direction.
GAP_CYCLE = 2
GAP_OBSERVER = "adt_3a"

#: Generous. What this guards against is sitting through a hang rather than a
#: slow machine: persistence is seconds a cycle and MOM6 is minutes.
QUIET = {"tier3_var_persist": 1800, "tier3_var": 3600}


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    if os.environ.get("ACKBAR_TIER3") != "1":
        pytest.skip("tier 3 runs the real applications; set ACKBAR_TIER3=1")
    if not slurm.available():
        pytest.skip("no sbatch on this machine")
    if not os.environ.get("ACKBAR_OUTPUT_ROOT"):
        pytest.skip("run `source site/activate.sh` first")
    if not (STATIC / "static" / DOMAIN / "diffusion" / "corr_hz.nc").exists():
        pytest.skip(f"no background error for {DOMAIN}; "
                    f"run tools/soca-diffusion.sh {DOMAIN}")


def initial_condition():
    """The offline state every experiment here starts from.

    Read out of the committed fixture rather than spelled again, and it is the
    one reference an assertion can still make once the run has drained:
    `cleanup` reaps `rst/0` two cycles in, and the static root is read-only to
    every experiment.
    """
    source = yaml.safe_load((REPO / "tests/experiments/tier3_var.yaml").read_text())
    return Path(source["model"]["initial_condition"]
                .replace("$(static_root)", str(STATIC)))


def build_archive(source, root):
    """Generate the archive an experiment document asks for, and only that.

    The platform list comes from the experiment's own `obs/*` layers rather
    than from the generator's default, and that is load bearing rather than
    tidy: the default is every platform the generator knows, which now includes
    the profile ones, and those cannot be drawn from a `--state` single state
    with a two dimensional anomaly on it. So a tier 3 test that took the
    default started failing the moment profiles were added, in a stage that has
    nothing to do with what it tests. Naming the platforms also makes the
    archive and the experiment agree by construction, which is what a synthetic
    archive is for.
    """
    platforms = [layer.split("/", 1)[1] for layer in source["inherit"]
                 if layer.startswith("obs/")]
    subprocess.run(
        [sys.executable, str(REPO / "tools" / "obs-archive-osse.py"),
         "--domain", DOMAIN,
         "--state", str(initial_condition()),
         "--start", source["cycle"]["start"],
         "--length", source["cycle"]["length"],
         "--count", str(source["cycle"]["count"]),
         "--platforms", *platforms,
         "--out", str(root)],
        check=True, capture_output=True,
    )
    return root


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """This test's own observation archive, with one file left out.

    Generated rather than shared, for two reasons. The gap is part of what is
    being tested, and a hole punched in an archive other experiments read would
    be a hole in their results too. And the archive is the *specification* of
    this test: it observes a known anomaly, and the assertion that the analysis
    recovered it only means anything if the two were built together.
    """
    source = yaml.safe_load((REPO / "tests/experiments/tier3_var.yaml").read_text())
    root = build_archive(source, tmp_path_factory.mktemp("obs"))
    sorted(root.rglob(f"*/{GAP_OBSERVER}.*.nc4"))[GAP_CYCLE - 1].unlink()
    return root


def truth(archive):
    """The anomaly the observations were drawn from, and where it is observable."""
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    with netCDF4.Dataset(archive / "truth.nc") as data:
        data.set_auto_mask(False)
        return {name: numpy.asarray(data.variables[name][:])
                for name in ("sst", "ssh", "open")}


def run_experiment(name, archive, tmp_path_factory):
    """Create, start and drain one committed experiment. Returns its paths."""
    source = yaml.safe_load((REPO / "tests/experiments" / f"{name}.yaml").read_text())
    source["vars"]["obs_dir"] = str(archive)
    path = tmp_path_factory.mktemp("cfg") / f"{name}.yaml"
    path.write_text(yaml.safe_dump(source))

    paths = experiment_paths(name, load_site())
    _purge(paths)
    assert main(["create", str(path)]) == 0
    assert main(["start", name]) == 0
    assert wait_for_quiet(name, QUIET[name]) == "drained"
    return experiment_paths(name, load_site())


@pytest.fixture(scope="module")
def persist(archive, tmp_path_factory):
    paths = run_experiment("tier3_var_persist", archive, tmp_path_factory)
    yield paths
    _purge(paths)


@pytest.fixture(scope="module")
def cycled(archive, tmp_path_factory):
    paths = run_experiment("tier3_var", archive, tmp_path_factory)
    yield paths
    _purge(paths)


# --- reading what a run left behind ------------------------------------------

def departures(paths, cycle, observer):
    """(ombg, oman) for one observer, over the locations that passed QC."""
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    path = next(paths.cycle_out("obs_out", cycle).glob(f"*{observer}*.nc4"))
    with netCDF4.Dataset(path) as data:
        variable = list(data["ombg"].variables)[0]
        before = numpy.asarray(data["ombg"][variable][:])
        after = numpy.asarray(data["oman"][variable][:])
    keep = (numpy.abs(before) < 1e30) & (numpy.abs(after) < 1e30)
    return before[keep], after[keep]


def observers(paths, cycle):
    return {record["name"]: record
            for record in json.loads(paths.observer_list(cycle).read_text())["observers"]}


def analysis_products(paths, cycle, member=0):
    return sorted(p.name for p in
                  (paths.member_out("ana", cycle, member) / "analysis").glob("*.nc"))


def emitted(paths, cycle):
    """The document the analysis actually read, kept out of scratch."""
    document = sorted((paths.log_dir(cycle)).glob("da.*.var.yaml"))
    assert len(document) == 1, f"cycle {cycle}: {document}"
    return yaml.safe_load(document[0].read_text())


# --- the analysis ------------------------------------------------------------

def test_the_analysis_moves_the_state_towards_the_observations(persist):
    """One number, and it covers the whole chain.

    The background error, both observation operators, the balance operator, the
    minimizer and the departure bookkeeping all have to be consistent for this
    to hold. Any of them can be individually plausible while it does not.
    """
    import numpy

    for cycle in CYCLES:
        for name in OBSERVERS:
            if cycle == GAP_CYCLE and name == GAP_OBSERVER:
                continue
            before, after = departures(persist, cycle, name)
            assert before.size > 10, f"cycle {cycle} {name}: almost nothing assimilated"
            rms = (numpy.sqrt(numpy.mean(before ** 2)),
                   numpy.sqrt(numpy.mean(after ** 2)))
            assert rms[1] < rms[0], f"cycle {cycle} {name}: ombg {rms[0]} oman {rms[1]}"


def test_both_departures_are_written(persist):
    """`ombg` and `oman`, and the second is the one that is easy to lose.

    `CostJo` saves it only on the final cost evaluation, which
    `oops::Variational` only runs when the configuration asks for output. An
    analysis configured without one writes `ombg`, no `oman`, and no complaint.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    path = next(persist.cycle_out("obs_out", 1).glob("*sst_noaa19*.nc4"))
    with netCDF4.Dataset(path) as data:
        assert "ombg" in data.groups and "oman" in data.groups


def test_the_analysis_and_the_increment_are_kept(persist):
    for cycle in KEPT:
        written = analysis_products(persist, cycle)
        assert len(written) == 2, f"cycle {cycle}: {written}"
        assert any(name.startswith("ocn.ana.an.") for name in written)
        assert any(name.startswith("ocn.incr.incr.") for name in written)


def test_the_analysis_products_do_not_pollute_the_restart_set(persist):
    """`ana/<n>/mem###` is a restart set and has to stay one.

    Writeback fills it by copying every file of the background's, persistence
    fills the next cycle's by copying every file of this one, and the forecast
    links all of them into `INPUT/`. A state file loose among them is inert to
    the model and then carried forward by every cycle after it, one more each
    time.
    """
    loose = sorted(p.name for p in persist.member_out("ana", LAST, 0).glob("ocn.*"))
    assert loose == []


def test_the_increment_is_not_zero(persist):
    """An analysis that changed nothing passes every structural assertion."""
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    path = next((persist.member_out("ana", LAST, 0) / "analysis").glob("ocn.incr.*.nc"))
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        values = numpy.asarray(data.variables["Temp"][0])
    assert numpy.abs(values).max() > 1e-4


def test_the_analysis_recovers_the_anomaly_the_observations_saw(persist, archive):
    """The OSSE assertion, and the only one that is about the *answer*.

    Every other test here says the machinery ran. This one says it found the
    right thing: the observations sample a truth that differs from the initial
    condition by an anomaly the generator wrote down, and what the assimilation
    put into the ocean is compared against that anomaly rather than against the
    departures it was fitted to.

    Measured against the initial condition, over the whole run, rather than
    against one cycle's increment. Two reasons, and the second is the
    interesting one. The initial condition is an offline product `cleanup` never
    reaps, while cycle 1's own background and increment are both gone by the end
    of three cycles. And with `model: persistence` the state moves only when an
    analysis moves it, so the difference between the last analysis and the
    initial condition is the whole of what the assimilation did. A single late
    increment is the wrong thing to ask for: by then the anomaly has largely
    been corrected, and what is left is the analysis fitting that cycle's
    observation noise.

    Two claims. The correction has the *shape* of the anomaly, and the analysed
    state is closer to the truth than the state it started from. Neither is a
    claim about magnitude: that is a statement about the background error's
    standard deviations, which are the bundle's defaults and are not claimed to
    be right.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    anomaly = truth(archive)
    observed = anomaly["open"] > 0

    def surface(path):
        with netCDF4.Dataset(path) as data:
            data.set_auto_mask(False)
            return {"sst": numpy.asarray(data.variables["Temp"][0, 0]),
                    "ssh": numpy.asarray(data.variables["ave_ssh"][0])}

    start = surface(initial_condition() / "MOM.res.nc")
    final = surface(persist.member_out("ana", LAST, 0) / "MOM.res.nc")

    # Different thresholds, because the two platforms observe their fields very
    # differently. Six hundred scattered SST points at 0.5 K against a 1 K
    # anomaly is a well observed field. Two hundred along-track ADT points at
    # 5 cm against an 8 cm anomaly is a signal-to-noise ratio near one by
    # construction, and no analysis recovers a shape it cannot see; the number
    # below is what that observing network supports, not a tuned pass mark.
    for name, floor in (("sst", 0.5), ("ssh", 0.3)):
        wanted = anomaly[name][observed]
        applied = (final[name] - start[name])[observed]
        correlation = numpy.corrcoef(wanted, applied)[0, 1]
        assert correlation > floor, f"{name}: correction correlates {correlation:.3f}"

        # The error against the truth, before and after. This is the number an
        # OSSE exists to produce, and an analysis can move the state in the
        # right direction and still overshoot past it.
        before = numpy.sqrt(numpy.mean(wanted ** 2))
        after = numpy.sqrt(numpy.mean((wanted - applied) ** 2))
        assert after < before, f"{name}: error {before:.4g} -> {after:.4g}"


def test_each_cycle_corrects_its_own_background(persist):
    """The previous cycle's restart, never the analysis this task writes into.

    With a solver configured, "where does the forecast start" and "what does the
    analysis correct" are different questions with different answers, and one
    function answering both would make the analysis correct the analysis.

    Compared against the path `Paths` computes rather than a suffix of it: the
    suffix form passed for every layout that happened to end in the same three
    components, which is most of them.
    """
    for cycle in CYCLES:
        basename = emitted(persist, cycle)["cost function"]["background"]["basename"]
        assert basename.rstrip("/") == \
            str(persist.member_out("rst", cycle - 1, 0))


# --- what reaches the restart -------------------------------------------------

def test_the_analysed_restart_carries_the_increment_in_the_ocean_only(persist):
    """The whole of writeback, checked against the file it read.

    Ocean cells differ from the background by the increment; land does not
    differ at all; and the variables the analysis never solved for are the
    background's exactly. The last is the one that is silent: an `h` quietly
    rewritten changes the vertical coordinate the temperature is defined on.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    with netCDF4.Dataset(STATIC / "static" / DOMAIN / "soca_gridspec.nc") as data:
        data.set_auto_mask(False)
        ocean = numpy.asarray(data.variables["mask2d"][0]) > 0.0

    def read(path, name):
        with netCDF4.Dataset(path) as data:
            data.set_auto_mask(False)
            return numpy.asarray(data.variables[name][0])

    background = persist.member_out("rst", LAST - 1, 0) / "MOM.res.nc"
    analysed = persist.member_out("ana", LAST, 0) / "MOM.res.nc"
    increment = next(
        (persist.member_out("ana", LAST, 0) / "analysis").glob("ocn.incr.*.nc"))

    applied = read(analysed, "Temp") - read(background, "Temp")
    assert numpy.abs(applied[:, ocean]).max() > 1e-4
    assert numpy.all(applied[:, ~ocean] == 0.0)

    # Where the column holds water, the same increment, to the precision two
    # different writers of the same doubles agree to.
    #
    # **Only where it holds water**, and the exclusion is the contract rather
    # than a tolerance. Under Z* every column carries all NK levels whether or
    # not the sea floor leaves room, and on this domain about a third of the
    # cells are collapsed. `ackbar.writeback.fill_down` deliberately carries the
    # deepest live increment down through those, because leaving them at the
    # background value while the water above them moves puts a density inversion
    # at the base of a third of all columns and the model convects it away.
    # So `analysed - background` is *supposed* to differ from `ocn.incr` below
    # the sea floor, and the two dimensional ocean mask alone is the wrong mask
    # to ask this over.
    alive = read(background, "h") >= 0.01
    wet = alive & ocean
    assert wet.sum() > 0
    assert numpy.allclose(applied[wet], read(increment, "Temp")[wet], atol=1e-9)

    # And where it does not, the column is flat from its deepest live level
    # down: the property `fill_down` exists to produce, checked off the written
    # file rather than by calling the function that wrote it.
    dead = ocean & ~alive
    deepest = numpy.maximum.accumulate(
        numpy.where(alive, numpy.arange(alive.shape[0]).reshape(-1, 1, 1), 0),
        axis=0)
    carried = numpy.take_along_axis(applied, deepest, axis=0)
    assert numpy.allclose(applied[dead], carried[dead], atol=1e-9)

    assert numpy.array_equal(read(analysed, "h"), read(background, "h"))


def test_the_analysed_restart_set_is_whole(persist):
    member = persist.member_out("ana", LAST, 0)
    assert (member / "coupler.res").exists()
    # Everything the background had, not only the file the analysis touched.
    # Losing `ice_model.res.nc` here is a forecast that quietly restarts from a
    # default sea ice state every cycle.
    background = persist.member_out("rst", LAST - 1, 0)
    assert ({p.name for p in background.iterdir() if p.is_file()}
            <= {p.name for p in member.iterdir() if p.is_file()})


def test_the_restart_the_forecast_reads_carries_the_analysis(cycled):
    """`ana/<n>` differs from the background it was made from.

    Necessary and not sufficient, which is worth being explicit about: this
    checks what writeback produced, not what the model then did with it. The
    model's half is `test_the_model_read_the_analysed_restart` below.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy

    def temperature(path):
        with netCDF4.Dataset(path) as data:
            data.set_auto_mask(False)
            return numpy.asarray(data.variables["Temp"][0])

    analysed = temperature(cycled.member_out("ana", LAST, 0) / "MOM.res.nc")
    background = temperature(cycled.member_out("rst", LAST - 1, 0) / "MOM.res.nc")
    assert not numpy.array_equal(analysed, background)


# --- the model reads what writeback wrote ------------------------------------

def test_mom6_integrates_the_analysed_restart(cycled):
    """The one claim persistence cannot make.

    A restart edited in place is refused by MOM6 for reasons that have nothing
    to do with the science in it: a checksum that no longer describes the data
    is the first, and it aborts inside the restart read. Three completed cycles
    is the assertion.
    """
    # `KEPT` rather than every cycle: `cleanup` has already reaped the earlier
    # restarts, which is itself evidence that the cycles after them completed.
    for cycle in KEPT:
        assert (cycled.member_out("rst", cycle, 0) / "coupler.res").exists()
    assert [cycled.stats_file(cycle).exists() for cycle in CYCLES] == \
        [True] * len(CYCLES)


def test_the_model_read_the_analysed_restart(cycled):
    """The failure that makes every other assertion here meaningless.

    Writeback can be perfect and the model can still ignore it: with
    `input_filename = 'n'` MOM6 initializes from `INIT_LAYERS_FROM_Z_FILE` and
    never opens `INPUT/MOM.res.nc`, so every cycle integrates the same cold
    start and the experiment cycles a free run while reporting an analysis.
    Nothing upstream notices, because the model runs and writes a restart set.

    A cold start has `VELOCITY_CONFIG = zero`, so the energy MOM6 reports for
    its own step zero is exactly zero. A resumed run's is not.
    """
    for cycle in CYCLES:
        assert initial_energy(cycled, cycle) > 1e-12


def test_the_checksums_that_still_apply_were_left_alone(cycled):
    """MOM6 read this file, so every checksum still in it matched.

    Dropping them for the whole file would be the easy way to make writeback
    work and would discard the integrity check on every variable the analysis
    never touched.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    with netCDF4.Dataset(cycled.member_out("ana", LAST, 0) / "MOM.res.nc") as data:
        assert "checksum" not in data.variables["Temp"].ncattrs()
        assert "checksum" in data.variables["h"].ncattrs()


# --- the gap -----------------------------------------------------------------

def test_the_cycle_over_the_gap_still_assimilated_what_it_had(persist):
    record = observers(persist, GAP_CYCLE)[GAP_OBSERVER]
    assert record["present"] is False
    before, after = departures(persist, GAP_CYCLE, "sst_noaa19")
    assert before.size > 10


def test_the_experiment_finished(persist):
    """Nothing above proves the run got past the gap: a workflow that stopped at
    cycle 2 would satisfy every assertion about cycles 1 and 2."""
    assert [persist.stats_file(cycle).exists() for cycle in CYCLES] == \
        [True] * len(CYCLES)


# --- the compressed record ---------------------------------------------------

def test_the_compressed_states_were_actually_written(cycled):
    """`post.state` is a leaf, so its failure does not strand the cycle.

    Which is right, and it is also why this test has to exist. Nothing in the
    graph waits on the state record, so a `post.state` that raises every cycle
    leaves a drained experiment, a full `run/` tree and an empty `bkg/`, with
    the rest of the suite green. The shape trap is the live one: a regional
    MOM6 writes `u` a column wider than the gridspec's staggered grid, and the
    reduction has to take the tracer-sized corner `writeback` writes into.
    """
    for cycle in CYCLES:
        product = cycled.product("bkg", cycle + 1, 0)
        assert product.exists(), product
        assert not product.with_name(product.name + ".partial").exists()


def test_the_record_carries_the_velocities_on_their_own_grids(cycled):
    """The fields that made the reduction fail, checked for rather than skipped.

    A record without `u` and `v` is one a Loop Current comparison cannot see the
    currents in, and the failure mode this guards is silent: a field whose grid
    does not match is easy to drop and nothing downstream would say so.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    with netCDF4.Dataset(cycled.product("bkg", CYCLES[0] + 1, 0)) as data:
        for name in ("temperature", "salinity", "sea_surface_height",
                     "eastward_velocity", "northward_velocity"):
            assert name in data.variables, name
        assert data["eastward_velocity"].shape[-2:] == data["mask_u"].shape
        assert data["temperature"].shape[-2:] == data["mask"].shape
