"""The observation archive: how it is laid out, and how a window reaches it.

An archive holds one directory per platform, and under it one file per time
bin, named by the ISO timestamp of the bin's start:

    <archive>/adt_j2/20150716T000000Z.nc4

**Nothing about the assimilation cycle is in that layout, and that is the whole
point of it.** The archive used to be written one file per cycle, cut at the
window boundary by the generator, and the seam between two files sat exactly
where an observation could fall through: an assimilation window is
`(begin, end]`, so an observation stamped at the instant a window opens is
dropped by that window, and it was in no other window's file to be picked up by.
About a quarter of every fixed cadence platform's observations were lost that
way and nothing said so. Cutting at generation time also meant the archive
carried a DA convention it has no business knowing, so an experiment could not
change its window length without rebuilding it, and a real observing system,
which arrives in granules of a few minutes and cannot promise to avoid an edge,
could not be filed in it at all.

So the cut happens once, at run time, by the one thing entitled to make it:
`stage.obs` concatenates the bins that touch the window and hands the result to
the observer, and ioda applies its own half open window to the result. There is
no seam inside a window any more, because there is only one file.

## The selection rule, and why it needs no index

The bins of one platform are contiguous and non overlapping, which the generator
guarantees by construction: it walks a period bin by bin and each file holds
exactly what fell in its own bin. Given that, the files a window `(begin, end]`
needs are

  - the file with the largest start at or before `begin`, which is the bin the
    window opens inside, and
  - every file whose start falls in `(begin, end]`.

Nothing here knows the bin duration, and nothing stores one. Daily bins, ten
minute granules and an archive that changed cadence half way through all work,
which matters because soca-science carried both a `P1D` and a `PT10M` database
and a real one is the second kind.

The upper bound is closed for the same reason the window is. An observation at
exactly `end` is inside the window, and if `end` falls on a bin boundary that
observation is in the bin *starting* at `end`, so a rule that stopped at
`start < end` would drop it. That is the original bug in its general form, one
window later, and it is what the round trip test pins.

A bin with no observations in it has no file. That does not break contiguity as
this uses it: the rule only ever needs the bin a given instant falls in to be
the last one at or before it, and a missing file means that bin was empty, so
nothing is lost by reaching further back.

## Times survive concatenation

ioda stores a time as an integer offset against an epoch named in
`MetaData/dateTime`'s `units`, and each bin file is written against its own bin
start. Concatenating them without a word would file the second day's
observations as if they were the first day's, which is a shift of exactly one
bin and is invisible in everything except the answer. So the offsets are rebased
onto one epoch here, and `units` is rewritten to match.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: How a bin's start is spelled in its filename. Seconds are in it so that a
#: granule archive is expressible in the same scheme as a daily one.
STAMP = "%Y%m%dT%H%M%SZ"

SUFFIX = ".nc4"

_STAMP = re.compile(r"^(\d{8}T\d{6}Z)" + re.escape(SUFFIX) + r"$")

#: ioda's location dimension, the one everything is concatenated along.
LOCATION = "Location"

METADATA = "MetaData"
TIME = "dateTime"

#: `units` on an ioda time variable: an interval and an instant. Only seconds
#: are handled, because that is what ioda writes and a different one would be a
#: file this has never seen rather than a case to guess at.
_UNITS = re.compile(r"^\s*(\w+)\s+since\s+(.+?)\s*$")


class ArchiveError(Exception):
    pass


def name(when):
    """The filename a bin starting at *when* is written under."""
    return when.strftime(STAMP) + SUFFIX


def bins(directory):
    """Every bin in one platform's directory, as (start, path), oldest first.

    A file whose name is not a stamp is ignored rather than refused: an archive
    is allowed to carry a README, and a directory that turns out to hold nothing
    this recognizes is reported by the caller as an empty archive, which is the
    message that helps.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.iterdir()):
        match = _STAMP.match(path.name)
        if match:
            found.append((datetime.strptime(match.group(1), STAMP)
                          .replace(tzinfo=timezone.utc), path))
    return sorted(found)


