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
  stochastic_physics/  submodule, NOAA-PSL/stochastic_physics
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

This is worth doing early rather than late, because the leaks accumulate quietly, and they
are never wrong on the machine they were written on. A build script sourcing a personal
environment file, a dataset root reached through a symlink inside a submodule, a scratch path
in a test, a CMake generator exported by a shell profile: each is correct on rancor and wrong
everywhere else. `site/` is where all of them belong.

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

Inside the output root there are two tiers, and the split *is* the retention policy. The top
level holds what the experiment is for. `run/` holds what it took to get there, and is the only
place `cleanup` ever deletes from.

```
<scratch_root>/<exp>/<date>/<task>[.<member>]/
    working directory: model inputs, INPUT/, logs, everything transient
    removed by the task on success, retained on failure

<output_root>/<exp>/
    HALT          present while paused; every submitter checks it
    cfg/          the finished config and the provenance record
    cfg/soca/     the SOCA document templates, frozen from the checkout
    cfg/<date>/<task>.sh    the emitted batch script, one per node

    ana/<date>/mem###.nc    the analysis, compressed                    kept
    ana/<date>/members.json which members the cycle had, and the policy  kept
    bkg/<date>/mem###.nc    the background, compressed                  kept
    obs_in/<date>/<platform>.nc4   what the observers were handed      kept
    obs_out/<date>/         ioda output, ombg and oman                  kept
    obs_out/<date>/summary.json    departure statistics for the cycle
    obs_out/<date>/observers.json  which observers the cycle had, and why not

    fcst/<init>/F###/mem###.nc     the long forecast at that lead       kept
    fcst/<init>/F012/mem###.nc     -> ../../../bkg/<init+length>/mem###.nc
    fcst/<init>/obs/F###/mem###/   its departures, one set per section  kept

    run/ledger.jsonl        append-only submission records
    run/submitted.<date>    the per-cycle submit marker
    run/<date>/log/         job stdout and stderr, and kept model traces  kept
    run/<date>/done/<task>[.mem###].json   sentinels, written last       kept
    run/<date>/stats.json   the resource harvest for the cycle           kept
    run/<date>/rst/mem###/  what this cycle's forecast wrote           reaped
    run/<date>/ana/mem###/  the analysed restart set the forecast reads
    run/<date>/ana/mem###/analysis/  what the analysis application wrote
    run/<date>/slot/mem###/<valid time>/  sub-window states, one directory each
    run/<date>/fcst/mem###/F###/          the long forecast's raw trajectory
```

Rules that fall out of this:

- **`ana/` and `bkg/` are the products and are compressed; the restart sets are not.** A MOM6
  restart carries every prognostic field the model has, and the forecast needs all of them, so
  the set under `run/<date>/ana/` cannot be reduced. What a comparison reads is a handful of
  fields, and `post.state` writes exactly those to `ana/<date>/mem###.nc` at about a fortieth
  of the size. That is what makes the retention policy possible: the thing worth keeping
  forever is small, and the thing that is large is regenerable from the pinned restarts.
  soca-science drew the same line and kept its analysed restart set in scratch entirely;
  ACKBAR cannot, because `writeback` and `forecast` are separate jobs and a task's scratch is
  deleted when it succeeds.
- **A directory is named by a date, and owned by the cycle that writes it.** `run/<T>/` is what
  the cycle whose analysis time is `T` did. The one place this needs care is `run/<T>/rst/`,
  which holds what *that* cycle's forecast wrote and is therefore valid at the next analysis
  time; naming it for its own valid time would put one cycle's output under another cycle's
  directory, and cleanup would stop being a date comparison. Cycle numbers survive in the
  interface, where a scheduler dependency and a heal are computed, and the offline initial
  condition goes in cycle 0's `rst/` as the output of a forecast that never ran.
- **One date format, to the second.** ISO 8601 basic format, which sorts chronologically as a
  string and carries no colons. Every duration an experiment may state is a whole number of
  hours, enforced by `graph.build._check_hours`, so the minutes and seconds are always zero;
  they are carried anyway because a date format is the one thing here that cannot change
  without invalidating every experiment already on disk.
- **The long forecast is the one thing keyed by lead rather than by valid time.** Everywhere
  else a directory is named for when its contents are valid, because the consumer knows the
  time it wants. Under `fcst/` the consumer wants the opposite: forecast skill is read against
  lead, grouped across initializations. Both parts of the key are needed, since two forecasts
  started five days apart pass through the same valid time and are not the same state. Leads
  are `F###` in hours, which is why the whole-hours rule above is enforced rather than assumed.
- **A background is a one-cycle forecast, so `bkg/` supplies the first lead.** Both forecasts
  start from the same state and MOM6 is deterministic, so integrating five days does not change
  the state at twelve hours. `fcst/<init>/F012/mem###.nc` is a *relative symlink* into `bkg/`
  rather than a second reduction of the same restart set, which makes that equality structural:
  two independent reductions can quietly disagree over rounding or a `FIELDS` change made on
  one path only, and a link cannot. Keeping `bkg/` where it is also keeps `ana/<T>` and
  `bkg/<T>` as the pair at one valid time, which is what makes an increment a subtraction
  between two files of the same name.
- **The long forecast's departures never land in `obs_out/`.** The observer layer's
  `obsdataout` is `obs_out/<T>/...` rendered at the cycle being evaluated, so a five day
  forecast that left it alone would overwrite the cycling departures of every cycle it reaches,
  and the symptom would be an O-B statistic quietly describing a forecast. They go to
  `fcst/<init>/obs/F###/mem###/`, keyed by the end of the section evaluated: today that is one
  section covering the whole forecast, and a larger domain that has to evaluate a day at a time
  needs no path change.
- **And its inputs never come out of `obs_in/`.** `obs_in/<T>` is written by cycle T's own
  `stage.obs`, and every window the long forecast scores belongs to a cycle that has not run:
  cycle 1's F048 window is staged by cycle 3. So `hofx.ext` joins the bins for its own lead
  windows, into its own scratch, which is what the `forecast.ext -> hofx.ext` edge has always
  said it does and is what makes the task's inputs a function of that task alone. The repeated
  work is a few seconds per window against a five day integration, and the alternative, a shared
  staging area any task fills if it is absent, buys nothing back: it races, it is
  skip-if-exists, and the file it would share is the wrong file anyway. The cycling join is
  deliberately a little wider than its window, because ioda makes the cut against the window the
  application was given; `hofx.ext` gives one application a window spanning the whole forecast
  and one observer per cycle inside it, so ioda separates nothing and its files have to arrive
  already cut, or the bin overlap between two adjacent lead windows is counted twice.
