"""Reading the site layer from the environment.

`site/<name>.sh` is the one file allowed to name machine-specific paths, and
`site/activate.sh` exports everything it defines. The workflow reads those
exports rather than parsing shell, so there is exactly one definition of what a
machine assumes and one mechanism that applies it.
"""

import os
from pathlib import Path

#: The checkout this module was imported from, and the default for `root`.
#: `ACKBAR_ROOT` is what `site/activate.sh` exports and it wins where it is set.
#: Where it is not, running out of a checkout should mean *that* checkout rather
#: than an error, so that a layer naming an executable under `$(ackbar_root)`
#: resolves either way. One definition, because two would eventually disagree
#: about which build a job ran.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Site settings the configuration core needs. Scheduler settings join this as
#: the phases that submit jobs arrive.
REQUIRED = ("scratch_root", "output_root")

OPTIONAL = (
    "site",
    "root",
    "partition",
    "account",
    # Every JCSDA slurm site template carries a QoS alongside partition and
    # account (ewok's `hosts/slurm.h`), because on the MSU machines the two do
    # not imply it: an account may reach several, and the default is not always
    # the one an experiment should be charged to.
    "qos",
    "launcher",
    "datasets_root",
    "static_root",
    # Queue limits. Validation projects the in-flight job count against these
    # rather than discovering the cap three cycles in, where a rejected sbatch
    # inside the submitter stops the experiment silently.
    "max_submit_jobs",
    "max_array_size",
    "can_submit_from_compute",
)


class SiteError(Exception):
    pass


def load_site(env=None):
    """Return the site settings as a plain mapping."""
    env = os.environ if env is None else env

    missing = [n for n in REQUIRED if not env.get(f"ACKBAR_{n.upper()}")]
    if missing:
        wanted = ", ".join(f"ACKBAR_{n.upper()}" for n in missing)
        verb = "is" if len(missing) == 1 else "are"
        raise SiteError(
            f"{wanted} {verb} not set. Run `source site/activate.sh`, or add a "
            f"site file for this machine modelled on site/rancor.sh."
        )

    site = {n: env[f"ACKBAR_{n.upper()}"] for n in REQUIRED}
    for name in OPTIONAL:
        value = env.get(f"ACKBAR_{name.upper()}")
        if value:
            site[name] = value
    site.setdefault("root", str(REPO_ROOT))
    return site
