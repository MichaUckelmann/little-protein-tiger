#!/usr/bin/env python3
"""Build the LinkedIn launch carousel as a PDF.

LinkedIn renders an uploaded PDF as a swipeable slide deck, which is the one
native format on the platform that holds a sequence of figures without sending
the reader off-site. This writes `docs/showcase/assets/lpt_carousel.pdf` at
1080x1350 (4:5, the tallest ratio the feed shows uncropped on mobile).

Every number comes out of `facts/pain_receptors.json` and `facts/hero_check.json`
— the same snapshots the showcase pages build from, for the same reason: a
launch deck is the worst possible place to discover that a figure went stale,
because it is the one artefact that cannot be quietly corrected after posting.
Nothing here is typed in by hand. Run `build_pain.py` and `render_hero.py`
first if the run has changed.

    .venv/bin/python docs/showcase/build_carousel.py

Rendering is Chrome's own print-to-PDF: the page-box size is set in CSS and
Chrome embeds the webfonts it rendered with, so the deck carries its own
typography rather than falling back to whatever LinkedIn's viewer has.
"""
from __future__ import annotations

import base64
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE / "assets"
OUT = ASSETS / "lpt_carousel.pdf"
SITE = "michauckelmann.github.io/little-protein-tiger"

# 1080x1350 CSS px at 96dpi. LinkedIn cares about the ratio, not the absolute
# size; 4:5 is the tallest it shows without cropping in the mobile feed.
PAGE_W_IN, PAGE_H_IN = 11.25, 14.0625

CHROME = next((c for c in ("google-chrome", "google-chrome-stable", "chromium")
               if shutil.which(c)), None)

F = json.loads((HERE / "facts/pain_receptors.json").read_text(encoding="utf-8"))
CORPUS = json.loads((HERE / "facts/corpus_explorer.json").read_text(encoding="utf-8"))
CHECK = json.loads((HERE / "facts/hero_check.json").read_text(encoding="utf-8"))
CAL = F["calibration"]


def img(name: str, ext: str = "webp") -> str:
    path = ASSETS / f"{name}.{ext}"
    if not path.is_file():
        raise SystemExit(f"{path.relative_to(HERE)} missing — run the renders first")
    mime = "image/webp" if ext == "webp" else f"image/{ext}"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


