#!/usr/bin/env python3
"""Render publication network figures for the protein pairs the wildcard skill forwarded.

    python scripts/render_graph_figures.py            # all pairs
    python scripts/render_graph_figures.py shoc2_kras

Writes docs/figures/graphs/<name>.{svg,pdf,png}. The pairs are the corpus-derived
hypotheses that passed every gate in the wildcard v2 evaluation (outputs/phase2/
*__wildcard2.gates.json). The skill saw the graph TOOLS' output (paths, co-dependency)
in those runs, never a drawing, and the pairs were not chosen because of any graph.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from render_fingerprint_pdf import _find_chrome  # noqa: E402
from src import network_figure as NF  # noqa: E402
from src._corpus_graph import export_subgraph  # noqa: E402

OUT = _ROOT / "docs" / "figures" / "graphs"
W, H = 960, 724
PAIRS = {   # name: (seed A, seed B, title, which wildcard runs forwarded it)
    "shoc2_kras": ("SHOC2", "KRAS", "SHOC2 and KRAS", "forwarded in 2 of 2 KRAS runs"),
    "spsb3_cgas": ("SPSB3", "CGAS", "SPSB3 and CGAS (cGAS)", "forwarded in 1 of 2 STING runs"),
    "menin_kmt2a": ("MEN1", "KMT2A", "MEN1 (menin) and KMT2A", "forwarded in 1 of 2 epigenetics runs"),
}


def build(name: str, tmp: Path) -> list[Path]:
    a, b, title, why = PAIRS[name]
    cy = tmp / f"{name}.cyjs"
    r = export_subgraph([a, b], _ROOT / "data" / "fingerprints", str(cy), depth=1, max_nodes=600, with_depmap=True)
    edges, meta = NF.collapse(cy, [a, b])
    sel = NF.select(edges, (a, b))
    pair = edges.get(tuple(sorted((a, b))))
    n_pair = pair.papers if pair else 0
    sub = "Corpus co-mention network with DepMap CRISPR co-dependency"
    n_dep = pair.depmap_n if pair and pair.depmap_n else 1208
    foot = (f"Gene-level view; spelling variants merged. Edge width counts distinct papers. Drawn: edges between a protein of interest and its\n"
            f"strongest partners ({sel['n_candidates']} partners in the corpus); edges among partners are omitted. "
            f"DepMap r: Pearson correlation of CRISPR gene\n"
            f"effect over {n_dep:,} cell lines. Kd, Ki: tightest value reported for the wild-type protein"
            f"{'; * = reported only for a mutant form' if NF.uses_mutant(sel, (a, b)) else ''}.\n"
            f"This pair was {why} in the wildcard evaluation.")
    svg = NF.render(sel, (a, b), title, sub, foot, W, H)
    OUT.mkdir(parents=True, exist_ok=True)
    sv = OUT / f"{name}.svg"
    sv.write_text(svg, encoding="utf-8")
    page = tmp / f"{name}.html"
    page.write_text(f'<!doctype html><meta charset="utf-8"><style>@page{{size:{W}px {H}px;margin:0}}'
                    f'html,body{{margin:0;background:#fff}}svg{{display:block}}</style>{svg}', encoding="utf-8")
    pdf, png = OUT / f"{name}.pdf", OUT / f"{name}.png"
    base = [_find_chrome(), "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars"]
    for p in (pdf, png):
        p.unlink(missing_ok=True)
    subprocess.run(base + ["--no-pdf-header-footer", f"--print-to-pdf={pdf}", page.as_uri()], capture_output=True)
    subprocess.run(base + [f"--screenshot={png}", f"--window-size={W},{H}", "--force-device-scale-factor=3",
                           page.as_uri()], capture_output=True)
    print(f"{name}: {len(sel['nodes'])} nodes, {len(sel['edges'])} edges, pair papers {n_pair}")
    return [sv, pdf, png]


def main() -> None:
    names = sys.argv[1:] or list(PAIRS)
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        for n in names:
            build(n, Path(t))


if __name__ == "__main__":
    main()
