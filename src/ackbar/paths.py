"""Where an experiment's files live.

One module, because "which directory is this" is a question the emitter, the
submitter, every task body, cleanup and healing all ask, and a second spelling
of any one of them is a bug that surfaces only as a missing file eight hours in.

The layout is the one in `docs/design.md`, On-disk layout. Two roots, because on
a real machine scratch and output are different filesystems with different purge
policies, and the site layer names them separately.

**Two tiers, and the split is the retention policy.** The top level holds what
the experiment is *for*: the compressed analysis, the compressed background, the
departures, and the configuration that produced them. `run/` holds what it took
to get there: restart sets, sub-window states, logs, sentinels and the ledger.
Nothing outside `run/` is ever deleted, and inside it exactly three
subdirectories per cycle are, the ones in `REAPED`.

    <experiment>/
      cfg/                                  frozen config and job scripts
      ana/20150105T130000Z/mem000.nc        the analysis, compressed    kept
      ana/20150105T130000Z/members.json     which members it used       kept
      bkg/20150105T130000Z/mem000.nc        the background, compressed  kept
      obs_out/20150105T130000Z/             departures and the list     kept
      fcst/20150105T130000Z/                the extended forecast       kept
        F012/mem000.nc -> ../../../bkg/20150106T010000Z/mem000.nc
        F120/mem000.nc                      compressed, at that lead
        obs/F120/                           departures at that lead
      run/
        ledger.jsonl
        20150105T130000Z/
          log/   done/   stats.json                                    kept
          rst/mem000/    what this cycle's forecast wrote            reaped
          ana/mem000/    the analysed set the forecast starts from   reaped
          slot/mem000/20150105T100000Z/   sub-window states          reaped
          fcst/mem000/F120/               extended forecast states   reaped

Two products are keyed by a *lead* under an initialization rather than by a
valid time, and only two: `fcst/<init>/F###/` and the departures beside it. A
background is a one-cycle forecast, so `bkg/<T>` is the same kind of thing as
`fcst/<T-length>/F<length>`, and the first lead of an extended forecast is a
symlink to the `bkg/` file rather than a second copy of it. That keeps `ana/<T>`
and `bkg/<T>` as the pair at one valid time, which is what makes an increment a
subtraction between two files with the same name.

This is soca-science's shape, and the reasoning is its reasoning: `ana/` and
`bkg/` are the products, they are compressed and small, and they outlive the
experiment. Its analysed restart set lived in scratch and was never persisted at
all, which ACKBAR cannot do because `writeback` and `forecast` are separate jobs
and a task's scratch is deleted when it succeeds. So it lives under `run/`,
which is the same statement about its status.

**Directories are named by a date, not by a cycle number.**
`run/20150105T130000Z/` is what the cycle whose analysis time is 2015-01-05
13:00 did, and `ana/20150105T130000Z/mem000.nc` is the analysis at that time.
The one place this needs care is `run/<T>/rst/`, which holds what *that* cycle's
forecast wrote and is therefore valid at the next analysis time rather than at
`T`. A directory is owned by the cycle that writes it, and every cycle writes
only under its own, which is what keeps cleanup a date comparison rather than a
data flow analysis.

Cycle numbers survive in the interface: `ackbar run --cycle 7` and the graph
speak in numbers, because a number is what a scheduler dependency and a heal are
computed from. Only the filesystem is dated, and the two are one function apart
(`Paths.date`, and `cycle_of` for the inverse).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config.jobtime import cycle_length, member_dir
from .duration import parse_instant

#: Top-level subdirectories. Mirrored by `run_dir`, `ana_dir` and friends in the
#: experiment-time symbol table, so a config can name the same places without
#: spelling them a second time.
#: `bvt` holds the vertical correlation scales a cycled B used, one file per
#: cycle. A product and not an intermediate: the next cycle's rolling average
#: reads the previous cycle's, and a run's series of them is the only record of
#: what the vertical correlation did over time. See `run.vertical_correlation_record`.
SUBDIRS = ("cfg", "ana", "bkg", "corr_vt", "fcst", "obs_out", "run")

#: What a cycle's `run/` directory holds that `cleanup` may delete, and how many
#: cycles earlier than the shared horizon each one may go. Everything else under
#: it is a record of what happened and is kept: a log, a sentinel and an
#: accounting file are bytes, and the states beside them are gigabytes.
#:
#: The offsets are the data flow. `rst/<n>` is read by the *next* cycle's
#: analysis and forecast, and `slot/<n>` is read by the next cycle's analysis
#: too, so both are released by `rst/<n+1>` existing. `ana/<n>` is read only by
#: `forecast(n)`, inside its own cycle, so `rst/<n>` existing already releases
#: it: one cycle earlier, which on a twenty member run is a whole cycle of
#: restart sets that were being held for nothing. `fcst/<n>` is the extended
#: forecast's raw output, read only by the tasks that reduce it, and it is
#: proved done by its own sentinel rather than by a later cycle's restart, so it
#: sits at the shared horizon rather than getting an offset of its own.
REAPED = {"rst": 0, "slot": 0, "fcst": 0, "ana": 1}

#: How an instant is spelled in a directory name, everywhere in the tree.
#:
#: ISO 8601 basic format, which sorts chronologically as a string, reads at a
#: glance, and carries no colons, which survive a filesystem and then surprise
#: everything that reads `host:path`.
#:
#: To the second, rather than soca-science's `YYYYMMDDHH`. Every duration an
#: experiment may state is a whole number of hours (`graph.build._check_hours`
#: enforces it, and ocean windows are hours at the shortest), so the minutes and
#: seconds are always zero and carry no information. They are here anyway
#: because the alternative is a format that has to change the first time that
#: assumption does, and a date format is the one thing in this tree that cannot
#: change without invalidating every experiment already on disk. One spelling
#: for cycles and for sub-window states, so the tree has one date format rather
#: than two.
CYCLE_DATE = "%Y%m%dT%H%M%SZ"

#: The same thing, named for the place it is used. Kept as a separate name
#: because a sub-window state is a different kind of instant from a cycle, and
#: a later reason to spell one of them differently should not have to rediscover
#: which call sites meant which.
SLOT_DATE = CYCLE_DATE

#: How a forecast lead is spelled, in hours: `F000`, `F024`, `F120`.
#:
#: A lead rather than a valid time, and this is the one place in the tree that
#: goes that way. Everywhere else a directory is named for when its contents are
#: valid, because the consumer knows the time it wants and not how far that is
#: from anything. Under `fcst/` the consumer wants the opposite: forecast skill
#: is read *against lead*, grouped across initializations, and the reference the
#: lead is measured from is the parent directory rather than something elsewhere
#: in the config. So the recomputation this format usually costs is one addition
#: against a name already in the path.
#:
#: Three digits sort correctly by name up to F999, which is 41 days. A longer
#: forecast than that sorts F1000 before F120, which is a display problem and
#: not a correctness one, since nothing parses these by ordering.
LEAD = "F%03d"


def lead_name(lead):
    """`timedelta` to `F###`. Whole hours only; see `graph.build._check_hours`."""
    hours, remainder = divmod(int(lead.total_seconds()), 3600)
    if remainder:
        raise ValueError(f"a forecast lead of {lead} is not a whole number of hours")
    return LEAD % hours


@dataclass(frozen=True)
class Paths:
    """Where one experiment's files are, and what date each cycle is.

    `start` and `length` are here rather than looked up per call because every
    path below is keyed by a date computed from them, and a Paths that could not
    answer "what date is cycle 7" would push that computation, and the chance of
    getting it wrong, out to every caller.
    """

    experiment: str
    output_root: Path
    scratch_root: Path
    start: datetime
    length: timedelta

    @classmethod
    def of(cls, config, site):
        return cls(
            experiment=config["experiment"]["name"],
            output_root=Path(site["output_root"]),
            scratch_root=Path(site["scratch_root"]),
            start=parse_instant(config["cycle"]["start"]),
            length=cycle_length(config),
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

    def date(self, cycle):
        """Cycle *n*'s analysis time, as it is spelled in a directory name.

        Cycle 1 is at `cycle.start`, so cycle 0 is one length earlier: that is
        where setup materializes the offline initial condition, which is what
        makes cycle 1 an ordinary cycle rather than a special case.
        """
        return (self.start + (cycle - 1) * self.length).strftime(CYCLE_DATE)

    # --- the run tier: what it took, and what cleanup reaps ------------------

    def cycle_dir(self, cycle):
        """Everything one cycle did, kept and reaped alike."""
        return self.sub("run") / self.date(cycle)

    def cycle_out(self, kind, cycle):
        """One kind of this cycle's intermediate state, or its observation output.

        `obs_out` is a product and sits at the top level; the rest are
        intermediate and sit under `run/`. Both are addressed here so that a
        caller says which kind it wants and never which tier that kind is in.
        """
        if kind == "obs_out":
            return self.sub("obs_out") / self.date(cycle)
        if kind not in REAPED:
            raise KeyError(f"unknown cycle directory {kind!r}")
        return self.cycle_dir(cycle) / kind

    def member_out(self, kind, cycle, member):
        return self.cycle_out(kind, cycle) / member_dir(member)

    def slot_out(self, cycle, member, when):
        """One sub-window state of cycle *n*'s forecast, by its valid time.

        A directory per slot holding the ocean restart under its ordinary name,
        rather than one directory of date-stamped files. That is what lets
        every consumer read `<somewhere>/MOM.res.nc`, so `model.restart.ocn`
        keeps one spelling and a state is addressed the same way whether it
        came from a slot, from a restart set or from the static root, whose
        `ic/<domain>/<product>/<date>/` is laid out identically.

        The leaf is a time rather than v2's `f###` offset: an offset has to be
        recomputed against a reference by everything that reads it, and every
        one of those computations is a chance to be an hour out.
        """
        return self.member_out("slot", cycle, member) / when.strftime(SLOT_DATE)

    # --- the product tier: what the experiment is for ------------------------

    def product(self, kind, cycle, member, *, at=None):
        """The compressed state one cycle leaves behind. See `ackbar/post.py`.

        `kind` is `ana` or `bkg`, and the two are separate files rather than one
        file holding both, because they are separate answers: a free run has a
        background and no analysis, and an experiment being compared against
        another is compared on one or the other, not on a pair.

        *at* names the valid time directly, and exists for the sub-window states
        a 4D forecast writes: those fall *between* analysis times, so no cycle
        number identifies them and `self.date` cannot spell them. The caller
        still passes the cycle that produced the state, because that is what the
        record's `cycle` attribute carries and what a heal is computed from.

        Sub-window states are filed under `bkg/` beside the ones on analysis
        times, not in a subdirectory of their own. A sub-window state *is* a
        background: it is what FGAT and 4D-Ens-Var compare an observation
        against at its own time, and the only thing separating it from the rest
        is that no cycle begins there. Two directories would force every
        consumer to know which kind it wanted before it could ask for a time,
        and the truth archive wants exactly the union.
        """
        if kind not in ("ana", "bkg"):
            raise KeyError(f"unknown product {kind!r}; the products are ana and bkg")
        stamp = self.date(cycle) if at is None else at.strftime(CYCLE_DATE)
        return self.sub(kind) / stamp / f"{member_dir(member)}.nc"

    def fcst_out(self, cycle, member, lead):
        """One state of the extended forecast, raw, as the model wrote it.

        Under `run/` and reaped, for the same reason `slot/` is: it is a full
        restart set per lead per member, which is the fastest growing thing an
        experiment produces, and `post.state` reduces it to something two orders
        of magnitude smaller that is kept.

        Deliberately *not* `rst/`, which is where `forecast.ext` used to write.
        Both forecasts start from the same state, so a state the extended one
        wrote at a time the cycling one also reaches is a different trajectory
        from that point on, and one directory cannot hold two answers.
        """
        return self.member_out("fcst", cycle, member) / lead_name(lead)

    def fcst_product(self, cycle, lead, member):
        """The compressed state at one lead of the forecast initialized at *cycle*.

        Keyed by initialization *and* lead, both, because neither alone
        identifies it: two forecasts started five days apart both pass through
        the same valid time, and they are not the same state.
        """
        return self.sub("fcst") / self.date(cycle) / lead_name(lead) \
            / f"{member_dir(member)}.nc"

    def fcst_obs(self, cycle, lead, member):
        """Departures against the extended forecast, for one section of it.

        Under the initialization rather than beside `obs_out/`, because
        `obs_out/<T>` already means "what the cycling analysis saw at T", and a
        five day forecast's departures at that same T are a different number
        answering a different question. Writing them to the configured
        `obs_out/` path would not merely be confusing, it would overwrite the
        cycling departures for every cycle the forecast reaches.

        *lead* names the **end of the section** evaluated, not one lead within
        it. Today there is one section, the whole forecast, so this is the final
        lead; the reason it is in the path at all is that a larger domain will
        have to evaluate a day at a time, and sections keyed by their end need
        no path change when that happens.

        Per member, because the observer layer's output filename carries the
        observer and the date and not the member, so two members of one long
        forecast would otherwise write the same file.
        """
        return self.sub("fcst") / self.date(cycle) / "obs" / lead_name(lead) \
            / member_dir(member)

    def member_list(self, cycle):
        """Which members this cycle's analysis actually used. See `ensemble.py`.

        Beside the compressed analysis rather than under `run/<T>/ana/` with the
        restart sets it describes, because those are reaped and this is the
        record of what was reaped: an experiment whose ensemble lost members to
        a divergence policy, and which no longer holds the file saying so, is one
        whose comparison against another experiment cannot tell the two apart.
        Kilobytes beside gigabytes, and only one of the two is worth keeping.
        """
        return self.sub("ana") / self.date(cycle) / "members.json"

    def obs_summary(self, cycle):
        """One cycle's observation-space statistics. See `ackbar/post.py`.

        Beside the departures it summarizes rather than in a directory of its
        own, because it is the same product read at a different resolution.
        """
        return self.cycle_out("obs_out", cycle) / "summary.json"

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

        Under `cfg/` rather than under `run/` because it is configuration: it is
        written before anything runs and is never touched again, which is the
        same status the frozen layers have.
        """
        return self.sub("cfg") / self.date(cycle) / f"{task}.sh"

    def log_dir(self, cycle):
        """Where one cycle's job output and kept model traces go.

        Kept, unlike the states beside it: a log is kilobytes and is the only
        thing left to read when a cycle failed months ago.
        """
        return self.cycle_dir(cycle) / "log"

    def done_dir(self, cycle):
        return self.cycle_dir(cycle) / "done"

    def job_log(self, cycle, task, array):
        """The `--output` pattern. Slurm expands it; ACKBAR never globs it.

        `%A_%a` for an array and `%j` otherwise, so a healed attempt lands
        beside the failed one instead of overwriting the evidence.
        """
        stem = "%A_%a" if array else "%j"
        return self.log_dir(cycle) / f"{task}.{stem}.out"

    def sentinel(self, cycle, task, member=None):
        """Written last by a successful task, and the only proof it finished.

        Skip-if-output-exists is not sufficient: a task killed mid-write leaves
        an output that exists and is truncated. See Task completion and
        idempotency in `docs/design.md`.
        """
        name = task if member is None else f"{task}.{member_dir(member)}"
        return self.done_dir(cycle) / f"{name}.json"

    def stats_file(self, cycle):
        return self.cycle_dir(cycle) / "stats.json"

    def scratch(self, cycle, task, member=None):
        name = task if member is None else f"{task}.{member_dir(member)}"
        return self.scratch_dir / self.date(cycle) / name

    # --- experiment-level state ---------------------------------------------

    @property
    def frozen_config(self):
        return self.sub("cfg") / "experiment.yaml"

    @property
    def templates(self):
        """The SOCA document templates, frozen at create time.

        Frozen for the reason the layers are: "what exactly did this experiment
        run" has to be answerable from the experiment directory, and editing the
        checkout mid-run must not change cycle 40 and not cycle 39.
        """
        return self.sub("cfg") / "soca"

    @property
    def provenance(self):
        return self.sub("cfg") / "provenance.json"

    @property
    def ledger_file(self):
        return self.sub("run") / "ledger.jsonl"

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
        return self.sub("run") / f"submitted.{self.date(cycle)}"

    #: Made when the experiment is created, because something writes into them
    #: before any cycle runs: `cfg` holds the frozen config and the job scripts
    #: that `create` emits, and `run` holds the ledger and the submit markers.
    #: The rest of `SUBDIRS` are products, and a product directory is made by
    #: whatever writes the first file into it.
    EAGER = ("cfg", "run")

    def ensure(self):
        """Create the experiment directory tree. Idempotent.

        **Only the directories something is about to write into.** An empty
        `ana/` under a free run, or an empty `fcst/` under an experiment with no
        extended forecast, is a claim that the experiment produces something it
        does not, and the person reading the tree while it runs is the one who
        pays for it: the useful question there is "what has this produced so
        far", and six directories that are always present answer it wrongly at a
        glance. Every writer already creates its own parent, so the products
        appear exactly when the first one is written.
        """
        for name in self.EAGER:
            self.sub(name).mkdir(parents=True, exist_ok=True)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        return self


def cycle_of(paths, date):
    """The cycle number a `run/` directory name belongs to, or None.

    Cleanup sweeps the directory rather than indexing one entry, so it has to
    turn a name back into a number. Anything that is not a date this experiment
    would have written is left alone rather than guessed at.
    """
    try:
        when = datetime.strptime(date, CYCLE_DATE).replace(tzinfo=paths.start.tzinfo)
    except ValueError:
        return None
    offset = when - paths.start
    seconds = paths.length.total_seconds()
    if offset.total_seconds() % seconds:
        return None
    return int(offset.total_seconds() // seconds) + 1


__all__ = ["Paths", "SUBDIRS", "REAPED", "CYCLE_DATE", "SLOT_DATE", "LEAD",
           "cycle_of", "lead_name"]
