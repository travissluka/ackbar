"""Running a SOCA application for one cycle.

`hofx` so far, and the shape is the same one `mom6sis2.py` uses: build a run
directory, launch, move the product to where the rest of the experiment reads
it. What differs is that a SOCA application is configured by a YAML file it is
handed, so the run directory exists for a smaller reason: SOCA still initializes
a MOM6 geometry, and MOM6 is configured by the directory it is started in.

Three things about that geometry decide everything below.

**SOCA copies the namelist it is given to `input.nml` in the working
directory**, and asserts that the file it was given is not already called that
(`soca/src/soca/Geometry/FmsInput.cc`). So the namelist ACKBAR owns is
`mom_input.nml`, and the run directory must be somewhere writable, which is
scratch.

**`parameter_filename` inside that namelist is relative**, so `MOM_input` and
`MOM_override` are read out of the run directory. They are linked from the same
stock case the forecast runs, which is the only way the grid SOCA analyses on
and the grid the model integrates on are guaranteed to be the same grid.

**The geometry is read, not generated.** `soca_gridspec.nc` is an offline
product of the static stage, keyed on domain, and building it is a full MOM6
initialization that no cycling job should pay for a constant. The domain layer
names the directory; `tools/soca-gridspec.sh` writes it.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import yaml

from .config.jobtime import cycle_time, symbols
from .graph.tasks import SOCA_BIN
from .mom6sis2 import ModelError, keep_traces

#: What the static stage produces and every application here reads.
GRIDSPEC = "soca_gridspec.nc"

#: Files linked from the model's stock case, and the whole of what MOM6 needs in
#: order to describe its own domain. Not the case wholesale, unlike a forecast:
#: everything else in it configures an integration that is not happening.
CASE_FILES = ("MOM_input", "MOM_override", "INPUT")

#: FMS reads a `diag_table` while the geometry is being built and treats its
#: absence as fatal, so one is written even though nothing here asks for a
#: diagnostic. Two lines is a whole diag_table: a label and the base date.
DIAG_TABLE = "soca\n{0.year} {0.month} {0.day} {0.hour} {0.minute} {0.second}\n"

#: The application's own config and log, kept out of scratch on the way past.
TRACES = ("hofx.yaml", "hofx.log")


def hofx(config, site, paths, cycle, task, *, background, observers):
    """Evaluate *observers* against the background state of one cycle.

    *observers* are the records `observations.selected` returns, so the decision
    about which ones run has already been made and recorded by `stage.obs`.
    """
    if not observers:
        # Not an error. An archive with a gap wide enough to drop everything is
        # a fact about the archive, and the realized list already says so.
        print(f"ackbar: {cycle}.{task} has no observers with input files; "
              f"nothing to evaluate")
        return []

    run = paths.scratch(cycle, task)
    stage(config, run, cycle)

    products = _redirect_output(observers, run / "out")
    document = hofx_config(config, cycle, observers, background=background)
    _write(run / "hofx.yaml", yaml.safe_dump(document, sort_keys=False))

    try:
        launch(config, site, run, task, "soca_hofx3d.x", "hofx.yaml", "hofx.log")
    finally:
        keep_traces(run, paths.sub("log") / str(cycle), task, None, names=TRACES)

    return commit(products)


# --- the run directory -------------------------------------------------------

def stage(config, run, cycle):
    """Build the run directory: the case's grid files, and nothing else."""
    base = Path(_require(config["model"], "base"))
    static = Path(_require(config["domain"], "static"))

    run.mkdir(parents=True, exist_ok=True)
    for name in CASE_FILES:
        source = base / name
        if not source.exists():
            raise ModelError(
                f"{source} is missing from the model's base case, and SOCA "
                f"needs it to describe the same domain the model integrates"
            )
        _link(run / name, source)

    grid = static / GRIDSPEC
    if not grid.exists():
        raise ModelError(
            f"{grid} does not exist. It is the static stage's product for this "
            f"domain, built once by tools/soca-gridspec.sh and shared by every "
            f"experiment on it, and no cycling job generates one."
        )
    _link(run / GRIDSPEC, grid)

    _write(run / "diag_table", DIAG_TABLE.format(cycle_time(config, cycle)))
    (run / "out").mkdir(exist_ok=True)


