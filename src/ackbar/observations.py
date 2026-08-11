"""Which observers actually ran, and where their files came from.

The in-cycle observation step is deliberately small. There is no downloading and
no conversion: an archive built offline already holds one file per platform per
window, and this reduces to finding the file this window needs and noticing when
it is not there. See Observations in `docs/design.md` for why that is the whole
job.

What is *not* small is the consequence of a file being absent. An observer whose
input is missing is dropped and the cycle continues, which is the right
behaviour for a fifty cycle experiment over a real archive with real gaps, and
it also means the observation set varies from cycle to cycle without anything
saying so. Two experiments that differ in which observers actually ran is the
difference that most distorts a comparison between them, and it is invisible in
both configurations, because both configure the same observers.

So the realized list is written per cycle, as a file, next to the observation
output it describes. It records every configured observer, whether it ran, and
what it read: the ones that were dropped are the point, so they are in the file
rather than absent from it.

`$required: true` on an observer inverts the default. Absence then fails the
cycle, which is what an experiment says when the platform is the reason the
experiment exists.

What is *not* a gap is a window in which no observer at all has a file. Nothing
downstream can tell that cycle from a healthy one: the analysis is skipped
because there is nothing to assimilate, the writeback copies the background
forward, and `post.obs` writes a document of zeros and passes. So `realize`
refuses it, and the reason it can is that the archive is an offline product
whose other windows are on disk to be compared against. See `_archive_covers`.
"""

import json
from pathlib import Path

from .config.jobtime import render, symbols

#: Whether a missing input file fails the cycle. See the module docstring.
#:
#: The sigil is the convention `merge.REMOVE` set and `bodies.BODY` follows:
#: everything else inside an observer is UFO's, so a key of ACKBAR's sitting
#: among `obs operator` and `obs filters` says so in its name. It also stops
#: this reading as JSON Schema's `required` two lines from where the schema
#: uses that word for its own purpose.
REQUIRED = "$required"

#: ACKBAR's own keys inside an observer's `obs space`, which are ACKBAR's to
#: read and not JEDI's to receive. Removed before the config reaches an
#: application, because a key UFO does not know is a key UFO may reject, and
#: being rejected for a value that was never meant to leave here is a bad way to
#: lose a cycle. `LOCALIZATION` is not among them: it sits at the top of an
#: observer rather than inside `obs space`, and `soca._observers` consumes it.
OWN_KEYS = (REQUIRED,)

#: An observer's own observation-space localization, which only an ensemble
#: filter reads. ACKBAR's key rather than UFO's `obs localizations`, because the
#: same observer body is handed to applications that do not localize, and a key
#: UFO does not expect there is a key UFO may reject.
#:
#: Here rather than in `soca.py`, which is what renders it, because it is also
#: what `graph.build` looks for when it asks whether a cycle running an ensemble
#: filter has localized anything, and `graph` cannot import `soca`.
LOCALIZATION = "$localization"


class ObservationError(Exception):
    pass


def observers(config, cycle):
    """Every configured observer for one cycle, rendered and classified.

    Returns a list of records in configuration order. `present` is the answer to
    the only question this module asks the filesystem, and it is asked once,
    here, rather than by each of the things that later want to know.
    """
    table = symbols(config, cycle)
    records = []
    for entry in config.get("observations") or ():
        rendered = render(entry, table)
        space = rendered.get("obs space") or {}
        name = space.get("name", "")
        source = _obsfile(space, "obsdatain")
        records.append({
            "name": name,
            "required": bool(space.get(REQUIRED)),
            "input": source,
            "output": _obsfile(space, "obsdataout"),
            "present": bool(source) and _exists(source),
            "config": strip_own_keys(rendered),
        })
    return records


def _obsfile(space, side):
    return ((space.get(side) or {}).get("engine") or {}).get("obsfile", "")


def _exists(path):
    return Path(path).exists()


def strip_own_keys(entry):
    """An observer as JEDI should see it: ACKBAR's own keys removed."""
    space = dict(entry.get("obs space") or {})
    for key in OWN_KEYS:
        space.pop(key, None)
    out = dict(entry)
    out["obs space"] = space
    return out


