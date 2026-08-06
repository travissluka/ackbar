# The analysis, and what happens to it

Two tasks, and the second is the one that carries the risk.

| Task | Runs | Produces |
|---|---|---|
| `da` | `soca_var.x` or `soca_letkf.x` | the analysis state, the increment, `ombg` and `oman` |
| `writeback` | python | the restart set the next forecast starts from |

`da` is one MPI job for the whole cycle; `writeback` is a member array. The
handoff between them is `ana/<n>/mem###/analysis/`, and the handoff out of
`writeback` is `ana/<n>/mem###/` itself, which the forecast reads exactly as it
reads a restart set the model wrote.

Which solver runs is the only thing that differs, and it differs in one place:
`da`'s body. `writeback` is solver-independent, because it reads a state and a
background and writes a restart set, and none of that depends on how the state
was arrived at.

## Where things go

```
ana/<n>/mem000/                       the analysed restart set: MOM.res.nc,
                                      ice_model.res.nc, coupler.res
ana/<n>/mem000/analysis/              what the application itself wrote
    ocn.ana.an.<date>.nc                the analysis state
    ocn.incr.incr.<date>.nc             analysis minus background
obs_out/<n>/<experiment>.<obs>.nc4    the departures
```

The subdirectory is not tidiness. `ana/<n>/mem###` is a *restart set*: writeback
fills it by copying every file of the background's, `model: persistence` fills
the next cycle's by copying every file of this one, and the forecast links all
of them into `INPUT/`. A state file loose among them is inert to the model and
is then carried forward by every cycle after it, one more each time.

## The document `soca_var.x` reads

Built by `ackbar/soca.py`, not templated. The only parts an experiment states
are the ones nothing else implies, and they live in
`config/layers/da/variational.yaml`: `background error` and `variational`.
Everything else comes from somewhere the experiment already says it.

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

The same construction, with one structural difference: the background is an
ensemble. oops takes that either as `members from template`, with a `%mem%`
pattern and a zero padding, or as `members`, an explicit list. ACKBAR builds the
list, and the reason is worth knowing before anyone "simplifies" it back.

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

**The control's analysis is the ensemble mean**, exactly. ACKBAR does not
compute one for `mem000`: `LocalEnsembleDA` writes the posterior mean with
`member` set to 0, and that lands on the control's directory. This is also why
`recenter` has no body. Recentring an ensemble onto a centre it already has is
the identity, and the centre of an LETKF's analysis ensemble is its own mean;
what brings a real body is a centre that is not, which is a hybrid.

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
`ana/<n>/members.json`, whether or not anything was missing: two experiments
that differ in which members ran are not comparable, and nothing else would say
so.

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
  - domain/om_1deg
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

## What is not here yet

- **Recentring**, and with it the hybrid. See above: there is no centre other
  than the ensemble's own mean until a deterministic analysis runs alongside
  the filter.
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
