"""Human-readable and graphviz renderings of a graph.

The JSON form in `model.to_dict` is what goldens compare and what other tools
read. These two are for looking at.
"""

from .model import AFTERANY, AFTERCORR

#: Edge glyphs, so that the dependency type is visible without reading it.
GLYPH = {AFTERCORR: "=>", AFTERANY: "..", "afterok": "->"}


def to_text(graph, cycles=None):
    """One block per cycle, each task with what it waits on."""
    wanted = graph.cycles if cycles is None else [c for c in graph.cycles if c in cycles]
    lines = [f"{graph.experiment}: {len(graph.nodes)} nodes, "
             f"{len(graph.edges)} edges, throttle {graph.throttle}"]

    for cycle in wanted:
        nodes = sorted(graph.cycle_nodes(cycle), key=lambda n: n.task)
        lines.append(f"\ncycle {cycle}")
        for node in nodes:
            shape = f"[{_range(node.members)}]" if node.is_array else ""
            deps = graph.parents(node.id)
            waits = " ".join(
                f"{GLYPH[e.kind]} {e.parent}" for e in sorted(deps, key=lambda e: e.parent)
            )
            lines.append(f"  {node.task + shape:<28} {waits}")
    lines.append(f"\nedge types: {GLYPH['afterok']} afterok  "
                 f"{GLYPH[AFTERCORR]} aftercorr (elementwise)  "
                 f"{GLYPH[AFTERANY]} afterany (leaf, tolerates failure)")
    return "\n".join(lines)


def _range(members):
    """`0-20` for a contiguous set, an explicit list otherwise."""
    if len(members) > 1 and members == tuple(range(members[0], members[-1] + 1)):
        return f"{members[0]}-{members[-1]}"
    return ",".join(str(m) for m in members)


def to_dot(graph, cycles=None):
    wanted = graph.cycles if cycles is None else [c for c in graph.cycles if c in cycles]
    keep = {n.id for n in graph.nodes if n.cycle in wanted}

    out = ["digraph ackbar {", "  rankdir=LR;", "  node [shape=box];"]
    for cycle in wanted:
        out.append(f"  subgraph cluster_{cycle} {{")
        out.append(f'    label="cycle {cycle}";')
        for node in sorted(graph.cycle_nodes(cycle), key=lambda n: n.task):
            shape = f"\\n[{_range(node.members)}]" if node.is_array else ""
            out.append(f'    "{node.id}" [label="{node.task}{shape}"];')
        out.append("  }")
    for edge in graph.edges:
        if edge.parent in keep and edge.child in keep:
            style = "dashed" if edge.kind == AFTERANY else "solid"
            out.append(
                f'  "{edge.parent}" -> "{edge.child}" '
                f'[label="{edge.kind}", style={style}];'
            )
    out.append("}")
    return "\n".join(out)
