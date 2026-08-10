"""The forcing box, the clip, and the file every source is normalized into.

`src/ackbar/forcing.py` is what both fetchers share, and almost everything in it
fails silently rather than loudly. A box computed a degree short leaves the
outermost row of the model interpolating from nothing; a latitude axis read in
the wrong direction flips the field about the equator, which over the Gulf is a
plausible looking wind blowing the wrong way; a `write_atm` that drops an
attribute produces a file FMS reads five hundred days off. None of those are
visible in a plot, so they are pinned here.

`tests/test_forcing_deaverage.py` covers the other half, the GEFS de-averaging,
and lives apart because it has to import the tool by path.
"""

import datetime
from pathlib import Path

import netCDF4
import numpy as np
import pytest

from ackbar import forcing


# --- the calendar --------------------------------------------------------
#
# NOLEAP has no 29 February, so a span containing one shifts every date after it
# by a day. That is a wrong answer rather than a failure, which is why the tool
# refuses the span instead of trusting whoever asked for it.


def test_a_span_over_29_february_is_refused():
    with pytest.raises(SystemExit) as error:
        forcing.assert_no_leap_day(datetime.datetime(2016, 2, 20),
                                   datetime.datetime(2016, 3, 10))
    assert "NOLEAP" in str(error.value)


def test_a_span_inside_a_leap_year_that_misses_the_day_is_allowed():
    """The year being a leap year is not the question; the span is."""
    forcing.assert_no_leap_day(datetime.datetime(2016, 3, 1),
                               datetime.datetime(2016, 12, 31))
    forcing.assert_no_leap_day(datetime.datetime(2016, 1, 1),
                               datetime.datetime(2016, 2, 28))


def test_a_span_in_a_common_year_is_allowed():
    forcing.assert_no_leap_day(datetime.datetime(2015, 2, 20),
                               datetime.datetime(2015, 3, 10))


def test_a_multi_year_span_is_refused_for_a_leap_day_in_the_middle():
    """The loop is over years, so a span whose endpoints are both in common
    years still has to be checked against the years between them."""
    with pytest.raises(SystemExit) as error:
        forcing.assert_no_leap_day(datetime.datetime(2015, 6, 1),
                                   datetime.datetime(2017, 6, 1))
    assert "2016-02-29" in str(error.value)


def test_the_day_at_either_end_of_the_span_still_counts():
    for start, end in ((datetime.datetime(2016, 2, 29),
                        datetime.datetime(2016, 3, 5)),
                       (datetime.datetime(2016, 2, 1),
                        datetime.datetime(2016, 2, 29))):
        with pytest.raises(SystemExit):
            forcing.assert_no_leap_day(start, end)


# --- humidity ------------------------------------------------------------
#
# The one closed-form conversion in the module, so it has checkable answers
# rather than only properties. The anchors below are textbook saturation values,
# not values re-derived from the same expression, which is the whole point of
# writing them down.


def test_the_saturation_vapour_pressure_at_the_triple_point_is_the_constant():
    """At 273.16 K the exponent is exactly zero, so the formula must return its
    own leading constant. That pins the two offsets in the exponent, which is
    where a transcription error would sit."""
    pressure = 101325.0
    got = forcing.specific_humidity(np.array([273.16]), np.array([pressure]))
    ratio = 0.621981
    expected = ratio * 611.21 / (pressure - (1.0 - ratio) * 611.21)
    assert float(got[0]) == pytest.approx(expected, rel=1e-12)


def test_saturation_humidity_matches_the_textbook_values():
    """Saturation specific humidity at 1013.25 hPa: about 3.8 g/kg at 0 C and
    about 14.5 g/kg at 20 C. Checked to 2%, which is looser than the formula and
    tighter than any plausible mistake in it."""
    pressure = np.full(2, 101325.0)
    dewpoint = np.array([273.15, 293.15])
    got = forcing.specific_humidity(dewpoint, pressure)
    assert float(got[0]) == pytest.approx(0.00376, rel=0.02)
    assert float(got[1]) == pytest.approx(0.01446, rel=0.02)


