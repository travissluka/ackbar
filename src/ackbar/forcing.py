"""Atmospheric forcing: the one file shape every source is normalized into.

ACKBAR's forcing archive holds one `atm.nc` per member per source, clipped to a
padded box around the domain's own grid and carrying the seven fields
MOM6-SIS2's data table reads. Sources differ in almost everything upstream of
this file and in
nothing downstream of it, which is the point: **the data table does not vary by
source.** One `data_table` names `INPUT/atm.nc` and the seven variables below,
and swapping ERA5 for GEFS is swapping which file the symlink points at.

That normalization is what keeps the per-member staging mechanism dumb, and it
is why there is no `data_table.<source>`: one `data_table.atm` serves every
source, and a member selects a *file* through `ensemble.inputs` rather than a
config variant. What a source does still set is `model.override.SIS_forcing`, a
fourth SIS parameter file holding `ADD_DIURNAL_SW = False`, because how often a
source reports shortwave is a property of the source. It is a separate file
rather than a line in the domain's `SIS_override` because that file carries
`NIGLOBAL` and `NJGLOBAL`, which must not be replaced.

## The field names are soca-science's

`T2 Q2 U10 V10 DSWRF DLWRF PRATE`, which is what both prior workflows used and
what `config/model/mom6sis2/domain/gom/common/data_table.atm` already names. No
reason to rename them, and one reason not to: a data table copied from either
prior workflow keeps working.

## The scalars are shifted to the height the data table declares

`data_table.atm` gives the coupler one `z_bot` for the whole atmospheric state,
and the products publish temperature and humidity at 2 m and winds at 10 m, so
one of the two is always mislabelled. The archive resolves it on the way in:
`shift_to_10m` moves the scalars up, `z_bot = 10` becomes true of all four
fields, and the ten per cent turbulent flux bias goes with it. That is what
CORE, JRA55-do and NWA12 all do, and it belongs here rather than in the model
because the correction tolerates an approximate surface temperature.

The height is recorded on the file as `HEIGHT_ATTRIBUTE` and checked at stage
time by `assert_reference_height`. Recorded rather than assumed: `T2` and `Q2`
keep their names because `--no-height-shift` can still build an archive that
really does hold 2 m fields, and a file that says which it is can be checked
where a file named `T10` could only be trusted.

## Surface pressure is fetched and not written

MOM6 here applies no atmospheric pressure load. The data table sets `p_surf` and
`p_bot` to a constant 1e5, both prior workflows did the same, and nothing in the
gom `MOM_input` reads a real one. So surface pressure is read from the source
where a humidity conversion needs it and then dropped rather than archived. If a
pressure load is ever wanted, that is a refetch, and the refetch is cheap.

## The calendar is the model's, not the source's

`coupler_nml` runs `NOLEAP`. FMS resolves a forcing file's time axis with the
calendar the file names, so a file stamped `julian` against a `NOLEAP` model is
offset by every leap day since year zero, which is about five hundred days and
fails loudly rather than subtly. Both prior workflows ran `julian` on both sides
and were consistent for that reason, not because `julian` is right here.

`NOLEAP` has no 29 February, so an archive spanning one is a real hazard: the
source has a day the model cannot name. `assert_no_leap_day` refuses to build one
rather than letting the dates slide by a day from March onwards.

## Every field carries its own time axis

`time_T2`, `time_DSWRF`, and so on, each an unlimited dimension of its own, which
netCDF4 permits and netCDF3 does not. That is soca-science's shape and it ran
that way for years, so it is proven against FMS rather than inferred.

It matters because instantaneous fields and interval means are honestly stamped
at different times. Temperature at 12:00 is the temperature at 12:00; downwelling
shortwave "at 12:00" is the mean over an hour and belongs at its midpoint. Forced
onto one axis, one of the two has to be interpolated, and it is always the
shortwave that pays: `tools/spike-gefs-atm.py` says so in its own docstring and
smears the diurnal cycle it was built to resolve. With per-field axes neither is
interpolated and neither is wrong.
"""

import datetime
import os
import re
from pathlib import Path

import netCDF4
import numpy as np

#: Degrees of forcing pulled beyond the domain edge.
#:
#: The margin is not decoration. FMS interpolates the forcing onto the model grid
#: with `bilinear` and `bicubic`, and a bicubic stencil reaches two source cells
#: past the point it is filling, so a box ending at the domain edge leaves the
#: outermost row of the model reading from nothing. Four degrees is sixteen ERA5
#: cells and four cells of the coarsest GEFS era, which is the one that sets the
#: floor. `fetch-glorys.py` makes the same argument for the same reason and calls
#: it `MARGIN` too.
MARGIN = 4.0

