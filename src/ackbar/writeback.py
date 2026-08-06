"""Turning an analysis into the restart set the next forecast starts from.

The analysis application writes a state: a NetCDF file holding the fields it
solved for, on the model's grid, at the analysis time. The model starts from a
restart set: a directory of files holding every prognostic variable it has,
including a great many the analysis never touched. Writeback is the step between
them, and it is a direct write into a copy of the background rather than an
increment fed to the model, because `soca_checkpoint_model.x` does not exist in
the pinned SOCA. That was settled by spike before this phase; see
`docs/build-order.md`.

Copy first, then overwrite in place. That ordering is what makes a rerun safe:
the source of every value is the background, which no task in the experiment
ever modifies, so a writeback that is killed halfway and run again produces the
same restart set rather than an analysis applied twice.

Four things about a MOM6 restart decide the rest of the module.

**Only the ocean cells.** The analysis carries a fill value on land, and writing
it would replace the background's land values with zeros. MOM6 mostly does not
care what is under the land mask, and "mostly" is the problem: the values that
survive there are the ones a diagnostic averages and a checksum covers.

**`u` and `v` are on staggered grids with one extra row or column.** The
forecast's MOM6 is built with symmetric memory, so its restart carries `u` on
`lonq`, which is one wider than `lonh`, and `v` on `latq`, one taller than
`lath`. SOCA's is not symmetric and hands back the tracer-sized array. Writing
that straight in would shift every velocity by one cell, which looks like a
model that develops a strange coastal jet a week later.

**The `checksum` attribute is a claim about the data.** MOM6 reads it back and
aborts on a mismatch, which is exactly right and exactly what a modified restart
triggers. The attribute is dropped from the variables this writes, and from no
others, so every field the analysis did not touch keeps its integrity check.
That is why `RESTART_CHECKSUMS_REQUIRED` is not in `MOM_override`: switching the
check off for the whole file to accommodate three variables would discard the
check on the twenty it still applies to.

**`coupler.res` is what says the set is whole**, so it is written last, the same
rule `mom6sis2.commit` follows.
"""

import shutil
from pathlib import Path

import netCDF4
import numpy as np
import yaml

from .mom6sis2 import STAMP, ModelError

#: The gridspec variable holding each grid's land mask. `grid` is the fields
#: metadata's own key, so this table is indexed by SOCA's vocabulary rather than
#: by a second one invented here.
MASKS = {"h": "mask2d", "u": "mask2du", "v": "mask2dv"}

#: The restart file the analysis writes into. One entry today, and it is a
#: mapping rather than a constant because the fields metadata's `io file` is
#: what decides where a field lives: an ice analysis writes `ice_model.res.nc`
#: and would need this to grow rather than to be edited.
IO_FILES = {"ocn": "ocn"}


def writeback(config, paths, cycle, member, *, background, analysis, target):
    """Build one member's analysed restart set in *target*.

    *analysis* is the state `da` wrote, or None when the cycle had no
    observations to assimilate. With None the restart set is the background
    unchanged, which is what "the analysis is the background" means on disk, and
    it is said out loud rather than inferred from a cycle that ran quickly.
    """
    if not (background / STAMP).exists():
        raise ModelError(
            f"{background} is not a restart set: no {STAMP}. There is nothing "
            f"to apply an analysis to."
        )

    restart = _require(config["model"].get("restart") or {}, "ocn")
    target.mkdir(parents=True, exist_ok=True)
    copy_set(background, target, exclude=(STAMP,))

    if analysis is None:
        print(f"ackbar: {cycle}.writeback member {member}: no analysis for this "
              f"cycle, so the restart set is the background unchanged")
    else:
        applied = apply_analysis(config, analysis, target / restart)
        for line in applied:
            print(f"ackbar: {cycle}.writeback member {member}: {line}")

    # Last, so that a set that has one is a set that is whole.
    _copy(background / STAMP, target / STAMP)
    return target


# --- the restart set ---------------------------------------------------------

def copy_set(source, target, exclude=()):
    """Every file of the background's restart set, except what is excluded.

    A restart set is more than the ocean file: SIS2's ice state, the icebergs
    and the calving field are all in it, and a forecast handed a directory
    missing one of them starts from a default rather than from the state the
    experiment was in. Copied rather than linked, so that the analysed set stays
    a set once `cleanup` reaps the background it came from.
    """
    for entry in sorted(Path(source).iterdir()):
        if entry.name in exclude or not entry.is_file():
            continue
        _copy(entry, target / entry.name)


def _copy(source, target):
    """Through a temporary name in the destination directory, then rename."""
    temp = target.with_name(target.name + ".partial")
    shutil.copyfile(source, temp)
    temp.replace(target)
    return target


# --- the analysis ------------------------------------------------------------

def apply_analysis(config, analysis, restart):
    """Overwrite *restart*'s ocean cells with the analysis. Returns a report.

    The report is returned rather than printed because it is the only place the
    numbers are legible: everything downstream is a forecast, and an analysis
    that was written to the wrong variable or shifted by a cell produces one of
    those just as readily as a correct analysis does.
    """
    variables = analysed_fields(config)
    masks = masks_of(config)
    lines = []

    with netCDF4.Dataset(analysis) as source, \
            netCDF4.Dataset(restart, "r+") as target:
        source.set_auto_mask(False)
        target.set_auto_mask(False)
        for field in variables:
            io = field["io name"]
            if io not in source.variables:
                raise ModelError(
                    f"the analysis has no {io}, so {field['name']} was solved "
                    f"for and not written. Check `analysis variables` against "
                    f"what the application's own output configuration asked for."
                )
            lines.append(place(target, field, masks[field["grid"]],
                               np.asarray(source.variables[io][0])))
    return lines


