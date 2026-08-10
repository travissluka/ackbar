"""What a cycle produced, small enough to keep after the cycle is deleted.

Two task bodies, and they exist for one reason: `cleanup` reaps restarts and
observation output on a schedule, so an experiment that records nothing before
that runs leaves no evidence it ever ran. Everything here is written *inside*
the cycle it describes, from files that are still there.

`post.obs` reduces the analysis's own departure files to one JSON document per
cycle. `post.state` reduces a member's background and analysis restart sets to
one compressed NetCDF per cycle per member. Neither computes skill: skill needs
a truth run, that is `verify`, and it is a different task with a different input
that an experiment may not have.

**Both are lossy on purpose, and neither is lossy about anything a comparison
reads.** The state record drops every prognostic field an ocean analysis is not
scored on, and stores what it keeps as float32 quantized to five significant
digits, which is 0.001 K at ocean temperatures and a tenth of a millimetre of
sea surface height. What that buys is roughly a factor of nine over the restart
it came from, and the whole argument for keeping it is that it is small enough
to keep for every cycle of every member of every experiment at once.

The output of both is at the top level of the experiment directory, which
`cleanup` never touches: `ana/<date>/mem###.nc`, `bkg/<date>/mem###.nc` and the
summary beside the departures in `obs_out/<date>/`. That is the entire retention
policy, and it is soca-science's: what is large and regenerable lives under
`run/` and is reaped, and what is small and impossible to regenerate once its
source is gone lives above it and is not. See the header of `paths.py`.
"""

import json
import re
from pathlib import Path

import netCDF4
import numpy as np

#: How a departure is spelled in an ioda output file, and what it means.
#:
#: **The group names carry an outer-iteration index and it is not optional.** A
#: variational analysis writes `hofx0`, `hofx1`, `EffectiveQC0` and
#: `EffectiveQC1`, not `hofx` and `EffectiveQC`. Looking for the bare name finds
#: nothing, and code that treats "no QC group" as "nothing was rejected" then
#: reports every observation as assimilated forever.
#:
#: The lowest index is the background evaluation and the highest is the final
#: one, so `ObsValue - hofx<low>` is O-B and `ObsValue - hofx<high>` is O-A. That
#: derivation is preferred over the `ombg` and `oman` the application writes for
#: one reason: it has no sign to get wrong. soca-science's iodaplots
#: configuration carries the note "bug in oops, ombg and oman are actually B-O
#: and A-O for var" beside a `-@ombg` that negates them, and against the pinned
#: SOCA that note is **false**: `ombg` here equals `ObsValue - hofx0` to float32
#: rounding. Deriving rather than inheriting means this module does not have to
#: be right about which vintage is in the build.
OBS_VALUE = "ObsValue"
#: The declared observation error, which is what a departure has to be read
#: against. O-B carries the model's error and the instrument's together, so an
#: O-B rms is not an error and a percentage reduction in one understates the
#: reduction in the other by however much of it the instrument owns. In an OSSE
#: that share is known exactly, because the noise was drawn with this number;
#: `tools/local/osse-compare.py` subtracts it in variance. `ObsError` and not
#: `EffectiveError`, which is what the filters left after inflation and is a
#: different quantity.
OBS_ERROR = "ObsError"
FORWARD = "hofx"
BACKGROUND = "ombg"
ANALYSIS = "oman"

#: What the filter chain concluded, zero meaning kept. The *lowest* index is the
#: one that decides what the solve saw, so it is the one that decides what
#: `assimilated` means; a later iteration can reject more, and reporting that as
#: what was assimilated would understate what the analysis actually fitted.
QC = "EffectiveQC"
QC_KEPT = 0

#: Trailing digits on a group name, which are the outer iteration.
_INDEXED = re.compile(r"^(?P<stem>.*?)(?P<index>\d*)$")

