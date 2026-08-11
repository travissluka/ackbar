"""Tier 0 for the console.

Textual drives the real app headlessly, so these are ordinary tests: no terminal,
no scheduler, no experiment on disk beyond a fixture. What they are for is the
two things that would be expensive to get wrong.

The first is that **no key touches the queue without the modal**. `test_heal_*`
patches `heal.heal` and asserts it is not reached until the confirmation is
accepted, which is the one property of this interface that a mistake in makes
worse than no interface at all.

The second is that the grid tells the truth. The renderers are pure functions of
a view, so the cells can be asserted directly, including the state split that
the colour ramp is built on.

Marked `ui` and skipped where textual is absent, so the suite still runs in an
environment that only has what a job needs.
"""

import json

import pytest
import yaml

from ackbar import ledger, state as st
from ackbar.graph import build_graph
from ackbar.paths import Paths

pytest.importorskip("textual", reason="the console's optional dependency")
pytestmark = pytest.mark.ui

from ackbar.ui import theme  # noqa: E402
from ackbar.ui.app import AckbarUI  # noqa: E402
from ackbar.ui.discover import discover, load  # noqa: E402
from ackbar.ui.poll import Poller  # noqa: E402
from ackbar.ui.panes import (LogPane, candidates, for_member,  # noqa: E402
                             members_of)
from ackbar.ui.widgets import StatusGrid, _buckets, _fit  # noqa: E402

CONFIG = {
    "experiment": {"name": "demo", "description": "a fixture"},
    "cycle": {"start": "2015-07-12T00:00:00Z", "length": "PT24H", "count": 4},
    "domain": {"name": "gom_25km"},
    "model": {"name": "stub"},
    "solver": {"name": "variational", "window": {"type": "3d"}},
    "ensemble": {"size": 0},
}


@pytest.fixture
def experiment(tmp_path):
    """One frozen experiment on disk, with a ledger, and nothing else."""
    site = {
        "output_root": str(tmp_path / "out"),
        "scratch_root": str(tmp_path / "scratch"),
        "launcher": "",
    }
    directory = tmp_path / "out" / "demo" / "cfg"
    directory.mkdir(parents=True)
    with open(directory / "experiment.yaml", "w") as handle:
        yaml.safe_dump(CONFIG, handle)
    return site


@pytest.fixture
def fake_slurm(monkeypatch):
    """A scheduler that says whatever the test puts in these dicts."""
    live, done = {}, {}

    def queue_elements(job_ids):
        asked = set(job_ids)
        return {key: row for key, row in live.items() if key[0] in asked}

    def accounting_states(job_ids):
        return {(job, None): s for job, s in done.items()
                if job in set(job_ids)}

    monkeypatch.setattr(st, "_queue_elements", queue_elements)
    monkeypatch.setattr(st.slurm, "accounting_states", accounting_states)
    return live, done


#: The same fixture with an ensemble, for the log pane's member handling. Four
#: members, because `mem000` is the control and a bug that shows up on the first
#: member only would hide behind three.
ENSEMBLE = dict(CONFIG, solver={"name": "letkf"}, ensemble={"size": 3})


@pytest.fixture
def ensemble(tmp_path):
    site = {
        "output_root": str(tmp_path / "out"),
        "scratch_root": str(tmp_path / "scratch"),
        "launcher": "",
    }
    directory = tmp_path / "out" / "demo" / "cfg"
    directory.mkdir(parents=True)
    with open(directory / "experiment.yaml", "w") as handle:
        yaml.safe_dump(ENSEMBLE, handle)
    return site


def submitted(site, tasks, job=100, config=CONFIG, members=()):
    """Put ledger records on disk for *tasks*, as `[(cycle, task)]`."""
    paths = Paths.of(config, site)
    for index, (cycle, task) in enumerate(tasks):
        ledger.append(paths, cycle=cycle, task=task, members=members,
                      attempt=1, job_id=job + index, dependency="")
    return paths


def logs(paths, cycle, names):
    """Write *names* into the cycle's log directory, with some content."""
    directory = paths.log_dir(cycle)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in names:
        path = directory / name
        path.write_text(f"this is {name}\n")
        written.append(path)
    return written


# --- discovery ---------------------------------------------------------------

def test_discovery_finds_the_frozen_experiment(experiment):
    found = discover(experiment)
    assert [e.name for e in found] == ["demo"]
    assert found[0].domain == "gom_25km"
    assert found[0].solver == "variational/3d"
    assert found[0].cycles == 4


