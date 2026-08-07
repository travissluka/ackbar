"""What tasks exist, when they exist, and what runs them.

One table, because "which tasks does this experiment have" is a question the
configuration answers and nothing else should. v2 answered it with a seven-way
case statement over `DA_MODE`; the axes here are model, solver, covariance,
window and ensemble source, and every entry below reads exactly one of them.

Everything ACKBAR runs is pinned by ACKBAR, so executables are repository
relative paths under `pkg/`, not names found on `$PATH`. A task with no
executable is python and shell only.
"""

from dataclasses import dataclass
from typing import Callable

SOCA_BIN = "pkg/jedi/build/bin"
MOM6SIS2_EXE = "pkg/mom6sis2/ice_ocean_SIS2/build/coupler_main"


@dataclass(frozen=True)
class TaskDef:
    name: str
    #: config -> bool. Whether this experiment runs the task at all.
    when: Callable
    #: Whether the task is submitted as an array over the member index set.
    member_level: bool = False
    #: config -> repository-relative executable path, or None.
    exe: Callable = lambda config: None
    description: str = ""


def _solver(config):
    return config.get("solver", {}).get("name", "none")


def _covariance(config):
    return config.get("solver", {}).get("covariance", "static")


def _has_ensemble(config):
    return bool(config.get("ensemble"))


def ensemble_source(config):
    """What maintains the ensemble from one cycle to the next.

    ACKBAR's name for v2's `DA_PERTURBATION_MODEL`, and a property of the
    ensemble rather than of the solver: an LETKF's ensemble is maintained by its
    own analysis, and a hybrid's is maintained by something the hybrid does not
    otherwise contain.
    """
    return (config.get("ensemble") or {}).get("source", "none")


def ensemble_covariance(config):
    """Whether this analysis draws its B from the ensemble.

    The two covariances that do, `ensemble` and `hybrid`, differ only by whether
    a static component sits beside it with a weight. That is a line in the
    analysis document, not a difference in what the cycle has to contain, so
    everything structural keys off this rather than off the value itself.
    """
    return (_solver(config) == "variational"
            and _covariance(config) in ("ensemble", "hybrid")
            and _has_ensemble(config))


def _has_obs(config):
    return bool(config.get("observations"))


def _da_exe(config):
    return {
        "variational": f"{SOCA_BIN}/soca_var.x",
        "letkf": f"{SOCA_BIN}/soca_letkf.x",
    }[_solver(config)]


def _forecast_exe(config):
    # persistence and stub are real configuration values, not placeholders, and
    # neither one runs a model binary.
    return MOM6SIS2_EXE if config["model"]["name"] == "mom6sis2" else None