#: What a state record holds, and the name it holds it under. The keys are the
#: MOM6 restart's own spelling and the values are ACKBAR's, which are the CF-ish
#: names anything reading this file downstream would guess.
#:
#: `h` is here because Temp and Salt are meaningless without it: MOM6's vertical
#: coordinate moves, so a temperature at index k is at a depth that is a
#: property of the state rather than of the grid. A record without thickness
#: cannot be interpolated to a depth, which is every subsurface comparison there
#: is.
#:
#: `u` and `v` are here, and they cost the record a second and third grid: the
#: forecast's MOM6 is built with symmetric memory, so `u` is one column wider
#: than the tracer grid and `v` one row taller (see the writeback header). They
#: are kept anyway, because soca-science kept them and because a comparison of
#: two ocean analyses that cannot look at the currents is not much of one on a
#: domain whose whole signal is the Loop Current.
#:
#: The value is the name in the record; the second element is which of the
#: gridspec's masks and coordinate pairs the field lives on.
FIELDS = {
    "Temp": ("temperature", "h"),
    "Salt": ("salinity", "h"),
    "h": ("thickness", "h"),
    "ave_ssh": ("sea_surface_height", "h"),
    "u": ("eastward_velocity", "u"),
    "v": ("northward_velocity", "v"),
}

#: The gridspec's name for each grid's mask, and the axis its extra point falls
#: on. Indexed by SOCA's own vocabulary, the same way `writeback.MASKS` is,
#: rather than by a second one invented here.
GRIDS = {
    "h": {"mask": "mask2d", "lon": "lon", "lat": "lat"},
    "u": {"mask": "mask2du", "lon": "lonu", "lat": "latu"},
    "v": {"mask": "mask2dv", "lon": "lonv", "lat": "latv"},
}

#: The two states a cycle produces. Its analysis is valid now; its forecast is
#: valid at the next analysis time and is that cycle's background. Both are
#: filed under the date they are valid at, so `bkg/<T>` and `ana/<T>` are two
#: readings of the same instant written by two different cycles.
#:
#: One file each rather than one file holding both. They are separate answers: a
#: comparison between two experiments is a comparison of their analyses or of
#: their backgrounds, never of a pair, and a free run writes one and not the
#: other.
STATES = ("bkg", "ana", "fcst")

#: Compression. `significant_digits` is netCDF's own quantization (BitGroom in
#: libnetcdf 4.9), applied before deflate: it zeroes the bits the stated
#: precision does not reach, which is what makes the deflate that follows
#: actually work on floating point. Without it float32 plus zlib buys about
#: three; with it, about nine.
#:
#: Five digits rather than a coarser round number because this is a *record of a
#: state*, not a plot. Five is beyond anything a verification metric resolves
#: and still far short of storing noise, and the failure mode of guessing low
#: here is discovering in six months that an archived experiment cannot answer a
#: question it was kept to answer.
ENCODING = {
    "zlib": True,
    "complevel": 4,
    "shuffle": True,
    "significant_digits": 5,
}

#: Land, in the record. The restarts carry a real number under the mask (MOM6
#: mostly does not care what is there) and averaging it silently poisons every
#: domain statistic, so it is replaced by the fill value once, here, rather than
#: by each reader remembering to.
FILL = netCDF4.default_fillvals["f4"]


class PostError(Exception):
    pass


# --- observation space --------------------------------------------------------

