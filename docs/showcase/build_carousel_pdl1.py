#!/usr/bin/env python3
"""Build the PD-L1 launch carousel as a PDF: assets/lpt_carousel_pdl1.pdf.

The companion to `build_carousel.py`, and deliberately the OTHER entry point.
That deck starts from one sentence about a disease and spends four reasoning
stages deciding what to bind; this one starts from a target the operator already
named (`--workflow binder`) and shows what the pipeline does with it — rank the
nine solved structures of it by their measured interfaces, prove the chain it
picked is the right molecule, choose the epitope, size the campaign from its own
trial, and gate 5,824 refolds down to a shortlist. Most people arriving at this
repo already know their target, so this is the deck that answers their question.

Every number comes out of `facts/campaign_pdl1.json`, the snapshot
`build_campaign.py` extracts from `projects/pdl1_e2e` — same file, same reason as
`build_carousel.py`: a launch deck is the worst place to discover a figure went
stale, because it is the one artefact you cannot quietly correct after posting.

The stylesheet, the `slide()` shell and the number formatting are IMPORTED from
`build_carousel.py` rather than copied. Two decks posted a week apart that share
a palette but drift in type scale look like two projects; and a stylesheet
maintained twice is a stylesheet maintained once and forgotten once.

    .venv/bin/python docs/showcase/build_carousel_pdl1.py

HEADLINE NUMBERS ARE THE RUN'S OWN, at the `hotspot_engagement >= 1` threshold
in force in August 2026 — 715 survivors, and the top 20 that ranking produced.
`config.yaml` sets 0.75 today, which is why slide 10 states the re-gate (752)
rather than quietly mixing the two: the twenty designs shown here came out of
the historical gate, so every other figure on the deck has to match it.
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
ROOT = HERE.parent.parent
ASSETS = HERE / "assets"
OUT = ASSETS / "lpt_carousel_pdl1.pdf"

sys.path.insert(0, str(HERE))

from build_carousel import (          # noqa: E402  — one design system, imported
    CHROME,
    CSS,
    GOOGLE_FONTS_LINK,
    angstrom,
    n,
    slide,
)

F = json.loads((HERE / "facts/campaign_pdl1.json").read_text(encoding="utf-8"))
CAL = F["calibration"]
GATE = F["gate_historical"]          # what the run itself decided — see module docstring
GEO = F["geometry"]
M4Z = F["manual_4zqk"]
N_SLIDES = 11

# The sequence-identity margin that settled the chain assignment. Read out of
# CLAUDE.md, where the repo records it, for the same reason `build_carousel.py`
# reads its cross-campaign dock rates from there: a figure quoted in two places
# drifts, and this one is the whole point of slide 3.
CHAIN_MARGIN = re.search(
    r"decisive\s+(\d+)%-vs-(\d+)% margin on the real PD-L1 case",
    (ROOT / "CLAUDE.md").read_text(encoding="utf-8")).groups()


#: Slide 8 needs a colour key and `build_carousel.py` has no legend primitive.
#: Kept here rather than pushed into the shared stylesheet because only this deck
#: has a figure whose colours mean something; the swatch hexes are the ones
#: `build_campaign.py` uses for the same figure, so the two cannot disagree.
EXTRA_CSS = """
.legend { display:flex; gap:26px; justify-content:center; flex-wrap:wrap;
          max-width:none; font-family:var(--mono); font-size:17px;
          color:var(--muted); margin-top:14px }
.legend span { display:flex; align-items:center; gap:8px }
.legend b { width:15px; height:15px; display:inline-block; border-radius:2px }
figure.tall { min-height:2.7in }
"""


def img(name: str) -> str:
    path = ASSETS / f"{name}.webp"
    if not path.is_file():
        raise SystemExit(f"{path.relative_to(HERE)} missing — run the renders first")
    return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()


def page(i: int) -> str:
    return f"{i} / {N_SLIDES}"


# ── 1. the hook ──────────────────────────────────────────────────────────────
def s1() -> str:
    """The other entry point, named as such in the first line.

    `build_carousel.py`'s hook sells discovery ("we gave it one sentence about
    pain"). Anyone who already has a target reads that and concludes the tool is
    not for them, so this one opens on the case where the target is settled and
    the remaining questions are structural.
    """
    return slide(f"""
