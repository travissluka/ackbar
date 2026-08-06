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

from .config.jobtime import cycle_time, member_dir, symbols
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
LETKF_TRACES = ("letkf.yaml", "letkf.log")
RECENTER_TRACES = ("recenter.yaml", "recenter.log")

#: How an application is told to spread observations across ranks. ACKBAR's
#: rather than a layer's, and this is a change from how soca-science expressed
#: it: v2 redefined an `&obs_distribution` anchor in each solver's document, so
#: the value lived with the DA mode and the observers substituted it in.
#:
#: That works exactly until a cycle contains two applications. A hybrid runs a
#: variational analysis and an ensemble filter over the *same* observer
#: configurations, and they need different distributions: a variational solve is
#: global and takes the cheap round robin, while an LETKF solves each point from
#: the observations within its localization radius and needs every rank to hold
#: a halo that wide. One substituted value cannot be both, and v2 patched around
#: that with `sed` markers keyed on whether the LETKF was running solo or inside
#: a `3dhyb`.
#:
#: So the distribution is not a property of the platform, and it is not really a
#: property of the experiment either: it is a property of the application
#: reading the file. ACKBAR sets it, and an observer layer says nothing about
#: it. The halo *size* is the one part an experiment does choose, because it has
#: to be at least the largest localization radius any observer uses; it is
#: `solver.ensemble distribution` and it is stated by the layer that configures
#: the filter.
GLOBAL_DISTRIBUTION = {"name": "RoundRobin"}


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

#: A member's analysis after it has been pulled onto the deterministic one.
#:
#: A name of its own rather than overwriting the analysis it was built from, and
#: the reason is diagnostic rather than technical. The recentring is the step
#: that decides how much of a hybrid's answer the ensemble is allowed to keep,
#: and the only way to see what it did is to have both states. v2 kept a copy
#: for the same reason and had to remember to; here the two are simply different
#: files and `writeback` is told which one it wants.
RECENTERED = ("rcnt", "an")

#: The `type` an application must be given when it writes one state per member.
#:
#: `soca_genfilename` builds a name from `type`, and `ens` is the only value
#: that puts the member index in it: everything else produces one name, which
#: six members then write in turn, leaving one file that is the last member's.
#: The application exits 0 either way. So the type ACKBAR *asks for* is this,
#: and the type it *names the committed file* with is the one in the constant
#: above, because by then the file is in that member's own directory and an
#: index in the name is redundant.
ENSEMBLE_TYPE = "ens"

#: The ensemble's prior and posterior spread. `sprdb` and `sprda` rather than
#: words, because `exp` becomes a dot-separated field of a filename and a dot
#: inside it would make one field two.
#:
#: Not decoration. An ensemble filter fails in two ways that look nothing alike
#: in the departures and identical in any single analysis: the spread collapses
#: and every later cycle ignores its observations, or the spread grows and the
#: filter chases noise. Prior and posterior spread side by side is the record of
#: which is happening, and nothing else in the workflow produces it.
SPREAD_PRIOR = ("sprdb", "an")
SPREAD_POSTERIOR = ("sprda", "an")

#: What an ensemble analysis writes besides the members themselves. All of them
#: land on the control's directory, because `oops::LocalEnsembleDA` writes each
#: with `member` set to 0.
ENSEMBLE_DIAGNOSTICS = (INCREMENT, SPREAD_PRIOR, SPREAD_POSTERIOR)

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

#: Where an ensemble filter's control-level products go when it is *not* the
#: experiment's analysis: a subdirectory of the control's own products.
#:
#: In a pure LETKF the posterior mean is the control's analysis, so it is
#: written as one. In a hybrid it is not: the control's analysis came from the
#: variational solve, which saw the same observations through a covariance the
#: ensemble alone does not have. The filter's mean, increment and spreads are
#: then diagnostics of the ensemble rather than answers, and two of them share a
#: filename with the deterministic analysis and its increment.
ENSEMBLE_PRODUCTS = "ensemble"


def product_name(kind, when):
    """The file SOCA writes for one of `ANALYSIS`, `INCREMENT` or `RECENTERED`."""
    exp, type_ = kind
    return f"ocn.{exp}.{type_}.{when.strftime(FILE_DATE)}.nc"


