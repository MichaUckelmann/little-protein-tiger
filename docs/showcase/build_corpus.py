#!/usr/bin/env python3
"""Build the corpus-explorer showcase page.

Every corpus statistic on this page is EXTRACTED here, never typed: paper and
curation counts out of `data/literature.db`, fingerprints off disk, the
literature graph through the same `src._corpus_graph._get_graph` the MCP tools
use, the DepMap index out of `data/depmap_edges.parquet`, and the session's
trace, quotes and token counts out of the recorded transcript. See `_facts.py`
for why that matters — this page's whole argument is provenance, and it used to
carry a footer claiming counts were "read from data/literature.db" beside
figures that were stale by ~25%.

The tracked snapshot in `facts/corpus_explorer.json` lets the page rebuild on a
machine without the corpus (`data/` is gitignored); a missing source raises
`SourceMissing`, never a half-extracted page.

    python docs/showcase/build_corpus.py     # -> corpus_explorer.html
"""
from __future__ import annotations

import html
import json
import math
import pathlib
import re
import sqlite3
import sys
import textwrap

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import _facts                                            # noqa: E402
from _common import head as _mkhead                      # noqa: E402

OUT = HERE / "corpus_explorer.html"

DB = ROOT / "data/literature.db"
FPDIR = ROOT / "data/fingerprints"
CLUSTERS = ROOT / "data/clusters.json"
DEPMAP_EDGES = ROOT / "data/depmap_edges.parquet"
TRANSCRIPT = ROOT / "outputs/mesothelioma_showcase.txt"
CYJS = ROOT / "mesothelioma_target_network.cyjs"
# The record the VGLL4 node on the map comes from.
FINGERPRINT_FILE = FPDIR / "doi_10.1016_j.devcel.2016.09.005.json"

# The ONLY name merge, kept from the previous hand-merged chart and now done in
# the extractor so the page can say what it did. Abbreviated/full spellings of
# the same journal are counted separately by the pipeline — that is the failure
# mode the page documents, not something to quietly fix here.
JOURNAL_MERGES = {"Nature Communications": "Nat Commun"}
TOP_JOURNALS = 10
TOP_HUBS = 10
TOP_CODEP = 10
YEAR_FLOOR = 2005          # below this the corpus is a scattering of classics
NOVELTY_PROTEINS = ["YAP1", "NF2"]
CODEP_PAIRS = [("LATS1", "LATS2"), ("NF2", "LATS2"),
               ("YAP1", "WWTR1"), ("NF2", "YAP1")]
# Node whitelist for the map: the genes this session actually discussed.
CANON = ["YAP1", "WWTR1", "TAZ", "TEAD1", "TEAD4", "LATS1", "LATS2", "NF2",
         "SAV1", "MST1", "AMOTL2", "VGLL4", "FOSL1", "JUN", "RHOA", "FAT1",
         "PTK2", "GPX4", "EGFR", "MARK2", "RAP2", "AHR", "SOX2", "RUNX2",
         "IGF1R", "MED15", "TCF4", "TEAD2"]
FINGERPRINT_KEYS = ["claim", "protein_pair", "experimental_context",
                    "affinities_kd_Molar", "inhibitory_constant_Ki",
                    "key_amino_acid_residues", "quantitative_or_qualitative",
                    "is_statistically_significant", "confidence_score",
                    "source_span"]


# --------------------------------------------------------------- extraction
def _require(path: pathlib.Path) -> pathlib.Path:
    """A non-text source (sqlite, parquet, a directory) — present, or SourceMissing."""
    if not path.exists():
        raise _facts.SourceMissing(str(path))
    return path


def _needs(module: str):
    """Import an optional dependency of the `corpus` extra, or report it absent."""
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:                      # noqa: PERF203 - explicit
        raise _facts.SourceMissing(f"python module {module!r} ({exc})") from exc


