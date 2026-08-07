"""The mixed layer the vertical background error is built from.

`ackbar.diffusion.mixed_layer` replaced a read of MOM6's `MLD`, whose long name
is "Instantaneous active mixing layer depth" and which on an afternoon restart
is the diurnal warm layer rather than the mixed layer under it. What that cost
is in the module's own docstring; what is pinned here is that it cannot come
back, and that the criterion behaves the same way outside the basin it was
found in.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ackbar import diffusion  # noqa: E402


def column(levels, thickness, temperature, salinity):
    """One water column, shaped the way a MOM6 restart is: (z, y, x)."""
    def field(values):
        return np.asarray(values, dtype=float).reshape(levels, 1, 1)
    return (field(thickness), field(temperature), field(salinity))


def stratified(surface, deep, mld, levels=40, dz=2.0, salinity=35.0):
    """A mixed layer of *mld* metres over a linear thermocline.

    Built from a depth rather than from a level count, so a test states the
    answer it expects in the units the answer comes back in.
    """
    depth = (np.arange(levels) + 0.5) * dz
    temperature = np.where(depth <= mld, surface,
                           surface - (surface - deep) * (depth - mld) / 100.0)
    return column(levels, np.full(levels, dz), temperature,
                  np.full(levels, salinity))


# --- the equation of state ---------------------------------------------------

@pytest.mark.parametrize("temperature,salinity,expected", [
    # Potential density at the surface, against standard values. The point of
    # the range is the range: a linear equation of state fitted anywhere in it
    # is wrong somewhere else in it, which is why this is Wright's.
    (10.0, 35.0, 1026.95),
    (0.0, 34.7, 1027.87),
    (28.0, 36.0, 1023.30),
    (-1.8, 34.0, 1027.35),
])
def test_density_is_right_from_polar_to_tropical(temperature, salinity, expected):
    assert diffusion.density(temperature, salinity) == pytest.approx(
        expected, abs=0.2)


def test_thermal_expansion_varies_by_a_factor_a_linear_state_cannot_hold():
    """Why the equation of state is not two constants.

    The threshold is a density contrast, so what sets the depth it finds is
    d(rho)/dT, and that runs by about a factor of five between polar and
    tropical water. A linear state equation with a subtropical coefficient
    finds the polar mixed layer at several times its real depth, and the polar
    mixed layer is the deep one.
    """
    def expansion(t, s=34.0):
        return abs(diffusion.density(t + 0.5, s) - diffusion.density(t - 0.5, s))

    assert expansion(28.0) / expansion(0.0) > 4.0


# --- the criterion -----------------------------------------------------------

def test_the_mixed_layer_is_the_depth_the_profile_actually_mixes_to():
    h, t, s = stratified(surface=28.0, deep=10.0, mld=30.0)
    assert diffusion.mixed_layer(h, t, s)[0, 0] == pytest.approx(30.0, abs=3.0)


def test_a_diurnal_warm_layer_does_not_become_the_mixed_layer():
    """The failure this function exists to prevent.

    Half a degree over the top four metres, on a twenty metre mixed layer:
    exactly the July profile that made MOM6's diagnostic report 3 m. Measured
    from the surface the criterion would find the bottom of the skin; measured
    from `MLD_REFERENCE` it steps over it.
    """
    h, t, s = stratified(surface=28.4, deep=10.0, mld=20.0)
    t[:2] += 0.5

    found = diffusion.mixed_layer(h, t, s)[0, 0]
    assert found == pytest.approx(20.0, abs=4.0)
    assert found > 10.0


def test_a_column_that_is_mixed_all_the_way_down_gets_its_own_bottom():
    levels = 20
    h, t, s = column(levels, np.full(levels, 5.0), np.full(levels, 4.0),
                     np.full(levels, 34.0))
    found = diffusion.mixed_layer(h, t, s)[0, 0]
    assert found == pytest.approx((levels - 0.5) * 5.0)


def test_a_density_inversion_does_not_confuse_the_search():
    """Why this is a scan and not a `searchsorted`.

    Density is not monotonic in depth everywhere. A warm intrusion under the
    thermocline makes one level lighter than the one above it, and a binary
    search assumes sortedness: handed a non-monotonic column it returns an
    index from whichever side of the inversion it happened to bisect into,
    which is an arbitrary depth rather than a wrong one. Scanning from the
    reference level down always returns the first crossing, which is what the
    criterion means.
    """
    h, t, s = stratified(surface=25.0, deep=6.0, mld=20.0, levels=60)
    t[18:22] += 6.0                      # a warm, light intrusion below the base

    found = diffusion.mixed_layer(h, t, s)[0, 0]
    assert found == pytest.approx(20.0, abs=4.0)

    # And the column really is non-monotonic, so the test is testing something.
    sigma = diffusion.density(t, s)[:, 0, 0]
    assert (np.diff(sigma) < 0).any()


def test_a_shelf_column_shallower_than_the_reference_depth_still_answers():
    """Three metres of water has no 10 m to refer to.

    It gets its own deepest level, which is the only sensible answer and is
    what the reference-level search gives without a special case.
    """
    h, t, s = column(3, [1.0, 1.0, 1.0], [20.0, 20.0, 20.0], [35.0] * 3)
    assert diffusion.mixed_layer(h, t, s)[0, 0] == pytest.approx(2.5)


def test_vanished_layers_at_the_sea_floor_are_not_mixed_layer():
    """Z* keeps zero thickness layers under the bathymetry.

    They hold the bottom value forever, so a criterion that counted them would
    report every shallow column as mixed to the deepest level in the grid.
    """
    levels = 30
    thickness = np.full(levels, 2.0)
    thickness[10:] = 0.0                 # the sea floor is at 20 m
    depth = np.cumsum(thickness) - thickness / 2.0
    temperature = np.where(depth <= 6.0, 25.0, 15.0)
    h, t, s = column(levels, thickness, temperature, np.full(levels, 35.0))

    assert diffusion.mixed_layer(h, t, s)[0, 0] <= 20.0


# --- what it feeds -----------------------------------------------------------

def test_a_deeper_mixed_layer_reaches_further_down():
    """The property the whole stage exists for.

    Not a fixed number of levels, because that depends on the vertical grid;
    the monotonicity is the claim, and it is what was broken when every column
    sat on the floor regardless of its mixed layer.
    """
    spec = {"min": 1.5, "method": "implicit", "iterations": 2}
    scales = []
    for mld in (10.0, 30.0, 60.0):
        h, t, s = stratified(surface=25.0, deep=5.0, mld=mld, levels=60)
        scales.append(diffusion.vertical_scales(
            h, diffusion.mixed_layer(h, t, s), spec)[0, 0, 0])

    assert scales[0] < scales[1] < scales[2]
    assert scales[0] > spec["min"]


def test_the_restart_is_read_for_temperature_and_salinity_not_for_mld(tmp_path):
    """A restart carrying MLD must not be able to reintroduce the old path."""
    import netCDF4

    levels = 30
    path = tmp_path / "MOM.res.nc"
    with netCDF4.Dataset(path, "w") as data:
        data.createDimension("Time", 1)
        data.createDimension("z", levels)
        data.createDimension("y", 1)
        data.createDimension("x", 1)
        h, t, s = stratified(surface=26.0, deep=8.0, mld=24.0, levels=levels)
        for name, field in (("h", h), ("Temp", t), ("Salt", s)):
            data.createVariable(name, "f8", ("Time", "z", "y", "x"))[:] = field
        # The trap: a diagnostic that says three metres.
        data.createVariable("MLD", "f8", ("Time", "y", "x"))[:] = 3.0

    grid = {"mask": np.ones((1, 1), dtype=bool)}
    _, mld = diffusion.read_restart(path, grid, np.array([0.0]))
    assert mld[0, 0] > 15.0
