#!/usr/bin/env python3
"""
Curate downloaded papers via Claude.

Usage:
    python scripts/curate_papers.py [--limit N] [--reprocess] [--dry-run] [--paper-key KEY]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests
import yaml
from loguru import logger

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.env_config import load_env  # noqa: E402
load_env(ROOT / ".env")

from src.database import Database, _paper_key
from src.models import Paper
from src.text_extractor import extract_text
from src.curator import curate_paper
from src.fingerprint_store import save_fingerprint

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"


def _rcsb_accessions_for_doi(doi: str) -> list[str]:
    """Query RCSB for PDB entries whose primary citation DOI matches.

    Authoritative for structures *deposited* by the paper.  Fast (~200ms).
    Returns empty list on any error so curation is never blocked.
    """
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
        return [hit["identifier"] for hit in r.json().get("result_set", [])]
    except Exception as exc:
        logger.warning(f"  RCSB DOI lookup failed for {doi}: {exc}")
        return []


def _merge_pdb_accessions(fingerprint: dict, doi: str | None) -> list[str]:
    """Merge curator-extracted accessions with RCSB-authoritative ones.

    Returns the merged list (may be empty).  Mutates fingerprint in place.
    """
    existing: list[str] = fingerprint.get("paper_metadata", {}).get("pdb_accessions") or []
    existing_upper = {a.upper() for a in existing}

    rcsb_ids: list[str] = []
    if doi:
        rcsb_ids = _rcsb_accessions_for_doi(doi)

    new_ids = [pid for pid in rcsb_ids if pid.upper() not in existing_upper]
    merged = existing + new_ids

    if merged != existing:
        fingerprint.setdefault("paper_metadata", {})["pdb_accessions"] = merged
        if new_ids:
            logger.info(f"  RCSB lookup added {len(new_ids)} accession(s): {new_ids}")

    return merged


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_paper_key(paper: Paper) -> str:
    return _paper_key(paper.doi, paper.pmcid, paper.pmid, paper.title)


def resolve_file_path(paper: Paper, config: dict) -> Path | None:
    """Return path to the downloaded file (PDF or XML), or None if missing.

    Tolerates Windows-style separators in the stored path so DBs written on
    Windows can be read on POSIX hosts after a machine migration.
    """
    if paper.pdf_path:
        raw = paper.pdf_path.replace("\\", "/")
        p = Path(raw)
        if p.exists():
            return p
        # Try resolving relative to project root (covers stored relative paths)
        p2 = ROOT / raw
        if p2.exists():
            return p2
        # Last resort: match by basename inside the configured pdf_dir
        pdf_dir = ROOT / config["paths"]["pdf_dir"]
        p3 = pdf_dir / Path(raw).name
        if p3.exists():
            return p3
    return None


def main():
    parser = argparse.ArgumentParser(description="Curate papers with Claude")
    parser.add_argument("--limit", type=int, default=0, help="Max papers to process (0 = all)")
    parser.add_argument("--reprocess", action="store_true", help="Reset and reprocess already-curated papers")
    parser.add_argument("--dry-run", action="store_true", help="List papers without calling Claude")
    parser.add_argument("--paper-key", dest="paper_key", default=None, help="Process a single paper by key")
    parser.add_argument("--provider", default=None, choices=["claude", "gemini", "local"],
                        help="Override curation provider from config (claude, gemini, or local)")
    parser.add_argument("--skip-normalize", action="store_true",
                        help="Skip the post-curation identifier-normalization backfill. "
                             "Use only for debugging — leaving it skipped means new "
                             "fingerprints lack the protein_identifiers block and graph / "
                             "DepMap tools won't see them.")
    parser.add_argument("--skip-graph-rebuild", action="store_true",
                        help="Skip the post-curation edge-index + cluster rebuild. "
                             "Graph and pairwise tools still see new fingerprints "
                             "via mtime-based cache invalidation, but the cluster "
                             "registry stays stale until rebuilt manually.")
    parser.add_argument("--skip-vector-ingest", action="store_true",
                        help="Skip embedding new fingerprints into the LanceDB "
                             "vector store. Semantic search via search_corpus "
                             "won't find new papers until ingest_vectors.py runs.")
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

        # Backfill canonical metadata from the DB record. The LLM is asked to
        # extract DOI / PMCID / title from the paper text and frequently fails
        # — older papers often don't carry the DOI in the parsable text. We
        # already have the canonical values in the Paper record; trust those
        # over the LLM extraction. Only fills in missing fields, never overwrites.
        fingerprint.setdefault("paper_metadata", {})
        pm = fingerprint["paper_metadata"]
        if paper.doi and not pm.get("doi"):
            pm["doi"] = paper.doi
        if paper.pmcid and not pm.get("pmcid"):
            pm["pmcid"] = paper.pmcid
        if paper.title and not pm.get("title"):
            pm["title"] = paper.title

        # Merge RCSB-authoritative PDB accessions with whatever the curator extracted.
        # This catches structures deposited by the paper that the model missed.
        _merge_pdb_accessions(fingerprint, paper.doi)

        # Persist fingerprint
        fp_path = save_fingerprint(paper_key, fingerprint, fingerprint_dir)
        cm = fingerprint.get("curation_metadata", {})
        tokens = (cm.get("input_tokens", 0) or 0) + (cm.get("output_tokens", 0) or 0)
        model = cm.get("model", curation_cfg.get("model", "unknown"))

        # Store as project-relative POSIX path so the DB remains portable
        # across OSes (Windows write → Linux read on machine migration).
        try:
            fp_rel = fp_path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            fp_rel = fp_path.as_posix()
        db.mark_curated(paper_key, fp_rel, model, tokens)
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

    # ----- Post-curation pipeline -----
    # Three independent stages run after a successful curation batch:
    #
    #   1. Identifier normalization (run_backfill)        — sprint 2
    #   2. Edge index + cluster rebuild (build_and_cluster) — sprint 5
    #   3. Vector ingestion (run_ingest)                   — semantic search
    #
    # Each stage skips when:
    #   - --dry-run was used (no fingerprints written)
    #   - stats["curated"] == 0 (everything failed/skipped)
    #   - the corresponding --skip-* flag is set
    #
    # All stages are idempotent. The cost ordering is:
    #   normalize: ~2 s, graph rebuild: 30 s – 2 min, vector ingest: scales
    #   with N new papers (~0.2 s each after model load).
    no_changes = args.dry_run or stats["curated"] == 0
    if args.dry_run:
        logger.info("Dry run: skipping post-curation hooks (no fingerprints written).")
    elif stats["curated"] == 0:
        logger.info("No new fingerprints written; skipping post-curation hooks.")

    # Stage 1: identifier normalization
    if not no_changes:
        if args.skip_normalize:
            logger.info("Skipping identifier normalization (--skip-normalize set).")
        else:
            logger.info(f"Normalising identifiers on {stats['curated']:,} new fingerprint(s)...")
            from scripts.normalize_identifiers import run_backfill
            result = run_backfill(fingerprint_dir, log_full_summary=False)
            fs = result["file_stats"]
            print(
                f"  Normalised: {fs.get('updated', 0):,} updated, "
                f"{fs.get('already_current', 0):,} already current"
            )

    # Stage 2: edge index + clustering
    if not no_changes:
        if args.skip_graph_rebuild:
            logger.info("Skipping graph rebuild (--skip-graph-rebuild set).")
        else:
            logger.info("Rebuilding edge index + clustering ...")
            from src.clustering import build_and_cluster
            result = build_and_cluster(fingerprint_dir)
            print(
                f"  Graph: edges_rebuilt={result['edges_rebuilt']}, "
                f"clusters={result['cluster_count']:,}"
            )

    # Stage 3: vector ingestion (LanceDB)
    if not no_changes:
        if args.skip_vector_ingest:
            logger.info("Skipping vector ingestion (--skip-vector-ingest set).")
        else:
            logger.info("Embedding new fingerprints into vector store ...")
            from scripts.ingest_vectors import run_ingest
            vs_cfg = config.get("vector_store", {})
            db_path = ROOT / vs_cfg.get("db_path", "data/vectors")
            embedding_model = vs_cfg.get("embedding_model", "NeuML/pubmedbert-base-embeddings")
            ingest = run_ingest(
                fingerprint_dir=fingerprint_dir,
                db_path=db_path,
                embedding_model=embedding_model,
                rebuild=False,
            )
            print(f"  Vectors: {ingest['new_records']:,} new records embedded")


if __name__ == "__main__":
    main()
