"""Tier 1: the task graph, against goldens and against its own invariants.

Goldens catch a change nobody meant to make. The property tests below catch the
class of bug a golden cannot: the ones whose symptom is a job that pends
forever rather than a job that fails, which is most of what Slurm does wrong
when the graph is wrong.

Regenerate the goldens with ACKBAR_UPDATE_GOLDENS=1, and read the diff.
"""

import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import GraphError, build_graph, member_set, to_dot, to_text
from ackbar.observations import LOCALIZATION
from ackbar.graph.build import extended_leads, extended_slots

REPO = Path(__file__).resolve().parents[1]
LAYERS = REPO / "config" / "layers"
EXPERIMENTS = Path(__file__).resolve().parent / "experiments"
GOLDENS = Path(__file__).resolve().parent / "goldens"

SITE = {"scratch_root": "/scratch", "output_root": "/out",
        "static_root": "/static",
        "root": str(REPO)}

#: One per graph shape worth pinning.
GOLDENED = [
    "free_om1deg",
    "var_om1deg",
    "fourd_om1deg",
    "letkf_om1deg",
    "envar_om1deg",
    "hybrid_om1deg",
]


@pytest.fixture(scope="module")
def keys():
    return merge_keys(load_schema())


def load(name, keys):
    layers = resolve_layers(EXPERIMENTS / f"{name}.yaml", LAYERS)
    return resolve(merge_layers(layers, keys), SITE)


def graph_for(name, keys):
    return build_graph(load(name, keys))


@pytest.fixture(params=GOLDENED)
def named_graph(request, keys):
    return request.param, graph_for(request.param, keys)


def _golden_text(data):
    """One line per node and per edge.

    A golden is only useful if its diff is readable, and `json.dumps(indent=2)`
    spends twelve lines on a node so that a changed dependency is a paragraph
    of context. Comparison is on parsed JSON, so this layout is presentation
    only.
    """
    def block(key):
        return ",\n".join("    " + json.dumps(item) for item in data[key])

    return (
        "{\n"
        f'  "experiment": {json.dumps(data["experiment"])},\n'
        f'  "throttle": {data["throttle"]},\n'
        '  "nodes": [\n' + block("nodes") + "\n  ],\n"
        '  "edges": [\n' + block("edges") + "\n  ]\n"
        "}\n"
    )


class TestGoldens:
    def test_the_graph_matches_its_golden(self, named_graph):
        name, graph = named_graph
        path = GOLDENS / f"{name}.json"
        produced = graph.to_dict()

        if os.environ.get("ACKBAR_UPDATE_GOLDENS"):
            path.parent.mkdir(exist_ok=True)
            path.write_text(_golden_text(produced))

        assert path.exists(), f"no golden for {name}; ACKBAR_UPDATE_GOLDENS=1 to write it"
        assert produced == json.loads(path.read_text())

    def test_generating_twice_gives_the_same_graph(self, named_graph, keys):
        # Determinism is a requirement, not an observation: heal regenerates a
        # subgraph long after the original submission and must agree with it.
        name, graph = named_graph
        assert graph.to_dict() == graph_for(name, keys).to_dict()


