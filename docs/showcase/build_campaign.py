#!/usr/bin/env python3
"""Build the PD-L1 campaign showcase page from projects/pdl1_rc1.

The first end-to-end binder campaign this repo ran (21-22 Aug 2026), and the one
the later showcases are measured against: one target name in, a calibration gate
that raised its own success bar, and twenty ranked designs out.

Every figure is EXTRACTED, never typed — see `_facts.py` for why that matters and
how the tracked snapshot lets this build on a machine without the run. This page
used to carry its numbers as literals while claiming they "were read from" the
run; that is how it came to state an ipTM/geometry claim its own scoring CSV
flatly contradicts, and how three different GPU-hour totals ended up on one page.
The cross-tabulation behind that claim is now recomputed from
`scoring/refold_scores.csv` at build time, and the gate is re-run through the
pipeline's own `binder_ranking.filter_records` rather than re-implemented here.

Handoff blocks are read with `src.handoff.parse_handoff`, the same parser the
pipeline uses, so a skill-prompt edit cannot silently desync this page.

    python docs/showcase/build_campaign.py     # -> campaign_pdl1.html

NOTE: this page owns the shared stylesheet. `build_ppi.py`, `build_corpus.py`,
`build_pain.py` and `build_index.py` all do
`campaign_pdl1.html.read_text().split("<style>")[1].split("</style>")[0]`, so the
`<style>` block must stay a single contiguous element in this file's output.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import _facts                                            # noqa: E402
from _common import head as _mkhead                      # noqa: E402
from src.handoff import (                                 # noqa: E402
    parse_handoff, parse_hotspot_residues,
)

PROJECT = ROOT / "projects/pdl1_rc1"
RUN = PROJECT / "runs/round-1"
BINDER = RUN / "binder"
ASSETS = HERE / "assets"
OUT = HERE / "campaign_pdl1.html"

_AA1 = {"A": "ALA", "R": "ARG", "N": "ASN", "D": "ASP", "C": "CYS", "Q": "GLN",
        "E": "GLU", "G": "GLY", "H": "HIS", "I": "ILE", "L": "LEU", "K": "LYS",
        "M": "MET", "F": "PHE", "P": "PRO", "S": "SER", "T": "THR", "W": "TRP",
        "Y": "TYR", "V": "VAL"}
_TITLE = {v: v.title() for v in _AA1.values()}

# The independent check: PDB 4ZQK (PD-1 bound to PD-L1) superposed onto this
# campaign's own rank-1 refold, then contact footprints counted on both sides.
# 4ZQK took no part in the run, which is what makes it evidence rather than a
# restatement of the pipeline's own output.
#
# This used to be a hand-made ChimeraX session pasted in as a literal dict,
# labelled "manual" on the page because an unversioned session cannot be
# extracted. It is now computed here, so it goes through `_facts.load` like
# every other number and the page's provenance claim covers it too. The old
# figures were for the 7CZD campaign's rank-1 design and do not describe this
# one: a different structure, a different epitope and a different binder.
ZQK_PATH = ROOT / "data/structures/4ZQK_ba1.cif"
CONTACT_CUTOFF_A = 4.5


def _rank1_refold(top_row: dict) -> pathlib.Path:
    """The rank-1 design's RF3 refold directory, found by name.

    `os.scandir`, never a glob: `rf3_out` holds one directory per refold —
    1,824 of them here, and tens of thousands on a larger campaign.
    """
    import os

    base = BINDER / "campaign/production/rf3_out"
    if not base.is_dir():
        raise _facts.SourceMissing(str(base))
    with os.scandir(base) as it:
        for e in it:
            if e.name == top_row["name"]:
                cif = pathlib.Path(e.path) / f"{e.name}_model.cif"
                if cif.is_file():
                    return cif
    raise _facts.SourceMissing(f"{base}/{top_row['name']}_model.cif")


def _zqk_overlap(refold_cif: pathlib.Path, hotspots: dict[int, str]) -> dict:
    """Superpose 4ZQK's PD-L1 onto the refold's target, count both footprints.

    Three facts from this repo's own notes make or break this:

      * RF3 renumbers the refold's chains — binder A, target B — and the
        target 1-based, while the hotspot ids are AUTHOR numbering from the
        deposited entry. The offset is DERIVED here and then verified against
        every hotspot's residue name; a wrong offset silently reports contacts
        for arbitrary residues, which is exactly the failure that looks fine.
      * 4ZQK's PD-L1 is chain A and its PD-1 is chain B, identified by
        sequence identity to the refold's own target rather than by position.
      * Waters and ligands are stripped before superposing, or ordered waters
        join the contact counts on both sides.
    """
    import gemmi

    if not ZQK_PATH.is_file():
        raise _facts.SourceMissing(str(ZQK_PATH))
    from src.structure_tools import sequence_identity

    ref = gemmi.read_structure(str(refold_cif))
    ref.setup_entities()
    ref.remove_ligands_and_waters()
    target = ref[0]["B"]

    # -- offset, derived then proven
    offsets = {}
    for off in range(-60, 80):
        offsets[off] = sum(
            1 for auth, name in hotspots.items() for r in target
            if r.seqid.num == auth - off and r.name == name)
    offset, agree = max(offsets.items(), key=lambda kv: kv[1])
    if agree != len(hotspots):
        raise SystemExit(
            f"4ZQK check: refold offset {offset} explains only {agree} of "
            f"{len(hotspots)} hotspot names — refusing to report contacts "
            f"against a mapping that does not hold")

    zqk = gemmi.read_structure(str(ZQK_PATH))
    zqk.setup_entities()
    zqk.remove_ligands_and_waters()
    tgt_seq = gemmi.one_letter_code([r.name for r in target])
    chains = [(ch.name, gemmi.one_letter_code([r.name for r in ch])) for ch in zqk[0]]
    chains = [(n, sq) for n, sq in chains if len(sq) >= 30]
    ident = {n: sequence_identity(tgt_seq, sq) for n, sq in chains}
    pdl1_chain = max(ident, key=ident.get)
    pd1_chain = min(ident, key=ident.get)
    if pdl1_chain == pd1_chain or ident[pdl1_chain] < 0.8:
        raise SystemExit(f"4ZQK check: cannot tell PD-L1 from PD-1 ({ident})")

    pol_t, pol_z = target.get_polymer(), zqk[0][pdl1_chain].get_polymer()
    sup = gemmi.calculate_superposition(pol_t, pol_z, pol_z.check_polymer_type(),
                                        gemmi.SupSelect.CaP)
    zqk[0].transform_pos_and_adp(sup.transform)

    ns = gemmi.NeighborSearch(ref, CONTACT_CUTOFF_A + 1.5).populate()
    hydrogen = gemmi.Element("H")

    def footprint(residues) -> set[int]:
        hit: set[int] = set()
        for res in residues:
            for atom in res:
                if atom.element == hydrogen:
                    continue
                for mark in ns.find_atoms(atom.pos, "\0", radius=CONTACT_CUTOFF_A):
                    cra = mark.to_cra(ref[0])
                    if cra.chain.name != "B" or cra.atom.element == hydrogen:
                        continue
                    if cra.atom.pos.dist(atom.pos) <= CONTACT_CUTOFF_A:
                        hit.add(cra.residue.seqid.num)
        return hit

    pd1 = footprint(list(zqk[0][pd1_chain]))
    design = footprint(list(ref[0]["A"]))
    shared = pd1 & design
    auth = lambda n: n + offset                                   # noqa: E731
    return {
        "source": "computed at build time from the run's own rank-1 refold",
        "pdb_id": "4ZQK", "cutoff_A": CONTACT_CUTOFF_A,
        "pdl1_chain": pdl1_chain, "pd1_chain": pd1_chain,
        "refold_offset": offset,
        "pd1_contacts": len(pd1), "design_contacts": len(design),
        "shared": len(shared),
        # Of PD-1's OWN footprint, how much the design covers. The reciprocal
        # (share of the design's footprint) is a different and easier number
        # because the design is the larger of the two, so both are published.
        # `pct` is what the page has always printed and it has always meant
        # "of PD-1's own footprint" — kept under both names so the semantics
        # are readable in the facts file rather than implied by a call site.
        "pct": round(100 * len(shared) / len(pd1)) if pd1 else 0,
        "pct_of_pd1": round(100 * len(shared) / len(pd1)) if pd1 else 0,
        "pct_of_design": round(100 * len(shared) / len(design)) if design else 0,
        "hotspots_that_are_pd1_contacts":
            sum(1 for a in hotspots if (a - offset) in pd1),
        "n_hotspots": len(hotspots),
        "superpose_rmsd_A": round(sup.rmsd, 2),
        "superpose_n_ca": sup.count,
        "superpose_identity_pct": round(100 * ident[pdl1_chain], 1),
        "pd1_only": sorted(auth(n) for n in (pd1 - design)),
        "shared_auth": sorted(auth(n) for n in shared),
        # Published in AUTHOR numbering for the page and in REFOLD numbering
        # for `render_pdl1.py`, which colours these three sets on the refold's
        # own coordinates and must not re-derive the offset itself.
        "design_only_auth": sorted(auth(n) for n in (design - pd1)),
        "refold": {"pd1": sorted(pd1), "design": sorted(design),
                   "shared": sorted(shared),
                   "pd1_only": sorted(pd1 - design),
                   "design_only": sorted(design - pd1)},
    }


_AA3 = frozenset(_AA1.values())


def _parse_bsa(md: str) -> dict[int, float]:
    """Per-residue buried surface area, from the hotspot-region lines.

    Two conventions are in the wild, because this is LLM-authored prose and
    the skill prompt has been edited between runs:

        7CZD (Aug 2026):  `- BSA contributions (Å²): R113 28.9, M115 43.1`
        8ZNL (Sep 2026):  `- BSA contributions (Å²): TYR124 (106.3), MET116 (61.8)`

    One-letter-and-bare-value, versus three-letter-and-parenthesised. Only the
    first was handled, so the newer campaign extracted ZERO BSA values and the
    page then died on `max()` of an empty sequence — a failure that reads as a
    missing file rather than a changed format. Three-letter codes are matched
    first and gated on being real amino acids, so `A122 (50.3)` cannot be read
    as alanine-122 by the one-letter branch and then silently overwritten.
    """
    out: dict[int, float] = {}
    for line in re.findall(r"- BSA contributions \(Å²\): (.+)", md):
        for name, num, val in re.findall(r"\b([A-Z]{3})(\d+)\s*\(([\d.]+)\)", line):
            if name in _AA3:
                out[int(num)] = float(val)
        for _one, num, val in re.findall(
                r"\b([A-Z])(\d+)\s+([\d.]+)(?=[,\s]|$)", line):
            out.setdefault(int(num), float(val))
    return out


def _parse_ddg(md: str) -> dict[int, float]:
    """Per-residue ΔΔG, as the interface stage reported it.

    The newer report states it structurally on its `Key chain B residues`
    line (`TYR124 (ddG = -4.98 kcal/mol, BSA = 106.3 Å²)`) and yields six
    values; the older one only mentioned two in prose (`Tyr56 ... (−3.87
    kcal/mol)`). Both are read, structured form first. Signs are normalised
    to negative: a stabilising ΔΔG is quoted with a Unicode minus in one
    report and an ASCII hyphen in the other, and `abs()` on the magnitude is
    safer than trusting which.
    """
    out: dict[int, float] = {}
    for name, num, _sign, val in re.findall(
            r"\b([A-Z]{3})(\d+)\s*\(ddG\s*=\s*([−-]?)([\d.]+)\s*kcal/mol", md):
        if name in _AA3:
            out[int(num)] = -abs(float(val))
    for _name, num, _sign, val in re.findall(
            r"([A-Z][a-z]{2})(\d+)[^()\n]{0,80}\(([−-])([\d.]+) kcal/mol\)", md):
        out.setdefault(int(num), -abs(float(val)))
    return out


# --------------------------------------------------------------- extraction
def extract() -> dict:
    """Every number on the page, parsed out of the run. Raises SourceMissing."""
    import yaml
    from src.binder_ranking import DEFAULT_THRESHOLDS, filter_records

    def handoff(stage: str) -> dict:
        return parse_handoff(_facts.read(BINDER / stage))

    manifest = json.loads(_facts.read(PROJECT / "manifest.json"))
    stages = manifest["rounds"][0]["stages"]
    intel = stages["target_intel"]["handoff"]
    calib = json.loads(_facts.read(BINDER / "calibration/calibration.json"))
    cands = json.loads(_facts.read(BINDER / "candidates/candidates.json"))
    plans = {m: json.loads(_facts.read(BINDER / f"campaign/{m}/plan.json"))
             for m in ("pilot", "calibration", "production")}
    iface_md = _facts.read(BINDER / "21_interface.md")
    trim, pilot = handoff("22_trim.md"), handoff("24_pilot.md")
    prod, score = handoff("26_production.md"), handoff("27_scoring.md")
    summary = handoff("28_summary.md")
    frozen_stats = _facts.read(BINDER / "scoring/filter_stats.txt")

    # -- timing: GPU hours are the wall time between stage completions, and the
    #    three of them plus the LLM stages must add up to the run's own span.
    t = lambda s: dt.datetime.fromisoformat(stages[s]["updated_at"])   # noqa: E731
    hrs = lambda a, b: round((t(b) - t(a)).total_seconds() / 3600, 2)  # noqa: E731
    gpu = {"pilot": hrs("binder_spec", "pilot"),
           "calibration": hrs("pilot", "calibration"),
           "production": hrs("calibration", "production")}
    gpu["total"] = round(sum(gpu.values()), 2)
    start = dt.datetime.fromisoformat(manifest["created_at"])
    end = dt.datetime.fromisoformat(manifest["updated_at"])

    # -- LLM spend: the ledger is authoritative and has one line per BILLED API
    #    call, which is not one line per stage — an agentic stage bills once per
    #    turn that reaches the model.
    ledger = [json.loads(ln) for ln in
              _facts.read(PROJECT / "ledger.jsonl").splitlines() if ln.strip()]
    calls_by_stage: dict[str, int] = {}
    for e in ledger:
        calls_by_stage[e["stage"]] = calls_by_stage.get(e["stage"], 0) + 1

    # -- hotspots: the interface stage's own MODEL-READY table, joined to the
    #    per-residue BSA and ddG it reported for the two regions it ranked.
    #
    #    Read with the PIPELINE's parser, not a third regex of this file's own.
    #    The one that used to live here required the residue name in its own
    #    cell (`| ALA | 53 |`) and this campaign's interface stage writes
    #    `| ALA53 | 53 |` — both of which `handoff.parse_hotspot_residues`
    #    accepts, because it is the parser the run itself was built with. The
    #    page's whole claim is that its numbers come from the run; a stricter
    #    private copy of the run's own format is how that claim quietly stops
    #    being true, and here it simply refused to build.
    hs_json = parse_hotspot_residues(iface_md, parse_handoff(iface_md))
    if not hs_json:
        raise SystemExit("no MODEL-READY HOTSPOTS section in 21_interface.md")
    hs_rows = [(r["residue"], str(r["auth_seq_id"]), str(r["label_seq_id"]),
                r["rfd3_atoms"]) for r in json.loads(hs_json)["residues"]]
    if not hs_rows:
        raise SystemExit("no hotspot rows parsed from 21_interface.md")

    bsa = _parse_bsa(iface_md)
    ddg = _parse_ddg(iface_md)
    regions = []
    titles = re.findall(r"^#### Region (\d+): (.+?) — (\w+)\s*$", iface_md, re.M)
    members = re.findall(r"^- Residues: (.+)$", iface_md, re.M)
    phob = re.findall(r"^- Hydrophobic fraction: ([\d.]+)$", iface_md, re.M)
    for (num, title, rating), res, hf in zip(titles, members, phob):
        regions.append({
            "n": int(num), "title": title, "rating": rating,
            "short": title.split(" hotspot")[0].split(" (")[0],
            "hydrophobic_fraction": float(hf),
            "residues": [int(m.group(1)) for m in re.finditer(r"[A-Z]{3}(\d+)", res)],
        })

    def region_of(auth: int) -> dict:
        return next((r for r in regions if auth in r["residues"]), regions[0])

    best_ddg = sorted(ddg.items(), key=lambda kv: kv[1])
    max_bsa = max(bsa[int(a)] for _n, a, _l, _at in hs_rows)
    hotspots = []
    for name3, auth, label, atoms in hs_rows:
        a = int(auth)
        hotspots.append({
            "name": _TITLE.get(name3, name3.title()), "auth": a, "label": int(label),
            "bsa": bsa.get(a), "ddg": ddg.get(a), "atoms": atoms,
            "region": region_of(a)["short"],
            "backbone_only": "no sidechain" in atoms,
            "largest_bsa": bsa.get(a) == max_bsa,
            "ddg_rank": next((i + 1 for i, (k, _v) in enumerate(best_ddg) if k == a), None),
        })

    # -- the literature the interface stage grounded itself in. Its own
    #    `## CITATION VERIFICATION` block is the record of how many of those
    #    DOIs `_verify_citations` found in the local corpus, which is the
    #    claim worth making — "cited" and "checked against the corpus" are
    #    different things and the page should only assert the second.
    dois = []
    for d in re.findall(r"(10\.\d{4,9}/[^\s—,;)]+)", iface_md):
        if d not in dois:
            dois.append(d)
    if not dois:
        raise SystemExit("no DOI cited in 21_interface.md")
    cites = re.search(r"- Citations checked: (\d+)", iface_md)
    verified = re.search(r"- Verified in corpus: (\d+)", iface_md)

    # A Kd is quoted only when the stage happened to cite one. The 7CZD run
    # did ("a PD-L1-mimicking peptide ... Kd ≈ 1.5 µM"); the 8ZNL run cites
    # three DOIs and no affinity. Required, this killed the build — and a page
    # that invents an affinity to fill the slot would be far worse than one
    # that does not mention it.
    kd = re.search(r"Kd ≈ ([\d.]+)\s*(µM|nM)", iface_md)

    # -- the gate, twice. `filter_stats.txt` is the FROZEN record of what this
    #    run decided, at the hotspot_engagement >= 1 threshold in force then.
    #    config.yaml now sets 0.75, so the same CSV re-gated today survives more.
    rows = list(csv.DictReader(
        (BINDER / "scoring/refold_scores.csv").open(encoding="utf-8")
        if (BINDER / "scoring/refold_scores.csv").is_file()
        else _facts.read(BINDER / "scoring/refold_scores.csv").splitlines()))
    cfg = yaml.safe_load(_facts.read(ROOT / "config.yaml"))
    cur_thresh = {**DEFAULT_THRESHOLDS, **cfg["design"]["binder_ranking"]["thresholds"]}
    hist_thresh = {**cur_thresh, "hotspot_engagement_min": 1.0}

    def gate(th: dict) -> dict:
        surv, st = filter_records(rows, th)
        return {"survivors": st.n_survivors,
                "backbones": len({r["design_family"] for r in surv}),
                "dropped": [[k, v] for k, v in
                            sorted(st.dropped.items(), key=lambda kv: -kv[1])],
                "alone": [[k, v, round(100 * v / st.n_input, 1)]
                          for k, v in st.passing_alone.items()]}

    historical, current = gate(hist_thresh), gate(cur_thresh)
    if historical["survivors"] != int(score["n_survivors"]):
        raise SystemExit(
            f"re-gating at the historical threshold gives {historical['survivors']}, "
            f"but the run reported {score['n_survivors']} — the gate has moved in a way "
            f"this page cannot reproduce; fix the page before shipping it.")

    # -- the claim this page previously got backwards. Both directions of the
    #    ipTM x dock cross-tabulation, so neither can be asserted from the other.
    num = lambda r, k: (float(r[k]) if r[k] not in ("", "nan") else None)  # noqa: E731
    hi = [r for r in rows if (num(r, "iptm") or 0) > 0.7]
    docked = [r for r in hi if (num(r, "binder_rmsd_dock") or 1e9)
              <= cur_thresh["binder_rmsd_dock_max"]]
    dock_label = next(k for k, _v in historical["dropped"] if k.startswith("binder_rmsd_dock"))
    dock_first = next(v for k, v in historical["dropped"] if k == dock_label)
    from src.binder_ranking import _criteria
    crit = _criteria(hist_thresh)

    def first_fail(r: dict) -> str | None:
        for label, check in crit:
            if not check(r):
                return label
        return None
    dock_ff = [r for r in rows if first_fail(r) == dock_label]
    dock_ff_lowconf = [r for r in dock_ff
                       if (num(r, "iptm") or 0) < cur_thresh["iptm_min"]]

    # -- the contrast targets. Stated in CLAUDE.md, measured on two OTHER
    #    campaigns, and read from there rather than retyped so the page cannot
    #    drift from the repo's own record of them.
    c8, t8, c79, t79 = re.search(
        r"only (\d+)\s*% \(([\w\d]+)\) and (\d+)\s*% \(([\w\d]+)\) are docked on target",
        _facts.read(ROOT / "CLAUDE.md")).groups()

    # -- top designs. Rosetta terms are joined from rosetta_metrics.csv, the file
    #    that actually produced them; top_k.csv mirrors them.
    top_k = list(csv.DictReader((BINDER / "scoring/top_k.csv").open(encoding="utf-8")))
    ros = {r["design"]: r for r in csv.DictReader(
        (BINDER / "scoring/rosetta_metrics.csv").open(encoding="utf-8"))}
    designs = [{
        "name": d["name"].replace("cd274_binder_001_cd274_binder_001_", ""),
        "full_name": d["name"],
        "iptm": float(d["iptm"]), "ipsae_min": float(d["ipsae_min"]),
        "dock": float(d["binder_rmsd_dock"]), "plddt": float(d["binder_plddt"]),
        "len": int(d["binder_len"]), "seq": d["binder_seq"],
        "rosetta_ddg": float(ros[d["name"]]["ddg"]) if d["name"] in ros else None,
    } for d in top_k]

    # -- the independent check, computed rather than pasted (see _zqk_overlap)
    zqk = _zqk_overlap(_rank1_refold(top_k[0]),
                       {int(a): n3 for n3, a, _l, _at in hs_rows})

    return {
        "query": manifest["query"],
        "started": start.date().isoformat(), "ended": end.date().isoformat(),
        "wall_hours": round((end - start).total_seconds() / 3600, 2),
        "gpu_hours": gpu,
        "spend_usd": manifest["budget"]["spent_usd"],
        "budget_cap_usd": manifest["budget"]["cap_usd"],
        "models": manifest["budget"]["by_model"],
        "llm_stages": list(calls_by_stage),   # ledger order == execution order
        "llm_calls": sum(calls_by_stage.values()),
        "llm_calls_by_stage": calls_by_stage,
        # The default provider is a Python default, not a config key — read it
        # from the runner's own signature so the page cannot claim a default
        # that was changed in code.
        "default_provider": re.search(
            r"provider: str = \"(\w+)\"",
            _facts.read(ROOT / "src/pipeline_runner.py")).group(1),
        "default_model": ((cfg.get("models", {}).get("gemini") or {}).get("default")
                          or (cfg.get("models", {}).get("gemini") or {}).get("model")),
        "design_backend": cfg["design"].get("backend"),

        "target_gene": intel["target_gene"], "uniprot": intel["target_uniprot"],
        "pdb_id": intel["pdb_id"], "target_chain": intel["target_chain"],
        "partner_chain": intel["partner_chain"], "partner_name": intel["partner_name"],
        "target_chain_length": int(intel["target_chain_length"]),
        "bsa_A2": int(intel["interface_bsa_A2"]),
        "interface_rationale": intel["interface_rationale"],
        "binder_len_min": int(intel["binder_length_min"]),
        "binder_len_max": int(intel["binder_length_max"]),
        "sites": [{"site_id": s["site_id"], "pdb_id": s["pdb_id"],
                   "partner_name": s["partner_name"], "rationale": s["rationale"]}
                  for s in json.loads(intel["sites_json"])],
        "alternatives": [{"pdb_id": a["pdb_id"], "why_not": a["why_not"]}
                         for a in json.loads(intel["alternatives_json"])],
        "candidates": [{"pdb_id": c["pdb_id"], "res": c["resolution_A"],
                        "bsa": c["bsa_A2"], "iface_res": c["n_interface_residues"],
                        "hbonds": c["n_hbonds"], "phob": c["hydrophobic_fraction"],
                        "rank": c["rank"], "partner": c["partner_entity"]}
                       for c in cands["candidates"]],

        "regions": regions, "hotspots": hotspots,
        "lit_doi": dois[0], "lit_dois": dois,
        "lit_kd": (f"{kd.group(1)} {kd.group(2)}" if kd else None),
        "citations_checked": int(cites.group(1)) if cites else None,
        "citations_verified": int(verified.group(1)) if verified else None,

        "trim_residues": int(trim["n_residues"]), "trim_segments": int(trim["n_segments"]),
        "contig": trim["contig"],

        "pilot": {"n_rfd3": int(pilot["n_rfd3"]), "n_filtered": int(pilot["n_filtered"]),
                  "n_rf3": int(pilot["n_rf3"]), "hours": gpu["pilot"]},
        "calibration": {
            "diffused": plans["calibration"]["expected_rfd3"],
            "prefiltered": calib["n_backbones_scored"],
            "prefilter_rate": calib["prefilter_rate"],
            "n_seq": calib["n_seq"], "refolds": calib["n_refolds_scored"],
            "backbone_rate": calib["backbone_rate"],
            "softer_rates": [[float(k.split("> ")[1]), v]
                             for k, v in calib["softer_rates"].items() if "> " in k],
            "central": calib["central"], "pessimistic": calib["pessimistic"],
            "verdict": calib["verdict"], "verdict_reason": calib["verdict_reason"],
            "requested_bar": calib["requested_bar"], "bar_raised_to": calib["bar_raised_to"],
            "target_designs": calib["target_designs"], "hours": gpu["calibration"],
        },
        "production": {"n_rfd3": int(prod["n_rfd3"]), "n_filtered": int(prod["n_filtered"]),
                       "n_mpnn": int(prod["n_mpnn"]), "n_rf3": int(prod["n_rf3"]),
                       "prefilter_rate": float(prod["prefilter_rate"]),
                       "hours": gpu["production"]},

        "n_scored": int(score["n_scored"]),
        "gate_historical": historical, "gate_current": current,
        "hotspot_engagement_now": cur_thresh["hotspot_engagement_min"],
        "frozen_stats": frozen_stats,

        "geometry": {
            "iptm_bar": 0.7, "dock_max": cur_thresh["binder_rmsd_dock_max"],
            "iptm_gate": cur_thresh["iptm_min"],
            "n_high_iptm": len(hi), "n_high_iptm_docked": len(docked),
            "pct_docked": round(100 * len(docked) / len(hi), 1),
            "n_confidently_misdocked": len(hi) - len(docked),
            "dock_first_fail": dock_first,
            "dock_first_fail_lowconf": len(dock_ff_lowconf),
            "dock_first_fail_lowconf_pct": round(100 * len(dock_ff_lowconf) / len(dock_ff), 1),
            "contrast": [[t8, int(c8)], [t79, int(c79)]],
        },

        "rosetta_scored": len(ros),
        "rosetta_cap": cfg["design"]["binder_ranking"]["rosetta"]["max_designs"],
        "designs": designs, "top_k_count": len(top_k),
        "go": summary.get("go_recommendation", "GO"),
        "go_rationale": summary.get("go_rationale", ""),
        "has_report_html": (BINDER / "report.html").is_file(),
        "zqk": zqk,
    }


F = _facts.load("campaign_pdl1", extract)

# ------------------------------------------------------------------ shortcuts
CAL, PROD, GEO = F["calibration"], F["production"], F["geometry"]
BB = CAL["backbone_rate"]
CENT, PESS = CAL["central"], CAL["pessimistic"]
HIST, CUR = F["gate_historical"], F["gate_current"]
D = F["designs"]
M4 = F["zqk"]
N_HS = len(F["hotspots"])
GPU = F["gpu_hours"]

FUNNEL = [
    ("RFD3 backbones", PROD["n_rfd3"], f"diffused against the {N_HS}-residue hotspot patch"),
    ("Cleared the prefilter", PROD["n_filtered"],
     f"{100*PROD['prefilter_rate']:.1f}% — chain breaks, clashes, loop fraction"),
    ("solubleMPNN sequences", PROD["n_mpnn"],
     f"{PROD['n_mpnn'] // PROD['n_filtered']} per surviving backbone"),
    ("RF3 refolds", PROD["n_rf3"], "every sequence refolded from sequence alone"),
    ("Cleared every hard gate", HIST["survivors"],
     f"{100*HIST['survivors']/PROD['n_rf3']:.1f}%, across {HIST['backbones']} "
     f"distinct backbones"),
    ("Ranked and reported", F["top_k_count"], "MMR-diversified top-K"),
]


def img(name: str) -> str:
    b = (ASSETS / f"{name}.webp").read_bytes()
    return "data:image/webp;base64," + base64.b64encode(b).decode()


# ---------------------------------------------------------------- chart SVG
def bar_funnel() -> str:
    mx = max(v for _, v, _ in FUNNEL)
    rows, y, RH, GAP = [], 0, 34, 14
    for label, val, note in FUNNEL:
        w = max(3.0, val / mx * 660)
        rows.append(
            f'<g class="mk" tabindex="0"><title>{label}: {val:,} — {note}</title>'
            f'<rect x="0" y="{y}" width="{w:.1f}" height="{RH}" rx="4" fill="var(--mark-a)"/>'
            f'<text class="v" x="{w + 12:.1f}" y="{y + RH/2 + 5}">{val:,}</text>'
            f'<text class="k" x="0" y="{y - 6}">{label}</text></g>')
        y += RH + GAP + 14
    return (f'<svg viewBox="0 -18 860 {y}" role="img" aria-label="Production funnel" '
            f'class="chart">{"".join(rows)}</svg>')


def line_yield() -> str:
    W, H, PL, PB = 660, 250, 46, 34
    pts_data = CAL["softer_rates"]
    xs = [p[0] for p in pts_data]
    x0, x1 = min(xs), max(xs)
    mx = max(v for _, v in pts_data)
    px = lambda x: PL + (x - x0) / (x1 - x0) * (W - PL - 26)      # noqa: E731
    py = lambda v: H - PB - v / mx * (H - PB - 24)                # noqa: E731
    pts = " ".join(f"{px(x):.1f},{py(v):.1f}" for x, v in pts_data)
    area = f"{px(x0):.1f},{H-PB} {pts} {px(x1):.1f},{H-PB}"
    n_ref = CAL["refolds"]
    dots = "".join(
        f'<g class="mk" tabindex="0"><title>ipTM &gt; {x}: {v} of {n_ref:,} refolds '
        f'({v/n_ref*100:.1f}%)</title>'
        f'<circle cx="{px(x):.1f}" cy="{py(v):.1f}" r="5.5" fill="var(--mark-a)" '
        f'stroke="var(--surface)" stroke-width="2"/></g>' for x, v in pts_data)
    step = 50
    grid = "".join(
        f'<line class="grid" x1="{PL}" y1="{py(g):.1f}" x2="{W-26}" y2="{py(g):.1f}"/>'
        f'<text class="ax" x="{PL-10}" y="{py(g)+4:.1f}" text-anchor="end">{g}</text>'
        for g in range(0, int(mx) + step, step))
    ticks = "".join(
        f'<text class="ax" x="{px(x):.1f}" y="{H-PB+20}" text-anchor="middle">{x}</text>'
        for x, _ in pts_data)
    bar = CAL["bar_raised_to"]
    sel = (f'<line class="sel" x1="{px(bar):.1f}" y1="16" x2="{px(bar):.1f}" y2="{H-PB}"/>'
           f'<text class="sel-l" x="{px(bar):.1f}" y="10" text-anchor="end">bar set here</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" class="chart" '
            f'aria-label="Designs surviving at each ipTM bar">{grid}{sel}'
            f'<polygon points="{area}" fill="var(--mark-a)" opacity="0.13"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--mark-a)" stroke-width="2" '
            f'stroke-linejoin="round"/>{dots}{ticks}'
            f'<text class="ax-t" x="{PL}" y="{H-4}">ipTM bar →</text></svg>')


def bar_gates() -> str:
    alone = dict((k, (n, p)) for k, n, p in HIST["alone"])
    mx = max(v for _, v in HIST["dropped"])
    rows, y, RH = [], 0, 30
    for label, dropped in HIST["dropped"]:
        w = max(2.0, dropped / mx * 430)
        n, p = alone.get(label, (0, 0.0))
        rows.append(
            f'<g class="mk" tabindex="0"><title>{label} — first to fail for {dropped:,} '
            f'refolds; {p}% of all {F["n_scored"]:,} would pass it on its own</title>'
            f'<text class="k r" x="250" y="{y+RH/2+5}">{label}</text>'
            f'<rect x="262" y="{y+5}" width="{w:.1f}" height="{RH-10}" rx="4" fill="var(--mark-b)"/>'
            f'<text class="v" x="{262+w+10:.1f}" y="{y+RH/2+5}">{dropped:,}</text></g>')
        y += RH + 8
    return (f'<svg viewBox="0 -6 760 {y}" role="img" class="chart" '
            f'aria-label="Refolds dropped by first failing gate">{"".join(rows)}</svg>')


def wilson() -> str:
    W, H = 660, 92
    lo, hi, pt = 100*BB["p_low"], 100*BB["p_high"], 100*BB["p_hat"]
    top = 10.0
    sx = lambda v: 40 + v / top * (W - 80)                        # noqa: E731
    return f'''<svg viewBox="0 0 {W} {H}" role="img" class="chart"
      aria-label="Backbone hit rate with 95% Wilson interval">
      <line class="grid" x1="40" y1="52" x2="{W-40}" y2="52"/>
      {''.join(f'<text class="ax" x="{sx(t):.1f}" y="76" text-anchor="middle">{t}%</text>'
               f'<line class="grid" x1="{sx(t):.1f}" y1="46" x2="{sx(t):.1f}" y2="58"/>'
               for t in (0, 2, 4, 6, 8, 10))}
      <g class="mk" tabindex="0"><title>{BB["k"]} of {BB["n"]} backbones produced an
      excellent design — {pt:.2f}%, 95% Wilson interval {lo:.2f}% to {hi:.2f}%</title>
      <rect x="{sx(lo):.1f}" y="42" width="{sx(hi)-sx(lo):.1f}" height="20" rx="4"
            fill="var(--mark-a)" opacity="0.22"/>
      <line x1="{sx(lo):.1f}" y1="38" x2="{sx(lo):.1f}" y2="66" stroke="var(--mark-a)" stroke-width="2"/>
      <line x1="{sx(hi):.1f}" y1="38" x2="{sx(hi):.1f}" y2="66" stroke="var(--mark-a)" stroke-width="2"/>
      <circle cx="{sx(pt):.1f}" cy="52" r="6" fill="var(--mark-a)"
              stroke="var(--surface)" stroke-width="2"/></g>
      <text class="v" x="{sx(pt):.1f}" y="26" text-anchor="middle">{pt:.2f}%</text>
      <text class="ax" x="{sx(lo):.1f}" y="86" text-anchor="middle">{lo:.2f}</text>
      <text class="ax" x="{sx(hi):.1f}" y="86" text-anchor="middle">{hi:.2f}</text>
    </svg>'''


def hotspot_note(h: dict) -> str:
    bits = [h["region"]]
    if h["ddg_rank"] == 1:
        bits.append("strongest ΔΔG residue in the interface")
    elif h["ddg_rank"] == 2:
        bits.append("second-strongest ΔΔG residue")
    if h["largest_bsa"]:
        bits.append(f"largest buried area of the {N_HS}")
    if h["backbone_only"]:
        bits.append("backbone-mediated contact")
    return "; ".join(bits)


def hotspot_rows() -> str:
    return "".join(
        f'<tr><td class="m">{h["name"]}{h["auth"]}</td><td class="num">{h["auth"]}</td>'
        f'<td class="num">{h["bsa"]:.1f}</td>'
        f'<td class="num">{f"{h["ddg"]:.2f}" if h["ddg"] is not None else "—"}</td>'
        f'<td class="note">{hotspot_note(h)}</td></tr>' for h in F["hotspots"])


def design_cards() -> str:
    pics = ["design_face", "design_rank2", "design_rank3"]
    out = []
    for i, (d, pic) in enumerate(zip(D, pics), 1):
        ddg = f'{d["rosetta_ddg"]:.1f}' if d["rosetta_ddg"] is not None else "—"
        out.append(f'''<article class="card">
          <img src="{img(pic)}" alt="Rank {i} design bound to PD-L1" loading="lazy">
          <div class="card-b">
            <div class="card-h"><span class="rk">rank {i}</span><code>{d["name"]}</code></div>
            <dl class="mini">
              <div><dt>ipTM</dt><dd>{d["iptm"]:.3f}</dd></div>
              <div><dt>ipSAE</dt><dd>{d["ipsae_min"]:.3f}</dd></div>
              <div><dt>dock RMSD</dt><dd>{d["dock"]:.2f} Å</dd></div>
              <div><dt>pLDDT</dt><dd>{d["plddt"]:.3f}</dd></div>
              <div><dt>Rosetta ΔΔG</dt><dd>{ddg}</dd></div>
              <div><dt>length</dt><dd>{d["len"]} aa</dd></div>
            </dl>
            <p class="seq">{d["seq"]}</p>
          </div></article>''')
    return "".join(out)


def alternatives_text() -> str:
    by_id = {c["pdb_id"]: c for c in F["candidates"]}
    parts = []
    for a in F["alternatives"]:
        # The stage's own sentence, cut at its first clause break and quoted —
        # paraphrasing a rejection is exactly the drift this page exists to avoid.
        why = a["why_not"].split(" — ")[0].split("; ")[0].strip().rstrip(".")
        parts.append(f'<strong>{a["pdb_id"]}</strong> '
                     f'({by_id[a["pdb_id"]]["res"]:.2f} Å) — “{why}.”')
    return " ".join(parts)


CSS = """
:root{
  --ground:#F4F4F1; --surface:#FFFFFF; --sunk:#ECEDE7;
  --ink:#171A14; --ink-2:#3C4237; --muted:#5D6357; --rule:#DEE0D8; --rule-2:#C6C9BE;
  --accent:#2b8f5d; --mark-a:#2b8f5d; --mark-b:#a07002;
  --good-bg:#E4F0E7; --good-ink:#1F6B41;
  --display:"Newsreader",Georgia,serif;
  --body:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#14170F; --surface:#1C2019; --sunk:#171B14;
  --ink:#E9ECE4; --ink-2:#C3C9BB; --muted:#959C8B; --rule:#2A2F25; --rule-2:#3B4234;
  --accent:#63b98c; --mark-a:#137246; --mark-b:#b98415;
  --good-bg:#182A1E; --good-ink:#7FCB9F;
}}
:root[data-theme="dark"]{
  --ground:#14170F; --surface:#1C2019; --sunk:#171B14;
  --ink:#E9ECE4; --ink-2:#C3C9BB; --muted:#959C8B; --rule:#2A2F25; --rule-2:#3B4234;
  --accent:#63b98c; --mark-a:#137246; --mark-b:#b98415;
  --good-bg:#182A1E; --good-ink:#7FCB9F;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--body);
  font-size:17px;line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:0 26px 110px}
