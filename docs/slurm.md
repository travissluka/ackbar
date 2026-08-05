# Slurm on rancor

A single-node Slurm so the ACKBAR workflow engine can be developed against a real batch
scheduler (sbatch, job dependencies, arrays, sacct polling) before it ever runs on an
HPC. It is a test scheduler, not a way to get more out of the box: rancor has 8 physical
cores and no other users.

Everything lives in `tools/slurm/`. Config is installed to `/etc/slurm/`.

## Install

```bash
sudo tools/slurm/install.sh          # all stages
sudo tools/slurm/accounting-setup.sh # cluster/account/user in the accounting DB
tools/slurm/smoke-test.sh            # prove it works
```

`install.sh` takes stage names to rerun a piece: `pkgs munge dirs db config svc verify`.
All stages are idempotent.

What it does:

- **pkgs** - `slurm-wlm`, `slurmdbd`, `munge` from Ubuntu universe (23.11), plus
  `mysql-server` if no MySQL/MariaDB server is installed. rancor already had a stock
  MySQL 8.0 datadir in `/var/lib/mysql` with no user databases; the install adopts it.
- **munge** - creates `/etc/munge/munge.key` if absent and verifies a round trip. Munge is
  Slurm's authentication; if it is down, every Slurm command fails with a credential error.
- **dirs** - `/var/spool/slurmctld` and `/var/log/slurm` owned by `slurm`,
  `/var/spool/slurmd` owned by root.
- **db** - random 32-char password in `/etc/slurm/.dbpass` (root, 0600), creates
  `slurm_acct_db` and the `slurm` DB user, and drops an innodb tuning snippet in
  `/etc/mysql/conf.d/slurmdbd.cnf` that slurmdbd wants.
- **config** - installs `slurm.conf`, `cgroup.conf`, and `slurmdbd.conf` (0600, owned by
  `slurm`, since it holds the DB password).
- **svc** - starts slurmdbd first so the accounting tables exist before slurmctld
  registers the cluster, then slurmctld and slurmd.

## Shape of the cluster

One node `rancor`: 1 socket, 8 cores, 2 threads/core, `RealMemory=60000` (of ~64 GB, the
rest is headroom so the OS is not the thing that OOMs).

`SelectType=select/cons_tres` with `CR_Core_Memory`, so cores are the allocatable unit and
memory requests are tracked and enforced. `--ntasks=8` takes the whole box, which matches
the `mpiexec -n 8` convention in the top-level `CLAUDE.md`.

`DefMemPerCPU=3750` (60000 / 16 CPUs) so a job that omits `--mem` is charged in proportion
to the cores it asked for. This matters more than it looks: Slurm's default is
`DefMemPerNode=UNLIMITED`, under which every job silently reserves all 60000M, so array
tasks and independent jobs run strictly one at a time no matter how many cores are free.
An `--array=1-4` that serializes would teach the workflow engine the wrong thing about
fan-out. `MaxMemPerCPU` is left unset, so a memory-bound job can still ask for one core and
50G.

Two partitions, so partition selection in the workflow is exercised rather than hardcoded:

| Partition | MaxTime | DefaultTime | PriorityTier | |
|---|---|---|---|---|
| `debug` | 30 min | 10 min | 100 | short, jumps the queue |
| `compute` | 8 h | 1 h | 10 | default |

Containment is cgroup v2 (`ProctrackType=proctrack/cgroup`, `TaskPlugin=task/cgroup,task/affinity`),
which is what makes a job that blows its `--mem` get killed the way it would on an HPC.

That needs **`ConstrainSwapSpace=yes`** as well as `ConstrainRAMSpace=yes`. With RAM alone,
Slurm sets the cgroup's `memory.max` and leaves `memory.swap.max` unlimited, so a job that
exceeds its request swaps past the limit and finishes `COMPLETED` instead of `OUT_OF_MEMORY`.
The workflow's memory fault test skips itself, with that explanation, on a cluster configured
the other way.

## srun and PMI

MPI is launched with `srun --mpi=pmi2`. spack-stack's MPICH speaks Slurm's PMI2 wire protocol
with no build flag on either side, even though it is configured `--with-slurm=no` and ships
hydra as its own process manager, so nothing had to be rebuilt to get this. `MpiDefault=pmi2`
in `slurm.conf` makes a bare `srun` work for interactive debugging; the workflow does not rely
on it, because `ACKBAR_LAUNCHER` in the site file spells the flag out and `MpiDefault` is a
per-site setting the workflow should never be at the mercy of.

Which plugins this Slurm actually has: `srun --mpi=list`. It reports `pmi2` and `pmix_v5`;
pmix is not usable here, since this MPICH was built without a PMIx client.