class TestInvariants:
    def test_the_graph_is_acyclic(self, named_graph):
        _, graph = named_graph
        assert len(graph.order()) == len(graph.nodes)

    def test_every_member_array_carries_the_canonical_index_set(self, named_graph, keys):
        # A mismatched index range makes aftercorr behave in an undocumented
        # way, and the symptom is an invisible forever-pending job.
        name, graph = named_graph
        canonical = member_set(load(name, keys))
        for node in graph.nodes:
            # The long forecast and its evaluation run over
            # `forecast.extended.members`, which is the control alone by
            # default. They are exempt from the canonical set and not from each
            # other: `test_aftercorr_only_ever_joins_matching_index_sets` is
            # what holds those two together.
            if node.members and node.task not in ("forecast.ext", "hofx.ext", "post.fcst"):
                assert node.members == canonical, node.id

    def test_aftercorr_only_ever_joins_matching_index_sets(self, named_graph):
        _, graph = named_graph
        by_id = {n.id: n for n in graph.nodes}
        for edge in graph.edges:
            if edge.kind == "aftercorr":
                assert by_id[edge.parent].members == by_id[edge.child].members, edge

    def test_leaves_have_no_successor(self, named_graph):
        # The whole reason cycles can overlap.
        _, graph = named_graph
        for task in ("post.obs", "post.state", "verify", "stats", "hofx.ext", "post.fcst"):
            for node in graph.nodes:
                if node.task == task:
                    assert graph.children(node.id) == [], node.id

    def test_no_leaf_waits_on_another_leaf_s_array(self, named_graph):
        """The reason `post.state -> verify` is not an edge.

        A leaf array hangs off the forecast by `aftercorr`, so a failed forecast
        member leaves its element *stranded* rather than failed. A stranded
        element never terminates on a permissive Slurm, so an `afterany` waiting
        on it never fires, and the leaf most wanted when something failed would
        be the one thing that never ran. Tier 2 found this; the invariant keeps
        it found.
        """
        _, graph = named_graph
        leaves = {"post.obs", "post.state", "verify", "stats", "hofx.ext", "post.fcst"}
        for edge in graph.edges:
            parent = edge.parent.split(".", 1)[1]
            child = edge.child.split(".", 1)[1]
            assert not (parent in leaves and child in leaves), edge

    def test_leaf_edges_tolerate_a_failed_upstream(self, named_graph):
        # afterok on an array is all-or-nothing, and the harvest is exactly the
        # task most wanted when a member has failed.
        _, graph = named_graph
        for edge in graph.edges:
            if edge.child.split(".", 1)[1] in ("stats", "post.obs", "verify"):
                assert edge.kind == "afterany", edge

    def test_nothing_outside_the_declared_roots_starts_unconditionally(self, named_graph):
        _, graph = named_graph
        first = min(graph.cycles)
        for node in graph.nodes:
            if node.cycle == first or node.task in ("cleanup", "stage.obs"):
                continue
            assert graph.parents(node.id), node.id


