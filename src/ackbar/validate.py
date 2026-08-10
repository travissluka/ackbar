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
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config.jobtime import SYMBOL_NAMES, JobTimeError, render, symbols
from .config.jobtime import TOKEN as JOBTIME_TOKEN
from .config.jobtime import unresolved as unresolved_jobtime
from .config.lint import ambiguous_numbers
from .config.resolve import unresolved as unresolved_experiment
from .config.schema import validate as validate_schema
from .config.template import TemplateError, slots_of
from .duration import DurationError, parse_duration, parse_instant
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

    jobtime_findings, paths, observations, timed = _jobtime_step(config, graph)
    findings += jobtime_findings

    if not offline:
        ran |= {3, 4, 5}
        findings += _path_step(paths, site)
        findings += _coverage_step(timed, config, graph)
        findings += _observation_step(observations)
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
    for cycle, member in job_time_context(config, graph):
        try:
            rendered = render(config, symbols(config, cycle, member))
        except JobTimeError as error:
            findings.append(Finding(
                2, error.path, f"cycle {cycle} member {member}: {error.message}"
            ))
            continue
        timed.update(((rendered.get("ensemble") or {}).get("inputs") or {}).values())
        for where, value in unresolved_jobtime(rendered):
            findings.append(Finding(
                2, where, f"cycle {cycle}: token survived substitution: {value!r}"
            ))
        _take_observation_inputs(rendered, observations)
        findings.extend(_forcing_pair(rendered))
        _collect_paths(rendered, paths)

    # One finding per distinct problem, not one per cycle: a bad symbol in a
    # shared config would otherwise be reported hundreds of times.
    return _dedupe(findings), paths, observations, timed


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


def _take_observation_inputs(rendered, observations):
    """Move each observer's input file out of the general path set.

    An observation file is the one input an experiment is allowed to be missing,
    so it cannot be checked by the rule that governs every other path. Removed
    from the tree rather than filtered afterwards, because the paths there are a
    flat set of strings by then and nothing distinguishes an archive file from a
    grid file.
    """
    for entry in rendered.get("observations") or ():
        space = entry.get("obs space") or {}
        engine = (space.get("obsdatain") or {}).get("engine") or {}
        path = engine.pop("obsfile", None)
        if not path:
            continue
        record = observations.setdefault(
            space.get("name", ""), {"required": bool(space.get(OBS_REQUIRED)),
                                    "paths": set()},
        )
        record["paths"].add(path)


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


def _coverage_step(timed, config, graph):
    """Per-member forcing has to span the run, and this is the only chance.

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
        findings.append(Finding(3, common, (
            f"{len(paths)} per-member input(s) cover {_stamp(start)} to "
            f"{_stamp(end)}, but the experiment reaches {_stamp(first)} to "
            f"{_stamp(last)}. The run would stop in time_interp_external at the "
            f"cycle that leaves the file. Rebuilding the archive then does not "
            f"heal it: a rebuild changes the anomaly mean, so every earlier "
            f"cycle would have integrated a boundary that no longer exists."
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
