#!/usr/bin/env python
"""Render every experiment's progress as a static site, once or on a loop.

    .venv/bin/python tools/monitor.py --once
    tools/monitor.sh                          # the loop plus the web server

Static files rather than a service. `ackbar status` already refuses to be
load-bearing (see `cmd_status`: v3's curses driver had to stay running for the
workflow to advance, so a dropped ssh session stalled an experiment), and a
monitor that an experiment depends on is the same mistake wearing a browser.
This reads the experiment tree, writes HTML and PNG, and exits. Nothing in the
workflow knows it exists, killing it does nothing, and what it serves is a
directory of files that survive it.

It reads three things and each is optional, so an experiment that has only just
been created renders as an experiment that has only just been created:

  run/ledger.jsonl + run/<date>/done/ + sacct   what has run, via `state.collect`
  run/<date>/stats.json                         what it cost, via `ackbar harvest`
  obs_out/<date>/summary.json                   what it assimilated
  ana|bkg/<date>/mem###.nc                      what it produced

The last two panels are absent rather than empty where their files do not
exist, which is the honest rendering of a cycle that has not written them.
"""

import argparse
import html
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402

from ackbar import state  # noqa: E402
from ackbar.graph.build import build_graph  # noqa: E402
from ackbar.paths import Paths  # noqa: E402
from ackbar.site import load_site  # noqa: E402

#: One colour per node state, matching the severity order in `ackbar.state` so
#: that the grid here and the glyph grid `ackbar status` prints are the same
#: reading. Failed is the only one that has to be findable at a glance from
#: across the room, so it is the only saturated colour.
COLOUR = {
    state.COMPLETE: "#3f7f5f",
    state.RUNNING: "#4a7fb5",
    state.PENDING: "#7a7a86",
    state.UNSUBMITTED: "#3a3a42",
    state.STRANDED: "#b58a3a",
    state.FAILED: "#c0392b",
}

#: How long a browser waits before asking again. Matched to `monitor.sh`'s
#: default interval: refreshing faster than the page is rebuilt just reloads
#: the same bytes, and refreshing slower means the tab is stale by definition.
REFRESH_SECONDS = 60


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docroot", type=Path,
                        default=REPO / "site" / "monitor",
                        help="where the site is written")
    parser.add_argument("--once", action="store_true",
                        help="render one pass and exit")
    parser.add_argument("--every", type=int, default=REFRESH_SECONDS,
                        help="seconds between passes when looping")
    parser.add_argument("--root", type=Path,
                        help="the output root to scan; defaults to the site's. "
                             "Aim it at the tier 3 tree to watch a test run")
    args = parser.parse_args(argv)

    root = args.root or Path(load_site()["output_root"])
    while True:
        try:
            count = render(root, args.docroot)
            print(f"monitor: {count} experiment(s) -> {args.docroot}", flush=True)
        except Exception as error:            # noqa: BLE001
            # A monitor that dies on one malformed experiment stops reporting on
            # every healthy one, which is the opposite of what it is for.
            print(f"monitor: pass failed: {error!r}", flush=True)
        if args.once:
            return 0
        time.sleep(args.every)


def render(root, docroot):
    docroot.mkdir(parents=True, exist_ok=True)
    (docroot / "style.css").write_text(STYLE)

    experiments = []
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if not (entry / "cfg" / "experiment.yaml").exists():
            continue
        summary = one(entry, docroot, root)
        if summary:
            experiments.append(summary)

    (docroot / "index.html").write_text(index_page(experiments, root))
    return len(experiments)


