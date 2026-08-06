"""The job-time pass: the closed symbol set, and how tokens are substituted."""

import pytest

from ackbar.config.jobtime import (
    SYMBOL_NAMES,
    JobTimeError,
    cycle_time,
    forecast_length,
    forecast_overshoot,
    handoff_time,
    member_dir,
    render,
    seed_for,
    slot_length,
    slot_times,
    symbols,
    unresolved,
    window_bounds,
    window_length,
)
from ackbar.duration import format_instant

CONFIG = {
    "experiment": {"name": "exp"},
    "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H", "count": 10},
}


def table(cycle=1, member=0, config=CONFIG):
    return symbols(config, cycle, member)


class TestCycleArithmetic:
    def test_cycle_one_is_at_the_start(self):
        assert format_instant(cycle_time(CONFIG, 1)) == "2018-04-15T00:00:00Z"

    def test_cycle_n_is_computable_from_n_alone(self):
        # The property the whole graph rests on: heal regenerates cycle 40
        # without simulating the 39 before it.
        assert format_instant(cycle_time(CONFIG, 40)) == "2018-05-24T00:00:00Z"

    def test_cycle_zero_is_where_the_offline_initial_condition_lands(self):
        # Which is what stops cycle 1 from being a special case.
        assert format_instant(cycle_time(CONFIG, 0)) == "2018-04-14T00:00:00Z"

    def test_the_window_is_centred_and_one_cycle_long(self):
        begin, end = window_bounds(CONFIG, 2)
        assert format_instant(begin) == "2018-04-15T12:00:00Z"
        assert format_instant(end) == "2018-04-16T12:00:00Z"

    def test_consecutive_windows_tile_without_gap_or_overlap(self):
        assert window_bounds(CONFIG, 2)[1] == window_bounds(CONFIG, 3)[0]

    def test_a_declared_window_is_still_centred_on_the_analysis_time(self):
        # Which is where the analysis is written: CostFctFGAT saves the state
        # at the window's midpoint. A window centred anywhere else produces an
        # analysis valid at a time no cycle starts from.
        short = {**CONFIG, "solver": {"window": {"type": "fgat",
                                                 "length": "PT6H"}}}
        assert window_length(short).total_seconds() == 6 * 3600
        begin, end = window_bounds(short, 2)
        assert format_instant(begin) == "2018-04-15T21:00:00Z"
        assert format_instant(end) == "2018-04-16T03:00:00Z"


class TestTheForecastCoversTheNextWindow:
    """What a four-dimensional window costs the model, and why.

    The next cycle's window is centred on the next analysis time, so half of it
    lies *after* that time. Nothing but this cycle's forecast can produce those
    states: the next cycle's own forecast starts from the analysis, so a state
    it writes at the same clock time is a different trajectory.
    """

    FGAT = {**CONFIG, "solver": {"window": {"type": "fgat"}},
            "forecast": {"slots": "PT6H"}}

    def test_a_three_dimensional_window_costs_nothing_extra(self):
        assert forecast_overshoot(CONFIG).total_seconds() == 0
        assert forecast_length(CONFIG) == window_length(CONFIG)

    def test_a_four_dimensional_window_costs_half_of_itself(self):
        # 1 + W/2C times the model, which at the default W == C is one and a
        # half, and is not avoidable by moving the window.
        assert forecast_overshoot(self.FGAT).total_seconds() == 12 * 3600
        assert forecast_length(self.FGAT).total_seconds() == 36 * 3600

    def test_the_states_are_exactly_the_next_cycles_window(self):
        assert slot_times(self.FGAT, 2)[0] == window_bounds(self.FGAT, 3)[0]
        assert slot_times(self.FGAT, 2)[-1] == window_bounds(self.FGAT, 3)[1]

    def test_the_next_cycle_starts_from_a_state_the_run_wrote_on_its_way(self):
        # Not the last thing written, which is half a window later. It is one
        # of the intervals, and it has to be one the forecast actually reached.
        handoff = handoff_time(self.FGAT, 2)
        assert format_instant(handoff) == "2018-04-17T00:00:00Z"
        assert handoff in slot_times(self.FGAT, 2)
        assert handoff != slot_times(self.FGAT, 2)[-1]

    def test_a_window_shorter_than_the_cycle_still_ends_on_the_last_instant(self):
        # The states are the last window's worth of the forecast wherever the
        # window falls, so a six hour window on a 24 hour cycle is three states
        # and not thirteen.
        short = {**CONFIG,
                 "solver": {"window": {"type": "fgat", "length": "PT6H"}},
                 "forecast": {"slots": "PT3H"}}
        assert [format_instant(t) for t in slot_times(short, 1)] == [
            "2018-04-15T21:00:00Z", "2018-04-16T00:00:00Z",
            "2018-04-16T03:00:00Z"]


