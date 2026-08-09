# Atmospheric forcing

What forces the ocean, where it comes from, and what each source can and cannot
supply. The offline stage `design.md` lists as `forcing | period` is this.

Until this exists in the workflow, every Gulf experiment and the truth run share
one NCAR/CORE normal-year climatology with no synoptic weather in it at all, and
`osse.md` names that as the reason no ensemble number in the OSSE is believable.
The size of the problem is not a matter of opinion: on `gom_25km`, from one
initial condition, five days of ERA5 against five days of that climatology
differ by **1.2 degC rms in sea surface temperature**, against an OSSE sea
surface temperature analysis error of about 0.15.

## The archive

```
$ACKBAR_STATIC_ROOT/forcing/<source>/mem000.nc
                                    mem001.nc
                                    ...
```

One file per member per source, clipped to a box around the domain family, all
sources normalized into one shape. What a file is called inside a run directory
is the stager's business, not the archive's; the boundary archive uses the same
layout so that one per-member overlay mechanism serves both.

**A deterministic source materializes all N members as symlinks to the one
file**, rather than the stager falling back when a member is missing. Fallback
logic is how "which forcing did member 7 read" stops being answerable; with
symlinks it is one `readlink`.

## The file

Seven fields, `T2 Q2 U10 V10 DSWRF DLWRF PRATE`, which is what both prior
workflows used, so a data table copied from either keeps working. Built by
`src/ackbar/forcing.py`, which is also where the box is defined.

Because the shape is the same for every source, **the data table does not vary by
source**. One `data_table` names `INPUT/atm.nc` and those seven variables, and
swapping ERA5 for GEFS is swapping which file a symlink points at. Source
selection is a separate mechanism from member selection, and only the second one
needed building.

Three details that are not free choices:

- **Every field carries its own unlimited time axis** (`time_T2`, `time_DSWRF`),
  which netCDF4 permits and netCDF3 does not. It is soca-science's shape, proven
  against FMS over years of use. It exists so instantaneous fields and interval
  means can be stamped honestly at different times: an interval mean belongs at
  its midpoint, and forced onto a shared axis it is always the shortwave that
  gets interpolated and smeared, which `tools/spike-gefs-atm.py` admits in its
  own docstring.
- **The calendar attribute is the model's `NOLEAP`**, not the sources'
  `gregorian`. FMS resolves the axis with the calendar the file names, and the
  two disagree by every leap day since year zero. A span containing 29 February
  is refused rather than silently slipped by a day.
- **Surface pressure is fetched and not written.** Nothing here applies an
  atmospheric pressure load: the data table sets `p_surf` and `p_bot` to a
  constant 1e5 and both prior workflows did the same. It is read only where a
  humidity conversion needs it.

## Sub-daily shortwave, and the flag that must move with it

`ADD_DIURNAL_SW` synthesizes a diurnal cycle on the assumption that the shortwave
it is handed is a daily mean. Every source here reports shortwave at least three
hourly, so it must be off, or the diurnal cycle is applied twice. It is a
property of the forcing and not of the domain, so it travels with the data table
rather than living in the domain's `SIS_override`, which is where `NIGLOBAL` and
`NJGLOBAL` live and must not be replaced.

## The twin

**ERA5 forces the truth. GEFS forces every experiment**, the deterministic ones
included. That last part is not a detail: if 3DVar read ERA5 while the LETKF read
GEFS, the solver comparison this repository exists to make would be decided by
which experiments got a free atmosphere.

ERA5 is hourly, quarter degree, instantaneous analysis for the state fields and
hourly *mean rates* for the fluxes, so there is no de-accumulation step and
therefore no way to get one wrong.

## Lagged forecast, not lagged date

Every GEFS member is valid at the same times as every other and as the truth.
What differs is which perturbed forecast it came from and how far ahead that
forecast was looking. That is real forecast uncertainty about the actual
verifying date.

It is deliberately *not* the scheme the open boundary uses. There, a member's
field is imported from another date, because GLORYS has one member and there is
nothing else to draw from, and the result is a plausible field that is not an
estimate of the uncertainty about this date. Where a native ensemble exists,
synthesizing one instead would be perverse.

