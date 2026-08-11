# Testing

Five tiers, defined in [`build-order.md`](build-order.md) along with which phase each part of
the workflow is verified at. This document is the operational half: what to run, when, and what
each tier can and cannot catch.

The tiers exist so that "did I break it" has a cheap answer most of the time. Tiers 0 through 2
are the regression suite and run before every commit. Tier 2 is the important one: it is the
only cheap test that exercises arrays, dependency edges and failure recovery, which is where
the risk actually is.

```bash
.venv/bin/python -m pytest -q                          # tiers 0 and 1, a few seconds
source site/activate.sh
.venv/bin/python -m pytest -q -m tier2                 # about 3 minutes
ACKBAR_TIER3=1 .venv/bin/python -m pytest -q -m tier3  # about a quarter of an hour
```

Everything runs out of the checkout's own venv. Tiers 0 and 1 deliberately do not need
`source site/activate.sh`: `conftest.py` pins a fake site for them, so a test that passed
because of where this machine keeps its scratch would be a test of this machine.

## Tier 0 and tier 1

Python only. No scheduler, no JEDI, no model, no input data, which is the point of the phase
ordering in `build-order.md`.

Tier 0 is the unit suite: the layer merge, substitution, the schema, duration arithmetic, path
construction, the document builders, and the reductions in `post.py`. Tier 1 is `validate` and
the graph goldens over the fixture experiments in `tests/experiments/`.

The goldens under `tests/goldens/` are one line per node and per edge so that a diff is
readable. Regenerate them with

```bash
ACKBAR_UPDATE_GOLDENS=1 .venv/bin/python -m pytest tests/test_graph.py
```

and read the diff rather than accepting it. A golden that changed for a reason nobody can state
is a graph change nobody intended.

`tests/test_obs_order.py` pins the other kind of join, the one between two tasks: every
observation file a graph's tasks hand to an observer has to be staged by a task that is an
ancestor of the reader, or by the reader itself. It exists because a tier 0 and tier 1 suite
that was entirely green missed `hofx.ext` reading windows that no cycle had staged yet, on
every cycle of every experiment with an extended forecast. Nothing there was wrong about
`stage.obs`; the gap was that every test of the observation paths was a test of a task reading
its own cycle's window. A test written against one task's paths would miss the next one of
these, so this one is written against the graph.

`tests/test_templates.py` pins both halves of the join between `config/soca/` and
`ackbar/soca.py`: a slot in a template with nothing to fill it is an application that dies on a
config value, and a value computed for a slot the template does not have is a block that has
gone missing. Nothing at run time compares the two until the application is about to launch,
and for `recenter` that is after two analyses have already run.

## Tier 2: the stub workflow

Needs a real Slurm and the real site. No JEDI and no model.

`model/stub` writes plausible bytes and sleeps, so a twenty member three cycle experiment costs
one core and a few seconds per job. What tier 2 tests is everything around the science: job
arrays, `afterok` and `aftercorr` edges, the ledger, the submitter chain, cleanup, status, and
healing.

```bash
source site/activate.sh
.venv/bin/python -m pytest -q -m tier2                      # about 3 minutes
ACKBAR_TIER2_FAST=1 .venv/bin/python -m pytest -q -m tier2  # about 1, drops requeue
```

Most of those three minutes is one thing: Slurm defers a requeued job about two minutes before
it is eligible again. `ACKBAR_TIER2_FAST=1` skips the requeue case for iterating, and is the
wrong thing to run before a commit, since requeue is the one fault the scheduler inflicts on
its own without being asked.

Failures are configuration rather than an afternoon nobody can repeat. `model.stub.fail` names
jobs by `<cycle>.<task>[.<member>]`, each field a glob:

```yaml
model:
  stub:
    seconds: 3
    fail:
      exit_nonzero: ['2.forecast.7']
      overrun_time: ['*.da.*']
```

The same job takes the same fault every time, so a healed attempt reproduces the failure it is
meant to fix rather than passing by luck. `tests/experiments/stub_letkf.yaml` is the workflow
test case.

## Tier 3: the real thing

Needs `ACKBAR_TIER3=1`, the built `coupler_main`, the built JEDI bundle, the offline initial
condition each experiment names, and, for anything with observers, the observation archive and
the domain's static stage.

```bash
source site/activate.sh
tools/soca-gridspec.sh gom_25km        # once per domain, and after a bundle bump
ACKBAR_TIER3=1 .venv/bin/python -m pytest -q -m tier3
```

All of it runs on `gom_25km`, where a simulated day costs six seconds against 178 at `om_1deg`
and nothing under test is global-only.

### What earns a tier 3 slot

**An experiment belongs in tier 3 only if it can fail in a way that needs MOM6 actually
integrating.** Everything else that needs real SOCA belongs on `model/persistence`, which runs
the real analysis applications against a background that does not move, and everything that
needs neither belongs in tier 2 or below.

That rule is why the covariance experiments moved. A hybrid's assertions are about document
contents, output collisions and recentring arithmetic, none of which the model participates in;
running one on persistence keeps every assertion and removes 42 forecasts. What stays on the
real model is the restart chain, the things only a regional domain can be asked, and the two
cases whose input is a trajectory.

| Experiment | Model | What only it can fail on |
|---|---|---|
| `tier3_gom` | mom6sis2 | restart continuity, reproduce-after-kill, the open boundary, ackbar's overrides reaching the model, sub-window state writes |
| `tier3_hofx` | mom6sis2 | observers against a real background, over an archive with a deliberate hole in it |
| `tier3_var` | mom6sis2 | a real analysis written back into a restart set and integrated forward |
| `tier3_letkf` | mom6sis2 | a member array carrying a real model through a filter |
| `tier3_fgat` | mom6sis2 | a pseudo model stepping a trajectory the previous cycle wrote |
| `tier3_4denvar` | mom6sis2 | an ensemble whose members are real trajectories |
| `tier3_fcst` | mom6sis2 | extended forecasts, their leads, and their departures |
| `tier3_var_persist` | persistence | the analysis document and its products, cheaply |
| `tier3_envar` | persistence | the ensemble covariance |
| `tier3_hybrid` | persistence | two covariances in one cycle, and recentring |
| `tier3_eakf` | persistence | the sequential solver's own configuration |

`tier3_diffusion` is separate: it calibrates and checks the static B rather than cycling
anything.

### Reading a tier 3 failure

The fixtures purge their experiment directory on teardown, which destroys the evidence a
failure leaves behind. When a tier 3 test fails for a reason the assertion does not explain,
recreate the experiment by hand and read the logs:

```bash
source site/activate.sh
sed 's/name: tier3_fgat/name: look_fgat/' tests/experiments/tier3_fgat.yaml > /tmp/look.yaml
ackbar create /tmp/look.yaml && ackbar start look_fgat
ackbar status look_fgat --verbose
```

`run/<date>/log/` holds what the application actually said. A SOCA application that aborts on
an assertion has usually done everything up to that point correctly, so read the whole log
rather than the tail.

## Tier 4

Production scale on `OM4_025`, on a real HPC, per release. Nothing here runs it.

## What the tiers do not cover

- **Science.** Nothing in the suite asserts that an analysis is *good*, only that it is the
  analysis the configuration described. Skill needs a truth run, which is [`osse.md`](osse.md).
- **Another SOCA.** Every JEDI-facing assertion is against the pinned bundle under `pkg/jedi`.
  A bundle bump is a tier 3 run, not a tier 0 one.
- **Another machine.** Tier 2 and tier 3 run against this site's Slurm. `docs/slurm.md`
  describes the two dependency profiles they are tested under, which is the closest thing to a
  portability test there is.
