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

**Abstracts are stripped from the shipped database.** They are the only
publisher-supplied TEXT the archive would otherwise carry, and nothing reads
them (see `_STRIPPED_COLUMNS`). What ships is derived fingerprints plus
bibliographic facts — see `docs/licensing.md`.

    python scripts/package_corpus.py                   # build the archive
    python scripts/package_corpus.py --check           # report what would go in

The archive is reproducible in content but not byte-identical (tar mtimes), so
the manifest carries a sha256 of each member for verification on download.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
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


#: Directories the maintainer's own experiments leave inside `data/`, which
#: `tf.add` would otherwise recurse into: `data/fingerprints/_gemma_compare`
#: and `_provider_compare` are model-comparison scratch from
#: `scripts/bench_models.py` and shipped inside the published v0.1.0 asset.
#: Nothing downstream reads them — `corpus_stats` and the vector ingest both
#: glob `*.json` at the top level — so they were pure noise in a 100 MB
#: download. Leading-underscore is the convention those scripts already use.
def _shippable(info: "tarfile.TarInfo") -> "tarfile.TarInfo | None":
    """Drop maintainer scratch from the archive. Returning None omits it."""
    if any(part.startswith("_") for part in Path(info.name).parts[:-1]):
        return None
    if Path(info.name).name.startswith("_") and info.isdir():
        return None
    return info


# Credential shapes that must never leave this machine inside a release.
# `curation_error` is the dangerous column: it stores the exception text of a
# failed call, and `requests` embeds the full request URL in what it raises —
# so a Gemini key passed as a query parameter (which is how this repo used to
# send it) was persisted verbatim into the shipped database. Found in a real
# archive AFTER it had already been published; the paths-only check below
# reported it clean.
_SECRET_PATTERNS = (
    ("Google/Gemini API key", r"AIzaSy[0-9A-Za-z_-]{20,}"),
    ("Anthropic API key",     r"sk-ant-[0-9A-Za-z_-]{20,}"),
    ("OpenAI API key",        r"sk-[0-9A-Za-z]{32,}"),
    ("GitHub token",          r"gh[pousr]_[0-9A-Za-z]{20,}"),
    ("key= in a URL",         r"[?&]key=[0-9A-Za-z_-]{20,}"),
    ("bearer token",          r"[Bb]earer\s+[0-9A-Za-z._-]{20,}"),
)


def _verify_no_secrets() -> list[str]:
    """Scan every TEXT column of every table for credential shapes.

    Deliberately over-broad: it walks all text columns rather than a list of
    the ones known to be risky today, because the column that leaked was not
    one anybody would have listed in advance.
    """
    import re

    problems: list[str] = []
    db = _ROOT / "data" / "literature.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    compiled = [(label, re.compile(pat)) for label, pat in _SECRET_PATTERNS]
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            for col in cols:
                try:
                    rows = conn.execute(
                        f'SELECT "{col}" FROM "{table}" '
                        f'WHERE "{col}" IS NOT NULL').fetchall()
                except sqlite3.Error:
                    continue
                for (value,) in rows:
                    if not isinstance(value, str):
                        continue
                    for label, rx in compiled:
                        if rx.search(value):
                            problems.append(
                                f"{table}.{col} contains what looks like a "
                                f"{label} — refusing to ship it")
                            break
                    else:
                        continue
                    break        # one report per column is enough
    finally:
        conn.close()
    return problems


# Columns blanked in the SHIPPED copy of the database. `abstract` holds
# publisher-supplied text (11,818 rows on the reference corpus, ~1,750 chars
# each), and a PMCID is NOT a redistribution grant — PMC is free-to-READ, and
# only its Open Access Subset is redistributable. We record no per-paper
# licence, so we cannot tell the two apart.
#
# Nothing reads this column. It is written by `src/search.py` at fetch time and
# round-tripped by `src/database.py`; the curator parses the downloaded PDF/XML
# and `search_corpus` embeds fingerprints. So dropping it costs nothing and
# removes the only third-party TEXT the archive would otherwise carry.
# Verified by grep across src/ and scripts/ — if that stops being true, this
# strip has to become a decision rather than a default.
_STRIPPED_COLUMNS = (("papers", "abstract"),)


def _sanitized_db(workdir: Path) -> tuple[Path, int]:
    """A copy of literature.db with third-party text blanked. Returns (path, rows)."""
    src = _ROOT / "data" / "literature.db"
    dest = workdir / "literature.db"
    shutil.copy2(src, dest)
    cleared = 0
    conn = sqlite3.connect(dest)
    try:
        for table, col in _STRIPPED_COLUMNS:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                continue
            cleared += conn.execute(
                f'SELECT COUNT(*) FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL AND TRIM("{col}") <> \'\''
            ).fetchone()[0]
            conn.execute(f'UPDATE "{table}" SET "{col}" = NULL')
        conn.commit()
        # Without VACUUM the freed pages keep the text on disk — the file would
        # still be readable with a hex editor, which is not "removed".
        conn.execute("VACUUM")
    finally:
        conn.close()
    return dest, cleared


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

    secrets = _verify_no_secrets()
    if secrets:
        for p in secrets:
            print(f"  [FAIL] {p}")
        print("\n  Refusing to package: the database would leak a credential.")
        return 1
    print("  [ok  ] no credential-shaped strings in the database")

    db = _ROOT / "data" / "literature.db"
    if db.is_file():
        conn = sqlite3.connect(db)
        try:
            for table, col in _STRIPPED_COLUMNS:
                n = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL '
                    f"AND TRIM(\"{col}\") <> ''").fetchone()[0]
                print(f"  [strip] {table}.{col}: {n:,} rows blanked in the "
                      f"shipped copy (third-party text, unread)")
        finally:
            conn.close()

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
                     "disk, and nothing downstream reads them)",
                     "papers.abstract (publisher-supplied text; nothing reads "
                     "it — see docs/licensing.md)"],
        **stats,
    }

    print(f"\nPackaging -> {out}")
    with tempfile.TemporaryDirectory() as td:
        tar_path = Path(td) / "corpus.tar"
        with tarfile.open(tar_path, "w") as tf:
            mf = Path(td) / "MANIFEST.json"
            mf.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            tf.add(mf, arcname="MANIFEST.json")
            clean_db, cleared = _sanitized_db(Path(td))
            for rel, _ in MEMBERS:
                if rel == "data/literature.db":
                    print(f"  + {rel}  ({cleared:,} abstracts stripped)")
                    tf.add(clean_db, arcname=rel)
                    continue
                print(f"  + {rel}")
                tf.add(_ROOT / rel, arcname=rel, filter=_shippable)

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