def test_humidity_rises_with_dewpoint_and_falls_with_pressure():
    """The two monotonicities. A sign slipped in the exponent or the ratio
    inverted still produces a number, and this is what says which way it goes."""
    dewpoint = np.array([273.15, 283.15, 293.15, 303.15])
    warmer = forcing.specific_humidity(dewpoint, np.full(4, 101325.0))
    assert np.all(np.diff(warmer) > 0)
    thinner = forcing.specific_humidity(np.full(4, 293.15),
                                        np.array([70000.0, 85000.0,
                                                  95000.0, 101325.0]))
    assert np.all(np.diff(thinner) < 0)


# --- the longitude extent ------------------------------------------------
#
# `min` and `max` cannot answer this, and the case that breaks them is the one
# that costs the most: a wrapped grid read as global turns a 290 MB ERA5 request
# into about 100 GB.


def test_a_plain_regional_extent_is_its_own_ends():
    west, east = forcing.lon_extent(np.array([260.0, 270.0, 280.0]))
    assert (west, east) == (260.0, 280.0)


def test_a_grid_wrapped_through_zero_goes_the_short_way_round():
    """The failure the extent exists for. A domain holding both 350 and 10 spans
    twenty degrees, not three hundred and forty, and the returned west is
    greater than the returned east to say so."""
    lon = np.concatenate([np.arange(350.0, 360.0), np.arange(0.0, 11.0)])
    assert forcing.lon_extent(lon) == (350.0, 10.0)


def test_a_grid_that_really_is_global_keeps_the_whole_range():
    """A global grid's largest gap is one cell, which is the case the wrapped
    test must not break."""
    lon = np.arange(0.0, 360.0, 1.0)
    assert forcing.lon_extent(lon) == (0.0, 359.0)


def test_negative_longitudes_are_read_on_the_same_circle():
    """A [-180, 180) source and a [0, 360) source describe the same domain."""
    signed = np.array([-100.0, -90.0, -80.0])
    assert forcing.lon_extent(signed) == (260.0, 280.0)


def test_a_single_column_has_no_extent_to_take():
    """`np.diff` of one value is empty, so this would be an argmax over nothing.
    A degenerate grid is not a real domain and is not a crash either."""
    assert forcing.lon_extent(np.array([[262.0, 262.0]])) == (262.0, 262.0)


# --- the box -------------------------------------------------------------


def test_the_box_pads_the_extent_by_the_margin():
    lon = np.array([[260.0, 280.0]])
    lat = np.array([[18.0, 30.0]])
    box = forcing.box_around(lon, lat, margin=4.0)
    assert box == {"west": 256.0, "east": 284.0, "south": 14.0, "north": 34.0}


def test_the_box_takes_the_extent_and_not_the_corners():
    """A rotated or curvilinear grid's corners are not its bounding box, so the
    box has to come from every point rather than from four of them."""
    lon = np.array([[262.0, 270.0], [258.0, 266.0]])
    lat = np.array([[20.0, 26.0], [24.0, 30.0]])
    box = forcing.box_around(lon, lat, margin=1.0)
    assert box["west"] == 257.0 and box["east"] == 271.0
    assert box["south"] == 19.0 and box["north"] == 31.0


def test_a_global_domain_asks_for_the_globe():
    lon = np.arange(0.0, 360.0, 1.0)[None, :]
    lat = np.linspace(-89.0, 89.0, 20)[:, None]
    box = forcing.box_around(lon, lat)
    assert box["west"] == 0.0 and box["east"] == 360.0


def test_the_padded_box_is_clamped_at_the_poles():
    """Latitude does not wrap, so the margin has to stop rather than run past
    90 and select nothing."""
    lon = np.array([[10.0, 20.0]])
    lat = np.array([[-88.0, 89.0]])
    box = forcing.box_around(lon, lat, margin=4.0)
    assert box["south"] == -90.0 and box["north"] == 90.0


