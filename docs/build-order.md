# Build and test order

Rough outline of what gets built in what order, and what each step is tested against.

Two tracks are interleaved. **Workflow machinery** (config, graph, submission, healing) comes
first and is finished before any real science runs through it, because it is testable in
minutes on a laptop and on rancor's single node. **Science capability** (free run, hofx, the
solvers) comes after, and each step then adds tasks and config layers rather than new
machinery.

Ordering is chosen by what each step de-risks, not by which mode it is.

## Test tiers

Every phase below states which tier it is verified at. The tiers exist so that "did I break
it" has a cheap answer most of the time.

| Tier | What runs | Needs | Runtime | Cadence |
|---|---|---|---|---|
| 0 | unit: merge, resolution, schema | python only | seconds | every commit |
| 1 | `validate` and graph goldens on fixture experiments | python only | seconds | every commit |
| 2 | stub workflow end to end, including the fault matrix | Slurm; no JEDI, no model | minutes | every commit |
| 3 | real cycling, `gom_25km`, a few cycles | full build | minutes | per phase |
| 4 | production scale, `OM4_025` | HPC | days | per release |

Tiers 0 through 2 are the regression suite. Tier 2 is the important one: it is the only cheap
test that exercises arrays, dependency edges, and failure recovery, which is where the risk
actually is.

## Phase 0. Configuration core

No scheduler, no JEDI, no model. Runs anywhere.

- **Build:** layer loading and deep merge, list merge by declared key with a removal marker,
  the experiment-time resolution pass, ackbar's schema, and `ackbar config why`.
- **Test:** tier 0, table driven. Every merge rule gets a case, especially `observations`
  keyed on `obs space.name`, since that is the one the DA layers depend on.
- **Done when:** a fixture experiment resolves to a frozen config, and a bad key, a wrong type,
  and an unkeyed list element each fail with a message that names the layer and the path.

Settle merge semantics here. The ~25 ported observer configs bake them in, and changing them
afterwards means revisiting all of those.

## Phase 1. Graph generation and validation

Still no scheduler.

- **Build:** the task graph as data (nodes, typed edges), task enable and disable driven by the
  merged config, the closed set of job-time symbols, `ackbar validate` steps 1 through 6, and
  `ackbar graph`.
- **Test:** tier 1. Golden graphs per distinct shape: free run, 3DVar, 4D, LETKF, and hybrid
  with a long forecast cadence. An OSSE truth run is a free run with observations configured,
  and 4D differs from 3D in configuration but not yet in the graph, so both are pinned rather
  than given shapes of their own. Determinism is a test: generate twice, compare. Properties:
  the graph is acyclic, every member level array carries the canonical index set, no leaf has a
  successor, and `aftercorr` never joins arrays with different index sets.
- **Done when:** a 50 cycle, 20 member hybrid graph generates in seconds and matches its golden,
  and `validate` rejects a missing input path and a missing executable before anything is
  submitted.

Steps 2 and 3 deserve more care here than their position in the list suggests. JEDI has no
usable parse-and-exit, so nothing between `validate` and the running executable will catch a
bad path, and the checks ackbar performs itself are the only ones there are. Generating every
job's YAML for every cycle and stat-ing every file it references is cheap, and it is the whole
of the early warning system.

## Phase 2. Emitter, ledger, submitter, and the stub model

The first phase that touches Slurm. This is the milestone the project's premise rests on.

- **Build:** the stub model, job script emission, `sbatch` with `--comment` identity, the
  append-only ledger, member arrays wired with `aftercorr`, the cycle throttle and the
  submitter job, and two local Slurm profiles (one with
  `DependencyParameters=kill_invalid_depend`, one with enforced limits and a small
  `MaxSubmitJobs`), switched by `tools/slurm/profile.sh`.
- **Test:** tier 2, in `tests/test_tier2.py`. A 20 member, 3 cycle experiment at 1 PE and a few
  seconds a member. Then the fault matrix, injected deterministically through
  `model.stub.fail`: nonzero exit, run past the time limit, blow the memory request, exit 0
  having written nothing, and requeue mid-task. Two of the six in the original list are not
  fault injectors and never were: an impossible memory request is a `resources` value that
  `sbatch` rejects, and a member that never starts is what an `aftercorr` child does when its
  own element failed. Both are tested as what they are.
- **Done when:** the clean run finishes in a couple of minutes, and every fault reaches its
  intended terminal state on **both** Slurm profiles with nothing left pending and undiagnosed.

