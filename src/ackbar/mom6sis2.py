"""Running MOM6-SIS2 for one cycle of one member.

The model is `coupler_main`, and it is configured entirely by the contents of the
directory it is started in. So a forecast here is three steps: build a run
directory, launch, and move the restarts it wrote to where the next cycle looks
for them. Nothing about the model is patched; everything ACKBAR needs to vary is
a file in that directory.

Two things about FMS shape the whole module.

**`INPUT/coupler.res` is a hardcoded path in `coupler_main`.** `restart_input_dir`
in `MOM_input_nml` and `SIS_input_nml` redirects MOM's and SIS's own restarts,
but the coupler reads the date it resumes from out of the literal string
`INPUT/coupler.res` and writes `RESTART/coupler.res`. A run directory that
symlinks `INPUT` straight at the shared static archive therefore has nowhere to
put the one file that says what time it is, and pointing `restart_input_dir` at
the previous cycle silently does not move the coupler with it: the model starts
from the namelist date instead, integrating the right state from the wrong time,
and it does not complain. So the run directory owns its own `INPUT`, a directory
of symlinks: the static archive, plus this cycle's incoming restart set.

**A stock case tells MOM6 that every run is a new run.** `input_filename` in
`MOM_input_nml` and `SIS_input_nml` is read one character at a time by
`MOM_restart::determine_is_new_run`: `'n'` is a new run, `'r'` reads the
automatically named restart files. MOM6-examples ships `'n'`, which is right for
a case distributed as an example and catastrophic for a cycling workflow. With
`'n'`, MOM6 initializes temperature and salinity from `INIT_LAYERS_FROM_Z_FILE`
and never opens `INPUT/MOM.res.nc`: every cycle integrates the same cold start
from a different date, so consecutive restart sets still differ, `coupler.res`
still carries the right clock, and every analysis is silently discarded. So
ACKBAR sets `'r'` in both groups, alongside `parameter_filename`.

**The date on a restart comes from the restart, not from the configuration.**
Once `INPUT/coupler.res` exists it overrides `coupler_nml`'s `current_date`, and
what the namelist still controls is only the *length* of the integration. Since
experiment setup materializes an initial condition into `rst/0`, every cycle
including the first resumes, so `current_date` is a fallback that a correct
experiment never reaches. It is written anyway, matching the cycle, because the
one case where it is read is a misconfigured cold start and starting at the right
date beats starting in 1958.
"""

import errno
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from . import stochastic
# Not `from . import forcing`: `stage` binds a local named `forcing` for the
# SIS_forcing half of a source, which would shadow the module.
from .forcing import assert_reference_height, table_files
from .config.jobtime import cycle_time, member_dir, render, symbols

#: A fourth MOM6 parameter file, written per member and read only by the
#: forecast, holding that member's stochastic physics switches. Read last, so it
#: wins, and separate from `MOM_override` because that file is the domain's and
#: SOCA reads it too: SOCA links a MOM6 that carries the same interface with the
#: stub behind it, and would fail on a switch this file turns on. See
#: `ackbar/stochastic.py`.
STOCHASTIC = "MOM_stochastic"

#: Files the run directory owns outright and writes fresh. Everything else in
#: the base case directory is linked through untouched. `STOCHASTIC` is owned
#: too but is not here, because it is written only for a perturbed member, and
#: this is the list of what every run has.
OWNED = ("input.nml", "diag_table")

#: Files the base case ships that ACKBAR replaces with its own, linked from the
#: paths `model.override` names rather than written here. Together with `OWNED`
#: this is the whole answer to "what does ACKBAR change about a stock case".
#:
#: Overrides are a *file* rather than generated text because SOCA reads
#: `MOM_override` too (see `soca.CASE_FILES`), and the two have to be the same
#: bytes. A model that the analysis configures differently from the forecast is
#: a grid they agree on by luck.
OVERRIDE = ("MOM_override", "SIS_override")

#: A third MOM6 parameter file, read by SOCA applications and never by the
#: forecast, after `MOM_input` and `MOM_override` which both of them read.
#: It exists for one reason: SOCA links a MOM6 built without symmetric memory,
#: which refuses to configure Flather open boundaries at all, so a regional
#: domain has to hand SOCA a geometry with its boundaries switched off while the
#: model integrates with them on. The file says so at length and is meant to be
#: deleted, not maintained. See `soca.stage`.
SOCA_OVERRIDE = "MOM_override.soca"

#: A third SIS2 parameter file, read last, holding what the *forcing* implies
#: about the model rather than what the domain does. Today that is one line,
#: `ADD_DIURNAL_SW = False`, and it has to travel with the forcing: the flag
#: synthesizes a diurnal cycle on the assumption that the shortwave handed to
#: SIS2 is a daily mean, so with a sub-daily source it is applied twice and the
#: sea surface gets a diurnal amplitude no atmosphere asked for.
#:
#: Not folded into the domain's `SIS_override`, which is where `NIGLOBAL` and
#: `NJGLOBAL` live: that file is the domain's and a forcing source is not, so
#: sharing one file would mean a copy of it per (domain, source) pair.
FORCING = "SIS_forcing"

