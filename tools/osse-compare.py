#!/usr/bin/env python
"""Score several experiments against each other in observation space.

    tools/osse-compare.py osse25-noda osse25-3dvar osse25-hybrid
    tools/osse-compare.py osse25-noda osse25-3dvar --skip 5

Reads `<experiment>/obs_out/<date>/summary.json`, the per-cycle summary
`post.obs` writes, and nothing else. Same reason `osse-plots.py` does: those
survive `cleanup`, so this runs on an experiment whose ioda output is long gone
and on one that is still going.

**Observation space is the whole of the comparison, for now.** `verify` is in
`run.DEFERRED` and unbuilt, so there is no state space score. What this measures
is how far each experiment's *background* was from the observations before it
saw them, which is a forecast score: an analysis that fits its own observations
better has proved nothing, and one whose next background fits the next cycle's
observations better has propagated the correction forward. That is the number a
forecast system is judged on and it is the one below.

What it cannot see is error where there are no observations. The synthetic
network here has no subsurface at all, so a scheme that improves the surface by
damaging the thermocline scores well and is worse. Read this against the state
space comparison when there is one.

The first cycles are dropped by `--skip`, because every experiment starts from
the same initial condition and their first backgrounds are therefore identical:
including them dilutes the difference toward zero by exactly the fraction of
the run they occupy.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

#: Beside `osse-plots.py`'s output, and served by the same monitor docroot.
DOCROOT = Path(__file__).resolve().parents[1] / "site" / "monitor" / "osse"

#: One colour per experiment, in the order they are named on the command line,
#: so the baseline is grey and whatever is being argued for is not.
COLOURS = ["#777777", "#1f4e79", "#a33", "#2a7", "#c80"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("experiments", nargs="+",
                        help="the baseline first; it is what the rest are scored against")
    parser.add_argument("--skip", type=int, default=3,
                        help="cycles to drop from the start, while the runs are still identical")
    parser.add_argument("--out", type=Path, default=None,
                        help="overrides the served directory")
    args = parser.parse_args(argv)

    root = os.environ.get("ACKBAR_OUTPUT_ROOT")
    if not root:
        sys.exit("osse-compare: ACKBAR_OUTPUT_ROOT is not set; "
                 "run `source site/activate.sh`")

    runs = {}
    for name in args.experiments:
        series = read(Path(root) / name)
        if not series:
            sys.exit(f"osse-compare: {name} has no obs_out/*/summary.json. "
                     f"Either it has not run a cycle yet or it carries no "
                     f"observers, which for a free run means no `hofx` task.")
        runs[name] = series

    out = args.out or DOCROOT / "compare"
    out.mkdir(parents=True, exist_ok=True)

    platforms = sorted({key for series in runs.values() for key in series})
    table = summarise(runs, platforms, args.skip)
    figures = [timeseries(runs, platforms, args.skip, out),
               skill(runs, platforms, args.skip, out)]
    (out / "summary.json").write_text(json.dumps(table, indent=2))
    page(out, args.experiments, args.skip, table, [f for f in figures if f])

    print(report(table, args.experiments))
    print(f"osse-compare: {out}")
    return 0


def read(experiment):
    """{(platform, variable): [(time, stats), ...]} for one experiment."""
    series = {}
    for path in sorted((experiment / "obs_out").glob("*/summary.json")):
        when = datetime.strptime(path.parent.name, "%Y%m%dT%H%M%SZ")
        for record in json.loads(path.read_text()).get("observers", []):
            for variable, stats in record.get("variables", {}).items():
                series.setdefault((record["name"], variable), []).append((when, stats))
    return {key: sorted(value) for key, value in series.items()}


def omb(entries, skip):
    """The O-B rms of each cycle after the first *skip*, as (times, values).

    A cycle whose observer was dropped, or whose summary carries no `omb`, is
    absent from both rather than zero. A zero reads as a perfect fit and would
    pull a mean down instead of leaving a hole in a line.
    """
    times, values = [], []
    for when, stats in entries[skip:]:
        value = stats.get("omb", {}).get("rms")
        if value is not None:
            times.append(when)
            values.append(float(value))
    return times, np.array(values)


def summarise(runs, platforms, skip):
    """The number the comparison comes down to: mean O-B rms, per platform."""
    table = {}
    for platform, variable in platforms:
        row = {}
        for name, series in runs.items():
            entries = series.get((platform, variable))
            if not entries:
                continue
            _, values = omb(entries, skip)
            if values.size:
                row[name] = {"omb_rms": float(values.mean()),
                             "cycles": int(values.size)}
        if row:
            table[f"{platform}/{variable}"] = row
    return table


def timeseries(runs, platforms, skip, out):
    if not platforms:
        return None
    figure, axes = plt.subplots(len(platforms), 1, squeeze=False,
                                figsize=(9, 2.8 * len(platforms)), sharex=True)
    for row, (platform, variable) in zip(axes[:, 0], platforms):
        for colour, (name, series) in zip(COLOURS, runs.items()):
            entries = series.get((platform, variable))
            if not entries:
                continue
            times, values = omb(entries, skip)
            row.plot(times, values, marker="o", ms=2.5, lw=1.2,
                     color=colour, label=name)
        row.set_title(f"{platform}: {variable}", fontsize=10)
        row.set_ylabel("O-B rms")
        row.grid(alpha=0.3)
        row.legend(fontsize=8, loc="upper right")
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(out / "omb.png", dpi=120)
    plt.close(figure)
    return ("omb.png",
            "Background departure per cycle, per platform. This is each "
            "experiment's forecast measured against observations it had not "
            "yet seen, so a line below another is a better forecast and not a "
            "better fit to what it just assimilated.")


def skill(runs, platforms, skip, out):
    """Percentage reduction against the first experiment named.

    One bar per platform per experiment, because a single number over all
    platforms would be dominated by whichever has the most observations, and
    which platform an experiment improves is most of what distinguishes one
    scheme from another.
    """
    baseline = next(iter(runs))
    others = [name for name in runs if name != baseline]
    if not others or not platforms:
        return None

    labels = [f"{platform}\n{variable}" for platform, variable in platforms]
    at = np.arange(len(platforms))
    width = 0.8 / len(others)

    figure, axis = plt.subplots(figsize=(1.4 * len(platforms) + 3, 4.5))
    for index, (colour, name) in enumerate(zip(COLOURS[1:], others)):
        gain = []
        for key in platforms:
            base = runs[baseline].get(key)
            theirs = runs[name].get(key)
            if not base or not theirs:
                gain.append(np.nan)
                continue
            b = omb(base, skip)[1].mean()
            t = omb(theirs, skip)[1].mean()
            gain.append(100.0 * (b - t) / b if b else np.nan)
        axis.bar(at + index * width - 0.4 + width / 2, gain, width,
                 color=colour, label=name)

    axis.axhline(0.0, color="#333", lw=1.0)
    axis.set_xticks(at)
    axis.set_xticklabels(labels, fontsize=8)
    axis.set_ylabel(f"% reduction in O-B rms against {baseline}")
    axis.grid(alpha=0.3, axis="y")
    axis.legend(fontsize=9)
    figure.tight_layout()
    figure.savefig(out / "skill.png", dpi=120)
    plt.close(figure)
    return ("skill.png",
            f"Mean background departure against {baseline}, as a percentage. "
            f"Above zero is better than the free run. A bar near zero on one "
            f"platform and large on another says the analysis moved the "
            f"surface it was shown and not the rest of the ocean.")


def report(table, order):
    """The comparison as text, for a terminal and for pasting into a message."""
    baseline = order[0]
    lines = [f"{'platform':<28}" + "".join(f"{name:>18}" for name in order)]
    for key, row in sorted(table.items()):
        cells = ""
        for name in order:
            entry = row.get(name)
            if entry is None:
                cells += f"{'-':>18}"
            elif name == baseline:
                cells += f"{entry['omb_rms']:>18.4f}"
            else:
                base = row.get(baseline, {}).get("omb_rms")
                gain = (100.0 * (base - entry["omb_rms"]) / base
                        if base else float("nan"))
                cells += f"{entry['omb_rms']:>11.4f}{gain:>+6.1f}%"
        lines.append(f"{key:<28}" + cells)
    return "\n".join(lines)


def page(out, order, skip, table, figures):
    body = [
        "<!doctype html><meta charset=utf-8>",
        "<title>OSSE comparison</title>",
        "<link rel=stylesheet href=../../style.css>",
        "<h1>OSSE, observation space</h1>",
        f"<p>{' vs '.join(order)}, first {skip} cycle(s) dropped. "
        "Background departures: each experiment's forecast against "
        "observations it had not yet seen.</p>",
        "<pre>" + report(table, order) + "</pre>",
    ]
    for name, caption in figures:
        body.append(f"<h2>{name}</h2><img src='{name}' style='max-width:100%'>")
        body.append(f"<p>{caption}</p>")
    (out / "index.html").write_text("\n".join(body))


if __name__ == "__main__":
    sys.exit(main())