Three things the fault matrix taught, all of them properties of Slurm rather than of ACKBAR,
and all three now in `docs/slurm.md`. Only the *direct* dependent of a failed job reads
`DependencyNeverSatisfied`; anything further down reads plain `Dependency`, indistinguishable
from waiting on a job that will still run, so "stuck" is a property of the whole queue and not
of one row. A job that exceeds `--mem` is killed only if the site constrains swap as well as
RAM. And a requeue costs about two minutes of deferral and, asked for by job id from inside an
array element, takes every sibling with it.

The suite is built around the cost of those. Every experiment is created and started up front
and waited on together, so their scheduling latencies overlap instead of summing, and no
experiment carries two slow faults in series: the one-minute timeout and the two-minute requeue
each get their own, so their waits run alongside each other rather than end to end. The requeue
case alone is most of the wall clock and `ACKBAR_TIER2_FAST=1` drops it, which is for iterating
and not for what runs before a commit: requeue is the one fault the scheduler inflicts on its
own, without being asked.

This is phase 2 rather than a testing afterthought because on 8 physical cores an
`--array=1-20` of 8-PE forecasts runs strictly serially. Without the stub, the property the
whole project exists for cannot be demonstrated locally, and none of the failure paths that
carry the real risk can be exercised until an HPC allocation is on the line.

## Phase 3. Status, healing, cleanup, and accounting

- **Build:** `sacct --json` parsing joined to the ledger, `squeue` reason reading, `scancel` of
  the stranded closure before resubmission, artifact-existence cleanup, and the per cycle
  resource harvest into `stats/<cycle>.json`.
- **Test:** tier 2, reusing the phase 2 fault matrix. Additionally: heal after each fault, heal
  twice, heal while the next cycle is already running, and cleanup racing a heal.
- **Done when:** every fault in the matrix is recoverable with `ackbar heal` and no manual
  `squeue` or `scancel`, and every cycle leaves a stats file.

Four things this phase settled, none of them visible from the design and each of them a bug
that only shows up hours in.

**The closure has to stop at what was actually submitted.** A failure strands the `submit`
task, so the next cycle does not exist: those nodes have no job ids, nothing to cancel and no
edges to rebuild. Including them makes the heal try to submit a cycle whose own roots have
never been submitted, which is exactly the case the submitter refuses on, and the heal dies
half-applied.

**`stats` and `cleanup` must not skip on their sentinels.** Every other task produces an
artifact and "already done" is the truth about it. These two report on or maintain a cycle as a
whole, and a heal changes what that cycle consists of. A skipped `stats` leaves a file
describing the run that was thrown away; a skipped `cleanup` is worse, because the one run that
refused to delete (correctly, the cycle it was keeping was incomplete) is then the only run
there will ever be, and the restarts leak for the life of the experiment.

**The cleanup-versus-heal race cannot happen, and the reason is worth writing down.** Cycle *n*
deletes cycle *n-2* only once *n-1* is complete for every member, and `submit` is gated
`afterok` on the forecast, so cycle *n* only exists at all once *n-1* finished. Anything a heal
could resubmit that still reads *n-2* is upstream of that forecast, and a heal only ever
resubmits downstream of a failure. The artifact gate is the safety net rather than the
mechanism.

**Healing is not fixing.** A deterministic fault resubmitted unchanged fails identically, which
is the common case in practice. `heal` says which failures look like that and resubmits anyway,
because refusing would mean the tool deciding it understands the science better than the person
running it.

At the end of this phase the workflow machinery is done. Everything after adds tasks and
layers.

## Phase 4. Free run cycling

`solver: none`, `model: mom6sis2`. The spine: cycle, restart, resume, clean up. Also produces
the OSSE truth run.

- **Offline prerequisites:** `static/<domain>` and one spun-up initial condition, materialized
  into the experiment's own cycle-0 forecast output location so that cycle 1 is not special.
- **Build:** the forecast task, model config layering, `diag_table` selection by forecast
  purpose, restart handoff.
- **Test:** tier 3, a few cycles at `gom_25km`. Tiers 0 through 2 keep passing.
- **Done when:** N cycles are restart continuous, and a killed cycle resumes and reproduces.

Also here, and the prerequisite for the rest of it: **`srun` launching MOM6 on rancor**, so that
job steps, per-task binding and per-step accounting are the same thing here as in production
rather than two regimes to reason about. `srun --mpi=pmi2` against spack-stack's MPICH needed no
rebuild and no PMIx; what it needed was knowing that `--mpi=none` silently gives every rank its
own `MPI_COMM_WORLD` instead of failing. See "srun and PMI" in `docs/slurm.md`, which also
carries the measured `MaxRSS` difference between the two launchers.

