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

**The document's shape is a file, its values are here.** Each application has a
template under `config/soca/`, holding the blocks, their names, their nesting
and the reasons for them. This module fills the `$(UPPERCASE)` slots in one and
writes the result. See `ackbar/config/template.py` for the rule that keeps a
template from becoming the thing every previous workflow got wrong: a value
Python also reads is never written in a template, only slotted, because two
spellings of a filename field is a `writeback` that finds nothing.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import yaml

from .config.jobtime import (FOUR_D, cycle_time, member_dir, slot_length,
                             window_bounds, window_type)
from .config.jobtime import render as render_jobtime
from .config.jobtime import symbols
from .config.template import fill
from .duration import format_duration, format_instant
from .duration import ISO_INSTANT
from .graph.tasks import SOCA_BIN
from .mom6sis2 import (OVERRIDE, SOCA_OVERRIDE, ModelError, keep_traces,
                       link_override)

#: The document templates, in the checkout. `create` freezes a copy into the
#: experiment's own `cfg/soca/`, and that is the copy a job reads; this is the
#: fallback for the tools and tests that have no experiment directory.
TEMPLATES = Path(__file__).resolve().parents[2] / "config" / "soca"

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

#: The SOCA applications this module runs, and the executable each one is.
#:
#: The key is a naming convention rather than a label: `var` is the template
#: `config/soca/var.yaml`, the document `var.yaml` written into the run
#: directory, the log `var.log` beside it, and the two files `keep_traces`
#: carries out of scratch. One name per application, so that adding a fifth is
#: a template and a line here.
APPLICATIONS = {
    "hofx": "soca_hofx3d.x",
    # The four-dimensional form, which is what a forecast trajectory needs: each
    # observation is compared against the state nearest its own time rather than
    # against one state standing in for the whole window.
    "hofx4d": "soca_hofx.x",
    "var": "soca_var.x",
    # The same executable as `var`, and a separate entry because the key names a
    # *document* rather than a binary: `varfgat.yaml` beside `varfgat.log` is
    # what says which cost function a cycle actually solved, and that is not
    # recoverable afterwards from the log of a solve that ran fine.
    "varfgat": "soca_var.x",
    # And the third, for the same reason. All three are `soca_var.x`; which cost
    # function a cycle solved is a property of the document it was handed, and
    # the name is what makes that answerable from the trace afterwards.
    "var4d": "soca_var.x",
    "letkf": "soca_letkf.x",
    "recenter": "soca_ensrecenter.x",
    # The vertical half of the background error's correlation, recalibrated
    # against the cycle's own background. The same executable the offline
    # `tools/soca-diffusion.sh` runs, and deliberately so: an experiment that
    # cycles this and one that does not must be reading a file built by the
    # same operator, or the two are not comparable.
    "vt": "soca_error_covariance_toolbox.x",
}

#: Which template each window type builds its analysis from. Three siblings
#: rather than one file with branches in it, because `4d` is not `3d` with a
#: different cost type: its background is a list of states and its ensemble is
#: one trajectory per member.
COST_TEMPLATE = {"3d": "var", "fgat": "varfgat", "4d": "var4d"}


def cost_template(config, trajectory):
    """Which document this cycle's analysis is built from.

    The window type, except for the first cycle of a four-dimensional
    experiment, which has no trajectory because nothing ran before it to write
    one. Its background is the staged initial condition and its window holds
    exactly one state, which is 3D-Var: a four-dimensional cost function
    compares each observation against the state nearest its own time, and when
    there is one state that is the same comparison. Solving it as FGAT over a
    single-entry pseudo model would step off the end of the list on the first
    observation, and as 4D-Ens-Var it would be an ensemble with one sub-window,
    which is a 3D-EnVar spelled at three times the cost.

    *trajectory* distinguishes the two cases it has to. `None` is "there is no
    predecessor", which is a fact about cycle 1 and is what this reads. An empty
    mapping is "the predecessor wrote nothing", which is a failure and is
    refused in `_trajectory` rather than quietly solved as 3D-Var.
    """
    window = window_type(config)
    if window in FOUR_D and trajectory is None:
        return "var"
    return COST_TEMPLATE.get(window)

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

