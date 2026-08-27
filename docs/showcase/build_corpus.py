#!/usr/bin/env python3
"""Build the corpus-explorer showcase page.

Numbers come from data/literature.db, data/fingerprints/, data/clusters.json and
the recorded session in outputs/mesothelioma_showcase.txt. The network figure is
laid out from mesothelioma_target_network.cyjs — the actual Cytoscape export that
session produced.  Rebuild:  python docs/showcase/build_corpus.py
"""
from __future__ import annotations
import json, math, pathlib

HERE = pathlib.Path(__file__).resolve().parent
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
CODEP = [("DOCK5",0.4111),("RAF1",0.3817),("TCF7L2",0.3195),("FERMT1",0.3062),
         ("RAB10",0.2864),("CTNNB1",0.2707),("MAP3K2",0.2701),("KLF3",0.2532)]

FINGERPRINT = """{
  "claim": "BI-3406 is a potent, selective inhibitor of the
            SOS1::KRAS protein-protein interaction.",
  "protein_pair": ["SOS1", "KRAS"],
  "experimental_context": "Surface plasmon resonance (SPR) Kd measurement on SOS1",
  "affinities_kd_Molar": 4.7e-07,
  "inhibitory_constant_Ki": null,
  "key_amino_acid_residues": ["Tyr884", "His905", "Met878"],
  "quantitative_or_qualitative": "quantitative",
  "is_statistically_significant": true,
  "confidence_score": 0.95,
  "source_span": "Section: Discovery of BI-3406, a potent and selective
                  SOS1::KRAS interaction inhibitor, Para 1"
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


def subgraph():
    """Real nodes/edges from the session's own Cytoscape export."""
    d = json.loads((ROOT / "mesothelioma_target_network.cyjs").read_text())["elements"]
    nd = {n["data"]["id"]: n["data"] for n in d["nodes"]}
    keep = [c for c in CANON if c in nd]
    ks = set(keep)
    edges = []
    seen = set()
    for e in d["edges"]:
        s, t = e["data"]["source"], e["data"]["target"]
        if s in ks and t in ks and (s, t) not in seen and (t, s) not in seen:
            seen.add((s, t))
            edges.append((s, t, e["data"].get("depmap_r"), e["data"].get("mentions", 1),
                          e["data"].get("tightest_kd_M")))
    used = {n for s, t, *_ in edges for n in (s, t)}
    keep = [k for k in keep if k in used]
    return keep, edges, {k: nd[k] for k in keep}


def layout(nodes, edges, W=760, H=470, iters=520):
    """Deterministic Fruchterman-Reingold. No RNG: seeded on a circle by index."""
    n = len(nodes); idx = {k: i for i, k in enumerate(nodes)}
    pos = [[W/2 + 190*math.cos(2*math.pi*i/n), H/2 + 150*math.sin(2*math.pi*i/n)]
           for i in range(n)]
    adj = [[0.0]*n for _ in range(n)]
    for s, t, *_ in edges:
        adj[idx[s]][idx[t]] = adj[idx[t]][idx[s]] = 1.0
    k = math.sqrt(W*H/n) * 0.72
    for it in range(iters):
        temp = k * (1 - it/iters) * 0.11
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i+1, n):
                dx, dy = pos[i][0]-pos[j][0], pos[i][1]-pos[j][1]
                dist = max(0.01, math.hypot(dx, dy))
                rep = k*k/dist
                ux, uy = dx/dist, dy/dist
                disp[i][0] += ux*rep; disp[i][1] += uy*rep
                disp[j][0] -= ux*rep; disp[j][1] -= uy*rep
                if adj[i][j]:
                    att = dist*dist/k
                    disp[i][0] -= ux*att; disp[i][1] -= uy*att
                    disp[j][0] += ux*att; disp[j][1] += uy*att
        for i in range(n):
            d = max(0.01, math.hypot(*disp[i]))
            pos[i][0] += disp[i][0]/d * min(d, temp)
            pos[i][1] += disp[i][1]/d * min(d, temp)
            pos[i][0] = min(W-46, max(46, pos[i][0]))
            pos[i][1] = min(H-26, max(26, pos[i][1]))
    return {k_: tuple(pos[i]) for k_, i in idx.items()}