Five things this phase settled, and the fifth was found much later, in phase 7, having
invalidated everything in between.

**A stock case tells MOM6 that every run is a new run.** `input_filename` in
`MOM_input_nml` is read one character at a time by `MOM_restart::determine_is_new_run`:
`'n'` means new, `'r'` means read the automatically named restart files. MOM6-examples ships
`'n'`, which is right for a case distributed as an example. With it, MOM6 initializes
temperature and salinity from `INIT_LAYERS_FROM_Z_FILE` and never opens `INPUT/MOM.res.nc` at
all.

Nothing about that is visible from outside. The model runs, integrates for the configured
length, and writes a complete restart set; `coupler.res` carries the right clock, because the
coupler reads it and MOM6's own initialization does not; and consecutive restart sets *differ*,
because each cold start is forced by a different day of the atmosphere. So "every cycle hands
its restart set to the next" passed, and so did "the restart sets are not all the same state",
which was written specifically to catch a workflow that copies its initial condition forward.
Both are true statements about a run that discards its own history every twelve hours.

It surfaced in phase 7 and only because of the ensemble: six members with six different
backgrounds produced six bit-identical forecasts. One member cannot show that. The check now in
place is the one that would have caught it on day one and costs nothing, since a cold start has
`VELOCITY_CONFIG = zero`: **the kinetic energy MOM6 reports for its own step zero is exactly
zero on a cold start and is not on a resumed run.**

**`INPUT/coupler.res` is a hardcoded string inside `coupler_main`.** `restart_input_dir` in
`MOM_input_nml` and `SIS_input_nml` moves MOM's and SIS's own restarts, and the coupler does not
follow it: it reads the date it resumes from out of that literal path and writes
`RESTART/coupler.res`. A run directory that symlinks `INPUT` at the shared static archive has
nowhere to put the one file that says what time it is, and pointing `restart_input_dir` at the
previous cycle does not move the coupler with it. The model then integrates the right state from
the wrong time and says nothing. So the run directory owns its own `INPUT`, built fresh every
attempt out of symlinks: the static archive first, this cycle's restart set over the top. That
rebuild is load-bearing rather than tidiness, since a stale `coupler.res` left by a failed
attempt is precisely the wrong-time case.

**The date lives in the restart, not in the configuration.** Once `INPUT/coupler.res` exists it
overrides `coupler_nml`'s `current_date`, and what the namelist still controls is the *length* of
the integration. Since setup materializes an initial condition into `rst/0`, every cycle
including the first resumes, and `current_date` is a fallback a correct experiment never reaches.
It gets written to match the cycle anyway, because the one case that reads it is a misconfigured
cold start, and starting at the right date beats starting in 1958.

**"Which file proves a restart set is whole" is model-specific, and getting it wrong is
silent.** `cleanup` refuses when the cycle it is keeping looks incomplete, and a refusal is a log
line, not a failure. Keyed off the stub's file name under a real model it refuses on every cycle
of every experiment, and the only symptom is a disk filling up over days.

**A leaf with no implementation yet should run and do nothing; a task in the data path should
not.** `post.state` and `verify` produce diagnostics nothing else reads, so a real-model
experiment that skips them loses a diagnostic and can be told it did, from the sentinel.
`writeback` doing nothing quietly means every later cycle forecasts from an unanalysed state
while the experiment looks healthy throughout. The list is `DEFERRED` in `run.py` and shrinks as
the science phases land. They stay in the graph rather than being cut from it, so that the phase
which adds a body is not also the phase that first discovers its edges were wrong.

## Phase 5. hofx

Exercises the whole observation pipeline with no analysis entangled in it, and doubles as the
OSSE observation generator.

- **Build:** observation staging, realized observer list written per cycle, the hofx task, and
  the ported observer configs revalidated against current SOCA.
- **Test:** tier 3 over the phase 4 free run.
- **Done when:** hofx produces `ombg` for every configured platform, and a missing observation
  file drops a non-required observer, is recorded in the per cycle observer list, and does not
  fail the cycle.
- **Then:** promote a free run to an OSSE truth run and generate synthetic observations.