#: The data table names the files and variables FMS's `data_override` reads for
#: every surface flux, so it is the forcing source's, not the domain's. A source
#: sets `model.data_table` and the case's own copy is not linked; leave it unset
#: and the case's copy is what the model reads.
DATA_TABLE = "data_table"

#: Files the base case ships that the model *writes* rather than reads. MOM6 and
#: SIS2 dump the parameter set they actually ran with into the working directory,
#: and MOM6-examples commits those dumps back into the case as documentation. So
#: they are outputs sitting in an input directory, and linking them means the
#: model opens a symlink for writing and edits the shared case in place: every
#: member of every cycle of every experiment writing the same file, and a
#: submodule that is permanently dirty so that a real change to it no longer
#: shows. Skipped here, and written fresh in the run directory instead.
GENERATED = ("MOM_parameter_doc.", "SIS_parameter_doc.", "available_diags.")

#: What proves a restart set is complete. Written by `coupler_restart` after the
#: component restarts, and the file the next cycle reads to know its own start
#: date, so its absence is exactly the failure that matters.
STAMP = "coupler.res"

#: Everything FMS leaves in `RESTART/` at an intermediate restart *except* the
#: ocean state, and all of it is discarded. `coupler_main` prefixes its own two
#: clock files with `date_to_string`, which is `YYYYMMDD.HHMMSS`; FMS's
#: `save_restart` appends the same stamp to every other component's restart, so
#: SIS2's arrives as `ice_model.res.nc20150105.040000.nc`. Prefix and suffix for
#: one stamp, which is why this is two alternatives rather than one.
#:
#: Neither is readable by anything downstream. The fields metadata's ice section
#: describes CICE's `cice.res.nc` rather than SIS2's `ice_model.res.nc`, and a
#: stamped clock file describes a time nothing resumes from. What is *not*
#: matched here is `coupler.intermediate.res`, which carries no stamp and
#: travels with the restart set: `coupler_main` reads it out of `INPUT/` next
#: cycle to know when the last interval fell.
FMS_STAMPED = re.compile(r"^\d{8}\.\d{6}\.|\d{8}\.\d{6}\.nc$")

#: How MOM6 names the one file here that anything reads, and it is its own
#: convention rather than FMS's. `MOM_restart::save_restart` builds the stamp
#: from the year, the day *of the year* and the second of the day, deliberately
#: not the month ("Compute the year-day, because I don't like months. - RWH"),
#: so `MOM.res.nc` at 04:00 on 2015-01-05 is `MOM.res_Y2015_D005_S14400.nc`.
#:
#: Built rather than parsed. Going the other way would mean reconstructing a
#: date from a year-day, and the model's calendar is `NOLEAP` while every date
#: ACKBAR computes is Python's proleptic Gregorian; the two disagree from March
#: of a leap year onwards. Constructing the name a slot *should* have makes that
#: disagreement a file this cannot find, which stops the cycle, rather than a
#: state filed under the wrong hour, which does not.
MOM_STAMP = "_Y{0:04d}_D{1:03d}_S{2:05d}"


class ModelError(Exception):
    pass


#: Small text the model writes about itself, kept beside the job's own log.
#: `ocean.stats` is where an ocean that is blowing up says so, one line per
#: timestep, and it is the first thing anyone asks for. It lives in the run
#: directory, which is scratch, which is deleted on success and purged by the
#: site on failure, so a successful forecast would otherwise leave no trace of
#: how it went at all.
#:
#: The parameter docs are here for a different reason: they are the only record
#: of what the model *actually* ran with, as opposed to what configuration says
#: it should have. Every parameter, resolved, including the ones a case set, the
#: ones ACKBAR overrode and the ones MOM6 defaulted. A comparison between two
#: experiments is only meaningful if the code and the parameters are accounted
#: for, and a bug-retention flag that quietly stayed on is exactly the kind of
#: difference that is invisible everywhere else. About 150 KB against a restart
#: set of a gigabyte, so the duplication across cycles is not worth a special
#: case to avoid.
#:
#: `ensemble.inputs` is written by `_input_dir` rather than by the model, and is
#: here for the same reason as the parameter docs: it is the only record of which
#: per-member file this member resolved to, and the directory holding the link is
#: deleted on success.
TRACES = ("ocean.stats", "SIS.stats", "logfile.000000.out",
          "MOM_parameter_doc.all", "SIS_parameter_doc.all",
          "MOM_parameter_doc.layout", "ensemble.inputs")


def forecast(config, site, paths, cycle, task, member, *, source, target,
             slots=None, handoff=None):
    """One model integration, from *source* restarts to *target* restarts.

    *slots* maps each valid time a sub-window state was asked for to where it
    belongs, and *handoff* is the valid time of the set *target* holds when that
    is not the end of the run. Both are passed in rather than computed here for
    the same reason *target* is: where a product lives and what time it is
    valid at are the experiment's layout, and this module's job is the model.
    """
    run = paths.scratch(cycle, task, member)
    logs = paths.log_dir(cycle)
    stage(config, run, cycle, task, source=source, member=member)
    try:
        launch(config, site, run, task)
    finally:
        # In `finally`, because the run that failed is the one whose trace is
        # worth having.
        keep_traces(run, logs, task, member)
    commit(run, target, slots=slots, handoff=handoff,
           restart=(config["model"].get("restart") or {}).get("ocn"))


