#!/usr/bin/env python3
"""Build the corpus-explorer showcase page.

Numbers come from data/literature.db, data/fingerprints/, data/clusters.json and
the recorded session in outputs/mesothelioma_showcase.txt. The network figure is
laid out from mesothelioma_target_network.cyjs — the actual Cytoscape export that
session produced.  Rebuild:  python docs/showcase/build_corpus.py
"""
from __future__ import annotations
import json, math, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))
ROOT = HERE.parent.parent
OUT = HERE / "corpus_explorer.html"

# ---------------------------------------------------------------- real data
CORPUS = dict(indexed=55689, downloaded=14307, curated=11055, fingerprints=11052,
              lit_nodes=18432, lit_edges=20658, clusters=362)
YEARS = [(2008,92),(2009,184),(2010,197),(2011,230),(2012,252),(2013,312),(2014,338),
         (2015,376),(2016,641),(2017,794),(2018,844),(2019,814),(2020,954),(2021,957),
         (2022,982),(2023,887),(2024,897),(2025,914),(2026,267)]
JOURNALS = [("Nature",1943),("Nat Commun",1592),("Cell",1008),("Nucleic Acids Res",854),
            ("Science",738),("eLife",351),("J Am Chem Soc",316),("Cell Rep",251),
            ("Sci Adv",232),("Mol Cell",211)]
HUBS = [("YAP-1",23,367),("BRD4",21,244),("OCT4",21,173),("STING",20,394),("EGFR",20,211),
        ("KRAS",17,233),("Dnmt3a",16,175),("CTCF",11,150),("TET1",11,142),("STAT3",11,119)]
# find_cocorrelated_genes("YAP1") — the page's own subject, not a stray example.
CODEP = [("ARHGEF7",0.2907),("TEAD1",0.2722),("PKN2",0.2709),("NCKAP1",0.2672),
         ("TEAD3",0.2652),("ILK",0.2608),("CRKL",0.2596),("TP53BP2",0.2565),
         ("ITGB1",0.2512),("MARK2",0.2458)]
# Verified pairs quoted in the paralog callout.
PARALOG = [("LATS1", "LATS2", 0.0437), ("NF2", "LATS2", 0.5456),
           ("YAP1", "WWTR1", 0.0202), ("NF2", "YAP1", -0.2425)]

FINGERPRINT = """{
  "claim": "The non-acetylated VGLL4-TDU domain peptide binds to
            TEAD1 with a dissociation constant of 3.1 nM.",
  "protein_pair": ["VGLL4", "TEAD1"],
  "experimental_context": "Photonic crystal nanobeam sensor assay",
  "affinities_kd_Molar": 3.1e-09,
  "inhibitory_constant_Ki": null,
  "key_amino_acid_residues": ["K225"],
  "quantitative_or_qualitative": "quantitative",
  "is_statistically_significant": true,
  "confidence_score": 0.95,
  "source_span": "Section: VGLL4 Activity Is Regulated by Its TDU
                  Domain Acetylation, Para 2"
}"""

TRACE = [
    ("call 1", "search_corpus", "query, study_category, top_k", "334 in / 163 out"),
    ("",      "search_corpus", "query, top_k", ""),
    ("call 2", "get_interactions_for", "protein", "7,452 in / 184 out"),
    ("",      "search_corpus", "query, top_k", ""),
    ("",      "get_fingerprint", "identifier", ""),
    ("call 3", "search_corpus", "query, top_k", "19,521 in / 169 out"),
    ("",      "get_interactions_for", "protein", ""),
    ("",      "cluster_for_protein", "protein", ""),
    ("call 4", "— writes the answer —", "", "23,296 in / 1,844 out"),
]

CANON = ["YAP1","WWTR1","TAZ","TEAD1","TEAD4","LATS1","LATS2","NF2","SAV1","MST1",
         "AMOTL2","VGLL4","FOSL1","JUN","RHOA","FAT1","PTK2","GPX4","EGFR","MARK2",
         "RAP2","AHR","SOX2","RUNX2","IGF1R","MED15","TCF4","TEAD2"]


