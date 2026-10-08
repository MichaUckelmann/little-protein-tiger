#!/usr/bin/env python3
"""Rewrite every fingerprint's `paper_metadata.title` from the paper database.

    python scripts/repair_fingerprint_titles.py            # dry run: count and show examples
    python scripts/repair_fingerprint_titles.py --apply    # back up, rewrite files, update vector rows

Why: `curate_papers.py` used to take the title from the model and use the
database title only when the model left it empty. For JATS XML papers the text
extractor sends body paragraphs only, so the model never sees the title and
writes one of its own; the database title (from PMC / Semantic Scholar search
metadata) is the correct one. Measured on the DOIs the pipeline cited, 82 of
264 fingerprints carried a title that differed from the paper's, every one of
them XML-sourced. `search_corpus` returns the fingerprint title, so a reader saw
a wrong title beside a correct DOI. The curator now always takes the database
title; this applies the same rule to what is already on disk.

Only `paper_metadata.title` changes. The embedded text does not include the
title, so no vector is recomputed and a later `ingest_vectors.py` reports every
row unchanged; the `title` and `fingerprint_json` columns of the changed rows
are updated in place so search results show the corrected title.

`--apply` first writes a tarball of the files it is about to change and a JSONL
of every old -> new title next to it (both under `data/`, which is gitignored).
Idempotent: a second run finds nothing to change.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.fingerprint_store import clean_title  # noqa: E402


def plan(db_path: Path, root: Path) -> tuple[list[dict], dict]:
    """Return (changes, stats). Reads only."""
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT paper_key, title, fingerprint_path FROM papers "
                       "WHERE curation_status='completed' AND fingerprint_path IS NOT NULL").fetchall()
    con.close()
    stats = {"db_rows": len(rows), "file_missing": 0, "no_db_title": 0, "unchanged": 0, "changed": 0}
    changes = []
    for paper_key, db_title, fp_rel in rows:
        path = root / fp_rel
        if not path.exists():
            stats["file_missing"] += 1
            continue
        new = clean_title(db_title)
        if not new:
            stats["no_db_title"] += 1
            continue
        fp = json.loads(path.read_text(encoding="utf-8"))
        old = (fp.get("paper_metadata") or {}).get("title")
        if old == new:
            stats["unchanged"] += 1
            continue
        stats["changed"] += 1
        changes.append({"paper_key": paper_key, "path": path, "old": old, "new": new})
    return changes, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-vectors", action="store_true", help="rewrite files only")
    ap.add_argument("--vectors-only", action="store_true",
                    help="only bring the vector index in line with the fingerprint files")
    ap.add_argument("--db", default=str(_ROOT / "data" / "literature.db"))
    ap.add_argument("--vector-dir", default=str(_ROOT / "data" / "vectors"))
    args = ap.parse_args()

    if args.vectors_only:
        sync_vector_rows(Path(args.vector_dir), _ROOT / "data" / "fingerprints")
        return
    changes, stats = plan(Path(args.db), _ROOT)
    print(json.dumps(stats))
    for c in changes[:5]:
        print(f"\n{c['paper_key']}\n  old: {c['old']}\n  new: {c['new']}")
    if not args.apply:
        print(f"\nDry run: {len(changes)} fingerprints would change. Pass --apply.")
        return
    if not changes:
        print("Nothing to change.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = _ROOT / "data" / f"fingerprint_titles_backup_{stamp}.tar.gz"
    with tarfile.open(bak, "w:gz") as tar:
        for c in changes:
            tar.add(c["path"], arcname=str(c["path"].relative_to(_ROOT)))
    log = _ROOT / "data" / f"fingerprint_titles_changes_{stamp}.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        for c in changes:
            fh.write(json.dumps({"paper_key": c["paper_key"], "old": c["old"], "new": c["new"]}, ensure_ascii=False) + "\n")
    print(f"Backed up {len(changes)} files -> {bak.name}; change log -> {log.name}")

    new_json: dict[str, str] = {}
    for c in changes:
        fp = json.loads(c["path"].read_text(encoding="utf-8"))
        fp.setdefault("paper_metadata", {})["title"] = c["new"]
        # Same serialisation as fingerprint_store.save_fingerprint.
        c["path"].write_text(json.dumps(fp, indent=2, ensure_ascii=False), encoding="utf-8")
        new_json[c["paper_key"]] = json.dumps(fp, ensure_ascii=False)  # as vector_store stores it
    print(f"Rewrote {len(changes)} fingerprint files.")

    if args.skip_vectors:
        return
    sync_vector_rows(Path(args.vector_dir), _ROOT / "data" / "fingerprints")


def sync_vector_rows(vector_dir: Path, fingerprint_dir: Path) -> None:
    """Bring `title` / `fingerprint_json` of the index in line with the files.

    Keyed by the index's own key (derived from the fingerprint's DOI, exactly as
    ingest does), NOT by the database's paper_key: the two differ for papers the
    database keys by PMCID but whose fingerprint carries a DOI. Keying on the
    database's left ~100 rows stale on the first run.
    """
    from src.vector_store import VectorStore
    files: dict[str, dict] = {}
    for f in sorted(fingerprint_dir.glob("*.json")):
        fp = json.loads(f.read_text(encoding="utf-8"))
        files[VectorStore._derive_paper_key(fp, f.stem)] = fp  # last file wins, as in ingest
    table = VectorStore(vector_dir)._get_table()
    rows = table.to_arrow().select(["paper_key", "title"]).to_pylist()
    updates = {}
    for r in rows:
        fp = files.get(r["paper_key"])
        if fp is None:
            continue
        want = (fp.get("paper_metadata") or {}).get("title") or ""
        if r["title"] != want:
            updates[r["paper_key"]] = (want, json.dumps(fp, ensure_ascii=False))
    if not updates:
        print("Vector index already matches the fingerprint files.")
        return
    update_vector_rows(vector_dir, updates)


def update_vector_rows(vector_dir: Path, updates: dict[str, tuple[str, str]]) -> None:
    """Set `title` and `fingerprint_json` on existing rows, keyed by paper_key.

    One merge_insert, not a loop of row updates: every LanceDB write creates a
    new table version, and the shipped index is already 28 versions deep.
    Rows are rebuilt from the table itself, so the stored vectors are carried
    over untouched.
    """
    import pyarrow as pa
    from src.vector_store import VectorStore, _sql_quote

    table = VectorStore(vector_dir)._get_table()
    data = table.to_arrow()
    keys = data["paper_key"].to_pylist()
    hit = [i for i, k in enumerate(keys) if k in updates]
    if not hit:
        print("No vector rows matched.")
        return
    src = data.take(hit)
    skeys = src["paper_key"].to_pylist()
    titles = pa.array([updates[k][0] for k in skeys], pa.string())
    fjson = pa.array([updates[k][1] for k in skeys], pa.string())
    src = src.set_column(src.schema.get_field_index("title"), "title", titles)
    src = src.set_column(src.schema.get_field_index("fingerprint_json"), "fingerprint_json", fjson)
    # A key indexed twice would make the merge ambiguous; update those one by one.
    seen: dict[str, int] = {}
    for k in skeys:
        seen[k] = seen.get(k, 0) + 1
    dup = {k for k, n in seen.items() if n > 1}
    uniq = [i for i, k in enumerate(skeys) if k not in dup]
    if uniq:
        (table.merge_insert("paper_key").when_matched_update_all().execute(src.take(uniq)))
    for k in dup:
        table.update(where=f"paper_key = {_sql_quote(k)}",
                     values={"title": updates[k][0], "fingerprint_json": updates[k][1]})
    print(f"Updated {len(hit)} vector rows ({len(dup)} duplicated keys handled separately).")


if __name__ == "__main__":
    main()
