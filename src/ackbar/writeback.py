"""Turning an analysis into the restart set the next forecast starts from.

The analysis application writes a state: a NetCDF file holding the fields it
solved for, on the model's grid, at the analysis time. The model starts from a
restart set: a directory of files holding every prognostic variable it has,
including a great many the analysis never touched. Writeback is the step between
them, and it is a direct write into a copy of the background rather than an
increment fed to the model, because `soca_checkpoint_model.x` does not exist in
the pinned SOCA. See `docs/build-order.md`.

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

Which end of the wider array the analysis corresponds to is not this module's
choice to make: SOCA's reader takes the *leading* tracer-sized corner of every
variable, so that is what an analysis holds and that is what this writes back
into. What makes those the right cells is that the domain's gridspec has been
shifted onto the same faces, which is `ackbar.gridspec` and is where the whole
argument lives. The mask this applies comes from that gridspec, so the two
cannot drift apart.

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

        # The velocities are scaled together, before either is written, because
        # the quantity being bounded is a property of the pair: how much water
        # the increment moves in and out of a column. See `divergence_scaling`.
        scales = {}
        limit = divergence_limit(config)
        if limit is not None and {"u", "v"} <= set(source.variables):
            scale_u, scale_v, report = divergence_scaling(
                target, source, masks, metrics_of(config), float(limit))
            scales = {"eastward_sea_water_velocity": scale_u,
                      "northward_sea_water_velocity": scale_v}
            lines.append(report)

        for field in variables:
            io = field["io name"]
            if io not in source.variables:
                raise ModelError(
                    f"the analysis has no {io}, so {field['name']} was solved "
                    f"for and not written. Check `analysis variables` against "
                    f"what the application's own output configuration asked for."
                )
            if field["name"] == THICKNESS:
                # Last, and by its own rule. See `place_thickness`.
                continue
            values = np.asarray(source.variables[io][0])
            mask = masks[field["grid"]]
            cropped = (alive[..., :mask.shape[0], :mask.shape[1]]
                       if values.ndim == alive.ndim else None)
            lines.append(place(target, field, mask, values,
                               limit=limits.get(field["name"]),
                               relaxation=relaxation, alive=cropped,
                               taper=scales.get(field["name"])))

        for field in variables:
            if field["name"] != THICKNESS:
                continue
            lines.append(place_thickness(
                target, field, masks[field["grid"]],
                np.asarray(source.variables[field["io name"]][0]),
                relaxation=relaxation))
    return lines


#: The mass field, which is written by `place_thickness` and by nothing else.
#: Named here rather than matched on `io name`, because what decides the rule is
#: which quantity this is and not which letter the restart calls it.
THICKNESS = "sea_water_cell_thickness"


def place_thickness(target, field, mask, values, relaxation=1.0):
    """Write the analysis's column mass, and only its column mass.

    **Off everywhere, and this docstring no longer argues for switching it on.**
    The gate below is that an experiment names `sea_water_cell_thickness` among
    its `analysis variables`; no layer does, so nothing runs this today. It is
    kept because the recipe and its positivity argument are the hard part, and
    the resolution reasoning in `docs/osse.md` says a coarse global domain may
    need exactly them.

    **The premise this was written for has been withdrawn.** It said the mass
    field carries a barotropic residual that nothing else reconstructs, on the
    strength of a `docs/osse.md` claim of 2.1 cm of implied sea level against a
    0.95 cm `ave_ssh` increment. That does not reproduce: read from
    `ocn.incr.incr.*.nc`, `sum(dh)` and `d(ave_ssh)` agree at ratio 1.00 to 1.05,
    correlation 0.99, slope 1.00. One signal, not two. Sea level is already
    written twice, as the steric height the temperature and salinity increment
    implies and as `ave_ssh` itself, and neither route is `h`.

    **`osse25-4dletkf-h` measured what writing it anyway is worth: nothing.** Ten
    cycles against the identical baseline gave SSH spread -0.21% and an e-folding
    of 8.6+-0.6 against 8.7+-0.6, with no field favoured. What survives a forecast
    correlates with the increment at +0.03, so it is unrelated adjustment rather
    than a damped copy of it. `docs/osse.md` has the delivery-route argument and
    the resolution caveat.

    **Not the per-layer increment.** Writing `h_ana` cell by cell is the obvious
    form and it is a model crash rather than a bad answer: on `gom_25km` the
    analysis itself puts a fraction of a percent of cells at or below zero
    thickness, and MOM6 reports that several steps downstream as
    `adjust_interface_motion: implied h<0`. So each column is *scaled*,

        h_new = h_bkg * sum(h_ana) / sum(h_bkg)

    which moves exactly the column integral and cannot produce a negative
    thickness while the analysis's own column integral is positive. Measured on
    this domain the factor stays inside half a percent of one.

    **What that deliberately discards is the vertical redistribution.** The
    filter also moved mass between layers, and that part is dropped here rather
    than approximated: under Z* the model regrids on its first step anyway, so
    the layer interfaces are the coordinate's business and the column integral
    is the state. Keeping the redistribution is what would need the positivity
    argument this form does not need.

    **No `fill_down`, and no `increment limits` entry.** Both act per cell on a
    field whose per-cell values are not being written. A bound on the column
    delta is a different instrument and is not one this needs yet: the scale
    factor is its own bound, and it is the model's response to a fast free
    surface change, not the size of the change, that would justify one.
    """
    io = field["io name"]
    if io not in target.variables:
        raise ModelError(f"the restart has no {io} to write {field['name']} into")

    data = np.asarray(target.variables[io][0])
    if values.shape != data.shape:
        raise ModelError(
            f"the restart's {io} is {data.shape} against an analysis "
            f"{values.shape}; the mass field is on the tracer grid in both"
        )

    wet = np.broadcast_to(mask, data.shape[-2:]) > 0
    if not np.all(np.isfinite(values[..., wet])):
        raise ModelError(
            f"the values for {io} are not finite in the ocean, so the column "
            f"mass the analysis implies cannot be formed"
        )

    was = data.sum(axis=0)
    now = values.sum(axis=0)
    # Relaxation acts on the column delta, on the same rule every other field
    # follows: the analysis is written weaker, and it is still the analysis.
    change = (now - was) * relaxation

    # A column whose background holds no water has no scale factor and no sea
    # level to move. `wet` already excludes land; this excludes the degenerate
    # case rather than dividing by it.
    usable = wet & (was > VANISHED)
    factor = np.ones_like(was)
    factor[usable] = (was[usable] + change[usable]) / was[usable]
    if not np.all(factor[usable] > 0.0):
        raise ModelError(
            f"the analysis asks a column's total {io} to become negative, "
            f"which is a state the model has no representation for"
        )

    data[..., usable] = data[..., usable] * factor[usable]
    target.variables[io][0] = data

    if "checksum" in target.variables[io].ncattrs():
        target.variables[io].delncattr("checksum")

    moved = change[usable]
    relaxed = f", relaxed to {relaxation:g}" if relaxation != 1.0 else ""
    return (f"{field['name']}: {moved.size} ocean column(s), sea level "
            f"increment min {moved.min():+.4g} max {moved.max():+.4g} "
            f"rms {np.sqrt(np.mean(moved ** 2)):.4g} m, scale factor "
            f"{factor[usable].min():.6f} to {factor[usable].max():.6f}{relaxed}")


#: How thin a layer has to be before it is not water. Under Z* every column
#: carries all `NK` levels whether or not the seafloor leaves room for them, so
#: the ones below the bottom collapse to a residual thickness. A third of the
#: cells on this domain are in that state.
#:
#: One centimetre is chosen with a gap on both sides rather than tuned: every
#: value across two orders of magnitude selects the same set of cells, so there
#: is nothing for the exact number to change.
VANISHED = 0.01


def fill_down(change, alive):
    """Carry each column's increment down through its collapsed layers.

    **Not the same as leaving them alone, and the difference is convection.** A
    vanished layer holds no water, so the increment the filter computed for it
    is meaningless and must not be used. But leaving the cell at its background
    value while the live layer above it moves puts a step into the column: the
    analysis warms the water at the bottom of the ocean by two degrees and the
    dead layer underneath stays where it was, which is a density inversion the
    model will happily convect away. On this domain that would be manufactured
    at the base of a third of all columns at once.

    Copying the last live increment downward instead leaves the column's
    vertical *gradient* exactly as the background had it through the dead part,
    so nothing is destabilised that was not already. The value written there is
    still meaningless, and it is meaningless in the way the model ignores rather
    than the way it reacts to.

    This is what makes `increment limits` load-bearing rather than a guard: the
    increment being propagated is the deepest live one, and if that is wrong the
    error is now repeated down the column instead of confined to one cell.
    """
    levels = np.arange(change.shape[0]).reshape(-1, 1, 1)
    # The index of the deepest live level at or above each cell. A column whose
    # very first level is dead keeps index 0, so it takes its own value and is
    # left alone, which is the only sensible answer when there is nothing above.
    source = np.maximum.accumulate(np.where(alive, levels, 0), axis=0)
    return np.take_along_axis(change, source, axis=0)


#: How many passes the divergence limiter is allowed. Each one only ever shrinks
#: a face, so the sequence is monotone and terminates; the cap is there because a
#: face is shared by two cells and takes the smaller of their two demands, so one
#: pass can leave a cell that was relying on cancellation still over its limit.
DIVERGENCE_PASSES = 10

#: An hour, in seconds. The limit below is quoted per hour because that is the
#: timescale the failure happens on: a column drains and the model dies inside
#: the first hour of the forecast, long before anything else has responded.
PER_HOUR = 3600.0


def divergence_limit(config):
    """How fast the velocity increment may move the free surface, per column.

    In units of the column's own depth per hour, so one number covers a 10 m
    shelf cell and a 3000 m basin. `None` when nothing is configured.
    """
    return (config.get("solver") or {}).get("increment divergence limit")


def divergence_scaling(restart, analysis, masks, metrics, limit):
    """Scale factors for the velocity increment, per face, from its divergence.

    **A velocity increment kills a column through the divergence of the transport
    it implies, not through its own magnitude.** The model reports `btstep: eta
    has dropped below bathyT` and the column is always a thin one, so the
    tempting fix is to damp the increment in shallow water. That
    treats a symptom: depth is a proxy, and it throws away a strong alongshore
    increment that the column could have carried perfectly well because it moves
    no water in or out.

    What actually kills the column is the divergence of the transport the
    increment implies:

        d(eta)/dt  =  -(1/A) div( sum_k h_k du_k )

    A 0.5 m/s increment across a 25 km cell is a divergence of 2e-5 per second,
    which against a 10 m column lowers the surface 0.7 m an hour and reaches the
    sea floor before the first hour is out. The same increment in 1000 m of water
    is nothing at all. So the bound is on that rate, as a fraction of the
    column's own depth per hour, and it is the only form of this that does not
    need a depth threshold argued into it.

    **The faces, not the cells.** A cell over its limit asks for all four of its
    faces to be scaled, and a face takes the smaller of what its two cells ask,
    so no cell ever ends up with more than it wanted. That is not a fixed point
    in one pass, because divergence is a signed sum and a cell relying on two
    large fluxes cancelling can come out worse when one of them shrinks more than
    the other, so it iterates until nothing is over or `DIVERGENCE_PASSES` is
    reached. Every pass only shrinks, so it converges.

    Computed on the raw increment rather than on the one `fill_down` has already
    touched, because the layers that fills are collapsed ones whose thickness is
    a fraction of a millimetre and which therefore carry no transport either way.

    Returns `(scale_u, scale_v, report)`, both tracer-sized, to be handed to
    `place` as its taper.
    """
    thickness = np.asarray(restart.variables["h"][0])
    height, width = thickness.shape[-2:]
    dx, dy, area = metrics["dx"], metrics["dy"], metrics["area"]
    depth = thickness.sum(axis=0)

    def increment(name, mask):
        got = np.asarray(analysis.variables[name][0])
        was = np.asarray(restart.variables[name][0])[..., :height, :width]
        return (got - was) * mask

    change_u = increment("u", masks["u"])
    change_v = increment("v", masks["v"])
    # The most the transport may take out of a column in a second.
    allowed = limit * depth / PER_HOUR

    scale_u = np.ones((height, width))
    scale_v = np.ones((height, width))
    over = 0
    for _ in range(DIVERGENCE_PASSES):
        # Transport through each cell's western and southern face, which is
        # where `u[i]` and `v[j]` sit on a symmetric-memory C grid.
        west = (thickness * change_u).sum(axis=0) * dy
        south = (thickness * change_v).sum(axis=0) * dx
        # The outer faces have no cell beyond them; `place` gives them their
        # neighbour's increment, so the transport there is the neighbour's too.
        east = np.concatenate([west[:, 1:], west[:, -1:]], axis=1)
        north = np.concatenate([south[1:, :], south[-1:, :]], axis=0)

        rate = np.abs((east - west) + (north - south)) / area
        excess = np.where(allowed > 0, rate / np.maximum(allowed, 1e-30), 0.0)
        over = int((excess > 1.0).sum())
        if not over:
            break
        cell = np.where(excess > 1.0, 1.0 / np.maximum(excess, 1e-30), 1.0)
        # A face takes the smaller of the two cells it separates.
        pass_u = np.minimum(cell, np.concatenate([cell[:, :1], cell[:, :-1]], axis=1))
        pass_v = np.minimum(cell, np.concatenate([cell[:1, :], cell[:-1, :]], axis=0))
        change_u = change_u * pass_u
        change_v = change_v * pass_v
        scale_u = scale_u * pass_u
        scale_v = scale_v * pass_v

    touched = int(((scale_u < 1.0) | (scale_v < 1.0)).sum())
    report = (f"divergence limit {limit:g} column depth(s) per hour: "
              f"{touched} face pair(s) reduced"
              + (f", {over} cell(s) still over after {DIVERGENCE_PASSES} passes"
                 if over else ""))
    return scale_u, scale_v, report


def metrics_of(config):
    """The cell area and face lengths, from the same gridspec as the masks.

    Approximated onto the tracer grid: `dy` at a cell's western face is taken as
    that cell's own `dy` rather than an interpolation between it and its
    neighbour's. On a grid whose spacing varies over hundreds of kilometres and
    for a quantity used to decide whether an increment is dangerous, the
    difference is far below the threshold being tested.
    """
    from .soca import GRIDSPEC

    path = Path(_require(config["domain"], "static")) / GRIDSPEC
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        return {name: np.asarray(data.variables[name][0])
                for name in ("dx", "dy", "area")}


def vanished_layers(restart):
    """Which cells hold enough water for their own increment to mean anything.

    **An analysis must not be written into a collapsed layer**, and the reason
    is not tidiness. A vanished layer has no water in it, so nothing constrains
    what an ensemble covariance says about its temperature: members drawn from
    different years carry unrelated residue there, the spread is large, and the
    filter dutifully produces a large increment. Writing one back gives that
    cell a density anomaly with no mass behind it, the first timestep's dynamics
    respond to the pressure gradient it implies, and the interface moves further
    than the layer is thick. MOM6 then stops with

        MOM_regridding: adjust_interface_motion() - implied h<0

    which names the symptom several steps downstream of the cause.

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
    innovation collapses. A constant relaxation low enough to survive cycle 1
    therefore throws away most of the analysis for every cycle after it.

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


