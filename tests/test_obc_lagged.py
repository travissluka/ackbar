"""What `tools/obc-lagged.py` writes, asked of a boundary small enough to check.

The tool is exercised at tier 3 against the real domain, but that test needs
Slurm and the model and it is blocked whenever the fixture's dates drift outside
the domain's boundary window. These are the claims that need neither: run it over
a synthetic boundary through `--source` and read the files back.

Synthetic rather than a trimmed copy of the real one because the assertions here
are about arithmetic and provenance, and both are easier to state when the input
is `zeta = day` than when it is the Gulf of Mexico.
"""

import subprocess
import sys
from pathlib import Path

import netCDF4
import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "obc-lagged.py"

DAYS, LEVELS, ALONG = 40, 3, 5

#: Two globals, because the two the real files carry play different roles: one
#: names the upstream dataset and one explains a modelling choice. A tool that
#: kept only the first would pass a one-attribute test.
GLOBALS = {"source": "SYNTHETIC v1, 2015-05-28 to 2015-07-06",
           "comment": "Supergrid, as BRUSHCUTTER_MODE requires."}


@pytest.fixture(scope="module")
def boundary(tmp_path_factory):
    """One segment, one field per staggering, values that name their own day."""
    path = tmp_path_factory.mktemp("obc-source") / "obc.nc"
    with netCDF4.Dataset(path, "w", format="NETCDF4_CLASSIC") as f:
        f.setncatts(GLOBALS)
        f.createDimension("time", None)
        f.createDimension("nz_segment_001_u", LEVELS)
        f.createDimension("ny_segment_001", 1)
        f.createDimension("nx_segment_001", ALONG)

        time = f.createVariable("time", "f8", ("time",))
        time.units = "days since 2015-05-28 00:00:00"
        time.calendar = "NOLEAP"
        time.cartesian_axis = "T"
        time[:] = np.arange(DAYS, dtype="f8")

        shape = ("time", "nz_segment_001_u", "ny_segment_001", "nx_segment_001")
        for name in ("u_segment_001", "dz_u_segment_001"):
            v = f.createVariable(name, "f8", shape)
            v.units = "m s-1" if name.startswith("u") else "m"
            # Each record is its own day number, so a lag is readable in the
            # values: a member lagged +3 holds day n+3 where the base holds n.
            v[:] = np.arange(DAYS, dtype="f8")[:, None, None, None] * np.ones(
                (1, LEVELS, 1, ALONG))

        z = f.createVariable("zeta_segment_001", "f8",
                             ("time", "ny_segment_001", "nx_segment_001"))
        z[:] = np.arange(DAYS, dtype="f8")[:, None, None] * np.ones((1, 1, ALONG))
    return path


@pytest.fixture(scope="module")
def archive(tmp_path_factory, boundary):
    out = tmp_path_factory.mktemp("obc-members")
    done = subprocess.run(
        [sys.executable, str(TOOL), "--source", str(boundary),
         "--span", "6", "--members", "4", "--amplitude", "2.0",
         "--out", str(out), "synthetic"],
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return out


def read(path, name="u_segment_001"):
    with netCDF4.Dataset(path) as f:
        return np.asarray(f[name][:])


# --- provenance --------------------------------------------------------------

def test_the_upstream_globals_survive(archive):
    """A member has to be able to say which pull its water came from.

    Attributes were copied per variable and not per file, so an archive could
    not name its own source and the only record of it was a directory name.
    """
    for member in ("mem000", "mem001"):
        with netCDF4.Dataset(archive / f"{member}.nc") as f:
            got = {k: f.getncattr(k) for k in f.ncattrs()}
        for key, value in GLOBALS.items():
            assert got.get(key) == value, f"{member} lost {key}"


def test_a_member_records_its_own_lag_and_amplitude(archive):
    """The ladder is recoverable from the files, not only from the command."""
    lags = {}
    for member in sorted(p.stem for p in archive.glob("mem*.nc")):
        with netCDF4.Dataset(archive / f"{member}.nc") as f:
            note = f.getncattr("perturbation")
        assert "amplitude 2.0" in note, note
        lags[member] = note
    assert "lag +0 days" in lags["mem000"]
    # span 6 over 4 members: alternating, magnitude 6*k/2 for k in 1..2.
    assert "lag +3 days" in lags["mem001"]
    assert "lag -3 days" in lags["mem002"]
    assert "lag +6 days" in lags["mem003"]
    assert "lag -6 days" in lags["mem004"]


def test_the_coverage_is_the_trimmed_window_not_the_sources(archive):
    """The attribute that would have made a short archive diagnosable.

    A ladder cuts max|lag| off each end, so a member spans less than the file it
    came from. Without this an experiment cycling past the end learns about it
    from `time_interp_external ... is before range of list`, inside MOM6, from
    whichever PE owns a segment, minutes into a job.
    """
    with netCDF4.Dataset(archive / "mem000.nc") as f:
        start = f.getncattr("time_coverage_start")
        end = f.getncattr("time_coverage_end")
        n = len(f.dimensions["time"])
    # Source runs day 0 to 39 from 2015-05-28, so 2015-05-28 to 2015-07-06;
    # span 6 trims 6 off each end, leaving day 6 to day 33.
    assert start == "2015-06-03T00:00:00Z", start
    assert end == "2015-06-30T00:00:00Z", end
    assert n == DAYS - 12


# --- the arithmetic ----------------------------------------------------------

def test_the_members_mean_is_exactly_the_unperturbed_boundary(archive):
    """The single property the anomaly form exists to provide.

    A plain lagged ensemble's mean is a running mean of the boundary, smoother
    than the boundary. This one subtracts the member mean, so the mean is the
    base for any ladder and any amplitude.
    """
    base = read(archive / "mem000.nc")
    members = [read(archive / f"mem{i:03d}.nc") for i in range(1, 5)]
    np.testing.assert_allclose(sum(members) / len(members), base, atol=1e-10)


def test_amplitude_scales_the_departure_and_nothing_else(archive, boundary,
                                                         tmp_path_factory):
    """Twice the amplitude is exactly twice the anomaly, same structure."""
    out = tmp_path_factory.mktemp("obc-half")
    done = subprocess.run(
        [sys.executable, str(TOOL), "--source", str(boundary),
         "--span", "6", "--members", "4", "--amplitude", "1.0",
         "--out", str(out), "synthetic"],
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    base = read(archive / "mem000.nc")
    strong = read(archive / "mem001.nc") - base
    weak = read(out / "mem001.nc") - read(out / "mem000.nc")
    np.testing.assert_allclose(strong, 2.0 * weak, atol=1e-10)


def test_the_staggered_partner_is_left_alone(archive):
    """`dz_` carries the same values but is not a field to perturb.

    MOM6 reads `<var>_segment_00N` and `dz_<var>_segment_00N` and dies if their
    shapes disagree; perturbing the thickness as if it were the velocity is the
    way to get a boundary that is self-inconsistent rather than merely wrong.
    """
    base = read(archive / "mem000.nc", "dz_u_segment_001")
    for i in range(1, 5):
        np.testing.assert_array_equal(
            read(archive / f"mem{i:03d}.nc", "dz_u_segment_001"), base)