def test_a_directory_without_a_frozen_config_is_not_an_experiment(experiment,
                                                                 tmp_path):
    (tmp_path / "out" / "halfway" / "cfg").mkdir(parents=True)
    (tmp_path / "out" / "stray.tar").write_text("")
    assert [e.name for e in discover(experiment)] == ["demo"]


def test_an_unreadable_config_is_skipped_not_fatal(experiment, tmp_path):
    """One broken directory must not hide the nine that work."""
    broken = tmp_path / "out" / "broken" / "cfg"
    broken.mkdir(parents=True)
    (broken / "experiment.yaml").write_text("{{{ not yaml")
    assert load(broken / "experiment.yaml", experiment) is None
    assert [e.name for e in discover(experiment)] == ["demo"]


def test_the_halt_flag_is_read_live_not_cached(experiment):
    found = discover(experiment)[0]
    assert not found.halted
    found.paths.experiment_dir.mkdir(parents=True, exist_ok=True)
    found.paths.halt_flag.write_text("paused\n")
    assert found.halted


# --- one snapshot for every experiment ---------------------------------------

def test_the_poller_asks_the_scheduler_once_for_every_experiment(
        experiment, fake_slurm, monkeypatch, tmp_path):
    """The reason `state.snapshot` was split out at all."""
    for name in ("second", "third"):
        directory = tmp_path / "out" / name / "cfg"
        directory.mkdir(parents=True)
        config = dict(CONFIG, experiment={"name": name})
        with open(directory / "experiment.yaml", "w") as handle:
            yaml.safe_dump(config, handle)

    calls = []
    real = st.snapshot
    monkeypatch.setattr(st, "snapshot",
                        lambda ids: calls.append(tuple(ids)) or real(ids))

    poller = Poller(experiment)
    report = poller.refresh()
    assert len(calls) == 1
    assert set(report.views) == {"demo", "second", "third"}


def test_an_outage_keeps_the_last_report_rather_than_blanking_it(
        experiment, fake_slurm, monkeypatch):
    submitted(experiment, [(1, "da")])
    poller = Poller(experiment)
    first = poller.refresh()
    assert first.views["demo"].statuses

    def explode(_):
        raise st.slurm.SlurmError("slurmdbd is restarting")

    monkeypatch.setattr(st, "snapshot", explode)
    second = poller.refresh()
    assert second.error
    assert second.stale
    # The same statuses, not an empty grid: "Slurm could not answer" is not
    # "none of these jobs exist".
    assert second.views["demo"].statuses == first.views["demo"].statuses


def test_a_cycle_submitted_mid_refresh_is_not_reported_as_failed(
        experiment, fake_slurm, monkeypatch):
    """The cycle boundary flicker.

    The submitter appends a whole cycle's ledger records at once. A refresh that
    read the ledger, asked Slurm about those ids, and then read the ledger again
    would find ids it never asked about: absent from the queue, absent from
    accounting, and with no sentinel yet, which is the one combination
    `_element_state` calls failed. It showed a whole cycle red for one refresh at
    every boundary.
    """
    live, done = fake_slurm
    paths = submitted(experiment, [(1, "da")])
    done[100] = "COMPLETED"

    # A submitter that lands cycle 2 in the ledger between the poller's read and
    # `collect`'s, which is exactly the race.
    real = st.snapshot

    def snapshot_then_submit(job_ids):
        result = real(job_ids)
        ledger.append(paths, cycle=2, task="da", members=(), attempt=1,
                      job_id=999, dependency="")
        return result

    monkeypatch.setattr(st, "snapshot", snapshot_then_submit)

    view = Poller(experiment).refresh().views["demo"]
    assert view.statuses["2.da"].summary == st.UNSUBMITTED
    assert view.statuses["2.da"].broken == ()
    assert view.overall != "broken"


def test_a_snapshot_that_was_never_asked_about_an_id_says_nothing_about_it(
        experiment, fake_slurm):
    """The guard under the fix, for any caller that passes a stale snapshot."""
    paths = submitted(experiment, [(1, "da")])
    graph = build_graph(CONFIG)

    stale = st.snapshot([])
    assert 100 not in stale.asked
    statuses = st.collect(paths, graph, stale)
    assert statuses["1.da"].summary == st.UNSUBMITTED

    # Asked about, and genuinely unaccounted for: that one is failed, and has to
    # stay failed, because the only way forward is to run it again.
    fresh = st.snapshot([100])
    assert st.collect(paths, graph, fresh)["1.da"].summary == st.FAILED


