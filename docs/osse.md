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

- **The atmosphere is a climatology and it is shared.** NCAR/CORE, no synoptic
  variability anywhere, and truth and experiments read the same file. So the only
  source of divergence is the initial condition, and the ensemble has nothing
  maintaining its spread except the filter. This is the largest of the three and
  the one to fix first.
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

## The mass field is never written back, and whether that is wrong depends on the solver

`sea_water_cell_thickness` is a *background* variable and not an *analysis*
variable in every DA layer, and `ackbar.writeback` writes exactly the analysis
list. Measured across `osse25-4dletkf`, `osse25-3dfgat` and `osse25-hybrid`, the
control member's analysed restart minus its own background gives `max|dh| = 0`
in all three, while `ave_ssh` moves by up to 0.18 m. `ave_ssh` is a diagnostic
MOM6 recomputes, so that write is discarded at the first step.

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
  following hours. Writing `h` as well would apply the same information twice:
  once as the density anomaly and again as the volume that anomaly implies.
  Skipping it is correct rather than merely harmless.

- **An ensemble filter loses the barotropic part, and that needs stating
  carefully, because the obvious version of the claim is wrong.** The ensemble
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
  which ACKBAR's template does not give. Read `ocn.incr.incr.*.nc` rather than
  differencing two states, and `osse25-4dletkf`'s control increment at
  2015-08-01 is `Temp` 0.2546 rms, `Salt` 0.0628, **`h` 0.0660**, `ave_ssh`
  0.0095. Summed over levels the thickness increment is **2.1 cm rms of sea
  level against a 0.95 cm `ave_ssh` increment**, so the mass field carries more
  than twice the sea level signal and it is the part discarded.

  What that costs is *not* the whole sea level constraint, because the
  temperature and salinity increment is written and the model regenerates the
  steric response from it. It is the **barotropic residual**: the volume
  redistribution the ensemble inferred, with no density anomaly behind it, which
  nothing else reconstructs. The same omission also means RTPS never reaches the
  mass field, whose spread decays from 0.233 to 0.116 over twenty cycles with no
  inflation applied to it at all.

- **A hybrid is a mixture**, and the ensemble half's barotropic contribution is
  the part that is lost.

The tempting next step is to blame this for the observation-space ranking, which
over three shared cycles is monotone in how much of the covariance is static
(`adt_hy2a` at +37.2%, +28.6% and +1.7% for 3D-FGAT, the hybrid and the EnVar,
with the SST platforms retaining 65 to 72% of 3D-FGAT's skill where the
altimeters retain 5 to 29%). **Do not.** Since the written temperature and
salinity increment carries the steric response, altimetry does reach the ocean
without `h`, and a twenty member ensemble that samples dense local SST covariance
well and deep mass structure badly predicts the same ranking. The discriminating
run is one short EnVar with `sea_water_cell_thickness` added to the analysis
variables: if altimeter skill jumps it is the writeback, and if not it is the
covariance.

So the fix is not "add `sea_water_cell_thickness` to every analysis variable
list". It is to add it to the *ensemble* solvers, where the increment exists and
is thrown away, and to leave the balanced variational path alone. And it needs
the recipe `tools/ensemble-recenter.py` already derives: adding the offset per
layer drives 0.8% of this domain's cells negative, which is the `implied h<0`
crash, while scaling each column by `(total + delta)/total` moves exactly the
column integral and preserves positivity by construction.

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
- **What to do about the shared climatological forcing**, which is the caveat at
  the top of this file and the reason no ensemble number here is believable.
  Perturbed per-member forcing, and different forcing for truth than for the
  experiments, are the two halves of it.
