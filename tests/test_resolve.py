"""The experiment-time substitution pass."""

import pytest

from ackbar.config.resolve import (
    ResolveError, builtin_symbols, resolve, symbol_table, unresolved,
)

SITE = {"scratch_root": "/scratch", "output_root": "/out"}


def config(**extra):
    base = {"experiment": {"name": "exp1"}}
    base.update(extra)
    return base


class TestBuiltins:
    def test_paths_come_from_the_site_roots_and_the_experiment_name(self):
        symbols = builtin_symbols(config(), SITE)
        assert symbols["experiment"] == "exp1"
        assert symbols["experiment_dir"] == "/out/exp1"
        assert symbols["scratch_dir"] == "/scratch/exp1"

    def test_the_on_disk_layout_is_built_in_rather_than_spelled_out_by_layers(self):
        symbols = builtin_symbols(config(), SITE)
        assert symbols["rst_dir"] == "/out/exp1/rst"
        assert symbols["obs_out_dir"] == "/out/exp1/obs_out"
        assert symbols["cfg_dir"] == "/out/exp1/cfg"

    def test_a_var_may_not_shadow_a_builtin(self):
        # Otherwise what a symbol means depends on which layer defined it.
        with pytest.raises(ResolveError, match="may not redefine"):
            symbol_table(config(vars={"experiment_dir": "/elsewhere"}), SITE)


class TestSubstitution:
    def test_a_whole_token_keeps_the_symbol_type(self):
        # minvalue: $(x) has to become a number, or the schema and JEDI both
        # see a string where they want a float.
        out = resolve(config(vars={"x": 0.5}, a={"minvalue": "$(x)"}), SITE)
        assert out["a"]["minvalue"] == 0.5
        assert isinstance(out["a"]["minvalue"], float)

    @pytest.mark.parametrize("value", [0.5, 20, True, ["a", "b"], {"k": "v"}])
    def test_a_whole_token_passes_any_type_through(self, value):
        out = resolve(config(vars={"x": value}, a="$(x)"), SITE)
        assert out["a"] == value

    def test_an_embedded_token_interpolates_as_text(self):
        out = resolve(config(vars={"n": 4}, a="mem$(n)/rst"), SITE)
        assert out["a"] == "mem4/rst"

    def test_several_tokens_in_one_string(self):
        out = resolve(config(vars={"a": "x", "b": "y"}, k="$(a)-$(b)"), SITE)
        assert out["k"] == "x-y"

    def test_a_container_cannot_be_interpolated_into_text(self):
        with pytest.raises(ResolveError, match="cannot be interpolated"):
            resolve(config(vars={"x": [1, 2]}, a="p/$(x)"), SITE)

    def test_substitution_reaches_into_lists_and_nested_dicts(self):
        cfg = config(vars={"x": 1}, a=[{"b": [{"c": "$(x)"}]}])
        assert resolve(cfg, SITE)["a"][0]["b"][0]["c"] == 1

    def test_non_strings_are_left_alone(self):
        cfg = config(a=1, b=None, c=True, d=[1.5])
        out = resolve(cfg, SITE)
        assert (out["a"], out["b"], out["c"], out["d"]) == (1, None, True, [1.5])


class TestJobTimeTokensSurvive:
    def test_double_brace_tokens_are_not_touched(self):
        out = resolve(config(a="{{current_cycle}}/f.nc"), SITE)
        assert out["a"] == "{{current_cycle}}/f.nc"

    def test_both_passes_can_appear_in_one_value(self):
        cfg = config(vars={"d": "/archive"}, a="$(d)/{{current_cycle}}/x.nc4")
        assert resolve(cfg, SITE)["a"] == "/archive/{{current_cycle}}/x.nc4"


class TestErrors:
    def test_an_unknown_symbol_names_the_config_path(self):
        with pytest.raises(ResolveError) as caught:
            resolve(config(domain={"resources": {"forecast": {"ntasks": "$(pes)"}}}), SITE)
        assert caught.value.path == "domain.resources.forecast.ntasks"
        assert "$(pes)" in str(caught.value)

    def test_the_error_points_at_the_job_time_syntax(self):
        # The likely mistake is reaching for a cycle date at experiment time.
        with pytest.raises(ResolveError, match=r"\{\{\.\.\.\}\}"):
            resolve(config(a="$(current_cycle)"), SITE)

    def test_a_path_through_a_list_is_reported_with_its_index(self):
        with pytest.raises(ResolveError) as caught:
            resolve(config(observations=[{"a": 1}, {"b": "$(nope)"}]), SITE)
        assert caught.value.path == "observations[1].b"


class TestVarsReferencingVars:
    def test_a_var_may_reference_another_var(self):
        table = symbol_table(config(vars={"root": "/a", "sub": "$(root)/b"}), SITE)
        assert table["sub"] == "/a/b"

    def test_a_var_may_reference_a_builtin(self):
        table = symbol_table(config(vars={"obs": "$(experiment_dir)/obs"}), SITE)
        assert table["obs"] == "/out/exp1/obs"

    def test_order_in_the_file_does_not_matter(self):
        table = symbol_table(config(vars={"sub": "$(root)/b", "root": "/a"}), SITE)
        assert table["sub"] == "/a/b"

    def test_a_direct_cycle_is_caught(self):
        with pytest.raises(ResolveError, match="circular"):
            symbol_table(config(vars={"a": "$(a)"}), SITE)

    def test_an_indirect_cycle_is_caught_and_shows_the_chain(self):
        with pytest.raises(ResolveError) as caught:
            symbol_table(config(vars={"a": "$(b)", "b": "$(c)", "c": "$(a)"}), SITE)
        assert "a -> b -> c -> a" in str(caught.value)

    def test_a_var_referencing_something_undefined_is_an_error(self):
        with pytest.raises(ResolveError, match=r"unknown symbol \$\(nope\)"):
            symbol_table(config(vars={"a": "$(nope)"}), SITE)


class TestUnresolved:
    def test_a_resolved_config_has_nothing_left(self):
        out = resolve(config(vars={"x": 1}, a="$(x)", b="{{cycle}}"), SITE)
        assert unresolved(out) == []

    def test_it_finds_a_token_that_slipped_through(self):
        assert unresolved({"a": {"b": "$(x)"}}) == [("a.b", "$(x)")]
