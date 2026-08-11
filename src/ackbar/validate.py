"""Everything checkable before a single job is submitted.

A cycle is many jobs and a bad value in one of them should not be discovered by
a job that starts eight hours from now. This is the whole of the early warning
system, and it is more load-bearing than it looks: JEDI offers no reliable
parse-and-exit at the application level, so nothing between here and the running
executable will catch anything. There is no step that asks JEDI whether it likes
its own configuration, because such a check would cover an unpredictable subset
of it, and a check that reports success for configurations that will still fail
is worse than no check.

What carries that weight instead is step 3. Malformed JEDI YAML is rare here,
because a document's values are assembled from data structures rather than
templated with `sed`, and its shape is a checked-in file that is parsed rather
than a string that is pasted. Missing and misnamed *files* are the common
failure, and those are entirely checkable up front.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import netCDF4
import numpy as np
import yaml

from .config.jobtime import SYMBOL_NAMES, JobTimeError, render, symbols
from .config.jobtime import TOKEN as JOBTIME_TOKEN
from .config.jobtime import unresolved as unresolved_jobtime
from .config.lint import ambiguous_numbers
from .config.resolve import unresolved as unresolved_experiment
from .config.schema import validate as validate_schema
from .config.template import TemplateError, slots_of
from .duration import DurationError, parse_duration, parse_instant
from .forcing import TABLE_FILE as FORCING_TABLE_FILE
from .gridspec import (STAGGER_ATTR, STAGGER_VALUE, STAGGER_VALUE_GENERATED,
                       assert_faces_recorded, extent, within)
from .soca import GRIDSPEC
from .graph import GraphError, build_graph, job_time_context, member_set
from .graph.build import extended_cycles
from .observations import REQUIRED as OBS_REQUIRED

#: The steps, in the order they run. Reported individually so that "what was
#: actually checked" is visible rather than implied by an exit code.
STEPS = (
    (1, "the merged config against ACKBAR's schema"),
    (2, "every job's config, for every cycle and member"),
    (3, "every referenced input path exists"),
    (4, "every executable exists and is runnable"),
    (5, "projected job count against queue limits"),
    (6, "the graph is acyclic and member arrays share the index set"),
)

#: Models that integrate a real ocean state, and so read a velocity off the
#: grid the gridspec describes. `stub` and `persistence` never do, which is why
#: `om_1deg` can carry a gridspec on the generated face set and still be the
#: domain the graph fixtures are built on. See `_gridspec_step`.
FORECAST_MODELS = ("mom6sis2",)

#: Steps that consult the filesystem or the site's scheduler limits. `--offline`
#: skips these, which is what the tier 0 and 1 tests run and what is useful on a
#: machine where the data is not staged yet.
FILESYSTEM_STEPS = (3, 4, 5)


@dataclass(frozen=True)
class Finding:
    step: int
    where: str
    message: str


def validate_experiment(config, schema, site, root, offline=False):
    """Check an experiment. Returns (findings, graph, steps that actually ran).

    Later steps are skipped rather than attempted when an earlier one has
    already failed, and which ones ran is returned rather than implied. A
    report that silently checked four of six things is the failure mode this
    command exists to prevent.
    """
    findings = list(_schema_step(config, schema))
    ran = {1}

    if findings:
        # A config that fails its own schema cannot be reasoned about further:
        # everything below would be reading values it has just been told are
        # the wrong shape, and would fail with a traceback rather than a
        # message.
        return _sorted(findings), None, ran

    try:
        graph = build_graph(config)
    except (GraphError, DurationError) as error:
        findings.append(Finding(6, "", str(error)))
        return _sorted(findings), None, ran | {6}

    ran |= {2, 6}
    findings += _graph_step(config, graph)
    findings += _template_step(root)

    jobtime_findings, paths, observations, (timed, shared) = _jobtime_step(
        config, graph)
    findings += jobtime_findings

    if not offline:
        ran |= {3, 4, 5}
        findings += _path_step(paths, site)
        findings += _gridspec_step(config)
        findings += _coverage_step(timed, config, graph, shared)
        findings += _observation_step(observations)
        findings += _observation_cycle_step(observations)
        findings += _observation_domain_step(config, observations)
        findings += _executable_step(graph, root)
        findings += _limit_step(graph, site)

    return _sorted(findings), graph, ran


def _sorted(findings):
    return sorted(findings, key=lambda f: (f.step, f.where, f.message))


def _schema_step(config, schema):
    for where, message in validate_schema(config, schema):
        yield Finding(1, where, message)

    for where, value in ambiguous_numbers(config):
        yield Finding(1, where, (
            f"{value!r} is a string to ACKBAR and a number to JEDI. PyYAML reads "
            f"YAML 1.1, where an exponent needs an explicit sign; eckit reads "
            f"YAML 1.2, where it does not. Write it as a plain number."
        ))

    # resolve() raises on an unknown symbol, so anything surviving here means
    # the experiment-time pass has a hole in it.
    for where, value in unresolved_experiment(config):
        yield Finding(1, where, f"unsubstituted experiment-time token: {value!r}")

    # The three values whose grammar the schema can only approximate. JSON
    # Schema `format` is annotation-only unless a format checker is installed,
    # and `pattern: '^P'` accepts `Potato`, so both reach the parser instead.
    # `cycle.length` is caught later by `build_graph`; `cycle.start` was not
    # caught anywhere: `parse_instant` raised out of the middle of this command,
    # so a one-character typo in a date reported one line with no config path
    # and skipped steps 2 through 6, which is the silently-partial report this
    # command exists to prevent.
    for where, parse in (("cycle.start", parse_instant),
                         ("cycle.length", parse_duration),
                         ("forecast.slots", parse_duration)):
        section, _, leaf = where.partition(".")
        text = (config.get(section) or {}).get(leaf)
        if not isinstance(text, str):
            continue
        try:
            parse(text)
        except (DurationError, JobTimeError) as error:
            yield Finding(1, where, str(error))


def _template_step(root):
    """The SOCA document templates, before one is frozen into the experiment.

    Three things, and all of them are properties of the file alone, which is why
    this needs no cycle and no observer list. Whether the *slots* a template
    declares are the ones the task computes is checked exactly, by
    `tests/test_templates.py`, because that comparison needs both halves and one
    of them is Python.

    A template with a mistake in it is otherwise found by the first cycle to run
    that application, which for `recenter` is after the ensemble and the
    deterministic analysis have both already run.
    """
    findings = []
    for source in sorted((Path(root) / "config" / "soca").glob("*.yaml")):
        where = f"config/soca/{source.name}"
        try:
            skeleton = yaml.safe_load(source.read_text())
        except yaml.YAMLError as error:
            findings.append(Finding(2, where, f"is not parseable YAML: {error}"))
            continue

        try:
            slots_of(skeleton)
        except TemplateError as error:
            findings.append(Finding(2, f"{where}: {error.path}", error.message))

        for path, value in unresolved_jobtime(skeleton):
            for token in JOBTIME_TOKEN.findall(value):
                name = token.partition(":")[0].strip()
                if name not in SYMBOL_NAMES:
                    findings.append(Finding(2, f"{where}: {path}", (
                        f"unknown job-time symbol {{{{{name}}}}}; the set is "
                        f"closed, see `ackbar config symbols`"
                    )))
    return findings


def _jobtime_step(config, graph):
    """Render the config as every job will see it, and collect its paths.

    Generating the whole experiment's configuration up front is cheap, and it
    is also a strong test that graph generation really is deterministic and
    free of side effects: nothing here could work otherwise.

    Rendered once per (cycle, member) rather than once per node, because the
    job-time symbols depend on those two and nothing else.
    """
    findings = []
    paths = set()
    observations = {}
    timed = set()
    shared = set()
    for cycle, member in job_time_context(config, graph):
        try:
            rendered = render(config, symbols(config, cycle, member))
        except JobTimeError as error:
            findings.append(Finding(
                2, error.path, f"cycle {cycle} member {member}: {error.message}"
            ))
            continue
        timed.update(((rendered.get("ensemble") or {}).get("inputs") or {}).values())
        shared.update(_shared_timed(rendered))
        for where, value in unresolved_jobtime(rendered):
            findings.append(Finding(
                2, where, f"cycle {cycle}: token survived substitution: {value!r}"
            ))
        _take_observation_inputs(rendered, observations, cycle)
        findings.extend(_forcing_pair(rendered))
        findings.extend(_forcing_table_files(rendered))
        _collect_paths(rendered, paths)

    # One finding per distinct problem, not one per cycle: a bad symbol in a
    # shared config would otherwise be reported hundreds of times.
    return _dedupe(findings), paths, observations, (timed | shared, shared)


#: Time-varying inputs the domain supplies to every member alike, as names under
#: `model.input`. The coverage check was built for `ensemble.inputs`, where a
#: short archive is the failure healing cannot recover, but the same failure
#: arrives from the shared copy and arrives more often: every experiment has a
#: boundary and only some have an ensemble of them. `tests/experiments/tier3_gom.yaml`
#: is the standing example, a fixture starting 2015-01-05 against a boundary that
#: begins 2015-05-28, which stops MOM6 at its first timestep.
SHARED_TIMED = ("obc.nc",)


def _shared_timed(rendered):
    """The domain's own time-varying inputs, for the coverage check.

    Only names that exist: a closed domain has no `obc.nc`, and a missing one is
    step 3's finding to report rather than this one's.
    """
    where = ((rendered.get("model") or {}).get("input"))
    if not isinstance(where, str):
        return []
    return [os.path.join(where, name) for name in SHARED_TIMED
            if os.path.exists(os.path.join(where, name))]


def _forcing_pair(rendered):
    """A forcing source is a data table and a `SIS_forcing`, or it is neither.

    `mom6sis2.stage` enforces this too, but that runs inside a submitted job, so
    without this an experiment configured with one half passes every validate
    step and dies in its first forecast. The paths themselves are checked by
    step 3 like any other; only the pairing is checked here.
    """
    model = rendered.get("model") or {}
    table = model.get("data_table")
    forcing = (model.get("override") or {}).get("SIS_forcing")
    if bool(table) == bool(forcing):
        return []
    have, missing = (("model.data_table", "model.override.SIS_forcing")
                     if table else
                     ("model.override.SIS_forcing", "model.data_table"))
    return [Finding(2, have, (
        f"{have} is set and {missing} is not. A forcing source is both: the "
        f"table says which file the fluxes come from, and SIS_forcing says "
        f"what that file's cadence implies about the model. A sub-daily "
        f"shortwave read with ADD_DIURNAL_SW left on runs to completion and "
        f"applies the diurnal cycle twice."
    ))]


#: What a data table row names as its file, `"INPUT/atm.nc"` in the fourth
#: column. Defined in `forcing.py`, which asks the other question of the same
#: column: this checks that something supplies each name, that one checks that
#: the file supplied says what height its scalars are at.
TABLE_FILE = FORCING_TABLE_FILE


def _forcing_table_files(rendered):
    """Every file a forcing table names has to be provided by something.

    `_forcing_pair` checks that a table comes with a `SIS_forcing`, which is the
    pairing that describes the *model*. This is the other half: a table names
    files under `INPUT/`, and unlike every other input in the config those names
    are not paths this validator would otherwise see, so nothing checks that
    anything supplies them.

    The failure it closes is `forcing/common` inherited without a source layer.
    That sets `model.data_table` to a table whose every row reads `INPUT/atm.nc`
    and sets `SIS_forcing`, so the pairing passes, and no `ensemble.inputs` and
    no domain copy provides the file. The graph submits and every member's first
    forecast dies inside `data_override`.

    A name is satisfied by the domain's own `INPUT/` or by an `ensemble.inputs`
    key, which are the two things that can put a file there.
    """
    model = rendered.get("model") or {}
    table = model.get("data_table")
    where = model.get("input")
    if not isinstance(table, str) or not os.path.exists(table):
        return []  # step 3 owns a table that is not there
    staged = set(((rendered.get("ensemble") or {}).get("inputs") or {}))
    with open(table) as handle:
        wanted = sorted(set(TABLE_FILE.findall(handle.read())))
    findings = []
    for name in wanted:
        if name in staged:
            continue
        if isinstance(where, str) and os.path.exists(os.path.join(where, name)):
            continue
        findings.append(Finding(2, "model.data_table", (
            f"{table} reads INPUT/{name}, and nothing provides it: it is not in "
            f"{where} and no ensemble.inputs key names it. Every forecast would "
            f"fail inside data_override. A forcing table needs the source layer "
            f"that supplies its file, not just the table."
        )))
    return findings


def _take_observation_inputs(rendered, observations, cycle):
    """Move each observer's input file out of the general path set.

    An observation file is the one input an experiment is allowed to be missing,
    so it cannot be checked by the rule that governs every other path. Removed
    from the tree rather than filtered afterwards, because the paths there are a
    flat set of strings by then and nothing distinguishes an archive file from a
    grid file.

    Kept by cycle as well as in one set, because the two observation steps ask
    the same files two different questions: one is about an observer across the
    experiment, the other about a cycle across the observers.
    """
    for entry in rendered.get("observations") or ():
        space = entry.get("obs space") or {}
        engine = (space.get("obsdatain") or {}).get("engine") or {}
        path = engine.pop("obsfile", None)
        if not path:
            continue
        record = observations.setdefault(
            space.get("name", ""), {"required": bool(space.get(OBS_REQUIRED)),
                                    "paths": set(), "by_cycle": {}},
        )
        record["paths"].add(path)
        record["by_cycle"].setdefault(cycle, set()).add(path)


#: Keys whose value is a filename with the extension left off. saber writes and
#: reads every parameter file through `util::readFieldSet`, which takes a stem
#: and appends `.nc`, so the string in the configuration is not the name of
#: anything on disk. Without this, a calibrated diffusion operator reports as a
#: missing input forever, which trains the reader to ignore step 3.
STEM_KEYS = ("filepath",)
STEM_SUFFIX = ".nc"


def _collect_paths(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in STEM_KEYS and isinstance(value, str) and _looks_like_path(value):
                # A `filepath` that already carries the extension is a whole
                # filename and not a stem. Not every reader behind this key is
                # `util::readFieldSet`: `SOCAParametricOceanStdDev` reaches its
                # sst floor through `soca::readNcAndInterp`, which opens the
                # string as given. Appending to that produced `sst_bgerr.nc.nc`,
                # a step 3 failure against a file that was there all along.
                out.add(value if value.endswith(STEM_SUFFIX)
                        else value + STEM_SUFFIX)
                continue
            _collect_paths(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_paths(value, out)
    elif isinstance(node, str) and _looks_like_path(node):
        out.add(node)


def _looks_like_path(text):
    # Absolute and with no whitespace. JEDI's own namespaced names ("MetaData/
    # latitude", "GeoVaLs/sea_area_fraction") are relative and do not match.
    return text.startswith("/") and len(text) > 1 and not any(c.isspace() for c in text)


def _gridspec_step(config):
    """A domain's gridspec must say which faces its staggered fields are on, must
    say what its domain layer says, and must not be the generated face set under
    a model that would integrate on it.

    Step 3 rather than a step of its own: an input that is present but will not
    say what it is is an input problem, and it reads beside "input path does not
    exist" without a reader having to learn a new number.

    A domain with no `static` has no geometry, which is the stub, and absence of
    the file is step 3's own finding and is said better there.
    """
    domain = config.get("domain") or {}
    static = domain.get("static")
    if not static:
        return []
    path = os.path.join(static, GRIDSPEC)
    if not os.path.exists(path):
        return []
    try:
        faces = assert_faces_recorded(path)
    except (ValueError, OSError) as error:
        return [Finding(3, path, str(error))]

    # Absent means shifted. The default is the safe one on purpose: a domain
    # whose layer has not considered the question must be the one that fails,
    # not the one that skips the shift, so adding a regional domain and
    # forgetting this key cannot be how the defect returns.
    declared = domain.get("staggered_faces", STAGGER_VALUE)
    if faces != declared:
        return [Finding(3, path, (
            f"the gridspec records {STAGGER_ATTR}={faces!r} but the domain "
            f"layer declares staggered_faces: {declared!r}. One of the two is "
            f"stale, and nothing else detects it: the gridspec is built once "
            f"per domain, so a later edit to the domain layer leaves the file "
            f"behind with no symptom. Rebuild it with tools/soca-gridspec.sh, "
            f"which reads the declaration and writes the file to match."))]

    model = (config.get("model") or {}).get("name")
    if declared == STAGGER_VALUE_GENERATED and model in FORECAST_MODELS:
        return [Finding(3, path, (
            f"this domain's staggered fields sit where `soca_gridgen.x` left "
            f"them, on the east and north faces, and this experiment "
            f"integrates the {model} forecast model. SOCA's reader loads the "
            f"west and south faces whatever the file says, so every velocity "
            f"would be masked and labelled a half cell from where it actually "
            f"is: increments written into faces the model carries no velocity "
            f"on, and faces it does carry one on that no increment reaches. "
            f"Nothing downstream reports this; the run completes and the "
            f"velocity analysis is wrong. `ackbar.gridspec.shift_staggered` is "
            f"what corrects it and cannot be applied to this domain, whose "
            f"outermost row and column hold ocean rather than the land a "
            f"bounded grid has there. Run this domain under model.name: stub "
            f"or persistence, or implement the periodic and tripolar case "
            f"described in the src/ackbar/gridspec.py docstring."))]
    return []


def _path_step(paths, site):
    """Inputs must exist. Outputs are what the experiment is about to create.

    The distinction is by root: anything under the scratch or output root is
    this experiment's own product, and everything else absolute is something it
    consumes and cannot make.
    """
    roots = tuple(
        os.path.normpath(site[key])
        for key in ("scratch_root", "output_root")
        if site.get(key)
    )
    findings = []
    for path in sorted(paths):
        if _under(path, roots):
            continue
        if not os.path.exists(path):
            findings.append(Finding(3, path, "input path does not exist"))
        elif not os.access(path, os.R_OK):
            findings.append(Finding(3, path, "input path is not readable"))
    return findings


def _needed_span(config, graph):
    """The first and last instant this experiment's forcing has to cover.

    From `symbols`, rather than recomputed here, because the window's begin and
    the forecast's end are exactly what a job reads and both already have edge
    cases in them: a 4D window opens before its cycle, and a long forecast runs
    past the next one. `extended_cycles` says which cycles run long, so a
    cadenced long forecast does not make the whole experiment demand coverage
    it never reaches.
    """
    cycles = sorted({cycle for cycle, _ in job_time_context(config, graph)})
    if not cycles:
        return None, None
    long_ones = set(extended_cycles(config, max(cycles)))
    begins, ends = [], []
    for cycle in cycles:
        table = symbols(config, cycle, 0)
        begins.append(_fields(table["window_begin"]))
        ends.append(_fields(table["forecast_end"]))
        if cycle in long_ones:
            ends.append(_fields(
                symbols(config, cycle, 0, task="forecast.ext")["forecast_end"]))
    return min(begins), max(ends)


def _coverage_step(timed, config, graph, shared=frozenset()):
    """A time-varying input has to span the run, and this is the only chance.

    A member input that stops early is the one input failure healing cannot
    recover. The run dies in `time_interp_external` at the cycle that walks off
    the end, and the fix means rebuilding the archive, which changes the anomaly
    mean for every member, so every earlier cycle integrated a boundary that no
    longer exists. The experiment is discarded rather than healed.

    **The time axis is read, not the `time_coverage_*` attributes.**
    `tools/obc-lagged.py` writes those and they are worth having for a human
    with `ncdump`, but a check that trusted them would only work on archives
    built by that one tool, would silently pass on every archive built before it
    started writing them, and would be checking an annotation rather than the
    thing the model reads. The axis is what MOM6 opens.

    A file with no time axis at all is not a time-varying input and is skipped:
    `ensemble.inputs` is a general mechanism, and a per-member static field is a
    legitimate use of it. "No time axis" means no variable carrying `axis = "T"`
    and no variable called `time`, which is deliberately wider than the latter
    alone: the atmospheric archive gives every field its own unlimited axis
    (`time_T2`, `time_DSWRF`, ...) and carries no variable named `time`, so a
    check keyed on that name would collect every `atm.nc`, find nothing, skip
    it, and report the experiment clean.
    """
    first, last = _needed_span(config, graph)
    if first is None:
        return []
    findings = []
    short = {}
    for path in sorted(timed):
        if not os.path.exists(path):
            continue  # step 3 owns absence, and says it better than this would
        try:
            covered = _covered_span(path)
        except OSError as error:
            findings.append(Finding(3, path, f"cannot be opened to check its "
                                             f"time coverage: {error}"))
            continue
        if covered is None:
            continue
        start, end = covered
        if start > first or end < last:
            short.setdefault(covered, []).append(path)

    # One finding per distinct span, not one per member. An ensemble is built in
    # one pass from one source, so every member is short in the same way, and
    # twenty identical paragraphs is the noise `_dedupe` exists to prevent for
    # the cycle loop. Grouping by span rather than deduping by message keeps the
    # case where one member really does differ.
    for (start, end), paths in sorted(short.items()):
        common = os.path.commonpath(paths) if len(paths) > 1 else paths[0]
        # The two cases end differently and the difference is the whole reason
        # to catch this early. A per-member archive cannot be extended in place,
        # so being told after the fact costs the experiment; the domain's own
        # copy can simply be refetched longer, and the reader should not be told
        # otherwise about a file they can fix in an afternoon.
        if shared and not (set(paths) - shared):
            what = f"{len(paths)} shared input(s)"
            repair = ("Refetch the domain's boundary over the experiment's "
                      "span before starting it.")
        else:
            what = f"{len(paths)} per-member input(s)"
            repair = ("Rebuilding the archive then does not heal it: a rebuild "
                      "changes the anomaly mean, so every earlier cycle would "
                      "have integrated a boundary that no longer exists.")
        findings.append(Finding(3, common, (
            f"{what} cover {_stamp(start)} to {_stamp(end)}, but the experiment "
            f"reaches {_stamp(first)} to {_stamp(last)}. The run would stop in "
            f"time_interp_external at the cycle that leaves the file. {repair}"
        )))
    return findings


def _covered_span(path):
    """(first, last) covered by *every* time axis in a file, or None if it has none.

    Narrowest rather than first, because a file's axes need not agree. The
    atmospheric archive gives each field its own, and an interval-mean field is
    stamped at its window midpoint, so its last record sits half an interval
    before the instantaneous fields' last record. The run stops when the
    earliest axis runs out, so that is the one the span has to report.

    Axes are found by `axis = "T"`, with `time` as the fallback for a file that
    predates the convention.
    """
    import netCDF4

    spans = []
    with netCDF4.Dataset(path) as data:
        names = [name for name, variable in data.variables.items()
                 if getattr(variable, "axis", "") == "T"]
        for name in names or ["time"]:
            if name not in data.variables or not len(data[name]):
                continue
            axis = data[name]
            units = getattr(axis, "units", "")
            if not units:
                continue
            calendar = getattr(axis, "calendar", "standard")
            edges = netCDF4.num2date([axis[0], axis[-1]], units, calendar)
            spans.append((_fields(edges[0]), _fields(edges[-1])))
    if not spans:
        return None
    return max(begin for begin, _ in spans), min(end for _, end in spans)


def _fields(when):
    """Calendar fields, which is the only footing these dates share.

    A boundary axis is usually NOLEAP, so `num2date` returns cftime, and a
    cftime date will not compare against the Python datetimes `symbols` builds:
    the operator raises rather than converting. Comparing the fields is also the
    right meaning rather than a workaround, because the model reads the axis in
    its own calendar and ACKBAR's dates are wall-clock stamps; converting either
    one into the other's calendar would move a date that nothing else moves.
    """
    return (when.year, when.month, when.day,
            when.hour, when.minute, when.second)


def _stamp(fields):
    return ("{0:04d}-{1:02d}-{2:02d}T{3:02d}:{4:02d}:{5:02d}Z").format(*fields)


def _observation_step(observations):
    """Observation files, which are the one input allowed to be absent.

    An archive has gaps: a platform goes down for a week, a satellite is not yet
    launched at the start of a reanalysis. `stage.obs` drops the observer for
    that cycle and records it, and refusing to create the experiment over it
    would make ACKBAR unusable on any real record.

    What is *not* data is an observer that has no file in any cycle at all. That
    is a misspelled archive path or a platform named wrongly, and it produces an
    experiment that runs to completion assimilating nothing, which is the most
    expensive way to discover a typo. So the rule is by proportion rather than
    by count: some missing is the archive, all missing is the configuration.

    A `required` observer is checked file by file, since the experiment has
    already said that its absence is not acceptable.
    """
    findings = []
    for name in sorted(observations):
        record = observations[name]
        absent = sorted(p for p in record["paths"] if not os.path.exists(p))
        # A file that is there and cannot be read is not an archive gap. Nothing
        # downstream treats it as one either: `stage.obs` asks whether the file
        # exists, keeps the observer, and hofx fails on it.
        findings += [
            Finding(3, path, "observation file is not readable")
            for path in sorted(record["paths"] - set(absent))
            if not os.access(path, os.R_OK)
        ]
        if not absent:
            continue
        if record["required"]:
            findings += [
                Finding(3, path, f"observer {name} is required and this file "
                                 f"does not exist")
                for path in absent
            ]
        elif len(absent) == len(record["paths"]):
            findings.append(Finding(3, f"observations.{name}", (
                f"no observation file exists for any of the {len(absent)} "
                f"cycle(s) configured, for example {absent[0]}. One missing "
                f"window is an archive gap and is dropped at run time; all of "
                f"them is a wrong path."
            )))
    return findings


def _observation_cycle_step(observations):
    """The same proportion rule as `_observation_step`, on the other axis.

    That one reads down a column: an observer with no file in any cycle is a
    wrong path. This one reads across a row: a cycle with no file for any
    observer is a window the archive does not have.

    **It is the failure nothing downstream can see.** The cycle runs to
    completion assimilating nothing, and every task in it succeeds: `stage.obs`
    drops each observer in turn, the analysis is skipped because there is
    nothing to solve against, `writeback` hands the background forward, and
    `post.obs` writes zeros. There is no artifact whose absence says so, and the
    experiment reports healthy while it free-runs. `observations.realize` stops
    such a cycle when it reaches one; this says it before anything is submitted,
    which is where the archive can still be rebuilt cheaply.

    Every cycle empty is left to `_observation_step`, which says the same thing
    per observer and says it better: an archive nothing resolves in is a path
    problem, and reporting it twice sends the reader looking for a second cause.
    """
    if not observations:
        return []
    cycles = sorted({cycle for record in observations.values()
                     for cycle in record["by_cycle"]})
    empty = [cycle for cycle in cycles
             if not any(os.path.exists(path)
                        for record in observations.values()
                        for path in record["by_cycle"].get(cycle, ()))]
    if not empty or len(empty) == len(cycles):
        return []
    example = sorted(path for record in observations.values()
                     for path in record["by_cycle"].get(min(empty), ()))
    covered = min(cycle for cycle in cycles if cycle not in empty)
    return [Finding(3, "observations", (
        f"{len(empty)} of {len(cycles)} cycle(s) have no observation file for "
        f"any observer: {', '.join(str(c) for c in empty[:8])}"
        f"{' ...' if len(empty) > 8 else ''}. Cycle {covered} has one, so the "
        f"archive exists and does not cover these windows. Each of them would "
        f"run to completion, assimilate nothing, and report success: there is "
        f"no output whose absence would say otherwise. Build the missing "
        f"windows, or run the cycles the archive covers. One platform missing "
        f"from a window is a gap and is dropped at run time; all of them is a "
        f"window that is not there. For example {example[0]} does not exist."
    ))]


#: The ioda groups and variables an observation file keeps its positions in.
#: Only these two are read here, and only from one file per observer.
OBS_METADATA = "MetaData"
OBS_LONGITUDE = "longitude"
OBS_LATITUDE = "latitude"


def _observation_domain_step(config, observations):
    """An archive with nothing inside the domain is not an experiment.

    Step 3 and a sibling of `_gridspec_step`: an input that exists and holds
    nothing this domain can use is an input problem, and it reads beside "input
    path does not exist" rather than needing a step of its own.

    **This is the one failure a regional analysis has no other way to notice.**
    A global observation file handed to a regional domain does not break: SOCA
    runs, every observation fails its `Domain Check`, and the cycle completes
    with an increment of zero. Fifty cycles of that look exactly like fifty
    healthy cycles until someone plots an increment. The archive is meant to be
    culled to the domain first, by `tools/obs-cull-domain.py`, and nothing about
    an unculled archive is visible from its path.

    **The rule is that every observer is empty, not that any one is.** One
    platform with nothing inside the domain is normal and becomes more normal
    once archives are culled: a satellite that did not pass over the Gulf this
    fortnight contributes files with no rows in them, by design. All of them
    empty is a different statement, and the only ones it can make are a wrong
    archive or a wrong domain. There is deliberately no threshold in between:
    "most of them outside" means the counts are inflated, which is what the
    culling stage is for, and any cutoff above zero is a knob with no defensible
    value.

    One file per observer, the first that exists **and has rows in it**, because
    this is asking what the archive *is* rather than auditing it. An archive
    whose first window is in the domain and whose fiftieth is not is not a thing
    that happens. The "has rows" half is not an optimization: a culled archive
    writes a present-but-empty file for every platform that saw nothing in a
    window, so stopping at the first file that merely exists samples a file with
    nothing to say, and an archive whose earliest window happens to be quiet
    across every platform would be refused for holding no in-domain
    observations when the truthful reading is that it was never asked. An
    archive that is empty everywhere this step looked produces no finding at
    all: the question it exists to answer, "are these observations somewhere
    else?", needs an observation to be asked of.
    """
    static = (config.get("domain") or {}).get("static")
    if not static:
        return []
    path = os.path.join(static, GRIDSPEC)
    if not os.path.exists(path):
        # No gridspec, no extent to test against. Its absence is `_path_step`'s
        # finding, and saying it twice would not make it truer.
        return []

    try:
        box = extent(path)
    except (OSError, KeyError, IndexError):
        # A gridspec that cannot be read is `_gridspec_step`'s to report. This
        # one declines to turn the same broken file into a second finding about
        # observations, which would send a reader looking in the wrong place.
        return []

    counted = {}
    for name in sorted(observations):
        for candidate in sorted(observations[name]["paths"]):
            if not os.path.exists(candidate) or not os.access(candidate, os.R_OK):
                continue
            try:
                found, total = _in_domain(candidate, box)
            except (OSError, KeyError, IndexError):
                # Unreadable or not shaped like an observation file. `stage.obs`
                # will keep the observer and the application will fail on it,
                # which is `_observation_step`'s territory rather than this
                # one's.
                break
            counted[name] = (found, total, candidate)
            if total:
                break
            # A file with no rows in it answers nothing. A culled archive writes
            # one whenever a platform saw nothing in a window, so the first file
            # an observer has is routinely empty and the question this step asks
            # is still open. Keep going until this observer has rows to speak
            # for it, and let the empty one stand only as a record that the
            # observer was looked at.

    # Only files with rows can say whether the archive is in the domain, so the
    # verdict is taken over those alone. Dropping the empty ones is also what
    # keeps the message below from reaching "none of the 0 observations", which
    # is a sentence about a state that cannot exist.
    sampled = {name: entry for name, entry in counted.items() if entry[1]}

    if not sampled or any(found for found, _, _ in sampled.values()):
        # An archive that is empty as far as it was read is not evidence of
        # anything, least of all of a wrong domain: every observer holding no
        # rows is exactly what a correctly culled archive looks like over a
        # quiet window. Refusing it here would refuse the archive this project
        # asks for, so the step declines to speak.
        return []

    example = min(sampled)
    total = sum(count for _, count, _ in sampled.values())
    return [Finding(3, "observations", (
        f"none of the {total} observations in the {len(sampled)} observer file(s) "
        f"checked fall inside {(config.get('domain') or {}).get('name', 'this domain')}, "
        f"whose grid spans {box[0]:.3f} to {box[1]:.3f} east and {box[2]:.3f} to "
        f"{box[3]:.3f} north. For example {counted[example][2]} has "
        f"{counted[example][1]} observations and none of them are in it. Every "
        f"cycle would run to completion, reject every observation at `Domain "
        f"Check`, and write an increment of zero. Cull the archive to this "
        f"domain first with tools/obs-cull-domain.py, or point obs_dir at an "
        f"archive that was built for it."
    ))]


def _in_domain(path, box):
    """How many of an observation file's positions are inside *box*, and of how many.

    netCDF4 rather than h5py because every other reader here is netCDF4, and an
    ioda file opens either way for reading. (`ensemble_hofx` needs h5py only
    because netCDF4 will not reopen an ioda file for *append*.)
    """
    with netCDF4.Dataset(path) as data:
        data.set_auto_mask(False)
        meta = data.groups[OBS_METADATA]
        lon = np.asarray(meta.variables[OBS_LONGITUDE][:]).ravel()
        lat = np.asarray(meta.variables[OBS_LATITUDE][:]).ravel()
    if lon.size != lat.size:
        raise IndexError(f"{path} has {lon.size} longitudes and {lat.size} latitudes")
    return int(within(lon, lat, box).sum()), int(lon.size)


def _under(path, roots):
    normalized = os.path.normpath(path)
    return any(
        normalized == root or normalized.startswith(root + os.sep) for root in roots
    )


def _executable_step(graph, root):
    """Executables are repository-relative, because ACKBAR pins what it runs.

    Matching them against the recorded provenance belongs here too, and lands
    with the provenance record itself.
    """
    findings = []
    for relative in sorted({n.exe for n in graph.nodes if n.exe}):
        path = os.path.join(root, relative)
        if not os.path.exists(path):
            findings.append(Finding(
                4, relative, "executable does not exist; has the bundle been built?"
            ))
        elif not os.access(path, os.X_OK):
            findings.append(Finding(4, relative, "executable is not runnable"))
    return findings


def _limit_step(graph, site):
    """Projected in-flight job count against the site's limits.

    Every array task counts individually against a submit limit, so at large
    ensemble sizes this approaches the per-user cap. Refusing here beats
    discovering it three cycles in, where a rejected `sbatch` inside the
    submitter stops the experiment silently.

    Projected disk is the other half of this step and is not computed yet:
    there is no measured per-cycle size until something writes one, which is
    the stub model.
    """
    findings = []
    per_cycle = max(
        (sum(node.jobs for node in graph.cycle_nodes(c)) for c in graph.cycles),
        default=0,
    )
    # `graph.throttle` is 1 and `graph.build` refuses anything else, so this
    # multiplication is reachable only at 1. Kept rather than folded away
    # because raising the throttle should be one edit and not an archaeology
    # exercise; see "The cycle throttle" in docs/design.md.
    in_flight = per_cycle * graph.throttle

    limit = _as_int(site.get("max_submit_jobs"))
    if limit and in_flight > limit:
        findings.append(Finding(5, "cycle.throttle", (
            f"{in_flight} jobs would be in flight ({per_cycle} per cycle times "
            f"a throttle of {graph.throttle}), over this site's "
            f"max_submit_jobs of {limit}"
        )))

    array_limit = _as_int(site.get("max_array_size"))
    if array_limit:
        for node in graph.nodes:
            if node.members and max(node.members) + 1 > array_limit:
                findings.append(Finding(5, node.task, (
                    f"array index {max(node.members)} is over this site's "
                    f"max_array_size of {array_limit}"
                )))
                break
    return findings


def _graph_step(config, graph):
    """Structure, plus the two things Slurm will not tell you about.

    A member array whose index set differs from the canonical one produces an
    `aftercorr` edge that never fires, and shows up as a job pending forever
    rather than as an error. And a task with no declared resources would be
    submitted with whatever `sbatch` defaults to, which on a real machine is a
    single core.
    """
    findings = []
    canonical = member_set(config)
    for node in sorted(graph.nodes, key=lambda n: n.id):
        # The long forecast and its evaluation are the declared subset, which is
        # why the edge into `forecast.ext` stays afterok rather than upgrading.
        # The edge *between* them does upgrade, and is safe to: they carry the
        # same set as each other, which is the property `aftercorr` needs.
        if node.members and node.task not in ("forecast.ext", "hofx.ext", "post.fcst") \
                and node.members != canonical:
            findings.append(Finding(6, node.id, (
                f"member array {list(node.members)} differs from the canonical "
                f"index set {list(canonical)}"
            )))

    for task in sorted({n.task for n in graph.nodes if not n.resources}):
        findings.append(Finding(6, f"domain.resources.{task}", (
            "no resources declared for this task, and no domain.resources.default"
        )))

    for edge in graph.edges:
        if edge.kind != "aftercorr":
            continue
        up, down = _node(graph, edge.parent), _node(graph, edge.child)
        if up.members != down.members:
            findings.append(Finding(6, f"{edge.parent} -> {edge.child}", (
                "aftercorr between arrays with different index sets never fires"
            )))

    return findings


def _solver(config):
    return (config.get("solver") or {}).get("name", "none")


def _node(graph, node_id):
    cycle, _, task = node_id.partition(".")
    return graph.get(int(cycle), task)


def _dedupe(findings):
    seen, out = set(), []
    for finding in findings:
        key = (finding.step, finding.where, finding.message.split(":", 1)[-1])
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
