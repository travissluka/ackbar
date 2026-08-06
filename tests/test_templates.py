"""Tier 0: the SOCA document templates, against the tasks that fill them.

`config/soca/*.yaml` holds the shape of each application's document and
`ackbar/soca.py` holds its values, and the join between them is a set of slot
names that nothing at run time compares until the application is about to be
launched. For `recenter` that is after the ensemble filter and the
deterministic analysis have both already run, so it is checked here instead.

Two directions, and both matter. A slot in a template with nothing to fill it
is an application that fails on a config value; a value computed for a slot the
template does not have is a *block that has gone missing*, which is the failure
mode every comment in those files is about. A variational document with no
`output` writes no `oman`, exits 0, and says nothing.
"""

from pathlib import Path

import pytest

from conftest import PATHS_CYCLE
import yaml

from ackbar import soca
from ackbar.config.template import TemplateError, fill, slots_of
from ackbar.config.jobtime import SYMBOL_NAMES, TOKEN
from ackbar.config.jobtime import unresolved as unresolved_jobtime

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "config" / "soca"

#: Which template each task's builder fills. The builders are exercised against
#: a real config elsewhere (`tests/test_soca.py`); what is pinned here is the
#: correspondence itself.
DOCUMENTS = ("hofx", "hofx4d", "var", "varfgat", "var4d", "letkf", "recenter")

#: The slots each document is built with, spelled out rather than read back from
#: the builder. A test that derived both sides from the same call would pass for
#: any pair of files that happened to agree with each other, including two that
#: had both dropped the same block.
EXPECTED = {
    "hofx": {"GEOMETRY", "BACKGROUND_DIR", "RESTART_FILE", "STATE_VARIABLES",
             "OBSERVERS"},
    "hofx4d": {"GEOMETRY", "TSTEP", "STATES", "BACKGROUND_DIR", "RESTART_FILE",
               "STATE_VARIABLES", "WINDOW_BEGIN", "WINDOW_LENGTH", "OBSERVERS"},
    "var": {"GEOMETRY", "ANALYSIS_VARIABLES", "BACKGROUND_DIR", "RESTART_FILE",
            "BACKGROUND_VARIABLES", "BACKGROUND_ERROR", "OBSERVERS",
            "VARIATIONAL", "ANALYSIS_OUTPUT", "INCREMENT_OUTPUT"},
    # `var` plus the pseudo model's two. Everything else is the same document
    # solved against a trajectory instead of one state, which is why the two
    # sets differ by exactly `TSTEP` and `STATES` and nothing else.
    "varfgat": {"GEOMETRY", "ANALYSIS_VARIABLES", "BACKGROUND_DIR",
                "RESTART_FILE", "BACKGROUND_VARIABLES", "BACKGROUND_ERROR",
                "OBSERVERS", "VARIATIONAL", "ANALYSIS_OUTPUT",
                "INCREMENT_OUTPUT", "TSTEP", "STATES"},
    # No `BACKGROUND_DIR`, `RESTART_FILE` or `BACKGROUND_VARIABLES`, and that
    # absence is the difference: a 4D-Ens-Var has one background per sub-window
    # rather than a single state to name by directory, so all three are folded
    # into the entries of `BACKGROUND_STATES`.
    "var4d": {"GEOMETRY", "ANALYSIS_VARIABLES", "BACKGROUND_ERROR", "OBSERVERS",
              "VARIATIONAL", "ANALYSIS_OUTPUT", "INCREMENT_STATES",
              "SUBWINDOW", "BACKGROUND_STATES"},
    "letkf": {"GEOMETRY", "MEMBER_BACKGROUNDS", "OBSERVERS", "LOCAL_ENSEMBLE_DA",
              "ANALYSIS_OUTPUT", "INCREMENT_OUTPUT", "SPREAD_PRIOR_OUTPUT",
              "SPREAD_POSTERIOR_OUTPUT"},
    "recenter": {"GEOMETRY", "ANALYSIS_VARIABLES", "CENTER_DIR", "CENTER_FILE",
                 "MEMBER_ANALYSES", "RECENTERED_OUTPUT"},
}


def load(name):
    return yaml.safe_load((TEMPLATES / f"{name}.yaml").read_text())


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_document_has_a_template(name):
    assert (TEMPLATES / f"{name}.yaml").exists()


@pytest.mark.parametrize("name", DOCUMENTS)
def test_the_template_declares_exactly_the_slots_the_task_fills(name):
    assert slots_of(load(name)) == EXPECTED[name]


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_job_time_token_is_in_the_closed_set(name):
    for _, value in unresolved_jobtime(load(name)):
        for token in TOKEN.findall(value):
            assert token.partition(":")[0].strip() in SYMBOL_NAMES


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_document_names_an_executable(name):
    """A template and a builder are not enough to run anything.

    `run_application` looks the executable up in `soca.APPLICATIONS`, and a
    document with no entry there raises a KeyError from inside a Slurm job
    rather than here. That is the whole distance this test closes: minutes of
    scheduler round trip against a millisecond.
    """
    assert name in soca.APPLICATIONS
    assert soca.APPLICATIONS[name].endswith(".x")


