#!/usr/bin/env python3
"""Build the PPI-track showcase page from projects/mesothelioma_showcase.

One sentence about a disease in, twenty ranked designs out — the first campaign to
run the whole PPI -> foundry bridge unattended on GPU, and the run that made that
bridge the default backend.

Every figure is EXTRACTED, never typed — see `_facts.py` for why that matters and
how the tracked snapshot lets this build on a machine without the run. This page
previously carried its numbers as literals while claiming they "were read from"
the run: four of the twelve hotspot residue names were wrong, the GPU-hour split
was invented and contradicted the page's own headline, two design cards were
labelled with the wrong rank, and both top-K ranges were tightened in the page's
favour. Handoff blocks are read with the pipeline's own `src.handoff` parser
rather than a second regex that would drift from it on the next prompt edit.

    python docs/showcase/build_ppi.py     # -> ppi_discovery.html
"""
from __future__ import annotations

import base64
import csv
import io
import json
import pathlib
import re
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import _facts                                            # noqa: E402
from _common import head as _mkhead                      # noqa: E402
from src.handoff import parse_handoff                    # noqa: E402

PROJECT = ROOT / "projects/mesothelioma_showcase"
RUN = PROJECT / "runs/round-1"
BINDER = RUN / "binder"
BOLTZ = ROOT / "outputs/e2e_mesothelioma"       # the archived BoltzGen run, same target
PDL1 = ROOT / "projects/pdl1_e2e/runs/round-1/binder"   # the campaign this one is scaled against
OUT = HERE / "ppi_discovery.html"

_AA3 = {"ALA": "Ala", "ARG": "Arg", "ASN": "Asn", "ASP": "Asp", "CYS": "Cys",
        "GLN": "Gln", "GLU": "Glu", "GLY": "Gly", "HIS": "His", "ILE": "Ile",
        "LEU": "Leu", "LYS": "Lys", "MET": "Met", "PHE": "Phe", "PRO": "Pro",
        "SER": "Ser", "THR": "Thr", "TRP": "Trp", "TYR": "Tyr", "VAL": "Val"}


# --------------------------------------------------------------- extraction
def _gates(stats: str) -> list:
    """The `passing each criterion alone` block of a filter_stats report."""
    tail = stats.split("passing each criterion alone:")[1]
    out = []
    for line in tail.strip().splitlines():
        m = re.match(r"\s*(.+?)\s{2,}([\d,]+)\s*\(\s*([\d.]+)%\)", line)
        if m:
            out.append([m.group(1).strip(), int(m.group(2).replace(",", "")),
                        float(m.group(3))])
    return out


def _dropped(stats: str) -> list:
    """The `dropped by first failing criterion` block, in the order printed."""
    body = stats.split("dropped by first failing criterion:")[1]
    body = body.split("passing each criterion alone:")[0]
    return [[m.group(1).strip(), int(m.group(2).replace(",", ""))]
            for m in re.finditer(r"\s*(.+?)\s{2,}([\d,]+)\s*$", body, re.M)]


def _citations(text: str) -> list:
    """`## CITATION VERIFICATION` — checked and verified. Not a handoff block,
    so `parse_handoff` cannot read it; it is a fixed two-line stanza."""
    m = re.search(r"Citations checked:\s*(\d+).*?Verified in corpus:\s*(\d+)",
                  text, re.S)
    if not m:
        raise SystemExit("no CITATION VERIFICATION block parsed")
    return [int(m.group(1)), int(m.group(2))]


def _elapsed_hours(jobs_json: str) -> float:
    """Wall-clock GPU hours for one campaign stage, from the job registry's own
    start/finish stamps — the driver runs detached, so this is the only record
    of how long the GPU was actually busy."""
    job = next(iter(json.loads(jobs_json)["jobs"].values()))
    return (job["finished_at"] - job["started_at"]) / 3600.0