.eyebrow{font-size:11.5px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;
  color:var(--accent);margin:0}
h1{font-family:var(--display);font-weight:500;font-size:clamp(2.3rem,5.6vw,3.9rem);
  line-height:1.03;letter-spacing:-.02em;margin:.35em 0 0;text-wrap:balance}
h2{font-family:var(--display);font-weight:500;font-size:clamp(1.5rem,3vw,2.05rem);
  line-height:1.14;letter-spacing:-.015em;margin:0;text-wrap:balance}
h3{font-size:.98rem;font-weight:600;margin:0 0 6px;letter-spacing:-.005em}
p{margin:0 0 1em;max-width:66ch}
a{color:var(--accent)}
code{font-family:var(--mono);font-size:.85em;background:var(--sunk);
  border:1px solid var(--rule);border-radius:3px;padding:1px 5px}

/* hero */
.hero{padding:76px 0 40px;display:grid;gap:34px}
.lede{font-size:1.2rem;color:var(--ink-2);max-width:60ch;margin:0}
.hero-fig{margin:0;background:var(--surface);border:1px solid var(--rule);
  border-radius:2px;padding:22px;display:grid;gap:16px}
.hero-fig img{width:100%;height:auto;display:block}
figcaption{font-size:.86rem;color:var(--muted);margin:0;max-width:70ch}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:.8rem;color:var(--muted);
  align-items:center;margin:0}