**`--mpi=none` does not fail, it lies.** `srun -n 8` still starts eight processes and every one
of them runs to completion, each in its own `MPI_COMM_WORLD` of size 1. A model launched that
way writes eight sets of output over each other rather than decomposing anything. There is no
error anywhere and Slurm records the job `COMPLETED`. Verify with a hello-world that prints
`MPI_Comm_size` and an `MPI_Allreduce`, not with an exit code.

**Which launcher you use changes what `MaxRSS` means**, which is why `ackbar` records the
launcher next to the numbers it harvests. The same 8-PE `OM_1deg` run under each:

| | step row | `MaxRSS` | what the number is |
|---|---|---|---|
| `srun --mpi=pmi2` | `<job>.0`, named for the executable | ~1.4 G | one rank |
| `mpiexec` | `<job>.batch` only | ~10.7 G | all 8 ranks on the node |

Roughly a factor of ranks-per-node between them, so sizing `--mem` from one regime with numbers
from the other is wrong by that factor in whichever direction hurts. srun also gets you a named
step row per launch, which is the only way per-launch accounting exists at all; under mpiexec
everything the script does is one `.batch` row. Neither run was measurably faster than the
other. Re-derive with `sacct -j <id> -P -o JobID,MaxRSS,MaxRSSTask,TotalCPU` on any two jobs.

Restarts are bit-identical between the two launchers, which is how the switch was verified:
same case, same layout, `cmp` on `RESTART/MOM.res.nc`.

## Two dependency profiles

Slurm's default is that a job whose dependency failed **pends forever** with reason
`DependencyNeverSatisfied` and never appears in `sacct` as failed at all. With
`DependencyParameters=kill_invalid_depend` it is cancelled instead, and the same experiment
leaves a completely different trail. Sites differ, the workflow has to handle both and depend
on neither, and the only way to know it does is to run the tier 2 suite under each:

```bash
sudo tools/slurm/profile.sh strict      && pytest tests/test_tier2.py
sudo tools/slurm/profile.sh permissive  && pytest tests/test_tier2.py
tools/slurm/profile.sh                  # which one is live
```

The strict profile also puts a small `MaxSubmitJobsPerUser` on a QOS, so that `ackbar validate`
step 5 is checked against a limit that exists rather than against rancor's 10000.

One thing the profiles do *not* change, and which is worth knowing before reading a stuck
queue: only the **direct** dependent of a failed job reads `DependencyNeverSatisfied`. Anything
further down the chain reads plain `Dependency`, which is indistinguishable from a job whose
parent is merely queued. "Nothing can make progress" is therefore a property of the whole
queue, never of one row.

What the two profiles look like from `heal`:

| | permissive (default) | `kill_invalid_depend` |
|---|---|---|
| the failure's dependents | pend forever | cancelled |
| the queue after a failure | never drains | drains |
| what `sacct` says about them | nothing at all | `CANCELLED` |
| `status` reads them as | stranded, from the `squeue` reason | failed, from accounting |
| what `heal` has left to cancel | all of them | nothing, Slurm did it |

So healing's `scancel` step is a no-op under the strict profile. That is not an argument for
dropping it: the profile that needs it is the default one, and it is the profile a new site is
most likely to be running.

## Requeue: two minutes, and it takes the whole array

Two behaviours, and together they were worth several minutes of every test run.

**A requeued job is deferred about two minutes** before it is eligible again. It shows as
`PENDING` with reason `BeginTime`, which reads like a scheduling problem rather than what it
is:

```
JobState=PENDING Reason=BeginTime Dependency=(null)
SubmitTime=...T11:48:05  EligibleTime=...T11:50:06
```

`Dependency=(null)`, so it is ready; `EligibleTime` is stamped 121 seconds out. This is Slurm
avoiding a tight requeue loop against a failing node, and there is no knob for it.

**`scontrol requeue $SLURM_JOB_ID` inside an array element requeues the entire array.** Every
sibling is killed and rerun, including ones that had already finished, and each then waits out
the deferral. Name the element as `$SLURM_ARRAY_JOB_ID`_`$SLURM_ARRAY_TASK_ID` instead.
Measured: a requeue asked for by member 3 of a 3-element array restarted all three, together,
150 seconds later.

Three consequences. Do not read a stalled-looking cycle as a fault until it has been that way
for longer than the deferral. Anything that requeues has to name the element precisely. And
this is the sharpest argument there is for the idempotency rules in `design.md`: a node failure
requeues jobs that already succeeded, and they must skip rather than redo. That path is not
hypothetical, it is what these measurements ran through.

Things that are *not* the cause, checked so nobody checks them again: queue depth and
`MinJobAge`. With 332 finished job records still held by the controller, an eight stage chain
of `aftercorr` arrays transitioned in 1 to 2 seconds every time, no deferrals. Measure with
`tools/slurm/measure-latency.sh` before changing scheduler settings.