def test_there_are_no_executables_nothing_builds():
    # The other direction. An entry with no template is an application ACKBAR
    # believes it can run and has no document for.
    assert set(soca.APPLICATIONS) == set(DOCUMENTS)


def test_there_are_no_templates_nothing_builds():
    """A file here that no task fills is a document that is never written.

    Which is not harmless: the next person to add an application copies the one
    that looks closest, and a file nothing runs is a file nothing has corrected.
    """
    assert {p.stem for p in TEMPLATES.glob("*.yaml")} == set(DOCUMENTS)


# --- the values a template is not allowed to hold ----------------------------

#: Keys whose value SOCA uses to *construct a filename* that ACKBAR then opens
#: by predicting the same name. Written into a document by `_written`, and read
#: back by `product_name`, so a template that spelled one out would be a second
#: source for a name that has to have exactly one.
NAME_KEYS = ("exp", "type", "datadir")


@pytest.mark.parametrize("name", DOCUMENTS)
def test_no_template_spells_out_a_filename_field(name):
    """The rule that separates this from every templated workflow before it.

    A template holds shape and constants; anything Python also reads stays a
    slot. These three are the ones with teeth, because getting them wrong is a
    `writeback` that opens a name nothing wrote and reports that the analysis
    produced nothing.
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in NAME_KEYS:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(load(name), name)
    assert found == []


# --- the filling itself ------------------------------------------------------

def test_an_unfilled_slot_is_refused():
    with pytest.raises(TemplateError, match="not a slot"):
        fill({"a": "$(MISSING)"}, {})


def test_a_computed_value_the_template_ignores_is_refused():
    """The direction that catches a template which has dropped a block."""
    with pytest.raises(TemplateError, match="has dropped a block"):
        fill({"a": "$(KEPT)"}, {"KEPT": 1, "OUTPUT": {"datadir": "out"}})


def test_a_lowercase_token_is_refused():
    """Lowercase is the experiment-time pass, which never reads these files."""
    with pytest.raises(TemplateError, match="lowercase"):
        fill({"a": "$(experiment_dir)"}, {})


def test_a_whole_token_takes_the_value_type():
    assert fill({"a": "$(N)"}, {"N": [1, 2]}) == {"a": [1, 2]}


def test_a_token_in_text_interpolates():
    assert fill({"a": "/d/$(N)/x"}, {"N": 7}) == {"a": "/d/7/x"}


def test_a_structure_cannot_be_interpolated_into_text():
    with pytest.raises(TemplateError, match="cannot be interpolated"):
        fill({"a": "/d/$(N)"}, {"N": {"k": 1}})


def test_slots_are_found_through_lists_and_nesting():
    document = {"a": [{"b": "$(ONE)"}], "c": {"d": ["$(TWO)"]}}
    assert slots_of(document) == {"ONE", "TWO"}


# --- which copy a job reads --------------------------------------------------

CYCLING = {
    "experiment": {"name": "e"},
    "cycle": {"start": "2018-04-15T00:00:00Z", "length": "PT24H", "count": 3},
}


def test_the_template_directory_is_the_one_the_caller_names(tmp_path):
    """A job reads its own frozen copy, not whatever the checkout says now.

    The whole reason `create` freezes these: editing `config/soca/` while an
    experiment is cycling must not give cycle 40 a different document than
    cycle 39.
    """
    (tmp_path / "hofx.yaml").write_text(
        "frozen: this copy\nwhen: '{{current_cycle}}'\n")
    assert soca.build_document("hofx", CYCLING, 2, {}, templates=tmp_path) == {
        "frozen": "this copy", "when": "2018-04-16T00:00:00Z",
    }


def test_a_missing_template_names_where_it_should_have_come_from(tmp_path):
    with pytest.raises(Exception, match="ackbar create"):
        soca.build_document("hofx", CYCLING, 1, {}, templates=tmp_path)


def test_create_freezes_every_template_into_the_experiment(tmp_path):
    from ackbar.create import _freeze_templates
    from ackbar.paths import Paths

    paths = Paths(experiment="e", output_root=tmp_path / "o",
                  scratch_root=tmp_path / "s", **PATHS_CYCLE).ensure()
    _freeze_templates(paths, REPO)

    frozen = {p.name for p in paths.templates.glob("*.yaml")}
    assert frozen == {p.name for p in TEMPLATES.glob("*.yaml")}
    assert (paths.templates / "var.yaml").read_text() == \
        (TEMPLATES / "var.yaml").read_text()
