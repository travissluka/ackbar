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

**The date on a restart comes from the restart, not from the configuration.**
Once `INPUT/coupler.res` exists it overrides `coupler_nml`'s `current_date`, and
what the namelist still controls is only the *length* of the integration. Since
experiment setup materializes an initial condition into `rst/0`, every cycle
including the first resumes, so `current_date` is a fallback that a correct
experiment never reaches. It is written anyway, matching the cycle, because the
one case where it is read is a misconfigured cold start and starting at the right
date beats starting in 1958.
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .config.jobtime import cycle_time, member_dir, symbols

#: Files the run directory owns outright. Everything else in the base case
#: directory is linked through untouched, so this list is also the answer to
#: "what does ACKBAR change about a stock MOM6-examples case".
OWNED = ("input.nml", "MOM_layout", "SIS_layout", "diag_table")

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


class ModelError(Exception):
    pass


#: Small text the model writes about itself, kept beside the job's own log.
#: `ocean.stats` is where an ocean that is blowing up says so, one line per
#: timestep, and it is the first thing anyone asks for. It lives in the run
#: directory, which is scratch, which is deleted on success and purged by the
#: site on failure, so a successful forecast would otherwise leave no trace of
#: how it went at all.
TRACES = ("ocean.stats", "SIS.stats", "logfile.000000.out")


def forecast(config, site, paths, cycle, task, member, *, source, target):
    """One model integration, from *source* restarts to *target* restarts."""
    run = paths.scratch(cycle, task, member)
    logs = paths.sub("log") / str(cycle)
    stage(config, run, cycle, task, source=source)
    try:
        launch(config, site, run, task)
    finally:
        # In `finally`, because the run that failed is the one whose trace is
        # worth having.
        keep_traces(run, logs, task, member)
    commit(run, target)


# --- the run directory -------------------------------------------------------

def stage(config, run, cycle, task, *, source):
    """Build the run directory. Every input is a symlink, every config a file."""
    model = config["model"]
    base = _path(model, "base")
    resources = config["domain"].get("resources", {}).get(task, {})
    if not (source / STAMP).exists():
        raise ModelError(
            f"{source} is not a restart set: no {STAMP}. Its producer either "
            f"never ran or exited without writing one, and starting from the "
            f"namelist date instead would integrate this state from the wrong "
            f"time without complaint."
        )

    run.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(base), key=lambda e: e.name):
        if entry.name in OWNED or entry.name in ("INPUT", "RESTART"):
            continue
        if entry.name.startswith(GENERATED):
            continue
        _link(run / entry.name, entry.path)
    _link(run / "coupler_main", _path(model, "executable"))

    # Emptied rather than reused. Scratch is kept on failure, so a healed
    # attempt starts with whatever the killed one had written here, and
    # committing a set assembled from two attempts is a state no forecast ever
    # produced.
    _fresh(run / "RESTART")

    _input_dir(run, base, source)
    _write(run / "input.nml", _namelist(base, config, cycle))
    for name in ("MOM_layout", "SIS_layout"):
        _write(run / name, _layout(resources, name, task))
    _write(run / "diag_table", _diag_table(config, model, cycle, task))


def _input_dir(run, base, source):
    """`INPUT/`: the static archive, then this cycle's restarts over the top.

    Rebuilt from nothing every attempt rather than updated in place. A healed
    forecast whose previous attempt left a stale `coupler.res` here would resume
    from the wrong date, and the model would run happily.
    """
    target = _fresh(run / "INPUT")
    for entry in sorted(os.scandir(base / "INPUT"), key=lambda e: e.name):
        _link(target / entry.name, entry.path)
    for entry in sorted(os.scandir(source), key=lambda e: e.name):
        _link(target / entry.name, entry.path)


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

def _layout(resources, name, task):
    """The PE decomposition, from the domain layer.

    Never inherited from the base case: MOM6-examples ships `12,10` for MOM and
    `32,18` for SIS with a comment saying not to use them, and a layout whose
    product is not the task's `ntasks` fails inside FMS with a message about
    domain decomposition rather than about configuration.
    """
    layout = resources.get("layout")
    if not layout:
        raise ModelError(
            f"domain.resources.{task}.layout is not set, and the layout shipped "
            f"with the base case is a placeholder that will not match "
            f"--ntasks={resources.get('ntasks', '?')}"
        )
    if layout[0] * layout[1] != resources.get("ntasks"):
        raise ModelError(
            f"domain.resources.{task}: layout {layout[0]}x{layout[1]} is "
            f"{layout[0] * layout[1]} PEs but ntasks is {resources.get('ntasks')}"
        )
    return f"! generated by ackbar\nLAYOUT = {layout[0]},{layout[1]}\nIO_LAYOUT = 1,1\n"


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
_PATCH = re.compile(
    r"(?P<head>&(?P<group>\w+)\b)(?P<body>.*?)(?P<tail>^\s*/\s*$)",
    re.DOTALL | re.MULTILINE,
)
_ASSIGN = re.compile(r"^(?P<indent>\s*)(?P<key>\w+)\s*=[^\n]*", re.MULTILINE)


def _namelist(base, config, cycle):
    """`input.nml` with the run length and the fallback date set for this cycle.

    `months`/`days` are zeroed as well as setting `hours`, because a base case
    that runs in months would otherwise add ours to its own.
    """
    table = symbols(config, cycle)
    updates = {
        "coupler_nml": {
            "months": 0,
            "days": 0,
            "hours": table["mom6_hours"],
            "current_date": table["mom6_current_date"],
        },
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


def keep_traces(run, logs, task, member):
    """Copy the model's own small logs out of scratch, next to the job's log.

    Named with the job id where there is one, for the same reason `--output`
    is: a healed attempt must land beside the failed one rather than overwrite
    the evidence of why it was healed.
    """
    attempt = os.environ.get("SLURM_ARRAY_JOB_ID") or os.environ.get("SLURM_JOB_ID")
    index = os.environ.get("SLURM_ARRAY_TASK_ID")
    stamp = f".{attempt}_{index}" if attempt and index else f".{attempt}" if attempt else ""
    stem = task if member is None else f"{task}.{member_dir(member)}"

    logs.mkdir(parents=True, exist_ok=True)
    for name in ("model.log",) + TRACES:
        source = run / name
        if source.exists():
            shutil.copyfile(source, logs / f"{stem}{stamp}.{name}")


def commit(run, target):
    """Move the restart set to where the next cycle reads it.

    Move rather than copy, and only after the model has exited zero: at a
    gigabyte a member a copy is the most expensive thing in the cycle. `STAMP`
    goes last, so a set that is present is a set that is whole even if this is
    killed halfway.
    """
    written = run / "RESTART"
    stamp = written / STAMP
    if not stamp.exists():
        raise ModelError(
            f"the model exited 0 but wrote no {written / STAMP}, so there is "
            f"nothing for the next cycle to start from"
        )

    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(written), key=lambda e: e.name):
        if entry.name != STAMP:
            os.replace(entry.path, target / entry.name)
    os.replace(stamp, target / STAMP)


def _path(model, key):
    value = model.get(key)
    if not value:
        raise ModelError(f"model.{key} is not set")
    return Path(value)