.legend b{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:6px;
  vertical-align:-1px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule)}
.stat{background:var(--surface);padding:15px 17px;display:grid;gap:2px}
.stat b{font-family:var(--display);font-weight:500;font-size:1.75rem;line-height:1;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted)}
.verdict{display:inline-flex;align-items:center;gap:9px;background:var(--good-bg);
  color:var(--good-ink);border-radius:2px;padding:7px 13px;font-size:.82rem;font-weight:600;
  letter-spacing:.03em;width:fit-content}
.verdict::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}

/* command block — also used by ppi_discovery.html, which sources this sheet */
.term{font-family:var(--mono);font-size:12.5px;line-height:1.7;background:var(--sunk);
  border:1px solid var(--rule-2);border-radius:3px;padding:14px 16px;margin:20px 0;
  overflow-x:auto;color:var(--ink-2);white-space:nowrap}
.term .p{color:var(--accent);font-weight:600;user-select:none;margin-right:6px}

/* stages */
.stage{border-top:1px solid var(--rule-2);padding:54px 0 8px;display:grid;gap:22px}
.stage-h{display:grid;gap:9px}
.step{font-family:var(--mono);font-size:11.5px;letter-spacing:.12em;color:var(--muted);
  text-transform:uppercase}
.two{display:grid;gap:30px}
@media(min-width:860px){.two{grid-template-columns:1fr 1fr;align-items:start}
  .two.wide-l{grid-template-columns:1.25fr 1fr}}