#: What a per-cycle vertical calibration is called, without the `.nc` saber
#: appends. The same word the offline stage uses, so that the `filepath` an
#: analysis reads differs between static and cycled only in its directory.
VT_STEM = "vt"

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


def traces(name):
    """An application's config and log, kept out of scratch on the way past.

    Both, and the config as much as the log: what an application read is half of
    why it did what it did, and scratch is purged.
    """
    return (f"{name}.yaml", f"{name}.log")


def run_application(name, config, site, paths, cycle, task, document):
    """Run one SOCA application to completion, and keep its two traces.

    The shape every task in this module has. A run directory holding the case's
    grid files, the *document* the caller built written into it, and a launch
    whose config and log are carried out of scratch whether or not it succeeded.

    What differs between the four is the document they build and what they
    commit afterwards, which is exactly why those stay with the caller and this
    does not. Returns the run directory, since the caller's products are in it.
    """
    run = paths.scratch(cycle, task)
    stage(config, run, cycle)
    _write(run / f"{name}.yaml", yaml.safe_dump(document, sort_keys=False))

    try:
        launch(config, site, run, task, APPLICATIONS[name],
               f"{name}.yaml", f"{name}.log")
    finally:
        keep_traces(run, paths.log_dir(cycle), task, None,
                    names=traces(name))
    return run


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

    products = _redirect_output(observers, paths.scratch(cycle, task) / "out")
    run_application(
        "hofx", config, site, paths, cycle, task,
        hofx_config(config, cycle, observers, background=background,
                    templates=paths.templates))
    return commit(products)


def hofx4d(config, site, paths, cycle, task, *, initial, states, observers,
           tstep, begin, length):
    """Evaluate *observers* against a forecast trajectory already on disk.

    *states* maps each state's valid time to the directory holding it, and
    *initial* is where the trajectory starts. `PseudoModel` does not integrate,
    so this costs a read of each state rather than a second forecast.

    One application over the whole trajectory. That is a memory decision as much
    as a bookkeeping one, and it is the one that will need revisiting first on a
    larger domain: the application holds every state it is given at once, so a
    five day forecast at a three hour cadence is forty states resident. The
    fallback is sections, a day at a time, which needs no graph change and no
    path change, only a loop here and a `length` per section.
    """
    if not observers:
        print(f"ackbar: {cycle}.{task} has no observers with input files; "
              f"nothing to evaluate")
        return []

    products = _redirect_output(observers, paths.scratch(cycle, task) / "out")
    run_application(
        "hofx4d", config, site, paths, cycle, task,
        hofx4d_config(config, cycle, observers, initial=initial, states=states,
                      tstep=tstep, begin=begin, length=length,
                      templates=paths.templates))
    return commit(products)


def analysis(config, site, paths, cycle, task, *, background, observers, target,
             ensemble=None, trajectory=None):
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

    products = _redirect_output(observers, paths.scratch(cycle, task) / "out")
    # The document decides its own name, so the trace kept beside the log says
    # which cost function solved this cycle. `var_config` is what refuses a
    # window type nothing builds, and it is called before the run directory is
    # staged so that refusal costs nothing.
    document = var_config(config, cycle, observers, background=background,
                          ensemble=ensemble, trajectory=trajectory,
                          templates=paths.templates)
    run = run_application(
        cost_template(config, trajectory), config, site, paths, cycle, task,
        document)

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
    run = run_application(
        "recenter", config, site, paths, cycle, task,
        recenter_config(config, cycle, center=center, ensemble=ensemble,
                        members=members, templates=paths.templates))

    name = product_name(RECENTERED, cycle_time(config, cycle))
    written = _positions(run / "out", members, mean=False)
    return commit([(source, target(member) / name)
                   for member, source in written.items()], move=True)


