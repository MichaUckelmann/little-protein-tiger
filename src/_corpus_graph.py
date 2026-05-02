"""
Corpus-wide graph and quantitative-evidence helpers over fingerprint JSONs.

Used by both transports — `src/mcp_server.py` exposes them as MCP tools,
`src/skill_runner.py` dispatches them in-process. Single source of truth
to avoid drift.

Both helpers enumerate fingerprints from disk via ``Path.glob("*.json")``;
no vector store, no embeddings. The full corpus (~1k fingerprints) is read
each call — fast enough not to need caching at current scale.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Protein-name normalisation and matching
# ---------------------------------------------------------------------------
#
# Real fingerprint data has inconsistent protein naming:
#   YAP / YAP1 / hYAP / h-YAP / HYAP1
#   SREBP-1 / SREBP1 / SREBP-1c
#   TEAD1 / TEAD2 / TEAD3 / TEAD4  (paralogs — should NOT be collapsed)
#
# Strategy: light normalisation (uppercase, strip species prefix, drop hyphens
# between letters and digits) followed by substring-or-prefix match. This is
# the same pattern used by _find_pdb_structures in skill_runner.py.

_SPECIES_PREFIX = re.compile(r"^(?:H|M|R|HSA|MMU|RNO)-(?=[A-Z0-9])", re.IGNORECASE)


def _normalize_protein(name: str) -> str:
    if not name:
        return ""
    s = name.strip().upper()
    s = _SPECIES_PREFIX.sub("", s)
    # Collapse "SREBP-1" → "SREBP1", but leave "ABC-DEF" alone (letters on both sides).
    s = re.sub(r"([A-Z])-(\d)", r"\1\2", s)
    s = re.sub(r"(\d)-([A-Z])", r"\1\2", s)
    return s.replace(" ", "")


def _protein_matches(stored: str, query: str) -> bool:
    """Match a stored fingerprint protein name against a user query.

    Returns True when the normalised query is a substring of the normalised
    stored name, or vice-versa for short queries (e.g. "YAP" should hit
    "YAP1"). Queries shorter than 2 chars are rejected.
    """
    s = _normalize_protein(stored)
    q = _normalize_protein(query)
    if len(q) < 2 or not s:
        return False
    return q in s or s in q


# ---------------------------------------------------------------------------
# Tool 1: corpus-wide partner lookup
# ---------------------------------------------------------------------------


def get_interactions_for(
    protein: str,
    fingerprint_dir: Path,
    depth: int = 1,
    min_mentions: int = 1,
    max_partners: int = 50,
) -> dict[str, Any]:
    """Aggregate interaction partners for ``protein`` across the corpus.

    Walks ``key_findings[].protein_pair`` in every fingerprint. For each
    matching finding, the *other* member of the pair becomes a partner and
    is aggregated with mention count, supporting DOIs, and any non-null
    Kd/Ki values from the same finding.
    """
    fingerprint_dir = Path(fingerprint_dir)
    if not fingerprint_dir.exists():
        return {
            "query_protein": protein,
            "depth": depth,
            "corpus_size": 0,
            "direct_partners": [],
            "second_degree_partners": [],
            "caveats": [f"Fingerprint directory not found: {fingerprint_dir}"],
        }

    files = list(fingerprint_dir.glob("*.json"))

    direct = _collect_partners(files, [protein])
    direct_partners = _shape_partners(direct, min_mentions, max_partners)

    second_degree: list[dict] = []
    if depth >= 2 and direct_partners:
        # Use the canonical partner names that came out of direct match
        # (the names as they appear in fingerprints, not the user query).
        seed_names = [p["partner"] for p in direct_partners]
        # Exclude the original query protein from second-degree results.
        excluded = {_normalize_protein(protein)} | {_normalize_protein(n) for n in seed_names}
        second_raw = _collect_partners(files, seed_names)
        # Filter: drop partners that are the original query or any first-degree partner.
        second_raw = {
            k: v for k, v in second_raw.items() if _normalize_protein(k) not in excluded
        }
        second_degree = _shape_partners(second_raw, min_mentions, max_partners)

    return {
        "query_protein": protein,
        "depth": depth,
        "corpus_size": len(files),
        "direct_partners": direct_partners,
        "second_degree_partners": second_degree if depth >= 2 else [],
        "caveats": [
            "Matching uses light alias normalisation (case-insensitive, species "
            "prefix stripped, internal hyphens dropped) plus substring/prefix "
            "containment — paralogs like TEAD1/TEAD2/TEAD3/TEAD4 are kept distinct, "
            "but a query of 'TEAD' will match all of them.",
            "Corpus is biochemistry-biased; in vivo and clinical interaction "
            "partners may be undercounted. Absence of a partner here is not "
            "evidence that the interaction does not exist in the wider literature.",
        ],
    }


def _collect_partners(
    files: list[Path], queries: list[str]
) -> dict[str, dict[str, Any]]:
    """Walk every fingerprint, accumulate partners keyed by partner name.

    For each ``key_findings`` entry whose ``protein_pair`` contains any of
    ``queries``, the other element becomes a partner. Multiple-query mode is
    used for depth-2 expansion.
    """
    partners: dict[str, dict[str, Any]] = {}
    # Skip self-matches: e.g. protein_pair=["KRAS-G12C","KRAS"] would otherwise
    # report "KRAS" as a partner of "KRAS".
    query_keys = {_normalize_protein(q) for q in queries}

    for fp_file in files:
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        doi = (fp.get("paper_metadata") or {}).get("doi")
        if not doi:
            continue

        for finding in fp.get("key_findings") or []:
            pair = finding.get("protein_pair") or []
            pair = [p for p in pair if p]
            if len(pair) != 2:
                continue

            # Determine which side (if any) matches our queries; the other side is the partner.
            partner_name: str | None = None
            for q in queries:
                if _protein_matches(pair[0], q):
                    partner_name = pair[1]
                    break
                if _protein_matches(pair[1], q):
                    partner_name = pair[0]
                    break
            if partner_name is None:
                continue

            partner_key = _normalize_protein(partner_name)
            if partner_key in query_keys:
                continue  # self-match (e.g. mutant variant paired with WT)

            # Group by normalised partner key, but display the longest seen variant
            # so "YAP1" wins over "YAP" when both appear in the corpus.
            entry = partners.setdefault(
                partner_key,
                {
                    "partner": partner_name,
                    "mention_count": 0,
                    "dois": set(),
                    "quantitative_anchors": [],
                },
            )
            if len(partner_name) > len(entry["partner"]):
                entry["partner"] = partner_name
            entry["mention_count"] += 1
            entry["dois"].add(doi)

            kd = finding.get("affinities_kd_Molar")
            ki = finding.get("inhibitory_constant_Ki")
            if kd is not None or ki is not None:
                entry["quantitative_anchors"].append(
                    {
                        "doi": doi,
                        "kd_M": kd,
                        "ki_M": ki,
                        "source_span": finding.get("source_span"),
                        "experimental_context": finding.get("experimental_context"),
                    }
                )

    return partners


def _shape_partners(
    raw: dict[str, dict[str, Any]],
    min_mentions: int,
    max_partners: int,
) -> list[dict[str, Any]]:
    shaped = [
        {
            "partner": v["partner"],
            "mention_count": v["mention_count"],
            "supporting_dois": sorted(v["dois"]),
            "quantitative_anchors": v["quantitative_anchors"],
        }
        for v in raw.values()
        if v["mention_count"] >= min_mentions
    ]
    shaped.sort(key=lambda x: (-x["mention_count"], x["partner"]))
    return shaped[:max_partners]


# ---------------------------------------------------------------------------
# Tool 2: quantitative evidence for a specific pair
# ---------------------------------------------------------------------------


def find_quantitative_evidence(
    protein_pair: list[str],
    fingerprint_dir: Path,
    metric: str = "Kd",
) -> dict[str, Any]:
    """Return all key_findings with non-null Kd/Ki for the requested pair.

    Pair matching is order-insensitive: ``["KRAS","RAF1"]`` matches a stored
    ``["RAF1","KRAS"]``. Results are sorted by metric value ascending so the
    tightest binder is first.
    """
    fingerprint_dir = Path(fingerprint_dir)
    metric = (metric or "Kd").strip()
    if metric not in {"Kd", "Ki", "both"}:
        return {
            "query_pair": protein_pair,
            "metric": metric,
            "results": [],
            "result_count": 0,
            "caveats": [f"Unknown metric '{metric}'. Use one of: Kd, Ki, both."],
        }

    if not fingerprint_dir.exists():
        return {
            "query_pair": protein_pair,
            "metric": metric,
            "results": [],
            "result_count": 0,
            "caveats": [f"Fingerprint directory not found: {fingerprint_dir}"],
        }

    if len(protein_pair) != 2:
        return {
            "query_pair": protein_pair,
            "metric": metric,
            "results": [],
            "result_count": 0,
            "caveats": ["protein_pair must contain exactly two protein names."],
        }

    a_query, b_query = protein_pair[0], protein_pair[1]
    results: list[dict[str, Any]] = []

    for fp_file in fingerprint_dir.glob("*.json"):
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        doi = (fp.get("paper_metadata") or {}).get("doi")
        if not doi:
            continue

        for finding in fp.get("key_findings") or []:
            pair = finding.get("protein_pair") or []
            pair = [p for p in pair if p]
            if len(pair) != 2:
                continue

            # Order-insensitive pair match.
            if not (
                (_protein_matches(pair[0], a_query) and _protein_matches(pair[1], b_query))
                or (_protein_matches(pair[0], b_query) and _protein_matches(pair[1], a_query))
            ):
                continue

            kd = finding.get("affinities_kd_Molar")
            ki = finding.get("inhibitory_constant_Ki")

            if metric == "Kd" and kd is None:
                continue
            if metric == "Ki" and ki is None:
                continue
            if metric == "both" and kd is None and ki is None:
                continue

            results.append(
                {
                    "doi": doi,
                    "source_span": finding.get("source_span"),
                    "claim": finding.get("claim"),
                    "kd_M": kd,
                    "ki_M": ki,
                    "experimental_context": finding.get("experimental_context"),
                    "key_amino_acid_residues": finding.get("key_amino_acid_residues") or [],
                    "confidence_score": finding.get("confidence_score"),
                    "stored_pair": pair,
                }
            )

    def _sort_key(r: dict[str, Any]) -> float:
        if metric == "Ki":
            return r["ki_M"] if r["ki_M"] is not None else float("inf")
        if metric == "Kd":
            return r["kd_M"] if r["kd_M"] is not None else float("inf")
        # "both": sort by whichever value is present, tightest first
        candidates = [v for v in (r["kd_M"], r["ki_M"]) if v is not None]
        return min(candidates) if candidates else float("inf")

    results.sort(key=_sort_key)

    return {
        "query_pair": protein_pair,
        "metric": metric,
        "results": results,
        "result_count": len(results),
        "caveats": [
            "Corpus quantitative coverage is sparse — most findings have null "
            "affinity. An empty result is therefore weak evidence; it does not "
            "imply that no published value exists.",
            "ΔΔG values are not currently captured by the curation schema; "
            "only Kd and Ki are queryable here.",
            "Pair matching is order-insensitive and uses light alias "
            "normalisation; verify the returned 'stored_pair' field corresponds "
            "to the proteins you actually meant.",
        ],
    }
