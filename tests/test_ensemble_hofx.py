"""The join between the two halves of a split ensemble filter.

`soca_hofx.x` runs once per member and writes one file each; `soca_letkf.x`
reads one file per observer holding every member's H(x). What is between them is
`ensemble_hofx.merge`, and the claims worth pinning are the ones that would
produce a wrong analysis rather than an error:

- the member groups are named and *ordered* the way `oops` reads them back;
- `ObsError` carries the quality control, because that is the only thing in the
  file that tells a solver running no filters what was rejected;
- files that do not describe the same observations in the same order are refused
  rather than merged row by row into something plausible.
"""

import h5py
import numpy as np
import pytest

from ackbar import ensemble_hofx
from ackbar.ensemble_hofx import MergeError, merge

VARIABLE = "absoluteDynamicTopography"
MISSING = np.float32(-3.3687953e38)


def write(path, *, hofx, error=None, qc_rejected=(), latitude=None):
    """One observation file of the shape `soca_hofx.x` leaves behind.

    Only the groups the merge touches. `EffectiveError` is missing wherever the
    filters rejected an observation, which is what oops does and is the whole
    mechanism the solver's mask rests on.
    """
    count = len(hofx)
    error = np.full(count, 0.1, dtype="f4") if error is None else np.asarray(error, "f4")
    error = error.copy()
    for index in qc_rejected:
        error[index] = MISSING
    latitude = np.arange(count, dtype="f4") if latitude is None else np.asarray(latitude, "f4")

    with h5py.File(path, "w") as ds:
        meta = ds.create_group("MetaData")
        meta.create_dataset("latitude", data=latitude)
        meta.create_dataset("longitude", data=np.arange(count, dtype="f4") * 2.0)
        meta.create_dataset("dateTime", data=np.arange(count, dtype="i8"))
        for group, values in (("hofx", np.asarray(hofx, "f4")),
                              ("EffectiveError", error),
                              ("ObsError", np.full(count, 0.1, dtype="f4")),
                              ("ObsValue", np.full(count, 1.0, dtype="f4"))):
            ds.create_group(group).create_dataset(
                VARIABLE, data=values, fillvalue=MISSING)
    return path


def read(path, group):
    with h5py.File(path) as ds:
        return ds[group][VARIABLE][:]


@pytest.fixture
def ensemble(tmp_path):
    """A reference run and three members, aligned and distinct."""
    reference = write(tmp_path / "mean.nc", hofx=[1.0, 2.0, 3.0, 4.0])
    members = [write(tmp_path / f"mem{n}.nc", hofx=[10.0 + n, 20.0 + n,
                                                    30.0 + n, 40.0 + n])
               for n in (1, 2, 3)]
    return reference, members, tmp_path / "merged.nc"


def test_each_member_lands_in_the_group_the_solver_reads_it_from(ensemble):
    """`hofx0_<n>`, numbered from one by position in the list.

    Not by ACKBAR's member index. `oops::LocalEnsembleSolver::readHofX` counts
    from 1 up to the size of the ensemble it was given, so an ensemble with a
    gap in it, which the divergence policy produces, must still be contiguous
    here. Off by one puts every member's departures against the wrong member's
    background perturbation, and the analysis still runs.
    """
    reference, members, out = ensemble
    merge(reference, members, out)
    for index, member in enumerate(members, start=1):
        assert np.array_equal(read(out, f"hofx0_{index}"), read(member, "hofx"))
    with h5py.File(out) as ds:
        assert "hofx0_4" not in ds


def test_the_prior_mean_group_is_the_reference_run(ensemble):
    """`hofx_y_mean_xb0` is H(mean(Xb)), which is what its name says.

    The solver reads it and, for an ensemble of more than one, uses it for
    nothing but a log line: it forms its departures against mean(H(Xb)), which
    it computes from the members. It has to be present all the same.
    """
    reference, members, out = ensemble
    merge(reference, members, out)
    assert np.array_equal(read(out, ensemble_hofx.PRIOR_MEAN),
                          read(reference, "hofx"))


def test_the_quality_control_reaches_the_solver_through_obserror(tmp_path):
    """The one thing in the file that says what was rejected.

    The solver runs no filters. It builds R from `ObsError` and reads a missing
    value there as "not assimilated", so the reference run's post-filter error
    has to replace the file's untouched column. Leaving the original in place
    would hand the filter every observation its own quality control threw out,
    silently.
    """
    reference = write(tmp_path / "mean.nc", hofx=[1.0, 2.0, 3.0, 4.0],
                      qc_rejected=(1, 3))
    members = [write(tmp_path / f"mem{n}.nc", hofx=[1.0, 2.0, 3.0, 4.0])
               for n in (1, 2)]
    out = merge(reference, members, tmp_path / "merged.nc")

    assert np.array_equal(read(out, "ObsError"), read(reference, "EffectiveError"))
    rejected = read(out, "ObsError")
    assert rejected[1] == MISSING and rejected[3] == MISSING
    assert rejected[0] == np.float32(0.1)


def test_members_describing_different_observations_are_refused(tmp_path):
    """The merge is by row and there is nothing to join on.

    ioda writes its output in rank-major order with no record of where each
    observation came from, so aligning the files is sound only because every
    member ran the same input on the same ranks with the same distribution.
    Nothing in the files says that, so it is checked. Without this the analysis
    would assimilate one observation's departure at another's location and
    report nothing at all.
    """
    reference = write(tmp_path / "mean.nc", hofx=[1.0, 2.0, 3.0])
    good = write(tmp_path / "mem1.nc", hofx=[1.0, 2.0, 3.0])
    moved = write(tmp_path / "mem2.nc", hofx=[1.0, 2.0, 3.0],
                  latitude=[0.0, 9.0, 2.0])
    with pytest.raises(MergeError, match="MetaData/latitude"):
        merge(reference, [good, moved], tmp_path / "merged.nc")
    assert not (tmp_path / "merged.nc").exists()


def test_a_member_with_a_different_observer_is_refused(tmp_path):
    """A member whose file holds a variable the reference's does not.

    Which means the two runs read different observer configurations, and the
    ensemble the solver would weight is not one ensemble.
    """
    reference = write(tmp_path / "mean.nc", hofx=[1.0, 2.0])
    member = tmp_path / "mem1.nc"
    write(member, hofx=[1.0, 2.0])
    with h5py.File(member, "a") as ds:
        del ds["hofx"][VARIABLE]
    with pytest.raises(MergeError, match="has no hofx/"):
        merge(reference, [member], tmp_path / "merged.nc")


def test_an_empty_ensemble_is_refused_rather_than_merged(tmp_path):
    reference = write(tmp_path / "mean.nc", hofx=[1.0, 2.0])
    with pytest.raises(MergeError, match="no member hofx"):
        merge(reference, [], tmp_path / "merged.nc")