def realize(config, paths, cycle):
    """Decide the cycle's observer set and write the list. Returns the records.

    Raises if a required observer's file is missing, which is the one case where
    a gap in the archive is an error rather than a fact about the archive, and
    if no observer has a file at all. See `_no_observations` for why those are
    different questions.
    """
    records = observers(config, cycle)
    missing = [r for r in records if r["required"] and not r["present"]]
    if missing:
        raise ObservationError(
            f"{len(missing)} required observer(s) have no input file for cycle "
            f"{cycle}: " + ", ".join(f"{r['name']} ({r['input']})" for r in missing)
        )
    if records and not any(r["present"] for r in records):
        raise ObservationError(_no_observations(config, cycle, records))
    write(paths, cycle, records)
    return records


def _no_observations(config, cycle, records):
    """Why a cycle with no observations at all is a failure, and which one it is.

    **An assimilating experiment cannot tell a cycle it chose not to assimilate
    from one it was unable to.** Both run green from end to end: `stage.obs`
    drops every observer and exits 0, `run.analysis_state` answers None so the
    analysis is skipped, `writeback` copies the background into `ana/`, and
    `post.obs` writes zero of zero and passes its own all-rejected check, which
    is about observations that were read. Three cycles of that is a free run
    inside an experiment that reports as an assimilation.

    What separates the two is not in this cycle. The observation archive is an
    offline product built before the experiment starts, so every other window it
    holds is on disk to be asked, and one platform being down is a gap in one
    platform while the rest of the archive answers. Every platform absent at the
    same instant is not an outage: it is a window that was never built, or a
    path that resolves to nothing. `_archive_covers` asks which.

    Deliberately no way to say "an empty cycle is fine here". The case does not
    exist yet: every archive this runs on is generated per window by
    `tools/obs-*`, so an empty window is a hole in a thing that was supposed to
    be complete. An experiment that genuinely wants to assimilate nothing for a
    stretch says so by not configuring observers, or by not running those cycles.
    """
    names = ", ".join(record["name"] for record in records)
    elsewhere = _archive_covers(config, cycle)
    if elsewhere is None:
        return (
            f"cycle {cycle} has no observation file for any of its "
            f"{len(records)} observers ({names}), and neither does any other "
            f"cycle of this experiment. That is a path that resolves to nothing "
            f"or an offline stage that never ran, not a gap in the archive. "
            f"`ackbar validate` reports it per observer before anything is "
            f"submitted; reaching it here means the archive moved after the "
            f"experiment was created.")
    return (
        f"cycle {cycle} has no observation file for any of its {len(records)} "
        f"observers ({names}), while cycle {elsewhere} has one. A platform goes "
        f"down on its own and the rest of the archive answers; all of them at "
        f"one instant is a window that was never built, or an experiment whose "
        f"period runs past the archive it names. Left to run, this cycle "
        f"assimilates nothing and every task in it reports success. Fill the "
        f"window, or shorten the experiment, then heal this cycle.")


def _archive_covers(config, cycle):
    """Another cycle of this experiment whose window some observer has a file for.

    None when there is none. Rendering every cycle's paths and statting them is
    the same question `validate._observation_step` asks up front, asked again
    here because an archive can be moved or half-rebuilt after an experiment is
    created, and because `ackbar validate --offline` never asked it at all.

    Only on the way to failing, so its cost is a few hundred `stat` calls in a
    cycle that is about to stop.
    """
    for other in range(1, int(config["cycle"]["count"]) + 1):
        if other == cycle:
            continue
        if any(record["present"] for record in observers(config, other)):
            return other
    return None


def write(paths, cycle, records):
    """The realized observer list, committed by rename like any other output."""
    payload = {
        "cycle": cycle,
        "observers": [
            {k: v for k, v in record.items() if k != "config"} for record in records
        ],
    }
    target = paths.observer_list(cycle)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    temp.replace(target)
    return target


def read(paths, cycle):
    """The realized list as `stage.obs` left it.

    Read rather than recomputed, so that what hofx evaluates is what the staging
    step decided and recorded. Recomputing would mean a file that appeared in
    the archive between the two jobs changes the observer set without changing
    the list that documents it.
    """
    target = paths.observer_list(cycle)
    if not target.exists():
        raise ObservationError(
            f"{target} does not exist, so no observer set was ever decided for "
            f"cycle {cycle}. That file is `stage.obs`'s output and hofx's input."
        )
    return json.loads(target.read_text())["observers"]


def selected(config, paths, cycle):
    """The observers hofx should evaluate: configured, and staged as present.

    Both halves matter. The configuration carries the observer bodies, which are
    too large to duplicate into the realized list; the realized list carries the
    decision, which is the thing that must not be made twice.
    """
    names = {r["name"] for r in read(paths, cycle) if r["present"]}
    return [r for r in observers(config, cycle) if r["name"] in names]