class TestConfigurationDrivesTheTaskSet:
    def test_a_free_run_has_no_analysis_and_no_writeback(self, keys):
        tasks = {n.task for n in graph_for("free_om1deg", keys).nodes}
        assert "da" not in tasks
        assert "writeback" not in tasks
        # noda is not a mode: observation evaluation is a separate property,
        # and here it is a standalone hofx.
        assert "hofx" in tasks

    def test_a_da_run_evaluates_observations_inside_the_analysis(self, keys):
        tasks = {n.task for n in graph_for("var_om1deg", keys).nodes}
        assert "hofx" not in tasks
        assert {"da", "writeback", "post.obs"} <= tasks

    def tasks_of(self, name, keys):
        return {n.task for n in graph_for(name, keys).nodes}

    def test_an_ensemble_covariance_recentres_and_an_letkf_does_not(self, keys):
        """The centre an LETKF would be recentred onto is its own mean.

        Which is the identity, so the job would exist to confirm it. A hybrid is
        where the centre is something else: the deterministic analysis, which
        saw the same observations through a covariance the ensemble does not
        have on its own.
        """
        assert "recenter" not in self.tasks_of("letkf_om1deg", keys)
        assert "recenter" in self.tasks_of("hybrid_om1deg", keys)
        assert "recenter" in self.tasks_of("envar_om1deg", keys)

    def test_only_an_ensemble_maintained_by_a_filter_has_a_second_analysis(self, keys):
        assert "da.ens" in self.tasks_of("hybrid_om1deg", keys)
        # `source: none`: the members run free and are only recentred.
        assert "da.ens" not in self.tasks_of("envar_om1deg", keys)
        # An LETKF's own analysis *is* the ensemble's.
        assert "da.ens" not in self.tasks_of("letkf_om1deg", keys)

    def test_a_pure_ensemble_covariance_has_no_static_b_to_calibrate(self, keys):
        assert "b.vt" in self.tasks_of("hybrid_om1deg", keys)
        assert "b.vt" not in self.tasks_of("envar_om1deg", keys)

    def test_the_two_ensemble_filters_build_the_same_graph(self, keys):
        """EAKF is a different solver, not a different cycle.

        `soca/test/CMakeLists.txt` gives the `eakf` ctest `EXE soca_letkf.x`, so
        it is the same application reading the same inputs and writing the same
        outputs, and every task, edge and array bound has to come out identical.
        Asserted as an equality rather than pinned as a second golden, because a
        golden would say what the shape is and this says the shape did not
        change.
        """
        letkf, eakf = graph_for("letkf_om1deg", keys), graph_for("eakf_om1deg", keys)

        def shape(graph):
            return sorted((n.cycle, n.task, n.members, n.exe, n.is_array)
                          for n in graph.nodes)

        def wiring(graph):
            return sorted((e.parent, e.child, e.kind) for e in graph.edges)

        assert shape(eakf) == shape(letkf)
        assert wiring(eakf) == wiring(letkf)

    def test_the_solver_chooses_the_executable(self, keys):
        def exe(name, task):
            return next(n.exe for n in graph_for(name, keys).nodes if n.task == task)

        assert exe("var_om1deg", "da").endswith("soca_var.x")
        assert exe("letkf_om1deg", "da").endswith("soca_letkf.x")
        assert exe("free_om1deg", "hofx").endswith("soca_hofx3d.x")
        # A hybrid runs both, which is the whole reason `da` split in two.
        assert exe("hybrid_om1deg", "da").endswith("soca_var.x")
        assert exe("hybrid_om1deg", "da.ens").endswith("soca_letkf.x")
        assert exe("hybrid_om1deg", "recenter").endswith("soca_ensrecenter.x")

    def test_a_covariance_drawn_from_an_unmaintained_ensemble_is_refused(self, keys):
        config = load("hybrid_om1deg", keys)
        config["ensemble"]["source"] = "eda"
        with pytest.raises(GraphError, match="nothing in this cycle implements"):
            build_graph(config)

    def test_an_ensemble_covariance_needs_a_control_to_recentre_onto(self, keys):
        """The centre is the deterministic analysis, which is the control's.

        Without one the first ensemble member would be treated as the answer,
        and the recentring would pull the ensemble onto one of its own members.
        """
        config = load("hybrid_om1deg", keys)
        config["ensemble"]["control"] = False
        with pytest.raises(GraphError, match="ensemble.control is false"):
            build_graph(config)

    def test_a_filter_maintained_ensemble_needs_the_filter_configured(self, keys):
        # The schema asks for these when the *solver* is an LETKF, and cannot
        # ask here, where what calls for one is a value in another subtree.
        config = load("hybrid_om1deg", keys)
        del config["solver"]["local ensemble DA"]
        with pytest.raises(GraphError, match="local ensemble DA"):
            build_graph(config)

    def test_a_filter_maintained_ensemble_needs_its_observers_localized(self, keys):
        """The one failure in this file that produces no bad-looking output.

        An unlocalized sample covariance of twenty members correlates every pair
        of points in the domain, so an observation on one side of the Gulf moves
        the analysis on the other, and the result still looks like a field. The
        check is here rather than in the schema because either subtree can
        satisfy it: `solver.ensemble localization` covers every observer at once,
        or each observer carries its own.
        """
        config = load("hybrid_om1deg", keys)
        assert not config["solver"].get("ensemble localization"), \
            "the fixture localizes per observer; this test would prove nothing"
        for entry in config["observations"]:
            del entry[LOCALIZATION]
        with pytest.raises(GraphError, match="would go into it unlocalized"):
            build_graph(config)

    def test_a_solver_localization_covers_observers_that_state_none(self, keys):
        # The `da/eakf` case: one statement about every observer at once.
        config = load("hybrid_om1deg", keys)
        for entry in config["observations"]:
            del entry[LOCALIZATION]
        config["solver"]["ensemble localization"] = [
            {"localization method": "Horizontal Gaspari-Cohn",
             "lengthscale": 100000}]
        build_graph(config)

    def test_four_d_changes_the_configuration_and_not_the_graph(self, keys):
        # The window type moves the arithmetic, not the task set: the overshoot
        # and the slot cadence change what the forecast job *does*, and there is
        # no task in one that is not in the other.
        three, four = graph_for("var_om1deg", keys), graph_for("fourd_om1deg", keys)
        assert [n.task for n in three.nodes] == [n.task for n in four.nodes]

    def test_a_four_d_window_over_a_static_b_is_refused_permanently(self, keys):
        """The one cell of the window by covariance table that cannot exist.

        Unlike the phase gate in `validate`, which says 4D-Ens-Var is not built
        yet, this one is not waiting on anything. `4d` *is* 4D-Ens-Var, whose
        four dimensions are the ensemble's; a static B has no time dimension to
        offer and carrying one across the window needs a linear model, which
        SOCA does not have for the ocean.

        Refused at graph build rather than at the first analysis, because by
        then the forecast has run at 1.5x the model cost to write a trajectory
        nothing can read.
        """
        config = load("var_om1deg", keys)
        config["solver"]["window"] = {"type": "4d"}
        config["forecast"] = {"slots": "PT6H"}
        with pytest.raises(GraphError, match="4D-Ens-Var"):
            build_graph(config)

        # And the same window is fine the moment there is an ensemble to be
        # four-dimensional with, which is what says the refusal is about the
        # covariance and not about the window.
        ensemble = load("envar_om1deg", keys)
        ensemble["solver"]["window"] = {"type": "4d"}
        ensemble["forecast"] = {"slots": "PT6H"}
        build_graph(ensemble)

    def test_fgat_takes_any_covariance(self, keys):
        """FGAT is one increment solved at the window's centre, so a static B is
        exactly as applicable as it is in 3D-Var. Only `4d` is restricted."""
        for name in ("var_om1deg", "envar_om1deg", "hybrid_om1deg"):
            config = load(name, keys)
            config["solver"]["window"] = {"type": "fgat"}
            config["forecast"] = {"slots": "PT6H"}
            build_graph(config)

    def test_the_last_cycle_does_not_submit_another(self, keys):
        graph = graph_for("var_om1deg", keys)
        last = max(graph.cycles)
        assert not graph.has(last, "submit")
        assert graph.has(last - 1, "submit")

    def test_the_first_cycle_has_nothing_to_clean(self, keys):
        graph = graph_for("var_om1deg", keys)
        assert not graph.has(1, "cleanup")
        assert graph.has(2, "cleanup")