def network_svg() -> str:
    nodes, edges, meta = subgraph()
    P = layout(nodes, edges)
    deg = {n: 0 for n in nodes}
    for s, t, *_ in edges:
        deg[s] += 1; deg[t] += 1
    es = []
    for s, t, r, m, kd in edges:
        x1, y1 = P[s]; x2, y2 = P[t]
        w = 0.9 + min(3.2, (m or 1) ** 0.5)
        if r is None:
            col, dash, lab = "var(--rule-2)", "3 3", "no DepMap pair"
        elif r < 0:
            col, dash, lab = "var(--mark-b)", "", f"DepMap r = {r:+.3f}"
        else:
            col, dash, lab = "var(--mark-a)", "", f"DepMap r = {r:+.3f}"
        op = 0.35 if r is None else min(0.95, 0.32 + abs(r) * 1.5)
        kds = f" · tightest Kd {kd:.2g} M" if kd else ""
        es.append(f'<g class="mk" tabindex="0"><title>{s} — {t}: {m} mention'
                  f'{"s" if m != 1 else ""} in the corpus, {lab}{kds}</title>'
                  f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                  f'stroke="{col}" stroke-width="{w:.1f}" opacity="{op:.2f}" '
                  f'stroke-dasharray="{dash}" stroke-linecap="round"/></g>')
    ns = []
    for n in nodes:
        x, y = P[n]
        seed = meta[n].get("is_seed")
        r = 6 + min(9, deg[n] * 0.9)
        ns.append(f'<g class="mk nd" tabindex="0"><title>{n} — {deg[n]} edges in this view, '
                  f'{meta[n].get("total_mentions", 0)} mentions in the corpus</title>'
                  f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" '
                  f'fill="{"var(--mark-a)" if seed else "var(--surface)"}" '
                  f'stroke="{"var(--mark-a)" if seed else "var(--rule-2)"}" stroke-width="2"/>'
                  f'<text x="{x:.1f}" y="{y - r - 6:.1f}" text-anchor="middle" '
                  f'class="nl{" seed" if seed else ""}">{n}</text></g>')
    return (f'<svg viewBox="0 0 760 470" role="img" class="chart net" '
            f'aria-label="Interaction network around the mesothelioma seed genes">'
            f'{"".join(es)}{"".join(ns)}</svg>'), len(nodes), len(edges)


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

NET_SVG, NET_N, NET_E = network_svg()

HTML = f"""<title>Corpus Explorer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
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
    </div>
    <pre class="json">{FINGERPRINT}</pre>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">The map it built</p>
    <h2>NF2 loss → YAP/TAZ–TEAD, drawn from the corpus itself</h2></div>
  <div class="chart-wrap">
    {NET_SVG}
    <p class="legend">
      <span><b style="background:var(--mark-a)"></b>filled node: a seed the session started from</span>
      <span><b style="background:var(--mark-a)"></b>edge: positive DepMap correlation</span>
      <span><b style="background:var(--mark-b)"></b>edge: negative correlation</span>
      <span><b style="background:var(--rule-2)"></b>dashed: co-mentioned, no DepMap pair</span>
    </p>
    <figcaption>{NET_N} genes and {NET_E} edges, drawn from the Cytoscape export this
    session produced (<code>mesothelioma_target_network.cyjs</code>, 172 nodes / 295 edges
    in full). Edge width is how often the pair is co-mentioned in the corpus; colour and
    opacity are the DepMap co-essentiality correlation. Hover any edge for its real values.</figcaption>
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
  <div class="grid2">
    <div class="chart-wrap"><h3>Corpus hubs — degree and mentions</h3>
      <div class="tw"><table><thead><tr><th>protein</th><th class="num">partners</th>
        <th class="num">mentions</th></tr></thead><tbody>{hub_rows()}</tbody></table></div>
      <figcaption>Built live from the fingerprints, not precomputed. Note YAP-1 and YAP
      appearing separately — real alias behaviour the tool reports on itself rather than
      papering over.</figcaption>
    </div>
    <div class="chart-wrap"><h3>KRAS co-essentiality — DepMap, 1,208 cell lines</h3>
      <div class="tw"><table><thead><tr><th>gene</th><th class="num">r</th></tr></thead>
        <tbody>{codep_rows()}</tbody></table></div>
      <figcaption>Nothing here is read from a paper. RAF1 at r = +0.38 is the corpus's
      own KRAS–RAF1 edge confirmed from an orthogonal direction; DOCK5 above it is not a
      pair the corpus talks about at all.</figcaption>
    </div>
  </div>
  <div class="two" style="margin-top:26px">
    <div class="tile"><span>novelty_signal("KRAS")</span><b>0.002</b>
      <p>238 mentions, 40 papers with structures, 34 prior targeting efforts, 57
      quantitative findings. The score is a saturation measure — near zero means
      "everyone is already here". It is designed to find the opposite.</p></div>
    <div>
      <p>These two graphs are kept separate on purpose. The literature graph is what has
      been <em>written down</em>: 18,432 proteins, 20,658 co-mention edges, rebuilt from the
      fingerprints on every query. The DepMap edge index is what CRISPR screens
      <em>measured</em>: 1,411 genes, 1,304 edges, each with a real correlation and sample
      size. An edge that appears in both is a much stronger claim than an edge in either.</p>
      <p>Louvain clustering over the combined weighting gives 362 co-functional modules.
      They are heavily long-tailed — 213 of them are simple pairs — which is itself an
      honest readout of how sparse well-evidenced interaction data actually is.</p>
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
