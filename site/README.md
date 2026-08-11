# The site layer

One file per machine, and the only place a machine-specific path may appear. Everything else in
the repository is written as though every machine were the same, which is only true because
this directory absorbs the difference.

`site/activate.sh` applies defaults, sources `site/$ACKBAR_SITE.sh`, and exports the result.
`ACKBAR_SITE` defaults to the short hostname, so `site/<hostname>.sh` is the ordinary case. On a
cluster whose login nodes are named separately, set `ACKBAR_SITE` yourself rather than naming
the file after a host you may not land on twice.

Porting ackbar to a new machine is writing one of these. `rancor.sh` is the only one anyone
has run, so it is the model: read it alongside this. `orion.sh` and `hercules.sh` also exist
and have never been run; see "Porting to an HPC" below for what they are and are not.

```bash
tools/site-probe.sh                        # run this first, on any new machine
cp site/rancor.sh site/$(hostname -s).sh   # then edit every path in it
source site/activate.sh
```

**`tools/site-probe.sh` is the first step for any machine**, not only the two HPCs below. It
prints every value a site file has to be told and cannot inherit from another host: the queues
this account may use, the QoS and cluster caps, the launcher flags this Slurm accepts, how an
unsatisfiable dependency behaves here, and which filesystems are candidates for the roots. It
is read-only. It submits no job, writes no file, and does not source `activate.sh`, because it
runs before there is a site file to source, and its answers are what make one writable. The
alternative it exists to replace is learning these one at a time, three cycles into a run, from
a job that stopped without saying why.

`activate.sh` exports the `ACKBAR_*` environment and nothing else. It does **not** activate the
Python venv, which is why commands elsewhere are spelled `.venv/bin/ackbar`.

## What a site file sets

Two are required. `ackbar` refuses to start without them, naming this file in the error.

| Variable | |
|---|---|
| `ACKBAR_SCRATCH_ROOT` | Per-experiment working area. Model run directories live here. **Required** |
| `ACKBAR_OUTPUT_ROOT` | Where experiments live, and how `ackbar <cmd> <name>` resolves a name to a directory. **Required** |

The rest are optional in the sense that ackbar starts without them. Several are optional the way
a parachute is optional, and one is not optional at all: `build-model.sh` refuses without
`ACKBAR_DATASETS_ROOT`, which makes it the first thing to set on a new host.

| Variable | |
|---|---|
| `ACKBAR_STATIC_ROOT` | Surfaces as `$(static_root)`. What the offline stages write and every domain layer reads. A layer naming it when it is unset fails with "unknown symbol", which is the good failure |
| `ACKBAR_DATASETS_ROOT` | Surfaces as `$(datasets_root)`. The MOM6-examples input data mirror; `build-model.sh` symlinks it into the model tree |
| `ACKBAR_ENV_SETUP` | Your spack-stack activation script. Sourced by `activate.sh`, which has no `set -e`, so a wrong path prints an error and carries on into a build that then fails on a missing compiler |
| `ACKBAR_LAUNCHER` | The whole MPI command, e.g. `srun --mpi=pmi2`. Defaults to a bare `srun`. See the traps below |
| `ACKBAR_OFFLINE_LAUNCHER` | The launcher for the offline tools, which run outside Slurm: everything up to and including the rank-count flag, so a tool spells one rank as `$ACKBAR_OFFLINE_LAUNCHER 1`. Defaults to `mpiexec -n`. A machine that forbids MPI on its login nodes sets `srun ... -n`, and the tools then queue instead of running where they were typed |
| `ACKBAR_PARTITION`, `ACKBAR_ACCOUNT`, `ACKBAR_QOS` | Passed through to `sbatch` when set, omitted when not. None is validated; a wrong one is rejected by `sbatch` at `ackbar start` |
| `ACKBAR_MPICC`, `ACKBAR_MPIFC`, `ACKBAR_CFLAGS`, `ACKBAR_FCFLAGS` | The model build's compiler and flags, defaulting to the GNU wrappers and GNU flags. Read only by `build-model.sh`. See "Compilers" below, which is not a preference |
| `ACKBAR_MAX_SUBMIT_JOBS` | Your queue's job limit. `validate` step 5 projects the experiment against it |
| `ACKBAR_MAX_ARRAY_SIZE` | Your queue's maximum array index, checked against the ensemble size |
| `ACKBAR_MPI_TASKS` | Ranks for the offline tools, which run outside Slurm. Defaults to 4; override per invocation with `NTASKS` |
| `ACKBAR_NJOBS` | Build parallelism. Defaults to 4 |
| `ACKBAR_BUILD_TYPE`, `ACKBAR_CMAKE_GENERATOR` | Build only. An empty generator means make |
| `ACKBAR_TEST_ROOT` | Scratch for the Slurm and model smoke tests. **No default**, and both smoke tests run under `set -u`, so omitting it fails them with "unbound variable" |
| `ACKBAR_ROOT` | The checkout jobs run out of. Defaults to the checkout ackbar was imported from; set it only if you mean something else |
| `ACKBAR_SITE` | Recorded in each experiment's `provenance.json`, and what selects this file |

