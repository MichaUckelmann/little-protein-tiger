#!/usr/bin/env python3
"""Build the preprint carousel as a PDF: assets/lpt-carousel-preprint.pdf.

Third deck in the series (`build_carousel.py`, `build_carousel_pdl1.py`) and the
only one that makes claims on behalf of a manuscript, so the register differs:
no hooks, no superlatives, every statement is one the preprint itself makes.
The stylesheet, `slide()` shell and number formatting are IMPORTED from
`build_carousel.py`, for the same reason `build_carousel_pdl1.py` does it — one
design system, maintained once.

Unlike its siblings this deck does NOT read `facts/*.json`: its figures are
panels cropped from the submitted manuscript's own Figures 2, 3 and 4
(`assets/preprint/fig*.png`, extracted from `lpt_manuscript_Uckelmann_20261029_submitted.pdf`), so the deck cannot disagree with the paper. The
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
OUT = ASSETS / "lpt-carousel-preprint.pdf"

sys.path.insert(0, str(HERE))
from build_carousel import CHROME, CSS, GOOGLE_FONTS_LINK, slide  # noqa: E402

REPO = "github.com/MichaUckelmann/little-protein-tiger"

#: Every figure quoted from the manuscript text. Check against the paper here.
PAPER = {
    "title": "LPT: Orchestrating bounded agentic loops for automated protein binder design",
    "papers": "~14,000",
    "exam_open": (41, 7, 0), "exam_closed": (0, 47, 1),   # correct / unknown / wrong
    "rate": ("17.9%", "9.6%", "10%"),                      # RAMP1, PD-L1, TIGIT
    "usd": ("$0.33", "$0.42", "$0.39"), "gpu": ("~11 h", "~34 h", "~20 h"),
}
N_SLIDES = 8

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
table.cmp { width:100%; border-collapse:collapse; margin-top:30px; font-size:22px }
table.cmp th { font-family:var(--mono); font-weight:500; font-size:15px; letter-spacing:.08em;
               text-transform:uppercase; color:var(--muted); text-align:left; padding:10px 8px;
               border-bottom:1px solid #2A2F25 }
table.cmp td { padding:16px 8px; border-bottom:1px solid #2A2F25 }
table.cmp td:first-child { color:var(--muted); font-size:19px }
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


def fig(name: str, cap: str) -> str:
    return (f'<figure class="f"><img src="{img(name)}" alt=""></figure>'
            f'<figcaption style="text-align:left;margin-top:16px">{cap}</figcaption>')


def s1() -> str:
    return slide(f"""
<div class="eyebrow">Manuscript</div>
<h1 style="font-size:74px">LPT: Orchestrating bounded agentic loops for automated protein binder design</h1>
<p class="wide" style="margin-top:34px">Fixed orchestration of LLM stages, each running a bounded agentic
loop with its own system prompt and tool access, backed by a curated literature database.
One natural-language prompt in; ranked, in silico validated binder designs out.</p>
<div class="grow"></div>
<p class="wide muted" style="font-size:24px">Michael Uckelmann<br>Hubrecht Institute, Utrecht, The Netherlands</p>
<div class="rule"></div>
<p class="wide" style="font-family:var(--mono);font-size:21px">Little Protein Tiger (LPT) &nbsp;&middot;&nbsp; {REPO}</p>
""", dark=True, page=pg(1))


def s2() -> str:
    return slide(f"""
<div class="eyebrow">Figure 3 &nbsp;/&nbsp; structure analysis</div>
<h2>&ldquo;Design novel inhibitors for pain receptors that are promising drug targets.&rdquo;</h2>
<figure class="f" style="margin-top:18px;display:block"><img src="{img('fig3A.png')}" alt="" style="display:block;width:100%">
<img src="{img('fig3B.png')}" alt="" style="display:block;width:100%;margin-top:10px"></figure>
<div class="box"><div class="h">From prompt to hotspots</div>
<p class="wide" style="font-size:22px">Starting from the prompt alone, the pipeline selected CALCRL&ndash;RAMP1 (PDB 3N7S).
The structure-analysis tool returns the interface analysis: interface residues, H-bonds and an
estimated binding contribution &epsilon; per residue. The model then groups residues into clusters
and chooses Region 1, a 10-residue patch spanning the core heterodimerisation surface of RAMP1,
and forwards it to the design phase as hotspots.</p></div>
<div class="grow"></div>
""", page=pg(2))


def s3() -> str:
    return slide(f"""
<div class="eyebrow">Figure 4A &nbsp;/&nbsp; end-to-end run 1</div>
<h2>&ldquo;Design de novo inhibitors for pain receptors that are promising drug targets.&rdquo;</h2>
{fig('fig4A.png', 'The pipeline chose to disrupt CALCRL&ndash;RAMP1 (PDB 3N7S). Hotspots in gold; two highly ranked designs in pink, with ipTM, minimum ipSAE and composite score.')}
<div class="stats">
  <div class="stat"><div class="n">{PAPER['rate'][0]}</div><div class="k">of refolds pass all gates, ipTM &ge; 0.7</div></div>
  <div class="stat"><div class="n">{PAPER['usd'][0]}</div><div class="k">LLM API cost</div></div>
  <div class="stat"><div class="n">{PAPER['gpu'][0]}</div><div class="k">GPU time, local workstation</div></div>