def _session(text: str) -> dict:
    """The recorded run: LLM calls, their token counts, the tools each triggered.

    SCOPED TO THE FIRST SESSION. The transcript holds two consecutive
    corpus-explorer runs against the same corpus, each with its own `call #1..4`
    and its own "Run complete" total. The page tells the story of ONE question,
    so mixing them is wrong in both directions: taking all the timestamps counts
    the four and a half minutes the human spent reading the first answer and
    typing the second, and taking all the `call #` lines double-counts the tool
    trace against a token total that only covers the first run.
    """
    end = text.find("Run complete")
    if end != -1:
        text = text[:text.index("\n", end) + 1] if "\n" in text[end:] else text

    calls: list[dict] = []
    for line in text.splitlines():
        if m := re.search(r"\[\w+\] call #(\d+)", line):
            calls.append({"n": int(m.group(1)), "tools": [], "tokens": ""})
        elif m := re.search(r"tokens: ([\d,]+) in / ([\d,]+) out", line):
            if calls:
                calls[-1]["tokens"] = f"{m.group(1)} in / {m.group(2)} out"
        elif m := re.search(r"→ (\w+)\(\[(.*?)\]\)", line):
            if calls:
                args = ", ".join(re.findall(r"'([^']+)'", m.group(2)))
                calls[-1]["tools"].append([m.group(1), args])
    if not calls:
        raise _facts.SourceMissing(f"{TRANSCRIPT} has no recorded LLM calls")

    total = re.search(r"total tokens: ([\d,]+) in / ([\d,]+) out across (\d+)", text)
    stamps = re.findall(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)", text, re.M)
    to_s = lambda s: (int(s[11:13]) * 3600 + int(s[14:16]) * 60 + float(s[17:]))
    return {
        "calls": [[c["n"], c["tokens"], c["tools"]] for c in calls],
        "n_tool_calls": sum(len(c["tools"]) for c in calls),
        "tokens_in": total.group(1), "tokens_out": total.group(2),
        "n_llm_calls": int(total.group(3)),
        "seconds": round(to_s(stamps[-1]) - to_s(stamps[0]), 1),
    }


def _answer_block(text: str, heading: str) -> list[str]:
    """The bullets under one `## heading` of the written answer, verbatim."""
    body = text.split(f"## {heading}", 1)[1].split("\n---", 1)[0]
    return [b.strip() for b in re.findall(r"^- (.+)$", body, re.M)]


# Coarse topic proxies over curated titles. Not a taxonomy and not disjoint —
# a structural paper about a chromatin complex counts in both — so these are
# reported as approximate shares, never summed.
FIELD_TERMS = [
    ("chromatin and epigenetics", ("chromatin", "histone", "nucleosome", "epigen", "methylat")),
    ("signalling", ("kinase", "signaling", "signalling", "receptor", "phosphoryl")),
    ("cancer biology", ("cancer", "tumor", "tumour", "oncogen", "carcinom", "metasta")),
    ("development and stem cells", ("embryo", "stem cell", "differentiat", "developmental", "organoid")),
    ("immunology", ("immun", "T cell", "macrophage", "inflammat", "antibod")),
    ("metabolism", ("metabol", "mitochondri", "glycoly", "lipid", "autophag")),
    ("neuroscience and pain", ("neuron", "neural", "synap", "brain", "pain", "nocicept", "migraine")),
    ("structural biology", ("cryo-EM", "crystal structure", "structural basis", "X-ray", "NMR")),
    ("protein design and folding", ("protein design", "de novo", "folding", "AlphaFold", "binder")),
    ("cell cycle and DNA repair", ("cell cycle", "mitosis", "DNA repair", "replication fork", "checkpoint")),
    # The thin end, measured so it can be named rather than guessed at. These
    # are the fields `scripts/ablate_corpus.py` uses as its low-coverage arm.
    ("cardiac", ("cardiac", "cardiomyo", "heart failure")),
    ("virology", ("virus", "viral", "SARS-CoV")),
    ("antibacterial", ("antibiotic", "antimicrobial", "bacterial resistance")),
    ("kidney", ("kidney", "renal", "nephro")),
    ("fibrosis", ("fibrosis", "fibrotic", "myofibroblast")),
]


