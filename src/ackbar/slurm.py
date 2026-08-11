"""Every call ACKBAR makes to Slurm, in one place.

Confined here so that the submitter, the healer and the harvest share one
spelling of each command, and so that the tier 0 and 1 tests can replace `run`
with a fake and exercise submission logic on a machine with no scheduler.

Nothing here interprets the workflow. It converts arguments to a command and a
command's output to plain data, and that is all.
"""

import getpass
import json
import shutil
import subprocess
import time

#: The only state that means the artifact is there. Everything else that has
#: left the queue is failed, deliberately: an unrecognized state is not good
#: news, and the cost of being wrong is a job that reads a file that does not
#: exist.
SUCCESS = ("COMPLETED",)

#: Still going, so no outcome yet. `sacct` reports these while a job is live.
ACTIVE = ("RUNNING", "PENDING", "REQUEUED", "RESIZING", "SUSPENDED", "COMPLETING")

#: A pending job whose dependency can never be satisfied. It never appears in
#: `sacct` as failed at all, and unless the site sets
#: `DependencyParameters=kill_invalid_depend` it pends forever, so `status` and
#: `heal` have to recognize it from the queue reason and call it terminal.
NEVER_SATISFIED = "DependencyNeverSatisfied"


class SlurmError(Exception):
    pass


def available():
    return shutil.which("sbatch") is not None


#: How long any one Slurm command may take before it is treated as hung.
#:
#: Generous, because `sacct` over a long experiment is genuinely slow and a
#: timeout that fires on a healthy query is worse than no timeout. The point is
#: that a wedged slurmdbd cannot hold a job open for its whole walltime: without
#: this, `subprocess.run` waits forever and the job dies on the Slurm time limit
#: hours later, reported as a timeout of the science rather than of a query.
QUERY_TIMEOUT = 120

#: How many times a query that could not be answered is retried before it
#: becomes an error, and how long to wait between attempts.
#:
#: For slurmdbd restarts and momentary load, which last seconds. Anything that
#: outlives this is an outage rather than a blip and should be visible.
QUERY_ATTEMPTS = 3
QUERY_BACKOFF = 2.0