# --- the run directory -------------------------------------------------------

def stage(config, run, cycle, task, *, source, member=None):
    """Build the run directory. Every input is a symlink, every config a file.

    *member* decides the stochastic physics and which inputs are staged, and
    only when the experiment configured either. Everything else about a run
    directory is the same for every member by construction, which is what makes
    the ensemble an ensemble.
    """
    model = config["model"]
    base = _path(model, "base")
    if not (source / STAMP).exists():
        raise ModelError(
            f"{source} is not a restart set: no {STAMP}. Its producer either "
            f"never ran or exited without writing one, and starting from the "
            f"namelist date instead would integrate this state from the wrong "
            f"time without complaint."
        )
    ocean = (model.get("restart") or {}).get("ocn")
    if ocean and not (source / ocean).exists():
        raise ModelError(
            f"{source} has a {STAMP} but no {ocean}, so it is a clock without "
            f"an ocean. Resuming would leave MOM6 to fail on the missing file "
            f"one cycle after whatever produced this set reported success."
        )

    # Both halves of a forcing source or neither. A data table naming a
    # sub-daily `atm.nc` while `ADD_DIURNAL_SW` keeps the case's default is
    # the one misconfiguration here that runs to completion and reports
    # success, and what it produces is a sea surface with the diurnal cycle
    # applied twice.
    data_table = model.get(DATA_TABLE)
    forcing = (model.get("override") or {}).get(FORCING)
    if bool(data_table) != bool(forcing):
        have, missing = ((f"model.{DATA_TABLE}", f"model.override.{FORCING}")
                         if data_table else
                         (f"model.override.{FORCING}", f"model.{DATA_TABLE}"))
        raise ModelError(
            f"{have} is set and {missing} is not. A forcing source is both: "
            f"the table says which file the fluxes come from, and {FORCING} "
            f"says what that file's cadence implies about the model. A "
            f"sub-daily shortwave read with ADD_DIURNAL_SW left on runs to "
            f"completion and applies the diurnal cycle twice."
        )

    run.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(base), key=lambda e: e.name):
        if entry.name in OWNED or entry.name in OVERRIDE:
            continue
        if data_table and entry.name in (DATA_TABLE, Path(data_table).name):
            # The case ships one for its own forcing. Linked through, it
            # would be the table the model read and the source's would be
            # the one nobody opened. The source's own file is skipped too:
            # it usually lives in this same directory, and linked under its
            # own name it sits in the run directory unread and unexplained.
            continue
        if entry.name == STOCHASTIC:
            # Never linked even when this run writes none of its own. A base
            # case is free to ship a file by that name, and linked through it
            # would be read after ACKBAR's override and win.
            continue
        if entry.name in ("INPUT", "RESTART"):
            continue
        if entry.name.startswith(GENERATED):
            continue
        _link(run / entry.name, entry.path)
    _link(run / "coupler_main", _path(model, "executable"))
    link_override(config, run)
    if data_table:
        table = Path(data_table)
        if not table.exists():
            raise ModelError(
                f"{table} does not exist, named by model.{DATA_TABLE}")
        _link(run / DATA_TABLE, table)
        link_override(config, run, names=(FORCING,))

    # Emptied rather than reused. Scratch is kept on failure, so a healed
    # attempt starts with whatever the killed one had written here, and
    # committing a set assembled from two attempts is a state no forecast ever
    # produced.
    _fresh(run / "RESTART")

    _input_dir(run, _path(model, "input"), source,
               overlay=member_inputs(config, cycle, member, task))

    # Every forcing file the table reads has to say what height its scalars sit
    # at, because the table declares one `z_bot` for the whole atmospheric state
    # and an archive built before the height shift existed holds 2 m fields
    # under a 10 m declaration. That runs to completion with about ten per cent
    # too little turbulent cooling and reports success, so it is caught here
    # rather than looked for afterwards in a sea surface temperature bias.
    #
    # Only files that are actually there: that something supplies each name is
    # `validate._forcing_table_files`'s question, asked before anything is
    # submitted, and asking it twice here would report the same problem in a
    # worse place.
    if data_table:
        for name in table_files(data_table):
            supplied = run / "INPUT" / name
            if not supplied.exists():
                continue
            try:
                assert_reference_height(supplied)
            except ValueError as error:
                raise ModelError(str(error)) from error

    # The stochastic physics, which is the one thing in a run directory that
    # differs between members. Both halves are written together or neither is:
    # the parameter file switches the scheme on inside MOM6, the namelist group
    # switches the pattern on inside the generator, and the generator fails the
    # run when only one of them is set.
    files, extra = _PARAMETER_FILES, ""
    if forcing:
        files = dict(files)
        files["SIS_input_nml"] = files["SIS_input_nml"] + (FORCING,)
    perturb = stochastic.settings(config)
    if perturb and stochastic.perturbs(member):
        _write(run / STOCHASTIC, stochastic.parameters(perturb))
        files = dict(files)
        files["MOM_input_nml"] = files["MOM_input_nml"] + (STOCHASTIC,)
        extra = stochastic.namelist(perturb, member, cycle)

    _write(run / "input.nml", _namelist(base, config, cycle, task, files) + extra)
    _write(run / "diag_table", _diag_table(config, model, cycle, task))


