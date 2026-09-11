"""
Deterministic, illustrated HTML campaign report for a binder-track run.

No LLM in the loop: everything here is a reader over files the pipeline
already writes (``trim_map.json``, ``candidates.json``, ``calibration.json``,
``refold_scores.csv``, the target-intel/interface stage handoffs) plus the
top-ranked designs' RF3 refold structures, rendered into one self-contained
HTML page with an embedded Mol* structure explorer. Where the report needs
"why" prose (why this site, why these hotspots), it renders the LLM stage's
*own* markdown report verbatim through the ``markdown`` package rather than
re-deriving new text — a deterministic stage has no business inventing
narrative, and the skills already write that narrative for a human reader.

Public entry point: :func:`build_report`.

Reuses (never re-implements) the pipeline's own logic:
    - :mod:`src.handoff` for the PIPELINE HANDOFF / MODEL-READY HOTSPOTS
      parsers pipeline_runner.py itself uses.
    - :mod:`src.binder_ranking` for the gate + ranking a completed campaign
      is scored with (``filter_records`` / ``rank_designs``), so the report's
      funnel chart and top-designs list can never drift from what the real
      scoring stage would produce.
"""

from __future__ import annotations

import base64
import gzip
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import handoff as handoff_mod
from src.binder_ranking import (
    DEFAULT_MAX_PER_BACKBONE,
    DEFAULT_TOP_K,
    FilterStats,
    filter_records,
    rank_designs,
    read_scores,
)
from src.report_common import (
    escape_html,
    ReportError,
    as_float as _as_float,
    display_root as _display_root,
    extract_citation_section as _extract_citation_section,
    histogram as _histogram,
    markdown_html as _markdown_html,
    read_json as _read_json,
    read_text as _read_text,
    safe_json,
    section_before_handoff as _section_before_handoff,
    stage_documents as _stage_documents,
)

_ROOT = Path(__file__).resolve().parent.parent
_MOLSTAR_DIR = _ROOT / "assets" / "vendor" / "molstar"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "report_templates" / "binder_report"
_SHARED_DIR = Path(__file__).resolve().parent / "report_templates" / "_shared"

# Every stage report the appendix renders whole, in run order. The 0* files
# only exist for a PPI-bridged campaign (`_bridge_ppi_to_foundry` wrote the
# binder track's own 20/21 from them) and are found by walking up from
# `binder_dir`; the 2* files are the binder track's own. Missing entries are
# skipped, so a campaign paused at calibration simply has no production or
# scoring report to append.
_APPENDIX_STAGES: list[tuple[str, str, str]] = [
    ("00", "Pathway and target selection", "00_pathway.md"),
    ("01", "Literature and tractability", "01_literature.md"),
    ("02", "Structure and hotspots", "02_structure.md"),
    ("20", "Target intel", "20_target_intel.md"),
    ("21", "Interface analysis", "21_interface.md"),
    ("22", "Target trim", "22_trim.md"),
    ("23", "Binder spec", "23_binder_spec.md"),
    ("24", "Pilot", "24_pilot.md"),
    ("25", "Calibration", "25_calibration.md"),
    ("26", "Production campaign", "26_production.md"),
    ("27", "Scoring and ranking", "27_scoring.md"),
    ("28", "Campaign summary", "28_summary.md"),
]


def _appendix(binder_dir: Path) -> list[dict]:
    """Every stage report for this campaign, rendered whole.

    `_find_up` for each file, not a fixed directory: a per-site trial's own
    22..28 live in its site dir while 20/21 (and any PPI-track 0*) are shared
    several levels up, and the search checks the start dir first, so a site
    always gets its own file where it has one.
    """
    entries = []
    for num, label, name in _APPENDIX_STAGES:
        path = _find_up(binder_dir, name)
        if path:
            entries.append((num, label, path))
    return _stage_documents(entries, rel_to=_display_root(binder_dir))



def _find_up(start: Path, name: str, max_levels: int = 4) -> Path | None:
    """Search `start`, then up to `max_levels` ancestors, for `name`.

    Handles both a top-level ``binder/`` run dir and a per-site
    ``binder/sites/<id>/binder/`` dir sharing one ``binder/20_target_intel.md``
    / ``binder/candidates/`` three levels up.
    """
    d = start
    for _ in range(max_levels + 1):
        p = d / name
        if p.exists():
            return p
        if d.parent == d:
            break
        d = d.parent
    return None