def calibrate_vt(config, site, paths, cycle, task, *, background, scales,
                 target):
    """Rebuild the vertical correlation of B against one cycle's background.

    The mixed layer sets how many model levels the analysis may spread a
    surface observation down through, and the offline calibration fixes it to
    the MLD of whatever restart `tools/soca-diffusion.sh` was handed. That is a
    constant standing in for something with a seasonal cycle of a factor of
    several and a response to a single storm. This is the same calibration
    against the state the cycle is about to assimilate into.

    *scales* is the length scale field, already written, and *target* is where
    the calibrated file belongs. Both are the caller's because the scale field
    comes from `ackbar.diffusion` rather than from SOCA, and because where the
    product goes is a fact about the experiment's layout.

    saber appends `.nc` to the stem it is given, here and in the `filepath` an
    analysis reads it back through, so what this returns is the file and what
    the configuration names is the stem.
    """
    run = run_application(
        "vt", config, site, paths, cycle, task,
        vt_config(config, cycle, background=background, scales=scales,
                  templates=paths.templates))
    written = run / "out" / f"{VT_STEM}.nc"
    if not written.exists():
        raise ModelError(
            f"{written} was not written, so the calibration produced no "
            f"vertical correlation for this cycle to be analysed through. The "
            f"application exited 0; its log is beside this task's yaml.")
    return commit([(written, target)], move=True)[0]


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

    products = _redirect_output(observers, paths.scratch(cycle, task) / "out",
                                into=departures)
    run = run_application(
        "letkf", config, site, paths, cycle, task,
        letkf_config(config, cycle, observers, backgrounds=backgrounds,
                     members=members, templates=paths.templates))

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

def build_document(name, config, cycle, slots, *, templates=None):
    """One application's YAML, from its template and the slots a task computed.

    Two passes over the file, in this order and not the other. The job-time
    `{{...}}` tokens are resolved first, against nothing but the template's own
    text, so that a `{{...}}` that happens to appear inside a computed value is
    left exactly as the thing that computed it wrote it. Then the `$(SLOT)`s are
    filled, which is where every value this module builds enters the document.

    *templates* is the directory holding them, which for a job is the
    experiment's frozen `cfg/soca` and for a tool or a test is the checkout's
    own. Read at the moment it is used rather than at import, because a job
    reading its own frozen copy is the whole point of freezing one.
    """
    source = Path(templates or TEMPLATES) / f"{name}.yaml"
    if not source.exists():
        raise ModelError(
            f"{source} does not exist, so there is no document to build. It is "
            f"frozen into the experiment by `ackbar create`, from "
            f"`config/soca/` in the checkout"
        )
    skeleton = render_jobtime(yaml.safe_load(source.read_text()),
                              symbols(config, cycle))
    return fill(skeleton, slots, source=str(source))


def _geometry(model):
    """The one geometry every application here is given.

    `geom_grid_file` is relative because it is linked into the run directory by
    `stage`; the other two are absolute paths the model layer names.
    """
    return {
        "geom_grid_file": f"{GRIDSPEC}",
        "mom6_input_nml": _require(model, "namelist"),
        "fields metadata": _require(model, "fields metadata"),
    }


def _basename(path):
    """A directory as SOCA wants it, which is with the separator on the end.

    SOCA concatenates `basename` and `ocn_filename` without one, so a value
    missing it names a sibling of the directory rather than a file inside it.
    """
    return f"{path}{os.sep}"


def _restart(model):
    """The ocean restart's filename, which every state description names."""
    return _require(model.get("restart") or {}, "ocn")


def _date(config, cycle):
    """The analysis time, as JEDI parses it.

    The instant every state in a cycle's document is stamped with, and for the
    ensemble writers the reference the member filenames are formed against, so
    the offset in them is zero.
    """
    return cycle_time(config, cycle).strftime(ISO_INSTANT)


def hofx_config(config, cycle, observers, *, background, templates=None):
    """The whole `soca_hofx3d.x` YAML. See `config/soca/hofx.yaml`."""
    model = config["model"]
    return build_document("hofx", config, cycle, {
        "GEOMETRY": _geometry(model),
        "BACKGROUND_DIR": _basename(background),
        "RESTART_FILE": _restart(model),
        "STATE_VARIABLES": list(_require(model, "state variables")),
        "OBSERVERS": _observers(observers, GLOBAL_DISTRIBUTION),
    }, templates=templates)


