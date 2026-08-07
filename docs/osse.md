# The Gulf of Mexico plumbing OSSE

Phase 11 in [`build-order.md`](build-order.md). This file is the plan for the
first one: what it runs, in what order, and what has to be built before any of
it can run.

## What this is, and what it is not

It is a **plumbing proof**. The claim it is allowed to make is that a nature run,
a synthetic observation archive, a sampled ensemble, five DA methods and a
state-space verification all connect to each other and produce numbers that are
not obviously wrong. It exists so that the first time those pieces meet is not
also the first time an answer depends on them.

It is **not** a benchmark, and nothing computed here should be quoted. Four
reasons, all structural and none of them fixable inside this run:

- **It is a perfect-model identical twin.** Truth and every experiment use the
  same model, the same resolution, the same forcing and the same open boundary,
  so there is no model error at all. Analysis skill measured against an identical
  twin is flattering by construction. `build-order.md` argues for a fraternal
  twin (nature at 4 or 8 km against DA at 12 or 25 km) and that is right and is
  the next version, not this one.
- **`gom_25km` is barely eddy permitting.** The first baroclinic Rossby radius in
  the Gulf is 35 to 45 km, so at 25 km the Loop Current sheds rings reluctantly
  and there is very little mesoscale for altimetry to correct. See
  [`domains.md`](domains.md). This is the domain to prove plumbing on and the
  wrong domain to measure anything on.
- **The atmosphere is a climatology and the open boundary is frozen.** No
  synoptic variability enters anywhere, so the only source of divergence between
  truth and experiment is the initial condition, and the ensemble has nothing
  maintaining its spread except the filter. See `domains.md` again.
