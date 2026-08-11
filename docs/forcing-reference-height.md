# Atmospheric reference height

`config/model/mom6sis2/domain/gom/common/data_table.atm` declares

```
"ATM" , "z_bot" , "" , "" , "bilinear" , 10.0
```

`z_bot` is the single height at which the FMS coupler believes the whole supplied
atmospheric state sits, and it is the reference level for the bulk flux calculation. That
table feeds it two different levels: `u_bot`/`v_bot` come from GEFS `U10`/`V10`, which are
correct at 10 m, while `t_bot`/`sphum_bot` come from GEFS `T2`/`Q2`, which are 2 m fields
being declared as if they sat at 10 m.

**Only the `.atm` table has this problem.** The domain's other table, `data_table`, reads
the CORE climatology, whose `T_10_MOD` and `Q_10_MOD` really are 10 m fields, so its
identical `z_bot = 10.0` is true. The two are easy to conflate because the declaration is
the same line in both.

FMS takes one `z_bot` for the whole atmospheric state, so the table cannot say 10 m for the
winds and 2 m for the scalars. This page records what the number does, what upstream does
instead, how large the error is, and what the fix is.

**The shift is built and is the default.** The archive now holds 10 m scalars and the
declaration above is true. What follows is still written as the argument for it rather than
rewritten as a description, because the argument is the part worth keeping: the measurement
is what says the fix is worth its cost, and it is what a future reader needs in order to
change it. `--no-height-shift` rebuilds the old behaviour deliberately.

Every number below comes from `tools/flux-height.py`, which is a transcription of the
model's own flux routine. Rerun it rather than trusting the tables here.

## What the number does

ACKBAR runs the Large and Yeager flux path: `ncar_ocean_flux = .true.` in each domain's
`input.nml`, so `ncar_ocean_fluxes` overrides the coefficients `mo_drag` computed.

The chain is `data_override('ATM','z_bot',...)` in
`pkg/mom6sis2/src/coupler/atm_land_ice_flux_exchange.F90`, put to the exchange grid in the
same file, arriving as `z_atm` in `pkg/mom6sis2/src/coupler/surface_flux.F90`. Inside
`ncar_ocean_fluxes` the height enters twice: the stability parameter
`zeta = k*bstar*z/ustar^2`, and the height translation `log(z/10)` that converts the wind to
a 10 m neutral wind and rescales `cd`, `ch` and `ce`.

**At `z = 10` every `log(z/10)` is zero.** There is no height translation at all: the
supplied `t`, `q` and `u` are used directly as the 10 m state. So the error is a pure
air-sea contrast error in the scalars, not an error in the transfer coefficients.

## What upstream does

GFDL's own tables declare `z_bot = 10.0` truthfully, because the products they read ship
10 m scalars:

- CORE, `pkg/mom6sis2/ice_ocean_SIS2/OM4_025/data_table`: `t_bot` reads `T_10_MOD` and
  `sphum_bot` reads `Q_10_MOD`. The `_10_` is literal.
- JRA55-do, `pkg/mom6sis2/ice_ocean_SIS2/OM4_025.JRA/data_table` and
  `.../OM_1deg/data_table`: `tas` and `huss`, which JRA55-do provides at 10 m.

So `10.0` is not a convention to copy across. Copying the number while substituting 2 m
fields is exactly what breaks it.

JRA55-do's user manual has an appendix on this problem, A.2, "Shifting the temperature and
specific humidity of JRA-55 from 2 m to 10 m": "JRA-55 provides the surface air temperature
and specific humidity at 2 m above the surface. As in the CORE dataset, the temperature and
specific humidity in the final dataset are provided at 10 m above the surface, where the
wind vectors are measured." The shift uses the Large and Yeager (2009) bulk formula with
Gill (1982) moist-air properties, iterating the transfer coefficient at most five times.
CORE (Large and Yeager 2004) does the same, hence the `_10_MOD` names.