def extract() -> dict:
    """Every figure on the page, read from the live corpus. Raises SourceMissing."""
    pq = _needs("pyarrow.parquet")
    graph = _needs("src._corpus_graph")
    normalizer = _needs("src.identifier_normalizer")
    netsvg = _needs("src.network_svg")

    con = sqlite3.connect(f"file:{_require(DB)}?mode=ro", uri=True)
    one = lambda q: con.execute(q).fetchone()[0]
    indexed = one("SELECT COUNT(*) FROM papers")
    downloaded = one("SELECT COUNT(*) FROM papers WHERE download_status='downloaded'")
    curated = one("SELECT COUNT(*) FROM papers WHERE curation_status='completed'")

    year_rows = con.execute(
        "SELECT year, COUNT(*) FROM papers WHERE curation_status='completed' "
        "AND year IS NOT NULL GROUP BY year ORDER BY year").fetchall()
    years = [[int(y), n] for y, n in year_rows if int(y) >= YEAR_FLOOR]
    years_before = sum(n for y, n in year_rows if int(y) < YEAR_FLOOR)

    # What the corpus is ABOUT, measured rather than asserted. "Weighted to
    # chromatin" is the honest headline, but the size of that weighting matters:
    # chromatin is the largest single slice and still only about a sixth, so
    # calling the whole thing a chromatin corpus (or "one lab's reading list")
    # understates it. Title matching is a coarse proxy and deliberately so — it
    # is reported as approximate everywhere it is used.
    coverage = []
    for label, terms in FIELD_TERMS:
        cond = " OR ".join(["lower(title) LIKE ?"] * len(terms))
        args = [f"%{t.lower()}%" for t in terms]
        n = con.execute(
            f"SELECT COUNT(*) FROM papers WHERE curation_status='completed' "
            f"AND ({cond})", args).fetchone()[0]
        coverage.append([label, n, round(n / curated * 100, 1)])
    coverage.sort(key=lambda r: -r[1])

    tally: dict[str, int] = {}
    for name, n in con.execute(
            "SELECT journal, COUNT(*) FROM papers WHERE curation_status='completed' "
            "AND journal IS NOT NULL AND journal != '' GROUP BY journal"):
        tally[JOURNAL_MERGES.get(name, name)] = tally.get(
            JOURNAL_MERGES.get(name, name), 0) + n
    journals = sorted(tally.items(), key=lambda kv: -kv[1])[:TOP_JOURNALS]
    merged = [[src, dst, tally[dst]] for src, dst in JOURNAL_MERGES.items()]
    con.close()

    fingerprints = len(list(_require(FPDIR).glob("*.json")))
    clusters = json.loads(_facts.read(CLUSTERS))

    g = graph._get_graph(FPDIR)
    hubs = [[h["protein"], h["degree"], h["total_mentions"]]
            for h in graph.interaction_hubs(FPDIR, top_n=TOP_HUBS)["hubs"]]

    codep = graph.find_cocorrelated_genes("YAP1")
    if not codep.get("available"):
        raise _facts.SourceMissing(f"DepMap unavailable: {codep.get('reason')}")
    pairs = []
    for a, b in CODEP_PAIRS:
        r = graph.get_genetic_codependency(a, b, FPDIR)
        if r.get("available"):
            pairs.append([a, b, round(r["r"], 4)])

    novelty = []
    for p in NOVELTY_PROTEINS:
        n = graph.novelty_signal(p, FPDIR)
        novelty.append([p, n["novelty_score"], n["counts"]["mentions"],
                        n["counts"]["prior_targeting"],
                        n["counts"]["quantitative_findings"]])

    edges = pq.read_table(_require(DEPMAP_EDGES))
    src_sym = edges.column("source_symbol").to_pylist()
    dst_sym = edges.column("target_symbol").to_pylist()

    stats = normalizer.get_normalizer().resolution_stats()
    nap1 = normalizer.get_normalizer().resolve("NAP1")

    fp = json.loads(_facts.read(_require(FINGERPRINT_FILE)))
    finding = next(f for f in fp["key_findings"] if f.get("affinities_kd_Molar"))

    nodes, net_edges, meta = netsvg.load_cyjs(_require(CYJS), CANON)
    full = json.loads(_facts.read(CYJS))["elements"]

    transcript = _facts.read(TRANSCRIPT)
    quote = next(p for p in transcript.split("\n\n") if "NF2 (Merlin)" in p)
    quote = quote[quote.index("**NF2 (Merlin)**"):]

    return {
        "indexed": indexed, "downloaded": downloaded, "curated": curated,
        "fingerprints": fingerprints,
        "clusters": clusters["cluster_count"],
        "lit_nodes": g.number_of_nodes(), "lit_edges": g.number_of_edges(),
        "depmap_genes": len(set(src_sym) | set(dst_sym)),
        "depmap_edges": edges.num_rows,
        "depmap_cell_lines": codep["results"][0]["n"],
        "years": years, "years_before": years_before, "year_floor": YEAR_FLOOR,
        "journals": [[k, v] for k, v in journals], "journal_merges": merged,
        "coverage": coverage,
        "hubs": hubs,
        "codep": [[r["gene"], r["r"]] for r in codep["results"][:TOP_CODEP]],
        "codep_pairs": pairs,
        "novelty": novelty,
        "resolver_symbols": stats["approved_symbols"],
        "resolver_dropped": stats["synonyms_dropped"],
        "resolver_nap1": nap1.filtered_reason or "resolved",
        "fingerprint_fields": [[k, finding[k]] for k in FINGERPRINT_KEYS],
        "fingerprint_doi": fp["paper_metadata"]["doi"],
        "fingerprint_model": fp["curation_metadata"]["model"],
        "fingerprint_month": fp["curation_metadata"]["curated_at"][:7],
        "net_svg": netsvg.render_svg(
            nodes, net_edges, meta,
            aria="Interaction network around the mesothelioma seed genes"),
        "net_legend": [[c, w] for c, w in netsvg.legend_items()],
        "net_nodes": len(nodes), "net_edges": len(net_edges),
        "net_full_nodes": len(full["nodes"]), "net_full_edges": len(full["edges"]),
        "session": _session(transcript),
        "not_say": _answer_block(transcript, "What the corpus does NOT say"),
        "uncited": transcript.count("[uncited]"),
        "uncited_example": next(
            b for b in _answer_block(transcript, "Tumour microenvironment targets")
            if "[uncited]" in b),
        "quote": quote.strip(),
    }