def hofx4d_config(config, cycle, observers, *, initial, states, tstep, begin,
                  length, templates=None):
    """The whole `soca_hofx.x` YAML. See `config/soca/hofx4d.yaml`."""
    model = config["model"]
    restart = _restart(model)
    return build_document("hofx4d", config, cycle, {
        "GEOMETRY": _geometry(model),
        "TSTEP": format_duration(tstep),
        # In time order, and the initial condition is not among them: SOCA's own
        # `hofx_4d_pseudo` starts this list one step in, and a list that
        # repeated the start would step the pseudo model off the end.
        "STATES": [{
            "read_from_file": 1,
            "date": format_instant(when),
            "basename": _basename(directory),
            "ocn_filename": restart,
        } for when, directory in sorted(states.items())],
        "BACKGROUND_DIR": _basename(initial),
        "RESTART_FILE": restart,
        "STATE_VARIABLES": list(_require(model, "state variables")),
        "WINDOW_BEGIN": format_instant(begin),
        "WINDOW_LENGTH": format_duration(length),
        "OBSERVERS": _observers(observers, GLOBAL_DISTRIBUTION),
    }, templates=templates)


def _observers(observers, distribution, localization=None):
    """The observer bodies, with what this application needs added to each.

    Set here rather than substituted into the observer layers. See
    `GLOBAL_DISTRIBUTION` for why: a hybrid cycle reads the same observers
    through two applications that need different answers, so it cannot be one
    value in the merged configuration.

    *localization* is the observation-space localization an ensemble filter
    applies, and it is here for the same reason and one more. Being per
    application is the same reason. The other is that it belongs to **every**
    observer, and the only way to say that in a merged configuration is to name
    them one at a time: the layers merge on `obs space.name`, so a `da/letkf`
    that spelled its localization out per platform did two things at once. It
    left every platform it had not heard of with no localization, and it
    invented an observer for every platform it named that the experiment did
    not carry.

    Which is what happened. The three filter layers named `adt_3a` and
    `sst_noaa19`, so the OSSE's seven real platforms went into a twenty member
    LETKF with no localization at all, and two phantom observers appeared whose
    only trace was an unreadable output file in the cycle summary. The second
    failure is visible if you look; the first is not visible in any output the
    run produces, and it is the one that matters, because an unlocalized sample
    covariance of twenty members has spurious correlations between every pair of
    points in the domain.

    An observer that states its own `obs localizations` keeps it. Nothing does
    today. The one that will is a platform whose footprint argues for a
    different radius than the domain's Rossby scale, which is a statement about
    the platform and belongs in its layer.
    """
    bodies = []
    for record in observers:
        body = dict(record["config"])
        body["obs space"] = dict(body["obs space"], distribution=dict(distribution))
        if localization is not None and "obs localizations" not in body:
            body["obs localizations"] = [dict(entry) for entry in localization]
        bodies.append(body)
    return bodies