</div>
<div class="grow"></div>
""", page=pg(3))


def s4() -> str:
    return slide(f"""
<div class="eyebrow">Figure 4B &nbsp;/&nbsp; end-to-end run 2</div>
<h2>&ldquo;Design immune checkpoint inhibitors.&rdquo;</h2>
{fig('fig4B.png', 'The pipeline chose to disrupt PD-1/PD-L1 (PDB 4ZQK) and designed binders to PD-L1. Hotspots in gold; two highly ranked designs in pink, with ipTM, minimum ipSAE and composite score.')}
<div class="stats">
  <div class="stat"><div class="n">{PAPER['rate'][1]}</div><div class="k">of refolds pass all gates, ipTM &ge; 0.7</div></div>
  <div class="stat"><div class="n">{PAPER['usd'][1]}</div><div class="k">LLM API cost</div></div>
  <div class="stat"><div class="n">{PAPER['gpu'][1]}</div><div class="k">GPU time, local workstation</div></div>
</div>
<div class="grow"></div>
""", page=pg(4))


def s5() -> str:
    return slide(f"""
<div class="eyebrow">Figure 4C &nbsp;/&nbsp; end-to-end run 3</div>
<h2>&ldquo;&hellip;checkpoint inhibitors that are not targeting PD1 or PD-L1 but could synergize with them.&rdquo;</h2>
{fig('fig4C.png', 'The pipeline chose to disrupt the TIGIT&ndash;PVR (CD155) interaction (PDB 3UDW). Hotspots in gold; two highly ranked designs in pink, with ipTM, minimum ipSAE and composite score.')}
<div class="stats">
  <div class="stat"><div class="n">{PAPER['rate'][2]}</div><div class="k">of refolds pass all gates, ipTM &ge; 0.7</div></div>
  <div class="stat"><div class="n">{PAPER['usd'][2]}</div><div class="k">LLM API cost</div></div>
  <div class="stat"><div class="n">{PAPER['gpu'][2]}</div><div class="k">GPU time, local workstation</div></div>
</div>
<div class="grow"></div>
""", page=pg(5))


def s6() -> str:
    eo, ec = PAPER["exam_open"], PAPER["exam_closed"]
    return slide(f"""
<div class="eyebrow">Figure 2A &nbsp;/&nbsp; literature database</div>
<h2>{PAPER['papers']} papers, digested and linked into an interaction graph.</h2>
{fig('fig2A.png', 'Nodes are proteins; edges carry affinity information and the number of papers mentioning the interaction (edge width). Edge colour marks the DepMap CRISPR gene-effect correlation of the pair. The graph shown is a small subgraph.')}
<div class="box"><div class="h">Closed-book against open-book exam</div>
<p class="wide" style="font-size:23px">48 questions answerable from the database. With database access:
{eo[0]} correct, {eo[1]} &ldquo;unknown&rdquo;. Without: {ec[0]} correct, {ec[1]} &ldquo;unknown&rdquo;,
{ec[2]} wrong. The exam was biased towards database content and some questions were leading.</p></div>
<div class="grow"></div>
""", page=pg(6))


def s7() -> str:
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
(PubMedBERT) for semantic search and aggregated into the interaction graph. Three complete
examples: Supplemental Information 4.</p>
<div class="grow"></div>
""", page=pg(7))


def s8() -> str:
    u, g, r = PAPER["usd"], PAPER["gpu"], PAPER["rate"]
    return slide(f"""
<div class="eyebrow">Scope and availability</div>
<h2>In silico results, at low cost.</h2>
<table class="cmp">
<tr><th></th><th>CALCRL / RAMP1</th><th>PD-1 / PD-L1</th><th>PVR / TIGIT</th></tr>
<tr><td>Pass all gates, ipTM &ge; 0.7</td><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>
<tr><td>LLM API cost</td><td>{u[0]}</td><td>{u[1]}</td><td>{u[2]}</td></tr>
<tr><td>GPU time (local)</td><td>{g[0]}</td><td>{g[1]}</td><td>{g[2]}</td></tr>
</table>
<p class="wide muted" style="font-size:21px;margin-top:14px">The top 20 designs of each campaign were refolded with
Boltz-2 and Protenix-v2; the majority pass all gates in all folding models.</p>
<div class="box"><div class="h">Limitations stated in the manuscript</div>
<p class="wide" style="font-size:23px">Experimental success rates have not been evaluated; in silico
metrics are a proxy. The database ({PAPER['papers']} papers in the example runs) limits target choice, and
the released database is smaller because of licensing.</p></div>
<div class="box"><div class="h">Availability</div>
<p class="wide" style="font-size:23px">Manuscript with Supplemental Information 1&ndash;6, appendices,
code, skills and corpus tooling:</p>
<p class="wide" style="font-family:var(--mono);font-size:24px;margin-top:10px">{REPO}</p></div>
<div class="grow"></div>
""", dark=True, page=pg(8))


def main() -> int:
    if CHROME is None:
        raise SystemExit("no Chrome/Chromium on PATH")
    slides = [s1(), s2(), s3(), s4(), s5(), s6(), s7(), s8()]
    assert len(slides) == N_SLIDES
    html = ('<meta charset="utf-8"><title>LPT manuscript</title>'
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