def network_svg():
    """The standard LPT map — one renderer for every interaction/DepMap figure."""
    from src.network_svg import load_cyjs, render_svg, legend_items
    nodes, edges, meta = load_cyjs(ROOT / "mesothelioma_target_network.cyjs", CANON)
    svg = render_svg(nodes, edges, meta,
                     aria="Interaction network around the mesothelioma seed genes")
    legend = "".join(f'<span><b style="background:{c}"></b>{w}</span>'
                     for c, w in legend_items())
    return svg, len(nodes), len(edges), legend


def years_svg() -> str:
    W, H, PL, PB = 700, 220, 40, 30
    mx = max(v for _, v in YEARS)
    x0, x1 = YEARS[0][0], YEARS[-1][0]
    px = lambda y: PL + (y - x0) / (x1 - x0) * (W - PL - 20)
    py = lambda v: H - PB - v / mx * (H - PB - 22)
    pts = " ".join(f"{px(y):.1f},{py(v):.1f}" for y, v in YEARS)
    grid = "".join(f'<line class="grid" x1="{PL}" y1="{py(g):.1f}" x2="{W-20}" y2="{py(g):.1f}"/>'
                   f'<text class="ax" x="{PL-8}" y="{py(g)+4:.1f}" text-anchor="end">{g}</text>'
                   for g in (0, 250, 500, 750, 1000))
    dots = "".join(f'<g class="mk" tabindex="0"><title>{y}: {v} curated papers</title>'
                   f'<circle cx="{px(y):.1f}" cy="{py(v):.1f}" r="4" fill="var(--mark-a)" '
                   f'stroke="var(--surface)" stroke-width="1.5"/></g>' for y, v in YEARS)
    ticks = "".join(f'<text class="ax" x="{px(y):.1f}" y="{H-PB+18}" text-anchor="middle">{y}</text>'
                    for y in (2008, 2012, 2016, 2020, 2024, 2026))
    return (f'<svg viewBox="0 0 {W} {H}" role="img" class="chart" '
            f'aria-label="Curated papers by publication year">{grid}'
            f'<polygon points="{px(x0):.1f},{H-PB} {pts} {px(x1):.1f},{H-PB}" '
            f'fill="var(--mark-a)" opacity="0.14"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--mark-a)" stroke-width="2" '
            f'stroke-linejoin="round"/>{dots}{ticks}</svg>')


def journals_svg() -> str:
    mx = max(v for _, v in JOURNALS)
    rows, y, RH = [], 0, 26
    for name, v in JOURNALS:
        w = v / mx * 330
        rows.append(f'<g class="mk" tabindex="0"><title>{name}: {v:,} curated papers</title>'
                    f'<text class="k r" x="150" y="{y+RH/2+4}">{name}</text>'
                    f'<rect x="162" y="{y+4}" width="{w:.1f}" height="{RH-8}" rx="3" '
                    f'fill="var(--mark-a)"/>'
                    f'<text class="v" x="{162+w+9:.1f}" y="{y+RH/2+4}">{v:,}</text></g>')
        y += RH + 5
    return (f'<svg viewBox="0 -4 620 {y}" role="img" class="chart" '
            f'aria-label="Top journals by curated paper count">{"".join(rows)}</svg>')


def trace_rows() -> str:
    return "".join(
        f'<tr><td class="c">{c}</td><td class="t">{t}</td>'
        f'<td class="a">{a}</td><td class="num">{k}</td></tr>'
        for c, t, a, k in TRACE)


def hub_rows() -> str:
    return "".join(f"<tr><td class=m>{p}</td><td class=num>{d}</td><td class=num>{m}</td></tr>"
                   for p, d, m in HUBS)


def codep_rows() -> str:
    return "".join(f"<tr><td class=m>{g}</td><td class=num>{r:+.4f}</td></tr>" for g, r in CODEP)