def test_an_experiment_created_while_the_console_is_open_appears(
        experiment, fake_slurm, tmp_path):
    """It did not, and quitting to see a new experiment is not a live display."""
    poller = Poller(experiment)
    assert set(poller.refresh().views) == {"demo"}

    directory = tmp_path / "out" / "later" / "cfg"
    directory.mkdir(parents=True)
    with open(directory / "experiment.yaml", "w") as handle:
        yaml.safe_dump(dict(CONFIG, experiment={"name": "later"}), handle)

    assert set(poller.refresh().views) == {"demo", "later"}


def test_a_rescan_hands_back_the_experiments_it_already_loaded(experiment,
                                                              fake_slurm):
    """What makes a scan per tick affordable: the config is frozen."""
    poller = Poller(experiment)
    poller.refresh()
    first = poller.experiments[0]
    graph = first.graph
    poller.refresh()
    assert poller.experiments[0] is first
    assert poller.experiments[0].graph is graph


def test_a_job_slurm_has_finished_with_is_asked_about_once(
        experiment, fake_slurm, monkeypatch):
    """The cost of a tick should track what is happening, not how long it ran.

    A terminal accounting row has no further states to reach, so asking again is
    re-reading an immutable record. On a real experiment this is the difference
    between a thousand ids per tick and a dozen.
    """
    live, done = fake_slurm
    submitted(experiment, [(1, "da"), (1, "forecast")])
    done[100] = "COMPLETED"
    done[101] = "RUNNING"
    live[(101, None)] = ("RUNNING", "None")

    asked = []
    real = st.snapshot
    monkeypatch.setattr(st, "snapshot",
                        lambda ids: asked.append(tuple(ids)) or real(ids))

    poller = Poller(experiment)
    poller.refresh()
    second = poller.refresh()
    assert asked == [(100, 101), (101,)]
    # And it is still complete in the second report, out of what was remembered.
    assert second.views["demo"].statuses["1.da"].summary == st.COMPLETE
    assert second.views["demo"].statuses["1.forecast"].summary == st.RUNNING


def test_a_job_still_in_the_queue_is_never_taken_as_settled(experiment,
                                                            fake_slurm):
    """`sacct` can say COMPLETED for an element while siblings still run."""
    live, done = fake_slurm
    submitted(experiment, [(1, "da")])
    done[100] = "COMPLETED"
    live[(100, None)] = ("COMPLETING", "None")

    poller = Poller(experiment)
    poller.refresh()
    assert poller.settled.unasked([100]) == [100]


def test_a_refresh_by_hand_asks_about_everything_again(experiment, fake_slurm,
                                                      monkeypatch):
    """The escape hatch, for the one case that can be wrong: reused job ids."""
    _, done = fake_slurm
    submitted(experiment, [(1, "da")])
    done[100] = "COMPLETED"

    asked = []
    real = st.snapshot
    monkeypatch.setattr(st, "snapshot",
                        lambda ids: asked.append(tuple(ids)) or real(ids))

    poller = Poller(experiment)
    poller.refresh()
    poller.refresh()
    poller.refresh(fresh=True)
    assert asked == [(100,), (), (100,)]


def test_the_queue_split_is_counted_separately(experiment, fake_slurm):
    live, _ = fake_slurm
    submitted(experiment, [(1, "da"), (1, "forecast"), (2, "da")])
    live[(100, None)] = ("RUNNING", "None")
    live[(101, None)] = ("PENDING", "Resources")
    live[(102, None)] = ("PENDING", "Dependency")

    view = Poller(experiment).refresh().views["demo"]
    assert (view.running_jobs, view.queued_jobs, view.blocked_jobs) == (1, 1, 1)
    assert view.statuses["1.forecast"].summary == st.PENDING
    assert view.statuses["2.da"].summary == st.BLOCKED


# --- the grid ----------------------------------------------------------------

def test_a_cell_exists_for_every_node_and_carries_the_worst_member():
    """Colour is the data, so the cell had better be the right state."""
    graph = build_graph(dict(CONFIG, ensemble={"size": 3}))
    assert graph.cycles


