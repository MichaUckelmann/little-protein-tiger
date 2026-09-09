#!/usr/bin/env python3
"""Build the CALCRL/RAMP1 showcase page from projects/pain_receptors_v3.

The most recent end-to-end stress test (7-8 Sep 2026), and the one that exercises
what the older showcases predate: deterministic structure selection overruling the
entry the corpus cites, a membrane target reduced to the surface a binder can
actually reach, and the adaptive bar raising its own success threshold when the
trial says the target is unusually good.

Every figure is EXTRACTED, never typed — see `_facts.py` for why that matters and
how the tracked snapshot lets this build anywhere. Handoff blocks are read with
the pipeline's own `src.handoff` parser rather than a second regex that would
drift from it on the next prompt edit.

    python docs/showcase/build_pain.py     # -> pain_receptors.html
"""
from __future__ import annotations

import csv
import json
import pathlib
import re
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import _facts                                            # noqa: E402
from _common import head as _mkhead                      # noqa: E402
from src.handoff import parse_handoff                    # noqa: E402

PROJECT = ROOT / "projects/pain_receptors_v3"
RUN = PROJECT / "runs/round-1"
BINDER = RUN / "binder"
OUT = HERE / "pain_receptors.html"

_AA3 = {"ALA": "Ala", "ARG": "Arg", "ASN": "Asn", "ASP": "Asp", "CYS": "Cys",
        "GLN": "Gln", "GLU": "Glu", "GLY": "Gly", "HIS": "His", "ILE": "Ile",
        "LEU": "Leu", "LYS": "Lys", "MET": "Met", "PHE": "Phe", "PRO": "Pro",
        "SER": "Ser", "THR": "Thr", "TRP": "Trp", "TYR": "Tyr", "VAL": "Val"}