The closest MOM6 analogue to ACKBAR does it too. MOM6-COBALT-NWA12 (Ross et al. 2023,
GMD 16, 6943) computes fluxes "using the hourly ERA5 atmospheric reanalysis and the Large
and Yeager (2004) bulk algorithm, with the inclusion of the Large and Yeager (2004)
adjustment for the temperature and humidity reference height of 2 m in the ERA5 data".

References:
- JRA55-do manual: <https://climate.mri-jma.go.jp/pub/ocean/JRA55-do/docs/v1_6-manual/User_manual_jra55_do_v1_6.pdf>
- Ross et al. 2023: <https://gmd.copernicus.org/articles/16/6943/2023/>

## Why the raw products cannot avoid it

GFS and GEFS publish temperature and humidity at 2 m and winds at 10 m, and nothing at the
other height. The operational GEFS inventory
(<https://www.nco.ncep.noaa.gov/pmb/products/gens/gec00.t00z.pgrb2s.0p25.f003.shtml>) has
TMP at 2 m only, UGRD and VGRD at 10 m only, and no surface temperature at all. ERA5 is the
same shape (`2t` and `2d` against `10u` and `10v`). ACKBAR's own selectors in
`tools/forcing-gefs.py` and `tools/forcing-era5.py` reflect that.

So the mismatch is forced by the product, not by a fetch choice, and the only question is
where the height shift happens.

## Inherited, not introduced

Both prior workflows carry the same thing:

- `~/work/soca-science/configs/model/mom6sis2/common/data_table` maps `t_bot` to `T2`,
  `sphum_bot` to `Q2`, `u_bot`/`v_bot` to `U10`/`V10`, with `z_bot = 10.0`. Its fetchers
  (`scripts/forc/forc_gefs.py`, `scripts/forc/forc_gfs.py`) pull exactly those levels, and
  the only transform in that path is relative humidity to specific humidity. There is no
  height adjustment anywhere in it.
- `~/work/soca-science-v3/configs/momsis2_1deg/data_table` never wired forcing at all
  (`t_bot`, `sphum_bot` and the winds are constants), so it is no evidence either way.

## How large the error is

Method: reimplement `ncar_ocean_fluxes` in Python (same Large and Yeager equations), build a
Monin-Obukhov surface layer whose 10 m state is the prescribed one, integrate the profiles
down to 2 m to obtain T2 and q2, then compare the fluxes computed from the true 10 m state
against the fluxes from the 2 m scalars declared at 10 m. Assumptions: 1013 hPa, the 0.98
Raoult factor on saturation humidity matching `raoult_sat_vap = .true.`, gustiness ignored,
Large and Yeager neutral-drag coefficients, no wave or swell effects. Positive is ocean to
atmosphere. The calculation is `tools/flux-height.py`.

| case (SST / Tair / RH / U10) | SH | LH | dSH | dLH | dTotal | % |
|---|---|---|---|---|---|---|
| Loop Current, cool dry air (28 / 24 / 70% / 8) | 50.7 | 341.4 | -4.6 | -33.1 | -37.7 | -9.6 |
| strong cold-air outbreak (24 / 12 / 50% / 12) | 259.5 | 796.6 | -24.5 | -80.1 | -104.6 | -9.9 |
| warm moist air over cool shelf (15 / 18 / 85% / 6) | -11.5 | -8.8 | +1.2 | +1.6 | +2.7 | +13.5 |
| weakly stable shelf, light wind (20 / 21.5 / 85% / 4) | -4.0 | +8.1 | +0.4 | -1.4 | -1.0 | -8.5 |

The sign is systematic. The 2 m air is closer to the surface value, so the air-sea contrast
is understated and the ocean loses too little heat in unstable conditions, which is the
Gulf's dominant regime. It is close to a 10 per cent underestimate of turbulent cooling,
dominated by latent heat.

Converted to SST by dividing by the local flux sensitivity (70 W m-2 K-1 in the typical
unstable case, 97 in the outbreak) that is an equilibrium warm bias of about +0.5 K
typically and +1.1 K in a cold-air outbreak, damped only by the ocean, since the atmosphere
is prescribed and cannot respond.

One limitation of the estimate: it assumes GEFS's 2 m and 10 m fields are mutually
consistent with the ocean surface under similarity theory, which they are not exactly, since
GEFS derived its 2 m fields over its own surface temperature. The sign and the order of the
number do not depend on that.

### Why setting `z_bot = 2` is worse

Declaring the state at 2 m makes `log(2/10)` nonzero and applies it to a wind that really is
at 10 m. The same calculation puts the resulting wind stress 28 to 56 per cent too high
across the four cases. Stress goes as roughly wind squared, so trading a 10 per cent scalar
flux error for a 30 to 50 per cent stress error is a bad trade.

### The shift tolerates an approximate SST

The height shift needs a surface temperature to set the stability. Repeating the calculation
with the shift performed against an SST in error by one degree leaves a residual flux error
of 2 to 7 W m-2, against 38 to 105 W m-2 uncorrected. The correction is a small term, so its
own error is a small fraction of a small term: any reasonable SST recovers most of it, and
the model's own live SST is not required.

This is what removes the one advantage a patched flux routine would have had, and it is why
the fix belongs in the archive rather than in the model.

## The fix, as built

T2 and Q2 are shifted to 10 m when the forcing archive is built, and `z_bot` stays at 10, so
the declaration in the data table is now true of all four fields. That is what CORE, JRA55-do
and NWA12 all do, and the last result above is why it can be done offline without the model's
SST.

`forcing.shift_to_10m` is the correction, a vectorized port of `shift_up` in
`tools/flux-height.py`. The scalar original stays where it is and stays the reference:
`tests/test_forcing_height.py` holds the port to it elementwise rather than to a table of
expected numbers, so if the two ever disagree the question is which is wrong rather than
whether the table was copied correctly. Both fetchers apply it, because ERA5 forces the truth
and GEFS the experiments and shifting only one would put a systematic flux difference between
them, which is worse than the shared bias.

The alternative, patching `surface_flux.F90` to carry separate momentum and scalar heights,
is more precise in principle because it uses the model's live SST, but the precision it buys
is inside the 2 to 7 W m-2 that an approximate SST already costs, and it puts a local patch
into a submodule (`pkg/mom6sis2/src/coupler`, which tracks NOAA-GFDL/coupler) that every
update then has to survive.

### The archive says which it is, rather than being assumed

`--no-height-shift` on either fetcher builds the old, unshifted archive. It exists because
reproducing the previous behaviour deliberately is a thing worth being able to do, and
because a switch is the only honest way to keep `T2` and `Q2` under those names: with the
switch, the height is a property of how a file was built, so it belongs on the file. Renaming
them `T10`/`Q10` would be a lie in exactly the case the switch serves.

Every `atm.nc` carries a `scalar_reference_height` global attribute and the two scalars carry
a matching `height`. `mom6sis2.stage` calls `forcing.assert_reference_height` on every file
the data table names, and **checks that the attribute is present, not that it has a
particular value.** An archive built before any of this has no attribute at all and is
refused by name; an archive built deliberately unshifted records 2.0 and runs. Asserting 10
would make `--no-height-shift` produce an archive that cannot be used, which would defeat the
switch.

### Which surface temperature to shift against

GEFS's own, from the same stream the rest of the forcing comes from. Not the ocean-state
archive, and this is a physics point rather than a convenience one: GEFS's `T2` and `Q2` were
produced by GEFS's surface layer over GEFS's surface temperature, so inverting that profile
back up to 10 m is self-consistent only against the surface the profile was built over.
Substituting GLORYS puts a stability into the inversion that GEFS never saw. JRA55-do did the
same thing, taking the surface temperature for the shift from JRA-55's own fields rather than
from an independent ocean analysis.

Where it comes from, per era in `tools/forcing-gefs.py`, and ERA5's own `skt` in
`tools/forcing-era5.py`:

- **reforecast** (2000 to 2019, the era the Gulf OSSE reads): `tmp_sfc_<date>_<member>.grib2`
  sits beside `tmp_2m` in the same per-field layout, at the same three hourly cadence, on the
  same quarter degree grid. One entry each in `STEM`, `LEVEL` and `SHORT`, and no
  interpolation anywhere in it.
- **operational-quarter** (2020 on, the era a real-observation experiment reads):
  `TMP:surface` is absent from `pgrb2sp25`, which is what the fetcher reads, and absent from
  `pgrb2ap5`, which carries TMP at ten pressure levels and at 2 m and nothing at the surface.
  It is in **`pgrb2bp5`**, the b-set, on a 0.5 degree grid: one message per lead file, with
  a `.idx`, so it is one range request rather than a 98 MB download.

Both paths have been run against the real archives. A two member, one day
`operational-quarter` build over `gom_25km` pulls 438.6 MB (435.4 MB of whole s-set files,
2.6 MB of b-set ranges, 0.6 MB of indices) in about a minute and writes 119x88x8 files
carrying `scalar_reference_height = 10.0`. Against an otherwise identical `--no-height-shift`
build, the winds are bit identical, the scalars move in 94 per cent of columns, and over deep
water the correction runs -0.32 to +0.12 K. `--no-height-shift` fetches no b-set data at all,
so it does not download a field it will not use. The ERA5 side needs no credentials, reads
`skt` from the same `e5.oper.an.sfc` family, and pulls 8.79 GB for one day because that
mirror's files are whole months.

**The byte range is load bearing and is not free.** `message_ranges` selects by forecast
hour, and *every message in a per-lead file is at that lead*, so the hour alone keeps the
whole file: measured at 102 MB pulled to read one 165 kB field. The b-set fetch passes a
parameter and level pattern as well, which is what `BSET_MESSAGE` is for, and
`tests/test_forcing_gefs_ranges.py` pins both halves. The reforecast is unaffected, because
one of its files holds one parameter at every lead and the hour really is the whole
selection.

### The land contamination, which is the 2020-on era only

`TMP:surface` is land skin temperature over land. On the **reforecast** that is harmless and
the doc's original reasoning holds: the field is already quarter degree, no interpolation
happens, every point gets its own surface, and MOM6 ignores the land ones.

It does **not** carry over to the operational eras. There the field is half degree and has to
be interpolated up to the quarter degree the rest of the forcing is on, and interpolation
lets a coastal sea point take a share of a land neighbour's skin temperature, which over the
Gulf in summer runs several kelvin hotter than the sea. That is a coupling the reforecast
path does not have.

It is left documented and tested rather than masked, on these grounds: the contamination is
bounded by the land-sea contrast, it is gone within one source cell of the coast, and the
shift is a small term whose residual is far below the 38 to 105 W m-2 it removes. A mask
would need the b-set's own land field and a decision about what to put in the masked cells,
which is more machinery than the error justifies.
`tests/test_forcing_height.py::test_a_land_adjacent_column_pulls_land_temperature_into_the_sea`
pins how far it reaches, so the day it stops being acceptable it is one test to change.

The interpolation is bilinear rather than nearest neighbour, for the reason FMS itself
interpolates the rest of the forcing bilinearly. On a half-to-quarter mapping every other
target point coincides exactly with a source point, so nearest neighbour would stamp a half
degree checkerboard onto a field that feeds the stability calculation, and a grid-scale
artifact in a forcing term reads as a bug long afterwards. The cost is the same.

One optimization was considered and declined: surface temperature is a prescribed boundary
condition, so it varies little with lead and probably little between members, and it could be
fetched once per initialization. The byte range already makes it cheap, and the saving rests
on an assumption about member variation that nothing has checked.

### The inversion does not always have an answer

**The fixed point iteration is not globally convergent, and real fields reach the corner where
it is not.** Where the surface is colder than the air the 2 m to 10 m profile is steep enough
that the under-relaxed step overshoots and grows: a measured GEFS column went 305 K, 402,
1152, 6942, NaN in six passes, and `write_atm`'s non-finite check refused the whole file. That
refusal is the system working, but it meant the archive could not be built at all.

Three things were measured before choosing what to do:

- **Stability is the discriminator, not wind.** The first columns found were also nearly calm,
  which made light wind look like the cause. Over the whole box, unsolved columns run to
  8 m s-1 while solved ones go down to 0.03 m s-1. What they share is `TS < T2`.
- **More iterations do not help.** 12, 40 and 120 passes leave 656, 656 and 657 unsolved
  columns of the same field. This is non-convergence, not an iteration budget.
- **It does not touch open water.** A deep central Gulf window is 0 of 325 columns at every
  iteration count, and 4 of 2600 column-times across the whole test archive. The six to nine
  per cent figure is over the *padded* box, which reaches four degrees past the domain and is
  largely land, at night, in January.

So a column the inversion cannot solve is **left at its 2 m value**, and both fetchers print
how many. Monin-Obukhov similarity is unreliable in that regime anyway, the turbulent fluxes
there are near zero, and MOM6 takes forcing only over ocean, so declining to shift costs
almost nothing and is the honest answer where the correction is not determined. The count is
printed rather than logged quietly because each such column carries the bias the shift exists
to remove, and how many there are is a property of the weather in the span.

A column is accepted if it ends inside an envelope around its own 2 m values and has stopped
moving. Both fields are tested, and the lower humidity bound is never below zero: the case
that shows why is very dry air over a much colder surface, where the temperature settles to a
perfectly plausible +4.09 K while the humidity walks to **-0.38 g/kg**, which would reach the
model as real.

Two things were tried and dropped, both because measurement said they earned nothing:

- **Clamping each iterate as it went.** Judged by the same final rule, free and clamped
  iteration accept 154036 and 154035 of 200000 sampled columns, differ on nine, and agree to
  1.2e-3 K wherever both accept. One test of the envelope, at the end, is the whole of it.
- **Rejecting any column the clamp had ever touched.** This looked like it was discarding
  thousands of good answers; on inspection almost all of them were columns whose humidity had
  gone negative and which *should* be discarded.

The temperature envelope is 5 K against a largest real correction of 1.28 K, so it is
comfortably outside. The humidity one is deliberately far looser than any physical specific
humidity: at 5 g/kg it was binding on real corrections rather than catching runaways.
Divergence in humidity always arrives with divergence in temperature, so the temperature bound
is what catches it. Over open water the choice makes no measurable difference either way.

### Consequences

**Every forcing archive built before this had to be rebuilt**, and the rebuild is what makes
an archive usable at all: a file with no `scalar_reference_height` is refused at stage time
rather than silently reused, which is the intended way to find out. The archives are now keyed
by purpose rather than by domain, so a rebuild is two fetches (`gom_truth/era5` and
`gom_exp/gefs`) rather than one per resolution.

**In an OSSE the bias is largely common-mode**: truth and every experiment run the same model
over the same archive, so a shared flux bias mostly cancels in the departures, and the
residual is only its state dependence. **Against real observations it is not defensible**: a
systematic half-degree Gulf warm bias exceeds the SST observation error, so the analysis
spends its increments fighting a forcing error, which is how an analysis comes to verify well
and forecast badly.

The `10.0` in `data_table.atm` carries a comment saying what it asserts, and that comment is
now a statement rather than a warning: `t_bot` and `sphum_bot` satisfy it. A bare `10.0`
reads as agreement with GFDL, which it finally is, and the climatology table beside it, where
the same number was always true, no longer differs in meaning.