class TestSlots:
    """The sub-window states one forecast writes as it goes."""

    CADENCED = {**CONFIG, "forecast": {"slots": "PT6H"}}

    def test_an_experiment_that_asked_for_none_has_none(self):
        assert slot_length(CONFIG) is None
        assert slot_times(CONFIG, 2) == ()

    def test_the_times_span_the_forecast_and_end_on_its_last_instant(self):
        # Cycle 2 runs 2018-04-16 to the 17th, so a six hour cadence is 06, 12,
        # 18 and the end. The last one duplicates the committed restart set,
        # deliberately: the alternative is a slot series with a hole at its own
        # last entry for every consumer to special-case.
        assert [format_instant(t) for t in slot_times(self.CADENCED, 2)] == [
            "2018-04-16T06:00:00Z", "2018-04-16T12:00:00Z",
            "2018-04-16T18:00:00Z", "2018-04-17T00:00:00Z"]

    def test_the_forecasts_own_start_is_not_one_of_them(self):
        # FMS writes when its clock *reaches* the next interval, so the first
        # lands one cadence in. The state at the start is the restart set the
        # forecast was handed rather than one it produced.
        assert cycle_time(self.CADENCED, 2) not in slot_times(self.CADENCED, 2)

    def test_the_cadence_reaches_mom6_as_its_own_interval_list(self):
        assert table(config=self.CADENCED)["mom6_restart_interval"] \
            == "0, 0, 0, 6, 0, 0"

    def test_no_cadence_is_the_value_that_means_write_none(self):
        assert table()["mom6_restart_interval"] == "0, 0, 0, 0, 0, 0"

    def test_a_cadence_longer_than_a_day_is_days_and_not_hours(self):
        # `restart_interval` is y, m, d, h, m, s, and 36 hours in the hours
        # field is a value FMS reads happily and a duration nobody wrote.
        weekly = {**CONFIG, "forecast": {"slots": "P1DT12H"}}
        assert table(config=weekly)["mom6_restart_interval"] == "0, 0, 1, 12, 0, 0"


class TestSeeds:
    def test_a_seed_is_reproducible_across_processes(self):
        # hash() is salted per process, so a heal would produce a different
        # ensemble than the original run and nothing would record it.
        assert seed_for(CONFIG, 3, 7) == seed_for(CONFIG, 3, 7)
        assert seed_for(CONFIG, 3, 7) == 2129586981

    def test_seeds_differ_by_cycle_member_and_experiment(self):
        other = {**CONFIG, "experiment": {"name": "other"}}
        assert seed_for(CONFIG, 3, 7) != seed_for(CONFIG, 4, 7)
        assert seed_for(CONFIG, 3, 7) != seed_for(CONFIG, 3, 8)
        assert seed_for(CONFIG, 3, 7) != seed_for(other, 3, 7)


class TestSubstitution:
    def test_a_whole_token_keeps_the_symbol_type(self):
        assert render({"m": "{{member}}"}, table(member=4)) == {"m": 4}
        assert isinstance(render({"s": "{{seed}}"}, table())["s"], int)

    def test_an_embedded_token_interpolates_as_text(self):
        out = render({"d": "rst/{{member_dir}}/MOM.res.nc"}, table(member=4))
        assert out["d"] == "rst/mem004/MOM.res.nc"

    def test_a_date_defaults_to_the_form_jedi_parses(self):
        assert render("{{window_begin}}", table(cycle=2)) == "2018-04-15T12:00:00Z"

    def test_a_format_spec_gives_a_path_friendly_date(self):
        # The reason the spec exists: an archive directory named with colons in
        # it is a hazard, and the same date is wanted both ways in one file.
        assert render("{{current_cycle:%Y%m%d%H}}", table(cycle=2)) == "2018041600"

    def test_a_format_spec_works_on_a_number_too(self):
        assert render("{{member:03d}}", table(member=7)) == "007"

    def test_substitution_reaches_into_lists_and_keys_of_nested_maps(self):
        out = render(
            {"obs": [{"file": "{{current_cycle:%Y%m%d}}.nc", "n": 1}]}, table()
        )
        assert out == {"obs": [{"file": "20180415.nc", "n": 1}]}

    def test_non_strings_pass_through_untouched(self):
        assert render({"a": 5, "b": None, "c": True}, table()) == \
            {"a": 5, "b": None, "c": True}

    def test_experiment_time_tokens_are_not_this_pass_s_business(self):
        # By this point they are long resolved, but the two syntaxes must not
        # collide even so.
        assert render("$(experiment_dir)", table()) == "$(experiment_dir)"


class TestTheSetIsClosed:
    def test_an_unknown_symbol_is_an_error_naming_the_path(self):
        with pytest.raises(JobTimeError) as caught:
            render({"a": {"b": "{{window_bgin}}"}}, table())
        assert caught.value.path == "a.b"
        assert "window_bgin" in caught.value.message

    def test_the_error_points_at_the_list_of_symbols(self):
        # v3's second pass resolved whatever happened to be in scope. The whole
        # difference is that this set is enumerable.
        with pytest.raises(JobTimeError, match="ackbar config symbols"):
            render("{{nope}}", table())

    def test_every_advertised_symbol_actually_resolves(self):
        for name in SYMBOL_NAMES:
            assert render(f"{{{{{name}}}}}", table()) is not None

    def test_nothing_is_advertised_that_the_table_does_not_have(self):
        assert set(SYMBOL_NAMES) == set(table())


class TestUnresolved:
    def test_a_clean_render_leaves_nothing(self):
        assert unresolved(render({"a": "{{cycle}}"}, table())) == []

    def test_a_survivor_is_reported_with_its_path(self):
        assert unresolved({"a": ["x", "{{cycle}}"]}) == [("a[1]", "{{cycle}}")]


class TestMemberDirectories:
    @pytest.mark.parametrize("member,expected", [(0, "mem000"), (7, "mem007"), (20, "mem020")])
    def test_every_member_is_memnnn_including_the_control(self, member, expected):
        # No ctrl-versus-ens split anywhere in the tree.
        assert member_dir(member) == expected
