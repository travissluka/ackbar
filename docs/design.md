# mmw workflow design

Design decisions for the cycling SOCA workflow. Rationale traces back to
`docs/prior-workflows.md` wherever a decision exists to avoid repeating v2 or v3.

## Principles

**Everything an experiment needs exists before the experiment starts.** Initial conditions,
static B, observations, and forcing are produced by separate offline stages and consumed
read-only. Experiments never generate their own inputs.

This is the single largest departure from v2, where a missing input triggered generation
inside the first cycle. That made cycle 1 structurally different from every other cycle (a
permanent source of "works on cycle 2, fails on cycle 1" bugs, and the entire reason v3 needed
`first_cycle_only` dependency machinery), and it let inputs vary silently between experiments
that were meant to be comparable.

**Nothing that can run concurrently runs in a loop.** Ensemble members are independent
scheduler units. Post-processing never blocks the next cycle.

**Slurm is assumed.** Not abstracted over.

## Execution model

Slurm is a hard dependency. Every target machine has it, and rancor now has a single-node
install for development (`docs/slurm.md`).

Consequences:

- **Slurm owns the dependency graph.** Jobs are submitted with `--dependency` edges and left
  alone. No daemon holds the graph in memory, no polling loop drives it forward, and there is
  no second state database: `sacct` is the state store. Rocoto's scheduling layer is
  redundant here. Its retry and monitoring layers are not, and are handled separately below.
