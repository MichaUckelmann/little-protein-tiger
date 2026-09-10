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
    # DECIMAL MB, not MiB. This divided by 2**20 and wrote "MB", so a
    # 104,535,812-byte asset printed as "100 MB" while every document said
    # ~105 MB and GitHub's own release page said 104 MB — three figures for one
    # file, and `SETUP_AGENT.md` tells the reader to trust this tool over its
    # own prose. Decimal is what GitHub, the docs and `ls -l` agree on.
    return f"{n/1e9:.2f} GB" if n >= 1e9 else f"{n/1e6:.0f} MB"


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
#: Only these, and only directly under `data/fingerprints/`. The first version
#: of this dropped ANY path with a leading-underscore component, which also
#: removed LanceDB's own `_versions/`, `_transactions/` and `_deletions/`
#: directories — and without `_versions/` the table cannot be opened at all.
#: That shipped: the 2026-09-10 asset listed `fingerprints` in
#: `table_names()` (which reads directory names) and then raised
#: `ValueError: Table 'fingerprints' was not found` on open, so `search_corpus`
#: was dead for anyone who downloaded it. A broad "looks internal" heuristic
#: has no business near a database's on-disk format.
_SCRATCH_DIRS = ("_gemma_compare", "_provider_compare")


def _shippable(info: "tarfile.TarInfo") -> "tarfile.TarInfo | None":
    """Drop maintainer scratch from the archive. Returning None omits it."""
    parts = Path(info.name).parts
    if len(parts) >= 3 and parts[0] == "data" and parts[1] == "fingerprints":
        if parts[2] in _SCRATCH_DIRS:
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


def permitted_keys(conn: sqlite3.Connection) -> tuple[set[str], dict[str, int]]:
    """Curated papers whose licence AFFIRMATIVELY permits derivative works.

    `papers.licence` is written by `scripts/audit_paper_licences.py`. NULL means
    never checked and `""` means checked-and-none-recorded; `permits_derivatives`
    treats both as restricted, which is the point — the conservative reading has
    to be the one you get for free.
    """
    from src.paper_licence import classify, permits_derivatives

    cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    if "licence" not in cols:
        raise SystemExit(
            "papers.licence does not exist. Run\n"
            "    python scripts/audit_paper_licences.py\n"
            "to resolve licences before packaging, or pass --no-licence-filter "
            "to ship every fingerprint regardless (see docs/licensing.md).")
    rows = conn.execute(
        "SELECT paper_key, licence FROM papers WHERE curation_status='completed'"
    ).fetchall()
    keep = {k for k, lic in rows if permits_derivatives(lic)}
    counts: dict[str, int] = {}
    for _k, lic in rows:
        v = classify(lic)
        counts[v] = counts.get(v, 0) + 1
    return keep, counts


def _fingerprint_keys(fp_dir: Path) -> dict[Path, str]:
    """{fingerprint file: paper_key}, derived exactly as the vector store does.

    Reusing `VectorStore._derive_paper_key` rather than parsing the filename:
    the two must agree or the filtered fingerprint set and the filtered vector
    table would disagree about the same paper.
    """
    import json as _json

    from src.vector_store import VectorStore

    out: dict[Path, str] = {}
    for f in sorted(fp_dir.glob("*.json")):
        try:
            fp = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            continue
        out[f] = VectorStore._derive_paper_key(fp, f.stem)
    return out


def stage_permitted(workdir: Path, keep: set[str]) -> tuple[Path, set[str]]:
    """Build a `data/` tree carrying only licence-permitted derivatives.

    Every artifact below is derived from fingerprint TEXT, so filtering the
    JSONs alone would not be enough:

      * `fingerprints/` — the derivative itself.
      * `vectors/` — the LanceDB table stores `embed_text` AND
        `fingerprint_json` inline, so an unfiltered index ships the very text
        the JSON was withheld to avoid shipping.
      * `depmap_edges.parquet` / `clusters.json` — aggregated from
        fingerprints, so they are rebuilt from the filtered set rather than
        copied.
      * `literature.db` — bibliographic metadata is fact, not expression, so
        every row stays; but `fingerprint_path` is cleared for a paper whose
        fingerprint is not in the archive, or the shipped database would point
        at files that do not exist.

    `pdb_metadata.json` is RCSB entry metadata and derives from no paper.
    """
    data = workdir / "data"
    (data / "fingerprints").mkdir(parents=True, exist_ok=True)
    src = _ROOT / "data"

    # ── fingerprints
    keys = _fingerprint_keys(src / "fingerprints")
    kept_files = [f for f, k in keys.items() if k in keep]
    for f in kept_files:
        shutil.copy2(f, data / "fingerprints" / f.name)
    print(f"  fingerprints  {len(kept_files):,} of {len(keys):,} kept")

    # ── vectors: filter the table by paper_key into a fresh LanceDB
    import lancedb

    src_db = lancedb.connect(str(src / "vectors"))
    names = list(src_db.table_names())
    if names:
        tbl = src_db.open_table(names[0])
        arrow = tbl.to_arrow()
        mask = [k in keep for k in arrow["paper_key"].to_pylist()]
        import pyarrow as pa

        filtered = arrow.filter(pa.array(mask))
        dest_db = lancedb.connect(str(data / "vectors"))
        dest_db.create_table(names[0], data=filtered, mode="overwrite")
        print(f"  vectors       {filtered.num_rows:,} of {arrow.num_rows:,} rows kept")

    # ── edges + clusters, rebuilt from what is actually shipping
    from src.clustering import build_and_cluster

    res = build_and_cluster(data / "fingerprints",
                            edges_path=data / "depmap_edges.parquet",
                            clusters_path=data / "clusters.json",
                            force_rebuild_edges=True)
    print(f"  graph         rebuilt: {res.get('cluster_count', 0):,} clusters "
          f"from the kept fingerprints")

    # ── everything paper-independent
    for name in ("pdb_metadata.json",):
        if (src / name).is_file():
            shutil.copy2(src / name, data / name)
    shipped = {keys[f] for f in kept_files}
    return data, shipped


