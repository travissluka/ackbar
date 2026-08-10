"""Which byte ranges the fetcher asks for, which is where a whole file hides.

The GEFS fetcher reads GRIB index files and requests only the byte ranges of the
messages it wants. Selecting the wrong set does not fail: it downloads more and
returns the same numbers, so nothing downstream can tell. That makes it exactly
the kind of thing worth a test with no network in it.

The case these exist for is real and was found by running the fetch rather than
by reading it. Surface temperature for the operational eras comes out of a
per-lead file, and **every message in a per-lead file is at that lead**, so
selecting on the forecast hour alone keeps all of them: a hundred megabytes
pulled to read one field of about two hundred kilobytes. The parameter and level
have to be part of the selection.

No network: `message_ranges` reads a cached index if one is there, so these write
the index themselves.
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location(
        "forcing_gefs", REPO / "tools" / "forcing-gefs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gefs = pytest.importorskip("eccodes") and load()

URL = ("s3://noaa-gefs-pds/gefs.20240115/00/atmos/pgrb2bp5/"
       "gep01.t00z.pgrb2b.0p50.f003")

#: A per-lead index, cut down to shape: every message at the same forecast hour,
#: with the wanted one in the middle so that keeping it alone is visibly
#: different from keeping the file.
PER_LEAD_INDEX = "\n".join([
    "1:0:d=2024011500:HGT:1 mb:3 hour fcst:ENS=low-res ctl",
    "2:100000:d=2024011500:TMP:1 mb:3 hour fcst:ENS=low-res ctl",
    "3:200000:d=2024011500:TMP:surface:3 hour fcst:ENS=low-res ctl",
    "4:200500:d=2024011500:RH:2 m above ground:3 hour fcst:ENS=low-res ctl",
    "5:300000:d=2024011500:UGRD:10 m above ground:3 hour fcst:ENS=low-res ctl",
]) + "\n"


def _cache(tmp_path, text):
    (tmp_path / (URL.rsplit("/", 1)[-1] + ".idx")).write_text(text)
    return tmp_path


def test_the_forecast_hour_alone_selects_the_whole_per_lead_file(tmp_path):
    """The defect, pinned as behaviour so the fix cannot be undone silently.

    Not an assertion that this is wanted: it is what the hour filter does on its
    own, and it is why `match` exists. If this ever stops being true the reason
    for `match` has changed and the next test should be the one that fails.
    """
    ranges = gefs.message_ranges(URL, _cache(tmp_path, PER_LEAD_INDEX), {3})
    assert ranges == [(0, None)]


def test_the_parameter_and_level_cut_it_to_one_message(tmp_path):
    ranges = gefs.message_ranges(URL, _cache(tmp_path, PER_LEAD_INDEX), {3},
                                 gefs.BSET_MESSAGE)
    assert ranges == [(200000, 200500)]


def test_the_wanted_range_is_a_fraction_of_the_file(tmp_path):
    """The whole point, stated as the number that matters.

    Measured against the real archive at 165 kB out of 102 MB, a factor of six
    hundred. The index here is proportioned to make the same point without a
    download.
    """
    whole = gefs.message_ranges(URL, _cache(tmp_path, PER_LEAD_INDEX), {3})
    one = gefs.message_ranges(URL, _cache(tmp_path, PER_LEAD_INDEX), {3},
                              gefs.BSET_MESSAGE)
    assert whole[0][1] is None            # open ended: to the end of the file
    assert one[0][1] - one[0][0] == 500


def test_a_matched_message_that_is_not_there_is_refused_by_name(tmp_path):
    """An era whose b set stops carrying it should say so, not fetch nothing."""
    without = PER_LEAD_INDEX.replace("TMP:surface", "TMP:2 m above ground")
    with pytest.raises(SystemExit) as raised:
        gefs.message_ranges(URL, _cache(tmp_path, without), {3},
                            gefs.BSET_MESSAGE)
    assert "TMP:surface" in str(raised.value)


def test_the_per_field_layout_still_selects_on_the_hour_alone(tmp_path):
    """The reforecast is the case `match` must not disturb.

    One parameter per file and every lead in it, so the hour is the whole
    selection and adding a parameter test would be redundant. The two middle
    messages are wanted and adjacent, so they merge into one range.
    """
    index = "\n".join([
        "1:0:d=2015071200:TMP:surface:3 hour fcst:ENS=low-res ctl",
        "2:500000:d=2015071200:TMP:surface:6 hour fcst:ENS=low-res ctl",
        "3:1000000:d=2015071200:TMP:surface:9 hour fcst:ENS=low-res ctl",
        "4:1500000:d=2015071200:TMP:surface:12 hour fcst:ENS=low-res ctl",
    ]) + "\n"
    url = ("s3://noaa-gefs-retrospective/GEFSv12/reforecast/2015/2015071200/"
           "c00/Days:1-10/tmp_sfc_2015071200_c00.grib2")
    (tmp_path / (url.rsplit("/", 1)[-1] + ".idx")).write_text(index)
    ranges = gefs.message_ranges(url, tmp_path, {6, 9})
    assert ranges == [(500000, 1500000)]