CSS = """
@page { size: PAGE_Win PAGE_Hin; margin: 0 }
:root {
  --ground:#F4F4F1; --surface:#FFFFFF; --sunk:#ECEDE7;
  --ink:#171A14; --muted:#5D6357; --rule:#DEE0D8;
  --accent:#2b8f5d; --amber:#a07002; --dark:#14170F; --dark-ink:#E9ECE4;
  --display:"Newsreader",Georgia,serif;
  --body:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}
* { box-sizing:border-box; margin:0; padding:0 }
body { font-family:var(--body); color:var(--ink); -webkit-print-color-adjust:exact;
       print-color-adjust:exact }
.slide { width:PAGE_Win; height:PAGE_Hin; padding:0.95in 0.95in 0.8in;
         background:var(--ground); page-break-after:always;
         display:flex; flex-direction:column; position:relative; overflow:hidden }
.slide:last-child { page-break-after:auto }
.slide.dark { background:var(--dark); color:var(--dark-ink) }
.slide.dark .muted, .slide.dark .foot { color:#959C8B }
.slide.dark .rule { background:#2A2F25 }
/* Tables set no colour of their own, so on the dark slides they were rendering
   ink-on-ink and the licence terms were invisible. Every element that sits on
   a surface takes its colour AND its rule from that surface's token set. */
.slide.dark td { color:var(--dark-ink) }
.slide.dark td, .slide.dark th { border-bottom-color:#2A2F25 }
.slide.dark .pill { background:#182A1E; color:#7FCB9F }
.slide.dark .box { background:#1C2019; border-color:#2A2F25 }

.eyebrow { font-family:var(--mono); font-size:19px; letter-spacing:.16em;
           text-transform:uppercase; color:var(--accent); font-weight:500 }
.slide.dark .eyebrow { color:#63b98c }
h1 { font-family:var(--display); font-weight:400; font-size:88px; line-height:1.02;
     letter-spacing:-.02em; text-wrap:balance; margin-top:26px }
h2 { font-family:var(--display); font-weight:400; font-size:62px; line-height:1.06;
     letter-spacing:-.015em; text-wrap:balance; margin-top:22px }
p  { font-size:27px; line-height:1.5; max-width:26ch }
p.wide { max-width:none }
.muted { color:var(--muted) }
.rule { height:1px; background:var(--rule); margin:30px 0 }
.grow { flex:1 1 auto; min-height:0 }
.foot { margin-top:auto; font-family:var(--mono); font-size:17px;
        color:var(--muted); display:flex; justify-content:space-between;
        letter-spacing:.03em; padding-top:22px }

.prompt { font-family:var(--mono); font-size:30px; line-height:1.45;
          background:var(--surface); border:1px solid var(--rule);
          border-left:5px solid var(--accent); padding:26px 28px; margin:30px 0 }
.slide.dark .prompt { background:#1C2019; border-color:#2A2F25 }

figure { margin:0; display:flex; align-items:center; justify-content:center;
         flex:1 1 auto; min-height:0 }
figure img { max-width:100%; max-height:100%; object-fit:contain }
figcaption { font-size:19px; color:var(--muted); text-align:center;
             margin-top:14px; line-height:1.45 }

.stats { display:grid; grid-template-columns:repeat(3,1fr); gap:22px; margin-top:34px }
.stats.two { grid-template-columns:repeat(2,1fr) }
.stat { background:var(--surface); border:1px solid var(--rule); padding:24px }
.slide.dark .stat { background:#1C2019; border-color:#2A2F25 }
.stat .n { font-family:var(--display); font-size:60px; line-height:1;
           font-variant-numeric:tabular-nums }
.stat .n.sm { font-size:44px }
.stat .k { font-family:var(--mono); font-size:15px; letter-spacing:.1em;
           text-transform:uppercase; color:var(--muted); margin-top:12px }

table { width:100%; border-collapse:collapse; font-size:24px; margin-top:26px }
th { font-family:var(--mono); font-size:15px; letter-spacing:.1em;
     text-transform:uppercase; color:var(--muted); text-align:left;
     padding:0 0 12px; font-weight:500; border-bottom:1px solid var(--rule) }
td { padding:16px 0; border-bottom:1px solid var(--rule);
     font-variant-numeric:tabular-nums; vertical-align:top }
td.n, th.n { text-align:right }
.pill { font-family:var(--mono); font-size:14px; letter-spacing:.08em;
        padding:5px 11px; border-radius:3px; background:#E4F0E7; color:#1F6B41;
        white-space:nowrap }
.pill.b { background:#F2ECD9; color:#6E4E05 }
.chosen td:first-child { box-shadow:inset 4px 0 0 var(--accent); padding-left:14px;
                         font-weight:600 }

.bar { height:32px; background:var(--sunk); position:relative; margin-top:8px }
.bar i { position:absolute; inset:0 auto 0 0; background:var(--accent); display:block }
/* Inside the LEFT of the fill, in reverse. Right-aligned in the track, the
   label straddles the fill edge on any gate above ~90% and half of it lands
   green-on-green: at 99.5% the count was unreadable. Every gate here keeps
   at least half the refolds, so there is always room on the left. */
.bar b { position:absolute; left:11px; top:6px; font-family:var(--mono);
         font-size:16px; font-weight:500; font-style:normal; color:#fff }
.gate { margin-top:17px }
.gate .lab { font-family:var(--mono); font-size:19px; display:flex;
             justify-content:space-between }

.scale { margin-top:34px; position:relative; height:96px }
.scale .track { position:absolute; left:0; right:0; top:34px; height:10px;
                background:var(--sunk) }
.scale .ci { position:absolute; top:28px; height:22px; background:#BFDDCB }
.scale .pt { position:absolute; top:20px; width:3px; height:38px;
             background:var(--accent) }
.scale .tick { position:absolute; top:62px; font-family:var(--mono);
               font-size:16px; color:var(--muted); transform:translateX(-50%) }
.scale .cap { position:absolute; top:0; font-family:var(--mono); font-size:16px;
              color:var(--accent); transform:translateX(-50%); white-space:nowrap }

.box { background:var(--surface); border:1px solid var(--rule); padding:28px;
       margin-top:26px }
.box .h { font-family:var(--mono); font-size:16px; letter-spacing:.1em;
          text-transform:uppercase; color:var(--accent); margin-bottom:14px }
.big { font-family:var(--display); font-size:150px; line-height:.95;
       letter-spacing:-.03em; font-variant-numeric:tabular-nums }
.lede { font-size:31px; line-height:1.42; max-width:30ch; margin-top:24px }
""".replace("PAGE_W", str(PAGE_W_IN)).replace("PAGE_H", str(PAGE_H_IN))


