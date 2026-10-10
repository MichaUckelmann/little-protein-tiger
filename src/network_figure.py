"""Publication figure of a protein pair's corpus and DepMap neighbourhood.

`network_svg` draws maps for web pages: hover tooltips, a force layout, labels that
can overlap. A printed figure has no hover, so this module draws the same two
quantities with everything stated on the page:

    edge width      number of DISTINCT papers in the corpus reporting the pair (log scale)
    edge colour     sign of the DepMap CRISPR co-dependency r (blue +, vermilion -)
    edge opacity    |r|
    dashed grey     co-mentioned, no DepMap pair
    edge label      r, and the tightest Kd / Ki, where there is something to say
    filled node     one of the two proteins the figure is about

The raw export is not drawn directly. It carries one node per spelling (KRASG12D,
KRAS4B, MLL1, MENIN ...), so nodes are collapsed to the human gene symbol the corpus
resolved them to, and an edge's evidence is the UNION of its papers' DOIs: summing
mention counts would count one paper once per spelling. Which neighbours to draw is a
choice made here and stated in the figure: each seed's strongest partners (by distinct
papers) plus every protein linked to both seeds. It is a view of the corpus, not a
claim that these partners are the biologically important ones.

Deterministic: no randomness, so a regenerated figure is byte-identical.
"""
from __future__ import annotations

import html
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

POS, NEG, NONE_COL = "#0072B2", "#D55E00", "#8C8C8C"      # Okabe-Ito blue / vermilion
SEED_FILL = "#1B2A41"
FONT = "Liberation Sans, Arial, Helvetica, sans-serif"
E = html.escape


_VARIANT = re.compile(r"[A-Z]\d{1,4}[A-Z*]$")


def is_variant(raw_id: str, gene: str) -> bool:
    """A node spelled as a point mutant (KRASG12D, SHOC2M173I, KRAS-Q61R), not a name variant (K-RAS, MLL1)."""
    return raw_id != gene and bool(_VARIANT.search(raw_id))


@dataclass
class GEdge:
    a: str
    b: str
    dois: set = field(default_factory=set)
    kd: float | None = None          # tightest over wild-type-named rows; falls back to any row
    ki: float | None = None
    kd_mut: bool = False             # True when the value shown comes only from a mutant-named row
    ki_mut: bool = False
    r: float | None = None
    depmap_n: int | None = None

    @property
    def papers(self) -> int:
        return len(self.dois)


def collapse(cyjs: str | Path, seeds: list[str]) -> tuple[dict[str, GEdge], dict]:
    """Gene-level edges {(a,b): GEdge} from a Cytoscape export, plus bookkeeping."""
    els = json.loads(Path(cyjs).read_text(encoding="utf-8"))["elements"]
    gene, dropped = {}, 0
    for n in els["nodes"]:
        d = n["data"]
        gene[d["id"]] = d.get("human_gene_symbol")
        dropped += 0 if gene[d["id"]] else 1
    edges: dict[tuple, GEdge] = {}
    for e in els["edges"]:
        d = e["data"]
        s, t = gene.get(d["source"]), gene.get(d["target"])
        if not s or not t or s == t:
            continue
        key = tuple(sorted((s, t)))
        g = edges.setdefault(key, GEdge(*key))
        g.dois |= {x for x in (d.get("dois") or "").split(";") if x}
        mut = is_variant(d["source"], s) or is_variant(d["target"], t)
        for attr, f in (("kd", "tightest_kd_M"), ("ki", "tightest_ki_M")):
            v = d.get(f)
            if not v:
                continue
            cur, cur_mut = getattr(g, attr), getattr(g, attr + "_mut")
            # a wild-type-named value always replaces a mutant one; within a class keep the tightest
            if cur is None or (cur_mut and not mut) or (cur_mut == mut and v < cur):
                setattr(g, attr, v)
                setattr(g, attr + "_mut", mut)
        if d.get("depmap_r") is not None:
            g.r, g.depmap_n = d["depmap_r"], d.get("depmap_n")
    return edges, {"unresolved_nodes": dropped, "seeds": seeds}