def analysed_fields(config):
    """The analysis variables, as fields metadata entries, in configured order.

    Refuses here rather than at the write, because "this variable is not in the
    metadata" and "this variable lives in the ice restart" are both statements
    about the configuration and neither should be discovered halfway through
    editing a file.
    """
    known = fields_of(config)
    out = []
    for name in _require(config["solver"], "analysis variables"):
        field = known.get(name)
        if field is None:
            raise ModelError(
                f"{name} is an analysis variable with no entry in the fields "
                f"metadata, so nothing knows which restart variable it is"
            )
        if field["io file"] not in IO_FILES:
            raise ModelError(
                f"{name} lives in the {field['io file']!r} restart, and "
                f"writeback only writes the ocean one. Writing it needs that "
                f"file added to IO_FILES and a mask for its grid."
            )
        out.append(field)
    return out


def place(target, field, mask, values):
    """Write *values* into one variable of an open restart, ocean cells only.

    Separate from `apply_analysis` because the values do not always come from an
    analysis: `ensemble.replace_from_mean` builds a missing member's background
    from the mean of the ones that arrived, and every rule below (the mask, the
    staggered grid, the checksum, the finiteness check) applies to that
    identically.
    """
    io = field["io name"]
    if io not in target.variables:
        raise ModelError(f"the restart has no {io} to write {field['name']} into")

    values = np.asarray(values)
    data = np.asarray(target.variables[io][0])
    height, width = mask.shape

    if values.shape[-2:] != mask.shape:
        raise ModelError(
            f"the values for {io} are {values.shape[-2:]} and the gridspec's "
            f"{field['grid']} mask is {mask.shape}. They are different grids."
        )
    # The tracer-sized corner of a possibly larger array. A symmetric-memory
    # restart carries `u` one column wider and `v` one row taller than the
    # analysis does, and this is the part they have in common. Basic slicing, so
    # `view` writes through into `data`.
    view = data[..., :height, :width]
    if view.shape != values.shape:
        raise ModelError(
            f"the restart's {io} is {data.shape} against an analysis "
            f"{values.shape}; those do not differ by a staggered grid's edge"
        )

    if not np.all(np.isfinite(values[..., mask])):
        raise ModelError(
            f"the values for {io} are not finite in the ocean. A minimization "
            f"that diverged produces this, and writing it would produce a "
            f"restart the forecast cannot read past."
        )

    change = values[..., mask] - view[..., mask]
    view[..., mask] = values[..., mask]
    target.variables[io][0] = data

    # The claim the file makes about itself is now false for this variable, and
    # only for this variable. See the module docstring.
    if "checksum" in target.variables[io].ncattrs():
        target.variables[io].delncattr("checksum")

    return (f"{field['name']}: {change.size} ocean point(s), increment "
            f"min {change.min():+.4g} max {change.max():+.4g} "
            f"rms {np.sqrt(np.mean(change ** 2)):.4g}")


# --- what the model calls things ---------------------------------------------

def fields_of(config):
    """The fields metadata, indexed by JEDI variable name.

    ACKBAR's own copy of the file SOCA reads, and read here rather than
    duplicated as a table in this module: the mapping from
    `sea_water_potential_temperature` to `Temp` on the `h` grid is a statement
    the model layer already makes, and a second copy of it is a writeback that
    keeps working after someone corrects the first.
    """
    path = _require(config["model"], "fields metadata")
    entries = yaml.safe_load(Path(path).read_text())
    fields = {}
    for entry in entries or ():
        if "io name" not in entry or "io file" not in entry:
            # A derived field: computed by SOCA, in no restart file, and not
            # something an analysis can be written back into.
            continue
        fields[entry["name"]] = {
            "name": entry["name"],
            "io name": entry["io name"],
            "io file": entry["io file"],
            "grid": entry.get("grid", "h"),
        }
    return fields


def masks_of(config):
    """The land masks, from the domain's gridspec.

    The gridspec rather than the restart's own land points, because a restart
    holds real numbers under the land and there is no value that reliably means
    "not ocean" in one. It is also the same file the diffusion calibration and
    the geometry read, so the analysis, its background error and its writeback
    cannot disagree about where the coast is.
    """
    from .soca import GRIDSPEC

    static = Path(_require(config["domain"], "static"))
    path = static / GRIDSPEC
    if not path.exists():
        raise ModelError(
            f"{path} does not exist. It is the static stage's product for this "
            f"domain, built by tools/soca-gridspec.sh, and writeback needs it "
            f"to know which cells are ocean."
        )
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return {grid: np.asarray(data.variables[name][0]) > 0.0
                for grid, name in MASKS.items()}


def _require(mapping, key):
    value = mapping.get(key)
    if not value:
        raise ModelError(f"{key} is not set, and writeback needs it")
    return value