def product_file(config, cycle, kind):
    """One of this cycle's state products, by name.

    One function, called by whatever writes it and by whatever reads it, because
    the name is constructed inside SOCA from three configuration values and a
    date format, and two spellings of that construction is a writeback that
    silently finds nothing to apply.
    """
    return product_name(kind, cycle_time(config, cycle))


def analysis_file(config, cycle):
    """The analysis state `da` produces and `writeback` reads."""
    return product_file(config, cycle, ANALYSIS)


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


def analysis(config, site, paths, cycle, task, *, background, observers, target,
             ensemble=None):
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
    document = var_config(config, cycle, observers, background=background,
                          ensemble=ensemble)
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


def recenter(config, site, paths, cycle, task, *, center, ensemble, members,
             target):
    """Pull every member of the analysis ensemble onto the deterministic analysis.

    What a hybrid does that an LETKF does not. The ensemble filter's analysis
    ensemble is centred on its own mean, which saw the observations through the
    ensemble covariance alone; the deterministic analysis saw them through the
    hybrid, which is the answer the experiment is producing. Leaving the two
    apart means the members cycle around a centre the experiment does not
    believe, and the ensemble drifts away from the run it is meant to describe.

    Each member keeps its own perturbation about the ensemble mean and is given
    the deterministic analysis as its centre, which is the whole of the
    operation: `member - mean(ensemble) + centre`.

    *center* is the deterministic analysis state, *ensemble* locates each
    member's own, and *target* is a function from a member index to where its
    recentred state belongs.
    """
    run = paths.scratch(cycle, task)
    stage(config, run, cycle)

    document = recenter_config(config, cycle, center=center, ensemble=ensemble,
                               members=members)
    _write(run / "recenter.yaml", yaml.safe_dump(document, sort_keys=False))

    try:
        launch(config, site, run, task, "soca_ensrecenter.x", "recenter.yaml",
               "recenter.log")
    finally:
        keep_traces(run, paths.sub("log") / str(cycle), task, None,
                    names=RECENTER_TRACES)

    name = product_name(RECENTERED, cycle_time(config, cycle))
    written = _positions(run / "out", members, mean=False)
    return commit([(source, target(member) / name)
                   for member, source in written.items()], move=True)


def letkf(config, site, paths, cycle, task, *, backgrounds, observers, members,
          target, departures=None):
    """Assimilate every member of one cycle's ensemble.

    One MPI job for the whole ensemble, which is what an LETKF is: the analysis
    at each point is a weighted combination of the members there, so no member
    can be solved for alone.

    *members* is the ensemble ACKBAR asked for and *backgrounds* is the
    directory holding it, one subdirectory per member. *target* is a function
    from a member index to where that member's analysis goes, because the
    application writes all of them into one directory and they belong in
    different ones.
    """
    if not observers:
        print(f"ackbar: {cycle}.{task} has no observers with input files; the "
              f"analysis for this cycle is the background")
        return []

    run = paths.scratch(cycle, task)
    stage(config, run, cycle)

    products = _redirect_output(observers, run / "out", into=departures)
    document = letkf_config(config, cycle, observers,
                            backgrounds=backgrounds, members=members)
    _write(run / "letkf.yaml", yaml.safe_dump(document, sort_keys=False))

    try:
        launch(config, site, run, task, "soca_letkf.x", "letkf.yaml", "letkf.log")
    finally:
        keep_traces(run, paths.sub("log") / str(cycle), task, None,
                    names=LETKF_TRACES)

    when = cycle_time(config, cycle)
    name = analysis_file(config, cycle)
    states = [(source, target(member) / name)
              for member, source in _positions(run / "out", members).items()]
    states += [(run / "out" / product_name(kind, when),
                target(0) / product_name(kind, when))
               for kind in ENSEMBLE_DIAGNOSTICS]
    return commit(products) + commit(states, move=True)


