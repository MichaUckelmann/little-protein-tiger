"""
Deterministic, illustrated HTML run report for the PPI track (pathway ->
literature -> structure -> design -> execution -> analysis -> summary).

Same shape and philosophy as :mod:`src.binder_report`: no LLM in the loop —
everything here is a reader over files the pipeline already wrote (the
seven stage markdown files, ``05_ranking/{ranked,top_k}.csv``, the boltzgen
refold CIFs) rendered into one self-contained HTML page with an embedded
Mol* structure explorer. Where the report needs "why" prose (why this
target, why this site, why these designs), it renders the LLM stage's own
markdown report verbatim through the ``markdown`` package rather than
re-deriving new text — a deterministic stage has no business inventing
narrative, and the skills already write that narrative for a human reader.

Public entry point: :func:`build_report`.

Reuses (never re-implements) the pipeline's own logic:
    - :mod:`src.handoff` for the PIPELINE HANDOFF / MODEL-READY HOTSPOTS
      parsers pipeline_runner.py itself uses.
    - :mod:`src.report_common` for the markdown/citation-section readers
      shared with :mod:`src.binder_report`.
    - :mod:`src.design_ranking` for the hard-filter gate a completed run was
      scored with (``filter_records``), so the report's funnel chart can
      never drift from what the real analysis stage would produce.
"""

from __future__ import annotations

import base64
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import handoff as handoff_mod
from src.design_ranking import FilterStats
from src.report_common import (
    escape_html,
    ReportError,
    as_float as _as_float,
    extract_citation_section as _extract_citation_section,
    histogram as _histogram,
    markdown_html as _markdown_html,
    read_text as _read_text,
    section_before_handoff as _section_before_handoff,
)

_ROOT = Path(__file__).resolve().parent.parent
_MOLSTAR_DIR = _ROOT / "assets" / "vendor" / "molstar"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "report_templates" / "ppi_report"
_SHARED_DIR = Path(__file__).resolve().parent / "report_templates" / "_shared"

_STAGE_FILES = {
    "pathway": "00_pathway.md",
    "literature": "01_literature.md",
    "structure": "02_structure.md",
    "design": "03_design_report.md",
    "execution": "04_execution.md",
    "analysis": "05_analysis.md",
    "summary": "06_summary.md",
}


# ---------------------------------------------------------------------
# small text helpers specific to this track's stage shape
# ---------------------------------------------------------------------

def _text_before(text: str, marker_pattern: str) -> str:
    """Everything before the first line matching `marker_pattern` (regex)."""
    m = re.search(marker_pattern, text, re.IGNORECASE | re.MULTILINE)
    return text[:m.start()] if m else text


def _extract_verdict(summary_text: str) -> tuple[str | None, str]:
    """('GO'|'NO_GO'|'CONDITIONAL_GO'|None, one-line reason) from the
    design-analyst summary's "## 1. Executive verdict" section — the same
    free-text convention `_stage_summary`'s prompt asks the skill to follow,
    never a machine-readable field (this stage has no PIPELINE HANDOFF)."""
    m = re.search(r"##\s*1\.\s*Executive verdict\s*\n+(.*?)(?=\n##|\Z)",
                  summary_text, re.DOTALL | re.IGNORECASE)
    body = m.group(1).strip() if m else summary_text.strip()
    vm = re.search(r"\*\*\s*(CONDITIONAL[ _-]?GO|NO[ _-]?GO|GO)\s*\.?\s*\*\*", body, re.IGNORECASE)
    verdict = vm.group(1).upper().replace("-", "_").replace(" ", "_") if vm else None
    # First sentence after the verdict marker, or the first line, as the reason.
    reason = body
    if vm:
        reason = body[vm.end():].strip()
    reason = re.split(r"(?<=[.!?])\s", reason.strip(), maxsplit=1)[0][:400] if reason else ""
    return verdict, reason