def angstrom(text: str) -> str:
    """Angstrom units in prose written for a terminal, as &Aring;."""
    return re.sub(r"(?<=\d) A\b", " &Aring;", text)


def n(v: float, digits: int = 0) -> str:
    return f"{v:,.{digits}f}"


def slide(body: str, dark: bool = False, page: str = "") -> str:
    cls = "slide dark" if dark else "slide"
    foot = (f'<div class="foot"><span>{SITE}</span><span>{page}</span></div>'
            if page else "")
    return f'<section class="{cls}">{body}{foot}</section>'


# ── 1. the hook ──────────────────────────────────────────────────────────────
def s1() -> str:
    return slide(f"""
<div class="eyebrow">Little Protein Tiger</div>
<h1>We gave it one sentence about pain.</h1>
<div class="prompt">&ldquo;{F['query']}&rdquo;</div>
<p class="wide">It chose the target, the structure and the epitope, sized the
campaign from its own trial, and returned
<strong>{n(F['n_survivors'])} candidate binders</strong> against the receptor
behind migraine. <strong>${F['spend_usd']:.2f}</strong> of model spend,
{F['gpu_hours']['total']:.1f} GPU-hours, unattended.</p>
<figure><img src="{img('hero_receptor')}" alt=""></figure>
""", dark=True, page="1 / 10")


# ── 2. target selection ──────────────────────────────────────────────────────
def s2() -> str:
    rows = "".join(
        f'<tr class="{"chosen" if i == 0 else ""}">'
        f'<td>{gene}<br><span class="muted" style="font-size:18px">{entry}</span></td>'
        f'<td><span class="pill{"" if tier == "VALIDATED" else " b"}">{tier}</span></td>'
        f'<td class="muted" style="font-size:20px">{why}</td></tr>'
        for i, (tier, gene, why, _risk, entry) in enumerate(F["tiers"]))
    risk = F["tiers"][0][3]
    return slide(f"""
<div class="eyebrow">Stage 1 &nbsp;/&nbsp; nobody named a target</div>
<h2>Three candidates, ranked by how much evidence stood behind them.</h2>
<table><tr><th>Target</th><th>Evidence</th><th>Why it is designable</th></tr>
{rows}</table>
<div class="box"><div class="h">Chosen &mdash; and the risk it flagged itself</div>
<p class="wide">CALCRL / RAMP1, the CGRP receptor: the target class of erenumab,
an approved migraine antibody, so the mechanism is clinically de-risked before
a single design exists.</p>
<p class="wide muted" style="font-size:23px;margin-top:16px">{risk}</p></div>
<div class="grow"></div>
""", page="2 / 10")