`tools/site-probe.sh` prints all of these at once; the individual queries, if you want them by
hand:

```bash
scontrol show config | grep -E 'MaxJobCount|MaxArraySize'
srun --mpi=list                                  # the launcher flags this site accepts
scontrol show config | grep -i dependency        # see kill_invalid_depend, below
sacctmgr show assoc user=$USER format=account,partition,qos
```

## Compilers

`build-model.sh` defaults to `mpicc`/`mpif90` with `-fallow-argument-mismatch`, which is a GNU
toolchain and nothing else. Two things break on an Intel one, and neither announces itself as
a compiler problem:

- `mpicc` and `mpif90` under Intel MPI wrap **gcc and gfortran**, not `icx` and `ifx`. They
  exist, they work, and they build a GNU MOM6 that then links against intel-built spack-stack
  libraries. The wrappers to name are `mpiicx` and `mpiifx`.
- `-fallow-argument-mismatch` is a gfortran workaround for MPI's non-uniform interfaces. `ifx`
  does not need it and rejects the option outright, so the flags cannot be changed separately
  from the wrappers.

Hence the four variables above, set together or not at all. JEDI is unaffected: `build-jedi.sh`
takes its compilers from the environment spack-stack sets up.

## Porting to an HPC

`site/orion.sh` and `site/hercules.sh` are written and **have never been run**, by someone with
no account on either machine. Treat them as a first draft with its sources named rather than as
a working configuration. What is in them comes from what JCSDA's own workflow does on those
machines: `JCSDA-internal/ewok` (`src/ewok/hosts/slurm.h` is its whole site layer, an `#SBATCH`
header plus `HDF5_USE_FILE_LOCKING` and `SLURM_EXPORT_ENV`), `JCSDA-internal/skylab`, and
`JCSDA-internal/jedi-tools`, whose `buildscripts/setup/*.sh` are where the module loads in
`site/env/` come from. When a JEDI release moves to a newer spack-stack, jedi-tools moves first
and `site/env/` follows it.

The module loads live in `site/env/<machine>.sh` rather than in the site file, because they are
what `ACKBAR_ENV_SETUP` names and because the split is the same one the site file makes
everywhere else: `site/env/` answers "which compiler and libraries", the site file answers
"which paths and queues". A machine whose spack-stack is already reachable through a personal
setup script does not need a file in `site/env/` at all; rancor points `ACKBAR_ENV_SETUP`
straight at one.

Every line either draft cannot know is marked `check:` with the command that answers it. In
rough order of how much they cost to get wrong:

1. **The `_work` line.** One assignment, and every root derives from it. Nothing else in the
   file names a path.
2. **Partition, account and QoS.** Rejected at `ackbar start`, so this one is cheap.
3. **The launcher.** A wrong `--mpi` flag does not fail; see the traps above. This is the one
   to verify with a program that prints `MPI_Comm_size`.
