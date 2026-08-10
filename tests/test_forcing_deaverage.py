"""The de-averaging, which is the one place the GEFS fetcher can be quietly wrong.

Radiation and precipitation arrive as windows since a six hourly reset, so within
each reset period one window arrives whole and the rest are differences. Getting
it wrong crashes nothing: it makes precipitation too large by a factor that grows
through each reset period and puts the shortwave's diurnal peak in the wrong
place. Both survive a run and both look plausible in a plot.

These build the windows from known per-interval truth and check that the
arithmetic gets that truth back, so no network and no GRIB are involved.
"""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]


def load():
    """Import the tool by path: it is a script, not a module in the package."""
    spec = importlib.util.spec_from_file_location(
        "forcing_gefs", REPO / "tools" / "forcing-gefs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gefs = pytest.importorskip("eccodes") and load()
ERA = next(e for e in gefs.ERAS if e.name == "reforecast")


def windows(truth, lead, step, reset):
    """The since-reset windows a provider would publish for *truth*.

    *truth* is the mean over each successive interval, which is what the
    de-averaging has to recover. A mean field's window is the running mean since
    the reset; an accumulation's is the running sum.
    """
    found, spans = {}, {}
    for name in gefs.WINDOWED:
        run = []
        for i, value in enumerate(truth):
            end = lead + (i + 1) * step
            here = lead + (end - 1 - lead) // reset * reset
            if end - step == here:
                run = []
            run.append(value)
            total = float(np.sum(run))
            whole = total if name == "PRATE" else total / len(run)
            found[(name, end)] = np.full((2, 2), whole, dtype="f8")
            spans[(name, end)] = (here, end)
    ends = [lead + (i + 1) * step for i in range(len(truth))]
    return found, spans, ends


def test_a_reset_period_of_means_comes_back_as_what_went_in():
    truth = [100.0, 300.0, 500.0, 200.0]      # two reset periods of two windows
    found, spans, ends = windows(truth, 12, ERA.step, ERA.reset)
    records, _ = gefs.deaverage(ERA, found, spans, ends, 12)
    got = [float(plane.mean()) for _, plane in records["DSWRF"]]
    assert got == pytest.approx(truth)


def test_an_accumulation_comes_back_as_a_rate():
    # PRATE is published as an accumulation over the window and wanted as a rate.
    truth = [0.0, 3.6, 7.2, 0.0]
    found, spans, ends = windows(truth, 12, ERA.step, ERA.reset)
    records, _ = gefs.deaverage(ERA, found, spans, ends, 12)
    got = [float(plane.mean()) for _, plane in records["PRATE"]]
    assert got == pytest.approx([v / (ERA.step * 3600.0) for v in truth])


def test_the_second_window_of_a_period_is_not_returned_whole():
    """The failure this exists to catch: treating every window as a value.

    Skipping the difference leaves the second window of each reset period at its
    running mean, which for a shortwave that switches on is roughly half of the
    truth and for precipitation is a doubling of the total.
    """
    truth = [0.0, 400.0]
    found, spans, ends = windows(truth, 12, ERA.step, ERA.reset)
    records, _ = gefs.deaverage(ERA, found, spans, ends, 12)
    second = float(records["DSWRF"][1][1].mean())
    assert second == pytest.approx(400.0)
    assert second != pytest.approx(200.0), "that is the undifferenced window"


def test_a_window_that_covers_something_else_stops_the_run():
    truth = [100.0, 300.0]
    found, spans, ends = windows(truth, 12, ERA.step, ERA.reset)
    spans[("DSWRF", 18)] = (0, 18)            # a since-hour-zero accumulation
    with pytest.raises(SystemExit) as error:
        gefs.deaverage(ERA, found, spans, ends, 12)
    assert "reset period is not what this tool reads" in str(error.value)


def test_records_are_stamped_at_the_window_midpoint():
    """Interval means sit half an interval before the instantaneous fields, and
    that offset is the whole reason each field carries its own time axis."""
    truth = [100.0, 300.0]
    found, spans, ends = windows(truth, 12, ERA.step, ERA.reset)
    records, _ = gefs.deaverage(ERA, found, spans, ends, 12)
    assert [hour for hour, _ in records["DSWRF"]] == [13.5, 16.5]


def test_a_cadence_that_does_not_divide_its_reset_is_refused():
    """`start - reset` would go negative and the difference would add rather
    than subtract, with the span check still satisfied."""
    era = ERA._replace(step=4, reset=6)
    with pytest.raises(SystemExit) as error:
        gefs.deaverage(era, {}, {}, [], 12)
    assert "do not divide" in str(error.value)


def test_a_negative_excursion_too_large_to_be_packing_residue_is_refused():
    truth = [100.0, 300.0]
    found, spans, ends = windows(truth, 12, ERA.step, ERA.reset)
    # A second window far below the first drives the difference well negative.
    found[("DSWRF", 18)] = np.full((2, 2), 10.0)
    with pytest.raises(SystemExit) as error:
        gefs.deaverage(ERA, found, spans, ends, 12)
    assert "packing residue" in str(error.value)