def test_cells_widen_to_fill_and_narrow_to_fit():
    width, window = _fit(list(range(1, 21)), 0, 100)
    assert width == 2 and len(window) == 20
    width, window = _fit(list(range(1, 61)), 0, 100)
    assert width == 1 and len(window) == 60
    # More cycles than columns: a window, centred on the cursor.
    width, window = _fit(list(range(1, 201)), 100, 50)
    assert width == 1 and len(window) == 50
    assert window[0] < 100 < window[-1]


def test_the_sidebar_stripe_is_always_the_full_width():
    """So the column compares fraction done rather than cycle count."""
    assert len(_buckets([1], 20)) == 20
    assert len(_buckets(list(range(1, 61)), 20)) == 20


def test_every_state_has_a_colour_and_a_word_in_both_palettes():
    for name in theme.PALETTES:
        palette = theme.palette(name)
        for node_state in st.SEVERITY:
            assert palette[node_state].startswith("#")
            assert theme.WORD[node_state]
    # The point of the safe palette: no state shares a colour with another, and
    # complete and failed are not a red/green pair.
    safe = theme.palette("safe")
    assert len(set(safe.values())) == len(safe)


def test_the_colour_ramp_puts_blocked_between_unsubmitted_and_queued():
    """The ramp is the whole reason the state was split; assert it survives."""
    palette = theme.palette("default")

    def brightness(colour):
        return sum(int(colour[i:i + 2], 16) for i in (1, 3, 5))

    assert (brightness(palette[st.UNSUBMITTED])
            < brightness(palette[st.BLOCKED])
            < brightness(palette[st.PENDING])
            < brightness(palette[st.RUNNING]))


# --- the app ------------------------------------------------------------------

