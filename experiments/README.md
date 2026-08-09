# experiments/

Two families, and the prefix says which.

**`osse-*` is the nature run, at `gom_12km`.** Its product is the truth the experiments below
are scored against, and the synthetic observations they assimilate. `osse-spinup` settles the
domain, `osse-truth` is the run that becomes the truth archive, and `osse-free` is a free run
beside it at the same resolution.

**`osse25-*` is the experiment matrix, at `gom_25km`.** Coarser than the truth on purpose, so
the comparison is a fraternal twin rather than an identical one. `osse25-noda` is the null run,
and every other file in the family is that file with one layer or one key changed. Each says
which in its header, and that is the point of the set: two experiments differ by exactly the
thing being compared.

`osse25-spinup`, `osse25-ensemble-settle` and `osse25-letkf-smoke` are staging rather than
science. The first builds the 25 km control, the second gives a fresh ensemble a free day to
shed its initialization shock, and the third is a short LETKF for checking plumbing before
committing a long run to the queue.

## Copying one

`osse25-3dvar.yaml` for a variational experiment, `osse25-letkf.yaml` for an ensemble filter.

Both name offline products under `$ACKBAR_STATIC_ROOT` that do not exist on a new machine: a
gridspec, a calibrated background error, an initial condition, and an observation archive.
[`../docs/osse.md`](../docs/osse.md) is how those are built, and the `osse-*` files above are
its first stages.
