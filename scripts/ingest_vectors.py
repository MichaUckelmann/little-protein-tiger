#!/usr/bin/env python3
"""
Embed fingerprint JSONs into LanceDB for semantic search.

Usage:
    python scripts/ingest_vectors.py [--config CONFIG] [--rebuild]

Options:
    --config CONFIG   Path to config.yaml (default: repo root config.yaml)
    --rebuild         Drop and recreate the vector table (re-embed everything)
"""
import argparse
import sys
from pathlib import Path

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

    logger.info(f"Fingerprint dir : {fingerprint_dir}")
    logger.info(f"Vector DB path  : {db_path}")
    logger.info(f"Embedding model : {embedding_model}")
    if args.rebuild:
        logger.warning("--rebuild flag set: existing index will be dropped.")

    store = VectorStore(db_path=db_path, embedding_model=embedding_model)
    count = store.ingest(fingerprint_dir=fingerprint_dir, rebuild=args.rebuild)

    print(f"\n--- Ingestion summary ---")
    print(f"  New records ingested : {count}")
    print(f"  Vector DB            : {db_path}")


if __name__ == "__main__":
    main()
