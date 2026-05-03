#!/usr/bin/env python3
"""
Backfill ``protein_identifiers`` sidecar block in fingerprint JSONs.

For each fingerprint:
  1. Walk every protein-name slot (key_findings.protein_pair, target_nodes,
     upstream_regulators, downstream_effectors, entities.proteins).
  2. Resolve each name via :class:`src.identifier_normalizer.IdentifierNormalizer`
     to a canonical human UniProt accession + HGNC gene symbol, with a
     confidence tier.
  3. Filter out junk (placeholders, research tools, complexes, non-specific
     histones) into a separate audit list.
  4. Write the result as a top-level ``protein_identifiers`` key in the
     fingerprint JSON. Curator never writes this key — same pattern as
     the canonical-DOI/PMCID backfill in ``scripts/backfill_fingerprint_metadata.py``.

Idempotent: skips fingerprints whose ``protein_identifiers.schema_version``
is already at ``CURRENT_SCHEMA``, unless ``force=True`` is given.

This module is also imported by ``scripts/curate_papers.py`` so freshly-
curated fingerprints get a ``protein_identifiers`` block as part of the
same pipeline run — see ``run_backfill()``.

Usage
-----
    python scripts/normalize_identifiers.py --dry-run         # preview only
    python scripts/normalize_identifiers.py --sample 100      # 100 fingerprints
    python scripts/normalize_identifiers.py                   # apply to all
    python scripts/normalize_identifiers.py --force           # rewrite all
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml
from loguru import logger

from src.identifier_normalizer import (
    IdentifierNormalizer,
    Resolution,
    extract_protein_occurrences,
    is_filtered,
)


CURRENT_SCHEMA = "1.0"
HUMAN_TAXON = 9606

IDMAPPING = _ROOT / "data" / "depmap" / "HUMAN_9606_idmapping.dat.gz"
HGNC_TSV = _ROOT / "data" / "depmap" / "hgnc_complete_set.tsv"


# Resolution tiers considered "high confidence" for headline coverage stats.
_HIGH_CONF_TIERS = frozenset({
    "exact_gene", "hgnc_alias", "hgnc_prev",
    "curated_alias", "paralog_default",
    "kb_id", "stripped_mutant",
})


def _pick_native_taxon(taxa: list) -> int | None:
    """Pick a single native taxon for resolution from the curator's list.

    Curators sometimes write taxa=[9606, 10090] for comparative studies, or
    bogus values like 0 or 1000000000. Strategy: prefer 9606 if listed
    (resolves cleanly without ortholog tag), else first plausible taxon,
    else None.
    """
    if not isinstance(taxa, list) or not taxa:
        return None
    valid = [t for t in taxa if isinstance(t, int) and 1 <= t < 10_000_000]
    if not valid:
        return None
    if HUMAN_TAXON in valid:
        return HUMAN_TAXON
    return valid[0]


def _build_block(fp: dict, normalizer: IdentifierNormalizer) -> tuple[dict, dict[str, int], dict[str, int]]:
    """Resolve every protein-name occurrence in one fingerprint.

    Returns ``(protein_identifiers_block, tier_counts, filter_counts)``.
    """
    occurrences = extract_protein_occurrences(fp)
    taxa = (fp.get("methodology") or {}).get("protein_origin_organism") or []
    native_taxon = _pick_native_taxon(taxa)

    # Aggregate by raw_name across the fingerprint.
    by_raw: dict[str, dict] = {}
    for raw_name, source_path in occurrences:
        bucket = by_raw.setdefault(raw_name, {"sources": []})
        bucket["sources"].append(source_path)

    entries: list[dict] = []
    filtered: list[dict] = []
    unresolved: list[dict] = []

    tier_counts: Counter = Counter()
    filter_counts: Counter = Counter()

    for raw_name, agg in by_raw.items():
        n_occ = len(agg["sources"])

        # Filter check first — same logic as the resolver, but lets us
        # short-circuit and record the reason for audit.
        reason = is_filtered(raw_name)
        if reason:
            filtered.append({
                "raw_name": raw_name,
                "reason": reason,
                "sources": agg["sources"],
            })
            filter_counts[reason] += n_occ
            continue

        res: Resolution = normalizer.resolve(raw_name, native_taxon=native_taxon)

        if res.match_confidence is None:
            unresolved.append({
                "raw_name": raw_name,
                "normalized": res.normalized,
                "sources": agg["sources"],
            })
            tier_counts["unresolved"] += n_occ
            continue

        entries.append({
            "raw_name": raw_name,
            "normalized": res.normalized,
            "sources": agg["sources"],
            "human_uniprot": res.human_uniprot,
            "human_gene_symbol": res.human_gene_symbol,
            "candidate_uniprots": res.candidate_uniprots,
            "match_confidence": res.match_confidence,
            "is_family_head": res.is_family_head,
            "is_human_ortholog_mapping": res.is_human_ortholog_mapping,
            "native_taxon": res.native_taxon,
        })
        tier_counts[res.match_confidence] += n_occ

    block = {
        "schema_version": CURRENT_SCHEMA,
        "normalized_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "sources": {
            "uniprot_idmapping": IDMAPPING.name,
            "hgnc": HGNC_TSV.name,
        },
        "native_taxon_resolved": native_taxon,
        "entries": entries,
        "filtered_out": filtered,
        "unresolved": unresolved,
        "stats": {
            "total_occurrences": len(occurrences),
            "unique_raw_names": len(by_raw),
            "resolved": len(entries),
            "filtered": len(filtered),
            "unresolved": len(unresolved),
            "by_tier": dict(tier_counts),
            "by_filter_reason": dict(filter_counts),
        },
    }
    return block, dict(tier_counts), dict(filter_counts)


def run_backfill(
    fingerprint_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    sample: int | None = None,
    dump_unresolved: Path | None = None,
    log_full_summary: bool = True,
) -> dict[str, Any]:
    """Apply identifier-normalisation backfill to every fingerprint in a directory.

    Importable from ``scripts/curate_papers.py`` so newly-curated fingerprints
    self-normalize at the end of each curation batch. The standalone CLI in
    ``main()`` is a thin wrapper around this function.

    Args:
        fingerprint_dir: Where the fingerprint JSONs live.
        force:           Re-write fingerprints whose schema is already current.
        dry_run:         Resolve and report stats without writing.
        sample:          If set, process only the first N fingerprints (sorted).
        dump_unresolved: If set, write the unresolved-name TSV here for review.
        log_full_summary: If True, log the per-tier coverage tables. Set False
                          when called from curate_papers to keep its end-of-run
                          output concise — only the file_stats are logged.

    Returns:
        Dict with ``file_stats`` (Counter), ``tier_counts``, ``filter_counts``,
        ``total_occurrences``, ``total_unique_names``, and ``unresolved_counter``.
    """
    fingerprint_dir = Path(fingerprint_dir).resolve()
    if not fingerprint_dir.exists():
        raise FileNotFoundError(f"fingerprint dir not found: {fingerprint_dir}")
    if not IDMAPPING.exists():
        raise FileNotFoundError(f"idmapping not found: {IDMAPPING}")
    if not HGNC_TSV.exists():
        raise FileNotFoundError(f"HGNC TSV not found: {HGNC_TSV}")

    logger.info(f"Loading resolver indexes ({IDMAPPING.name} + {HGNC_TSV.name}) ...")
    normalizer = IdentifierNormalizer(IDMAPPING, HGNC_TSV)
    logger.info(
        f"  HGNC approved: {len(normalizer._hgnc_symbol_to_uniprot):,} | "
        f"HGNC alias keys: {len(normalizer._hgnc_alias_to_symbol):,} | "
        f"HGNC prev: {len(normalizer._hgnc_prev_to_symbol):,} | "
        f"UniProt gene rows: {len(normalizer._by_gene):,}"
    )

    files = sorted(fingerprint_dir.glob("*.json"))
    if sample:
        files = files[:sample]
    logger.info(f"Scanning {len(files):,} fingerprints in {fingerprint_dir}")

    file_stats: Counter = Counter()
    total_tier: Counter = Counter()
    total_filter: Counter = Counter()
    total_occurrences = 0
    total_unique_names = 0
    unresolved_counter: Counter = Counter()

    sample_logged = 0
    SAMPLE_CAP = 5

    for fp_file in files:
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"  unparseable: {fp_file.name} ({exc})")
            file_stats["unparseable"] += 1
            continue

        existing = fp.get("protein_identifiers")
        if existing and existing.get("schema_version") == CURRENT_SCHEMA and not force:
            file_stats["already_current"] += 1
            stats = existing.get("stats") or {}
            total_occurrences += stats.get("total_occurrences", 0)
            total_unique_names += stats.get("unique_raw_names", 0)
            for tier, n in (stats.get("by_tier") or {}).items():
                total_tier[tier] += n
            for reason, n in (stats.get("by_filter_reason") or {}).items():
                total_filter[reason] += n
            continue

        block, tier_counts, filter_counts = _build_block(fp, normalizer)
        for tier, n in tier_counts.items():
            total_tier[tier] += n
        for reason, n in filter_counts.items():
            total_filter[reason] += n
        total_occurrences += block["stats"]["total_occurrences"]
        total_unique_names += block["stats"]["unique_raw_names"]
        for u in block["unresolved"]:
            unresolved_counter[u["raw_name"]] += len(u["sources"])

        if dry_run:
            file_stats["would_update"] += 1
            if sample_logged < SAMPLE_CAP:
                stats = block["stats"]
                logger.info(
                    f"  [would update] {fp_file.name}: "
                    f"{stats['total_occurrences']} occ / "
                    f"{stats['unique_raw_names']} unique -> "
                    f"resolved={stats['resolved']} "
                    f"filtered={stats['filtered']} "
                    f"unresolved={stats['unresolved']}"
                )
                sample_logged += 1
        else:
            fp["protein_identifiers"] = block
            fp_file.write_text(
                json.dumps(fp, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            file_stats["updated"] += 1
            if sample_logged < SAMPLE_CAP:
                stats = block["stats"]
                logger.info(
                    f"  [updated] {fp_file.name}: "
                    f"{stats['total_occurrences']} occ / "
                    f"{stats['unique_raw_names']} unique -> "
                    f"resolved={stats['resolved']} "
                    f"filtered={stats['filtered']} "
                    f"unresolved={stats['unresolved']}"
                )
                sample_logged += 1

    # ---------- summary ----------
    logger.info("--- file summary ---")
    for k, v in file_stats.items():
        logger.info(f"  {k}: {v:,}")

    if log_full_summary and total_occurrences:
        def pct(n: int, d: int) -> str:
            return f"{(100.0 * n / d):.1f}%" if d else "n/a"

        logger.info(f"--- coverage by tier (occurrence-weighted, n={total_occurrences:,}) ---")
        for tier in [
            "exact_gene", "hgnc_alias", "hgnc_prev", "curated_alias",
            "paralog_default", "family_head", "kb_id", "stripped_mutant",
            "uniprot_gene", "uniprot_synonym", "fuzzy", "unresolved",
        ]:
            n = total_tier.get(tier, 0)
            if n:
                logger.info(f"  {tier:18s} {n:>8,}  ({pct(n, total_occurrences)})")

        logger.info("--- filter audit (occurrence-weighted) ---")
        for reason in ["placeholder", "non_specific_histone", "complex", "viral", "research_tool"]:
            n = total_filter.get(reason, 0)
            if n:
                logger.info(f"  {reason:22s} {n:>8,}  ({pct(n, total_occurrences)})")

        high_conf = sum(total_tier.get(t, 0) for t in _HIGH_CONF_TIERS)
        logger.info(
            f"--- HIGH-CONF (exact + alias + prev + curated + paralog_default + kb_id + stripped_mutant): "
            f"{pct(high_conf, total_occurrences)} of all occurrences ---"
        )

    if dump_unresolved and unresolved_counter:
        out_path = dump_unresolved
        if not out_path.is_absolute():
            out_path = _ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write("# raw_name\toccurrences\n")
            for name, n in unresolved_counter.most_common():
                fh.write(f"{name}\t{n}\n")
        logger.info(f"  wrote {len(unresolved_counter):,} unresolved names -> {out_path}")

    if dry_run and file_stats["would_update"]:
        logger.info(
            f"Run without --dry-run to apply {file_stats['would_update']:,} updates."
        )

    return {
        "file_stats": file_stats,
        "tier_counts": total_tier,
        "filter_counts": total_filter,
        "total_occurrences": total_occurrences,
        "total_unique_names": total_unique_names,
        "unresolved_counter": unresolved_counter,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and report stats without writing.")
    parser.add_argument("--sample", type=int, default=None,
                        help="Only process the first N fingerprints (sorted).")
    parser.add_argument("--force", action="store_true",
                        help="Rewrite even fingerprints whose schema is current.")
    parser.add_argument("--dump-unresolved", type=Path, default=None,
                        help="Dump unresolved raw_names + counts to a TSV for review.")
    args = parser.parse_args()

    cfg = yaml.safe_load((_ROOT / args.config).read_text(encoding="utf-8"))
    fingerprint_dir = (_ROOT / cfg["paths"]["fingerprint_dir"]).resolve()

    run_backfill(
        fingerprint_dir,
        force=args.force,
        dry_run=args.dry_run,
        sample=args.sample,
        dump_unresolved=args.dump_unresolved,
        log_full_summary=True,
    )


if __name__ == "__main__":
    main()