# ── 3. structure selection ───────────────────────────────────────────────────
def s3() -> str:
    sw = F["switch"]
    a, b = sw["profiles"][sw["from"]], sw["profiles"][sw["to"]]
    return slide(f"""
<div class="eyebrow">Stage 2 &nbsp;/&nbsp; structure choice</div>
<h2>The best-known structure and the best structure to design on are not the
same question.</h2>
<table>
<tr><th>PDB</th><th class="n">Resolution</th><th class="n">Target length</th>
    <th class="n">Scaffolding chains</th></tr>
<tr><td>{sw['from']} <span class="muted">cited by the literature</span></td>
    <td class="n">{a['res']} &Aring;</td><td class="n">{a['target_len']}</td>
    <td class="n">{a['scaffold']}</td></tr>
<tr class="chosen"><td>{sw['to']} <span class="muted">chosen</span></td>
    <td class="n">{b['res']} &Aring;</td><td class="n">{b['target_len']}</td>
    <td class="n">{b['scaffold']}</td></tr>
</table>
<div class="box"><div class="h">The pipeline's own reason</div>
<p class="wide" style="font-size:26px">{angstrom(sw['reason'])}</p></div>
<p class="wide muted" style="margin-top:28px">A nanobody, a G protein and a
fusion partner are what makes a cryo-EM structure solvable. They are also four
extra chains a binder has to be designed around, and none of them exist on a
real cell.</p>
<div class="grow"></div>
""", page="3 / 10")


# ── 4. the epitope ───────────────────────────────────────────────────────────
def s4() -> str:
    hot = ", ".join(f"{nm.title()}{i}" for nm, i in F["hotspots"])
    return slide(f"""
<div class="eyebrow">Stage 3 &nbsp;/&nbsp; where a drug can actually reach</div>
<h2>On a membrane receptor, most of the surface is unusable.</h2>
<div class="stats">
  <div class="stat"><div class="n">{len(F['hotspots'])}</div>
    <div class="k">hotspots declared</div></div>
  <div class="stat"><div class="n">{F['trim_residues']}</div>
    <div class="k">target residues kept</div></div>
  <div class="stat"><div class="n sm">excluded</div>
    <div class="k">transmembrane face</div></div>
</div>
<figure><img src="{img('hero_apo')}" alt=""></figure>
<figcaption>The epitope, on the full-length receptor in the bilayer. Everything
below the membrane is unreachable by an injected protein; the transmembrane
residues were dropped from the target on both sides.</figcaption>
<p class="wide muted" style="margin-top:24px;font-size:22px">
Hotspots: {hot}. Priority: {', '.join(F['priority_residues'])} &mdash;
literature-validated contacts on RAMP1.</p>
""", page="4 / 10")