def place(target, field, mask, values, limit=None, relaxation=1.0, alive=None,
          taper=None):
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

    # The increment as a field, so the vertical fill below can see a column.
    change = values - view
    if alive is not None and change.ndim == alive.ndim:
        change = fill_down(change, alive)

    # By column, before anything that acts by value. How much water an increment
    # moves in and out of a column is a property of the pair of velocities and of
    # the sea floor under them, so it is settled before either one is judged on
    # its own size. See `divergence_scaling`.
    if taper is not None:
        change = change * taper

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
    change = change * relaxation
    beyond = 0
    if limit is not None:
        beyond = int((np.abs(change[where]) > limit).sum())
        change = limit * np.tanh(change / limit)

    view[where] = view[where] + change[where]

    # **The staggered outer face.** A symmetric-memory restart carries `u` one
    # column wider and `v` one row taller than the analysis, so the loop above
    # leaves that face untouched. For a tracer that is the whole story, because
    # an h-grid field has no such face. For a velocity it is a defect: every
    # face inside moves and the outermost does not, which is a step in the
    # velocity field exactly at the domain edge, and on a regional domain that
    # edge is an open boundary where the OBC is imposing its own solution.
    #
    # There is no analysed value for that face to use, since SOCA's analysis is
    # tracer-sized, so it takes the increment of the face beside it. That is the
    # weakest assumption available that does not invent a gradient: the two
    # faces are half a cell apart, and the alternative of leaving it at the
    # background asserts the increment falls to zero over that distance.
    if data.shape[-2:] != values.shape[-2:]:
        if data.shape[-1] > width:      # u: one extra column at the east edge
            data[..., :height, width] += change[..., :, -1] * mask[..., :, -1]
        if data.shape[-2] > height:     # v: one extra row at the north edge
            data[..., height, :width] += change[..., -1, :] * mask[..., -1, :]

    target.variables[io][0] = data
    change = change[where]

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