<div class="eyebrow">Little Protein Tiger</div>
<h1>You already know your target.</h1>
<div class="prompt">&ldquo;{F['query']}&rdquo;</div>
<p class="wide">No structure named, no epitope chosen. It ranked the nine solved
structures of PD-L1 by their measured interfaces, proved the chain it picked was
PD-L1 and not the nanobody bound to it, chose a nine-residue epitope, sized the
campaign from its own trial, and returned
<strong>{n(GATE['survivors'])} gated candidates</strong> from
{n(F['n_scored'])} refolds &mdash; <strong>${F['spend_usd']:.2f}</strong> of
model spend and {F['gpu_hours']['total']:.1f} GPU-hours, unattended.</p>
<figure><img src="{img('design_face')}" alt=""></figure>
""", dark=True, page=page(1))


# ── 2. which structure of it ─────────────────────────────────────────────────
def s2() -> str:
    """Nine structures, ranked on geometry before a model reads anything.

    This is the step the discovery deck has no equivalent of: with the target
    named, `target_resolve.build_candidate_table` computes every candidate
    complex's interface FIRST, so the skill argues over measurements rather than
    over abstracts. The columns are the measurements it is given.
    """
    rows = "".join(
        f'<tr class="{"chosen" if c["rank"] == 1 else ""}">'
        f'<td>{c["pdb_id"]}<br><span class="muted" style="font-size:18px">'
        f'{c["partner"]}</span></td>'
        f'<td class="n">{c["res"]:.2f}</td><td class="n">{n(c["bsa"])}</td>'
        f'<td class="n">{c["iface_res"]}</td><td class="n">{c["hbonds"]}</td>'
        f'<td class="n">{c["phob"]:.2f}</td></tr>'
        for c in F["candidates"][:5])
    alt = F["alternatives"][0]
    return slide(f"""
<div class="eyebrow">Stage 1 &nbsp;/&nbsp; which structure of it</div>
<h2>Nine solved structures. Ranked on the interface, not the citation count.</h2>
<table>
  <tr><th>Entry</th><th class="n">&Aring;</th><th class="n">BSA &Aring;&sup2;</th>
      <th class="n">Iface res</th><th class="n">H-bonds</th>
      <th class="n">Phob</th></tr>
  {rows}
</table>
<div class="box"><div class="h">Why not the runner-up</div>
<p class="wide" style="font-size:23px">{alt['pdb_id']} &mdash;
{angstrom(alt['why_not'])}</p></div>
<p class="wide muted" style="font-size:22px;margin-top:22px">Every figure in
that table is computed from coordinates before any model reads the list, so the
choice is argued over measurements rather than over abstracts &mdash;
{F['pdb_id']} on the largest, best-resolved description of the epitope every
characterised PD-L1 blocker uses.</p>
<div class="grow"></div>
""", page=page(2))


# ── 3. which molecule, actually ──────────────────────────────────────────────
def s3() -> str:
    """The guard, and the incident that bought it.

    A promo deck that only shows successes teaches a reader nothing about
    whether to trust the next run. This one is the most useful thing in the
    campaign: the failure is invisible to every other check, because a swapped
    chain yields real, correctly-numbered residues on the wrong protein.
    """
    lo, hi = CHAIN_MARGIN[1], CHAIN_MARGIN[0]
    return slide(f"""
<div class="eyebrow">Stage 1 &nbsp;/&nbsp; and which molecule, actually</div>
<h2>Chain A of this structure is not the target. It is the reagent.</h2>
<p class="wide">{F['pdb_id']} holds PD-L1 on chain {F['target_chain']} and the
{F['partner_name'].split(' (')[0]} used to crystallise it on chain
{F['partner_chain']}. Told to design against &ldquo;PD-L1&rdquo;, an earlier
version of this stage picked chain {F['partner_chain']} and put its hotspots on
the nanobody&rsquo;s own CDR loop.</p>
<div class="stats two">
  <div class="stat"><div class="n">{hi}%</div>
    <div class="k">chain {F['target_chain']} vs {F['uniprot']}</div></div>
  <div class="stat"><div class="n">{lo}%</div>
    <div class="k">chain {F['partner_chain']} vs {F['uniprot']}</div></div>
