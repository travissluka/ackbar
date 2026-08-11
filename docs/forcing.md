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
$ACKBAR_STATIC_ROOT/forcing/<purpose>/<source>/mem000.nc
                                               mem001.nc
                                               ...
```

One file per member per source per purpose, all sources normalized into one shape.
What a file is called inside a run directory is the stager's business, not the
archive's; the boundary archive uses the same layout so that one per-member
overlay mechanism serves both.

**A deterministic source materializes all N members as symlinks to the one
file**, rather than the stager falling back when a member is missing. Fallback
logic is how "which forcing did member 7 read" stops being answerable; with
symlinks it is one `readlink`.

## The key is the purpose, not the domain

A purpose is what the files are *for*: `gom_truth` is the nature run's
atmosphere, `gom_exp` is the one every experiment reads, and the two exist
because the OSSE's whole design is that truth and experiments see different
weather. **Keying by domain instead is the tempting mistake, and it has the
relationship exactly backwards.** Both archives sit on the source product's own
quarter degree grid and FMS interpolates onto the model grid when the model
runs, so nothing in a forcing file is a function of the model's resolution:
`gom_12km` and `gom_25km` reading one atmosphere is correct, and truth and
experiment sharing one is the thing the twin exists to prevent.

An experiment states its purpose (`forcing_purpose` in its `vars`) and the
default is `gom_exp`, so the truth run is the one that has to say otherwise.

## The box comes from a family of grids

`domain_box` reads `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/ocean_hgrid.nc`,
takes the extent of the supergrid rather than its corners, and pads it by
`MARGIN`, four degrees. `fetch-glorys.py` builds its GLORYS request the same way
for the same reason, so the two offline stages agree on where a domain is by
construction rather than by two constants staying in step.

**A purpose serves a family, so the box is a union.** `family_box("gom")` is the
union of every staged `gom_*` domain's box, because `gom_exp` forces `gom_25km`
today and could force `gom_12km`, `gom_8km` or `gom_4km` tomorrow. Over the Gulf
the four grids differ by under a tenth of a degree, so the union costs at most
one source cell on an edge; the point is not the size but that the archive
cannot be cut for whichever domain happened to run first.

The family is discovered from the static root each time rather than listed, so a
domain staged later widens the next fetch by itself. For an archive already on
disk that is not enough, so the two ends are joined: the domains a file covered
are written onto it, and `forcing.assert_covers` checks at stage time that the
domain being run sits inside the file's axes with `EDGE_CELLS` to spare. A
domain outside it fails in the job that stages it. Without that check it would
not fail at all: FMS fills from the nearest source cell, so the model runs and
its outermost row is quietly wrong.

**Four degrees is not decoration.** FMS interpolates forcing onto the model grid
with `bilinear` and `bicubic`, and a bicubic stencil reaches two source cells
past the point it fills, so a box ending at the domain edge leaves the outermost
row of the model reading from nothing. Four degrees is sixteen ERA5 cells and
four cells of the coarsest GEFS era, which is the one that sets the floor.

Two consequences, both deliberate:

- **One fetch serves the family, and the union is why that is safe.** The four
  Gulf resolutions do not quite share a footprint (`gom_25km` starts at 17.995N,
  `gom_12km` at 18.058N), and cutting to one of them would leave another reading
  a box that stops inside it, at the grid edge where it is least visible. Taking
  the union costs one source cell and removes the question.
- **A global domain takes the whole globe**, and a regional domain straddling the
  source's longitude seam is refused by name rather than half-handled: that box
  is two slices and every reader here takes one. Nothing in the repository needs
  it, so it is a message and not a code path.

The box written into each file's `box` attribute is the axes actually written,
not the request, so it says what is in the file. `covers` is an attribute too,
naming the domains the box was built for, because a purpose-keyed path cannot
say it: `forcing/gom_exp/gefs` says what the files are for and not which grids
they reach.

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
- **`T2` and `Q2` are shifted from the 2 m the products publish to the 10 m the
  data table declares**, so `z_bot = 10` is true of all four fields rather than
  only the winds. The products publish scalars at 2 m and winds at 10 m and
  nothing at the other height, so the mismatch was forced by them rather than by
  a fetch choice, and leaving it biased the turbulent fluxes by about 10 per cent
  in one direction. Each file records the height as a
  `scalar_reference_height` attribute and `mom6sis2.stage` refuses one that will
  not say, which is what stops an archive built before the shift from being
  reused silently. `--no-height-shift` builds the old behaviour on purpose.
  `docs/forcing-reference-height.md` carries the measurement, the argument, and
  the one limitation, which is land skin temperature reaching coastal points on
  the 2020-on GEFS eras only.

## Sub-daily shortwave, and the flag that must move with it

`ADD_DIURNAL_SW` synthesizes a diurnal cycle on the assumption that the shortwave
it is handed is a daily mean. Every source here reports shortwave at least three
hourly, so it must be off, or the diurnal cycle is applied twice. It is a
property of the forcing and not of the domain, so it travels with the data table
rather than living in the domain's `SIS_override`, which is where `NIGLOBAL` and
`NJGLOBAL` live and must not be replaced.

**This is load bearing, not a formality.** SIS2's code default is False, but the
Gulf case sets `ADD_DIURNAL_SW = True` in `SIS_input` and no `SIS_override`
mentions it, which is correct for the daily-mean climatology every other
experiment reads. So `SIS_forcing` flips a value the case actively sets, on the
first run that uses it, and the both-or-neither check guards a live
misconfiguration rather than a hypothetical one.

## The twin

**The intent is that ERA5 forces the truth and GEFS forces every experiment**,
the deterministic ones included. That last part is not a detail: if 3DVar read
ERA5 while the LETKF read GEFS, the solver comparison this repository exists to
make would be decided by which experiments got a free atmosphere.

Both halves hold now. `osse-truth` inherits `forcing/era5`; every experiment
inherits a GEFS source, `forcing/gefs` for one shared atmosphere and
`ensemble/perturbed-inputs` for a member per member. The failure mode to watch
for is not one solver on the wrong source but an experiment on *no* source: a
layer left off falls back to the model's built-in climatology, which runs
without complaint and is further from an ERA5 truth than any GEFS forecast is.

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

**More members than an era has forecasts means more than one lead.** The
reforecast has five members before 2020-09, so an ensemble larger than five is
the outer product of those members with a ladder of leads, and everything below
about a single lead has to be read with that in mind. Members drawn from
different rungs **are not exchangeable and the filter assumes they are**: a
member at 84 hours comes from a wider error distribution than one at 12, so the
ensemble is a mixture rather than a sample, its spread exceeds any single lead's,
and a rank histogram will not be flat even with a perfect filter. Every number
measured on this branch was measured on a five rung ladder. From 2020-09 onward,
31 native members retire the ladder.

Size the ladder by counting *runs*: `ensemble.size: N` with a control is N+1
runs and therefore N+1 atmospheres.

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

`reforecast` is implemented and is the only era anything has been fetched from.
`operational-quarter` is implemented and **has never been executed**: it takes a
different code path (one file per lead rather than per field, dewpoint rather
than specific humidity, whole-file download rather than byte ranges), so treat
its first run as a spike rather than as a fetch. The two middle eras are in the
table with their cadence, member count and grid, and are refused by name until
someone reads their layout, because an archive built from a guessed directory
shape is worse than no archive.

There are no automated tests over the fetchers as wholes; what is tested is the
de-averaging arithmetic (`tests/test_forcing_deaverage.py`) and the staging half
in `mom6sis2` (`tests/test_mom6sis2.py`). Everything else about a fetcher is
checked by running it.

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

## How an experiment reads it

A member selects a *file* and a source selects *two config values*, and those
are separate mechanisms.

The file comes through `ensemble.inputs`, which maps a name inside `INPUT/` to a
path template that may carry `{{member_dir}}`. `forcing/gefs-ensemble` sets
`atm.nc` to `$(forcing_archive)/gefs/{{member_dir}}.nc` and that is the whole of
the per-member half. The mechanism is not this stage's: it is the same one a
per-member open boundary uses, and `mom6sis2.member_inputs` and
[`ensemble-spread.md`](ensemble-spread.md) document it. A
template with no `{{member_dir}}` in it is how an ensemble would read one shared
atmosphere.

The config values are `model.data_table`, naming the table that points at
`INPUT/atm.nc`, and `model.override.SIS_forcing`, a fourth SIS parameter file
holding `ADD_DIURNAL_SW = False`. `mom6sis2.stage` refuses one without the
other, because a sub-daily shortwave read with that flag left on is the one
misconfiguration here that runs to completion and reports success. `SIS_forcing`
is a separate file rather than a line in the domain's `SIS_override` because
that file carries `NIGLOBAL` and `NJGLOBAL`.

There is no `forcing.source` axis and no `data_table.<source>`: every source is
normalized into the same file shape, so one table serves all of them and
swapping source is swapping which archive directory the template names.

## What this has actually been run against

One dedicated experiment, since retired once the answer was in: `gom_25km`, ten
cycles from 2015-07-12, twenty-one members on a five rung ladder, against a
baseline with everything else identical. Measured there, the surface temperature
spread
collapse is largely arrested: the cycle-mean falls 65% over ten cycles with the
shared climatology and 16% with a GEFS member per member. Salinity is barely
moved, and the thickness-weighted column mean does not move at all, which is
worth knowing because the column mean is what `tools/local/letkf-spread.py`
reports and it called the whole thing a null result.

That run's truth was climatology-forced, so it measures spread and not skill.
Nothing here has been run on another domain, with stochastic physics, or with
per-member boundaries.

## Not built yet

- The lagged-difference scheme, for member counts above what a ladder holds.
- The two middle GEFS eras.