def one(experiment_dir, docroot, root):
    """Read one experiment and write its page. Returns the index row."""
    name = experiment_dir.name
    config = yaml.safe_load((experiment_dir / "cfg" / "experiment.yaml").read_text())
    paths = Paths.of(config, {"output_root": root, "scratch_root": experiment_dir})
    graph = build_graph(config)

    try:
        statuses = state.collect(paths, graph)
        overall = state.finished(statuses, graph)
    except Exception as error:                    # noqa: BLE001
        # `state.collect` shells out to squeue and sacct. When the controller is
        # down, what is on disk is still true, so say that rather than nothing.
        statuses, overall = sentinels(paths, graph), f"unknown ({error!r})"

    target = docroot / name
    target.mkdir(parents=True, exist_ok=True)
    stats = read_stats(paths, graph.cycles)
    figures = [f for f in (cost_figure(stats, target),
                           elapsed_figure(stats, target)) if f]

    (target / "index.html").write_text(experiment_page(
        name, config, graph, statuses, overall, stats, figures))

    done = sum(1 for s in statuses.values() if s.summary == state.COMPLETE)
    return {
        "name": name,
        "description": (config.get("experiment") or {}).get("description", ""),
        "overall": overall,
        "done": done,
        "nodes": len(graph.nodes),
        "cycles": len(graph.cycles),
        "core_seconds": sum(s.get("totals", {}).get("core_seconds", 0)
                            for s in stats.values()),
        "broken": sum(1 for s in statuses.values() if s.broken),
    }


def sentinels(paths, graph):
    """The fallback view: what the `done/` directory proves, and nothing else.

    Every task writes its sentinel last, so its presence is the only proof a
    task finished (see `Paths.sentinel`). That makes this an under-report and
    never an over-report: a running job looks unsubmitted here, which is the
    right direction for a degraded reading to err in.
    """
    out = {}
    for node in graph.nodes:
        status = state.NodeStatus(cycle=node.cycle, task=node.task,
                                  members=node.members)
        for member in (node.members or (None,)):
            proof = paths.sentinel(node.cycle, node.task, member)
            status.elements[member] = (state.COMPLETE if proof.exists()
                                       else state.UNSUBMITTED)
        out[node.id] = status
    return out


def read_stats(paths, cycles):
    """`stats/<cycle>.json` for every cycle that has one. Keyed by cycle."""
    out = {}
    for cycle in cycles:
        path = paths.stats_file(cycle)
        if not path.exists():
            continue
        try:
            out[cycle] = json.loads(path.read_text())
        except json.JSONDecodeError:
            # Harvest writes through a temporary and renames, so a torn file
            # here means it is being written right now. It will be whole next
            # pass and one missing cycle is not worth a failed render.
            continue
    return out


# --- figures ------------------------------------------------------------------

def cost_figure(stats, target):
    """Core-seconds per cycle. The one number that says what an experiment costs."""
    cycles = sorted(stats)
    values = [stats[c].get("totals", {}).get("core_seconds", 0) for c in cycles]
    if not any(values):
        return None
    figure, axes = _axes("core seconds")
    axes.bar(cycles, values, color="#4a7fb5")
    axes.set_xlabel("cycle")
    return _save(figure, target / "cost.png", "cost per cycle")


def elapsed_figure(stats, target):
    """Wall clock per task, by cycle. Where a slow experiment is slow."""
    series = {}
    for cycle in sorted(stats):
        for job in stats[cycle].get("jobs", []):
            seconds = _seconds(job.get("elapsed"))
            if job.get("task") and seconds:
                # Max over members: an array's wall clock is its slowest
                # element, because the cycle waits for all of them.
                key = (job["task"], cycle)
                series[key] = max(series.get(key, 0), seconds)
    if not series:
        return None
    tasks = sorted({task for task, _ in series})
    figure, axes = _axes("seconds")
    for task in tasks:
        points = sorted((c, s) for (t, c), s in series.items() if t == task)
        axes.plot([c for c, _ in points], [s for _, s in points],
                  marker="o", markersize=3, label=task)
    axes.set_xlabel("cycle")
    axes.legend(fontsize=6, ncol=2, frameon=False, labelcolor="#c8c8d0")
    return _save(figure, target / "elapsed.png", "wall clock per task")