</div>
<div class="box"><div class="h">What runs now, before a trim is built</div>
<p class="wide" style="font-size:23px">Every modelled residue of the chosen chain
is aligned against the target&rsquo;s canonical UniProt sequence, and a mismatch
halts the run. Checking the residues cannot catch this &mdash; they are real and
correctly numbered, just on the wrong protein.</p></div>
<figure class="tall" style="margin-top:18px;min-height:3in">
<img src="{img('native_face')}" alt=""></figure>
<figcaption>The nanobody bound to PD-L1 in {F['pdb_id']}, down the epitope axis.
Designing against its loops would have been geometrically unremarkable and
biologically worthless.</figcaption>
""", page=page(3))


# ── 4. the epitope ───────────────────────────────────────────────────────────
def s4() -> str:
    """The epitope, listed rather than tabulated.

    Only 2 of the 9 hotspots carry a computed ddG, so a ddG-ranked table is two
    rows of data and seven of dashes — the list plus the two that were ranked
    says the same thing and leaves the surface render room to be legible.
    """
    ranked = sorted((h for h in F["hotspots"] if h.get("ddg") is not None),
                    key=lambda h: h["ddg"])
    listed = ", ".join(f'{h["name"]}{h["auth"]}' for h in F["hotspots"])
    strongest = ", ".join(f'{h["name"]}{h["auth"]} ({h["ddg"]:.1f})' for h in ranked)
    best = F["regions"][0]
    return slide(f"""
<div class="eyebrow">Stage 2 &nbsp;/&nbsp; which face of it</div>
<h2>Nine residues, on the face PD-1 itself binds.</h2>
<div class="stats">
  <div class="stat"><div class="n">{len(F['hotspots'])}</div>
    <div class="k">hotspots declared</div></div>
  <div class="stat"><div class="n">{len(F['regions'])}</div>
    <div class="k">candidate regions scored</div></div>
  <div class="stat"><div class="n sm">{best['rating'].lower()}</div>
    <div class="k">{best['hydrophobic_fraction'] * 100:.0f}% hydrophobic</div></div>