def obs_stats(observers, target):
    """Reduce this cycle's departure files to one JSON document.

    *observers* is the realized list `stage.obs` wrote, already filtered to the
    ones that ran. Reads only what the analysis produced, so a cycle whose
    analysis failed still writes a document: it says which observers have no
    output, which is the diagnostic that is wanted at exactly that moment.
    """
    records = []
    for observer in observers:
        output = Path(observer["output"])
        record = {"name": observer["name"], "file": str(output)}
        if not output.exists():
            record["error"] = "no output file, so this observer's analysis did not run"
            records.append(record)
            continue
        try:
            record.update(_one_observer(output))
        except OSError as error:
            record["error"] = f"{type(error).__name__}: {error}"
        records.append(record)

    payload = {"observers": records, "totals": _obs_totals(records)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def _one_observer(path):
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        groups = set(data.groups)
        variables = sorted(data.groups[OBS_VALUE].variables) if OBS_VALUE in groups else []
        record = {"groups": sorted(groups), "variables": {}}
        for name in variables:
            record["variables"][name] = _one_variable(data, groups, name)
    return record


def _one_variable(data, groups, name):
    observed = _column(data, OBS_VALUE, name)
    qc_group = _first(groups, QC, name, data)
    kept, flags = _kept(data, qc_group, name, observed.shape)

    out = {
        "count": int(observed.size),
        "assimilated": int(kept.sum()),
        "qc_group": qc_group,
        "obs_mean": _mean(observed[kept]),
        "obs_error_rms": _error(data, groups, name, kept),
    }
    if flags is None:
        # No QC group at all means the file cannot say, which is not the same
        # as nothing having been rejected, so it is said rather than implied.
        out["qc_group"] = None
        out["note"] = "no EffectiveQC group; every observation counted as kept"
    else:
        # Which filter rejected what, as far as the file can say: the value is
        # the QC flag the chain stopped on, and its meaning is ufo's enum. The
        # counts are reported without interpreting them, because interpreting
        # them means pinning a table that lives in another repository.
        codes, counts = np.unique(flags[flags != QC_KEPT], return_counts=True)
        out["rejected"] = {str(int(c)): int(n) for c, n in zip(codes, counts)}

    _departures(data, groups, name, observed, kept, out)
    return out


def _departures(data, groups, name, observed, kept, out):
    """O-B and O-A, from the forward operator where the file has one."""
    forward = _indexed(groups, FORWARD, name, data)
    if forward:
        out["omb"] = _departure(observed - _column(data, forward[0], name), kept)
        out["omb_from"] = f"{OBS_VALUE} - {forward[0]}"
        if len(forward) > 1:
            out["oma"] = _departure(observed - _column(data, forward[-1], name), kept)
            out["oma_from"] = f"{OBS_VALUE} - {forward[-1]}"
        return

    # No forward operator in the file. Take what the application wrote, and name
    # the group rather than claim a sign for it.
    for key, stem in (("omb", BACKGROUND), ("oma", ANALYSIS)):
        group = _first(groups, stem, name, data)
        if group:
            out[key] = _departure(_column(data, group, name), kept)
            out[f"{key}_from"] = group


def _indexed(groups, stem, name, data):
    """Every group named *stem* or *stem* plus digits, in iteration order.

    Restricted to the ones that actually carry this variable: a file can hold
    several simulated variables and an operator can write a diagnostic for one
    and not another.
    """
    found = []
    for group in groups:
        match = _INDEXED.match(group)
        if match.group("stem") != stem:
            continue
        if name not in data.groups[group].variables:
            continue
        found.append((int(match.group("index") or -1), group))
    return [group for _, group in sorted(found)]


def _first(groups, stem, name, data):
    found = _indexed(groups, stem, name, data)
    return found[0] if found else None


def _column(data, group, name):
    return np.asarray(data.groups[group].variables[name][:], dtype="f8").ravel()


def _kept(data, qc_group, name, shape):
    """A boolean mask of the assimilated observations, and the raw flags."""
    if not qc_group:
        return np.ones(shape, dtype=bool), None
    flags = np.asarray(data.groups[qc_group].variables[name][:]).ravel()
    return flags == QC_KEPT, flags


def _error(data, groups, name, kept):
    """The rms declared observation error over the observations that were kept.

    `None` where the file has no `ObsError` group, which is not zero: a reader
    that cannot find the instrument's share must not conclude the instrument
    has none. See `OBS_ERROR`.
    """
    if OBS_ERROR not in groups or name not in data.groups[OBS_ERROR].variables:
        return None
    values = _column(data, OBS_ERROR, name)[kept]
    values = values[np.isfinite(values)]
    return (_round(float(np.sqrt(np.mean(values ** 2))))
            if values.size else None)


def _departure(values, kept):
    values = values[kept]
    values = values[np.isfinite(values)]
    return {
        "count": int(values.size),
        "mean": _mean(values),
        "rms": _round(float(np.sqrt(np.mean(values ** 2)))) if values.size else None,
    }


def _mean(values):
    values = values[np.isfinite(values)]
    return _round(float(values.mean())) if values.size else None


def _round(value):
    # Six digits, because this is a summary read by a human and by a plot and
    # by neither at full double precision.
    return round(value, 6)


def _obs_totals(records):
    total = assimilated = 0
    for record in records:
        for stats in (record.get("variables") or {}).values():
            total += stats.get("count", 0)
            assimilated += stats.get("assimilated", 0)
    return {
        "observers": len(records),
        "failed": sum(1 for r in records if "error" in r),
        "count": total,
        "assimilated": assimilated,
    }


# --- state space --------------------------------------------------------------

def state_record(state, source, gridspec, target, *, cycle, member, valid,
                 extra=None):
    """One compressed NetCDF holding one state, for `ana/` or `bkg/`.

    *state* is a name in `STATES` and *source* the restart set holding it. The
    caller decides whether a state exists; this raises if it was asked for one
    that does not, because a `bkg/` file that quietly failed to appear is
    indistinguishable from a cycle that had no background.

    The grid comes from the domain's gridspec rather than from the restart,
    because a MOM6 restart carries indices and no coordinates: a record that
    cannot say where its own values are is not a record anything can plot.

    *extra* is written as further attributes, and exists for the one state whose
    valid time does not identify it: a long forecast's state is a state *and* an
    initialization, so `fcst` records carry both. Two forecasts started five
    days apart pass through the same valid time and are not the same state, so a
    file that recorded only when it is valid could not be told from the other.
    """
    if state not in STATES:
        raise PostError(f"unknown state {state!r}; the states are {STATES}")
    source = Path(source)
    if not (source / "MOM.res.nc").exists():
        raise PostError(
            f"cycle {cycle} member {member} has no {state} state: "
            f"{source / 'MOM.res.nc'} does not exist"
        )

    grids = _read_grid(gridspec)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    with netCDF4.Dataset(temp, "w", format="NETCDF4") as out:
        out.title = "ackbar state record"
        out.state = {"bkg": "background", "ana": "analysis",
                     "fcst": "forecast"}[state]
        out.cycle = cycle
        out.member = member
        out.valid_time = valid
        out.source = str(source)
        out.contents = ("one state at the time named by valid_time, quantized "
                        "to five significant digits; not a restart")
        for name, value in (extra or {}).items():
            setattr(out, name, value)
        _write_grid(out, grids)
        written = _write_state(out, source, grids)
    temp.replace(target)
    return written


def _read_grid(gridspec):
    """The coordinates and mask of every grid `FIELDS` puts a field on.

    A `u` or `v` grid the gridspec does not carry is absent rather than an
    error: an older static stage predates them, and the tracer fields are still
    a usable record. A field whose grid is missing is then skipped, and
    `state_record` returns the list of what it actually wrote.
    """
    gridspec = Path(gridspec)
    if not gridspec.exists():
        raise PostError(
            f"{gridspec} does not exist. It is the domain's static stage, built "
            f"by tools/soca-gridspec.sh, and it is where this record's "
            f"coordinates come from."
        )
    grids = {}
    with netCDF4.Dataset(gridspec) as data:
        data.set_auto_mask(False)
        for key, names in GRIDS.items():
            if not all(n in data.variables for n in names.values()):
                continue
            grids[key] = {
                "lon": np.asarray(data.variables[names["lon"]][0], dtype="f8"),
                "lat": np.asarray(data.variables[names["lat"]][0], dtype="f8"),
                "mask": np.asarray(data.variables[names["mask"]][0], dtype="f8") > 0.0,
            }
    if "h" not in grids:
        raise PostError(
            f"{gridspec} has no `lon`, `lat` and `mask2d`, so it is not a soca "
            f"gridspec. Rebuild it with tools/soca-gridspec.sh."
        )
    return grids


def _write_grid(out, grids):
    """One coordinate set and mask per grid, suffixed by the grid it is.

    The tracer grid is unsuffixed, so a reader that only wants temperature and
    sea surface height sees `lon`, `lat`, `mask` and never learns there are
    three. The staggered ones are `lon_u`, `mask_v` and so on.
    """
    for key, grid in grids.items():
        suffix = "" if key == "h" else f"_{key}"
        ny, nx = grid["lon"].shape
        out.createDimension(f"y{suffix}", ny)
        out.createDimension(f"x{suffix}", nx)
        dims = (f"y{suffix}", f"x{suffix}")
        for name, values, units in (("lon", grid["lon"], "degrees_east"),
                                    ("lat", grid["lat"], "degrees_north")):
            variable = out.createVariable(f"{name}{suffix}", "f8", dims,
                                          zlib=True, complevel=4, shuffle=True)
            variable.units = units
            variable[:] = values
        mask = out.createVariable(f"mask{suffix}", "i1", dims, zlib=True,
                                  complevel=4)
        mask.long_name = "1 where ocean"
        mask.comment = ("the domain statistics every consumer computes have to "
                        "be over the same cells, so the mask travels with the "
                        "record")
        mask[:] = grid["mask"].astype("i1")


def _write_state(out, restart, grids):
    """Every field of `FIELDS` this restart has, under ACKBAR's name for it."""
    written = []
    with netCDF4.Dataset(restart / "MOM.res.nc") as data:
        data.set_auto_mask(False)
        for source, (name, key) in FIELDS.items():
            if source not in data.variables or key not in grids:
                continue
            grid = grids[key]
            values = _tracer_corner(
                np.asarray(data.variables[source][0], dtype="f4"),
                grid["lon"].shape, restart, source, key)
            written.append(_variable(out, name, values, grid, data, source, key))
    return written


def _tracer_corner(values, shape, restart, source, key):
    """The tracer-sized corner of a possibly larger array.

    A regional domain's MOM6 is built with symmetric memory, because that is
    what the Flather open boundaries need, so its restart carries `u` one column
    wider and `v` one row taller than the gridspec's staggered grids. SOCA is
    not symmetric and works in the tracer-sized array.

    The *leading* corner, and that is not a guess: it is the corner SOCA's
    reader takes, so it is what an analysis holds and what `writeback._place`
    writes back (`data[..., :height, :width]`). The cells this records therefore
    have to be those cells, or the record and the thing the model actually
    integrates are two different grids and the velocity in the archive is
    shifted a column from the velocity in the run.

    Those are the *west* and *south* faces. The coordinates written beside them
    say so, because the domain's gridspec has been shifted onto the same faces
    and this reads `lonu` and `latu` straight out of it. See `ackbar.gridspec`
    for why, and note that a record written before that shift is on the other
    convention.

    Anything that does not differ by a single staggered edge is refused. Silence
    is how a field vanishes from an archive without anything saying so, and a
    wrong grid is how a velocity ends up shifted; both are worth stopping for.
    """
    height, width = shape
    excess = tuple(a - b for a, b in zip(values.shape[-2:], shape))
    if any(e not in (0, 1) for e in excess):
        raise PostError(
            f"{source} in {restart} is {values.shape}, which is not the {key} "
            f"grid {shape} and does not differ from it by a staggered grid's "
            f"edge. `FIELDS` in post.py says which grid each field is on; see "
            f"`GRIDS` beside it."
        )
    return values[..., :height, :width]


def _variable(out, name, values, grid, data, source, key):
    suffix = "" if key == "h" else f"_{key}"
    if values.ndim == 3 and "z" not in out.dimensions:
        out.createDimension("z", values.shape[0])
    dims = (("z",) if values.ndim == 3 else ()) + (f"y{suffix}", f"x{suffix}")

    variable = out.createVariable(name, "f4", dims, fill_value=FILL, **ENCODING)
    variable.source = source
    variable.grid = key
    for attribute in ("units", "long_name"):
        if hasattr(data.variables[source], attribute):
            setattr(variable, attribute, getattr(data.variables[source], attribute))

    values = np.where(grid["mask"], values, FILL)
    variable[:] = values
    return name