CSS = (HERE / "campaign_pdl1.html").read_text().split("<style>")[1].split("</style>")[0]
CSS += """
.net .nl{font-family:var(--mono);font-size:10.5px;fill:var(--muted)}
.net .nl.seed{fill:var(--ink);font-weight:500}
.net .nd circle{transition:none}
.term{background:var(--sunk);border:1px solid var(--rule);border-radius:2px;
  padding:16px 18px;font-family:var(--mono);font-size:12.5px;line-height:1.7;
  overflow-x:auto;color:var(--ink-2)}
.term .p{color:var(--accent);font-weight:500}
pre.json{background:var(--sunk);border:1px solid var(--rule);border-radius:2px;
  padding:16px 18px;font-family:var(--mono);font-size:11.5px;line-height:1.6;
  overflow-x:auto;margin:0;color:var(--ink-2)}
td.c{font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap}
td.t{font-family:var(--mono);font-size:12px;color:var(--accent);white-space:nowrap}
td.a{font-family:var(--mono);font-size:11px;color:var(--muted)}
.grid2{display:grid;gap:22px}
@media(min-width:820px){.grid2{grid-template-columns:1fr 1fr}}
.tile{background:var(--surface);border:1px solid var(--rule);border-radius:2px;
  padding:18px 20px;display:grid;gap:8px;align-content:start}
.tile b{font-family:var(--display);font-weight:500;font-size:2.1rem;line-height:1;
  letter-spacing:-.02em}
.tile span{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted)}
.tile p{font-size:.88rem;color:var(--muted);margin:4px 0 0}
"""

NET_SVG, NET_N, NET_E, NET_LEGEND = network_svg()

from _common import head as _mkhead
_HEAD = _mkhead('Corpus Explorer', 'One real corpus-explorer session: eight tool calls over 11,055 curated papers turned into a cited target map, with the interaction and DepMap graphs behind it.', 'corpus_explorer.html', 'corpus')

