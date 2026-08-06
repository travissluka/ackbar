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

`required: true` on an observer inverts the default. Absence then fails the
cycle, which is what an experiment says when the platform is the reason the
experiment exists.
"""

import json
from pathlib import Path

from .config.jobtime import render, symbols

#: ACKBAR's own keys inside an observer, which are ACKBAR's to read and not
#: JEDI's to receive. Removed before the config reaches an application, because
#: a key UFO does not know is a key UFO may reject, and being rejected for a
#: value that was never meant to leave here is a bad way to lose a cycle.
OWN_KEYS = ("required",)


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
            "required": bool(space.get("required")),
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
    a gap in the archive is an error rather than a fact about the archive.
    """
    records = observers(config, cycle)
    missing = [r for r in records if r["required"] and not r["present"]]
    if missing:
        raise ObservationError(
            f"{len(missing)} required observer(s) have no input file for cycle "
            f"{cycle}: " + ", ".join(f"{r['name']} ({r['input']})" for r in missing)
        )
    write(paths, cycle, records)
    return records


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
