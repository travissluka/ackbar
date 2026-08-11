# Ensemble spread

Where an ACKBAR ensemble's spread comes from, how each mechanism is configured, and what has
to exist offline before any of them works. Three mechanisms exist:

| mechanism | perturbs | configured by |
|---|---|---|
| oSPPT stochastic physics | the model's diabatic tendency | `ensemble.stochastic` |
| per-member open boundaries | the lateral boundary a regional domain reads | `ensemble.inputs` + `tools/obc-lagged.py` |
| per-member atmospheric forcing | the atmosphere over the domain | `ensemble.inputs` + `forcing/gefs-ensemble` |

This file is the machinery: the YAML, the offline archives, the staging, the knobs. The
science and the measured results are carried separately, and every measurement quoted below is
attributed to the file it came from ([`design.md`](design.md), [`forcing.md`](forcing.md),
[`domains.md`](domains.md), an experiment header, or a tool's own output).

## What an ACKBAR ensemble is

- **Members are independent concurrent units**: one Slurm array element per member per task,
  with their own run directories, and nothing serializes them except the analysis that consumes
  them all.
- **Every member is `mem###`, including the control**, which is `mem000`. There is no
  ctrl-versus-ens split in the paths, the arrays or the graph.
- **`ensemble.size` excludes the control.** `size: 20` with `control: true` is twenty-one runs,
  `mem000` through `mem020`; with `control: false` it is `mem001` through `mem020`
  (`graph/build.py:member_set`). Anything that supplies one file per member has to supply
  twenty-one of them, which is the arithmetic error the `forcing/gefs-ensemble` header warns
  about.
- **`ensemble.source` names what maintains the ensemble across cycles**, not where it started.
  An LETKF's members are maintained by its own analysis. The `ENSEMBLE_SOURCES` check in
  `graph/build.py` refuses anything but `letkf` and `none`, but only when a *variational* solver
  draws its background error from the ensemble; for `solver: letkf` the check returns early and
  the value is unconstrained.
- **`ensemble.initial_condition`** names a directory holding one restart set per member,
  `mem001` and up, separate from `model.initial_condition` because the control starts from the
  unperturbed state and the members do not. Absent means every member starts from the same
  state, which is an ensemble of zero spread. Two builders exist:
  - `tools/ensemble-ic.sh <domain> <members> [<ic>]`, drawing each member from the static
    background error with `soca_enspert.x`. Spread by construction equal to what B claims, with
    B's correlation scales, no flow structure, and no velocity spread at all.
  - `tools/ensemble-ic-lagged.sh <domain> <ic> <year>...`, drawing each member from the same
    calendar date in another year. Every member is a real, balanced ocean with its own Loop
    Current.
- **`on_missing_member`** is the policy for a member whose forecast did not arrive
  (`src/ackbar/ensemble.py`). It is stated per experiment and has no schema default; the code
  falls back to `fail_cycle`.

| policy | what the cycle does |
|---|---|
| `fail_cycle` | stop; the ensemble is the experiment |
| `run_degraded` | assimilate what arrived, record what did not. Lower rank, more sampling noise, less spread, and the effect outlives the missing cycle |
| `replace_from_mean` | rebuild the missing member from the mean of the others. Keeps the rank in name only, since the replacement carries no independent information, but the member exists so its forecast runs |

Either way the cycle writes `members.json` beside the analysis, naming what was present,
missing and rebuilt.

## Why spread needs a source at all

An ensemble filter removes variance every cycle. A free forecast from one atmosphere, one
boundary and one model puts back very little, so an ensemble maintained only by its own
analysis narrows toward a mean the observations can no longer move.

**Relaxation cannot substitute for a source.** `da/letkf` sets `rtps: 0.95`, and its header is
explicit about why that is not a fix: RTPS relaxes the posterior spread back toward *the
prior's*, and with no external source the prior is itself the previous cycle's posterior, so
the relaxation chases a floor that moves down with it and the sequence decays geometrically for
any coefficient below one. `oops::ETKFLinearAlgebra` inflates by
`1 + rtps * (fsprd - asprd) / asprd`, so `rtps: 1.0` sets the posterior spread exactly to the
prior's: the filter stops subtracting, and something outside it has to put spread back. A
stochastic experiment therefore pairs `rtps: 1.0` with a source, and pairing a relaxation
coefficient below one with no source is the configuration that decays.

**Perturbed parameters are deliberately not used.** A member given its own parameter values is
a different model from every other member, so the ensemble covariance stops being the
covariance of anything and the mean carries whatever bias the offsets produce. That was
measured as well as argued: seventeen parameter groups swept five ways each, all producing a
fixed offset that does not grow, most of it sitting on the model's own divergence floor
(`site/monitor/spread/report.html`). A stochastic scheme draws afresh each cycle and its
members stay exchangeable.

The comparison Domains in [`design.md`](design.md) records, against a control ensemble
perturbed at two parts in a billion so the model's own divergence rate is subtracted rather
than counted:

| source | where it acts | day 5 surface temperature spread, in excess of the divergence floor |
|---|---|---|
| ensemble atmospheric forcing | mixed layer, still growing at day 5 | 0.84 degC |
| stochastic physics, oSPPT | the whole column, stationary | 0.21 degC |
| open boundary | inward from the segments, still growing at day 30 | 0.07 degC |
| perturbed parameters | nowhere much; a fixed offset that does not grow | at most 0.18 degC |

Day 5 understates the boundary more than any other row: a boundary perturbation has to be
advected in, and takes roughly three weeks to mature on this basin. `design.md` carries the
caveat in full, and the free-forecast horizon is not the quantity a cycling filter cares about
anyway (see [How to tell whether it worked](#how-to-tell-whether-it-worked)).

## Mechanism 1: `ensemble.stochastic` (oSPPT)

### What it perturbs

`MOM_diabatic_driver.F90` replaces the change a timestep made in thickness, temperature and
salinity with that change times a random field centred on one. The pattern is generated by
`NOAA-PSL/stochastic_physics`, vendored at `pkg/stochastic_physics` and compiled into
`coupler_main`; MOM6 reads the switches and does not own the pattern. `DO_SPPT` turns the
application on inside MOM6, `ocnsppt` turns the pattern on inside the generator, and
`init_stochastic_physics_ocn` fails the run when the two disagree, which is why ACKBAR writes
both from one place (`src/ackbar/stochastic.py`).

### The YAML

```yaml
ensemble:
  size: 20
  control: true
  source: offline
  on_missing_member: replace_from_mean
  initial_condition: $(static_root)/ic/gom_25km/osse-control-25km/20150712T00/ensemble20
  stochastic:
    seed: 20150712
    sppt:
      amplitude: 0.35
      length_scale: 500000.0
      timescale: PT6H
```

| key | schema | meaning |
|---|---|---|
| `stochastic` | object, `additionalProperties: false`, requires `seed` and `sppt` | absent means off, and off is byte for byte the model with no generator compiled in |
| `seed` | integer, 1 to 99999999 | what makes this experiment's draws its own. Two experiments sharing it draw the same patterns, which is how a controlled comparison is written. A cycle date fits |
| `sppt` | object, requires `amplitude`, `length_scale`, `timescale` | the only scheme; `_SCHEMES` in `stochastic.py` is a dict of one |
| `amplitude` | number > 0, no upper bound | standard deviation of the *pattern*, not of the multiplier |
| `length_scale` | number > 0 | metres, an e-folding length. ACKBAR writes `new_lscale = .true.` unconditionally so it means that |
| `timescale` | string | the pattern's AR(1) decorrelation time, ISO 8601. Well inside a cycle, or a member's perturbation is one draw held nearly constant |

Written as `500000.0` rather than `500e3`: PyYAML reads the latter as a string.

`length_scale` is bare metres with no lower guard: a scale below a few grid cells is smoothed
away by the model before it can grow.

`timescale` is validated as a string only. A value that is not an ISO 8601 duration is refused
by `parse_duration` inside the job that stages the forecast, not by `ackbar validate`.

### What has to exist offline

Nothing downloaded. This is the only mechanism that needs no archive. Two conditions instead:

- **`coupler_main` must carry the pattern generator.** `./build-model.sh` compiles
  `pkg/stochastic_physics` into it. With no scheme switched on the executable is bit for bit a
  stock build, so there is no separate "stochastic build".
- **`model.name` must be `mom6sis2`.** `graph/build.py:_check_stochastic` refuses `persistence`
  and `stub` rather than staging a parameter file nothing opens.

### Which fields it reaches

- **Reaches**: surface temperature and the mixed layer, and the whole column, stationary.
  Measured on `gom_25km` at about a tenth of a degree of surface temperature spread per cycle
  (`src/ackbar/stochastic.py`).
- **Does not reach, directly**: sea surface height and salinity. Over a day the diabatic change
  in column mass and in salt is too small for a multiplier to move
  (`src/ackbar/stochastic.py`). Measured, the two moved by 1.00 and 0.99, which is to say by
  nothing.

So an experiment whose under-dispersion is altimetric is not fixed by this knob.

### Tuning knobs

| knob | what it does |
|---|---|
| `amplitude` | the pattern's spread. `run_stochastic_physics_ocn` maps the pattern through `2 / (1 + exp(-x))`, so the multiplier is bounded in `(0, 2)` by construction rather than clipped, and its mean is one. Near one that is `1 + x/2`, so the model applies about half the spread asked for, and less than half as the logistic saturates |
| `length_scale` | the pattern's decorrelation scale. NOAA-PSL's 500 km is a global value; on a basin three e-folding lengths wide it deserves a second thought, and it has never been swept inside a filter |
| `timescale` | how fast the pattern refreshes within a cycle |
| `seed` | reproducibility and independence, not size |

Delivered spread against amplitude, from the schema's own note and the pattern statistics in
`site/monitor/spread/`:

| amplitude | delivered pattern spread |
|---|---|
| 0.2 | 0.090 |
| 0.4 | 0.174 (schema note: 0.17) |
| 0.8 | 0.309, against a linear 0.348 |
| 1.5 | 0.46 |

Raising it past about 1 buys progressively less.

**The seed is a function of the member and the cycle and of nothing else**, derived as
`((seed * 1000 + member) * 100000 + cycle) * 10 + offset`. Not the clock (a healed forecast
would integrate a different trajectory from the one it is replacing), not the task (the cycling
and extended forecasts start from the same state and must agree on their first day), and not
the layer. Every cycle draws a fresh pattern initialized at its stationary variance; there is
no pattern continuity across cycles because the generator has no writer for the ocean's.

**The control is never perturbed.** `stochastic.perturbs` returns false for `mem000`: it is
what the experiment is scored on and what a variational experiment is compared against, and it
is the member an LETKF writes the posterior mean into, which is not a draw.

### Two MOM6 schemes ACKBAR does not carry

- `PERT_EPBL` was implemented, measured and removed. The perturbation is real (`epbl1_wts` and
  `epbl2_wts` come out at standard deviations of 0.32 and 0.24 around one) but it moved surface
  temperature spread by 2% and the column not at all. Worth trying again only once ensemble
  atmospheric forcing exists; adding it back is one entry in `_SCHEMES` plus a schema block.
- `DO_SKEB` crashes inside `MOM_neutral_diffusion` on this domain at every amplitude tried,
  down to a fortieth of the one that first failed.

## Mechanism 2: `ensemble.inputs` with per-member open boundaries

### What it perturbs

The lateral boundary a regional domain reads. Every member of an experiment without this
integrates the same `obc.nc`, so at the edge the ensemble has no spread at all, and the interior
spread it does have drains out through an edge every member agrees about. That is a property of
having fetched one boundary, not of the ocean.

Regional only. A global domain has no `obc.nc` and nothing here to run; its equivalent lever is
ensemble atmospheric forcing.

### The YAML

```yaml
ensemble:
  size: 20
  control: true
  source: offline
  on_missing_member: replace_from_mean
  inputs:
    obc.nc: $(static_root)/obc/gom_25km/glorys-lag21-m20/{{member_dir}}.nc
  initial_condition: $(static_root)/ic/gom_25km/osse-control-25km/20150712T00/ensemble20
```

| half | schema | meaning |
|---|---|---|
| key | filename, `^[A-Za-z0-9_.-]+$`, and not `.` or `..` | the name the file takes *inside `INPUT/`*, which is what MOM6 opens |
| value | string | a path template, resolved per member. `{{member_dir}}` renders as `mem000`, `mem001`, ... |

The two halves are deliberately independent: the name is what the model opens, the path is
where that file's ensemble lives, which is what lets a boundary ensemble and an atmospheric one
be built by different offline stages with different layouts and staged by the same code.

`mem000` gets the unperturbed boundary, so the control is scored against exactly the boundary a
baseline experiment used.

### What has to exist offline

Two stages, in this order, neither of them a cycle task and neither reaching the network from
inside a job.

```bash
# 1. the domain's own boundary, once per domain
env -u PYTHONPATH .venv-data/bin/python \
    tools/fetch-glorys.py obc gom_25km <start> <end>

# 2. the member ensemble, once per experiment family
env -u PYTHONPATH .venv-data/bin/python tools/obc-lagged.py \
    --span 21 --members 20 \
    --out $ACKBAR_STATIC_ROOT/obc/gom_25km/glorys-lag21-m20 \
    gom_25km
```

`fetch-glorys.py` writes `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/obc.nc`, shared read-only by
every experiment on the domain. `obc-lagged.py` reads that file and writes `mem000.nc` and up
into a directory the experiment then names. Nothing is written into the domain's own `INPUT/`:
a member boundary that landed there would become every later run's boundary without saying so.

**Fetch the source long enough for the experiment plus twice the span.** A member lagged +L days
reads the base file L days past the last time it is asked for, so the output covers
`[first + max lag, last - max lag]`. `domains.md` works the example: a span 21 ladder from a
boundary covering 2015-05-28 to 2015-09-10 covers 2015-06-18 to 2015-08-20, while the `osse25-*`
experiments run 45 daily cycles from 2015-07-12 whose extended forecasts reach about 2015-08-31.
Eleven days short. `ackbar validate` step 3 catches it by reading each member's own time axis;
without that the run fails inside `time_interp_external` around cycle 40, and healing cannot
recover, because rebuilding the ladder changes the boundary every earlier cycle already
integrated against.

### Which fields it reaches

Each member is `member(t) = base(t) + amplitude * (base(t + lag) - mean_over_members)`, so the
ensemble mean boundary is exactly the unperturbed boundary by construction, for any number of
members and any set of lags. A plain lagged ensemble does not have that property: the mean of N
lagged copies is a running mean, smoother than the boundary, and that smoothing is a bias that
grows with the span rather than shrinking with member count.

From `tools/obc-lagged.py` and Domains in [`design.md`](design.md), on `gom_25km`:

- **Reaches**: sea surface height with structure, which is the field the altimeters see and the
  field a Loop Current lives in. Two thirds of the sea surface height spread is more than 200 km
  from any segment. Temperature spread peaks *below* the thermocline rather than at the surface.
  0.14 degC of surface temperature spread by day 30, still rising.
- **Reaches slowly**: anything at all on a five day horizon. Three weeks to mature.
- **Carries a mode no analysis can remove**: about 47% of the interior sea surface height spread
  variance is one basin-wide number, 3.0 cm of 4.35 cm at day 30. `ufo::ObsADT::simulateObs`
  subtracts `mean(H(x) - y)` over the observation space, and `ObsADTTLAD` does the same in the
  tangent linear and the adjoint, so a member whose sea level is uniformly high carries an error
  no altimeter reports.

In a cycling filter the quantity that matters is the collapse rate, not the free-forecast
spread. Ten cycles of `osse25-4dletkf` against the same experiment with the boundary ensemble
added: the baseline's domain sea surface height prior spread falls monotonically from 0.0650 m
to 0.0238 m and is still falling, while the boundary ensemble's tracks it down to about 0.043 m
and then stops, holding 0.0432 to 0.0448 m over the last five cycles. Banded by distance from
the nearest segment and averaged over the ten cycles, the ratio is 2.78 within 50 km and 1.10
beyond 400 km. What a boundary ensemble supplies is a source the analysis cannot consume,
because it is re-imposed from outside the domain each cycle rather than carried in the state the
filter updates. That is a statement about spread and not about calibration: both runs share
truth's boundary, so neither carries a boundary error.

### Tuning knobs

All of them belong to `tools/obc-lagged.py`, at archive build time, not to the experiment.

| knob | what it does |
|---|---|
| `--span` with `--members` | derives the lag ladder from the largest lag in days. The form an experiment family carries, because a member count can change without re-deciding anything |
| `--lags=-21,-13,...` | the ladder stated outright, one whole-day lag per member, in member order. Written with an equals sign because the list starts with a minus. Lags are whole days so a member's boundary is exactly a real analysis and not a blend |
| `--amplitude` | scales the anomaly about the member mean. Default 1.0 is the lagged ocean's own amplitude. Separates size from structure, the same split `ensemble.stochastic` has between pattern and amplitude |
| `--source` | a boundary file other than the domain's `obc.nc` |

The archive is built at amplitude 1.0 and that is probably low: differencing GLORYS against
HYCOM on the same day, each as an anomaly about its own boundary mean, implies 1.4 to 2.1
depending on the field, median 1.6. It stays at 1 because the paired difference was wanted
first and amplitude is a second axis.

Note that a second product used for calibration carries a different sea surface height datum,
0.16 m between HYCOM GOFS 3.1 and GLORYS on this boundary, which under FLATHER is a permanent
head and has to be removed at the source (`fetch-hycom.py --match-ssh`).

## Mechanism 3: `ensemble.inputs` with per-member atmospheric forcing

### What it perturbs

The atmosphere over the domain: each member's `INPUT/atm.nc` is a different GEFS forecast
instead of the domain's shared climatology. Every member is valid at the same times as every
other and as the truth; what differs is which perturbed forecast it came from and how far ahead
that forecast was looking. Real forecast uncertainty about the actual verifying date, in
physically calibrated units, with no amplitude to tune.

### The YAML

One inherit line. The layer sets `ensemble.inputs` itself.

```yaml
inherit:
  - domain/gom_25km
  - model/mom6sis2
  - da/letkf
  - forcing/gefs-ensemble
```

`forcing/gefs-ensemble` sets

```yaml
ensemble:
  inputs:
    atm.nc: $(forcing_archive)/gefs/{{member_dir}}.nc
```

and inherits `forcing/common`, which supplies the two config values a source needs and the
per-member half does not: `model.data_table` (the table naming `INPUT/atm.nc` and its seven
fields) and `model.override.SIS_forcing` (a fourth SIS parameter file holding
`ADD_DIURNAL_SW = False`). `mom6sis2.stage` refuses one without the other, because a sub-daily
shortwave read with that flag left on is the one misconfiguration here that runs to completion
and reports success.

`$(forcing_archive)` resolves to `$(static_root)/forcing/$(forcing_purpose)`, so the archive is
keyed by what the files are for, `gom_exp` here and `gom_truth` for the nature run, rather than
by which domain reads them. A template with no `{{member_dir}}` in it is how an ensemble would
read one shared atmosphere; no such layer is written.

### What has to exist offline

```bash
tools/forcing-gefs.py 2015-07-10 2015-09-01 --leads 12,36,60,84,108 \
    --members 21 --family gom \
    --out $ACKBAR_STATIC_ROOT/forcing/gom_exp/gefs
```

`--family gom` rather than one domain: the box is the union over every staged `gom_*` domain, so
the same archive serves `gom_12km` or `gom_4km` if one of them runs this experiment later.
`forcing.assert_covers` refuses at stage time an archive that does not reach the domain being
run, which is what makes the union a check rather than a hope.

**Count the runs, not the members.** `ensemble.size: 20` with `control: true` is twenty-one runs
and therefore twenty-one atmospheres. Five members on a five rung lead ladder is twenty-one
files (`mem000` through `mem020`); four rungs is twenty, and the twenty-first member has nothing
to read.

**The ladder is an artefact of the period.** The GEFS reforecast has five members before
2020-09, so an ensemble larger than five is the outer product of those members with a ladder of
leads (12, 36, 60, 84, 108 hours here). Members drawn from different rungs **are not
exchangeable and the filter assumes they are**: a member at 84 hours is drawn from a wider error
distribution than one at 12, so the ensemble is a mixture rather than a sample, its spread
exceeds any single lead's, and a rank histogram will not be flat even with a perfect filter.
From 2020-09 onward 31 native members retire the ladder, which is the argument for running an
OSSE on a period after that date.

The eras, from [`forcing.md`](forcing.md), which carries the full table:

| era | years | members | status |
|---|---|---|---|
| `reforecast` | 2000-2019 | 5 | implemented, the only era anything has been fetched from |
| `operational-1deg` | 2017 to 2018-07 | 21 | refused by name |
| `operational-half` | 2018-07 to 2020-09 | 21 | refused by name |
| `operational-quarter` | 2020-09 on | 31 | implemented, never executed. Treat its first run as a spike |

Nothing is silently approximate: a period no era covers, a period two eras cover, an era not
implemented, a member count an era cannot supply, a lead its cadence cannot express, each names
itself and stops. The de-averaging of GEFS accumulation windows is the one place a fetch can be
quietly wrong; `forcing.md` has that section.

### Which fields it reaches

- **Reaches, and uniquely**: salinity and momentum, directly. This is the only mechanism in
  ACKBAR that does. oSPPT perturbs a temperature tendency and reaches sea surface height and salt
  only through what the ocean does with it, so an ensemble driven by one atmosphere is
  under-spread in exactly the variables the surface observations constrain
  (`config/layers/forcing/gefs-ensemble.yaml`).
- **Reaches, largest of the three**: the mixed layer, still growing at day 5, 0.84 degC of
  surface temperature spread over the divergence floor.
- Measured over ten cycles ([`forcing.md`](forcing.md)): the surface
  temperature spread collapse is largely arrested, with the cycle mean falling 65% over ten
  cycles on the shared climatology and 16% with a GEFS member per member. Salinity is barely
  moved and the thickness-weighted column mean does not move at all, which matters because the
  column mean is what `tools/local/letkf-spread.py` reports, and it called the whole thing a null
  result.

### Tuning knobs

| knob | what it does |
|---|---|
| `--leads` | the one real knob. It sets how much spread the ensemble has *and* how wrong the experiments' atmosphere is against the truth's, both in calibrated units, with the tradeoff a real forecast system has. Choose it by measurement: the smallest lead whose spread spans the ERA5-minus-control difference over the same box and hours |
| `--members` | how many, counting the control as the first. Bounded by what the era has |
| `--domain` | which grid to clip to, plus a four degree margin |
| `--era` | which GEFS product, when the dates alone do not decide it |

There is no amplitude, span, mean preservation or clamping, because those manufacture spread and
this spread is measured. Physical consistency is free, since a member's seven fields come out of
one model run.

**The truth caveat, now closed.** ERA5 forces the truth and GEFS forces every experiment, the
deterministic ones included. The spread numbers above were measured before that held, against a
climatology-forced truth, so read them as properties of the ensemble rather than as skill. The
skill question is open again and is what the eight arm comparison answers.

## How a per-member file reaches the model

Both `ensemble.inputs` mechanisms share one staging path, `mom6sis2.member_inputs` and
`mom6sis2._input_dir`. `INPUT/` is rebuilt from nothing every attempt, in three layers:

1. the domain's shared archive,
2. **this member's own files** (the `ensemble.inputs` overlay),
3. this cycle's restart set.

The overlay is after the domain because a per-member input *replaces* the domain's shared copy
of that file. Both are before the restart set because `coupler_main` reads `INPUT/coupler.res`
from a hardcoded path and the restart set has to win it: a cycle whose date came from anywhere
else integrates the right state from the wrong time.

Four behaviours follow, all deliberate:

- **A name that is also in the restart set is refused**, before anything is linked, rather than
  built, linked, overwritten and never read.
- **Every member resolves to a file**, including the control and including a source with only one
  realization, which materializes as N symlinks to that one file.
- **A missing file is an error**, never a fall back to the domain's copy. The fallback is a
  member with no perturbation, and its only symptom is an ensemble slightly less spread than it
  should be, which is indistinguishable from the scheme being weaker than it is.
- **What each member resolved to is recorded** in an `ensemble.inputs` file beside the run and
  copied out with the model's other traces. `readlink INPUT/obc.nc` answers only while the job
  is running, because the run directory is scratch and is deleted on success.

`ackbar validate` checks both failures that would otherwise surface inside a job: every rendered
per-member path is stat'd at step 3, and each file's own time axis is compared against the span
the graph will ask for. The coverage check reads the time axis rather than the
`time_coverage_start`/`time_coverage_end` attributes `obc-lagged.py` writes, because those are an
annotation and not the thing the model opens.

Both mechanisms require `model.name: mom6sis2` (`graph/build.py:_check_member_inputs`): only
that model stages an `INPUT/` for a member's files to be linked into.

## Choosing between them

| what you are short of | reach for | why |
|---|---|---|
| spread in salinity or velocity | ensemble atmospheric forcing | the only mechanism that reaches them directly |
| spread in surface temperature and the mixed layer | oSPPT, or ensemble forcing | oSPPT is the cheapest thing that moves surface temperature at all, and needs no archive |
| spread in sea surface height with spatial structure | per-member open boundaries | oSPPT does nothing for sea level; the boundary is the field a Loop Current lives in |
| spread that does not collapse under cycling | per-member open boundaries | it is re-imposed from outside the domain each cycle, so the analysis cannot consume it |
| something running today, on a domain with no boundary and no forcing archive | oSPPT | nothing to download; `build-model.sh` already compiled the generator |
| spread on a global domain | ensemble atmospheric forcing | there is no open boundary to perturb |
| an answer within ten cycles | oSPPT or ensemble forcing | a boundary perturbation takes about three weeks to mature |

They compose, and the question a combined run answers is not whether the spread goes up (it
must) but whether the three are close to additive in *variance* and whether the field-by-field
imbalance each leaves behind cancels or compounds. The cost of asking it that way: with three
sources changed at once the run is not a paired difference against any single other one.

The comparison arms take two of the three, through `ensemble/perturbed-inputs`: per-member
atmosphere and per-member boundary. oSPPT is left out because it is the smallest of the three
and the only one with an amplitude to tune.

One composition rule is already known. Ensemble forcing adds surface temperature spread of its
own, so an oSPPT amplitude already known to overshoot in surface temperature should not be
stacked on top of it.

## How to tell whether it worked

**Spread against error, never spread alone.** More spread is not better. An under-dispersed
filter under-weights its observations, because the Kalman gain grows with the prior spread, so
an ensemble that claims more confidence than it has leaves the analysis too near the background.
The failure in the other direction is just as real: oSPPT is a *model error* term, and an
ensemble given more spread than the model's error justifies over-weights its observations and
pulls noise into the analysis.

In an OSSE the truth is a run, so this is measurable rather than arguable. Read
`tools/local/osse-state-error.py` **before** `tools/local/letkf-spread.py`, and do not accept a
spread result that costs state space skill.

The worked example: oSPPT at amplitude 0.8 with `rtps: 1.0`, against its baseline, over cycles
6 to 22.

| field | spread x | error x | spread/error, base -> stoch |
|---|---|---|---|
| SSH | 1.28 | 0.98 | 0.26 -> 0.35 |
| SST | 2.55 | 1.02 | 0.68 -> 1.70 |
| SSS | 1.15 | 1.06 | 0.59 -> 0.64 |
| u | 1.29 | 1.01 | 0.47 -> 0.60 |
| v | 1.31 | 1.04 | 0.43 -> 0.55 |

Every field's spread went up and no field's error came down. Surface temperature is the only one
that crossed one, and it overshot by 70%, while every other field is still at least a third
short and sea surface height is worst at 0.35. That is an ensemble over-dispersed in the one
field the observations are densest in, which is exactly the configuration that pulls observation
noise into the analysis: the departures got worse by 12 to 20% on every temperature platform
while the altimeters improved by up to 6%.

Two lessons in that table, both worth keeping:

- **Attribute a change to the knob that made it.** The two knobs act on different fields, and
  `osse25-letkf-stoch5` is what separates them: oSPPT alone at `rtps: 0.95` moved surface
  temperature spread by 1.3 to 1.7x and sea surface height and salinity by nothing (1.00 and
  0.99). So the 28 to 31% gained in sea surface height and velocity above is the relaxation's,
  and the 155% in surface temperature is oSPPT's. Backing the relaxation off would give up the
  altimetric gain, the only skill improvement the pair produced, and leave the overshoot in
  place.
- **Size the amplitude against the error, not against a global default.** The 0.35 amplitude was
  derived that way: surface temperature spread has to fall from 0.261 to about 0.15 to sit on its
  error of 0.154, which is removing four fifths of the added *variance*
  (`0.261^2 - 0.102^2 = 0.0578` against a target `0.150^2 - 0.102^2 = 0.0121`), so the added
  standard deviation comes down by a factor of 0.46, and 0.8 x 0.46 with a little back for the
  logistic's saturation is 0.35.

Two measurement traps:

- **A column mean can hide a surface result.** `tools/local/letkf-spread.py` reports the
  thickness-weighted column mean, which does not move under ensemble forcing at all, and it
  called that mechanism a null result ([`forcing.md`](forcing.md)).
- **A free-forecast horizon is not the cycling quantity.** In a cycling filter the thing a spread
  source moves is the collapse rate, which no free-forecast measurement shows, and which is why
  the day 5 table above understates the boundary.

## What has been tried and rejected

- **Perturbed parameters.** A member with its own parameter values is a different model, so the
  ensemble covariance stops being the covariance of anything and the mean carries the offsets'
  bias. Measured, not just argued: seventeen parameter groups swept five ways each, all
  producing a fixed offset that does not grow, most of it on the model's own divergence floor
  (`site/monitor/spread/report.html`). A stochastic scheme draws afresh each cycle and its
  members stay exchangeable.
- **Removing the boundary ensemble's basin-wide `zeta`.** The reasoning is easy to reconstruct
  and is wrong: every GoM segment is FLATHER, FLATHER imposes a prescribed head, so a
  boundary-wide `zeta` anomaly ought to pump the basin. It was built, measured and reverted.
  Interior basin-wide spread did not fall (0.0298 m against 0.0362 m across two otherwise
  identical seven member spikes), and correlating each member's boundary-wide `zeta` anomaly
  against its day 30 basin-mean sea surface height gives -0.23: no relationship and the wrong
  sign. The remaining candidate is net volume flux through the segments, which needs the domain's
  mask and topography to constrain. `tools/obc-lagged.py` carries the full account.
- **`PERT_EPBL`** and **`DO_SKEB`**, above.
- **A bigger relaxation coefficient instead of a source.** No coefficient below one holds an
  ensemble up, and one at one only stops the filter subtracting. See `da/letkf.yaml`.

Unbuilt rather than rejected: additive inflation from a climatological pool, and the
lagged-difference forcing scheme for member counts above what a lead ladder holds.