def run(command, check=True, stdin=None):
    """Run one Slurm command. The single point tests replace."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, input=stdin,
            timeout=QUERY_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise SlurmError(
            f"{' '.join(command)} did not answer in {QUERY_TIMEOUT}s"
        ) from error
    if check and result.returncode != 0:
        raise SlurmError(
            f"{' '.join(command)} exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def sbatch(script, *, job_name, comment, dependency=None, array=None,
           partition=None, account=None):
    """Submit one script. Returns the job id as an integer.

    Identity is carried twice, as the design requires: in the job name so
    `sacct` rows are readable by eye, and in `--comment` so it survives into
    accounting as a structured field. The name alone is not enough, because
    `sacct` truncates it and because several experiments run at once.

    `--array` and `--dependency` are command line rather than script, because
    they are the two things that differ between the first attempt and a healed
    one. Everything else is frozen in the script.
    """
    command = ["sbatch", "--parsable", f"--job-name={job_name}",
               f"--comment={comment}"]
    if dependency:
        command.append(f"--dependency={dependency}")
    if array:
        command.append(f"--array={array}")
    if partition:
        command.append(f"--partition={partition}")
    if account:
        command.append(f"--account={account}")
    command.append(str(script))

    result = run(command)
    # `--parsable` gives "<id>" or "<id>;<cluster>".
    return int(result.stdout.strip().split(";")[0])


def queue(job_ids=None):
    """Live state from `squeue`: {job id: (state, reason)}.

    Reasons exist only while a job is queued and are gone forever afterward,
    which is why this cannot be replaced by `sacct`. `sacct`'s own Reason field
    is empty post mortem.

    Array elements are collapsed onto their base job id. A node is one
    submission here, and per element state belongs to the harvest, not to the
    question "may I still depend on this".
    """
    command = ["squeue", "-h", "-o", "%i|%T|%r"]
    if job_ids:
        command += ["-j", ",".join(str(i) for i in job_ids)]
    # A job id that has already left the queue makes squeue exit nonzero, which
    # is the ordinary case here rather than an error.
    result = run(command, check=False)
    out = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        raw, state, reason = parts
        base = raw.split("_")[0]
        if not base.isdigit():
            continue
        # Worst state wins for an array: one pending element means the
        # submission as a whole is not finished, and one element that can never
        # run means the submission is dead however healthy the rest look.
        # `setdefault` would keep whichever row `squeue` happened to emit
        # first, which hides a `DependencyNeverSatisfied` behind an ordinary
        # `PENDING` and answers `active` for a submission that is partly dead.
        key = int(base)
        if key not in out or reason == NEVER_SATISFIED:
            out[key] = (state, reason)
    return out


def named(experiment):
    """Live job ids belonging to *experiment*, found by job name.

    The half of `cancel` the ledger cannot answer: a job somebody submitted by
    hand is nowhere in the ledger, and `scancel` has no name glob to find it
    with.

    Matched as `<experiment>.<cycle>.<task>` rather than by prefix, because a
    prefix would make cancelling `osse-truth` also cancel `osse-truth.presmooth`
    and `osse-truth-v2`. The cycle field is a number, so requiring the segment
    after the experiment name to be one separates the two without needing to
    know what experiments exist.
    """
    result = run(["squeue", "-h", "-o", "%i|%j", "-u", getpass.getuser()],
                 check=False)
    out = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 2:
            continue
        raw, name = parts
        base = raw.split("_")[0]
        if not base.isdigit():
            continue
        rest = name[len(experiment) + 1:].split(".")
        if name.startswith(f"{experiment}.") and rest and rest[0].isdigit():
            out.add(int(base))
    return out


def accounting(job_ids):
    """Outcome from `sacct --json`: {job id: {"state":, "reason":, "exit":}}.

    `--json` rather than `-P`, because the parsable delimiter collides with the
    structured comment ACKBAR puts identity in, and because the default column
    widths truncate the job name.

    **An unanswerable query raises rather than returning nothing.** The two are
    not the same statement and this used to make them one: an empty result means
    "Slurm has no record of these jobs", which `state_of` reports as `unknown`
    and the submitter turns into a refusal to submit. So a slurmdbd that was
    restarting for five seconds inside a submitter's window stopped an overnight
    experiment, with a message about a dependency that was never in doubt.

    Retried first, because that is what a blip deserves. What survives the
    retries is an outage, and an outage has to be visible: reported as itself,
    not as every job in the experiment having ceased to exist.
    """
    if not job_ids:
        return {}
    command = ["sacct", "-j", ",".join(str(i) for i in job_ids), "--json"]
    payload = None
    for attempt in range(QUERY_ATTEMPTS):
        result = run(command, check=False)
        if result.returncode == 0 and result.stdout.strip():
            try:
                payload = json.loads(result.stdout)
                break
            except json.JSONDecodeError:
                pass
        if attempt + 1 < QUERY_ATTEMPTS:
            time.sleep(QUERY_BACKOFF * (attempt + 1))
    if payload is None:
        raise SlurmError(
            f"sacct could not answer for {len(job_ids)} job(s) after "
            f"{QUERY_ATTEMPTS} attempts; treating this as an outage rather "
            f"than as those jobs not existing"
        )

    out = {}
    for job in payload.get("jobs", []):
        state = job.get("state", {})
        current = state.get("current")
        # Slurm spells this as a list in recent versions and a bare string in
        # older ones.
        if isinstance(current, list):
            current = current[0] if current else "UNKNOWN"
        record = {
            "state": current or "UNKNOWN",
            "reason": state.get("reason", ""),
            "comment": (job.get("comment") or {}).get("job", ""),
            "name": job.get("name", ""),
        }
        # Keyed on the array *base*, which is what everything here asks about.
        # `sacct --json` reports one object per array element, each carrying its
        # own `job_id`; only the element that happened to be allocated the base
        # id lands under the id the caller passed. Keyed on that, an array's
        # answer is one arbitrary element's answer, so a member array with a
        # failure in it reports whatever the base-id element did. `state_of`
        # would then call it `completed` and `submit._dependency` would drop the
        # edge as redundant, releasing a consumer of a member that was never
        # written. That is the third case this module's docstring calls
        # catastrophically wrong.
        key = int((job.get("array") or {}).get("job_id") or job["job_id"])
        if key not in out or _worse(record["state"], out[key]["state"]):
            out[key] = record
    return out


def accounting_states(job_ids):
    """{(base job id, array index): state} from `sacct`, states and nothing else.

    The same question `accounting` answers, asked the cheap way, for the callers
    that only need the state. `--json` serializes every field Slurm knows for
    every job: at a thousand ids that is fifteen megabytes to produce, transfer
    and parse, and it measured ten seconds against three hundred milliseconds
    for the two columns actually read. A live display cannot pay that per tick,
    and neither should `status` on an experiment that has been running a week.

    Per array *element*, because that is the resolution the caller wants and the
    resolution `sacct` natively reports; `accounting` collapses elements onto the
    base id and this deliberately does not. A non-array job is keyed
    `(id, None)`.

    Same outage contract as `accounting`, arrived at differently. There, empty
    output cannot be told from a dbd that is not answering, so empty is a
    failure; here `--json` is not in the way, so the exit code says which it is:
    `sacct` exits zero for a job it has no record of and non-zero when it cannot
    ask. The one ambiguous case, zero rows for ids that were asked about, falls
    through to `accounting` rather than being interpreted here, so the hardened
    path is what answers when the cheap one says nothing at all.
    """
    if not job_ids:
        return {}
    command = ["sacct", "-n", "-X", "-P", "-o", "JobID,State",
               "-j", ",".join(str(i) for i in job_ids)]
    result = None
    for attempt in range(QUERY_ATTEMPTS):
        result = run(command, check=False)
        if result.returncode == 0:
            break
        if attempt + 1 < QUERY_ATTEMPTS:
            time.sleep(QUERY_BACKOFF * (attempt + 1))
    if result is None or result.returncode != 0:
        raise SlurmError(
            f"sacct could not answer for {len(job_ids)} job(s) after "
            f"{QUERY_ATTEMPTS} attempts; treating this as an outage rather "
            f"than as those jobs not existing"
        )

    out = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 2:
            continue
        raw, state = parts
        base, _, element = raw.partition("_")
        # "123_[5-20]", the un-started remainder of an array. It has no outcome
        # yet by definition, and the queue is where it is visible.
        if not base.isdigit() or element.startswith("["):
            continue
        member = int(element) if element.isdigit() else None
        # "CANCELLED by 1000" and friends.
        out[(int(base), member)] = state.split()[0]

    if not out:
        return {key: record["state"]
                for key, record in _base_keyed(accounting(job_ids)).items()}
    return out


def _base_keyed(records):
    """`accounting`'s base-keyed rows in `accounting_states`' key shape."""
    return {(job_id, None): record for job_id, record in records.items()}


def collapse_elements(states):
    """Per-element states from `accounting_states`, worst element per base id.

    The same collapse `accounting` does internally, exposed because a caller
    that asked per element still wants the "what did this job do" answer for
    the elements accounting has no row for.
    """
    out = {}
    for (base, _member), state in states.items():
        if base not in out or _worse(state, out[base]):
            out[base] = state
    return out


def _worse(state, than):
    """Whether an array element's outcome should displace one already seen.

    Anything that is not a success displaces a success, and an outright failure
    displaces a state that is merely still running. One failed element makes the
    array failed no matter what its siblings did.
    """
    if than in SUCCESS:
        return state not in SUCCESS
    if than in ACTIVE:
        return state not in SUCCESS and state not in ACTIVE
    return False


def state_of(job_ids):
    """What each job id is, as the submitter needs to know it.

    One of `active`, `completed`, `failed`, `unknown`. The queue is consulted
    first and wins, because a job that is still in it has no `sacct` outcome
    yet, and because `DependencyNeverSatisfied` is visible only there.
    """
    live = queue(job_ids)
    done = accounting([i for i in job_ids if i not in live])

    out = {}
    for job_id in job_ids:
        if job_id in live:
            state, reason = live[job_id]
            out[job_id] = "failed" if reason == NEVER_SATISFIED else "active"
            continue
        record = done.get(job_id)
        if record is None:
            out[job_id] = "unknown"
        elif record["state"] in SUCCESS:
            out[job_id] = "completed"
        elif record["state"] in ACTIVE:
            out[job_id] = "active"
        else:
            out[job_id] = "failed"
    return out


def scancel(job_ids):
    if not job_ids:
        return
    run(["scancel"] + [str(i) for i in job_ids], check=False)


def array_spec(members):
    """`--array` for a member index set, as compact ranges.

    Compact because a site's `MaxArrayTasks` and the command line both care,
    and because `1-20` in `squeue` is readable where twenty comma separated
    indices are not.
    """
    if not members:
        return None
    ordered = sorted(members)
    runs = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        runs.append((start, previous))
        start = previous = value
    runs.append((start, previous))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)
