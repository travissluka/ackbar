"""Turning a merged configuration into a task graph.

Deterministic and side-effect free, which is a constraint and not a
description: `heal` regenerates a subgraph long after the original submission
and has to get the same answer, so nothing here may consult the clock, the
filesystem, or the scheduler.
"""

from ..config.jobtime import (FOUR_D, cycle_length, cycle_time,
                              forecast_overshoot, slot_length, window_length,
                              window_type)
from ..duration import format_duration, parse_duration
from ..observations import LOCALIZATION
from .model import AFTERCORR, Graph, GraphError, Node
from .tasks import (
    CROSS_CYCLE,
    CROSS_CYCLE_NO_ANALYSIS,
    EDGES,
    ROOTS,
    SUPPRESSED_BY,
    TASKS,
    ensemble_covariance,
    ensemble_source,
)

#: Ways of maintaining an ensemble across cycles that ACKBAR can actually run.
#: `letkf` puts an ensemble filter in the cycle beside the deterministic
#: analysis; `none` lets the members run free and only pulls them back onto that
#: analysis, which is a cheaper and genuinely different experiment rather than a
#: degraded version of the first.
#:
#: `eda` and the rest are in the schema because they are the vocabulary, and
#: refused here because a covariance drawn from an ensemble nothing maintains is
#: not an under-specified experiment, it is one whose spread decays to nothing
#: over a few cycles with no error to say so.
ENSEMBLE_SOURCES = ("letkf", "none")


def member_set(config):
    """The canonical member index set.

    Pinned in one place and asserted everywhere, because `aftercorr` behaviour
    on mismatched index ranges is undocumented and a mismatch produces an
    invisible forever-pending job rather than an error.

    Every experiment has one, even a free run, whose single member is the
    control. That is what keeps `mem000` from being a special case: there is no
    ctrl-versus-ens split in the paths, the arrays, or the graph.
    """
    ensemble = config.get("ensemble")
    if not ensemble:
        return (0,)
    size = ensemble["size"]
    if ensemble.get("control", True):
        return tuple(range(0, size + 1))
    return tuple(range(1, size + 1))


def extended_members(config, canonical):
    """Which members get a long forecast. Defaults to the control alone."""
    declared = config.get("forecast", {}).get("extended", {}).get("members")
    if declared is None:
        return (canonical[0],)
    unknown = sorted(set(declared) - set(canonical))
    if unknown:
        raise GraphError(
            f"forecast.extended.members names members that do not exist: "
            f"{unknown}; the ensemble is {canonical[0]} to {canonical[-1]}"
        )
    return tuple(sorted(set(declared)))