def _link(target, source):
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(source)


def _write(target, text):
    target.write_text(text if text.endswith("\n") else text + "\n")


# --- the configuration the application reads ---------------------------------

def hofx_config(config, cycle, observers, *, background):
    """The whole `soca_hofx3d.x` YAML, as a data structure.

    Built rather than templated. Every value here is one an experiment already
    states somewhere else, and the failure this avoids is the one every prior
    workflow had: a template and a configuration that agree until someone edits
    one of them.
    """
    table = symbols(config, cycle)
    model = config["model"]
    return {
        "geometry": {
            "geom_grid_file": f"{GRIDSPEC}",
            "mom6_input_nml": _require(model, "namelist"),
            "fields metadata": _require(model, "fields metadata"),
        },
        # The background, which for a run with no analysis is the previous
        # cycle's forecast and for cycle 1 is the initial condition materialized
        # into `rst/0`. The trailing separator matters: SOCA concatenates
        # basename and filename without one.
        "state": {
            "date": table["current_cycle"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "read_from_file": 1,
            "basename": f"{background}{os.sep}",
            "ocn_filename": _require(model.get("restart") or {}, "ocn"),
            "state variables": list(_require(model, "state variables")),
        },
        # The window the observers select on, which is the cycle's own window
        # and not something hofx gets to choose.
        "time window": {
            "begin": table["window_begin"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "length": table["window_length"],
        },
        "observations": {"observers": [record["config"] for record in observers]},
    }


def _redirect_output(observers, staging):
    """Point every observer's output at scratch, and say where it belongs.

    An application that writes straight to `obs_out/` leaves a truncated file
    there when it is killed, and the next run of the same cycle finds an output
    that exists. Writing to scratch and renaming afterwards is what every other
    task here does; this is the same rule applied to a config value rather than
    to a path in code.
    """
    products = []
    for record in observers:
        final = record["output"]
        if not final:
            raise ModelError(
                f"observer {record['name']} has no obsdataout file, so its "
                f"hofx would run and be discarded"
            )
        local = staging / Path(final).name
        record["config"]["obs space"]["obsdataout"]["engine"]["obsfile"] = str(local)
        products.append((local, Path(final)))
    return products


# --- launching and committing ------------------------------------------------

def launch(config, site, run, task, executable, document, log_name):
    """Run one SOCA application to completion in *run*, or raise with the log."""
    resources = config["domain"].get("resources", {}).get(task, {})
    ntasks = resources.get("ntasks", 1)
    # The same repository-relative path the graph declares, so that what
    # `validate` checks for and what a job runs cannot become two executables.
    binary = Path(site.get("root", ".")) / SOCA_BIN / executable

    launcher = shlex.split(site.get("launcher") or "")
    command = launcher + (["-n", str(ntasks)] if launcher else []) \
        + [str(binary), document]

    log = run / log_name
    with open(log, "wb") as handle:
        result = subprocess.run(command, cwd=run, stdout=handle,
                                stderr=subprocess.STDOUT)
    if result.returncode:
        raise ModelError(
            f"{' '.join(command)} exited {result.returncode}; the run directory "
            f"is kept, see {log}"
        )


def commit(products):
    """Move each observer's output to where the rest of the experiment reads it.

    Copy and rename rather than `os.replace` straight across, because scratch
    and output are different filesystems on a real machine and a rename between
    them fails. These are observation-space files of tens of kilobytes, so the
    copy costs nothing; a restart set would not be moved this way.
    """
    written = []
    for local, final in products:
        if not local.exists():
            raise ModelError(
                f"the application exited 0 but wrote no {local}, so the "
                f"observer it belongs to produced no ombg"
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        temp = final.with_name(final.name + ".partial")
        shutil.copyfile(local, temp)
        os.replace(temp, final)
        written.append(final)
    return written


def _require(mapping, key):
    value = mapping.get(key)
    if not value:
        raise ModelError(f"{key} is not set, and SOCA cannot be configured without it")
    return value