def link_override(config, run, names=OVERRIDE):
    """Link ACKBAR's override files into *run*.

    Shared with `soca.stage`, which needs the same `MOM_override` the forecast
    reads: SOCA builds a MOM6 geometry from the same parameter files, and two
    parameter sets is two grids that happen to agree. *names* differs between
    the two callers only by `SOCA_OVERRIDE`, which is a workaround rather than
    a difference either of them wants.
    """
    override = config["model"].get("override") or {}
    for name in names:
        path = override.get(name)
        if not path:
            raise ModelError(
                f"model.override.{name} is not set. It is what ACKBAR changes "
                f"about the stock case, and a case run without it silently "
                f"keeps whatever bug-retention flags that case ships with."
            )
        source = Path(path)
        if not source.exists():
            raise ModelError(f"{source} does not exist, named by model.override.{name}")
        _link(run / name, source)


def member_inputs(config, cycle, member, task="forecast"):
    """This member's own copies of the domain's inputs, as `(name, path)` pairs.

    `ensemble.inputs` maps a name *inside `INPUT/`* to a path template carrying
    `{{member_dir}}`. The two halves are deliberately independent: the name is
    what the model opens, the path is where that file's ensemble lives, and
    keeping them apart is what lets a boundary ensemble and an atmospheric one
    be built by different offline stages with different layouts and staged by
    the same three lines here. Neither stage has to know what the other calls
    its files, and this function does not have to know what either produced.

    Every member resolves to a path, including the control. A source with only
    one realization is not a special case: `tools/obc-lagged.py` writes the
    unperturbed `mem000.nc` beside the perturbed ones, and a deterministic
    source materializes every member as a symlink to its single file, so what
    member seven read is recorded whether or not member seven had anything of
    its own. The record is the `ensemble.inputs` file written beside the run and
    kept with the traces; `readlink INPUT/atm.nc` says the same thing but only
    until the run directory is reaped.

    That is also why a missing file raises rather than falling back to the
    domain archive's copy. The fallback is a member with no perturbation, and
    its only symptom is an ensemble slightly less spread than it should be,
    which is indistinguishable from the scheme being weaker than it is.
    """
    inputs = (config.get("ensemble") or {}).get("inputs") or {}
    if not inputs:
        return []
    if member is None:
        raise ModelError(
            "ensemble.inputs is set, but this run was staged without a member, "
            "so {{member_dir}} has nothing to resolve against. A run that "
            "stages per-member inputs is a member."
        )

    resolved = []
    # *task* because `forecast.ext` is the same executable over a different
    # window, and its length and end reach a template through this table. It
    # changes nothing for `{{member_dir}}` and everything for a path that dates
    # itself by the forecast it belongs to.
    table = symbols(config, cycle, member, task=task)
    for name, path in sorted(render(inputs, table).items()):
        path = Path(path)
        if not path.exists():
            raise ModelError(
                f"ensemble.inputs['{name}'] resolves to {path} for "
                f"{member_dir(member)}, which does not exist. The offline stage "
                f"behind it writes one file per member, so this is most often "
                f"an experiment configured with more members than that stage "
                f"was run for."
            )
        resolved.append((name, path))
    return resolved


def _input_dir(run, data, source, overlay=()):
    """`INPUT/`: the domain's data, this member's own, then this cycle's restarts.

    *data* is a directory the domain layer names, not `base/INPUT`, because the
    two halves of a case live apart: text in the repository, grids and initial
    conditions in the static root. For `OM_1deg` they happen to coincide, since
    MOM6-examples ships its own `INPUT` of symlinks into a dataset mirror.

    The three layers are in that order for two reasons. *overlay* is after
    *data* because a per-member input is a replacement for the domain's shared
    copy of that file, which is exactly what a perturbed `obc.nc` is. Both are
    before *source* because `coupler_main` reads `INPUT/coupler.res` from a
    hardcoded path and the restart set has to win it: a cycle whose date came
    from anywhere else integrates the right state from the wrong time.

    Rebuilt from nothing every attempt rather than updated in place. A healed
    forecast whose previous attempt left a stale `coupler.res` here would resume
    from the wrong date, and the model would run happily.
    """
    # The restart set is read once and the collision refused *before* anything
    # is linked, so a run that is going to be rejected does not first build a
    # directory that looks staged. Checking afterwards left the failure sitting
    # on top of a half-populated INPUT/, which is the state a healer would find.
    restarts = sorted(os.scandir(source), key=lambda e: e.name)
    names = {entry.name for entry in restarts}
    shadowed = sorted(name for name, _ in overlay if name in names)
    if shadowed:
        raise ModelError(
            f"{', '.join(shadowed)} is both an ensemble.inputs name and a file "
            f"in the restart set {source}. The restart set is staged last and "
            f"would win, so the per-member file would be built, linked, "
            f"overwritten and never read."
        )

    target = _fresh(run / "INPUT")
    for entry in sorted(os.scandir(data), key=lambda e: e.name):
        _link(target / entry.name, entry.path)
    for name, path in overlay:
        _link(target / name, path)
    for entry in restarts:
        _link(target / entry.name, entry.path)

    # What this member actually read, written where it survives the cycle.
    # `INPUT/` is scratch and is deleted on success, so `readlink INPUT/obc.nc`
    # answers only while the job is running: for a paired experiment whose whole
    # result is "which boundary did each member integrate", a finished run could
    # not answer its own question. `TRACES` carries this out beside the log.
    if overlay:
        (run / "ensemble.inputs").write_text(
            "".join(f"{name} -> {os.path.realpath(path)}\n"
                    for name, path in overlay))