The revalidation of the ported observer configs is mostly done, and what it turned up is where to
look rather than a list of fixes. Every operator, filter, distribution and parameter key the two
layers use still resolves against the pinned bundle, checked by grepping `pkg/jedi` for the maker
strings, which is also how to recheck after a bundle bump. Three things are worth knowing before
reading those configs:

- **The ADT operator lives in UFO, not SOCA**, at `ufo/src/ufo/operators/marine/adt/`. Anyone
  carrying soca-science intuition looks in `soca/src` first and concludes it was dropped. It also
  asserts its variable is exactly `absoluteDynamicTopography` and fails at construction otherwise.
- **`GeoVaLs/sea_surface_temperature` exists only because the fields metadata says so.** It is the
  `name surface` of `sea_water_potential_temperature`, so an observer's land or temperature check
  depends on the *model* layer's `fields metadata`, which is a coupling between two layers that
  neither one mentions.
- **`observation alias file` is valid on both operators and filters**, which is why the same key
  appears in different places in ported configs without either being wrong.

`obsop_name_map.yml` and the model layer's `fields metadata` are ACKBAR's own copies, under
`config/obs/` and `config/model/mom6sis2/`, and they are named **absolutely** rather than by
the relative name the JEDI examples use. That was the whole fix, and nothing needed staging. A
relative path resolves against the run directory, so the only thing that can notice its absence
is the job that fails on it; an absolute one is stated by a layer, stat'd by `validate` step 3,
and rejected before submission. The rule generalizes: a JEDI config key naming a file ACKBAR
ships should carry the absolute path, since every application here opens either.

The other thing this phase turned up is that **SOCA's geometry is an offline product, and
getting one out of a stock MOM6-examples case is not obvious**. Three requirements, none of
which announce themselves:

- The namelist handed to `mom6_input_nml` must not be called `input.nml`. SOCA copies it to
  that name in the working directory and asserts the source was something else.
- `parameter_filename` inside it is relative, so `MOM_input` and `MOM_override` have to be in
  the run directory. They are linked from the model's own case, which is what makes the grid
  SOCA analyses on and the grid the model integrates the same grid.
- A `diag_table` must exist even though nothing here writes a diagnostic. FMS reads one during
  `initialize_MOM`, and its absence surfaces as a segfault inside the geometry constructor
  rather than as a message about a missing file.

`tools/soca-gridspec.sh` encodes all three for the offline stage and `ackbar/soca.py` for the
run directory. Phases 6 and 7 need the same geometry and should build on that rather than on a
second recipe.

**Regional domains arrived out of order**, between phases 5 and 6, because the global 1 degree
domain was too slow to iterate on: a simulated day costs 178 seconds there against 6 at
`gom_25km`. That work added the Gulf of Mexico domains, split a MOM6 case into the text half
that belongs in git and the data half that does not, gave ACKBAR its own `MOM_override`, and
answered two of the spikes below. It did not add the regional stages the design calls for:
domain-scoped observation culling is still owed, and it is owed before a regional *analysis*
rather than before a regional free run. Grid-edge masking is no longer on that list as work:
it is an open question to settle by looking at the first regional 3DVar increment, since v2's
workaround may be describing a SOCA that no longer exists. See Domains in `design.md`.
`docs/domains.md` is the entry point.

## Phase 6. Variational, static B, 3D

Static B and analysis-to-restart writeback.

- **Prerequisite spike:** IAU versus direct restart write, run *before* this phase, not inside
  it. Closed: `soca_checkpoint_model.x` does not exist in the pinned SOCA, so writeback is a
  python direct write.
- **Build:** the background error's correlation as an offline stage, the DA task, the writeback
  task, and `model: persistence`. The per cycle vertical B calibration is a measured
  improvement on the offline one rather than a prerequisite for it, so it is not in this phase;
  `b.vt` is in the graph and deferred, and what closes it is the vertical `filepath` naming a
  per-cycle file instead of a static one.
- **Test:** tier 3, `tests/test_tier3_var.py`, two experiments at `gom_25km`. Tiers 0 through 2
  keep passing, plus `test_soca.py`, `test_writeback.py` and `test_persistence.py`.
- **Order within the phase:** bring it up on `model: persistence` first. That gives the full DA
  loop, including writeback and background handoff, at no model cost, and a baseline to score
  against once MOM6 is back in the loop. Then switch to `model: mom6sis2`.
- **Done when:** persistence 3DVar cycles and scores against the free run, and the same
  experiment with MOM6 in the loop does too.

