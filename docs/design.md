# ACKBAR workflow design

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

**Every cycle looks the same.** Not just inputs: the graph, the paths, and the resolution
rules for cycle 1 are identical to those for cycle 50. Anywhere the first or last cycle needs a
special case, the fix is to make setup or teardown produce the thing that makes it ordinary.

**Nothing that can run concurrently runs in a loop.** Ensemble members are independent
scheduler units. Post-processing never blocks the next cycle.

**A task either committed its output or it did not.** There is no partially complete task. This
is what makes retry safe, and retry is not optional (see Task completion and idempotency).

**Fail before submitting, not three cycles in.** Everything checkable is checked once, up
front, against the fully resolved configuration.

**Slurm is assumed.** Not abstracted over.

## Repository and package layout

ACKBAR builds everything it runs. Both the JEDI bundle and the forecast model are vendored as
submodules under `pkg/`:

```
pkg/
  jedi/            bundle CMakeLists
    oops/          submodule
    saber/         submodule
    ioda/          submodule
    ufo/           submodule
    soca/          submodule
    ...
  mom6sis2/        submodule, NOAA-GFDL/MOM6-examples
build-jedi.sh
build-model.sh
```

The reason is provenance, not tidiness. One ackbar commit pins the exact JEDI and MOM6 source
that produced a result, so "which code ran" is answerable from the experiment record rather
than from whatever happened to be in a shared build directory that day. A personal bundle
elsewhere on the machine stays useful for development, but it is not what experiments run
against.

Note that SOCA links its own MOM6 as a library, from NOAA-EMC, which is a different repository
from the forecast model's MOM6. Both pins are recorded; keeping them compatible is a standing
concern, not something the layout solves. See `docs/model-build.md`.

## Execution model

Slurm is a hard dependency. Every target machine has it, and rancor has a single-node install
for development (`docs/slurm.md`).

Consequences:

- **Slurm owns the dependency graph.** Jobs are submitted with `--dependency` edges and left
  alone. No daemon holds the graph in memory and no polling loop drives it forward. Slurm is
  authoritative for *outcome*. It is not authoritative for *identity* (see Experiment state).