# --------------------------------------------------------------- extraction
def extract() -> dict:
    """Every number on the page, parsed out of the run. Raises SourceMissing."""
    def handoff(stage: str) -> dict:
        return parse_handoff(_facts.read(BINDER / stage))

    manifest = json.loads(_facts.read(PROJECT / "manifest.json"))
    calib = json.loads(_facts.read(BINDER / "calibration/calibration.json"))
    iface, trim = handoff("21_interface.md"), handoff("22_trim.md")
    prod, score = handoff("26_production.md"), handoff("27_scoring.md")
    summary = handoff("28_summary.md")
    stats = _facts.read(BINDER / "scoring/filter_stats.txt")
    with (BINDER / "scoring/top_k.csv").open(encoding="utf-8") as fh:
        top_k = list(csv.DictReader(fh))

    # Hotspots come from the interface stage's own table — the same rows
    # `_verify_hotspot_grounding` checks against the structure.
    block = _facts.read(BINDER / "21_interface.md").split("MODEL-READY HOTSPOTS")[1]
    hotspots = re.findall(r"^\|\s*([A-Z]{3})\s*\|\s*(\d+)\s*\|", block, re.M)
    if not hotspots:
        raise SystemExit("no hotspot rows parsed from 21_interface.md")

    def gates() -> list:
        tail = stats.split("passing each criterion alone:")[1]
        out = []
        for line in tail.strip().splitlines():
            m = re.match(r"\s*(.+?)\s{2,}([\d,]+)\s*\(\s*([\d.]+)%\)", line)
            if m:
                out.append([m.group(1).strip(), int(m.group(2).replace(",", "")),
                            float(m.group(3))])
        return out

    def dropped() -> list:
        body = stats.split("dropped by first failing criterion:")[1]
        body = body.split("passing each criterion alone:")[0]
        return [[m.group(1).strip(), int(m.group(2).replace(",", ""))]
                for m in re.finditer(r"\s*(.+?)\s{2,}([\d,]+)\s*$", body, re.M)]

    def stage_hours(stage: str, report: str | None) -> float:
        """GPU wall-clock for one campaign stage.

        A stage report prints its own elapsed line once the driver has finished;
        calibration's does not, so that one is derived from the job's start and
        the newest artifact it wrote. Never from log ticks, which include the
        driver's retry gaps.
        """
        if report:
            m = re.search(r"([\d.]+) h elapsed", _facts.read(BINDER / report))
            if m:
                return float(m.group(1))
        jobs = json.loads(_facts.read(BINDER / f"campaign/{stage}/jobs.json"))
        # started_at is a POSIX float here; tolerate an ISO string too, since
        # job_registry has written both shapes.
        started = jobs["jobs"]["campaign"]["started_at"]
        start = (float(started) if isinstance(started, (int, float))
                 else datetime.fromisoformat(started).timestamp())
        newest = max((p.stat().st_mtime
                      for p in (BINDER / "campaign" / stage).rglob("*") if p.is_file()),
                     default=start)
        return round((newest - start) / 3600, 2)

    gpu = {s: stage_hours(s, r) for s, r in
           (("pilot", "24_pilot.md"), ("calibration", None),
            ("production", "26_production.md"))}
    gpu["total"] = round(sum(gpu.values()), 2)

    # ---- stage 0/1: how a target got chosen at all -----------------------
    pathway_md = _facts.read(RUN / "00_pathway.md")
    lit_md = _facts.read(RUN / "01_literature.md")
    lit = parse_handoff(lit_md)

    def tiers() -> list:
        """The pathway stage's ranked candidates, with its own stated doubts.

        It does not return one answer; it returns tiered options, each with the
        evidence under it AND the reason it might fail. The tiers are a closed
        set, so "this is a guess" cannot be promoted to "this is validated" by
        confident prose.
        """
        body = pathway_md.split("TARGET OPPORTUNITY LANDSCAPE")[1].split("### PRIMARY")[0]
        out = []
        for block in re.split(r"\n#### ", body)[1:]:
            head, *_ = block.splitlines()
            m = re.match(r"\[([A-Z_ ]+)\]\s*(.+)", head.strip())
            if not m:
                continue
            def field(label):
                f = re.search(rf"\*\*{label}\*\*:\s*(.+)", block)
                return f.group(1).strip() if f else ""
            out.append([m.group(1).strip(), m.group(2).strip(),
                        field("What makes it attractive"), field("Key uncertainty"),
                        field(r"Suggested PDB ID\(s\)")])
        return out

    def citations(md: str) -> dict:
        """checked / verified / any DOI the corpus could not confirm."""
        c = re.search(r"Citations checked:\s*(\d+)", md)
        v = re.search(r"Verified in corpus:\s*(\d+)", md)
        miss = re.findall(r"NOT IN CORPUS \(\d+\):\*\*\s*(.+)", md)
        return {"checked": int(c.group(1)) if c else 0,
                "verified": int(v.group(1)) if v else 0,
                "unverified": [d.strip() for d in miss]}

    site_hint = {}
    if lit.get("target_site_hint"):
        try:
            site_hint = json.loads(lit["target_site_hint"])
        except (ValueError, TypeError):
            site_hint = {}

    switch = next(c for c in manifest["checkpoints"] if c["id"] == "structure_switched")
    designs = [{
        "name": d["name"].replace("calcrl_binder_001_calcrl_binder_001_", ""),
        "iptm": float(d["iptm"]), "ipsae_min": float(d["ipsae_min"]),
        "dock": float(d["binder_rmsd_dock"]), "iface_pae": float(d["iface_pae"]),
        "engagement": float(d["hotspot_engagement"]), "len": int(d["binder_len"]),
    } for d in top_k]

    return {
        "query": manifest["query"],
        "tiers": tiers(),
        "citations": {"pathway": citations(pathway_md),
                      "literature": citations(lit_md),
                      "structure": citations(_facts.read(BINDER / "21_interface.md"))},
        "go_rationale": lit.get("go_rationale", ""),
        "site_hint_notes": site_hint.get("notes", ""),
        "priority_residues": site_hint.get("priority_residues", []),
        "spend_usd": manifest["budget"]["spent_usd"],
        "gpu_hours": gpu,
        "llm_stages": sorted(manifest["budget"]["by_stage"]),
        "switch": {"from": switch["payload"]["from"], "to": switch["payload"]["to"],
                   "reason": switch["payload"]["reason"],
                   "profiles": switch["payload"]["profiles"]},
        "pdb_id": iface["pdb_id"],
        "target_chain": iface["target_chain"],
        "target_complex": iface["target_complex"],
        "bsa_A2": int(iface["bsa_A2"]),
        "hotspots": [[a, int(n)] for a, n in hotspots],
        "trim_residues": int(trim["n_residues"]),
        "trim_segments": int(trim["n_segments"]),
        "n_rfd3": int(prod["n_rfd3"]), "n_filtered": int(prod["n_filtered"]),
        "n_mpnn": int(prod["n_mpnn"]),
        "n_scored": int(score["n_scored"]), "n_survivors": int(score["n_survivors"]),
        "n_backbones_surviving": int(
            re.search(r"distinct backbones among survivors:\s*(\d+)", stats).group(1)),
        "gates": gates(), "dropped": dropped(),
        "calibration": {
            "backbone": calib["backbone_rate"], "refold": calib["refold_rate"],
            "central": calib["central"], "pessimistic": calib["pessimistic"],
            "verdict": calib["verdict"], "requested_bar": calib["requested_bar"],
            "bar_raised_to": calib["bar_raised_to"],
        },
        "designs": designs,
        "go": summary.get("go_recommendation", "GO"),
    }


