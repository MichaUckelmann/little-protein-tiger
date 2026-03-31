#!/usr/bin/env python3
"""
CLI entry point for Sprint 1 paper fetching.

Usage:
    python scripts/fetch_papers.py [--config config.yaml] [--keywords "custom term"] [--max 100] [--dry-run]
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml
from dotenv import load_dotenv
from loguru import logger

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import Database
from src.models import DownloadStatus
from src.search import EuropePMCClient, NCBIPMCClient
from src.downloader import download_papers
from src.ranking import score_paper, is_conference_abstract, is_tiered_journal


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def print_stats(db: Database):
    stats = db.stats()
    print("\n-- Database Stats --")
    print(f"  Total papers : {stats['total']}")
    print(f"  By status    : {stats['by_status']}")
    print(f"  By source    : {stats['by_source']}")
    print()


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Fetch scientific PDFs from PMC and preprint servers")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--keywords", nargs="+", help="Override keywords from config")
    parser.add_argument("--max", type=int, help="Max results per keyword (overrides config)")
    parser.add_argument("--dry-run", action="store_true", help="Search only, skip downloads")
    args = parser.parse_args()

    config = load_config(args.config)
    keywords     = args.keywords or config["keywords"]
    max_results  = args.max or config["search"]["max_results_per_query"]
    epmc_sources = config["search"].get("europepmc_sources", ["ppr"])
    use_ncbi     = config["search"].get("ncbi_pmc", True)
    pdf_dir      = Path(config["paths"]["pdf_dir"])
    db_path      = Path(config["paths"]["db_path"])
    delay_epmc   = config["rate_limits"]["europepmc_delay_s"]
    delay_ncbi   = config["rate_limits"].get("ncbi_delay_s", 0.34)
    delay_dl     = config["rate_limits"]["download_delay_s"]
    max_retries  = config["rate_limits"]["max_retries"]
    quality_cfg  = config.get("quality", {})
    tier1_extra  = quality_cfg.get("tier1_extra", [])
    tier2_extra  = quality_cfg.get("tier2_extra", [])
    exclude_types = {t.lower() for t in quality_cfg.get("exclude_pub_types", ["Congress", "Meeting Abstract"])}
    min_score         = quality_cfg.get("min_score_to_download", 0.0)
    require_tiered    = quality_cfg.get("require_tiered_journal", False)

    pdf_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Config loaded: {len(keywords)} keywords, max {max_results}/keyword")
    if args.dry_run:
        logger.info("DRY RUN mode — no files will be downloaded")

    db = Database(db_path)
    all_papers: dict[str, object] = {}

    # --- NCBI PMC search (journal articles) ---
    if use_ncbi:
        logger.info("=== NCBI PMC eSearch ===")
        ncbi = NCBIPMCClient(delay_s=delay_ncbi)
        ncbi_papers = ncbi.search(keywords=keywords, max_results=max_results)
        for p in ncbi_papers:
            key = p.doi or p.pmcid or p.title
            all_papers[key] = p

    # --- Europe PMC search (preprints) ---
    if epmc_sources:
        logger.info("=== Europe PMC (preprints) ===")
        epmc = EuropePMCClient(delay_s=delay_epmc)
        epmc_papers = epmc.search(keywords=keywords, max_results=max_results, sources=epmc_sources)
        for p in epmc_papers:
            key = p.doi or p.pmcid or p.title
            if key not in all_papers:
                all_papers[key] = p

    papers = list(all_papers.values())
    logger.info(f"Total unique papers across all sources: {len(papers)}")

    # Score and mark conference abstracts
    excluded = 0
    for paper in papers:
        paper.priority_score = score_paper(paper, tier1_extra, tier2_extra)
        if paper.priority_score == 0.0:
            paper.download_status = DownloadStatus.failed
            excluded += 1
    if excluded:
        logger.info(f"Excluded {excluded} conference abstracts (score=0)")

    papers.sort(key=lambda p: p.priority_score, reverse=True)

    for paper in papers:
        db.upsert_paper(paper)
    logger.info(f"Upserted {len(papers)} papers to DB")

    if args.dry_run:
        print(f"\n-- Dry Run Results ({len(papers)} papers found) --")
        print(f"  {'#':>3}  {'score':>5}  {'type':<8}  title")
        print(f"  {'-'*3}  {'-'*5}  {'-'*8}  {'-'*60}")
        for i, p in enumerate(papers[:50], 1):
            src = p.source.value[:7] if p.source else "unknown"
            score_str = f"{p.priority_score:.2f}"
            marker = "!" if p.priority_score == 0.0 else (" " if p.priority_score >= 0.7 else " ")
            print(f"  {i:3}.{marker} {score_str}  {src:<8}  {p.title[:70]}")
        if len(papers) > 50:
            print(f"  ... and {len(papers) - 50} more")
        print_stats(db)
        return

    # Download — re-fetch from DB sorted by priority, skip excluded
    all_db_papers = db.get_papers()
    pending = [
        p for p in all_db_papers
        if p.download_status == DownloadStatus.pending
        and p.priority_score > min_score
        and (not require_tiered or is_tiered_journal(p.journal, tier1_extra, tier2_extra))
    ]
    pending.sort(key=lambda p: p.priority_score, reverse=True)
    logger.info(f"{len(pending)} papers pending download (sorted by priority)")

    download_papers(
        papers=pending,
        db=db,
        pdf_dir=pdf_dir,
        delay_s=delay_dl,
        max_retries=max_retries,
        dry_run=False,
    )

    print_stats(db)


if __name__ == "__main__":
    main()