- **The graph is reconstructible and addressable.** Because it is a pure function of the
  merged config, it can be regenerated at any time, and a subgraph ("cycle *n* from task *x*
  onward") can be resubmitted without touching the rest. This is what makes healing possible,
  and it constrains graph generation to be deterministic and side-effect free.
- **Jobs carry their identity twice.** In the job name, as
  `<exp>.<cycle>.<task>` with the array index carrying the member, so `sacct` rows are readable
  by eye; and in `--comment=ackbar:<exp>:<cycle>:<task>`, which survives into accounting where
  `AccountingStoreFlags` includes `job_comment` and comes back as a structured field from
  `sacct --json`. Parse `--json`, never fixed-width columns: the default `JobName` field is
  truncated and `-P` uses a delimiter that collides with structured comments. The experiment
  name is part of the identity because running several experiments at once is the entire point
  of the project, and without it `sacct --name` attributes rows to the wrong experiment and
  `--dependency=singleton` silently couples unrelated experiments.
- **Job arrays for ensemble members.** `--array` for per-member forecasts and per-member
  checkpointing. `--dependency=aftercorr` gives elementwise array-to-array edges, so member
  *i*'s post-processing follows member *i*'s forecast with no barrier across members. Pin one
  canonical index set for every member-level array and assert it in the generator, because
  `aftercorr` behavior on mismatched index ranges is not documented and a mismatched index
  produces an invisible forever-pending job rather than an error.
- **Cycling without a daemon.** Cycle *n*'s graph includes a job that submits cycle *n+K*.
  Self-perpetuating, like v2's resubmission, but at the graph level instead of stuffing an
  entire cycle into one job.

There is a thin **site layer**, described below. v2's `MACHINE` / `MODEL_SCRIPT` indirection
layers are not carried forward, and neither is an abstraction over Slurm itself. If a machine
without Slurm ever matters, the graph is data and a different emitter gets written then.

## The site layer

Machine-dependent paths are not eliminated, because they cannot be. They are **confined to one
file per machine**, and nothing outside that file may name a path that only exists on one
machine.

`site/<name>.sh`, selected by `$ACKBAR_SITE` and defaulting to the short hostname. It is
sourced by the build scripts and read by the workflow, so there is exactly one place to edit
when ackbar lands on a new machine, and one file to read to find out what a machine assumes.

What it carries:

| | |
|---|---|
| environment | how to activate spack-stack (its path is machine dependent, and on rancor it currently lives in a personal `env.sh` outside the repo) |
| build | `NJOBS`, build type, and the CMake generator |
| data roots | dataset root, `static_root` (what the offline stages produce), `scratch_root`, `output_root` |
| scheduler | partition, account, MPI launcher, `max_submit_jobs`, `max_array_size`, `can_submit_from_compute` |

The roots reach configuration layers as the experiment-time symbols `$(ackbar_root)`,
`$(datasets_root)` and `$(static_root)`, which is how a model layer names a grid file or an
executable without becoming machine-specific itself. A symbol the site does not define is absent
from the table rather than empty, so a layer that needs one fails with "unknown symbol" instead
of resolving to a path that begins at the root of the disk.

The MPI launcher is the whole command, `srun --mpi=pmi2` rather than `srun`. Which PMI plugin a
site defaults to is a site's business and getting it wrong does not fail: `--mpi=none` runs every
rank as its own `MPI_COMM_WORLD` of size 1 and exits zero (`docs/slurm.md`, srun and PMI).

**Make is the default generator.** Ninja is faster and is what rancor happens to have, but it
is not reliably present on HPC, and a build that only works where Ninja is installed is a
machine dependency wearing a different hat. A site file may opt into Ninja; nothing else may
assume it.

This is worth doing early rather than late, because the leaks accumulate quietly. Already
present before any workflow code exists: `source ~/work/env.sh` in both build scripts, a
`/data/...` dataset root reached through a symlink inside a submodule, `/data/ackbar` scratch
paths in the Slurm smoke test, and a CMake generator exported by a personal environment file.
None of those are wrong on rancor. All of them are wrong everywhere else.

### What Slurm actually does, as opposed to what is convenient to assume

Four behaviors that the obvious implementation gets wrong. Each is a property of Slurm or of
site configuration, so check rather than assume.

**Failed-upstream dependents are not cancelled by default.** They pend forever with reason
`DependencyNeverSatisfied`, and only reach a terminal state if the site sets
`DependencyParameters=kill_invalid_depend`. Check with
`scontrol show config | grep -i dependency`. Handle both cases and depend on neither: `status`
and `heal` read `squeue -h -o '%i %T %r'` and treat `DependencyNeverSatisfied` as
terminally failed, because such a job never appears as failed in `sacct` at all. A local
Slurm profile with the parameter set exists so both behaviors are testable.

**Requeue permanently poisons dependents.** From `man sbatch`: once a dependency fails, the
dependent never runs "even if the preceding job is requeued and has a different termination
state in a subsequent execution". So `scontrol requeue` is never a valid healing action.
Healing always means fresh job ids for the entire affected closure.

**Requeue reruns the batch script from its beginning**, and requeue on node failure is enabled
by default (`JobRequeue`). Any job that has side effects before it can be interrupted must
assume it will run twice. This is why every task is idempotent and why the submitter needs the
specific protections below.

**`afterok` edges expire.** A dependency can only be created while the target job is active or
within `MinJobAge` after it ends. That is minutes at most, and shorter at many sites. Edge
construction is therefore state-aware, not blind: for each upstream job id, still in `squeue`
means keep the edge, already `COMPLETED` means drop the edge (the artifact is on disk and the
edge is redundant), failed or absent-and-not-completed means refuse to submit and exit nonzero.
This makes the submitter and the healer real programs rather than `sbatch` wrappers.

Two smaller ones. `afterok` on an array is all-or-nothing, which is correct for LETKF and wrong
for observing leaves: the stats harvest is exactly the task most needed when a member fails, so
observing leaves use `afterany` and tolerate missing rows. And Slurm's dependency cycle
detection stops at `max_depend_depth` (default 10), so the generator topologically sorts and
fails on a cycle itself.

### Queue limits

Expected in-flight job count is roughly `K * (scalar tasks + member-level arrays * N)`, and
every array task counts individually against submit limits. At large ensemble sizes this
approaches the per-user limits some sites impose. The site layer carries `max_submit_jobs` and
`max_array_size`; the generator computes the projected in-flight count and refuses to start
rather than discovering the limit three cycles in, where a rejected `sbatch` inside the
submitter stops the experiment silently.

Some sites also forbid submitting from compute nodes, which would kill the self-perpetuating
submitter outright. The site layer carries `can_submit_from_compute`, checked first thing on
any new machine.

### The cycle throttle

`K = 1`. One cycle in flight, submitted by the previous cycle.

`K > 1` multiplies the interaction surface with healing and cleanup, and buys little: the
overlap that matters comes from leaves having no successors, which happens at any `K`. It also
enlarges an already large blast radius, since `forecast(n) -> da(n+1)` means one failed forecast
invalidates every cycle in flight. Disk argues the same way, at roughly 1 GB per restart set
times members times cycles in flight. Make it a setting when `df` and a measured queue wait say
so, not before.

### The submitter

Cycle *n*'s graph contains one job that submits cycle *n+1*. Its rules:

- **Gated `afterok` on cycle *n*'s forecast.** Fail-stop is the right default for science: one
  transient node failure stops cycling and needs a heal, which is much better than the chain
  outrunning a failure and producing cycles of garbage off a bad background.
- **`--no-requeue`**, plus an `O_EXCL` per-cycle submission marker written before any `sbatch`
  call, plus the ledger as the authority on "was this cycle already submitted". Without all
  three, a requeued submitter submits an entire cycle twice and two graphs race into the same
  directories. That is the most likely way to silently corrupt an experiment.
- **Exits nonzero if any `sbatch` fails**, so a stopped chain appears as `FAILED` rather than as
  a `COMPLETED` job with no effect.
- **Checks the halt flag first**, and checks free disk against projected usage. Either one
  short-circuits submission.

### Starting, stopping, and finishing

The merged config declares a terminal cycle. "Finished" means the ledger shows the terminal
cycle's last task complete; anything else with an empty queue is "stalled", and `status` says
so. Without this, a killed submitter is indistinguishable from a completed experiment: nothing
is `FAILED`, the queue drains, and it looks done.

Two verbs, because they are different intentions:

- `ackbar pause` touches a halt flag in the experiment directory. Every submitter checks it and
  exits 0 without submitting, so the graph drains and the experiment stops at a clean cycle
  boundary. This is the normal way to stop.
- `ackbar cancel` cancels the union of the ledger's live job ids and a `squeue` name-prefix
  scan. `scancel` alone races the submitter and kills forecasts mid-restart-write, and there is
  no name glob, so this has to be built or people will reach for `scancel -u`.

`ackbar resume` reads the highest submitted cycle from the ledger and re-arms.

## Experiment state: the ledger

Slurm answers "how did job 12345 end". It cannot answer "what is the job id of cycle 7's
writeback", which is exactly what `heal`, `cancel`, and `status` need, and its records are
purged on a site retention that can be weeks.

So the experiment directory holds an **append-only ledger**: one record per submitted job with
experiment, cycle, task, member, attempt number, job id, and submit time.

This is not the "second state database" the execution model rejects. It holds **identity**
only, is written once at submit, and is never a source of truth about outcome. Slurm remains
authoritative for state. Retry counters live here because they cannot live anywhere else:
job names are deliberately identical across attempts, and `sacct` rows purge.

The per-cycle resource harvest (below) writes alongside it, and together they are what makes an
experiment answerable after `sacct` has forgotten it.

## On-disk layout

Scratch and output are separate roots, named separately by the site layer, because on a real
machine they are different filesystems with different purge policies:

```
site:
  scratch_root: /data/ackbar/scratch      # HPC: /lustre/scratch/$USER/ackbar
  output_root:  /data/ackbar/exp          # HPC: /project/ackbar
```

```
<scratch_root>/<exp>/<cycle>/<task>[.<member>]/
    working directory: model inputs, INPUT/, logs, everything transient
    removed by the task on success, retained on failure

<output_root>/<exp>/
    HALT          present while paused; every submitter checks it
    cfg/          resolved config, the ordered layer files verbatim, provenance record
    cfg/<cycle>/<task>.sh   the emitted batch script, one per node
    ledger/       append-only submission records, and the per-cycle submit marker
    done/<cycle>/<task>[.mem###].json   sentinels, written last by a successful task
    stats/        <cycle>.json, one file per cycle, never appended to
    log/          job stdout and stderr, by cycle and task
    rst/<cycle>/mem###/     restart sets
    bkg/<cycle>/mem###/     backgrounds
    ana/<cycle>/mem###/     analyses and increments
    obs_out/<cycle>/        ioda output, ombg and oman
```

Rules that fall out of this:

- **A cycle directory is named by the cycle that *produced* it**, not by the valid time of what
  is in it. `rst/7` is what cycle 7's forecast wrote, and cycle 8's analysis reads it. The
  alternative, naming by valid time, reads better in isolation and costs more everywhere it
  matters: under producer naming a node's outputs are always under its own cycle number, so
  cleanup is a cycle count rather than a data-flow analysis, and the offline initial condition
  goes in `rst/0` as the output of a forecast that never ran.
- **Scratch is deleted by the task itself on success and kept on failure.** A failed cycle
  leaves everything needed to debug it; a successful one leaves nothing.
- **Job scripts are emitted once, at create time, for every cycle.** They are header carriers,
  not generated code: the body is one call back into ACKBAR, so a task is python that can be
  tested with no scheduler. `--array` and `--dependency` are deliberately absent from the
  script and passed on the `sbatch` command line, because they are the two values that differ
  between a first attempt and a healed one.
- **Every member is `mem###`, including the control, which is `mem000`.** No `ctrl` versus `ens`
  split anywhere in the tree. v2's `ana/{ctrl,ens}` split is precisely what made every ensemble
  loop carry a special case, and an array index that maps directly to a path is what keeps
  member-level tasks uniform.
- **A task writes only into its own scratch directory and its own output paths**, and commits
  by rename. Two tasks never write the same file.

## Task completion and idempotency

Retry is not an optional feature: node-failure requeue reruns a batch script from its
beginning whether or not a healer exists. So every task must be safe to run twice.

The central rule is **write to a temporary path, commit by atomic rename, and write a sentinel
last** recording job id, attempt, and exit state. A task skips only when the final renamed
artifact and its sentinel both exist.

Two tasks are exempt and always rerun: `stats` and `cleanup`. Neither produces an artifact of
its own, so "already done" is not a claim about a file but about a whole cycle, and a heal
changes what that cycle consists of. A skipped `stats` leaves a harvest of the abandoned run. A
skipped `cleanup` is worse: it refuses to delete while the cycle it is keeping is incomplete,
which is precisely the state a failure leaves behind, so the one run that declined becomes the
only run there will ever be and the restarts leak for the life of the experiment.

The weaker rule, skip if the output path exists, is what v2 had and it is not sufficient here.
A `TIMEOUT` or `OUT_OF_MEMORY` kill during a restart write leaves a truncated one gigabyte
`MOM.res.nc` that exists. Under skip-if-exists the retry declines to redo the very task it was
launched for and reports success. MOM6 restart writing is not atomic; nothing downstream can
tell the difference.

Tasks that need specific care, in order of how badly they fail:

- **Writeback, direct restart write.** v2's `soca_domom6_action.py checkpoint` copies the
  background restart and then writes `Temp`, `Salt`, `u`, `v`, `ave_ssh` **in place**. It is
  safe only because it re-copies the background first. A rerun that treats the copy as done
  applies the increment to an already-incremented state, and nothing in the file records that
  it happened. Always start from the pristine background, write to a temp path, commit by
  rename. IAU is structurally safer here, since the increment is a forecast input rather than a
  restart mutation.
- **Cleanup.** Retention keys off **artifact existence**, not job state: a cycle's inputs may be
  removed once every declared consumer's output exists. Keying off job state instead means a
  retried cleanup evaluates a regenerated subgraph with new job ids, concludes the old
  consumers are gone, and deletes restarts that a resubmitted consumer is about to read. The
  artifact rule also dissolves the "cleanup must be dependency-aware" problem outright: no
  dependency list, no race with healing, no age arithmetic.
- **The stats harvest.** The task most likely to run twice, and its job is to write rows.
  `stats/<cycle>.json`, one file per cycle, never appended to. Accumulate at analysis time.
- **`post.obs` statistics and `post.state` compression.** Same append hazard. Compression is
  lossy (`ncks -7 -L 4 --ppc default=.2`), so it must never run in place, and the source must
  survive until the destination is committed.
- **Anything with a random seed.** Perturbation-based ensemble sources are not reproducible
  across a rerun unless the seed derives deterministically from experiment, cycle, and member.
  v2 had a `__SEED__` token for exactly this. Decide it at design time, not when the first heal
  produces a different ensemble than the original run.
- **Forecast and analysis.** Safe with temp-then-rename, provided the completion check is a
  complete artifact set including `coupler.res` at the expected date, not directory existence.
  Which file carries that proof is model-specific, so it is asked of the model rather than
  spelled out at each of the two places that need it (the skip rule, and cleanup). Hardcoding one
  model's answer breaks cleanup under the other, and cleanup declining is a log line rather than
  a failure, so the symptom is a disk filling over days.

**Tasks whose bodies have not been written yet** run and do nothing, but only where that is
safe, and the sentinel records that it happened. The line is not "unimplemented", it is
*produces nothing anything else reads*: a leaf whose absence shows up as a missing diagnostic can
be skipped and reported, while a task in the data path cannot, because a `writeback` that
quietly did nothing means every later cycle forecasts from an unanalysed state and the
experiment looks healthy throughout. They stay in the graph rather than being cut from it, so
that the phase which adds a body is not also the phase that discovers its edges were wrong.

## Cross-cycle overlap

The only genuine cross-cycle edge is `forecast(n) -> da(n+1)`. Everything else is either
intra-cycle or a leaf with no successor, so it overlaps the following cycle freely:

- observation statistics
- state compression, ensemble mean and spread
- extended (long) forecasts and their verification
- plotting

This kills v2's one-job-per-cycle model and its single `cycle_status` date file.

Disk, not CPU, is the binding constraint. On rancor an 8-PE model run leaves no spare cores for
post-processing to overlap into, so the local wallclock win is small. The structure still
matters, and pays off at `OM4_025`.

## Monitoring and healing

HPCs terminate jobs for reasons that have nothing to do with the science. The workflow needs to
survive that, and it needs to be observable without watching `squeue`.

Two separate things, deliberately not fused:

- **`status`, a read-only client.** One-shot view of the experiment as a grid of cycles against
  tasks, built from the ledger, `sacct`, and `squeue`. Runs anywhere, holds no state, and
  closing it does nothing. It needs all three sources: the ledger for identity, `sacct` for
  outcome, and `squeue` for pending reasons, which exist only while a job is queued and are
  gone forever afterward. `sacct`'s own `Reason` field is empty post-mortem and
  `DerivedExitCode` is not a substitute.
- **`heal`, a manual command.** Detects failed jobs and resubmits the affected subgraph.

Keeping them apart matters: v3's driver was a curses UI in a foreground process
(`curses.wrapper`) that had to stay running for the workflow to advance, so a dropped ssh
session stalled the experiment. Viewing must never be load-bearing.

**Healing is five steps, and the third is the one people forget:**

1. identify the failed job
2. compute the transitive closure of its dependents from the regenerated graph
3. **`scancel` every id in the closure that is still queued**
4. regenerate and resubmit with fresh, state-aware edges
5. record the new ids and attempt numbers in the ledger

Step 3 is mandatory rather than tidy. Because unsatisfiable dependents pend rather than
cancel, those jobs are still holding live job ids and claimed working directories, and because
requeue poisons dependencies permanently, a successful replacement upstream does not release
them. Skipping step 3 gives two jobs per task, one of which will never run.

Note the blast radius. `forecast(n) -> da(n+1)` means one failed forecast invalidates every
cycle in flight. That is inherent to cycling, and it is another argument for `K = 1`.

Two boundaries on the closure, both of them narrower than "everything downstream":

- **It stops at what has actually been submitted.** A failure strands the `submit` task, so the
  next cycle does not exist yet. Its nodes hold no job ids, have nothing to cancel and no edges
  to rebuild, and the resubmitted `submit` will submit them in the ordinary way. Reaching into
  them means asking the submitter to build a cycle whose own roots have never been submitted,
  which is exactly the case it refuses on.
- **It resubmits whole nodes, not the failed members.** Resubmitting `--array=3` of a twenty
  member forecast would work and would be a smaller job, and it is not worth it: every member
  that already succeeded skips on its sentinel in about a second, and keeping the index sets
  identical means an `aftercorr` edge is never rebuilt between two arrays that disagree. Slurm
  reports that disagreement as a job which pends forever rather than as an error, so it is the
  failure mode to design against.

A heal fixes consequences, never causes. A deterministic fault resubmitted unchanged fails
identically, so `heal` names the failures that look genuine (`FAILED`, `TIMEOUT`,
`OUT_OF_MEMORY`) and resubmits them anyway. Refusing would be the tool claiming to understand
the science better than the person running it; saying nothing would be letting them heal three
times before reading a log.

**Manual first.** `ackbar heal <exp>` as a one-shot: list what failed, regenerate the subgraph,
resubmit. The failure classifier, per-class retry policy, resource escalation, and a recurring
self-submitting healer job are all wrappers around the same subgraph code, and each is an
afternoon's work once there are real exit states to look at. Building them before the first
overnight run has died is guessing. Slurm's own distinctions (`NODE_FAIL`, `PREEMPTED`,
`TIMEOUT`, `OUT_OF_MEMORY`, `FAILED` with an exit code) are the natural policy axis when that
time comes, with infrastructure failures retrying freely and a genuine nonzero exit never
retrying at all.