#: Output name -> (units, long name). The order is the order they are written.
#:
#: The two scalars do not name a height and the winds do. That is not an
#: oversight: the winds are at 10 m however the file was built, while the
#: scalars are at 2 m as published and at 10 m once shifted, so their height is
#: a property of the build rather than of the field. It is written onto the file
#: as `HEIGHT_ATTRIBUTE` instead of baked into a name. Renaming them `T10`/`Q10`
#: would be a lie in exactly the case `--no-height-shift` exists to serve.
FIELDS = {
    "T2":    ("K",             "near surface air temperature"),
    "Q2":    ("kg kg-1",       "near surface specific humidity"),
    "U10":   ("m s-1",         "10 metre eastward wind"),
    "V10":   ("m s-1",         "10 metre northward wind"),
    "DSWRF": ("W m-2",         "surface downwelling shortwave radiation flux"),
    "DLWRF": ("W m-2",         "surface downwelling longwave radiation flux"),
    "PRATE": ("kg m-2 s-1",    "total precipitation rate"),
}

#: The scalars whose height the shift moves, and which carry a `height`
#: attribute of their own.
SCALARS = ("T2", "Q2")

#: What the model's `coupler_nml` runs. See the module docstring.
CALENDAR = "NOLEAP"

#: The height every domain's `data_table.atm` declares the whole atmospheric
#: state at, via `z_bot`, and the height the shift moves the scalars to.
REFERENCE_HEIGHT = 10.0

#: The height GEFS and ERA5 publish temperature and humidity at. Neither
#: publishes them at `REFERENCE_HEIGHT`, and neither publishes winds at this
#: one, so the mismatch is forced by the products rather than by a fetch choice.
PRODUCT_HEIGHT = 2.0

#: How far the shift may move a column and still be believed.
#:
#: An acceptance envelope, not a bound applied during the solve. The inversion
#: is a fixed point iteration that is **not** globally convergent: over a
#: surface colder than the air the 2 m to 10 m profile is steep enough that the
#: under-relaxed step overshoots and grows, and a real GEFS column walks 305 K
#: to 402 to 1152 to 6942 to a NaN in six passes. A correction that ends outside
#: this envelope was not determined, and the column keeps its 2 m values.
#:
#: **The two are not the same distance out, and that was measured rather than
#: assumed.** Temperature moves 0.16 to 0.97 K in the documented cases and 1.28
#: K at the extreme of a measured field, so 5 K is comfortably outside it.
#: Humidity is not: at a 5 g/kg bound the largest correction admitted was
#: 4.82 g/kg, meaning the bound had become binding and was deciding answers
#: rather than catching runaways. It is set above any physical specific humidity
#: instead, which lets those columns through at up to 9.7 g/kg. Divergence in
#: humidity always arrives with divergence in temperature, so the temperature
#: bound is what catches it and the humidity one only has to stay out of the
#: way.
#:
#: **Over open water the choice makes no difference at all**: a deep central
#: Gulf window is identical under both, dT -0.3189 to +0.1239 K either way. The
#: columns the loosening admits are stable land points, where the correction is
#: large, unreliable, and read by nothing, since MOM6 takes forcing only over
#: ocean.
MAX_TEMPERATURE_SHIFT = 5.0
MAX_HUMIDITY_SHIFT = 2.0e-2

#: How still the last iterate has to be for a column to count as solved.
#:
#: Measured, not guessed, and the first value tried was wrong. Across a swept
#: grid of 236 physical states the median final step is 2e-8 K and the largest
#: on a column that genuinely solves is 2.6e-4 K, so 1e-4 rejected a good
#: column. This is forty times looser than that worst case and still two orders
#: of magnitude below the smallest correction the shift makes anywhere (0.16 K),
#: so it cannot reject a real correction and it still catches a column that is
#: still moving. It is a backstop: the clamp is what identifies divergence.
SETTLED = 1.0e-2

