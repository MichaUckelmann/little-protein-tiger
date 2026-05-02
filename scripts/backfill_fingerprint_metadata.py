#!/usr/bin/env python3
"""
One-off: backfill missing paper_metadata fields in fingerprint JSONs.

Earlier curator runs let the LLM extract paper_metadata.doi / pmcid / title
from paper text, which fails for ~37% of papers (DOI not in parsable body,
older formatting, etc.). The Paper record in `data/literature.db` always
has the canonical values — this script restores them post-hoc.

Idempotent: only fills in fields that are currently null. Existing non-null
values are never overwritten.

Usage
-----
    python scripts/backfill_fingerprint_metadata.py --dry-run    # preview
    python scripts/backfill_fingerprint_metadata.py              # apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml
from loguru import logger

from src.database import Database, _paper_key
from src.models import Paper


def _safe_filename(paper_key: str) -> str:
    """Match the convention in src.fingerprint_store.save_fingerprint."""
    return paper_key.replace("/", "_").replace(":", "_").replace(" ", "_")


def _build_index(papers: list[Paper]) -> dict[str, Paper]:
    """Return {sanitised_filename_stem: Paper} for fast filename → paper lookup."""
    idx: dict[str, Paper] = {}
    for p in papers:
        pk = _paper_key(p.doi, p.pmcid, p.pmid, p.title)
        idx[_safe_filename(pk)] = p
    return idx


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill DOI / PMCID / title in fingerprint JSONs from the canonical DB record."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = yaml.safe_load((_ROOT / args.config).read_text(encoding="utf-8"))
    fingerprint_dir = (_ROOT / cfg["paths"]["fingerprint_dir"]).resolve()
    db_path = _ROOT / cfg["paths"]["db_path"]

    if not fingerprint_dir.exists():
        sys.exit(f"ERROR: fingerprint dir not found: {fingerprint_dir}")
    if not db_path.exists():
        sys.exit(f"ERROR: literature DB not found: {db_path}")

    db = Database(db_path)
    by_filename = _build_index(db.get_papers())
    logger.info(f"Loaded {len(by_filename):,} paper records from {db_path}")

    files = sorted(fingerprint_dir.glob("*.json"))
    logger.info(f"Scanning {len(files):,} fingerprints in {fingerprint_dir}")

    stats = {
        "checked": 0,
        "would_update": 0,
        "updated": 0,
        "no_db_match": 0,
        "no_changes_needed": 0,
        "skipped_no_canonical": 0,
        "unparseable": 0,
    }
    sample_logged = 0
    SAMPLE_CAP = 10

    for fp_file in files:
        stats["checked"] += 1
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Skipping unparseable {fp_file.name}: {exc}")
            stats["unparseable"] += 1
            continue

        pm = fp.get("paper_metadata") or {}

        needs_doi   = not pm.get("doi")
        needs_pmcid = not pm.get("pmcid")
        needs_title = not pm.get("title")
        if not (needs_doi or needs_pmcid or needs_title):
            stats["no_changes_needed"] += 1
            continue

        paper = by_filename.get(fp_file.stem)
        if paper is None:
            stats["no_db_match"] += 1
            continue

        if not (paper.doi or paper.pmcid or paper.title):
            stats["skipped_no_canonical"] += 1
            continue

        changes: list[str] = []
        if needs_doi and paper.doi:
            pm["doi"] = paper.doi
            changes.append(f"doi -> {paper.doi}")
        if needs_pmcid and paper.pmcid:
            pm["pmcid"] = paper.pmcid
            changes.append(f"pmcid -> {paper.pmcid}")
        if needs_title and paper.title:
            pm["title"] = paper.title
            changes.append(f"title -> '{paper.title[:60]}...'" if len(paper.title) > 60
                           else f"title -> '{paper.title}'")

        if not changes:
            stats["no_changes_needed"] += 1
            continue

        fp["paper_metadata"] = pm

        if args.dry_run:
            stats["would_update"] += 1
            if sample_logged < SAMPLE_CAP:
                logger.info(f"[would update] {fp_file.name}: {'; '.join(changes)}")
                sample_logged += 1
        else:
            fp_file.write_text(
                json.dumps(fp, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            stats["updated"] += 1
            if sample_logged < SAMPLE_CAP:
                logger.info(f"[updated] {fp_file.name}: {'; '.join(changes)}")
                sample_logged += 1

    logger.info("--- summary ---")
    for k, v in stats.items():
        logger.info(f"  {k}: {v:,}")

    if args.dry_run and stats["would_update"]:
        logger.info(
            f"Run without --dry-run to apply {stats['would_update']:,} updates."
        )


if __name__ == "__main__":
    main()