The entry points are [`background-error.md`](background-error.md) for the B and
[`analysis.md`](analysis.md) for the two tasks. Four things this phase settled, all of them
about a JEDI configuration that is wrong by *omission*, which is the expensive kind: JEDI has
no parse-and-exit, so each of these was found by an application that had already read a
background and built every block.

**A covariance's `linear variable change` needs `input variables`.** Without it,
`oops::ModelSpaceCovarianceBase` holds a null pointer and dereferences it the first time it
evaluates Jb. Both sources set it, and both set it to the analysis variables, which is why
ACKBAR builds it rather than asking a layer to state them a third time.

**`output` is what makes the departures complete.** `CostJo` saves `oman` only on the final
cost evaluation, and `oops::Variational` runs one only when something asks for output. An
analysis with no output section writes `ombg`, no `oman`, and no message about either. This is
the case the schema comment about departure diagnostics was written for, before it was known
which key carried them.

**A dropped checksum is better than a disabled one.** MOM6 aborts when a restart's `checksum`
attribute no longer describes the data, which is exactly what editing one in place produces.
`RESTART_CHECKSUMS_REQUIRED = False` was the expected fix and is the wrong one: writeback drops
the attribute from the three variables it writes, and every other variable in the file keeps
its integrity check.

**A regional analysis still needs domain-scoped observation culling**, and the reason is not the
one the design predicted. Global observation files do not break a regional domain: SOCA runs,
every out-of-domain observation fails QC, and the cycle completes. What it produces is an
analysis with nothing in it, so the culling is owed for the sake of the *observation counts*,
not for stability. What tier 3 does instead is read an archive that was generated in-domain
(`obs/gom-osse-smoke`), which sidesteps the question rather than answering it: the first
experiment to point a regional domain at a global archive still gets an empty analysis and no
complaint.

## Phase 7. LETKF

Parallel members and ensemble initial conditions. Member arrays now carry a real model, so
this is where the phase 2 array work meets real cost.

- **Build:** the LETKF task, the divergence policy, and `tools/ensemble-ic.sh`.
- **Test:** tier 3, `tests/test_tier3_letkf.py`, at gom_25km: seven forecasts a cycle as one
  array, one analysis consuming all of them, seven writebacks. Plus `test_ensemble.py` at
  tier 0, where the policies live.
- **Done when:** LETKF cycles with MOM6 in the loop, every member's analysis is its own, and a
  member lost mid-run is handled by the policy the experiment stated rather than by the run
  stopping.

Four things this phase settled.

**Recentring is not part of a pure LETKF, and this is not a deferral.** Recentring an ensemble
onto a centre it already has is the identity, and the centre of an LETKF's analysis ensemble is
its own mean: `LocalEnsembleDA` computes that mean and ACKBAR hands it to the control member.
So `recenter` was in `DEFERRED` with a body that did nothing *because nothing was the right
answer*. What brings a real body is a centre that is not the ensemble's own mean, which is a
hybrid; phase 8 added it, along with the `da` split, and moved the task off the LETKF entirely
rather than leaving it there to compute an identity.

**The index a member is written out as is its position, not its number.**
`oops::DataSetBase::write` numbers by place in the list it was handed, so an ensemble with a
gap in it is renumbered on output. That is why the background is built as an explicit
`members` list rather than a `members from template`: with a template the input and output
numbering disagree exactly when a member is missing, and the consequence is one member's
analysis written into another member's restart set with nothing to notice.

**A distribution's parameters go directly under `distribution`.** soca-science nested them
under `options`, which was right for an older ioda; the Halo distribution this LETKF needs
finds no `halo size` there and refuses to construct. The observer layers now substitute the
whole mapping, so a distribution's parameters stay next to the distribution that needs them.

**An ensemble perturbed from a static B is spin-up, not an ensemble**, and an ensemble on one
atmosphere is not one either. `tools/ensemble-ic.sh` draws each member from the same covariance
the variational analysis uses, which is the right place to start and gives spread with no
dynamical balance and no flow dependence; at gom_25km that spread is about 0.09 K in surface
temperature, so the filter is heavily overconfident and moves the state very little. The larger
problem is that every member is then forced by the *same* atmosphere, so what spread there is
gets handed back to the forcing they share, cycle after cycle. An LETKF wants an ensemble
perturbed in the atmosphere as well as in the ocean.

Both are data problems rather than workflow ones, and both belong with the nature run below.
Until they are solved, an LETKF scored against a 3DVar here is not being scored on its merits,
which is the reason that work should precede any comparison between solvers.

