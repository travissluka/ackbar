# config/

Six directories, and the useful distinction between them is **who reads a file
and when**. Nothing here declares its own kind; the directory is the kind, and
the loader enforces it by only looking in one place for each.

| | Read by | When |
|---|---|---|
| `layers/` | `ackbar create`, merged into an experiment | experiment time |
| `soca/` | `ackbar/soca.py`, filled and handed to a JEDI application | task time, every cycle |
| `schema/` | `ackbar validate`, against the merged config | experiment time |
| `model/` | MOM6-SIS2, staged into the run directory | every forecast |
| `obs/` | UFO, named by an observer layer | every analysis |
| `static/` | the offline `tools/`, before any experiment exists | once per domain |

## layers/

What an experiment inherits, in `<kind>/<name>.yaml`. Deep-merged in the order
the experiment lists, later wins. A layer may inherit other layers; a
`<kind>/common/` directory holds the ones that are *only* ever inherited and
never listed by an experiment, and none of those is a complete anything on its
own.

Mostly ACKBAR's own keys, with two subtrees passed through verbatim to JEDI: an
observer below `obs space`, and the background error blocks under `solver`. That
mixture is why a handful of keys carry a `$` prefix. It marks a key ACKBAR reads
and JEDI never sees, so that it cannot be mistaken for something UFO will act
on: `$remove`, `$inherit`, `$required`, `$localization`. Keys at the root of a
merged config take no sigil, that whole level being ACKBAR's.

See the README at the repository root for how merging and substitution work.

## soca/

One document template per SOCA application, holding the blocks, their names,
their nesting, and the reasons for them. `ackbar/soca.py` fills the
`$(UPPERCASE)` slots and writes the result beside the job's log, which is the
copy that says what an application actually ran.

**A value Python also reads is never written here, only slotted.** Two spellings
of a filename field is a `writeback` that finds nothing, and it is the mistake
every previous workflow made. `ackbar/config/template.py` refuses a slot the
template does not use, so the two cannot drift apart silently.

`create` freezes a copy of this directory into each experiment, so editing a
template cannot change a run already in flight.

## schema/

ACKBAR's own schema for a fully merged experiment, doing two jobs in one
document: validating the keys ACKBAR owns, and declaring how lists merge via
`x-ackbar-merge-key`. Both in one file so the merge rules cannot drift from the
shape they describe.

Deliberately uneven. Where ACKBAR owns the keys, an unknown one is an error;
where the subtree is verbatim JEDI config, unknown keys pass. This schema
describes ACKBAR's config and is not a model of OOPS.

## model/, obs/, static/

`model/` is the stock MOM6-SIS2 case: namelists, parameter files, and the
`MOM_override` that makes a domain its resolution. Files a model needs that are
ACKBAR's rather than the case's.

`obs/` holds files an observer names, currently the operator alias map. UFO's,
not ACKBAR's, and referenced absolutely so `ackbar validate` stats it before
anything is submitted.

`static/` is parameters for the offline stages, read by `tools/` and by nothing
in the workflow. An experiment names the *product* of a file here, never these
numbers, so that every experiment on a domain compares analyses against one
background error rather than against whatever its own configuration described.