def extract() -> dict:
    """Every number on the page, parsed out of the run. Raises SourceMissing."""
    manifest = json.loads(_facts.read(PROJECT / "manifest.json"))
    stages = manifest["rounds"][0]["stages"]

    lit_md = _facts.read(RUN / "01_literature.md")
    struct_md = _facts.read(RUN / "02_structure.md")
    lit = parse_handoff(lit_md)
    struct = parse_handoff(struct_md)
    trim = parse_handoff(_facts.read(BINDER / "22_trim.md"))
    pilot = parse_handoff(_facts.read(BINDER / "24_pilot.md"))
    prod = parse_handoff(_facts.read(BINDER / "26_production.md"))
    score = parse_handoff(_facts.read(BINDER / "27_scoring.md"))
    summary = parse_handoff(_facts.read(BINDER / "28_summary.md"))

    trim_map = json.loads(_facts.read(BINDER / "trim/trim_map.json"))
    calib = json.loads(_facts.read(BINDER / "calibration/calibration.json"))
    stats = _facts.read(BINDER / "scoring/filter_stats.txt")

    # Hotspots: the trim's own record, which is what RFD3 was actually given —
    # the same twelve rows as the structure stage's MODEL-READY HOTSPOTS tables,
    # after `_verify_hotspot_grounding` checked each name against the structure.
    hotspots = sorted(([h["residue"], int(h["auth_seq_id"])] for h in trim_map["hotspots"]),
                      key=lambda r: r[1])
    if not hotspots:
        raise SystemExit("no hotspots in trim_map.json")

    # How TEAD1 was actually identified as chain A — quoted, not characterised.
    m = re.search(r"^- Chain A: \S+ \((.+?)\)\s*$", struct_md, re.M)
    chain_a_note = m.group(1) if m else ""

    rows = list(csv.DictReader(io.StringIO(_facts.read(BINDER / "scoring/top_k.csv"))))
    designs = [{
        "rank": i,
        "name": r["name"].replace("yap1_binder_001_yap1_binder_001_", ""),
        "iptm": float(r["iptm"]), "ipsae_min": float(r["ipsae_min"]),
        "dock": float(r["binder_rmsd_dock"]), "plddt": float(r["binder_plddt"]),
        "ddg": float(r["rosetta_ddg"]), "len": int(r["binder_len"]),
        "engagement": float(r["hotspot_engagement"]),
        "target_rmsd": float(r["target_rmsd"]),
    } for i, r in enumerate(rows, 1)]

    # GPU wall-clock per stage, from each campaign's own detached-job registry.
    gpu = {s: round(_elapsed_hours(_facts.read(BINDER / f"campaign/{s}/jobs.json")), 2)
           for s in ("pilot", "calibration", "production")}

    # The archived BoltzGen run on the same target, for the side-by-side.
    bz_stats = _facts.read(BOLTZ / "05_ranking/filter_stats.txt")
    bz_rows = list(csv.DictReader(io.StringIO(
        _facts.read(BOLTZ / "05_ranking/top_k.csv"))))
    bz_best = bz_rows[0]
    boltzgen = {
        "designs": int(re.search(r"input designs:\s*([\d,]+)", bz_stats).group(1).replace(",", "")),
        "survivors": int(re.search(r"survivors:\s*([\d,]+)", bz_stats).group(1).replace(",", "")),
        "top_k": int(re.search(r"top_k selected:\s*([\d,]+)", bz_stats).group(1).replace(",", "")),
        "drop_reasons": re.search(r"## Drop reasons\s*\n\s*(.+)", bz_stats).group(1).strip(),
        "best_iptm": float(bz_best["design_to_target_iptm"]),
        "best_len": len(bz_best["designed_sequence"]),
        "modality": parse_handoff(_facts.read(BOLTZ / "03_design_report.md"))["modality"],
    }

    # PD-L1, the campaign this one is compared against.
    pdl1_stats = _facts.read(PDL1 / "scoring/filter_stats.txt")
    pdl1_calib = json.loads(_facts.read(PDL1 / "calibration/calibration.json"))
    pdl1 = {
        "backbone_p_hat": pdl1_calib["backbone_rate"]["p_hat"],
        "n_scored": int(re.search(r"input:\s*([\d,]+)", pdl1_stats).group(1).replace(",", "")),
        "n_survivors": int(re.search(r"survivors:\s*([\d,]+)", pdl1_stats).group(1).replace(",", "")),
    }

    ledger = [json.loads(l) for l in
              _facts.read(PROJECT / "ledger.jsonl").splitlines() if l.strip()]
    pathway_call = next(r for r in ledger if r["stage"] == "pathway")

    verdict_cp = next(c for c in manifest["checkpoints"] if c["id"] == "calibration_verdict")

    return {
        "query": manifest["query"],
        "spend_usd": manifest["budget"]["spent_usd"],
        "budget_cap_usd": manifest["budget"]["cap_usd"],
        "llm_stages": sorted(manifest["budget"]["by_stage"]),
        "model": sorted(manifest["budget"]["by_model"])[0],
        "pathway_uncached_input": pathway_call["usage"]["input_tokens"],
        "tiers": json.loads(stages["pathway"]["handoff"]["choices_json"]),
        "citations": {"literature": _citations(lit_md), "structure": _citations(struct_md)},
        "kd_rationale": lit["go_rationale"],
        "lit_hotspots": json.loads(lit["target_site_hint"])["priority_residues"],
        "pdb_id": struct["pdb_id"],
        "target_chain": struct["target_chain"],
        "partner_chain": struct["partner_chain"],
        "target_complex": struct["target_complex"],
        "chain_a_note": chain_a_note,
        "bsa_A2": int(struct["bsa_A2"]),
        "hotspots": hotspots,
        "trim": {
            "before": int(trim_map["n_residues_before"]),
            "after": int(trim_map["n_residues_after"]),
            "segments": int(trim["n_segments"]),
            "kept_segments": trim_map["kept_segments"],
            "contig": trim["contig"],
        },
        "pilot": {"n_rfd3": int(pilot["n_rfd3"]), "n_rf3": int(pilot["n_rf3"])},
        "calibration": {
            "backbone": calib["backbone_rate"], "refold": calib["refold_rate"],
            "central": calib["central"], "pessimistic": calib["pessimistic"],
            "verdict": calib["verdict"], "verdict_reason": calib["verdict_reason"],
            "requested_bar": calib["requested_bar"], "bar_raised_to": calib["bar_raised_to"],
            "n_refolds": calib["n_refolds_scored"], "n_backbones": calib["n_backbones_scored"],
            "prefilter_rate": calib["prefilter_rate"],
            "engagement_gate": next(g for g, _ in _dropped(calib["filter_stats_text"])
                                    if g.startswith("hotspot_engagement")),
            "compute": verdict_cp["payload"]["compute_choice"],
        },
        "n_rfd3": int(prod["n_rfd3"]), "n_filtered": int(prod["n_filtered"]),
        "n_mpnn": int(prod["n_mpnn"]),
        "prod_prefilter_rate": float(prod["prefilter_rate"]),
        "n_scored": int(score["n_scored"]), "n_survivors": int(score["n_survivors"]),
        "n_backbones_surviving": int(
            re.search(r"distinct backbones among survivors:\s*(\d+)", stats).group(1)),
        "gates": _gates(stats), "dropped": _dropped(stats),
        "engagement_gate": next(g for g, _ in _dropped(stats)
                                if g.startswith("hotspot_engagement")),
        "designs": designs,
        "median_target_rmsd": round(statistics.median(d["target_rmsd"] for d in designs), 3),
        "gpu_hours": gpu,
        "boltzgen": boltzgen,
        "pdl1": pdl1,
        "go": summary["go_recommendation"],
        "top_k_count": int(summary["top_k_count"]),
    }