def select(edges: dict, seeds: tuple[str, str], per_seed: int = 4, shared: int = 5) -> dict:
    """Pick the nodes to draw: shared partners, then each seed's strongest private partners."""
    a, b = seeds
    nb: dict[str, dict[str, GEdge]] = defaultdict(dict)       # neighbour -> {seed: edge}
    for (x, y), g in edges.items():
        for s, o in ((a, b), (b, a)):
            if s in (x, y):
                other = y if x == s else x
                if other != o:
                    nb[other][s] = g
    sh = sorted((n for n, d in nb.items() if len(d) == 2),
                key=lambda n: (-min(nb[n][a].papers, nb[n][b].papers), n))[:shared]

    def strongest(s):
        c = [n for n, d in nb.items() if s in d and len(d) == 1 and n not in sh]
        return sorted(c, key=lambda n: (-nb[n][s].papers, -abs(nb[n][s].r or 0), n))[:per_seed]

    only_a, only_b = strongest(a), strongest(b)
    nodes = [a, b, *sh, *only_a, *only_b]
    keep = set(nodes)
    sub = {k: g for k, g in edges.items() if k[0] in keep and k[1] in keep}
    return {"nodes": nodes, "shared": sh, "only_a": only_a, "only_b": only_b, "edges": sub,
            "n_candidates": len(nb)}


def fmt_k(v: float, name: str) -> str:
    for scale, unit in ((1e-3, "mM"), (1e-6, "µM"), (1e-9, "nM"), (1e-12, "pM")):
        if v >= scale * 0.9995:
            return f"{name} {float(f'{v / scale:.2g}'):g} {unit}"
    return f"{name} {v:.1e} M"


def edge_width(n: int) -> float:
    return min(8.0, 1.3 + 1.15 * math.log2(max(n, 1)))


def edge_opacity(r: float | None) -> float:
    return 0.5 if r is None else 0.38 + 0.62 * min(abs(r) / 0.6, 1.0)


def edge_color(r: float | None) -> str:
    return NONE_COL if r is None else (NEG if r < 0 else POS)


def layout(sel: dict, seeds: tuple[str, str], W: int, top: int, bottom: int) -> dict[str, tuple[float, float]]:
    """Seeds on a horizontal axis; shared partners above and below it (never ON it, which
    would hide the seed-seed edge); each seed's private partners on an outer arc."""
    a, b = seeds
    yc = (top + bottom) / 2
    dx = 185
    pos = {a: (W / 2 - dx, yc), b: (W / 2 + dx, yc)}
    sh = sel["shared"]
    up, down = sh[0::2], sh[1::2]
    for group, sign in ((up, -1), (down, +1)):
        for i, n in enumerate(group):
            pos[n] = (W / 2 + (0 if len(group) < 3 else (i - 1) * 0), yc + sign * (110 + 84 * i))
    for group, seed, sign in ((sel["only_a"], a, -1), (sel["only_b"], b, +1)):
        k = len(group)
        for i, n in enumerate(group):
            ang = math.radians((i - (k - 1) / 2) * (140 / max(k - 1, 1))) if k > 1 else 0.0
            R = 190
            pos[n] = (pos[seed][0] + sign * R * math.cos(ang), pos[seed][1] + R * math.sin(ang))
    return pos


class Boxes:
    """Occupied rectangles, so labels can be placed where nothing is."""

    def __init__(self):
        self.r: list[tuple[float, float, float, float]] = []

    def add(self, x0, y0, x1, y1):
        self.r.append((x0, y0, x1, y1))

    def hit(self, x0, y0, x1, y1) -> bool:
        return any(x0 < b[2] and x1 > b[0] and y0 < b[3] and y1 > b[1] for b in self.r)


def tw(s: str, size: float, bold: bool = False) -> float:
    return len(s) * size * (0.58 if bold else 0.53)


def uses_mutant(sel: dict, seeds: tuple[str, str]) -> bool:
    return any((g.kd and g.kd_mut) or (g.ki and g.ki_mut) for g in sel["edges"].values()
               if g.a in seeds or g.b in seeds)