- **A lead window with nothing in it is a dropped observer, not a refused cycle.** `realize`
  refuses a *cycle* in which no observer has anything, because that cycle is indistinguishable
  from a healthy one downstream. A lead window is not that kind of object: the cost is one score
  at one lead, every experiment in a comparison reads the same archive so the same lead is
  missing from all of them at once, and the absence is visible as a departure file that is not
  there. `adt_c2` genuinely has no file on the days CryoSat-2's repeat misses the domain, and
  those days are lead windows for five initializations each.
- **A member directory under `ana/` is a restart set and nothing else.** Writeback fills it by
  copying every file of the background's, `model: persistence` fills the next cycle's by
  copying every file of this one, and the forecast links all of them into `INPUT/`. So what the
  analysis application wrote goes in a subdirectory: a state file loose among them is inert to
  the model and then carried forward by every cycle after it, one more each time.
- **What a cycle actually had goes next to what it produced.** Which observers were staged and
  which ensemble members arrived are properties of the cycle rather than of the configuration,
  they vary from cycle to cycle without anything else saying so, and two experiments that
  differ in either are not comparable. So each is a file, written whether or not anything was
  missing.
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

  **That variable list has no `hocn`, and whether that matters depends on the solver.**
  The model is Boussinesq, so it conserves volume and a column's sea surface height is
  `sum(h) - D`; `ave_ssh` is a diagnostic MOM6 recomputes, so writing it does nothing.
  For a *variational* analysis with `unbalanced ssh` at zero that costs nothing, because
  the SSH increment is steric by construction and the temperature and salinity increment
  already carries it, with the model's own adjustment producing the height. For an
  *ensemble filter* it costs a real increment: there is no balance operator, the filter
  analyses `h` directly, and measured on `osse25-4dletkf` that analysis differs from the
  background by up to 4.27 m and none of it reaches the restart. `docs/osse.md` has the
  split and the fix.
- **Cleanup.** Retention keys off **artifact existence**, not job state: a cycle's inputs may be
  removed once every declared consumer's output exists. Keying off job state instead means a
  retried cleanup evaluates a regenerated subgraph with new job ids, concludes the old
  consumers are gone, and deletes restarts that a resubmitted consumer is about to read. The
  artifact rule also dissolves the "cleanup must be dependency-aware" problem outright: no
  dependency list, no race with healing, no age arithmetic. **The horizon it proves is the most
  recent complete cycle, not the previous one**, and that distinction is the whole of whether
  the rule works. `submit` is released by the cycling forecast, while `forecast.ext`, `hofx.ext`
  and `post.fcst` run on past it as leaves, so cycle *n*'s cleanup starts while cycle *n-1*'s
  long forecast is still integrating: not occasionally, but every cycle of every experiment with
  an extended forecast. Fixed at *n-1* the proof never holds, the sweep that is meant to collect
  the arrears has no pass that reaches it, and nothing says so louder than a log line. Measured
  on `osse25-4dletkf`: twenty one cycles, twenty one refusals, 5.9 GB per cycle held for the
  life of the run against the 12 GB it should have been. Walking back costs one extra cycle of
  state and is safe under the same argument the horizon rests on, that cycle *n* reads cycle
  *n-1* and nothing older.
- **The stats harvest.** The task most likely to run twice, and its job is to write rows.
  `run/<date>/stats.json`, one file per cycle, never appended to. Accumulate at analysis time.
- **`post.obs` statistics and `post.state` compression.** Same append hazard. Compression is
  lossy (`ncks -7 -L 4 --ppc default=.2`), so it must never run in place, and the source must
  survive until the destination is committed.
- **Anything with a random seed.** Perturbation-based ensemble sources are not reproducible
  across a rerun unless the seed derives deterministically from experiment, cycle, and member.
  v2 had a `__SEED__` token for exactly this. Settled for stochastic physics in
  `ackbar/stochastic.py`, whose seed is a pure function of `ensemble.stochastic.seed` (frozen
  into `cfg/` at create time), the member and the cycle, and of nothing else, so a healed
  forecast integrates the trajectory the failed attempt was producing.
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

Harvest per cycle, into `run/<date>/stats.json`, one file per cycle. `sacct` rows are purged on a
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
- **A layer may inherit too, and it means something narrower than an experiment's `inherit:`.**
  An experiment's list assembles a stack; a layer's says *what it is a kind of*. `obs/adt_j2`
  inherits `obs/adt` because Jason-2 is an altimeter, and the experiment goes on listing the
  platforms it flies. The tree flattens depth first with parents before children, and the
  flattened list is what `provenance.json` records, so the account of what contributed still
  names every file in the order it contributed. A repeat in an experiment's own list stays
  an error, because it is hand-written and precedence-changing; a layer reached twice through
  the tree is deduped in silence, because four altimeters inheriting one body is the point.
- **An observer may inherit a shared body.** The keyed merge below reaches an observer only by
  naming it, so a layer holding the common half of a platform family has nothing to merge into.
  Such a layer declares it under `observation bodies:` by name, and a platform writes
  `$inherit: adt` inside its observer. The body merges *under* the observer, so the platform
  wins, and the key is gone before UFO sees anything. Deliberately the same word at both
  scopes: a list of layer names at the top of a file, one body name inside an observer.
- **Lists merge by an explicitly declared key.** The trap case is `observations:`, where a
  `da/letkf` layer must be able to change one observer's localization without restating all of
  them. The merge rules declare the key per list path (`observations` keyed on
  `obs space.name`), require it to be present, and support an explicit removal marker so an
  inherited element can be deleted rather than only overridden. Lists with no natural key
  (`variables`, saber blocks, filter chains) replace wholesale. This is a schema decision baked
  into all 25 ported observer configs, so it is settled before they are ported.
