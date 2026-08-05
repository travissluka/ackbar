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

**mpiexec inside a Slurm allocation.** MPICH's hydra launcher detects Slurm and tries to
launch through `srun`, which needs a PMI plugin Slurm was not configured with here
(`MpiDefault=none`). If `mpiexec -n 8` hangs or errors inside a job, force local launching:

```bash
export HYDRA_LAUNCHER=fork      # or: mpiexec -launcher fork -n 8 ...
```

Single node, so nothing is lost. Revisit only if the fake-node setup below is adopted.

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
