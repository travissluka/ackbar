"""The height shift, its regrid, and the attribute that says a file was shifted.

Three things are checked here and they fail in different ways.

**The shift itself** is a vectorized port of `shift_up` in `tools/flux-height.py`,
which is a transcription of `ncar_ocean_fluxes` in the model's own
`surface_flux.F90`. The port is held to the original elementwise rather than to
a table of expected numbers: a table would be this file asserting what the port
already does, while the original is independent, stdlib only, and readable
against the Fortran. If the two ever disagree, one of them is wrong and the
question is which, which is the position worth being in.

**The regrid** exists only for the operational GEFS eras, whose surface
temperature is half degree while their forcing is quarter degree. Its
interesting property is not that it interpolates but that it is exact on a
linear field and on coincident points, because those are what say it is bilinear
rather than approximately so.

**The attribute** is what stops an archive built before any of this from being
staged silently against a data table that assumes it. The check is presence and
not value, so an archive built deliberately unshifted still runs.
"""

import importlib.util

import netCDF4
import numpy as np
import pytest

from ackbar import forcing


REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


def _reference():
    """`tools/flux-height.py` as a module.

    Loaded by path because the file is a script with a hyphen in its name, and
    it prints its tables at import, which pytest captures. Keeping it a script
    is deliberate: it is the thing a person runs to regenerate the numbers in
    `docs/forcing-reference-height.md`.
    """
    spec = importlib.util.spec_from_file_location(
        "flux_height", REPO / "tools" / "flux-height.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REF = _reference()

#: The four cases `docs/forcing-reference-height.md` tabulates, as
#: (sst, air temperature, relative humidity, 10 m wind). Two unstable, which is
#: the Gulf's dominant regime, and two stable.
CASES = REF.cases


def _two_metre_state(sst, tair, rh, wind):
    """A self-consistent surface layer's 2 m state, which is what a product ships.

    Built by the reference rather than made up, so the inputs to the shift are a
    profile that really could have come out of a model's surface layer.
    """
    ts, _qs, t10, q10, t2, q2, _f = REF.reference(sst, tair, rh, wind)
    return dict(ts=ts, t10=t10, q10=q10, t2=t2, q2=q2, wind=wind)


# --- the shift against its reference ----------------------------------------


def test_the_vector_shift_matches_the_scalar_reference_on_the_documented_cases():
    for name, sst, tair, rh, wind in CASES:
        state = _two_metre_state(sst, tair, rh, wind)
        want_t, want_q = REF.shift_up(
            state["wind"], state["t2"], state["q2"], state["ts"])
        got_t, got_q = forcing.shift_to_10m(
            np.array([state["t2"]]), np.array([state["q2"]]),
            np.array([state["wind"]]), np.array([state["ts"]]))
        assert got_t[0] == pytest.approx(want_t, abs=1e-10), name
        assert got_q[0] == pytest.approx(want_q, abs=1e-15), name


def test_the_vector_shift_matches_the_scalar_reference_across_a_swept_grid():
    """Not just the four tabulated cases: the port has to hold everywhere.

    The sweep spans both stability branches and down to light wind, which is
    where a vectorized `where` is most likely to have picked the wrong branch.

    States the reference cannot build are dropped rather than compared. At a
    fifth of a metre per second in strongly stable air its own profile
    extrapolates to a 2 m temperature near 223 K and a *negative* specific
    humidity, which is an artifact of inverting a surface layer that thin rather
    than an atmosphere any product reports. Comparing there would only measure
    the humidity floor, which is tested on its own and deliberately differs from
    the unfloored reference.
    """
    wants, gots = [], []
    for sst in (12.0, 20.0, 28.0, 31.0):
        for offset in (-12.0, -4.0, 0.0, 3.0, 6.0):
            for rh in (0.4, 0.7, 0.95):
                for wind in (1.0, 2.0, 8.0, 18.0):
                    state = _two_metre_state(sst, sst + offset, rh, wind)
                    if not (state["q2"] > 0.0 and 230.0 < state["t2"] < 330.0):
                        continue
                    wants.append(REF.shift_up(state["wind"], state["t2"],
                                              state["q2"], state["ts"]))
                    gots.append((state["t2"], state["q2"], state["wind"],
                                 state["ts"]))
    t2, q2, wind, ts = (np.array(column) for column in zip(*gots))
    got_t, got_q = forcing.shift_to_10m(t2, q2, wind, ts)
    want_t = np.array([w[0] for w in wants])
    want_q = np.array([w[1] for w in wants])
    # Most of the grid survives the physicality filter; if a change to the
    # reference ever drops it to a handful this stops being a sweep.
    assert len(wants) > 200
    np.testing.assert_allclose(got_t, want_t, atol=1e-10, rtol=0)
    np.testing.assert_allclose(got_q, want_q, atol=1e-15, rtol=0)


def test_the_shift_is_elementwise_over_a_plane():
    """A plane is the shape the fetchers hand it, and it must not couple points."""
    states = [_two_metre_state(sst, sst + offset, 0.7, wind)
              for sst in (18.0, 26.0)
              for offset in (-6.0, 2.0)
              for wind in (3.0, 11.0)]
    t2 = np.array([s["t2"] for s in states]).reshape(2, 4)
    q2 = np.array([s["q2"] for s in states]).reshape(2, 4)
    wind = np.array([s["wind"] for s in states]).reshape(2, 4)
    ts = np.array([s["ts"] for s in states]).reshape(2, 4)

    plane_t, plane_q = forcing.shift_to_10m(t2, q2, wind, ts)
    assert plane_t.shape == (2, 4)
    for index, state in enumerate(states):
        one_t, one_q = forcing.shift_to_10m(
            np.array([state["t2"]]), np.array([state["q2"]]),
            np.array([state["wind"]]), np.array([state["ts"]]))
        assert plane_t.ravel()[index] == pytest.approx(one_t[0], abs=1e-12)
        assert plane_q.ravel()[index] == pytest.approx(one_q[0], abs=1e-18)


def test_the_shift_recovers_the_state_the_profile_came_from():
    """The strong property: shifting up undoes integrating down.

    Each case's 2 m state was produced by integrating a known 10 m state down,
    so the shift given the true surface temperature has to land back on it. This
    is what says the inversion converges rather than merely moving in the right
    direction.
    """
    for name, sst, tair, rh, wind in CASES:
        state = _two_metre_state(sst, tair, rh, wind)
        got_t, got_q = forcing.shift_to_10m(
            np.array([state["t2"]]), np.array([state["q2"]]),
            np.array([state["wind"]]), np.array([state["ts"]]))
        assert got_t[0] == pytest.approx(state["t10"], abs=1e-6), name
        assert got_q[0] == pytest.approx(state["q10"], abs=1e-9), name


def test_the_shift_moves_the_air_away_from_the_sea_when_it_is_unstable():
    """Sign, in the regime the Gulf spends most of its time in.

    Unstable means the sea is warmer, heat goes up, and temperature falls with
    height, so the 10 m air is cooler than the 2 m air. That is the whole reason
    declaring 2 m scalars at 10 m understates the contrast and the ocean loses
    too little heat.
    """
    state = _two_metre_state(28.0, 24.0, 0.70, 8.0)
    got_t, got_q = forcing.shift_to_10m(
        np.array([state["t2"]]), np.array([state["q2"]]),
        np.array([state["wind"]]), np.array([state["ts"]]))
    assert got_t[0] < state["t2"]
    assert got_q[0] < state["q2"]


def test_the_shift_moves_the_air_toward_the_sea_when_it_is_stable():
    state = _two_metre_state(15.0, 18.0, 0.85, 6.0)
    got_t, _got_q = forcing.shift_to_10m(
        np.array([state["t2"]]), np.array([state["q2"]]),
        np.array([state["wind"]]), np.array([state["ts"]]))
    assert got_t[0] > state["t2"]


def test_a_surface_temperature_in_error_by_a_kelvin_still_lands_close():
    """Why the shift can live in the archive instead of in the model.

    A patched `surface_flux.F90` would have the model's live sea surface
    temperature; the archive has the product's own, which is the right one for
    inverting the product's profile but is not the ocean the model will run.

    The bound is absolute, in kelvin, and deliberately not a fraction of the
    correction. In the two stable cases a whole kelvin of surface error moves
    the answer by about half of what the correction itself moved, so a ratio
    would read as alarming; what makes the shift worth doing anyway is the flux,
    where `docs/forcing-reference-height.md` measures 2 to 7 W m-2 of residual
    against the 38 to 105 W m-2 the shift removes. The unstable cases, which are
    the Gulf's dominant regime, are the well behaved ones.
    """
    for name, sst, tair, rh, wind in CASES:
        state = _two_metre_state(sst, tair, rh, wind)
        args = (np.array([state["t2"]]), np.array([state["q2"]]),
                np.array([state["wind"]]))
        exact_t, _ = forcing.shift_to_10m(*args, np.array([state["ts"]]))
        off_t, _ = forcing.shift_to_10m(*args, np.array([state["ts"] + 1.0]))
        assert abs(off_t[0] - exact_t[0]) < 0.35, name


def test_a_negative_humidity_cannot_reach_the_archive():
    """The floor, exercised where it actually bites rather than trusted.

    Bone dry air over a cold sea drives the inversion toward a negative
    humidity. A negative specific humidity in an archive is one the model would
    read as real, so it is floored here rather than downstream.
    """
    t2 = np.array([250.0])
    q2 = np.array([1e-9])
    got_t, got_q = forcing.shift_to_10m(t2, q2, np.array([20.0]),
                                        np.array([248.0]))
    assert np.isfinite(got_t).all()
    assert (got_q >= 0.0).all()


# --- the regrid -------------------------------------------------------------


def test_the_regrid_reproduces_a_linear_field_exactly():
    """The property that says bilinear, not merely smooth."""
    lon = np.arange(250.0, 300.0, 0.5)
    lat = np.arange(10.0, 40.0, 0.5)
    plane = 3.0 * lat[:, None] + 2.0 * lon[None, :]
    to_lon = np.arange(255.0, 290.0, 0.25)
    to_lat = np.arange(15.0, 35.0, 0.25)

    got = forcing.regrid_linear(lon, lat, plane, to_lon, to_lat)
    want = 3.0 * to_lat[:, None] + 2.0 * to_lon[None, :]
    assert got.shape == (to_lat.size, to_lon.size)
    np.testing.assert_allclose(got, want, atol=1e-9)


def test_the_regrid_is_exact_where_the_two_grids_coincide():
    """Every other quarter degree point is a half degree point, untouched."""
    lon = np.arange(250.0, 300.0, 0.5)
    lat = np.arange(10.0, 40.0, 0.5)
    rng = np.random.default_rng(0)
    plane = rng.normal(300.0, 5.0, (lat.size, lon.size))
    to_lon = np.arange(255.0, 290.0, 0.25)
    to_lat = np.arange(15.0, 35.0, 0.25)

    got = forcing.regrid_linear(lon, lat, plane, to_lon, to_lat)
    on_lon = np.isin(to_lon, lon)
    on_lat = np.isin(to_lat, lat)
    want = plane[np.isin(lat, to_lat)][:, np.isin(lon, to_lon)]
    np.testing.assert_allclose(got[np.ix_(on_lat, on_lon)], want, atol=1e-9)


def test_the_regrid_wraps_in_longitude():
    """A target between the last source column and the first is not clamped.

    Nothing in the repository has a box there, because a wrapped box is refused
    earlier. This is here so that the refusal stays the reason it never happens,
    rather than this quietly returning an edge value if the refusal is ever
    relaxed.
    """
    lon = np.array([0.0, 90.0, 180.0, 270.0])
    lat = np.array([0.0, 10.0])
    plane = np.array([[10.0, 20.0, 30.0, 40.0], [10.0, 20.0, 30.0, 40.0]])
    got = forcing.regrid_linear(lon, lat, plane, np.array([315.0]), lat)
    # Halfway between the last column and the first one round the circle, so
    # halfway between 40 and 10. Clamping would give 40.
    assert got[0, 0] == pytest.approx(25.0)


def test_the_regrid_takes_latitude_handed_down_from_the_pole():
    """GEFS scans north to south, and reading it as ascending flips the field."""
    lon = np.array([250.0, 251.0])
    down = np.array([40.0, 39.0, 38.0])
    plane = np.array([[40.0, 40.0], [39.0, 39.0], [38.0, 38.0]])
    got = forcing.regrid_linear(lon, down, plane, lon, np.array([38.5, 39.5]))
    np.testing.assert_allclose(got[:, 0], [38.5, 39.5], atol=1e-9)


def test_a_land_adjacent_column_pulls_land_temperature_into_the_sea():
    """A real limitation of the interpolated path, pinned rather than hidden.

    `TMP:surface` is land skin temperature over land. On the reforecast that is
    harmless: the field is already quarter degree, no interpolation happens, and
    each point gets its own surface, which is the correct one. On the
    operational eras the field is half degree and interpolating it lets a
    coastal sea point take a share of a land neighbour's skin temperature, which
    over the Gulf in summer runs several kelvin hotter than the sea.

    This test says what that costs: the contamination is bounded by the land-sea
    contrast, it decays to nothing within one half degree source cell, and open
    water away from the coast is untouched. That is why it is documented as a
    limitation of the 2020-on era rather than masked. The shift is a small term
    and it is still removing far more error than this adds.
    """
    lon = np.array([260.0, 260.5, 261.0, 261.5, 262.0])
    lat = np.array([25.0, 25.5])
    sea, land = 303.0, 313.0
    # The two western columns are land, the rest is sea.
    plane = np.array([[land, land, sea, sea, sea],
                      [land, land, sea, sea, sea]])

    to_lon = np.arange(260.0, 262.01, 0.25)
    got = forcing.regrid_linear(lon, lat, plane, to_lon, lat)[0]

    contamination = got - sea
    # Bounded by the contrast, and never beyond it.
    assert contamination.max() <= (land - sea) + 1e-9
    # The sea point adjacent to the coast takes half the contrast, which is the
    # worst case and is what a half degree cell straddling a coastline means.
    assert got[np.argmin(np.abs(to_lon - 260.75))] == pytest.approx(
        (land + sea) / 2.0)
    # One source cell further out to sea it is gone entirely.
    assert got[to_lon >= 261.0].max() == pytest.approx(sea)


# --- the attribute ----------------------------------------------------------


ORIGIN = __import__("datetime").datetime(2015, 7, 12)


def _series():
    hours = np.array([0.0, 3.0])
    cube = np.ones((2, 2, 2))
    return {name: (hours, cube.copy()) for name in forcing.FIELDS}


def test_the_file_records_the_height_its_scalars_are_at(tmp_path):
    path = tmp_path / "atm.nc"
    forcing.write_atm(path, np.array([260.0, 261.0]), np.array([10.0, 11.0]),
                      ORIGIN, _series(), "gefs", "gom_25km",
                      scalar_height=forcing.REFERENCE_HEIGHT)
    with netCDF4.Dataset(path) as f:
        assert f.getncattr(forcing.HEIGHT_ATTRIBUTE) == 10.0


def test_the_scalars_carry_their_height_and_the_winds_are_left_alone(tmp_path):
    """The winds are at 10 m however the file was built, so they say nothing."""
    path = tmp_path / "atm.nc"
    forcing.write_atm(path, np.array([260.0, 261.0]), np.array([10.0, 11.0]),
                      ORIGIN, _series(), "gefs", "gom_25km",
                      scalar_height=forcing.PRODUCT_HEIGHT)
    with netCDF4.Dataset(path) as f:
        assert f["T2"].height == 2.0
        assert f["Q2"].height == 2.0
        assert "height" not in f["U10"].ncattrs()


def test_an_archive_that_will_not_say_its_height_is_refused(tmp_path):
    """The case the check exists for: an archive built before the shift did."""
    path = tmp_path / "atm.nc"
    forcing.write_atm(path, np.array([260.0, 261.0]), np.array([10.0, 11.0]),
                      ORIGIN, _series(), "gefs", "gom_25km",
                      scalar_height=forcing.REFERENCE_HEIGHT)
    with netCDF4.Dataset(path, "a") as f:
        f.delncattr(forcing.HEIGHT_ATTRIBUTE)

    with pytest.raises(ValueError) as raised:
        forcing.assert_reference_height(path)
    assert "no-height-shift" in str(raised.value)


def test_an_archive_built_unshifted_on_purpose_is_allowed(tmp_path):
    """Presence is the check, not a particular value.

    Asserting the shifted height here would make `--no-height-shift` build an
    archive that cannot be run, which would leave no way to reproduce the old
    behaviour deliberately.
    """
    path = tmp_path / "atm.nc"
    forcing.write_atm(path, np.array([260.0, 261.0]), np.array([10.0, 11.0]),
                      ORIGIN, _series(), "gefs", "gom_25km",
                      scalar_height=forcing.PRODUCT_HEIGHT)
    assert forcing.assert_reference_height(path) == 2.0
