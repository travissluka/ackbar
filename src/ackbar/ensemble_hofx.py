"""Combine per-member departures into the one file `soca_letkf.x` reads.

The join between the two halves of a split ensemble filter. The observer half
runs `soca_hofx.x` once per member and writes one observation file per member;
the solver half is handed a single file per observer holding every member's
H(x), and `driver: read HX from disk` is what makes it read rather than compute.

What the solver looks for, from `oops::LocalEnsembleSolver::readHofX`:

===================== ========================================================
``hofx0_<n>``         member *n*'s H(x), numbered from 1 by position in the
                      solver's own member list, not by ACKBAR's member index
``hofx_y_mean_xb0``   H(mean(Xb)). Read, and for an ensemble of more than one
                      used for nothing but a log line: the departures are
                      formed against ``mean(H(Xb))``, which the solver computes
                      from the members. It has to be present all the same
``ObsError``          **the assimilation mask.** The monolithic filter fills
                      this by saving the observation error left after the
                      filters ran on H(mean(Xb)), and its missing values are
                      how a rejected observation reaches the solver, which runs
                      no filters of its own
===================== ========================================================

So the reference run, whose `EffectiveError` becomes `ObsError` here, is the one
that decides what is assimilated. It is the ensemble mean's, because that is
what `oops` uses, and matching it is what makes the split reproduce the
monolithic filter rather than merely resemble it.

h5py rather than netCDF4, which cannot open an ioda file for append: ioda writes
HDF5 the netCDF-4 library declines to reopen for writing, and the failure is an
unhelpful ``NetCDF: Can't write file``. Each member group is made by copying the
reference's own `hofx` group and overwriting the values, so ioda's dimension
scale attachments come along rather than being reconstructed from guesswork.
"""

import shutil
from pathlib import Path

import h5py
import numpy as np

#: What the observer writes, with no iteration suffix. `oops::Observer` appends
#: the `iteration` its caller set, and `HofX4D` sets none.
FORWARD = "hofx"
EFFECTIVE_ERROR = "EffectiveError"

#: What the solver reads. The trailing `0` is the solver's own iteration, which
#: is 0 for the prior.
MEMBER = "hofx0_{index}"
PRIOR_MEAN = "hofx_y_mean_xb0"
OBS_ERROR = "ObsError"

#: The columns that have to agree for a row-wise merge to mean anything. See
#: `_check_aligned`.
IDENTITY = ("dateTime", "latitude", "longitude")

#: ioda's location dimension, and the only thing an empty observation file is
#: guaranteed to hold. See `_is_empty`.
LOCATION = "Location"


class MergeError(Exception):
    pass


def _is_empty(path):
    """Whether an ioda file holds an empty observation space.

    `Location` with no rows, which is ioda's own definition: the reader opens
    that dataset unconditionally and sets its `emptyFile_` flag from the
    dimension being zero. Everything else in the file is optional at that point,
    and a file ioda wrote for an empty space holds nothing else at all.
    """
    with h5py.File(path) as data:
        if LOCATION not in data:
            # No `Location` at all is not an empty space, it is a file that is
            # not an observation file. The checks below say so far better than a
            # KeyError from the middle of the merge would.
            return False
        return data[LOCATION].shape[0] == 0


def _check_aligned(reference, members):
    """Refuse a merge whose files describe different observations.

    The member files are combined by row, and there is nothing to join on: ioda
    writes its output in rank-major order with no record of where each
    observation came from in the input. That order is deterministic given the
    same input file, distribution and rank count, which every member run has,
    so the merge is sound. It is sound by *coincidence of configuration*
    though, and nothing about the files says so.

    Hence this. It is cheap, it is exact, and it turns a silently wrong analysis
    into a job that fails with a reason.
    """
    with h5py.File(reference) as first:
        if "MetaData" not in first:
            raise MergeError(f"{reference} has no MetaData group, so it cannot "
                             f"be checked against the member files")
        keys = [key for key in IDENTITY if key in first["MetaData"]]
        if not keys:
            raise MergeError(
                f"{reference} has none of {', '.join(IDENTITY)} in MetaData, so "
                f"there is no way to tell whether the member files describe the "
                f"same observations in the same order")
        expected = {key: first["MetaData"][key][:] for key in keys}

    for path in members:
        with h5py.File(path) as member:
            for key in keys:
                if key not in member.get("MetaData", {}):
                    raise MergeError(f"{path} has no MetaData/{key} and "
                                     f"{reference} does")
                if not np.array_equal(expected[key], member["MetaData"][key][:]):
                    raise MergeError(
                        f"{path} and {reference} disagree on MetaData/{key}, so "
                        f"they do not hold the same observations in the same "
                        f"order and cannot be merged row by row. Every member's "
                        f"hofx has to run the same input file with the same "
                        f"distribution on the same number of ranks.")


