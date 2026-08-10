# Atmospheric reference height

`config/model/mom6sis2/domain/gom/common/data_table` and `data_table.atm` declare

```
"ATM" , "z_bot" , "" , "" , "bilinear" , 10.0
```

`z_bot` is the single height at which the FMS coupler believes the whole supplied
atmospheric state sits, and it is the reference level for the bulk flux calculation. The
same table feeds it two different levels: `u_bot`/`v_bot` come from GEFS `U10`/`V10`, which
are correct at 10 m, while `t_bot`/`sphum_bot` come from GEFS `T2`/`Q2`, which are 2 m
fields being declared as if they sat at 10 m.

FMS takes one `z_bot` for the whole atmospheric state, so the table cannot say 10 m for the
winds and 2 m for the scalars. This page records what the number does, what upstream does
instead, how large the error is, and what the fix is.

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

## The fix

Shift T2 and Q2 to 10 m when the forcing archive is built, keeping `z_bot = 10`, so that the
declaration in the data table becomes true. That is what CORE, JRA55-do and NWA12 all do, and
the last result above says it can be done offline without the model's SST.

The alternative, patching `surface_flux.F90` to carry separate momentum and scalar heights,
is more precise in principle because it uses the model's live SST, but the precision it buys
is inside the 2 to 7 W m-2 that an approximate SST already costs, and it puts a local patch
into a submodule (`pkg/mom6sis2/src/coupler`, which tracks NOAA-GFDL/coupler) that every
update then has to survive.

### Which surface temperature to shift against

GEFS's own, from the same stream the rest of the forcing comes from. Not the ocean-state
archive, and this is a physics point rather than a convenience one: GEFS's `T2` and `Q2` were
produced by GEFS's surface layer over GEFS's surface temperature, so inverting that profile
back up to 10 m is self-consistent only against the surface the profile was built over.
Substituting GLORYS puts a stability into the inversion that GEFS never saw. JRA55-do did the
same thing, taking the surface temperature for the shift from JRA-55's own fields rather than
from an independent ocean analysis.

Where it lives, per era in `tools/forcing-gefs.py`:

- **reforecast** (2000 to 2019, the era the Gulf OSSE reads): `tmp_sfc_<date>_<member>.grib2`
  sits beside `tmp_2m` in the same per-field layout. One entry each in `STEM` and `LEVEL`.
- **operational-quarter** (2020 on, the era a real-observation experiment reads):
  `TMP:surface` is absent from `pgrb2sp25`, which is what the fetcher reads, and absent from
  `pgrb2ap5`. It is in **`pgrb2bp5`**, the b-set, on a 0.5 degree grid. That is a third URL
  family and a per-era spec, though not much bandwidth, since the fetcher already reads
  `.idx` byte ranges.

Three details that the implementation should get right and that are each cheap:

- The b-set SST is 0.5 degree against 0.25 degree forcing. Inside the tolerance measured
  above, though it will look soft across the Loop Current front.
- `TMP:surface` is land skin temperature over land. That is the correct surface for those
  points and MOM6 ignores them, so it needs no masking. Worth a comment so that nobody
  later "fixes" it.
- It is a prescribed boundary condition, so it varies little with lead and probably little
  between members. If both hold, fetch it once per initialization rather than once per lead
  per member. Verify before relying on it.

### Consequences

- The forcing archive is per domain and shared with truth, so making the change means
  rebuilding forcing, rerunning truth and rerunning every experiment that reads it.
- Once the archive holds 10 m scalars, `T2` and `Q2` are the wrong names for what it holds.
- The archive should record its own reference height, as a global attribute checked at stage
  time, so that an archive built before the shift fails loudly against a run that assumes it
  rather than silently reintroducing the bias.

**In an OSSE the bias is largely common-mode**: truth and every experiment run the same model
over the same archive, so a shared flux bias mostly cancels in the departures, and the
residual is only its state dependence. **Against real observations it is not defensible**: a
systematic half-degree Gulf warm bias exceeds the SST observation error, so the analysis
spends its increments fighting a forcing error, which is how an analysis comes to verify well
and forecast badly.

Until the shift exists, the `10.0` in the data tables should carry a comment saying what it
asserts and that `t_bot` and `sphum_bot` do not yet satisfy it. A bare `10.0` reads as
agreement with GFDL when it is the opposite.