- **Three resolution passes, all explicit, and the syntax names who fills it.** `$(lowercase)`
  resolves at experiment creation and is frozen. `{{lowercase}}` resolves at job time, because
  cycle date, window begin and length, previous and next cycle, member index, forecast length,
  and MOM6's `current_date` and `hours` cannot be known once. v3 had exactly this split; what
  it lacked was a named, closed set of job-time symbols. That set is defined and validated, not
  discovered, and `ackbar config symbols` prints it. Either syntax may carry a format spec
  after a colon, which is what lets one file want the same date two ways: `{{window_begin}}` is
  the ISO instant JEDI parses, `{{current_cycle:%Y%m%d%H}}` is the archive directory that has
  no colons in it.

  `$(UPPERCASE)` is the third and is *task* time: a slot in a `config/soca/` document template,
  filled by the one function in `ackbar/soca.py` that builds that document, from values only it
  can compute. It shares the experiment-time sigil deliberately. A template is not a layer and
  must never be merged as one, and if one ever is, the experiment-time pass meets
  `$(OBSERVERS)` and refuses it as an unknown symbol rather than resolving it to nothing.

  **The job-time pass runs over two things and not the whole config.** `observations.py`
  renders each observer entry, and `soca.py` renders a document template before filling its
  slots. Every other subtree reaches a job with its `{{...}}` unrendered, because the values a
  job needs are computed in the job body from the cycle it was given rather than substituted
  into config. That is the narrower promise and it is the one that is kept. It matters because
  `ackbar validate` renders the *whole* config to check it, so a `{{...}}` written anywhere
  else validates clean and arrives at the job literally, which looks like a path that does not
  exist rather than like a substitution bug. Widening the pass is the fix if something outside
  those two ever needs a job-time value; until then this paragraph is the specification.

- **A document's shape is a file; its values are Python.** Each SOCA application has a template
  under `config/soca/`, and `ackbar/soca.py` fills its slots. Splitting them this way is only
  safe because of one narrow rule: **a template holds a value only when nothing in Python reads
  it.** The moment a value is read on both sides it becomes a slot, because two spellings of a
  filename field is a `writeback` that opens a name nothing wrote and reports an analysis that
  produced nothing. `exp`, `type` and `datadir` are the three with teeth. Filling is strict in
  both directions: an unfilled slot is an error, and so is a computed value the template does
  not use, which is a template that has quietly dropped a block. `tests/test_templates.py` pins
  the slot set of each template against what its builder supplies, because run time is too late
  for `recenter`, whose first execution is after the ensemble filter and the deterministic
  analysis have both already run.
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
- **Provenance by replay, not by wrapped values.** `ackbar config why <dotted.key>` replays the
  merge with the layer list truncated at each level, reporting the last truncation that changed
  the value. Same answer as threading an origin through every scalar, on plain dicts, with
  nothing in the hot path. Neither v2 nor v3 can answer "why is this parameter that" at all.
  It replays over the layer tree in the checkout; `provenance.json` records which layers an
  experiment used, in order, and the commit they were read at, which is what pins the replay to
  the right text. The layer files were once copied into `cfg/` as well, and are not any more:
  nothing read them, and a second pre-substitution account of the config beside the finished one
  is a thing to read by mistake.
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
4. every executable exists and is runnable
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

Recorded at experiment creation into `cfg/`, by `create._provenance`:

- the experiment name, and when it was created
- `ackbar_root`, and `ackbar_commit`: `git rev-parse HEAD` with a `-dirty` suffix when the
  working tree is not clean
- the site name, and the names of the layers the config was merged from

A clean `ackbar_commit` pins every submodule under `pkg/` through its gitlink, so it does
answer "which JEDI" for a clean tree. When the tree is dirty, `-dirty` is the whole of the
signal: it does not say what differs, and a rebuilt build directory under an unchanged commit
is invisible to it.

What is *not* recorded, and is owed before two experiments can be compared with confidence:
per-submodule commits, the sha256 and mtime of each executable the experiment invokes, the
spack-stack environment identifier, and the resolved site layer rather than its name. Nothing
re-checks provenance on resume; `ackbar resume` clears the halt flag and resubmits.

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
by the contents of the directory it starts in, so the model layer names a base directory of stock
MOM6-SIS2 text (`mom6_base_dir`) and the forecast task links every file in it through untouched
except the handful ACKBAR owns: it writes `input.nml` and `diag_table`, and it links
`MOM_override` and `SIS_override` from its own copies instead of the base's. That keeps the
physics coming from the model submodule, or from one reviewed directory shared by a family of
domains, instead of from a per-experiment fork of it, and it makes "what did ACKBAR change" a
four-item list rather than a diff.

The exception is that a stock directory also contains files the model *writes*. MOM6 and SIS2 dump
the parameter set they actually ran with, and MOM6-examples commits those dumps back in as
documentation, so they are outputs sitting in an input directory. Linking one means the model
opens a symlink for writing and edits the shared directory in place, under every other member,
cycle and experiment at once. They are skipped, and the model writes its own into the run
directory. `GENERATED` in `mom6sis2.py` is the list. The general form of the rule: link an input,
never a name the model is going to open for writing, and the base directory does not distinguish
them.

Three of those four are worth saying why:

- **The overrides are ACKBAR's, never the base's**, and ACKBAR puts them in `parameter_filename`
  itself so that a base directory which never mentions them still reads them. They hold what makes
  a domain its own resolution and the bug-retention flags MOM6-examples keeps on to protect its
  regression answers, which are wrong to keep in a testbed whose entire output is an error
  statistic. There is deliberately no `LAYOUT`: MOM6 decomposes for itself, and a layout in
  configuration would be a second home for the PE count. See `docs/domains.md`.
- **The `diag_table` is chosen by what the forecast is for**, which is the one thing that
  genuinely differs between the two forecast tasks. A cycling forecast's product is the restart
  set the next cycle reads, and it writes no history at all; writing it every cycle of an
  ensemble is how a free run fills a disk. An extended forecast exists to be scored and writes
  intervals. Same executable, same code path, different file, which is what makes this
  configuration rather than a branch.
- **`input.nml` is patched, not regenerated.** ACKBAR sets the run length, the fallback date and
  the intermediate restart cadence in `coupler_nml` and leaves the couple of dozen groups of
  model physics alone.
- **A 4D window's sub-window states are intermediate restarts, not history.** `forecast.slots`
  becomes `restart_interval` in `coupler_nml`, so one model run dumps a state at each of them as
  its clock passes; the alternative shape, a chain of short forecasts, pays a model
  initialization per slot and puts a restart handoff between each pair. They are restarts rather
  than `diag_table` output because SOCA reads a state through `fields metadata`, whose every
  ocean entry maps to a restart variable name and not the diagnostic name for the same field.
  See phase 9 in `docs/build-order.md`.
- **A 4D window makes the cycling forecast overshoot, and its restart set becomes an interval of
  the run.** The window is centred on the analysis time, because that is where FGAT writes the
  analysis, so half of the *next* cycle's window lies after that time and only this cycle's
  forecast can cover it. It runs the cycle length plus half a window, which is `1 + W/2C` times
  the model and one and a half at the usual `W == C`. The set the next cycle starts from is then
  the interval at its own analysis time rather than the last thing written, and every interval is
  a complete set, so `mom6sis2.commit` assembles it by matching the stamp and undoing the three
  naming conventions that land in one `RESTART/`. Integrating past a time does not change the
  state at it.