#: Global attribute naming the height `T2` and `Q2` actually sit at.
#:
#: Recorded rather than assumed, and checked for presence rather than for a
#: particular value. An archive built before the shift existed carries no such
#: attribute and is refused; an archive built deliberately unshifted carries
#: `PRODUCT_HEIGHT` and is allowed. Asserting `REFERENCE_HEIGHT` here would make
#: the second case unrunnable, which is the case `--no-height-shift` exists for.
HEIGHT_ATTRIBUTE = "scalar_reference_height"


def assert_no_leap_day(start, end):
    """Refuse a span containing 29 February, which the model cannot name."""
    for year in range(start.year, end.year + 1):
        try:
            leap = datetime.datetime(year, 2, 29)
        except ValueError:
            continue
        if start <= leap <= end:
            raise SystemExit(
                f"forcing: {start:%Y-%m-%d} to {end:%Y-%m-%d} contains "
                f"{leap:%Y-%m-%d}, which does not exist in the model's NOLEAP "
                f"calendar. Choose a span inside one non-leap year, or teach "
                f"the model a calendar that has the day.")


def specific_humidity(dewpoint, pressure):
    """Specific humidity from dewpoint temperature and surface pressure.

    ECMWF's own saturation formulation over water (IFS documentation, the Tetens
    form Buck fitted), applied at the dewpoint, where by definition the vapour
    pressure equals the saturation vapour pressure.

    Over water rather than the mixed water-and-ice form on purpose: the box is
    the Gulf of Mexico in summer and the ice branch would never be taken, so the
    simpler expression is the honest one to read.

    *dewpoint* in K, *pressure* in Pa, result in kg/kg.
    """
    vapour = 611.21 * np.exp(17.502 * (dewpoint - 273.16) / (dewpoint - 32.19))
    ratio = 0.621981  # Rdry / Rvap
    return ratio * vapour / (pressure - (1.0 - ratio) * vapour)


# --- The height shift -------------------------------------------------------
#
# `data_table.atm` declares one `z_bot` for the whole atmospheric state and the
# products publish scalars at 2 m and winds at 10 m, so one of the two is always
# mislabelled. Declaring the 2 m scalars at 10 m understates the air-sea
# contrast and costs about ten per cent of the turbulent cooling in one
# direction; declaring the state at 2 m instead mislabels a true 10 m wind and
# costs 28 to 56 per cent on the stress, which is a worse trade. So the scalars
# are shifted to 10 m when the archive is built and `z_bot = 10` becomes true.
# CORE, JRA55-do and MOM6-COBALT-NWA12 all do the same thing.
# `docs/forcing-reference-height.md` carries the measurement and the argument.
#
# The routine below is a vectorized port of `shift_up` in `tools/flux-height.py`,
# which is itself a transcription of `ncar_ocean_fluxes` in
# `pkg/mom6sis2/src/coupler/surface_flux.F90`, the flux path ACKBAR runs. The
# scalar original stays where it is and stays the reference: it is stdlib only
# and readable against the Fortran, and `tests/test_forcing_height.py` holds this
# port to it. Two implementations of one algorithm is the point, not duplication.

#: Von Karman, gravity, and the dry gas constant, as `surface_flux.F90` has them.
_K, _G, _RD = 0.4, 9.8, 287.04

#: The pressure the shift is computed at.
#:
#: A constant rather than the source's own surface pressure, for two reasons.
#: It matches the scalar reference exactly, so the test can compare them without
#: a pressure field to thread through. And the shift is a small term whose own
#: error is a small fraction of it: an SST wrong by a whole kelvin leaves 2 to 7
#: W m-2 against the 38 to 105 W m-2 it removes, and the pressure dependence
#: here is far weaker than that. The GEFS reforecast does not fetch surface
#: pressure at all (its humidity arrives as specific), so a real pressure would
#: mean a whole extra field for a correction to a correction.
_PRESSURE = 101325.0


def _qsat(temperature, pressure=_PRESSURE):
    """Saturation specific humidity, the form `surface_flux.F90` uses."""
    vapour = 611.2 * np.exp(17.67 * (temperature - 273.15)
                            / (temperature - 29.65))
    return 0.622 * vapour / (pressure - 0.378 * vapour)