def covering(entries, begin, end):
    """The bins a window `(begin, end]` reads, in time order.

    *entries* is what `bins` returned. See the module docstring for the rule and
    for why the upper bound is closed and the lower one is not.
    """
    opening = [path for start, path in entries if start <= begin]
    inside = [path for start, path in entries if begin < start <= end]
    return (opening[-1:] if opening else []) + inside


def window(directory, begin, end):
    """`covering`, straight from a platform's directory."""
    return covering(bins(directory), begin, end)


def concatenate(sources, target):
    """Join *sources* along `Location` into *target*. Returns the row count.

    Structure preserving, like `tools/obs-cull-domain.py` and for the same
    reason: a real archive's converter writes groups this has never seen, and
    the one thing worse than refusing them is dropping them silently. Nothing
    here names an observed variable.

    Written to a temporary name and renamed, so an interrupted stage leaves no
    half file that the next thing to read it would take for a whole one.
    """
    import netCDF4
    import numpy as np

    sources = [Path(path) for path in sources]
    if not sources:
        raise ArchiveError("nothing to concatenate")

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")

    handles = []
    try:
        for path in sources:
            data = netCDF4.Dataset(path)
            data.set_auto_mask(False)
            handles.append(data)
        for path, data in zip(sources, handles):
            if LOCATION not in data.dimensions:
                raise ArchiveError(
                    f"{path} has no {LOCATION} dimension, so it is not an ioda "
                    f"observation file this can join")
        rows = [len(data.dimensions[LOCATION]) for data in handles]
        epoch = _epoch(sources, handles)

        try:
            with netCDF4.Dataset(temp, "w") as out:
                _join_group(sources, handles, out, sum(rows), epoch, np)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        temp.replace(target)
    finally:
        for data in handles:
            data.close()
    return sum(rows)


def _epoch(sources, handles):
    """The instant every source's times are rebased onto: the earliest of them.

    A file with no `MetaData/dateTime` contributes no epoch, and if none of them
    has one there is nothing to rebase and this returns None. That is not a
    hypothetical tidiness: `Location` alone is a valid ioda file, and the
    concatenator is not the place to start requiring more of an archive than
    ioda does.
    """
    found = []
    for path, data in zip(sources, handles):
        variable = _time_variable(data)
        if variable is None:
            continue
        found.append(_units_epoch(path, variable))
    return min(found) if found else None


def _time_variable(data):
    group = data.groups.get(METADATA)
    if group is None:
        return None
    return group.variables.get(TIME)


def _units_epoch(path, variable):
    units = getattr(variable, "units", "")
    match = _UNITS.match(units)
    if not match:
        raise ArchiveError(
            f"{path} has {METADATA}/{TIME} with units {units!r}, which is not "
            f"'<interval> since <instant>', so its times cannot be rebased")
    interval, instant = match.group(1), match.group(2)
    if interval != "seconds":
        raise ArchiveError(
            f"{path} keeps its times in {interval} rather than seconds. ioda "
            f"writes seconds and nothing here has seen anything else, so this "
            f"refuses rather than guesses.")
    return _instant(path, instant)


def _instant(path, text):
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ArchiveError(f"{path} names its time epoch as {text!r}, which is not "
                       f"an instant this can read")


def _join_group(sources, handles, out, rows, epoch, np, path=()):
    """One group of every source, joined into *out*.

    The first source decides the structure and the rest are checked against it.
    A platform whose files disagree about their variables is a broken archive
    rather than something to paper over: the alternative is a staged file whose
    later rows are fill values for a variable the earlier ones had.
    """
    first = handles[0]
    for name_, dimension in first.dimensions.items():
        size = None if dimension.isunlimited() else len(dimension)
        out.createDimension(name_, rows if name_ == LOCATION else size)

    out.setncatts({key: first.getncattr(key) for key in first.ncattrs()})

    for name_ in first.variables:
        for source, data in zip(sources[1:], handles[1:]):
            if name_ not in data.variables:
                raise ArchiveError(
                    f"{source} has no {'/'.join(path + (name_,))} and "
                    f"{sources[0]} does, so these are not files of one platform")
        _join_variable(name_, sources, handles, out, rows, epoch, np, path)

    for name_ in first.groups:
        for source, data in zip(sources[1:], handles[1:]):
            if name_ not in data.groups:
                raise ArchiveError(
                    f"{source} has no {'/'.join(path + (name_,))} group and "
                    f"{sources[0]} does, so these are not files of one platform")
        _join_group(sources, [data.groups[name_] for data in handles],
                    out.createGroup(name_), rows, epoch, np, path + (name_,))


