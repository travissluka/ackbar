"""ISO 8601 durations and instants."""

from datetime import datetime, timedelta, timezone

import pytest

from ackbar.duration import (
    DurationError,
    format_duration,
    format_instant,
    parse_duration,
    parse_instant,
)


class TestParse:
    @pytest.mark.parametrize("text,expected", [
        ("PT6H", timedelta(hours=6)),
        ("PT24H", timedelta(hours=24)),
        ("P1D", timedelta(days=1)),
        ("P7D", timedelta(days=7)),
        ("P1W", timedelta(weeks=1)),
        ("PT30M", timedelta(minutes=30)),
        ("PT90S", timedelta(seconds=90)),
        ("P1DT12H30M", timedelta(days=1, hours=12, minutes=30)),
        ("P0DT0H0M0S", timedelta(0)),
    ])
    def test_accepted(self, text, expected):
        assert parse_duration(text) == expected

    @pytest.mark.parametrize("text", ["P", "PT", "24H", "", "P1H", "1D", "PT1D"])
    def test_rejected(self, text):
        with pytest.raises(DurationError):
            parse_duration(text)

    @pytest.mark.parametrize("text", ["P1M", "P1Y", "P1Y6M", "P2MT12H"])
    def test_calendar_units_are_rejected_by_name(self, text):
        # Not merely unsupported. `start + n * P1M` is not a function of n, so
        # cycle 40's date would depend on the path taken to reach it.
        with pytest.raises(DurationError, match="years and months"):
            parse_duration(text)

    def test_minutes_after_the_t_are_still_minutes(self):
        # The same letter means months before the T and minutes after it, and
        # the rejection above must not swallow the second case.
        assert parse_duration("PT15M") == timedelta(minutes=15)


class TestFormat:
    def test_round_trips(self):
        for text in ("PT6H", "P1D", "P7D", "P1DT12H30M"):
            assert parse_duration(format_duration(parse_duration(text))) == \
                parse_duration(text)

    def test_the_form_is_canonical_not_short(self):
        # Two spellings of the same duration rendering differently would be a
        # golden diff that means nothing.
        assert format_duration(timedelta(hours=24)) == "P1DT0H0M0S"
        assert format_duration(parse_duration("P1D")) == \
            format_duration(parse_duration("PT24H"))


class TestInstants:
    def test_a_trailing_z_is_utc(self):
        # fromisoformat learned to read Z only in 3.11, and the configs are
        # full of them.
        assert parse_instant("2018-04-15T00:00:00Z") == \
            datetime(2018, 4, 15, tzinfo=timezone.utc)

    def test_a_naive_time_is_assumed_utc(self):
        assert parse_instant("2018-04-15T00:00:00") == \
            datetime(2018, 4, 15, tzinfo=timezone.utc)

    def test_an_offset_is_converted(self):
        assert parse_instant("2018-04-15T06:00:00+06:00") == \
            datetime(2018, 4, 15, tzinfo=timezone.utc)

    def test_a_datetime_passes_through(self):
        # PyYAML resolves an unquoted timestamp to a datetime, so the config
        # may hand us either.
        assert parse_instant(datetime(2018, 4, 15)) == \
            datetime(2018, 4, 15, tzinfo=timezone.utc)

    def test_format_is_what_jedi_reads(self):
        assert format_instant(parse_instant("2018-04-15T00:00:00Z")) == \
            "2018-04-15T00:00:00Z"

    def test_a_bad_date_names_itself(self):
        with pytest.raises(DurationError, match="15 April 2018"):
            parse_instant("15 April 2018")