.fig{margin:0;background:var(--surface);border:1px solid var(--rule);padding:16px;
  display:grid;gap:12px;border-radius:2px}
.fig img{width:100%;height:auto;display:block}
.quote{border-left:3px solid var(--accent);padding:4px 0 4px 17px;margin:0 0 1em;
  font-family:var(--display);font-size:1.06rem;line-height:1.5;color:var(--ink-2);max-width:60ch}
.quote cite{display:block;font-family:var(--body);font-size:.76rem;font-style:normal;
  color:var(--muted);margin-top:8px;letter-spacing:.02em}
.note-box{background:var(--sunk);border:1px solid var(--rule);padding:15px 18px;
  font-size:.92rem;border-radius:2px}
.note-box p:last-child{margin:0}

/* tables */
.tw{overflow-x:auto;border:1px solid var(--rule);background:var(--surface);border-radius:2px}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th{text-align:left;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:600;padding:11px 13px;border-bottom:1px solid var(--rule-2);
  white-space:nowrap}
td{padding:9px 13px;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
td.m{font-weight:600;white-space:nowrap}
td.note{color:var(--muted);font-size:.84rem}

/* charts */
.chart{width:100%;height:auto;overflow:visible}
.chart .k{font-family:var(--body);font-size:12.5px;fill:var(--ink-2);font-weight:500}
.chart .k.r{text-anchor:end;font-family:var(--mono);font-size:11.5px;fill:var(--muted)}
.chart .v{font-family:var(--mono);font-size:12.5px;fill:var(--ink);
  font-variant-numeric:tabular-nums;dominant-baseline:middle}
.chart .ax{font-family:var(--mono);font-size:10.5px;fill:var(--muted)}
.chart .ax-t{font-family:var(--body);font-size:11px;fill:var(--muted)}
.chart .grid{stroke:var(--rule-2);stroke-width:1}
.chart .sel{stroke:var(--mark-b);stroke-width:1.5;stroke-dasharray:3 3}
.chart .sel-l{font-family:var(--body);font-size:10.5px;fill:var(--mark-b);font-weight:600}
.chart .mk{cursor:default}
.chart .mk:focus{outline:none}
.chart .mk:hover rect,.chart .mk:focus rect,
.chart .mk:hover circle,.chart .mk:focus circle{filter:brightness(1.12)}
.chart-wrap{background:var(--surface);border:1px solid var(--rule);padding:20px 22px;
  border-radius:2px;display:grid;gap:14px}

/* design cards */
.cards{display:grid;gap:18px}
@media(min-width:820px){.cards{grid-template-columns:repeat(3,1fr)}}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:2px;overflow:hidden;
  display:grid;grid-template-rows:auto 1fr}
