"""The job-time pass: the closed symbol set, and how tokens are substituted."""

import pytest

from ackbar.config.jobtime import (
    SYMBOL_NAMES,
    JobTimeError,
    cycle_time,
    member_dir,
    render,
    seed_for,
    symbols,
    unresolved,
    window_bounds,
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
