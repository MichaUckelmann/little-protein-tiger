#!/usr/bin/env python3
"""
Download PDB structures referenced in fingerprint pdb_accessions fields.

Scans all fingerprints in data/fingerprints/, collects every unique PDB accession
from paper_metadata.pdb_accessions, and downloads the mmCIF file from RCSB into
data/structures/<ID>.cif. Skips accessions already on disk.

Usage:
    python scripts/download_pdb_structures.py [--dry-run] [--id 4U6V] [--format cif|pdb]
"""

import argparse
import sys
import time
from pathlib import Path

import requests
import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RCSB_DOWNLOAD = "https://files.rcsb.org/download/{id}.{fmt}"
REQUEST_DELAY_S = 0.25


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def collect_accessions(fingerprint_dir: Path) -> dict[str, list[str]]:
    """
    Returns {pdb_id: [doi, ...]} mapping each unique accession to the DOIs
    of fingerprints that cite it. Used for provenance reporting.
    """
    import json
    accessions: dict[str, list[str]] = {}
    for fp_path in sorted(fingerprint_dir.glob("*.json")):
        try:
            data = json.loads(fp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        doi = data.get("paper_metadata", {}).get("doi", fp_path.stem)
        for pdb_id in data.get("paper_metadata", {}).get("pdb_accessions", []):
            pdb_id = pdb_id.upper().strip()
            if pdb_id:
                accessions.setdefault(pdb_id, []).append(doi)
    return accessions


def download_structure(pdb_id: str, dest: Path, fmt: str) -> bool:
    """Download one structure. Returns True on success."""
    url = RCSB_DOWNLOAD.format(id=pdb_id, fmt=fmt)
    try:
        r = requests.get(url, timeout=30, stream=True)
        if r.status_code == 404:
            logger.warning(f"{pdb_id}: not found at RCSB ({url})")
            return False
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except requests.RequestException as e:
        logger.error(f"{pdb_id}: download failed — {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Download PDB structures cited in fingerprints")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be downloaded without fetching")
    parser.add_argument("--id", dest="pdb_id", default=None,
                        help="Download a single PDB ID (e.g. 4U6V)")
    parser.add_argument("--format", dest="fmt", default="cif", choices=["cif", "pdb"],
                        help="File format to download (default: cif)")
    parser.add_argument("--redownload", action="store_true",
                        help="Re-download structures already on disk")
    args = parser.parse_args()

    config = load_config()
    fingerprint_dir = ROOT / config["paths"]["fingerprint_dir"]
    structures_dir = ROOT / config["paths"].get("structures_dir", "data/structures")
    structures_dir.mkdir(parents=True, exist_ok=True)

    # Collect all accessions from fingerprints
    if args.pdb_id:
        accessions = {args.pdb_id.upper(): ["(command-line)"]}
    else:
        accessions = collect_accessions(fingerprint_dir)

    if not accessions:
        logger.info("No pdb_accessions found in any fingerprint. Run backfill_pdb_accessions.py first.")
        sys.exit(0)

    logger.info(f"Found {len(accessions)} unique PDB accessions across {fingerprint_dir.name}/")

    downloaded = 0
    skipped = 0
    failed = 0

    for pdb_id, dois in sorted(accessions.items()):
        dest = structures_dir / f"{pdb_id}.{args.fmt}"

        if dest.exists() and not args.redownload:
            logger.debug(f"{pdb_id}: already on disk, skipping")
            skipped += 1
            continue

        if args.dry_run:
            logger.info(f"{pdb_id}: would download -> {dest}  (cited by: {', '.join(dois[:3])}{'...' if len(dois) > 3 else ''})")
            downloaded += 1
            continue

        logger.info(f"{pdb_id}: downloading {args.fmt.upper()}...")
        ok = download_structure(pdb_id, dest, args.fmt)
        if ok:
            size_kb = dest.stat().st_size // 1024
            logger.info(f"{pdb_id}: saved to {dest} ({size_kb} KB)")
            downloaded += 1
        else:
            failed += 1
        time.sleep(REQUEST_DELAY_S)

    print(f"\nDone.  downloaded={downloaded}  skipped_existing={skipped}  failed={failed}")
    print(f"Structures dir: {structures_dir}")
    if args.dry_run:
        print("(dry-run — no files written)")


if __name__ == "__main__":
    main()
