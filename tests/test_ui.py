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
        return dict(live)

    def accounting(job_ids):
        return {job: {"state": s} for job, s in done.items()}

    def accounting_elements(job_ids):
        return {(job, None): {"state": s} for job, s in done.items()}

    monkeypatch.setattr(st, "_queue_elements", queue_elements)
    monkeypatch.setattr(st.slurm, "accounting", accounting)
    monkeypatch.setattr(st, "_accounting_elements", accounting_elements)
    return live, done


def submitted(site, tasks, job=100):
    """Put ledger records on disk for *tasks*, as `[(cycle, task)]`."""
    paths = Paths.of(CONFIG, site)
    for index, (cycle, task) in enumerate(tasks):
        ledger.append(paths, cycle=cycle, task=task, members=(),
                      attempt=1, job_id=job + index, dependency="")
    return paths


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
