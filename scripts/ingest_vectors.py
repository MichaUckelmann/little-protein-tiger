#!/usr/bin/env python3
"""
Embed fingerprint JSONs into LanceDB for semantic search.

Idempotent: ``VectorStore.ingest`` only embeds fingerprints whose
paper_key is not already in the table. Pass ``--rebuild`` to drop the
table and re-embed everything.

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
from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

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

    Returns:
        ``{"new_records": int, "db_path": str, "embedding_model": str}``
    """
    logger.info(f"Vector ingest: fingerprints={fingerprint_dir} db={db_path} rebuild={rebuild}")
    store = VectorStore(db_path=db_path, embedding_model=embedding_model)
    count = store.ingest(fingerprint_dir=fingerprint_dir, rebuild=rebuild)
    return {
        "new_records": int(count),
        "db_path": str(db_path),
        "embedding_model": embedding_model,
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
    )

    print("\n--- Ingestion summary ---")
    print(f"  New records ingested : {result['new_records']}")
    print(f"  Vector DB            : {result['db_path']}")


if __name__ == "__main__":
    main()