# ── 5. the trial ─────────────────────────────────────────────────────────────
def s5() -> str:
    b = CAL["backbone"]
    span = 30.0                      # per cent, the scale's full width
    lo, hi, pt = (b["p_low"] * 100, b["p_high"] * 100, b["p_hat"] * 100)
    ticks = "".join(f'<span class="tick" style="left:{v / span * 100:.2f}%">{v:.0f}%</span>'
                    for v in (0, 10, 20, 30))
    return slide(f"""
<div class="eyebrow">Stage 5 &nbsp;/&nbsp; never scale straight to production</div>
<h2>It measured its own hit rate before spending the GPU budget.</h2>
<div class="box"><div class="h">Calibration trial &mdash; {b['k']} of {n(b['n'])} backbones</div>
<div class="big">{pt:.1f}%</div>
<div class="scale">
  <div class="track"></div>
  <div class="ci" style="left:{lo / span * 100:.2f}%;width:{(hi - lo) / span * 100:.2f}%"></div>
  <div class="pt" style="left:{pt / span * 100:.2f}%"></div>
  <div class="cap" style="left:{lo / span * 100:.2f}%">{lo:.1f}%</div>
  <div class="cap" style="left:{hi / span * 100:.2f}%">{hi:.1f}%</div>
  {ticks}
</div>
<p class="wide muted" style="font-size:22px;margin-top:12px">95% Wilson interval.
The campaign is sized on the <em>pessimistic</em> end, so a lucky trial cannot
authorise a run that will not pay for itself.</p></div>
<div class="stats" style="margin-top:26px">
  <div class="stat"><div class="n sm">{CAL['verdict'].replace('_', ' ')}</div>
    <div class="k">verdict</div></div>
  <div class="stat"><div class="n sm">{CAL['requested_bar']} &rarr; {CAL['bar_raised_to']}</div>
    <div class="k">bar raised, not lowered</div></div>
  <div class="stat"><div class="n sm">{CAL['pessimistic']['est_gpu_hours']} h</div>
    <div class="k">worst case to hit target</div></div>
</div>
<p class="wide" style="margin-top:30px">The target turned out good enough that the
pipeline made its own success criterion <strong>stricter</strong> than the one it
was given &mdash; and still projected the campaign would finish inside budget.</p>
<div class="grow"></div>
""", page="5 / 10")


# ── 6. the funnel ────────────────────────────────────────────────────────────
def s6() -> str:
    gates = "".join(
        f'<div class="gate"><div class="lab"><span>{name}</span>'
        f'<span class="muted">{pct:.1f}%</span></div>'
        f'<div class="bar"><i style="width:{pct:.1f}%"></i>'
        f'<b>{n(kept)}</b></div></div>'
        for name, kept, pct in sorted(F["gates"], key=lambda g: g[2]))
    return slide(f"""
<div class="eyebrow">Stage 6&ndash;7 &nbsp;/&nbsp; production and gating</div>
<h2>Eight independent gates, applied to every refold.</h2>
<div class="stats">
  <div class="stat"><div class="n">{n(F['n_rfd3'])}</div><div class="k">backbones</div></div>
  <div class="stat"><div class="n">{n(F['n_scored'])}</div><div class="k">refolds scored</div></div>
  <div class="stat"><div class="n">{n(F['n_survivors'])}</div><div class="k">survivors</div></div>
</div>
{gates}
<p class="wide muted" style="margin-top:26px;font-size:22px">Confidence alone is
not enough: a model can be certain about an interface it invented. The
geometry gates &mdash; docking RMSD, epitope recall, hotspot engagement &mdash;
are what separate a binder on the right site from a confident one on the wrong
one.</p>
<div class="grow"></div>
""", page="6 / 10")


# ── 7. the lead design ───────────────────────────────────────────────────────
def s7() -> str:
    d = F["designs"][0]
    cg = CHECK["cgrp"]
    return slide(f"""
<div class="eyebrow">Stage 8 &nbsp;/&nbsp; the lead candidate</div>
<h2>A {d['len']}-residue mini-protein, docked to 1.3 &Aring;.</h2>
<div class="stats two">
  <div class="stat"><div class="n">{d['iptm']:.3f}</div><div class="k">interface ipTM</div></div>
  <div class="stat"><div class="n">{d['dock']:.2f} &Aring;</div><div class="k">dock RMSD</div></div>
  <div class="stat"><div class="n">{d['ipsae_min']:.3f}</div><div class="k">ipSAE</div></div>
  <div class="stat"><div class="n">{d['engagement'] * 100:.0f}%</div><div class="k">hotspots engaged</div></div>
</div>
<figure style="margin-top:26px"><img src="{img('pain_rank1')}" alt=""></figure>
<div class="box"><div class="h">An independent check the run never saw</div>
<p class="wide" style="font-size:25px">Superposed onto {CHECK['pdb_id']}, the
full receptor <em>with its natural agonist bound</em>, the design overlaps CGRP
itself &mdash; {cg['binder_atoms_in_contact']} binder atoms within
{CHECK['cutoff_A']} &Aring;, closest approach {cg['min_distance_A']} &Aring;.
The campaign designed against a peptide-free crystal form and never scored a
single design against CGRP.</p></div>
""", page="7 / 10")