Still owed, and moved here from this phase's original list: a home for the **temperature and
salinity clamping** v2 did inside its checkpoint. Nothing has needed it yet, and writeback's
existing guard is a refusal on a non-finite analysis. Add it when a cycle produces a
temperature that a clamp would have caught, not before.

## Phase 7.5. A Gulf of Mexico OSSE

Not in the original plan, and it belongs before any comparison between solvers rather than
after. Without a nature run, "did LETKF beat 3DVar" can only be answered from departures, and
each system fits its own observations, so the comparison is self-referential. A nature run
gives state-space verification, which is what `verify` exists for and what the benchmarking
premise of this repository rests on. It also gives phase 7's ensemble somewhere honest to come
from.

- **Blocked on data, not on workflow.** The Gulf domains run with a single frozen SODA
  five-day mean on the open boundary and a short forcing sample, so a months-long run needs
  time-varying boundary conditions and a real forcing archive first. That is an offline stage.
- **Decide the twin.** Nature and DA at the same resolution is an identical twin: easy, and
  flattering. Nature at 4 or 8 km against DA at 12 or 25 km is fraternal, is the honest
  version, and changes what the observation generator has to do.
- **Perturbed atmospheric forcing comes with it.** It is the same data problem, and without it
  the ensemble every phase after this one depends on cannot hold its spread. See phase 7.
- **`tools/obs-archive-osse.py` is the small version of it**, and says so: its truth is one
  state plus a fixed anomaly rather than a trajectory.

## Phase 8. Ensemble and hybrid covariance

`solver.covariance` becomes a value that is read rather than one that is only validated. Three
answers: `static`, which is what phase 6 built; `ensemble`, which drops the static B; and
`hybrid`, which weights the two.

- **Build:** the covariance assembly in `ackbar/soca.py`, the localization as a fourth product
  of the offline diffusion stage, the `da` split, and a real `recenter`.
- **Test:** tier 3, `tests/test_tier3_hybrid.py`, at gom_25km against the same domain, dates,
  ensemble and observation archive as `tier3_var` and `tier3_letkf`, so that the three differ
  in one inherited layer each. Plus tier 1 goldens for the two new shapes.
- **Done when:** a hybrid cycles with MOM6 in the loop, both components are in the document the
  application read, and every member ends its cycle centred on the deterministic analysis.

**It was not configuration only, and the prediction that it would be was wrong in one specific
way.** The covariance itself is configuration: three lines of assembly and a layer. What is not
is *where the ensemble comes from*. A covariance drawn from an ensemble needs that ensemble
maintained, cycle after cycle, and nothing in a variational experiment does that. So the cycle
gains a second analysis, which is the split `design.md` left open, and it gains a recentring,
which phase 7 predicted would arrive here.

Five things this phase settled.

**`da` split, and the name stayed.** `da` is the analysis that produces the *control's* answer,
whichever solver that is; `da.ens` is what maintains the ensemble a hybrid's covariance is drawn
from. Two nodes rather than one node parameterized by instance, because they are different
applications with different configs, different resources and different member cardinality, and
because `soca_letkf.x` running under a name that says `var` is the first thing to confuse
anyone reading a queue. Keeping `da` for the first of them means no existing shape's graph,
paths or sentinels moved.

**`recenter` moved off the LETKF, where it never belonged.** Phase 7 left it deferred with a
body that did nothing, because recentring an ensemble onto a centre it already has is the
identity. A hybrid is where the centre is something else, so `recenter` is now present for
exactly the covariances that read an ensemble and absent for the filter. It is also one job
rather than an array: the mean it subtracts belongs to every member at once.

**A distribution cannot be a layered value, and this is where that becomes visible.** soca-science
varied `&obs_distribution` per solver file, which works right up until a cycle contains two
applications reading the same observers: the solve wants round robin and the filter wants a halo.
v2 met this exactly here and patched it with `sed` markers keyed on whether the LETKF was running
solo or inside a `3dhyb`. The distribution is a property of the application, so ACKBAR sets it,
an observer layer no longer mentions it, and what an experiment states is the halo size.

**A per-member writer has to be told `type: ens`.** `soca_genfilename` puts the member index in
a name for that type and no other, so any other value has six members writing one filename in
turn while the application exits 0. What is left is a single file holding the last member's
state, and `_positions` finding nothing is the only reason it was noticed at all.