If a recurring healer job is added later it inherits every submitter hazard above, plus
duplication when two healers run at once. `--dependency=singleton` on an experiment-unique job
name is the primitive for that, with `--no-requeue`, a bounded life that stops re-arming at the
terminal cycle, and `sbatch --begin=now+15minutes` rather than sleeping inside an allocation.

## Resource accounting

Per-stage CPU and memory usage is recorded for every job, to be accumulated and analyzed
offline. v2 tracked exactly one number, the average cycle runtime, and only so it could decide
when to resubmit itself. That is not enough to size jobs or find the real bottleneck.

Slurm collects this, so the work is harvesting and attribution: `Elapsed`, `TotalCPU`,
`AveCPU`, `MaxRSS`, `MaxRSSNode`, `MaxRSSTask`, `ReqMem`, `AllocCPUS`, `NNodes`, and the exit
state.

Three things about the harvest that are not obvious:

- **`MaxRSS` and `AveCPU` exist only on step rows, never on job rows.** The natural query,
  `sacct -X` for one row per job, returns no memory data at all. Harvest without `-X` and
  reduce the maximum over step rows per job id.
- **The launcher changes what `MaxRSS` means**, so it is recorded next to the numbers. Under
  `srun`, ranks are separate steps and `MaxRSS` is per task. Under `mpiexec` there is only a
  `.batch` step whose cgroup covers every rank on the node, so the same field is a node
  aggregate. Sizing memory from one regime using numbers from the other is wrong by roughly
  ranks-per-node. Rancor should move to `srun` for this reason (see Build order).