def var_config(config, cycle, observers, *, background, ensemble=None,
               trajectory=None, templates=None):
    """The whole `soca_var.x` YAML. See `config/soca/var.yaml`.

    The values come from where the experiment already states them: the geometry
    from the model layer, the background from the previous cycle's restart set,
    the window from the cycle, the observers from `stage.obs`. Two subtrees are
    passed through verbatim from `config/layers/da/variational.yaml`, because
    they are the only parts nothing else in the experiment implies: the
    background error, and the minimizer.

    *ensemble* is the member backgrounds a hybrid or ensemble covariance draws
    from, and is absent for a static B. See `background_error`.
    """
    model = config["model"]
    solver = config["solver"]
    window = window_type(config)
    name = cost_template(config, trajectory)
    if name is None:
        raise ModelError(
            f"solver.window.type is {window!r}, which nothing builds. The "
            f"three that do are {', '.join(sorted(COST_TEMPLATE))}."
        )

    variables = list(_require(solver, "analysis variables"))
    geometry = _geometry(model)
    restart = _restart(model)
    background_variables = list(_require(solver, "background variables"))

    # The background is the one part whose *shape* differs between the three.
    # 3D-Var and FGAT correct a single state and name it by directory; a
    # 4D-Ens-Var has one per sub-window and there is no single state to name.
    if name == "var4d":
        states = _window_states(
            config, cycle, trajectory, restart, background_variables)
        shaped = {
            "SUBWINDOW": format_duration(slot_length(config)),
            "BACKGROUND_STATES": states,
            # One write configuration per sub-window, because the increment is
            # one per sub-window. `output` above is not: it is enrolled as a
            # `StateWriter` post-processor and is handed one state at a time,
            # while the increment goes through `ControlIncrement::write` into
            # `DataSetBase::write`, which asserts `confs.size() == ntimes_` and
            # otherwise asserts there is exactly one time.
            #
            # SOCA's own `4denvar` test hides this by having no `final` block at
            # all, so it never writes an increment. Dropping ours would be the
            # smaller change and the wrong one: the increment is the one field
            # that answers "did this cycle do anything" without comparing two
            # experiments.
            #
            # The entries are identical. `soca_genfilename` dates each file from
            # the state it is writing, so five copies of one configuration
            # produce five differently named files, and the one at the analysis
            # time is the one `writeback` reads.
            "INCREMENT_STATES": [_written(INCREMENT) for _ in states],
        }
    else:
        shaped = {
            "BACKGROUND_DIR": _basename(background),
            "RESTART_FILE": restart,
            "BACKGROUND_VARIABLES": background_variables,
            "INCREMENT_OUTPUT": _written(INCREMENT),
        }
        if name == "varfgat":
            shaped.update(_trajectory(config, cycle, trajectory, restart))

    return build_document(name, config, cycle, {
        "GEOMETRY": geometry,
        "ANALYSIS_VARIABLES": variables,
        "BACKGROUND_ERROR": background_error(solver, variables, ensemble=ensemble),
        "OBSERVERS": _observers(observers, GLOBAL_DISTRIBUTION),
        "VARIATIONAL": _variational(solver, geometry),
        "ANALYSIS_OUTPUT": _written(ANALYSIS),
        **shaped,
    }, templates=templates)


def _window_states(config, cycle, trajectory, restart, variables):
    """The background of a 4D-Ens-Var: one state per sub-window boundary.

    Every slot including the window's own start, which is where this differs
    from the pseudo model's list in `_trajectory`. There the states are what a
    model steps *to* and the start is what it steps *from*; here nothing is
    being stepped, and the first sub-window's observations are compared against
    the state at the start.
    """
    if not trajectory:
        raise ModelError(
            f"solver.window.type is '4d', so cycle {cycle}'s background is one "
            f"state per sub-window, and no trajectory was supplied. The "
            f"forecast writes those states at `forecast.slots`."
        )
    return [{
        "read_from_file": 1,
        "date": format_instant(when),
        "basename": _basename(directory),
        "ocn_filename": restart,
        "state variables": list(variables),
    } for when, directory in sorted(trajectory.items())]


def _trajectory(config, cycle, trajectory, restart):
    """The pseudo model's slots, for the FGAT document only.

    *trajectory* maps a valid time to the directory holding the state at it, and
    is the previous cycle's sub-window states: `slot_times` computes them so
    that cycle *n*'s forecast writes exactly cycle *n+1*'s window.

    The window's own start is dropped rather than absent from the caller's map,
    because the same map is what names the background. A pseudo model's first
    state is one step in, and repeating the start would step it off the end of
    the list before the window closed.
    """
    if not trajectory:
        raise ModelError(
            f"solver.window.type is {window_type(config)!r}, which compares "
            f"each observation against the state nearest its own time, and "
            f"cycle {cycle} was given no trajectory to read. The forecast "
            f"writes those states at `forecast.slots`; without them there is "
            f"nothing for the pseudo model to hand out."
        )
    begin, _ = window_bounds(config, cycle)
    states = [{
        "read_from_file": 1,
        "date": format_instant(when),
        "basename": _basename(directory),
        "ocn_filename": restart,
    } for when, directory in sorted(trajectory.items()) if when != begin]

    if not states:
        raise ModelError(
            f"cycle {cycle}'s window contains no state after its own start, so "
            f"the pseudo model has nothing to step to. `forecast.slots` has to "
            f"divide the window into at least two."
        )
    return {"TSTEP": format_duration(slot_length(config)), "STATES": states}