def _extract_hotspot_narrative(text: str) -> str:
    """HOTSPOT REGIONS / GLUE POCKETS through DESIGN RECOMMENDATIONS.

    Stops before MODEL-READY HOTSPOTS, which is the machine-readable
    table rendered separately as :attr:`hotspots`, not prose.
    """
    m = re.search(
        r"###\s+(?:HOTSPOT REGIONS|GLUE POCKETS).*?(?=\n###\s+MODEL.READY HOTSPOTS|\Z)",
        text, re.DOTALL | re.IGNORECASE)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------
# loading structured pipeline output
# ---------------------------------------------------------------------

def _load_target_intel(binder_dir: Path) -> tuple[dict, str]:
    path = _find_up(binder_dir, "20_target_intel.md")
    if not path:
        return {}, ""
    text = path.read_text(encoding="utf-8")
    return handoff_mod.parse_handoff(text), _section_before_handoff(text)


def _load_interface(binder_dir: Path) -> tuple[dict, str, str | None]:
    path = binder_dir / "21_interface.md"
    if not path.exists():
        return {}, "", None
    text = path.read_text(encoding="utf-8")
    handoff = handoff_mod.parse_handoff(text)
    narrative = _extract_hotspot_narrative(text)
    citation = _extract_citation_section(text)
    return handoff, _markdown_html(narrative), citation


def _load_candidates(binder_dir: Path) -> list[dict] | None:
    path = _find_up(binder_dir, "candidates/candidates.json")
    data = _read_json(path) if path else None
    if not data:
        return None
    return data.get("candidates")


def _load_hotspots(binder_dir: Path, interface_handoff: dict, interface_text_raw: str | None) -> list[dict]:
    trim_map = _read_json(binder_dir / "trim" / "trim_map.json")
    if trim_map and trim_map.get("hotspots"):
        return trim_map["hotspots"]
    if interface_text_raw:
        raw = handoff_mod.parse_hotspot_residues(interface_text_raw, interface_handoff)
        if raw:
            return json.loads(raw).get("residues", [])
    return []


def _native_structure(binder_dir: Path, target_intel: dict, interface_handoff: dict,
                       structures_dir: Path) -> dict | None:
    trim_map = _read_json(binder_dir / "trim" / "trim_map.json")
    pdb_id = (interface_handoff.get("pdb_id") or target_intel.get("pdb_id") or "").strip()
    target_chain = (interface_handoff.get("target_chain") or target_intel.get("target_chain") or "A").strip()
    partner_chain = (interface_handoff.get("partner_chain") or target_intel.get("partner_chain") or "").strip()
    path: Path | None = None
    if trim_map and trim_map.get("source"):
        p = Path(trim_map["source"])
        if p.exists():
            path = p
        pdb_id = trim_map.get("pdb_id", pdb_id)
        target_chain = trim_map.get("target_chain", target_chain)
        partner_chain = trim_map.get("partner_chain", partner_chain)
    if path is None and pdb_id and pdb_id != "NOT_FOUND":
        for candidate in (f"{pdb_id.upper()}_ba1.cif", f"{pdb_id.upper()}.cif"):
            p = structures_dir / candidate
            if p.exists():
                path = p
                break
    if path is None:
        return None
    return {
        "path": path, "pdb_id": pdb_id,
        "target_chain": target_chain, "partner_chain": partner_chain,
    }


def _design_hotspot_auth_seq_ids(binder_dir: Path, mode: str) -> list[int] | None:
    """
    Hotspot residue numbers in the REFOLDED designs' own numbering, for
    highlighting a design structure in the viewer.

    `REPORT.hotspots` (from `_load_hotspots`) is in the *native* structure's
    numbering — correct for highlighting the native structure, but RFD3
    renumbers the target chain in its own output (chain B; see CLAUDE.md's
    "RFD3 output chains are always binder = A, target = B" — the
    input->output renumbering lives ONLY in a design sidecar's
    `diffused_index_map`, never in the input spec). Using the native
    numbering to highlight a refolded design silently selects the wrong
    residues — same class of bug `src.binder_metrics.hotspots_from_rfd3`
    exists to prevent for scoring; the viewer needs the identical fix.

    The target-chain remap is the same for every design in one campaign
    (the target region is copied through unchanged, only renumbered — it
    isn't itself diffused), so any one sidecar from the mode that was
    actually scored is enough. Returns `None` if no sidecar can be found
    (e.g. a cluster-scored campaign, whose RFD3-equivalent output lives
    under a differently-named `diffuse/` directory this code doesn't
    search) — callers should fall back to the native numbering rather than
    highlighting nothing.
    """
    from src.binder_metrics import hotspots_from_rfd3
    from src.foundry_runner import FoundryPaths, find_design_sidecar

    paths = FoundryPaths.under(binder_dir / "campaign" / mode)
    if not paths.rfd3_dir.is_dir():
        return None
    sidecar = find_design_sidecar(paths)
    if sidecar is None:
        return None
    try:
        ids = hotspots_from_rfd3(sidecar, "B")
    except (KeyError, ValueError, json.JSONDecodeError):
        return None
    return ids or None