def _axes(ylabel):
    figure, axes = plt.subplots(figsize=(7.5, 3.0), dpi=140)
    figure.patch.set_facecolor("#16161c")
    axes.set_facecolor("#16161c")
    axes.set_ylabel(ylabel)
    for spine in ("top", "right"):
        axes.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        axes.spines[spine].set_color("#4a4a56")
    axes.tick_params(colors="#c8c8d0", labelsize=7)
    axes.yaxis.label.set_color("#c8c8d0")
    axes.xaxis.label.set_color("#c8c8d0")
    axes.grid(axis="y", color="#2a2a34", linewidth=0.6)
    axes.set_axisbelow(True)
    return figure, axes


def _save(figure, path, caption):
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)
    return {"src": path.name, "caption": caption}


def _seconds(elapsed):
    """`[DD-]HH:MM:SS` to seconds. Slurm's spelling, and it is not ISO."""
    text = (elapsed or "").strip()
    if not text:
        return 0
    days, _, rest = text.rpartition("-")
    parts = [int(p) for p in rest.split(":")] if rest else []
    total = 0
    for part in parts:
        total = total * 60 + part
    return total + int(days or 0) * 86400


# --- pages --------------------------------------------------------------------

def index_page(experiments, root):
    rows = []
    for item in experiments:
        badge = f'<span class="pill {_class(item["overall"])}">{_e(item["overall"])}</span>'
        rows.append(
            f'<tr><td><a href="{_e(item["name"])}/">{_e(item["name"])}</a>'
            f'<div class="sub">{_e(item["description"])}</div></td>'
            f'<td>{badge}</td>'
            f'<td class="num">{item["done"]} / {item["nodes"]}</td>'
            f'<td class="num">{item["cycles"]}</td>'
            f'<td class="num">{item["core_seconds"]:,.0f}</td>'
            f'<td class="num">{item["broken"] or ""}</td></tr>'
        )
    body = (
        "<table><thead><tr><th>experiment</th><th>state</th><th>tasks done</th>"
        "<th>cycles</th><th>core s</th><th>broken</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        if rows else
        f'<p class="empty">No experiments under <code>{_e(str(root))}</code> yet.</p>'
    )
    return _page("ackbar", f"<h1>ackbar</h1><p class='sub'>{_e(str(root))}</p>{body}")


def experiment_page(name, config, graph, statuses, overall, stats, figures):
    cycle_conf = config.get("cycle") or {}
    facts = [
        ("state", overall),
        ("cycles", f"{len(graph.cycles)} x {cycle_conf.get('length', '?')}"
                   f" from {cycle_conf.get('start', '?')}"),
        ("solver", (config.get("solver") or {}).get("name", "none")),
        ("domain", (config.get("domain") or {}).get("name", "?")),
        ("model", (config.get("model") or {}).get("name", "?")),
    ]
    members = (config.get("ensemble") or {}).get("size")
    if members:
        facts.append(("ensemble", str(members)))

    parts = [
        f"<p><a href='../'>&larr; all experiments</a></p><h1>{_e(name)}</h1>",
        f"<p class='sub'>{_e((config.get('experiment') or {}).get('description', ''))}</p>",
        "<dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(str(v))}</dd>"
                         for k, v in facts) + "</dl>",
        "<h2>progress</h2>",
        grid(graph, statuses),
    ]
    for figure in figures:
        parts.append(f"<h2>{_e(figure['caption'])}</h2>"
                     f"<img src='{_e(figure['src'])}' alt='{_e(figure['caption'])}'>")
    if not stats:
        parts.append("<p class='empty'>No harvested accounting yet; the cost and "
                     "timing panels appear once <code>stats/&lt;cycle&gt;.json</code> "
                     "exists.</p>")
    parts.append(
        "<p class='empty'>Observation-space and state-space verification panels "
        "arrive with the <code>post.obs</code>, <code>post.state</code> and "
        "<code>verify</code> bodies. They are listed as deferred in "
        "<code>run.py</code> and are absent here rather than empty.</p>")
    return _page(f"ackbar: {name}", "".join(parts))


