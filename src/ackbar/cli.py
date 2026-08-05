"""The ackbar command line.

Phase 0 exposes the configuration core only: resolve a layer stack, explain
where a value came from, and validate the result against ACKBAR's schema. The
graph-level checks that make up the rest of ``validate`` arrive with the graph,
in phase 1.
"""

import argparse
import sys
from pathlib import Path

import yaml

from .config.layers import LayerError, merge_layers, resolve_layers
from .config.lint import ambiguous_numbers
from .config.merge import MergeError
from .config.resolve import ResolveError, resolve, unresolved
from .config.schema import load_schema, merge_keys, validate
from .config.why import why
from .site import SiteError, load_site

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
    yaml.safe_dump(config, sys.stdout, sort_keys=False, default_flow_style=False)
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


def cmd_validate(args):
    layers, keys, schema, config = _resolved(args)
    errors = validate(config, schema)

    for path, value in ambiguous_numbers(config):
        errors.append((
            path,
            f"{value!r} is a string to ACKBAR and a number to JEDI. PyYAML reads "
            f"YAML 1.1, where an exponent needs an explicit sign; eckit reads "
            f"YAML 1.2, where it does not. Write it as a plain number.",
        ))

    # Belt and braces: resolve() raises on an unknown symbol, so anything left
    # here means the substitution pass has a hole in it.
    for path, value in unresolved(config):
        errors.append((path, f"unsubstituted token remains: {value!r}"))

    if not errors:
        print(f"ok: {len(layers)} layers, {len(config)} top-level keys")
        return 0

    # Name the layer as well as the path. "Which file do I edit" is the only
    # question worth answering at this moment, and the merge replay already
    # knows.
    print(f"{len(errors)} problem(s) in {args.experiment}:", file=sys.stderr)
    for path, message in errors:
        blame = _blame(layers, keys, path)
        where = path or "<root>"
        print(f"  {where}: {message}", file=sys.stderr)
        if blame:
            print(f"      last set by layer: {blame}", file=sys.stderr)
    return 1


def _blame(layers, keys, path):
    if not path:
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
    p.set_defaults(func=cmd_validate)

    config = sub.add_parser("config", help="inspect resolved configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)

    p = config_sub.add_parser("resolve", help="print the resolved configuration")
    p.add_argument(
        "--raw", action="store_true",
        help="stop after the merge, leaving $(...) unsubstituted",
    )
    p.add_argument("experiment", type=Path)
    p.set_defaults(func=cmd_resolve)

    p = config_sub.add_parser("why", help="explain where a value came from")
    p.add_argument("experiment", type=Path)
    p.add_argument("key", help="dotted path, e.g. observations[0].obs error.covariance model")
    p.set_defaults(func=cmd_why)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (LayerError, MergeError, ResolveError, SiteError) as error:
        print(f"ackbar: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