v2 had a seven-way case statement over `DA_MODE`. Most of those modes are the same code with
different covariance or window settings. The real axes:

| Axis | Values |
|---|---|
| model | `mom6sis2`, `persistence`, `stub` |
| solver | `none`, `variational`, `letkf` |
| covariance (variational only) | `static`, `ensemble`, `hybrid` |
| window | `3d`, `fgat`, `4d`, each with its own length, defaulting to the cycle |
| ensemble source | `letkf`, `none`. `eda`, `offline` and `perturbation` are vocabulary the schema carries; `graph.build` refuses them when the *covariance* is drawn from the ensemble, and does not check them for an ensemble filter, which maintains its own members |

So v2's modes map as: `3dvar` = variational+static+3d, `3denvar` = variational+ensemble+3d,
`3dhyb` = variational+hybrid+3d, `3dfgat` = variational+static+fgat, `4denvar` and `4dhyb` are
the 4d column, `letkf` is the other solver, and **`eda` is not a mode at all**, it is an
ensemble source.

**The window axis is not square across the two solvers.** `fgat` is variational only, and
`graph/build.py::_check_ensemble_window` refuses it on an ensemble filter. FGAT *is* the
combination of right-time departures with an analysis-time covariance, and an ensemble filter
cannot state that combination: its departures and its observation-space perturbations come from
one hofx over one set of member states. Three-dimensional states put both at the window's centre,
which is `3d`; four-dimensional ones put both at their own slots, which is `4d`. There is no
third loading. A variational solver keeps `fgat` because its trajectory and its B are separate
objects, so its departures can come off a stepped trajectory while its B stays at the analysis
time. The asymmetry is what each solver can express, not an omission.

Which makes the cross-solver pairing three rows against two:

| variational | ensemble filter | |
|---|---|---|
| `3d` + ensemble | `3d` | exact: same departures, same covariance, two ways of solving |
| `4d` + ensemble | `4d` | exact: both make the increment `X_b(t) w` in a per-slot basis |
| `fgat` + ensemble | `4d` | nearest available, not exact |

The last row is the one to state carefully when results are reported. It matches on departure
timing, which is the axis that has actually moved a number here (see `docs/osse.md` section G,
where it is the whole of the 3DVar to 3D-FGAT surface temperature result), and differs in whether
the ensemble covariance is per-slot: `fgat` carries one set of perturbations at the analysis time
(`member_states` in `ackbar/soca.py`) where `4d` samples the ensemble at each sub-window. Pairing
it against `3d` instead would differ in the departure timing,
which is worse, because that is the axis known to matter.

### The ensemble filter is split into an observer and a solver

`soca_letkf.x` can compute its own ensemble departures, and doing so is what makes a
four-dimensional ensemble filter expensive: `oops::LocalEnsembleDA` holds the whole background
`StateSet`, so twenty members over a four-slot window is a hundred states resident before the
solve starts. That is the thing that will not fit on a real domain, and it is a cost the method
does not actually require.

So ACKBAR runs the two halves separately, inside the one `da` job:

1. `soca_ensmeanandvariance.x` once per sub-window, building the prior mean trajectory;
2. `soca_hofx.x` on that mean, then once per member, serially;
3. a merge into one file per observer, holding every member's H(x);
4. `soca_letkf.x` with `driver: read HX from disk`, reading those files.

**The consequence is that a 4D-LETKF costs the solver exactly what a 3D one costs it.** The
weights are solved in ensemble space and applied to the perturbations at the analysis time, so the
solver reads one state per member whatever the window is: the four dimensions live entirely in the
departures. That is Hunt et al.'s 4D-LETKF and not an approximation of it, and it means there is
one analysis to write back rather than one per sub-window. It also means `member_trajectories` in
`ackbar/soca.py` is read by the *observer* for an ensemble filter and by the *solver* for a
4D-Ens-Var, which is the one place the two methods' plumbing differs while their answers agree.

Three things about the split are easy to get wrong and are pinned by tests:

- **`ObsError` in the merged file is the assimilation mask.** The solver runs no filters, so
  `_solver_observers` strips them; what reaches it instead is the observation error left after the
  filters ran on H(mean(Xb)), whose missing values `oops` reads as "not assimilated". This is why
  the mean trajectory is computed at all: matching the state `oops` evaluates QC against is what
  makes the split reproduce the monolithic filter rather than merely resemble it.
- **The merge is by row, and nothing in the files says that is safe.** ioda writes rank-major
  order with no index to join on. It is deterministic given the same input, distribution and rank
  count, which every member run has, so `ensemble_hofx` asserts identical observation metadata
  across the members and refuses rather than producing a quietly wrong analysis.
- **The observer takes `RoundRobin` and the solver `Halo`.** The observer is global; the solver
  needs every rank to hold a halo as wide as its localization. Both read the same observer layer,
  which is why `GLOBAL_DISTRIBUTION` is set per application and never by a layer.

The split gives up nothing, and the one thing it looked like it would is worth recording because
the first version of this section had it wrong. The posterior observer evaluates a single analysis
state at the window's centre, so in a four-dimensional window it is a different operator from the
`ombg` taken over the trajectory. Turning it off is not the fix, for two reasons. `oops` saves the
obs space only when `!read HX from disk || do posterior observer`, so with the departures read from
disk that writes **no** observation output at all, `ombg` included. And `soca_var.x` does the same
thing anyway: `CostFctFGAT::doLinearize` sets `fgat_` only on iteration 0 and `finishLinearize`
clears it and replaces the background and first guess with the midpoint state, so every later
`runNL` takes the single-state branch and 3D-FGAT's `oman` is a centre evaluation against a 4D
`ombg` too. Leaving it on therefore makes an ensemble filter's `oman` mean exactly what the
variational one's means, which is what makes the two comparable at all. A trajectory-consistent
`oman` for both is the background trajectory plus the posterior mean increment at every slot
through a second observer run, and it is not built for either.

Two solvers, and the variational one is parameterized. The configuration layers carry the
parameterization, so there is no mode dispatch in the code.

That parameterization is a handful of keys under `solver`, and they are exactly the parts of the
analysis document ACKBAR cannot derive from something the experiment already states.
`background error` is the static B as a SABER block; `variational` is the minimizer and its loop
structure; `ensemble error` is what localizes the ensemble component, and `hybrid weights` is how
much of each a hybrid takes. All of them except the weights are
verbatim SABER and OOPS config, named by the schema and unvalidated inside, on the same rule as
the body of an observer: ACKBAR's schema describes ACKBAR's config and is not a model of either.
Two more are variable lists, and they are two rather than one because `background variables`
is a superset of `analysis variables`: the background error blocks read cell thickness, mixed
layer thickness and depth to build their standard deviations and never write them. Conflating the
two is what soca-science did, through a single `__DA_VARIABLES__` anchor, and then patched around.

