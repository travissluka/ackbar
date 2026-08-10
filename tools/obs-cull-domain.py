#!/usr/bin/env python
"""Cut an observation archive down to one domain's grid extent.

    tools/obs-cull-domain.py gom_12km \\
        --in  $ACKBAR_STATIC_ROOT/obs/rads-2015 \\
        --out $ACKBAR_STATIC_ROOT/obs/rads-2015-gom_12km

An offline stage, keyed on domain, like the gridspec and the background error:
run once against an archive, producing a domain-scoped archive that every
experiment on that domain then reads unchanged. The tree under `--out` mirrors
`--in` file for file, so nothing downstream learns a new layout: an experiment
points `obs_dir` at the culled archive instead of the global one and changes in
no other way.

**What this is owed for is not stability.** A global observation file handed to
a regional domain does not break anything: SOCA runs, every observation outside
the grid fails its `Domain Check`, and the cycle completes with an analysis that
assimilated nothing and an increment of zero. The only symptom is a number in a
log. So this exists so that the observation counts an experiment reports are the
counts it assimilated, so that a `Domain Check` rejection means what it says
rather than "on the other side of the world", and so that the archive is not
orders of magnitude larger than the domain needs.

**Why offline rather than in the cycle.** Four reasons, in order of weight. The
archive is read-only and experiments are pure consumers, so culling in-cycle
would mean a job rewriting an input on every cycle of every experiment. Two
experiments on one domain then assimilate the same observations because it is
the same file, not because two runs of the same filter agreed, which is the
argument that keeps the static stage out of the experiment. The per-cycle path
stays identical across domains, so nothing about `stage.obs` has to learn what a
domain is. And the size problem is fixed once rather than paid for repeatedly.

The cost of that placement is that nothing stops an experiment pointing at an
unculled archive, which is why `ackbar validate` refuses an experiment whose
every observer has nothing inside the domain, and why `post.obs` fails a cycle
that read observations and assimilated none of them. This tool is the fix; those
two are what make its absence loud.

## The grid's extent, and deliberately nothing else

An observation is kept when it falls inside the bounding box of the domain's own
`soca_gridspec.nc` cell centres. Not the land mask, and not the bathymetry,
**and dropping observations over land here would be a mistake**:

- The workflow is one global observation set plus one culled set per region we
  run experiments in. Keying a culled archive on the domain's *extent* alone
  means that changing that domain's mask or its topography does not invalidate
  it. Dropping land would bake the mask into the archive, and the archive would
  then silently disagree with the grid the next `tools/soca-gridspec.sh` wrote.
- SOCA's own `Domain Check` rejects a land observation at run time, which is the
  right place for it. The counts then honestly report "present but rejected"
  rather than hiding a rejection inside an offline stage nobody re-reads.

So a point in the middle of Florida survives this and is thrown out by the
analysis, on purpose. Do not add a mask test here.

The extent is read from the gridspec rather than written down a second time in a
config: the domain is already a first-class configuration axis and its grid is
the source of truth for what is inside it. `ackbar.gridspec.extent` and
`ackbar.gridspec.within` are what read it, shared with `ackbar validate` so that
the stage and the check that reports its absence cannot disagree about what is
in the domain.

Longitudes are compared after wrapping both sides into [-180, 180), for the same
reason `tools/sst-bgerr.py` wraps: the global tripolar grid stores -300 to 60
and observation files are written -180 to 180. A domain that spans the wrapped
range then admits everything, which is the safe direction to be wrong in, since
the alternative is an archive silently emptied.

## A window with nothing in it gets an empty file, not no file

This is the opposite of what `tools/obs-archive-osse.py` says, and that file is
wrong for this bundle. Its comment claims an ioda file with a zero length
`Location` is "something the observer has to survive and nothing promises it
will". The pinned ioda promises exactly that: `ReadH5File.cpp` defines the
canonical empty representation, `ReaderSinglePool.cpp:322` sets an `emptyFile_`
flag from `Location`'s dimension being zero and every later step branches on it,
`checkForRequiredVars` skips the latitude/longitude/dateTime requirement when
the file is empty, and `ObsSpace::empty()` reports every variable as present so
that nothing downstream has to ask. Both distributions are safe, RoundRobin
trivially and Halo through an explicit guard. `obs-archive-osse.py` is not
changed here: its behaviour is pinned by the committed tier 3 archive.

A present empty file says something an absent one cannot: this platform was
considered for this window and had nothing inside the domain. An absent file is
indistinguishable from a fetch that failed, and `stage.obs` treats it as a gap.

`Location` must exist in the file even when it holds no rows, because the reader
opens it unconditionally to decide emptiness. Subsetting preserves it along with
everything else, so an empty file written here declares its groups and variables
at length zero rather than being the barest thing ioda will accept. Its type is
the source archive's own, not the bundle fixture's: the fixture happens to store
`Location` as a float and the reader looks only at the dimension.

**A missing file is a different case with a different default, and the two are
not conflated.** ioda would build the identical empty space from an *absent*
file under `missing file action: warn`, and that was considered and declined: it
is a key on every observer layer, it changes what a genuinely missing archive
file means across every experiment including the global ones, and it trades a
loud missing input for a quiet one. Writing a small file has far less blast
radius than redefining absence.
"""