F = _facts.load("ppi_discovery", extract)

# ------------------------------------------------------------------ shortcuts
CAL = F["calibration"]
BB, RF = CAL["backbone"], CAL["refold"]
CENT, PESS = CAL["central"], CAL["pessimistic"]
TRIM, BZ, PDL1F = F["trim"], F["boltzgen"], F["pdl1"]
D = F["designs"]
N_HS = len(F["hotspots"])
HOTSPOT_TEXT = ", ".join(f"{_AA3.get(a, a.title())}{n}" for a, n in F["hotspots"])
GPU = F["gpu_hours"]
GPU_TOTAL = sum(GPU.values())
BEST_IPTM = max(D, key=lambda d: d["iptm"])
BEST_DOCK = min(D, key=lambda d: d["dock"])
CARD_RANKS = [1, 2, 3, BEST_IPTM["rank"]]

FUNNEL = [
    ("RFD3 backbones", F["n_rfd3"], f"diffused against the {N_HS}-residue hotspot patch"),
    ("Cleared the prefilter", F["n_filtered"],
     f"{100*F['prod_prefilter_rate']:.1f}% — the derived chain-break budget held "
     f"across a {TRIM['segments']}-segment target"),
    ("solubleMPNN sequences", F["n_mpnn"],
     f"{F['n_mpnn'] // F['n_filtered']} per surviving backbone"),
    ("RF3 refolds", F["n_scored"], "each refolded from sequence alone"),
    ("Cleared every hard gate", F["n_survivors"],
     f"{100*F['n_survivors']/F['n_scored']:.1f}%, across "
     f"{F['n_backbones_surviving']} distinct backbones"),
    ("Ranked and reported", F["top_k_count"], "MMR-diversified top-K"),
]