Everything else the analysis reads, ACKBAR builds: the geometry from the model layer, the
background from the previous cycle's restart set, the time window from the cycle, the observers
from `observations`, and the departure diagnostics, which are not configurable because an analysis
writing no `ombg`/`oman` leaves post-processing with nothing to read and looks healthy the whole
way through. A `variational` solver that states none of these is refused by the schema rather
than run on whatever OOPS defaults to, since that is a different experiment and not an
under-specified one. The schema asks for each of them under the covariance that reads it: a
pure ensemble covariance needs no static B, and only a hybrid needs weights.

The distribution used to be on this list and is not. It is a property of the *application*
reading an observation file rather than of the experiment, which only became visible when a
hybrid cycle put two applications over one observer list: see `GLOBAL_DISTRIBUTION` in
`ackbar/soca.py`.

**`noda` is not a mode either.** Two independent properties: does the run produce an analysis,
and does it evaluate observations. A free run does neither, hofx evaluates only. So a free run
is `solver: none`, and observation evaluation is a property of any run. In a DA run the
analysis application produces `ombg`/`oman` itself; in a free run a standalone hofx task
produces the same diagnostics.

## Domains

Three classes must be supported, not two:

- **global 1 degree** (`OM_1deg`), the coarse global case. It was meant to be the development
  and test domain and is not: a forecast there is slow enough that iterating on it was the
  bottleneck, so `gom_25km` took that role and `om_1deg`'s live use is the graph fixtures,
  which run no model
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
  own MOM6 as a library from NOAA-EMC. v2 papered over the mismatch with `soca_dynsym2dyn.sh`,
  converting restarts between layouts, while its own config file carried the note "TODO
  investigate building soca with MOM6 regional (symmetric memory)". That TODO is still the real
  fix and it is a build-level decision, not a workflow one.

  It bites earlier than the restart shapes, though. MOM6 refuses to *configure* Flather open
  boundaries at all in a non-symmetric build, aborting inside `soca_geom_init`, so on a regional
  domain every SOCA application dies during geometry construction before it has read an
  observation. ACKBAR works around that with a `MOM_override.soca` per domain, read by SOCA and
  never by the forecast, which switches the segments off for geometry purposes only: the grid
  with three Flather segments and the grid with none are the same grid, and the grid is all SOCA
  wants from MOM6. See `docs/domains.md`. This is a workaround with a deletion condition, not a
  design.
- **Open boundary conditions.** Regional runs need boundary forcing from a parent solution, so
  they add both a per-cycle input and an offline stage that global configurations do not have.
  Note that MOM6's OBC code was substantially overhauled between the 2024 and 2026 pins (see
  `docs/model-build.md`), so any OBC configuration ported from a v2-era regional setup should
  be assumed stale until checked.
- **Grid edge masking.** The analysis must not write into the boundary and sponge zone. v2
  zeroed the outer ring of `mask2d` after gridgen (`soca_domom6_action.py mask-grid-edges`).

  Whether ACKBAR still needs to is **an open question, not a task**. That workaround is old
  enough that it may be describing a SOCA which no longer exists, and copying it forward
  because v2 had it is exactly the mistake `prior-workflows.md` exists to prevent: it would
  throw away the outermost row of every analysis on evidence nobody checked. It also is not
  free of consequences elsewhere. The diffusion calibration reads `mask2d` (see
  [`background-error.md`](background-error.md)), so masking the ring changes the correlation
  everywhere near the boundary, not just at it.

  Settle it by experiment rather than by reading: once a 3DVar cycle runs on a regional
  domain, look at the increment in the boundary columns. If SOCA already leaves them alone,
  the masking is not owed and this bullet goes away. If it does not, mask the ring in
  `tools/soca-gridspec.sh` and recalibrate the diffusion, in that order.