F = _facts.load("corpus_explorer", extract)

# ------------------------------------------------------------------ shortcuts
S = F["session"]
NOVELTY = {n[0]: n[1:] for n in F["novelty"]}
PAIRS = {(a, b): r for a, b, r in F["codep_pairs"]}
GATE_PCT = 100 * F["downloaded"] / F["indexed"]
NAT_COMMUN = F["journal_merges"][0] if F["journal_merges"] else None


# -------------------------------------------------------------- text helpers
def md(text: str) -> str:
    """The answer's own markdown, inline only — this page quotes it verbatim."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[uncited\]", '<code>[uncited]</code>', out)
    out = re.sub(r"\((10\.[\w./\-]+?)\)",
                 r'(<a href="https://doi.org/\1">\1</a>)', out)
    return out


def json_block(fields) -> str:
    """The fingerprint record, laid out the way a curator's output actually reads."""
    lines = ["{"]
    for i, (key, value) in enumerate(fields):
        head = f'  "{key}": '
        if isinstance(value, str):
            body = textwrap.wrap(value, 58) or [""]
            pad = " " * (len(head) + 1)
            body[0] = f'"{body[0]}'
            body[-1] = f'{body[-1]}"'
            rendered = ("\n" + pad).join(body)
        else:
            rendered = json.dumps(value)
        lines.append(head + rendered + ("," if i < len(fields) - 1 else ""))
    lines.append("}")
    return html.escape("\n".join(lines))


# ------------------------------------------------------------------- charts
def _ticks(mx: int, n: int = 5) -> list[int]:
    mag = 10 ** math.floor(math.log10(max(mx / n, 1)))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= mx / n)
    return [int(step * i) for i in range(n + 1)]


