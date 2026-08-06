# The analysis, and what happens to it

Up to four tasks, and the last one carries the risk.

| Task | Runs | Present when | Produces |
|---|---|---|---|
| `da` | `soca_var.x` or `soca_letkf.x` | there is a solver | the control's analysis, the increment, `ombg` and `oman` |
| `da.ens` | `soca_letkf.x` | `ensemble.source: letkf` beside a variational solver | every member's analysis, and the ensemble's own departures |
| `recenter` | `soca_ensrecenter.x` | the covariance reads an ensemble | every member's analysis, moved onto the control's |
| `writeback` | python | there is a solver | the restart set the next forecast starts from |

`da`, `da.ens` and `recenter` are each one MPI job for the whole cycle;
`writeback` is a member array. The handoff between them is
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

The same construction, from `config/soca/letkf.yaml` and the same builder, with
one structural difference: the background is an ensemble. oops takes that either
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

**The driver.** `do posterior observer` computes `oman`; without it the cycle
produces departures against the background only. `save posterior mean` is what
gives the control member an analysis at all. `save posterior ensemble` is the
analysis. `LocalEnsembleDA` throws by name when a flag is set and its output
block is missing, which makes this the one part of the document that fails
loudly.

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
  `offline`, `perturbation`) is refused by `ackbar/graph/build.py`, because a
  covariance drawn from an ensemble that nothing updates loses its spread over a
  few cycles and reports no error while it does.

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

That is the right starting point and the wrong ensemble. The perturbations are
static: they carry B's correlation length scales and none of the flow structure
of the ocean at that instant, so the ensemble has spread but no dynamical
balance, and the first few cycles of any experiment started this way are
spin-up. The spread is also whatever B claims, which at `gom_25km` is about
0.09 K in surface temperature: an ensemble that confident gives observations
very little weight. A better ensemble is a set of states from a long run,
sampled far enough apart to be independent, which is what an OSSE nature run
provides.

The stage hit the same omission the analysis did, from the other side. A
covariance's `linear variable change` with no `output variables` produces an
increment carrying no fields, so every member came back exactly equal to the
state it was perturbed from and nothing said so. Both now go through
`soca.background_error`, which is the one place that decides a covariance's
variable lists are the analysis variables.

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

**`u` and `v` are staggered.** The forecast's MOM6 is built with symmetric
memory, so its restart carries `u` one column wider and `v` one row taller than
the tracer grid; SOCA's is not symmetric and hands back the tracer-sized array.
Writing that straight in shifts every velocity by one cell, which surfaces as a
model that grows a strange coastal jet a week later. Not exercised by the
default `analysis variables`, which is exactly why it is handled and tested.

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
  They are the bundle's defaults, which is a decision waiting to be made rather
  than one that has been.

## What an ensemble here cannot yet show

Two things about the ensemble are worth knowing before any result from an LETKF
experiment is compared with a variational one.

**The spread is drawn from the static B**, so it is whatever that covariance
claims and carries none of the flow structure of the ocean at that instant.

**Every member is forced by the same atmosphere.** The members differ only in
their ocean state, and each cycle they are pushed towards a common solution by
the surface fluxes they share. An ensemble that means something needs
perturbations in the atmosphere too, which is a data problem rather than a
workflow one: it arrives with the forcing archive that the nature run needs.

Neither is a reason not to run the filter, and both are reasons not to read a
score off it yet.
