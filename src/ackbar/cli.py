"""The ackbar command line.

Two families of command, split by what they read.

**Before an experiment exists**, `config`, `graph` and `validate` take a path to
an experiment file and walk the layer stack. **After `ackbar create`**, every
command takes an experiment *name* and reads the frozen `cfg/experiment.yaml`.
That split is the "resolved once and frozen" rule made operational: a job eight
hours from now must not depend on a layer file nobody has touched since.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import harvest, heal, ledger, run, slurm, state
from .config.jobtime import SYMBOLS, render, symbols
from .config.layers import LayerError, merge_layers, resolve_layers
from .config.merge import MergeError
from .config.resolve import ResolveError, resolve
from .config.schema import load_schema, merge_keys
from .config.why import why
from .create import CreateError, create
from .duration import DurationError
from .graph import GraphError, build_graph, to_dot, to_text
from .paths import Paths
from .site import SiteError, load_site
from .submit import SubmitError, submit_cycle
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


def _frozen(name):
    """The frozen config of an experiment that already exists, by name.

    No layer tree, no substitution, no schema: this is the file the experiment
    was created from, and reading anything else here would mean a job's config
    could change under it between cycle 1 and cycle 50.
    """
    site = load_site()
    paths = Paths(
        experiment=name,
        output_root=Path(site["output_root"]),
        scratch_root=Path(site["scratch_root"]),
    )
    if not paths.frozen_config.exists():
        raise CreateError(
            f"no experiment named {name!r} under {site['output_root']} "
            f"({paths.frozen_config} does not exist). Run `ackbar create` first."
        )
    with open(paths.frozen_config) as handle:
        return yaml.safe_load(handle), site, paths


def cmd_create(args):
    layers, _, schema, config = _resolved(args)
    site = load_site()
    root = site["root"]
    paths, graph, scripts = create(
        config, site, schema, layers, root=root, force=args.force,
    )
    print(f"created {paths.experiment_dir}")
    print(f"  {len(layers)} layers frozen, {len(scripts)} job scripts emitted")
    print(f"  {len(graph.nodes)} nodes over {config['cycle']['count']} cycles")
    print(f"\nstart it with: ackbar start {paths.experiment}")
    return 0


def cmd_start(args):
    config, site, paths = _frozen(args.name)
    if ledger.submitted_cycles(paths):
        raise SubmitError(
            f"{args.name} has already been started. `ackbar resume` re-arms a "
            f"paused experiment; `ackbar heal` deals with a broken one."
        )
    return _report(submit_cycle(config, site, paths, 1, dry_run=args.dry_run))


def cmd_submit(args):
    config, site, paths = _frozen(args.name)
    return _report(submit_cycle(config, site, paths, args.cycle,
                                dry_run=args.dry_run))


def _report(records):
    for record in records:
        spec = record.get("array")
        if not isinstance(spec, str):
            spec = slurm.array_spec(record.get("members") or ())
        line = f"  {record['cycle']}.{record['task']}"
        if spec:
            line += f"[{spec}]"
        print(f"{line:<30} {record.get('job_id', '-')!s:>8}  "
              f"{record.get('dependency') or ''}")
    return 0


def cmd_run(args):
    """The body of every emitted job script."""
    config, site, paths = _frozen(args.name)
    return run.run_task(config, site, paths, args.cycle, args.task, args.member)


def cmd_pause(args):
    _, _, paths = _frozen(args.name)
    paths.halt_flag.write_text("paused by ackbar pause\n")
    print(f"{paths.halt_flag} created; the graph will drain and stop at a "
          f"cycle boundary")
    return 0


def cmd_resume(args):
    config, site, paths = _frozen(args.name)
    if paths.halt_flag.exists():
        paths.halt_flag.unlink()
        print(f"removed {paths.halt_flag}")
    cycles = ledger.submitted_cycles(paths)
    if not cycles:
        raise SubmitError(f"{args.name} has never been started; use `ackbar start`")
    nxt = max(cycles) + 1
    if nxt > config["cycle"]["count"]:
        print(f"cycle {max(cycles)} is the last; nothing to re-arm")
        return 0
    return _report(submit_cycle(config, site, paths, nxt, dry_run=args.dry_run))


def cmd_cancel(args):
    """Cancel the union of the ledger's job ids and a name-prefix queue scan.

    Both, because `scancel` has no name glob and the ledger cannot know about a
    job somebody submitted by hand, while the queue cannot know about a job that
    has not started yet under a name Slurm truncated.
    """
    _, _, paths = _frozen(args.name)
    known = {r["job_id"] for r in ledger.read(paths)}
    live = set(slurm.queue()) & known
    if not live:
        print("nothing of this experiment is in the queue")
        return 0
    print(f"cancelling {len(live)} job(s): {', '.join(str(i) for i in sorted(live))}")
    slurm.scancel(sorted(live))
    return 0


def cmd_status(args):
    """A read-only view, holding no state. Closing it does nothing.

    Deliberately not fused with `heal`. v3's driver was a curses UI in a
    foreground process that had to stay running for the workflow to advance, so
    a dropped ssh session stalled the experiment. Viewing must never be
    load-bearing.
    """
    config, _, paths = _frozen(args.name)
    graph = build_graph(config)
    statuses = state.collect(paths, graph)

    tasks = sorted({s.task for s in statuses.values()})
    width = max(len(t) for t in tasks)
    cycles = graph.cycles

    print(f"{args.name}: {len(graph.nodes)} nodes over {len(cycles)} cycles")
    print()
    # Cycle numbers read downward, a digit per row, because at fifty cycles the
    # only thing that fits in a column is one character.
    margin = " " * (width + 3)
    for power in (100, 10, 1):
        if max(cycles) < power:
            continue
        print(margin + "".join(
            str(c // power % 10) if c >= power else " " for c in cycles
        ))
    for task in tasks:
        row = "".join(
            state.GLYPH[statuses[f"{c}.{task}"].summary]
            if f"{c}.{task}" in statuses else " "
            for c in cycles
        )
        print(f"  {task:<{width}} {row}")

    print()
    print("  " + "  ".join(
        f"{state.GLYPH[name]} {name}" for name in state.SEVERITY
    ))

    overall = state.finished(statuses, graph)
    print(f"\n{args.name} is {overall}")

    if args.verbose:
        # Which job id was cycle 7's writeback, which is the question no
        # scheduler query can answer once the job has left the queue.
        print("\nnodes:")
        for node in graph.order():
            status = statuses[node]
            if not status.job_id:
                continue
            counts = ", ".join(f"{n} {s}" for s, n in
                               sorted(status.counts().items()))
            attempt = "" if status.attempt <= 1 else \
                f", attempt {status.attempt}"
            print(f"  {node:<{width + 4}} job {status.job_id}"
                  f"{attempt}: {counts}")

    if args.verbose or overall == "broken":
        broken = [i for i, s in statuses.items() if s.broken]
        broken.sort(key=lambda i: (statuses[i].cycle, statuses[i].task))
        if broken:
            print("\nbroken:")
            for line in heal.describe(statuses, broken):
                print(line)
            print("\nrun `ackbar heal` to resubmit the affected subgraph")
    return 0 if overall in ("finished", "running") else 1


def cmd_heal(args):
    config, site, paths = _frozen(args.name)
    graph = build_graph(config)
    statuses = state.collect(paths, graph)
    broken, closure, cancel = heal.plan(config, paths, graph, statuses)

    if not broken:
        print(f"{args.name}: nothing is broken")
        return 0

    print(f"{len(broken)} broken node(s):")
    for line in heal.describe(statuses, broken):
        print(line)

    stubborn = heal.unhealable(statuses, broken)
    if stubborn:
        print("\nresubmitting these unchanged will most likely fail the same "
              "way; consider fixing the cause first:")
        for node_id, reasons in stubborn:
            print(f"  {node_id}: {', '.join(reasons)}")

    # The blast radius, printed before anything is cancelled. One failed
    # forecast invalidates every cycle in flight.
    print(f"\nclosure is {len(closure)} node(s): {', '.join(closure)}")
    if cancel:
        print(f"will cancel {len(cancel)} live job(s): "
              f"{', '.join(str(i) for i in cancel)}")

    if args.dry_run:
        print("\n--dry-run, nothing was cancelled or submitted")
        return 0

    _, _, cancelled, records = heal.heal(config, site, paths, graph=graph)
    print(f"\ncancelled {len(cancelled)}, resubmitted {len(records)}:")
    return _report(records)


def cmd_harvest(args):
    config, site, paths = _frozen(args.name)
    cycles = args.cycles or build_graph(config).cycles
    for cycle in cycles:
        payload = harvest.write(paths, cycle, launcher=site.get("launcher", ""))
        totals = payload["totals"]
        print(f"  cycle {cycle}: {totals['jobs']} job(s), "
              f"{totals['failed']} not completed, "
              f"{totals['core_seconds']}s core, "
              f"peak RSS {totals['max_rss_kb']}K -> {paths.stats_file(cycle)}")
    return 0


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
        print(f"  {{{{{name}}}}}".ljust(26) + " " + description)
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
    root = site["root"]
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

    p = sub.add_parser("create", help="freeze an experiment on disk and emit its jobs")
    p.add_argument("experiment", type=Path)
    p.add_argument(
        "--force", action="store_true",
        help="overwrite an existing experiment that has not submitted anything",
    )
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("start", help="submit the first cycle")
    p.add_argument("name", help="experiment name, as given to `ackbar create`")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be submitted, and submit nothing")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("submit", help="submit one cycle; the submitter job's own verb")
    p.add_argument("name")
    p.add_argument("--cycle", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("run", help="run one task; this is what a job script calls")
    p.add_argument("name")
    p.add_argument("--cycle", type=int, required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--member", type=int, default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("pause", help="stop cleanly at the next cycle boundary")
    p.add_argument("name")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="clear the halt flag and re-arm")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("cancel", help="cancel every live job of an experiment")
    p.add_argument("name")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("status", help="a read-only view of what the experiment is doing")
    p.add_argument("name")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="detail every broken node, not only when something is")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("heal", help="resubmit the subgraph affected by a failure")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true",
                   help="show the closure and what would be cancelled")
    p.set_defaults(func=cmd_heal)

    p = sub.add_parser("harvest", help="pull sacct into stats/<cycle>.json")
    p.add_argument("name")
    p.add_argument("--cycle", dest="cycles", type=int, action="append",
                   help="only this cycle; repeatable. Default is all of them")
    p.set_defaults(func=cmd_harvest)

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
            DurationError, CreateError, SubmitError, slurm.SlurmError,
            run.TaskError, heal.HealError) as error:
        print(f"ackbar: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