**Exactly one job may apply the divergence policy.** `replace_from_mean` rebuilds a member's
restart set, and two jobs doing that concurrently write the same file. So `da.ens` resolves the
ensemble and `da` reads the record it wrote, which is why the two are ordered rather than run
side by side even though no file passes between them. The alternative is one half of a hybrid
reading a member the other half rebuilt.

Still owed, and not owed before a result: `ensemble.source` has three values in the schema that
nothing implements (`eda`, `offline`, `perturbation`), and `graph/build.py` refuses them rather
than letting a covariance be drawn from an ensemble that nothing updates. `eda` is phase 10.

## Phase 9. 4D windows

Sub-window forecast slots, and the first phase in which the *window* stops being a property that
only the configuration knows about. Design these deliberately rather than inheriting v2's `f###`
symlink farm.

`solver.window` has been validated and unread since phase 1, exactly as `solver.covariance` was
until phase 8, and `fourd_om1deg` exists as a golden precisely so that the shape change shows up
as a diff. What follows is what that diff has to contain. It is written per mode because the
three modes differ in what they need, and only one of them needs a new task.

### The window, which all three share

**Where the analysis time sits in the window becomes a configured property of the solver.**
Centred is the current assumption and it is what `config/jobtime.py` computes: `window_begin` is
the cycle time minus half the cycle length. 3D-FGAT and 4DEnVar are both happy with that. Strong
constraint 4DVar is not: its window must *begin* at the analysis time, because the state it
solves for is the one the model is integrated forward from. So `window_begin` stops being a
constant expression and becomes a function of `solver.window`, and every symbol derived from it
follows. That is a change in `symbols()` and in the observation archive's window selection, and
it is the one change here that can silently produce a working experiment assimilating the wrong
half day of observations.

**The window length stops being the cycle length.** They are equal today and the equality is
implicit. A 4D window that overlaps its neighbours, which is common in practice, breaks it, and
so does a window shorter than the cycle. `window_length` should become its own configured
duration defaulting to the cycle length, and `cleanup` has to key off the longest window
anything still reads rather than off `n-2`.

### 3D-FGAT: no new task, one new relationship

FGAT is a 3D analysis whose observation operator is evaluated against the background *at the
observation's own time* rather than at the analysis time. The increment is still one state at
one time, so writeback, the graph and the restart handoff are all unchanged.

What changes is what the analysis reads. `cost type` becomes `3D-FGAT`, and the background stops
being a single state and becomes a sequence of them across the window: the pinned bundle's
`3dvarfgat_pseudo.yml` supplies the shape. SOCA gets those either from a `pseudo model`, which
reads pre-written states off disk, or from a real linear model, which does not exist for MOM6.
So the pseudo model is the answer, and it needs states at sub-window intervals, which is the one
thing the workflow does not currently produce.

**That is the whole of phase 9's real work, and it is the forecast's, not the analysis's.** The
cycling forecast writes a restart set and no history at all, deliberately: writing history every
cycle of an ensemble is how a free run fills a disk (`docs/design.md`, Model and DA modes). A 4D
window needs it to write a state every *slot*. That is a `diag_table` selected by purpose, which
is a mechanism phase 4 already built, plus a place for the slots to go and a rule for reaping
them.

Concretely, in order of how much they can go wrong:

1. **A slot cadence**, `solver.slots` or a duration under `window`, validated to divide the
   window length. The number of slots is what sets the cost of everything below.
2. **A `diag_table` that writes a full state per slot**, chosen by the forecast task the way the
   extended forecast's is chosen today. Not the restart writer: these are history files, and
   what SOCA reads them with is its own state reader, so the fields metadata has to name every
   background variable in them.
3. **Where they go.** `bkg/<n>/mem###/` is in `SUBDIRS` already and is unused, which is where
   this was always going to land. One file per slot, named by valid time and not by an `f###`
   offset: v2's symlink farm existed because its names were offsets and every consumer had to
   recompute them.
4. **`cleanup` reaping them.** A slot state is a full 3D field, so an ensemble at a 6 slot window
   is six times the per-cycle output of the restarts, and this is the item that fills a disk in
   the second week rather than the first.
5. **A `pseudo model` block in the var document.** Split the way every document now is: the
   block's shape, and the `cost type` beside it, are a sibling file under `config/soca/`, since
   FGAT differs from 3D-Var in structure rather than in values. The *states* it names stay a
   slot that `ackbar/soca.py` fills from the same list that decided where the forecast wrote
   them, because a document and a directory listing that disagree is an analysis reading a
   state from the wrong hour.

### 4DEnVar: the slots, times the ensemble

