# The analysis, and what happens to it

Up to four tasks, and the last one carries the risk.

| Task | Runs | Present when | Produces |
|---|---|---|---|
| `da` | `soca_var.x`, or the ensemble filter's chain below | there is a solver | the control's analysis, the increment, `ombg` and `oman` |
| `da.ens` | the ensemble filter's chain | `ensemble.source: letkf` beside a variational solver | every member's analysis, and the ensemble's own departures |
| `recenter` | `soca_ensrecenter.x` | the covariance reads an ensemble | every member's analysis, moved onto the control's |
| `writeback` | python | there is a solver | the restart set the next forecast starts from |

`da`, `da.ens` and `recenter` are each one MPI *job* for the whole cycle;
`writeback` is a member array. An ensemble filter's job runs several
applications inside that one job rather than a single one: see below. The handoff between them is
`run/<date>/ana/mem###/analysis/`, and the handoff out of `writeback` is
`run/<date>/ana/mem###/` itself, which the forecast reads exactly as it reads a restart
set the model wrote.

**`da` is the analysis that produces the control's answer, whichever solver that
is.** A 3DVar and an LETKF both have only that one; a hybrid additionally has
`da.ens`, which is what maintains the ensemble its covariance is drawn from.
They are two nodes rather than one node run twice because they are different
applications with different configurations, different resources and different
member cardinality, and because `soca_letkf.x` under a name that says `var`
would be the first thing to confuse anyone reading a queue.

`writeback` is solver-independent, because it reads a state and a background and
writes a restart set, and none of that depends on how the state was arrived at.
Which state it reads *does* depend: the control's is `da`'s analysis, and every
other member's is the recentred one wherever there is a recentring.

## Where things go

```
run/<date>/ana/mem000/                the analysed restart set: MOM.res.nc,
                                      ice_model.res.nc, coupler.res
run/<date>/ana/mem000/analysis/       what the application itself wrote
    ocn.ana.an.<date>.nc                the analysis state
    ocn.incr.incr.<date>.nc             analysis minus background
obs_out/<date>/<experiment>.<obs>.nc4 the departures
```

The subdirectory is not tidiness. `run/<date>/ana/mem###` is a *restart set*: writeback
fills it by copying every file of the background's, `model: persistence` fills
the next cycle's by copying every file of this one, and the forecast links all
of them into `INPUT/`. A state file loose among them is inert to the model and
is then carried forward by every cycle after it, one more each time.

## The document `soca_var.x` reads

Two files. `config/soca/var.yaml` is the *shape*: which blocks exist, what they
are called, where they sit, and why. `ackbar/soca.py` fills its `$(UPPERCASE)`
slots with the *values*, every one of which comes from somewhere the experiment
already says it. The only parts an experiment states directly are the ones
nothing else implies, and they live in `config/layers/da/variational.yaml`:
`background error` and `variational`.

**Three shapes, not one**, chosen by `solver.window.type` in `cost_template`.
They are sibling files rather than branches inside one, because two of the
differences are structural rather than a changed value:

| `window.type` | template | cost type | background |
|---|---|---|---|
| `3d` | `var.yaml` | `3D-Var` | one state, at the analysis time |
| `fgat` | `varfgat.yaml` | `3D-FGAT` | one state, at the window's start, plus a `PseudoModel` over the rest |
| `4d` | `var4d.yaml` | `4D-Ens-Var` | a list, one state per sub-window |

FGAT and 4D read the same files, the previous cycle's sub-window slots, and they
read them differently. FGAT's pseudo model list starts one step *after* the
window's start, because the state there is what the model steps from and is
named separately as the background. 4D-Ens-Var's list includes the start,
because nothing is being stepped: each sub-window's observations are compared
against the state at it, and the first sub-window's is the one at the start.
soca-science's `run.var.sh` places them the same way, arrived at independently.

**The first cycle of a `fgat` or `4d` experiment solves `3D-Var`**, because
nothing ran before it to write a trajectory: its background is the staged
initial condition and its window holds one state, which is what either cost
function degenerates to over one state. The document is named for what was
solved, so `da.<jobid>.var.yaml` in cycle 1 beside `da.<jobid>.varfgat.yaml`
afterwards is the record of which cost function each cycle used. That is the
only reason the three share an executable and not a name.

**`4d` requires an ensemble covariance**, and this is refused at graph build
rather than owed: a 4D-Ens-Var's four dimensions are its ensemble's, and
carrying a static B across the window needs a linear model SOCA does not have
for the ocean. `fgat` takes any covariance, because it solves one increment at
the window's midpoint.

One trap inside `var4d.yaml`, because it is the kind that is found by running
and not by reading: the analysis `output` is a single block and the increment
`output` is a list of them, one per sub-window. oops enrolls the first as a
`StateWriter` post-processor and hands it one state at a time, while the second
goes through `ControlIncrement::write` into `DataSetBase::write`, which asserts
there is exactly one time unless a `states` key is present. SOCA's own `4denvar`
test has no `final` block at all, so it never writes an increment and never
reaches that assertion.

The split is not the templating that soca-science did with `sed`, and the rule
that makes it not is narrow: a template holds a value only when nothing in
Python reads it. The moment a value is read on both sides, it becomes a slot,
because two spellings of a filename field is a `writeback` that opens a name
nothing wrote and reports an analysis that produced nothing. `exp`, `type` and
`datadir` are the three with teeth, and `tests/test_templates.py` refuses a
template that spells any of them out.

Four values are ACKBAR's own, and each of them is wrong by *omission* rather
than by being wrong, which is the failure mode worth knowing about:

**`variational.iterations[].geometry`.** `CostFunction::linearize` reads one per
outer iteration and throws without it.