</div>
<p class="wide" style="margin-top:22px">{listed} &mdash; the
{best['short']}, chosen over one alternative face. {strongest} carry a computed
&Delta;&Delta;G. A region declares at most twelve: more is not stricter, because
RFD3&rsquo;s hit rate falls as the set grows.</p>
<figure class="tall" style="margin-top:14px"><img src="{img('epitope')}" alt="">
</figure>
<figcaption>The epitope with no binder present. Residue names and sidechain atoms
are read from the file at each position &mdash; a hotspot table can be internally
consistent and still describe a different structure of the same protein.</figcaption>
<p class="wide muted" style="font-size:22px;margin-top:20px">The trim then did
nothing, which is also a result: at {F['target_chain_length']} residues against a
220 budget the chain was kept whole, in {F['trim_segments']} segment
(<code>{F['contig']}</code>). Deciding a target already fits is as much that
stage&rsquo;s job as cutting one down.</p>
<div class="grow"></div>
""", page=page(4))


# ── 5. the trial ─────────────────────────────────────────────────────────────
def s5() -> str:
    """Never scale straight to production — the same beat as the other deck,
    because it is the one decision that separates a costed campaign from a
    hopeful one, and the numbers here are a different target's."""
    b = CAL["backbone_rate"]
    lo, hi, pt = b["p_low"] * 100, b["p_high"] * 100, b["p_hat"] * 100
    span = 12.0
    pos = lambda v: min(100.0, max(0.0, v / span * 100))       # noqa: E731
    ticks = "".join(
        f'<div class="tick" style="left:{pos(v):.1f}%">{v:.0f}%</div>'
        for v in (0, 3, 6, 9, 12))
    pess, cent = CAL["pessimistic"], CAL["central"]
    return slide(f"""
<div class="eyebrow">Stage 4 &nbsp;/&nbsp; never scale straight to production</div>
<h2>It measured its own hit rate before spending a day of GPU.</h2>
<p class="wide">A {n(CAL['diffused'])}-backbone trial, refolded
{CAL['n_seq']} sequences deep: <strong>{b['k']} of {n(b['n'])}</strong>
backbones produced a design clearing every gate at the excellence bar.</p>
<div class="scale">
  <div class="cap" style="left:{pos(pt):.1f}%">{pt:.2f}%</div>
  <div class="track"></div>
  <div class="ci" style="left:{pos(lo):.1f}%;width:{pos(hi) - pos(lo):.1f}%"></div>
  <div class="pt" style="left:{pos(pt):.1f}%"></div>
  {ticks}
</div>
<p class="wide muted" style="font-size:22px">95% Wilson interval
{lo:.2f}&ndash;{hi:.2f}%. At this rate a trial sees a handful of hits, and a
point estimate off a handful is not a number worth four days &mdash; so the
campaign is sized against the pessimistic end.</p>
<table>
  <tr><th>Basis</th><th class="n">Backbones</th><th class="n">Refolds</th>
      <th class="n">GPU-hours</th></tr>
  <tr><td>point estimate</td><td class="n">{n(cent['required_backbones'])}</td>
      <td class="n">{n(cent['required_refolds'])}</td>
      <td class="n">{cent['est_gpu_hours']:.1f}</td></tr>
  <tr class="chosen"><td>Wilson lower bound</td>
      <td class="n">{n(pess['required_backbones'])}</td>
      <td class="n">{n(pess['required_refolds'])}</td>
      <td class="n">{pess['est_gpu_hours']:.1f}</td></tr>
</table>
<div class="box"><div class="h">Verdict &nbsp;&middot;&nbsp; {CAL['verdict'].replace('_', ' ')}</div>
<p class="wide" style="font-size:23px">It also raised its own success bar from
{CAL['requested_bar']} to <strong>{CAL['bar_raised_to']}</strong>. An unusually
good target should not be sized to the same fixed bar as a marginal one, so the
bar climbs to the strictest rung the trial still supports and the budget still
affords.</p></div>
<div class="grow"></div>
""", page=page(5))


# ── 6. production and the gates ──────────────────────────────────────────────
def s6() -> str:
    # Strictest first, so the column reads as a funnel. Sorted, not listed:
    # a hand-written order silently stops being ascending the moment a
    # threshold moves.
    bars = "".join(
        f'<div class="gate"><div class="lab"><span>{k}</span>'
        f'<span class="muted">{pct:.1f}%</span></div>'
        f'<div class="bar"><i style="width:{pct:.1f}%"></i>'
        f'<b>{n(v)}</b></div></div>'
        for k, v, pct in sorted(GATE["alone"], key=lambda a: a[2]))
    p = F["production"]
    return slide(f"""
<div class="eyebrow">Stages 5&ndash;6 &nbsp;/&nbsp; production and gating</div>
<h2>Eight gates, each applied to every refold on its own.</h2>
<div class="stats">
  <div class="stat"><div class="n">{n(p['n_rfd3'])}</div>
    <div class="k">backbones diffused</div></div>
  <div class="stat"><div class="n">{n(p['n_rf3'])}</div>
    <div class="k">refolds scored</div></div>
  <div class="stat"><div class="n">{n(GATE['survivors'])}</div>
    <div class="k">survivors</div></div>
</div>
<p class="wide muted" style="font-size:22px;margin-top:22px">A cheap sidecar
prefilter kept {p['prefilter_rate'] * 100:.0f}% of the backbones
({n(p['n_filtered'])}), and each survivor was folded as
{CAL['n_seq']} sequences. Bars are the share of all {n(F['n_scored'])} refolds
each gate would pass <em>alone</em>; the {n(GATE['survivors'])} survivors are
the intersection, across {GATE['backbones']} distinct backbones.</p>
{bars}
<div class="grow"></div>
""", page=page(6))