4. **The queue caps.** On a shared machine the cap that bites is the QoS per-user one, not
   `MaxJobCount`.

Two things a site file cannot fix, both worth knowing before the first run:

**Rank counts are tuned for eight cores.** Every domain layer's `resources:` block was written
against rancor, so no task asks for more than 8 ranks and nothing has ever needed a second
node. On a machine with 40 or 80 cores per node those numbers are not wrong, merely small, and
they live in `config/layers/domain/`, not here.

**The offline stages are not portable data.** `ACKBAR_STATIC_ROOT` holds grids, static B and
observation archives built by `tools/`, and an experiment reads them rather than making them.
A new machine has none of it, so the offline stages run there before any experiment does.

## Traps

None of these fails at `validate` time. Each one surfaces later, as a job that behaves oddly
rather than a command that says no.

**A wrong `ACKBAR_LAUNCHER` lies.** The wrong PMI flag does not error. It starts *N* processes,
each its own `MPI_COMM_WORLD` of size one, which write *N* outputs over each other, and Slurm
records the job `COMPLETED`. Verify the launcher with a program that prints `MPI_Comm_size`,
never with an exit code.

**The queue limits default to unchecked.** Leave `ACKBAR_MAX_SUBMIT_JOBS` or
`ACKBAR_MAX_ARRAY_SIZE` out and `validate` step 5 reports ok, because there is nothing to check
against. The real cap then arrives as a rejected `sbatch` inside a running experiment's own
submitter, which stops the cycle chain quietly. That silent stop is the thing the check exists
to prevent, so a site file that omits them has disabled it.

**The venv and the checkout are baked into every job script** at `create` time, as absolute
paths. Both must be visible to the compute nodes at the same path, and moving either invalidates
an experiment already in flight.

**Every job re-sources `activate.sh` on the compute node.** This works because `sbatch`
propagates the exported `ACKBAR_SITE`. On a site that forces `--export=NONE`, jobs die on their
second line.

**`DependencyParameters=kill_invalid_depend`** changes what a failure looks like: with it, a job
whose dependency can never be satisfied is killed rather than left pending. Both behaviours are
handled, but which one you have decides what `ackbar status` shows you. Check before you are
debugging something else.

**Rank counts are not here.** They live in the domain layer, tuned against one machine's core
count. A machine with wider nodes inherits those numbers; there is no site-level override.

## Verifying a new site file

Each step needs strictly more of the machine than the last, so stop at the first that fails.

```bash
.venv/bin/python -m pytest -q                                    # no site, no Slurm, no JEDI
source site/activate.sh                                          # says which file it looked for
.venv/bin/ackbar validate tests/experiments/stub_letkf.yaml --offline
tools/slurm/smoke-test.sh                                        # the scheduler behaviour relied on
.venv/bin/python -m pytest -q -m tier2                           # real Slurm, no JEDI, ~3 min
ACKBAR_TIER3=1 .venv/bin/python -m pytest -q -m tier3            # needs both builds, ~15 min
```

[`docs/slurm.md`](../docs/slurm.md) covers what the smoke test checks and, if this machine has
no scheduler at all, how `tools/slurm/` installs a single-node one.

Two of those steps carry rancor in them. `tools/slurm/smoke-test.sh` names a `debug` partition
and otherwise relies on the default partition and account, so on a machine where those are not
what this account may use it fails on its own second job rather than on anything about ACKBAR;
edit the `#SBATCH` line or read the failure and move on. The tier 3 suite needs both builds and
the offline stages' output, so it is the last thing to try on a new machine, not an early check
that something works.

## Known gaps

`ACKBAR_CAN_SUBMIT_FROM_COMPUTE` is defined and documented as something checked on a new
machine. Nothing reads it. On a site that forbids submission from compute nodes, cycle 1 runs
and cycle 2 is never submitted, because the job that submits the next cycle is itself a compute
job. Check by hand before trusting a long run.
