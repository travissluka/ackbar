"""Reading the site layer from the environment.

`site/<name>.sh` is the one file allowed to name machine-specific paths, and
`site/activate.sh` exports everything it defines. The workflow reads those
exports rather than parsing shell, so there is exactly one definition of what a
machine assumes and one mechanism that applies it.
"""

import os

#: Site settings the configuration core needs. Scheduler settings join this as
#: the phases that submit jobs arrive.
REQUIRED = ("scratch_root", "output_root")

OPTIONAL = ("site", "partition", "account", "launcher", "datasets_root")


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
    return site
