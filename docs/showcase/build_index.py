#!/usr/bin/env python3
"""Build the showcase landing page.  Rebuild: python docs/showcase/build_index.py"""
from __future__ import annotations
import base64, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _common import head as _mkhead
OUT = HERE / "index.html"
CSS = (HERE / "campaign_pdl1.html").read_text().split("<style>")[1].split("</style>")[0]
CSS += """
.cards3{display:grid;gap:20px;margin-top:8px}
@media(min-width:900px){.cards3{grid-template-columns:repeat(3,1fr)}}
.pc{background:var(--surface);border:1px solid var(--rule);border-radius:2px;overflow:hidden;
  display:grid;grid-template-rows:auto 1fr;text-decoration:none;color:inherit;
  transition:border-color .15s}
.pc:hover,.pc:focus-visible{border-color:var(--accent)}
.pc img{width:100%;height:auto;display:block;border-bottom:1px solid var(--rule)}
.pc-b{padding:18px 19px;display:grid;gap:9px;align-content:start}
.pc h3{font-family:var(--display);font-weight:500;font-size:1.22rem;letter-spacing:-.01em;
  margin:0;line-height:1.2}
.pc p{font-size:.9rem;color:var(--muted);margin:0;max-width:none}
.pc .go{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--accent)}
.src{font-family:var(--mono);font-size:11px;color:var(--muted)}
"""

def b64(p):
    """Thumbnail the OG card for in-page use.

    The PNGs themselves stay full-size for social crawlers, which will not take a
    data URI; the landing page only needs a 640px preview, as WebP.
    """
    import io
    from PIL import Image
    im = Image.open(HERE / "assets" / p).convert("RGB")
    im = im.resize((640, round(im.height * 640 / im.width)), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "WEBP", quality=82, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()

PAGES = [
    ("campaign_pdl1.html", "og_campaign.png", "A binder campaign, end to end",
     "PD-L1. Why that structure and that face of it, the nine hotspots and the ΔΔG behind "
     "them, a calibration gate that raised its own bar, and twenty ranked designs.",
     "projects/pdl1_e2e"),
    ("corpus_explorer.html", "og_corpus.png", "One question, one target map",
     "A real corpus-explorer session over 11,055 curated papers: the tool calls it made, "
     "what a curated finding looks like, and the interaction and DepMap graphs it built.",
     "outputs/mesothelioma_showcase.txt"),
    ("ppi_discovery.html", "og_ppi.png", "Disease name to designed binders",
     "No target named: the pipeline picked YAP1/TEAD1, argued for it, sized its own "
     "campaign from a measured hit rate, and ran it — 317 designs through every gate for "
     "79 cents of model spend.",
     "projects/mesothelioma_showcase"),
]

cards = "".join(f'''<a class="pc" href="{href}">
  <img src="{b64(img)}" alt="">
  <div class="pc-b"><h3>{title}</h3><p>{desc}</p>
  <span class="src">built from {src}</span><span class="go">Read →</span></div></a>'''
  for href, img, title, desc, src in PAGES)

HEAD = _mkhead(
    "Little Protein Tiger Showcase",
    "Three illustrated walkthroughs of a protein-binder design pipeline, built from real "
    "runs: a complete PD-L1 campaign, a corpus-explorer session, and the PPI discovery track.",
    "index.html", "campaign")

HTML = f"""{HEAD}
<style>{CSS}</style>
<div class="wrap">
<header class="hero">
  <div><p class="eyebrow">Little Protein Tiger</p>
  <h1>What it actually does, shown on real runs</h1></div>
  <p class="lede">LPT is a command-line pipeline that goes from a disease or a target name
  to designed protein binders, with a curated literature corpus and a structural-biology
  toolkit underneath. These three pages are walkthroughs of runs that really happened in
  this repository — every number is read out of a run directory, not illustrative.</p>
  <div class="cards3">{cards}</div>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">what you are looking at</p>
  <h2>Measured, not asserted</h2></div>
  <div class="two">
    <div>
      <p>Two habits show up on every page. The first is that decisions are made from
      measurements the pipeline took itself: which structure to design against comes from a
      table of nine it compared, and how big a campaign to run comes from counting hits in a
      trial and extrapolating with a confidence interval — never from a fixed default.</p>
      <p>The second is that the pages say what the runs do <em>not</em> show. One of them
      walks through a run whose filters dropped nothing, and says so, because a funnel that
      rejects nothing has not demonstrated that it works.</p>
    </div>
    <div>
      <div class="note-box"><p><strong>Nothing here has been near a bench.</strong> Every
      design on these pages is an unvalidated computational hypothesis. Anyone synthesising
      a sequence is responsible for screening it — see
      <code>docs/responsible-use.md</code>.</p></div>
      <p style="margin-top:16px;font-size:.92rem">Each page is one self-contained HTML file
      with its images inlined, generated by a script beside it, so it can be rebuilt when a
      run changes. Source: <code>docs/showcase/</code>.</p>
    </div>
  </div>
</section>
</div>
"""
OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
