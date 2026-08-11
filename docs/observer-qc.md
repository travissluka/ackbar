# Per-application observation QC

**Nothing on this page is implemented.** It is a design, written down so it can be built
later without rederiving it. What exists today is described first, so the gap is visible;
everything from "The design" onward is a proposal.

The problem it solves: a hybrid EnVar wants its variational half to assimilate every
observation and its ensemble half to assimilate a thinned subset. Both halves read the same
observers, so the thinning cannot live in the observer body under UFO's own `obs filters`
key, and it cannot be switched by DA mode alone, because a standalone LETKF and the LETKF
inside a hybrid want different answers.

## Why one filter chain is not enough

The ensemble filter's spread collapses when it assimilates dense observations, sea surface
temperature above all: every member is pulled onto the same L3 field, and a sample
covariance of twenty members that have been pulled together stops describing the error it
is supposed to describe. Thinning the ensemble half is the usual response.

Doing the same to the variational half would be a loss for nothing. A variational solve
does not have a spread to collapse, and the whole reason to run a hybrid is that the static
half can use observations the ensemble half cannot afford. So the two halves want different
observation counts, from the same archive, in the same cycle.

That is one difference. There is a second, and it is the one that rules out the obvious
implementations: **whether the ensemble half thins is a property of the experiment, not of
the platform.** A standalone 4D-LETKF is an analysis in its own right and its observation
count is the experiment being run. A hybrid's ensemble half is a covariance estimator
feeding a variational analysis, and its observation count is a tuning knob. The same
platform, the same instrument, the same mesh, wants thinning in one experiment and not in
the other.

## Where the filters actually run today

Worth being precise about, because the natural guess is wrong in a way that would send an
implementation to the wrong file.

A hybrid cycle reads its observers through two applications, and the filters run in both:

- the variational half, `soca.var_config`, whose filters run inside the cost function of
  `soca_var.x`;
- the ensemble half's **observer**, `soca.member_hofx_config`, whose filters run in
  `soca_hofx.x`, once for the ensemble mean and once per member.

The ensemble half's **solver** runs no filters at all. `soca._solver_observers` drops
`obs filters` outright, and the reason is recorded there: the filters already ran in the
observer, and their verdict reaches the solver as missing values in the merged departure
file's `ObsError`, which `oops::LocalEnsembleSolver` reads as "not assimilated".

So "the LETKF's QC" means the ensemble observer's QC. A filter added to the solver half
would be ignored at best.

One consequence simplifies the whole design. `ensemble_hofx.py` builds the merged file by
copying the **reference** run's `EffectiveError` into `ObsError`, and the reference is the
ensemble mean's observer. Only that one run's verdict becomes the assimilation mask; the
members contribute H(x) columns and nothing else. So a filter added to the ensemble observer
does not have to be proven bit-identical across members, and filters do not remove rows, so
the row-alignment check the merge performs is unaffected.

## What soca-science did, and what it did not

The old bash workflow had a mechanism for this and it is worth knowing both halves of the
story, because the mechanism is remembered as more complete than it was.

`scripts/workflow/subscripts/common.sh`, function `process_OBSERVATIONS <app>`, is a text
preprocessor over the assembled obs YAML:

```bash
sed -i "s/\!IF_APP_$callingApp //g" obs.yaml
sed -i "s/\!IF_DA_$DA_MODE //g" obs.yaml
sed -i "s/\!IF_/#IF_/g" obs.yaml
```

A line prefixed `!IF_APP_letkf ` loses the prefix and becomes live YAML when the caller is
the LETKF; otherwise the third `sed` turns it into a comment. The hybrid calls the function
twice, once per leg, over the same per-platform sources. Two axes, application and DA mode,
which is the right decomposition.

What was actually built on it:

- `obs localizations`, LETKF only, in every platform file;
- obs error inflation at 3.0 on SST, LETKF only, and **only** in the `hat10` regional files.
  This is the sole per-solver filter difference anywhere in that repository;
- separate observation *type* lists, `DA_LETKF_OBS_LIST` a subset of `OBS_LIST_OCN`, plus a
  shorter LETKF window.

What was not:

- **Thinning was never solver-switched.** Every SST and SSS platform ends with a
  hand-commented `Gaussian_Thinning` block and the note "having problems with the LETKF, try
  again later". The only live thinning in the repository is in `oc_snpp.yaml`, unguarded, so
  it ran in the LETKF too.
- **`!IF_DA_` is used in zero config files.** The mode axis, the one that would have
  distinguished a standalone LETKF from a hybrid's, exists entirely in the plumbing.
- **`!IF_APP_VAR` never fires.** It is uppercase in every platform file and `run.var.sh`
  passes lowercase `var`, so the `sed` does not match and the third pass comments the line
  out. Dead configuration in every observation file, and nothing ever reported it.

That last point is the one to carry forward as a requirement rather than a curiosity. A
name-based selector with no validation produces configuration that silently does nothing,
and it can survive for years.

## The design

Two keys. The platform declares named fragments and applies none of them; the DA layer
selects which fragments each application appends.

The mode axis costs nothing here, because in ACKBAR **the layer is the mode**:
`config/layers/da/letkf.yaml` is a standalone LETKF and `config/layers/da/hybrid.yaml` is a
hybrid, so a key stated in one is not stated in the other. Only the application axis needs
naming.

### The platform half

In the observer family, beside the shared chain, e.g. `config/layers/obs/common/sst.yaml`:

```yaml
    obs filters:
    - filter: Domain Check          # the shared chain, every application
      ...

    # Appended after the chain above when a solver layer selects them by name.
    # What these are is a property of what measured it; whether they apply is a
    # property of the experiment. See `solver.observer qc`.
    $qc:
      thinned:
      - filter: Gaussian_Thinning
        horizontal_mesh: 50.0
        use_reduced_horizontal_grid: false
      inflated:
      - filter: Perform Action
        action: {name: inflate error, inflation factor: 3.0}
```