def extended_cycles(config, count):
    """The cycles a long forecast runs off.

    Cadence is a setting rather than the cycle period, because a 7 day forecast
    off every 24 hour cycle is seven times the model cost of the cycling itself.
    """
    extended = config.get("forecast", {}).get("extended")
    if not extended:
        return ()
    every = parse_duration(extended["every"]).total_seconds()
    if every <= 0:
        raise GraphError("forecast.extended.every must be a positive duration")
    step = cycle_length(config).total_seconds()
    if every % step:
        raise GraphError(
            f"forecast.extended.every ({extended['every']}) is not a whole "
            f"number of cycles ({config['cycle']['length']}), so the cadence "
            f"would drift"
        )
    stride = int(every // step)
    return tuple(range(1, count + 1, stride))


def extended_length(config):
    """How far the long forecast runs, or None if there is not one."""
    extended = (config.get("forecast") or {}).get("extended")
    if not extended:
        return None
    length = parse_duration(extended["length"])
    if length.total_seconds() <= 0:
        raise GraphError("forecast.extended.length must be a positive duration")
    return length


def _ladder(config, key, default_to_length=True):
    """The leads one cadence under `forecast.extended` names, shortest first."""
    length = extended_length(config)
    if length is None:
        return ()
    stated = (config["forecast"]["extended"]).get(key)
    if not stated:
        return (length,) if default_to_length else ()
    step = parse_duration(stated)
    if step.total_seconds() <= 0:
        raise GraphError(f"forecast.extended.{key} must be a positive duration")
    if length.total_seconds() % step.total_seconds():
        raise GraphError(
            f"forecast.extended.{key} ({stated}) does not divide "
            f"forecast.extended.length ({config['forecast']['extended']['length']}), "
            f"so the last state the model writes would not be at the lead the "
            f"forecast was asked for"
        )
    count = int(length.total_seconds() // step.total_seconds())
    return tuple(step * n for n in range(1, count + 1))


def extended_leads(config):
    """Which leads keep a compressed state, shortest first.

    `keep_states` is optional and its absence means one state, at the end. That
    is a legitimate experiment, it is just not a skill curve: verification
    against lead needs more than one lead to have an axis.

    The cycle-length lead is in this list even though nothing writes a file for
    it. `bkg/` already holds that state, from the cycling forecast, which starts
    from the same set and is therefore the same trajectory; `post.state` links
    it rather than storing it twice. See `Paths.fcst_product`.
    """
    return _ladder(config, "keep_states")


def extended_slots(config):
    """Which leads the model writes a state at, for the departures to use.

    Finer than `extended_leads` and for a different consumer. An observation is
    compared against the state nearest its own time, so this cadence is the
    resolution of every departure the long forecast produces, and a daily one
    would compare a 06Z observation against an 00Z state. These are reaped once
    `hofx.ext` has read them.

    Falls back to the kept leads when `slots` is not set, which makes a forecast
    with no sub-window cadence evaluate its observations against the states it
    was keeping anyway rather than against nothing.
    """
    return _ladder(config, "slots", default_to_length=False) \
        or extended_leads(config)


def extended_lead_cycles(config):
    """The leads a set of departures is produced at, one per cycle covered.

    Departures are per cycle rather than per slot because observations are:
    each covered cycle has an observer set and a window, and `hofx.ext` hands
    the four-dimensional application that window with the slot states inside it.
    So the trajectory is sampled at `slots` and the *output* is one file set per
    cycle, which is also what makes it directly comparable to the cycling
    background's departures at the same time.

    Floors, so that a forecast whose length is not a whole number of cycles
    scores the cycles it does cover rather than refusing.
    """
    length = extended_length(config)
    if length is None:
        return ()
    cycle = cycle_length(config)
    count = int(length.total_seconds() // cycle.total_seconds())
    return tuple(cycle * n for n in range(1, count + 1))


def _check_extended(config):
    """The long forecast's own relations, once its cadences are known."""
    length = extended_length(config)
    if length is None:
        return
    extended = config["forecast"]["extended"]
    leads = extended_leads(config)
    slots = extended_slots(config)

    # Every kept lead lands on an analysis time. That is what makes the
    # verification comparable and the implementation small: the observations a
    # lead is scored against are then exactly some cycle's observers, with that
    # cycle's window, so `hofx.ext` reuses `observations.observers` rather than
    # learning to select at an arbitrary instant, and the score at a lead is
    # against the same observations the cycling background was scored against at
    # that time. Which is the comparison forecast verification is *for*.
    cycle = cycle_length(config)
    if extended.get("keep_states") and \
            parse_duration(extended["keep_states"]).total_seconds() % cycle.total_seconds():
        raise GraphError(
            f"forecast.extended.keep_states ({extended['keep_states']}) is not a whole "
            f"number of cycles ({config['cycle']['length']}), so a kept lead "
            f"would fall between two analysis times and be scored against no "
            f"cycle's observations"
        )

    # The kept states have to be a subset of the states the model writes, or
    # keeping one would mean a second write cadence and a lead the trajectory
    # does not contain.
    step = slots[0]
    for lead in leads:
        if lead.total_seconds() % step.total_seconds():
            raise GraphError(
                f"forecast.extended.slots ({_spell(step)}) does not divide the "
                f"kept lead {_spell(lead)}, so the state that lead names is not "
                f"one the model writes"
            )

    # And the slot cadence tiles a cycle, because `hofx.ext` evaluates one
    # cycle's window at a time: the observations exist per cycle, so the
    # trajectory handed to the four-dimensional application is the slot states
    # inside that cycle's window. A cadence that straddles an analysis time
    # would leave a window with no state at its own centre.
    if step.total_seconds() % cycle.total_seconds() and \
            cycle.total_seconds() % step.total_seconds():
        raise GraphError(
            f"forecast.extended.slots ({_spell(step)}) neither divides nor is a "
            f"multiple of the cycle length ({config['cycle']['length']}), so the "
            f"long forecast's states do not line up with the windows its "
            f"departures are computed over"
        )

    _check_coupling(config, "forecast.extended.slots", step)
    _check_coupling(config, "the extended forecast", length)


def build_graph(config):
    """The whole experiment: every cycle, every task, every edge."""
    _check_ensemble_source(config)
    _check_hours(config)
    _check_window(config)
    _check_four_d_covariance(config)
    _check_ensemble_window(config)
    _check_extended(config)
    count = config["cycle"]["count"]
    canonical = member_set(config)
    graph = Graph(
        experiment=config["experiment"]["name"],
        throttle=config["cycle"].get("throttle", 1),
    )

    enabled = [task for task in TASKS if task.when(config)]
    long_forecast = set(extended_cycles(config, count))
    long_members = extended_members(config, canonical) if long_forecast else ()
    resources = config.get("domain", {}).get("resources", {}) or {}

    for cycle in range(1, count + 1):
        for task in enabled:
            if not _runs_in_cycle(task.name, cycle, count, long_forecast):
                continue
            if task.name in ("forecast.ext", "hofx.ext", "post.fcst"):
                # The same set as the long forecast, and it has to be: the
                # `aftercorr` between them is elementwise by array index, so two
                # different member sets would silently pair member *i* of one
                # with a different member of the other.
                members = long_members
            elif task.member_level:
                members = canonical
            else:
                members = ()
            graph.add(Node(
                cycle=cycle,
                task=task.name,
                members=members,
                exe=task.exe(config),
                resources=resources.get(task.name, resources.get("default", {})),
            ))

        for parent, child, kind in EDGES:
            suppressor = SUPPRESSED_BY.get((parent, child))
            if suppressor and graph.has(cycle, suppressor):
                continue
            graph.link(
                f"{cycle}.{parent}",
                f"{cycle}.{child}",
                _kind(graph, cycle, parent, child, kind),
            )

    _cross_cycle(graph, count)
    _check_roots(graph)
    graph.order()  # fail here rather than leaving Slurm to not notice
    return graph


def _check_ensemble_source(config):
    """A covariance drawn from an ensemble needs that ensemble maintained.

    Here rather than in the schema because it is a relation between two
    subtrees, `solver.covariance` and `ensemble.source`, and a schema that
    expressed it would be read by nobody. It is checked at graph build time
    because that is what every command does first.
    """
    if not ensemble_covariance(config):
        return
    source = ensemble_source(config)
    if source not in ENSEMBLE_SOURCES:
        raise GraphError(
            f"solver.covariance is "
            f"{config['solver'].get('covariance')!r}, so the analysis draws its "
            f"background error from the ensemble, and ensemble.source is "
            f"{source!r}, which nothing in this cycle implements. Use one of "
            f"{', '.join(ENSEMBLE_SOURCES)}: an ensemble that nothing updates "
            f"loses its spread over a few cycles and reports no error while it "
            f"does."
        )
    if not config["ensemble"].get("control", True):
        raise GraphError(
            "solver.covariance draws its background error from the ensemble, "
            "and ensemble.control is false. The centre every member is "
            "recentred onto is the deterministic analysis, which is the "
            "control's; without one there is nothing for the recentring to be "
            "about and the first ensemble member would be treated as the "
            "answer."
        )
    if source != "letkf":
        return
    # `da.ens` is an ensemble filter, so it needs what any ensemble filter
    # needs. The schema asks for these when the *solver* is an LETKF and cannot
    # ask for them here, where what calls for one is a value in another subtree.
    missing = [key for key in ("local ensemble DA", "ensemble distribution")
               if not config["solver"].get(key)]
    if missing:
        raise GraphError(
            f"ensemble.source is 'letkf', so this cycle runs an ensemble "
            f"filter beside the variational analysis, and solver states no "
            f"{' and no '.join(missing)}. Inherit a layer that configures one."
        )
    _check_localized(config)


def _check_localized(config):
    """Every observer an ensemble filter reads is localized by something.

    The failure this exists for produces no error and no odd-looking output: an
    unlocalized sample covariance of twenty members has spurious correlations
    between every pair of points in the domain, so an observation on one side of
    the Gulf moves the analysis on the other, and the field it produces looks
    like a field. Only a comparison against something else shows it.

    Two sources satisfy it, and the reason this is not a schema rule is that
    they live in different subtrees. `solver.ensemble localization` is one
    statement about every observer at once, which is `da/eakf`. Otherwise each
    observer carries its own `$localization`, from `obs/common/*.yaml`, because
    the distance an observation stays representative over is a property of what
    measured it. See `_observers` in `ackbar/soca.py`, which renders whichever
    applies.
    """
    if config["solver"].get("ensemble localization"):
        return
    bare = [(entry.get("obs space") or {}).get("name", "<unnamed>")
            for entry in config.get("observations") or ()
            if not entry.get(LOCALIZATION)]
    if bare:
        raise GraphError(
            f"ensemble.source is 'letkf', so this cycle runs an ensemble "
            f"filter, and {len(bare)} observer(s) would go into it unlocalized: "
            f"{', '.join(bare)}. Give each a {LOCALIZATION!r}, normally by "
            f"inheriting the platform's layer under obs/common/, or state one "
            f"solver.ensemble localization to cover every observer at once."
        )


def _check_hours(config):
    """Every duration an experiment states is a whole number of hours.

    An assumption rather than a discovery, and it is written down here so that
    the places relying on it can rely on it. Two do. `paths.lead_name` spells a
    forecast lead as `F###` in hours and has no spelling for ninety minutes, and
    `paths.CYCLE_DATE` carries minutes and seconds that are always zero. Both
    would be wrong rather than merely awkward for a sub-hourly experiment, and
    wrong in the quiet way: two leads colliding in one directory name, with the
    second overwriting the first.

    Ocean windows are hours at the shortest, so this costs nothing real. It is
    checked rather than assumed because the cost of being wrong is silent, and
    an experiment that states `PT90M` should learn about it at create time and
    not from a verification curve with a lead missing.
    """
    for key, value in (
        ("cycle.length", (config.get("cycle") or {}).get("length")),
        ("solver.window.length",
         ((config.get("solver") or {}).get("window") or {}).get("length")),
        ("forecast.slots", (config.get("forecast") or {}).get("slots")),
    ) + tuple(
        (f"forecast.extended.{name}", extended.get(name))
        for extended in [(config.get("forecast") or {}).get("extended") or {}]
        for name in ("length", "every", "keep_states", "slots")
    ):
        if value is None:
            continue
        if parse_duration(value).total_seconds() % 3600:
            raise GraphError(
                f"{key} is {value}, which is not a whole number of hours. "
                f"ACKBAR names forecast leads in hours (`F###`) and cycle "
                f"directories to the hour, so a sub-hourly duration has no "
                f"spelling and two of them would land in one directory."
            )


def _check_window(config):
    """What the window, the cycle and the sub-window cadence imply about each other.

    Checked here, with the rest of what one subtree implies about another,
    rather than in the schema, which can say `forecast.slots` is a duration and
    not that it is *this* experiment's duration.

    Three relations, and none of them is a rounding question. FMS writes a state
    each time its clock *reaches* the next interval, counting from the start of
    the run, so any time the workflow names that is not a whole number of
    cadences in is a file the model never wrote. Two of the three are the same
    invariant soca-science checked, `(DA_TIMESLOTS-1)*DA_SUBWINDOW_LEN ==
    DA_WINDOW_LEN` in its `cycle.sh`; the third is ACKBAR's, and it exists
    because the forecast here is one run rather than a chain of them, so the
    state the next cycle resumes from is an interval of this one.

    A window that is not shorter than two cycles is refused outright: its own
    start would fall at or before the forecast's, and nothing is written at hour
    zero. That is also what keeps every state one cycle's analysis reads inside
    one forecast, which is what lets `cleanup` reap on the cycle before last.
    """
    step = slot_length(config)
    window = window_length(config)
    cycle = cycle_length(config)
    overshoot = forecast_overshoot(config)

    if window.total_seconds() <= 0:
        raise GraphError("solver.window.length must be a positive duration")
    if window_type(config) in FOUR_D and step is None:
        raise GraphError(
            f"solver.window.type is {window_type(config)!r}, which compares each "
            f"observation against the state nearest its own time, and "
            f"forecast.slots is not set, so the forecast writes no states for it "
            f"to read. Set a sub-window cadence that divides the window "
            f"({_spell(window)})."
        )
    # The run length is constrained by the model's clock whether or not any
    # states are asked of it, so this half is checked before the cadence
    # relations, which only exist once there are slots.
    _check_coupling(config, "the forecast", cycle + overshoot)

    if step is None:
        return
    if step.total_seconds() <= 0:
        raise GraphError("forecast.slots must be a positive duration")

    # Persistence hands one restart set forward and writes nothing in between,
    # so it has nowhere to put a sub-window state. Refused rather than ignored:
    # the forecast would exit 0 having written none of the states it declared,
    # and the skip rule needs the sentinel *and* the outputs, so the task would
    # be rerun forever instead of failing. That is the one configuration
    # `model: persistence` exists to make cheap.
    if (config.get("model") or {}).get("name") == "persistence":
        raise GraphError(
            f"forecast.slots ({config['forecast']['slots']}) asks for a state "
            f"partway through a forecast, and model.persistence does not "
            f"integrate: it hands one restart set forward unchanged. Bring the "
            f"sub-window states up on a real model, or drop forecast.slots and "
            f"the four-dimensional window with it."
        )

    # The window has to fit inside the forecast that covers it, and a window
    # whose analysis reads a state *at* its own start needs it to fit with a
    # cadence to spare: nothing is written at hour zero, so the state there is
    # the set the forecast was handed rather than one it produced. A free
    # running forecast reads nothing and is allowed the flush fit, which is what
    # `W == C` with no overshoot is.
    lead = cycle + overshoot - window
    if lead.total_seconds() < 0 or (overshoot and not lead.total_seconds()):
        raise GraphError(
            f"the window ({_spell(window)}) begins at or before the start of the "
            f"forecast that has to cover it, which runs {_spell(cycle)} plus "
            f"{_spell(overshoot)} of overshoot. The state at hour zero is the "
            f"set that forecast was handed, not one it wrote. Shorten "
            f"solver.window.length or lengthen the cycle."
        )

    for name, quantity, why in (
        ("the window", window,
         "so the sub-window states would not tile it and the analysis would "
         "read a trajectory that stops before its own last observation"),
        ("the cycle length", cycle,
         "so the forecast writes nothing at the time the next cycle starts "
         "from"),
        ("the forecast's lead-in to the window", lead,
         "so the window would begin between two of the forecast's writes"),
    ):
        if quantity.total_seconds() % step.total_seconds():
            raise GraphError(
                f"forecast.slots ({config['forecast']['slots']}) does not divide "
                f"{name} ({_spell(quantity)}), {why}"
            )

    # Last, because a cadence that fails one of the relations above fails it for
    # a reason the experiment's author can act on, and this one would otherwise
    # report a coupling step they never chose.
    _check_coupling(config, "forecast.slots", step)


def _check_four_d_covariance(config):
    """`4d` is 4D-Ens-Var, so it needs an ensemble to be four-dimensional with.

    The combination someone reaches for first, and the only cell in the window
    by covariance table that cannot exist. `3d` and `fgat` are cost functions
    over one increment and take any covariance; `4d` is not a third of those,
    it is 4D-Ens-Var, whose four dimensions *are* the ensemble's. Its members
    are read as trajectories and the covariance is their spread at each
    sub-window.

    A static B has no time dimension to offer, and carrying an increment across
    the window instead needs a linear model, which is what makes 4D-Var 4D-Var.
    SOCA has `Identity` and the HTLM, neither of which is a tangent linear
    ocean, so the honest answer is that this experiment cannot be run here and
    not that it has not been wired up yet.

    Separate from the phase gate in `validate`, which says 4D-Ens-Var is not
    built yet and gets deleted when it is. This one is permanent.
    """
    if window_type(config) != "4d":
        return
    solver = (config.get("solver") or {}).get("name", "none")
    covariance = (config.get("solver") or {}).get("covariance")
    if solver == "variational" and covariance not in ("ensemble", "hybrid"):
        raise GraphError(
            f"solver.window.type is '4d', which is 4D-Ens-Var, and "
            f"solver.covariance is {covariance!r}. The four dimensions of a "
            f"4D-Ens-Var are the ensemble's: its members are read as "
            f"trajectories and the covariance is their spread at each "
            f"sub-window, so a static B has nothing four-dimensional to offer. "
            f"Carrying one across the window instead needs a linear model, "
            f"which is what makes 4D-Var 4D-Var and which SOCA does not have "
            f"for the ocean. Use 'ensemble' or 'hybrid', or 'fgat', which "
            f"compares against the trajectory and solves one increment at the "
            f"window's centre."
        )


def _check_ensemble_window(config):
    """An ensemble filter has a `3d` window and a `4d` one. It has no `fgat`.

    FGAT is defined by what it does between the observation's time and the
    analysis time: it takes the departure at the right time and then carries the
    increment back with the identity, because it has no tangent linear model to
    carry it with. That is the whole method, and it is a concession.

    An ensemble filter never makes that concession, because it never lacks the
    propagator. Its increment is `X_b(t) w`: one weight vector, and a basis that
    is the ensemble's own spread at time *t*. Composing the two gives
    `X_b(t) X_b(t_a)^+`, which is the ensemble's estimate of the tangent linear
    model, so an observation in one sub-window reaches the analysis through
    another sub-window's covariance. Constant weights are not a constant
    increment. 4D-LETKF therefore sits with 4D-Ens-Var and 4D-Var in what it can
    do, and not with FGAT.

    A degenerate middle *is* constructible: departures at their own times scored
    against the ensemble perturbations at the *analysis* time, which is the
    structural analogue of FGAT-Var. It is refused rather than offered because
    it buys nothing here. FGAT-Var earns its inconsistency by escaping the need
    for a linear model; the same trade for an ensemble filter saves only the
    per-slot member states, which `4d` needed anyway to evaluate the departures
    at their own times. So it is the same cost for a worse covariance.

    Refused at graph build rather than left to the analysis, which is where it
    would otherwise land: `letkf_config` never reads `solver.window.type`, so
    `fgat` today is accepted in full and silently runs `3d`.
    """
    if (config.get("solver") or {}).get("name") != "letkf":
        return
    if window_type(config) != "fgat":
        return
    raise GraphError(
        "solver.window.type is 'fgat' and solver.name is 'letkf'. FGAT is the "
        "method that takes each departure at its own time and then carries the "
        "increment back with the identity, because it has no tangent linear "
        "model; an ensemble filter always has one, in the shape of its own "
        "spread at each sub-window, so there is nothing for it to concede. Use "
        "'3d', which compares every observation against the state at the "
        "window's centre, or '4d', which compares each against its own "
        "sub-window and updates through that sub-window's covariance. Going "
        "from one to the other costs only the per-slot member states, which "
        "'fgat' would have had to write anyway."
    )


def _check_coupling(config, name, quantity):
    """What the model's own clock will accept, asked before the job is written.

    `coupler_main` refuses outright a run length that is not a whole number of
    coupled steps, so a window whose half reaches past one lands as a FATAL at
    job start, on every cycle. It is a quieter failure for the states:
    intervals are tested at the bottom of the coupled loop and stamped with the
    step time actually reached, so a cadence finer than the coupling writes on
    the coupling step instead and the times asked for never exist at all. That
    one is caught eventually, by `_claim_slot`, after the whole run has been
    paid for, and blamed on `restart_interval`.

    Only for a model that declares a coupling step, which leaves the stub and
    persistence layers alone.
    """
    seconds = (config.get("model") or {}).get("coupling_seconds")
    if not seconds or quantity is None:
        return
    if quantity.total_seconds() % seconds:
        raise GraphError(
            f"model.coupling_seconds ({seconds}) does not divide {name} "
            f"({_spell(quantity)}). `coupler_main` runs a whole number of "
            f"coupled steps and nothing else, so this is a FATAL at job start "
            f"on every cycle rather than a slow drift."
        )


def _spell(delta):
    """A duration as the configuration would have written it."""
    return format_duration(delta)


def _runs_in_cycle(name, cycle, count, long_forecast):
    if name == "submit":
        # The last cycle has nothing to submit. Without this the experiment
        # never ends, which is how v2's resubmission loop behaved.
        return cycle < count
    if name == "cleanup":
        # Nothing to clean before the first cycle has produced anything.
        return cycle > 1
    if name in ("forecast.ext", "hofx.ext", "post.fcst"):
        return cycle in long_forecast
    return True


def _kind(graph, cycle, parent, child, kind):
    """Upgrade an array-to-array edge to the elementwise form.

    Only `afterok` upgrades. `aftercorr` requires the corresponding task to
    have *succeeded*, so an `afterany` leaf that upgraded would stop tolerating
    the member failure it exists to report.
    """
    if kind != "afterok":
        return kind
    up, down = graph.get(cycle, parent), graph.get(cycle, child)
    if up and down and up.members and up.members == down.members:
        return AFTERCORR
    return kind


def _cross_cycle(graph, count):
    """The only genuine cross-cycle edges: this cycle's forecast feeds the next.

    Everything else is intra-cycle or a leaf with no successor, which is what
    lets post-processing, long forecasts and verification overlap the following
    cycle instead of holding it up.
    """
    for cycle in range(1, count):
        nxt = cycle + 1
        consumers = list(CROSS_CYCLE)
        if not graph.has(nxt, "da"):
            # No analysis, so the restart handoff is forecast to forecast.
            consumers += list(CROSS_CYCLE_NO_ANALYSIS)
        for task in consumers:
            graph.link(
                f"{cycle}.forecast",
                f"{nxt}.{task}",
                _cross_kind(graph, cycle, nxt, task),
            )


def _cross_kind(graph, cycle, nxt, task):
    up, down = graph.get(cycle, "forecast"), graph.get(nxt, task)
    if up and down and up.members and up.members == down.members:
        return AFTERCORR
    return "afterok"


def _check_roots(graph):
    """Every node either has a parent or is a declared root.

    A node with no parent starts as soon as its cycle is submitted. That is
    right for the tasks in ROOTS and for the first cycle, whose inputs are all
    offline, and a bug anywhere else, where it means an edge the table forgot
    and a job that runs before its input exists.
    """
    first = min(graph.cycles) if graph.cycles else None
    stray = []
    for node in graph.nodes:
        if graph.parents(node.id):
            continue
        if node.task in ROOTS or node.cycle == first:
            continue
        stray.append(node.id)
    if stray:
        raise GraphError(
            f"these tasks have no dependency and would be submitted "
            f"immediately: {', '.join(sorted(stray))}"
        )


def job_time_context(config, graph):
    """Every (cycle, member) pair the experiment will ever render config for.

    Distinct pairs rather than distinct nodes: the job-time symbols depend on
    the cycle and the member and on nothing else, so rendering per node would
    repeat identical work once per task.
    """
    pairs = set()
    for node in graph.nodes:
        for member in (node.members or (member_set(config)[0],)):
            pairs.add((node.cycle, member))
    return sorted(pairs)


__all__ = [
    "build_graph",
    "cycle_time",
    "extended_cycles",
    "extended_members",
    "job_time_context",
    "member_set",
]