def _sanitized_db(workdir: Path,
                  shipped_files: set[str] | None = None) -> tuple[Path, int]:
    """A copy of literature.db with third-party text blanked. Returns (path, rows).

    `shipped_files` is the set of fingerprint FILENAMES actually in the
    archive, and a row is cleared when its `fingerprint_path` basename is not
    among them. Deliberately the filenames and not a set of paper_keys:
    matching on keys left 255 rows pointing at files that had not shipped
    (`papers.paper_key` and `VectorStore._derive_paper_key` do not always
    agree), and then 4 more after that was fixed. The invariant worth
    enforcing is "no row points at a file that is not here", so check exactly
    that.
    """
    src = _ROOT / "data" / "literature.db"
    dest = workdir / "literature.db"
    shutil.copy2(src, dest)
    cleared = 0
    conn = sqlite3.connect(dest)
    try:
        if shipped_files is not None:
            rows = conn.execute(
                "SELECT paper_key, fingerprint_path FROM papers "
                "WHERE curation_status='completed'").fetchall()
            dropped = [(k,) for k, path in rows
                       if not path or Path(path).name not in shipped_files]
            conn.executemany(
                "UPDATE papers SET fingerprint_path=NULL, "
                "curation_status='licence_withheld' WHERE paper_key=?", dropped)
            print(f"  literature.db {len(dropped):,} rows marked "
                  f"'licence_withheld' (row kept, fingerprint not shipped)")
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


def _verify_archive(out: Path) -> bool:
    """Extract the finished archive and USE it, before anyone else has to.

    `--check` inspects the local corpus; this inspects the artifact. The
    distinction is not academic: a tar filter meant to drop two scratch
    directories also stripped LanceDB's `_versions/`, and every local check
    passed while the shipped vector index could not be opened. Nothing short of
    opening the packaged table would have caught it.
    """
    import sqlite3 as _sq
    import subprocess as _sp
    import tempfile as _tf

    print("\n  Verifying the archive itself:")
    with _tf.TemporaryDirectory() as td:
        root = Path(td)
        try:
            _sp.run(f'zstd -dc "{out}" | tar -xf - -C "{root}"',
                    shell=True, check=True, capture_output=True)
        except _sp.CalledProcessError as exc:
            print(f"    [FAIL] cannot extract: {exc.stderr[:200]!r}")
            return False

        data = root / "data"
        fps = len(list((data / "fingerprints").glob("*.json")))
        print(f"    [ok  ] {fps:,} fingerprint JSONs")

        try:
            import lancedb

            tbl = lancedb.connect(str(data / "vectors")).open_table("fingerprints")
            rows = tbl.count_rows()
        except Exception as exc:                              # noqa: BLE001
            print(f"    [FAIL] the vector index does not open: "
                  f"{type(exc).__name__}: {exc}")
            print("           search_corpus would be dead for every user.")
            return False
        if rows == 0:
            print("    [FAIL] the vector index opens but is empty")
            return False
        print(f"    [ok  ] vector index opens, {rows:,} rows")

        try:
            conn = _sq.connect(f"file:{data / 'literature.db'}?mode=ro", uri=True)
            n = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            conn.close()
        except Exception as exc:                              # noqa: BLE001
            print(f"    [FAIL] the database does not open: {exc}")
            return False
        print(f"    [ok  ] database opens, {n:,} papers")

        for name in ("depmap_edges.parquet", "clusters.json"):
            if not (data / name).is_file():
                print(f"    [FAIL] {name} missing")
                return False
        print("    [ok  ] edge index and clusters present")

        names = {f.name for f in (data / "fingerprints").glob("*.json")}
        conn = _sq.connect(f"file:{data / 'literature.db'}?mode=ro", uri=True)
        dangling = [p for (p,) in conn.execute(
            "SELECT fingerprint_path FROM papers WHERE fingerprint_path "
            "IS NOT NULL AND TRIM(fingerprint_path) <> ''")
            if Path(p).name not in names]
        conn.close()
        if dangling:
            print(f"    [FAIL] {len(dangling):,} database rows point at "
                  f"fingerprints not in the archive, e.g. {dangling[0]}")
            return False
        print("    [ok  ] no database row points at a missing fingerprint")
    return True