#: The per-cycle task set. Order is presentation order.
TASKS = (
    TaskDef(
        name="cleanup",
        when=lambda config: True,
        description="remove the previous cycle's inputs, gated on artifact existence",
    ),
    TaskDef(
        name="stage.obs",
        when=_has_obs,
        description="link this window's observations, drop absent non-required observers",
    ),
    TaskDef(
        name="b.corr_vt",
        # The static B's vertical correlation, so a pure ensemble covariance has
        # nothing here to calibrate. A hybrid does: one of its two components is
        # that same static B.
        when=lambda config: (_solver(config) == "variational"
                             and _covariance(config) != "ensemble"),
        # No executable named on purpose. Which application does this, or
        # whether it is a saber block inside the analysis rather than a task at
        # all, is settled in the variational phase. It is not
        # soca_sqrtvertloc.x, which computes vertical localization for an
        # ensemble covariance: a different quantity from the vertical
        # correlation scales of the static B.
        description=(
            "per-cycle vertical B calibration: vertical scales track the mixed "
            "layer, so unlike horizontal and localization scales they cannot be "
            "precomputed offline"
        ),
    ),
    TaskDef(
        name="da",
        when=lambda config: _solver(config) != "none",
        exe=_da_exe,
        # The analysis that produces the *control's* answer, whichever solver
        # that is. What maintains the ensemble a hybrid draws its B from is
        # `da.ens` below, and the two are separate nodes rather than one node
        # run twice because they are different applications with different
        # configs, different resources and different member cardinality.
        description="the analysis. LETKF is one MPI job consuming every member",
    ),
    TaskDef(
        name="da.ens",
        # Only when something in this cycle has to maintain the ensemble. An
        # LETKF experiment does not have one of these: its `da` already is the
        # ensemble filter. A hybrid whose members are only recentred does not
        # either, and that is `source: none` rather than an omission.
        when=lambda config: (ensemble_covariance(config)
                             and ensemble_source(config) == "letkf"),
        exe=lambda config: f"{SOCA_BIN}/soca_letkf.x",
        description=(
            "the ensemble's own analysis, which is what gives a hybrid's "
            "covariance a flow-dependent ensemble to be drawn from"
        ),
    ),
    TaskDef(
        name="hofx",
        # A free run is `solver: none`; observation evaluation is a property of
        # any run, not a mode. In a DA run the analysis application produces
        # ombg and oman itself, so this task is the free-run path to the same
        # diagnostics, and the OSSE observation generator.
        when=lambda config: _solver(config) == "none" and _has_obs(config),
        exe=lambda config: f"{SOCA_BIN}/soca_hofx3d.x",
        description="observation evaluation for a run with no analysis",
    ),
    TaskDef(
        name="recenter",
        # Not for an LETKF, which is where this used to be. Recentring an
        # ensemble onto a centre it already has is the identity, and the centre
        # of an LETKF's analysis ensemble is its own mean. A hybrid is the case
        # where the centre is something else: the deterministic analysis, which
        # saw the whole observation set through a covariance the ensemble alone
        # does not have.
        #
        # One job over the whole ensemble rather than an array, because that is
        # what `soca_ensrecenter.x` is: the mean it subtracts is a property of
        # every member at once, so no member can be recentred alone.
        when=ensemble_covariance,
        exe=lambda config: f"{SOCA_BIN}/soca_ensrecenter.x",
        description="pull the ensemble onto the deterministic analysis",
    ),
    TaskDef(
        name="writeback",
        when=lambda config: _solver(config) != "none",
        member_level=True,
        description=(
            "produce the restart set the next forecast reads. Direct restart "
            "write first; IAU is an alternate implementation behind the same "
            "edge rather than a different graph"
        ),
    ),
    TaskDef(
        name="forecast",
        when=lambda config: True,
        member_level=True,
        exe=_forecast_exe,
        description="the cycling forecast, which produces the next background",
    ),
    TaskDef(
        name="forecast.ext",
        when=lambda config: bool(config.get("forecast", {}).get("extended")),
        member_level=True,
        exe=_forecast_exe,
        description="long forecast on its own cadence, a leaf with no successor",
    ),
    TaskDef(
        name="hofx.ext",
        # Unlike `hofx`, this runs whatever the solver is. In a DA run the
        # analysis application produces departures for the *cycling* background
        # and for nothing else, so without this the long forecast is the one
        # thing in the experiment with no observation-space score, which is the
        # score forecast verification is made of.
        # The four-dimensional application, because that is what a forecast
        # trajectory is: states at a cadence, and each observation compared
        # against the one nearest its own time. The three-dimensional one would
        # need a run per slot and would compare a 06Z observation against an 00Z
        # state.
        #
        # One application run per cycle the forecast covers, rather than one
        # over the whole five days. That is a memory decision as much as a
        # bookkeeping one: the four-dimensional application holds the whole
        # trajectory it is given, so a single run over a five day forecast at a
        # three hour cadence holds forty states at once. At `gom_25km` that is
        # nothing; on a real domain it is the thing that will not fit, and the
        # per-cycle split is already the smaller unit to fall back to. If even a
        # cycle is too much the next split is by sub-window, which needs no
        # graph change, only a loop inside the body.
        when=lambda config: (bool(config.get("forecast", {}).get("extended"))
                             and _has_obs(config)),
        member_level=True,
        exe=lambda config: f"{SOCA_BIN}/soca_hofx.x",
        description="observation evaluation of the long forecast, lead by lead",
    ),
    TaskDef(
        name="post.fcst",
        # Not folded into `post.state`, and the reason is the graph rather than
        # tidiness. `post.state` is an array over every member hanging off the
        # cycling forecast; the long forecast runs for a subset, so an edge from
        # it into `post.state` could not upgrade to `aftercorr` and would make
        # one failed long forecast member cancel the record of every member's
        # background. Its own task hangs off `forecast.ext` elementwise instead.
        when=lambda config: bool(config.get("forecast", {}).get("extended")),
        member_level=True,
        description="the compressed states of the long forecast, lead by lead",
    ),
    TaskDef(
        name="post.obs",
        when=_has_obs,
        description="observation-space statistics and the realized observer list",
    ),
    TaskDef(
        name="post.state",
        when=lambda config: True,
        member_level=True,
        description="the compressed state record that outlives the cycle",
    ),
    TaskDef(
        name="verify",
        when=lambda config: True,
        description="scoring against the verification source",
    ),
    TaskDef(
        name="stats",
        when=lambda config: True,
        description="harvest this cycle's resource usage into stats/<cycle>.json",
    ),
    TaskDef(
        name="submit",
        when=lambda config: True,
        description="submit the next cycle; this is what makes cycling daemon-free",
    ),
)

BY_NAME = {task.name: task for task in TASKS}

