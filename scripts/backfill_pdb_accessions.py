#!/usr/bin/env python3
"""
Backfill paper_metadata.pdb_accessions in existing fingerprints without re-curation.

For each fingerprint that is missing pdb_accessions (or has an empty list), queries
the RCSB PDB Search API using the paper's DOI (and PMID as fallback) to find all PDB
entries whose primary citation is that paper.

IMPORTANT LIMITATION: Only finds structures *deposited by* the paper (primary citation).
Structures merely *referenced* in the paper body (e.g. 7AHL cited as a reference in the
MEDI4893 paper) will NOT be found. Those referenced structures still require manual lookup
or re-curation.

Usage:
    python scripts/backfill_pdb_accessions.py [--dry-run] [--doi 10.1074/jbc.M114.601328]
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
FINGERPRINT_DIR = ROOT / "data" / "fingerprints"
REQUEST_DELAY_S = 0.3  # be polite to RCSB


def rcsb_query_by_doi(doi: str) -> list[str]:
    """Return PDB IDs whose primary citation DOI matches (case-insensitive)."""
    # RCSB stores DOIs in uppercase
    payload = {
        "query": {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_primary_citation.pdbx_database_id_DOI",
                "operator": "exact_match",
                "value": doi.upper(),
            },
        },
        "return_type": "entry",
        "request_options": {"return_all_hits": True},
    }
    try:
        r = requests.post(RCSB_SEARCH_URL, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [hit["identifier"] for hit in data.get("result_set", [])]
    except requests.RequestException as e:
        logger.warning(f"RCSB DOI query failed for {doi}: {e}")
        return []


def rcsb_query_by_pmid(pmid: str) -> list[str]:
    """Return PDB IDs whose primary citation PubMed ID matches."""
    payload = {
        "query": {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_primary_citation.pdbx_database_id_PubMed",
                "operator": "exact_match",
                "value": pmid,
            },
        },
        "return_type": "entry",
        "request_options": {"return_all_hits": True},
    }
    try:
        r = requests.post(RCSB_SEARCH_URL, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [hit["identifier"] for hit in data.get("result_set", [])]
    except requests.RequestException as e:
        logger.warning(f"RCSB PMID query failed for {pmid}: {e}")
        return []


def get_pmid_for_doi(doi: str) -> str | None:
    """Look up PMID in the local SQLite DB for a given DOI."""
    try:
        import sqlite3
        import yaml

        with open(ROOT / "config.yaml") as f:
            config = yaml.safe_load(f)
        db_path = ROOT / config["paths"]["db_path"]
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT pmid FROM papers WHERE doi = ?", (doi,)).fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.debug(f"Could not look up PMID for {doi}: {e}")
        return None


def load_fingerprint(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_fingerprint(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def needs_backfill(fp: dict) -> bool:
    """True if pdb_accessions is absent or empty."""
    return not fp.get("paper_metadata", {}).get("pdb_accessions")


def backfill_one(fp_path: Path, dry_run: bool) -> tuple[str, list[str]]:
    """
    Returns (doi_or_key, pdb_ids_found).
    Writes updated fingerprint unless dry_run.
    """
    fp = load_fingerprint(fp_path)

    doi = fp.get("paper_metadata", {}).get("doi")
    if not doi:
        return (fp_path.stem, [])

    pdb_ids = rcsb_query_by_doi(doi)
    time.sleep(REQUEST_DELAY_S)

    if not pdb_ids:
        # Fallback: try PMID
        pmid = get_pmid_for_doi(doi)
        if pmid:
            pdb_ids = rcsb_query_by_pmid(pmid)
            time.sleep(REQUEST_DELAY_S)

    if pdb_ids and not dry_run:
        fp.setdefault("paper_metadata", {})["pdb_accessions"] = pdb_ids
        save_fingerprint(fp_path, fp)

    return (doi, pdb_ids)


def main():
    parser = argparse.ArgumentParser(description="Backfill pdb_accessions in fingerprints via RCSB API")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written without modifying files")
    parser.add_argument("--doi", default=None, help="Process only this DOI (for testing)")
    parser.add_argument("--all", dest="all_fps", action="store_true",
                        help="Process all fingerprints, including those with existing pdb_accessions")
    args = parser.parse_args()

    fps = sorted(FINGERPRINT_DIR.glob("*.json"))
    if not fps:
        logger.error(f"No fingerprints found in {FINGERPRINT_DIR}")
        sys.exit(1)

    if args.doi:
        # Normalise to filename key format
        safe = args.doi.replace("/", "_").replace(":", "_").replace(" ", "_")
        fps = [f for f in fps if safe in f.stem]
        if not fps:
            logger.error(f"No fingerprint found for DOI {args.doi}")
            sys.exit(1)

    updated = 0
    skipped = 0
    not_found = 0

    for fp_path in fps:
        fp = load_fingerprint(fp_path)
        if not args.all_fps and not needs_backfill(fp):
            skipped += 1
            continue

        doi, pdb_ids = backfill_one(fp_path, dry_run=args.dry_run)

        if pdb_ids:
            action = "would write" if args.dry_run else "wrote"
            logger.info(f"{doi}: {action} pdb_accessions={pdb_ids}")
            updated += 1
        else:
            logger.debug(f"{doi}: no deposited structures found in RCSB")
            not_found += 1

    print(f"\nDone. updated={updated}  no_structures={not_found}  skipped_already_filled={skipped}")
    if args.dry_run:
        print("(dry-run — no files written)")


if __name__ == "__main__":
    main()
