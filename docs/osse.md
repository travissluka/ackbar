# The Gulf of Mexico OSSE

What the study is, how its pieces fit, and the traps found in building it. The
stages are the `experiments/osse-*` and `experiments/osse25-*` files, which carry
their own headers; [`../experiments/README.md`](../experiments/README.md) is the
index. This file is the reasoning around them.

## What this is, and what it is not

The claim it is allowed to make is that a nature run, a synthetic observation
archive, a lagged ensemble, the DA methods and a state-space verification all
connect to each other and produce numbers that are not obviously wrong. It exists
so that the first time those pieces meet is not also the first time an answer
depends on them.

It is **not** a benchmark. Four reasons, all structural:

- **The experiments do not all share a forcing source with each other.** Truth
  inherits `forcing/era5` and reads `forcing/gom_truth/era5`, so the nature run
  has the weather that actually happened, and an experiment inheriting
  `forcing/gefs-ensemble` reads a GEFS forecast of that weather, wrong by the
  amount a real forecast is wrong. That pairing is what makes a skill number
  possible. What is not settled is the rest of the suite: an experiment that
  inherits no forcing layer runs on the NCAR/CORE climatology, which has no
  synoptic variability anywhere, and is therefore *further* from truth than one
  on GEFS for a reason that has nothing to do with its solver. Comparing a
  climatology-forced 3DVar against a GEFS-forced LETKF measures which of them
  got an atmosphere. The fix is the deterministic GEFS source layer that
  [`forcing.md`](forcing.md) lists as not built, plus a line in each experiment.
- **The observation errors are iid and exactly correct.** The generator adds
  Gaussian noise at the stated error and the observer assimilates that same
  number, which is the easy case and the right one to prove plumbing with. A
  mis-specified R needs the two to be stated separately in the generator before
  it can exist.