- **The graph is reconstructible and addressable.** Because it is a pure function of the
  merged config, it can be regenerated at any time, and a subgraph ("cycle *n* from task *x*
  onward") can be resubmitted without touching the rest. This is what makes healing possible,
  and it constrains graph generation to be deterministic and side-effect free.
- **Jobs are named systematically.** Cycle, task, and array index encode into the job name so
  that `sacct` rows are attributable after the fact without a lookup table.
- **Job arrays for ensemble members.** `--array` for per-member forecasts and per-member
  checkpointing. `--dependency=aftercorr` gives elementwise array-to-array edges, so member
  *i*'s post-processing follows member *i*'s forecast with no barrier across members. This
  capability is the main reason not to abstract over the scheduler.
- **Cycling without a daemon.** Each cycle's graph includes a job that submits cycle *n+K*.
  Self-perpetuating, like v2's resubmission, but at the graph level instead of stuffing an
  entire cycle into one job. *K* is the cycle throttle and falls out as jobs in flight.

If a non-slurm machine ever matters, the escape hatch is an adapter that presents the slurm
interface and runs everything serially. It is a fallback of last resort, not a supported
second backend, and it must not fork the graph semantics.

There is still a thin **site layer**: spack-stack lives at a different path on each machine,
plus partition, account, core count, and MPI launcher. That is site configuration, not a
workload manager abstraction. v2's `MACHINE` / `MODEL_SCRIPT` indirection layers are not
carried forward.

## Cross-cycle overlap

The only genuine cross-cycle edge is `forecast(n) -> da(n+1)`. Everything else is either
intra-cycle or a leaf with no successor, so it overlaps the following cycle freely:

- observation statistics
- state compression, ensemble mean and spread
- extended (long) forecasts and their verification
- plotting

This kills v2's one-job-per-cycle model and its single `cycle_status` date file. State is
per-task-per-cycle and lives in slurm.

Two consequences that are easy to miss:

- **Cleanup must be dependency-aware.** v2's age-based sweep over `rst/` races with in-flight
  post-processing: it can delete cycle *n*'s restarts while a consumer is still reading them.
  Retention keys off "all consumers completed", not date arithmetic.
- **Disk is the binding constraint, not CPU.** Roughly 1 GB per restart set, times members,
  times cycles in flight. The right value for the cycle throttle is set by `df`, not by cores.
  On rancor an 8-PE model run leaves no spare cores for post-processing to overlap into, so
  the local wallclock win is small. The structure still matters, and pays off at `OM4_025`.

## Resource accounting

Per-stage CPU and memory usage is recorded for every job, to be accumulated and analyzed
offline. v2 tracked exactly one number, the average cycle runtime, and only so it could decide
when to resubmit itself. That is not enough to size jobs or find the real bottleneck.

Slurm already collects this, so the work is harvesting and attribution, not measurement:
`Elapsed`, `TotalCPU`, `AveCPU`, `MaxRSS`, `ReqMem`, `AllocCPUS`, `NNodes`, and the exit state,
per job and per job step. Job arrays produce one row per member.

- **Harvest per cycle, accumulate offline.** `sacct` records are purged from slurmdbd after a
  site-configured retention period, so rows must be pulled into the experiment directory while
  they still exist. A small leaf task per cycle writes them to `stats/`; analysis happens
  later against the accumulated files, not against the live database.
- **Attribution comes from the job naming convention**, so a harvested row maps back to cycle,
  task, and ensemble member without a side table.
- **This feeds the healer.** Knowing a task's actual peak memory and runtime is what lets an
  automatic relaunch escalate resources on a timeout or an out-of-memory kill, rather than
  resubmitting into the same wall.

Requires slurm accounting to be gathering resource data rather than just job records
(`JobAcctGatherType` set to a cgroup or linux gatherer, with a sampling frequency fine enough
to catch peaks). Worth verifying on rancor before relying on `MaxRSS`.

## Monitoring and healing

HPCs terminate jobs for reasons that have nothing to do with the science. The workflow needs to
survive that unattended, and it needs to be observable without watching `squeue`.

Two separate things, deliberately not fused:

- **`status`, a read-only client.** One-shot or refreshing view of the experiment as a grid of
  cycles against tasks, built from `sacct` plus the generated graph. Runs anywhere, holds no
  state, and closing it does nothing.
- **`heal`, an explicit opt-in.** Detects failed jobs and resubmits the affected subgraph.

Keeping them apart matters: v3's driver was a curses UI in a foreground process
(`curses.wrapper`) that had to stay running for the workflow to advance, so a dropped ssh
session stalled the experiment. Viewing must never be load-bearing.

**The healer runs on the cluster, not on a workstation.** A low-resource recurring job that
re-submits itself on an interval. Unattended healing without a daemon on your laptop, and
without the workflow depending on your terminal.

Design points:

- **A failure cascades.** Under `afterok`, everything downstream of a failed job goes to
  `DependencyNeverSatisfied` and is cancelled. So healing is never just "resubmit that job":
  it is "regenerate the subgraph from that task onward and resubmit it". This is why graph
  generation has to be addressable.
- **Retry safety depends on task idempotency.** A relaunched task must detect its own
  completed output and exit cleanly. v2 had this property and let it decay (the skip-if-done
  blocks in `run.var.sh` and `run.fcst.sh` are commented out); here it is load-bearing for
  automatic retry, not just for crash recovery, so it is declared once centrally rather than
  reimplemented per task.
- **Failure classes get different policies.** Slurm distinguishes `NODE_FAIL`, `PREEMPTED`,
  `TIMEOUT`, `OUT_OF_MEMORY`, and plain `FAILED` with an exit code. Infrastructure failures
  retry freely; timeouts and out-of-memory kills retry with escalated resources informed by the
  harvested statistics; a genuine nonzero exit is a science or configuration error and must
  not be retried into an infinite loop. Retry counts are bounded per task and the healer stops
  and says so rather than churning.

## Configuration

Explicit named layers, deep-merged in order, later wins. The experiment file declares its own
inheritance:

```yaml
inherit:
  - model/om_1deg
  - da/variational
  - covariance/hybrid
  - window/3d
  - obs/osse
ens_size: 20
```

v3 had layering, but implicitly, through nearest-enclosing-dict scoping during token
resolution. That is surprising in the wrong way and impossible to reason about from the
experiment file alone.

Rules:

- **Merge before substitution.** Otherwise a layer cannot override a value another layer has
  already interpolated.
- **Lists merge by name, not position.** The trap case is `observations:`. A `da/letkf` layer
  must be able to change one observer's localization without restating all of them.
- **Provenance is recorded during the merge.** Every resolved value knows which layer set it.
  Neither v2 nor v3 can answer "why is this parameter that", and it is miserable to retrofit.
- **Resolve once, write to the experiment directory, run only from that.** v3 got this right
  (`cfg/exp_config.yaml`). It is what makes an experiment reproducible after the repo moves
  underneath it.
- **Layers can enable and disable tasks, not just set values.** OSSE adds a truth run and an
  obs generation stage; long forecasts add tasks on their own cadence. The task graph is
  therefore a function of the fully merged config and is computed after merge.

YAML is generated from data structures, following v3's include-substitute-filter pipeline.
v2's three layers of `sed` (token replacement, `!IF_APP_*` line prefixes, and block splicing
with hardcoded indentation) are not carried forward in any form.