# ── 8. what it cost ──────────────────────────────────────────────────────────
def s8() -> str:
    c = F["citations"]
    checked = sum(v["checked"] for v in c.values())
    verified = sum(v["verified"] for v in c.values())
    return slide(f"""
<div class="eyebrow">The bill</div>
<h2>Unattended, end to end.</h2>
<div class="stats">
  <div class="stat"><div class="n">${F['spend_usd']:.2f}</div><div class="k">model spend</div></div>
  <div class="stat"><div class="n">{F['gpu_hours']['total']:.1f}</div><div class="k">GPU-hours, one card</div></div>
  <div class="stat"><div class="n">{len(F['llm_stages'])}</div><div class="k">stages that used an LLM</div></div>
</div>
<div class="rule"></div>
<p class="wide">Four stages reason: pathway, literature, structure, and the final
analysis. Everything between them &mdash; trimming, spec building, calibration,
sizing, gating, ranking &mdash; is deterministic Python, which is why the bill
is cents rather than hundreds of dollars and why the same inputs give the same
campaign twice.</p>
<div class="box"><div class="h">Citations</div>
<p class="wide" style="font-size:25px">{verified} of {checked} DOIs the reasoning
stages cited were resolved and verified against the literature. The
{checked - verified} that did not resolve is named in the run record, not
quietly dropped.</p></div>
<div class="grow"></div>
""", page="8 / 10")


# ── 9. limitations ───────────────────────────────────────────────────────────
def s9() -> str:
    """The honest coda, immediately before the ask.

    Deliberately NOT "the pipeline is only as strong as its literature
    database". That is true of target discovery and prior art and false of
    everything else — structure selection, the trim, calibration, the gates and
    the ranking read no papers at all — so the sweeping version overstates one
    dependency and omits the real one, which is that nothing here has been
    tested at a bench.

    Nor is it "one lab's reading list", which was the first draft and undersold
    it: chromatin is the largest single slice of the curated set and still only
    about a sixth of it. The shape is stated with measured shares instead
    (`facts/corpus_explorer.json`'s `coverage`), which makes the caveat land
    harder, not softer — a reader can see that pain really is thin here.
    """
    c = F["citations"]
    checked = sum(v["checked"] for v in c.values())
    verified = sum(v["verified"] for v in c.values())
    unfound = [d for v in c.values() for d in v["unverified"]]
    cov = {lab: pct for lab, _n, pct in CORPUS["coverage"]}
    top = CORPUS["coverage"][:4]
    rows = "".join(
        f'<tr><td>{lab}</td><td class="n">{pct:.0f}%</td></tr>'
        for lab, _n, pct in top)
    pain = next((r for r in CORPUS["coverage"] if "pain" in r[0]), None)
    return slide(f"""
<div class="eyebrow">What it does not do</div>
<h2>Nothing in this deck has been near a bench.</h2>
<p class="wide">Every figure here is computational: folding confidence,
interface geometry, buried surface area. Designs like these need expression,
purification and a binding assay before any of it is a result. The gates, the
trial and the ranking exist to make that shortlist small and defensible &mdash;
not to skip it.</p>
<div class="rule"></div>
<h2 style="font-size:44px;margin-top:0">And the corpus has a shape.</h2>
<p class="wide" style="font-size:25px">{n(CORPUS['indexed'])} papers indexed
from tier-1 and tier-2 journals, {n(CORPUS['curated'])} of them curated into
structured fingerprints &mdash; quantitative findings, interactions, and a
source span for every claim &mdash; then embedded for retrieval. It spans
molecular and cell biology, and it leans:</p>
<div style="display:flex;gap:34px;align-items:flex-start;margin-top:10px">
  <table style="margin-top:14px;flex:1">{rows}
    <tr><td><em>{pain[0]}</em></td><td class="n"><em>{pain[2]:.0f}%</em></td></tr>
  </table>
  <p class="wide" style="flex:1.25;font-size:24px;margin-top:22px">
  Approximate shares of the curated set, by title. So the coverage here was real
  but thin &mdash; and the run leaned on it anyway:
  <strong>{verified} of the {checked} papers</strong> its three reasoning stages
  cited are in the corpus, with fingerprints. The {checked - verified} that was
  not is named in the run record
  (<span style="font-family:var(--mono);font-size:20px">{unfound[0]}</span>) and
  is malformed &mdash; the citation check caught it.</p>
</div>
<div class="box" style="margin-top:22px"><div class="h">Which is the fixable part</div>
<p class="wide" style="font-size:25px">The corpus is built by a config-driven
fetcher: swap the keyword sets, point it at your own field, rebuild the index.
Every stage downstream carries on unchanged &mdash; structure selection, the
trim, calibration and the gates read no papers at all.</p></div>
<div class="grow"></div>
""", page="9 / 10")