def years_svg() -> str:
    ys = F["years"]
    W, H, PL, PB = 700, 220, 44, 30
    grid_vals = _ticks(max(v for _, v in ys))
    mx = grid_vals[-1]
    x0, x1 = ys[0][0], ys[-1][0]
    px = lambda y: PL + (y - x0) / (x1 - x0) * (W - PL - 20)
    py = lambda v: H - PB - v / mx * (H - PB - 22)
    pts = " ".join(f"{px(y):.1f},{py(v):.1f}" for y, v in ys)
    grid = "".join(
        f'<line class="grid" x1="{PL}" y1="{py(g):.1f}" x2="{W-20}" y2="{py(g):.1f}"/>'
        f'<text class="ax" x="{PL-8}" y="{py(g)+4:.1f}" text-anchor="end">{g:,}</text>'
        for g in grid_vals)
    dots = "".join(
        f'<g class="mk" tabindex="0"><title>{y}: {v:,} curated papers</title>'
        f'<circle cx="{px(y):.1f}" cy="{py(v):.1f}" r="4" fill="var(--mark-a)" '
        f'stroke="var(--surface)" stroke-width="1.5"/></g>' for y, v in ys)
    labelled = [y for y in range(x0, x1 + 1, 5) if x1 - y > 2] + [x1]
    ticks = "".join(
        f'<text class="ax" x="{px(y):.1f}" y="{H-PB+18}" text-anchor="middle">{y}</text>'
        for y in labelled)
    return (f'<svg viewBox="0 0 {W} {H}" role="img" class="chart" '
            f'aria-label="Curated papers by publication year">{grid}'
            f'<polygon points="{px(x0):.1f},{H-PB} {pts} {px(x1):.1f},{H-PB}" '
            f'fill="var(--mark-a)" opacity="0.14"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--mark-a)" '
            f'stroke-width="2" stroke-linejoin="round"/>{dots}{ticks}</svg>')


def journals_svg() -> str:
    js = F["journals"]
    mx = max(v for _, v in js)
    rows, y, RH = [], 0, 26
    for name, v in js:
        w = v / mx * 330
        rows.append(
            f'<g class="mk" tabindex="0"><title>{html.escape(name)}: {v:,} curated '
            f'papers</title><text class="k r" x="150" y="{y+RH/2+4}">'
            f'{html.escape(name)}</text>'
            f'<rect x="162" y="{y+4}" width="{w:.1f}" height="{RH-8}" rx="3" '
            f'fill="var(--mark-a)"/>'
            f'<text class="v" x="{162+w+9:.1f}" y="{y+RH/2+4}">{v:,}</text></g>')
        y += RH + 5
    return (f'<svg viewBox="0 -4 620 {y}" role="img" class="chart" '
            f'aria-label="Top journals by curated paper count">{"".join(rows)}</svg>')


# -------------------------------------------------------------------- tables
def trace_rows() -> str:
    out = []
    for n, tokens, tools in S["calls"]:
        rows = tools or [["— writes the answer —", ""]]
        for i, (tool, args) in enumerate(rows):
            out.append(
                f'<tr><td class="c">{"call " + str(n) if i == 0 else ""}</td>'
                f'<td class="t">{html.escape(tool)}</td>'
                f'<td class="a">{html.escape(args)}</td>'
                f'<td class="num">{tokens if i == 0 else ""}</td></tr>')
    return "".join(out)


def hub_rows() -> str:
    return "".join(f"<tr><td class=m>{html.escape(p)}</td><td class=num>{d}</td>"
                   f"<td class=num>{m:,}</td></tr>" for p, d, m in F["hubs"])


def codep_rows() -> str:
    return "".join(f"<tr><td class=m>{g}</td><td class=num>{r:+.4f}</td></tr>"
                   for g, r in F["codep"])


def not_say_items() -> str:
    return "".join(f"<li>{md(b)}</li>" for b in F["not_say"])