def _psi(zeta, kind):
    """Monin-Obukhov stability function, momentum (`m`) or scalar (`h`).

    Both branches are evaluated everywhere and selected elementwise, which is
    what vectorizing the scalar original's `if` costs. The unstable expression
    is finite for stable `zeta` too, because `x2` is floored at one, so the
    unused branch cannot produce a warning or a NaN that `np.where` then carries.
    """
    x2 = np.maximum(np.sqrt(np.abs(1.0 - 16.0 * zeta)), 1.0)
    x = np.sqrt(x2)
    if kind == "m":
        unstable = (np.log((1.0 + 2.0 * x + x2) * (1.0 + x2) / 8.0)
                    - 2.0 * (np.arctan(x) - np.arctan(1.0)))
    else:
        unstable = 2.0 * np.log((1.0 + x2) / 2.0)
    return np.where(zeta > 0.0, -5.0 * zeta, unstable)


def _similarity(wind, t, ts, q, qs, height, iterations=6):
    """`ncar_ocean_fluxes`, returning the scales rather than the fluxes.

    *wind*, *t* and *q* are all declared at *height*. The model iterates twice;
    six is used here, as in the scalar reference, so the answer is the
    algorithm's converged one rather than a statement about its iteration count.
    """
    tv = t * (1.0 + 0.608 * q)
    wind = np.maximum(wind, 0.5)
    at10 = wind
    cd_n10 = (2.7 / at10 + 0.142 + 0.0764 * at10) / 1e3
    rt = np.sqrt(cd_n10)
    ce_n10 = 34.6 * rt / 1e3
    stab = np.where(t > ts, 1.0, 0.0)
    ch_n10 = (18.0 * stab + 32.7 * (1.0 - stab)) * rt / 1e3
    cd, ch, ce = cd_n10, ch_n10, ce_n10
    offset = np.log(height / 10.0)

    ustar = tstar = qstar = zeta = None
    for _ in range(iterations):
        cd_rt = np.sqrt(cd)
        ustar = cd_rt * wind
        tstar = (ch / cd_rt) * (t - ts)
        qstar = (ce / cd_rt) * (q - qs)
        bstar = _G * (tstar / tv + qstar / (q + 1.0 / 0.608))
        zeta = _K * bstar * height / (ustar * ustar)
        zeta = np.copysign(np.minimum(np.abs(zeta), 10.0), zeta)
        pm, ph = _psi(zeta, "m"), _psi(zeta, "h")
        at10 = wind / (1.0 + rt * (offset - pm) / _K)
        cd_n10 = (2.7 / at10 + 0.142 + 0.0764 * at10) / 1e3
        rt = np.sqrt(cd_n10)
        ce_n10 = 34.6 * rt / 1e3
        stab = np.where(zeta > 0.0, 1.0, 0.0)
        ch_n10 = (18.0 * stab + 32.7 * (1.0 - stab)) * rt / 1e3
        xx = (offset - pm) / _K
        cd = cd_n10 / (1.0 + rt * xx) ** 2
        xx = (offset - ph) / _K
        ch = ch_n10 / (1.0 + ch_n10 * xx / rt) ** 2
        ce = ce_n10 / (1.0 + ce_n10 * xx / rt) ** 2
    # The scales are the ones formed at the top of the last pass, from the
    # coefficients of the pass before, which is what the scalar reference
    # returns. Recomputing them from the final coefficients would be a different
    # number and the test against the reference would catch it.
    return ustar, tstar, qstar, zeta


def _profile_down(wind, t10, q10, ts, qs, height):
    """The scalars at *height*, given a 10 m state and the surface under it.

    The roughness length is never formed: writing the profile at both heights
    and differencing eliminates it, so only the 10 m state and the stability are
    needed.
    """
    _ustar, tstar, qstar, zeta = _similarity(wind, t10, ts, q10, qs, 10.0)
    safe = np.where(zeta == 0.0, 1.0, zeta)
    length = np.where(zeta == 0.0, 1e9, 10.0 / safe)
    shape = (np.log(height / 10.0) - _psi(height / length, "h")
             + _psi(zeta, "h")) / _K
    return t10 + tstar * shape, q10 + qstar * shape


