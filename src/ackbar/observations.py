"""Which observers actually ran, and where their files came from.

The in-cycle observation step is deliberately small. There is no downloading and
no conversion: an archive built offline already holds every observation, filed
in fixed time bins that know nothing about any cycle, and this reduces to
joining the bins this window touches and noticing when there are none. See
Observations in `docs/design.md` for why that is the whole job, and
`ackbar/obsarchive.py` for the layout, the selection rule, and the bug that
produced both.

The join is the one piece of real work here, and it is the reason the archive is
allowed to be window agnostic. The alternative, an archive already cut into
windows, put a file boundary exactly where an observation could fall through:
ioda keeps `(begin, end]`, so an observation at the instant a window opens is
dropped by that window and was in no other window's file.

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

What is *not* a gap is a window in which no observer at all has anything. Nothing
downstream can tell that cycle from a healthy one: the analysis is skipped
because there is nothing to assimilate, the writeback copies the background
forward, and `post.obs` writes a document of zeros and passes. So `realize`
refuses it, and the reason it can is that the archive is an offline product
whose other windows are on disk to be compared against. See `_archive_covers`.

**A lead window is not a cycle for that purpose.** `hofx.ext` reads the windows
of the cycles its forecast reaches, several of them at once, and one of them
having nothing costs a score at one lead and changes nothing else: there is no
state to carry forward wrongly, no analysis to skip, and every experiment in a
comparison reads the same archive, so the same lead is missing from all of them
or from none. The refusal exists for the failure that is *invisible*, and this
one is a departure file that is not there. So `stage_lead` drops an empty lead
window and says so, and `realize` is the only thing that refuses.

`$required` is read the same way and for the same reason. It says this platform
is why the experiment exists, which is a statement about what gets assimilated,
so it fails a cycle and not a lead: a verification stopped because an altimeter
has a gap on day four costs the score at every other lead as well.
"""

import json
from pathlib import Path

from . import obsarchive
from .config.jobtime import render, symbols, window_bounds

#: Where this platform's bins live: a directory of the observation archive, one
#: level down from what an experiment sets `obs_dir` to. ACKBAR's own key, for
#: the reason `REQUIRED` is: JEDI is handed the joined file and never the
#: archive, so a key naming the archive must not reach it.
ARCHIVE = "$archive"

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
OWN_KEYS = (REQUIRED, ARCHIVE)

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
    here, rather than by each of the things that later want to know. It is a
    question about the *archive*, not about the staged file: the staged file is
    this cycle's own output and does not exist until `realize` has written it,
    so asking after it would make every observer absent until the moment it is
    not.

    `sources` is the bins the join will read, in time order, and it is carried
    on the record rather than recomputed by the staging step so that the
    decision and the work cannot disagree about what the window touches.

    **What makes an observer present is an observation in the window, not a file
    on disk.** Under a window agnostic archive those are different questions: the
    selection rule reaches back to the last bin at or before the window, so a
    file is nearly always found, and an archive that stopped half way through an
    experiment would otherwise keep every observer for every later cycle, hand
    each one a file with nothing inside its window, and record a full observing
    system that assimilated nothing. So the times are read and counted, which is
    what `window rows` is.
    """
    table = symbols(config, cycle)
    begin, end = table["window_begin"], table["window_end"]
    records = []
    for entry in config.get("observations") or ():
        rendered = render(entry, table)
        space = rendered.get("obs space") or {}
        name = space.get("name", "")
        archive = space.get(ARCHIVE) or ""
        sources = obsarchive.window(archive, begin, end) if archive else []
        found = obsarchive.count_inside(sources, begin, end) if sources else 0
        records.append({
            "name": name,
            "required": bool(space.get(REQUIRED)),
            "archive": archive,
            "sources": [str(path) for path in sources],
            "input": _obsfile(space, "obsdatain"),
            "output": _obsfile(space, "obsdataout"),
            "window rows": found,
            "present": bool(found),
            "config": strip_own_keys(rendered),
        })
    return records


def _obsfile(space, side):
    return ((space.get(side) or {}).get("engine") or {}).get("obsfile", "")


def strip_own_keys(entry):
    """An observer as JEDI should see it: ACKBAR's own keys removed."""
    space = dict(entry.get("obs space") or {})
    for key in OWN_KEYS:
        space.pop(key, None)
    out = dict(entry)
    out["obs space"] = space
    return out