def package(out: Path, level: int = 10, licence_filter: bool = True) -> int:
    if check():
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)

    stats = corpus_stats()
    excludes = ["data/pdfs (source documents — ~95% of the corpus on "
                "disk, and nothing downstream reads them)",
                "papers.abstract (publisher-supplied text; nothing reads "
                "it — see docs/licensing.md)"]
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contents": [rel for rel, _ in MEMBERS],
        **stats,
    }

    print(f"\nPackaging -> {out}")
    with tempfile.TemporaryDirectory() as td:
        tar_path = Path(td) / "corpus.tar"
        staged: Path | None = None
        keep: set[str] = set()
        shipped_keys: set[str] = set()

        if licence_filter:
            with sqlite3.connect(_ROOT / "data" / "literature.db") as conn:
                keep, counts = permitted_keys(conn)
            print(f"  licence filter ON — keeping only papers whose licence "
                  f"permits derivatives")
            print(f"    derivatives permitted {counts.get('derivatives_ok', 0):>7,}")
            print(f"    NO DERIVATIVES        {counts.get('no_derivatives', 0):>7,}"
                  f"   excluded")
            print(f"    unknown / unlicensed  {counts.get('unknown', 0):>7,}"
                  f"   excluded")
            staged, shipped_keys = stage_permitted(Path(td) / "staged", keep)
            shipped = len(list((staged / "fingerprints").glob("*.json")))
            manifest["fingerprints"] = shipped
            manifest["papers_curated_shipped"] = shipped
            manifest["papers_curated_local"] = stats.get("papers_curated")
            manifest["licence_filter"] = {
                "applied": True,
                "rule": "ship a fingerprint only when papers.licence "
                        "affirmatively permits derivative works; a No-Derivatives "
                        "term or no recorded licence excludes it",
                "source": "europepmc, via scripts/audit_paper_licences.py",
                **{k: v for k, v in counts.items()},
            }
            excludes.append(
                f"fingerprints, vectors and graph entries for "
                f"{counts.get('no_derivatives', 0) + counts.get('unknown', 0):,} "
                f"papers whose licence does not permit derivative works")
        else:
            print("  licence filter OFF (--no-licence-filter) — every "
                  "fingerprint ships regardless of the paper's licence")
            manifest["licence_filter"] = {"applied": False}
        manifest["excludes"] = excludes

        with tarfile.open(tar_path, "w") as tf:
            mf = Path(td) / "MANIFEST.json"
            mf.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            tf.add(mf, arcname="MANIFEST.json")
            shipped_files = ({f.name for f in
                              (staged / "fingerprints").glob("*.json")}
                             if licence_filter else None)
            clean_db, cleared = _sanitized_db(Path(td), shipped_files)
            for rel, _ in MEMBERS:
                if rel == "data/literature.db":
                    print(f"  + {rel}  ({cleared:,} abstracts stripped)")
                    tf.add(clean_db, arcname=rel)
                    continue
                source = (staged.parent / rel) if staged else (_ROOT / rel)
                if not source.exists():
                    print(f"  - {rel}  (nothing left after the licence filter)")
                    continue
                print(f"  + {rel}")
                tf.add(source, arcname=rel, filter=_shippable)

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
    if not _verify_archive(out):
        return 1

    print(f"\n  {manifest.get('fingerprints', 0):,} fingerprints in the "
          f"archive.")
    print("\nAttach this to a GitHub release; scripts/fetch_corpus.py downloads"
          "\nthe latest release asset by name.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="Report what would be packaged; build nothing.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--level", type=int, default=10, help="zstd level (default 10)")
    ap.add_argument("--no-licence-filter", dest="licence_filter",
                    action="store_false", default=True,
                    help="ship every fingerprint regardless of the paper's "
                         "licence. The default EXCLUDES No-Derivatives and "
                         "unlicensed papers — see docs/licensing.md before "
                         "using this.")
    args = ap.parse_args()
    return (check() if args.check
            else package(args.out, args.level, args.licence_filter))


if __name__ == "__main__":
    raise SystemExit(main())