HTML = f"""{_HEAD}
<style>{CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · corpus-explorer</p>
    <h1>Ask eleven thousand papers a question and get a target map back</h1>
  </div>
  <p class="lede">The corpus is not a search box. It is a curated store of structured
  findings — every claim carrying its own page reference — wired to an interaction graph
  and to DepMap co-essentiality. This is one real session: one question, four model calls,
  sixty-five seconds.</p>

  <div class="term"><span class="p">&gt;</span> what's the target space around mesothelioma?</div>

  <div class="stats">
    <div class="stat"><b>55,689</b><span>papers indexed</span></div>
    <div class="stat"><b>11,055</b><span>curated fingerprints</span></div>
    <div class="stat"><b>20,658</b><span>interaction edges</span></div>
    <div class="stat"><b>362</b><span>co-functional clusters</span></div>
    <div class="stat"><b>65 s</b><span>to answer</span></div>
  </div>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">What the tools did</p>
    <h2>Eight tool calls, then one answer</h2></div>
  <p>The skill is not told which tools to use. It searches, follows the hits into the
  interaction graph, pulls a full fingerprint when it needs the evidence behind a claim,
  and asks which co-functional cluster a protein belongs to — then writes. The whole
  trace is logged, so what it looked at is inspectable after the fact.</p>
  <div class="tw"><table>
    <thead><tr><th></th><th>tool</th><th>arguments</th><th class="num">tokens</th></tr></thead>
    <tbody>{trace_rows()}</tbody></table></div>
  <p style="margin-top:16px;font-size:.9rem;color:var(--muted)">50,603 input / 2,360 output
  tokens across four calls. Retrieval is budgeted and iterative on purpose — the skill is
  told to stop searching once the picture stops changing.</p>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">What one paper becomes</p>
    <h2>A finding, not a snippet</h2></div>
  <div class="two">
    <div>
      <p>Curation turns each paper into structured JSON against a fixed schema. Units are
      normalised at extraction time — affinities are floats in <strong>Molar</strong>, never
      nM or µM — organisms become NCBI taxonomy ids, and study categories come from a closed
      enum. That is what makes "find every K<sub>d</sub> tighter than 100 nM for this pair"
      a query rather than a reading exercise.</p>
      <p>The rule that matters most is the last field: <strong>every claim carries a
      <code>source_span</code></strong>. A finding that cannot say which page and paragraph
      it came from is rejected by the consumers downstream. There is no way to produce a
      confident-sounding number here without a pointer to where it was read.</p>
      <p>This particular record is why the map above has a VGLL4 node at all: a natural
      TEAD1 ligand with a measured 3.1 nM K<sub>d</sub>, curated out of
      <a href="https://doi.org/10.1016/j.devcel.2016.09.005">10.1016/j.devcel.2016.09.005</a>
      by <code>gemini-3.1-flash-lite-preview</code> in May 2026 — provenance is stored per
      file, so you can always ask which model read what.</p>
    </div>
    <pre class="json">{FINGERPRINT}</pre>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">The map it built</p>
    <h2>NF2 loss → YAP/TAZ–TEAD, drawn from the corpus itself</h2></div>
  <div class="chart-wrap">
    {NET_SVG}
    <p class="legend">{NET_LEGEND}</p>
    <figcaption>{NET_N} genes and {NET_E} edges, drawn from the Cytoscape export this
    session produced (<code>mesothelioma_target_network.cyjs</code>, 172 nodes / 295 edges
    in full). Edge width is how often the pair is co-mentioned in the corpus; colour and
    opacity are the DepMap co-essentiality correlation. Hover any edge for its real values.
    <br><br><strong>The map is a literature map.</strong> Its nodes and edges come from
    co-mention in the corpus; DepMap only <em>colours</em> edges that are already there. So
    a gene can be strongly co-essential with YAP1 and still be absent here — ARHGEF7
    (r&nbsp;=&nbsp;+0.29) is in the table further down and not on this map, because no paper
    in this corpus mentions it alongside YAP1. That is the blind spot, not a
    rendering choice.</figcaption>
  </div>
  <div class="two" style="margin-top:26px">
    <div>
      <blockquote class="quote">NF2 (Merlin) is inactivated in 30–40% of malignant pleural
      mesothelioma cases. When NF2 is lost, it can no longer relay contact-inhibition
      signals through the Hippo kinase cascade, so YAP1 and TAZ accumulate in the nucleus
      and drive a TEAD1-4-dependent pro-proliferative transcriptional program
      <cite>— corpus-explorer, citing 10.1111/jcmm.18330</cite></blockquote>
      <p>Every claim in the written answer carries its DOI inline. That is a hard
      requirement of the skill, not a stylistic preference: an answer with an uncited
      number is a failed answer.</p>
    </div>
    <div>
      <div class="note-box"><h3>The finding that needed both sources</h3>
      <p>LATS1 and LATS2 are essentially <em>not</em> co-essential with each other
      (r = +0.04) — the textbook signature of paralog buffering. The co-essentiality only
      appears against the upstream node: NF2–LATS2 reaches r = +0.55. Literature alone
      would have called them one module; DepMap alone would have called them unrelated.</p></div>
      <p style="margin-top:16px;font-size:.92rem">The session closed by exporting the whole
      neighbourhood — 172 nodes, 295 edges, DepMap r on every edge that has one — as a
      Cytoscape file, so the map outlives the conversation.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Two signals, deliberately not merged</p>
    <h2>What the literature says, and what the cells say</h2></div>
  <p>Both tables below are about this page's own subject. The left one is
  corpus-wide — the most connected proteins in the <em>whole</em> 11,055-paper store,
  which is where you see what this corpus is actually made of. The right one is
  co-essentiality for YAP1 specifically, computed from CRISPR screens and not from any
  paper at all.</p>
  <div class="grid2">
    <div class="chart-wrap"><h3>Corpus hubs — the whole corpus, not this query</h3>
      <div class="tw"><table><thead><tr><th>protein</th><th class="num">partners</th>
        <th class="num">mentions</th></tr></thead><tbody>{hub_rows()}</tbody></table></div>
      <figcaption>Built live from the fingerprints. YAP-1 tops the list because of what
      this lab reads, not because it is the most connected protein in biology — which is
      the honest reason the mesothelioma question landed in such well-covered territory.
      Note YAP-1 and YAP appearing separately: real alias behaviour the tool reports on
      itself rather than papering over.</figcaption>
    </div>
    <div class="chart-wrap"><h3>YAP1 co-essentiality — DepMap, 1,208 cell lines</h3>
      <div class="tw"><table><thead><tr><th>gene</th><th class="num">r</th></tr></thead>
        <tbody>{codep_rows()}</tbody></table></div>
      <figcaption>Nothing here is read from a paper. TEAD1 and TEAD3 surfacing on their
      own is the corpus's central claim confirmed from an orthogonal direction; ARHGEF7,
      ILK, CRKL and ITGB1 above and around them are adhesion and cytoskeletal genes the
      corpus does not connect to YAP1 at all.</figcaption>
    </div>
  </div>
  <div class="two" style="margin-top:26px">
    <div>
      <div class="note-box"><h3>The finding that needed both sources</h3>
      <p>LATS1 and LATS2 are essentially <em>not</em> co-essential with each other
      (r&nbsp;=&nbsp;+0.04) — the signature of paralog buffering. The signal only appears
      against the upstream node: NF2–LATS2 reaches <strong>r&nbsp;=&nbsp;+0.55</strong>.
      The same pattern holds for the pair the pathway stage flagged as the escape risk:
      <strong>YAP1–WWTR1 (TAZ) is r&nbsp;=&nbsp;+0.02</strong> — knocking out one does not
      substitute for the other, which is exactly why a TAZ-blind binder can be bypassed.
      And NF2–YAP1 is <strong>negative</strong>, r&nbsp;=&nbsp;−0.24: lose the brake, gain
      the dependency.</p>
      <p>Literature alone would have called LATS1/2 one module. DepMap alone would have
      called them unrelated. Neither source answers this on its own.</p></div>
    </div>
    <div>
      <div class="tile"><span>novelty_signal</span><b>0.10</b>
        <p>YAP1: 370 mentions, 99 prior targeting efforts, 16 quantitative findings. The
        score is a <em>saturation</em> measure — near zero means everyone is already here.
        NF2 scores 0.51 on the same scale, off 100 mentions and a single targeting
        effort.</p></div>
      <p style="margin-top:16px;font-size:.92rem">That contrast is a prompt, not a
      recommendation. NF2 looks unexplored because it is a tumour suppressor that is
      <em>lost</em> in these tumours — there is nothing there to bind. A high novelty score
      says the literature is thin, and nothing about whether a target is tractable.</p>
      <p style="font-size:.92rem">The two graphs stay separate on purpose. The literature
      graph is what has been <em>written down</em>: 18,432 proteins, 20,658 co-mention
      edges, rebuilt from the fingerprints on every query. The DepMap index is what CRISPR
      screens <em>measured</em>: 1,411 genes, 1,304 edges, each with a real correlation and
      sample size. An edge in both is a much stronger claim than an edge in either.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">What is in it</p>
    <h2>One lab's reading list, and it says so</h2></div>
  <div class="chart-wrap"><h3>Curated papers by publication year</h3>{years_svg()}
    <figcaption>11,055 curated fingerprints. The plateau from 2020 is the corpus being
    actively maintained rather than harvested once; 2026 is a partial year.</figcaption></div>
  <div class="two" style="margin-top:24px">
    <div class="chart-wrap"><h3>Top journals by curated paper count</h3>{journals_svg()}</div>
    <div>
      <p>Search indexes every hit, but only tier 1 and 2 journals are downloaded and
      curated — 32% of what the searches find. That gate is the single most consequential
      knob in the whole literature track, and it fails silently in one specific way: journal
      matching is <em>exact</em> against a normalised name, so a journal spelled a way the
      list does not contain is a 100% exclusion with no warning.</p>
      <div class="note-box"><p>You can see that failure mode in the table beside this
      paragraph. <code>Nat Commun</code> and <code>Nature Communications</code> are the same
      journal, counted separately, and have been merged by hand here. The pipeline does not
      merge them for you — which is exactly why the behaviour is documented rather than
      hidden.</p></div>
      <p>This corpus is weighted toward chromatin biology, histone chaperones and
      structural/chemical biology. It is one lab's reading list. Ask it a question outside
      that space and the honest answer is that it does not know — which is why the skill is
      required to end every answer with what the corpus does <em>not</em> say, and why the
      MCP servers are configured never to reach for these tools on their own.</p>
    </div>
  </div>
</section>

<footer>
  <p>Session transcript: <code>outputs/mesothelioma_showcase.txt</code>. Corpus counts read
  from <code>data/literature.db</code> and <code>data/fingerprints/</code>; graph figures
  from <code>data/clusters.json</code>, <code>data/depmap_edges.parquet</code> and the
  session's own <code>mesothelioma_target_network.cyjs</code>. Rebuild this page with
  <code>python docs/showcase/build_corpus.py</code>.</p>
</footer>

</div>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB) — network {NET_N} nodes / {NET_E} edges")