import argparse
import os
import sys
from pathlib import Path

import netCDF4
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

# The extent and the membership test are `ackbar.gridspec`'s, not this file's,
# because `ackbar validate` applies the same two to decide whether an
# experiment's archive has anything in its domain. Two copies that drifted apart
# would produce the exact failure this stage exists to close.
from ackbar.gridspec import extent, within  # noqa: E402

STATIC = Path(os.environ.get("ACKBAR_STATIC_ROOT", "/data/ackbar/static"))

#: ioda's location dimension, and the one this subsets along. Everything else in
#: a file is either indexed by it or is not observation data at all.
LOCATION = "Location"

#: Where an ioda file keeps the coordinates this reads.
METADATA = "MetaData"
LONGITUDE = "longitude"
LATITUDE = "latitude"


class CullError(Exception):
    pass


def read_coordinates(data):
    """An ioda file's longitude and latitude, or a reason it has none."""
    if METADATA not in data.groups:
        raise CullError(f"has no {METADATA} group, so there is nothing in it "
                        f"saying where its observations are")
    meta = data.groups[METADATA]
    for name in (LONGITUDE, LATITUDE):
        if name not in meta.variables:
            raise CullError(f"has no {METADATA}/{name}, so it cannot be culled "
                            f"to a domain")
    return (np.asarray(meta.variables[LONGITUDE][:]).ravel(),
            np.asarray(meta.variables[LATITUDE][:]).ravel())


def cull_file(source, target, box):
    """Write *target* holding only the observations of *source* inside *box*.

    Returns (kept, total). Written to a temporary name and renamed, like every
    other artifact here, so an interrupted run leaves no half archive that the
    next thing to read it would take for a complete one.
    """
    with netCDF4.Dataset(source) as data:
        data.set_auto_mask(False)
        if LOCATION not in data.dimensions:
            raise CullError(f"has no {LOCATION} dimension, so it is not an ioda "
                            f"observation file this can subset")
        total = len(data.dimensions[LOCATION])
        lon, lat = read_coordinates(data)
        if lon.size != total or lat.size != total:
            raise CullError(
                f"has {total} locations but {lon.size} longitudes and "
                f"{lat.size} latitudes, so its coordinates do not describe its "
                f"observations")
        keep = within(lon, lat, box)

        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".partial")
        try:
            with netCDF4.Dataset(temp, "w") as out:
                _copy_group(data, out, keep)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        temp.replace(target)
    return int(keep.sum()), total


def _copy_group(source, target, keep):
    """Copy a group and everything under it, subsetting along `Location`.

    Structure-preserving rather than a list of the variables an ackbar-generated
    archive happens to hold: a real archive's converter writes groups this has
    never seen, and a cull that quietly dropped them would be the worst kind of
    wrong. Nothing here names an observed variable.
    """
    for name, dimension in source.dimensions.items():
        if dimension.isunlimited():
            size = None
        elif name == LOCATION:
            size = int(keep.sum())
        else:
            size = len(dimension)
        target.createDimension(name, size)

    target.setncatts({name: source.getncattr(name) for name in source.ncattrs()})

    for name, variable in source.variables.items():
        _copy_variable(name, variable, target, keep)

    for name, group in source.groups.items():
        _copy_group(group, target.createGroup(name), keep)


