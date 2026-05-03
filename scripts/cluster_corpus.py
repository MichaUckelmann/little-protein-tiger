#!/usr/bin/env python3
"""
Run Louvain community detection on the DepMap-enriched literature graph.

Pipeline: literature graph -> ``scripts/build_edge_index.py`` ->
``data/depmap_edges.parquet`` -> THIS SCRIPT -> ``data/clusters.json``.

By default, builds the edge index first if it's missing or stale (older
than the most-recently-modified fingerprint). Pass ``--rebuild-edges`` to
force a rebuild.

Usage
-----
    python scripts/cluster_corpus.py
    python scripts/cluster_corpus.py --resolution 1.5
    python scripts/cluster_corpus.py --rebuild-edges
    python scripts/cluster_corpus.py --output data/clusters_high_res.json --resolution 2.0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml
from loguru import logger

from src.clustering import (
    DEFAULT_CLUSTERS_PATH, build_and_cluster, cluster_graph,
)
from src.edge_index import DEFAULT_PATH as DEFAULT_EDGES_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--resolution", type=float, default=1.0,
                        help="Louvain resolution. Higher = smaller, more clusters. Default 1.0.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for Louvain reproducibility.")
    parser.add_argument("--edges", type=Path, default=DEFAULT_EDGES_PATH,
                        help=f"Edge index parquet. Default {DEFAULT_EDGES_PATH.relative_to(_ROOT)}.")
    parser.add_argument("--output", type=Path, default=DEFAULT_CLUSTERS_PATH,
                        help=f"Output clusters.json. Default {DEFAULT_CLUSTERS_PATH.relative_to(_ROOT)}.")
    parser.add_argument("--rebuild-edges", action="store_true",
                        help="Force a rebuild of the edge index even if it exists.")
    parser.add_argument("--skip-edge-rebuild", action="store_true",
                        help="Use the existing edge index even if newer fingerprints "
                             "exist. Useful for fast iteration on the cluster algorithm.")
    args = parser.parse_args()

    cfg = yaml.safe_load((_ROOT / args.config).read_text(encoding="utf-8"))
    fingerprint_dir = (_ROOT / cfg["paths"]["fingerprint_dir"]).resolve()

    edges_path = args.edges if args.edges.is_absolute() else _ROOT / args.edges
    output_path = args.output if args.output.is_absolute() else _ROOT / args.output

    # --skip-edge-rebuild bypasses the staleness check by re-clustering from
    # whatever parquet exists (errors if missing). Otherwise hand off to the
    # shared orchestrator that handles the rebuild-when-stale logic.
    if args.skip_edge_rebuild:
        logger.info(f"Using existing edge index: {edges_path} (--skip-edge-rebuild)")
        payload = cluster_graph(
            edges_path=edges_path,
            output_path=output_path,
            resolution=args.resolution,
            seed=args.seed,
        )
    else:
        result = build_and_cluster(
            fingerprint_dir,
            edges_path=edges_path,
            clusters_path=output_path,
            resolution=args.resolution,
            seed=args.seed,
            force_rebuild_edges=args.rebuild_edges,
        )
        # Re-load the payload for summary logging — build_and_cluster returns
        # a thin status dict, not the full payload.
        import json as _json
        payload = _json.loads(output_path.read_text(encoding="utf-8"))

    logger.info("--- summary ---")
    logger.info(f"  total clusters: {payload['cluster_count']:,}")
    sizes = sorted([c["size"] for c in payload["clusters"]], reverse=True)
    if sizes:
        logger.info(f"  size distribution: max={sizes[0]} median={sizes[len(sizes)//2]} singletons={sum(1 for s in sizes if s == 1)}")
        logger.info(f"  top 5 cluster sizes: {sizes[:5]}")
    logger.info(f"  written to: {output_path}")


if __name__ == "__main__":
    main()