- **Observation culling to the domain.** v2 did this per cycle with `soca_domaincheck.py` and
  flagged it in its own source as a temporary fix that "new workflow should address in a more
  effective manner". Here it is **an offline stage keyed on domain**, like the gridspec and the
  background error: run once against an archive, producing a domain-scoped archive that every
  experiment on that domain then reads unchanged. That placement is what keeps the per-cycle
  path identical across domains, and it is also what makes two experiments on one domain
  comparable by construction rather than by inspection.

  What it is owed *for* turns out not to be stability. A global observation file handed to a
  regional domain does not break anything: SOCA runs, every out-of-domain observation fails QC,
  and the cycle completes. It is owed so that the observation counts an experiment reports are
  the counts it assimilated, so that a `Domain Check` rejection means what it says rather than
  "outside the grid", and so that the archive is not orders of magnitude larger than the domain
  needs.

  Built as `tools/obs-cull-domain.py`. Two decisions inside it are worth stating here because
  both are the kind a later reader reverses on sight. It culls on the grid's **extent** and
  never on its land mask, which keeps a culled archive keyed on the extent alone, so a change
  to a domain's mask or topography does not invalidate it, and leaves a land rejection to
  SOCA's `Domain Check` where it is counted honestly. And a time bin with nothing in it gets an
  **empty file** rather than no file, because ioda has a canonical empty representation and the
  culled archive should mirror the source file for file. Whether the observer runs is decided
  from the observations in the window rather than from the file's presence, so an empty bin and
  an absent one lead to the same place: the observer is dropped for the windows it covers.

  Because the stage is offline, nothing stops an experiment pointing at an unculled archive, so
  two checks make that loud rather than silent. `validate` step 3 refuses an experiment whose
  *every* observer has nothing inside the domain, which is a wrong path rather than a quiet
  platform, and `post.obs` fails a cycle that read observations and assimilated none of them.
  Both are careful to read "has observations elsewhere" and never "has no observations": step 3
  samples the first file per observer that has **rows in it** rather than the first that exists,
  because the empty file two paragraphs above is what a quiet window looks like, and an archive
  that is empty everywhere it was read produces no finding at all rather than a refusal whose
  message counts zero observations.
  Neither existed when this bullet was written, and without them the offline placement would
  have traded a silent empty analysis for a silent empty archive.
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
in-cycle obs step reduces to: join the archive bins this window touches into one file, and drop
observers with nothing inside the window (unless marked required, per v3's `_required`). The
archive is keyed by date and the experiment's own output by cycle number, which looks
inconsistent and is not: an archive is built once and read by experiments that number their
cycles differently, while everything an experiment writes has to be addressable by cleanup and
the harvest, which work off cycle numbers.

**The archive knows nothing about the assimilation cycle, and the window is cut once.** It is
filed in fixed time bins, `<platform>/<bin start>.nc4`, and the bin size is the generator's
argument rather than anything the workflow stores. It was one file per window, cut by the
generator, and that was wrong in a way that cost real observations: an assimilation window is
half open, `(begin, end]`, so an observation stamped at the instant a window opens is dropped
by that window, and with a per window archive it was in no other window's file to be picked up
by. Every fixed cadence platform lost about a quarter of its observations in every cycle, in
silence, because the file held the row and the observer simply never formed a departure from
it. Cutting at generation time also baked a DA convention into a read-only product: an
experiment could not change its window length without rebuilding the archive, and a real
observing system, which arrives in granules of a few minutes and cannot promise to avoid an
edge, could not be filed in it at all. soca-science did not do it this way either: its
`prep.obs.sh` kept a continuous `P1D` or `PT10M` database, fetched everything covering the
window, concatenated it and cut once.

So `stage.obs` selects the bins, joins them, and hands ioda a single file to apply its own half
open window to. The selection needs no index and no stored bin size: the files a window
`(begin, end]` needs are the one with the largest start at or before `begin` plus every one
starting in `(begin, end]`, which is correct for daily bins, ten minute granules, and an
archive that changed cadence half way through. It rests on one property, that a platform's bins
tile the period without overlapping, which is the generator's to guarantee and is stated in the
archive's own `README`. The upper bound is closed for the same reason the window is: an
observation at exactly `end` is inside the window, and if `end` falls on a bin boundary that
observation is in the bin *starting* at `end`. That is the original bug in its general form.

Two consequences worth stating. `obs_cat.x` is not built in this bundle, so the join is Python
(`src/ackbar/obsarchive.py`): structure preserving, along `Location`, and rebasing the times,
because ioda names a time epoch in `MetaData/dateTime`'s `units` and each bin is written
against its own, so a naive join would file a day's observations on the day before and every
one of them would still land inside the window. And what makes an observer *present* is an
observation inside the window rather than a file on disk, because the selection rule reaches
back to the last bin at or before the window and therefore nearly always finds one: without the
count, an archive that stopped half way through an experiment would keep every observer for
every later cycle and record a full observing system that assimilated nothing.

The joined file is the experiment's own, at `obs_in/<date>/<platform>.nc4`, and it is kept
rather than reaped: it is what was actually handed to the observers, which the archive alone no
longer answers.

**One file, one writer, and a reader that needs a window someone else owns stages it itself.**
`obs_in/<T>` is cycle T's `stage.obs`'s output, so a task in cycle 1 that reads `obs_in/<T+2>`
is reading a file no job has been ordered to write yet. `hofx.ext` is the one task that wants
such a window, and it joins its own; see the long forecast's entry in On-disk layout above for
why it also has to cut, which `stage.obs` must not.

Because that makes the observation set vary silently, **the realized observer list is written
per cycle** and diffed by the comparison tooling. Two experiments differing in which observers
actually ran is the difference that most affects a comparison, and it must not be invisible.

It lives at `obs_out/<date>/observers.json`, beside the observation output it describes rather
than under `cfg/`, because it is a product of the cycle and not of the configuration: the same
experiment over a different archive writes a different one. Every configured observer is in it
whether or not it ran, since a list naming only what ran makes a drop indistinguishable from an
observer that was never configured. `stage.obs` writes it and hofx reads it rather than asking
the filesystem a second time, so a file that arrives between the two jobs cannot change the
observer set without changing the record of it.

**Some missing is the archive, all missing is the configuration.** An observation file is the
one input an experiment is allowed to be missing, so `validate` cannot check it the way it
checks a grid file; skipping it entirely would let a misspelled archive path produce an
experiment that runs to completion assimilating nothing. The rule that separates the two is
proportion. An observer with nothing in some windows is a gap, and is left to `stage.obs`. An
observer with nothing in any window at all is reported before submission. One marked `required`
is checked window by window, because the experiment has already said its own gaps are not
acceptable. The unit is a window and not a file: under a window agnostic archive a file is
nearly always found, so both halves of the rule ask the times whether anything is inside the
window, which is the same question `stage.obs` asks and off the same reader.

**The same rule reads across the observers as well as down the cycles, and that half is a
refusal.** A cycle in which no observer at all has an observation assimilates nothing, and nothing
downstream can say so: the analysis is skipped because there is nothing to solve against,
`writeback` copies the background into `ana/`, `post.obs` writes zeros and passes its own
all-rejected check, which is about observations that were read. Three such cycles are a free
run inside an experiment that reports as an assimilation, and no artifact's absence marks them.
So `validate` reports the empty windows before submission and `stage.obs` refuses one when it
reaches it, which is where `ackbar heal` picks the cycle up once the archive is fixed. What
makes the refusal defensible rather than superstitious is the archive itself: it is an offline
product, so its other windows are on disk to be compared against, and one platform being absent
is a gap while every platform absent at the same instant is a window that was never built.
There is deliberately no way to declare an empty cycle acceptable. Every archive here is
generated over a period it covers completely, so an empty window is a hole in something that
was meant to be whole; the
cost is a single-observer experiment over a gappy real archive, where the platform's gap and
the empty cycle are the same event and the run stops at it.

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
| model input | domain | the MOM6-SIS2 configuration's data half: grid, bathymetry, open boundaries |
| static | domain | `soca_gridspec.nc`, horizontal correlation scales, localization scales |
| initial condition | domain, source, date | a spun-up restart set |
| observations | period (and domain, if culled) | archive of ioda files, real or OSSE-generated |
| forcing | purpose, source, period | atmospheric forcing archive (`docs/forcing.md`) |
| boundary forcing | domain, period | open boundary conditions from a parent solution (regional only) |

Static is keyed on **domain, not experiment**, so one `static/om_1deg/` is shared read-only
across every experiment on that domain. That is what makes experiments comparable by
construction. It is also the one stage an experiment does not name for itself: the domain layer
carries the path, because "keyed on domain" and "chosen per experiment" are contradictory, and
an experiment that could point at its own gridspec could be incomparable without saying so.

`soca_gridspec.nc` is the geometry SOCA reads instead of initializing MOM6 to discover the
grid, and building it *is* a full MOM6 initialization: more memory than the analysis that
follows it, spent to produce the same bytes every time. That is the argument for the whole
stage in miniature. `tools/soca-gridspec.sh` writes it from the model's own configuration, so the
grid the analysis works on and the grid the model integrates on are the same grid by
construction rather than by two configurations agreeing. Nothing detects a stale one, so it is
rebuilt after a bundle bump that touches MOM6 or SOCA's geometry.

The stage's other product is `static/<domain>/diffusion/`, the calibrated correlation of the
static background error and the localization an ensemble covariance applies, both written by
`tools/soca-diffusion.sh`. It belongs here on the same
argument and one more: its normalization is a Monte Carlo estimate, so two runs of it do not
agree bit for bit, and an experiment that calibrated its own would differ from its neighbour
in a way no comparison would attribute correctly. A free run names none of it.
[`background-error.md`](background-error.md) is the entry point.

They live under `$ACKBAR_STATIC_ROOT`, one directory per stage, keyed as the table says and in
that order: `ic/<domain>/<source>/<YYYYMMDDThh>` is the initial condition stage,
`obs/<source>/<period>` the observation stage, and `static/<domain>` the static stage. The
observation layers append their own `<platform>/` beneath what they are given, so `$(obs_dir)`
names the archive and not a platform inside it, and nothing about a cycle appears below it.

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
state happens to sit at its start date. That holds for observations too: `$(obs_dir)` is a `vars`
entry the experiment sets, the same way it sets `model.initial_condition`, and not a built-in
computed from the site and the cycle range. An OSSE experiment and a real-observation experiment
over the identical period differ in exactly that one line, which is a difference worth being able
to read off the config.

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

With a covariance drawn from an ensemble, two nodes join that line and `writeback` moves behind
them:

```
  obs staging  ->  da.ens  ->  da  ->  recenter  ->  writeback  ->  ...
                   da.ens  ->  recenter
```

`da` is the analysis producing the control's answer, whichever solver that is; `da.ens` is the
ensemble filter that maintains the ensemble the covariance is drawn from, present only when
`ensemble.source` says something in this cycle has to. The edge between the two is an ordering
rather than a data dependency, since the covariance reads the members' *backgrounds*: what it
serializes is the divergence policy, which exactly one job may apply.

Cross-cycle: `forecast(n) -> da(n+1)`, and the submitter, and nothing else.

**Cycle 1 is not special.** Experiment setup materializes the offline initial condition into
the experiment's own cycle-0 forecast output location, so `da(1)` resolves its background by
exactly the same rule as `da(50)`. Without that one step the asymmetry the design claims to
have removed is merely relocated, and `first_cycle_only` machinery comes back.

Cross-cycle is `forecast(n) -> da(n+1)`, and also `forecast(n) -> b.corr_vt(n+1)`, because vertical
B calibrates from the background. With no analysis configured the restart handoff is
`forecast(n) -> forecast(n+1)` directly.

**Two tasks are roots of their cycle and nothing else is.** `stage.obs` reads the observation
archive, which is an offline stage that exists before the experiment starts, so nothing in the
experiment gates it. `cleanup` is gated on artifact existence rather than on job state, for the
reasons in Task completion. Any other task without an incoming edge is a job that runs before
its input exists, so the generator refuses to produce one.

Ensemble shape: per-member forecasts are a job array over the canonical index set; an ensemble
filter is a single MPI job consuming all members, and so is a recentring, because the mean it
subtracts belongs to every member at once; per-member writeback is an array. Every
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

**Where the ensemble's spread comes from is a configuration axis, not a property of the
solver.** An ensemble filter removes spread every cycle and a free forecast from one atmosphere
puts back very little, so an ensemble maintained only by its own analysis narrows towards a
mean the observations then cannot move. Relaxation to prior spread rescales what is there; it
cannot create structure the ensemble does not carry. Three sources were measured on `gom_25km`
against a control ensemble perturbed at two parts in a billion, so that the model's own
divergence rate could be subtracted rather than counted:

| source | where it acts | day 5 surface temperature spread, in excess of the divergence floor |
|---|---|---|
| ensemble atmospheric forcing | mixed layer, and still growing at day 5 | 0.84 degC |
| stochastic physics, oSPPT | the whole column, stationary | 0.21 degC |
| open boundary | inward from the segments, still growing at day 30 | 0.07 degC |
| perturbed parameters | nowhere much; a fixed offset that does not grow | at most 0.18 degC |

The boundary's day 5 entry understates it more than any other row, and the column is kept as it
is rather than widened, because a single horizon cannot be fair to all four. A boundary
perturbation has to be advected in: Counillon and Bertino (2009) measured, on this basin,
anomalies entering at the inflow, travelling around the Loop Current at about 30 km/day and
taking roughly three weeks to mature. The same ensemble reaches 0.14 degC of surface temperature
spread by day 30 and is still rising, its temperature spread peaks below the thermocline rather
than at the surface, and two thirds of its sea surface height spread is more than 200 km from
any segment. `tools/obc-lagged.py` carries the measurements.

**In a cycling filter the quantity it moves is not the spread but the collapse rate**, which no
free-forecast measurement can show and which is the reason the table above understates it.
Ten cycles of `osse25-4dletkf` against the same experiment with `ensemble.inputs` added: the
control's domain sea surface height prior spread falls monotonically from 0.0650 m to 0.0238 m
and is still falling, while the boundary ensemble's tracks it down to about 0.043 m and then
stops, holding 0.0432 to 0.0448 m over the last five cycles. They differ by 1.9 times at the
last cycle and the gap is widening. An LETKF's analysis removes more variance than its forecast
regenerates; what a boundary ensemble supplies is a source the analysis cannot consume, because
it is re-imposed from outside the domain each cycle rather than carried in the state the filter
updates. Banded by distance from the nearest open segment, and averaged over the ten cycles
rather than taken at the last one, the ratio is 2.78 within 50 km and 1.10 beyond 400 km: the
effect is concentrated where the shared boundary suppressed spread, not spread evenly over the
basin.

**That is a statement about spread and not about calibration.** Both runs share truth's
boundary, so neither carries a boundary error and nothing here says the added spread is
*earned*; an ensemble can be given spread it has not earned, and the number that would settle it
is spread over error against a truth whose boundary differs. It also runs at amplitude 1 where
the inter-product anchor points at 1.5, and some unknown fraction of the sea surface height
spread is the basin-wide mode no altimeter reports, so the arrested curve is an upper bound on
the arrest the filter feels. `tools/obc-spread.py` computes it and `site/monitor/osse/obc/`
carries the figures and the same caveats.

Three of the four are implemented: `ensemble.stochastic` is the model-internal one, and the
other two arrive as files through `ensemble.inputs` (below).
[`ensemble-spread.md`](ensemble-spread.md) is the reference for configuring all three and for
the offline archive each needs. Stochastic physics is *stochastic*
rather than a perturbed-parameter ensemble on purpose: a member with its own parameter values is
a different model every cycle, so the ensemble covariance stops being the covariance of anything
and the mean is biased by whatever the parameter offsets do. A stochastic scheme draws afresh
each cycle, and its members stay exchangeable.

**Spread that arrives as a file arrives through one mechanism.** `ensemble.inputs` maps a name
inside the model's `INPUT/` to a path template carrying `{{member_dir}}`, and `mom6sis2.stage`
links each member's file in between the domain's shared archive and the incoming restart set.
That ordering is the whole design: a per-member input replaces the domain's copy of that file,
and both lose to the restart set, because `coupler_main` reads `INPUT/coupler.res` from a
hardcoded path and a cycle whose date came from anywhere else integrates the right state from
the wrong time. A name that appears in both is refused rather than silently overwritten.

The key is the name the model opens and the value is where that file's ensemble lives, and those
two are deliberately independent, so a boundary ensemble and an atmospheric one can be built by
different offline stages with different layouts and staged by the same code. Every member
resolves to a file, including the control and including a source with only one realization,
which materializes as N symlinks to that one file. What each member resolved to is written to
an `ensemble.inputs` file beside the run and copied out with the model's other traces, because
`readlink INPUT/atm.nc` answers only while the job is running: the run directory is scratch and
is deleted on success, so a paired experiment whose result is "which boundary did each member
integrate" could otherwise not answer its own question once it finished. A missing file is refused rather than
falling back to the domain's copy, because the fallback is a member with no perturbation whose
only symptom is an ensemble slightly less spread than it should be.

That refusal is raised where the member is staged, which means inside the job, so **`ackbar
validate` checks both of the failures that would otherwise reach it at step 3.** An archive
missing a member is an ordinary path finding, because the rendered value is an absolute path
per member and `_collect_paths` already walks it. An archive whose time coverage does not span
the experiment's cycles is the worse of the two, because it does not fail until the cycle that
runs off the end of the file, healing cannot recover it, and the message comes from
`time_interp_external` inside MOM6 rather than from anything ACKBAR wrote; `_coverage_step`
compares the file's own span against the window the graph will ask for.

That check reads the time axis rather than the `time_coverage_start`/`time_coverage_end`
attributes `tools/obc-lagged.py` writes. Trusting the attributes would only work for archives
from that one tool, would pass silently on every archive built before it started writing them,
and would be checking an annotation rather than the thing the model opens. "Time axis" means
any variable carrying `axis = "T"`, falling back to a variable named `time`, and the narrowest
span across them: the atmospheric archive gives every field its own unlimited axis and carries
no variable named `time` at all, and its interval means end half an interval short of its
instantaneous fields.

**A boundary ensemble carries a basin-wide sea surface height mode that no analysis can remove,
and the obvious fix does not work.** `ufo::ObsADT::simulateObs` computes `offset = mean(H(x) - y)`
over the observation space and subtracts it from `H(x)`, and `ObsADTTLAD` does the same in the
tangent linear and the adjoint. Altimetry is therefore assimilated as an anomaly about its own
domain mean, so a member whose sea level is uniformly high carries an error no observation
reports and neither an LETKF nor a variational solver can remove. On `gom_25km` that mode is
47% of the interior sea surface height spread variance, 3.0 cm of 4.35 cm at day 30.

The reasoning that says to fix it by removing the boundary's own basin-wide `zeta` is easy to
reconstruct and is wrong, so it is recorded here: every GoM segment is FLATHER, FLATHER imposes
a prescribed head, so a boundary-wide `zeta` anomaly ought to pump the basin. It was built and
measured. The interior basin-wide spread did not fall, 0.0298 m against 0.0362 m across two
otherwise identical seven member spikes, and correlating each member's boundary-wide `zeta`
anomaly against its day 30 basin-mean sea surface height gives -0.23: no relationship and the
wrong sign. The change was reverted. The remaining candidate is net volume flux through the
segments, which needs the domain's mask and topography to constrain, and `tools/obc-lagged.py`
carries the full account.

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
- **Ensemble geometry on rancor.** 8 cores total against an 8-PE model run means real-model
  member parallelism requires fewer PEs per member or oversubscription. The stub model removes
  this from the critical path for testing the workflow, but a small real configuration running
  four concurrent members at 2 PEs is still worth having as a correctness check.
- **Whether there is a control member, per DA method.** Settled for two of them. An LETKF does
  not use one, and `stub_letkf` runs with `control: false`. A hybrid does: `mem000` is the
  deterministic analysis, it is not assimilated by the filter, and it is the centre every other
  member is recentred onto. The graph carries this as `ensemble.control`, defaulting to true.
  What is still open is EDA, where every member is a deterministic analysis of perturbed
  observations and the control is one more of them or none of them.

### Settled, and listed here because the reasoning is easy to reopen

**Where the analysis time sits in the window: centred, and not a choice.** The obvious proposal
is to make placement a configured property of the solver, and `oops` refuses it.
`CostFctFGAT::doLinearize` saves the state at `timeWindow_.midpoint()` and `finishLinearize`
replaces the background with it, so an off-centre window writes an analysis valid at a time no
cycle starts from. The window is `window_bounds` in `src/ackbar/config/jobtime.py`, the analysis
time plus and minus half the *window* length, which is `solver.window.length` and falls back to
the cycle length. `W` and `C` are independent, and `graph.build._check_window` refuses each way
they can fail to fit, by name.

**`da` is two nodes, and the first kept its name.** `da` is the analysis that produces the
*control's* answer, whichever solver that is; `da.ens` is what maintains the ensemble a hybrid's
covariance is drawn from, and exists only where `ensemble.source` says something in this cycle
has to. Two nodes rather than one parameterized by instance, because they are different
applications with different configs, different resources and different member cardinality, and
because `soca_letkf.x` running under a name that says `var` is the first thing to confuse anyone
reading a queue. Keeping `da` for the first of them meant no existing shape's graph, paths or
sentinels moved. The edge between them is an ordering rather than a data dependency: exactly one
job may apply the divergence policy, since `replace_from_mean` rebuilds a member's restart set.

**The vertical B is calibrated per cycle by `soca_error_covariance_toolbox.x`, as `b.corr_vt`.**
An experiment opts in with `config/layers/da/corr_vt_cycled.yaml`, which rebuilds the vertical
scales from that cycle's own background and blends them into a rolling average; without it the
offline calibration seeds every cycle. `soca_sqrtvertloc.x` is not it: that is vertical
*localization* for an ensemble covariance, a different quantity from the static B's correlation
scales.

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