# ── 7. confidence is not enough ──────────────────────────────────────────────
def s7() -> str:
    c8, c79 = GEO["contrast"]
    return slide(f"""
<div class="eyebrow">Stages 5&ndash;6 &nbsp;/&nbsp; why geometry, not confidence</div>
<h2>A model can be confident about an interface it invented.</h2>
<p class="wide">The refolder is never shown the docked pose, so ipTM reports
confidence in whatever interface the model chose &mdash; not in the one that was
designed for. Rank on it alone and you select confidently mis-docked binders.</p>
<div class="stats">
  <div class="stat"><div class="n">{n(GEO['n_high_iptm'])}</div>
    <div class="k">refolds over ipTM {GEO['iptm_bar']}</div></div>
  <div class="stat"><div class="n">{GEO['pct_docked']:.0f}%</div>
    <div class="k">of those docked &le; {GEO['dock_max']:.0f} &Aring;</div></div>
  <div class="stat"><div class="n">{GEO['n_confidently_misdocked']}</div>
    <div class="k">confident and mis-docked</div></div>
</div>
<div class="box"><div class="h">This target is the easy case</div>
<p class="wide" style="font-size:23px">{GEO['pct_docked']:.0f}% is unusually
good. Of designs over ipTM {GEO['iptm_bar']} on two other campaigns in this
repo, only <strong>{c8[1]}%</strong> ({c8[0]}) and <strong>{c79[1]}%</strong>
({c79[0]}) were docked on target at all. The dock-RMSD gate is the one doing the
work, and on a harder target it does nearly all of it.</p></div>
<p class="wide muted" style="font-size:22px;margin-top:24px">The commonest
single failure was geometric and it was rarely alone:
{n(GEO['dock_first_fail'])} refolds failed dock-RMSD first, and
{n(GEO['dock_first_fail_lowconf'])} of those
({GEO['dock_first_fail_lowconf_pct']:.1f}%) were below the ipTM gate as
well.</p>
<div class="grow"></div>
""", page=page(7))


# ── 8. the independent check ─────────────────────────────────────────────────
def s8() -> str:
    """The one slide that is not the pipeline's own arithmetic.

    Everything else here is the campaign grading itself against its own
    thresholds. This is a structure the run never opened, superposed by hand
    afterwards, and it is the only evidence on the deck that the epitope the
    pipeline chose is the epitope that matters — so it is labelled as manual,
    in the copy and not only in a footnote.
    """
    d = F["designs"][0]
    return slide(f"""
<div class="eyebrow">The check the run did not do</div>
<h2>It aimed at PD-1&rsquo;s own footprint, without ever being shown it.</h2>
<p class="wide">Nothing in the campaign opened {M4Z['pdb_id']}, the PD-1/PD-L1
complex. Superposed onto the top-ranked design afterwards
({M4Z['superpose_rmsd_A']} &Aring; over {M4Z['superpose_n_ca']} C&alpha;,
{M4Z['superpose_identity_pct']:.0f}% identity), the two binders land on the same
face.</p>
<div class="stats">
  <div class="stat"><div class="n">{M4Z['shared']}<span class="muted"
    style="font-size:34px">/{M4Z['pd1_contacts']}</span></div>
    <div class="k">PD-1 contacts also touched</div></div>
  <div class="stat"><div class="n">{M4Z['pct']}%</div>
    <div class="k">footprint overlap</div></div>
  <div class="stat"><div class="n">{M4Z['hotspots_that_are_pd1_contacts']}<span
    class="muted" style="font-size:34px">/{M4Z['n_hotspots']}</span></div>
    <div class="k">hotspots are PD-1 contacts</div></div>
</div>
<figure class="tall" style="margin-top:16px">
<img src="{img('footprint')}" alt=""></figure>
<p class="legend">
  <span><b style="background:#c0872b"></b>both ({M4Z['shared']})</span>
  <span><b style="background:#5e62b0"></b>PD-1 only ({len(M4Z['pd1_only'])})</span>
  <span><b style="background:#2f8f74"></b>design only
    ({M4Z['design_contacts'] - M4Z['shared']})</span>
  <span><b style="background:#9aa79d"></b>neither</span>
</p>
<figcaption><strong>Manual analysis, not part of the run</strong> &mdash; a UCSF
ChimeraX session done by hand afterwards, at a {M4Z['cutoff_A']} &Aring; cutoff.
An unversioned session cannot be extracted, so it is not presented as something
the pipeline produced.</figcaption>
<div class="box"><div class="h">Rank 1</div>
<p class="wide" style="font-size:23px">{d['len']} residues, ipTM
{d['iptm']:.3f}, dock-RMSD {d['dock']:.2f} &Aring;, binder pLDDT
{d['plddt']:.2f}, Rosetta &Delta;&Delta;G {d['rosetta_ddg']:.0f} REU. One of
{F['top_k_count']} shortlisted, each from a different backbone.</p></div>
<div class="grow"></div>
""", page=page(8))