**`background error.linear variable change.input variables` and
`output variables`.** Both are the analysis variables. Without `input
variables`, `oops::ModelSpaceCovarianceBase` holds a null pointer and
dereferences it the first time it evaluates Jb: the application reads the whole
background, builds every saber block, prints the diffusion operator it loaded,
and *then* segfaults, several minutes in and nowhere near the cause.

**`output`.** It writes the analysis, and it is also what makes the departures
complete. `oops::Variational` runs its final cost evaluation only when something
asks for output, and `CostJo` saves `oman` on that evaluation and nowhere else.
An analysis configured without an output writes `ombg`, no `oman`, and no
message about either.

**`final.increment.output.state component`.** A `ControlIncrement` is the model
increment plus the model and observation bias corrections, and it hands each of
the three its own subsection. Without the nesting the writer reports a missing
`datadir`.

## The document `soca_letkf.x` reads

**`soca_letkf.x` is the second half of `da`, not the whole of it.** The ensemble
filter runs its observer separately, in the same job, before the solver starts:
`soca_ensmeanandvariance.x` once per sub-window for the prior mean,
`soca_hofx.x` on that mean and then once per member, a merge into one file per
observer, and only then the solver with `driver: read HX from disk`.

The reason is memory. `oops::LocalEnsembleDA` holds its whole background
`StateSet` for the duration, so computing the departures inside it means twenty
members times every sub-window resident before the solve begins. Run outside,
the peak is one member's trajectory, and the solver reads one state per member
whatever the window type is, because a 4D-LETKF's four dimensions live entirely
in the departures. `docs/design.md` has the argument, what the merged file has to
contain, and the three ways the merge can be quietly wrong.

Two consequences show up in this document rather than in that one. The observers
here carry **no `obs filters`**: they ran in the observer, and their verdict
arrives in the merged file's `ObsError`, whose missing values are what the solver
reads as "not assimilated". And `obsdatain` is the merged file rather than the
archive, which is what `driver: read HX from disk` expects to find an ensemble
in.

What follows is the solver's document. The same construction, from
`config/soca/letkf.yaml` and the same builder as the variational one, with one
structural difference: the background is an ensemble. oops takes that either
as `members from template` (its own sense of the word: a `%mem%` pattern and a
zero padding) or as `members`, an explicit list. ACKBAR builds the list, and the
reason is worth knowing before anyone "simplifies" it back.

**The index a member is written out as is its position, not its number.**
`oops::DataSetBase::write` numbers what it writes by each state's place in the
list it was handed, so an ensemble of members 1, 2 and 4 comes back as files 1,
2 and 3. With a template that has an `except` in it, the input numbering and the
output numbering disagree exactly when a member is missing, which is exactly
when nobody is looking. With a list, the correspondence is one sorted list
against another and it is checked by count. The verbosity costs nothing in a
file nobody hand-edits, and it buys every member's background being a path
`ackbar validate` stats before anything is submitted.

Three things are ACKBAR's rather than a layer's:

**The driver.** `save posterior mean` is what gives the control member an
analysis at all. `save posterior ensemble` is the analysis. `LocalEnsembleDA`
throws by name when a flag is set and its output block is missing, which makes
this the one part of the document that fails loudly.

`do posterior observer` computes `oman`, and it is also **what writes the
departure files at all** for an ensemble filter. `oops::LocalEnsembleDA` saves
the obs space only when `!read HX from disk || do posterior observer`, so with
the departures read from disk, turning this off leaves the application exiting 0
having written no observation output whatsoever, `ombg` included.

What to know when reading the number: the posterior observer evaluates the
analysis, which this application holds as one state at the window's centre. In a
3D window that is the same comparison `ombg` got. In a four-dimensional one it
is not, because `ombg` came from the whole trajectory.

**`soca_var.x` does the same thing in an FGAT window**, which is what makes this
a property of the comparison rather than of the ensemble filter.
`CostFctFGAT::doLinearize` sets `fgat_` only on iteration 0, and
`finishLinearize` clears it and replaces both background and first guess with
the state at the midpoint; every later `runNL` therefore takes the single-state
branch. So `osse25-3dfgat`'s `oman` is a centre evaluation against a 4D `ombg`
too, and has been since it was first run. The two experiments' `oman` mean the
same thing as each other, which is the property that matters, and neither is
trajectory-consistent with its own `ombg`. Making them so means evaluating the
background trajectory plus the analysis increment at every slot through a second
observer run, for both solvers, and it is not built.

**The spread.** Prior and posterior variance are written every cycle. An
ensemble filter fails in two ways that look identical in any single analysis:
the spread collapses and every later cycle ignores its observations, or the
spread grows and the filter chases noise. Nothing else in the workflow records
which is happening.

**The control's analysis is the ensemble mean**, exactly, when the LETKF *is*
the experiment's analysis. ACKBAR computes nothing for `mem000`:
`LocalEnsembleDA` writes the posterior mean with `member` set to 0, and that
lands on the control's directory. It is also why a pure LETKF does not recentre:
the centre it would be moved onto is the mean it already has.

**Inside a hybrid the same application's mean is a diagnostic**, because the
control's answer came from the variational solve instead. It goes to
`run/<date>/ana/mem000/analysis/ensemble/`, one directory down, and so do the filter's
increment and its two spread files. Two of those four share a filename with the
deterministic analysis and its increment, and every one of those collisions
would leave a file that exists and holds the wrong state.

**A per-member writer has to be told `type: ens`.** `soca_genfilename` puts the
member index in a name for that type and for no other, so any other value has
six members writing one filename in turn and the application exiting 0 with the
last member's state in it. The type ACKBAR asks for and the type it names the
committed file with are therefore different: once the file is in that member's
own directory the index is redundant.

## Localizing an ensemble filter, which happens in observation space

