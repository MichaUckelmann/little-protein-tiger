#!/usr/bin/env python3
"""Build the PPI-track showcase page from projects/mesothelioma_showcase.

One sentence in, twenty ranked designs out — the first campaign to run the whole
PPI -> foundry bridge unattended on GPU. Facts come from that project's stage
markdowns, calibration.json, scoring/filter_stats.txt and scoring/top_k.csv; the
BoltzGen comparison from the archived outputs/e2e_mesothelioma run on the same
target.  Rebuild:  python docs/showcase/build_ppi.py
"""
from __future__ import annotations
import base64, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _common import head as _mkhead
OUT = HERE / "ppi_discovery.html"
CSS = (HERE / "campaign_pdl1.html").read_text().split("<style>")[1].split("</style>")[0]
CSS += """
.tier{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.06em;
  padding:2px 7px;border-radius:2px;background:var(--sunk);border:1px solid var(--rule-2);
  color:var(--muted);white-space:nowrap}
.tier.v{background:var(--good-bg);color:var(--good-ink);border-color:transparent}
td.why{font-size:.84rem;color:var(--ink-2)}
td.unc{font-size:.82rem;color:var(--muted);font-style:italic}
.vs{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule)}
@media(min-width:760px){.vs{grid-template-columns:1fr 1fr}}
.vs>div{background:var(--surface);padding:18px 20px;display:grid;gap:6px;align-content:start}
.vs h3{margin:0 0 4px}
.vs .n{font-family:var(--display);font-weight:500;font-size:1.5rem;line-height:1.1}
.vs .m{font-family:var(--mono);font-size:12px;color:var(--muted)}
.vs p{font-size:.86rem;color:var(--muted);margin:6px 0 0}
"""

def img(name):
    return "data:image/webp;base64," + base64.b64encode(
        (HERE / "assets" / f"{name}.webp").read_bytes()).decode()

TIERS = [
    ("VALIDATED", True, "YAP1 / TEAD1", "3KYS · 5GN0",
     "Mesothelioma xenograft regression and objective clinical activity confirmed upon "
     "disrupting the YAP-TEAD transcriptional axis.",
     "TAZ co-activator redundancy and MAPK/FOSL1-mediated acquired resistance."),
    ("BIOLOGICALLY_JUSTIFIED", False, "VGLL4 / TEAD4", "5NO6 · 5GN0",
     "VGLL4-mimetic peptides competitively displace YAP from TEAD and suppress tumour "
     "growth in vivo.",
     "Need for broad pan-TEAD cross-reactivity and delivery optimisation."),
    ("PATHWAY_INFERRED", False, "FOSL1 / TEAD", "—",
     "FOSL1 upregulation mediates adaptive resistance by restoring TEAD chromatin "
     "recruitment under selective pressure.",
     "No high-resolution co-crystal coordinates in the corpus."),
]
FUNNEL = [
    ("RFD3 backbones", 392, "diffused against the 12-residue hotspot patch"),
    ("Cleared the prefilter", 338, "86.2% — the multi-segment trim held"),
    ("solubleMPNN sequences", 1352, "4 per surviving backbone"),
    ("RF3 refolds", 1352, "every sequence refolded from scratch"),
    ("Cleared every hard gate", 317, "23.4%, across 169 distinct backbones"),
    ("Ranked and reported", 20, "MMR-diversified top-K"),
]
GATES = [("no steric clash", 391, 47.9), ("hotspot_engagement ≥ 0.75", 342, 61.2),
         ("binder_rmsd_dock ≤ 5 Å", 206, 84.8), ("ipTM ≥ 0.5", 75, 77.8),
         ("binder_rmsd_fold ≤ 2 Å", 20, 90.8), ("binder_pLDDT ≥ 0.75", 1, 98.4)]
TOP = [(1, "13_model_1_b0_d1", 0.914, 0.751, 0.63, 0.807, -87.3, 86),
       (2, "49_model_2_b0_d2", 0.914, 0.765, 0.71, 0.809, -68.8, 86),
       (3, "55_model_0_b0_d3", 0.937, 0.794, 1.08, 0.810, -63.9, 70)]
HOTSPOTS = "Gln246, Asp249, Lys274, Trp276, Phe314, Val318, Tyr346, Lys350, Glu353, Leu366, Ile404, Tyr406"


def tier_rows():
    return "".join(
        f'<tr><td><span class="tier{" v" if v else ""}">{t}</span></td>'
        f'<td class="m">{c}</td><td class="num">{p}</td>'
        f'<td class="why">{w}</td><td class="unc">{u}</td></tr>'
        for t, v, c, p, w, u in TIERS)


