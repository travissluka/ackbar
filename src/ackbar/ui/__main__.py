"""`ackbar-ui`, and `python -m ackbar.ui`.

The dependency check lives here rather than at an import in `cli.py` so that the
message names the one command that fixes it. A traceback about textual is a
worse answer than a sentence, and this is the only place the answer is known.
"""

import argparse
import sys

from ..site import SiteError, load_site
from .discover import discover

MISSING = """ackbar-ui needs textual, which is an optional dependency.

    pip install -e '{root}[ui]'

It is optional on purpose: every compute job imports `ackbar`, and the console's
dependencies have no business being installed on a node that only runs a model.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ackbar-ui",
        description="The interactive console: every running experiment, and "
                    "every verb for one, in one screen.",
    )
    parser.add_argument(
        "name", nargs="?",
        help="experiment to open on. The default is the one touched most "
             "recently, which is almost always the one you meant",
    )
    parser.add_argument(
        "--palette", default="default", choices=("default", "safe"),
        help="`safe` avoids the red/green pair (also toggled in-app with P)",
    )
    parser.add_argument(
        "--interval", type=float, default=None,
        help="seconds between scheduler refreshes (default 5)",
    )
    parser.add_argument(
        "--all", action="store_true", dest="show_all",
        help="list finished experiments however old, not only recent ones",
    )
    parser.add_argument(
        "--output-root", default=None,
        help="look here instead of the site's output root",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print the experiments that would be shown, and exit. Needs no "
             "terminal, so it is also the way to check the scan from a script",
    )
    args = parser.parse_args(argv)

    try:
        site = load_site()
    except SiteError as error:
        print(f"ackbar-ui: {error}", file=sys.stderr)
        return 2

    if args.list:
        return _list(site, args.output_root)

    try:
        from .app import AckbarUI, INTERVAL
    except ImportError as error:
        if "textual" not in str(error) and "rich" not in str(error):
            raise
        from ..site import REPO_ROOT
        print(MISSING.format(root=REPO_ROOT), file=sys.stderr)
        return 2

    app = AckbarUI(
        site=site,
        root=args.output_root,
        palette=args.palette,
        interval=args.interval if args.interval else INTERVAL,
        show_all=args.show_all,
    )
    if args.name:
        app.selected = args.name
    app.run()
    return 0


def _list(site, root):
    experiments = discover(site, root)
    if not experiments:
        print(f"no experiments under {root or site['output_root']}")
        return 1
    for experiment in experiments:
        halted = " halted" if experiment.halted else ""
        print(f"{experiment.name:<28} {experiment.domain:<10} "
              f"{experiment.solver:<16} {experiment.cycles:>3} cycles{halted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