**The two localizations in this workflow are different mechanisms and neither
implies the other.** A variational solve with an ensemble covariance localizes in
*model* space, by a Schur product with a SABER diffusion block; that block lives
in `config/layers/da/hybrid.yaml` and reaches `soca_var.x`. An ensemble filter
localizes in *observation* space, by tapering each observation's weight in the
local solve at each analysis point, and it never sees a SABER block at all:
`grep saber` over a generated `letkf.yaml` returns nothing, by construction. The
entries that do reach it are each observer's `$localization`, rendered to UFO's
`obs localizations` by `soca._observers`. `docs/background-error.md` covers the
model-space half.

The consequence that matters here is that only the observation-space half can be
told which observation it is for. A model-space localization is applied to the
increment as a whole, so it is one operator for every platform in the window;
this half is a list per observer. That is why the table below can give ADT and
SST different vertical treatment while the EnVar and the hybrid give every
observation the same, and it is why the EnVar's absent vertical localization is a
decision rather than a gap. `docs/background-error.md` makes that argument.

Three things about the observation-space half are silent when they are wrong,
and all three are load-bearing.

**The geometry has to iterate in three dimensions.** `soca::Geometry` reads
`iterator dimension` and defaults it to 2. With 2, `soca::GeometryIterator` never
advances its level index and `soca::Increment` packs the entire column into one
local state vector, so a single weight matrix is solved per column from every
observation inside the horizontal radius *at any depth* and applied identically
to all fifty levels. Vertical localization in that mode is not merely absent, it
is unrepresentable: a vertical entry would be evaluated at level one for the
whole column. ACKBAR does not state the dimension in a layer. It derives it, in
`soca._iterator_dimension`, from whether any observer in that document carries a
vertical entry, so the two halves cannot disagree. The derivation also runs the
other way, and that direction is about cost rather than correctness: three
dimensional iteration multiplies the number of local solves by the level count
and buys nothing when every observer is localized horizontally only, which is
`da/eakf`.

**`Rossby` must be the first entry in an observer's list.**
`soca::ObsLocRossby::computeLocalization` caches its result per horizontal point,
and on a cache hit it *assigns* the cached vector into the localization vector
rather than multiplying into it. `oops::ObsLocalizations::computeLocalization`
walks the entries in order into one vector that starts as ones, so the assignment
is harmless only while Rossby is the entry that sees the ones. Put a vertical
entry first and Rossby overwrites it at every level of a column but the first,
which is forty-nine levels in fifty, and the result is exactly the unlocalized
column the entry was added to prevent.

**The vertical coordinate is a model level index, not a depth in metres.**
`soca_geom_mod` fills the geometry's `vert_coord` with `real(jz)`, so the
iterator's third coordinate at level k is k and a `vertical lengthscale` counts
levels. A surface platform therefore says which level it measured with
`assign constant vertical coordinate to obs: true` and
`constant vertical coordinate value: 1`. Pointing at a depth field instead
(`ioda vertical coordinate: depth`) would compare metres against level indices,
so a 175 m observation would sit 175 levels from every level of the column,
localize to zero everywhere, and be assimilated by nothing, while the run
reported healthy.

Which family gets what:

| Family | Vertical entry | Why |
|---|---|---|
| `sst_*`, `sss_*`, `drifter_*` | constant coordinate 1 | one level measured, and it is the top one |
| `adt_*` | none | depth integrated, so there is no level to centre a taper on |
| `argo_*`, `glider_*` | none | by decision; see below |

SOCA's own `test/testinput/letkf3d.yml` draws the same line between its surface
temperature observer and its ADT observer, and it is the CI-verified schema for
this bundle.

### The horizontal radius, which is not the multiple it reads as

