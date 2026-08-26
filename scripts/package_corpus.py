#!/usr/bin/env python3
"""Package the curated corpus for release. Maintainer-side.

New users should NOT have to spend money or days rebuilding a corpus that
already exists. This bundles the derived artifacts — fingerprints, the vector
index, the paper database and the graph — into one archive to attach to a
GitHub release. `scripts/fetch_corpus.py` is the other half.

**Source documents are deliberately excluded.** The PDFs and XMLs are ~95% of
the corpus on disk (50 GB of 51 GB) and nothing downstream reads them: every
tool reads fingerprints, vectors and the database. A user who wants to
re-curate or extend can re-fetch the documents themselves.

    python scripts/package_corpus.py                   # build the archive
    python scripts/package_corpus.py --check           # report what would go in

The archive is reproducible in content but not byte-identical (tar mtimes), so
the manifest carries a sha256 of each member for verification on download.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# What ships. Order matters only for readability.
MEMBERS = (
    ("data/fingerprints", "curated paper fingerprints (the corpus itself)"),
    ("data/vectors", "LanceDB embeddings for search_corpus"),
    ("data/literature.db", "paper index, search history and pipeline state"),
    ("data/depmap_edges.parquet", "protein interaction edge index"),
    ("data/clusters.json", "co-functional clusters"),
    ("data/pdb_metadata.json", "RCSB entry metadata cache"),
)

DEFAULT_OUT = _ROOT / "dist" / "lpt-corpus.tar.zst"


def _human(n: int) -> str:
    return f"{n/2**30:.2f} GB" if n >= 2**30 else f"{n/2**20:.0f} MB"


def _size(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size if path.is_file() else 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def corpus_stats() -> dict:
    """Facts a downloader will want before spending the bandwidth."""
    stats: dict = {}
    db = _ROOT / "data" / "literature.db"
    if db.is_file():
        conn = sqlite3.connect(db)
        try:
            q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
            stats["papers_indexed"] = q("SELECT COUNT(*) FROM papers")
            stats["papers_curated"] = q(
                "SELECT COUNT(*) FROM papers WHERE curation_status='completed'")
        finally:
            conn.close()
    fps = _ROOT / "data" / "fingerprints"
    stats["fingerprints"] = len(list(fps.glob("*.json"))) if fps.is_dir() else 0
    return stats


def _verify_no_personal_data() -> list[str]:
    """The DB stores paths. Refuse to ship absolute or personal ones."""
    problems: list[str] = []
    db = _ROOT / "data" / "literature.db"
    if not db.is_file():
        return ["data/literature.db is missing"]
    conn = sqlite3.connect(db)
    try:
        for col in ("pdf_path", "fingerprint_path"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE {col} LIKE '/%' "
                f"OR {col} LIKE '%:\\%' ESCAPE '\\'").fetchone()[0]
            if n:
                problems.append(f"{n} rows have an absolute {col}")
            n = conn.execute(
                f"SELECT COUNT(*) FROM papers WHERE {col} LIKE '%home%'"
            ).fetchone()[0]
            if n:
                problems.append(f"{n} rows have a home-directory {col}")
    finally:
        conn.close()
    return problems


def check() -> int:
    print(f"Corpus release contents (from {_ROOT}/data)\n")
    total = 0
    missing = []
    for rel, why in MEMBERS:
        path = _ROOT / rel
        size = _size(path)
        total += size
        mark = "ok  " if size else "MISS"
        if not size:
            missing.append(rel)
        print(f"  [{mark}] {rel:30s} {_human(size):>9s}   {why}")
    print(f"\n  {'total':37s} {_human(total):>9s} uncompressed")

    stats = corpus_stats()
    if stats:
        print(f"\n  {stats.get('papers_curated', 0):,} curated papers, "
              f"{stats.get('papers_indexed', 0):,} indexed, "
              f"{stats.get('fingerprints', 0):,} fingerprints")

    problems = _verify_no_personal_data()
    print()
    if problems:
        for p in problems:
            print(f"  [FAIL] {p}")
        print("\n  Refusing to package: the database would leak local paths.")
        return 1
    print("  [ok  ] no absolute or personal paths in the database")

    if missing:
        print(f"\n  Cannot package — missing: {', '.join(missing)}")
        return 1
    print("\n  Ready to package.")
    return 0


def package(out: Path, level: int = 10) -> int:
    if check():
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)

    stats = corpus_stats()
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contents": [rel for rel, _ in MEMBERS],
        "excludes": ["data/pdfs (source documents — ~95% of the corpus on "
                     "disk, and nothing downstream reads them)"],
        **stats,
    }

    print(f"\nPackaging -> {out}")
    with tempfile.TemporaryDirectory() as td:
        tar_path = Path(td) / "corpus.tar"
        with tarfile.open(tar_path, "w") as tf:
            mf = Path(td) / "MANIFEST.json"
            mf.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            tf.add(mf, arcname="MANIFEST.json")
            for rel, _ in MEMBERS:
                print(f"  + {rel}")
                tf.add(_ROOT / rel, arcname=rel)

        raw = tar_path.stat().st_size
        # zstd -10 is a good size/time trade here; the archive is written once
        # and downloaded many times.
        try:
            subprocess.run(["zstd", f"-{level}", "-q", "-f",
                            str(tar_path), "-o", str(out)], check=True)
        except FileNotFoundError:
            print("  zstd not found — falling back to gzip")
            out = out.with_suffix(".gz")
            subprocess.run(["gzip", "-9", "-c", str(tar_path)],
                           stdout=out.open("wb"), check=True)

    comp = out.stat().st_size
    print(f"\n  {out}")
    print(f"  {_human(raw)} -> {_human(comp)}  ({100*comp/raw:.0f}% of raw)")
    print(f"  sha256 {_sha256(out)}")
    print(f"\n  {stats.get('papers_curated', 0):,} curated papers.")
    print("\nAttach this to a GitHub release; scripts/fetch_corpus.py downloads"
          "\nthe latest release asset by name.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="Report what would be packaged; build nothing.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--level", type=int, default=10, help="zstd level (default 10)")
    args = ap.parse_args()
    return check() if args.check else package(args.out, args.level)


if __name__ == "__main__":
    raise SystemExit(main())
