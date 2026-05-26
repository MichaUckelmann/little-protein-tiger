#!/usr/bin/env python3
"""Score the side-by-side fingerprints produced by compare_providers.py.

Reads data/fingerprints/_provider_compare/<provider>/*.json + _summary.json,
emits a per-paper scorecard and a per-provider aggregate, writes REPORT.md.

Quality metrics (each fingerprint contributes 0..n points):
  - n_findings              : count of key_findings (capped at 5 by prompt)
  - pair_clean              : findings whose protein_pair contains exactly two
                              protein-name strings, no residues/domains/SMILES.
  - residues_with_position  : findings where >=1 residue carries a numeric pos
                              (e.g. "Phe69" rather than "Phe").
  - kd_captured             : findings with non-null affinities_kd_Molar OR
                              inhibitory_constant_Ki.
  - has_source_span         : findings with source_span set.
  - hook_words              : 100..150 target; report mean.
  - pdb_count               : len(paper_metadata.pdb_accessions).
  - study_category_filled   : 1 if field is set to a valid enum, else 0.
  - pathway_context_present : 1 if study_category=pathway_biology AND
                              pathway_context is populated.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_OUT = ROOT / "data" / "fingerprints" / "_provider_compare"

THREE_LETTER_AA = {
    "Ala", "Arg", "Asn", "Asp", "Cys", "Gln", "Glu", "Gly", "His", "Ile",
    "Leu", "Lys", "Met", "Phe", "Pro", "Ser", "Thr", "Trp", "Tyr", "Val",
}
AA_REGEX = re.compile(
    r"\b(" + "|".join(THREE_LETTER_AA) + r")\s*\d+", re.IGNORECASE
)
DOMAIN_SUFFIX = re.compile(r"-(TBD|BD|SH2|SH3|RBD|KD|PDZ|TAD|CRD|DBD)\b", re.IGNORECASE)
RESIDUE_POS = re.compile(r"\b[A-Za-z]{1,4}\s*\d+", re.IGNORECASE)
VALID_CATEGORIES = {
    "biochemistry", "pathway_biology", "structural_biology",
    "host_pathogen", "clinical", "review",
}

PROVIDERS = ["claude", "gemini", "local"]


def is_pair_clean(pair: list | None) -> tuple[bool, str]:
    """Return (clean, reason)."""
    if not pair or not isinstance(pair, list):
        return False, "empty/non-list"
    if len(pair) != 2:
        return False, f"len={len(pair)}"
    for member in pair:
        if member is None or not isinstance(member, str):
            return False, "non-string member"
        if AA_REGEX.search(member):
            return False, f"contains residue: {member!r}"
        # Domain suffix like "YAP-TBD" is allowed as 1 entry; but the prompt
        # examples say a domain is NOT a separate protein from its parent.
        # We only flag if BOTH members differ only by domain suffix.
    if len(pair) == 2 and isinstance(pair[0], str) and isinstance(pair[1], str):
        base0 = DOMAIN_SUFFIX.sub("", pair[0]).strip().upper()
        base1 = DOMAIN_SUFFIX.sub("", pair[1]).strip().upper()
        if base0 == base1 and (DOMAIN_SUFFIX.search(pair[0]) or DOMAIN_SUFFIX.search(pair[1])):
            return False, f"domain-of-self: {pair}"
    return True, ""


def residue_has_position(r: str) -> bool:
    if not r:
        return False
    return bool(RESIDUE_POS.search(r))


def score_fingerprint(fp: dict) -> dict:
    findings = fp.get("key_findings") or []
    pair_violations = []   # real violations: residue-in-pair, domain-of-self
    pair_missing = []      # findings without any protein_pair (incompleteness)
    pair_clean_count = 0
    residue_pos_count = 0
    kd_count = 0
    source_span_count = 0
    pm = fp.get("paper_metadata") or {}
    hook = pm.get("situational_context_hook") or ""
    pdbs = pm.get("pdb_accessions") or []
    cat = fp.get("study_category")
    pathway_ctx = fp.get("pathway_context") or {}

    for f in findings:
        pair = f.get("protein_pair")
        clean, reason = is_pair_clean(pair)
        if clean:
            pair_clean_count += 1
        else:
            if not pair or not isinstance(pair, list):
                pair_missing.append({"pair": pair, "why": reason})
            else:
                pair_violations.append({"pair": pair, "why": reason})
        if any(residue_has_position(r) for r in (f.get("key_amino_acid_residues") or [])):
            residue_pos_count += 1
        if f.get("affinities_kd_Molar") is not None or f.get("inhibitory_constant_Ki") is not None:
            kd_count += 1
        if f.get("source_span"):
            source_span_count += 1

    return {
        "relevant": fp.get("relevant"),
        "n_findings": len(findings),
        "pair_clean": pair_clean_count,
        "pair_violations": pair_violations,
        "pair_missing": pair_missing,
        "residues_with_position": residue_pos_count,
        "kd_captured": kd_count,
        "has_source_span": source_span_count,
        "hook_words": len(hook.split()) if hook else 0,
        "pdb_count": len(pdbs),
        "study_category": cat,
        "study_category_valid": cat in VALID_CATEGORIES,
        "pathway_context_present": (
            cat == "pathway_biology"
            and bool(pathway_ctx)
            and (
                bool(pathway_ctx.get("pathways"))
                or bool(pathway_ctx.get("target_nodes"))
                or bool(pathway_ctx.get("disease_associations"))
            )
        ),
    }


def load_summary() -> dict:
    p = ROOT_OUT / "_summary.json"
    if not p.exists():
        sys.exit(f"missing {p} — run compare_providers.py first")
    return json.loads(p.read_text())


def safe_key(k: str) -> str:
    return k.replace(":", "_").replace("/", "_")


def main():
    summary = load_summary()
    runs = summary["runs"]

    # Collect all paper_keys that succeeded for at least one provider
    paper_keys = sorted({r["paper_key"] for r in runs})
    by_provider_paper: dict[tuple[str, str], dict] = {}
    for r in runs:
        if r.get("ok"):
            fp_path = ROOT_OUT / r["provider"] / f"{safe_key(r['paper_key'])}.json"
            if fp_path.exists():
                fp = json.loads(fp_path.read_text())
                by_provider_paper[(r["provider"], r["paper_key"])] = {
                    "fp": fp, "meta": r, "score": score_fingerprint(fp),
                }
        else:
            by_provider_paper[(r["provider"], r["paper_key"])] = {
                "fp": None, "meta": r, "score": None,
            }

    PPI_HEAVY = {
        "doi:10.1038/s41467-026-68319-1",
        "doi:10.1016/j.cell.2012.02.013",
        "doi:10.1126/science.abi6226",
        "doi:10.7554/eLife.36307",
        "doi:10.1038/s41586-023-06788-w",
    }

    lines: list[str] = []
    lines.append("# Provider comparison: Claude Haiku 4.5 vs Gemini 3.1 Flash Lite vs Gemma 4 26B-A4B")
    lines.append("")
    lines.append("Generated by `scripts/score_provider_compare.py`.")
    lines.append("")
    lines.append("Two sets:")
    lines.append("- **PPI-heavy** — 5 papers picked because Claude had previously produced rich `protein_pair`/`residues`/`Kd` findings.")
    lines.append("- **Random** — 5 papers from the uncurated backlog (seed=42).")
    lines.append("")
    lines.append("Same `curation_prompt.md` (schema v2.0), same input text, same `max_input_chars=150000`.")
    lines.append("")

    # --- Key takeaways (computed) ---
    agg = {}
    for prov in PROVIDERS:
        prov_runs = [v for (p, _), v in by_provider_paper.items() if p == prov and v["score"] is not None]
        n_relevant = sum(1 for v in prov_runs if v["fp"] and v["fp"].get("relevant"))
        agg[prov] = {
            "ok": len(prov_runs),
            "relevant_count": n_relevant,
            "wall_avg": statistics.mean([v["meta"]["wall_s"] for v in prov_runs if v["meta"].get("wall_s")]) if prov_runs else 0,
            "in_tok_total": sum(v["meta"].get("input_tokens", 0) for v in prov_runs),
            "out_tok_total": sum(v["meta"].get("output_tokens", 0) for v in prov_runs),
            "findings_total": sum(v["score"]["n_findings"] for v in prov_runs),
            "pair_violations": sum(len(v["score"]["pair_violations"]) for v in prov_runs),
            "pair_missing": sum(len(v["score"]["pair_missing"]) for v in prov_runs),
            "res_pos_total": sum(v["score"]["residues_with_position"] for v in prov_runs),
            "kd_total": sum(v["score"]["kd_captured"] for v in prov_runs),
            "category_set": sum(1 for v in prov_runs if v["score"]["study_category_valid"]),
        }

    lines.append("## Key takeaways")
    lines.append("")
    fastest = min(PROVIDERS, key=lambda p: agg[p]["wall_avg"] or 1e9)
    most_findings = max(PROVIDERS, key=lambda p: agg[p]["findings_total"])
    most_residues = max(PROVIDERS, key=lambda p: agg[p]["res_pos_total"])
    most_kd = max(PROVIDERS, key=lambda p: agg[p]["kd_total"])
    lines.append(f"- **Speed**: `{fastest}` fastest at {agg[fastest]['wall_avg']:.1f}s/paper avg vs the others (~{agg['claude']['wall_avg']:.0f}s claude, ~{agg['local']['wall_avg']:.0f}s local).")
    lines.append(f"- **Most findings extracted**: `{most_findings}` ({agg[most_findings]['findings_total']} across 10 papers).")
    lines.append(f"- **Residue positions captured** (e.g. \"Phe69\" not just \"Phe\"): `{most_residues}` wins with {agg[most_residues]['res_pos_total']}/{agg[most_residues]['findings_total']} findings.")
    lines.append(f"- **Kd / Ki numbers captured**: `{most_kd}` wins with {agg[most_kd]['kd_total']}/{agg[most_kd]['findings_total']} findings.")
    lines.append(f"- **Relevance gating**: claude gated {10 - agg['claude']['relevant_count']} of 10 papers as irrelevant; gemini {10 - agg['gemini']['relevant_count']}; local {10 - agg['local']['relevant_count']}.")
    lines.append(f"- **pair_clean discipline** (post-patch): violations claude={agg['claude']['pair_violations']}, gemini={agg['gemini']['pair_violations']}, local={agg['local']['pair_violations']}. Findings *missing* protein_pair entirely: claude={agg['claude']['pair_missing']}, gemini={agg['gemini']['pair_missing']}, local={agg['local']['pair_missing']}.")
    lines.append(f"- **Cost-relevant**: claude {agg['claude']['in_tok_total']:,} in / {agg['claude']['out_tok_total']:,} out tokens; gemini {agg['gemini']['in_tok_total']:,} / {agg['gemini']['out_tok_total']:,}; local {agg['local']['in_tok_total']:,} / {agg['local']['out_tok_total']:,} (local in-tok is low because Ollama returns prompt_tokens after truncation to num_ctx).")
    lines.append("")

    # --- Per-provider aggregate ---
    lines.append("## Per-provider aggregate")
    lines.append("")
    lines.append("| Provider | OK | Avg wall (s) | Σ in-tok | Σ out-tok | Σ findings | pair_clean | res+pos | Kd ✓ | source_span | category set | hook µ-words |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for prov in PROVIDERS:
        prov_runs = [v for (p, _), v in by_provider_paper.items() if p == prov]
        ok = sum(1 for v in prov_runs if v["score"] is not None)
        if ok == 0:
            lines.append(f"| {prov} | 0/{len(prov_runs)} | — | — | — | — | — | — | — | — | — | — |")
            continue
        walls = [v["meta"]["wall_s"] for v in prov_runs if v["meta"].get("ok")]
        in_tok = sum(v["meta"].get("input_tokens", 0) for v in prov_runs if v["meta"].get("ok"))
        out_tok = sum(v["meta"].get("output_tokens", 0) for v in prov_runs if v["meta"].get("ok"))
        findings = sum(v["score"]["n_findings"] for v in prov_runs if v["score"])
        clean = sum(v["score"]["pair_clean"] for v in prov_runs if v["score"])
        residpos = sum(v["score"]["residues_with_position"] for v in prov_runs if v["score"])
        kd = sum(v["score"]["kd_captured"] for v in prov_runs if v["score"])
        ss = sum(v["score"]["has_source_span"] for v in prov_runs if v["score"])
        cat = sum(1 for v in prov_runs if v["score"] and v["score"]["study_category_valid"])
        hook_avg = statistics.mean([v["score"]["hook_words"] for v in prov_runs if v["score"] and v["score"]["hook_words"]]) if findings else 0
        lines.append(
            f"| {prov} | {ok}/{len(prov_runs)} | {statistics.mean(walls):.1f} | "
            f"{in_tok:,} | {out_tok:,} | {findings} | "
            f"{clean}/{findings} | {residpos}/{findings} | {kd}/{findings} | "
            f"{ss}/{findings} | {cat}/{ok} | {hook_avg:.0f} |"
        )
    lines.append("")

    # --- PPI-heavy detail ---
    lines.append("## PPI-heavy set — per-paper breakdown")
    lines.append("")
    for key in [k for k in paper_keys if k in PPI_HEAVY]:
        lines.append(f"### {key}")
        lines.append("")
        lines.append("| | n_findings | pair_clean | res+pos | Kd ✓ | category | hook words | PDBs | wall (s) |")
        lines.append("|---|---:|---:|---:|---:|---|---:|---:|---:|")
        for prov in PROVIDERS:
            v = by_provider_paper.get((prov, key))
            if v is None or v["score"] is None:
                err = (v or {}).get("meta", {}).get("error", "missing")
                lines.append(f"| {prov} | — | — | — | — | — | — | — | (FAIL: {err[:60]}) |")
                continue
            s = v["score"]
            cat = s["study_category"] or "—"
            lines.append(
                f"| {prov} | {s['n_findings']} | {s['pair_clean']}/{s['n_findings']} | "
                f"{s['residues_with_position']}/{s['n_findings']} | "
                f"{s['kd_captured']}/{s['n_findings']} | {cat} | {s['hook_words']} | "
                f"{s['pdb_count']} | {v['meta'].get('wall_s', '?')} |"
            )
        # Raw protein_pair values (side-by-side scan)
        lines.append("")
        lines.append("<details><summary>protein_pair values per finding</summary>")
        lines.append("")
        for prov in PROVIDERS:
            v = by_provider_paper.get((prov, key))
            if v and v["fp"]:
                findings = v["fp"].get("key_findings") or []
                pairs_str = "; ".join(str(f.get("protein_pair")) for f in findings)
                lines.append(f"- **{prov}**: {pairs_str}")
        lines.append("")
        lines.append("</details>")
        # protein_pair violations detail
        any_violations = False
        for prov in PROVIDERS:
            v = by_provider_paper.get((prov, key))
            if v and v["score"] and v["score"]["pair_violations"]:
                if not any_violations:
                    lines.append("")
                    lines.append("**protein_pair violations (residue-in-pair or domain-of-self):**")
                    any_violations = True
                for pv in v["score"]["pair_violations"]:
                    lines.append(f"- `{prov}`: {pv['pair']} — {pv['why']}")
        lines.append("")

    # --- Random set detail ---
    lines.append("## Random uncurated set — per-paper breakdown")
    lines.append("")
    for key in [k for k in paper_keys if k not in PPI_HEAVY]:
        lines.append(f"### {key}")
        lines.append("")
        lines.append("| | relevant | n_findings | pair_clean | res+pos | Kd ✓ | category | pathway_ctx | hook words | wall (s) |")
        lines.append("|---|---|---:|---:|---:|---:|---|---|---:|---:|")
        for prov in PROVIDERS:
            v = by_provider_paper.get((prov, key))
            if v is None or v["score"] is None:
                err = (v or {}).get("meta", {}).get("error", "missing")
                lines.append(f"| {prov} | — | — | — | — | — | — | — | — | (FAIL: {err[:60]}) |")
                continue
            s = v["score"]
            cat = s["study_category"] or "—"
            pc = "yes" if s["pathway_context_present"] else ("n/a" if cat != "pathway_biology" else "MISSING")
            lines.append(
                f"| {prov} | {s['relevant']} | {s['n_findings']} | "
                f"{s['pair_clean']}/{s['n_findings']} | "
                f"{s['residues_with_position']}/{s['n_findings']} | "
                f"{s['kd_captured']}/{s['n_findings']} | {cat} | {pc} | "
                f"{s['hook_words']} | "
                f"{v['meta'].get('wall_s') if v['meta'].get('wall_s') is not None else '?'} |"
            )
        # Raw protein_pair values for random papers too
        lines.append("")
        lines.append("<details><summary>protein_pair values per finding</summary>")
        lines.append("")
        for prov in PROVIDERS:
            v = by_provider_paper.get((prov, key))
            if v and v["fp"]:
                findings = v["fp"].get("key_findings") or []
                if findings:
                    pairs_str = "; ".join(str(f.get("protein_pair")) for f in findings)
                    lines.append(f"- **{prov}**: {pairs_str}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    out = ROOT_OUT / "REPORT.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
