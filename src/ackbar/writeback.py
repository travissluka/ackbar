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
        applied = apply_analysis(config, analysis, target / restart, cycle)
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

def apply_analysis(config, analysis, restart, cycle=1):
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
        alive = vanished_layers(target)
        limits = increment_limits(config)
        relaxation = increment_relaxation(config, cycle)
        for field in variables:
            io = field["io name"]
            if io not in source.variables:
                raise ModelError(
                    f"the analysis has no {io}, so {field['name']} was solved "
                    f"for and not written. Check `analysis variables` against "
                    f"what the application's own output configuration asked for."
                )
            values = np.asarray(source.variables[io][0])
            mask = masks[field["grid"]]
            if values.ndim == alive.ndim:
                mask = alive[..., :mask.shape[0], :mask.shape[1]] & mask
            lines.append(place(target, field, mask, values,
                               limit=limits.get(field["name"]),
                               relaxation=relaxation))
    return lines


#: How thin a layer has to be before it is not water. Under Z* every column
#: carries all `NK` levels whether or not the seafloor leaves room for them, so
#: the ones below the bottom collapse to a residual thickness. A third of the
#: cells on this domain are in that state.
#:
#: One centimetre is chosen with a gap on both sides rather than tuned: on
#: gom_25km the share of cells below the threshold moves from 34.0% at a
#: centimetre to 35.3% at a metre, so every value across two orders of magnitude
#: selects the same set, and there is nothing for the exact number to change.
VANISHED = 0.01


def vanished_layers(restart):
    """Which cells hold enough water to be worth analysing.

    **An analysis must not be written into a collapsed layer**, and the reason
    is not tidiness. A vanished layer has no water in it, so nothing constrains
    what an ensemble covariance says about its temperature: members drawn from
    different years carry unrelated residue there, the spread is large, and the
    filter dutifully produces a large increment. Writing one back gives that
    cell a density anomaly with no mass behind it, the first timestep's dynamics
    respond to the pressure gradient it implies, and the interface moves further
    than the layer is thick. MOM6 then stops with

        MOM_regridding: adjust_interface_motion() - implied h<0

    which names the symptom several steps downstream of the cause. It is how
    this was found: an LETKF put 16 degC into a 1.3 mm layer at level 34 of a
    52 m column, and thirteen of eighteen members died on the first cycle.

    A variational analysis survives the same restart because its background
    error is a covariance model that tapers rather than a sample, so it never
    produces increments of that size. That makes this a guard the ensemble
    filters need and the others merely never trip, which is an argument for
    putting it here rather than in a solver's layer: it is a fact about the
    model's vertical coordinate, not about how the increment was computed.
    """
    return np.asarray(restart.variables["h"][0]) >= VANISHED


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


def increment_limits(config):
    """The largest increment each variable may be written with, or `{}`.

    **A cap is a statement that the analysis was wrong, not that it was strong**,
    so it is configuration rather than a constant: what counts as too large is a
    property of the domain and the field, and a number that is generous in the
    Gulf's thermocline is absurd at 3000 m.

    Unset means unlimited, which is what every variational experiment here runs
    with. They do not need it: a covariance model tapers, so its increments are
    bounded by construction. A sample covariance is not, and an ensemble filter
    given twenty members will occasionally produce an increment several times
    the spread it was drawn from. Those are the ones that break the model rather
    than improve it, and MOM6 reports them from `adjust_interface_motion` many
    steps after the cause.
    """
    return (config.get("solver") or {}).get("increment limits") or {}


def increment_relaxation(config, cycle):
    """How much of the increment to write, as a fraction. 1.0 writes all of it.

    Either a number, which is constant, or `{first: <fraction>, cycles: <n>}`,
    which ramps linearly from *first* at cycle 1 to 1.0 at cycle *n* and stays
    there. **The ramp is the useful form and the constant is the degenerate
    one**, because the thing being damped is a spin-up transient rather than a
    property of the filter.

    Cycle 1's background is the ensemble's initial condition, and on an OSSE
    that is deliberately a poor estimate: the members here are ocean states from
    twenty *different years*, so the innovation is the largest it will ever be
    and so is the shock of correcting it. Every later background is a 24 hour
    forecast from an analysis that has already seen the observations, so the
    innovation collapses. Measured on `osse25-3dvar`, whose writeback kept all
    45 cycles: increment rms 0.332 at cycle 1, about 0.15 by cycle 5, and 0.11
    from cycle 20 onwards. A constant relaxation set low enough to survive cycle
    1 therefore throws away most of the analysis for forty cycles that never
    needed it.

    **This is the crude form of IAU and it is honest about being crude.** The
    thing that breaks the model is not the size of the increment but the speed
    of it: applying the whole correction between two timesteps leaves the ocean
    with a density field its velocity field knows nothing about, and the
    adjustment squeezes a 2 m layer to a fraction of a millimetre inside one
    step. IAU spreads the same correction across the forecast, so the model
    stays balanced the whole way. Scaling is what is available until then.

    **Preferred over a tighter bound, for a reason worth keeping.** A bound acts
    on each point independently and non-linearly, so it flattens the peaks and
    changes the *shape* of the increment field: two neighbouring points at
    different distances past the bound end up closer together than they were,
    and the gradient between them is not the analysis's any more. Gradients are
    exactly what the model responds to. A scalar multiplies every point by the
    same number, so every gradient shrinks by that number and none is invented.
    The analysis is weaker and it is still the analysis.
    """
    value = (config.get("solver") or {}).get("increment relaxation")
    if value is None:
        return 1.0
    if not isinstance(value, dict):
        return float(value)

    first, cycles = float(value["first"]), int(value["cycles"])
    if cycles <= 1 or cycle >= cycles:
        return 1.0
    return first + (1.0 - first) * (cycle - 1) / (cycles - 1)