# ---------------------------------------------------------------------
# scores / ranking — reuses src.binder_ranking, never re-implements it
# ---------------------------------------------------------------------

def _resolve_designs(binder_dir: Path, rcfg: dict) -> tuple[list[dict], str, list[dict], FilterStats]:
    """(all_refold_rows, source_label, top_designs, filter_stats).

    Prefers the ``binder_scoring`` stage's output (production or a scored
    calibration run); falls back to scoring the raw calibration CSV in
    memory with the same config a real ``binder_scoring`` run would use —
    the identical fallback ``_stage_binder_scoring`` itself takes when a
    round never reached production.
    """
    thresholds = rcfg.get("thresholds")
    weights = rcfg.get("weights")
    mmr = rcfg.get("mmr")
    top_k = int(rcfg.get("top_k", DEFAULT_TOP_K))
    max_per_backbone = int(rcfg.get("max_per_backbone", DEFAULT_MAX_PER_BACKBONE))

    scoring_dir = binder_dir / "scoring"
    scores_csv = scoring_dir / "refold_scores.csv"
    top_k_csv = scoring_dir / "top_k.csv"
    if scores_csv.exists() and top_k_csv.exists():
        rows = read_scores(scores_csv)
        top_rows = read_scores(top_k_csv)
        _, stats = filter_records(rows, thresholds)
        return rows, "production campaign", top_rows, stats

    cal_csv = binder_dir / "calibration" / "refold_scores.csv"
    if cal_csv.exists():
        rows = read_scores(cal_csv)
        n_backbones = len({r.get("design_family") for r in rows if r.get("design_family")})
        ranking = rank_designs(
            rows, thresholds=thresholds, weights=weights, mmr=mmr,
            top_k=top_k, max_per_backbone=max_per_backbone)
        label = f"calibration trial ({n_backbones:,} backbones, {len(rows):,} refolds)"
        return rows, label, ranking.top_k, ranking.filter_stats

    raise ReportError(
        f"No refold scores found under {binder_dir} — run at least a "
        "calibration trial (--stop-after trial) before generating a report.")


def _design_summary(row: dict) -> dict:
    binder_len = _as_float(row, "binder_len")
    return {
        "name": row.get("name"),
        "family": row.get("design_family"),
        "iptm": _as_float(row, "iptm"),
        "ipsae_min": _as_float(row, "ipsae_min"),
        "rmsd_dock": _as_float(row, "binder_rmsd_dock"),
        "plddt": _as_float(row, "binder_plddt"),
        "hotspot_engagement": _as_float(row, "hotspot_engagement"),
        "epitope_recall": _as_float(row, "epitope_recall"),
        "binder_len": int(binder_len) if binder_len is not None else None,
        "seq": row.get("binder_seq"),
        "refold_cif": row.get("refold_cif"),
    }


def _scatter_sample(rows: list[dict], survivor_ids: set[int], success_metric: str,
                     excellence_bar: float, n: int = 700, seed: int = 7) -> list[list]:
    rng = random.Random(seed)
    idxs = list(range(len(rows)))
    rng.shuffle(idxs)
    out: list[list] = []
    for i in idxs[:n]:
        r = rows[i]
        x, y = _as_float(r, "iptm"), _as_float(r, "ipsae_min")
        if x is None or y is None:
            continue
        metric_val = _as_float(r, success_metric)
        excellent = (id(r) in survivor_ids) and metric_val is not None and metric_val >= excellence_bar
        out.append([round(x, 4), round(y, 4), 1 if excellent else 0])
    return out


