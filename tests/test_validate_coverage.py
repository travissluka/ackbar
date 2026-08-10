"""Tier 1: does step 3 catch a per-member input that stops before the run does?

This is the one input failure `ackbar heal` cannot recover. A member boundary
that runs out mid-experiment kills the cycle that walks off the end, and the
obvious repair, rebuilding the archive longer, changes the anomaly mean for every
member, so every earlier cycle turns out to have integrated a boundary that no
longer exists. The experiment is discarded rather than healed, which is what
makes catching it before submission worth a filesystem read per file.

The check reads the *time axis*, not the `time_coverage_*` attributes that
`tools/obc-lagged.py` writes. Those are for a human with `ncdump`. A check built
on them would pass silently on every archive built before the tool started
writing them, would only ever work for archives from that one tool, and would be
checking an annotation rather than the thing MOM6 opens. These tests write files
with no attributes at all, on purpose.
"""

from pathlib import Path

import netCDF4
import numpy as np
import pytest

from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import build_graph
from ackbar.validate import (_coverage_step, _needed_span, _shared_timed,
                             _stamp, validate_experiment)

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"

SITE = {
    "scratch_root": "/scratch",
    "output_root": "/out",
    "static_root": "/static",
    "max_submit_jobs": "10000",
    "max_array_size": "1000",
    "root": str(REPO),
}

#: A cycling fixture with more than one cycle, so a span is a range.
FIXTURE = "fourd_om1deg"


@pytest.fixture(scope="module")
def experiment():
    keys = merge_keys(load_schema())
    layers = resolve_layers(EXPERIMENTS / f"{FIXTURE}.yaml", LAYERS)
    config = resolve(merge_layers(layers, keys), SITE)
    return config, build_graph(config)


def boundary(path, first, last, calendar="NOLEAP"):
    """A file whose only relevant content is a daily time axis, and no globals."""
    with netCDF4.Dataset(path, "w", format="NETCDF4_CLASSIC") as f:
        f.createDimension("time", None)
        t = f.createVariable("time", "f8", ("time",))
        t.units = f"days since {first:%Y-%m-%d} 00:00:00"
        t.calendar = calendar
        t[:] = np.arange((last - first).days + 1, dtype="f8")
    return str(path)


def test_the_span_runs_from_the_windows_open_to_the_forecasts_end(experiment):
    """Not cycle.start to cycle.start + count * length.

    A 4D window opens before its own cycle, so the boundary is read earlier than
    the first cycle stamp, and a forecast runs past the last one. Both edges are
    taken from `symbols`, which is what a job reads.
    """
    config, graph = experiment
    first, last = _needed_span(config, graph)
    start = str(config["cycle"]["start"])[:10]
    assert _stamp(first) < start + "T99", "the window must open no later than cycle 1"
    assert _stamp(first)[:10] <= start
    assert _stamp(last) > _stamp(first)


def test_a_boundary_that_covers_the_run_is_silent(experiment, tmp_path):
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    late = dt.datetime(last[0], last[1], last[2]) + dt.timedelta(days=30)
    path = boundary(tmp_path / "mem000.nc", early, late)
    assert _coverage_step({path}, config, graph) == []


def test_a_boundary_that_stops_early_is_reported(experiment, tmp_path):
    """The failure this exists for, and the message has to name both spans."""
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    short = dt.datetime(last[0], last[1], last[2]) - dt.timedelta(days=1)
    path = boundary(tmp_path / "mem000.nc", early, short)
    findings = _coverage_step({path}, config, graph)
    assert len(findings) == 1, findings
    assert findings[0].step == 3
    assert "time_interp_external" in findings[0].message
    assert "does not heal" in findings[0].message


def test_every_member_short_the_same_way_is_one_finding(experiment, tmp_path):
    """An ensemble is built in one pass, so all of it is short together.

    Twenty identical paragraphs is the noise `_dedupe` exists to prevent for the
    cycle loop, and it trains the reader to skim step 3.
    """
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    short = dt.datetime(last[0], last[1], last[2]) - dt.timedelta(days=1)
    paths = {boundary(tmp_path / f"mem{i:03d}.nc", early, short) for i in range(20)}
    findings = _coverage_step(paths, config, graph)
    assert len(findings) == 1, findings
    assert "20 per-member input(s)" in findings[0].message
    assert findings[0].where == str(tmp_path)