def test_a_wrapped_regional_domain_is_refused_rather_than_fetched_whole():
    """The refusal that replaced classifying such a domain as global. Two reads
    joined is the honest implementation, and no domain here needs it, so this
    says so instead of half-doing it."""
    lon = np.concatenate([np.arange(350.0, 360.0), np.arange(0.0, 11.0)])[None, :]
    lat = np.array([[0.0, 5.0]])
    with pytest.raises(SystemExit) as error:
        forcing.box_around(lon, lat, margin=4.0)
    assert "longitude seam" in str(error.value)


def test_a_domain_whose_padding_alone_crosses_the_seam_is_refused_too():
    """The domain does not wrap; its box does. Same single-slice problem, and
    the easier one to miss because the grid itself looks ordinary."""
    lon = np.array([[1.0, 20.0]])
    lat = np.array([[0.0, 5.0]])
    with pytest.raises(SystemExit) as error:
        forcing.box_around(lon, lat, margin=4.0)
    assert "longitude seam" in str(error.value)


#: The box `gom_25km` asks a source for. A golden, because the box is the one
#: number in the fetch that decides what data exists on disk afterwards: too
#: small and the model interpolates from nothing along an edge, and nothing in
#: the run says so.
GOM_25KM_BOX = {"west": 257.875, "east": 287.625,
                "south": 13.995, "north": 35.995}


def test_the_gulf_box_is_what_the_archive_was_built_with(monkeypatch):
    """Read from the domain's own supergrid rather than restated, which is why
    this needs the staged domain and skips without it."""
    root = "/data/ackbar/static"
    grid = Path(root, "domain", "gom_25km", "INPUT", "ocean_hgrid.nc")
    if not grid.exists():
        pytest.skip(f"{grid} is not staged")
    monkeypatch.setenv("ACKBAR_STATIC_ROOT", root)
    assert forcing.domain_box("gom_25km") == pytest.approx(GOM_25KM_BOX)


def test_a_domain_with_no_grid_says_which_stage_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ACKBAR_STATIC_ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as error:
        forcing.domain_box("gom_25km")
    assert "gom_25km" in str(error.value)


def test_an_unactivated_site_is_named_as_such(monkeypatch):
    monkeypatch.delenv("ACKBAR_STATIC_ROOT", raising=False)
    with pytest.raises(SystemExit) as error:
        forcing.domain_box("gom_25km")
    assert "activate.sh" in str(error.value)


# --- the slices, and the latitude direction ------------------------------
#
# The direction is the dangerous one. Reading a descending axis as ascending
# mirrors the field about the middle of the box, which over the Gulf is a
# perfectly plausible wind pattern pointing the wrong way, and no check
# downstream of here can see it.


def test_an_ascending_source_is_not_flipped():
    lons = np.arange(250.0, 300.0, 1.0)
    lats = np.arange(0.0, 50.0, 1.0)
    box = {"west": 260.0, "east": 265.0, "south": 10.0, "north": 15.0}
    sy, sx, y, x, flip_y = forcing.box_slices(lons, lats, box)
    assert not flip_y
    assert (sy, sx) == (slice(10, 16), slice(10, 16))
    assert list(x) == [260.0, 261.0, 262.0, 263.0, 264.0, 265.0]
    assert list(y) == [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]


def test_a_descending_source_is_reported_and_its_axis_returned_ascending():
    """ERA5 hands latitude down from the north pole. The slice still indexes the
    file's own order, and the axis handed back is the one the output carries."""
    lons = np.arange(250.0, 300.0, 1.0)
    lats = np.arange(49.0, -1.0, -1.0)
    box = {"west": 260.0, "east": 262.0, "south": 10.0, "north": 12.0}
    sy, sx, y, x, flip_y = forcing.box_slices(lons, lats, box)
    assert flip_y
    assert (sy.start, sy.stop) == (37, 40)
    assert list(y) == [10.0, 11.0, 12.0]


def test_a_single_row_of_latitude_is_not_a_direction():
    """`y[0] > y[-1]` is false for one row either way, and flipping a length one
    axis would be a no-op that still has to not be reported."""
    lons = np.arange(250.0, 300.0, 1.0)
    lats = np.array([12.0])
    box = {"west": 260.0, "east": 262.0, "south": 10.0, "north": 15.0}
    *_, flip_y = forcing.box_slices(lons, lats, box)
    assert not flip_y


