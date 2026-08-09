# The site layer

One file per machine, and the only place a machine-specific path may appear. Everything else in
the repository is written as though every machine were the same, which is only true because
this directory absorbs the difference.

`site/activate.sh` applies defaults, sources `site/$ACKBAR_SITE.sh`, and exports the result.
`ACKBAR_SITE` defaults to the short hostname, so `site/<hostname>.sh` is the ordinary case. On a
cluster whose login nodes are named separately, set `ACKBAR_SITE` yourself rather than naming
the file after a host you may not land on twice.

Porting ackbar to a new machine is writing one of these. `rancor.sh` is the only one that
exists, so it is both the model and the whole of the prior art: read it alongside this.

```bash
cp site/rancor.sh site/$(hostname -s).sh   # then edit every path in it
source site/activate.sh
```

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
| `ACKBAR_PARTITION`, `ACKBAR_ACCOUNT` | Passed through to `sbatch` when set, omitted when not. Neither is validated; a wrong one is rejected by `sbatch` at `ackbar start` |
| `ACKBAR_MAX_SUBMIT_JOBS` | Your queue's job limit. `validate` step 5 projects the experiment against it |
| `ACKBAR_MAX_ARRAY_SIZE` | Your queue's maximum array index, checked against the ensemble size |
| `ACKBAR_MPI_TASKS` | Ranks for the offline tools, which run outside Slurm. Defaults to 4; override per invocation with `NTASKS` |
| `ACKBAR_NJOBS` | Build parallelism. Defaults to 4 |
| `ACKBAR_BUILD_TYPE`, `ACKBAR_CMAKE_GENERATOR` | Build only. An empty generator means make |
| `ACKBAR_TEST_ROOT` | Scratch for the Slurm and model smoke tests. **No default**, and both smoke tests run under `set -u`, so omitting it fails them with "unbound variable" |
| `ACKBAR_ROOT` | The checkout jobs run out of. Defaults to the checkout ackbar was imported from; set it only if you mean something else |
| `ACKBAR_SITE` | Recorded in each experiment's `provenance.json`, and what selects this file |

Find the two queue limits with:

```bash
scontrol show config | grep -E 'MaxJobCount|MaxArraySize'
srun --mpi=list                                  # the launcher flags this site accepts
scontrol show config | grep -i dependency        # see kill_invalid_depend, below
```

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

## Known gaps

`ACKBAR_CAN_SUBMIT_FROM_COMPUTE` is defined and documented as something checked on a new
machine. Nothing reads it. On a site that forbids submission from compute nodes, cycle 1 runs
and cycle 2 is never submitted, because the job that submits the next cycle is itself a compute
job. Check by hand before trusting a long run.
