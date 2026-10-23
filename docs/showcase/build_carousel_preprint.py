#!/usr/bin/env python3
"""Build the preprint carousel as a PDF: assets/lpt_carousel_preprint.pdf.

Third deck in the series (`build_carousel.py`, `build_carousel_pdl1.py`) and the
only one that makes claims on behalf of a manuscript, so the register differs:
no hooks, no superlatives, every statement is one the preprint itself makes.
The stylesheet, `slide()` shell and number formatting are IMPORTED from
`build_carousel.py`, for the same reason `build_carousel_pdl1.py` does it — one
design system, maintained once.

Unlike its siblings this deck does NOT read `facts/*.json`: its figures are
panels cropped from the manuscript's own Figures 2 and 3
(`assets/preprint/fig*.png`), so the deck cannot disagree with the paper. The
few numbers typed below are quoted from the manuscript text and are listed in
one place (`PAPER`) so they can be checked against it in a single read.

    .venv/bin/python docs/showcase/build_carousel_preprint.py
"""
from __future__ import annotations

import base64
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ASSETS = HERE / "assets"
OUT = ASSETS / "lpt_carousel_preprint.pdf"

sys.path.insert(0, str(HERE))
from build_carousel import CHROME, CSS, GOOGLE_FONTS_LINK, slide  # noqa: E402

REPO = "github.com/MichaUckelmann/little-protein-tiger"

#: Every figure quoted from the manuscript text. Check against the paper here.
PAPER = {
    "papers": "~14,000",
    "prompts": 7, "replicates": 3, "reports_per_arm": 21,
    "judges": "claude-sonnet-5 and gpt-5.6-terra",
    "exam_open": (41, 7, 0), "exam_closed": (0, 47, 1),   # correct / unknown / wrong
    "rate_pain": "17.9%", "rate_pdl1": "9.6%",
    "usd": ("$0.33", "$0.42"), "gpu": ("~11 h", "~34 h"),
}
N_SLIDES = 9

EXTRA_CSS = """
.slide { padding:0.8in 0.85in 0.7in }
figure.f { background:#fff; border:1px solid var(--rule); padding:14px;
           flex:0 0 auto; margin-top:22px }
figure.f img { width:100%; height:auto; max-height:none }
.slide h2 { font-size:54px }
.fp { background:#fff; border:1px solid var(--rule); border-left:5px solid var(--accent);
      padding:22px 26px; margin-top:22px; font-size:21px; line-height:1.45 }
.fp .t { font-family:var(--display); font-size:30px; line-height:1.2; margin-bottom:8px }
.fp .m { font-family:var(--mono); font-size:15px; color:var(--muted);
         letter-spacing:.04em; margin-bottom:12px }
.fp .l { font-family:var(--mono); font-size:14px; letter-spacing:.1em;
         text-transform:uppercase; color:var(--accent); margin:14px 0 4px }
.fp dl { display:grid; grid-template-columns:150px 1fr; gap:3px 14px;
         font-size:18px; line-height:1.35 }
.fp dt { font-family:var(--mono); font-size:14px; color:var(--muted); padding-top:3px }
.fp dd { margin:0 }
ol.st { list-style:none; margin-top:26px }
ol.st li { display:grid; grid-template-columns:56px 1fr; padding:15px 0;
           border-bottom:1px solid var(--rule); font-size:24px; line-height:1.4 }
ol.st li b { font-family:var(--mono); font-weight:500; color:var(--accent); font-size:20px;
             padding-top:3px }
ol.st li span { color:var(--muted); font-size:20px; display:block }
"""


def img(name: str) -> str:
    p = ASSETS / "preprint" / name
    if not p.is_file():
        raise SystemExit(f"{p} missing")
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def pg(i: int) -> str:
    return f"{i} / {N_SLIDES}"


def s1() -> str:
    return slide(f"""
<div class="eyebrow">Preprint</div>
<h1 style="font-size:78px">Automated protein design to disrupt disease-relevant protein&ndash;protein interactions</h1>
<p class="wide" style="margin-top:34px">A framework of LLM agent skills with access to protein design
software, backed by a literature database of digested scientific papers. A
one-sentence prompt in; ranked, in silico validated binder designs out.</p>
<div class="grow"></div>
<p class="wide muted" style="font-size:24px">Michael Uckelmann<br>Hubrecht Institute, Utrecht, The Netherlands</p>
<div class="rule"></div>
<p class="wide" style="font-family:var(--mono);font-size:21px">Little Protein Tiger (LPT) &nbsp;&middot;&nbsp; {REPO}</p>
""", dark=True, page=pg(1))