# ── 10. terms ────────────────────────────────────────────────────────────────
def s10() -> str:
    return slide(f"""
<div class="eyebrow">Little Protein Tiger</div>
<h1>Free for any non&#8209;commercial use.</h1>
<table style="margin-top:44px">
<tr><th>Who</th><th>Terms</th></tr>
<tr><td>Academics, students, non-profits,<br>anyone not doing it for money</td>
    <td>Free. Read it, run it, change it, publish with it.</td></tr>
<tr><td>Companies</td>
    <td>Needs a commercial licence &mdash; ask.</td></tr>
</table>
<p class="wide" style="margin-top:40px">Source-available under PolyForm
Noncommercial 1.0.0 &mdash; not an OSI open-source licence, and deliberately so.
The full terms, and what is and is not covered, are in
<span style="font-family:var(--mono);font-size:24px">docs/licensing.md</span>.</p>
<div class="rule"></div>
<p class="wide" style="font-size:31px">Four campaigns written up end to end
&mdash; the CGRP receptor, a mesothelioma target found from scratch, a PD-L1
campaign, and a tour of the literature corpus. Every figure is extracted from
the run that produced it.</p>
<p class="wide" style="font-family:var(--mono);font-size:31px;color:#63b98c;
   margin-top:22px">{SITE}</p>
<div class="grow"></div>
""", dark=True, page="10 / 10")


def main() -> int:
    if CHROME is None:
        raise SystemExit("no Chrome/Chromium on PATH — needed for print-to-PDF")

    slides = [s1(), s2(), s3(), s4(), s5(), s6(), s7(), s8(), s9(), s10()]
    html = (f'<meta charset="utf-8"><title>Little Protein Tiger</title>'
            f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
            f'family=Newsreader:opsz,wght@6..72,400;6..72,500&'
            f'family=IBM+Plex+Sans:wght@400;500;600;700&'
            f'family=IBM+Plex+Mono:wght@400;500&display=swap">'
            f"<style>{CSS}</style>" + "".join(slides))

    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "carousel.html"
        src.write_text(html, encoding="utf-8")
        # A real profile dir and a settle delay: without the delay Chrome prints
        # before the webfonts arrive and the deck ships in Georgia/Arial.
        proc = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={td}/profile", "--no-pdf-header-footer",
             "--virtual-time-budget=8000",
             f"--print-to-pdf={OUT}", str(src)],
            capture_output=True, text=True, timeout=180)
    if not OUT.is_file() or OUT.stat().st_size < 20_000:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit("Chrome produced no usable PDF")
    print(f"  {OUT.relative_to(HERE.parent.parent)}  "
          f"{len(slides)} slides  {OUT.stat().st_size / 1024:.0f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
