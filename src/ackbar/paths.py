"""Where an experiment's files live.

One module, because "which directory is this" is a question the emitter, the
submitter, every task body, cleanup and healing all ask, and a second spelling
of any one of them is a bug that surfaces only as a missing file eight hours in.

The layout is the one in `docs/design.md`, On-disk layout. Two roots, because on
a real machine scratch and output are different filesystems with different purge
policies, and the site layer names them separately.

A directory under `rst/`, `ana/`, `bkg/` or `obs_out/` is named by **the cycle
that produced it**, not by the valid time of what is in it. So `rst/7` is what
cycle 7's forecast wrote, and cycle 8's analysis reads it. That choice makes a
node's outputs always live under its own cycle number, which is what lets
cleanup work off a cycle count instead of a data-flow analysis.
"""

from dataclasses import dataclass
from pathlib import Path

from .config.jobtime import member_dir

#: Subdirectories of the experiment directory. Mirrored by `done_dir`,
#: `rst_dir` and friends in the experiment-time symbol table, so a config can
#: name the same places without spelling them a second time.
SUBDIRS = ("cfg", "ledger", "stats", "log", "rst", "bkg", "ana", "obs_out", "done")


@dataclass(frozen=True)
class Paths:
    experiment: str
    output_root: Path
    scratch_root: Path

    @classmethod
    def of(cls, config, site):
        return cls(
            experiment=config["experiment"]["name"],
            output_root=Path(site["output_root"]),
            scratch_root=Path(site["scratch_root"]),
        )

    # --- roots ---------------------------------------------------------------

    @property
    def experiment_dir(self):
        return self.output_root / self.experiment

    @property
    def scratch_dir(self):
        return self.scratch_root / self.experiment

    def sub(self, name):
        if name not in SUBDIRS:
            raise KeyError(f"unknown experiment subdirectory {name!r}")
        return self.experiment_dir / name

    # --- per-cycle output ----------------------------------------------------

    def cycle_out(self, kind, cycle):
        return self.sub(kind) / str(cycle)

    def member_out(self, kind, cycle, member):
        return self.cycle_out(kind, cycle) / member_dir(member)

    def observer_list(self, cycle):
        """Which observers this cycle actually ran, and what they read.

        Beside the observation output rather than under `cfg/`, because it is a
        product of the cycle and not of the configuration: the same experiment
        run over a different archive writes a different one. See
        `ackbar/observations.py` for why it is written at all.
        """
        return self.cycle_out("obs_out", cycle) / "observers.json"

    # --- one job -------------------------------------------------------------

    def job_script(self, cycle, task):
        """The emitted batch script. Frozen at create time, never rewritten.

        Per cycle rather than per task, so that `sacct`'s stored job script and
        a reading of the file both answer "what exactly did cycle 7 run" without
        a command line to reconstruct.
        """
        return self.sub("cfg") / str(cycle) / f"{task}.sh"

    def job_log(self, cycle, task, array):
        """The `--output` pattern. Slurm expands it; ACKBAR never globs it.

        `%A_%a` for an array and `%j` otherwise, so a healed attempt lands
        beside the failed one instead of overwriting the evidence.
        """
        stem = "%A_%a" if array else "%j"
        return self.sub("log") / str(cycle) / f"{task}.{stem}.out"

    def sentinel(self, cycle, task, member=None):
        """Written last by a successful task, and the only proof it finished.

        Skip-if-output-exists is not sufficient: a task killed mid-write leaves
        an output that exists and is truncated. See Task completion and
        idempotency in `docs/design.md`.
        """
        name = task if member is None else f"{task}.{member_dir(member)}"
        return self.sub("done") / str(cycle) / f"{name}.json"

    def scratch(self, cycle, task, member=None):
        name = task if member is None else f"{task}.{member_dir(member)}"
        return self.scratch_dir / str(cycle) / name

    # --- experiment-level state ---------------------------------------------

    @property
    def frozen_config(self):
        return self.sub("cfg") / "experiment.yaml"

    @property
    def provenance(self):
        return self.sub("cfg") / "provenance.json"

    @property
    def ledger_file(self):
        return self.sub("ledger") / "submissions.jsonl"

    @property
    def halt_flag(self):
        return self.experiment_dir / "HALT"

    def submit_marker(self, cycle):
        """Created `O_EXCL` before any `sbatch` for a cycle.

        A requeued submitter reruns its batch script from the beginning, and
        without this it submits an entire cycle twice and two graphs race into
        the same directories. That is the most likely way to corrupt an
        experiment silently, so it gets a marker, a `--no-requeue`, and the
        ledger, and depends on none of them alone.
        """
        return self.sub("ledger") / f"submitted.{cycle}"

    def stats_file(self, cycle):
        return self.sub("stats") / f"{cycle}.json"

    def ensure(self):
        """Create the experiment directory tree. Idempotent."""
        for name in SUBDIRS:
            self.sub(name).mkdir(parents=True, exist_ok=True)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        return self