NET_LEGEND = "".join(f'<span><b style="background:{c}"></b>{w}</span>'
                     for c, w in F["net_legend"])

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
ul.gaps{margin:0;padding-left:1.1em;display:grid;gap:10px;max-width:74ch}
ul.gaps li{font-size:.95rem;line-height:1.6;color:var(--ink-2)}
"""

_HEAD = _mkhead(
    'Corpus Explorer',
    f'One real corpus-explorer session: {S["n_tool_calls"]} tool calls over '
    f'{F["curated"]:,} curated papers turned into a cited target map, with the '
    f'interaction and DepMap graphs behind it.',
    'corpus_explorer.html', 'corpus')

HTML = f"""{_HEAD}
<style>{CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · corpus-explorer</p>
    <h1>Ask {F["curated"]:,} papers a question and get a target map back</h1>
  </div>
  <p class="lede">The corpus is not a search box. It is a curated store of structured
  findings — every claim carrying its own page reference — wired to an interaction graph
  and to DepMap co-essentiality. It is also small and lopsided: {F["curated"]:,} papers
  weighted toward chromatin, histone chaperones and structural biology, and silent on
  most of biology. This is one real session against it: one question,
  {S["n_llm_calls"]} model calls, {S["seconds"]:.0f} seconds.</p>

  <div class="term"><span class="p">&gt;</span> what's the target space around mesothelioma?</div>

  <div class="stats">
    <div class="stat"><b>{F["indexed"]:,}</b><span>papers indexed</span></div>
    <div class="stat"><b>{F["fingerprints"]:,}</b><span>curated fingerprints</span></div>
    <div class="stat"><b>{F["lit_edges"]:,}</b><span>interaction edges</span></div>
    <div class="stat"><b>{F["clusters"]}</b><span>co-functional clusters</span></div>
    <div class="stat"><b>{S["seconds"]:.0f} s</b><span>to answer</span></div>
  </div>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">What the tools did</p>
    <h2>{S["n_tool_calls"]} tool calls, then one answer</h2></div>
  <p>The skill is not told which tools to use. It searches, follows the hits into the
  interaction graph, pulls a full fingerprint when it needs the evidence behind a claim,
  and asks which co-functional cluster a protein belongs to — then writes.</p>
  <div class="tw"><table>
    <thead><tr><th></th><th>tool</th><th>arguments</th><th class="num">tokens</th></tr></thead>
    <tbody>{trace_rows()}</tbody></table></div>
  <p style="margin-top:16px;font-size:.9rem;color:var(--muted)">{S["tokens_in"]} input /
  {S["tokens_out"]} output tokens across {S["n_llm_calls"]} calls. Retrieval is budgeted
  and iterative on purpose — the skill is told to stop searching once the picture stops
  changing.</p>
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
      <p>This particular record is why the map below has a VGLL4 node at all: a natural
      TEAD1 ligand with a measured 3.1 nM K<sub>d</sub>, curated out of
      <a href="https://doi.org/{F["fingerprint_doi"]}">{F["fingerprint_doi"]}</a>
      by <code>{F["fingerprint_model"]}</code> in {F["fingerprint_month"]} — provenance is
      stored per file, so you can always ask which model read what.</p>
    </div>
    <pre class="json">{json_block(F["fingerprint_fields"])}</pre>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">The map it built</p>
    <h2>NF2 loss → YAP/TAZ–TEAD, drawn from the corpus itself</h2></div>
  <div class="chart-wrap">
    {F["net_svg"]}
    <p class="legend">{NET_LEGEND}</p>
    <figcaption>{F["net_nodes"]} genes and {F["net_edges"]} edges, drawn from the Cytoscape
    export this session produced (<code>mesothelioma_target_network.cyjs</code>,
    {F["net_full_nodes"]} nodes / {F["net_full_edges"]} edges in full). Edge width is how
    often the pair is co-mentioned in the corpus; colour and opacity are the DepMap
    co-essentiality correlation. Hover any edge for its real values.
    <br><br><strong>The map is a literature map.</strong> Its nodes and edges come from
    co-mention in the corpus; DepMap only <em>colours</em> edges that are already there. So
    a gene can be strongly co-essential with YAP1 and still be absent here — ARHGEF7
    (r&nbsp;=&nbsp;{F["codep"][0][1]:+.2f}) is in the table further down and not on this
    map, because no paper in this corpus mentions it alongside YAP1. That is the blind
    spot, not a rendering choice.</figcaption>
  </div>
  <div class="two" style="margin-top:26px">
    <div>
      <blockquote class="quote">{md(F["quote"])}
      <cite>— corpus-explorer, verbatim from the session transcript</cite></blockquote>
      <p>Every claim in the written answer is either cited or explicitly marked
      <code>[uncited]</code>. The skill labels its own unsupported statements rather than
      letting them blend in with the sourced ones — this answer carries
      {F["uncited"]} such marks, every one of them a claim the model knew from
      training and could not find in the corpus.</p>
    </div>
    <div>
      <div class="note-box"><h3>What an uncited claim looks like</h3>
      <p>{md(F["uncited_example"])}</p>
      <p style="margin-bottom:0">The DOI-bearing half of that sentence came from a paper.
      The <code>[uncited]</code> half is background the reader now knows to check.</p></div>
      <p style="margin-top:16px;font-size:.92rem">The session closed by exporting the whole
      neighbourhood — {F["net_full_nodes"]} nodes, {F["net_full_edges"]} edges, DepMap r on
      every edge that has one — as a Cytoscape file, so the map outlives the
      conversation.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Two signals, deliberately not merged</p>
    <h2>What the literature says, and what the cells say</h2></div>
  <p>Both tables below are about this page's own subject. The left one is
  corpus-wide — the most connected proteins in the <em>whole</em> {F["curated"]:,}-paper
  store, which is where you see what this corpus is actually made of. The right one is
  co-essentiality for YAP1 specifically, computed from CRISPR screens and not from any
  paper at all.</p>
  <div class="grid2">
    <div class="chart-wrap"><h3>Corpus hubs — the whole corpus, not this query</h3>
      <div class="tw"><table><thead><tr><th>protein</th><th class="num">partners</th>
        <th class="num">mentions</th></tr></thead><tbody>{hub_rows()}</tbody></table></div>
      <figcaption>Built live from the fingerprints. {F["hubs"][0][0]} tops the list because
      of what this lab reads, not because it is the most connected protein in biology —
      which is the honest reason the mesothelioma question landed in such well-covered
      territory. {F["hubs"][0][0]} and YAP appear as separate rows: the resolver merges
      only names it can resolve without guessing, and that restraint is the point — see
      below.</figcaption>
    </div>
    <div class="chart-wrap"><h3>YAP1 co-essentiality — DepMap, {F["depmap_cell_lines"]:,} cell lines</h3>
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
      (r&nbsp;=&nbsp;{PAIRS[("LATS1", "LATS2")]:+.2f}) — the signature of paralog
      buffering. The signal only appears against the upstream node: NF2–LATS2 reaches
      <strong>r&nbsp;=&nbsp;{PAIRS[("NF2", "LATS2")]:+.2f}</strong>. The same pattern holds
      for YAP1 and its paralog:
      <strong>YAP1–WWTR1 (TAZ) is r&nbsp;=&nbsp;{PAIRS[("YAP1", "WWTR1")]:+.2f}</strong> —
      knocking out one does not substitute for the other, which is exactly why a TAZ-blind
      binder can be bypassed. And NF2–YAP1 is <strong>negative</strong>,
      r&nbsp;=&nbsp;{PAIRS[("NF2", "YAP1")]:+.2f}: lose the brake, gain the dependency.</p>
      <p>Literature alone would have called LATS1/2 one module. DepMap alone would have
      called them unrelated. Neither source answers this on its own.</p></div>
    </div>
    <div>
      <div class="tile"><span>novelty_signal</span><b>{NOVELTY["YAP1"][0]:.2f}</b>
        <p>YAP1: {NOVELTY["YAP1"][1]} mentions, {NOVELTY["YAP1"][2]} prior targeting
        efforts, {NOVELTY["YAP1"][3]} quantitative findings. The score is a
        <em>saturation</em> measure — near zero means everyone is already here.
        NF2 scores {NOVELTY["NF2"][0]:.2f} on the same scale, off {NOVELTY["NF2"][1]}
        mentions and {NOVELTY["NF2"][2]} targeting effort.</p></div>
      <p style="margin-top:16px;font-size:.92rem">That contrast is a prompt, not a
      recommendation. NF2 looks unexplored because it is a tumour suppressor that is
      <em>lost</em> in these tumours — there is nothing there to bind. A high novelty score
      says the literature is thin, and nothing about whether a target is tractable.</p>
      <p style="font-size:.92rem">The literature graph is what has been <em>written
      down</em>: {F["lit_nodes"]:,} proteins, {F["lit_edges"]:,} co-mention edges, rebuilt
      from the fingerprints on every query. The DepMap index is what CRISPR screens
      <em>measured</em>: {F["depmap_genes"]:,} genes, {F["depmap_edges"]:,} edges, each
      with a real correlation and sample size. An edge in both is a much stronger claim
      than an edge in either.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">What is in it</p>
    <h2>One lab's reading list, and it says so</h2></div>
  <div class="chart-wrap"><h3>Curated papers by publication year</h3>{years_svg()}
    <figcaption>{F["curated"]:,} curated papers, {F["years"][0][0]} onward
    ({F["years_before"]} older ones are off the left edge). The plateau from 2020 is the
    corpus being actively maintained rather than harvested once; {F["years"][-1][0]} is a
    partial year.</figcaption></div>
  <div class="two" style="margin-top:24px">
    <div class="chart-wrap"><h3>Top journals by curated paper count</h3>{journals_svg()}</div>
    <div>
      <p>Search indexes every hit, but only tier 1 and 2 journals are downloaded and
      curated — {GATE_PCT:.0f}% of what the searches find
      ({F["downloaded"]:,} of {F["indexed"]:,}). That gate is the single most consequential
      knob in the whole literature track, and it fails silently in one specific way: journal
      matching is <em>exact</em> against a normalised name, so a journal spelled a way the
      list does not contain is a 100% exclusion with no warning.</p>
      <div class="note-box"><p>You can see that failure mode in the chart beside this
      paragraph. <code>{NAT_COMMUN[1]}</code> and <code>{NAT_COMMUN[0]}</code> are the same
      journal, counted separately in the database, and merged into one
      {NAT_COMMUN[2]:,}-paper bar by this page's own extractor. The pipeline does not merge
      them for you — which is exactly why the behaviour is documented rather than
      hidden.</p></div>
      <p>This corpus is weighted toward chromatin biology, histone chaperones and
      structural/chemical biology. It is one lab's reading list. Ask it a question outside
      that space and the honest answer is that it does not know — which is why the skill is
      required to end every answer with what the corpus does <em>not</em> say, and why the
      MCP servers are configured never to reach for these tools on their own.</p>
    </div>
  </div>
  <div class="grid2" style="margin-top:24px">
    <div class="note-box"><h3>You do not have to build it</h3>
    <p>The curated corpus ships as a release asset: <code>python
    scripts/fetch_corpus.py</code> installs the {F["fingerprints"]:,} fingerprints, the
    LanceDB vector index and the paper database, and <code>search_corpus</code> works
    immediately. No API key, no LLM spend, no days of downloading. Source PDFs are not
    included — they are ~95% of the corpus on disk and nothing downstream reads them — and
    publisher-supplied abstracts are stripped from the shipped database. Everything on this
    page is reproducible from that download.</p></div>
    <div class="note-box"><h3>An ambiguous name resolves to nothing</h3>
    <p>Names are resolved against {F["resolver_symbols"]:,} approved gene symbols through
    ordered tiers, and the resolver is allowed to refuse. A synonym that is also another
    gene's approved symbol is dropped at load ({F["resolver_dropped"]:,} such links), and a
    synonym still claimed by several approved genes resolves to <em>nothing</em> rather
    than to the alphabetically-first claimant. <code>NAP1</code> — a yeast name — comes
    back <code>{F["resolver_nap1"]}</code>, not <code>ACOT8</code>. The visible cost is the
    split hub row above; the invisible cost of the alternative is a confident answer about
    the wrong gene.</p></div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Where it stops</p>
    <h2>What the corpus does NOT say</h2></div>
  <p>The skill is required to close every answer with its own gaps, and this is that
  closing section verbatim — not a summary of it. For a mesothelioma question these are
  the first things an expert would ask about, and the corpus does not have them:</p>
  <ul class="gaps">{not_say_items()}</ul>
  <p style="margin-top:18px">A retrieval system that cannot say this ends up answering
  from the shape of what it happens to hold. Naming BAP1, CDKN2A/MTAP–PRMT5 and mesothelin
  as absent is more useful here than anything the corpus did return, because it is what
  tells you which question to take somewhere else.</p>
</section>

<footer>
  <p>Session transcript: <code>outputs/mesothelioma_showcase.txt</code>. Every figure on
  this page is extracted at build time — paper counts from <code>data/literature.db</code>,
  fingerprints from <code>data/fingerprints/</code>, clusters from
  <code>data/clusters.json</code>, the interaction graph through
  <code>src._corpus_graph</code>, the co-essentiality index from
  <code>data/depmap_edges.parquet</code>, and the map from the session's own
  <code>mesothelioma_target_network.cyjs</code> — and committed to
  <code>docs/showcase/facts/corpus_explorer.json</code>, so a number that moves shows up as
  a diff. Rebuild with <code>python docs/showcase/build_corpus.py</code>.</p>
</footer>

</div>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB) — "
      f"{F['curated']:,} curated, map {F['net_nodes']} nodes / {F['net_edges']} edges")