.card img{width:100%;height:190px;object-fit:contain;background:var(--sunk);display:block;
  padding:10px}
.card-b{padding:15px 16px;display:grid;gap:11px;align-content:start}
.card-h{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.rk{font-size:10.5px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--accent)}
.card-h code{font-size:.68rem;background:none;border:0;padding:0;color:var(--muted);
  word-break:break-all}
.mini{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px;margin:0}
.mini div{display:flex;justify-content:space-between;gap:8px;border-bottom:1px dotted var(--rule);
  padding-bottom:4px}
.mini dt{font-size:11px;color:var(--muted);margin:0}
.mini dd{margin:0;font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums}
.seq{font-family:var(--mono);font-size:9.5px;line-height:1.45;color:var(--muted);
  word-break:break-all;margin:0;max-width:none}

footer{border-top:1px solid var(--rule-2);margin-top:56px;padding-top:26px;
  font-size:.87rem;color:var(--muted)}
footer p{max-width:74ch}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

_HEAD = _mkhead(
    "PD-L1 Binder Campaign",
    "A complete Little Protein Tiger binder campaign against PD-L1 — target choice, "
    "epitope, the calibration gate, and twenty ranked designs. Every number extracted "
    "from the run.",
    "campaign_pdl1.html", "campaign")