class TestMemberSets:
    def test_a_run_with_no_ensemble_still_has_a_control(self, keys):
        # mem000 is not a special case; it is member 0 of a set of one.
        assert member_set(load("var_om1deg", keys)) == (0,)

    def test_an_ensemble_includes_the_control_by_default(self, keys):
        assert member_set(load("letkf_om1deg", keys)) == tuple(range(21))

    def test_the_control_can_be_turned_off(self, keys):
        config = load("letkf_om1deg", keys)
        config["ensemble"]["control"] = False
        assert member_set(config) == tuple(range(1, 21))

    def test_a_one_member_task_is_still_an_array(self, keys):
        # Emitting it as a scalar job would silently invalidate every aftercorr
        # edge into it.
        graph = graph_for("free_om1deg", keys)
        assert graph.get(1, "forecast").is_array

    def test_the_long_forecast_is_a_declared_subset(self, keys):
        graph = graph_for("hybrid_om1deg", keys)
        assert graph.get(1, "forecast.ext").members == (0,)
        assert graph.get(1, "forecast").members == tuple(range(5))

    def test_the_edge_into_a_subset_array_is_not_elementwise(self, keys):
        # aftercorr requires matching index ranges, so a member subset cannot
        # use it against the full member array.
        graph = graph_for("hybrid_om1deg", keys)
        edge, = [e for e in graph.edges if e.child == "1.forecast.ext"]
        assert edge.kind == "afterok"