- **The DA settings are not tuned.** Several of them are known placeholders; see
  [Settle these before it runs](#settle-these-before-it-runs).

Stating this in the plan rather than in the write-up is deliberate. An OSSE that
produces a plausible skill number is very easy to over-read six months later.

## The shape of it

Six stages. Each is an ordinary ACKBAR experiment or an offline tool, and
nothing here is a special execution mode.

```
A  spinup      free run, ~6 months, from the existing cold start
B  truth       free run, 60 days, continuing A, cycled at the DA cycle length
C  promote     truth restarts become a read-only archive; sample the ICs
D  observe     synthetic sst / adt / profile obs from the truth trajectory
E  experiments free, 3dvar, 3denvar, letkf, hybrid, 4dletkf, all from one IC set
F  verify      state space against the truth archive, obs space against departures
```

### A. Spinup

An `osse-spinup` experiment: `domain/gom_25km`, `model/mom6sis2`, `da/none`,
starting from a cold start valid at **2015-01-05 00:00**, rebuilt for this with

    tools/coldstart-ic.sh gom_25km 2015-01-04T12 12 hycom-smoke

**On a synoptic hour, deliberately.** The existing smoke initial condition is
valid at 01:00, because the cold start that produced it began at 13:00, and
every tier 3 experiment cycles at 01Z and 13Z as a result. That is nobody's
convention: analysis times are 00/06/12/18Z everywhere in operational and
reanalysis practice, the observation archives are binned on them, and an
experiment on the odd hour is one whose plots have to be explained every time
they are shown.

The chain is what makes this stick, so it has to be got right at the top. A
cold start's product is named for the *end* of its integration, `cycle.start`
has to equal the initial condition's valid time or every window sits offset
from its background, and so the hour chosen for the cold start is the hour every
experiment downstream of it cycles on, permanently. The smoke initial condition
stays as it is: the tier 3 experiments are pinned to it and rebuilding it would
churn nine of them for a cosmetic gain in something no one plots.

`P5D` cycles, run until the currents stop changing character rather than for a
fixed count. Around 36 cycles (180 days) is the expectation and not the
instruction. What decides it is a plot of domain kinetic energy and of the Loop
Current intrusion latitude against cycle: the spinup is over when the trend is
gone and only the seasonal cycle remains. NCAR/CORE climatology does have a
seasonal cycle, so a flat line is not what to wait for.

Cycled rather than run as one long forecast because a 180 day single job has one
failure mode with no checkpoint, and because cycling is the thing being proved.
The cost is 36 restart handoffs, which is a feature: if the handoff is going to
lose the ocean it should do it here and not in stage B.

### B. Truth

An `osse-truth` experiment continuing from the last restart of A, `da/none`,
**cycled at exactly the DA cycle length** so that every restart set it writes is a
truth state valid at some experiment's analysis time. 60 cycles at `PT24H`.

It also carries `forecast.slots: PT6H`. Four extra restart writes per cycle is
nearly free (FMS dumps in place on a coupled step), and it buys two things: an
observation can be sampled from the truth state nearest its own time rather than
from the analysis-time state, and 4DLETKF can be verified against a truth that
actually has sub-window structure. Without slots, every sub-window comparison in
the experiment is being scored against a truth that does not move inside the
window, which is the exact criticism this OSSE exists to remove.

60 days rather than 21 because the extended forecasts launched from the last DA
cycle need truth to verify against out to their own lead time, and because
having truth on both sides of the DA period makes the ensemble and control
sampling in C possible without reaching into the DA period itself.

### C. Promotion and the initial conditions

**Promotion** copies `exp/osse-truth/run/<date>/rst/` and its slot states to
`$ACKBAR_STATIC_ROOT/truth/gom_25km/osse-2015/<YYYYMMDDHH>/`, keyed by date
rather than by cycle number, because an archive has no cycle numbers and the
experiments reading it number their own cycles differently. This is the same
argument the observation archive layout already rests on; see the header of
`paths.py`. Once promoted the truth is read-only and the truth experiment's own
output can be reaped.

**The control initial condition** is one truth date, chosen outside the DA
period.

> **What the quarter degree OSSE actually does, and why it differs.** The
> control is not a truth date at all: it is GLORYS 2014-07-10 asserted to be an
> estimate of 2015-07-10, built by `tools/fetch-glorys.py ic --valid-at` and
> settled by `osse25-spinup`. Drawing the control from the truth run means the
> experiment starts from a state produced by the same model at the same
> resolution, which understates the error; drawing it from a reanalysis of a
> different year gives a real ocean with an independent Loop Current phase and
> no shared model lineage at all.
>
> The year matters more than the offset. A control drawn from 2015, two days
> before the truth's own launch, sat 0.022 m from the truth in sea surface
> height, which is below the altimeter noise: nearly every departure the
> analysis saw was noise it could not remove, and the reported skill was
> roughly half what the analysis achieved. The same week of 2014 is 0.178 m.
> Lagging within 2015 reaches a similar number but buys it by moving up the
> seasonal cycle, which is bias rather than mesoscale error and rewards a
> different scheme.

**The members** are `N` further truth dates, sampled at random from the
truth run, then recentred:

    member_i  :=  control + (state_i - mean over i of state_i)

so the ensemble mean is exactly the control and the spread is the truth run's
own climatological spread. This is a time-lagged ensemble and it is the cheapest
honest way to get spread out of a model with no perturbed forcing.

> **This part is not built and its absence is measurable.** `tools/ensemble-ic.sh`
> draws members from the static background error with `soca_enspert.x` instead,
> which its own header is explicit about. The result was underdispersive against
> the actual error by a factor of twelve in sea surface height and eighteen in
> temperature, flat for the whole run, which makes a 50/50 hybrid into 3DVar
> with the static B at half weight. That is not a tuning problem: static
> perturbations carry B's correlation scales and none of the flow structure, and
> nothing in a run where every member sees the same atmosphere and the same open
> boundary will grow them.
>
> Recentring on truth dates is one fix and it leaks the truth's model and
> resolution into the experiment. The other, now that `--valid-at` exists, is to
> draw the members the way the control is drawn: `N` GLORYS dates far enough
> apart to be independent, each asserted at the control's date and settled the
> same way, then recentred. Every member is then a real ocean with real
> mesoscale structure, and none of them is the truth.

Three constraints on the sampling, all worth stating because getting any of them
wrong makes the experiment look better than it is:

- **Every sampled date is excluded from a window around the DA start**, so that
  no member is accidentally very close to the truth at cycle 1. Ten days either
  side.
- **The control is not the truth at the DA start date**, which is where the
  initial error comes from. It is the whole reason there is anything to correct.
- **Recentred states are not model states.** The sum is imbalanced, and salinity
  and layer thickness can leave their physical range. The recentring step
  therefore has to clip thickness and salinity and report how much it clipped;
  a silent clip is a spread that is not the spread that was sampled. One cycle
  of forecast reconciles most of the imbalance, which is why cycle 1's analysis
  is not the one to read.

### D. Observations

Three platforms, generated from the truth trajectory:

| Platform | Variable | Error | Layout | Per cycle |
|---|---|---|---|---|
| `sst_noaa19` | `seaSurfaceTemperature` | 0.5 K | swath | ~600 |
| `adt_3a` | `absoluteDynamicTopography` | 0.05 m | along track | 3 tracks |
| `insitu_pfl` | `waterTemperature` | 0.2 K | profiles | ~20 casts |

The first two exist as observer layers and as generator platforms already. The
profile is new in both places and is the interesting one: it is the first
subsurface observer in the repository, so it is the first thing that exercises
the vertical background error at all. Ported from soca-science's
`insitu_pfl.yaml`, operator `InsituTemperature`. Salinity profiles are a second
`obs space` on the same file and are a follow-on, not part of the first run.

Values are the truth interpolated to the observation location and time, plus
Gaussian noise at the stated error. **The error the generator adds and the error
the observer assimilates are the same number**, which makes this a
well-specified-R experiment. That is the easy case and it is the right one first;
a mis-specified R is a later variation and it needs the two numbers to be stated
separately in the generator before it can be.

### E. The experiments

Six, differing by their inherited DA layer and by nothing else. Same domain,
same dates, same control IC, same ensemble, same observation archive.

| Experiment | Layers | What it is for |
|---|---|---|
| `osse-free` | `da/none` | the null. Every skill number is measured against this |
| `osse-3dvar` | `da/variational` | deterministic, static B |
| `osse-3denvar` | `da/envar` | deterministic, ensemble B |
| `osse-letkf` | `da/letkf` | ensemble filter |
| `osse-hybrid` | `da/variational` + `da/hybrid` | both, with recentring |
| `osse-4dletkf` | `da/letkf`, `solver.window.type: '4d'` | the phase 9 path |

`PT24H` cycles, 21 of them, starting ten days into the truth period. 4D
variational modes are absent because `validate` refuses them: there is no pseudo
model wired for a variational FGAT yet, and `4d` is currently allowed only for
`letkf` and `none`. That refusal is correct and this OSSE is not the place to
change it.

Every experiment carries `forecast.extended: {length: P5D, every: P5D}` on the
control member, which gives four long forecasts apiece to verify at leads of one
to five days. This is the part that most needs a truth run: a five day forecast
scored against its own analysis says nothing.

### F. Verification

Two spaces, and the OSSE is the first thing that needs either of them.

**State space**, against the truth archive: domain RMSE and bias for SST, SSH,
and temperature and salinity by level, for the background and the analysis,
every cycle, per experiment. Restricted to the domain interior so that the
sponge next to the open boundary is not being scored. For the ensemble methods,
also ensemble spread against ensemble mean error, which is the one diagnostic
that says whether the filter is consistent rather than merely running.

**Observation space**, from the `ombg` and `oman` the analysis already writes:
count, mean and RMS departure by platform, before and after, plus how many
observations each filter rejected. This is cheap, it is the diagnostic that
localizes a broken observer, and it does not depend on the truth at all.

**The comparison across experiments** is a separate step and a separate tool.
It reads each experiment's verification output and produces one table and one
figure set. It is the stated premise of the repository and it does not exist
yet; see [What has to be built](#what-has-to-be-built).

## What has to be built

In dependency order. Items 1 to 3 are workflow bodies that are currently
declared and empty (`DEFERRED` in `run.py`), and the OSSE is what forces them.

1. **`post.obs`.** *Done.* Departure statistics from what the analysis writes.
   No new inputs, no truth, smallest of the three.
2. **`post.state`.** *Done.* A compact per-cycle NetCDF holding the fields
   verification reads. This is what lets `cleanup` reap restarts on schedule and
   still leave a state-space record; without it the choice is between keeping
   every restart and having nothing to verify.
3. **The long forecast's own products.** *Done.* `forecast.ext` no longer writes
   into the cycling forecast's restart directory, which was a collision only the
   real model could produce; its trajectory goes to `run/<init>/fcst/`,
   `post.fcst` reduces the kept leads to `fcst/<init>/F###/`, and `hofx.ext`
   evaluates the whole trajectory with `soca_hofx.x` into
   `fcst/<init>/obs/F###/`. Two cadences configure it: `forecast.extended.interval`
   for the kept states, `forecast.extended.slots`, finer, for the trajectory the
   departures are computed against.
4. **`verify`.** Scoring against a truth archive. Needs one new config key, a
   verification source naming the promoted truth, and it has to tolerate the
   source being absent (a real-observation experiment has no truth). It reads
   `bkg/`, `ana/` and `fcst/` and keys off artifact existence rather than job
   state, for the reason given under the graph below.
5. **Truth promotion.** *Done.* `tools/local/promote-truth.sh` copies
   `exp/<name>/run/<date>/rst/` and its slots to
   `$ACKBAR_STATIC_ROOT/truth/<domain>/<name>/<date>/`, keyed by the date each
   state is *valid* at, which for a restart set is one cycle after the directory
   holding it. It refuses an experiment whose solver is not `none`.
6. **`obs-archive-osse.py --truth-run`.** *Done.* Reads the promoted archive and
   samples the state nearest each observation's own time, refusing rather than
   clamping an observation outside it. The single-state mode stays, because the
   smoke archive tier 3 uses is built with it, and the draw order was preserved
   so that archive still reproduces bit for bit.
7. **The profile platform**, in the generator and as
   `config/layers/obs/insitu_pfl.yaml`. `config/obs/obsop_name_map.yml` already
   carries `waterTemperature` and `depthBelowWaterSurface`, so the alias side is
   done; what is new is a generator that writes a vertical coordinate at all,
   since both existing platforms are surface only.
8. **`tools/ensemble-ic.py sample`.** The date-sampled, recentred ensemble of
   stage C, with the clipping report. The existing `plan`/`place` verbs stay:
   perturbing one state from static B is a different ensemble and still the
   right one for a smoke test. The control half of stage C is done:
   `tools/restamp-ic.sh` promotes a truth state to the IC stage under a
   different date, which is where the initial error comes from. It is one line
   of `coupler.res`, and it is necessary because a restart carries its own clock
   and MOM6 takes the date from there rather than from the namelist.
9. **The comparison tool.** Reads N experiments' verification output, writes the
   table and the figures.

Item 3 did change the graph, which nothing else here does: `hofx.ext` and
`post.fcst` are new leaves hanging off `forecast.ext` elementwise. Everything
else above leaves the graph, the scheduler layer, the restart handoff and the
solver configuration alone. That is the point of running this now rather than
earlier: the workflow is finished, and this is a consumer of it.

One thing to revisit before this runs on anything larger than `gom_25km`.
`hofx.ext` evaluates the whole forecast in one application run, and the
four-dimensional application holds every state it is given at once: five days at
a three hour cadence is forty states resident. On a bigger domain that is the
first thing that will not fit. The fallback is sections, a day at a time, and it
needs no graph change and no path change, only a loop inside `soca.hofx4d` and a
section length to configure. `fcst/<init>/obs/F###/` is already keyed by the end
of the section evaluated for exactly that reason.

## Cost

Model time on `gom_25km` is 6.3 s per simulated day on 8 ranks
(`domains.md`), which makes the nature run almost free and the experiments
dominated by their analyses.

| Stage | Simulated days | Model wall clock |
|---|---|---|
| A spinup | 180 | ~19 min |
| B truth | 60, plus slot writes | ~7 min |
| E, per ensemble experiment | 21 cycles x 11 states | ~25 min |
| E, extended forecasts | 4 x 5 days | ~2 min |

Five ensemble experiments plus the free run come to a few hours including
analyses, on a box where nothing runs concurrently anyway. The whole OSSE is
under a day of compute and the schedule is set by the eight items above, not by
the machine.

Disk is around 10 MB per restart set. Twenty-one cycles times eleven members
times six experiments is roughly 14 GB, or 55 GB with sub-window states kept, of
800 GB free on `/data`. Comfortable, but `cleanup.keep_every` and a working
`post.state` are still the right way to run it, because that is how it will have
to be run at `gom_12km` and the OSSE is where that gets proved.

## Settle these before it runs

Four analysis settings are known placeholders, recorded in the review sweep, and
every one of them changes what this OSSE produces. They are science decisions
and they belong in the configuration before the experiments run, not in the
caveats afterwards.

- **`unbalanced ssh: {}`** gives 0.1 m of unbalanced SSH error across the whole
  Gulf where soca-science set it to exactly zero, and the SSH increment is
  written to `ave_ssh`, which MOM6 overwrites within a timestep. Part of every
  ADT innovation is being fitted into a field the model erases.
- **`sst: {fixed value: 1.0}`** is a flat placeholder. The GODAS field is in the
  checkout at `pkg/jedi/soca/test/Data/godas_sst_bgerr.nc`, mean 0.49 K with
  real structure.
- ~~**`ninner: 10`**~~ **Settled: 20.** The `1.0e-10` reduction target still
  never fires, so every solve stops on the count and the analysis is a truncated
  minimization. That is a choice rather than a fault, but it makes `ninner` the
  cost knob and the convergence knob at once, so two experiments compared across
  different values of it are not comparable.
- ~~**LETKF inflation**~~ **Settled: `rtps: 0.95` alone**, in both `da/letkf`
  and `da/hybrid`, replacing the bundle unit test's three simultaneous
  mechanisms. With no perturbed forcing and a time-lagged ensemble, relaxation
  to the prior spread is the only thing holding the spread up at all.

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
  fewer coastal observations than `osse-letkf` does at the same domain. Those
  two experiments are then not comparable, which defeats the reason they are in
  the same table.

## Open choices

Recommendations first, since none of these blocks writing the code.

- **Cycle length.** `PT24H`. `PT12H` doubles the cycle count and the analysis
  cost for a domain whose signal barely evolves in a day.
- **Ensemble size.** 10 members. Six is what tier 3 uses and is too few for a
  rank-limited covariance to be worth reading; 20 doubles the model cost for a
  proof that does not need it.
- **Spinup length.** Decided by the kinetic energy plot, not in advance.
- **4DLETKF.** Include it. It is the only 4D path `validate` allows, the slot
  states already work on the free run, and leaving it out means phase 9 is never
  exercised end to end.
- **Salinity profiles.** Not in the first run. Temperature profiles are already
  the first subsurface observer and one new thing at a time.