- **Sampled peaks miss short spikes.** `JobAcctGatherFrequency` is typically tens of seconds,
  so a peak during a restart read or a diffusion operator build can fall entirely between
  samples. Read the cgroup high-water mark at task end and record that too; it is a true peak
  rather than a sample. Any future escalation should be multiplicative rather than
  observed-peak-plus-epsilon.

Harvest per cycle, into `stats/<cycle>.json`, one file per cycle. `sacct` rows are purged on a
site retention, so they have to be pulled into the experiment directory while they exist.
Attribution comes from the job name and comment, so a harvested row maps back to cycle, task,
and member without a side table. The harvest is an `afterany` leaf, because it is most valuable
exactly when something failed.

## Configuration

Explicit named layers, deep-merged in order, later wins. The experiment file declares its own
inheritance:

```yaml
inherit:
  - model/mom6sis2_om_1deg
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
- **Lists merge by an explicitly declared key.** The trap case is `observations:`, where a
  `da/letkf` layer must be able to change one observer's localization without restating all of
  them. The merge rules declare the key per list path (`observations` keyed on
  `obs space.name`), require it to be present, and support an explicit removal marker so an
  inherited element can be deleted rather than only overridden. Lists with no natural key
  (`variables`, saber blocks, filter chains) replace wholesale. This is a schema decision baked
  into all 25 ported observer configs, so it is settled before they are ported.
- **Two resolution passes, both explicit.** The first resolves at experiment creation and is
  frozen. The second resolves at job time, because cycle date, window begin and length,
  previous and next cycle, member index, forecast length, and MOM6's `current_date` and `hours`
  cannot be known once. v3 had exactly this split (`$(var)` and `{{var}}`); what it lacked was
  a named, closed set of job-time symbols. That set is defined and validated, not discovered,
  and `ackbar config symbols` prints it. Either syntax may carry a format spec after a colon,
  which is what lets one file want the same date two ways: `{{window_begin}}` is the ISO
  instant JEDI parses, `{{current_cycle:%Y%m%d%H}}` is the archive directory that has no colons
  in it.
- **Cycle *n* is computable from *n* alone.** The analysis time is `cycle.start + (n-1) *
  cycle.length`, the window is centred on it and one cycle long so that consecutive windows
  tile without gap or overlap, and cycle 0 is where experiment setup materializes the offline
  initial condition. Calendar durations (months, years) are rejected rather than approximated,
  because `start + n * P1M` is not a function of `n`, and cycle 40's date would then depend on
  the path taken to reach it rather than on the number. That would break `heal`, which
  regenerates a subgraph without replaying what came before it.
- **Seeds derive from experiment, cycle and member.** Anything else means a healed member
  carries a different perturbation than the original run and nothing records it. `hash()` in
  particular is salted per process and would do exactly that.
- **Provenance by replay, not by wrapped values.** The ordered layer files are copied verbatim
  into `cfg/` next to the resolved config, and `ackbar config why <dotted.key>` replays the
  merge with the layer list truncated at each level, reporting the last truncation that changed
  the value. Same answer as threading an origin through every scalar, on plain dicts, with
  nothing in the hot path. Neither v2 nor v3 can answer "why is this parameter that" at all.
- **Resolve once, write to the experiment directory, run only from that.** v3 got this right
  (`cfg/exp_config.yaml`). It is what makes an experiment reproducible after the repo moves
  underneath it. The frozen config is never rewritten; anything that varies per attempt, such
  as escalated resources, is an append-only record beside it.
- **Layers can enable and disable tasks, not just set values.** OSSE adds a truth run and an
  obs generation stage; long forecasts add tasks on their own cadence. The task graph is
  therefore a function of the fully merged config and is computed after merge.
- **Per-task resources are a named, rewritable surface**, declared in the domain layer, since
  domain is what actually drives the difference between `OM_1deg` and `OM4_025`. `--time`,
  `--mem`, `--ntasks`, and PE layout live there. The site layer supplies only partition,
  account, launcher, roots, and limits. Resources are not literals inside emitted job scripts.

YAML is generated from data structures. v2's three layers of `sed` (token replacement,
`!IF_APP_*` line prefixes, and block splicing with hardcoded indentation) are not carried
forward in any form.

**ackbar and JEDI do not read the same YAML.** PyYAML implements YAML 1.1, whose float
resolver requires a decimal point and an explicitly signed exponent. eckit and yaml-cpp
implement the YAML 1.2 core schema, which requires neither. So `halo size: 500e3`, written
exactly that way in `soca_letkf.yaml`, is the string `'500e3'` to ackbar and the number 500000
to JEDI, and so are `5.0e5` and `1e-3`. The re-emitted file usually still looks right, because
PyYAML writes these back unquoted; the damage is internal, where ackbar's own schema rejects a
string it wanted to be a number and the error names a value that looks fine. The ported
observer configs are full of thresholds written this way, so validation checks for it
explicitly rather than leaving it to be discovered per file.

## Configuration validation

A cycle is many jobs, and a bad value in one of them should not be discovered by a job that
starts eight hours from now. `ackbar validate <exp>` runs before anything is submitted, and
submission runs it implicitly:

1. the merged config against ackbar's own schema
2. generation of every job's YAML for every cycle, checked as well-formed
3. every referenced input path exists and is readable
4. every executable exists and is runnable, and matches the recorded provenance
5. projected disk usage against free space, and projected job count against queue limits
6. the graph is acyclic, every member-level array shares the canonical index set, and every
   enabled task has declared resources

Step 2 is the one that pays. Generating the whole experiment's YAML up front is cheap, it
catches missing observation files and stale paths at the only moment when fixing them is free,
and it is a strong test that graph generation really is deterministic and side effect free.

Two properties of the report matter as much as the checks. **A step that did not run says so**,
rather than being absorbed into a pass: `--offline` skips the three steps that need the
filesystem or the site's queue limits, and a config that fails step 1 stops the rest, since
everything below would be reading values it has just been told are the wrong shape. And **an
input is distinguished from an output by root**: anything under the scratch or output root is
something this experiment is about to create, and everything else absolute is something it
consumes and cannot make.

What it cannot catch is what JEDI itself will reject, since ackbar's schema describes ackbar's
config rather than OOPS's.

**There is no seventh step, because JEDI no longer offers a reliable parse-and-exit.** Such a
path existed at one point but not at the application level any more; some components validate
their own configuration and others do not, and a check that covers an unpredictable subset is
worse than none, because it reports success for configurations that will still fail. So a
malformed JEDI config is discovered when the executable runs, and nowhere earlier.

Two things carry the weight that step would have carried, and both are ackbar's own:

- **Step 3 does most of the work in practice.** Malformed JEDI YAML is rare, because it is
  generated from data structures rather than templated. Missing or misnamed *files* are the
  common failure, and that is entirely checkable up front.
- **The cycle throttle bounds the blast radius.** With a throttle of 1, a config error surfaces
  in cycle 1 rather than after fifty cycles have been queued behind it.

The residual risk is a JEDI-valid-looking config that the executable rejects on the first
cycle. That is a one-cycle cost, paid once per experiment definition, and it is the reason
milestone ordering brings each new solver up on a short run before a long one.

## Provenance

Configuration provenance answers "why is this value that". Code provenance answers "what
produced this result", and without it the project's premise fails: two experiments in a
comparison can run against different binaries with nothing recording it. This matters
concretely here, because answer-changing MOM6 defaults moved between the pins ackbar might use
and because build directories get rebuilt in place.

Recorded at experiment creation into `cfg/`, and re-checked on resume with a loud warning on
drift:

- `git describe` for ackbar itself and for every submodule under `pkg/`
- the sha256 and mtime of every executable the experiment will invoke
- the spack-stack environment identifier
- the resolved site layer

## Model and DA modes

**The model is a configuration axis**, like the solver. Three values:

| Value | Meaning |
|---|---|
| `mom6sis2` | the real forecast model |
| `persistence` | the forecast is the analysis, carried forward unchanged |
| `stub` | a fake model: burns a configured number of PEs and wallclock, writes correctly sized output, does no science |

`persistence` is a real scientific configuration, not a placeholder. It is the baseline every
DA system is measured against, and it exercises the entire cycling loop (analysis, writeback,
background handoff, observation evaluation) at zero model cost, which makes it the cheapest
honest end-to-end test of the DA path.

`stub` is what makes the *workflow* testable (see Build order), and it is not a scientific
configuration at all. The two are distinct on purpose: `persistence` produces a state you can
score, `stub` produces bytes of the right shape.

The stub also carries **deterministic fault injection**, under `model.stub.fail`. Each entry
selects jobs by `<cycle>.<task>[.<member>]`, every field a shell glob, and names the way they
should fail: nonzero exit, run past the time limit, blow the memory request, exit 0 having
written nothing, requeue mid-task. Selecting *jobs* rather than a probability is the whole
point: the same job fails the same way on a rerun, so a failure is reproducible configuration
rather than an afternoon nobody can repeat, and a healed attempt reproduces the failure it is
meant to fix rather than passing by luck.

**A MOM6-SIS2 forecast is a run directory and nothing else.** The model is configured entirely
by the contents of the directory it starts in, so the model layer names a stock MOM6-examples
case and the forecast task links every file in it through untouched except the handful ACKBAR
owns: `input.nml`, `MOM_layout`, `SIS_layout`, `diag_table`. That keeps the case's physics
coming from the model submodule instead of from a fork of it in this repository, and it makes
"what did ACKBAR change" a four-item list rather than a diff.

Three of those four are worth saying why:

- **The layout comes from the domain layer**, never from the case. MOM6-examples ships
  `LAYOUT = 12,10` for MOM and `32,18` for SIS with a comment saying not to use them, and a
  layout whose product is not the task's `ntasks` fails inside FMS with a message about domain
  decomposition rather than about configuration. ACKBAR checks the product against `ntasks`
  before a job is even submitted.
- **The `diag_table` is chosen by what the forecast is for**, which is the one thing that
  genuinely differs between the two forecast tasks. A cycling forecast's product is the restart
  set the next cycle reads, and it writes no history at all; writing it every cycle of an
  ensemble is how a free run fills a disk. An extended forecast exists to be scored and writes
  intervals. Same executable, same code path, different file, which is what makes this
  configuration rather than a branch.
- **`input.nml` is patched, not regenerated.** ACKBAR sets the run length and the fallback date
  in `coupler_nml` and leaves the couple of dozen groups of model physics alone.

v2 had a seven-way case statement over `DA_MODE`. Most of those modes are the same code with
different covariance or window settings. The real axes:

| Axis | Values |
|---|---|
| model | `mom6sis2`, `persistence`, `stub` |
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

## Domains

Three classes must be supported, not two:

- **global 1 degree** (`OM_1deg`), the development and test domain
- **global quarter degree** (`OM4_025`), the production domain
- **regional domains at various resolutions**

**Domain is a first-class configuration axis**, on the same footing as DA mode. It is not a
flag bolted onto a global system, which is exactly what v2 did: `DA_REGIONAL_ENABLED` is
described in its own config file as "triggers the regional hack". That implementation is not
carried forward. The capability is.

A domain layer names the grid, bathymetry, resolution, PE layout, per-task resources, and open
boundary setup. The static and initial-condition stages key off it.

Regional costs more than a different grid file. What it actually pulls in:

- **Symmetric memory.** MOM6 regional configurations use symmetric memory, and SOCA links its
  own MOM6 as a library from NOAA-EMC. If the two are built inconsistently the restart array
  shapes disagree. v2 papered over this with `soca_dynsym2dyn.sh`, converting restarts between
  layouts, while its own config file carried the note "TODO investigate building soca with
  MOM6 regional (symmetric memory)". That TODO is the real fix and it is a build-level
  decision, not a workflow one. It has to be settled before regional works at all.
- **Open boundary conditions.** Regional runs need boundary forcing from a parent solution, so
  they add both a per-cycle input and an offline stage that global configurations do not have.
  Note that MOM6's OBC code was substantially overhauled between the 2024 and 2026 pins (see
  `docs/model-build.md`), so any OBC configuration ported from a v2-era regional setup should
  be assumed stale until checked.
- **Grid edge masking.** The analysis must not write into the boundary and sponge zone. v2
  zeroed the outer ring of `mask2d` after gridgen (`soca_domom6_action.py mask-grid-edges`).
- **Observation culling to the domain.** v2 did this per cycle with `soca_domaincheck.py` and
  flagged it in its own source as a temporary fix that "new workflow should address in a more
  effective manner". Cull at archive-build time instead, so the per-cycle path stays identical
  across domains.
- **Domain-specific observation configuration.** v2 kept a parallel tree under
  `configs/soca/regional/hat10/obs/` where several observers genuinely differ from their
  global counterparts (for example ADT variants referenced to a different geoid). So observer
  configuration layers by domain, which the configuration design already supports.
- **A constraint on the writeback decision.** v2 was forced onto the python direct-write path
  for regional, because its restarts were dynamic-symmetric and the model-based checkpoint
  could not handle them. Whichever approach the writeback spike settles on has to work for
  regional too, not just global.

Regional adds tasks to the graph and a build-level constraint. It is not merely the same code
path with a different grid file, and the README should not claim otherwise.

## Observations

**No downloading inside the cycle.** v2 could download and convert observations mid-cycle
(`OBS_GEN_ENABLED`, `OBS_*_DWNLD`, `OBS_*_CNVRT`, the `scripts/obs/*.sh` downloader set)
because it was built with realtime running in mind. These are retrospective experiments. The
in-cycle obs step reduces to: find the file covering this window in the archive, link it, and
drop observers whose input file is absent (unless marked required, per v3's `_required`).

Because that makes the observation set vary silently, **the realized observer list is written
per cycle** and diffed by the comparison tooling. Two experiments differing in which observers
actually ran is the difference that most affects a comparison, and it must not be invisible.

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
| static | domain | `soca_gridspec.nc`, horizontal correlation scales, localization scales |
| initial condition | domain, source, date | a spun-up restart set |
| observations | period (and domain, if culled) | archive of ioda files, real or OSSE-generated |
| forcing | period | atmospheric forcing archive |
| boundary forcing | domain, period | open boundary conditions from a parent solution (regional only) |

Static is keyed on **domain, not experiment**, so one `static/om_1deg/` is shared read-only
across every experiment on that domain. That is what makes experiments comparable by
construction.

They live under `$ACKBAR_STATIC_ROOT`, one directory per stage, keyed as the table says and in
that order: `ic/<domain>/<source>/<YYYYMMDDThh>` is the initial condition stage, and
`static/<domain>` is the static stage.

`<source>` names the **producer**, since the domain and the date are already in the path around
it: `woa13-smoke`, `jra55-spinup`, `osse-truth-<experiment>`. It is a directory level rather than
part of the date's name because one source normally yields many dates. A spinup gets snapshotted
at several states, and an OSSE truth run is promoted at whichever dates the experiments reading
it need. The level also carries the stage's `README`, which is where provenance belongs: "12 hour
cold start" and "10 year spinup" are the same five files, and only the `README` and the slug say
which one this is. Name the slug so it warns, because an experiment selects a state by writing
this path out and that is the last point anyone looks at it.

An experiment names a product by path, through `$(static_root)`, rather than composing that path
from its own domain and start date. Composition would tie the layout to the resolver and make a
hand-placed input unusable, and the question an experiment is asking is which state, not which
state happens to sit at its start date.

**Spinup is a separate job script**, not part of any experiment. It either cold starts from
WOA13 and integrates with realistic atmospheric forcing, or converts an external state (GFS,
ORA5, and similar). Experiments assume an initial condition already exists.

**Promotion is an explicit step.** An OSSE truth run is an ordinary experiment, and its output
becomes another experiment's read-only observation archive. That does not violate "experiments
never generate their own inputs", but only because promotion is a named stage that freezes the
output, versions it, and records which experiment and which code produced it. Without that step
the principle quietly erodes.

**Vertical B is the one genuine exception.** Vertical diffusion scales track the mixed layer
and depend on the background, so they cannot be precomputed offline. Calibration is an explicit
per-cycle task in the graph (v2 ran it every cycle too, but as a side effect of a prep step
rather than a first-class task). Horizontal and localization scales stay offline in `static/`.

## Task graph

Per cycle, roughly:

```
  forcing, obs staging  ->  B.vt  ->  da  ->  writeback  ->  forecast (ctrl)
                                                         ->  forecast (ens array)
                                                         ->  forecast_ext (leaf, own cadence)
  da, forecast  ->  post.obs, post.state, verify, stats  (all leaves, afterany)
  forecast(n)   ->  submit(n+1)
  cleanup       ->  artifact-existence gated, not dependency gated
```

Cross-cycle: `forecast(n) -> da(n+1)`, and the submitter, and nothing else.

**Cycle 1 is not special.** Experiment setup materializes the offline initial condition into
the experiment's own cycle-0 forecast output location, so `da(1)` resolves its background by
exactly the same rule as `da(50)`. Without that one step the asymmetry the design claims to
have removed is merely relocated, and `first_cycle_only` machinery comes back.

Cross-cycle is `forecast(n) -> da(n+1)`, and also `forecast(n) -> b.vt(n+1)`, because vertical
B calibrates from the background. With no analysis configured the restart handoff is
`forecast(n) -> forecast(n+1)` directly.

**Two tasks are roots of their cycle and nothing else is.** `stage.obs` reads the observation
archive, which is an offline stage that exists before the experiment starts, so nothing in the
experiment gates it. `cleanup` is gated on artifact existence rather than on job state, for the
reasons in Task completion. Any other task without an incoming edge is a job that runs before
its input exists, so the generator refuses to produce one.

Ensemble shape: per-member forecasts are a job array over the canonical index set; LETKF is a
single MPI job consuming all members; recentering and per-member writeback are arrays. Every
one of these is a serial `for` loop in v2. The control is `mem000` within the same indexing,
not a separate concept.

**The canonical index set is `0..size`**, the control plus the ensemble, or `1..size` when
`ensemble.control` is false. An experiment with no ensemble block still has one, `(0,)`, so
`mem000` is member 0 of a set of one rather than a different kind of thing. A member-level task
is submitted as an array even when the set has one element: emitting that one as a scalar job
would look tidier and would silently invalidate every `aftercorr` edge into it, which Slurm
reports as a job that pends forever rather than as an error.

**A missing or diverged member is the normal case, not the exception.** Arrays make partial
success routine, and the solver has to have a stated policy: fail the cycle, run degraded with
the members that succeeded, or replace the missing member from the mean. These have different
graph shapes and different science, and the choice is per experiment. Related, v2 clamped
temperature to `[-1.9, 33]` and salinity to `[0.1, 38]` inside its checkpoint; that divergence
guard needs a home in whatever replaces that code path.

**Writeback is one node with one contract:** produce the restart set the next forecast reads.
Direct restart write is the first implementation. IAU is then an alternate implementation
behind the same edge rather than a rewiring of the graph, which is a far smaller commitment
than carrying two graph shapes.

Long forecasts are leaves on their own cadence, independent of cycle length. A 7 day forecast
off every 24 hour cycle is seven times the model cost of the cycling itself, so cadence is a
setting, not the cycle period. They need a different `diag_table` from cycling forecasts
(interval diagnostics for scoring, rather than restarts), which is a config layering case:
model configuration varies by forecast purpose. Which members to extend is a setting,
defaulting to `mem000`. Note that a member subset cannot use `aftercorr` against the full
member array, since that requires matching index ranges.

## Build order

See [`build-order.md`](build-order.md), which carries the implementation phases, the test tier
each is verified at, and the spikes that must land before particular phases.

The shape of it: the workflow machinery (configuration, graph, submission, healing) is built
and finished against a stub model before any real science runs through it, because that is the
only way the ensemble parallelism this project exists for can be demonstrated on 8 cores, and
the only cheap way to exercise the failure paths. The science milestones then follow in the
order that de-risks them fastest: free run, hofx, variational, LETKF, hybrid, 4D, EDA, and
regional last.

## Open

- **IAU.** Direct restart write is first, behind the writeback contract above. Investigate what
  SOCA offers now before writing anything; if nothing suitable exists, a python direct write
  based on v2's `soca_domom6_action.py` is the fallback, and v2's `socaincr2mom6` is the
  starting point for IAU. Resolve by spike test during implementation, not on paper, and run
  the spike before milestone 3 rather than inside it.
- **MOM6 back-compat parameter pins.** Whether SOCA configs keep `EQN_OF_STATE = "WRIGHT"`
  and friends or drop them for corrected physics. See `docs/model-build.md`. Coupled to
  spinup: dropping them invalidates any initial condition produced under the old physics, so
  decide before generating one.
- **Packaging.** Python package versus scripts. Layered config with schema validation and
  generated task graphs effectively forces python for the workflow itself; the question is
  really how much lives in the package versus in job scripts it emits.
- **Ensemble geometry on rancor.** 8 cores total against an 8-PE model run means real-model
  member parallelism requires fewer PEs per member or oversubscription. The stub model removes
  this from the critical path for testing the workflow, but a small real configuration running
  four concurrent members at 2 PEs is still worth having as a correctness check.
- **Divergence policy** for a missing or bad member, per experiment. See Task graph.
- **Whether there is a control member, per DA method.** LETKF does not use one. Hybrid EnVar
  has a deterministic member, which is a different thing wearing the same index. The graph
  carries this as `ensemble.control`, defaulting to true, so both shapes already generate; what
  is undecided is which methods set it, and that is settled when each method is implemented
  rather than guessed now.
- **Where the analysis time sits in the window.** Centred is the current assumption, and it is
  what soca-science did, but it is not the only valid position: 4DVar requires the window to
  begin at the analysis time, and 4DEnVar allows either. So window placement becomes a
  configured property of the solver when the 4D window work lands, not a constant.
- **`da` is not necessarily one node.** Hybrid EnVar needs both the EnVar analysis and whatever
  maintains the ensemble its covariance is drawn from (a LETKF, or another perturbation model),
  and those are two distinct applications with distinct configs, resources, and member
  cardinality inside the same cycle. The task table today gives `da` one name and one
  executable chosen by solver. When hybrid is implemented this becomes either two tasks
  (`da.var` and `da.ens`) or one task parameterized by instance, and the graph edges, the
  resource table, and the per-task config paths all key off whichever is chosen. Decided in the
  hybrid phase.
- **Which application calibrates vertical B per cycle**, if a separate one does at all. The
  task exists in the graph with no executable named. `soca_sqrtvertloc.x` is not it: that is
  vertical *localization* for an ensemble covariance, a different quantity from the vertical
  correlation scales of the static B. Settled in the variational phase, where the DA task
  itself is built and it becomes clear whether this is a task or a saber block inside one.

## Not carried forward

From v2: in-cycle observation downloading, R2D2, the regional *implementation* (the capability
is first class here, see Domains above), the `MACHINE` and `MODEL_SCRIPT` indirection layers,
`sed`-templated YAML, environment variables as the inter-step interface, generate-if-missing
inputs, the 3D-to-4D symlink farm, age-based cleanup, the `ctrl` versus `ens` split, and every
serial member loop.

From v3: rocoto, implicit scope-based config resolution, the foreground curses driver, and the
rule that a task cannot resume if its working directory exists.

Also not carried forward, from an earlier draft of this document: an adapter presenting a
Slurm-shaped interface over a serial runner. It reintroduced the abstraction this design
rejects, and it could not have honored `aftercorr` semantics anyway.