`$qc` is an ACKBAR sigil key, exactly parallel to `$localization` and for the same reason:
the same observer body is handed to applications that must not see it, and a key UFO does
not expect is a key UFO may reject. It is declared on the platform because a thinning mesh
is a property of the instrument, dense L3 SST wanting one that along-track altimetry and
Argo profiles do not.

### The solver half

In the DA layer, beside the ensemble block it already carries:

```yaml
solver:
  observer qc:
    variational: []
    ensemble: [inflated, thinned]
```

Three applications are selectable:

| name | builder | where its filters run |
|---|---|---|
| `variational` | `soca.var_config` | inside `soca_var.x`'s cost function |
| `ensemble` | `soca.member_hofx_config` | `soca_hofx.x`, mean and each member |
| `hofx` | `soca.hofx_config`, `soca.hofx4d_config` | the verification observer |

The ensemble filter's solver half is deliberately absent from that table. It drops
`obs filters` wholesale, for the reason given above.

`hofx` is selectable for symmetry rather than because anything should use it. Verification
scores an experiment against the observations it did not necessarily assimilate, so leaving
it empty is almost always right.

### The three cases it has to express

| want | how |
|---|---|
| different QC for any LETKF | `observer qc: {ensemble: [...]}` in `da/letkf.yaml` |
| variational differs from the ensemble half | both keys in one table, in one layer |
| only the hybrid EnVar's ensemble half | `observer qc: {ensemble: [...]}` in `da/hybrid.yaml` |

The third is the case that rules out the simpler designs. A standalone LETKF taking
`[inflated]` while a hybrid takes `[inflated, thinned]` needs the fragments to be *named*,
because the difference is in content and not merely in on and off. A single list plus a
boolean gate cannot reach it.

`config/layers/da/envar.yaml` inherits `da/hybrid`, so a pure EnVar takes the hybrid's
selection. That is intended: it is still an ensemble filter feeding a variational analysis.
`da/eakf.yaml` inherits `da/letkf` and so takes the standalone selection.

### Rules

Fixed now, because a list-shaped key grows ambiguous later otherwise.

- **Appended, never replacing**, after the shared chain, in the order the layer lists the
  names. Both known uses need that order: thinning must see the survivors of the quality
  checks, and inflating an error that a Domain Check is about to reject is wasted work.
  Appending is also what keeps the applications provably comparable: the ensemble half's
  chain is the variational half's chain plus what was selected, so the two cannot silently
  diverge on something like the land mask.
- **The selector is names, not filters.** A DA layer stating filter bodies per platform is
  the anti-pattern recorded in `soca._observers` and in the schema: it invents a phantom
  observer for every platform the experiment does not carry, and leaves every platform it
  does not name untouched.
- **A selected name that no platform declares is an error**, raised by validation, not a
  silent no-op. This is the `!IF_APP_VAR` lesson above and it is not optional.
- **A declared but unselected fragment is fine**, and so is a platform with no `$qc` at all
  while a layer selects a name some other platform declares. Only SST is expected to need
  thinning, and requiring every platform to declare every name would be noise.
- **No "before the shared chain" variant.** Neither known use wants it. If something ever
  genuinely does, it is a second key and should be argued for then.

Collapse behaviour is the property that makes this safe to land ahead of any experiment
using it: no `$qc` and no `observer qc` is exactly today's behaviour, so nothing changes for
an experiment that does not opt in.

## Implementation notes

- The single funnel is `soca._observers`. Every application's observer list already passes
  through it, and it already carries both a per-application flag (`localize=`) and a
  per-application value (`distribution`), and already renders one sigil key to a UFO key.
  This is the same shape a third time.
- `soca.member_hofx_config` is the call site that matters. The obvious wrong target is
  `soca._solver_observers`, which is where the LETKF appears in the code but not where its
  filters run.
- The `$qc` constant belongs in `observations.py` beside `LOCALIZATION`, not in `soca.py`.
  The module docstring there records why: `graph.build` reads such keys and cannot import
  `soca`. It also has to be added to `strip_own_keys`, so it can never reach a UFO document
  through an application that did not select it.
- Schema: `$qc` beside `$localization` in `config/schema/experiment.yaml`, an object whose
  values are arrays of objects each requiring `filter`; and `solver.observer qc`, an object
  with the three application names as keys and arrays of strings as values. The observer
  schema is `additionalProperties: true`, so an undeclared key means a typo passes silently.
- Name validation has a precedent to copy in shape: `config/bodies.py` raises `BodyError`
  when an observer names a body nobody declared. The same check for selected QC names
  belongs with the other observation steps in `validate.py`.
- `docs/analysis.md` wants a line next to the existing note about the solver dropping
  filters.

## The caveat that comes with using it

`da/hybrid.yaml` and `da/letkf.yaml` go to real trouble to be equal by construction: the
same inflation, the writeback bounds in a shared file, observation-space localization pushed
into the observer body specifically so the two solvers read the same numbers and cannot
drift apart. The stated reason is that a filter experiment and a hybrid one are set against
each other to isolate the covariance, so they must differ by that and by nothing else.

Selecting different QC between them deliberately breaks that invariant. The justification is
the asymmetry at the top of this page, that the hybrid's ensemble half is a covariance
estimator rather than an analysis, and it is a real justification rather than a
rationalisation. But it means a hybrid-versus-LETKF comparison then differs by two things,
and a table of results carries no note saying so.

So the selection lands with a comment in both layers recording that the difference is
intentional and why, in the same voice as the inflation comment already there. Whoever reads
the comparison a year later needs to find it without reading this page.