def _positions(written, members, mean=True):
    """Which file the application wrote for which member.

    Found by looking rather than by predicting the name, which is a deliberate
    exception to how the rest of this module works. SOCA builds an ensemble
    filename as `ocn.<exp>.ens.<index>.<reference date>.<offset>.nc`, and the
    last two fields are a duration oops formats from a date this code supplies
    to itself. The index is the field that has to be read correctly, so it is
    the one that is parsed.

    **That index is a position, not a member number.** `oops::DataSetBase::write`
    numbers what it writes by each state's place in the list it was given, so an
    ensemble of members 1, 2, 4 is written out as 1, 2, 3. The correspondence
    below is therefore to the *sorted* member list, and it is the single thing
    in this file whose being wrong would put one member's analysis into another
    member's directory with nothing to notice. It is checked by count and by the
    contiguity of what was written.

    Index 0 is the ensemble *mean*, which `oops::LocalEnsembleDA` writes when
    asked to save the posterior mean. It goes to the control's directory,
    because that is the whole of what the control's analysis is in an ensemble
    filter: ACKBAR does not separately compute one. The recentring writes no
    mean, since its centre is a state it was handed rather than one it computed,
    and that is what *mean* selects.

    Everything is then renamed to the name the variational analysis writes. The
    index is redundant once the file is in that member's own directory and the
    date is the cycle's, and what is left is a name `writeback` can open without
    being told which solver produced it.
    """
    found = {}
    for path in sorted(written.glob("ocn.*.ens.*.nc")):
        fields = path.name.split(".")
        if len(fields) > 4 and fields[3].isdigit():
            found[int(fields[3])] = path

    ordered = sorted(members)
    expected = set(range(1, len(ordered) + 1)) | ({0} if mean else set())
    if set(found) != expected:
        raise ModelError(
            f"the application exited 0 having written states {sorted(found)} "
            f"into {written}, and {len(ordered)} member(s)"
            f"{' plus a mean' if mean else ''} were asked for. Which file "
            f"belongs to which member is read off that numbering, so a gap in "
            f"it is not something to work around."
        )
    # Position to member, and 0 to 0: the mean is the control's.
    states = {member: found[index + 1] for index, member in enumerate(ordered)}
    return states | {0: found[0]} if mean else states


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
        "observations": {"observers": _observers(observers, GLOBAL_DISTRIBUTION)},
    }


def _observers(observers, distribution):
    """The observer bodies, with the distribution this application needs.

    Set here rather than substituted into the observer layers. See
    `GLOBAL_DISTRIBUTION` for why: a hybrid cycle reads the same observers
    through two applications that need different answers, so it cannot be one
    value in the merged configuration.
    """
    bodies = []
    for record in observers:
        body = dict(record["config"])
        body["obs space"] = dict(body["obs space"], distribution=dict(distribution))
        bodies.append(body)
    return bodies


