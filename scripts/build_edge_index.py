#!/usr/bin/env python3
"""
Build the DepMap-enriched edge index for the literature graph.

Walks every edge in the cached literature graph, computes DepMap CRISPR
co-essentiality (Pearson r over Chronos scores) using the same family-aware
resolver that ``get_genetic_codependency`` uses, and persists the result to
``data/depmap_edges.parquet``. Sprint-5 clustering reads this file rather
than recomputing every time.

The first run takes ~1–2 minutes (DepMap CSV load + ~10k pairwise
correlations); subsequent runs are seconds because the graph cache is warm.

Usage
-----
    python scripts/build_edge_index.py
    python scripts/build_edge_index.py --include-non-human
    python scripts/build_edge_index.py --output data/some_other.parquet
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml
from loguru import logger

from src.edge_index import build_edge_index, DEFAULT_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", type=Path, default=DEFAULT_PATH,
                        help=f"Output parquet path. Default {DEFAULT_PATH.relative_to(_ROOT)}.")
    parser.add_argument("--include-non-human", action="store_true",
                        help="Include edges with non-human-resolvable endpoints. "
                             "By default only edges where both endpoints carry a "
                             "human_gene_symbol are kept (clustering's natural input).")
    parser.add_argument("--min-n", type=int, default=100,
                        help="Minimum DepMap cell-line overlap. Default 100.")
    args = parser.parse_args()

    cfg = yaml.safe_load((_ROOT / args.config).read_text(encoding="utf-8"))
    fingerprint_dir = (_ROOT / cfg["paths"]["fingerprint_dir"]).resolve()
    if not fingerprint_dir.exists():
        sys.exit(f"ERROR: fingerprint dir not found: {fingerprint_dir}")

    output = args.output
    if not output.is_absolute():
        output = _ROOT / output

    stats = build_edge_index(
        fingerprint_dir=fingerprint_dir,
        output_path=output,
        human_only=not args.include_non_human,
        min_n=args.min_n,
    )

    logger.info("--- summary ---")
    for k, v in stats.items():
        logger.info(f"  {k}: {v}")


if __name__ == "__main__":
    main()
