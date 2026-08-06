"""Running a SOCA application for one cycle.

`hofx` and the variational analysis, and the shape is the same one `mom6sis2.py`
uses: build a run directory, launch, move the product to where the rest of the
experiment reads it. What differs is that a SOCA application is configured by a
YAML file it is handed, so the run directory exists for a smaller reason: SOCA
still initializes a MOM6 geometry, and MOM6 is configured by the directory it is
started in.

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
from .mom6sis2 import (OVERRIDE, SOCA_OVERRIDE, ModelError, keep_traces,
                       link_override)

#: What the static stage produces and every application here reads.
GRIDSPEC = "soca_gridspec.nc"

#: Files linked from the model's stock case, and the whole of what MOM6 needs in
#: order to describe its own domain. Not the case wholesale, unlike a forecast:
#: everything else in it configures an integration that is not happening.
#:
#: `MOM_override` is absent here and staged separately, because it is ACKBAR's
#: file rather than the case's and must be the same bytes the forecast reads.
#: `INPUT` is absent for a different reason: the data half of a case is a path
#: the domain layer names, not a subdirectory of the text half.
CASE_FILES = ("MOM_input",)

#: FMS reads a `diag_table` while the geometry is being built and treats its
#: absence as fatal, so one is written even though nothing here asks for a
#: diagnostic. Two lines is a whole diag_table: a label and the base date.
DIAG_TABLE = "soca\n{0.year} {0.month} {0.day} {0.hour} {0.minute} {0.second}\n"

#: The application's own config and log, kept out of scratch on the way past.
TRACES = ("hofx.yaml", "hofx.log")
VAR_TRACES = ("var.yaml", "var.log")

#: How SOCA names a file it writes: `<datadir>/ocn.<exp>.<type>.<date>.nc`, from
#: `soca_genfilename`. `type` is SOCA's own closed vocabulary ("an", "incr",
#: "fc", "ens") and decides how the date is formed, so it is not free text.
#: `exp` is, and ACKBAR uses a fixed word rather than the experiment name: the
#: file already lives in that experiment's own directory, and a name a task can
#: compute without consulting the configuration is one `writeback` can open
#: without being told.
#:
#: `date colons: false` in the writer's config is what makes the date
#: `20150105T130000Z` rather than `2015-01-05T13:00:00Z`. Colons in a filename
#: survive a filesystem and then surprise everything that reads `host:path`.
ANALYSIS = ("ana", "an")
INCREMENT = ("incr", "incr")
FILE_DATE = "%Y%m%dT%H%M%SZ"

#: The directory the analysis application's own products go in, inside the
#: member's analysed restart set.
#:
#: A subdirectory rather than loose files, and this matters more than it looks
#: like it should. `ana/<n>/mem###` is a *restart set*: writeback fills it by
#: copying every file of the background's, persistence fills the next cycle's by
#: copying every file of this one, and the forecast links all of them into
#: `INPUT/`. A state file sitting loose among them is inert to the model and is
#: then carried forward by every cycle after it, one more each time.
PRODUCTS = "analysis"


def product_name(kind, when):
    """The file SOCA writes for one of `ANALYSIS` or `INCREMENT`."""
    exp, type_ = kind
    return f"ocn.{exp}.{type_}.{when.strftime(FILE_DATE)}.nc"


def analysis_file(config, cycle):
    """The analysis state `da` produces and `writeback` reads.

    One function, called by both, because the name is constructed inside SOCA
    from three configuration values and a date format, and two spellings of that
    construction is a writeback that silently finds nothing to apply.
    """
    return product_name(ANALYSIS, cycle_time(config, cycle))


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


def analysis(config, site, paths, cycle, task, *, background, observers, target):
    """Solve for one cycle's analysis, into *target*.

    Two kinds of product, and they are committed together because a run that
    produced one and not the other is a cycle nobody can interpret: the analysis
    state, which `writeback` turns into a restart set, and the departures, which
    are the experiment's actual output.

    Returns the files written, or the empty list when the cycle had no
    observations to assimilate. That is not a failure. Over any real archive
    some window has nothing in it, and the analysis in that window is the
    background: `writeback` says so and hands it across unchanged. Running the
    minimizer against an empty observer set to reach the same answer would be
    the same result at the price of a whole cycle's risk.
    """
    if not observers:
        print(f"ackbar: {cycle}.{task} has no observers with input files; the "
              f"analysis for this cycle is the background")
        return []

    run = paths.scratch(cycle, task)
    stage(config, run, cycle)

    products = _redirect_output(observers, run / "out")
    document = var_config(config, cycle, observers, background=background)
    _write(run / "var.yaml", yaml.safe_dump(document, sort_keys=False))

    try:
        launch(config, site, run, task, "soca_var.x", "var.yaml", "var.log")
    finally:
        keep_traces(run, paths.sub("log") / str(cycle), task, None, names=VAR_TRACES)

    when = cycle_time(config, cycle)
    states = [(run / "out" / product_name(kind, when),
               target / product_name(kind, when))
              for kind in (ANALYSIS, INCREMENT)]
    return commit(products) + commit(states, move=True)


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

    # The same override files the forecast links, from the same configured
    # paths. MOM6 reads its parameters here exactly as it does in a forecast, so
    # a parameter that changed the grid and was applied to only one of them
    # would give the analysis and the model two different domains.
    #
    # Plus `SOCA_OVERRIDE`, which is the one thing the two are allowed to
    # disagree about, because SOCA's MOM6 is built without symmetric memory and
    # cannot configure the open boundaries the forecast runs with. Read the file
    # for how long that is meant to last.
    link_override(config, run, OVERRIDE + (SOCA_OVERRIDE,))
    _link(run / "INPUT", Path(_require(config["model"], "input")))

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


def var_config(config, cycle, observers, *, background):
    """The whole `soca_var.x` YAML, as a data structure.

    Built the same way `hofx_config` is, and from the same places: the geometry
    from the model layer, the background from the previous cycle's restart set,
    the window from the cycle, the observers from `stage.obs`. Two subtrees are
    passed through verbatim from `config/layers/da/variational.yaml`, because
    they are the only parts nothing else in the experiment implies: the
    background error, and the minimizer.

    Four things here are ACKBAR's rather than a layer's, and each is a thing
    that is wrong by omission rather than by being wrong.

    **The background error's variable lists.** A `linear variable change` inside
    a covariance needs `input variables` and `output variables`, and they are
    the analysis variables in both sources. Omitting `input variables` leaves
    `oops::ModelSpaceCovarianceBase::BVars_` a null pointer, which is
    dereferenced not at construction but the first time Jb is evaluated: the
    application reads the whole background, builds every block, prints the
    diffusion it loaded, and *then* segfaults. Built here rather than stated,
    because a layer that restated the analysis variables a third time is a layer
    that can disagree with itself.

    **The inner loop's geometry.** `CostFunction::linearize` reads
    `variational.iterations[].geometry` and throws if it is absent, so every
    iteration needs one. It is the outer geometry, because ACKBAR does not run a
    multi-resolution incremental analysis; the day it does, that is what this
    value becomes and the layer still does not have to know the paths.

    **`output`.** It is what writes the analysis, and it is also what makes the
    departures complete. `oops::Variational` only runs its final cost evaluation
    when something asks for one, and `CostJo` saves `oman` on that evaluation
    and nowhere else, so an analysis configured without an output writes `ombg`,
    no `oman`, and no message about either.

    **`final.increment`.** Analysis minus background, which is the one field
    that answers "did this cycle do anything" without a comparison against
    another experiment.
    """
    table = symbols(config, cycle)
    model = config["model"]
    solver = config["solver"]
    variables = list(_require(solver, "analysis variables"))
    geometry = {
        "geom_grid_file": f"{GRIDSPEC}",
        "mom6_input_nml": _require(model, "namelist"),
        "fields metadata": _require(model, "fields metadata"),
    }
    date = table["current_cycle"].strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "cost function": {
            # 3D-Var is the only window this builds so far. FGAT and 4D differ
            # here and in the graph, which is why `solver.window` is validated
            # and not yet read: a value this ignores is worse than one it
            # rejects.
            "cost type": "3D-Var",
            "time window": {
                "begin": table["window_begin"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "length": table["window_length"],
            },
            "analysis variables": variables,
            "geometry": geometry,
            "background": {
                "read_from_file": 1,
                "basename": f"{background}{os.sep}",
                "ocn_filename": _require(model.get("restart") or {}, "ocn"),
                "date": date,
                # Not `model.state variables`, which is what the observers need
                # read and interpolated. The background error blocks read fields
                # the analysis never solves for, and leaving one out is a block
                # that constructs and then reads a field of zeros.
                "state variables": list(_require(solver, "background variables")),
            },
            "background error": _background_error(solver, variables),
            "observations": {
                "observers": [record["config"] for record in observers],
            },
        },
        "variational": _variational(solver, geometry),
        "output": _written(ANALYSIS),
        # `state component` is not decoration. The increment written here is a
        # `ControlIncrement`, which is the model increment plus the model and
        # observation bias corrections, and it hands each of the three its own
        # subsection. The state's is the only one anything here fills in.
        "final": {"increment": {"output": {"state component": _written(INCREMENT)}}},
    }


def _background_error(solver, variables):
    """The B description, with the balance operator's variable lists filled in.

    Set rather than checked, and set even when the layer already says something,
    for the same reason the geometry is: the answer is the analysis variables,
    the analysis variables are stated once, and a second statement of them is
    only ever a chance to disagree.
    """
    section = dict(_require(solver, "background error"))
    change = section.get("linear variable change")
    if change:
        section["linear variable change"] = dict(
            change, **{"input variables": variables, "output variables": variables})
    return section


def _variational(solver, geometry):
    """The minimizer, with the inner loop geometry filled in."""
    section = dict(_require(solver, "variational"))
    iterations = section.get("iterations")
    if not iterations:
        raise ModelError(
            "solver.variational.iterations is empty, so the analysis would run "
            "no outer loop and return the background"
        )
    section["iterations"] = [dict(entry, geometry=geometry) for entry in iterations]
    return section


def _written(kind):
    """Where SOCA puts one of its own products, and what it calls it.

    `datadir` is the run directory's `out`, like every observer's output and for
    the same reason: an application killed partway leaves a truncated file, and
    the only place that is safe is one nothing else reads.
    """
    exp, type_ = kind
    return {"datadir": "out", "exp": exp, "type": type_, "date colons": False}


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


def commit(products, move=False):
    """Put each of an application's outputs where the rest of the experiment reads it.

    Through a temporary name in the destination directory and then `os.replace`,
    rather than straight across, because scratch and output are different
    filesystems on a real machine and a rename between them fails. The rename
    from the temporary name is within one directory and so is atomic: a file
    that is there is a file that is whole.

    *move* is the difference between an observation-space file and a state.
    Departures are tens of kilobytes and copying them costs nothing; an analysis
    is a copy of every 3D field in the background, and on a global domain that
    is hundreds of megabytes that nothing needs two of. `shutil.move` is a
    rename where it can be and a copy where it cannot.
    """
    written = []
    for local, final in products:
        if not local.exists():
            raise ModelError(
                f"the application exited 0 but wrote no {local}, so the task "
                f"produced nothing for its consumer to read"
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        temp = final.with_name(final.name + ".partial")
        if move:
            temp.unlink(missing_ok=True)
            shutil.move(local, temp)
        else:
            shutil.copyfile(local, temp)
        os.replace(temp, final)
        written.append(final)
    return written


def _require(mapping, key):
    value = mapping.get(key)
    if not value:
        raise ModelError(f"{key} is not set, and SOCA cannot be configured without it")
    return value