def realize(config, paths, cycle):
    """Stage the cycle's observations, decide the observer set, write the list.

    Raises if a required observer has nothing in this window, which is the one
    case where a gap in the archive is an error rather than a fact about it, and
    if no observer has anything at all. See `_no_observations` for why those are
    different questions.

    The join happens before the list is written, so the list never claims an
    observer is present on the strength of a file the join then failed to
    produce.
    """
    begin, end = window_bounds(config, cycle)
    records = observers(config, cycle)
    nameless = [r for r in records if not r["archive"]]
    if nameless:
        raise ObservationError(
            f"{len(nameless)} observer(s) name no {ARCHIVE} for cycle {cycle}: "
            + ", ".join(r["name"] for r in nameless)
            + f". Every observer layer names the archive directory its platform's "
            f"time bins are in; see src/ackbar/obsarchive.py."
        )
    missing = [r for r in records if r["required"] and not r["present"]]
    if missing:
        raise ObservationError(
            f"{len(missing)} required observer(s) have no observation in the "
            f"window of cycle {cycle}: "
            + ", ".join(f"{r['name']} ({r['archive']})" for r in missing)
        )
    if records and not any(r["present"] for r in records):
        raise ObservationError(_no_observations(config, cycle, records))
    for record in records:
        if record["present"]:
            stage(record, begin, end)
    write(paths, cycle, records)
    return records


def stage(record, begin, end):
    """Join one observer's bins into the file its `obsdatain` names.

    Sets `rows`, the size of the staged file, which is larger than `window rows`
    on purpose: a bin is not cut to fit a window, so the staged file is a little
    wider than the window at each end and ioda makes the single cut. Cutting
    here as well would put the half open rule in two places, and the second copy
    is the one that would be wrong.
    """
    try:
        record["rows"] = obsarchive.concatenate(record["sources"],
                                                Path(record["input"]))
    except obsarchive.ArchiveError as error:
        raise ObservationError(f"{record['name']}: {error}") from error
    return record


def redirect_input(record, path):
    """Point one observer at a file the reading task stages for itself.

    The sibling of `soca._redirect_output`, and here rather than there because
    `input` is this module's key: the record and the document JEDI is handed
    have to name one file, and a caller that set only one of them would leave
    the other pointing at a path nothing writes.
    """
    record["input"] = str(path)
    record["config"]["obs space"]["obsdatain"]["engine"]["obsfile"] = str(path)
    return record


def stage_lead(record, begin, end):
    """Join and cut one lead window's observations into `record["input"]`.

    What `stage.obs` does for its own cycle, done by a task that needs a window
    which is not its own. `hofx.ext` is the only caller and the reason is the
    ordering: it evaluates a five day forecast against the windows of the cycles
    that forecast reaches, and those cycles have not run, so `obs_in/<T>` for
    every one of them is a directory that does not exist yet. Cycle 1's F048
    window is staged by cycle 3.

    So this task stages what it reads, into its own working directory, and the
    repeated work is the price of a task whose inputs are a function of that
    task alone. See `run._hofx_ext_observers`.

    **Cut to the window, unlike `stage.obs`'s join.** The application is given
    one window covering the whole forecast, so ioda does not separate one lead
    window's rows from the next one's, and an uncut file would hand two adjacent
    observers the same rows. `obsarchive.concatenate` says the rest.

    `record["input"]` already names the task's own file, because
    `redirect_input` is what decides the observer set rather than what stages
    it: the decision has to be visible to the task's declared io, which runs
    before any staging.
    """
    try:
        record["rows"] = obsarchive.concatenate(
            record["sources"], Path(record["input"]), window=(begin, end))
    except obsarchive.ArchiveError as error:
        raise ObservationError(f"{record['name']}: {error}") from error
    return record


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
    exist yet: every archive this runs on is generated by `tools/obs-*` over a
    period it covers completely, so a window with nothing in it is a hole in a
    thing that was supposed to be whole. An experiment that genuinely wants to assimilate nothing for a
    stretch says so by not configuring observers, or by not running those cycles.
    """
    names = ", ".join(record["name"] for record in records)
    elsewhere = _archive_covers(config, cycle)
    if elsewhere is None:
        return (
            f"cycle {cycle} has no observation for any of its "
            f"{len(records)} observers ({names}), and neither does any other "
            f"cycle of this experiment. That is a path that resolves to nothing "
            f"or an offline stage that never ran, not a gap in the archive. "
            f"`ackbar validate` reports it per observer before anything is "
            f"submitted; reaching it here means the archive moved after the "
            f"experiment was created.")
    return (
        f"cycle {cycle} has no observation for any of its {len(records)} "
        f"observers ({names}), while cycle {elsewhere} has some. A platform goes "
        f"down on its own and the rest of the archive answers; all of them at "
        f"one instant is a window that was never built, or an experiment whose "
        f"period runs past the archive it names. Left to run, this cycle "
        f"assimilates nothing and every task in it reports success. Fill the "
        f"window, or shorten the experiment, then heal this cycle.")


def _archive_covers(config, cycle):
    """Another cycle of this experiment whose window some observer has a file for.

    None when there is none. Asking every cycle's window of the archive is the
    same question `validate._observation_step` asks up front, asked again here
    because an archive can be moved or half-rebuilt after an experiment is
    created, and because `ackbar validate --offline` never asked it at all.

    Only on the way to failing, and the archive's times are cached by
    `obsarchive`, so its cost is reading each bin once in a cycle that is about
    to stop.
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
