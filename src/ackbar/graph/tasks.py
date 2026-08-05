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


def _has_ensemble(config):
    return bool(config.get("ensemble"))


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
        name="b.vt",
        when=lambda config: _solver(config) == "variational",
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
        # One node today because every solver implemented so far runs one
        # application. Hybrid EnVar will not: it needs the EnVar analysis and
        # whatever maintains its ensemble (a LETKF, or another perturbation
        # model) in the same cycle, with different configs and resources. See
        # Open in docs/design.md; splitting this is a hybrid-phase decision.
        description="the analysis. LETKF is one MPI job consuming every member",
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
        when=lambda config: _solver(config) == "letkf" and _has_ensemble(config),
        member_level=True,
        exe=lambda config: f"{SOCA_BIN}/soca_ensrecenter.x",
        description="recentre the analysis ensemble",
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
        name="post.obs",
        when=_has_obs,
        description="observation-space statistics and the realized observer list",
    ),
    TaskDef(
        name="post.state",
        when=lambda config: True,
        member_level=True,
        description="state compression, ensemble mean and spread",
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
    ("stage.obs", "hofx", "afterok"),
    ("b.vt", "da", "afterok"),
    ("da", "recenter", "afterok"),
    ("recenter", "writeback", "afterok"),
    ("da", "writeback", "afterok"),
    ("writeback", "forecast", "afterok"),
    ("forecast", "forecast.ext", "afterok"),
    ("forecast", "post.state", "afterok"),
    # Leaves take afterany: the harvest and the observation statistics are
    # exactly what is most wanted when a member has failed, so they must not be
    # cancelled along with it.
    ("da", "post.obs", "afterany"),
    ("hofx", "post.obs", "afterany"),
    ("da", "verify", "afterany"),
    ("forecast", "verify", "afterany"),
    ("da", "stats", "afterany"),
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
CROSS_CYCLE = ("da", "hofx", "b.vt")
CROSS_CYCLE_NO_ANALYSIS = ("forecast",)