def bar_funnel():
    mx = max(v for _, v, _ in FUNNEL)
    rows, y, RH = [], 0, 34
    for label, val, note in FUNNEL:
        w = max(3.0, val / mx * 660)
        rows.append(f'<g class="mk" tabindex="0"><title>{label}: {val:,} — {note}</title>'
                    f'<rect x="0" y="{y}" width="{w:.1f}" height="{RH}" rx="4" fill="var(--mark-a)"/>'
                    f'<text class="v" x="{w+12:.1f}" y="{y+RH/2+5}">{val:,}</text>'
                    f'<text class="k" x="0" y="{y-6}">{label}</text></g>')
        y += RH + 28
    return f'<svg viewBox="0 -18 860 {y}" role="img" class="chart" aria-label="Production funnel">{"".join(rows)}</svg>'


def bar_gates():
    mx = max(v for _, v, _ in GATES)
    rows, y, RH = [], 0, 30
    for label, dropped, alone in GATES:
        w = max(2.0, dropped / mx * 400)
        rows.append(f'<g class="mk" tabindex="0"><title>{label} — first to fail for {dropped:,} '
                    f'refolds; {alone}% of all 1,352 would pass it alone</title>'
                    f'<text class="k r" x="248" y="{y+RH/2+5}">{label}</text>'
                    f'<rect x="260" y="{y+5}" width="{w:.1f}" height="{RH-10}" rx="4" fill="var(--mark-b)"/>'
                    f'<text class="v" x="{260+w+10:.1f}" y="{y+RH/2+5}">{dropped:,}</text></g>')
        y += RH + 8
    return f'<svg viewBox="0 -6 760 {y}" role="img" class="chart" aria-label="Refolds dropped by first failing gate">{"".join(rows)}</svg>'


def design_cards():
    out = []
    for rank, name, iptm, ips, dock, plddt, ddg, ln in TOP:
        out.append(f'''<article class="card">
          <div class="card-b">
            <div class="card-h"><span class="rk">rank {rank}</span><code>{name}</code></div>
            <dl class="mini">
              <div><dt>ipTM</dt><dd>{iptm:.3f}</dd></div>
              <div><dt>ipSAE</dt><dd>{ips:.3f}</dd></div>
              <div><dt>dock RMSD</dt><dd>{dock:.2f} Å</dd></div>
              <div><dt>pLDDT</dt><dd>{plddt:.3f}</dd></div>
              <div><dt>Rosetta ΔΔG</dt><dd>{ddg:.1f}</dd></div>
              <div><dt>length</dt><dd>{ln} aa</dd></div>
            </dl>
          </div></article>''')
    return "".join(out)


_HEAD = _mkhead(
    "PPI Discovery Track",
    "One sentence about a disease, twenty ranked binder designs out: Little Protein "
    "Tiger's PPI track picking a target, proving it from the literature, and running a "
    "GPU campaign against it unattended.",
    "ppi_discovery.html", "ppi")