def render(sel: dict, seeds: tuple[str, str], title: str, subtitle: str, footer: str,
           W: int = 900, H: int = 660) -> str:
    top, bottom = 100, H - 148
    pos = layout(sel, seeds, W, top, bottom)
    seedset = set(seeds)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>']
    # title
    out.append(f'<text x="24" y="38" font-size="24" font-weight="bold" fill="#111">{E(title)}</text>')
    out.append(f'<text x="24" y="62" font-size="14" fill="#555">{E(subtitle)}</text>')

    boxes = Boxes()
    rad = {n: (26 if n in seedset else 17) for n in sel["nodes"]}
    for n, (x, y) in pos.items():
        boxes.add(x - rad[n], y - rad[n], x + rad[n], y + rad[n])

    # edges, strongest last so thin lines do not hide under thick ones
    edges = sorted((g for g in sel["edges"].values() if g.a in seedset or g.b in seedset),
                   key=lambda g: g.papers)
    for g in edges:
        (x1, y1), (x2, y2) = pos[g.a], pos[g.b]
        dash = "" if g.r is not None else ' stroke-dasharray="2 7"'
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{edge_color(g.r)}" '
                   f'stroke-width="{edge_width(g.papers):.1f}" stroke-opacity="{edge_opacity(g.r):.2f}" '
                   f'stroke-linecap="round"{dash}/>')

    # node labels (placed first so edge labels avoid them)
    cx, cy = W / 2, (top + bottom) / 2
    labels = []
    for n in sel["nodes"]:
        x, y = pos[n]
        seed = n in seedset
        size = 19 if seed else 16
        w = tw(n, size, seed)
        vx, vy = x - cx, y - cy
        norm = math.hypot(vx, vy) or 1
        cands = []
        if seed:
            cands = [(x, y - rad[n] - 10, "middle"), (x, y + rad[n] + 22, "middle")]
        else:
            ox, oy = vx / norm, vy / norm
            ang0 = math.atan2(oy, ox)
            for da in (0, 0.5, -0.5, 1.0, -1.0, math.pi):
                a_ = ang0 + da
                lx, ly = x + math.cos(a_) * (rad[n] + 7), y + math.sin(a_) * (rad[n] + 7)
                anchor = "start" if math.cos(a_) > 0.35 else "end" if math.cos(a_) < -0.35 else "middle"
                cands.append((lx, ly + (size * 0.35 if abs(math.sin(a_)) < 0.5 else (size if math.sin(a_) > 0 else -2)), anchor))
        chosen = cands[0]
        for lx, ly, anc in cands:
            x0 = lx - (w if anc == "end" else w / 2 if anc == "middle" else 0)
            if not boxes.hit(x0, ly - size, x0 + w, ly + 4) and 4 < x0 and x0 + w < W - 4 and ly - size > top - 30:
                chosen = (lx, ly, anc)
                break
        lx, ly, anc = chosen
        x0 = lx - (w if anc == "end" else w / 2 if anc == "middle" else 0)
        boxes.add(x0, ly - size, x0 + w, ly + 4)
        labels.append((n, lx, ly, anc, size, seed))

    # edge labels
    elabels = []
    any_mut = False
    for g in sorted(edges, key=lambda g: -g.papers):
        pair = {g.a, g.b} == seedset
        parts = []
        if pair:
            parts.append(f"{g.papers} paper{'s' if g.papers != 1 else ''}")
        if g.r is not None and (pair or abs(g.r) >= 0.3):
            parts.append(f"r {g.r:+.2f}")
        for v, nm, mut in ((g.kd, "Kd", g.kd_mut), (g.ki, "Ki", g.ki_mut)):
            if v:
                parts.append(fmt_k(v, nm) + ("*" if mut else ""))
                any_mut = any_mut or mut
        if not parts:
            continue
        txt = " · ".join(parts)
        size = 14.5 if pair else 12.5
        w = tw(txt, size) + 10
        (x1, y1), (x2, y2) = pos[g.a], pos[g.b]
        placed = None
        for t in (0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74):
            mx, my = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t - (14 if pair else 0)
            if not boxes.hit(mx - w / 2, my - size, mx + w / 2, my + 6) and w / 2 + 4 < mx < W - w / 2 - 4:
                placed = (mx, my)
                break
        if placed is None:
            continue
        boxes.add(placed[0] - w / 2, placed[1] - size, placed[0] + w / 2, placed[1] + 6)
        elabels.append((txt, placed[0], placed[1], size, pair))

    for txt, mx, my, size, pair in elabels:
        out.append(f'<text x="{mx:.1f}" y="{my:.1f}" font-size="{size}" text-anchor="middle" '
                   f'fill="{"#111" if pair else "#333"}" font-weight="{"bold" if pair else "normal"}" '
                   f'stroke="#fff" stroke-width="4.5" paint-order="stroke" stroke-linejoin="round">{E(txt)}</text>')

    for s in seeds:       # a seed with nothing else in the corpus is a finding, so say it
        if not any(s in (g.a, g.b) and {g.a, g.b} != seedset for g in sel["edges"].values()):
            out.append(f'<text x="{pos[s][0]:.1f}" y="{pos[s][1] + rad[s] + 24:.1f}" font-size="13" font-style="italic" '
                       f'text-anchor="middle" fill="#666">no other partners in the corpus</text>')
    for n in sel["nodes"]:
        x, y = pos[n]
        seed = n in seedset
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad[n]}" fill="{SEED_FILL if seed else "#fff"}" '
                   f'stroke="{SEED_FILL if seed else "#555"}" stroke-width="2"/>')
    for n, lx, ly, anc, size, seed in labels:
        out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="{size}" text-anchor="{anc}" '
                   f'font-weight="{"bold" if seed else "normal"}" fill="#111" stroke="#fff" stroke-width="4" '
                   f'paint-order="stroke" stroke-linejoin="round">{E(n)}</text>')

    # legend
    ly0 = H - 124
    out.append(f'<line x1="24" y1="{ly0 - 12}" x2="{W - 24}" y2="{ly0 - 12}" stroke="#DDD"/>')
    out.append(f'<text x="24" y="{ly0 + 8}" font-size="13" font-weight="bold" fill="#333">DepMap r</text>')
    lx = 100
    for col, dash, lab, r in ((POS, "", "positive", 0.5), (NEG, "", "negative", -0.5),
                              (NONE_COL, "2 7", "no DepMap pair", None)):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<line x1="{lx}" y1="{ly0 + 3}" x2="{lx + 34}" y2="{ly0 + 3}" stroke="{col}" stroke-width="3.5" '
                   f'stroke-opacity="{edge_opacity(r):.2f}" stroke-linecap="round"{da}/>')
        out.append(f'<text x="{lx + 42}" y="{ly0 + 8}" font-size="13" fill="#333">{lab}</text>')
        lx += 42 + tw(lab, 13) + 24
    out.append(f'<text x="{lx + 10}" y="{ly0 + 8}" font-size="13" fill="#555">fainter = weaker |r|</text>')
    out.append(f'<text x="24" y="{ly0 + 36}" font-size="13" font-weight="bold" fill="#333">Papers</text>')
    lx = 100
    for n in (1, 3, 10, 30):
        out.append(f'<line x1="{lx}" y1="{ly0 + 31}" x2="{lx + 34}" y2="{ly0 + 31}" stroke="#555" stroke-width="{edge_width(n):.1f}" stroke-linecap="round"/>')
        out.append(f'<text x="{lx + 42}" y="{ly0 + 36}" font-size="13" fill="#333">{n}</text>')
        lx += 42 + tw(str(n), 13) + 28
    out.append(f'<circle cx="{lx + 14}" cy="{ly0 + 31}" r="9" fill="{SEED_FILL}"/>')
    out.append(f'<text x="{lx + 30}" y="{ly0 + 36}" font-size="13" fill="#333">the two proteins of interest</text>')
    for i, line in enumerate(footer.split("\n")):
        out.append(f'<text x="24" y="{ly0 + 62 + 16 * i}" font-size="12" fill="#666">{E(line)}</text>')
    out.append("</svg>")
    return "".join(out)