def shift_to_10m(t2, q2, wind, ts, iterations=12, relaxation=0.8):
    """Temperature and humidity moved from 2 m to 10 m, the archive's fix.

    *wind* is the 10 m wind speed, *ts* the surface temperature the profile was
    built over. Inverts the Monin-Obukhov profile: guess a 10 m state, integrate
    it down to 2 m, and correct the guess by what it missed.

    Under-relaxed rather than solved, following the scalar reference, because it
    is called over a whole domain including columns where the surface
    temperature is only approximately the one the source's own profile was built
    over, and it has to stay well behaved there rather than converge fastest.

    **Which surface temperature is a physics point, not a convenience one.** It
    must be the source's own, from the same stream the rest of the forcing comes
    from: the product's 2 m fields were produced by its surface layer over its
    surface temperature, so inverting that profile is self-consistent only
    against the surface the profile was built over. An independent ocean
    analysis puts a stability into the inversion the product never saw.
    JRA55-do took it from JRA-55's own fields for the same reason.

    The shift tolerates an approximate surface temperature, which is what lets
    it live in the archive rather than in a patched `surface_flux.F90`: one
    kelvin of error in *ts* leaves 2 to 7 W m-2 against the 38 to 105 W m-2 the
    shift removes.

    Returns `(t10, q10, undetermined)`, where *undetermined* counts the columns
    the inversion could not solve and which were therefore **left at their 2 m
    values**. That count is returned rather than logged because a silent
    fallback is the thing worth refusing: it is a small reintroduction of the
    bias this function exists to remove, and it has to be visible to whoever
    builds the archive.

    **The iteration is not globally convergent, and real fields reach the
    corner where it is not.** Air over a surface colder than itself makes the
    2 m to 10 m profile steep enough that the step overshoots and grows without
    bound. More iterations do not rescue it: 12, 40 and 120 passes leave 656,
    656 and 657 unsolved columns of the same measured field.

    Measured over the Gulf box in January, six to nine per cent of columns land
    there, and **none of them are open water**: a deep central Gulf window is
    0 of 325 at every iteration count. The box is padded four degrees past the
    domain and the stable columns in it are land at night, which MOM6 does not
    read. Monin-Obukhov similarity is unreliable in that regime anyway and the
    fluxes there are near zero, so declining to shift is the honest answer where
    the correction is not determined, and it is strictly better than the NaN
    that reaches the archive otherwise.
    """
    t2 = np.asarray(t2, dtype="f8")
    q2 = np.asarray(q2, dtype="f8")
    qs = 0.98 * _qsat(ts)
    t10, q10 = t2.copy(), q2.copy()
    low_t, high_t = t2 - MAX_TEMPERATURE_SHIFT, t2 + MAX_TEMPERATURE_SHIFT
    # A negative specific humidity is unphysical and would reach the model as
    # one, so the lower bound is a floor as well as a clamp.
    low_q = np.maximum(q2 - MAX_HUMIDITY_SHIFT, 0.0)
    high_q = q2 + MAX_HUMIDITY_SHIFT
    moved = np.full(np.shape(t2), np.inf)

    # The iteration runs free. A diverging column overflows and then takes the
    # square root of a negative drag coefficient; that is caught below, and the
    # warnings it prints on the way are noise because the answer is discarded.
    #
    # Clamping each iterate into the envelope as it went was tried and dropped
    # because it earns nothing: judged by the same final rule, free and clamped
    # iteration accept 154036 and 154035 of 200000 sampled columns, differ on
    # nine of them, and agree to 1.2e-3 K everywhere both accept. One test of
    # the envelope, at the end, is the whole of it.
    with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
        for _ in range(iterations):
            guess_t, guess_q = _profile_down(wind, t10, q10, ts, qs,
                                             PRODUCT_HEIGHT)
            new_t = t10 + relaxation * (t2 - guess_t)
            new_q = q10 + relaxation * (q2 - guess_q)
            moved = np.abs(new_t - t10)
            t10, q10 = new_t, new_q

    # Judged on where the column ended. The envelope is the acceptance test and
    # nothing else: a correction outside it was not determined, whatever the
    # arithmetic did to arrive at it. The lower humidity bound is never below
    # zero, so a column driven negative is rejected here rather than needing a
    # floor of its own.
    #
    # NaN and inf fail every comparison, so they land in `~settled` without
    # their own test. The step size is a backstop for a column drifting too
    # slowly to leave the envelope inside the iteration count.
    inside = ((t10 > low_t) & (t10 < high_t)
              & (q10 > low_q) & (q10 < high_q))
    settled = inside & (moved <= SETTLED)
    return (np.where(settled, t10, t2), np.where(settled, q10, q2),
            int(np.count_nonzero(~settled)))