def _join_variable(name_, sources, handles, out, rows, epoch, np, path):
    variable = handles[0].variables[name_]
    attributes = variable.ncattrs()
    fill = variable.getncattr("_FillValue") if "_FillValue" in attributes else None
    written = out.createVariable(
        name_, variable.datatype, variable.dimensions, fill_value=fill)
    written.setncatts({key: variable.getncattr(key) for key in attributes
                       if key != "_FillValue"})

    if name_ == LOCATION and variable.dimensions == (LOCATION,):
        # ioda's row index, which numbers the file it is in rather than saying
        # anything about an observation. Renumbered rather than joined, which is
        # the one place this invents a value instead of copying one.
        written[:] = np.arange(rows)
        return

    if LOCATION not in variable.dimensions:
        # Not observation data: a `nvars` count, a scalar attribute variable.
        # The first source's copy stands for all of them.
        written[...] = variable[...]
        return

    axis = variable.dimensions.index(LOCATION)
    pieces = []
    for source, data in zip(sources, handles):
        values = np.asarray(data.variables[name_][...])
        if path == (METADATA,) and name_ == TIME and epoch is not None:
            shift = (_units_epoch(source, data.variables[name_])
                     - epoch).total_seconds()
            values = values + int(round(shift))
        pieces.append(values)
    joined = np.concatenate(pieces, axis=axis) if pieces else np.empty(0)
    if path == (METADATA,) and name_ == TIME and epoch is not None:
        written.units = f"seconds since {epoch.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    written[...] = joined


def inside(times, begin, end):
    """Which of *times* an observer would keep, given the window `(begin, end]`.

    The half open rule in one place, so that the test that pins it and anything
    that ever needs to report it are reading the same sentence. ioda applies
    this itself; nothing in ACKBAR does the cut.
    """
    return [begin < when <= end for when in times]


def count_inside(paths, begin, end):
    """How many observations in *paths* an observer would keep.

    The question "does this observer have anything this cycle" reduces to this,
    and it has to be asked of the times rather than of the filenames. Under a
    window agnostic archive the selection rule reaches back to the last bin at
    or before the window, so *some* file is nearly always found; whether
    anything in it is inside the window is a different question and it is the
    one that decides whether the observer runs.
    """
    return sum(inside(times(paths), begin, end))


#: Times already read, by path. The archive is an offline product that nothing
#: in a run writes to, and a validate pass asks the same bins the same question
#: once per cycle, so re-reading them is pure repetition. A process that
#: outlived a rebuild of the archive would hold a stale answer, and none does:
#: `validate` and `stage.obs` are each one short-lived command.
_TIMES = {}


def times(paths):
    """Every observation time in *paths*, oldest file first."""
    found = []
    for path in paths:
        key = str(path)
        if key not in _TIMES:
            _TIMES[key] = read_times(path)
        found += _TIMES[key]
    return found


def read_times(path):
    """`MetaData/dateTime` of an ioda file, as instants.

    Raises `ArchiveError` for a file that cannot be opened or has no times this
    can read, rather than answering "no observations": a corrupt file and an
    empty window are different things and the caller decides what to do about
    each.
    """
    import netCDF4
    import numpy as np

    try:
        with netCDF4.Dataset(path) as data:
            data.set_auto_mask(False)
            variable = _time_variable(data)
            if variable is None:
                return []
            epoch = _units_epoch(Path(path), variable)
            offsets = np.asarray(variable[:]).ravel()
    except OSError as error:
        raise ArchiveError(f"{path} cannot be read as an observation file: "
                           f"{error}") from error
    return [epoch + timedelta(seconds=int(offset)) for offset in offsets]
