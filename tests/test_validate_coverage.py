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
from ackbar.validate import _coverage_step, _needed_span, _stamp

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