# ---------------------------------------------------------------------
# assembling REPORT_DATA / STRUCTS
# ---------------------------------------------------------------------

def _site_decision(target_intel: dict) -> dict:
    try:
        alternatives = json.loads(target_intel.get("alternatives_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        alternatives = []
    return {
        "pdb_id": target_intel.get("pdb_id", "—"),
        "target_chain": target_intel.get("target_chain", "—"),
        "partner_chain": target_intel.get("partner_chain", "—"),
        "partner_name": target_intel.get("partner_name", "—"),
        "interface_rationale": target_intel.get("interface_rationale", ""),
        "go_recommendation": target_intel.get("go_recommendation", ""),
        "go_rationale": target_intel.get("go_rationale", ""),
        "design_intent": target_intel.get("design_intent", ""),
        "modality": target_intel.get("modality", ""),
        "alternatives": alternatives,
    }


def _hero_and_rail(target_intel: dict, calibration: dict | None, source_label: str,
                    n_total: int, n_top: int, best_ipsae: float | None) -> tuple[dict, dict]:
    gene = target_intel.get("target_gene", "target")
    pdb_id = target_intel.get("pdb_id", "—")
    partner_full = target_intel.get("partner_name", "the native partner")
    partner = partner_full.split(" (", 1)[0]  # drop a parenthetical qualifier for the headline only
    intent = target_intel.get("design_intent", "disrupt")
    modality = target_intel.get("modality", "binder")

    verdict = (calibration or {}).get("verdict")
    pills = []
    if verdict:
        kind = "good" if verdict == "SCALE_UP" else "warn" if verdict in ("ITERATE", "SCALE_UP_PARTIAL") else "bad"
        pills.append({"label": f"{verdict} verdict", "kind": kind})
    br = (calibration or {}).get("backbone_rate")
    if br:
        pills.append({"label": f"{br['k']} / {br['n']} backbones ≥ excellence bar", "kind": ""})
    if best_ipsae is not None:
        pills.append({"label": f"best ipSAE min {best_ipsae:.3f}", "kind": ""})
    pills.append({"label": f"design intent: {intent}", "kind": ""})

    stats = [
        {"k": "Refolds scored", "v": f"{n_total:,}", "sub": source_label},
        {"k": "Top designs", "v": str(n_top), "sub": "ranked, one per backbone"},
    ]
    if br:
        stats.insert(0, {"k": "Hit rate", "v": f"{br['p_hat']*100:.1f}", "unit": "%",
                          "sub": f"95% CI {br['p_low']*100:.1f}–{br['p_high']*100:.1f}%"})
    if best_ipsae is not None:
        stats.append({"k": "Best ipSAE min", "v": f"{best_ipsae:.3f}"})

    hero = {
        "eyebrow": f"Binder design {'trial' if 'calibration' in source_label else 'campaign'} — {source_label}",
        "title": f"Does a de novo {modality} occupy the {partner} interface on {gene}?",
        "subtitle": (f"Structured report over the {gene} binder-design run against PDB {pdb_id} — "
                     f"site and hotspot rationale, confidence metrics, and every top-ranked design's "
                     f"actual refolded structure."),
        "pills": pills,
        "stats": stats,
    }
    rail = {
        "brand": "LPT · Binder Track",
        "target": gene,
        "pdb_line": f"{pdb_id} · {target_intel.get('target_chain', '?')}–{target_intel.get('partner_chain', '?')}",
        "nav": [
            {"href": "#site", "label": "01 · Site selection"},
            {"href": "#hotspots", "label": "02 · Hotspots"},
            {"href": "#confidence", "label": "03 · Confidence"},
            {"href": "#designs", "label": "04 · Top designs"},
            {"href": "#appendix", "label": "05 · Full stage reports"},
            {"href": "#methods", "label": "Methods"},
        ],
        "stats": (
            ([{"k": "Verdict", "v": verdict, "color": "var(--good)" if verdict == "SCALE_UP" else None}] if verdict else [])
            + ([{"k": "Backbone hit rate", "v": f"{br['p_hat']*100:.1f}", "unit": "%"}] if br else [])
            + [{"k": "Refolds screened", "v": f"{n_total:,}"}]
        ),
    }
    return hero, rail


def _footer_html(binder_dir: Path, source_label: str, generated_at: str) -> str:
    # Model provenance first: which model wrote which stage, and whether any
    # model declined the work before one accepted it. Best-effort — a
    # provenance read must never be what breaks a report.
    provenance = ""
    try:
        from src import run_provenance

        record = run_provenance.collect(binder_dir)
        run_provenance.write(binder_dir)
        provenance = run_provenance.footer_html(record)
    except Exception:                                         # noqa: BLE001
        provenance = ""
    return provenance + (
        f"<p><b>Provenance.</b> Generated from <code>{binder_dir}</code> "
        f"({source_label}). Structure/site selection ran through "
        f"<code>binder-target-intel</code>; hotspot selection through "
        f"<code>complex-structure-analysis</code>; trimming, spec generation, "
        f"campaign execution (RFD3 → solubleMPNN → RF3), and all scoring shown "
        f"here are deterministic Python — no LLM in the loop past hotspot "
        f"selection, and none in the loop for this report itself.</p>"
        f"<p style=\"margin-top:10px;\">Structures rendered client-side with "
        f"<a href=\"https://molstar.org\" target=\"_blank\" rel=\"noopener\">Mol*</a> "
        f"(vendored, MIT-licensed). Generated {generated_at}.</p>"
    )


def _structure_payload(path: Path, label: str, sub: str, target_chain: str,
                        binder_chain: str | None, kind: str) -> dict:
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return {
        "b64": base64.b64encode(data).decode("ascii"),
        "label": label, "sub": sub,
        "target_chain": target_chain, "binder_chain": binder_chain, "kind": kind,
    }


# ---------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------

def build_report(binder_dir: Path, out_path: Path | None = None,
                  cfg: dict | None = None, top_n_structures: int = 5) -> Path:
    """Build the self-contained HTML campaign report for one binder run.

    `binder_dir` is a directory that itself contains ``trim/``,
    ``calibration/``, optionally ``scoring/``, ``21_interface.md`` and (found
    by walking up to ``max_levels`` ancestors) ``20_target_intel.md`` /
    ``candidates/candidates.json`` — i.e. either a top-level
    ``<run_dir>/binder/`` or a per-site ``<run_dir>/binder/sites/<id>/binder/``.

    Writes to ``out_path`` (default: ``binder_dir/report.html``) and returns
    that path.  Raises :class:`ReportError` if there is not yet enough data
    to report on (no calibration trial has finished).
    """
    binder_dir = Path(binder_dir)
    if not binder_dir.is_dir():
        raise ReportError(f"{binder_dir} is not a directory")
    cfg = cfg or {}
    rcfg = ((cfg.get("design") or {}).get("binder_ranking")) or {}
    success_metric = rcfg.get("success_metric", "iptm")
    excellence_bar = float(rcfg.get("excellence_bar", 0.7))
    structures_dir = _ROOT / (cfg.get("paths", {}) or {}).get("structures_dir", "data/structures")

    target_intel, site_narrative_raw = _load_target_intel(binder_dir)
    interface_handoff, hotspot_narrative_html, citation_html = _load_interface(binder_dir)
    candidates = _load_candidates(binder_dir)
    hotspots = _load_hotspots(binder_dir, interface_handoff, _read_text(binder_dir / "21_interface.md"))
    calibration = _read_json(binder_dir / "calibration" / "calibration.json")
    if calibration:
        # adaptive_bar (src/campaign_calibration.py) may have sized this
        # campaign at a bar stricter than the config default — an unusually
        # good target (e.g. KRAS/RAF1) gets scored "excellent" against the
        # bar it was ACTUALLY sized at, not the generic fallback, so the
        # scatter/histogram highlighting matches the verdict that was made.
        excellence_bar = float(
            calibration.get("bar_raised_to")
            or calibration.get("requested_bar")
            or excellence_bar)
        # Same reasoning for the METRIC as for the bar, and the run already
        # froze it: reading success_metric from today's config meant editing
        # config.yaml silently relabelled the scatter axis of an old report,
        # so the plot claimed a campaign was sized on a metric it never used.
        # This is exactly the run-time-vs-config-time drift that
        # `ppi_report._parse_filter_stats` exists to avoid.
        success_metric = calibration.get("success_metric") or success_metric

    rows, source_label, top_rows, filter_stats = _resolve_designs(binder_dir, rcfg)
    # "production campaign" is the only fixed source_label string; every
    # other label (the calibration-trial one is dynamic, includes counts)
    # means the calibration campaign was scored instead. Same mode names
    # _run_gpu_stage/_binder_paths use.
    resolved_mode = "production" if source_label == "production campaign" else "calibration"
    design_hotspot_ids = _design_hotspot_auth_seq_ids(binder_dir, resolved_mode)
    survivors, _ = filter_records(rows, rcfg.get("thresholds"))
    survivor_ids = {id(r) for r in survivors}
    top_designs = [_design_summary(r) for r in top_rows[:top_n_structures]]

    iptm_vals = [v for v in (_as_float(r, "iptm") for r in rows) if v is not None]
    ipsae_vals = [v for v in (_as_float(r, "ipsae_min") for r in rows) if v is not None]
    best_ipsae = max(ipsae_vals) if ipsae_vals else None

    passing_alone = [
        {"criterion": k, "n": n, "pct": round(100.0 * n / max(filter_stats.n_input, 1), 1)}
        for k, n in filter_stats.passing_alone.items()
    ]
    dropped_by = [{"criterion": k, "n": n} for k, n in filter_stats.dropped.items() if k != "scoring error"]

    hero, rail = _hero_and_rail(target_intel, calibration, source_label, len(rows), len(top_designs), best_ipsae)

    site_decision = _site_decision(target_intel)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    report_data: dict[str, Any] = {
        "hero": hero,
        "rail": rail,
        "site_narrative_html": _markdown_html(site_narrative_raw),
        "site_decision": site_decision,
        "candidates": candidates,
        "hotspot_narrative_html": hotspot_narrative_html,
        "hotspots": hotspots,
        # Native-structure numbering (see `hotspots` above) is wrong for a
        # refolded design — RFD3 renumbers the target chain in its own
        # output. `None` when no design sidecar could be found; the viewer
        # falls back to the native list (better than highlighting nothing).
        "design_hotspot_auth_seq_ids": design_hotspot_ids,
        "citation_html": citation_html,
        "calibration": {
            "verdict": calibration.get("verdict"),
            "verdict_reason": calibration.get("verdict_reason"),
            "success_metric": success_metric,
            "excellence_bar": excellence_bar,
            "backbone_rate": calibration.get("backbone_rate"),
        } if calibration else None,
        "metrics": {
            "n_total": len(rows),
            "iptm_hist": _histogram(iptm_vals, 0.0, 1.0, 25),
            "ipsae_hist": _histogram(ipsae_vals, 0.0, 1.0, 25),
            "scatter": _scatter_sample(rows, survivor_ids, success_metric, excellence_bar),
        },
        "funnel": {"passing_alone": passing_alone, "dropped_by": dropped_by},
        "top_designs": top_designs,
        "source_label": source_label,
        "appendix": _appendix(binder_dir),
        "footer_html": _footer_html(binder_dir, source_label, generated_at),
    }

    structures: dict[str, dict] = {}
    native = _native_structure(binder_dir, target_intel, interface_handoff, structures_dir)
    if native:
        structures["native"] = _structure_payload(
            native["path"], f"Native · {native['pdb_id']}",
            "crystal/predicted structure", native["target_chain"], native["partner_chain"], "native")
    for i, d in enumerate(top_designs):
        cif = d.get("refold_cif")
        if not cif:
            continue
        p = Path(cif)
        if not p.exists():
            continue
        structures[f"design_{i}"] = _structure_payload(
            p, d.get("family") or d.get("name") or f"design {i}",
            f"{d.get('binder_len', '?')} aa binder", "B", "A", "design")

    out_path = out_path or (binder_dir / "report.html")
    html = _render(report_data, structures, title=f"{target_intel.get('target_gene', 'Binder')} campaign report")
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _render(report_data: dict, structures: dict, title: str) -> str:
    shell = (_TEMPLATE_DIR / "shell.html").read_text(encoding="utf-8")
    base_css = (_SHARED_DIR / "base.css").read_text(encoding="utf-8")
    base_js = (_SHARED_DIR / "base.js").read_text(encoding="utf-8")
    app_js = (_TEMPLATE_DIR / "app.js").read_text(encoding="utf-8")
    molstar_js = (_MOLSTAR_DIR / "molstar.js").read_text(encoding="utf-8")
    molstar_css = (_MOLSTAR_DIR / "molstar.css").read_text(encoding="utf-8")

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