## DA modes

v2 had a seven-way case statement over `DA_MODE`. Most of those modes are the same code with
different covariance or window settings. The real axes:

| Axis | Values |
|---|---|
| solver | `none`, `variational`, `letkf` |
| covariance (variational only) | `static`, `ensemble`, `hybrid` |
| window | `3d`, `fgat`, `4d` |
| ensemble source | `letkf`, `eda`, `perturbation`, `external` |

So v2's modes map as: `3dvar` = variational+static+3d, `3denvar` = variational+ensemble+3d,
`3dhyb` = variational+hybrid+3d, `3dfgat` = variational+static+fgat, `4denvar` and `4dhyb` are
the 4d column, `letkf` is the other solver, and **`eda` is not a mode at all**, it is an
ensemble source.

Two solvers, and the variational one is parameterized. The configuration layers carry the
parameterization, so there is no mode dispatch in the code.

**`noda` is not a mode either.** Two independent properties: does the run produce an analysis,
and does it evaluate observations. A free run does neither, hofx evaluates only. So a free run
is `solver: none`, and observation evaluation is a property of any run. In a DA run the
analysis application produces `ombg`/`oman` itself; in a free run a standalone hofx task
produces the same diagnostics.

## Observations

**No downloading inside the cycle.** v2 could download and convert observations mid-cycle
(`OBS_GEN_ENABLED`, `OBS_*_DWNLD`, `OBS_*_CNVRT`, the `scripts/obs/*.sh` downloader set)
because it was built with realtime running in mind. These are retrospective experiments. The
in-cycle obs step reduces to: find the file covering this window in the archive, link it, and
drop observers whose input file is absent (unless marked required, per v3's `_required`).

**OSSE first.** Synthetic observations generated from a truth run are the first obs source.
Real observations come later, via an offline archive-building script, using v2's downloaders
and ioda converters as a one-time tool rather than a per-cycle step.

This makes hofx load-bearing early: it is both the diagnostic path for free runs and the OSSE
observation generator.

The ~25 per-platform observer configs in `~/work/soca-science/configs/soca/obs/` are the most
reusable asset in either prior repo and should be ported (and revalidated against current
SOCA) regardless of obs source.

## Offline stages

Each produces versioned, read-only inputs. Experiments are pure consumers.

| Stage | Keyed on | Contents |
|---|---|---|
| static | resolution | `soca_gridspec.nc`, horizontal correlation scales, localization scales |
| initial condition | source and date | a spun-up restart set |
| observations | period | archive of ioda files, real or OSSE-generated |
| forcing | period | atmospheric forcing archive |

Static is keyed on **resolution, not experiment**, so one `static/om_1deg/` is shared
read-only across every experiment at that resolution. That is what makes experiments
comparable by construction.

**Spinup is a separate job script**, not part of any experiment. It either cold starts from
WOA13 and integrates with realistic atmospheric forcing, or converts an external state (GFS,
ORA5, and similar). Experiments assume an initial condition already exists.

**Vertical B is the one exception.** Vertical diffusion scales track the mixed layer and
depend on the background, so they cannot be precomputed offline. Calibration is an explicit
per-cycle task in the graph (v2 ran it every cycle too, but as a side effect of a prep step
rather than a first-class task). Horizontal and localization scales stay offline in `static/`.

## Task graph

Per cycle, roughly:

```
  forcing, obs staging  ->  B.vt  ->  da  ->  writeback  ->  forecast (ctrl)
                                                         ->  forecast (ens array)
                                                         ->  forecast_ext (leaf, own cadence)
  da, forecast  ->  post.obs, post.state, verify, stats  (all leaves)
  cleanup  ->  gated on all consumers of the target cycle
```

Cross-cycle: `forecast(n) -> da(n+1)`, and nothing else.

Ensemble shape: per-member forecasts are a job array; LETKF is a single MPI job consuming all
members; recentering and per-member writeback are arrays. Every one of these is a serial
`for` loop in v2.

Long forecasts are leaves on their own cadence, independent of cycle length. A 7 day forecast
off every 24 hour cycle is seven times the model cost of the cycling itself, so cadence is a
setting, not the cycle period. They need a different `diag_table` from cycling forecasts
(interval diagnostics for scoring, rather than restarts), which is a config layering case:
model configuration varies by forecast purpose. Which members to extend is a setting,
defaulting to the control member.

## Build order

Chosen by what each step de-risks, not by which mode it is. Three real implementation
milestones; everything else is configuration.

1. **Free run cycling** (`solver: none`). The spine: cycle, restart, resume, clean up. Also
   produces the OSSE truth run.
2. **hofx.** Exercises the entire observation pipeline with no analysis entangled in it, and
   doubles as the OSSE observation generator.
3. **Variational + static + 3d.** Milestone: static B, and analysis-to-restart writeback.
4. **LETKF.** Milestone: parallel members, ensemble initial conditions, recentering.
5. **Ensemble and hybrid covariance.** Configuration only, once 3 and 4 both work.
6. **4D windows.** Milestone: sub-window forecast slots. Design these deliberately rather than
   inheriting v2's `f###` symlink farm.
7. **EDA** as an ensemble source, which mostly falls out of 4.

## Open

- **Analysis to MOM6 restart.** Both IAU and direct restart write are wanted, selectable per
  experiment. Investigate what SOCA offers now before writing anything; if nothing suitable
  exists, a python direct write based on v2's `soca_domom6_action.py` is the fallback, and
  v2's `socaincr2mom6` is the starting point for IAU. Resolve by spike test during
  implementation, not on paper. The graph must accommodate both shapes: direct write is a
  discrete task between analysis and forecast, IAU makes the increment an input to the
  forecast task instead.
- **MOM6 back-compat parameter pins.** Whether SOCA configs keep `EQN_OF_STATE = "WRIGHT"`
  and friends or drop them for corrected physics. See `docs/model-build.md`. Coupled to
  spinup: dropping them invalidates any initial condition produced under the old physics, so
  decide before generating one.
- **Packaging.** Python package versus scripts. Layered config with provenance and generated
  task graphs effectively forces python for the workflow itself; the question is really how
  much lives in the package versus in job scripts it emits.
- **Ensemble geometry on rancor.** 8 cores total against an 8-PE model run means parallel
  members require fewer PEs per member (4 members at 2 PEs) or oversubscription. Affects
  whether member parallelism is demonstrable locally, not whether it is designed in.

## Not carried forward

From v2: in-cycle observation downloading, R2D2, the regional hack, the `MACHINE` and
`MODEL_SCRIPT` indirection layers, `sed`-templated YAML, environment variables as the
inter-step interface, generate-if-missing inputs, the 3D-to-4D symlink farm, and every serial
member loop.

From v3: rocoto, implicit scope-based config resolution, and the rule that a task cannot
resume if its working directory exists.