def regrid_linear(lon, lat, plane, to_lon, to_lat):
    """*plane* interpolated bilinearly from one rectilinear grid onto another.

    Only the operational GEFS eras need this, and only for the surface
    temperature: it is the one field absent from the 0.25 degree set the fetcher
    reads and present only in the b set, which is half degree. Everything else
    arrives on the grid it is used on.

    Bilinear rather than nearest neighbour, for the reason FMS itself
    interpolates the rest of the forcing bilinearly. On a half-to-quarter
    mapping every other target point coincides exactly with a source point, so
    nearest neighbour would stamp a half degree checkerboard onto a field that
    feeds the stability calculation, and a grid-scale artifact in a forcing term
    is the kind of thing that reads as a bug long afterwards. The cost is the
    same.

    Longitude is periodic and latitude is not: a target between the last source
    column and the first wraps, and a target past the outermost row clamps,
    which is what a pole-adjacent point should get.
    """
    lon, lat = np.asarray(lon, dtype="f8"), np.asarray(lat, dtype="f8")
    plane = np.asarray(plane, dtype="f8")
    if lat.size > 1 and lat[0] > lat[-1]:
        lat, plane = lat[::-1], plane[::-1, :]
    order = np.argsort(lon)
    lon, plane = lon[order], plane[:, order]
    rows = np.stack([np.interp(to_lon, lon, row, period=360.0)
                     for row in plane])
    return np.stack([np.interp(to_lat, lat, column) for column in rows.T],
                    axis=1)


#: What a data table row names as its file, `"INPUT/atm.nc"` in the fourth
#: column. Quoted, and the table is whitespace-and-comma separated with no
#: escaping, so a regex over the whole line is enough and a parser is not.
#:
#: Lives here rather than in `validate.py`, which asks a different question of
#: the same column (that something supplies each name) and imports this. One
#: expression, because two copies of it would have to stay in step.
TABLE_FILE = re.compile(r'"INPUT/([^"/]+)"')


def table_files(path):
    """Every name under `INPUT/` that a data table reads, deduplicated."""
    return sorted(set(TABLE_FILE.findall(Path(path).read_text())))


def assert_reference_height(path):
    """Refuse a staged `atm.nc` that does not say what height its scalars are at.

    The case this exists for is an archive built before the shift did, which
    carries 2 m scalars, no attribute, and no way to tell from the file. Staged
    against a data table declaring `z_bot = 10` it reintroduces the flux bias
    silently and the run reports success the whole way through.

    Presence is the check, not a particular value. An archive built deliberately
    with `--no-height-shift` records `PRODUCT_HEIGHT` and is allowed to run:
    that is a stated choice about a known bias, and asserting `REFERENCE_HEIGHT`
    here would make the choice unrunnable. The file records what it is and this
    refuses only a file that will not say.
    """
    with netCDF4.Dataset(path) as f:
        if HEIGHT_ATTRIBUTE not in f.ncattrs():
            raise ValueError(
                f"{path} has no {HEIGHT_ATTRIBUTE} attribute, so the height "
                f"its T2 and Q2 sit at is unknown and the data table's "
                f"z_bot cannot be trusted. An archive built before the height "
                f"shift existed looks exactly like this and carries a ten per "
                f"cent turbulent flux bias. Rebuild it with "
                f"tools/forcing-gefs.py or tools/forcing-era5.py, or rebuild "
                f"it with --no-height-shift to keep the old behaviour on "
                f"purpose. See docs/forcing-reference-height.md.")
        return float(f.getncattr(HEIGHT_ATTRIBUTE))


def domain_box(domain, margin=MARGIN):
    """The clip box for *domain*, read from its supergrid.

    Not a constant, because a constant is a second statement of where a domain is
    and the two drift. `ocean_hgrid.nc` is where a domain says what it covers,
    every offline stage that needs an extent already reads it (`fetch-glorys.py`
    builds its GLORYS request the same way), and a domain whose grid moves gets a
    forcing box that moves with it.

    The corollary is that **the box belongs to a domain, not to a family**, so the
    archive is keyed by domain: the four Gulf resolutions do not have quite the
    same footprint (`gom_25km` starts at 17.995N, `gom_12km` at 18.058N) and
    pretending one fetch serves all four means one of them silently reads a box
    that stops inside it.

    Returns the box in the source's own coordinates: degrees east on [0, 360) and
    degrees north.
    """
    root = os.environ.get("ACKBAR_STATIC_ROOT")
    if not root:
        raise SystemExit("forcing: run `source site/activate.sh` first")
    path = Path(root, "domain", domain, "INPUT", "ocean_hgrid.nc")
    if not path.exists():
        raise SystemExit(
            f"forcing: {path} does not exist, so {domain}'s extent is unknown. "
            f"The domain's grid is built by its own offline stage and has to "
            f"exist before forcing can be clipped to it.")
    with netCDF4.Dataset(path) as f:
        return box_around(np.asarray(f["x"][:]), np.asarray(f["y"][:]), margin)


