#!/usr/bin/env python3
"""Build the PPI-discovery showcase page from outputs/e2e_mesothelioma.

Facts come from that run's 00_pathway.md, 01_literature.md, 02_structure.md and
05_ranking/filter_stats.txt.  Rebuild:  python docs/showcase/build_ppi.py
"""
from __future__ import annotations
import base64, pathlib

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "ppi_discovery.html"
CSS = (HERE / "campaign_pdl1.html").read_text().split("<style>")[1].split("</style>")[0]
CSS += """
.tier{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.06em;
  padding:2px 7px;border-radius:2px;background:var(--sunk);border:1px solid var(--rule-2);
  color:var(--muted);white-space:nowrap}
.tier.v{background:var(--good-bg);color:var(--good-ink);border-color:transparent}
td.why{font-size:.84rem;color:var(--ink-2)}
td.unc{font-size:.82rem;color:var(--muted);font-style:italic}
"""

def img(name):
    return "data:image/webp;base64," + base64.b64encode(
        (HERE / "assets" / f"{name}.webp").read_bytes()).decode()

TIERS = [
    ("VALIDATED", True, "YAP1 / TEAD1", "3KYS · 5GN0 · 8P0M",
     "Mesothelioma xenograft regression confirmed upon YAP-TEAD inhibition; VT3989 shows "
     "57% clinical activity in refractory MPM; VGLL4-mimetic peptides suppress tumour "
     "growth via this interface.",
     "TAZ paralog redundancy may allow escape; MAPK/FOSL1 bypass resistance documented "
     "under chronic inhibition."),
    ("BIOLOGICALLY_JUSTIFIED", False, "MOB1A / LATS1", "5BRK",
     "CRISPR KO of LATS1/2 abolishes YAP/TAZ phosphorylation; the pMob1–LATS1 co-crystal "
     "defines a stabilizable interface upstream of YAP.",
     "Bi-allelic LATS1 deletion in a significant MPM fraction limits applicability; no "
     "therapeutic precedent for this PPI in corpus."),
    ("PATHWAY_INFERRED", False, "WWTR1 / TEAD4", "5GN0",
     "TAZ compensates for YAP1 loss; dual YAP/TAZ blockade via a shared TEAD surface "
     "addresses paralog escape.",
     "No MPM-specific WWTR1 genetic dependency data in corpus; the TAZ/TEAD interface may "
     "differ subtly from the YAP/TEAD1 one used for design."),
]
EVIDENCE = [
    ("NF2 inactivated in 30–40% of malignant pleural mesothelioma", "10.1111/jcmm.18330"),
    ("23.2% of TCGA cases carry truncating NF2 alterations", "10.1016/j.celrep.2018.10.001"),
    ("YAP1 F69A → ~400-fold loss of TEAD affinity (ΔΔG ≈ 3.48 kcal/mol)", "10.7554/eLife.25068"),
    ("YAP1 L91A → ΔΔG = 4.40 kcal/mol", "10.7554/eLife.25068"),
    ("R89A : D272A double mutant → ΔΔG<sub>int</sub> = −3.49 kcal/mol", "10.7554/eLife.25068"),
    ("VT-103 binds the TEAD1–VGLL4 ternary complex at K<sub>d</sub> = 41.6 nM (SPR)",
     "10.1073/pnas.2425984122"),
]
HOTSPOTS = ["Asp249", "Trp276", "Phe314", "Val322", "Tyr346", "Tyr406"]


def tier_rows():
    return "".join(
        f'<tr><td><span class="tier{" v" if v else ""}">{t}</span></td>'
        f'<td class="m">{c}</td><td class="num">{p}</td>'
        f'<td class="why">{w}</td><td class="unc">{u}</td></tr>'
        for t, v, c, p, w, u in TIERS)


def ev_rows():
    return "".join(
        f'<tr><td class="why">{c}</td>'
        f'<td class="num"><a href="https://doi.org/{d}">{d}</a></td></tr>'
        for c, d in EVIDENCE)