#: Intra-cycle dependencies, as (parent, child, kind). An edge whose endpoints
#: are not both present in a cycle is simply not created, so this table is
#: written once for the whole workflow rather than per DA mode.
#:
#: `aftercorr` gives array-to-array elementwise edges, so member *i* proceeds
#: without waiting for the rest. It is upgraded from the `afterok` written here
#: whenever both endpoints turn out to be arrays over the same index set.
EDGES = (
    ("stage.obs", "da", "afterok"),
    ("stage.obs", "da.ens", "afterok"),
    ("stage.obs", "hofx", "afterok"),
    ("b.corr_vt", "da", "afterok"),
    # The ensemble's analysis before the deterministic one, and this is an
    # ordering rather than a data dependency: the hybrid's covariance is built
    # from the ensemble's *backgrounds*, which the previous cycle produced, so
    # nothing here waits on a file.
    #
    # What it serializes is the divergence policy. Exactly one job may apply it,
    # because `replace_from_mean` rebuilds a member's restart set and two jobs
    # doing that at once write the same file; and the ensemble the deterministic
    # analysis draws its B from has to be the ensemble the filter assimilated,
    # or the two halves of one hybrid describe two different ensembles.
    ("da.ens", "da", "afterok"),
    ("da", "recenter", "afterok"),
    ("da.ens", "recenter", "afterok"),
    ("recenter", "writeback", "afterok"),
    ("da", "writeback", "afterok"),
    ("writeback", "forecast", "afterok"),
    ("forecast", "forecast.ext", "afterok"),
    ("forecast", "post.state", "afterok"),
    # Elementwise, because both endpoints are arrays over the same members, so
    # this upgrades to `aftercorr` and member *i*'s evaluation waits only on
    # member *i*'s forecast. That also keeps the stranding hazard contained: a
    # long forecast member that fails strands its own evaluation and no one
    # else's, which is the same shape as `forecast -> post.state`.
    #
    # It reads the observation archive directly rather than waiting on
    # `stage.obs`, because the observations it needs are valid days *ahead* of
    # this cycle and `stage.obs` stages this cycle's. Where a lead reaches past
    # the end of the archive the observer drops for that lead and says so, which
    # is the behaviour `observations.py` already has for a gap.
    ("forecast.ext", "hofx.ext", "afterok"),
    ("forecast.ext", "post.fcst", "afterok"),
    # Leaves take afterany: the harvest and the observation statistics are
    # exactly what is most wanted when a member has failed, so they must not be
    # cancelled along with it.
    ("da", "post.obs", "afterany"),
    ("da.ens", "post.obs", "afterany"),
    ("hofx", "post.obs", "afterany"),
    ("da", "verify", "afterany"),
    ("da.ens", "verify", "afterany"),
    ("forecast", "verify", "afterany"),
    # There is deliberately no `post.state -> verify` edge, and tier 2 is what
    # settled it. Scoring does read the compressed record rather than the
    # restarts, so the edge looks right on paper. What it does in practice is
    # strand `verify`: `post.state` is an array whose elements hang off the
    # forecast by `aftercorr`, so a failed forecast member leaves its element
    # *stranded* rather than failed, a stranded element never terminates on a
    # permissive Slurm, and an `afterany` waiting on it never fires. The leaf
    # most wanted when something failed would be the one thing that never ran.
    #
    # So `verify` keys off artifact existence instead, which is the same rule
    # `cleanup` follows and for the same reason. Its `afterany` on the forecast
    # already puts it after the states it reads; whichever records exist when it
    # runs are the ones it scores, and it says which those were.
    ("da", "stats", "afterany"),
    ("da.ens", "stats", "afterany"),
    ("forecast", "stats", "afterany"),
    ("forecast", "submit", "afterok"),
)

#: An edge that exists only when another task is absent. `da -> writeback`
#: applies when nothing recentres in between; when recentre does exist it owns
#: that edge instead, and keeping both would make writeback wait on the whole
#: analysis as well as on its own member.
SUPPRESSED_BY = {("da", "writeback"): "recenter"}

#: Tasks that legitimately depend on nothing inside the experiment, and are
#: therefore roots of their cycle. Anything else without a parent is an edge the
#: table forgot, which is a job that runs before its input exists.
#:
#: `cleanup` keys off artifact existence rather than job state, deliberately:
#: keying off job state means a retried cleanup evaluates a regenerated subgraph
#: with new job ids, concludes the old consumers are gone, and deletes restarts
#: that a resubmitted consumer is about to read.
#:
#: `stage.obs` reads the observation archive, which is an offline stage that
#: exists before the experiment starts. Nothing in the experiment produces it,
#: so nothing in the experiment gates it.
ROOTS = ("cleanup", "stage.obs")

#: Cross-cycle edges: what in cycle n+1 consumes cycle n's forecast. The only
#: genuine cross-cycle dependency there is, which is what lets everything else
#: overlap the following cycle.
#:
#: `forecast` appears here only when there is no analysis, because with one the
#: path already runs through da -> writeback -> forecast.
CROSS_CYCLE = ("da", "da.ens", "hofx", "b.corr_vt")
CROSS_CYCLE_NO_ANALYSIS = ("forecast",)