def letkf_config(config, cycle, observers, *, backgrounds, members,
                 templates=None):
    """The whole `soca_letkf.x` YAML. See `config/soca/letkf.yaml`.

    The same construction as `var_config`, with one structural difference: the
    background is an *ensemble*.

    *members* is the ensemble, which is every member index except the control.
    The control's analysis is the ensemble mean, and the driver is what asks for
    it.
    """
    model = config["model"]
    solver = config["solver"]
    date = _date(config, cycle)

    if not members:
        raise ModelError(
            "the ensemble is empty, so there is nothing to assimilate. Every "
            "member's forecast is missing, and the experiment's "
            "`ensemble.on_missing_member` policy let the cycle continue anyway."
        )

    restart = _restart(model)
    states = member_states(
        lambda member: backgrounds / member_dir(member) / restart,
        members,
        date=date,
        variables=_require(solver, "background variables"),
    )

    return build_document("letkf", config, cycle, {
        "GEOMETRY": _geometry(model),
        "MEMBER_BACKGROUNDS": states,
        "OBSERVERS": _observers(observers,
                                _require(solver, "ensemble distribution"),
                                _require(solver, "ensemble localization")),
        "LOCAL_ENSEMBLE_DA": _require(solver, "local ensemble DA"),
        "ANALYSIS_OUTPUT": _written(ANALYSIS, type=ENSEMBLE_TYPE, date=date),
        "INCREMENT_OUTPUT": _written(INCREMENT, date=date),
        "SPREAD_PRIOR_OUTPUT": _written(SPREAD_PRIOR, date=date),
        "SPREAD_POSTERIOR_OUTPUT": _written(SPREAD_POSTERIOR, date=date),
    }, templates=templates)


def recenter_config(config, cycle, *, center, ensemble, members, templates=None):
    """The whole `soca_ensrecenter.x` YAML. See `config/soca/recenter.yaml`."""
    variables = list(_require(config["solver"], "analysis variables"))
    date = _date(config, cycle)
    center = Path(center)

    return build_document("recenter", config, cycle, {
        "GEOMETRY": _geometry(config["model"]),
        "ANALYSIS_VARIABLES": variables,
        "CENTER_DIR": _basename(center.parent),
        "CENTER_FILE": center.name,
        "MEMBER_ANALYSES": member_states(ensemble, members, date=date,
                                         variables=variables),
        "RECENTERED_OUTPUT": _written(RECENTERED, type=ENSEMBLE_TYPE, date=date),
    }, templates=templates)


def vt_config(config, cycle, *, background, scales, templates=None):
    """The whole `soca_error_covariance_toolbox.x` YAML. See `config/soca/vt.yaml`.

    The vertical scheme is not a slot. It is written literally in the template,
    where it must match `config/layers/da/variational.yaml`: the file this
    writes is read back through that block, and a normalization computed with
    one operator and applied by another is a background error wrong by a factor
    nothing reports. Filling both from the same solver block used to make them
    unable to disagree. Keeping them equal is now a thing to do by hand, which
    is the cost of the template saying what it configures.
    """
    model = config["model"]
    background = Path(background)

    return build_document("vt", config, cycle, {
        "GEOMETRY": _geometry(model),
        "BACKGROUND": {
            "read_from_file": 1,
            "basename": _basename(background.parent),
            "ocn_filename": background.name,
            "date": _date(config, cycle),
            # One field, because one is all the calibration reads: it needs a
            # state to define the geometry against and the scales come from the
            # separate file below.
            "state variables": ["sea_water_potential_temperature"],
        },
        "SCALES_DATE": _date(config, cycle),
        "SCALES_FILE": Path(scales).name,
        "WRITE_STEM": f"out/{VT_STEM}",
    }, templates=templates)