def grid(graph, statuses):
    """Cycle across, task down. The same reading as `ackbar status`, in colour."""
    tasks = sorted({node.task for node in graph.nodes})
    head = "".join(f"<th>{c}</th>" for c in graph.cycles)
    rows = []
    for task in tasks:
        cells = []
        for cycle in graph.cycles:
            status = statuses.get(f"{cycle}.{task}")
            if status is None:
                cells.append("<td class='cell'></td>")
                continue
            summary = status.summary
            counts = ", ".join(f"{n} {s}" for s, n in sorted(status.counts().items()))
            title = f"{cycle}.{task}: {summary}" + (f" ({counts})" if counts else "")
            cells.append(f"<td class='cell' style='background:{COLOUR[summary]}' "
                         f"title='{_e(title)}'></td>")
        rows.append(f"<tr><th class='task'>{_e(task)}</th>{''.join(cells)}</tr>")
    legend = " ".join(
        f"<span class='key'><i style='background:{COLOUR[n]}'></i>{n}</span>"
        for n in state.SEVERITY)
    return (f"<table class='grid'><thead><tr><th></th>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            f"<p class='legend'>{legend}</p>")


def _class(overall):
    return {"finished": "ok", "running": "run", "broken": "bad"}.get(overall, "idle")


def _e(value):
    return html.escape(str(value))


def _page(title, body):
    return (f"<!doctype html><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>"
            f"<title>{_e(title)}</title>"
            f"<link rel='stylesheet' href='/style.css'>"
            f"<main>{body}</main>"
            f"<footer>rebuilt every {REFRESH_SECONDS}s by "
            f"<code>tools/monitor.py</code></footer>")


STYLE = """\
:root { color-scheme: dark; }
body { margin: 0; background: #101015; color: #d8d8e0;
       font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
main { max-width: 62rem; margin: 0 auto; padding: 2.5rem 1.5rem 1rem; }
h1 { font-size: 1.5rem; margin: 0 0 .2rem; letter-spacing: .02em; }
h2 { font-size: .95rem; text-transform: uppercase; letter-spacing: .1em;
     color: #8a8a98; margin: 2.2rem 0 .6rem; font-weight: 500; }
a { color: #7fb3e0; }
.sub { color: #8a8a98; margin: 0 0 1.4rem; }
.empty { color: #8a8a98; border-left: 2px solid #2a2a34; padding-left: .8rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .4rem .6rem;
         border-bottom: 1px solid #23232c; }
th { color: #8a8a98; font-weight: 500; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
dl { display: grid; grid-template-columns: 7rem 1fr; gap: .15rem 1rem;
     margin: 0 0 1rem; }
dt { color: #8a8a98; }
dd { margin: 0; }
.grid { width: auto; table-layout: fixed; }
.grid th, .grid td { border: none; padding: 0; }
.grid thead th { font-size: .6rem; color: #6a6a78; padding: 0 0 .3rem;
                 text-align: center; width: 14px; }
.grid .task { padding-right: .7rem; font-size: .78rem; color: #b0b0bc;
              white-space: nowrap; }
.cell { width: 12px; height: 12px; border: 1px solid #101015 !important; }
.legend { margin-top: .8rem; font-size: .72rem; color: #8a8a98; }
.key { margin-right: .9rem; white-space: nowrap; }
.key i { display: inline-block; width: 9px; height: 9px; margin-right: .3rem; }
.pill { padding: .1rem .5rem; border-radius: 999px; font-size: .72rem; }
.pill.ok { background: #24402f; color: #7fd1a3; }
.pill.run { background: #1f3550; color: #8ec2f0; }
.pill.bad { background: #4a2020; color: #f0a09a; }
.pill.idle { background: #2a2a34; color: #a0a0ae; }
img { width: 100%; border-radius: 4px; }
footer { max-width: 62rem; margin: 0 auto; padding: 1rem 1.5rem 3rem;
         color: #55555f; font-size: .72rem; }
code { color: #b0b0bc; }
"""


if __name__ == "__main__":
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/ackbar-mpl")
    sys.exit(main())