class TestCadence:
    def test_a_long_forecast_runs_on_its_own_cadence(self, keys):
        # Every 2 days off a 24 hour cycle, so cycles 1 and 3 of 4.
        graph = graph_for("hybrid_om1deg", keys)
        assert [c for c in graph.cycles if graph.has(c, "forecast.ext")] == [1, 3]

    def test_a_cadence_that_is_not_a_whole_number_of_cycles_is_refused(self, keys):
        config = load("hybrid_om1deg", keys)
        config["forecast"]["extended"]["every"] = "PT18H"
        with pytest.raises(GraphError, match="whole number of cycles"):
            build_graph(config)

    def test_extending_a_member_that_does_not_exist_is_refused(self, keys):
        config = load("hybrid_om1deg", keys)
        config["forecast"]["extended"]["members"] = [0, 99]
        with pytest.raises(GraphError, match="do not exist"):
            build_graph(config)


class TestTheLongForecastsTwoCadences:
    """`interval` keeps states, `slots` writes them for the departures.

    Two cadences because the two products want different ones: a compressed
    state per lead is the expensive kept product and daily is usually enough,
    while a departure wants the trajectory fine enough that an observation is
    compared against a state near its own time.
    """

    def extended(self, keys, **overrides):
        config = load("hybrid_om1deg", keys)
        config["forecast"]["extended"].update(overrides)
        return config

    def test_the_kept_leads_run_to_the_length_of_the_forecast(self, keys):
        config = self.extended(keys, length="P5D", interval="P1D")
        assert extended_leads(config) == tuple(timedelta(days=n) for n in range(1, 6))

    def test_without_an_interval_there_is_one_state_at_the_end(self, keys):
        # A legitimate experiment, just not a skill curve.
        config = self.extended(keys, length="P5D")
        config["forecast"]["extended"].pop("interval", None)
        assert extended_leads(config) == (timedelta(days=5),)

    def test_the_slots_are_finer_than_the_kept_leads(self, keys):
        config = self.extended(keys, length="P1D", interval="P1D", slots="PT6H")
        assert extended_slots(config) == tuple(
            timedelta(hours=h) for h in (6, 12, 18, 24))
        assert extended_leads(config) == (timedelta(days=1),)

    def test_the_slots_fall_back_to_the_kept_leads(self, keys):
        # So that a forecast with no sub-window cadence evaluates its
        # observations against the states it was keeping anyway.
        config = self.extended(keys, length="P2D", interval="P1D")
        config["forecast"]["extended"].pop("slots", None)
        assert extended_slots(config) == extended_leads(config)

    def test_a_kept_lead_the_model_never_writes_is_refused(self, keys):
        # The kept states have to be a subset of the written ones, or keeping
        # one would name a state the trajectory does not contain.
        config = self.extended(keys, length="P2D", interval="P1D", slots="PT16H")
        with pytest.raises(GraphError, match="not one the model writes"):
            build_graph(config)

    def test_an_interval_that_misses_the_analysis_times_is_refused(self, keys):
        """A kept lead has to land on a cycle time.

        That is what makes the verification comparable: the lead is scored
        against the same observations the cycling background was scored against
        at that time. A lead between two analysis times is scored against no
        cycle's observations at all.
        """
        config = self.extended(keys, length="P3D", interval="PT36H")
        with pytest.raises(GraphError, match="whole number of cycles"):
            build_graph(config)

    def test_an_interval_that_does_not_divide_the_length_is_refused(self, keys):
        config = self.extended(keys, length="P5D", interval="P2D")
        with pytest.raises(GraphError, match="does not divide"):
            build_graph(config)