def _fresh(target):
    """An empty directory, whatever was there before."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    return target


def _link(target, source):
    """Symlink, replacing whatever is there. Never a copy: a restart set is
    gigabytes and the run directory is rebuilt on every attempt."""
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(source)


def _write(target, text):
    target.write_text(text if text.endswith("\n") else text + "\n")


# --- the files ACKBAR owns ---------------------------------------------------

def _diag_table(config, model, cycle, task):
    """The diagnostics, chosen by what the forecast is *for*.

    A cycling forecast exists to produce the next background and writes no
    diagnostics at all; an extended forecast exists to be scored and writes
    intervals. Same model, same code path, different file, which is why this is
    configuration rather than a branch.

    Line 2 is the FMS base date and it must be the cycle's own start, since
    diagnostic time axes are offsets from it.
    """
    tables = model.get("diag_table") or {}
    choice = tables.get(task)
    if not choice:
        raise ModelError(
            f"model.diag_table has no entry for {task!r}; a forecast with no "
            f"diag_table is a decision, so it has to be written down"
        )
    start = cycle_time(config, cycle)
    body = Path(choice).read_text().splitlines()
    if len(body) < 2:
        raise ModelError(f"{choice} is not a diag_table: fewer than two lines")
    body[1] = "{0.year} {0.month} {0.day} {0.hour} {0.minute} {0.second}".format(start)
    return "\n".join(body)


#: Keys ACKBAR sets in the base case's `input.nml`, by namelist group. Patched
#: rather than regenerated: the file carries a couple of dozen groups of model
#: physics that are the case's, not ACKBAR's, and rewriting it would quietly
#: fork them.
#:
#: The terminator is any `/` that ends a line, not one alone on a line.
#: MOM6-examples' OM_1deg closes its SIS group on the last value:
#:
#:     parameter_filename = 'SIS_input',
#:                          'SIS_override' /
#:
#: and a pattern that insisted on a bare `/` simply does not match that group,
#: so the patch is silently skipped and `parameter_filename` keeps whatever the
#: case said. Quoted paths ending in a separator (`'INPUT/'`, `'./'`) are not
#: mistaken for it, because there the `/` is followed by the closing quote.
_PATCH = re.compile(
    r"(?P<head>&(?P<group>\w+)\b)(?P<body>.*?)(?P<tail>/[ \t]*$)",
    re.DOTALL | re.MULTILINE,
)
#: One assignment, including any continuation lines. A namelist list is
#: routinely written one element per line:
#:
#:     parameter_filename = 'MOM_input',
#:                          'MOM_override'
#:
#: so an assignment does not end at its own newline. It ends at the next line
#: that starts a new assignment, closes the group, or is blank. Matching only
#: the first line would replace it and leave the continuation behind as an
#: orphan, which is a namelist that no longer parses.
_ASSIGN = re.compile(
    r"^(?P<indent>\s*)(?P<key>\w+)\s*=[^\n]*"
    r"(?:\n(?!\s*(?:\w+\s*=|/|$))[^\n]*)*",
    re.MULTILINE,
)

#: The parameter files each component reads, in the order it reads them. Later
#: files override earlier ones, so ACKBAR's override goes last: that is what
#: makes `#override` in it beat the case's own setting.
#:
#: Set here rather than inherited because a case cannot be relied on to list
#: the override at all. MOM6-examples' OM_1deg does; the imported Gulf cases
#: name only `MOM_input`, and a case whose overrides are silently not read is
#: the failure this whole mechanism exists to prevent.
#:
#: No `MOM_layout`. MOM6 decomposes for itself, and it picks the same layout a
#: person would.
_PARAMETER_FILES = {
    "MOM_input_nml": ("MOM_input", "MOM_override"),
    "SIS_input_nml": ("SIS_input", "SIS_override"),
}

#: Whether the component resumes from its restart files, and it is the single
#: most consequential value in this file.
#:
#: `MOM_restart::determine_is_new_run` reads exactly one character: `'n'` means
#: a new run, `'r'` means read the automatically named restart files, `'F'`
#: means restart if they happen to be there. Stock MOM6-examples cases ship
#: `'n'`, because a case distributed as an example is meant to be started from
#: its own initial condition file.
#:
#: With `'n'`, MOM6 initializes temperature and salinity from
#: `INIT_LAYERS_FROM_Z_FILE` and never opens `INPUT/MOM.res.nc` at all. Every
#: cycle then integrates the same cold-start state, every analysis is silently
#: discarded, and nothing anywhere reports it: the model runs, writes a restart
#: set, and the workflow hands it to the next cycle. What that costs is not
#: bounded by the analysis, it is the whole experiment.
#:
#: `'r'` and not `'F'`. Under `'F'` a missing restart is a cold start rather
#: than a failure, which is exactly the way this was invisible in the first
#: place. Every ACKBAR cycle resumes, including the first: setup materializes
#: the initial condition into `rst/0`.
_RESUME = "'r'"


def _namelist(base, config, cycle, task, files=None):
    """`input.nml` with the run length, fallback date and parameter files set.

    `months`/`days` are zeroed as well as setting the rest, because a base case
    that runs in months would otherwise add ours to its own.

    *task* because the long forecast runs for a different length and writes at
    a different cadence, and both reach `coupler_nml` through the symbol table.

    *files* is `_PARAMETER_FILES` for every run that is not perturbed, and that
    list plus `MOM_stochastic` for one that is. Passed rather than decided here
    so that a member's whole difference from the control is settled in one place.
    """
    files = _PARAMETER_FILES if files is None else files
    table = symbols(config, cycle, task=task)
    coupler = {
        "months": 0,
        "days": 0,
        "hours": table["mom6_hours"],
        "minutes": table["mom6_minutes"],
        "seconds": table["mom6_seconds"],
        "current_date": table["mom6_current_date"],
        # How often the run writes an intermediate restart, and all zeros for
        # "never", which is what an experiment with no `forecast.slots` gets.
        # Written either way rather than only when it is wanted: a base case
        # that set one of its own would otherwise decide how much a cycling
        # forecast writes, which is the same class of surprise `input_filename`
        # was.
        "restart_interval": table["mom6_restart_interval"],
    }
    # The coupling timestep, when the domain states one. It varies with
    # resolution exactly as `DT` does, and it lives in `input.nml` rather than
    # in `MOM_input`, so it is the one resolution-dependent value that a shared
    # case directory cannot carry. Patching it here is what lets four Gulf
    # domains read one `input.nml` instead of four that drift.
    seconds = config["model"].get("coupling_seconds")
    if seconds:
        coupler["dt_cpld"] = seconds
        coupler["dt_atmos"] = seconds
    updates = {"coupler_nml": coupler}
    for group, names in files.items():
        updates[group] = {
            "parameter_filename": ", ".join(f"'{name}'" for name in names),
            "input_filename": _RESUME,
        }

    text = (base / "input.nml").read_text()
    for group, values in updates.items():
        text = _patch_group(text, group, values)
    return text


def _patch_group(text, group, values):
    found = [False]

    def replace(match):
        if match.group("group") != group:
            return match.group(0)
        found[0] = True
        body, remaining = match.group("body"), dict(values)

        def assign(hit):
            key = hit.group("key")
            if key not in remaining:
                return hit.group(0)
            return f"{hit.group('indent')}{key} = {remaining.pop(key)}"

        body = _ASSIGN.sub(assign, body)
        added = "".join(f"            {k} = {v}\n" for k, v in remaining.items())
        return match.group("head") + body.rstrip("\n") + "\n" + added + match.group("tail")

    out = _PATCH.sub(replace, text)
    if not found[0]:
        raise ModelError(f"input.nml has no &{group} group to configure")
    return out


# --- launching and committing ------------------------------------------------

def launch(config, site, run, task):
    """Run the model to completion in *run*, or raise with the log named."""
    ntasks = config["domain"].get("resources", {}).get(task, {}).get("ntasks", 1)
    launcher = shlex.split(site.get("launcher") or "")
    command = launcher + (["-n", str(ntasks)] if launcher else []) + ["./coupler_main"]

    log = run / "model.log"
    with open(log, "wb") as handle:
        result = subprocess.run(command, cwd=run, stdout=handle,
                                stderr=subprocess.STDOUT)
    if result.returncode:
        raise ModelError(
            f"{' '.join(command)} exited {result.returncode}; the run directory "
            f"is kept, see {log}"
        )


def keep_traces(run, logs, task, member, names=("model.log",) + TRACES):
    """Copy an executable's own small logs out of scratch, next to the job's log.

    Named with the job id where there is one, for the same reason `--output`
    is: a healed attempt must land beside the failed one rather than overwrite
    the evidence of why it was healed.

    *names* is a parameter because SOCA leaves different files behind than the
    model does, and what is worth having one copy of is the naming rather than
    the list. Two spellings of the attempt stamp would put a healed forecast's
    trace and a healed hofx's trace in different places.
    """
    attempt = os.environ.get("SLURM_ARRAY_JOB_ID") or os.environ.get("SLURM_JOB_ID")
    index = os.environ.get("SLURM_ARRAY_TASK_ID")
    stamp = f".{attempt}_{index}" if attempt and index else f".{attempt}" if attempt else ""
    stem = task if member is None else f"{task}.{member_dir(member)}"

    logs.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = run / name
        if source.exists():
            shutil.copyfile(source, logs / f"{stem}{stamp}.{name}")


def commit(run, target, slots=None, restart=None, handoff=None):
    """Sort what the run wrote: the restart set to *target*, the slots to theirs.

    Move rather than copy, and only after the model has exited zero: at a
    gigabyte a member a copy is the most expensive thing in the cycle. `STAMP`
    goes last, so a set that is present is a set that is whole even if this is
    killed halfway.

    `RESTART/` holds two kinds of file once `forecast.slots` is set. FMS writes
    a complete, self-consistent set at every interval under a time stamp, and
    another at the end of the run with no stamp at all. **One model run produces
    all of them.** It compares its clock against the next interval on each
    coupled step and dumps in place, so each set costs a write and nothing
    else: no chained short forecasts, no second initialization, no restart
    handoff between them to get wrong.

    *handoff* names which of those sets the next cycle starts from, and is None
    for the unstamped one at the end. A 4D window is what makes them differ:
    the forecast overshoots the next analysis time by half a window so that the
    far end of that window exists, and the state the next cycle resumes from is
    then the interval at the analysis time rather than the last thing written.
    Integrating past a time does not change the state at it.

    *slots* maps each valid time a sub-window state was asked for to the
    directory it belongs in, and is None for a forecast that was not asked for
    any. Each is claimed by the name it should have and a missing one stops the
    cycle. Everything unclaimed is deleted rather than left behind, so that what
    survives a forecast is exactly what something asked it to produce: the next
    cycle links the whole of *target* into its own `INPUT/`, and a stamped file
    carried in there is one FMS would find and ACKBAR never named.
    """
    written = run / "RESTART"
    if not (written / STAMP).exists():
        raise ModelError(
            f"the model exited 0 but wrote no {written / STAMP}, so there is "
            f"nothing for the next cycle to start from"
        )

    slots = dict(slots or {})
    # The handoff time is a sub-window time too, whenever there is a window: it
    # is the centre of the next cycle's, and MOM6 writes the state there once.
    # So that slot is a link to the file in the restart set rather than a
    # second claim on it, made after the set is assembled.
    shared = slots.pop(handoff, None) if handoff is not None else None
    for when, directory in sorted(slots.items()):
        _claim_slot(written, when, directory, restart)

    keep = (_final_set(written, restart) if handoff is None
            else _stamped_set(written, handoff, restart))
    keeping = {source for source, _ in keep}
    for entry in sorted(os.scandir(written), key=lambda e: e.name):
        if entry.is_file() and Path(entry.path) not in keeping:
            os.unlink(entry.path)

    target.mkdir(parents=True, exist_ok=True)
    stamp = None
    for source, name in sorted(keep, key=lambda pair: pair[1]):
        if name == STAMP:
            stamp = source
        else:
            _move(source, target / name)
    _move(stamp, target / STAMP)

    if shared is not None:
        _link_state(target, shared, restart)


def _move(source, target):
    """One file from the run directory into the experiment tree.

    `os.replace` is the whole operation while scratch and output share a
    filesystem, which is how rancor is configured and why this has never been
    exercised otherwise. They are meant to be two filesystems: `paths.py` says
    so, `run._commit` says so, and the site file for the first real HPC will say
    so by pointing scratch at a Lustre scratch and output at a project
    directory. A rename between two filesystems raises `EXDEV`, so every
    forecast would fail at its last step, after the model had run.

    The fallback copies to a temporary *in the destination directory* and
    renames within it, so the visible name still appears whole or not at all.
    That is what lets `commit` write `coupler.res` last and have its presence
    mean the set is complete.
    """
    try:
        os.replace(source, target)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    partial = target.with_name(target.name + ".partial")
    shutil.copyfile(source, partial)
    os.replace(partial, target)
    os.unlink(source)


def _final_set(written, restart):
    """The unstamped set: what `coupler_end` leaves at the end of the run.

    Everything without a stamp on it, rather than a list of names, so that a
    component restart ACKBAR has never heard of still travels with the set.
    """
    return [(Path(entry.path), entry.name)
            for entry in os.scandir(written)
            if entry.is_file()
            and not FMS_STAMPED.search(entry.name)
            and not _stamped_ocean(entry.name, restart)]


def _stamped_set(written, when, restart):
    """The set FMS wrote at one interval, and the plain name each file takes.

    Three stamping conventions land in one directory and all three have to be
    undone. `coupler_main` prefixes its own two clock files with
    `YYYYMMDD.HHMMSS`; FMS appends the same string to a component restart, so
    SIS2's arrives as `ice_model.res.nc20150105.040000.nc`; MOM6 ignores the
    string it was handed and builds its own year-day stamp. Matching on the
    stamp rather than on a list of names is what carries an unknown component's
    restart along, exactly as `_final_set` does.

    The interval's `coupler.res` is a complete one: `coupler_restart` writes the
    calendar, the run's start date and `Time` at the interval, which is what a
    resume reads. Its `coupler.intermediate.res` carries the interval's own time
    and is what schedules the next run's first interval.
    """
    if not restart:
        raise ModelError(
            "model.restart.ocn is not set, so the ocean state in the set this "
            f"run wrote at {when} has no name to be looked for under, and the "
            f"set would be assembled without it"
        )
    stamp = when.strftime("%Y%m%d.%H%M%S")
    body = _mom_body(when, restart)
    found = []
    for entry in os.scandir(written):
        if not entry.is_file():
            continue
        name = entry.name
        if body and name.startswith(body):
            plain = restart.rpartition(".")[0] + name[len(body):]
        elif name.startswith(stamp + "."):
            plain = name[len(stamp) + 1:]
        elif name.endswith(stamp + ".nc"):
            plain = name[:-len(stamp) - 3]
        else:
            continue
        found.append((Path(entry.path), plain))

    if not any(name == STAMP for _, name in found):
        raise ModelError(
            f"the model exited 0 having written no {stamp}.{STAMP} in {written}, "
            f"so the state at {when} that the next cycle starts from does not "
            f"exist. `restart_interval` in `coupler_nml` has to divide the "
            f"cycle length for the forecast to write a set there at all."
        )
    # And the ocean, whose absence is otherwise invisible. `coupler_main` stamps
    # its own two clock files and FMS stamps SIS2's, so an interval written on a
    # domain whose `MOM_override` lacks `RESTART_CONTROL = 2` looks complete:
    # MOM6 took the default bit and overwrote one unstamped `MOM.res.nc` rather
    # than stamping anything. Unchecked, `commit` then deletes that unstamped
    # file as unclaimed and hands forward a set carrying a clock and an ice
    # state and no ocean, which passes every "is this set whole" test ackbar
    # makes (all of them key on `coupler.res`) and fails a cycle later inside
    # MOM6, by which point the analysis has already run against it.
    if not any(name == restart for _, name in found):
        raise ModelError(
            f"the model exited 0 having written no {restart} at {when} in "
            f"{written}, so the set the next cycle resumes from would carry a "
            f"clock and an ice state and no ocean. `RESTART_CONTROL` in this "
            f"domain's `MOM_override` has to have bit 1 set (`= 2`) for MOM6 to "
            f"write a time-stamped restart at all; the default bit 0 overwrites "
            f"one unstamped file per interval."
        )
    return found


def _mom_body(when, restart):
    """How MOM6 spells *restart* at *when*, without the suffix. `MOM.res` ->
    `MOM.res_Y2015_D005_S14400`, which `MOM.res_1.nc` extends to
    `MOM.res_Y2015_D005_S14400_1.nc`."""
    if not restart:
        return None
    stem = restart.rpartition(".")[0]
    return stem + MOM_STAMP.format(when.year, when.timetuple().tm_yday,
                                   when.hour * 3600 + when.minute * 60 + when.second)


def _claim_slot(written, when, directory, restart):
    """Move the state MOM6 wrote at *when* into the directory it belongs in.

    The ocean state alone, and not the whole set FMS wrote at that interval: a
    slot is what the analysis reads a background out of, and SOCA reads the
    ocean restart through `fields metadata`. Every file of it, though: a restart
    with enough fields is split into `MOM.res.nc`, `MOM.res_1.nc` and up, and a
    slot missing one of those is a state SOCA reads as far as it goes and then
    reports a field it could not find.
    """
    if not restart:
        raise ModelError(
            "model.restart.ocn is not set, so the state this run wrote at "
            f"{when} has no name to be looked for under"
        )
    stem, _, suffix = restart.rpartition(".")
    body = _mom_body(when, restart)

    found = sorted(Path(written).glob(f"{body}*.{suffix}"))
    if not found:
        raise ModelError(
            f"the model exited 0 having written no {body}.{suffix} in {written}, "
            f"so the state at {when} that this cycle was asked for does not "
            f"exist. RESTART_CONTROL has to have bit 1 set for MOM6 to write a "
            f"time-stamped restart at all, and `restart_interval` in "
            f"`coupler_nml` has to reach that time."
        )
    directory.mkdir(parents=True, exist_ok=True)
    for source in found:
        _move(source, directory / source.name.replace(body, stem))


def _link_state(target, directory, restart):
    """The slot at the handoff time, as links to the restart set's own files.

    A hard link rather than a copy, because it is the same state and a copy of
    it is a gigabyte, and rather than a symlink because the two directories are
    reaped independently and a link that outlives its target is a background
    that reads as missing only when something opens it.
    """
    stem, _, suffix = restart.rpartition(".")
    directory.mkdir(parents=True, exist_ok=True)
    for source in sorted(Path(target).glob(f"{stem}*.{suffix}")):
        link = directory / source.name
        if link.exists():
            link.unlink()
        os.link(source, link)


def _stamped_ocean(name, restart):
    """Any MOM6-stamped state, which is one written at an interval."""
    if not restart:
        return False
    stem = restart.rpartition(".")[0]
    return bool(re.match(rf"{re.escape(stem)}_Y\d+_D\d+_S\d+", name))


def _path(model, key):
    value = model.get(key)
    if not value:
        raise ModelError(f"model.{key} is not set")
    return Path(value)