HTML = f"""<title>PPI Discovery Track</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · ppi track · discovery stages</p>
    <h1>From a disease name to a specific groove on a specific protein</h1>
  </div>
  <p class="lede">The binder track starts when you already know your target. This is the
  half that runs before that: given only a disease, pick the protein–protein interaction
  worth attacking, prove it from the literature, and hand a structural site to the design
  stages. One run, one sentence in.</p>
  <div class="term"><span class="p">$</span> python scripts/run_pipeline.py --workflow ppi \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--query "Design cancer therapeutics to target key nodes in mesothelioma." \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--project mesothelioma</div>
  <figure class="hero-fig">
    <img src="{img('tead1_yap1')}" alt="The YAP1 peptide wrapping the TEAD1 surface in PDB 3KYS, with the six selected hotspot residues highlighted.">
    <p class="legend">
      <span><b style="background:#2f8f74"></b>YAP1 — the partner to displace</span>
      <span><b style="background:#9aa79d"></b>TEAD1 — the design target</span>
      <span><b style="background:#c0872b"></b>the six hotspots the structure stage chose</span>
    </p>
    <figcaption>Where the three discovery stages landed: PDB <strong>3KYS</strong>, TEAD1
    chain A as the target, the YAP1 Ω-loop and α-helix as the interaction to break.
    3,402 Å² buried — tractability rated <em>excellent</em>.</figcaption>
  </figure>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 0 · pathway-expert</p>
    <h2>Three candidate interactions, ranked by how much evidence stands behind them</h2></div>
  <p>The pathway stage does not return one answer. It returns tiered options, each with the
  evidence it rests on <em>and</em> the reason it might fail — and the tier names are a closed
  set, so "this is a guess" cannot be quietly upgraded into "this is validated" by confident
  prose.</p>
  <div class="tw"><table>
    <thead><tr><th>tier</th><th>complex</th><th class="num">structures</th>
      <th>evidence</th><th>stated uncertainty</th></tr></thead>
    <tbody>{tier_rows()}</tbody></table></div>
  <p style="margin-top:16px">It picked YAP1/TEAD1 and carried the other two forward as
  recorded alternatives rather than discarding them. The key uncertainty on the winner —
  TAZ paralog redundancy — is the same one the corpus-explorer session surfaced
  independently from DepMap co-essentiality.</p>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 1 · molecular-biology-expert</p>
    <h2>Is there mutagenesis behind the interface, or only a picture of it?</h2></div>
  <div class="two">
    <div>
      <p>A large buried surface means nothing on its own. The literature stage goes looking
      for the numbers that say which residues actually carry the binding energy — and it is
      required to cite each one, by DOI, from the local corpus.</p>
      <p>What it found here is unusually strong: single alanine substitutions on the YAP1
      side that cost 3.5–4.4 kcal/mol, a double-mutant cycle giving a coupling energy, and
      a clinical-stage inhibitor with a measured K<sub>d</sub>. That is what moved this
      interface from "large" to <em>tractable</em>.</p>
    </div>
    <div class="tw"><table>
      <thead><tr><th>finding</th><th class="num">source</th></tr></thead>
      <tbody>{ev_rows()}</tbody></table></div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 2 · complex-structure-analysis</p>
    <h2>Six residues, in the structure's own numbering</h2></div>
  <p>The structure stage computes buried area per residue from the actual coordinates and
  returns a hotspot table in both author and label numbering, because the design tools
  downstream disagree about which one they want. On TEAD1 it selected
  {", ".join(f"<strong>{h}</strong>" for h in HOTSPOTS)} — the interface groove the
  YAP1 Ω-loop drops into.</p>
  <div class="note-box"><p>Note what changed between stages. The pathway stage's handoff
  suggested TEAD1 would be chain B. The structure stage resolved chain identity from the
  mmCIF itself and assigned TEAD1 to chain <strong>A</strong> — no upstream skill is
  trusted to emit chain letters, because getting this backwards means designing against
  the wrong molecule. Two guards check the assignment before anything is built from it.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stages 3–6 · design and analysis</p>
    <h2>What came out, and what it does not prove</h2></div>
  <p>This run used the BoltzGen design path — it predates the switch that now sends a
  PPI-discovered target into the same RFD3 → solubleMPNN → RF3 machine the binder track
  uses. 100 designs were generated, ranked on a composite of design-to-target ipTM,
  interface PAE, hotspot SASA change and complex pLDDT, and the top 20 reported. The
  leading design was a 15-mer, <code>SGTTVDGLLATLEGK</code>, at composite +10.38
  (ipTM 0.591, interface PAE 6.03, hotspot ΔSASA 369.3 Å²).</p>
  <div class="note-box"><p><strong>Read the funnel honestly: 100 designs in, 100 survivors,
  zero dropped.</strong> At this scale the hard gates were not discriminating — which is
  exactly the situation the binder track's calibration stage was later built to detect and
  refuse. A run that filters nothing has not demonstrated that its filters work. Compare
  the PD-L1 campaign, where 5,824 refolds became 715 survivors and the reason each one died
  is recorded.</p></div>
  <p>What this page shows well is the <em>discovery</em> half: a disease name became a
  tiered set of candidate interactions, then a specific interface with mutagenesis behind
  it, then six numbered residues on a named chain — each step cited, each handoff on disk.
  What it does not show is a validated binder, and nothing here has been near a bench.</p>
</section>

<footer>
  <p>Read from <code>outputs/e2e_mesothelioma</code> — <code>00_pathway.md</code>,
  <code>01_literature.md</code>, <code>02_structure.md</code> and
  <code>05_ranking/filter_stats.txt</code>. Structure image rendered with UCSF ChimeraX
  from <code>data/structures/3KYS_ba1.cif</code>. Rebuild this page with
  <code>python docs/showcase/build_ppi.py</code>.</p>
</footer>

</div>
"""
OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