def lon_extent(lon):
    """(west, east) in [0, 360) covering *lon*, going the short way round.

    `min` and `max` cannot do this, and the difference is not academic. A grid
    that wraps through zero holds both 0.1 and 359.9, so its min/max extent is
    the whole globe: a Gulf of Guinea domain from 350E to 10E, or any domain
    straddling the antimeridian on a [-180, 180) grid, would be classified as
    global and fetched as one, which is 290 MB of ERA5 becoming about 100 GB.

    The extent is the complement of the largest gap between adjacent
    longitudes. For a grid that genuinely covers the globe that gap is one cell
    and the extent is everything; for a wrapped regional grid it is the ocean of
    longitudes the domain does not touch, and the returned west is greater than
    the returned east.
    """
    values = np.unique(np.asarray(lon).ravel() % 360.0)
    if values.size < 2:
        return float(values[0]), float(values[0])
    gaps = np.diff(values)
    across_zero = 360.0 - (values[-1] - values[0])
    if across_zero >= gaps.max():
        return float(values[0]), float(values[-1])
    cut = int(np.argmax(gaps))
    return float(values[cut + 1]), float(values[cut])


def box_around(lon, lat, margin=MARGIN):
    """A padded box around supergrid coordinates *lon*, *lat*, in [0, 360).

    Takes the extent rather than the corners, so a curvilinear or rotated grid
    gets the box that contains it rather than a box through four of its points.
    """
    west, east = lon_extent(lon)
    covered = (east - west) % 360.0
    south, north = float(lat.min()) - margin, float(lat.max()) + margin
    if covered + 2.0 * margin >= 360.0:
        west, east = 0.0, 360.0
    else:
        west, east = (west - margin) % 360.0, (east + margin) % 360.0
        if west >= east:
            # A box straddling where the source's longitude axis wraps is two
            # slices, not one, and every caller here reads one. Refused rather
            # than half-handled: a global domain takes the branch above, and no
            # regional domain in this repository crosses it.
            raise SystemExit(
                f"forcing: the box {west:.3f} to {east:.3f} east crosses the "
                f"source's longitude seam, which the single-slice clip cannot "
                f"express. A domain that needs this needs two reads joined.")
    south, north = max(south, -90.0), min(north, 90.0)
    return {"west": west, "east": east, "south": south, "north": north}


def box_slices(lons, lats, box):
    """Where *box* sits in a source grid, as slices, plus the axes it selects.

    Returns `(slice_y, slice_x, y, x, flip_y)`. Slices rather than index arrays
    so that a caller can read the box straight out of a netCDF variable instead
    of reading the globe and throwing it away: an ERA5 month is 1.5 GB globally
    and 15 MB over the Gulf, and the difference is whether this runs in memory.

    *flip_y* says the source hands latitude down from the pole. Sources do that
    about as often as not, and reading a descending axis as ascending flips the
    field about the equator, which in this box is a plausible looking wind
    blowing the wrong way rather than an error.
    """
    keep_x = np.where((lons >= box["west"]) & (lons <= box["east"]))[0]
    keep_y = np.where((lats >= box["south"]) & (lats <= box["north"]))[0]
    if keep_x.size == 0 or keep_y.size == 0:
        raise SystemExit(
            f"forcing: the source grid does not reach {box}; it spans "
            f"{lons.min():.2f} to {lons.max():.2f} east and "
            f"{lats.min():.2f} to {lats.max():.2f} north")
    # Contiguous by construction here: the box does not cross the prime meridian
    # in a 0-360 source, and no source in use hands out a scrambled axis. A gap
    # would silently widen the box, so it is checked rather than assumed.
    for keep, name in ((keep_x, "longitude"), (keep_y, "latitude")):
        if keep.size > 1 and np.any(np.diff(keep) != 1):
            raise SystemExit(f"forcing: the box is not contiguous in {name}")
    sy = slice(keep_y[0], keep_y[-1] + 1)
    sx = slice(keep_x[0], keep_x[-1] + 1)
    y, x = lats[sy], lons[sx]
    flip_y = y.size > 1 and y[0] > y[-1]
    if flip_y:
        y = y[::-1]
    return sy, sx, y, x, flip_y