4DEnVar is phase 8's ensemble covariance with the members supplied *per slot*: the localized
sample covariance is built across time as well as space, so the increment is four-dimensional
without a linear model anywhere. `4denvar.yml` in the bundle is the shape, and
`4dhybenvar.yml` is it with the static component beside it.

Everything above applies, and one thing is added: the slot states have to exist **for every
member**, not just for the control. So the `bkg/` output and its reaping become member-level,
and the disk figure is slots times members times a full state per cycle. On a regional domain
that is affordable and on `OM4_025` it is the constraint the phase is designed around.

Structurally the analysis document changes and nothing else does. The ensemble the covariance
reads becomes a list of *lists*, one per slot, and `soca.member_states` grows a time axis; the
localization gains a time dimension in its own configuration. `da.ens`, `recenter`, `writeback`
and the graph are untouched, because what a 4DEnVar writes back is still one analysis at one
time per member.

### 4DLETKF: the smallest change of the three

An LETKF is already a 4D filter when its observations are distributed through a window: the
solver interpolates each member to the observation's time, which is what `letkf.yml` does
against `letkf3d.yml` in the bundle. So the ensemble filter needs the same per-member slot states
4DEnVar needs and nothing else at all: no cost function, no pseudo model, no minimizer.

Which means the ordering inside phase 9 is not the ordering in the title. Build the slot states
first, on the free run, where they are cheap and where the only question is whether SOCA can read
what MOM6 wrote. Then 4DLETKF, which consumes them and adds no other machinery. Then 3D-FGAT,
which adds the pseudo model and the window placement. Then 4DEnVar, which is the second of those
crossed with phase 8's covariance, and 4DVar last if at all, since it needs a linear model that
does not exist for this model.

### What phase 9 does not need

No new solver, no new writeback, and no change to the restart handoff. The increment is still one
state at one time in every mode above; what is four-dimensional is the *comparison*, and that is
what the slot states are for.

## Phase 10. EDA

As an ensemble source. Mostly falls out of phase 7.

## After that: regional

All of the above is on the global domains, `OM_1deg` for development and `OM4_025` for
production. Regional comes once global cycling works. See Domains in `design.md` for what it
pulls in.

## Spikes, and the phase each must precede

These are the questions whose answers invalidate work already done, so they are dated by what
they would break rather than by when they are convenient.

| Spike | Must precede | Why |
|---|---|---|
| ~~Symmetric memory: how SOCA's own MOM6 is compiled for regional~~ | ~~phase 4~~ | **Answered: SOCA's is non-symmetric, the forecast model's is symmetric, and they are not going to be reconciled yet.** It bites earlier than the restart shapes it was expected to: MOM6 refuses to *configure* Flather OBCs in a non-symmetric build, so every SOCA application aborts inside `soca_geom_init` on a regional domain. Worked around per domain by `MOM_override.soca`, which switches the segments off for SOCA only; the grid is the same grid either way and the grid is all SOCA wants. Rebuilding SOCA's MOM6 with symmetric memory is still the real fix, and it is still a build-level decision. See `docs/domains.md`. |
| ~~MOM6 back-compat parameter pins (`EQN_OF_STATE = "WRIGHT"` and friends)~~ | ~~phase 4's offline IC~~ | **Answered: do not pin them, and turn the bug flags off instead.** Neither case sets `EQN_OF_STATE`, so both inherit the current `WRIGHT_FULL`. What did need deciding is the other direction: MOM6-examples' `OM_1deg` *enables* seven MOM and four SIS bug-retention flags whose defaults are now off, to protect its own regression answers. ACKBAR's overrides turn them off, which does invalidate initial conditions produced before that, and the smoke ICs are cheap to rebuild. |
| ~~A cheap parse-and-exit path in SOCA or OOPS~~ | ~~phase 1~~ | **Answered: no.** It existed once but not at the application level any more, and only some components validate their own config. `validate` therefore has six steps, not seven, and a bad JEDI config is found when the executable runs. See Configuration validation in `design.md` for what carries that weight instead. |
| ~~IAU versus direct restart write~~ | ~~phase 6~~ | **Answered: direct write, and it was not a choice.** `soca_checkpoint_model.x` does not exist in the pinned SOCA, so writeback is python editing a copy of the background. IAU remains an alternate implementation behind the same graph edge rather than a different graph. See [`analysis.md`](analysis.md). |
| `srun` and PMI on rancor | with phase 4 | Job steps and per-step accounting differ between `mpiexec` and `srun`. |
