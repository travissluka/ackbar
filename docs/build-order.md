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
| 3 | real cycling, `OM_1deg`, a few cycles | full build | tens of minutes | per phase |
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
- **Test:** tier 3, a few cycles at `OM_1deg`. Tiers 0 through 2 keep passing.
- **Done when:** N cycles are restart continuous, and a killed cycle resumes and reproduces.

Also here, and the prerequisite for the rest of it: **`srun` launching MOM6 on rancor**, so that
job steps, per-task binding and per-step accounting are the same thing here as in production
rather than two regimes to reason about. `srun --mpi=pmi2` against spack-stack's MPICH needed no
rebuild and no PMIx; what it needed was knowing that `--mpi=none` silently gives every rank its
own `MPI_COMM_WORLD` instead of failing. See "srun and PMI" in `docs/slurm.md`, which also
carries the measured `MaxRSS` difference between the two launchers.

Four things this phase settled.

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
- **Done already:** the background error's correlation, as an offline stage rather than as a
  task. `tools/soca-diffusion.sh` calibrates it and `tools/soca-dirac.sh` checks it, both keyed
  on domain. See [`background-error.md`](background-error.md).
- **Build:** the DA task and the writeback task. The per cycle vertical B calibration is a
  measured improvement on the offline one rather than a prerequisite for it, so it is not in
  this phase.
- **Order within the phase:** bring it up on `model: persistence` first. That gives the full DA
  loop, including writeback and background handoff, at no model cost, and a baseline to score
  against once MOM6 is back in the loop. Then switch to `model: mom6sis2`.
- **Done when:** persistence 3DVar cycles and scores against the free run, and the same
  experiment with MOM6 in the loop does too.

## Phase 7. LETKF

Parallel members, ensemble initial conditions, recentering. Member arrays now carry a real
model, so this is where the phase 2 array work meets real cost.

Requires a stated **divergence policy** for a missing or bad member, and a home for the
temperature and salinity clamping that v2 did inside its checkpoint.

## Phase 8. Ensemble and hybrid covariance

Configuration only, once 6 and 7 both work. No new tasks. If this phase needs new machinery,
something earlier was wrong.

## Phase 9. 4D windows

Sub-window forecast slots. Design these deliberately rather than inheriting v2's `f###` symlink
farm.

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
| IAU versus direct restart write | phase 6 | Decides what the writeback task is. Resolve by spike, not on paper. |
| `srun` and PMI on rancor | with phase 4 | Job steps and per-step accounting differ between `mpiexec` and `srun`. |
