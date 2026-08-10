"""Tier 0: which supergrid row is an `OBC_SEGMENT_00N` string talking about?

`segment_geometry` decides where a boundary file's data is written, and it is
reached only by the offline fetchers, so nothing in the workflow's own test tiers
touches it. It was verified against the two staged Gulf domains by hand, which
covers what those two domains say and nothing else; these are the cases that
have no domain on disk to check them against.

`mom6_parameters` is the only part that reads a domain, so it is replaced and
the supergrid is a small array. That keeps this tier 0: no data, no model, no
`ACKBAR_STATIC_ROOT`.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import obc_grid

#: 8 cells by 5 cells, so the supergrid is 17 by 11 and `ni`/`nj` are distinct.
#: Distinct on purpose: an off-by-one that swapped them would still pass on a
#: square domain, and the Gulf domains are not square either.
NI, NJ = 8, 5


@pytest.fixture
def grid():
    sx, sy = np.meshgrid(np.arange(2 * NI + 1, dtype="f8"),
                         np.arange(2 * NJ + 1, dtype="f8"))
    return sx, sy


def geometry(monkeypatch, grid, *specs):
    parameters = {"OBC_NUMBER_OF_SEGMENTS": str(len(specs))}
    for index, spec in enumerate(specs, start=1):
        parameters[f"OBC_SEGMENT_{index:03d}"] = spec
    monkeypatch.setattr(obc_grid, "mom6_parameters",
                        lambda domain, who: parameters)
    return obc_grid.segment_geometry("fake", *grid)


def refused(monkeypatch, grid, *specs):
    with pytest.raises(SystemExit) as caught:
        geometry(monkeypatch, grid, *specs)
    return str(caught.value)


# --- the shapes the Gulf actually declares -----------------------------------

def test_the_gulfs_own_three_segments(monkeypatch, grid):
    """The arrangement both staged domains use, including the reversed one."""
    got = geometry(monkeypatch, grid,
                   "J=N,I=N:0", "I=N,J=0:N", "J=0,I=0:N")
    assert got["001"]["shape"] == (1, 2 * NI + 1)
    assert got["002"]["shape"] == (2 * NJ + 1, 1)
    assert got["003"]["shape"] == (1, 2 * NI + 1)
    # A J segment is a row: constant latitude, longitude running the width.
    assert np.all(got["003"]["lat"] == 0)
    assert got["001"]["lat"][0] == 2 * NJ


def test_a_numeric_edge_is_the_same_edge_as_N(monkeypatch, grid):
    """MOM6 accepts `I=8` where 8 is the width, and refusing it refused a
    domain for how it spelled itself."""
    spelled = geometry(monkeypatch, grid, f"I={NI},J=0:{NJ}")
    named = geometry(monkeypatch, grid, "I=N,J=0:N")
    assert np.array_equal(spelled["001"]["lon"], named["001"]["lon"])


# --- what it has to refuse ---------------------------------------------------

def test_an_interior_segment_is_refused(monkeypatch, grid):
    assert "neither edge" in refused(monkeypatch, grid, "I=3,J=0:N")


def test_a_range_along_the_segments_own_axis_is_refused(monkeypatch, grid):
    """`I=N,I=0:N` is not a description of an edge.

    A segment on a line of constant I is a column and is indexed by J. Reading
    the range's own letter to size it accepted this and built a full column,
    which is a wrong answer rather than a refusal.
    """
    message = refused(monkeypatch, grid, "I=N,I=0:N")
    assert "declares its range along" in message


def test_a_single_point_range_is_refused(monkeypatch, grid):
    """`J=0:0` is one point. Collecting the ends into a set made it a subset of
    the full-edge names and so indistinguishable from a whole edge."""
    assert "part of an edge" in refused(monkeypatch, grid, "I=N,J=0:0")


def test_a_genuinely_partial_edge_is_refused(monkeypatch, grid):
    assert "part of an edge" in refused(monkeypatch, grid, "I=N,J=1:3")


def test_a_closed_domain_says_so(monkeypatch, grid):
    monkeypatch.setattr(obc_grid, "mom6_parameters",
                        lambda domain, who: {"OBC_NUMBER_OF_SEGMENTS": "0"})
    with pytest.raises(SystemExit) as caught:
        obc_grid.segment_geometry("fake", *grid)
    assert "no open boundary" in str(caught.value)