def s2() -> str:
    return slide(f"""
<div class="eyebrow">Framework</div>
<h2>Language-model reasoning, deterministic tools, and a curated literature layer.</h2>
<ol class="st">
<li><b>01</b><div>Pathway and target selection<span>LLM skill queries the literature database and interaction graph; proposes a target pair.</span></div></li>
<li><b>02</b><div>Structure and epitope analysis<span>Candidate complexes ranked on measured interfaces; hotspots chosen and checked against the coordinates.</span></div></li>
<li><b>03</b><div>Backbone generation<span>RFD3 diffusion, sidecar prefilter, solubleMPNN sequence redesign.</span></div></li>
<li><b>04</b><div>Refolding and gating<span>RoseTTAFold3 refolds scored on ipTM, docking and fold RMSD, pLDDT, interface PAE, hotspot engagement.</span></div></li>
<li><b>05</b><div>Ranking and report<span>Composite z-score with ipTM and ipSAE; optional Rosetta terms.</span></div></li>
</ol>
<p class="wide muted" style="font-size:22px;margin-top:26px">Scale is set from a calibration run that
measures the hit rate on its own output before production is sized.</p>
<div class="grow"></div>
""", page=pg(2))


def s3() -> str:
    return slide(f"""
<div class="eyebrow">Literature layer</div>
<h2>Each paper is digested into a structured fingerprint.</h2>
<p class="wide muted" style="font-size:23px;margin-top:18px">Key findings with evidence, source span and
confidence; proteins and structures; pathway context. Example (abridged):</p>
<div class="fp">
  <div class="t">USP26 functions as a negative regulator of cellular reprogramming by stabilising PRC1 complex components</div>
  <div class="m">DOI 10.1038/s41467-017-00301-4 &nbsp;&middot;&nbsp; pathway_biology &nbsp;&middot;&nbsp; experimental_in_vitro &nbsp;&middot;&nbsp; schema 2.0</div>
  <div class="l">Context hook</div>
  <div>In mouse embryonic fibroblasts undergoing OSKM-mediated reprogramming, the deubiquitinase USP26
  physically interacts with CBX4 and CBX6, removes K48-linked polyubiquitin and protects them from
  proteasomal degradation. Knockdown of Usp26 increases AP+ iPSC colony generation fourfold.</div>
  <div class="l">Key finding 1 of 5</div>
  <dl>
    <dt>claim</dt><dd>shRNA knockdown of Usp26 enhances reprogramming efficiency fourfold</dd>
    <dt>protein_pair</dt><dd>USP26 &harr; NANOG</dd>
    <dt>evidence</dt><dd>~400 AP+ colonies vs ~100 in control shRNA (quantitative)</dd>
    <dt>context</dt><dd>Dox-induced OSKM MEF reprogramming, day 12</dd>
    <dt>confidence</dt><dd>0.95</dd>
    <dt>source_span</dt><dd>Page 3, Para 1</dd>
  </dl>
  <div class="l">Key finding 3 of 5</div>
  <dl>
    <dt>protein_pair</dt><dd>USP26 &harr; CBX6</dd>
    <dt>residues</dt><dd>Cys304</dd>
    <dt>evidence</dt><dd>Loss of K48 deubiquitination in C304S and &Delta;295&ndash;312 mutants (qualitative)</dd>
    <dt>source_span</dt><dd>Page 5, Para 1</dd>
  </dl>
</div>
<p class="wide muted" style="font-size:21px;margin-top:20px">Fingerprints are embedded
(PubMedBERT) for semantic search and aggregated into an interaction graph. Three complete
examples: Supplemental Information 4.</p>
<div class="grow"></div>
""", page=pg(3))


def s4() -> str:
    return slide(f"""
<div class="eyebrow">Figure 2A &nbsp;/&nbsp; interaction graph</div>
<h2>Protein pairs from the corpus form a graph the skills can traverse.</h2>
<figure class="f"><img src="{img('fig2A.png')}" alt=""></figure>
<figcaption style="text-align:left;margin-top:16px">Nodes are proteins; edges carry affinity
information and the number of papers mentioning the interaction (edge width). Edge colour
marks the DepMap CRISPR gene-effect correlation of the pair (blue: r &ge; 0.15; grey: |r| &lt; 0.15).
The graph shown is a small subgraph.</figcaption>
<p class="wide" style="font-size:23px;margin-top:20px">The skills can query this graph, find shortest paths
between proteins, and reason along edges with quantitative evidence attached.</p>
<div class="grow"></div>
""", page=pg(4))


def s5() -> str:
    return slide(f"""
<div class="eyebrow">Figure 2B &nbsp;/&nbsp; ablation</div>
<h2>Literature access mainly improves traceability.</h2>
<figure class="f"><img src="{img('fig2B.png')}" alt=""></figure>
<figcaption style="text-align:left;margin-top:16px">Rubric score (1&ndash;5) relative to a no-tools
run; standard pathway skill and wildcard skill. Seven prompts, three replicates, 21 reports per arm;
judged by {PAPER['judges']}. Bars: 95% bootstrap interval over prompts.</figcaption>
<div class="box"><div class="h">Reading</div>
<p class="wide" style="font-size:23px">Traceability shows the largest gain for both skills and both
judges. On target rationale, calibration, prior art and usefulness the differences are small and
mostly within about half a point. The study does not assess whether the chosen targets are better,
and the judging models may themselves be steered by their training knowledge.</p></div>
<div class="grow"></div>
""", page=pg(5))