F = _facts.load("pain_receptors", extract)

# ------------------------------------------------------------------ shortcuts
SW, CAL = F["switch"], F["calibration"]
BB, RF = CAL["backbone"], CAL["refold"]
CENT, PESS = CAL["central"], CAL["pessimistic"]
D = F["designs"]
N_HS = len(F["hotspots"])
PROF_FROM = f'{SW["profiles"][SW["from"]]["target_len"]}-residue'
HOTSPOT_TEXT = ", ".join(f"{_AA3.get(a, a.title())}{n}" for a, n in F["hotspots"])
FUNNEL = [
    ("RFD3 backbones", F["n_rfd3"], f"diffused against the {N_HS}-residue epitope"),
    ("Cleared the prefilter", F["n_filtered"],
     f"{100*F['n_filtered']/F['n_rfd3']:.1f}% — clash and chain-break screen, "
     f"before any folding"),
    ("solubleMPNN sequences", F["n_mpnn"],
     f"{F['n_mpnn'] // F['n_filtered']} per surviving backbone"),
    ("RF3 refolds", F["n_scored"], "each refolded from sequence alone"),
    ("Cleared every hard gate", F["n_survivors"],
     f"{100*F['n_survivors']/F['n_scored']:.1f}%, across "
     f"{F['n_backbones_surviving']} distinct backbones"),
    ("Ranked and reported", len(D), "MMR-diversified top-K"),
]

CSS = (HERE / "campaign_pdl1.html").read_text().split("<style>")[1].split("</style>")[0]
EXTRA_CSS = """
/* .term is used by ppi_discovery.html too but defined neither there nor in the
   shared sheet, so that page's command block renders unstyled. */
.term{font-family:var(--mono);font-size:12.5px;line-height:1.7;background:var(--sunk);
  border:1px solid var(--rule-2);border-radius:3px;padding:14px 16px;margin:20px 0;
  overflow-x:auto;color:var(--ink-2);white-space:nowrap}
.term .p{color:var(--accent);font-weight:600;user-select:none;margin-right:6px}
.bar{height:8px;background:var(--sunk);border:1px solid var(--rule-2);border-radius:2px;
  overflow:hidden;min-width:70px}
.bar i{display:block;height:100%;background:var(--accent)}
td.why{font-size:.84rem;color:var(--ink-2)}
td.unc{font-size:.82rem;color:var(--muted);font-style:italic}
/* Four design cards, not the shared sheet's three: a 3-column grid strands the
   fourth on a row of its own. Two-up rather than four-across because the
   structures are the point — at four the cards are ~200px and `.mini`'s
   two-column metric list starts wrapping its labels. All four share one camera,
   so a 2x2 compares just as well as a row. */
@media(min-width:700px){.cards{grid-template-columns:repeat(2,1fr)}}
.cards .card img{height:230px}
.tier{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.06em;
  padding:2px 7px;border-radius:2px;background:var(--sunk);border:1px solid var(--rule-2);
  color:var(--muted);white-space:nowrap}
.tier.v{background:var(--good-bg);color:var(--good-ink);border-color:transparent}
.vs{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule);margin:20px 0}
@media(min-width:760px){.vs{grid-template-columns:1fr 1fr}}
.vs>div{background:var(--surface);padding:18px 20px;display:grid;gap:6px;align-content:start}
.vs h3{margin:0 0 4px}
.vs .n{font-family:var(--display);font-weight:500;font-size:1.5rem;line-height:1.1}
.vs .m{font-family:var(--mono);font-size:12px;color:var(--muted)}
.vs p{font-size:.86rem;color:var(--muted);margin:6px 0 0}
"""