def vertical_scale_spec(solver):
    """What `ackbar.diffusion.vertical_scales` needs, from the experiment.

    Two numbers with two different homes, joined here. `method` and
    `iterations` describe the operator and come from the same block the
    analysis reads the calibration back through, so the two cannot disagree.
    The floor is not saber's: it is a statement about how tightly the analysis
    may correlate below the mixed layer, `config/static/diffusion.yaml` holds
    it for the offline stage, and that file is deliberately unreadable from a
    job. So a cycling experiment states it, which is what `da/vt_cycled` does.
    """
    vertical = _vertical_block(solver)
    floor = solver.get("vertical scale floor")
    if floor is None:
        raise ModelError(
            "solver states no `vertical scale floor`, which is the smallest "
            "vertical correlation `b.vt` may produce, in model levels. It has "
            "no default here on purpose: the offline stage takes it from "
            "`config/static/diffusion.yaml`, a job cannot read that file, and "
            "a calibration built with a different floor than the domain's "
            "static one is not comparable with it. `da/vt_cycled` states it.")
    return {"min": float(floor),
            "method": _require(vertical, "method"),
            "iterations": _require(vertical, "iterations")}


def _vertical_block(solver):
    """The `vertical:` mapping of the group that carries the tracers.

    Found by looking for it rather than by index, because the groups are a list
    an experiment may reorder and only one of them has a vertical: sea surface
    height is a single level and has no vertical correlation to describe.
    """
    groups = (((solver.get("background error") or {})
               .get("saber central block") or {})
              .get("read") or {}).get("groups") or []
    for group in groups:
        if "vertical" in group:
            return group["vertical"]
    raise ModelError(
        "no group under `solver.background error` states a `vertical:`, so "
        "there is no vertical correlation to recalibrate. `b.vt` runs because "
        "the experiment asked for `solver.vertical background error: cycled`; "
        "one of the two is wrong.")


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


def member_trajectories(locate, members, *, slots, variables):
    """One *trajectory* per member, for 4D-Ens-Var's ensemble covariance.

    `member_states`'s four-dimensional form, and the same list-not-template
    reasoning applies: `oops::DataSetBase` numbers members by position in the
    list it was handed. What changes is that each element carries a `states`
    list instead of being a state, because the covariance at each sub-window is
    the ensemble's spread there.

    *locate* takes a member and a valid time. *slots* is every sub-window
    boundary including the window's own start, which is where this differs from
    the pseudo model's list in `varfgat.yaml`: those states are what a model
    steps *to* and start one step in, and these are what the observations in
    each sub-window are compared against, so the first sub-window needs the one
    at the start.

    Every member gets the same times, which oops asserts rather than tolerates:
    a member short one slot would otherwise contribute a covariance sampled over
    a shorter window than the rest.
    """
    return [{
        "states": [{
            "basename": f"{Path(locate(member, when)).parent}{os.sep}",
            "ocn_filename": Path(locate(member, when)).name,
            "read_from_file": 1,
            "date": format_instant(when),
            "state variables": list(variables),
        } for when in sorted(slots)],
    } for member in sorted(members)]


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


def _written(kind, **extra):
    """Where SOCA puts one of its own products, and what it calls it.

    `datadir` is the run directory's `out`, like every observer's output and for
    the same reason: an application killed partway leaves a truncated file, and
    the only place that is safe is one nothing else reads.

    *extra* is what a per-member writer adds: a `date`, which is the reference
    its filenames are formed against, and for the members themselves a `type`
    that overrides the committed name's. See `ENSEMBLE_TYPE`.
    """
    exp, type_ = kind
    return {"datadir": "out", "exp": exp, "type": type_,
            "date colons": False, **extra}


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
