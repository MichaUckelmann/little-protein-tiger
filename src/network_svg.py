"""One rendering for every interaction/DepMap map LPT draws.

`export_subgraph` and the graph tools emit Cytoscape JSON; this turns that into
an SVG with a fixed visual grammar, so a map looks the same wherever it appears
(showcase pages, campaign reports, anything added later).

The grammar, and what each channel means:

    node fill      filled = a seed the query started from, hollow = pulled in
    node radius    degree within the drawn subgraph
    edge width     co-mention count in the corpus (sqrt-scaled)
    edge colour    sign of the DepMap correlation — positive, negative, or
                   dashed grey for "co-mentioned, no DepMap pair"
    edge opacity   |r|, so a weak correlation reads as a faint line

Nothing here reads the corpus itself: callers pass nodes and edges, so the same
renderer works on a saved .cyjs, a live `export_subgraph` result, or a
hand-built subgraph.

The layout is a deterministic Fruchterman-Reingold — seeded on a circle by
index, no RNG — so the same graph always produces the same picture and a
regenerated report does not churn.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

# Validated against the dataviz six checks for both light and dark surfaces;
# see docs/showcase/README.md. Callers may override via `palette=`.
PALETTE = {
    "positive": "var(--mark-a, #2b8f5d)",
    "negative": "var(--mark-b, #a07002)",
    "unknown": "var(--rule-2, #c6c9be)",
    "seed": "var(--mark-a, #2b8f5d)",
    "node": "var(--surface, #ffffff)",
    "node_edge": "var(--rule-2, #c6c9be)",
    "label": "var(--muted, #5d6357)",
    "label_seed": "var(--ink, #171a14)",
}

_STYLE = """
.lpt-net text{{font-family:var(--mono,ui-monospace,monospace);font-size:10.5px}}
.lpt-net .nl{{fill:{label}}}
.lpt-net .nl.seed{{fill:{label_seed};font-weight:500}}
.lpt-net .mk{{cursor:default}}
.lpt-net .mk:focus{{outline:none}}
.lpt-net .mk:hover line,.lpt-net .mk:focus line{{stroke-opacity:1}}
.lpt-net .mk:hover circle,.lpt-net .mk:focus circle{{filter:brightness(1.1)}}
"""


class Edge:
    """source, target, depmap r (or None), corpus co-mention count, tightest Kd."""

    __slots__ = ("source", "target", "r", "mentions", "kd")

    def __init__(self, source: str, target: str, r: float | None = None,
                 mentions: int = 1, kd: float | None = None):
        self.source, self.target = source, target
        self.r, self.mentions, self.kd = r, mentions, kd


def load_cyjs(path: str | Path, keep: Sequence[str] | None = None
              ) -> tuple[list[str], list[Edge], dict[str, dict]]:
    """Read a Cytoscape export. `keep` restricts to a node whitelist, in its order."""
    d = json.loads(Path(path).read_text())["elements"]
    meta = {n["data"]["id"]: n["data"] for n in d["nodes"]}
    nodes = [k for k in (keep or meta) if k in meta]
    ks, edges, seen = set(nodes), [], set()
    for e in d["edges"]:
        s, t = e["data"]["source"], e["data"]["target"]
        if s in ks and t in ks and (s, t) not in seen and (t, s) not in seen:
            seen.add((s, t))
            edges.append(Edge(s, t, e["data"].get("depmap_r"),
                              e["data"].get("mentions", 1),
                              e["data"].get("tightest_kd_M")))
    used = {n for e in edges for n in (e.source, e.target)}
    return [n for n in nodes if n in used], edges, meta


def layout(nodes: Sequence[str], edges: Iterable[Edge], width: int = 760,
           height: int = 470, iters: int = 520) -> dict[str, tuple[float, float]]:
    """Deterministic Fruchterman-Reingold. Same input -> same coordinates, always."""
    n = len(nodes)
    if n == 0:
        return {}
    idx = {k: i for i, k in enumerate(nodes)}
    pos = [[width / 2 + 190 * math.cos(2 * math.pi * i / n),
            height / 2 + 150 * math.sin(2 * math.pi * i / n)] for i in range(n)]
    adj = [[0.0] * n for _ in range(n)]
    for e in edges:
        i, j = idx[e.source], idx[e.target]
        adj[i][j] = adj[j][i] = 1.0
    k = math.sqrt(width * height / n) * 0.72
    for it in range(iters):
        temp = k * (1 - it / iters) * 0.11
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx, dy = pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]
                dist = max(0.01, math.hypot(dx, dy))
                ux, uy, rep = dx / dist, dy / dist, k * k / dist
                disp[i][0] += ux * rep; disp[i][1] += uy * rep
                disp[j][0] -= ux * rep; disp[j][1] -= uy * rep
                if adj[i][j]:
                    att = dist * dist / k
                    disp[i][0] -= ux * att; disp[i][1] -= uy * att
                    disp[j][0] += ux * att; disp[j][1] += uy * att
        for i in range(n):
            d = max(0.01, math.hypot(*disp[i]))
            pos[i][0] = min(width - 46, max(46, pos[i][0] + disp[i][0] / d * min(d, temp)))
            pos[i][1] = min(height - 26, max(26, pos[i][1] + disp[i][1] / d * min(d, temp)))
    return {k_: (pos[i][0], pos[i][1]) for k_, i in idx.items()}


def _edge_style(e: Edge, pal: dict) -> tuple[str, str, float, str]:
    if e.r is None:
        return pal["unknown"], "3 3", 0.35, "no DepMap pair"
    col = pal["negative"] if e.r < 0 else pal["positive"]
    return col, "", min(0.95, 0.32 + abs(e.r) * 1.5), f"DepMap r = {e.r:+.3f}"


def render_svg(nodes: Sequence[str], edges: Sequence[Edge],
               meta: dict[str, dict] | None = None, width: int = 760,
               height: int = 470, palette: dict | None = None,
               label_min_degree: int = 1, aria: str = "Interaction network") -> str:
    """The standard map. Returns a self-styling <svg> that inherits theme tokens."""
    meta, pal = meta or {}, {**PALETTE, **(palette or {})}
    pos = layout(nodes, edges, width, height)
    deg: dict[str, int] = {n: 0 for n in nodes}
    for e in edges:
        deg[e.source] += 1
        deg[e.target] += 1

    parts = [f'<svg viewBox="0 0 {width} {height}" class="lpt-net" role="img" '
             f'aria-label="{aria}"><style>{_STYLE.format(**pal)}</style>']
    for e in edges:
        (x1, y1), (x2, y2) = pos[e.source], pos[e.target]
        col, dash, op, lab = _edge_style(e, pal)
        w = 0.9 + min(3.2, (e.mentions or 1) ** 0.5)
        kd = f" · tightest Kd {e.kd:.2g} M" if e.kd else ""
        plural = "s" if (e.mentions or 1) != 1 else ""
        parts.append(
            f'<g class="mk" tabindex="0"><title>{e.source} — {e.target}: '
            f'{e.mentions} mention{plural} in the corpus, {lab}{kd}</title>'
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{col}" stroke-width="{w:.1f}" stroke-opacity="{op:.2f}" '
            f'stroke-dasharray="{dash}" stroke-linecap="round"/></g>')
    for n in nodes:
        x, y = pos[n]
        seed = bool(meta.get(n, {}).get("is_seed"))
        r = 6 + min(9, deg[n] * 0.9)
        label = (f'<text x="{x:.1f}" y="{y - r - 6:.1f}" text-anchor="middle" '
                 f'class="nl{" seed" if seed else ""}">{n}</text>'
                 if seed or deg[n] >= label_min_degree else "")
        parts.append(
            f'<g class="mk" tabindex="0"><title>{n} — {deg[n]} edges in this view, '
            f'{meta.get(n, {}).get("total_mentions", 0)} mentions in the corpus</title>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" '
            f'fill="{pal["seed"] if seed else pal["node"]}" '
            f'stroke="{pal["seed"] if seed else pal["node_edge"]}" stroke-width="2"/>'
            f'{label}</g>')
    parts.append("</svg>")
    return "".join(parts)


def legend_items() -> list[tuple[str, str]]:
    """(colour, meaning) pairs, so every caller writes the same legend."""
    return [(PALETTE["seed"], "seed the query started from"),
            (PALETTE["positive"], "positive DepMap correlation"),
            (PALETTE["negative"], "negative correlation"),
            (PALETTE["unknown"], "co-mentioned, no DepMap pair")]