def s6() -> str:
    eo, ec = PAPER["exam_open"], PAPER["exam_closed"]
    return slide(f"""
<div class="eyebrow">Figure 2C &nbsp;/&nbsp; tool use</div>
<h2>The wildcard skill uses the graph tools; the standard skill mostly does not.</h2>
<figure class="f"><img src="{img('fig2C.png')}" alt=""></figure>
<figcaption style="text-align:left;margin-top:16px">Mean tool calls per run (n = 21 per skill;
fraction of runs calling the tool in brackets).</figcaption>
<div class="box"><div class="h">Closed-book against open-book exam</div>
<p class="wide" style="font-size:23px">48 detailed questions answerable from the database. Open book:
{eo[0]} correct, {eo[1]} &ldquo;unknown&rdquo;. Closed book: {ec[0]} correct, {ec[1]} &ldquo;unknown&rdquo;,
{ec[2]} wrong. The exam was biased towards database content and some questions were leading
(Appendix II).</p></div>
<div class="grow"></div>
""", page=pg(6))


def s7() -> str:
    return slide(f"""
<div class="eyebrow">Figure 3A &nbsp;/&nbsp; end-to-end run 1</div>
<h2>&ldquo;Design novel inhibitors for pain receptors that are promising drug targets.&rdquo;</h2>
<figure class="f"><img src="{img('fig3A.png')}" alt=""></figure>
<figcaption style="text-align:left;margin-top:16px">Selected target: CALCRL/RAMP1 (PDB 3N7S); hotspots
(gold) on the RAMP1 heterodimerisation surface. Four highest-ranking designs (pink) with ipTM, minimum
ipSAE and composite score.</figcaption>
<p class="wide" style="font-size:23px;margin-top:18px">{PAPER['rate_pain']} of designs passed all hard gates and
ipTM &ge; 0.7. All stage reports: Supplemental Information 5.</p>
<div class="grow"></div>
""", page=pg(7))


def s8() -> str:
    return slide(f"""
<div class="eyebrow">Figure 3B &nbsp;/&nbsp; end-to-end run 2</div>
<h2>&ldquo;Design immune checkpoint inhibitors.&rdquo;</h2>
<figure class="f"><img src="{img('fig3B.png')}" alt=""></figure>
<figcaption style="text-align:left;margin-top:16px">Selected target: PD-L1 in complex with PD-1
(PDB 4ZQK); hotspots (gold) on the PD-1-binding face. Four highest-ranking designs (pink) with ipTM,
minimum ipSAE and composite score.</figcaption>
<p class="wide" style="font-size:23px;margin-top:18px">{PAPER['rate_pdl1']} of designs passed all hard gates and
ipTM &ge; 0.7. All stage reports: Supplemental Information 2.</p>
<div class="grow"></div>
""", page=pg(8))


def s9() -> str:
    u, g = PAPER["usd"], PAPER["gpu"]
    return slide(f"""
<div class="eyebrow">Scope and availability</div>
<h2>In silico results, at low cost.</h2>
<div class="stats two">
  <div class="stat"><div class="n sm">{u[0]} / {u[1]}</div><div class="k">LLM API cost, runs 1 / 2</div></div>
  <div class="stat"><div class="n sm">{g[0]} / {g[1]}</div><div class="k">GPU time, local workstation</div></div>
</div>
<div class="box"><div class="h">Limitations stated in the preprint</div>
<p class="wide" style="font-size:23px">Experimental success rates have not been evaluated; in silico
metrics are a proxy. The corpus (about 14,000 papers in the example runs) limits target choice, and
the released corpus is smaller because of licensing. There is no ground truth for whether a chosen
target is a good one.</p></div>
<div class="box"><div class="h">Availability</div>
<p class="wide" style="font-size:23px">Preprint with Supplemental Information 1&ndash;6 and Appendices I&ndash;II,
code, skills and corpus tooling:</p>
<p class="wide" style="font-family:var(--mono);font-size:24px;margin-top:10px">{REPO}</p></div>
<div class="grow"></div>
""", dark=True, page=pg(9))


def main() -> int:
    if CHROME is None:
        raise SystemExit("no Chrome/Chromium on PATH")
    slides = [s1(), s2(), s3(), s4(), s5(), s6(), s7(), s8(), s9()]
    assert len(slides) == N_SLIDES
    html = ('<meta charset="utf-8"><title>LPT preprint</title>'
            f'{GOOGLE_FONTS_LINK}<style>{CSS}{EXTRA_CSS}</style>' + "".join(slides))
    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "c.html"
        src.write_text(html, encoding="utf-8")
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                        f"--user-data-dir={td}/p", "--no-pdf-header-footer",
                        "--virtual-time-budget=8000", f"--print-to-pdf={OUT}", str(src)],
                       capture_output=True, text=True, timeout=180)
    if not OUT.is_file() or OUT.stat().st_size < 20_000:
        raise SystemExit("Chrome produced no usable PDF")
    print(f"{OUT.relative_to(ROOT)}  {OUT.stat().st_size / 1024:.0f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
