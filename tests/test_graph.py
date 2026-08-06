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
from pathlib import Path

import pytest

from ackbar.config.layers import merge_layers, resolve_layers
from ackbar.config.resolve import resolve
from ackbar.config.schema import load_schema, merge_keys
from ackbar.graph import GraphError, build_graph, member_set, to_dot, to_text

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
            if node.members and node.task != "forecast.ext":
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
        for task in ("post.obs", "post.state", "verify", "stats", "forecast.ext"):
            for node in graph.nodes:
                if node.task == task:
                    assert graph.children(node.id) == [], node.id

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

    def test_four_d_changes_the_configuration_and_not_yet_the_graph(self, keys):
        # Pinned so that phase 9's sub-window forecast slots show up as a
        # golden diff rather than as a surprise.
        three, four = graph_for("var_om1deg", keys), graph_for("fourd_om1deg", keys)
        assert [n.task for n in three.nodes] == [n.task for n in four.nodes]

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
        assert elapsed < 5.0, f"took {elapsed:.1f}s"

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