# ── 9. the bill ──────────────────────────────────────────────────────────────
def s9() -> str:
    g = F["gpu_hours"]
    by = F["llm_calls_by_stage"]
    return slide(f"""
<div class="eyebrow">The bill</div>
<h2>Three reasoning stages. Five model calls. The rest is arithmetic.</h2>
<div class="stats">
  <div class="stat"><div class="n">${F['spend_usd']:.2f}</div>
    <div class="k">model spend, cap ${F['budget_cap_usd']:.0f}</div></div>
  <div class="stat"><div class="n">{g['total']:.1f}</div>
    <div class="k">GPU-hours, one card</div></div>
  <div class="stat"><div class="n">{F['wall_hours']:.0f}</div>
    <div class="k">hours wall clock</div></div>
</div>
<table>
  <tr><th>Stage</th><th class="n">Model calls</th><th class="n">GPU-h</th></tr>
  <tr><td>target intel &mdash; structures ranked, chain verified</td>
      <td class="n">{by['target_intel']}</td><td class="n">&mdash;</td></tr>
  <tr><td>interface &mdash; the epitope</td>
      <td class="n">{by['interface']}</td><td class="n">&mdash;</td></tr>
  <tr><td>trim, spec, pilot</td><td class="n">0</td>
      <td class="n">{g['pilot']:.2f}</td></tr>
  <tr><td>calibration &mdash; the trial and its verdict</td><td class="n">0</td>
      <td class="n">{g['calibration']:.2f}</td></tr>
  <tr><td>production, gating, ranking</td><td class="n">0</td>
      <td class="n">{g['production']:.2f}</td></tr>
  <tr><td>summary &mdash; the write-up</td>
      <td class="n">{by['binder_summary']}</td><td class="n">&mdash;</td></tr>
</table>
<div class="box"><div class="h">Two of those five calls were a bug</div>
<p class="wide" style="font-size:23px">The interface stage ran
{by['interface']} times because the query it was handed never named the entry
the orchestrator had <em>already downloaded</em>: given a description and no
accession, the skill twice concluded no structure existed and asked for one.
The query now always states the id and chains. This run also drove
<code>claude-sonnet-5</code> (${F['models']['claude-sonnet-5']:.2f} of the
${F['spend_usd']:.2f}); the default is now <code>{F['default_model']}</code>,
about four times cheaper on input &mdash; and no model is built in, since
Claude, Gemini and GPT each drive the same loop, tools and cost ledger.</p></div>
<div class="grow"></div>
""", page=page(9))