Four things follow, all simplifications: no amplitude, span, mean preservation or
clamping, since those manufacture spread and this spread is measured; physical
consistency for free, since a member's seven fields come out of one model run;
no rule about lagging every variable together, since nothing is lagged; and one
knob.

**That knob is the lead**, and it sets two things at once: how much spread the
ensemble has, and how wrong the experiments' atmosphere is against the truth's.
Both in physically calibrated units, with the tradeoff a real forecast system
has, since a longer lead buys spread and costs accuracy. Choose it by
measurement, not taste: the smallest lead whose ensemble spread spans the
ERA5-minus-control difference over the same box and hours. That comparison needs
no model time, because both sources land in the same file shape on the same
grid.

A member's series is stitched at constant lead, taking the hours from `lead` to
`lead` plus the initialization interval out of each successive initialization, so
every record is the same age and its error statistics are stationary.
soca-science did the same with a fixed six hour lead. The joins are small jumps
and they are the price of a series longer than a forecast.

## GEFS is not one archive

The period is a first class input, not an assumption. Nothing below is derivable
from a date, so it is stated per era and selected by one, and `tools/forcing-gefs.py`
carries the table.

| era | years | members | inits/day | output | grid | humidity | one file holds |
|---|---|---|---|---|---|---|---|
| `reforecast` | 2000-2019 | 5 | 1 | 3 h | 0.25 deg | specific | every lead of one field |
| `operational-1deg` | 2017 to 2018-07 | 21 | 4 | 6 h | 1.0 deg | dewpoint | every field at one lead |
| `operational-half` | 2018-07 to 2020-09 | 21 | 4 | 6 h | 0.5 deg | dewpoint | every field at one lead |
| `operational-quarter` | 2020-09 on | 31 | 4 | 3 h | 0.25 deg | dewpoint | every field at one lead |

`reforecast` and `operational-quarter` are implemented and tested. The two middle
eras are in the table with their cadence, member count and grid, and are refused
by name until someone reads their layout, because an archive built from a guessed
directory shape is worse than no archive.

Nothing is silently approximate. A period no era covers, a period two eras cover
(2017 to 2019, where the reforecast and the operational archive are different
products over the same days), an era not implemented, a member count an era
cannot supply, a lead its cadence cannot express: each names itself and stops.

### What each era can supply

**The five member eras cannot supply a twenty member ensemble.** The reforecast
has five members on an ordinary day and eleven on a Wednesday, and eleven once a
week is not something a daily cycle can read. So on the earlier period native
GEFS is an *input to* the lagged-difference scheme rather than an ensemble in its
own right, which is why that scheme is first class here rather than a fallback
to be dropped if native measures better on the later period.

From 2020-09 onward, 31 members means a twenty member experiment is native,
exchangeable, and needs no construction at all. That is the argument for running
the OSSE on a period after that date, and it is the only argument: GLORYS covers
1993 to the present, so the boundary constrains nothing.

## De-averaging, the one place this can be quietly wrong

ERA5 publishes mean rates and needs none of this. GEFS publishes windows since a
six hourly reset, so within each reset period one window arrives whole and the
rest are differences. Getting it wrong crashes nothing: it makes precipitation
too large by a factor that grows through each reset period, and puts the
shortwave's diurnal peak in the wrong place. Every window read is checked against
the one the arithmetic assumes.

Differencing two independently packed windows leaves a residue, so the strictly
positive fields are floored at zero and the excursion is reported rather than
hidden. **The tolerance needs an absolute part as well as a relative one**, and
that is worth stating because the obvious implementation is wrong: measured
against the result's own magnitude, a night-time shortwave is nothing *but*
residue, and the first version of the check rejected -8 W m-2 "against a range of
8". Both bounds sit far below what a real misreading costs, which is hundreds of
W m-2.

## Not built yet

- The per-member overlay in `mom6sis2.py`, and the `forcing.source` config axis.
- The `data_table` and `SIS_override` source variants as config rather than as
  spike files.
- The lagged-difference scheme, for member counts above what an era holds.
- The two middle GEFS eras.
