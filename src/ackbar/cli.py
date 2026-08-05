"""The ackbar command line.

What exists so far is everything that can be checked before a scheduler is
involved: resolve a layer stack, explain where a value came from, build the task
graph, and validate the result. Submission arrives in phase 2; see
`docs/build-order.md`.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

from .config.jobtime import SYMBOLS, render, symbols
from .config.layers import LayerError, merge_layers, resolve_layers
from .config.merge import MergeError
from .config.resolve import ResolveError, resolve
from .config.schema import load_schema, merge_keys
from .config.why import why
from .duration import DurationError
from .graph import GraphError, build_graph, to_dot, to_text
from .site import SiteError, load_site
from .validate import FILESYSTEM_STEPS, STEPS, validate_experiment

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LAYERS = REPO_ROOT / "config" / "layers"


def _resolved(args, substitute=True):
    """Layers, merge keys, schema, and the config for one experiment.

    The order is merge, then substitute, then validate. Merging last would stop
    a layer from overriding a value another layer interpolated, and validating
    before substitution would check `$(ntasks)` rather than the integer it
    stands for.
    """
    schema = load_schema(args.schema)
    keys = merge_keys(schema)
    layers = resolve_layers(args.experiment, args.layers)
    config = merge_layers(layers, keys)
    if substitute:
        config = resolve(config, load_site())
    return layers, keys, schema, config


def cmd_resolve(args):
    _, _, _, config = _resolved(args, substitute=not args.raw)
    if args.cycle is not None:
        # The second pass, shown for one job rather than left as {{...}}. This
        # is what the job actually reads.
        config = render(config, symbols(config, args.cycle, args.member))
    yaml.safe_dump(config, sys.stdout, sort_keys=False, default_flow_style=False)
    return 0


def cmd_symbols(args):
    print("Job-time symbols. The set is closed: anything else is an error.\n")
    for name, description in SYMBOLS:
        print(f"  {{{{{name}}}}}".ljust(26) + description)
    print(
        "\nA whole-token string takes the symbol's type; embedded in text it "
        "interpolates\nas text. Either form may carry a format spec after a "
        "colon, so dates take\nstrftime patterns: {{current_cycle:%Y%m%d%H}}."
    )
    print("\nExperiment-time values use $(...) instead, and are frozen when the "
          "experiment\nis created. See `ackbar config resolve`.")
    return 0


def cmd_why(args):
    # Blame is about which layer set a value, so it works on the merged config
    # before substitution and needs no site.
    layers, keys, _, _ = _resolved(args, substitute=False)
    history = why(layers, args.key, keys)
    if not history:
        print(f"{args.key}: never set by any layer")
        return 1
    for name, value in history:
        print(f"  {name:<28} {_render(value)}")
    print(f"\n{args.key} is set by {history[-1][0]}")
    return 0


def _render(value):
    """One-line YAML for a config value.

    A document whose root is a scalar gets an explicit `...` end marker, which
    is noise here.
    """
    text = yaml.safe_dump(value, default_flow_style=True, width=10**6).strip()
    if text.endswith("\n..."):
        text = text[:-4].rstrip()
    return text


def cmd_graph(args):
    _, _, _, config = _resolved(args)
    graph = build_graph(config)
    cycles = set(args.cycles) if args.cycles else None

    if args.json:
        data = graph.to_dict()
        if cycles is not None:
            keep = {n["id"] for n in data["nodes"] if n["cycle"] in cycles}
            data["nodes"] = [n for n in data["nodes"] if n["id"] in keep]
            data["edges"] = [
                e for e in data["edges"] if e["from"] in keep and e["to"] in keep
            ]
        json.dump(data, sys.stdout, indent=2, sort_keys=False)
        print()
    elif args.dot:
        print(to_dot(graph, cycles))
    else:
        print(to_text(graph, cycles))
    return 0


def cmd_validate(args):
    layers, keys, schema, config = _resolved(args)
    site = load_site()
    root = site.get("root", str(REPO_ROOT))
    findings, _, ran = validate_experiment(
        config, schema, site, root, offline=args.offline
    )

    by_step = {}
    for finding in findings:
        by_step.setdefault(finding.step, []).append(finding)

    ok = True
    for number, title in STEPS:
        problems = by_step.get(number, [])
        if number not in ran:
            reason = "--offline" if number in FILESYSTEM_STEPS and args.offline \
                else "an earlier step failed"
            print(f"  {number}. {title}: not run ({reason})")
            continue
        if not problems:
            print(f"  {number}. {title}: ok")
            continue
        ok = False
        print(f"  {number}. {title}: {len(problems)} problem(s)")

    if ok:
        print(f"\nok: {len(layers)} layers, {config['cycle']['count']} cycles")
        return 0

    # Name the layer as well as the path. "Which file do I edit" is the only
    # question worth answering at this moment, and the merge replay already
    # knows.
    sys.stdout.flush()
    print(f"\n{len(findings)} problem(s) in {args.experiment}:", file=sys.stderr)
    for finding in findings:
        print(f"  [{finding.step}] {finding.where or '<root>'}: {finding.message}",
              file=sys.stderr)
        blame = _blame(layers, keys, finding.where)
        if blame:
            print(f"      last set by layer: {blame}", file=sys.stderr)
    return 1


def _blame(layers, keys, path):
    if not path or path.startswith("/"):
        return None
    try:
        history = why(layers, path, keys)
    except (ValueError, MergeError):
        return None
    return history[-1][0] if history else None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ackbar", description=__doc__)
    parser.add_argument(
        "--layers", default=DEFAULT_LAYERS, type=Path,
        help="root of the layer tree (default: %(default)s)",
    )
    parser.add_argument(
        "--schema", default=None, type=Path,
        help="schema to validate against (default: config/schema/experiment.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check an experiment before anything is submitted")
    p.add_argument("experiment", type=Path)
    p.add_argument(
        "--offline", action="store_true",
        help="skip the steps that need the filesystem or the site's queue limits",
    )
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("graph", help="show the task graph")
    p.add_argument("experiment", type=Path)
    p.add_argument(
        "--cycle", dest="cycles", type=int, action="append",
        help="show only this cycle; repeatable",
    )
    output = p.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="the machine-readable form")
    output.add_argument("--dot", action="store_true", help="graphviz")
    p.set_defaults(func=cmd_graph)

    config = sub.add_parser("config", help="inspect resolved configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)

    p = config_sub.add_parser("resolve", help="print the resolved configuration")
    p.add_argument(
        "--raw", action="store_true",
        help="stop after the merge, leaving $(...) unsubstituted",
    )
    p.add_argument(
        "--cycle", type=int, default=None,
        help="also run the job-time pass, as the job for this cycle would see it",
    )
    p.add_argument("--member", type=int, default=0, help="member for --cycle")
    p.add_argument("experiment", type=Path)
    p.set_defaults(func=cmd_resolve)

    p = config_sub.add_parser("why", help="explain where a value came from")
    p.add_argument("experiment", type=Path)
    p.add_argument("key", help="dotted path, e.g. observations[0].obs error.covariance model")
    p.set_defaults(func=cmd_why)

    p = config_sub.add_parser("symbols", help="list the closed set of job-time symbols")
    p.set_defaults(func=cmd_symbols)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (LayerError, MergeError, ResolveError, SiteError, GraphError,
            DurationError) as error:
        print(f"ackbar: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
