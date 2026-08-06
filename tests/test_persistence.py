"""Tier 0: the forecast that does not integrate.

Small, and worth testing anyway for one reason: persistence is what the DA loop
is brought up on, so a bug here looks like a bug in the analysis. The clock in
particular. A restart set carrying the wrong time is read without complaint by
everything downstream, and the symptom is an analysis that appears to be
correcting the wrong ocean.
"""

import pytest

from conftest import PATHS_CYCLE

from ackbar import persistence
from ackbar.mom6sis2 import STAMP, ModelError
from ackbar.paths import Paths

CONFIG = {
    "experiment": {"name": "e"},
    "cycle": {"start": "2015-01-05T01:00:00Z", "length": "PT12H", "count": 3},
    "model": {"name": "persistence"},
}

#: A real one, byte for byte, from `ic/gom_25km`. The column widths and the
#: trailing labels are FMS's and are what the parsing has to survive.
COUPLER_RES = (
    "     4        (Calendar: no_calendar=0, thirty_day_months=1, julian=2,"
    " gregorian=3, noleap=4)\n"
    "  2015     1     4    13     0     0        Model start time:   year,"
    " month, day, hour, minute, second\n"
    "  2015     1     5     1     0     0        Current model time: year,"
    " month, day, hour, minute, second\n"
)


@pytest.fixture
def scene(tmp_path):
    paths = Paths(experiment="e", output_root=tmp_path / "o",
                  scratch_root=tmp_path / "s", **PATHS_CYCLE).ensure()
    source = paths.member_out("ana", 1, 0)
    source.mkdir(parents=True)
    (source / "MOM.res.nc").write_bytes(b"ocean\n")
    (source / "ice_model.res.nc").write_bytes(b"ice\n")
    (source / STAMP).write_text(COUPLER_RES)
    return paths, source, paths.member_out("rst", 1, 0)


def run(scene):
    paths, source, target = scene
    return persistence.forecast(CONFIG, paths, 1, "forecast", 0,
                                source=source, target=target)


def test_the_state_arrives_unchanged(scene):
    target = run(scene)
    assert (target / "MOM.res.nc").read_bytes() == b"ocean\n"
    assert (target / "ice_model.res.nc").read_bytes() == b"ice\n"
    assert not list(target.glob("*.partial"))


def test_the_clock_advances_by_one_cycle(scene):
    """Cycle 1 of a twelve hour experiment starting at 01:00 ends at 13:00.

    The forecast's product is the *next* cycle's background, so its time is the
    end of this cycle's window and not its analysis time.
    """
    target = run(scene)
    lines = (target / STAMP).read_text().splitlines()
    assert lines[2].split()[:6] == ["2015", "1", "5", "13", "0", "0"]


def test_the_calendar_and_the_start_time_are_left_alone(scene):
    """Facts about the run this state came from, not about this cycle.

    Rewriting the start time would make every restart claim it began where it
    happens to be, which is the one thing a restart chain is for recording.
    """
    target = run(scene)
    lines = (target / STAMP).read_text().splitlines()
    assert lines[0] == COUPLER_RES.splitlines()[0]
    assert lines[1] == COUPLER_RES.splitlines()[1]
    # The trailing label survives, because the substitution is on the numbers.
    assert "Current model time" in lines[2]


def test_the_state_is_copied_and_not_shared(scene):
    """`cleanup` reaps the directory this came from, two cycles later.

    A link would leave the next forecast pointing at nothing, and the failure
    would surface far from the cycle that caused it.
    """
    paths, source, _ = scene
    target = run(scene)
    assert not (target / "MOM.res.nc").is_symlink()
    (source / "MOM.res.nc").unlink()
    assert (target / "MOM.res.nc").read_bytes() == b"ocean\n"


def test_a_source_that_is_not_a_restart_set_is_refused(scene):
    paths, source, target = scene
    (source / STAMP).unlink()
    with pytest.raises(ModelError, match="not a restart set"):
        persistence.forecast(CONFIG, paths, 1, "forecast", 0,
                             source=source, target=target)


def test_a_coupler_res_that_is_not_one_is_refused(scene):
    """Rather than a restart set that says it is valid in 1958.

    The whole file is three lines, so a wrong one is far more likely to be
    something else entirely than to be a subtly wrong date.
    """
    paths, source, target = scene
    (source / STAMP).write_text("this is not a coupler.res\nnor is this\nor this\n")
    with pytest.raises(ModelError, match="six integers"):
        persistence.forecast(CONFIG, paths, 1, "forecast", 0,
                             source=source, target=target)


def test_a_truncated_coupler_res_is_refused(scene):
    paths, source, target = scene
    (source / STAMP).write_text("     4\n  2015 1 4 13 0 0\n")
    with pytest.raises(ModelError, match="three"):
        persistence.forecast(CONFIG, paths, 1, "forecast", 0,
                             source=source, target=target)