def test_a_member_with_a_different_span_is_its_own_finding(experiment, tmp_path):
    """Grouping is by span, not by message, so one odd member still shows."""
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    paths = {boundary(tmp_path / f"mem{i:03d}.nc", early,
                      dt.datetime(last[0], last[1], last[2])
                      - dt.timedelta(days=1 + 10 * i))
             for i in range(2)}
    assert len(_coverage_step(paths, config, graph)) == 2


def test_a_file_with_no_time_axis_is_not_a_finding(experiment, tmp_path):
    """`ensemble.inputs` is general; a per-member static field is legitimate."""
    config, graph = experiment
    path = tmp_path / "static.nc"
    with netCDF4.Dataset(path, "w", format="NETCDF4_CLASSIC") as f:
        f.createDimension("x", 3)
        f.createVariable("depth", "f8", ("x",))[:] = [1.0, 2.0, 3.0]
    assert _coverage_step({str(path)}, config, graph) == []


def test_a_path_that_is_not_there_is_left_to_step_three(experiment, tmp_path):
    """Absence is already reported, and better, by the existing path check."""
    config, graph = experiment
    assert _coverage_step({str(tmp_path / "gone.nc")}, config, graph) == []


def test_a_file_that_is_not_netcdf_is_reported_rather_than_raised(experiment,
                                                                 tmp_path):
    """`stage()` in `test_validate.py` touches empty files, and a traceback out
    of the middle of validate is the partial report this command exists to
    prevent."""
    config, graph = experiment
    path = tmp_path / "empty.nc"
    path.touch()
    findings = _coverage_step({str(path)}, config, graph)
    assert len(findings) == 1
    assert "cannot be opened" in findings[0].message


# --- an archive whose fields each carry their own axis ------------------------
#
# The atmospheric archive gives every field its own unlimited axis and writes no
# variable called `time`, so a check keyed on that name collects every `atm.nc`,
# finds nothing, skips it, and reports the experiment clean. Which is the one
# outcome this step exists to prevent.


def atmosphere(path, first, last, short=()):
    """Seven fields, seven axes, and no variable named `time`.

    *short* names fields whose axis stops a day early, which is what an
    interval-mean field stamped at its window midpoint does at the end of a
    series.
    """
    fields = ("T2", "Q2", "U10", "V10", "DSWRF", "DLWRF", "PRATE")
    with netCDF4.Dataset(path, "w", format="NETCDF4") as f:
        for name in fields:
            axis = f"time_{name}"
            f.createDimension(axis, None)
            t = f.createVariable(axis, "f8", (axis,))
            t.units = f"days since {first:%Y-%m-%d} 00:00:00"
            t.calendar = "NOLEAP"
            t.axis = "T"
            days = (last - first).days + 1 - (1 if name in short else 0)
            t[:] = np.arange(days, dtype="f8")
    return str(path)


def test_an_atmosphere_that_covers_the_run_is_silent(experiment, tmp_path):
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    late = dt.datetime(last[0], last[1], last[2]) + dt.timedelta(days=30)
    path = atmosphere(tmp_path / "mem000.nc", early, late)
    assert _coverage_step({path}, config, graph) == []


def test_an_atmosphere_that_stops_early_is_caught_and_not_skipped(experiment,
                                                                 tmp_path):
    """The regression: per-field axes are a time axis, and a file carrying only
    those must not read as a static field."""
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    stops = dt.datetime(last[0], last[1], last[2]) - dt.timedelta(days=5)
    path = atmosphere(tmp_path / "mem000.nc", early, stops)
    findings = _coverage_step({path}, config, graph)
    assert len(findings) == 1
    assert "time_interp_external" in findings[0].message


def test_the_narrowest_axis_is_the_one_that_counts(experiment, tmp_path):
    """A file's axes need not agree, and the run stops when the first runs out,
    so taking whichever axis came first would pass a file that fails."""
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    edge = dt.datetime(last[0], last[1], last[2])
    # Every field reaches the end except the shortwave, which stops a day short.
    path = atmosphere(tmp_path / "mem000.nc", early, edge, short=("DSWRF",))
    assert len(_coverage_step({path}, config, graph)) == 1


def test_a_file_with_neither_an_axis_nor_a_time_variable_is_still_skipped(
        experiment, tmp_path):
    """A per-member static field remains a legitimate use of the mechanism."""
    config, graph = experiment
    path = tmp_path / "static.nc"
    with netCDF4.Dataset(path, "w", format="NETCDF4") as f:
        f.createDimension("x", 3)
        f.createVariable("depth", "f8", ("x",))[:] = [1.0, 2.0, 3.0]
    assert _coverage_step({str(path)}, config, graph) == []


