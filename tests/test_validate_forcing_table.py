"""Tier 1: does something actually supply the files a forcing table names?

`_forcing_pair` already checks the pairing that describes the *model*: a data
table comes with a `SIS_forcing`, because a sub-daily shortwave read with
`ADD_DIURNAL_SW` left on applies the diurnal cycle twice. This is the other
half, and it is about the *files*.

A data table names files under `INPUT/`, in a quoted fourth column. Those names
are the one class of input in the whole configuration that is not a path this
validator would otherwise walk, so nothing checked that anything puts them
there. Inheriting `forcing/common` without a source layer is the case that
matters: it sets a table whose every row reads `INPUT/atm.nc` and sets
`SIS_forcing`, so the pairing passes, and with no `ensemble.inputs` and no copy
in the domain's own `INPUT/` the experiment validates clean on all six steps and
every member's first forecast dies inside `data_override`.
"""

from pathlib import Path

from ackbar.validate import _forcing_table_files

#: Two rows that name a file and two that do not, which is what a real table
#: looks like: `p_surf` and `z_bot` are constants with an empty file column.
TABLE = '''\
"ATM" , "p_surf"    , ""      , ""              , "bilinear" ,  1.e5
"ATM" , "t_bot"     , "T2"    , "INPUT/atm.nc"  , "bilinear" ,  1.0
"ICE" , "lprec"     , "PRATE" , "INPUT/atm.nc"  , "bilinear" ,  1.0
"ATM" , "z_bot"     , ""      , ""              , "bilinear" , 10.0
'''


def case(tmp_path, *, staged=(), present=()):
    """A rendered config with a table, an `INPUT/`, and an overlay."""
    table = tmp_path / "data_table.atm"
    table.write_text(TABLE)
    where = tmp_path / "INPUT"
    where.mkdir(exist_ok=True)
    for name in present:
        (where / name).touch()
    rendered = {"model": {"data_table": str(table), "input": str(where)}}
    if staged:
        rendered["ensemble"] = {"inputs": {n: "/archive/{{member_dir}}.nc"
                                           for n in staged}}
    return rendered


def test_a_table_whose_file_nothing_supplies_is_reported(tmp_path):
    """`forcing/common` inherited on its own, which is the live case."""
    findings = _forcing_table_files(case(tmp_path))
    assert len(findings) == 1
    assert findings[0].step == 2
    assert "atm.nc" in findings[0].message
    assert "data_override" in findings[0].message


def test_an_overlay_key_satisfies_it(tmp_path):
    """What `forcing/gefs-ensemble` does: one `ensemble.inputs` key per file."""
    assert _forcing_table_files(case(tmp_path, staged=("atm.nc",))) == []


def test_a_copy_in_the_domains_own_input_satisfies_it(tmp_path):
    """The other way a file gets there: shared by every member, not per member.
    A deterministic experiment forced by one archive is a legitimate shape."""
    assert _forcing_table_files(case(tmp_path, present=("atm.nc",))) == []


def test_each_missing_file_is_its_own_finding(tmp_path):
    """A table can name more than one, and naming them separately is what makes
    the message actionable."""
    table = tmp_path / "data_table.atm"
    table.write_text(TABLE + '"ICE" , "runoff" , "R" , "INPUT/river.nc" , '
                             '"bilinear" , 1.0\n')
    where = tmp_path / "INPUT"
    where.mkdir()
    findings = _forcing_table_files(
        {"model": {"data_table": str(table), "input": str(where)}})
    assert {f.message.split("INPUT/")[1].split(",")[0] for f in findings} == {
        "atm.nc", "river.nc"}


def test_no_table_is_not_this_checks_business(tmp_path):
    """Every experiment that inherits no forcing layer at all."""
    assert _forcing_table_files({"model": {"input": str(tmp_path)}}) == []
    assert _forcing_table_files({}) == []


def test_a_table_that_is_not_there_is_left_to_step_three(tmp_path):
    """Absence of the table itself is already reported, and says it better."""
    rendered = {"model": {"data_table": str(tmp_path / "gone"),
                          "input": str(tmp_path)}}
    assert _forcing_table_files(rendered) == []


def test_the_repos_own_table_names_exactly_one_file(tmp_path):
    """A guard on the parse rather than on the config: if the table's shape
    changes, the regex stops finding anything and every case above still passes
    while the check silently does nothing."""
    repo = Path(__file__).resolve().parents[1]
    table = repo / "config/model/mom6sis2/domain/gom/common/data_table.atm"
    where = tmp_path / "INPUT"
    where.mkdir()
    findings = _forcing_table_files(
        {"model": {"data_table": str(table), "input": str(where)}})
    assert len(findings) == 1
    assert "INPUT/atm.nc" in findings[0].message