- **The DA settings are not tuned.** The background error values are the pinned
  bundle's defaults, because v2's tuning lived in `BkgErrGODAS` and does not map
  onto `SOCAParametricOceanStdDev`. See [Settle these](#settle-these).
- **The open boundary is shared too, and that is now a decision rather than an
  oversight.** `gom_12km` and `gom_25km` carry the same GLORYS slice, so the
  truth run and every experiment are held to the same edge. It is a constraint no
  real regional system has: whatever the analysis gets wrong in the interior, the
  boundary keeps pulling it back towards the state the observations were
  generated from, and the Loop Current inflow in particular is prescribed
  identically in both. Every skill number here is flattered by it and should be
  read that way.

  Giving truth an independent boundary was built and then declined. The reason is
  the mode it would introduce: two reanalyses differ at the boundary by a
  basin-wide sea level offset, 0.14 to 0.16 m of datum plus a residual that moves
  from day to day, and `ObsADT` removes the domain mean before forming a
  departure (see [`design.md`](design.md)), so that component is invisible to
  every solver. A twin built that way would carry a permanent uncorrectable
  penalty that says nothing about the DA and confounds everything that does.
  `tools/fetch-hycom.py` stays, because the second source is what calibrates the
  boundary ensemble's amplitude, which is a use that wants the disagreement
  measured rather than injected.

It is a *fraternal* twin, which is the one structural thing that came out right:
truth is `gom_12km` and every experiment is `gom_25km`, so model error is real
rather than absent. The cost is that `gom_25km` is barely eddy permitting, with
the Gulf's first baroclinic Rossby radius at 35 to 45 km, so the Loop Current
sheds rings reluctantly and there is little mesoscale for altimetry to correct.
See [`domains.md`](domains.md).

Stating this here rather than in a write-up is deliberate. An OSSE that produces
a plausible skill number is very easy to over-read six months later.

## The shape of it

Each stage is an ordinary ACKBAR experiment or an offline tool, and nothing here
is a special execution mode.

```
spinup      osse-spinup      gom_12km, settles the nature domain
truth       osse-truth       gom_12km, the run that becomes the truth archive
promote     tools/local/promote-truth.sh, and the 25 km control beside it
observe     tools/obs-archive-osse.py, sampling the truth trajectory
experiments osse25-*         gom_25km, one DA method each
verify      state space against the truth archive, obs space against departures
```

Two details of that ordering are load bearing and easy to get wrong when
rebuilding it. The truth archive is assembled from `bkg/` records rather than
from restarts, because every sub-window state is already reduced and filed by
valid time, which is what lets `cleanup` reap the restarts while the run is still
going. And the ensemble is drawn from lagged GLORYS years by
`tools/ensemble-ic-lagged.sh` rather than perturbed through the static B, with
the recentring last, after the settle.

### The observing system

Thirteen platforms: four altimeters, two SST, one SSS, two drifter and four
profile and glider. [`observing-system.md`](observing-system.md) is the reference
and owns the table; the observer layers under `config/layers/obs/` are what
actually runs.

### Verification

**State space**, against the truth archive: domain RMSE and bias for SST, SSH,
and temperature and salinity by level, for the background and the analysis, every
cycle, per experiment. Restricted to the domain interior so the sponge next to
the open boundary is not being scored. For the ensemble methods, also ensemble
spread against ensemble mean error, which is the one diagnostic that says whether
the filter is consistent rather than merely running.

**Observation space**, from the departures the analysis already writes: count,
mean and RMS departure by platform, before and after, plus how many observations
each filter rejected. Cheap, localizes a broken observer, and does not depend on
the truth at all.

The `verify` task in the graph does neither: it is declared and writes a deferred
sentinel. The comparison across experiments lives in `tools/local/`, which is not
in the repository.


## The diurnal trap in a 3D window

**A 3D window aliases the diurnal SST cycle into a bias at the analysis hour,
and it is large enough to reverse the sign of the SST result.** This is a
property of the configuration and not of the covariance, and it is worth
stating here because every diagnostic that could catch it points the wrong way.

3DVar compares every observation in the window against one state at the
window's centre. The SST observations are not at the centre: a swath crosses
this domain over a dozen hours and the network's times are spread across the
whole window, so their mean is near the *daily* mean. The analysis is valid at
the centre. Assimilating them as if they were all valid there pulls the
analysis from the centre's SST toward the daily mean, every cycle, in the same
direction, and the cycling makes it persistent.

The size of the offset is not a free parameter. It is the truth's own
domain-mean SST at the analysis hour minus its daily mean, which is measured
from the truth archive directly:

```python
# domain-mean SST by UTC hour, over the promoted truth
for path in sorted(truth.glob("2015*.nc")):
    hour = path.stem[9:11]                       # 20150712T0600.nc
    with netCDF4.Dataset(path) as source:
        by_hour[hour].append(np.nanmean(source["temperature"][0]))
```

Run that before reading any SST skill number from a 3D experiment. If the
spread across hours is comparable to the SST error being claimed, the claim is
about the window and not about the analysis.

Three things follow, and all three are counter-intuitive:

- **The state-space SST error can rise while every SST O-B falls.** The cold
  background fits observations whose mean is the daily mean *better* than a
  correct one does. Both numbers are right; they are answering different
  questions.
- **The de-biased error is the diagnostic**, not the rms. A 3D run's SST
  pattern error can beat the free run's while its rms loses to it, and the rms
  is the number a reader takes.
- **FGAT is the fix and not a tuning knob.** `da/variational` with
  `solver.window.type: fgat` and a `forecast.slots` cadence compares each
  observation against the state nearest its own time and solves the same single
  increment at the centre. Nothing about the background error changes.

Two bounds on how well it can be shown. `tools/obs-archive-osse.py` samples the
truth state *nearest* each observation rather than interpolating, so an
observation carries up to half the truth's cadence of ocean evolution as
representativeness error, and in a diurnally varying field that is a floor
under any SST score. And `forecast.slots` finer than the truth's cadence buys
nothing: it resolves time the observations do not carry. Promote the truth at
the cadence the diurnal cycle needs, then set the slots to match it.

## Settle these

Analysis settings that started as placeholders. Each changes what the OSSE
produces, and each is a science decision rather than a plumbing one. Three of the
five are now settled and are kept here because the reasoning behind them decides
larger questions.

- **`unbalanced ssh`** is *settled*, and this entry is kept only because the
  reasoning behind it decides a larger question below.
  `config/layers/da/variational.yaml` pins it to `min: 0.0, max: 0.0`, which is
  soca-science's hat10 value, rather than leaving the block's 0.1 m default. So
  **every SSH increment a variational analysis here produces is balanced**: it
  is the steric height implied by the temperature and salinity increment through
  `ksshts`, and there is no unbalanced part.
- ~~**`sst: {fixed value: 1.0}`**~~ **Settled: a derived field.**
  `config/layers/da/variational.yaml` reads `$(domain_static)/sst_bgerr.nc`,
  built for the domain by `tools/sst-bgerr.py`, rather than a flat constant.
- ~~**`ninner: 10`**~~ **Settled: 20.** The `1.0e-10` reduction target still
  never fires, so every solve stops on the count and the analysis is a truncated
  minimization. That is a choice rather than a fault, but it makes `ninner` the
  cost knob and the convergence knob at once, so two experiments compared across
  different values of it are not comparable.
- ~~**LETKF inflation**~~ **Settled: `rtps: 0.95` alone**, in both `da/letkf`
  and `da/hybrid`, replacing the bundle unit test's three simultaneous
  mechanisms. With no perturbed forcing and a time-lagged ensemble, relaxation
  to the prior spread is the only thing holding the spread up at all.

## The mass field, and why writing it back does not deliver the analysed sea level

`sea_water_cell_thickness` is a *background* variable in every DA layer and an
*analysis* variable only where an experiment names it, and `ackbar.writeback`
writes exactly the analysis list. `writeback.place_thickness` is what writes it
when named, by scaling each column rather than writing the analysis cell by
cell; that function carries the recipe and the argument for it.

**Three fields in the restart claim to be the free surface, and only one is the
state.** `h` is the state. `sfc` is `CS%eta`, the split-RK2 dynamics' free
surface (`MOM_dynamics_split_RK2.F90`). `ave_ssh` is the coupler-facing sea
level, averaged over the coupling step and inverse-barometer adjusted
(`MOM.F90`). Both `sfc` and `ave_ssh` are read back from the restart when
present and recomputed from `h` only when absent, so the claim this section used
to make, that `ave_ssh` is "a diagnostic MOM6 recomputes, so that write is
discarded at the first step", was too strong. It is read. It is simply not
dynamical, and it is refilled from the model's own diagnosis at the first
coupling step.

A writeback that moves `h` and leaves `sfc` therefore hands the barotropic
solver a free surface disagreeing with `sum(h)` by exactly the increment. MOM6
closes that gap itself, through `bt_mass_source` (`MOM_barotropic.F90`), which
forms `d_eta = eta_h - eta` and applies it as a mass source, so `h` wins;
`BOUND_BT_CORRECTION = True` caps the rate at which it does.

**Whether that loses anything is a different question for each solver, and the
answer turns on `BOUSSINESQ = True`.** The model conserves volume, not mass, so
a column's sea surface height is `sum(h) - D` and nothing else, and a
temperature increment does not raise sea level instantaneously. What it does is
change the density, and the model's own barotropic adjustment then moves volume
until the free surface matches. Steric sea level here is something the model
*produces* from density, not something a state carries.

- **A variational analysis over the static B loses nothing.** With
  `unbalanced ssh` at zero its whole SSH increment is steric by construction, so
  the temperature and salinity increment *is* the sea level increment, written
  in the field that carries it, and the model realizes the height over the
  following hours.

  The reason given here used to be that writing `h` too "would apply the same
  information twice, once as the density anomaly and again as the volume that
  anomaly implies". **That reasoning is wrong and the conclusion survives it.**
  The model relaxes towards hydrostatic and barotropic balance rather than
  adding a steric response on top of whatever surface it was handed, so a
  pre-balanced state is reached sooner, not twice over. What makes skipping `h`
  correct here is that the variational SSH increment is a deterministic function
  of the T and S increment the model already receives, so nothing is lost by
  omitting it, only a few hours of adjustment.

- **An ensemble filter has no balance operator, and its increment is coherent
  anyway.** The ensemble
  component carries no balance operator, only `covariance model`, `members` and
  `localization`. It does not need one: localization here is horizontal and
  there is no cross-variable localization, so the covariance is fully
  multivariate and a member's sea level perturbation carries its correlation
  with that member's temperature and salinity. It measures out. Over the twenty
  members at 2015-07-31 the perturbations give `corr(sum(h), ave_ssh) = 0.87`
  with spreads of 1.68 cm and 1.50 cm: the members are physically consistent
  states and the covariance encodes the steric relationship empirically.

  So the filter's increment is coherent, and `h` is part of it: `h` is in
  `background variables`, and `oops::LocalEnsembleDA` sets the increment
  variables to the state variables when no `increment variables` key is given,
  which ACKBAR's template does not give.

- **A hybrid is a mixture**, and the ensemble half is where the question below
  applies.

### The premise this section used to rest on does not reproduce

It claimed that the control increment gives "2.1 cm rms of sea level against a
0.95 cm `ave_ssh` increment, so the mass field carries more than twice the sea
level signal and it is the part discarded". **That sentence is what justified
writing the mass field back at all, so it is recorded as withdrawn rather than
quietly renumbered.** Read from `ocn.incr.incr.*.nc` directly, control member,
on two cycles: `sum(dh)` 0.0227 m against `d(ave_ssh)` 0.0224 m, and 0.0251
against 0.0248. Ratio 1.01 and 1.02, correlation 0.98 to 0.99, regression slope
1.00. The two fields carry one signal, so there is no separable "barotropic
residual" that `h` holds and `ave_ssh` lacks.

Read the increment file rather than differencing states, and if you do reach for
a `bkg/` record, take the restart it names rather than the one whose directory
shares its stamp. **`run/<cycle>/rst` holds what that cycle's forecast
*produced*, so it is valid at the *next* cycle**, and a record carries a
`source` attribute naming the set it came from. Comparing `bkg/<stamp>` against
`run/<same stamp>/rst` compares two states twenty four hours apart and reports
metre-scale disagreement in `h` that is entirely the time offset. Against the
set it names, a record agrees to 3.7e-06 relative, which is the quantization its
own `contents` attribute declares: five significant digits, not a restart.

### Where the analysed sea level actually goes

It is written twice already, and neither route is `h`.

- **The steric part travels in temperature and salinity.** The steric height
  implied by the increment's own T and S, `sum_k [alpha_k dT_k - beta_k dS_k]
  h_k` with alpha and beta evaluated at the background state, correlates with
  the analysed `ave_ssh` increment at **0.82** on both measured cycles, with
  regression slopes of 0.78 and 0.91 and matching magnitudes. The model receives
  that and regenerates the height over the following hours.
- **The geostrophic part travels in the velocities.** The surface geostrophic
  velocity implied by the analysed sea level increment correlates with the
  increment's own surface `u` and `v` at **0.46 to 0.52**, amplitudes within
  10%. The members are balanced states, so their velocity and sea level
  perturbations are mutually geostrophic and the filter's increment inherits it.

What is left over has neither a density signature nor a geostrophic one: a
sub-deformation-scale mass anomaly. Handing one of those to a rotating fluid
poses a Rossby adjustment problem whose answer is standard. The final state is
set by the potential vorticity, the retained fraction of the height anomaly goes
roughly as `L^2 / (L^2 + Ld^2)`, and below the deformation radius the mass
anomaly radiates away as gravity waves while the velocity field survives. On
`gom_25km`, where `Ld` is about 45 km and localization is 1.5 Rossby radii, a
written thickness increment retains about 10% of its amplitude after a day and
its residual correlates with what was applied at **+0.04**. What survives is
unrelated adjustment, not a damped copy.

So writing `h` for an ensemble solver is not wrong, and it is not a fix either.
It sends a third copy of information the analysis already sent twice, plus a
residual the model is obliged to radiate. It does let RTPS reach the mass field,
whose spread otherwise decays from 0.233 to 0.116 over twenty cycles with no
inflation applied to it at all, but a spread gain that radiates within the cycle
is not a spread gain at the next analysis time.

When it is written, it must be written as a column scaling. Writing the analysis
cell by cell is a model crash rather than a bad answer: the analysis itself puts
0.30% of this domain's cells at or below zero thickness, which MOM6 reports
several steps downstream as `adjust_interface_motion: implied h<0`. Scaling each
column by `(total + delta) / total` moves exactly the column integral and
preserves positivity by construction, which is what `writeback.place_thickness`
and `tools/ensemble-recenter.py` both do.

### `sfc` is worth fixing, and fixing it changes nothing

Moving `sfc` by the same column delta makes the restart internally consistent at
time zero rather than after a few barotropic sub-steps, and it is worth doing on
those grounds alone. It does not change the outcome. Retention rises from 10% to
15% of the applied amplitude, but the projection of the residual onto the
increment is 0.0042 without it and 0.0044 with it, so the extra is uncorrelated
adjustment rather than better-preserved signal. It should not be described as
harmful. Any claim that it is, resting on wall-clock run times, is measuring
contention on a box that is also running an experiment.

### The resolution dependence, which is the part that generalizes

The controlling parameter is `L / Ld`, the increment's horizontal scale against
the deformation radius, and it is not fixed across this project's domains.

- On the regional GoM domains localization is 1.5 Rossby radii, so `L` tracks
  `Ld` by construction and the ratio stays near one. Going from 25 km to 4 km
  resolves finer structure and pushes more of the increment below `Ld`, so more
  of the mass anomaly radiates and **the conclusion strengthens**. Sampling
  error argues the same way: twenty members against far more degrees of freedom
  makes the non-steric residual likelier to be noise, and writing it would put
  that noise straight into the free surface.
- On a coarse global domain the localization radius falls below the grid and is
  floored at two grid cells, so `L` is set by the grid and exceeds `Ld` widely,
  most of all at high latitude where `Ld` collapses. In that regime the mass
  field is the retained quantity rather than the radiated one, and **this
  section's conclusion may reverse**. `OM_1deg` and the high-latitude part of
  `OM4_025` are places to re-measure rather than to assume.

### The observation-space ranking

The ranking over three shared cycles is monotone in how much of the covariance
is static (`adt_hy2a` at +37.2%, +28.6% and +1.7% for 3D-FGAT, the hybrid and
the EnVar, with the SST platforms retaining 65 to 72% of 3D-FGAT's skill where
the altimeters retain 5 to 29%). This section used to propose one short EnVar
with `sea_water_cell_thickness` added to the analysis variables as the
discriminating run. **That run is no longer worth its cycles.** Sea level does
reach the model, through T and S at correlation 0.82 and through the velocities
at 0.5, so the writeback is not what costs the altimeters their skill. The
remaining candidate is the covariance, which is what a twenty member ensemble
sampling dense local SST well and deep mass structure badly would predict.

That last step is an inference from the delivery routes above, not a measurement
of altimeter skill, and it stays labelled as one until an experiment says
otherwise.

Two settings changed alongside them, neither a placeholder and both worth
recording because they are departures from soca-science:

- **`rossby mult: 1.5`**, up from 1.0, in both filter layers. At one Rossby
  radius on a domain that barely resolves one, each analysis point sees too few
  observations for a rank-limited update, and the trade against some spurious
  long-range correlation is worth taking at these resolutions. The 500 km halo
  still clears it by a wide margin in the Gulf and by much less near the equator,
  so a global domain has to revisit the two together.
- **`ninner: 20`**, up from 10.

Two more, from the same sweep, that bear on this run specifically:

- **`ENERGYSAVEDAYS` on `gom_25km`** means the NaN and truncation guards only
  ever see step zero, so a forecast that blows up mid-cycle exits 0 and writes a
  restart set of NaNs. On a 180 day spinup that is a real exposure.
- **The hybrid inherits the variational land mask (0.9)**, so its filter sees
  fewer coastal observations than `osse25-letkf` does at the same domain. Those
  two experiments are then not comparable, which defeats the reason they are in
  the same table.

## Open choices

- **Cycle length.** `PT24H`. `PT12H` doubles the cycle count and the analysis
  cost for a domain whose signal barely evolves in a day.
- **Spinup length.** Decided by the kinetic energy plot, not in advance.
- **One forcing source for every experiment.** Truth reads ERA5 and the two
  ensemble-forcing experiments read GEFS, which is the pairing a skill number
  needs ([`forcing.md`](forcing.md)). Every other experiment inherits no forcing
  layer and runs on the climatology, so the suite is not yet internally
  comparable: see the first caveat at the top of this file. It needs the
  deterministic GEFS source layer and one line per experiment, not a decision.
