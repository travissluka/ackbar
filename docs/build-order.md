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
| 3 | real cycling, `OM_1deg`, a few cycles | full build | hours | per phase |
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
- **Test:** tier 1. Golden graphs for free run, 3DVar, LETKF, hybrid, 4D, OSSE, and a long
  forecast cadence. Determinism is a test: generate twice, compare. Properties: the graph is
  acyclic, every member level array carries the canonical index set, no leaf has a successor.
- **Done when:** a 50 cycle, 20 member hybrid graph generates in seconds and matches its golden,
  and `validate` rejects a missing input path and a missing executable before anything is
  submitted.

## Phase 2. Emitter, ledger, submitter, and the stub model

The first phase that touches Slurm. This is the milestone the project's premise rests on.

- **Build:** the stub model, job script emission, `sbatch` with `--comment` identity, the
  append-only ledger, member arrays wired with `aftercorr`, the cycle throttle and the
  submitter job, and two local Slurm profiles (one with
  `DependencyParameters=kill_invalid_depend`, one with enforced limits and a small
  `MaxSubmitJobs`).
- **Test:** tier 2. A 20 member, 3 cycle experiment at 1 PE and 30 seconds a member. Then the
  fault matrix, injected deterministically: nonzero exit, run past the time limit, impossible
  memory request, exit 0 having written nothing, requeue mid-task, and a member that never
  starts.
- **Done when:** the clean run finishes in about two minutes, and every fault reaches its
  intended terminal state on **both** Slurm profiles with nothing left pending and undiagnosed.

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

Also here: **get `srun` launching MOM6 on rancor**, which needs a PMI plugin Slurm can talk to.
Without it, job steps, per-task binding, and per-step accounting all differ between rancor and
production, exactly where the interesting behavior lives.

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

## Phase 6. Variational, static B, 3D

Static B and analysis-to-restart writeback.

- **Prerequisite spike:** IAU versus direct restart write, run *before* this phase, not inside
  it.
- **Build:** the DA task, the writeback task, and the per cycle vertical B calibration task.
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
| Symmetric memory: how SOCA's own MOM6 is compiled for regional | phase 4 | It is a build level decision. Discovering it late means rebuilding everything the workflow was validated against. |
| MOM6 back-compat parameter pins (`EQN_OF_STATE = "WRIGHT"` and friends) | phase 4's offline IC | Dropping them invalidates any initial condition produced under the old physics. |
| A cheap parse-and-exit path in SOCA or OOPS | phase 1 | If one exists, a cycle-1 dry run through the real executables becomes a seventh `validate` step, and that changes what phase 1 builds. |
| IAU versus direct restart write | phase 6 | Decides what the writeback task is. Resolve by spike, not on paper. |
| `srun` and PMI on rancor | with phase 4 | Job steps and per-step accounting differ between `mpiexec` and `srun`. |