def merge(reference, members, out):
    """Write *out*, the ensemble departure file, from one file per member.

    *reference* is the ensemble mean's run, which supplies everything but the
    members: the observations themselves, the metadata, and the observation
    error that carries the quality control. *members* is one path per member in
    the order the solver will list them.
    """
    members = list(members)
    if not members:
        raise MergeError("no member hofx files, so there is no ensemble to "
                         "assimilate")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # **An observer with no observations in this window is routine, and every
    # check below would refuse it.** A domain-scoped archive produces empty
    # files by design: a satellite that did not pass over the domain this cycle
    # contributes a file with no rows, and `tools/obs-cull-domain.py` writes one
    # rather than omitting it so that "considered, saw nothing" is
    # distinguishable from "the fetch failed".
    #
    # Nothing upstream filters those out, and it is worth saying why, because
    # the filters look like they would. `observations.selected` keeps an
    # observer when its input file is `present`, which is a question about the
    # filesystem, and an empty file exists. So the observer is staged, hofx runs
    # it, `soca.ensemble_departures` reaches this, and every one of the three
    # checks below fires on a file that is exactly as it should be:
    # `_check_aligned` on the absent `MetaData`, then `FORWARD`, then
    # `EFFECTIVE_ERROR`, all absent because `put_db` is a no-op on an empty
    # space. The `MergeError` becomes a `ModelError` becomes a `TaskError`, the
    # loop over observers does not continue, and one empty platform takes the
    # whole cycle's departures with it.
    #
    # So it returns the reference unchanged. That is not a shortcut around the
    # merge, it is the merge: `ObsSpace::empty()` reports every variable as
    # present, so the solver reads `hofx0_n`, `hofx_y_mean_xb0` and `ObsError`
    # out of an empty space and gets zero-length vectors for all of them,
    # exactly as it would from a merged file that had been built row by row out
    # of nothing.
    empty = [_is_empty(path) for path in (reference, *members)]
    if all(empty):
        shutil.copy(reference, out)
        return out
    if any(empty):
        # Not a case to pick a branch for. Every member runs the same input file
        # through the same distribution on the same rank count, so their spaces
        # are empty together or not at all. One of each means the member files
        # are not what they are believed to be, and a merge that silently took
        # either branch would turn that into an analysis nobody could question.
        names = [str(path) for path, blank
                 in zip((reference, *members), empty) if blank]
        raise MergeError(
            f"{len(names)} of {len(empty)} files hold an empty observation "
            f"space and the rest do not: {', '.join(names)}. Every member "
            f"evaluates the same observations, so they are empty together or "
            f"not at all, and a mixture means these files do not describe one "
            f"observer's window.")

    _check_aligned(reference, members)

    shutil.copy(reference, out)
    with h5py.File(out, "a") as merged:
        if FORWARD not in merged:
            raise MergeError(f"{reference} has no {FORWARD} group, so the "
                             f"observer did not run or did not save its H(x)")
        simulated = list(merged[FORWARD])
        for index, path in enumerate(members, start=1):
            group = MEMBER.format(index=index)
            merged.copy(FORWARD, group)
            with h5py.File(path) as member:
                for name in simulated:
                    if name not in member[FORWARD]:
                        raise MergeError(
                            f"{path} has no {FORWARD}/{name}, which "
                            f"{reference} does: the member ran a different "
                            f"observer configuration")
                    merged[group][name][:] = member[FORWARD][name][:]
        merged.copy(FORWARD, PRIOR_MEAN)

        # The solver builds R from `ObsError` and reads its missing values as
        # "not assimilated". What carries that is the reference run's
        # post-filter error, so it replaces the file's own untouched column.
        if EFFECTIVE_ERROR not in merged:
            raise MergeError(f"{reference} has no {EFFECTIVE_ERROR} group, so "
                             f"nothing in it says which observations passed "
                             f"quality control")
        for name in simulated:
            merged[OBS_ERROR][name][:] = merged[EFFECTIVE_ERROR][name][:]

        # **The bare `hofx` group has to go, and this is not tidiness.** The
        # solver writes the whole obs space back out, so anything left here
        # appears in the committed departure file, and `post.obs` prefers an
        # indexed forward operator over `ombg`/`oman` when it finds one: a lone
        # `hofx` would make it report O-B as `ObsValue - H(mean(Xb))` and no O-A
        # at all. The filter's own departure is `ObsValue - mean(H(Xb))`, which
        # is a different quantity, and the substitution is silent.
        #
        # Nothing is lost. This group is H(mean(Xb)), which is exactly what
        # `hofx_y_mean_xb0` above now holds under the name that says so.
        del merged[FORWARD]
    return out