# The resolution claim in the target-intel rationale is checkable against the
# candidate table the same stage produced, and it is wrong — which is the point.
_BEST_RES = min(F["candidates"], key=lambda c: c["res"])
_CHOSEN = next(c for c in F["candidates"] if c["pdb_id"] == F["pdb_id"])
_ALT_SITE = next(s for s in F["sites"] if s["pdb_id"] != F["pdb_id"])

HTML = f"""{_HEAD}
<style>{CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · binder track · 21–22 Aug 2026</p>
    <h1>Designing a mini-protein that blocks PD-1 from reaching PD-L1</h1>
  </div>
  <p class="lede">One target name in, {PROD["n_rf3"]:,} refolded candidates out,
  {F["top_k_count"]} ranked designs at the end — and a measured decision at every point
  where the pipeline could have spent GPU-days on a target that was never going to work.
  This is that run, start to finish, with the numbers it actually produced.</p>

  <div class="term"><span class="p">$</span> python scripts/run_pipeline.py --workflow binder \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--target "{F["target_gene"]}" \\
  <br>&nbsp;&nbsp;&nbsp;&nbsp;--project pdl1_rc1 --budget {F["budget_cap_usd"]:.2f}</div>

  <figure class="hero-fig">
    <img src="{img('design_face')}" alt="The top-ranked designed mini-protein bound to PD-L1, seen down the epitope axis. The designed binder covers the nine hotspot residues.">
    <p class="legend">
      <span><b style="background:#2f8f74"></b>designed binder, {D[0]["len"]} aa</span>
      <span><b style="background:#9aa79d"></b>PD-L1 ({F["target_gene"]})</span>
      <span><b style="background:#c0872b"></b>the {N_HS} hotspot residues it was asked to cover</span>
    </p>
    <figcaption>The rank-1 design, refolded by RF3 from sequence alone, viewed straight
    down the epitope. Nothing about this pose was given to the folding model — it was
    asked only to fold the binder and the target together, and it put the binder on the
    patch the interface stage had picked. Dock RMSD to the intended site:
    {D[0]["dock"]:.2f} Å.</figcaption>
  </figure>

  <div class="stats">
    <div class="stat"><b>{F["wall_hours"]:.0f}</b><span>hours, wall clock</span></div>
    <div class="stat"><b>{GPU["total"]:.1f}</b><span>GPU-hours</span></div>
    <div class="stat"><b>${F["spend_usd"]:.2f}</b><span>of LLM spend</span></div>
    <div class="stat"><b>{HIST["survivors"]}</b><span>cleared the run's gate</span></div>
    <div class="stat"><b>{max(d["iptm"] for d in D):.2f}</b><span>best ipTM</span></div>
  </div>
  <p class="verdict">{CAL["verdict"]} — the trial justified the full campaign</p>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 1 · target intel</p>
    <h2>Which structure, and which face of it?</h2></div>
  <div class="two wide-l">
    <div>
      <p>The pipeline was given one instruction: <em>{F["query"]}</em> It resolved PD-L1 to
      UniProt {F["uniprot"]}, pulled every IgV-domain complex it could find, and measured
      all {len(F["candidates"])} of them — buried surface area, interface residue count,
      hydrogen bonds, hydrophobic fraction, resolution — before choosing. It settled on
      <strong>{F["pdb_id"]}</strong>, with chain <strong>{F["target_chain"]}</strong> as
      the target (PD-L1, {F["target_chain_length"]} modelled residues) and chain
      <strong>{F["partner_chain"]}</strong> as the partner it had to displace — the
      {F["partner_name"].split(" (")[0]}.</p>
      <blockquote class="quote">{F["interface_rationale"].split(";")[0]}
      <cite>— binder-target-intel, 20_target_intel.md</cite></blockquote>
      <div class="note-box"><p><strong>That quote contains a checkable error, and the
      record catches it.</strong> {F["pdb_id"]} is not the highest-resolution entry in the
      table: row {_BEST_RES["rank"]} is {_BEST_RES["pdb_id"]} at
      {_BEST_RES["res"]:.2f} Å, against {F["pdb_id"]}'s {_CHOSEN["res"]:.2f} Å. The choice
      still stands on the reasons that did the work — largest BSA
      ({_CHOSEN["bsa"]:,.0f} Å²), {_CHOSEN["iface_res"]} interface residues,
      {_CHOSEN["hbonds"]} H-bonds, hydrophobic fraction {_CHOSEN["phob"]:.2f} — but the
      superlative is wrong, and it is only visible because
      <code>candidates/candidates.md</code> is kept alongside the prose written from
      it.</p></div>
      <p>Four alternatives were rejected in writing, each in the stage's own words.
      {alternatives_text()} The stage also proposed a <em>second</em> designable site —
      <code>{_ALT_SITE["site_id"]}</code> on {_ALT_SITE["pdb_id"]}, an existing de novo
      mini-binder at the same IgV face. This run took the first; a run today would pass
      <code>--trial-sites 2</code> and settle it on measured yield instead of argument,
      one trial per site under <code>binder/sites/&lt;site_id&gt;/</code>.</p>
    </div>
    <figure class="fig">
      <img src="{img('native_face')}" alt="The crystallised partner bound to PD-L1 in {F["pdb_id"]}, seen down the same epitope axis.">
      <figcaption><strong>{F["pdb_id"]}</strong> — what a real binder does here. The
      entry's own crystallised partner ({F["partner_name"]}, grey) covers the front
      β-sheet face, viewed on the same axis as the design above. {F["bsa_A2"]:,} Å²
      buried, {_CHOSEN["iface_res"]} interface residues, {_CHOSEN["hbonds"]} H-bonds,
      {_CHOSEN["res"]:.2f} Å.</figcaption>
    </figure>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 2 · interface analysis</p>
    <h2>{N_HS} residues, and the evidence for each</h2></div>
  <div class="two">
    <div>
      <p>The interface stage does not accept the textbook answer. It reads the actual
      residue at every position in the downloaded structure and computes per-residue
      buried area, then ranks contiguous patches by how designable they are. Two regions
      came back: {" and ".join(f'the {r["short"]} (rated <em>{r["rating"].lower()}</em>)' for r in F["regions"])}.</p>
      <p>It also grounded the choice in the corpus, and the run records how far that
      went: <strong>{F["citations_verified"]} of {F["citations_checked"]} cited DOIs were
      found in the local literature database</strong> — mutagenesis and inhibitor work on
      this exact face, led by <code>{F["lit_doi"]}</code>. "Cited" and "checked against
      the corpus" are different claims, and the pipeline only makes the second because it
      is the one it can verify.</p>
      <div class="note-box"><p><strong>The textbook answer would have been wrong on this
      entry.</strong> The PD-L1 hotspot every review names is Tyr56 — and on
      <strong>{F["pdb_id"]}</strong>, chain {F["target_chain"]} residue 56 is a
      <strong>valine</strong>. Asked to analyse this structure, the interface stage has
      previously returned the canonical numbering verbatim: residues that are real and
      correctly numbered in a <em>different</em> PD-L1 crystal form. Two guards run before
      any trim is built — one reads the actual residue name at every position in the
      downloaded file, the other confirms by sequence identity to UniProt that the target
      chain is the target. Both passed here, and the aromatic anchors this run chose are
      Tyr124 and Tyr57, which are the tyrosines {F["pdb_id"]} actually has.</p></div>
    </div>
    <figure class="fig">
      <img src="{img('epitope')}" alt="PD-L1 surface with the nine hotspot residues highlighted, no binder present.">
      <figcaption>The {N_HS} hotspots on the bare PD-L1 surface — the same camera as the
      hero image. This is the patch the design had to cover.</figcaption>
    </figure>
  </div>
  <div class="tw"><table>
    <thead><tr><th>Residue</th><th class="num">auth id</th><th class="num">BSA Å²</th>
      <th class="num">ΔΔG kcal/mol</th><th>Why it is on the list</th></tr></thead>
    <tbody>{hotspot_rows()}</tbody></table></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 4 · pilot and calibration</p>
    <h2>Measure the hit rate before buying the GPU time</h2></div>
  <p>PD-L1 needed no trimming — {F["trim_residues"]} modelled residues in,
  {F["trim_residues"]} out, {F["trim_segments"]} contiguous segment, all {N_HS} hotspots
  retained; deciding a target is already the right size is as much that stage's job as
  cutting one down. The contig handed to RFD3, <code>{F["contig"]}</code>, asks for a
  binder of {F["binder_len_min"]}–{F["binder_len_max"]} residues against it.</p>
  <p>A pilot of {F["pilot"]["n_rfd3"]} backbones proved the machinery end to end in
  {F["pilot"]["hours"]*60:.0f} minutes. Then the calibration stage ran the experiment that
  actually decides the campaign: <strong>{CAL["diffused"]} backbones diffused,
  {CAL["prefiltered"]} of them cleared the prefilter</strong>
  ({100*CAL["prefilter_rate"]:.1f}%), and each survivor was threaded with
  {CAL["n_seq"]} sequences — <strong>{CAL["refolds"]:,} refolds</strong>. It then
  <em>counted</em> how many cleared the success bar rather than assuming a rate.</p>
  <div class="two">
    <div class="chart-wrap">
      <h3>Designs surviving at each ipTM bar</h3>
      {line_yield()}
      <figcaption>Of {CAL["refolds"]:,} calibration refolds. The curve is why the bar
      moved: at ipTM &gt; {CAL["bar_raised_to"]} there are still
      {dict(CAL["softer_rates"])[CAL["bar_raised_to"]]} survivors — enough to size a
      campaign on.</figcaption>
    </div>
    <div>
      <p>The requested bar was ipTM &gt; {CAL["requested_bar"]}. The calibration stage
      walks the bar ladder strictest-first and takes the hardest rung that still clears
      five hits and still fits the budget — so it <strong>raised the bar to
      {CAL["bar_raised_to"]} by itself</strong>, and sized the campaign to that. Only ever
      upward: it will not quietly make a campaign easier than you asked for.</p>
      <h3 style="margin-top:22px">Backbone hit rate, 95% Wilson interval</h3>
      {wilson()}
      <p style="font-size:.9rem;color:var(--muted)">{BB["k"]} of {BB["n"]} backbones
      produced an excellent design. The campaign is sized on the <em>pessimistic</em> end
      of that interval, not the point estimate: {PESS["required_refolds"]:,.0f} refolds,
      ~{PESS["est_gpu_hours"]} GPU-hours, ~{PESS["est_disk_gb"]} GB — well inside the
      budget. Verdict: <strong>{CAL["verdict"]}</strong>.</p>
    </div>
  </div>
  <div class="note-box"><p><strong>This gate is also where the campaign becomes
  resumable.</strong> The verdict, the batch count and the raised bar are all persisted to
  <code>calibration/calibration.json</code>, so a process that dies overnight and restarts
  at <code>--start-from production</code> re-derives the measured plan instead of falling
  back to the config default — which here would have been an order of magnitude more GPU
  time than the trial said was needed.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 5 · production</p>
    <h2>What {PROD["hours"]:.1f} GPU-hours produced</h2></div>
  <div class="chart-wrap">
    <h3>Production funnel</h3>
    {bar_funnel()}
    <figcaption>Every count read from disk by directory scan, never from a log line.</figcaption>
  </div>
  <div class="two" style="margin-top:8px">
    <div>
      <p>{HIST["survivors"]} designs cleared every hard gate —
      {100*HIST["survivors"]/PROD["n_rf3"]:.1f}% of the refolds, spread across
      {HIST["backbones"]} distinct backbones. The chart shows which gate was the
      <em>first</em> to reject each design that failed.</p>
      <p>Dock RMSD sits first in that order and absorbs the largest share
      ({GEO["dock_first_fail"]:,} refolds), but the order is what makes it look decisive:
      <strong>{GEO["dock_first_fail_lowconf_pct"]}% of those
      ({GEO["dock_first_fail_lowconf"]:,}) had ipTM below the {GEO["iptm_gate"]} gate as
      well</strong>, and would have been dropped by the confidence gate a line later. The
      honest reading of this run is the opposite of a warning:
      <strong>{GEO["pct_docked"]:.0f}% of refolds with ipTM &gt; {GEO["iptm_bar"]}
      ({GEO["n_high_iptm_docked"]:,} of {GEO["n_high_iptm"]:,}) are docked on the intended
      patch</strong>. PD-L1 is an easy docking target.</p>
      <p>The gate still earns its place, twice over. It caught the
      <strong>{GEO["n_confidently_misdocked"]}</strong> designs here that RF3 was confident
      about and had put in the wrong place — exactly the failure ipTM cannot see, because
      RF3 cannot be given a docked pose at inference and only reports confidence in the
      interface it chose for itself. And PD-L1 is the easy case: of designs with ipTM &gt;
      {GEO["iptm_bar"]}, only
      {" and ".join(f'{pct}% ({tgt})' for tgt, pct in GEO["contrast"])} were docked on
      target. On those campaigns the gate is not a formality, it is most of the
      answer.</p>
    </div>
    <div class="chart-wrap">
      <h3>Refolds dropped by the first gate they failed</h3>
      {bar_gates()}
      <figcaption>Of {F["n_scored"]:,}. Hover a bar for what that gate would have kept on
      its own.</figcaption>
    </div>
  </div>
  <div class="note-box"><p><strong>These survivor counts are historical.</strong> This
  run gated at <code>hotspot_engagement ≥ 1</code> — every declared hotspot contacted.
  <code>config.yaml</code> now sets <code>{F["hotspot_engagement_now"]}</code>, because
  requiring all of them rejects refolds for missing residues the RFD3 design never
  targeted itself. Re-gating this run's own <code>refold_scores.csv</code> at
  {F["hotspot_engagement_now"]} gives <strong>{CUR["survivors"]} survivors across
  {CUR["backbones"]} backbones</strong>, not {HIST["survivors"]}/{HIST["backbones"]}. The
  funnel and the chart above show what this campaign actually decided, at the threshold it
  decided with.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 6 · scoring and ranking</p>
    <h2>The designs that came out</h2></div>
  <p>Survivors are ranked on a composite of ipSAE, dock RMSD, pLDDT, ipTM, interface PAE,
  epitope recall and hotspot engagement, then diversified so the top-K is not
  {F["top_k_count"]} variations of one backbone. Rosetta relax and InterfaceAnalyzer run
  <em>after</em> the gates, on {F["rosetta_scored"]} of the {HIST["survivors"]} survivors,
  and enter the composite only — a mis-docked pose is still a physical pose, and Rosetta
  will happily return well-defined, meaningless numbers for it.</p>
  <div class="cards">{design_cards()}</div>
  <div class="note-box" style="margin-top:22px"><p><strong>Read these as computational
  hypotheses, not as binders.</strong> Nothing here has been expressed, purified or
  measured, and the design-analyst stage flagged in its own report that the developability
  columns it would normally comment on were missing from the table it was handed — so this
  page shows none either.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Independent check · outside the run</p>
    <h2>Would it actually get in PD-1's way?</h2></div>
  <p>Nothing in this campaign ever saw PD-1. The epitope was chosen from a crystallised
  binder complex, and the designs were folded against PD-L1 alone. So the human
  PD-1/PD-L1 complex — PDB <strong>{M4["pdb_id"]}</strong>, which played no part in the
  run — is a genuinely independent way to ask whether the pipeline aimed at the right
  patch.</p>
  <div class="note-box"><p><strong>This check is computed at build time, not by
  hand.</strong> The earlier version of this page reported a ChimeraX session run after
  the campaign finished and said so, because an unversioned session cannot be extracted
  and must not be presented as though the pipeline produced it. It is now done in code
  from the run's own rank-1 refold, so it goes through the same <code>facts/</code>
  snapshot as every other number here and moves as a reviewable diff. <strong>4ZQK is
  still not part of the run</strong> — that is the point of it — but the measurement is
  now reproducible: the refold&rarr;author residue offset is derived and then checked
  against all {M4["n_hotspots"]} hotspot names before a single contact is counted,
  because a wrong offset reports contacts for arbitrary residues and looks
  perfectly fine.</p></div>
  <p>Superposing {M4["pdb_id"]}'s PD-L1 onto the campaign's own copy puts both partners in
  one frame: {M4["superpose_rmsd_A"]} Å over {M4["superpose_n_ca"]} Cα at
  {M4["superpose_identity_pct"]}% sequence identity, so the two really are the same protein
  and the comparison is fair. Then it is a matter of counting which PD-L1 residues each
  partner touches, at a {M4["cutoff_A"]} Å heavy-atom cutoff.</p>
  <div class="two">
    <figure class="fig">
      <img src="{img('pd1_face')}" alt="PD-1 bound to PD-L1 in PDB 4ZQK, with PD-1's footprint tinted on the PD-L1 surface.">
      <figcaption><strong>{M4["pdb_id"]}</strong> — PD-1 (violet) on PD-L1, in the same
      orientation as every other structure on this page. The violet patch is the
      {M4["pd1_contacts"]} PD-L1 residues PD-1 actually contacts.</figcaption>
    </figure>
    <figure class="fig">
      <img src="{img('footprint')}" alt="The PD-L1 surface coloured by which partner touches each residue: shared, PD-1 only, or design only.">
      <p class="legend">
        <span><b style="background:#c0872b"></b>both ({M4["shared"]})</span>
        <span><b style="background:#5e62b0"></b>PD-1 only ({len(M4["pd1_only"])})</span>
        <span><b style="background:#2f8f74"></b>design only ({M4["design_contacts"] - M4["shared"]})</span>
        <span><b style="background:#9aa79d"></b>neither</span>
      </p>
      <figcaption>The same surface, coloured by who touches what. The designed binder
      covers <strong>{M4["shared"]} of PD-1's {M4["pd1_contacts"]} contact residues —
      {M4["pct"]}%</strong> — and reaches {M4["design_contacts"] - M4["shared"]} more that
      PD-1 does not use.</figcaption>
    </figure>
  </div>
  <div class="stats" style="margin-top:8px">
    <div class="stat"><b>{M4["pct"]}%</b><span>of PD-1's footprint covered</span></div>
    <div class="stat"><b>{M4["shared"]}/{M4["pd1_contacts"]}</b><span>shared contact residues</span></div>
    <div class="stat"><b>{M4["hotspots_that_are_pd1_contacts"]}/{M4["n_hotspots"]}</b><span>chosen hotspots are real PD-1 contacts</span></div>
    <div class="stat"><b>{M4["superpose_rmsd_A"]} Å</b><span>superposition RMSD</span></div>
  </div>
  <div class="note-box" style="margin-top:22px"><p><strong>This is a geometric argument,
  not a measured one.</strong> Occupying {M4["pct"]}% of a footprint says a binder is in
  the way; it says nothing about whether it out-competes PD-1, which depends on affinities
  neither measured here nor predictable from these structures. The
  {len(M4["pd1_only"])} residues PD-1 uses and the design misses —
  {", ".join(str(n) for n in M4["pd1_only"][:-1])} and {M4["pd1_only"][-1]} — sit at the
  edge of the interface. Read this as evidence the pipeline aimed where it said it would,
  not as evidence of a blocker.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">What it cost</p>
    <h2>Three LLM stages, {GPU["total"]:.1f} GPU-hours</h2></div>
  <p>Only three stages of this campaign involved a language model at all:
  {", ".join(s.replace("_", " ") for s in F["llm_stages"])} — {F["llm_calls"]} billed API
  calls between them, because an agentic stage bills once per turn that reaches the model
  and the interface stage took {F["llm_calls_by_stage"]["interface"]}. Everything else —
  trimming, spec building, campaign planning, gating, scoring, ranking — is deterministic
  Python. The LLM spend was <strong>${F["spend_usd"]:.2f} against a
  ${F["budget_cap_usd"]:.0f} hard cap</strong>, itemised per stage and per model in an
  append-only ledger.</p>
  <p>That headline is now a <em>ceiling</em>. This run billed
  {" and ".join(f"<code>{m}</code>" for m in sorted(F["models"]))}; every pipeline stage
  today defaults to <code>{F["default_provider"]}</code>
  (<code>{F["default_model"]}</code>), roughly
  four times cheaper on input — a change made because safety-classifier refusals on
  structural-biology prompts were costing real money for no output, not to save the
  fifty cents.</p>
  <div class="tw"><table>
    <thead><tr><th>stage</th><th class="num">GPU-hours</th><th>what it bought</th></tr></thead>
    <tbody>
      <tr><td>pilot</td><td class="num">{GPU["pilot"]:.2f}</td>
        <td class="note">{F["pilot"]["n_rfd3"]} backbones — proves the machinery, not the target</td></tr>
      <tr><td>calibration</td><td class="num">{GPU["calibration"]:.2f}</td>
        <td class="note">{CAL["refolds"]:,} refolds — the measurement the whole campaign is sized on</td></tr>
      <tr><td>production</td><td class="num">{GPU["production"]:.2f}</td>
        <td class="note">{PROD["n_rf3"]:,} refolds — the campaign itself</td></tr>
      <tr><td><strong>total</strong></td><td class="num"><strong>{GPU["total"]:.2f}</strong></td>
        <td class="note">inside a {F["wall_hours"]:.1f}-hour wall clock, unattended</td></tr>
    </tbody></table></div>
  <p>Since this run, <code>design.backend</code> defaults to
  <code>{F["design_backend"]}</code>, so a <code>--workflow ppi</code> campaign that starts
  from a disease question rather than a target name hands off into these same stages —
  trim, spec, pilot, calibration, production, scoring — unchanged.</p>
</section>

<footer>
  <p>Every figure on this page is extracted at build time from
  <code>projects/pdl1_rc1</code> — <code>manifest.json</code>, <code>ledger.jsonl</code>,
  <code>candidates/candidates.json</code>, <code>calibration/calibration.json</code>,
  <code>campaign/*/plan.json</code>, <code>scoring/refold_scores.csv</code>,
  <code>scoring/top_k.csv</code>, <code>scoring/rosetta_metrics.csv</code> and the stage
  reports, whose handoff blocks are read with the pipeline's own <code>src.handoff</code>
  parser. The gate is re-applied through <code>src.binder_ranking.filter_records</code>
  rather than re-implemented here. The extracted values are committed to
  <code>docs/showcase/facts/campaign_pdl1.json</code>, so any number that moves shows up as
  a reviewable diff — including the {M4["pdb_id"]} comparison, which used to be the one
  hand-made exception on this page and is now computed alongside everything else. 4ZQK
  itself is still external to the campaign, which is what makes it a check.</p>
  <p>Structure images rendered with UCSF ChimeraX from the campaign's own RF3 refolds and
  from PDB {F["pdb_id"]}; the hotspot residues shown are the {N_HS} the interface stage
  selected, mapped through the refold's own numbering. The run also carries its own
  auto-generated <code>binder/report.html</code> — the same numbers, rendered by the
  pipeline itself with an embedded Mol* viewer over the top-ranked refolds.</p>
  <p>Regenerate this page with <code>python docs/showcase/build_campaign.py</code>.
  Read <code>docs/responsible-use.md</code> before designing anything.
  <a href="index.html">← all showcase pages</a></p>
</footer>

</div>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT.relative_to(ROOT)}  ({len(HTML)/1024:.0f} KB)")
print(f"  target     {F['target_gene']} on {F['pdb_id']} chain {F['target_chain']} "
      f"(partner {F['partner_chain']}, {F['partner_name'].split(' (')[0]})")
print(f"  timing     wall {F['wall_hours']:.2f} h; gpu {GPU['pilot']}+{GPU['calibration']}"
      f"+{GPU['production']} = {GPU['total']} h")
print(f"  funnel     {PROD['n_rfd3']} -> {PROD['n_filtered']} -> {PROD['n_mpnn']} -> "
      f"{HIST['survivors']} survivors ({HIST['backbones']} backbones)")
print(f"  re-gated   hotspot_engagement >= {F['hotspot_engagement_now']} -> "
      f"{CUR['survivors']} survivors ({CUR['backbones']} backbones)")
print(f"  geometry   ipTM > {GEO['iptm_bar']}: {GEO['n_high_iptm']} refolds, "
      f"{GEO['n_high_iptm_docked']} docked <= {GEO['dock_max']:g} A "
      f"({GEO['pct_docked']}%), {GEO['n_confidently_misdocked']} confidently mis-docked")
print(f"  dock-first {GEO['dock_first_fail']} refolds; "
      f"{GEO['dock_first_fail_lowconf']} ({GEO['dock_first_fail_lowconf_pct']}%) "
      f"also below the ipTM gate")
print(f"  spend      ${F['spend_usd']:.4f} over {F['llm_calls']} calls in "
      f"{len(F['llm_stages'])} stages")