def test_a_source_that_does_not_reach_the_box_says_what_it_does_cover():
    lons = np.arange(0.0, 40.0, 1.0)
    lats = np.arange(0.0, 40.0, 1.0)
    box = {"west": 260.0, "east": 265.0, "south": 10.0, "north": 15.0}
    with pytest.raises(SystemExit) as error:
        forcing.box_slices(lons, lats, box)
    assert "does not reach" in str(error.value)


def test_a_scrambled_axis_is_refused_rather_than_silently_widening_the_box():
    """The selection is turned into a slice, so a gap in it would quietly pull in
    everything between the two pieces."""
    lons = np.array([260.0, 261.0, 350.0, 262.0])
    lats = np.arange(0.0, 40.0, 1.0)
    box = {"west": 260.0, "east": 262.0, "south": 10.0, "north": 15.0}
    with pytest.raises(SystemExit) as error:
        forcing.box_slices(lons, lats, box)
    assert "not contiguous in longitude" in str(error.value)


# --- the clip ------------------------------------------------------------


def test_the_clip_cuts_to_the_box():
    lons = np.arange(250.0, 300.0, 1.0)
    lats = np.arange(0.0, 50.0, 1.0)
    cube = np.arange(2 * 50 * 50, dtype="f8").reshape(2, 50, 50)
    box = {"west": 260.0, "east": 262.0, "south": 10.0, "north": 12.0}
    x, y, out = forcing.clip(lons, lats, cube, box)
    assert list(x) == [260.0, 261.0, 262.0]
    assert list(y) == [10.0, 11.0, 12.0]
    assert out.shape == (2, 3, 3)
    assert np.array_equal(out, cube[:, 10:13, 10:13])


def test_the_clip_flips_the_field_with_the_axis_it_flipped():
    """The catastrophic one. If the axis is reversed and the cube is not, every
    row of the field is paired with the wrong latitude, and the file that comes
    out is a valid file of a mirrored ocean.

    Built so the answer is unambiguous: the value at each point is its own
    latitude, so a row that has moved without its coordinate is visible.
    """
    lons = np.arange(250.0, 300.0, 1.0)
    lats = np.arange(49.0, -1.0, -1.0)
    cube = np.broadcast_to(lats[None, :, None], (1, 50, 50)).copy()
    box = {"west": 260.0, "east": 262.0, "south": 10.0, "north": 12.0}
    x, y, out = forcing.clip(lons, lats, cube, box)
    assert list(y) == [10.0, 11.0, 12.0]
    assert np.array_equal(out[0, :, 0], np.array(y))


# --- the file ------------------------------------------------------------


def series_for(hours=(0.0, 3.0), shape=(2, 2)):
    """One record set with every field present, which `write_atm` requires."""
    hours = np.asarray(hours, dtype="f8")
    return {name: (hours, np.full((hours.size,) + shape, i + 1.0, dtype="f8"))
            for i, name in enumerate(forcing.FIELDS)}


ORIGIN = datetime.datetime(2015, 7, 11, 0, 0, 0)


def test_the_file_carries_every_field_on_its_own_time_axis(tmp_path):
    """Per-field axes are what let an instantaneous field and an interval mean be
    stamped honestly. One shared axis would interpolate one of them."""
    path = tmp_path / "atm.nc"
    forcing.write_atm(path, np.array([260.0, 261.0]), np.array([10.0, 11.0]),
                      ORIGIN, series_for(), "era5", "gom_25km")
    with netCDF4.Dataset(path) as f:
        assert set(forcing.FIELDS) <= set(f.variables)
        for name in forcing.FIELDS:
            axis = f"time_{name}"
            assert f[name].dimensions == (axis, "LAT", "LON")
            assert f[axis].calendar == forcing.CALENDAR
            assert f[axis].units == "hours since 2015-07-11 00:00:00"
            assert f[axis].axis == "T"