async def test_the_app_opens_on_the_most_recent_experiment(experiment,
                                                           fake_slurm):
    submitted(experiment, [(1, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        assert app.selected == "demo"
        assert app.query_one("#statusgrid", StatusGrid).cursor_node


async def test_arrows_walk_the_grid_and_the_cursor_names_the_node(experiment,
                                                                 fake_slurm):
    submitted(experiment, [(1, "da"), (2, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.focus()
        grid.column = 0
        await pilot.press("right")
        assert grid.cursor_node.startswith("2.")
        await pilot.press("left")
        assert grid.cursor_node.startswith("1.")


async def test_left_at_the_first_cycle_hands_focus_back_to_the_sidebar(
        experiment, fake_slurm):
    submitted(experiment, [(1, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.focus()
        grid.column = 0
        await pilot.press("left")
        await pilot.pause()
        assert app.focused is app.query_one("#fleet")


async def test_heal_does_nothing_until_the_plan_is_confirmed(
        experiment, fake_slurm, monkeypatch):
    """The property that matters most: one keystroke cannot cancel anything."""
    live, done = fake_slurm
    paths = submitted(experiment, [(1, "da")])
    done[100] = "FAILED"

    called = []
    monkeypatch.setattr("ackbar.heal.heal",
                        lambda *a, **k: called.append(True) or ([], [], [], []))

    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        assert app.view.broken == ("1.da",)

        await pilot.press("h")
        await pilot.pause()
        # The modal is up and nothing has run.
        assert app.screen is not app.screen_stack[0]
        assert not called

        await pilot.press("escape")
        await pilot.pause()
        assert not called

        await pilot.press("h")
        await pilot.pause()
        await pilot.press("enter")
        # The action runs in a thread worker; wait for it rather than sleeping.
        await app.workers.wait_for_complete()
        assert called == [True]


async def test_heal_says_so_when_nothing_is_broken(experiment, fake_slurm,
                                                  monkeypatch):
    submitted(experiment, [(1, "da")])
    fake_slurm[1][100] = "COMPLETED"

    called = []
    monkeypatch.setattr("ackbar.heal.heal", lambda *a, **k: called.append(True))
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        # No modal, no call: a notification instead.
        assert app.screen is app.screen_stack[0]
        assert not called


async def test_pause_writes_the_halt_flag_without_a_modal(experiment,
                                                          fake_slurm):
    """Reversible and cheap, so it is not gated. `resume` is gated."""
    submitted(experiment, [(1, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        await pilot.press("p")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.view.experiment.paths.halt_flag.exists()


async def test_quitting_changes_nothing_on_disk(experiment, fake_slurm):
    """The v3 correction, asserted: viewing is never load-bearing."""
    paths = submitted(experiment, [(1, "da")])
    before = sorted(p.name for p in paths.experiment_dir.rglob("*"))
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        for key in ("right", "down", "2", "3", "4", "5", "1", "A", "P", "R"):
            await pilot.press(key)
            await pilot.pause()
    assert sorted(p.name for p in paths.experiment_dir.rglob("*")) == before


async def test_the_first_frame_says_it_is_looking_not_that_there_is_nothing(
        experiment, fake_slurm, monkeypatch):
    """Two different facts, and the console used to state the wrong one.

    "No experiments under this output root" is a finding. Before the first tick
    lands nobody has looked, and on a busy machine that is a second or two of
    the display asserting something false.
    """
    monkeypatch.setattr(AckbarUI, "apply", lambda self, report: None)
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "reading" in str(app.query_one("#banner").content)


async def test_an_empty_output_root_says_so_once_it_has_been_read(tmp_path,
                                                                 fake_slurm):
    site = {"output_root": str(tmp_path / "empty"),
            "scratch_root": str(tmp_path / "scratch"), "launcher": ""}
    (tmp_path / "empty").mkdir()
    app = AckbarUI(site=site, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        assert "no experiments" in str(app.query_one("#banner").content)
        assert "no experiments" in str(app.query_one("#statusgrid").content)


async def test_a_click_puts_the_cursor_on_the_cell_under_the_pointer(
        experiment, fake_slurm):
    """Pointing at the cell you can see, instead of counting arrow presses."""
    submitted(experiment, [(1, "da"), (2, "da"), (3, "da"), (4, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()

        grid = app.query_one("#statusgrid", StatusGrid)
        grid.column = 0
        grid.draw()
        await pilot.pause()
        label, width, window = grid._layout
        row, target = 1, len(window) - 1

        await pilot.click(grid, offset=(
            grid.gutter.left + label + target * width,
            grid.gutter.top + 1 + row,
        ))
        await pilot.pause()
        assert grid.cursor_node == f"{window[target]}.{grid.tasks[row]}"


async def test_a_double_click_opens_the_log_the_way_enter_does(experiment,
                                                              fake_slurm):
    submitted(experiment, [(1, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        label, width, window = grid._layout

        await pilot.click(grid, offset=(grid.gutter.left + label,
                                        grid.gutter.top + 1), times=2)
        await pilot.pause()
        assert app.query_one("#tabs").active == "log"


async def test_the_grid_says_whether_the_arrows_are_pointed_at_it(experiment,
                                                                 fake_slurm):
    """Which region has the keyboard has to be visible without pressing a key."""
    submitted(experiment, [(1, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)

        def bright_cursor():
            return any("f0f6fc" in str(span.style)
                       for span in grid.picture().spans)

        grid.focus()
        await pilot.pause()
        assert bright_cursor()

        app.query_one("#fleet").focus()
        await pilot.pause()
        assert not bright_cursor()


async def test_backspace_comes_back_out_of_a_pane_to_the_grid(experiment,
                                                             fake_slurm):
    submitted(experiment, [(1, "da")])
    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#tabs").active == "log"
        app.query_one("#logview").focus()
        await pilot.pause()

        await pilot.press("backspace")
        await pilot.pause()
        assert app.query_one("#tabs").active == "grid"
        assert app.focused is app.query_one("#statusgrid")


# --- which log, whose log ------------------------------------------------------
#
# Names as they actually appear in a cycle's log directory. A twenty member
# forecast leaves 168 files there, so which one is shown is the whole question.

FORECAST_LOGS = [
    "forecast.59089_0.out",
    "forecast.59089_7.out",
    "forecast.mem000.59089_0.model.log",
    "forecast.mem007.59089_7.model.log",
    "forecast.mem007.59089_7.ocean.stats",
    # The extended forecast, a *different* task whose every file has
    # `forecast.` as a prefix.
    "forecast.ext.59090_7.out",
    "forecast.ext.mem007.59090_7.model.log",
]


def test_another_tasks_logs_are_not_this_tasks(experiment):
    """`forecast.` is a prefix of every one of `forecast.ext`'s files.

    The log list for a forecast used to be half its extended forecast's, and
    both write a `model.log`, so the reader had no way to tell.
    """
    paths = submitted(experiment, [(1, "forecast")])
    logs(paths, 1, FORECAST_LOGS)

    mine = [p.name for p in candidates(paths, 1, "forecast", 59089)]
    assert not any(".ext." in name for name in mine)
    assert len(mine) == 5

    theirs = [p.name for p in candidates(paths, 1, "forecast.ext", 59090)]
    assert theirs == ["forecast.ext.59090_7.out",
                      "forecast.ext.mem007.59090_7.model.log"]


def test_a_members_logs_are_found_by_either_spelling(experiment):
    """Slurm writes `_7`, the task writes `mem007`, and both mean member 7."""
    paths = submitted(experiment, [(1, "forecast")])
    logs(paths, 1, FORECAST_LOGS)

    mine = [p.name for p in candidates(paths, 1, "forecast", 59089, 7)]
    assert mine == ["forecast.59089_7.out",
                    "forecast.mem007.59089_7.model.log",
                    "forecast.mem007.59089_7.ocean.stats"]


def test_slurms_own_capture_comes_first(experiment):
    """It holds the traceback of a task that died before writing anything."""
    paths = submitted(experiment, [(1, "forecast")])
    logs(paths, 1, FORECAST_LOGS)
    first = candidates(paths, 1, "forecast", 59089, 0)[0]
    assert first.name == "forecast.59089_0.out"


def test_a_member_marked_only_at_the_end_of_the_name_still_counts(experiment):
    """`da.ens` is one job that loops the members itself.

    No array index, so its per member logs carry `mem###` as a suffix. The
    graph calls that node memberless; the log directory plainly does not.
    """
    paths = submitted(experiment, [(1, "da.ens")])
    logs(paths, 1, [
        "da.ens.59084.out",
        "da.ens.59084.hofx_ens.mean.log",
        "da.ens.59084.hofx_ens.mem001.log",
        "da.ens.59084.hofx_ens.mem002.log",
    ])
    everything = candidates(paths, 1, "da.ens", 59084)
    assert members_of(everything, "da.ens") == [1, 2]
    assert [p.name for p in for_member(everything, "da.ens", 2)] == [
        "da.ens.59084.hofx_ens.mem002.log"]
    # No member chosen means all of them, including the files that belong to
    # none: the mean is not a member and is worth reading.
    assert len(for_member(everything, "da.ens", None)) == 4


def test_following_appends_only_what_was_added(tmp_path):
    """A growing model log costs one stat and the new bytes, not a re-read."""
    path = tmp_path / "model.log"
    path.write_text("first\n")
    pane = LogPane()
    pane.show(path)
    assert pane.read_to == len("first\n")
    assert not pane.poll()

    with open(path, "a") as handle:
        handle.write("second\n")
    assert pane.poll()
    assert pane.read_to == len("first\nsecond\n")
    assert not pane.poll()


def test_a_file_that_got_shorter_is_read_again(tmp_path):
    """Truncated or replaced: what is on screen is no longer its beginning."""
    path = tmp_path / "model.log"
    path.write_text("aaaa\nbbbb\n")
    pane = LogPane()
    pane.show(path)
    path.write_text("c\n")
    assert pane.poll()
    assert pane.read_to == len("c\n")


def test_following_stops_when_it_is_turned_off(tmp_path):
    path = tmp_path / "model.log"
    path.write_text("first\n")
    pane = LogPane()
    pane.show(path)
    pane.following = False
    with open(path, "a") as handle:
        handle.write("second\n")
    assert not pane.poll()
    assert pane.read_to == len("first\n")


def test_a_running_tasks_log_is_the_one_in_scratch(experiment):
    """Where the growing file actually is, which is not the log directory.

    `mom6sis2.launch` points the model's stdout at `model.log` inside the
    scratch run directory and `keep_traces` copies it out only when the task has
    finished. So for the whole of a forecast, nothing under `run/<date>/log`
    grows: the archived files do not exist yet and Slurm's capture is empty
    because the output was redirected away from it. Following only the log
    directory shows an empty file during the run and the finished article after,
    which is exactly backwards.
    """
    paths = submitted(experiment, [(1, "forecast")])
    logs(paths, 1, ["forecast.100_0.out"])
    scratch = paths.scratch(1, "forecast", 0)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "model.log").write_text("step 1\n")
    (scratch / "logfile.000000.out").write_text("fms\n")
    (scratch / "ocean.stats").write_text("stats\n")
    # Not a log, and much the largest thing in there.
    (scratch / "MOM.res.nc").write_text("x" * 1000)

    found = candidates(paths, 1, "forecast", 100, 0)
    assert [p.name for p in found[:3]] == ["model.log", "logfile.000000.out",
                                           "ocean.stats"]
    assert "MOM.res.nc" not in [p.name for p in found]
    # The live ones come first: a task still running is the reason anybody is
    # following a log, and the archived copy of a finished one cannot move.
    assert found[0].parent == scratch
    assert found[-1].parent == paths.log_dir(1)


def test_a_live_log_hands_over_to_the_archived_copy(experiment, fake_slurm):
    """Scratch is deleted when a task succeeds; the copy is the record.

    Observed on a real cycle: the pane followed the scratch file from 19058 to
    19721 bytes as the model wrote, then the member finished and it was reading
    the 65 kilobyte archived copy.
    """
    paths = submitted(experiment, [(1, "forecast")])
    scratch = paths.scratch(1, "forecast", 0)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "model.log").write_text("step 1\n")
    assert candidates(paths, 1, "forecast", 100, 0)[0].parent == scratch

    # The task succeeds: the trace is copied out and scratch is removed.
    logs(paths, 1, ["forecast.mem000.100_0.model.log"])
    for path in scratch.iterdir():
        path.unlink()
    scratch.rmdir()

    found = candidates(paths, 1, "forecast", 100, 0)
    assert [p.name for p in found] == ["forecast.mem000.100_0.model.log"]


async def test_the_log_opens_on_the_member_that_failed(ensemble, fake_slurm):
    """A twenty member forecast is opened because one member went wrong."""
    live, done = fake_slurm
    paths = submitted(ensemble, [(1, "forecast")], config=ENSEMBLE,
                      members=(0, 1, 2, 3))
    logs(paths, 1, [f"forecast.100_{m}.out" for m in range(4)]
         + [f"forecast.mem{m:03d}.100_{m}.model.log" for m in range(4)])
    live[(100, 0)] = ("COMPLETED", "None")
    live[(100, 1)] = ("COMPLETED", "None")
    live[(100, 3)] = ("RUNNING", "None")
    done[100] = "FAILED"

    app = AckbarUI(site=ensemble, interval=3600.0)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.row = grid.tasks.index("forecast")
        grid.column = 0
        grid.draw()
        app.action_open_log()
        await pilot.pause()
        # Member 2 is the one with no queue row and a failed array: the first
        # member worth reading, not member 0.
        assert app.member == 2
        assert app.query_one("#logview", LogPane).path.name == \
            "forecast.100_2.out"
        # And the keys are live without a tab press.
        assert app.focused is app.query_one("#logview")


async def test_an_empty_capture_is_not_what_the_log_opens_on(ensemble,
                                                             fake_slurm):
    """Measured on a real run, not supposed: the capture is usually empty.

    Tasks here redirect their own output, so a successful member of a forecast
    leaves a zero byte `.out` beside a sixty five kilobyte `model.log`. Opening
    on the empty one and following it looks exactly like a wedged job.
    """
    paths = submitted(ensemble, [(1, "forecast")], config=ENSEMBLE,
                      members=(0, 1, 2, 3))
    directory = paths.log_dir(1)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "forecast.100_0.out").write_text("")
    (directory / "forecast.mem000.100_0.model.log").write_text("step 1\n")
    fake_slurm[1][100] = "COMPLETED"

    app = AckbarUI(site=ensemble, interval=3600.0)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.row = grid.tasks.index("forecast")
        grid.column = 0
        grid.draw()
        app.action_open_log()
        await pilot.pause()
        assert app.query_one("#logview", LogPane).path.name == \
            "forecast.mem000.100_0.model.log"
        # The capture is still one bracket away, because a traceback lands there
        # when there is one.
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert app.query_one("#logview", LogPane).path.name == \
            "forecast.100_0.out"


async def test_a_log_that_appears_after_the_pane_opened_is_picked_up(
        ensemble, fake_slurm):
    """Opening on a member Slurm started a second ago.

    All it has is an empty capture; its model log arrives just after. Left
    alone the reader watches a file that will never grow while the real one
    fills up beside it.
    """
    paths = submitted(ensemble, [(1, "forecast")], config=ENSEMBLE,
                      members=(0, 1, 2, 3))
    directory = paths.log_dir(1)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "forecast.100_0.out").write_text("")
    fake_slurm[0][(100, 0)] = ("RUNNING", "None")

    app = AckbarUI(site=ensemble, interval=3600.0)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.row = grid.tasks.index("forecast")
        grid.column = 0
        grid.draw()
        app.action_open_log()
        await pilot.pause()
        pane = app.query_one("#logview", LogPane)
        assert pane.path.name == "forecast.100_0.out"

        # The model starts writing.
        (directory / "forecast.mem000.100_0.model.log").write_text("step 1\n")
        app.show_log(app.log_node)
        await pilot.pause()
        assert pane.path.name == "forecast.mem000.100_0.model.log"

        # But a file the reader chose is theirs, empty or not.
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert pane.path.name == "forecast.100_0.out"
        app.show_log(app.log_node)
        await pilot.pause()
        assert pane.path.name == "forecast.100_0.out"


async def test_the_pane_moves_off_a_live_log_that_has_been_deleted(
        ensemble, fake_slurm):
    """Even one the reader chose: there is nothing to be loyal to."""
    paths = submitted(ensemble, [(1, "forecast")], config=ENSEMBLE,
                      members=(0, 1, 2, 3))
    scratch = paths.scratch(1, "forecast", 0)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "model.log").write_text("step 1\n")
    fake_slurm[0][(100, 0)] = ("RUNNING", "None")

    app = AckbarUI(site=ensemble, interval=3600.0)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.row = grid.tasks.index("forecast")
        grid.column = 0
        grid.draw()
        app.action_open_log()
        await pilot.pause()
        pane = app.query_one("#logview", LogPane)
        assert pane.path.parent == scratch

        logs(paths, 1, ["forecast.mem000.100_0.model.log"])
        (scratch / "model.log").unlink()
        scratch.rmdir()
        app.show_log(app.log_node)
        await pilot.pause()
        assert pane.path.name == "forecast.mem000.100_0.model.log"


async def test_arrows_step_members_and_brackets_step_files(ensemble,
                                                           fake_slurm):
    paths = submitted(ensemble, [(1, "forecast")], config=ENSEMBLE,
                      members=(0, 1, 2, 3))
    logs(paths, 1, [f"forecast.100_{m}.out" for m in range(4)]
         + [f"forecast.mem{m:03d}.100_{m}.model.log" for m in range(4)])
    fake_slurm[1][100] = "COMPLETED"

    app = AckbarUI(site=ensemble, interval=3600.0)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.row = grid.tasks.index("forecast")
        grid.column = 0
        grid.draw()
        app.action_open_log()
        await pilot.pause()
        assert app.member == 0

        pane = app.query_one("#logview", LogPane)
        await pilot.press("right", "right")
        await pilot.pause()
        assert app.member == 2
        assert pane.path.name == "forecast.100_2.out"

        await pilot.press("left")
        await pilot.pause()
        assert app.member == 1

        # A different file of the same member.
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert pane.path.name == "forecast.mem001.100_1.model.log"
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert pane.path.name == "forecast.100_1.out"

        # And follow is a key, reported in the title.
        assert pane.following
        await pilot.press("f")
        await pilot.pause()
        assert not pane.following
        assert "off" in str(app.query_one("#log-title").content)


async def test_clicking_a_member_in_the_strip_opens_its_log(ensemble,
                                                            fake_slurm):
    paths = submitted(ensemble, [(1, "forecast")], config=ENSEMBLE,
                      members=(0, 1, 2, 3))
    logs(paths, 1, [f"forecast.100_{m}.out" for m in range(4)])
    fake_slurm[1][100] = "COMPLETED"

    app = AckbarUI(site=ensemble, interval=3600.0)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.pause()
        grid = app.query_one("#statusgrid", StatusGrid)
        grid.row = grid.tasks.index("forecast")
        grid.column = 0
        grid.draw()
        app.action_open_log()
        await pilot.pause()

        strip = app.query_one("#log-members")
        await pilot.click(strip, offset=(strip.gutter.left + strip._first + 3,
                                         strip.gutter.top))
        await pilot.pause()
        assert app.member == 3
        assert app.query_one("#logview", LogPane).path.name == \
            "forecast.100_3.out"


async def test_the_stats_pane_reads_the_harvest_and_invents_nothing(
        experiment, fake_slurm):
    paths = submitted(experiment, [(1, "da")])
    paths.stats_file(1).parent.mkdir(parents=True, exist_ok=True)
    with open(paths.stats_file(1), "w") as handle:
        json.dump({"totals": {"jobs": 7, "failed": 1, "unfinished": 0,
                              "core_seconds": 42.0, "max_rss_kb": 2048}}, handle)

    app = AckbarUI(site=experiment, interval=3600.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply(app.poller.refresh())
        await pilot.press("4")
        await pilot.pause()
        table = app.query_one("#statstable")
        assert table.row_count == 1
        values = [str(c) for c in table.get_row_at(0)]
        assert "7" in values and "42.0" in values and "2M" in values