def clip(lons, lats, cube, box):
    """Cut *cube* to *box*, returning `(x, y, cube)` with coordinates ascending.

    For a source already wholly in memory, which is what reading GRIB gives.
    Anything reading netCDF should use `box_slices` and never hold the globe.
    """
    sy, sx, y, x, flip_y = box_slices(lons, lats, box)
    out = cube[:, sy, sx]
    if flip_y:
        out = out[:, ::-1, :]
    return x, y, out


def write_atm(path, x, y, origin, series, source, domain, *, scalar_height):
    """Write one member's `atm.nc`.

    *series* maps a name in `FIELDS` to `(hours, cube)`, where *hours* are hours
    since *origin* and *cube* is (time, lat, lon) on the *x*, *y* grid. Every
    field in `FIELDS` must be present: a data table that names a variable the
    file does not carry fails inside `time_interp_external` with a message about
    the file rather than about the variable, and half an hour is lost to it.

    *scalar_height* is the height `T2` and `Q2` are at, `REFERENCE_HEIGHT` for a
    shifted archive and `PRODUCT_HEIGHT` for one built with `--no-height-shift`.
    Keyword-only and with no default on purpose: a caller that has not thought
    about the height cannot accidentally claim a shift it did not perform, which
    is the failure this whole attribute exists to prevent.

    *source* and *domain* are recorded as global attributes, because the archive
    path says which source and domain a file came from and a file that has been
    moved does not. The box is recorded from the axes actually written rather
    than from the request, so the attribute says what is in the file and not what
    was asked for; the two differ by up to one source cell on every edge.
    """
    missing = [name for name in FIELDS if name not in series]
    if missing:
        raise SystemExit(f"forcing: {path} would be missing {missing}")

    with netCDF4.Dataset(path, "w", format="NETCDF4") as out:
        out.createDimension("LON", len(x))
        out.createDimension("LAT", len(y))
        v = out.createVariable("LON", "f8", ("LON",))
        v.units, v.long_name, v.axis = "degrees_east", "longitude", "X"
        v[:] = x
        v = out.createVariable("LAT", "f8", ("LAT",))
        v.units, v.long_name, v.axis = "degrees_north", "latitude", "Y"
        v[:] = y

        for name, (units, long_name) in FIELDS.items():
            hours, cube = series[name]
            hours = np.asarray(hours, dtype="f8")
            if cube.shape[0] != hours.size:
                raise SystemExit(
                    f"forcing: {name} has {cube.shape[0]} records against "
                    f"{hours.size} times")
            if np.any(np.diff(hours) <= 0):
                raise SystemExit(f"forcing: {name}'s times are not increasing")
            # A missing value in forcing is not a gap the model works around:
            # `data_override` interpolates it into the flux and the ocean
            # carries NaN from the first timestep. Sources mask land in some
            # fields and not others, so this is a real possibility rather than
            # a formality, and it has to fail here where the source is still
            # known.
            if not np.isfinite(cube).all():
                bad = int((~np.isfinite(cube)).sum())
                raise SystemExit(
                    f"forcing: {name} has {bad} non-finite values of "
                    f"{cube.size}; the source masks something this box needs")

            axis = f"time_{name}"
            out.createDimension(axis, None)
            t = out.createVariable(axis, "f8", (axis,))
            # `axis` and `calendar` are both load bearing. soca-science set them
            # with an `ncatted` call after the fact rather than at creation,
            # which is how they are easy to lose.
            t.units = f"hours since {origin:%Y-%m-%d %H:%M:%S}"
            t.calendar = CALENDAR
            t.axis = "T"
            t.long_name = "time"
            t[:] = hours

            f = out.createVariable(name, "f4", (axis, "LAT", "LON"),
                                   zlib=True, complevel=4)
            f.units = units
            f.long_name = long_name
            if name in SCALARS:
                f.height = float(scalar_height)
            f[:] = cube

        out.source = source
        out.domain = domain
        out.box = (f"{x[0]:.3f} to {x[-1]:.3f} east, "
                   f"{y[0]:.3f} to {y[-1]:.3f} north")
        # What the file is, not what it should be. `assert_reference_height`
        # checks that this is here at all, because an archive built before the
        # shift existed has no way to say it holds 2 m scalars.
        setattr(out, HEIGHT_ATTRIBUTE, float(scalar_height))