HTML = f"""{_HEAD}
<style>{CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · ppi track · 27–28 Aug 2026</p>
    <h1>One sentence about a disease, twenty designed binders out</h1>
  </div>
  <p class="lede">No target was named. The pipeline chose YAP1/TEAD1 from the literature,
  argued for it, cut the structure down, sized its own campaign from a measured hit rate,
  and ran it — 22 GPU-hours and 79 cents of model spend later, 317 designs had cleared
  every gate. This is that run.</p>
  <div class="term"><span class="p">$</span> python scripts/run_pipeline.py --workflow ppi \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--query "Design cancer therapeutics to target key nodes in mesothelioma." \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--project mesothelioma_showcase --budget 5.00</div>
  <figure class="hero-fig">
    <img src="{img('meso_design')}" alt="The top-ranked designed mini-protein bound to TEAD1, covering the twelve hotspot residues.">
    <p class="legend">
      <span><b style="background:#2f8f74"></b>designed binder, 86 aa</span>
      <span><b style="background:#9aa79d"></b>TEAD1</span>
      <span><b style="background:#c0872b"></b>the twelve hotspots it was asked to cover</span>
    </p>
    <figcaption>The rank-1 design, refolded by RF3 from sequence alone, on the YAP-binding
    face of TEAD1. Dock RMSD to the intended site: <strong>0.63 Å</strong>. All twelve
    hotspots engaged.</figcaption>
  </figure>
  <div class="stats">
    <div class="stat"><b>$0.79</b><span>total model spend</span></div>
    <div class="stat"><b>22</b><span>GPU-hours</span></div>
    <div class="stat"><b>317</b><span>designs through every gate</span></div>
    <div class="stat"><b>0.937</b><span>best ipTM</span></div>
    <div class="stat"><b>4</b><span>LLM calls in the whole run</span></div>
  </div>
  <p class="verdict">GO — 20 ranked designs, every one engaging all twelve hotspots</p>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 0 · pathway-expert</p>
    <h2>Three candidate interactions, ranked by the evidence behind them</h2></div>
  <p>The pathway stage does not return one answer. It returns tiered options, each with
  the evidence it rests on <em>and</em> the reason it might fail — and the tiers are a
  closed set, so "this is a guess" cannot be quietly promoted to "this is validated" by
  confident prose.</p>
  <div class="tw"><table>
    <thead><tr><th>tier</th><th>complex</th><th class="num">structures</th>
      <th>evidence</th><th>stated uncertainty</th></tr></thead>
    <tbody>{tier_rows()}</tbody></table></div>
  <div class="note-box" style="margin-top:18px"><p>The uncertainty it flagged on the
  winner — <em>TAZ co-activator redundancy</em> — is independently visible in the DepMap
  data behind the <a href="corpus_explorer.html">corpus-explorer page</a>: YAP1 and WWTR1
  (TAZ) are co-essential at r&nbsp;=&nbsp;+0.02, meaning losing one does not substitute
  for the other. Two different sources, same warning.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stages 1–2 · literature and structure</p>
    <h2>Is there mutagenesis behind the interface, or only a picture of it?</h2></div>
  <div class="two">
    <div>
      <p>A large buried surface means nothing on its own. The literature stage went
      looking for the numbers that say which residues carry the binding energy, and came
      back with a native affinity of <strong>K<sub>d</sub> ≈ 18 nM</strong>, alanine-scanning
      hotspots at F69, R89, L91 and F95, and precedent for peptidomimetic druggability —
      every claim carrying its DOI. Both citations it made were verified against the local
      corpus; none were invented.</p>
      <p>The structure stage then worked on the coordinates rather than the prose. On
      PDB <strong>3KYS</strong> it measured 3,402 Å² of buried surface, confirmed TEAD1 as
      chain A by sequence identity — no upstream stage is trusted to emit chain letters —
      and selected twelve hotspots: {HOTSPOTS}.</p>
    </div>
    <figure class="fig">
      <img src="{img('meso_native')}" alt="The YAP1 peptide wrapping the TEAD1 surface in 3KYS, with the twelve selected hotspots highlighted.">
      <figcaption><strong>3KYS</strong> — what the native interaction looks like. The YAP1
      peptide (grey) wraps the TEAD1 surface; ochre marks the twelve residues the design
      was aimed at.</figcaption>
    </figure>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 4 · calibration</p>
    <h2>The campaign sized itself, then raised its own bar</h2></div>
  <p>A 100-backbone pilot proved the machinery in 1.9 h. Calibration then ran the
  experiment that decides the campaign: 481 backbones, 1,924 refolds, counting how many
  designs actually cleared the success bar rather than assuming a rate.</p>
  <p>It also cleared a first for this pipeline. TEAD1 needed no trimming — all 207
  modelled residues were already inside budget — but 3KYS is itself missing residues
  230–238 and 344, disordered loops with no density, so the target reaches RFD3 in
  <strong>three pieces</strong>. The target is fixed conditioning rather than something
  the model builds, so the gaps are not its problem; what they do change is the
  prefilter's chain-break count, which is <em>derived</em> from the segment number rather
  than hardcoded. A hardcoded limit would have rejected every design. It held —
  <strong>86.2% of backbones cleared the prefilter</strong> — and the refolded target
  superposes onto the deposited chain at <strong>0.11 Å</strong> Cα RMSD, so the three
  pieces have not drifted apart.</p>
  <div class="two">
    <div>
      <p>The measured backbone hit rate was <strong>18.50%</strong> (89 of 481, 95% Wilson
      interval 15.29–22.22%) — three and a half times PD-L1's 5.31%. The requested bar was
      ipTM&nbsp;&gt;&nbsp;0.7; because the trial supported a harder one within budget, the
      stage <strong>raised it to 0.85 by itself</strong> and sized to that. It only ever
      moves the bar up.</p>
      <p>Sizing is done on the pessimistic end of the interval, never the point estimate:
      ~1,308 refolds, ~4 GPU-h, ~3 GB, comfortably inside the 120 h / 120 GB budget.
      Verdict: <strong>SCALE_UP</strong>, and the run paused there for a human, which is
      the point of the gate.</p>
    </div>
    <div class="chart-wrap"><h3>Refolds dropped by the first gate they failed</h3>
      {bar_gates()}
      <figcaption>Of 1,352 production refolds. Hover a bar for what that gate would keep
      on its own.</figcaption></div>
  </div>
  <div class="note-box" style="margin-top:20px"><p>The failure mode here is different from
  PD-L1's, and more tractable. There, dock-RMSD did the killing — designs confidently
  docked in the wrong place. Here <strong>84.8% pass dock-RMSD</strong> and the leading
  killer is steric clash. The designs are landing on the right face in the right pose and
  being rejected on packing.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 5 · production and scoring</p>
    <h2>What twenty-two GPU-hours produced</h2></div>
  <div class="chart-wrap"><h3>Production funnel</h3>{bar_funnel()}
    <figcaption>Every count read from disk by directory scan, never from a log line. The
    MPNN and RF3 rows exceed the backbone row because each surviving backbone gets four
    sequences, and each is refolded independently.</figcaption></div>
  <p style="margin-top:22px"><strong>317 designs cleared every hard gate — 23.4% of the
  refolds</strong>, across 169 distinct backbones. For scale, the PD-L1 campaign returned
  12.3% from four times the compute. Rosetta relax and InterfaceAnalyzer ran afterwards on
  300 of the survivors and enter the composite only; a mis-docked pose is still a physical
  pose, and Rosetta will happily score one.</p>
  <div class="cards">{design_cards()}</div>
  <p style="margin-top:20px">Every one of the top twenty engages <strong>all twelve
  hotspots</strong>, with ipTM 0.90–0.94 and dock-RMSD 0.63–1.48 Å across binder lengths
  from 70 to 86 residues and distinct backbone families — so the diversification is
  returning genuinely different solutions, not twenty variations of one hit.</p>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">The same question, twice</p>
    <h2>What changed when the PPI track was rewired</h2></div>
  <p>An earlier run asked this pipeline the same thing about mesothelioma and reached the
  same target — YAP1/TEAD1 on 3KYS — through the older BoltzGen path. Putting them side by
  side is the clearest measure of what the foundry bridge bought.</p>
  <div class="vs">
    <div><h3>BoltzGen path (archived)</h3>
      <div class="n">100 → 100 → 20</div>
      <div class="m">designs → survivors → top-K</div>
      <p>Every design survived every filter and none were dropped. A funnel that rejects
      nothing has not demonstrated that its filters work — and at 100 designs there is no
      measured hit rate to size anything from. Best design: a 15-mer at ipTM 0.591.</p></div>
    <div><h3>foundry bridge (this run)</h3>
      <div class="n">392 → 1,352 → 317 → 20</div>
      <div class="m">backbones → refolds → gated → top-K</div>
      <p>Sized from a measured 18.5% hit rate with a confidence interval, gated on six
      criteria with the attrition recorded per criterion, and ranked on a composite
      including Rosetta terms. Best design: an 86-mer at ipTM 0.914, dock-RMSD 0.63 Å.</p></div>
  </div>
  <div class="note-box" style="margin-top:20px"><p>This run is also the first to prove the
  bridge end to end on GPU. Until it finished, the hand-off from PPI discovery into the
  binder track's stage machine was verified only by unit tests with the campaign stubbed
  out — the pipeline's own documentation listed "no real end-to-end GPU run has proven the
  bridge's output quality" as a known gap. It no longer does.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">What it cost, and what it is not</p>
    <h2>Four model calls</h2></div>
  <p>Four LLM calls in the entire run — pathway, literature, structure, and the final
  review — for <strong>$0.79 against a $5.00 cap</strong>. Everything between them is
  deterministic Python: the trim, the spec, campaign planning, gating, scoring, ranking.
  The GPU did 1.9 h of pilot, 9.5 h of calibration and 7.4 h of production.</p>
  <div class="note-box"><p><strong>Every design here is an unvalidated computational
  hypothesis.</strong> Nothing has been expressed, purified or measured. A high ipTM says
  a folding model is confident about an interface it chose; a low dock-RMSD says the
  binder is where it was asked to be. Neither is an affinity, and neither is evidence that
  this would disrupt YAP1–TEAD1 in a cell. Anyone synthesising one of these sequences is
  responsible for screening it — see <code>docs/responsible-use.md</code>.</p></div>
</section>

<footer>
  <p>Read from <code>projects/mesothelioma_showcase</code> — the stage reports,
  <code>calibration.json</code>, <code>scoring/filter_stats.txt</code> and
  <code>scoring/top_k.csv</code> — with the BoltzGen comparison from
  <code>outputs/e2e_mesothelioma</code>. Structure images rendered with UCSF ChimeraX from
  the campaign's own RF3 refolds and from PDB 3KYS; hotspots shown are the twelve the
  interface stage selected, mapped through the refold's own numbering. Rebuild with
  <code>python docs/showcase/build_ppi.py</code>.</p>
</footer>

</div>
"""
OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