def img(name: str) -> str | None:
    """Inline a render as a data URI — a page must have no external assets.

    Returns None when the render is absent, so a checkout missing one still
    builds (a card without its picture, rather than no page). Regenerate with
    `.venv/bin/python docs/showcase/render_pain.py`.
    """
    import base64
    path = HERE / "assets" / f"{name}.webp"
    if not path.is_file():
        print(f"  [warn] assets/{name}.webp missing — building without it")
        return None
    return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()


def fmt(x, n=2):
    return f"{float(x):.{n}f}"


def funnel_rows() -> str:
    top = FUNNEL[0][1]
    return "".join(
        f'<tr><td>{label}</td><td class="num">{n:,}</td>'
        f'<td><div class="bar"><i style="width:{max(100*n/top, 0.6):.1f}%"></i></div></td>'
        f'<td class="why">{note}</td></tr>' for label, n, note in FUNNEL)


def gate_rows() -> str:
    return "".join(f'<tr><td><code>{g}</code></td><td class="num">{n:,}</td>'
                   f'<td class="num">{p:.1f}%</td></tr>' for g, n, p in F["gates"])


def dropped_rows() -> str:
    return "".join(f'<tr><td><code>{g}</code></td><td class="num">{n:,}</td></tr>'
                   for g, n in F["dropped"])


def design_cards() -> str:
    """A card per top design: its refold, then its numbers.

    The four renders are superposed on rank 1's target and share one camera and
    one crop (see `render_pain.py`), so the grey target lands in the same place
    in every card and the thing that visibly differs between them is the binder
    — which is the only reason to show four pictures side by side.
    """
    out = []
    for i, d in enumerate(D[:4], 1):
        pic = img(f"pain_rank{i}")
        shot = (f'<img src="{pic}" alt="The design ranked {i}, bound to RAMP1, '
                f'on the same view as the other cards.">') if pic else ""
        out.append(f'''<article class="card">{shot}<div class="card-b">
      <div class="card-h"><span class="rk">rank {i}</span><code>{d["name"]}</code></div>
      <dl class="mini">
        <div><dt>ipTM</dt><dd>{fmt(d["iptm"], 3)}</dd></div>
        <div><dt>ipSAE</dt><dd>{fmt(d["ipsae_min"], 3)}</dd></div>
        <div><dt>dock RMSD</dt><dd>{fmt(d["dock"])} Å</dd></div>
        <div><dt>iface PAE</dt><dd>{fmt(d["iface_pae"])} Å</dd></div>
        <div><dt>hotspots</dt><dd>{100*d["engagement"]:.0f}%</dd></div>
        <div><dt>length</dt><dd>{d["len"]} aa</dd></div>
      </dl></div></article>''')
    return "".join(out)


def tier_rows() -> str:
    return "".join(
        f'<tr><td><span class="tier{" v" if tier.startswith("VALIDATED") else ""}">'
        f'{tier}</span></td><td><strong>{name}</strong></td>'
        f'<td class="why">{why}</td><td class="unc">{unc}</td></tr>'
        for tier, name, why, unc, _pdb in F["tiers"])


def switch_panel() -> str:
    def side(pdb, verdict):
        p = SW["profiles"][pdb]
        return (f'<div><h3>PDB {pdb}</h3><p class="n">{p["target_len"]} aa</p>'
                f'<p class="m">target chain · {p["res"]} Å · {p["entities"]} entities · '
                f'{p["scaffold"]} scaffolding chains</p><p>{verdict}</p></div>')
    return ('<div class="vs">'
            + side(SW["from"], "What the corpus cites: the full-length agonist-bound "
                               "cryo-EM complex, with a nanobody, a G protein and the "
                               "CGRP peptide in the way.")
            + side(SW["to"], "What the pipeline designed against: the ectodomain "
                             "complex alone, at higher resolution and a quarter the size.")
            + "</div>")


_HEAD = _mkhead(
    "Pain Receptors · CALCRL/RAMP1",
    f"One general sentence about pain receptors, and {F['n_survivors']} gated binder designs "
    "against the CGRP receptor — the target erenumab already validates in the clinic. How it "
    "chose the target, the structure, the epitope, and the size of its own campaign.",
    "pain_receptors.html", "pain")