`rossby mult` is not the localization radius in Rossby radii, and the two knobs
in the same `$localization` list are not on the same scale.
`soca::ObsLocRossby::computeLocalization` (`ObsLocRossby.cc:47-57`) forms
`base value + rossby mult * rossby_radius`, floors that by
`min grid mult * sqrt(area)`, applies the optional `min value` and `max value`
clamps if they are set, and only then multiplies by `2/sqrt(0.3)` = 3.65 before
handing the result to Gaspari-Cohn as the hard cutoff (the "convert from
gaussian to gaspari-cohn width" line). ACKBAR sets `min grid mult: 1.0` and
leaves `base value` at its default of zero with neither clamp set, so here the
cutoff is 3.65 times `mult * rossby_radius`, or the floor where that is larger.
`rossby mult: 1.5` therefore cuts off at 5.5 Rossby radii, not 1.5. The 5.5 is
the only part of this that is a single number.

**The cutoff is a field rather than a constant**, because `rossby_radius` is a
field and `ObsLocRossby` reads it fresh at every analysis point. Over
`gom_25km`'s wet points it varies by better than a factor of two:

| percentile | 0 | 10 | 50 | 75 | 90 | 100 |
|---|---|---|---|---|---|---|
| `rossby_radius`, km | 27.3 | 31.2 | 38.1 | 43.6 | 55.4 | 61.9 |
| cutoff at `rossby mult: 1.5`, km | 150 | 171 | 209 | 239 | 303 | 339 |

So the honest statement of the horizontal reach at `rossby mult: 1.5` is about
150 km at the tightest point and about 340 km at the loosest, with a median near
210 km. Any single number quoted for it is wrong somewhere on the domain. To
re-measure for another domain or another multiple, read `rossby_radius` and
`mask2d` from that domain's `soca_gridspec.nc` in the experiment's static
directory; `tools/soca-gridspec.sh` is what writes the file, and the radius
comes into it from the bundle's `rossrad.nc`.

The floor is inert on this grid: `sqrt(area)` tops out at 27.1 km against a
smallest Rossby radius of 27.3 km, so `min grid mult: 1.0` never binds, even for
the profiles at `rossby mult: 1.0`. It is the term that takes over on a coarse
grid or at high latitude, where the radius falls below the cell.

That range is a measurement as well as an arithmetic claim. A single observation
placed in the deep central Gulf (see below) reaches to 209 km and is zero beyond
183 km; the two numbers differ because the cutoff is recomputed at each analysis
point from *that* point's Rossby radius rather than from the observation's, so
the edge moves with the field.

**The vertical entry carries no such factor.** `ufo::ObsVertLocalization` passes
`vertical lengthscale` to `oops::gc99` unscaled, so there the cutoff is the
lengthscale itself, in levels. A block that sets `rossby mult: 1.5` and
`vertical lengthscale: 10` is asking for 3.65 times the horizontal scale it
names and exactly the vertical one.

### The length scale, which is the least settled number here

`vertical lengthscale` is in levels, and the Gulf grid is `FNC1:2,5500,4,0.01` at
NK=50: 2 m at the surface, stretching downward. So the levels are not a uniform
ruler and the number has to be read against the grid it counts.

| Level | 5 | 10 | 15 | 20 | 25 | 30 | 40 | 50 |
|---|---|---|---|---|---|---|---|---|
| Bottom (m) | 10 | 21 | 41 | 91 | 210 | 465 | 1827 | 7643 |
| Centre (m) | 9 | 20 | 39 | 84 | 194 | 433 | 1721 | - |

Levels are localized by their centres, which is why both rows are here: a
lengthscale of 10 measures from level 1 to level 11, and what it reaches is
level 11's centre, not level 10's bottom. The two differ by about 10% in the
top hundred metres and the second row is the one to quote. Level 50's centre is
blank because no column in this domain is 7643 m deep, so the deepest levels
are squeezed onto the sea floor wherever they are wet.

The surface families use **10**. Gaspari-Cohn on that distance is a taper to a
hard cutoff at the scale rather than an e-folding scale, so a surface
observation reaches to 23 m, at about half weight by 10 m, and reaches nothing
below: the increment is bitwise zero from level 11 down, which is the first
level a distance of 10 puts at zero weight, and 23 m is where that level's
centre sits. The bundle's own test uses 5 on a 25 level grid, which on this one
would confine a surface observation to the top 10 m.

**The direction to sweep is up, not down.** Ten levels is 23 m, and the Gulf
mixed layer is deeper than that for most of the year: the water an SST
measurement is genuinely representative of extends to the mixed layer base, which
is levels 15 to 20 here, or 40 to 90 m. Localizing tighter than the true
correlation length throws away signal the ensemble was there to provide, and
restratifies a layer the model has mixed. Ten is the conservative end of a range
whose upper end is a physical argument rather than a guess, and it is chosen that
way because the failure it replaces was an increment at one ensemble standard
deviation from 1 m to 3400 m. Each family carries the value as a var
(`sst_vertical_localization_levels` and its two siblings), so a sweep is three
one-line overrides in an experiment and needs no block restated.

**The other axis is the ensemble size, and it is the one that ends the tuning.**
Vertical localization exists to suppress sampling noise, so it is normally turned
off altogether once the ensemble is large enough, 40 members or more, in an
ensemble filter and in an EnVar alike. This ensemble is 20, and these entries
exist for that reason rather than because a surface observation should never
reach below the mixed layer. Sweeping the scale is worth doing at 20 members;
carrying whichever value wins into a larger ensemble is not, because the right
answer there may be no entry at all.

### Why the profiles are not localized in the vertical

`argo_*` and `glider_*` update the whole column, and that is the decision rather
than a gap waiting to be filled. It is the conservative error in this
configuration rather than the dangerous one: a profile is the only thing in the
network that sees the vertical structure at all, so an over-reaching profile
spreads real information too far, while an over-reaching surface observation
spreads a surface signal into water it never touched.

The obvious config knob is not the answer, and reaching for it is worse than
doing nothing. A cast reports depth in metres while the geometry's coordinate is
a model level index, so `ioda vertical coordinate: depth` compares metres against
level indices and localizes a 175 m observation to exactly zero at every level,
deleting the only subsurface information the network has. Localizing a cast would
mean converting its depths to fractional level indices against the background's
own thicknesses, and that work is declined, not deferred.

The residual risk is worth naming, because it is what would reopen this: with the
profiles unlocalized, a deep observation reaches the surface through a 20 member
sample covariance, which is the same class of spurious correlation the surface
entries were added to remove, running the other way. That is the first thing to
check if a near surface field stays worse than the free run while the subsurface
improves.

The size of that risk has been measured at one point rather than argued. A
single 400 m Argo temperature observation in the deep central Gulf moves the
surface level of its own column by 0.024 K while it moves the thermocline by
2.11 K, so 1% of the response leaks to the surface. That is the cost of leaving
the profiles unlocalized, on one column at one depth, and it is small enough
that it does not by itself reopen the decision.

### Testing the localization with one observation

`tools/single-ob.py` is what those numbers come from. It runs
`ackbar.soca.letkf_config` over an experiment's own layer stack with an
observation set of exactly one row, so the operator it tests is the operator the
experiments run rather than a second description of it, and the increment it
writes shows the reach in metres and kilometres that the config states in levels
and Rossby multiples. The tool's header carries how to invoke it.

What it shows on `gom_25km`, at 25.12 N 90.00 W in 3506 m of water: SST tapers
over ten levels and is bitwise zero from level 11 down; ADT and Argo, which
carry no vertical entry, update the whole 3506 m column and both peak at level
25, which is 194 m and is the ensemble's own thermocline response rather than
anything the localization did.

**A genuinely single ADT observation assimilates nothing, and looks like a
localization result.** `ufo::ObsADT` subtracts the mean of `H(x) - obs` over the
whole observation space before forming a departure, which is the same domain
mean removal that makes an altimeter blind to a basin-wide sea level offset (see
`docs/osse.md`). With one row in the space, that mean *is* the departure, so the
departure is identically zero and the increment is exactly zero everywhere. The
run reports healthy throughout, and the flat increment reads as "no vertical
localization response" when it means "nothing was assimilated". The way around
it is to give the altimeter company: `tools/single-ob.py` adds 99 filler rows
whose own departure is zero, all of them beyond the horizontal cutoff, so the
local solve at the column under test still sees exactly one observation while
the operator has a population to take a mean over. Anyone testing altimetry one
observation at a time will meet this first.

## The covariance, and where an ensemble comes into it

`solver.covariance` was validated and unread until phase 8. It now decides what
`ackbar/soca.py` assembles, and there are three answers:

| Value | The B the analysis reads | v2's name |
|---|---|---|
| `static` | the SABER block the layer describes | `3dvar` |
| `ensemble` | the ensemble alone, localized | `3denvar` |
| `hybrid` | both, as weighted components | `3dhyb` |

The two that read an ensemble read the *previous cycle's member forecasts*,
which is what a background error is: the covariance of forecast error, sampled
by an ensemble of forecasts. Reading the members' analyses instead would sample
an error the assimilation has already removed, and the result would be a
covariance that shrinks every cycle with nothing to report it.

Three things an experiment states and one it never does:

- **`solver.ensemble error.localization`**, verbatim SABER, in
  `config/layers/da/hybrid.yaml`. It is a diffusion operator reading a scale
  field from the same offline stage as the correlation, and the scales are
  deliberately wider. Localizing tighter than the true correlation length throws
  away exactly the structure the ensemble is there to provide, and the symptom
  is an ensemble component that looks like it is not contributing.
- **`solver.hybrid weights`**, two numbers, neither defaulted. Half and half is
  the textbook hybrid and is therefore not any particular ocean's answer, and an
  experiment that did not state them is one whose result cannot be attributed to
  either component.
- **`ensemble.source`**, which is what maintains the members from one cycle to
  the next: ACKBAR's name for v2's `DA_PERTURBATION_MODEL`. `letkf` puts a
  filter in the cycle beside the deterministic analysis. `none` lets the members
  run free and only recentres them, which is cheaper and is a different
  experiment rather than a degraded one: that ensemble has flow dependence and
  no observation information of its own. The rest of the vocabulary (`eda`,
  `offline`, `perturbation`) is refused by `ackbar/graph/build.py` *when the
  covariance is drawn from the ensemble*, because a covariance drawn from an
  ensemble that nothing updates loses its spread over a few cycles and reports no
  error while it does. An ensemble filter maintains its own members, so the check
  does not apply to it and the value is unconstrained there.

What an experiment never states is the members themselves. They are paths, one
per member, under the previous cycle's `rst/`, and a layer naming them would be
a layer that has to know the on-disk layout and the cycle number.

## Recentring, which is what a hybrid does and an LETKF does not

`member - mean(ensemble) + centre`: each member keeps its own perturbation about
the ensemble mean and is given the deterministic analysis as its centre. It is
`soca_ensrecenter.x`, one job over the whole ensemble, because the mean it
subtracts belongs to every member at once.

Without it the members cycle around the ensemble filter's own mean while the run
being reported is the deterministic one, and the two drift apart with nothing to
say so. The recentred states are written beside the analyses they came from
rather than over them (`ocn.rcnt.an.<date>.nc` against `ocn.ana.an.<date>.nc`),
because the recentring is the step that decides how much of a hybrid's answer
the ensemble keeps and having both states is the only way to see what it did.

Only the analysis variables are recentred. The application does
`x = x_center; x += pert`, which replaces every field it is given, so naming a
field the analysis never solved for would hand every member the control's layer
thicknesses: a different vertical grid under the same water.

## Two applications, one observer list

A hybrid cycle reads the same observers through a variational solve and an
ensemble filter, and they need different things from them. Two consequences,
both of which soca-science met and patched around with `sed` markers keyed on
whether the LETKF was running solo or inside a `3dhyb`:

**The distribution is ACKBAR's.** It is a property of the application reading
the file rather than of the platform, so an observer layer says nothing about
it: the solve gets `RoundRobin`, the filter gets whatever
`solver.ensemble distribution` says, which is a `Halo` at least as wide as the
largest localization radius in use. What an experiment chooses is that size.

**The departures need two homes.** Both applications write an observation-space
file per observer, and the observer layer names one path. The control's are the
experiment's product and keep it; the filter's are a diagnostic of the ensemble
and go to `obs_out/<date>/ensemble/`, which is the same split v2 expressed as
`OBS_OUT_CTRL_DIR` and `OBS_OUT_ENS_DIR`.

The land mask threshold is not in this list. It differs by solver in both
sources and is still an ordinary substituted value, because it is a QC decision
about which observations exist and a hybrid should be giving its two halves the
same ones.

## A member that did not arrive

Once members are array elements, a missing forecast stops being rare: an
experiment with twenty of them has twenty chances per cycle. The answer is
stated per experiment as `ensemble.on_missing_member`, and the three values are
three different experiments rather than three degrees of tolerance:

| Policy | What the cycle does |
|---|---|
| `fail_cycle` | stop; the ensemble is the experiment |
| `run_degraded` | assimilate what arrived, and record what did not |
| `replace_from_mean` | rebuild the missing member from the others |

`run_degraded` needs care. A filter given eighteen members where it expected
twenty produces an analysis of lower rank, more sampling noise and less spread,
and the effect outlives the cycle that caused it. So every cycle writes
`run/<date>/ana/members.json`, whether or not anything was missing: two experiments
that differ in which members ran are not comparable, and nothing else would say
so.

**Exactly one job applies the policy.** In a hybrid that is `da.ens`, and the
variational analysis reads the record it wrote rather than resolving again,
which is why the two are ordered rather than run side by side. Two independent
applications of `replace_from_mean` would have each half of one hybrid rebuild
its own copy of a missing member, writing the same restart set at the same time.

`replace_from_mean` keeps the rank the same in name only, since the replacement
carries no independent information. What it buys is that the member exists, so
its forecast runs and the ensemble is back to full strength one cycle later
rather than carrying a hole forever. A mean state is a state a model integrates:
it is the same object the filter hands the control every cycle.

## Where an ensemble starts

`tools/ensemble-ic.sh <domain> <members>` draws each member from the
experiment's own static background error, using `soca_enspert.x`, and writes one
restart set per member beside the state it perturbed. An experiment names it
with `ensemble.initial_condition`; the control starts from
`model.initial_condition`, unperturbed.

That is a usable starting point and the wrong ensemble. The perturbations are
static: they carry B's correlation length scales and none of the flow structure
of the ocean at that instant, so the ensemble has spread but no dynamical
balance, and the first few cycles of any experiment started this way are
spin-up. The spread is also whatever B claims, which is confident enough to give
observations very little weight. It is what tier 3 uses, where the point is to
exercise the path rather than to believe the answer.

**A real experiment uses the lagged ensemble instead.**
`tools/ensemble-ic-lagged.sh` draws each member from a different GLORYS year at
the same time of year, so the spread is real ocean variability and every member
is a dynamically balanced state. Three steps, and their order matters:
`ensemble-ic-lagged.sh` builds the members, `experiments/osse25-ensemble-settle`
gives them one free day to shed the initialization shock, and
`tools/ensemble-recenter.py` moves the mean onto the control **last**, because
recentring before the settle leaves the mean a free day ahead of the control and
an ensemble filter cannot tell that offset from information.

The stage hit the same omission the analysis did, from the other side. A
covariance's `linear variable change` with no `output variables` produces an
increment carrying no fields, so every member came back exactly equal to the
state it was perturbed from and nothing said so. Both now go through
`soca.background_error`, which is the one place that decides a covariance's
variable lists are the analysis variables.

**The build order above is load-bearing, not just tidier.** With members
recentred before the settle, an unlimited LETKF increment (`config/layers/
da/common/limits.yaml` before it carried a bound) put 16 degC into a 1.3 mm
layer and crashed thirteen of eighteen members on the first cycle, reported by
MOM6 several steps downstream as `adjust_interface_motion: implied h<0`. Adding
the soft increment bound alone brought that down to two failures, not zero: a
bound acts per point and non-linearly, so it flattens peaks and changes the
*shape* of the increment field, which is what the model actually responds to.
What removed the last two failures was giving the lagged ensemble a free day
before recentring, so the members no longer carried the cold start's
interpolation shock into the first analysis. The bound in
`config/layers/da/common/limits.yaml` is still worth having as a guard against a
single wild point, but it is not a substitute for the settle step.

## Writeback

A direct write into a copy of the background, because `soca_checkpoint_model.x`
does not exist in the pinned SOCA. That was settled by spike before the phase;
see [`build-order.md`](build-order.md).

Copy first, then overwrite in place. That ordering is what makes a rerun safe:
every value's source is the background, which no task in the experiment ever
modifies, so a writeback killed halfway and run again produces the same restart
set rather than an analysis applied twice.

Four properties of a MOM6 restart shape the rest of it.

**Only the ocean cells.** The analysis carries a fill value on land. MOM6 mostly
does not care what is under the mask, and "mostly" is the problem: those values
are what a diagnostic averages and a checksum covers. The mask comes from the
domain's `soca_gridspec.nc`, which is also what the geometry and the diffusion
calibration read, so the analysis, its background error and its writeback cannot
disagree about where the coast is.

**`u` and `v` are staggered, and which face an index means is settled by the
gridspec.** The forecast's MOM6 is built with symmetric memory, so its restart
carries `u` one column wider and `v` one row taller than the tracer grid. The
extra face is at the *west* and *south*, because a symmetric grid sets
`IsdB = isd-1`, so restart column 0 of `u` is the west face of tracer 0. SOCA's
MOM6 is not symmetric and has no index for that face at all: `soca_gridgen.x`
writes a `lonu` and a `mask2du` describing the *east* faces, since MOM6's `u(I)`
always sits between `h(I)` and `h(I+1)`.

Those two do not line up, and SOCA cannot be asked to line them up: its reader
(`commit_reader_strided`) starts every variable at the tracer origin and reads a
tracer count of columns, with no branch on which grid a field is on. So SOCA
loads the west faces however the gridspec is labelled. ACKBAR therefore moves the
label to the data, in `ackbar.gridspec.shift_staggered`, applied once per domain
by `tools/soca-gridspec.sh`: every staggered field is shifted one index so that
index i is the face on the low side of tracer i, `mask2du(i)` becomes
`mask2dT(i-1) * mask2dT(i)`, which is the model's own `mask2dCu` at the face the
reader takes, and the gridspec records the convention as
`ackbar_staggered_faces = "west/south"`. `writeback` and `post` keep the leading
tracer-sized corner, which is what SOCA reads, so nothing else moves.

The alternative, converting every restart before SOCA reads it as
soca-science's `soca_dynsym2dyn.sh` did, is equally correct and costs a copy of
every restart the solver reads: about 3.9 GB per cycle at gom_25km with twenty
members and five slots, against a 16 GB experiment.

**A gridspec that will not say which faces it is on does not run.** That is the
failure this approach introduces and it is the one worth being loud about: rerun
`soca_gridgen.x` without the post-step and the defect returns with no symptom at
all, after the documentation says it is fixed. So `validate` step 3 calls
`ackbar.gridspec.assert_faces_recorded` on the domain's gridspec and refuses one
lacking the attribute, the same shape as `forcing.assert_reference_height` for
an archive that will not say what height its scalars are at. Presence is the
check, not a particular value: a file that records a face set records a
decision, and a file that records nothing predates the decision.

**The shift is not applicable on a global grid, which is not the same as being
unnecessary there.** `shift_staggered` works by dropping the outermost row and
column, sound only where those faces have no cell beyond them. A bounded domain
masks them to land; a global one reaches the cell beyond through the zonal
wraparound and the tripolar fold, so they are open ocean and the shift would
discard analysable faces. Such a domain records
`ackbar_staggered_faces = "east/north"` through `ackbar.gridspec.record_generated`
instead, which states truthfully where its staggered fields sit.

The mismatch itself is untouched by that. It follows from SOCA's MOM6 being
built without symmetric memory while the forecast's is built with it, and holds
globally exactly as it holds regionally. So the declaration is not an exemption,
and `validate` step 3 refuses the generated face set together with a model in
`FORECAST_MODELS`, which today is `mom6sis2` alone: `stub` and `persistence`
read no velocity off the grid. `om_1deg` is usable because its live work is the
tier 0 and 1 graph fixtures, which never integrate anything. `OM4_025` is global
too and is the production target, so the periodic and tripolar shift has to be
written before that domain runs a model; `src/ackbar/gridspec.py` says what it
would take, and the x axis wanting `np.roll` is the easy half of it.

Which of the two a domain gets is declared in its layer as
`domain.staggered_faces`, read by `tools/soca-gridspec.sh` when it builds the
file and checked against the file by `validate`. Absent means `west/south`, so a
domain whose layer has never considered the question fails rather than quietly
skipping the shift, and a declaration that disagrees with the file on disk is
itself a step 3 finding, which is what catches a domain layer edited after its
gridspec was built.

Two consequences of putting it there. **A domain's gridspec has to be shifted
before any experiment on it can be created**, so deploying this and merging it
are one action rather than two. And the check runs at `create`, so a gridspec
replaced *after* an experiment was created is not caught; the cycle that reads
it will run and be wrong. Rebuild a domain's static products between
experiments, not underneath one.

Nothing else consumes the staggered masks, which is why moving them is
self-contained. On ACKBAR's side the only readers of `mask2du` and `mask2dv` are
`writeback.MASKS` and `post.GRIDS`; `tools/sst-bgerr.py`, `tools/dirac.py`,
`tools/dirac-page.py`, `tools/obs-archive-osse.py` and `ackbar.diffusion` read
the tracer `mask2d` only. Inside SOCA they reach the `field%mask` pointer for a `u` or `v` field,
which decides the fill value on read, and the atlas `mask_u`/`mask_v` fields,
which the geometry's own comment marks as carried for `gpnorm` alone. The SABER
diffusion calibration does not touch them: its background variable is
`sea_surface_height_above_geoid`, its products are on `nb_nodes`, which is the
tracer node set, and its configuration names no mask. So the correlation and
localization files do not need rebuilding when a gridspec is shifted, and a
`u` or `v` `gpnorm` in a log is the only number that moves.

The evidence, so nobody has to re-derive it, measured on gom_25km:
`max|lonu - lonq[1:88]|` is 0.0 exactly against 0.25 for `lonq[0:87]`;
`mask2du == mask2d[i] * mask2d[i+1]` with 0 mismatches of 4816; and pairing SOCA
index i with restart column i left 128 u and 110 v faces that the gridspec called
ocean and the model has no velocity on, which the shift takes to zero. Re-check
it on any domain with `tools/uv-stagger-figures.py`, which counts and draws it,
or at tier 0 with `tests/test_gridspec_stagger.py`.

**A velocity record written before this change is on the other convention.**
`post` labels archived `u` with the gridspec's `lonu`, so every `bkg/` and `ana/`
velocity in an experiment run before the shift is one cell from where its
coordinate says. Truth archives carry the identical offset, so an experiment's
own velocity diagnostics are self-consistent and its scores against truth stand;
what does not work is reading a velocity record from before the shift alongside
one from after. Temperature, salinity, thickness and sea surface height are
unaffected in every case. Which convention a record is on is answerable from its
domain's gridspec, which carries the attribute, rather than from its date.

**The `checksum` attribute is a claim about the data.** MOM6 reads it back and
aborts on a mismatch, which is right and is what a modified restart triggers.
The attribute is dropped from the variables writeback overwrites and from no
others, so every field the analysis did not touch keeps its integrity check.
This is why `RESTART_CHECKSUMS_REQUIRED` is *not* set in `MOM_override`:
switching the check off for the whole file to accommodate three variables would
discard it on the twenty it still applies to.

**`coupler.res` is what says the set is whole**, so it is written last, the same
rule `mom6sis2.commit` follows.

Which restart variable a JEDI variable is, and which grid it is on, comes from
`config/model/mom6sis2/fields_metadata.yaml`, the same file SOCA reads. A second
copy of that mapping inside writeback would be one that keeps working after
someone corrects the first.

### Holding the increment back, and why nothing does

`writeback` reads two optional settings, `increment relaxation` and `increment
divergence limit`. No experiment sets either, and that is a result rather than an
oversight.

Both existed because members died in `btstep: eta has dropped below bathyT`, in a
thin column, whenever the LETKF analysis was applied at full strength. First a
ramp from 0.25 to 1.0 over five cycles, then a divergence limit that replaced it
and did keep every member alive. Neither was the cause. The columns were draining
because `gom_25km`'s imported bathymetry put a 3.5 m cell against a 4083 m one,
and capping the steepness at `r <= 0.9` with `tools/smooth-topography.py` removed
the failure at its source: five analysed cycles at full strength with the bound
disabled, twenty one of twenty one forecasts alive every cycle.

Leaving the bound switched on anyway would not have been free. Measured on a real
analysis it cut the velocity increment at **16% of wet cells, by a median factor
of 0.48**, and more than half of those cells were deeper than 200 m rather than
the thin ones it was justified by. That is **17% of the depth-weighted velocity
increment discarded every cycle**, and the count per cycle plateaued rather than
decaying, so it was a standing constraint on the analysis rather than a spin-up
guard.

Both settings stay implemented. A domain whose sea floor cannot be smoothed is
what they are for, and `divergence_scaling` is tested. Do not switch either on
without measuring what it removes. [`domains.md`](domains.md) carries the
bathymetry side, including why a cap chosen on `gom_25km` should not be carried
to `gom_12km`.

IAU would be better than either, and is not wired. It spreads the increment
across the forecast instead of applying all of it between two timesteps, so it
removes the violence rather than the correction; MOM6 already implements it in
`MOM_oda_incupd.F90` (`ODA_INCUPD_NHOURS`, `ODA_INCUPD_UV`).

## A window with no observations

The analysis in a window with nothing in it is the background. Over any real
archive some window is empty, so this is a state of a correct experiment rather
than a failure: `da` writes nothing and says so, `writeback` hands the
background across unchanged and says so, and the realized observer list records
which observers were dropped and why. Running the minimizer against an empty
observer set to arrive at the same answer would be the same result at the price
of a whole cycle's risk.

The one thing this must not do is look like a cycle that failed quietly, which
is why both halves print a line naming the reason.

## Bringing it up

`model: persistence` runs the whole loop with the model taken out of it. Inherit
it after `model/mom6sis2`, since it is that configuration not integrated:

```yaml
inherit:
  - domain/gom_25km
  - model/mom6sis2
  - model/persistence
  - da/variational
```

The DA loop and the model fail in the same place, and separating them is the
difference between "the analysis is wrong" and "something in this cycle is
wrong". Persistence is also the baseline every analysis is measured against.

`tests/test_tier3_var.py` runs both, at `gom_25km`. Two of its assertions carry
most of the weight, and they are different in kind.

`oman` smaller than `ombg` in RMS says the machinery is *coherent*: the
background error, both observation operators, the balance operator, the
minimizer and the departure bookkeeping all have to agree for it to hold, and
each of them can be individually plausible while it does not.

The increment correlating with a known anomaly says the analysis found the right
*answer*. The observations are synthetic and sample a truth that differs from
the background by an anomaly `tools/obs-archive-osse.py` wrote down, so the
increment is compared against the thing it was supposed to find rather than
against the departures it was fitted to. A correlation and not a magnitude: the
magnitude is a statement about the standard deviations, which are the bundle's
defaults and are not claimed to be right.

Observations of a regional domain have to be built for it. The bundle's own
ioda files are scattered over the world ocean, and a regional analysis reading
them assimilates nothing while running perfectly. Nothing about that failure is
loud, which is why the archive is generated from the domain's own gridspec.

`tests/test_tier3_hybrid.py` runs the two-analysis cycle at the same domain,
dates, ensemble and archive, so that the three tier 3 experiments differ in one
inherited layer each. What it checks is not that a hybrid is better, which it
has no ensemble to demonstrate: it checks that the covariance the application
read has both components in it, that the two analyses did not overwrite each
other's files, and that the recentring moved every member by the same field
while leaving the spread alone. Each of those failures leaves an experiment that
finishes normally.

## What is not here yet

- **Temperature and salinity clamping.** v2 clamped both inside its checkpoint.
  Nothing here does, and no clamp has been needed so far; the guard writeback
  does have is a refusal on a non-finite analysis, which is what a diverged
  minimization produces.
- **An ensemble whose spread means something.** See "Where an ensemble starts".
- **Output compression.** `post.state`'s job, and lossy (`ncks -7 -L 4 --ppc`),
  so it must never run in place and the source has to survive until the
  destination is committed. Owed before production, not before a result.
- **The standard deviations.** See [`background-error.md`](background-error.md).
  Mostly the bundle's defaults, which is a decision waiting to be made rather
  than one that has been. Sea surface temperature is the exception: it reads a
  field derived for the domain by `tools/sst-bgerr.py`.

## What an ensemble here cannot yet show

Two things about the ensemble are worth knowing before any result from an LETKF
experiment is compared with a variational one.

**The spread comes from lagged GLORYS years**, drawn by
`tools/ensemble-ic-lagged.sh` and recentred onto the control. It is real ocean
variability rather than a covariance model's idea of it, which is the better of
the two available answers, but it is climatological spread and not the flow
structure of the ocean at that instant. `tools/ensemble-ic.sh` builds the other
kind, perturbing one state through the static B, and is what tier 3 uses.

**An experiment that inherits no forcing layer has every member forced by the
same atmosphere**, and each cycle they are pushed towards a common solution by
the surface fluxes they share. `ensemble.inputs` gives a member its own `atm.nc`
the same way it gives it its own `obc.nc`: [`forcing.md`](forcing.md) says how
the archive is built and Domains in [`design.md`](design.md) ranks what it is
worth. The archives are keyed by purpose rather than by domain, so the nature
run reads ERA5 and the experiments a GEFS forecast of it, which is the pairing
that makes the difference between the two a skill number rather than a spread
one.

The members no longer differ *only* in their ocean state, which is the part of
this caveat that has moved. `ensemble.stochastic` gives each its own model
perturbation, and a boundary ensemble gives each its own edge. Neither replaces
the atmosphere: measured on `gom_25km`, an ensemble atmosphere produced four
times the day 5 surface temperature spread of stochastic physics, and the
boundary needs three weeks before it is doing much at all.

Neither is a reason not to run the filter, and both are reasons not to read a
score off it yet.