# --- the shared boundary, which is not a per-member input at all --------------
#
# The check was built for `ensemble.inputs`, where a short archive is the failure
# healing cannot recover. The same failure arrives through the domain's own
# `INPUT/obc.nc`, and arrives more often: every regional experiment has a
# boundary and only some have an ensemble of them.
# `tests/experiments/tier3_gom.yaml` is the standing example, a fixture starting
# 2015-01-05 against a GLORYS boundary that begins 2015-05-28, which stops MOM6
# at its first timestep and blocks four tier 3 modules. Validate reported that
# experiment clean on all six steps.


def test_the_domains_own_boundary_is_collected(tmp_path):
    """Not just the per-member ones: `model.input` supplies a file too."""
    (tmp_path / "obc.nc").touch()
    rendered = {"model": {"input": str(tmp_path)}}
    assert _shared_timed(rendered) == [str(tmp_path / "obc.nc")]


def test_a_closed_domain_has_no_boundary_to_collect(tmp_path):
    """A global domain has no `obc.nc`, and a missing one is step 3's finding to
    report rather than this one's."""
    assert _shared_timed({"model": {"input": str(tmp_path)}}) == []


def test_a_model_with_no_input_directory_is_not_a_boundary_question():
    """`model: stub` and `model: persistence` have no `INPUT/` at all."""
    assert _shared_timed({"model": {"name": "stub"}}) == []
    assert _shared_timed({}) == []


def test_a_short_shared_boundary_is_reported(experiment, tmp_path):
    """The whole point: the same failure, from the file every member reads."""
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    short = dt.datetime(last[0], last[1], last[2]) - dt.timedelta(days=1)
    path = boundary(tmp_path / "obc.nc", early, short)
    findings = _coverage_step({path}, config, graph, {path})
    assert len(findings) == 1
    assert "time_interp_external" in findings[0].message


def test_the_two_cases_are_told_apart_because_the_repair_differs(experiment,
                                                                tmp_path):
    """A per-member archive cannot be extended in place, and telling someone
    their afternoon's refetch has cost them the experiment is worse than saying
    nothing. The domain's own boundary is simply refetched longer."""
    config, graph = experiment
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    short = dt.datetime(last[0], last[1], last[2]) - dt.timedelta(days=1)
    path = boundary(tmp_path / "obc.nc", early, short)

    of_the_domain = _coverage_step({path}, config, graph, {path})[0].message
    assert "shared input" in of_the_domain
    assert "Refetch" in of_the_domain
    assert "anomaly mean" not in of_the_domain

    of_the_ensemble = _coverage_step({path}, config, graph)[0].message
    assert "per-member input" in of_the_ensemble
    assert "anomaly mean" in of_the_ensemble


def test_a_whole_validate_run_reaches_the_domains_boundary(tmp_path):
    """The wiring, not the two halves of it.

    Every other test here calls `_coverage_step` with a hand-built set, so the
    two pieces that make this reach a real experiment are both unexercised:
    `_jobtime_step` collecting from `model.input`, and `validate_experiment`
    handing that set to the check. Drop either and every other test in this file
    still passes while `tier3_gom` validates clean again, which is the state
    this whole section exists to leave behind.

    So: a real config, a short boundary where the model reads one, and the whole
    six-step run. Asserted by message rather than by count, because a fake site
    means plenty of other paths are legitimately missing.
    """
    keys = merge_keys(load_schema())
    layers = resolve_layers(EXPERIMENTS / f"{FIXTURE}.yaml", LAYERS)
    config = resolve(merge_layers(layers, keys), SITE)
    graph = build_graph(config)
    first, last = _needed_span(config, graph)
    import datetime as dt
    early = dt.datetime(first[0], first[1], first[2]) - dt.timedelta(days=30)
    short = dt.datetime(last[0], last[1], last[2]) - dt.timedelta(days=1)
    boundary(tmp_path / "obc.nc", early, short)
    config["model"]["input"] = str(tmp_path)

    findings, _graph, _ran = validate_experiment(
        config, load_schema(), SITE, SITE["root"])
    coverage = [f for f in findings if "time_interp_external" in f.message]
    assert len(coverage) == 1, [f.message for f in findings]
    assert "shared input" in coverage[0].message
    assert coverage[0].where == str(tmp_path / "obc.nc")