def place(target, field, mask, values, limit=None, relaxation=1.0):
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
    height, width = mask.shape[-2:]

    if values.shape[-2:] != mask.shape[-2:]:
        raise ModelError(
            f"the values for {io} are {values.shape[-2:]} and the gridspec's "
            f"{field['grid']} mask is {mask.shape[-2:]}. They are different grids."
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

    # A 2D mask says which columns are ocean and applies to every level. A mask
    # already the shape of the values says which *cells* may be written, which
    # is what `vanished_layers` produces, and it is not a broadcast of the
    # first: a column can be ocean at the surface and have no water in it at
    # level thirty.
    where = mask if mask.shape == values.shape else np.broadcast_to(mask, values.shape)

    if not np.all(np.isfinite(values[where])):
        raise ModelError(
            f"the values for {io} are not finite in the ocean. A minimization "
            f"that diverged produces this, and writing it would produce a "
            f"restart the forecast cannot read past."
        )

    change = values[where] - view[where]

    # Bounded against the *background*, so the limit is on the increment and
    # not on the state: a temperature is not wrong for being 30 degrees, it is
    # wrong for having moved 30 degrees in one analysis.
    #
    # **Soft rather than a hard clip**, and the reason is the failure this
    # exists for. `np.clip` is continuous in space but it pins every point past
    # the bound to exactly the bound, so a region of large increment becomes a
    # plateau with a kink around its edge: real structure flattened, and a new
    # gradient manufactured at the boundary. Spurious gradients driving spurious
    # dynamics is precisely what breaks the model here, so a limiter that makes
    # more of them is the wrong instrument. `L*tanh(x/L)` is smooth, monotone
    # and asymptotic to the bound, so the field keeps its shape and nothing is
    # flat that was not flat before.
    #
    # It is not free: tanh damps everything, not only the tail. At a tenth of
    # the bound the loss is 0.3%, at half it is 8%, at the bound itself 24%. So
    # the bound wants to sit well above the increments that are wanted, and it
    # is the tail it is aimed at.
    # Relaxation first, then the bound. Scaling is the shape-preserving part and
    # should act on what the filter actually produced; the bound is the tail
    # guard and belongs on what is about to be written.
    beyond = 0
    if relaxation != 1.0 or limit is not None:
        change = change * relaxation
        if limit is not None:
            beyond = int((np.abs(change) > limit).sum())
            change = limit * np.tanh(change / limit)
        values = values.copy()
        values[where] = view[where] + change

    view[where] = values[where]
    target.variables[io][0] = data

    # The claim the file makes about itself is now false for this variable, and
    # only for this variable. See the module docstring.
    if "checksum" in target.variables[io].ncattrs():
        target.variables[io].delncattr("checksum")

    # How much the limiter did is reported rather than swallowed, and it is the
    # number to watch: a handful of points past the bound is an ensemble filter
    # meeting it at the edges, and a large fraction is an analysis not to be
    # trusted whether or not the model survives it. The increment quoted is the
    # one actually written, so `max` never exceeds the bound and the count is
    # what says how often it was reached.
    limited = ""
    if relaxation != 1.0:
        limited += f", relaxed to {relaxation:g}"
    if limit is not None:
        share = 100.0 * beyond / max(change.size, 1)
        limited += f", {beyond} point(s) over the {limit:g} limit ({share:.3f}%)"
    return (f"{field['name']}: {change.size} ocean point(s), increment "
            f"min {change.min():+.4g} max {change.max():+.4g} "
            f"rms {np.sqrt(np.mean(change ** 2)):.4g}{limited}")


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