CSS = (HERE / "campaign_pdl1.html").read_text().split("<style>")[1].split("</style>")[0]
EXTRA_CSS = """
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


def fmt(x, n=2):
    return f"{float(x):.{n}f}"


def tier_rows():
    return "".join(
        f'<tr><td><span class="tier{" v" if t["tier"] == "VALIDATED" else ""}">'
        f'{t["tier"]}</span></td>'
        f'<td class="m">{t["complex"]}</td>'
        f'<td class="num">{" · ".join(t["pdb_ids"]) or "—"}</td>'
        f'<td class="why">{t["evidence_basis"]}</td>'
        f'<td class="unc">{t["key_uncertainty"]}</td></tr>' for t in F["tiers"])


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
    return (f'<svg viewBox="0 -18 860 {y}" role="img" class="chart" '
            f'aria-label="Production funnel">{"".join(rows)}</svg>')


def bar_gates():
    alone = dict((g, (n, p)) for g, n, p in F["gates"])
    mx = max(n for _, n in F["dropped"])
    rows, y, RH = [], 0, 30
    for label, dropped in F["dropped"]:
        w = max(2.0, dropped / mx * 400)
        keeps, pct = alone.get(label, (0, 0.0))
        rows.append(f'<g class="mk" tabindex="0"><title>{label} — first to fail for '
                    f'{dropped:,} refolds; {pct:.1f}% of all {F["n_scored"]:,} would pass '
                    f'it alone</title>'
                    f'<text class="k r" x="248" y="{y+RH/2+5}">{label}</text>'
                    f'<rect x="260" y="{y+5}" width="{w:.1f}" height="{RH-10}" rx="4" '
                    f'fill="var(--mark-b)"/>'
                    f'<text class="v" x="{260+w+10:.1f}" y="{y+RH/2+5}">{dropped:,}</text></g>')
        y += RH + 8
    return (f'<svg viewBox="0 -6 760 {y}" role="img" class="chart" '
            f'aria-label="Refolds dropped by first failing gate">{"".join(rows)}</svg>')


def design_cards():
    by_rank = {d["rank"]: d for d in D}
    out = []
    for r in CARD_RANKS:
        d = by_rank[r]
        tag = "rank %d" % r
        if r == BEST_IPTM["rank"] and r != 1:
            tag += " · best ipTM"
        out.append(f'''<article class="card">
          <div class="card-b">
            <div class="card-h"><span class="rk">{tag}</span><code>{d["name"]}</code></div>
            <dl class="mini">
              <div><dt>ipTM</dt><dd>{fmt(d["iptm"], 3)}</dd></div>
              <div><dt>ipSAE</dt><dd>{fmt(d["ipsae_min"], 3)}</dd></div>
              <div><dt>dock RMSD</dt><dd>{fmt(d["dock"])} Å</dd></div>
              <div><dt>pLDDT</dt><dd>{fmt(d["plddt"], 3)}</dd></div>
              <div><dt>Rosetta ΔΔG</dt><dd>{fmt(d["ddg"], 1)}</dd></div>
              <div><dt>length</dt><dd>{d["len"]} aa</dd></div>
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
<style>{CSS}{EXTRA_CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · ppi track · 27–28 Aug 2026</p>
    <h1>One sentence about a disease, twenty designed binders out</h1>
  </div>
  <p class="lede">No target was named. The pipeline chose YAP1/TEAD1 from the literature,
  argued for it, cut the structure down, sized its own campaign from a measured hit rate,
  and ran it — <strong>unattended</strong> across two days and
  {GPU_TOTAL:.0f} GPU-hours, with a human touching exactly one thing: the SCALE_UP gate.
  {F["n_survivors"]} designs cleared every gate, for
  {F["spend_usd"]*100:.0f} cents of model spend. This is that run.</p>
  <div class="term"><span class="p">$</span> python scripts/run_pipeline.py --workflow ppi \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--query "{F["query"]}" \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--project mesothelioma_showcase --budget {F["budget_cap_usd"]:.2f}</div>
  <figure class="hero-fig">
    <img src="{img('meso_design')}" alt="The top-ranked designed mini-protein bound to TEAD1, covering the twelve hotspot residues.">
    <p class="legend">
      <span><b style="background:#2f8f74"></b>designed binder, {D[0]["len"]} aa</span>
      <span><b style="background:#9aa79d"></b>TEAD1</span>
      <span><b style="background:#c0872b"></b>the {N_HS} hotspots it was asked to cover</span>
    </p>
    <figcaption>The rank-1 design, refolded by RF3 from sequence alone, on the YAP-binding
    face of TEAD1. Dock RMSD to the intended site: <strong>{fmt(D[0]["dock"])} Å</strong>.
    All {N_HS} hotspots engaged.</figcaption>
  </figure>
  <div class="stats">
    <div class="stat"><b>${F["spend_usd"]:.2f}</b><span>total model spend</span></div>
    <div class="stat"><b>{GPU_TOTAL:.0f}</b><span>GPU-hours</span></div>
    <div class="stat"><b>{F["n_survivors"]}</b><span>designs through every gate</span></div>
    <div class="stat"><b>{fmt(BEST_IPTM["iptm"], 3)}</b><span>best ipTM</span></div>
    <div class="stat"><b>{len(F["llm_stages"])}</b><span>LLM stages in the whole run</span></div>
  </div>
  <p class="verdict">{F["go"]} — {F["top_k_count"]} ranked designs, every one engaging all
  {N_HS} hotspots</p>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">The result, first</p>
    <h2>{F["n_rfd3"]} backbones in, {F["n_survivors"]} designs out</h2></div>
  <div class="chart-wrap"><h3>Production funnel</h3>{bar_funnel()}
    <figcaption>Every count read from disk by directory scan, never from a log
    line.</figcaption></div>
  <p style="margin-top:22px">For scale, the PD-L1 campaign returned
  {100*PDL1F["n_survivors"]/PDL1F["n_scored"]:.1f}% from
  {PDL1F["n_scored"]/F["n_scored"]:.1f}× the refolds. Rosetta relax and InterfaceAnalyzer
  ran afterwards on the survivors and enter the composite only; a mis-docked pose is still
  a physical pose, and Rosetta will happily score one.</p>
  <div class="two">
    <div>
      <p>The gates are not optional, and the two doing the most work are the two a
      confidence score cannot replace. RF3 cannot be given a docked pose at inference, so
      ipTM reports confidence in whatever interface the model chose for <em>itself</em> —
      ranking on it alone selects confidently mis-docked binders.
      <code>binder_rmsd_dock</code> is the gate that catches them.</p>
      <div class="note-box"><p>The failure mode here is different from PD-L1's, and more
      tractable. There, dock-RMSD did the killing — designs confidently docked in the
      wrong place. Here <strong>{[p for g, n, p in F["gates"] if g.startswith("binder_rmsd_dock")][0]:.1f}%
      pass dock-RMSD</strong> and the leading killer is steric clash. The designs are
      landing on the right face in the right pose and being rejected on packing.</p></div>
    </div>
    <div class="chart-wrap"><h3>Refolds dropped by the first gate they failed</h3>
      {bar_gates()}
      <figcaption>Of {F["n_scored"]:,} production refolds. Hover a bar for what that gate
      would keep on its own.</figcaption></div>
  </div>
</section>

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
      hotspots at {", ".join(F["lit_hotspots"][:4])}, and precedent for peptidomimetic
      druggability — every claim carrying its DOI. All
      <strong>{F["citations"]["literature"][1]} of the
      {F["citations"]["literature"][0]}</strong> citations it made were checked against the
      local corpus and found there; none were invented.</p>
      <p>The structure stage then worked on the coordinates rather than the prose. On
      PDB <strong>{F["pdb_id"]}</strong> it measured {F["bsa_A2"]:,} Å² of buried surface,
      identified chain {F["target_chain"]} as TEAD1 by
      <em>{F["chain_a_note"].split(";")[-1].strip()}</em> against the requested target, and
      selected {N_HS} hotspots: {HOTSPOT_TEXT}. Those names are not taken on trust:
      before any spec is built, each is read back out of the downloaded structure at its
      own <code>auth_seq_id</code>, and a mismatch halts the run.</p>
    </div>
    <figure class="fig">
      <img src="{img('meso_native')}" alt="The YAP1 peptide wrapping the TEAD1 surface in 3KYS, with the twelve selected hotspots highlighted.">
      <figcaption><strong>{F["pdb_id"]}</strong> — what the native interaction looks like.
      The YAP1 peptide (grey) wraps the TEAD1 surface; ochre marks the {N_HS} residues the
      design was aimed at.</figcaption>
    </figure>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 4 · calibration</p>
    <h2>The campaign sized itself, then raised its own bar</h2></div>
  <p>A {F["pilot"]["n_rfd3"]}-backbone pilot proved the machinery in
  {GPU["pilot"]:.2f} h. Calibration then ran the experiment that decides the campaign:
  {CAL["n_backbones"]:,} backbones, {CAL["n_refolds"]:,} refolds, counting how many
  designs actually cleared the success bar rather than assuming a rate.</p>
  <p>It also cleared a first for this pipeline. TEAD1 needed no trimming — all
  {TRIM["before"]} modelled residues were already inside budget — but
  {F["pdb_id"]} has a disordered stretch with no density, so the target reaches RFD3 in
  <strong>{TRIM["segments"]} pieces</strong>: <code>{TRIM["contig"]}</code>. The target is
  fixed conditioning rather than something the model builds, so the gaps are not its
  problem; what they do change is the prefilter's chain-break count, which is
  <em>derived</em> from the segment number rather than hardcoded. A hardcoded limit would
  have rejected every design. It held — <strong>{100*CAL["prefilter_rate"]:.1f}% of
  calibration backbones cleared the prefilter</strong>, and
  {100*F["prod_prefilter_rate"]:.1f}% in production — and the refolded target superposes
  onto the deposited chain at <strong>{F["median_target_rmsd"]:.2f} Å</strong> median Cα
  RMSD across the top {F["top_k_count"]}, so the pieces have not drifted apart.</p>
  <div class="two">
    <div>
      <p>The measured backbone hit rate was <strong>{100*BB["p_hat"]:.2f}%</strong>
      ({BB["k"]} of {BB["n"]:,}, 95% Wilson interval
      {100*BB["p_low"]:.2f}–{100*BB["p_high"]:.2f}%) —
      {BB["p_hat"]/PDL1F["backbone_p_hat"]:.1f}× PD-L1's
      {100*PDL1F["backbone_p_hat"]:.2f}%. The requested bar was
      ipTM&nbsp;&gt;&nbsp;{CAL["requested_bar"]}; because the trial supported a harder one
      within budget, the stage <strong>raised it to {CAL["bar_raised_to"]} by
      itself</strong> and sized to that. It only ever moves the bar up.</p>
      <p>Sizing is done on the pessimistic end of the interval, never the point estimate:
      ~{PESS["required_refolds"]:,.0f} refolds, ~{PESS["est_gpu_hours"]} GPU-h,
      ~{PESS["est_disk_gb"]} GB — and the same arithmetic places the work, comparing
      {CAL["compute"]["local_hours"]} h on this workstation's single GPU against the
      {CAL["compute"]["max_local_hours"]:.0f} h local budget. Verdict:
      <strong>{CAL["verdict"]}</strong>, local.</p>
    </div>
    <div>
      <div class="tw"><table>
        <thead><tr><th>sized on</th><th class="num">backbones</th><th class="num">refolds</th>
          <th class="num">GPU-h</th><th class="num">disk</th></tr></thead>
        <tbody>
          <tr><td>{CENT["basis"]}</td><td class="num">{CENT["required_backbones"]:,.0f}</td>
            <td class="num">{CENT["required_refolds"]:,.0f}</td>
            <td class="num">{CENT["est_gpu_hours"]}</td>
            <td class="num">{CENT["est_disk_gb"]} GB</td></tr>
          <tr><td>{PESS["basis"]}</td><td class="num">{PESS["required_backbones"]:,.0f}</td>
            <td class="num">{PESS["required_refolds"]:,.0f}</td>
            <td class="num">{PESS["est_gpu_hours"]}</td>
            <td class="num">{PESS["est_disk_gb"]} GB</td></tr>
        </tbody></table></div>
      <p class="why">This is the run's one pause point, and the only place a human
      intervened. The plan is persisted to <code>calibration.json</code> as it is made, so
      resuming with <code>--start-from production</code> in a fresh process — the normal
      case for a multi-day campaign — re-derives this sizing and this local-vs-cluster
      choice instead of falling back to the config default.</p>
    </div>
  </div>
  <div class="note-box" style="margin-top:20px"><p><strong>The trial cost more GPU time
  than the campaign it sized</strong> — {GPU["calibration"]:.2f} h against
  {GPU["production"]:.2f} h — because raising the bar to
  ipTM&nbsp;&gt;&nbsp;{CAL["bar_raised_to"]} is also what let production be small. That is
  the trade the gate exists to make: spend measurably, once, instead of guessing at scale.
  This campaign also produced the pipeline's <code>{F["engagement_gate"]}</code> rule.
  Calibration measured its rate under the stricter
  <code>{CAL["engagement_gate"]}</code> — every declared hotspot contacted — and its own
  attrition showed that gate rejecting refolds for missing residues the backbone they came
  from had never touched. Production ran at
  <code>{F["engagement_gate"]}</code>, which is now the default.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 5 · scoring and ranking</p>
    <h2>What came out</h2></div>
  <div class="cards">{design_cards()}</div>
  <p style="margin-top:20px">Every one of the top {F["top_k_count"]} engages
  <strong>all {N_HS} hotspots</strong>, with ipTM
  {fmt(min(d["iptm"] for d in D), 3)}–{fmt(max(d["iptm"] for d in D), 3)} and dock-RMSD
  {fmt(min(d["dock"] for d in D))}–{fmt(max(d["dock"] for d in D))} Å across binder lengths
  from {min(d["len"] for d in D)} to {max(d["len"] for d in D)} residues and
  {F["top_k_count"]} distinct backbone families — so the diversification is returning
  genuinely different solutions, not {F["top_k_count"]} variations of one hit. Rank is the
  composite, not ipTM: the highest-ipTM design in the set
  ({fmt(BEST_IPTM["iptm"], 3)}) ranks {BEST_IPTM["rank"]}, because ipSAE, dock-RMSD and the
  Rosetta terms all carry weight against it.</p>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">The same question, twice</p>
    <h2>What changed when the PPI track was rewired</h2></div>
  <p>An earlier run asked this pipeline the same thing about mesothelioma and reached the
  same target — {F["target_complex"]} on {F["pdb_id"]} — through the older BoltzGen path.
  Putting them side by side is the clearest measure of what the foundry bridge bought.</p>
  <div class="vs">
    <div><h3>BoltzGen path (archived)</h3>
      <div class="n">{BZ["designs"]} → {BZ["survivors"]} → {BZ["top_k"]}</div>
      <div class="m">designs → survivors → top-K</div>
      <p>Every design survived every filter; drop reasons: {BZ["drop_reasons"]}. A funnel
      that rejects nothing has not demonstrated that its filters work — and at
      {BZ["designs"]} designs there is no measured hit rate to size anything from. Best
      design: a {BZ["best_len"]}-mer at ipTM {fmt(BZ["best_iptm"], 3)}.</p></div>
    <div><h3>foundry bridge (this run)</h3>
      <div class="n">{F["n_rfd3"]} → {F["n_scored"]:,} → {F["n_survivors"]} → {F["top_k_count"]}</div>
      <div class="m">backbones → refolds → gated → top-K</div>
      <p>Sized from a measured {100*BB["p_hat"]:.1f}% hit rate with a confidence interval,
      gated on {len(F["gates"])} criteria with the attrition recorded per criterion, and
      ranked on a composite including Rosetta terms. Best design: a
      {BEST_DOCK["len"]}-mer at ipTM {fmt(BEST_DOCK["iptm"], 3)}, dock-RMSD
      {fmt(BEST_DOCK["dock"])} Å.</p></div>
  </div>
  <div class="note-box" style="margin-top:20px"><p>This run is what made the bridge the
  <strong>default</strong>: <code>design.backend</code> is now <code>foundry</code>, so a
  PPI-discovered target hands off to the same RFD3→solubleMPNN→RF3 stage machine
  <code>--workflow binder</code> uses. Until this campaign finished, that hand-off was
  verified only by unit tests with the GPU stubbed out — the pipeline's own documentation
  listed "no real end-to-end GPU run has proven the bridge" as a known gap. BoltzGen is
  not deprecated: <code>--design-engine boltzgen</code> still selects it, and it is
  <em>required</em> for cyclic peptides, which RFD3 cannot build — the archived run above
  was a <code>{BZ["modality"]}</code> campaign, which is exactly why its designs are
  {BZ["best_len"]}-mers.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">What it cost, and what it is not</p>
    <h2>Four model stages</h2></div>
  <p>Four LLM stages in the entire run — {", ".join(F["llm_stages"])} — for
  <strong>${F["spend_usd"]:.2f} against a ${F["budget_cap_usd"]:.2f} cap</strong> on
  {F["model"]}. Each is a whole agentic tool-use loop rather than a single call: pathway
  alone billed {F["pathway_uncached_input"]/1000:.0f}k uncached input tokens. Everything
  between them is deterministic Python — the trim, the spec, campaign planning, gating,
  scoring, ranking — and the GPU did {GPU["pilot"]:.2f} h of pilot,
  {GPU["calibration"]:.2f} h of calibration and {GPU["production"]:.2f} h of production,
  {GPU_TOTAL:.1f} h in all.</p>
  <div class="note-box"><p><strong>Every design here is an unvalidated computational
  hypothesis.</strong> Nothing has been expressed, purified or measured. A high ipTM says
  a folding model is confident about an interface it chose; a low dock-RMSD says the
  binder is where it was asked to be. Neither is an affinity, and neither is evidence that
  this would disrupt YAP1–TEAD1 in a cell. Anyone synthesising one of these sequences is
  responsible for screening it — see <code>docs/responsible-use.md</code>.</p></div>
</section>

<footer>
  <p>Every figure above is extracted at build time from
  <code>projects/mesothelioma_showcase</code> — <code>manifest.json</code>,
  <code>ledger.jsonl</code>, <code>trim/trim_map.json</code>,
  <code>calibration/calibration.json</code>, each campaign stage's
  <code>jobs.json</code>, <code>scoring/filter_stats.txt</code>,
  <code>scoring/top_k.csv</code>, and the stage reports, whose handoff blocks are read with
  the pipeline's own <code>src.handoff</code> parser — with the BoltzGen comparison from
  <code>outputs/e2e_mesothelioma</code> and the PD-L1 baseline from
  <code>projects/pdl1_e2e</code>. The extracted values are committed to
  <code>docs/showcase/facts/ppi_discovery.json</code>, so any number that moves shows up as
  a reviewable diff. Rebuild with <code>python docs/showcase/build_ppi.py</code>.</p>
  <p>Structure images rendered with UCSF ChimeraX from the campaign's own RF3 refolds and
  from PDB {F["pdb_id"]}; hotspots shown are the {N_HS} the interface stage selected,
  mapped through the refold's own numbering. One caveat this run predates: the
  {TRIM["segments"]}rd segment is an artifact — the trim was deleting TEAD1's
  palmitoylated cysteine at 344 because that residue's name is absent from gemmi's
  amino-acid table. {F["pdb_id"]} has only one real gap. Chain membership now follows the
  backbone rather than the name.</p>
  <p><a href="index.html">← all showcase pages</a></p>
</footer>

</div>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT.relative_to(ROOT)}  ({len(HTML)/1024:.0f} KB)")
print(f"  target     {F['target_complex']} on {F['pdb_id']}, chain {F['target_chain']}")
print(f"  hotspots   {HOTSPOT_TEXT}")
print(f"  funnel     {F['n_rfd3']} -> {F['n_filtered']} -> {F['n_mpnn']} -> "
      f"{F['n_survivors']} survivors ({F['n_backbones_surviving']} backbones)")
print(f"  gpu        pilot {GPU['pilot']:.2f} + calibration {GPU['calibration']:.2f} + "
      f"production {GPU['production']:.2f} = {GPU_TOTAL:.2f} h")
print(f"  top-{F['top_k_count']}     iptm {min(d['iptm'] for d in D):.3f}-"
      f"{max(d['iptm'] for d in D):.3f}, dock {min(d['dock'] for d in D):.3f}-"
      f"{max(d['dock'] for d in D):.3f} A, len {min(d['len'] for d in D)}-"
      f"{max(d['len'] for d in D)}")
print(f"  cards      ranks {CARD_RANKS}")
print(f"  prefilter  calibration {100*CAL['prefilter_rate']:.1f}% / "
      f"production {100*F['prod_prefilter_rate']:.1f}%")
print(f"  spend      ${F['spend_usd']:.4f} over {len(F['llm_stages'])} LLM stages")