def _copy_variable(name, variable, target, keep):
    # `_FillValue` is the one attribute netCDF4 will not set after creation, so
    # it is passed in and then skipped rather than copied with the rest.
    attributes = variable.ncattrs()
    fill = variable.getncattr("_FillValue") if "_FillValue" in attributes else None
    out = target.createVariable(
        name, variable.datatype, variable.dimensions, fill_value=fill)
    out.setncatts({attribute: variable.getncattr(attribute)
                   for attribute in attributes if attribute != "_FillValue"})

    if name == LOCATION and variable.dimensions == (LOCATION,):
        # ioda's location index, which is a coordinate rather than data: it
        # numbers the rows of the file it is in. Subsetting it would carry the
        # source file's numbering into a file with different rows, so it is
        # renumbered instead. This is the one variable whose values this
        # invents rather than copies.
        out[:] = np.arange(int(keep.sum()))
        return

    if LOCATION not in variable.dimensions:
        out[...] = variable[...]
        return

    axis = variable.dimensions.index(LOCATION)
    out[...] = np.compress(keep, np.asarray(variable[...]), axis=axis)


def platform_of(path):
    """The observer a file belongs to, which is its name before the date.

    `adt_j2.2015071206.nc4` is `adt_j2`. Used only to total the report by
    platform, so a naming scheme this does not recognize degrades to a coarser
    report rather than to an error.
    """
    return path.name.split(".")[0]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain", help="a domain with a gridspec, e.g. gom_12km")
    parser.add_argument("--in", dest="source", type=Path, required=True,
                        help="the archive to read, unculled")
    parser.add_argument("--out", type=Path, required=True,
                        help="the archive to write, culled to this domain")
    parser.add_argument("--gridspec", type=Path, default=None,
                        help="overrides the path derived from the domain")
    args = parser.parse_args()

    gridspec = args.gridspec or STATIC / "static" / args.domain / "soca_gridspec.nc"
    if not gridspec.exists():
        sys.exit(f"obs-cull-domain: {gridspec} does not exist. It is the "
                 f"domain's static stage, built by tools/soca-gridspec.sh.")
    if not args.source.is_dir():
        sys.exit(f"obs-cull-domain: {args.source} is not a directory")

    # Culling in place would read and truncate the same file, and the archive it
    # destroyed is the one nothing else has a copy of.
    if args.out.resolve() == args.source.resolve():
        sys.exit(f"obs-cull-domain: --in and --out are the same directory "
                 f"({args.source}). This writes a second archive beside the "
                 f"first rather than editing one in place.")

    box = extent(gridspec)
    print(f"obs-cull-domain: {args.domain} spans "
          f"{box[0]:.3f} to {box[1]:.3f} east, "
          f"{box[2]:.3f} to {box[3]:.3f} north, per {gridspec}")

    files = sorted(args.source.rglob("*.nc4"))
    if not files:
        sys.exit(f"obs-cull-domain: {args.source} holds no .nc4 files, so there "
                 f"is no archive here to cull")

    totals = {}
    empties = 0
    for path in files:
        target = args.out / path.relative_to(args.source)
        try:
            kept, total = cull_file(path, target, box)
        except CullError as error:
            sys.exit(f"obs-cull-domain: {path} {error}")
        count = totals.setdefault(platform_of(path),
                                  {"kept": 0, "total": 0, "files": 0})
        count["kept"] += kept
        count["total"] += total
        count["files"] += 1
        empties += not kept

    kept = sum(count["kept"] for count in totals.values())
    total = sum(count["total"] for count in totals.values())
    print(f"obs-cull-domain: {len(files)} file(s) -> {args.out}")
    for platform in sorted(totals):
        count = totals[platform]
        share = 100.0 * count["kept"] / count["total"] if count["total"] else 0.0
        print(f"  {platform:16s} {count['kept']:9d} of {count['total']:9d} "
              f"({share:5.1f}%) over {count['files']} file(s)")
    print(f"  {'all':16s} {kept:9d} of {total:9d} "
          f"({100.0 * kept / total if total else 0.0:5.1f}%), "
          f"{empties} file(s) with nothing in the domain")

    if not kept:
        # Every observation in the archive is outside this domain. What was just
        # written is a complete set of empty files, which an experiment would
        # read and run to completion against while assimilating nothing, so it
        # is said here rather than left for `ackbar validate` to find later.
        sys.exit(f"obs-cull-domain: nothing in {args.source} falls inside "
                 f"{args.domain}. The archive and the domain do not overlap at "
                 f"all, which usually means one of them is not what it was "
                 f"thought to be.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
