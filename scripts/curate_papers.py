#!/usr/bin/env python3
"""
Curate downloaded papers via Claude.

Usage:
    python scripts/curate_papers.py [--limit N] [--reprocess] [--dry-run] [--paper-key KEY]
"""
import argparse
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from loguru import logger

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from src.database import Database, _paper_key
from src.models import Paper
from src.text_extractor import extract_text
from src.curator import curate_paper
from src.fingerprint_store import save_fingerprint


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_paper_key(paper: Paper) -> str:
    return _paper_key(paper.doi, paper.pmcid, paper.pmid, paper.title)


def resolve_file_path(paper: Paper, config: dict) -> Path | None:
    """Return path to the downloaded file (PDF or XML), or None if missing."""
    if paper.pdf_path:
        p = Path(paper.pdf_path)
        if p.exists():
            return p
        # Try resolving relative to project root
        p2 = ROOT / paper.pdf_path
        if p2.exists():
            return p2
    return None


def main():
    parser = argparse.ArgumentParser(description="Curate papers with Claude")
    parser.add_argument("--limit", type=int, default=0, help="Max papers to process (0 = all)")
    parser.add_argument("--reprocess", action="store_true", help="Reset and reprocess already-curated papers")
    parser.add_argument("--dry-run", action="store_true", help="List papers without calling Claude")
    parser.add_argument("--paper-key", dest="paper_key", default=None, help="Process a single paper by key")
    parser.add_argument("--provider", default=None, choices=["claude", "gemini", "local"],
                        help="Override curation provider from config (claude, gemini, or local)")
    args = parser.parse_args()

    config = load_config()
    if args.provider:
        config["curation"]["provider"] = args.provider
    db_path = ROOT / config["paths"]["db_path"]
    fingerprint_dir = ROOT / config["paths"]["fingerprint_dir"]
    curation_cfg = config.get("curation", {})
    delay_s = curation_cfg.get("delay_s", 1.0)

    db = Database(db_path)

    # --reprocess: reset specific or all curated papers back to pending
    if args.reprocess:
        if args.paper_key:
            db.reset_curation(args.paper_key)
            logger.info(f"Reset curation for {args.paper_key}")
        else:
            papers_all = db.get_papers()
            reset_count = 0
            for p in papers_all:
                if p.curation_status.value in ("completed", "failed", "skipped"):
                    key = get_paper_key(p)
                    db.reset_curation(key)
                    reset_count += 1
            logger.info(f"Reset {reset_count} papers to curation_status=pending")

    # Fetch target papers
    if args.paper_key:
        all_papers = db.get_papers()
        papers = [p for p in all_papers if get_paper_key(p) == args.paper_key]
        if not papers:
            logger.error(f"Paper key not found: {args.paper_key}")
            sys.exit(1)
    else:
        papers = db.get_uncurated(limit=args.limit)

    if not papers:
        logger.info("No uncurated downloaded papers found.")
        return

    logger.info(f"Found {len(papers)} papers to process")

    stats = {"curated": 0, "skipped": 0, "failed": 0, "tokens": 0}

    for i, paper in enumerate(papers, start=1):
        paper_key = get_paper_key(paper)
        title_short = paper.title[:70] + "..." if len(paper.title) > 70 else paper.title
        logger.info(f"[{i}/{len(papers)}] {paper_key} — {title_short}")

        file_path = resolve_file_path(paper, config)
        if file_path is None:
            logger.warning(f"  File not found on disk — skipping: pdf_path={paper.pdf_path}")
            stats["failed"] += 1
            if not args.dry_run:
                db.mark_curation_failed(paper_key, "file not found on disk")
            continue

        try:
            text, source_format = extract_text(file_path, max_chars=curation_cfg.get("max_input_chars", 150000))
        except Exception as exc:
            logger.warning(f"  Text extraction failed: {exc}")
            stats["failed"] += 1
            if not args.dry_run:
                db.mark_curation_failed(paper_key, f"extraction error: {exc}")
            continue

        logger.info(f"  Extracted {len(text):,} chars from {source_format.upper()}")

        if args.dry_run:
            logger.info("  [dry-run] skipping Claude call")
            continue

        # Call Claude
        try:
            fingerprint = curate_paper(
                paper_key=paper_key,
                text=text,
                source_format=source_format,
                config=config,
                prompt_path=ROOT / curation_cfg.get("prompt_path", "curation_prompt.md"),
            )
        except Exception as exc:
            logger.error(f"  Curation failed: {exc}")
            stats["failed"] += 1
            db.mark_curation_failed(paper_key, str(exc))
            time.sleep(delay_s)
            continue

        if not fingerprint.get("relevant", True):
            logger.info("  Irrelevant paper — marking skipped")
            stats["skipped"] += 1
            db.mark_curation_skipped(paper_key)
            time.sleep(delay_s)
            continue

        # Persist fingerprint
        fp_path = save_fingerprint(paper_key, fingerprint, fingerprint_dir)
        cm = fingerprint.get("curation_metadata", {})
        tokens = (cm.get("input_tokens", 0) or 0) + (cm.get("output_tokens", 0) or 0)
        model = cm.get("model", curation_cfg.get("model", "unknown"))

        db.mark_curated(paper_key, str(fp_path), model, tokens)
        stats["curated"] += 1
        stats["tokens"] += tokens

        logger.info(f"  Saved fingerprint → {fp_path.name}  ({tokens:,} tokens)")
        time.sleep(delay_s)

    # Final summary
    print("\n--- Curation Summary ---")
    print(f"  Curated : {stats['curated']}")
    print(f"  Skipped : {stats['skipped']}  (irrelevant papers)")
    print(f"  Failed  : {stats['failed']}")
    print(f"  Tokens  : {stats['tokens']:,}")


if __name__ == "__main__":
    main()