# ── 10. what it does not do, and what has moved since ────────────────────────
def s10() -> str:
    """The coda, and the one thing a reader cannot check for themselves.

    A campaign published three weeks after it ran invites exactly one question —
    would today's code still decide this? — so it is answered on the deck with
    the measured answer rather than left to trust.
    """
    cur = F["gate_current"]
    return slide(f"""
<div class="eyebrow">What it does not do</div>
<h2>Nothing here has been near a bench.</h2>
<p class="wide">Every figure on this deck is computational: folding confidence,
interface geometry, buried surface area, Rosetta terms over
{F['rosetta_scored']} relaxed poses. Designs like these need expression,
purification and a binding assay before any of it is a result. The trial, the
gates and the ranking exist to make that shortlist small and defensible &mdash;
not to skip it.</p>
<div class="rule"></div>
<h2 style="font-size:44px;margin-top:0">And the code has moved since the run.</h2>
<p class="wide" style="font-size:25px">This campaign ran
{F['started']}&ndash;{F['ended']}. One gate has been deliberately loosened
since: <code>hotspot_engagement</code> went from 1.0 to
{F['hotspot_engagement_now']}, because requiring every hotspot rejected refolds
for missing residues their own backbone never targeted. Re-gating the same
{n(F['n_scored'])} refolds through today&rsquo;s code:</p>
<table>
  <tr><th>&nbsp;</th><th class="n">Survivors</th><th class="n">Backbones</th>
      <th class="n">Top 20</th></tr>
  <tr><td>as it ran, {F['ended']}</td>
      <td class="n">{n(GATE['survivors'])}</td>
      <td class="n">{GATE['backbones']}</td>
      <td class="n">&mdash;</td></tr>
  <tr><td>re-gated today</td><td class="n">{n(cur['survivors'])}</td>
      <td class="n">{cur['backbones']}</td>
      <td class="n">same 20</td></tr>
</table>
<p class="wide muted" style="font-size:22px;margin-top:20px">Same twenty
designs, in the same order but for one adjacent swap, and the trial&rsquo;s
verdict and self-raised bar are unchanged. The figures above are the run&rsquo;s
own, at the threshold in force when it ran &mdash; the shortlist shown on this
deck came out of that gate, so mixing the two would be worse than stating
both.</p>
<div class="grow"></div>
""", page=page(10))


# ── 11. terms ────────────────────────────────────────────────────────────────
def s11() -> str:
    return slide(f"""
<div class="eyebrow">Little Protein Tiger</div>
<h1>Free for any non&#8209;commercial use.</h1>
<p class="wide lede" style="max-width:34ch">Source-available under PolyForm
Noncommercial 1.0.0. Academic and personal use, no fee, no negotiation.
Commercial use needs a separate licence.</p>
<div class="rule"></div>
<table>
  <tr><td>Entry point shown here</td>
      <td><code>--workflow binder</code>, target named</td></tr>
  <tr><td>The other one</td>
      <td><code>--workflow ppi</code>, one sentence, no target</td></tr>
  <tr><td>And a third</td>
      <td><code>--workflow structure</code>, your own file</td></tr>
  <tr><td>This campaign, written up in full</td>
      <td><code>campaign_pdl1.html</code></td></tr>
</table>
<p class="wide" style="margin-top:34px">Four campaigns documented end to end,
every figure extracted from the run that produced it &mdash; including the ones
that make the tool look worse.</p>
<div class="grow"></div>
""", dark=True, page=page(11))


def main() -> int:
    if CHROME is None:
        raise SystemExit("no Chrome/Chromium on PATH")
    # Three headlines spell a count as a word, which the facts file cannot
    # update. `build_carousel.py` does the same ("Three candidates"), so the
    # convention stands — but a word that has quietly stopped matching its own
    # data is exactly the failure every other figure here is extracted to
    # avoid, so it fails the build instead.
    for word, count, what in (("nine", len(F["candidates"]), "candidate structures"),
                              ("nine", len(F["hotspots"]), "hotspots")):
        if count != 9:
            raise SystemExit(
                f"the deck says '{word}' {what} and the facts say {count} — "
                f"reword slides 1, 2 and 4 before shipping it")

    slides = [s1(), s2(), s3(), s4(), s5(), s6(), s7(), s8(), s9(), s10(), s11()]
    if len(slides) != N_SLIDES:
        raise SystemExit(f"{len(slides)} slides but the footers say {N_SLIDES}")
    html = (f'<meta charset="utf-8"><title>Little Protein Tiger &mdash; PD-L1</title>'
            f'{GOOGLE_FONTS_LINK}<style>{CSS}{EXTRA_CSS}</style>' + "".join(slides))

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
    print(f"  {OUT.relative_to(ROOT)}  {len(slides)} slides  "
          f"{OUT.stat().st_size / 1024:.0f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