def test_the_units_and_long_names_are_the_ones_the_data_table_expects(tmp_path):
    path = tmp_path / "atm.nc"
    forcing.write_atm(path, np.array([260.0, 261.0]), np.array([10.0, 11.0]),
                      ORIGIN, series_for(), "era5", "gom_25km")
    with netCDF4.Dataset(path) as f:
        for name, (units, long_name) in forcing.FIELDS.items():
            assert f[name].units == units
            assert f[name].long_name == long_name
        assert f["LON"].units == "degrees_east"
        assert f["LAT"].units == "degrees_north"


def test_the_values_and_the_axes_survive_the_round_trip(tmp_path):
    path = tmp_path / "atm.nc"
    x, y = np.array([260.0, 261.5]), np.array([10.0, 11.25])
    series = series_for(hours=(0.0, 3.0, 6.0))
    forcing.write_atm(path, x, y, ORIGIN, series, "era5", "gom_25km")
    with netCDF4.Dataset(path) as f:
        assert list(f["LON"][:]) == list(x)
        assert list(f["LAT"][:]) == list(y)
        for name, (hours, cube) in series.items():
            assert list(f[f"time_{name}"][:]) == list(hours)
            assert np.allclose(f[name][:], cube)


def test_the_box_attribute_describes_the_axes_and_not_the_request(tmp_path):
    """The written box differs from the requested one by up to a source cell on
    every edge, and the attribute is read as a statement about the file."""
    path = tmp_path / "atm.nc"
    forcing.write_atm(path, np.array([257.875, 287.625]),
                      np.array([13.995, 35.995]),
                      ORIGIN, series_for(), "era5", "gom_25km")
    with netCDF4.Dataset(path) as f:
        assert f.box == "257.875 to 287.625 east, 13.995 to 35.995 north"
        assert f.source == "era5" and f.domain == "gom_25km"


def test_a_missing_field_is_refused_before_anything_is_written(tmp_path):
    """A data table naming a variable the file lacks fails inside
    `time_interp_external` with a message about the file, not the variable."""
    path = tmp_path / "atm.nc"
    series = series_for()
    del series["DLWRF"]
    with pytest.raises(SystemExit) as error:
        forcing.write_atm(path, np.array([260.0, 261.0]),
                          np.array([10.0, 11.0]), ORIGIN, series,
                          "era5", "gom_25km")
    assert "DLWRF" in str(error.value)
    assert not path.exists()


def test_a_field_with_the_wrong_number_of_records_is_refused(tmp_path):
    path = tmp_path / "atm.nc"
    series = series_for()
    hours, cube = series["T2"]
    series["T2"] = (hours, cube[:1])
    with pytest.raises(SystemExit) as error:
        forcing.write_atm(path, np.array([260.0, 261.0]),
                          np.array([10.0, 11.0]), ORIGIN, series,
                          "era5", "gom_25km")
    assert "1 records against 2 times" in str(error.value)


def test_times_that_do_not_increase_are_refused(tmp_path):
    """A repeated or reversed stamp is what a mis-ordered fetch produces, and FMS
    reads such an axis without complaining."""
    path = tmp_path / "atm.nc"
    series = series_for(hours=(3.0, 3.0))
    with pytest.raises(SystemExit) as error:
        forcing.write_atm(path, np.array([260.0, 261.0]),
                          np.array([10.0, 11.0]), ORIGIN, series,
                          "era5", "gom_25km")
    assert "not increasing" in str(error.value)


def test_a_non_finite_value_is_refused_where_the_source_is_still_known(tmp_path):
    """`data_override` interpolates a NaN into the flux and the ocean carries it
    from the first timestep, by which point the source is long gone."""
    path = tmp_path / "atm.nc"
    series = series_for()
    hours, cube = series["PRATE"]
    cube = cube.copy()
    cube[0, 0, 0] = np.nan
    series["PRATE"] = (hours, cube)
    with pytest.raises(SystemExit) as error:
        forcing.write_atm(path, np.array([260.0, 261.0]),
                          np.array([10.0, 11.0]), ORIGIN, series,
                          "era5", "gom_25km")
    assert "PRATE" in str(error.value) and "non-finite" in str(error.value)