Accounting goes through slurmdbd into MySQL, so **`sacct` works for completed jobs**. That
matters: the workflow engine should learn a job's fate from `sacct`, not from parsing
logs, because that is the only thing that still knows about a job after it leaves the
queue.

## What the workflow engine should rely on

The portable subset, all of which the smoke test proves:

- `sbatch --parsable job.sh` returns a bare job id on stdout.
- `sbatch --dependency=afterok:<id>` for the cycle chain. Also `afterany`, `singleton`.
- `--array=1-N` for per-member ensemble fan-out.
- `squeue -h -j <id> -o %T` for live state; empty output means the job has left the queue,
  which is **not** the same as success.
- `sacct -j <id> -n -P -o State,ExitCode` for the authoritative outcome. States to handle:
  `COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`, `OUT_OF_MEMORY`, `NODE_FAIL`.
- `scancel <id>` and `scontrol hold/release <id>`.

`MinJobAge=300` means a finished job stays in `squeue` for 5 minutes. Do not let the engine
depend on that; on a busy HPC it can be much shorter.

`tools/slurm/smoke-test.sh` exercises exactly this list and prints what `sacct` made of
each case. Rerun it after any config change; it is the regression test for the cluster.

## Traps

**mpiexec inside a Slurm allocation.** Only relevant if you deliberately go around `srun`.
MPICH's hydra launcher detects Slurm and tries to launch through `srun` itself, which fails
unless the PMI plugin lines up. If `mpiexec -n 8` hangs or errors inside a job, force local
launching:

```bash
export HYDRA_LAUNCHER=fork      # or: mpiexec -launcher fork -n 8 ...
```

Single node, so nothing is lost, but you give up the per-launch step row (see srun and PMI).

**`SLURM_CPUS_ON_NODE` counts threads, not cores.** With `ThreadsPerCore=2`, a job asking
for one task gets one *core*, and reports `SLURM_CPUS_ON_NODE=2`. Sizing an MPI launch off
that variable will double the rank count. Use `SLURM_NTASKS`, or ask for what you want
explicitly.

**cgroup v2.** If `slurmd` refuses to start with a cgroup error, fall back by editing
`/etc/slurm/slurm.conf`:

```
ProctrackType=proctrack/linuxproc
TaskPlugin=task/affinity
JobAcctGatherType=jobacct_gather/linux
```

then `sudo systemctl restart slurmctld slurmd`. Memory limits stop being enforced;
everything else still works.

**Node stuck in DOWN or DRAIN.** Usually the aftermath of a config edit or an unclean
restart. `sudo scontrol update nodename=rancor state=resume`. The reason is in
`scontrol show node rancor`.

**Config edits.** `slurm.conf` must be identical everywhere it is read. Edit
`tools/slurm/slurm.conf`, then `sudo tools/slurm/install.sh config svc` rather than editing
`/etc/slurm/slurm.conf` directly, so the repo stays the source of truth.

## Live state

Never trust a number written down in a doc; ask the cluster.

```bash
sinfo -l                       # partitions, node state
sinfo -N -o '%N %c %m %t'      # cpus, memory, state per node
scontrol show node rancor      # everything about the node
scontrol show config           # effective config as slurmctld sees it
squeue -l                      # queue
sacct -a -X --starttime today  # what ran today
sacctmgr show assoc            # accounting associations
journalctl -u slurmctld -u slurmd -u slurmdbd --since '10 min ago'
```

Logs: `/var/log/slurm/{slurmctld,slurmd,slurmdbd}.log`.

## Two fake nodes, later

Some workflow logic only shows up with more than one node: `--nodes=2`,
`--ntasks-per-node`, node lists in `$SLURM_JOB_NODELIST`, per-node task placement. The
config carries a commented block for running two 4-core nodes `n01`/`n02` as separate
slurmd daemons on the same host (multi-slurmd).

To switch:

1. In `tools/slurm/slurm.conf`, comment the `NodeName=rancor` line and uncomment the
   FAKE NODES block.
2. Make the per-daemon paths unique, since two slurmds cannot share a spool dir:
   `SlurmdSpoolDir=/var/spool/slurmd-%n`, `SlurmdLogFile=/var/log/slurm/slurmd-%n.log`,
   `SlurmdPidFile=/run/slurmd-%n.pid`.
3. Replace `slurmd.service` with a template unit running `slurmd -N %i`, enabled as
   `slurmd@n01` and `slurmd@n02`.

Caveat worth checking before committing to it: upstream Slurm requires the daemon to have
been built with `--enable-multiple-slurmd` for this. Whether Ubuntu's package carries that
flag is unverified. If the second daemon refuses to start, the alternatives are running
each `slurmd` in its own Docker container (rancor has docker, and the user is in the
`docker` group) or building Slurm from source with the flag.

The two fake nodes oversubscribe the same 8 real cores, so they are for testing placement
logic, never for timing anything.