def _extract_choices(pathway_handoff: dict) -> list[dict]:
    try:
        choices = json.loads(pathway_handoff.get("choices_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    return choices if isinstance(choices, list) else []


# ---------------------------------------------------------------------
# loading structured pipeline output
# ---------------------------------------------------------------------

def _load_pathway(run_dir: Path) -> tuple[dict, str, list[dict]]:
    text = _read_text(run_dir / _STAGE_FILES["pathway"])
    if not text:
        return {}, "", []
    handoff = handoff_mod.parse_handoff(text)
    narrative = _section_before_handoff(text)
    return handoff, _markdown_html(narrative), _extract_choices(handoff)


def _load_literature(run_dir: Path) -> tuple[dict, str, str | None]:
    text = _read_text(run_dir / _STAGE_FILES["literature"])
    if not text:
        return {}, "", None
    handoff = handoff_mod.parse_handoff(text)
    narrative = _section_before_handoff(text)
    return handoff, _markdown_html(narrative), _extract_citation_section(text)


def _load_structure(run_dir: Path) -> tuple[dict, str, list[dict], str | None]:
    text = _read_text(run_dir / _STAGE_FILES["structure"])
    if not text:
        return {}, "", [], None
    handoff = handoff_mod.parse_handoff(text)
    narrative = _text_before(text, r"###\s+MODEL.READY HOTSPOTS")
    hotspots: list[dict] = []
    raw = handoff_mod.parse_hotspot_residues(text, handoff)
    if raw:
        hotspots = json.loads(raw).get("residues", [])
    citation = _extract_citation_section(text)
    return handoff, _markdown_html(narrative), hotspots, citation


def _load_design(run_dir: Path) -> tuple[dict, str]:
    text = _read_text(run_dir / _STAGE_FILES["design"])
    if not text:
        return {}, ""
    handoff = handoff_mod.parse_handoff(text)
    narrative = _section_before_handoff(text)
    return handoff, _markdown_html(narrative)


def _native_structure(structures_dir: Path, struct_handoff: dict) -> dict | None:
    pdb_id = (struct_handoff.get("pdb_id") or "").strip()
    target_chain = (struct_handoff.get("target_chain") or struct_handoff.get("chain_a") or "A").strip()
    partner_chain = (struct_handoff.get("partner_chain") or struct_handoff.get("chain_b") or "").strip()
    if not pdb_id or pdb_id.upper() in ("NOT_FOUND", ""):
        return None
    path: Path | None = None
    for candidate in (f"{pdb_id.upper()}_ba1.cif", f"{pdb_id.upper()}.cif"):
        p = structures_dir / candidate
        if p.exists():
            path = p
            break
    if path is None:
        return None
    return {"path": path, "pdb_id": pdb_id, "target_chain": target_chain, "partner_chain": partner_chain}


# ---------------------------------------------------------------------
# scores / ranking — reuses src.design_ranking, never re-implements it
# ---------------------------------------------------------------------

def _read_csv_rows(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _parse_filter_stats(path: Path) -> FilterStats:
    """Deserialize the frozen `filter_stats.txt` `design_ranking.
    write_ranking_outputs` wrote at run time — not a re-derivation against
    today's config.yaml thresholds, which may have changed since this run
    executed. Same "read what the stage actually produced" discipline as
    src.binder_report reading refold_scores.csv instead of re-scoring.
    """
    text = path.read_text(encoding="utf-8")
    n_input_m = re.search(r"input designs:\s*(\d+)", text)
    n_survivors_m = re.search(r"survivors:\s*(\d+)", text)
    stats = FilterStats(
        n_input=int(n_input_m.group(1)) if n_input_m else 0,
        n_survivors=int(n_survivors_m.group(1)) if n_survivors_m else 0,
    )
    section = re.search(r"## Drop reasons\n(.*?)\n\n", text, re.DOTALL)
    if section:
        for line in section.group(1).splitlines():
            line = line.strip()
            if not line or line == "(none)":
                continue
            if ":" not in line:
                continue
            reason, _, count = line.rpartition(":")
            try:
                stats.dropped[reason.strip()] = int(count.strip())
            except ValueError:
                continue
    return stats


def _resolve_designs(run_dir: Path) -> tuple[list[dict], list[dict], FilterStats]:
    """(all_records, top_k_records, filter_stats) — all three read directly
    off what `_stage_analysis` actually wrote for this run."""
    enriched = run_dir / "05_metrics_enriched.csv"
    top_k_csv = run_dir / "05_ranking" / "top_k.csv"
    stats_txt = run_dir / "05_ranking" / "filter_stats.txt"
    if not enriched.exists() or not top_k_csv.exists() or not stats_txt.exists():
        raise ReportError(
            f"No analysis output found under {run_dir} — run at least through "
            "the analysis stage (--start-from analysis) before generating a report.")
    records = _read_csv_rows(enriched)
    top_k = _read_csv_rows(top_k_csv)
    stats = _parse_filter_stats(stats_txt)
    return records, top_k, stats


def _design_summary(row: dict) -> dict:
    return {
        "name": row.get("design_id") or row.get("id"),
        "composite_score": _as_float(row, "composite_score"),
        "composite_rank": row.get("composite_rank"),
        "iptm": _as_float(row, "design_to_target_iptm"),
        "ipae": _as_float(row, "min_design_to_target_pae"),
        "plddt": _as_float(row, "complex_plddt"),
        "hotspot_sasa_delta": _as_float(row, "lpt_hotspot_sasa_delta"),
        "seq": row.get("designed_chain_sequence") or row.get("designed_sequence"),
        "cif_path": row.get("cif_path"),
    }


# ---------------------------------------------------------------------
# assembling REPORT_DATA
# ---------------------------------------------------------------------

def _hero_and_rail(pathway_handoff: dict, lit_handoff: dict, struct_handoff: dict,
                    verdict: str | None, n_total: int, n_survivors: int, n_top: int) -> tuple[dict, dict]:
    target = struct_handoff.get("target_complex") or pathway_handoff.get("target_complex") or "target"
    pdb_id = struct_handoff.get("pdb_id") or pathway_handoff.get("pdb_id") or "—"
    intent = struct_handoff.get("design_intent") or lit_handoff.get("design_intent") or "disrupt"
    modality = struct_handoff.get("modality") or lit_handoff.get("modality") or "binder"
    go = lit_handoff.get("go_recommendation") or pathway_handoff.get("go_recommendation")

    pills = []
    if go:
        kind = "good" if go == "GO" else "bad" if go == "NO_GO" else "warn"
        pills.append({"label": f"{go} (literature)", "kind": kind})
    if verdict:
        kind = "good" if verdict == "GO" else "bad" if verdict == "NO_GO" else "warn"
        pills.append({"label": f"{verdict} (final)", "kind": kind})
    pills.append({"label": f"design intent: {intent}", "kind": ""})
    pills.append({"label": f"modality: {modality}", "kind": ""})

    stats = [
        {"k": "Designs scored", "v": f"{n_total:,}"},
        {"k": "Survive hard gates", "v": f"{n_survivors:,}",
         "sub": f"{100.0 * n_survivors / max(n_total, 1):.1f}%"},
        {"k": "Top-K", "v": str(n_top), "sub": "MMR-diversified"},
    ]

    hero = {
        "eyebrow": "PPI design run",
        "title": f"Does a de novo {modality} occupy the {target} interface on {pdb_id}?",
        "subtitle": (f"Structured report over the {target} design run against PDB {pdb_id} — "
                     f"pathway rationale, prior art, hotspot evidence, and every top-ranked "
                     f"design's actual refolded structure."),
        "pills": pills,
        "stats": stats,
    }
    rail = {
        "brand": "LPT · PPI Track",
        "target": target,
        "pdb_line": f"{pdb_id} · {struct_handoff.get('target_chain', struct_handoff.get('chain_a', '?'))}"
                    f"–{struct_handoff.get('partner_chain', struct_handoff.get('chain_b', '?'))}",
        "nav": [
            {"href": "#pathway", "label": "01 · Pathway & target"},
            {"href": "#literature", "label": "02 · Prior art"},
            {"href": "#structure", "label": "03 · Structure & hotspots"},
            {"href": "#generation", "label": "04 · Design generation"},
            {"href": "#confidence", "label": "05 · Ranking"},
            {"href": "#designs", "label": "06 · Top designs"},
            {"href": "#verdict", "label": "07 · Analyst verdict"},
        ],
        "stats": (
            ([{"k": "Verdict", "v": verdict, "color": "var(--good)" if verdict == "GO" else None}]
             if verdict else [])
            + [{"k": "Designs scored", "v": f"{n_total:,}"}]
        ),
    }
    return hero, rail


def _footer_html(run_dir: Path, generated_at: str) -> str:
    return (
        f"<p><b>Provenance.</b> Generated from <code>{run_dir}</code>. Target "
        f"selection ran through <code>pathway-expert</code> (or "
        f"<code>wildcard-expert</code>); tractability and prior art through "
        f"<code>molecular-biology-expert</code>; hotspot selection through "
        f"<code>complex-structure-analysis</code>; the design spec through "
        f"<code>protein-design-script</code>; execution (BoltzGen), analysis "
        f"(filtering, composite ranking, MMR) shown here are deterministic "
        f"Python; the final verdict through <code>design-analyst</code>. No "
        f"LLM in the loop for this report itself.</p>"
        f"<p style=\"margin-top:10px;\">Structures rendered client-side with "
        f"<a href=\"https://molstar.org\" target=\"_blank\" rel=\"noopener\">Mol*</a> "
        f"(vendored, MIT-licensed). Generated {generated_at}.</p>"
    )


def _structure_payload(path: Path, label: str, sub: str, target_chain: str,
                        binder_chain: str | None, kind: str) -> dict:
    return {
        "b64": base64.b64encode(path.read_bytes()).decode("ascii"),
        "label": label, "sub": sub,
        "target_chain": target_chain, "binder_chain": binder_chain, "kind": kind,
    }


# ---------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------

def build_report(run_dir: Path, out_path: Path | None = None,
                  cfg: dict | None = None, top_n_structures: int = 5) -> Path:
    """Build the self-contained HTML run report for one PPI-track run.

    `run_dir` is the run's output directory (either the legacy
    `outputs/<slug>_<date>/` layout or a project's `runs/<round>/` dir) — the
    same directory `00_pathway.md` .. `06_summary.md` were written into.

    Writes to `out_path` (default: `run_dir/report.html`) and returns that
    path. Raises :class:`ReportError` if the analysis stage hasn't produced
    ranked designs yet.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise ReportError(f"{run_dir} is not a directory")
    cfg = cfg or {}
    design_cfg = cfg.get("design") or {}
    # Only used to draw the gate marker line on the histograms below — the
    # funnel counts themselves come from the run's own frozen
    # filter_stats.txt (_resolve_designs), not these. Unlike the binder
    # track's calibration.json, PPI runs don't persist the thresholds a
    # given run actually scored against, so this can drift from the real
    # gate if config.yaml's thresholds changed after the run executed.
    thresholds = design_cfg.get("thresholds") or {}
    structures_dir = _ROOT / (cfg.get("paths", {}) or {}).get("structures_dir", "data/structures")
    binder_chain = (design_cfg.get("workstation") or {}).get("binder_chain", "B")

    pathway_handoff, pathway_narrative_html, choices = _load_pathway(run_dir)
    lit_handoff, lit_narrative_html, lit_citation_html = _load_literature(run_dir)
    struct_handoff, struct_narrative_html, hotspots, struct_citation_html = _load_structure(run_dir)
    design_handoff, design_narrative_html = _load_design(run_dir)
    execution_html = _markdown_html(_read_text(run_dir / _STAGE_FILES["execution"]) or "")
    summary_text = _read_text(run_dir / _STAGE_FILES["summary"]) or ""
    summary_html = _markdown_html(summary_text)
    verdict, verdict_reason = _extract_verdict(summary_text) if summary_text else (None, "")

    records, top_k, filter_stats = _resolve_designs(run_dir)
    top_designs = [_design_summary(r) for r in top_k[:top_n_structures]]

    iptm_vals = [v for v in (_as_float(r, "design_to_target_iptm") for r in records) if v is not None]
    sasa_vals = [v for v in (_as_float(r, "lpt_hotspot_sasa_delta") for r in records) if v is not None]
    sasa_hi = max(sasa_vals + [30.0]) * 1.05 if sasa_vals else 60.0

    n_input = filter_stats.n_input or len(records)
    dropped_rows = sorted(filter_stats.dropped.items(), key=lambda kv: -kv[1])
    funnel_rows = [{"criterion": k, "n": n, "pct": round(100.0 * n / max(n_input, 1), 1)}
                   for k, n in dropped_rows]

    hero, rail = _hero_and_rail(pathway_handoff, lit_handoff, struct_handoff, verdict,
                                 n_input, filter_stats.n_survivors, len(top_designs))

    site_decision = {
        "pdb_id": struct_handoff.get("pdb_id", "—"),
        "target_chain": struct_handoff.get("target_chain") or struct_handoff.get("chain_a", "—"),
        "partner_chain": struct_handoff.get("partner_chain") or struct_handoff.get("chain_b", "—"),
        "target_complex": struct_handoff.get("target_complex", "—"),
        "tractability": struct_handoff.get("tractability") or lit_handoff.get("tractability", ""),
        "go_recommendation": lit_handoff.get("go_recommendation", ""),
        "go_rationale": lit_handoff.get("go_rationale", ""),
        "design_intent": struct_handoff.get("design_intent", ""),
        "modality": struct_handoff.get("modality", ""),
    }

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    report_data: dict[str, Any] = {
        "hero": hero,
        "rail": rail,
        "pathway_narrative_html": pathway_narrative_html,
        "choices": choices,
        "site_decision": site_decision,
        "literature_narrative_html": lit_narrative_html,
        "literature_citation_html": lit_citation_html,
        "structure_narrative_html": struct_narrative_html,
        "structure_citation_html": struct_citation_html,
        "hotspots": hotspots,
        # `hotspots[*].auth_seq_id` is native-structure numbering — correct
        # for highlighting the native structure, but wrong for a BoltzGen
        # design refold: BoltzGen renumbers the target chain in its output,
        # specifically the original mmCIF `label_seq` becomes the new
        # `auth_seq_id` (see _stage_analysis's docstring in
        # pipeline_runner.py, and its own hotspot remap before SASA
        # enrichment — this is the same remap, done once here for the
        # viewer instead of per-worker-call). `label_seq_id` already falls
        # back to `auth_seq_id` in handoff.parse_hotspot_residues when the
        # source table's value was non-numeric, so no extra None-handling
        # is needed here.
        "design_hotspot_auth_seq_ids": (
            [h["label_seq_id"] for h in hotspots] if hotspots else None),
        "design_narrative_html": design_narrative_html,
        "execution_html": execution_html,
        "metrics": {
            "n_total": n_input,
            "n_survivors": filter_stats.n_survivors,
            "iptm_hist": _histogram(iptm_vals, 0.0, 1.0, 25),
            "sasa_hist": _histogram(sasa_vals, 0.0, sasa_hi, 20),
            "iptm_min": float(thresholds.get("iptm_min", 0.6)),
            "hotspot_sasa_delta_min": float(thresholds.get("hotspot_sasa_delta_min", 30.0)),
        },
        "funnel": funnel_rows,
        "top_designs": top_designs,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "summary_html": summary_html,
        "footer_html": _footer_html(run_dir, generated_at),
    }

    structures: dict[str, dict] = {}
    native = _native_structure(structures_dir, struct_handoff)
    if native:
        structures["native"] = _structure_payload(
            native["path"], f"Native · {native['pdb_id']}",
            "crystal/predicted structure", native["target_chain"], native["partner_chain"], "native")
    for i, d in enumerate(top_designs):
        cif = d.get("cif_path")
        if not cif:
            continue
        p = Path(cif)
        if not p.is_absolute():
            p = _ROOT / p
        if not p.exists():
            continue
        structures[f"design_{i}"] = _structure_payload(
            p, d.get("name") or f"design {i}", "boltzgen refold",
            struct_handoff.get("target_chain") or struct_handoff.get("chain_a", "A"),
            binder_chain, "design")

    out_path = out_path or (run_dir / "report.html")
    title = f"{struct_handoff.get('target_complex') or pathway_handoff.get('target_complex') or 'PPI'} run report"
    html = _render(report_data, structures, title=title)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _render(report_data: dict, structures: dict, title: str) -> str:
    shell = (_TEMPLATE_DIR / "shell.html").read_text(encoding="utf-8")
    base_css = (_SHARED_DIR / "base.css").read_text(encoding="utf-8")
    base_js = (_SHARED_DIR / "base.js").read_text(encoding="utf-8")
    app_js = (_TEMPLATE_DIR / "app.js").read_text(encoding="utf-8")
    molstar_js = (_MOLSTAR_DIR / "molstar.js").read_text(encoding="utf-8")
    molstar_css = (_MOLSTAR_DIR / "molstar.css").read_text(encoding="utf-8")

    def safe_json(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False).replace("</script", "<\\/script").replace("<!--", "<\\!--")

    html = shell
    # LLM-derived target name going straight into <title>.
    html = html.replace("@@TITLE@@", escape_html(title))
    html = html.replace("/*@@BASE_CSS@@*/", base_css)
    html = html.replace("/*@@MOLSTAR_CSS@@*/", molstar_css)
    html = html.replace("/*@@MOLSTAR_JS@@*/", molstar_js)
    html = html.replace("/*@@REPORT_DATA@@*/", safe_json(report_data))
    html = html.replace("/*@@STRUCTURES_DATA@@*/", safe_json(structures))
    html = html.replace("/*@@APP_JS@@*/", base_js + "\n" + app_js)
    return html
