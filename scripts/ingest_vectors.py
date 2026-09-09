#!/usr/bin/env python3
"""
Embed fingerprint JSONs into LanceDB for semantic search.

Idempotent: ``VectorStore.ingest`` embeds fingerprints that are not yet
indexed, and RE-embeds any whose text has changed since they were (a
re-curated or re-normalised fingerprint). Unchanged ones are skipped.
Pass ``--rebuild`` to drop the table and re-embed everything, or
``--prune-orphans`` to also delete indexed rows whose fingerprint file
no longer exists.

Also imported by ``scripts/curate_papers.py`` so newly-curated
fingerprints get embedded automatically — see ``run_ingest()``.

Usage
-----
    python scripts/ingest_vectors.py [--config CONFIG] [--rebuild]
"""
import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.env_config import load_env  # noqa: E402
load_env(ROOT / ".env")

from src.vector_store import VectorStore


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_ingest(
    fingerprint_dir: Path,
    db_path: Path,
    *,
    embedding_model: str = "NeuML/pubmedbert-base-embeddings",
    rebuild: bool = False,
    prune_orphans: bool = False,
) -> dict[str, Any]:
    """Embed any new fingerprints into the vector store; return a stats dict.

    Importable from ``scripts/curate_papers.py`` — same pattern as
    ``run_backfill`` for identifier normalisation. The
    sentence-transformer model loads once (~5 s) per call; embedding
    each new fingerprint is ~100–300 ms depending on text length.

    Args:
        fingerprint_dir: Directory of fingerprint JSONs.
        db_path:         LanceDB directory path.
        embedding_model: HuggingFace model name (default PubMedBERT).
        rebuild:         Drop the table before ingesting (re-embeds all).
        prune_orphans:   Delete indexed rows with no fingerprint file left.

    Returns:
        ``{"new_records", "db_path", "embedding_model"}`` plus the ingest
        breakdown (``added``, ``updated``, ``unchanged``, ``pruned``,
        ``orphans_seen``, ...).
    """
    logger.info(f"Vector ingest: fingerprints={fingerprint_dir} db={db_path} rebuild={rebuild}")
    store = VectorStore(db_path=db_path, embedding_model=embedding_model)
    count = store.ingest(fingerprint_dir=fingerprint_dir, rebuild=rebuild,
                         prune_orphans=prune_orphans)
    return {
        "new_records": int(count),
        "db_path": str(db_path),
        "embedding_model": embedding_model,
        **getattr(store, "last_ingest_stats", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest fingerprint JSONs into LanceDB vector store."
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.yaml"),
        help="Path to config.yaml (default: repo root config.yaml)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop and recreate the vector table (re-embed everything).",
    )
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help=("Delete indexed rows whose fingerprint file no longer exists. "
              "Destructive, so opt-in; without it those rows keep matching "
              "search_corpus while get_fingerprint fails on them."),
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))

    fingerprint_dir = ROOT / config["paths"]["fingerprint_dir"]
    vs_cfg = config.get("vector_store", {})
    db_path = ROOT / vs_cfg.get("db_path", "data/vectors")
    embedding_model = vs_cfg.get("embedding_model", "NeuML/pubmedbert-base-embeddings")

    if args.rebuild:
        logger.warning("--rebuild flag set: existing index will be dropped.")

    result = run_ingest(
        fingerprint_dir=fingerprint_dir,
        db_path=db_path,
        embedding_model=embedding_model,
        rebuild=args.rebuild,
        prune_orphans=args.prune_orphans,
    )

    print("\n--- Ingestion summary ---")
    print(f"  Rows written         : {result['new_records']}")
    print(f"    new                : {result.get('added', 0)}")
    print(f"    re-embedded        : {result.get('updated', 0)}")
    print(f"  Unchanged (skipped)  : {result.get('unchanged', 0)}")
    print(f"  Orphaned rows        : {result.get('orphans_seen', 0)}"
          f" ({result.get('pruned', 0)} pruned)")
    print(f"  Vector DB            : {result['db_path']}")


if __name__ == "__main__":
    main()
