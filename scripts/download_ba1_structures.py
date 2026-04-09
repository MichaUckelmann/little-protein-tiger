#!/usr/bin/env python3
"""
Download RCSB Biological Assembly 1 for every PDB already in data/structures/.

Skips entries that already have a _ba1.cif file.
Skips entries where RCSB returns 404 (no assembly defined for that entry).

Usage:
    python scripts/download_ba1_structures.py
    python scripts/download_ba1_structures.py --structures-dir path/to/structures
"""

import argparse
import gzip
import time
from pathlib import Path

import requests
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRUCTURES_DIR = ROOT / "data" / "structures"
RCSB_BA1_URL = "https://files.rcsb.org/download/{pdb_id}-assembly1.cif.gz"
REQUEST_DELAY_S = 0.3


def download_ba1(pdb_id: str, dest: Path, session: requests.Session) -> str:
    """Download BA1 for pdb_id to dest. Returns 'ok', 'skipped', or 'missing'."""
    url = RCSB_BA1_URL.format(pdb_id=pdb_id)
    try:
        r = session.get(url, timeout=30)
        if r.status_code == 404:
            return "missing"
        r.raise_for_status()
        dest.write_bytes(gzip.decompress(r.content))
        return "ok"
    except Exception as e:
        if dest.exists():
            dest.unlink()  # remove partial file
        logger.warning(f"  {pdb_id}: failed — {e}")
        return "error"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--structures-dir",
        type=Path,
        default=DEFAULT_STRUCTURES_DIR,
        help="Directory containing {PDB_ID}.cif files (default: data/structures/)",
    )
    args = parser.parse_args()

    structures_dir: Path = args.structures_dir
    if not structures_dir.exists():
        logger.error(f"Directory not found: {structures_dir}")
        raise SystemExit(1)

    # Collect PDB IDs that have an ASU but no BA1 yet
    asu_files = sorted(structures_dir.glob("????.cif"))  # exactly 4-char stem
    todo = [
        f.stem.upper()
        for f in asu_files
        if not (structures_dir / f"{f.stem.upper()}_ba1.cif").exists()
    ]

    already = len(asu_files) - len(todo)
    logger.info(f"Found {len(asu_files)} ASU files — {already} already have BA1, {len(todo)} to download")

    if not todo:
        logger.info("Nothing to do.")
        return

    counts = {"ok": 0, "missing": 0, "error": 0}
    session = requests.Session()
    session.headers.update({"User-Agent": "LittleProteinTiger/1.0"})

    for i, pdb_id in enumerate(todo, 1):
        dest = structures_dir / f"{pdb_id}_ba1.cif"
        result = download_ba1(pdb_id, dest, session)
        counts[result] += 1

        symbol = {"ok": "✓", "missing": "–", "error": "✗"}[result]
        if result == "ok":
            note = f"{dest.stat().st_size // 1024} KB"
        elif result == "missing":
            note = "no BA1"
        else:
            note = "failed"
        logger.info(f"  [{i:>4}/{len(todo)}] {symbol} {pdb_id}  {note}")

        time.sleep(REQUEST_DELAY_S)

    logger.info(
        f"\nDone — downloaded: {counts['ok']}, no BA1 on RCSB: {counts['missing']}, errors: {counts['error']}"
    )


if __name__ == "__main__":
    main()