class TestEverythingIsWholeHours:
    """An assumption, written down so the places relying on it can.

    `paths.lead_name` spells a lead as `F###` in hours and has no spelling for
    ninety minutes, and cycle directories carry minutes and seconds that are
    always zero. Both would be wrong quietly for a sub-hourly experiment: two
    leads colliding in one name, the second overwriting the first.
    """

    @pytest.mark.parametrize("where,value", [
        (("cycle", "length"), "PT90M"),
        (("forecast", "slots"), "PT90M"),
    ])
    def test_a_sub_hourly_duration_is_refused(self, keys, where, value):
        config = load("hybrid_om1deg", keys)
        config[where[0]][where[1]] = value
        with pytest.raises(GraphError, match="whole number of hours"):
            build_graph(config)

    def test_a_sub_hourly_extended_cadence_is_refused(self, keys):
        config = load("hybrid_om1deg", keys)
        config["forecast"]["extended"]["slots"] = "PT30M"
        with pytest.raises(GraphError, match="whole number of hours"):
            build_graph(config)

    def test_the_fixtures_are_all_whole_hours(self, named_graph):
        # The assumption is only free if nothing already violates it.
        _, graph = named_graph
        assert graph.nodes

    def test_a_slot_cadence_that_does_not_divide_the_window_is_refused(self, keys):
        """The last state would land short of the end of the window.

        The analysis then reads a trajectory that stops before its own last
        observation, and every observation after that point is compared against
        a background from the wrong hour. The model runs, the minimizer
        converges, and nothing anywhere says so.
        """
        config = load("hybrid_om1deg", keys)
        config["forecast"]["slots"] = "PT5H"
        with pytest.raises(GraphError, match="does not divide the window"):
            build_graph(config)

    def test_a_cadence_that_divides_it_builds(self, keys):
        config = load("hybrid_om1deg", keys)
        config["forecast"]["slots"] = "PT6H"
        # No new task and no new edge: what is four-dimensional is the
        # comparison, and the states are outputs of a forecast that already
        # exists. Compared as `to_dict`, which carries the edges: `Node` holds
        # nothing about the window or the cadence, so comparing `.nodes` alone
        # is an equality that holds for every possible value and establishes
        # only that `build_graph` did not raise.
        assert build_graph(config).to_dict() \
            == build_graph(load("hybrid_om1deg", keys)).to_dict()

    def test_a_forecast_the_coupled_clock_cannot_end_on_is_refused(self, keys):
        """`coupler_main` runs a whole number of coupled steps and nothing else.

        A two hour window on a domain coupling every two hours overshoots by an
        hour, so the run is seven hours and the model refuses it outright. That
        is a FATAL at job start on every cycle of the experiment, and nothing
        before this said so.
        """
        config = load("hybrid_om1deg", keys)
        assert config["model"]["coupling_seconds"] == 7200
        config["cycle"]["length"] = "PT6H"
        config["solver"]["window"] = {"type": "4d", "length": "PT2H"}
        config["forecast"] = {"slots": "PT1H"}
        with pytest.raises(GraphError, match="does not divide the forecast"):
            build_graph(config)

    def test_a_cadence_finer_than_the_coupling_step_is_refused(self, keys):
        """The quieter half: these states are never written at all.

        Intervals are tested at the bottom of the coupled loop and stamped with
        the step actually reached, so an hourly cadence on a two-hourly coupling
        writes on the even hours and the odd ones simply do not exist. Caught
        without this only by `_claim_slot`, after the whole run is paid for, and
        blamed there on `restart_interval`.
        """
        config = load("hybrid_om1deg", keys)
        config["forecast"] = {"slots": "PT1H"}
        with pytest.raises(GraphError, match="does not divide forecast.slots"):
            build_graph(config)

    def test_asking_persistence_for_sub_window_states_is_refused(self, keys):
        """It hands one set forward and writes nothing in between.

        Ignored rather than refused, the forecast exits 0 having written none of
        the states it declared, and the skip rule wants the sentinel *and* the
        outputs, so the task reruns forever instead of failing. Bringing the DA
        loop up cheaply on persistence is exactly what that layer is for.
        """
        config = load("stub_letkf", keys)
        config["model"] = {"name": "persistence"}
        config["forecast"] = {"slots": "PT6H"}
        with pytest.raises(GraphError, match="does not integrate"):
            build_graph(config)

    def test_a_model_with_no_coupled_clock_is_left_alone(self, keys):
        # The stub and persistence layers declare no coupling step, and a
        # relation about a clock they do not have would refuse them for nothing.
        config = load("stub_letkf", keys)
        assert "coupling_seconds" not in config["model"]
        build_graph(config)

    def test_a_cadence_that_misses_the_handoff_is_refused(self, keys):
        """The set the next cycle starts from is one of the forecast's intervals.

        A 12 hour window at a 4 hour cadence on a 10 hour cycle: the forecast
        runs 16 hours, the window tiles from F04 to F16, and every state the
        analysis reads exists. What does not is F10, where the next cycle
        begins, because a window with an odd number of sub-windows puts its own
        centre between two of them. Without this the cycle would hand forward
        whatever happened to be in `RESTART/`.
        """
        config = load("hybrid_om1deg", keys)
        config["cycle"]["length"] = "PT10H"
        config["solver"]["window"] = {"type": "4d", "length": "PT12H"}
        config["forecast"] = {"slots": "PT4H"}
        with pytest.raises(GraphError, match="does not divide the cycle length"):
            build_graph(config)

    def test_a_cadence_that_misses_the_start_of_the_window_is_refused(self, keys):
        """Every state has to land on a multiple of the cadence from hour zero.

        A 12 hour window on a 24 hour cycle starts at F18, and a cadence of 4
        hours divides both the window and the cycle while never reaching it.
        """
        config = load("hybrid_om1deg", keys)
        config["solver"]["window"] = {"type": "fgat", "length": "PT12H"}
        config["forecast"] = {"slots": "PT4H"}
        with pytest.raises(GraphError, match="lead-in"):
            build_graph(config)

    def test_a_four_dimensional_window_with_no_states_to_read_is_refused(self, keys):
        config = load("hybrid_om1deg", keys)
        config["solver"]["window"] = {"type": "fgat"}
        config.pop("forecast", None)
        with pytest.raises(GraphError, match="forecast.slots is not set"):
            build_graph(config)

    def test_a_window_the_forecast_cannot_cover_is_refused(self, keys):
        """A window of two cycles reaches back to the forecast's own first step.

        Which is the one time nothing is written at: the state there is the set
        the forecast was handed. It is also what keeps every state one analysis
        reads inside one forecast, and therefore what lets `cleanup` reap on the
        cycle before last.
        """
        config = load("hybrid_om1deg", keys)
        config["solver"]["window"] = {"type": "4d", "length": "PT48H"}
        config["forecast"] = {"slots": "PT6H"}
        with pytest.raises(GraphError, match="begins at or before the start"):
            build_graph(config)


