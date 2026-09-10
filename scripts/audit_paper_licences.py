#!/usr/bin/env python3
"""Resolve a per-paper licence for the corpus, and flag No-Derivatives papers.

`docs/licensing.md` states plainly that LPT "records no per-paper licence
field, so the shipped database **cannot** tell you which papers are in the OA
Subset", and that adding it "would have to be" done. This is that. It answers
one question:

    which curated papers carry a licence that forbids derivative works,
    and therefore may forbid shipping a fingerprint derived from them?

    .venv/bin/python scripts/audit_paper_licences.py            # scan + report
    .venv/bin/python scripts/audit_paper_licences.py --report    # re-report, no network
    .venv/bin/python scripts/audit_paper_licences.py --nd-only   # just the ND set

## Where the licence comes from

Europe PMC's REST `search` endpoint with `resultType=core`, which returns a
`license` string (`"cc by"`, `"cc by-nc-nd"`, …) plus `isOpenAccess`. It is
queried by PMCID where we have one and by DOI otherwise, batched
`_BATCH` identifiers per request — 40 IDs resolve in well under a second, so
the whole corpus is a few hundred requests rather than 14,517.

Europe PMC rather than PMC's own OA service (which `docs/licensing.md`
suggested) for two reasons: it accepts DOIs, so it covers the ~2% of curated
papers with no PMCID and the bioRxiv/medRxiv preprints, and one call returns
the licence for a batch instead of one article.

## What "unknown" means, and why it is not "fine"

A paper Europe PMC has no `license` for is **not** thereby permissive. It is
usually a publisher deposit that is free to read and not licensed for reuse —
i.e. all rights reserved. The report counts those separately from the
ND set and does not fold them into either "clear" or "blocked", because they
need a human decision, not a default.

## What it is for

A fingerprint is treated as a derivative work of its paper, so the published
archive carries one only where the paper's licence permits redistributing
derivatives. This script is what resolves that licence and writes it into
`papers.licence`, which is where `fetch_papers.py`, `curate_papers.py` and
`package_corpus.py` all read it from. See `docs/licensing.md`.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time
from collections import Counter

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.env_config import load_env  # noqa: E402
from src.paper_licence import (  # noqa: E402
    DERIVATIVES_OK, NO_DERIVATIVES, UNKNOWN, classify,
)

_DB = _ROOT / "data" / "literature.db"
_CACHE = _ROOT / "data" / "paper_licences.json"
_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
#: Europe PMC handles 40 OR-ed identifiers comfortably; larger batches start
#: tripping its query-length limits.
_BATCH = 40

def _rows(conn: sqlite3.Connection, everything: bool) -> list[dict]:
    where = "" if everything else "where curation_status='completed'"
    cur = conn.execute(
        f"select paper_key, doi, pmcid, title, journal, year, source, "
        f"fingerprint_path, curation_status from papers {where}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _query_batch(session, ids: list[tuple[str, str]], sleep: float) -> dict:
    """{identifier: europepmc record} for a batch of (kind, value) ids."""
    clause = " OR ".join(
        f'{"PMCID" if kind == "pmcid" else "DOI"}:"{val}"' for kind, val in ids)
    r = session.get(_SEARCH, params={"query": clause, "format": "json",
                                     "resultType": "core",
                                     "pageSize": len(ids) + 10}, timeout=60)
    r.raise_for_status()
    time.sleep(sleep)
    out: dict[str, dict] = {}
    for rec in (r.json().get("resultList", {}).get("result") or []):
        for key in (rec.get("pmcid"), (rec.get("doi") or "").lower()):
            if key:
                out[key] = rec
    return out


def scan(rows: list[dict], sleep: float, limit: int | None) -> dict[str, dict]:
    """Resolve licences, resuming from the cache so a re-run is nearly free."""
    import requests

    cache: dict[str, dict] = {}
    if _CACHE.is_file():
        cache = json.loads(_CACHE.read_text(encoding="utf-8"))
        print(f"  resuming: {len(cache):,} already resolved in {_CACHE.name}")

    todo = [r for r in rows if r["paper_key"] not in cache]
    if limit:
        todo = todo[:limit]
    print(f"  {len(todo):,} to resolve, {_BATCH} per request "
          f"(~{-(-len(todo) // _BATCH):,} requests)")

    session = requests.Session()
    session.headers["User-Agent"] = "little-protein-tiger licence audit"
    done = 0
    for start in range(0, len(todo), _BATCH):
        chunk = todo[start:start + _BATCH]
        ids = []
        for r in chunk:
            if r.get("pmcid") and str(r["pmcid"]).strip():
                ids.append(("pmcid", str(r["pmcid"]).strip()))
            elif r.get("doi") and str(r["doi"]).strip():
                ids.append(("doi", str(r["doi"]).strip()))
        try:
            found = _query_batch(session, ids, sleep)
        except Exception as exc:                              # noqa: BLE001
            # One bad batch must not lose the work already cached.
            print(f"    batch at {start} failed ({type(exc).__name__}: "
                  f"{str(exc)[:70]}) — leaving it unresolved")
            _CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
            continue
        for r in chunk:
            rec = (found.get(str(r.get("pmcid") or "").strip())
                   or found.get(str(r.get("doi") or "").lower().strip()))
            cache[r["paper_key"]] = {
                "doi": r.get("doi"), "pmcid": r.get("pmcid"),
                "license": (rec or {}).get("license"),
                "is_open_access": (rec or {}).get("isOpenAccess"),
                "resolved": rec is not None,
            }
        done += len(chunk)
        if start % (_BATCH * 25) == 0 or done == len(todo):
            _CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
            print(f"    {done:,}/{len(todo):,}")
    _CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return cache


def write_to_db(cache: dict[str, dict]) -> int:
    """Persist the resolved licence into `papers.licence`.

    The cache alone cannot gate anything — `fetch_papers.py` and
    `package_corpus.py` both need the value where a SQL query can see it, and
    a shipped database that carries its own licence provenance is auditable by
    whoever receives it.
    """
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    n = 0
    with sqlite3.connect(_DB) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
        if "licence" not in cols:
            raise SystemExit(
                "papers.licence does not exist — open the database through "
                "src.database.Database once so its migration runs")
        for key, info in cache.items():
            # "" is meaningful: checked, and the source records no licence.
            # NULL stays "never checked", so the two are distinguishable.
            conn.execute(
                "UPDATE papers SET licence=?, licence_source=?, "
                "licence_checked_at=? WHERE paper_key=?",
                (info.get("license") or "", "europepmc", now, key))
            n += 1
    return n


def report(rows: list[dict], cache: dict[str, dict], nd_only: bool) -> int:
    by_key = {r["paper_key"]: r for r in rows}
    verdicts = Counter()
    licences = Counter()
    nd: list[dict] = []
    unresolved: list[dict] = []

    for key, info in cache.items():
        if key not in by_key:
            continue
        v = classify(info.get("license"))
        verdicts[v] += 1
        licences[(info.get("license") or "—").lower()] += 1
        if v == NO_DERIVATIVES:
            nd.append({**by_key[key], **info})
        elif not info.get("resolved"):
            unresolved.append({**by_key[key], **info})

    total = sum(verdicts.values())
    if nd_only:
        for p in sorted(nd, key=lambda p: (p.get("journal") or "", p.get("year") or 0)):
            print(f"{p.get('license'):<12} {p.get('doi')}  {(p.get('title') or '')[:70]}")
        return 1 if nd else 0

    print(f"\nLicences for {total:,} papers "
          f"({len(rows) - total:,} not yet resolved)\n")
    for lic, n in licences.most_common():
        print(f"  {lic:<24}{n:>7,}   {n / max(total, 1) * 100:>5.1f}%")

    print(f"\n  {'derivatives permitted':<24}{verdicts[DERIVATIVES_OK]:>7,}")
    print(f"  {'NO DERIVATIVES':<24}{verdicts[NO_DERIVATIVES]:>7,}"
          f"   <-- excluded from the release archive")
    print(f"  {'unknown / unlicensed':<24}{verdicts[UNKNOWN]:>7,}"
          f"   <-- also excluded: absence of permission is not permission")
    if unresolved:
        print(f"  {'(no Europe PMC record)':<24}{len(unresolved):>7,}")

    if nd:
        print(f"\n{len(nd):,} No-Derivatives papers. First 10 by journal:")
        for p in sorted(nd, key=lambda p: (p.get("journal") or ""))[:10]:
            print(f"  {p.get('license'):<12}{(p.get('journal') or '?')[:34]:<36}"
                  f"{p.get('doi')}")
        out = _ROOT / "data" / "nd_papers.json"
        out.write_text(json.dumps(nd, indent=1), encoding="utf-8")
        print(f"\n  full list -> {out.relative_to(_ROOT)}")
        print("\n  Fingerprints of these papers are NOT published: a fingerprint\n"
              "  is treated as a derivative work, and the release carries one\n"
              "  only where the licence permits redistributing derivatives.\n"
              "  See docs/licensing.md.")
    return 1 if nd else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true",
                    help="re-report from the cache; make no network calls")
    ap.add_argument("--nd-only", action="store_true",
                    help="print only the No-Derivatives papers, one per line")
    ap.add_argument("--all", action="store_true",
                    help="every indexed paper, not just the curated ones")
    ap.add_argument("--limit", type=int, default=None,
                    help="resolve at most N new papers (for a quick sample)")
    ap.add_argument("--sleep", type=float, default=0.2,
                    help="seconds between requests (default 0.2)")
    args = ap.parse_args()

    load_env()
    if not _DB.is_file():
        raise SystemExit(f"{_DB} not found — run scripts/fetch_corpus.py first")

    with sqlite3.connect(f"file:{_DB}?mode=ro", uri=True) as conn:
        rows = _rows(conn, args.all)
    print(f"{len(rows):,} papers in scope "
          f"({'all indexed' if args.all else 'curated only'})")

    if args.report or args.nd_only:
        if not _CACHE.is_file():
            raise SystemExit(f"no cache at {_CACHE} — run without --report first")
        cache = json.loads(_CACHE.read_text(encoding="utf-8"))
    else:
        cache = scan(rows, args.sleep, args.limit)
        n = write_to_db(cache)
        print(f"  wrote papers.licence for {n:,} rows")

    return report(rows, cache, args.nd_only)


if __name__ == "__main__":
    raise SystemExit(main())
