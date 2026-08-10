"""Tier 0: which entries of the diffusion calibration are zeroed over land.

`masked` is a per-entry key on `horizontal:` in `config/static/diffusion.yaml`,
defaulting to true. A correlation wants it: a zero scale is how the diffusion
operator is told a cell is not ocean. A localization does not, because it is a
taper in a Schur product and carries no state value anywhere, so masking it only
truncates the kernel at every coast and throws away the ensemble's cross-coast
structure. `loc_hz_open` is the entry that asks for no mask.

Both directions fail silently in the field, which is why they are pinned here.
An unmasked entry that gets masked anyway produces a localization that is
narrower near every coast than the file says, and the analysis it feeds looks
merely disappointing. A masked entry that stops being masked produces a
background error that communicates through land, and the calibration, the solve
and the plots all still work.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ackbar import diffusion  # noqa: E402

#: A grid coarse enough that the Rossby radius floors in places and does not in
#: others, which is the pair of regimes the floor exists for.
DX = 10.0e3


@pytest.fixture
def grid():
    """A small grid with a land block in the middle of it.

    The Rossby radius is finite over the land as well, which is what SOCA's
    gridspec actually carries: it is computed from a climatology on the whole
    array rather than only where the model has water, so an unmasked scale field
    is continuous across the coast rather than stepping to a floor at it. A
    fixture that zeroed it would be testing a grid ACKBAR does not have.
    """
    shape = (12, 16)
    rossby = np.full(shape, 40.0e3)
    rossby[:, :4] = 12.0e3      # a corner where the floor binds
    mask = np.ones(shape, dtype=bool)
    mask[4:8, 6:10] = False     # the land block
    return {
        "rossby_radius": rossby,
        "dx": np.full(shape, DX),
        "dy": np.full(shape, DX),
        "area": np.full(shape, DX * DX),
        "mask": mask,
        "dtype": np.dtype("float64"),
    }


def scales(grid, spec):
    return diffusion.horizontal_scales(grid, spec,
                                       diffusion.smoothing_scale(grid))


CORRELATION = {"rossby mult": 1.0, "min grid mult": 1.5, "max": 250.0e3}
LOCALIZATION = {"rossby mult": 1.5, "min grid mult": 2.0, "max": 350.0e3,
                "masked": False}


def test_an_entry_that_asked_for_no_mask_keeps_its_land_values(grid):
    """The whole point of `loc_hz_open`, and the thing a stray `np.where` undoes.

    Every land cell has to come back with a usable scale, not merely a nonzero
    one: the floor is applied before the mask is ever consulted, so the weakest
    true statement is that no cell anywhere is below the floor.
    """
    field = scales(grid, LOCALIZATION)
    land = field[~grid["mask"]]
    assert land.size > 0
    assert (land > 0.0).all()
    assert (field >= DX * LOCALIZATION["min grid mult"]).all()


def test_an_entry_that_did_not_ask_stays_masked(grid):
    """No key means the old behaviour, so no existing entry moved.

    Both spellings, because `masked: true` written out and `masked` left off are
    the same request and a default read the wrong way round would only show up
    on whichever of the two nobody wrote.
    """
    default = scales(grid, CORRELATION)
    explicit = scales(grid, dict(CORRELATION, masked=True))
    assert (default[~grid["mask"]] == 0.0).all()
    assert (default[grid["mask"]] > 0.0).all()
    assert np.array_equal(default, explicit)


def test_masking_changes_nothing_over_the_ocean(grid):
    """Masked and unmasked differ over land and nowhere else.

    Which is what makes `loc_hz` and `loc_hz_open` a controlled pair: the
    smoothing runs on the raw field before the mask is applied, so an ocean cell
    gets the same number either way and a run of each measures the mask. If the
    mask ever moves into the smoothing, this is what says so.
    """
    masked = scales(grid, dict(LOCALIZATION, masked=True))
    unmasked = scales(grid, LOCALIZATION)
    ocean = grid["mask"]
    assert np.array_equal(masked[ocean], unmasked[ocean])
    assert not np.array_equal(masked, unmasked)


def test_the_land_scale_is_the_ocean_scale_and_not_the_floor(grid):
    """An unmasked field is smooth across the coast, not a moat of floor values.

    A localization whose land cells sat at the grid floor would be a barrier
    dressed as an opening: the operator would still be unable to connect two
    ocean points across a headland, and it would do it quietly, since the file
    is nonzero everywhere and every check on positivity passes. What stops that
    is the gridspec's Rossby radius over land, so what is asserted is that the
    land block comes back near the surrounding open-ocean value rather than near
    `min grid mult` cells.
    """
    field = scales(grid, LOCALIZATION)
    land = field[~grid["mask"]]
    offshore = field[:, 10:][grid["mask"][:, 10:]]
    assert land.min() > 2.0 * DX * LOCALIZATION["min grid mult"]
    assert land.mean() == pytest.approx(offshore.mean(), rel=0.1)


# --- the join to SOCA's reader ------------------------------------------------

#: The model layer's own file, because the property being checked is a property
#: of that file rather than of a fixture of it.
METADATA = Path(__file__).resolve().parents[1] / "config/model/mom6sis2/fields_metadata.yaml"


def entry(name):
    import yaml
    for item in yaml.safe_load(METADATA.read_text()):
        if item["name"] == name:
            return item
    raise AssertionError(f"{name} is not in {METADATA}")


def test_the_horizontal_scale_field_travels_under_an_unmasked_variable():
    """Everything above is undone by SOCA's reader if this is not true.

    `soca_fields_read` replaces every land cell of a *masked* field with the
    field's fill value before saber sees the array, so a scale field handed over
    under a masked variable arrives masked no matter what
    `horizontal_scales` wrote. That is exactly what happened: the calibration
    named `sea_surface_height_above_geoid`, `loc_hz_open.nc` came out bit
    identical to `loc_hz.nc`, and every test above passed the whole time,
    because every one of them stops at the file.

    Two halves, and both matter. The variable has to be `masked: false`, and the
    document has to name the file under that variable's own `io file`, because
    the reader only opens a restart slot the configuration named.
    """
    field = entry(diffusion.HZ_JEDI_VARIABLE)
    assert field.get("masked") is False, (
        f"{diffusion.HZ_JEDI_VARIABLE} is masked in the fields metadata, so "
        f"SOCA will fill its land cells before saber reads them and "
        f"`masked: false` in config/static/diffusion.yaml will do nothing")
    assert field["io name"] == diffusion.HZ_VARIABLE
    assert field["io file"] == diffusion.HZ_IO_FILE


def test_the_vertical_scale_field_is_still_an_ocean_variable():
    """The other half of the same pair, which did not move and must not.

    The vertical scales are three dimensional and masked like the tracers they
    correlate, and `ackbar.run`'s per-cycle `b.corr_vt` writes the same file
    through `config/soca/vt.yaml`. A change to the horizontal names that quietly
    moved this one would put the cycled calibration and the offline one on two
    different variables.
    """
    field = entry(diffusion.VT_JEDI_VARIABLE)
    assert field["io name"] == diffusion.VT_VARIABLE
    assert field["io file"] == diffusion.VT_IO_FILE