class TestCrossCycle:
    def test_the_only_cross_cycle_edges_come_out_of_the_forecast(self, named_graph):
        _, graph = named_graph
        for edge in graph.edges:
            up, down = int(edge.parent.split(".")[0]), int(edge.child.split(".")[0])
            if up != down:
                assert edge.parent.endswith(".forecast"), edge
                assert down == up + 1, edge

    def test_with_no_analysis_the_handoff_is_forecast_to_forecast(self, keys):
        graph = graph_for("free_om1deg", keys)
        assert any(
            e.parent == "1.forecast" and e.child == "2.forecast" for e in graph.edges
        )

    def test_with_an_analysis_the_handoff_runs_through_it(self, keys):
        graph = graph_for("var_om1deg", keys)
        assert any(e.parent == "1.forecast" and e.child == "2.da" for e in graph.edges)
        assert not any(
            e.parent == "1.forecast" and e.child == "2.forecast" for e in graph.edges
        )

    def test_vertical_b_calibrates_from_the_previous_background(self, keys):
        # The one genuine exception to precomputing B offline: vertical scales
        # track the mixed layer, so they depend on the background.
        graph = graph_for("var_om1deg", keys)
        assert any(e.parent == "1.forecast" and e.child == "2.b.vt" for e in graph.edges)


