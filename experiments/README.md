# experiments/

Two families, and the prefix says which.

**`osse-*` is the nature run, at `gom_12km`.** Its product is the truth the experiments below
are scored against, and the synthetic observations they assimilate. `osse-spinup` settles the
domain and `osse-truth` is the run that becomes the truth archive. The truth reads ERA5, a
reanalysis, which is one half of what makes this a fraternal twin.

**`osse25-*` is the experiment matrix, at `gom_25km`.** Coarser than the truth on purpose, and
forced by a GEFS forecast rather than the truth's own reanalysis, so the analysis has to cope
with resolution error and forcing error the way it would in production. `osse25-noda` is the
null run, and every other file in the family is that file with one layer or one key changed.
Each says which in its header, and that is the point of the set: two experiments differ by
exactly the thing being compared.

`osse25-spinup` and `osse25-ensemble-settle` are staging rather than science. The first builds
the 25 km control; the second gives a fresh ensemble a free day to shed its initialization
shock.

## The comparison

Eight arms, all 20 cycles from the same start, the same initial conditions and the same
observation archive, so every difference at every lead is a paired one.

| arm | solver | window |
|---|---|---|
| `osse25-noda` | none | n/a |
| `osse25-3dvar` | variational, static B | 3D |
| `osse25-3dfgat` | variational, static B | FGAT |
| `osse25-4dletkf` | ensemble filter | 4D |
| `osse25-envar` | variational, ensemble B | FGAT |
| `osse25-4denvar` | variational, ensemble B | 4D |
| `osse25-hybrid` | variational, hybrid B | FGAT |
| `osse25-4dhybrid` | variational, hybrid B | 4D |

The FGAT and 4D rows are pairs on one question, whether the covariance gets a time dimension,
asked once over a pure ensemble and once over a hybrid. A 3D ensemble filter is deliberately
absent: it is known to trail the 4D one, which costs almost nothing more.

The five arms with an ensemble inherit `ensemble/perturbed-inputs`, which gives every member
its own atmosphere and its own open boundary. Without it the prior's spread decays every cycle
and no relaxation coefficient stops it; [`../docs/ensemble-spread.md`](../docs/ensemble-spread.md)
is the reference.

## Sweeps beside the matrix

`osse25-4dletkf-vloc15` is not a ninth arm. It is `osse25-4dletkf` with the surface platforms'
vertical localization raised from ten model levels to fifteen, and it is scored against that
arm rather than against the matrix, which is why the arm itself is never edited. Its header
carries the mixed layer measurement the number comes from and what Gaspari-Cohn does with it.

## Copying one

`osse25-3dvar.yaml` for a variational experiment, `osse25-4dletkf.yaml` for an ensemble filter.

Both name offline products under `$ACKBAR_STATIC_ROOT` that do not exist on a new machine: a
gridspec, a calibrated background error, an initial condition, a forcing archive, and an
observation archive. [`../docs/osse.md`](../docs/osse.md) is how those are built, and the
`osse-*` files above are its first stages.