def var_config(config, cycle, observers, *, background, ensemble=None):
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

    *ensemble* is the member backgrounds a hybrid or ensemble covariance draws
    from, and is absent for a static B. See `background_error`.
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
            "background error": background_error(solver, variables,
                                                 ensemble=ensemble),
            "observations": {
                "observers": _observers(observers, GLOBAL_DISTRIBUTION),
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


def letkf_config(config, cycle, observers, *, backgrounds, members):
    """The whole `soca_letkf.x` YAML, as a data structure.

    The same construction as `var_config`, with one structural difference: the
    background is an *ensemble*.

    oops takes an ensemble either as `members from template`, with a `%mem%`
    pattern and a zero padding, or as `members`, an explicit list. The list is
    what is built here even though the template would fit, because the template
    is the thing that quietly goes wrong: an ensemble with a gap in it needs an
    `except`, the index a member is written out as is its *position* in the
    template rather than its own number, and the two disagree exactly when a
    member is missing. A list of twenty near-identical blocks is verbose in a
    file nobody hand-edits, and in exchange every member's background is a path
    that `ackbar validate` stats before anything is submitted.

    *members* is the ensemble, which is every member index except the control.
    The control's analysis is the ensemble mean, and the driver is what asks for
    it.

    Two things are ACKBAR's rather than a layer's, on the same rule as the
    variational document: they are wrong by omission and the omission is quiet.

    **The driver.** `do posterior observer` is what computes `oman`; without it
    the cycle produces departures against the background only, and `post.obs`
    has half of what it needs. `save posterior mean` is what gives the control
    member an analysis at all.

    **The outputs.** One block per thing saved, and each is required by the
    driver flag that asks for it: `oops::LocalEnsembleDA` throws by name when a
    flag is set and its output block is absent, which is the one failure in this
    document that is loud.
    """
    table = symbols(config, cycle)
    model = config["model"]
    solver = config["solver"]
    date = table["current_cycle"].strftime("%Y-%m-%dT%H:%M:%SZ")

    if not members:
        raise ModelError(
            "the ensemble is empty, so there is nothing to assimilate. Every "
            "member's forecast is missing, and the experiment's "
            "`ensemble.on_missing_member` policy let the cycle continue anyway."
        )

    restart = _require(model.get("restart") or {}, "ocn")
    states = member_states(
        lambda member: backgrounds / member_dir(member) / restart,
        members,
        date=date,
        variables=_require(solver, "background variables"),
    )

    return {
        "geometry": {
            "geom_grid_file": f"{GRIDSPEC}",
            "mom6_input_nml": _require(model, "namelist"),
            "fields metadata": _require(model, "fields metadata"),
        },
        "time window": {
            "begin": table["window_begin"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "length": table["window_length"],
        },
        "background": {"members": states},
        "observations": {
            "observers": _observers(observers,
                                    _require(solver, "ensemble distribution")),
        },
        "local ensemble DA": _require(solver, "local ensemble DA"),
        "driver": {
            # Without this there is no `oman`, and nothing says so.
            "do posterior observer": True,
            # Without this the control member has no analysis.
            "save posterior mean": True,
            "save posterior ensemble": True,
            "save posterior mean increment": True,
            "save prior variance": True,
            "save posterior variance": True,
        },
        # `date` is the reference the ensemble filenames are formed against.
        # It is the analysis time, so the offset in them is zero.
        "output": dict(_written(ANALYSIS), type=ENSEMBLE_TYPE, date=date),
        "output increment": dict(_written(INCREMENT), date=date),
        "output variance prior": dict(_written(SPREAD_PRIOR), date=date),
        "output variance posterior": dict(_written(SPREAD_POSTERIOR), date=date),
    }


def recenter_config(config, cycle, *, center, ensemble, members):
    """The whole `soca_ensrecenter.x` YAML, as a data structure.

    Small, and every part of it is a path or a variable list, which is why it is
    the one analysis document with nothing passed through from a layer: there is
    no science in a recentring, only an arithmetic identity about where an
    ensemble sits.

    The variables are the *analysis* variables rather than the background's, and
    the reason is what the application does with them:
    `x = x_center; x += pert` replaces every field of the member with the
    centre's before adding the perturbation back. Naming a field here that the
    analysis never solved for would recentre it too, so a member would come back
    carrying the control's layer thicknesses, which is a different vertical grid
    for the same water.
    """
    variables = list(_require(config["solver"], "analysis variables"))
    date = symbols(config, cycle)["current_cycle"].strftime("%Y-%m-%dT%H:%M:%SZ")
    model = config["model"]
    center = Path(center)

    return {
        "geometry": {
            "geom_grid_file": f"{GRIDSPEC}",
            "mom6_input_nml": _require(model, "namelist"),
            "fields metadata": _require(model, "fields metadata"),
        },
        "recenter variables": variables,
        "center": {
            "read_from_file": 1,
            "basename": f"{center.parent}{os.sep}",
            "ocn_filename": center.name,
            "date": date,
            "state variables": variables,
        },
        "ensemble": {
            "members": member_states(ensemble, members, date=date,
                                     variables=variables),
        },
        "recentered output": dict(_written(RECENTERED), type=ENSEMBLE_TYPE,
                                  date=date),
    }


def background_error(solver, variables, *, ensemble=None):
    """The B description, assembled from the covariance the experiment asked for.

    Three shapes, and which one is built is `solver.covariance`, which until
    this phase was validated and never read.

    `static`     the SABER block the layer states, and nothing else.
    `ensemble`   the ensemble alone, localized.
    `hybrid`     both, as weighted components.

    *ensemble* is the member states, which only ACKBAR can supply: they are the
    previous cycle's forecasts, one directory per member, and a layer naming
    them would be a layer that has to know the on-disk layout and the cycle
    number. Required by the two covariances that read one, and refused by the
    one that does not, rather than ignored: a static B handed an ensemble is an
    experiment whose author believes it is doing something it is not.

    Public because the analysis is not the only thing that reads this B. The
    ensemble initial condition stage draws its perturbations from the same
    covariance, and it hit the same omission from the other side: with no
    `output variables`, `changeVarTL` produces an increment carrying no fields
    at all, so every member came back exactly equal to the state it was
    perturbed from and nothing said so.
    """
    covariance = solver.get("covariance", "static")
    if covariance not in ("static", "ensemble", "hybrid"):
        raise ModelError(
            f"solver.covariance is {covariance!r}, which is not a covariance "
            f"this builds"
        )
    if (covariance == "static") is bool(ensemble):
        raise ModelError(
            f"solver.covariance is {covariance!r} and "
            f"{len(ensemble or ())} ensemble member(s) were supplied; a static "
            f"covariance reads none and the others read every one"
        )

    if covariance == "static":
        return _static_error(solver, variables)
    if covariance == "ensemble":
        return _ensemble_error(solver, variables, ensemble)

    weights = _require(solver, "hybrid weights")
    return {
        "covariance model": "hybrid",
        "components": [
            {"covariance": _static_error(solver, variables),
             "weight": {"value": _weight(weights, "static")}},
            {"covariance": _ensemble_error(solver, variables, ensemble),
             "weight": {"value": _weight(weights, "ensemble")}},
        ],
    }


def _weight(weights, name):
    """One component's weight, which has to be stated rather than defaulted.

    Halving the static B and adding half an ensemble is the textbook hybrid and
    is not therefore the right answer for any particular ocean, so there is no
    default here. An experiment that does not say what it weighted them at is an
    experiment whose result cannot be attributed to either.
    """
    value = weights.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ModelError(
            f"solver.hybrid weights.{name} is {value!r}; a hybrid covariance "
            f"needs a number for each of its two components"
        )
    return float(value)


def _static_error(solver, variables):
    """The layer's SABER block, with the balance operator's variable lists in it.

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


def _ensemble_error(solver, variables, ensemble):
    """The ensemble component: the members, and what localizes them.

    The localization is the layer's, verbatim, on the same rule as the static
    B's blocks: it is a SABER description of a length scale calibrated offline,
    and ACKBAR has nothing to add to it except the variables it applies to.

    That variable list is what makes the localization multivariate, and it is
    the reason `multivariate strategy: duplicated` belongs in the layer: one
    scale field is read and applied to every analysis variable, rather than one
    per variable. Getting it wrong is quiet in the way this file's other
    omissions are, because saber constructs either way.
    """
    section = dict(_require(solver, "ensemble error"))
    localization = dict(_require(section, "localization"))
    return {
        "covariance model": "ensemble",
        "members": list(ensemble),
        "localization": dict(localization, **{"localization variables": variables}),
    }


def member_states(locate, members, *, date, variables):
    """One state description per member, for an application that reads an ensemble.

    Three of them do: the LETKF's background, a hybrid's ensemble component, and
    the recentring. One function, because a member's state is one path and three
    spellings of it is two chances to read a different ensemble than the one
    that was forecast. *locate* is a member index to the file itself, which is a
    restart for the first two and an analysis for the third.

    An explicit list rather than oops's `members from template`, and the reason
    is the one phase 7 found: `oops::DataSetBase` numbers by position in the
    list it was handed, so a template's `%mem%` and the index a member is
    written out as disagree exactly when a member is missing. A list of twenty
    near-identical blocks is verbose in a file nobody hand-edits, and in
    exchange every member's state is a path `ackbar validate` stats before
    anything is submitted.
    """
    states = []
    for member in sorted(members):
        path = Path(locate(member))
        states.append({
            # The trailing separator matters: SOCA concatenates basename and
            # filename without one.
            "basename": f"{path.parent}{os.sep}",
            "ocn_filename": path.name,
            "read_from_file": 1,
            "date": date,
            "state variables": list(variables),
        })
    return states


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


def _redirect_output(observers, staging, into=None):
    """Point every observer's output at scratch, and say where it belongs.

    An application that writes straight to `obs_out/` leaves a truncated file
    there when it is killed, and the next run of the same cycle finds an output
    that exists. Writing to scratch and renaming afterwards is what every other
    task here does; this is the same rule applied to a config value rather than
    to a path in code.

    *into* moves the committed file into a subdirectory of where the observer
    layer put it, and exists for exactly one case: a hybrid cycle runs two
    applications over the same observers, and both write departures under the
    same configured name. The control's are the experiment's product and keep
    that name; the ensemble's are a diagnostic and go one level down.
    """
    products = []
    for record in observers:
        final = record["output"]
        if not final:
            raise ModelError(
                f"observer {record['name']} has no obsdataout file, so its "
                f"hofx would run and be discarded"
            )
        final = Path(final)
        local = staging / final.name
        record["config"]["obs space"]["obsdataout"]["engine"]["obsfile"] = str(local)
        products.append((local, final.parent / into / final.name if into else final))
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