class TestCycleDetection:
    def test_a_cycle_is_reported_rather_than_left_to_slurm(self, keys):
        # Slurm's own detection stops at max_depend_depth (default 10) and
        # reports a longer cycle as a job that simply never runs.
        graph = graph_for("var_om1deg", keys)
        graph.link("2.forecast", "1.b.vt", "afterok")
        with pytest.raises(GraphError, match="cycle"):
            graph.order()


class TestScale:
    def test_fifty_cycles_of_twenty_members_generates_in_seconds(self, keys):
        config = load("big_hybrid", keys)
        start = time.monotonic()
        graph = build_graph(config)
        elapsed = time.monotonic() - start

        assert len(graph.cycles) == 50
        assert sum(n.jobs for n in graph.nodes) > 3000
        # Wide on purpose. What this guards is an accidental quadratic, which
        # would take minutes rather than seconds; a bound tight enough to also
        # catch a constant-factor regression is a bound that flakes on a box
        # running a tier 3 sweep beside it, and a flaky assertion in the
        # every-commit suite teaches people to rerun rather than read.
        assert elapsed < 30.0, f"took {elapsed:.1f}s"

    def test_it_is_deterministic_at_scale_too(self, keys):
        config = load("big_hybrid", keys)
        assert build_graph(config).to_dict() == build_graph(config).to_dict()

    def test_the_shape_matches_its_golden(self, keys):
        # A summary rather than the graph itself: a golden of a thousand nodes
        # is never read, and what is worth pinning at this size is the counts,
        # which is where an off-by-one in the cadence or the last cycle shows.
        graph = build_graph(load("big_hybrid", keys))
        tasks = {}
        for node in graph.nodes:
            entry = tasks.setdefault(node.task, {"nodes": 0, "jobs": 0})
            entry["nodes"] += 1
            entry["jobs"] += node.jobs
        kinds = {}
        for edge in graph.edges:
            kinds[edge.kind] = kinds.get(edge.kind, 0) + 1

        produced = {
            "cycles": len(graph.cycles),
            "nodes": len(graph.nodes),
            "jobs": sum(n.jobs for n in graph.nodes),
            "edges": kinds,
            "tasks": dict(sorted(tasks.items())),
        }
        path = GOLDENS / "big_hybrid.summary.json"
        if os.environ.get("ACKBAR_UPDATE_GOLDENS"):
            path.write_text(json.dumps(produced, indent=2) + "\n")
        assert produced == json.loads(path.read_text())


class TestRendering:
    def test_text_names_every_task_in_the_cycle(self, keys):
        graph = graph_for("hybrid_om1deg", keys)
        text = to_text(graph, cycles={2})
        assert "cycle 2" in text and "cycle 1" not in text
        for task in ("da", "writeback", "forecast", "stats"):
            assert task in text

    def test_dot_is_produced_for_the_selected_cycles_only(self, keys):
        dot = to_dot(graph_for("hybrid_om1deg", keys), cycles={1})
        assert dot.startswith("digraph ackbar {") and dot.endswith("}")
        assert '"1.da"' in dot and '"2.da"' not in dot