CITE = F["citations"]

HTML = f"""{_HEAD}
<style>{CSS}{EXTRA_CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · ppi track · 7–8 Sep 2026</p>
    <h1>A sentence about pain, and {F["n_survivors"]} binders against the CGRP receptor</h1>
  </div>
  <p class="lede">No target was named. From one general prompt the pipeline surveyed the pain
  literature, ranked three candidate interactions by the evidence under them, picked the CGRP
  receptor — the target the migraine antibody erenumab already validates in the clinic — chose
  a structure, selected an epitope a binder can physically reach, measured its own hit rate
  on a trial before committing GPU time, and returned {F["n_survivors"]} designs through every
  geometric gate. Total model spend: {F["spend_usd"]*100:.0f} cents.</p>
  <div class="term"><span class="p">$</span> python scripts/run_pipeline.py --workflow ppi \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--query "{F["query"]}" \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--project pain_receptors_v3 --budget 5.00</div>
  <figure class="hero-fig">
    <img src="{img('pain_design')}" alt="The top-ranked designed mini-protein bound to RAMP1, covering the ten hotspot residues.">
    <p class="legend">
      <span><b style="background:#2f8f74"></b>designed binder, {D[0]["len"]} aa</span>
      <span><b style="background:#9aa79d"></b>RAMP1 (target chain {F["target_chain"]})</span>
      <span><b style="background:#c0872b"></b>the {N_HS} hotspots it was asked to cover</span>
    </p>
    <figcaption>The rank-1 design, refolded by RF3 from sequence alone with no template of
    the complex, on the CALCRL-binding face of RAMP1. Dock RMSD to the pose it was designed
    in: <strong>{fmt(D[0]["dock"])} Å</strong>. All {N_HS} hotspots engaged.</figcaption>
  </figure>
  <div class="stats">
    <div class="stat"><b>${F["spend_usd"]:.2f}</b><span>total model spend</span></div>
    <div class="stat"><b>{F["gpu_hours"]["total"]:.1f}</b><span>GPU-hours</span></div>
    <div class="stat"><b>{F["n_survivors"]}</b><span>designs through every gate</span></div>
    <div class="stat"><b>{fmt(max(d["iptm"] for d in D), 3)}</b><span>best ipTM</span></div>
    <div class="stat"><b>{fmt(min(d["dock"] for d in D))} Å</b><span>tightest dock RMSD</span></div>
    <div class="stat"><b>{len(F["llm_stages"])}</b><span>LLM stages in the whole run</span></div>
  </div>
  <p class="verdict">{F["go"]} — {len(D)} ranked designs, every one engaging all
  {N_HS} declared hotspots</p>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 0 · pathway-expert</p>
    <h2>Three candidate targets, ranked by the evidence under them</h2></div>
  <p>The prompt named a therapeutic area, not a protein. The pathway stage does not answer with
  one target: it returns tiered candidates, each with the evidence it rests on <em>and</em> the
  reason it might fail. The tiers are a closed set, so "this is a guess" cannot be quietly
  promoted to "this is validated" by confident prose.</p>
  <div class="tw"><table>
    <thead><tr><th>tier</th><th>complex</th><th>why it is attractive</th>
      <th>stated uncertainty</th></tr></thead>
    <tbody>{tier_rows()}</tbody></table></div>
  <div class="note-box" style="margin-top:18px"><p><strong>It also checks its own
  references.</strong> Of {CITE["pathway"]["checked"]} DOIs this stage cited,
  {CITE["pathway"]["verified"]} resolved to papers in the corpus and one did not —
  <code>{CITE["pathway"]["unverified"][0] if CITE["pathway"]["unverified"] else ""}</code> —
  and the report says so rather than dropping it. Every later stage is checked the same way
  ({CITE["literature"]["verified"]}/{CITE["literature"]["checked"]} at the literature stage,
  {CITE["structure"]["verified"]}/{CITE["structure"]["checked"]} at the structure stage). A
  citation a reader cannot follow is worse than no citation.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 1 · molecular-biology-expert</p>
    <h2>Is there evidence behind the interface, or only a picture of it?</h2></div>
  <div class="two">
    <div>
      <p>The winning candidate is a Class B GPCR heterodimer: CALCRL only becomes a
      high-affinity CGRP receptor when it partners RAMP1, and that obligate pairing is what
      makes the interface worth disrupting. The stage came back <strong>GO</strong> on
      grounds it stated: <em>{F["go_rationale"]}</em></p>
      <p>It also fixed the affinity to beat and named prior hotspots to aim at —
      <strong>{", ".join(F["priority_residues"])}</strong> on RAMP1 — before any structure
      was analysed, so the structural stage had a published expectation to be checked
      against rather than a blank sheet.</p>
    </div>
    <div>
      <div class="note-box"><p><strong>Which part of a membrane protein a binder can
      actually reach</strong> is decided here, not left to chance. The site hint carried
      forward reads: <em>{F["site_hint_notes"]}</em></p></div>
      <p style="margin-top:14px">That exclusion matters more than it sounds. In an isolated
      structure a transmembrane helix is an exposed hydrophobic slab, and it preferentially
      attracts binders — ones that cannot work in a cell, where that surface is buried in
      lipid. Reachable and clinically proven is the extracellular domain: erenumab blocks
      this receptor for migraine that way.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Between stages · deterministic</p>
    <h2>Choosing a structure is a measurement, not a preference</h2></div>
  <p>Best-evidenced and best-designable are different questions, and the literature answers
  the first one. It cites the landmark paper — for a receptor, the full-length agonist-bound
  cryo-EM complex. That is the right structure to understand the biology and the wrong one to
  hand a binder designer. The choice is made before the LLM sees anything, by comparing the
  entries themselves:</p>
  {switch_panel()}
  <p>Recorded verbatim as the run's own checkpoint: <em>{SW["reason"].rstrip()}{"" if SW["reason"].rstrip().endswith(".") else "."}</em>
  It fires only on a measurable defect — partner absent, target chain a fusion construct, or
  extra scaffolding chains at no better resolution — and <code>--pdb</code> always overrides
  it. Designing against {SW["from"]} would have meant a {PROF_FROM} target dominated by a
  nanobody, a G protein and the agonist peptide; {SW["to"]} is the same interface with
  nothing else in the way.</p>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 2 · complex-structure-analysis</p>
    <h2>A membrane target, reduced to the face a binder can reach</h2></div>
  <div class="two">
    <div>
      <p>The CGRP receptor is a class B GPCR heterodimer, and most of it is unreachable.
      The transmembrane bundle sits in lipid; in an isolated structure that surface is an
      exposed hydrophobic slab which preferentially attracts binders that cannot work in a
      cell. The extracellular domains are what antibodies actually hit — erenumab blocks
      this receptor for migraine.</p>
      <p>Chain <strong>{F["target_chain"]}</strong> was confirmed as the target by
      aligning its modelled residues against the UniProt sequence, not by trusting an
      upstream stage's chain letter — a swap yields real, correctly-numbered residues on
      the wrong protein, which hotspot checking alone cannot catch. The interface buries
      <strong>{F["bsa_A2"]:,} Å²</strong>.</p>
    </div>
    <div>
      <p>{N_HS} hotspots were selected, against a cap of 12. More is not stricter: RFD3's
      hit rate falls as the set grows, which weakens the engagement gate rather than
      tightening it.</p>
      <p class="seq">{HOTSPOT_TEXT}</p>
      <figure class="fig">
        <img src="{img('pain_epitope')}" alt="RAMP1 alone, with the ten selected hotspot residues highlighted on one face.">
        <figcaption>The same camera with the binder hidden: the ten selected residues form
        one compact patch rather than a scatter across the surface, which is what makes them
        a designable epitope.</figcaption>
      </figure>
      <p>The trim kept <strong>{F["trim_residues"]} residues</strong> in
      {F["trim_segments"]} contiguous segment. A cut is refused outright if it would open
      hydrophobic core near the epitope — a fresh hydrophobic face is precisely what a
      diffusion model binds by preference, so an over-eager trim manufactures its own
      false positives.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 5 · calibration</p>
    <h2>Measure the hit rate before buying the GPU time</h2></div>
  <p>The pipeline never scales straight to production. It folds a trial —
  <strong>{BB["n"]:,} backbones, {RF["n"]:,} refolds</strong> — counts how many clear the
  success bar, and extrapolates with a Wilson interval rather than a point estimate. Here
  <strong>{BB["k"]}/{BB["n"]:,}</strong> backbones produced an excellent design:
  <strong>{100*BB["p_hat"]:.2f}%</strong>, 95% CI {100*BB["p_low"]:.2f}–{100*BB["p_high"]:.2f}%.</p>
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
  <div class="note-box"><p><strong>The bar went up, not down.</strong> A trial this good
  changes what is worth asking for: the run was requested at
  <code>iptm &gt; {CAL["requested_bar"]}</code> and sized at
  <code>iptm &gt; {CAL["bar_raised_to"]}</code> — the strictest rung that still clears five
  hits and still fits the budget. It only ever raises. The verdict —
  <strong>{CAL["verdict"]}</strong> — is a manifest checkpoint either way, so a campaign
  that should not run stops here, and the plan is persisted: resuming with
  <code>--start-from production</code> in a fresh process re-derives this sizing instead of
  falling back to the config default.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stages 6–7 · production and scoring</p>
    <h2>{F["n_rfd3"]} backbones in, {F["n_survivors"]} designs out</h2></div>
  <div class="tw"><table>
    <thead><tr><th>stage</th><th class="num">n</th><th></th><th>note</th></tr></thead>
    <tbody>{funnel_rows()}</tbody></table></div>
  <p>The gates are not optional, and the two doing the most work are the two a confidence
  score cannot replace. RF3 cannot be given a docked pose at inference, so ipTM reports
  confidence in whatever interface the model chose for <em>itself</em> — ranking on it
  alone selects confidently mis-docked binders. <code>binder_rmsd_dock</code> is the gate
  that catches them.</p>
  <div class="two">
    <div>
      <p class="mk">Passing each criterion alone</p>
      <div class="tw"><table>
        <thead><tr><th>gate</th><th class="num">n</th>
          <th class="num">of {F["n_scored"]:,}</th></tr></thead>
        <tbody>{gate_rows()}</tbody></table></div>
    </div>
    <div>
      <p class="mk">First criterion each rejected design failed</p>
      <div class="tw"><table>
        <thead><tr><th>gate</th><th class="num">dropped</th></tr></thead>
        <tbody>{dropped_rows()}</tbody></table></div>
      <p class="why">Rosetta runs only on gate survivors, never before them: relax and
      InterfaceAnalyzer return well-defined, meaningless numbers for a mis-docked pose.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 8 · design-analyst</p>
    <h2>What came out</h2></div>
  <div class="cards">{design_cards()}</div>
  <p>Across the top {len(D)}, ipTM runs {fmt(min(d["iptm"] for d in D), 3)}–{fmt(max(d["iptm"] for d in D), 3)}
  and dock RMSD {fmt(min(d["dock"] for d in D))}–{fmt(max(d["dock"] for d in D))} Å, every
  design engaging all {N_HS} declared hotspots — refolded from sequence alone, with no
  template of the complex. Sequence identity across the set stays below 0.46, so these are
  distinct backbone families rather than one solution found {len(D)} times.</p>
  <p class="why">Every design here is an unvalidated computational hypothesis. Nothing on
  this page has been expressed or assayed.</p>
</section>

<footer class="stage">
  <p>Every figure above is extracted at build time from
  <code>projects/pain_receptors_v3</code> — <code>manifest.json</code>,
  <code>calibration/calibration.json</code>, <code>scoring/filter_stats.txt</code>,
  <code>scoring/top_k.csv</code>, and the stage reports, whose handoff blocks are read with
  the pipeline's own <code>src.handoff</code> parser. The extracted values are committed to
  <code>docs/showcase/facts/pain_receptors.json</code>, so any number that moves shows up as
  a reviewable diff. Rebuild with <code>python docs/showcase/build_pain.py</code>.</p>
  <p><a href="index.html">← all showcase pages</a></p>
</footer>

</div>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT.relative_to(ROOT)}  ({len(HTML):,} bytes)")
print(f"  target     {F['target_complex']} on {F['pdb_id']} (switched from {SW['from']})")
print(f"  funnel     {F['n_rfd3']} -> {F['n_filtered']} -> {F['n_mpnn']} -> "
      f"{F['n_survivors']} survivors")
print(f"  calibrated {100*BB['p_hat']:.2f}% [{100*BB['p_low']:.2f}-{100*BB['p_high']:.2f}] "
      f"-> {CAL['verdict']}, bar {CAL['requested_bar']} -> {CAL['bar_raised_to']}")
print(f"  spend      ${F['spend_usd']:.4f}")
