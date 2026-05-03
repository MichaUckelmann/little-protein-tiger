"""
Persisted DepMap-enriched edge index for the literature interaction graph.

The literature graph (``src._corpus_graph._build_graph``) is rebuilt from
fingerprint JSONs on demand; recomputing DepMap Pearson r for every edge
on every cluster run is wasteful (~30 s for ~10k edges). This module
materialises a parquet sidecar so clustering and other batch analyses
can iterate cheaply.

Schema
------
``data/depmap_edges.parquet`` — one row per edge, with columns:

    source           str   graph node key (normalised name)
    target           str   graph node key
    source_symbol    str?  resolved human gene symbol, or null
    target_symbol    str?  resolved human gene symbol, or null
    mentions         int   literature-graph edge weight (paper count)
    depmap_r         f64?  best-pair Pearson r from DepMap, or null
    depmap_n         i32?  cell-line overlap for the chosen pair, or null
    weight           f64   clustering weight; see ``edge_weight()``

Edge inclusion + weight formula
--------------------------------
A literature edge contributes to clustering iff:

    mentions   >= MIN_MENTIONS          (default 1)
    AND  ( depmap_r is None             # literature-only weak signal
           OR  abs(depmap_r) >= MIN_ABS_R  # DepMap-supported edge
         )

The weight is:

    weight = mentions * (abs(depmap_r) if depmap_r else DEFAULT_R)

where ``DEFAULT_R = 0.1`` keeps DepMap-unavailable edges in the graph
as weak community signals. DepMap-AVAILABLE-but-weak edges (|r| < 0.15)
are dropped — DepMap actively says these genes are uncorrelated, which
is informative.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger


_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = _ROOT / "data" / "depmap_edges.parquet"

# Edge inclusion / weighting parameters. See module docstring.
MIN_MENTIONS = 1
MIN_ABS_R = 0.15
DEFAULT_R = 0.1


def edge_weight(mentions: int, depmap_r: float | None) -> float | None:
    """Compute the clustering weight for one edge, or ``None`` to exclude it.

    Rules:
      - mentions < MIN_MENTIONS: exclude
      - DepMap r unavailable: weight = mentions * DEFAULT_R (weak literature)
      - DepMap r available but |r| < MIN_ABS_R: exclude (DepMap rejects it)
      - DepMap r available and |r| >= MIN_ABS_R: weight = mentions * |r|
    """
    if mentions < MIN_MENTIONS:
        return None
    if depmap_r is None:
        return float(mentions * DEFAULT_R)
    if abs(depmap_r) < MIN_ABS_R:
        return None
    return float(mentions * abs(depmap_r))


def _node_gene_candidates(node_attrs: dict, normalizer: Any) -> list[str]:
    """Reverse-lookup all human gene symbols for a graph node's UniProt candidates.

    Each node carries ``candidate_uniprots`` (a list of UniProt accessions
    aggregated across fingerprints). Gene symbols aren't stored directly,
    so we walk HGNC's symbol→uniprot index inverse to recover them. For a
    family-head node like 'TEAD' this yields [TEAD1, TEAD2, TEAD3, TEAD4].

    Falls back to ``human_gene_symbol`` (the dominant single one) when the
    inverse lookup yields nothing.
    """
    cands: list[str] = []
    candidate_ups = node_attrs.get("candidate_uniprots") or []
    if candidate_ups:
        ups_set = set(candidate_ups)
        for sym, ups in normalizer._hgnc_symbol_to_uniprot.items():
            if any(u in ups for u in ups_set):
                cands.append(sym)
    if not cands and node_attrs.get("human_gene_symbol"):
        cands = [node_attrs["human_gene_symbol"]]
    return cands


def build_edge_index(
    fingerprint_dir: Path,
    output_path: Path = DEFAULT_PATH,
    *,
    human_only: bool = True,
    min_n: int = 100,
) -> dict[str, Any]:
    """Walk the literature graph, compute DepMap r per edge, persist to parquet.

    Edges are **collapsed by gene symbol** before persisting — the graph
    can carry separate nodes for ``KRAS`` and ``K-RAS`` (the old graph
    normaliser preserves letter-letter hyphens), but for clustering they
    should merge into one super-node. Mentions across collapsed edges are
    summed; DepMap r is computed once per unique gene-pair.

    Args:
        fingerprint_dir: Where fingerprint JSONs live (used to build/cache graph).
        output_path:     Where to write the parquet (default ``data/depmap_edges.parquet``).
        human_only:      Restrict to edges where both endpoints resolved to a
                         human gene symbol (matches clustering's needs).
        min_n:           Minimum DepMap cell-line overlap. Below this, the
                         pair's r is treated as unavailable (default_r path).

    Returns:
        Stats dict: ``edges_total``, ``node_edges_collapsed``,
        ``unique_gene_pairs``, ``edges_with_depmap``, ``edges_kept``,
        ``edges_excluded``, ``output_path``.
    """
    from src._corpus_graph import _get_graph
    from src.depmap import correlation_for_pair_family
    from src.identifier_normalizer import get_normalizer

    g = _get_graph(Path(fingerprint_dir))
    normalizer = get_normalizer()

    logger.info(
        f"Building edge index over {g.number_of_edges():,} graph edges "
        f"(human_only={human_only}, min_n={min_n}) — collapsing by gene_symbol ..."
    )

    # Pre-compute gene candidates per node once — same node hit by many edges.
    candidates: dict[str, list[str]] = {}
    for n, d in g.nodes(data=True):
        candidates[n] = _node_gene_candidates(d, normalizer)

    # First pass: collapse node-level edges to gene-symbol pairs.
    # Each unique (sym_a, sym_b) pair aggregates mentions across all
    # contributing node-level edges. Use canonical ordering so (KRAS, BRAF)
    # and (BRAF, KRAS) merge.
    pair_data: dict[tuple[str, str], dict[str, Any]] = {}
    edges_total = g.number_of_edges()
    skipped_no_symbol = 0
    progress_bucket = max(1, edges_total // 20)

    for i, (u, v, data) in enumerate(g.edges(data=True)):
        if i and i % progress_bucket == 0:
            logger.info(f"  {i:,} / {edges_total:,} graph edges processed")

        s_sym = g.nodes[u].get("human_gene_symbol")
        t_sym = g.nodes[v].get("human_gene_symbol")

        if human_only and (not s_sym or not t_sym):
            skipped_no_symbol += 1
            continue
        if not s_sym or not t_sym:
            # Without human_only we still need a label; fall back to node key.
            s_sym = s_sym or u
            t_sym = t_sym or v

        # Canonical ordering for the merge key.
        if s_sym <= t_sym:
            key = (s_sym, t_sym)
            ca = candidates[u]
            cb = candidates[v]
        else:
            key = (t_sym, s_sym)
            ca = candidates[v]
            cb = candidates[u]

        bucket = pair_data.get(key)
        if bucket is None:
            pair_data[key] = {
                "mentions": int(data["mentions"]),
                "candidates_a": list(ca),
                "candidates_b": list(cb),
            }
        else:
            bucket["mentions"] += int(data["mentions"])
            # Union candidate lists (rare — only relevant if multiple node-pairs
            # resolved to the same gene-pair via different family expansions).
            for c in ca:
                if c not in bucket["candidates_a"]:
                    bucket["candidates_a"].append(c)
            for c in cb:
                if c not in bucket["candidates_b"]:
                    bucket["candidates_b"].append(c)

    logger.info(
        f"Collapsed {edges_total - skipped_no_symbol:,} node-edges into "
        f"{len(pair_data):,} unique gene-symbol pairs ({skipped_no_symbol:,} skipped)."
    )

    # Second pass: compute DepMap r per unique pair, apply weight formula,
    # build output rows.
    rows: list[dict[str, Any]] = []
    stats = {
        "edges_total": edges_total,
        "node_edges_collapsed": edges_total - skipped_no_symbol,
        "unique_gene_pairs": len(pair_data),
        "edges_with_depmap": 0,
        "edges_kept": 0,
        "edges_excluded": 0,
    }

    progress_bucket = max(1, len(pair_data) // 20)
    for i, ((sym_a, sym_b), bucket) in enumerate(pair_data.items()):
        if i and i % progress_bucket == 0:
            logger.info(f"  {i:,} / {len(pair_data):,} unique pairs DepMap-queried")

        depmap_r: float | None = None
        depmap_n: int | None = None
        ca = bucket["candidates_a"]
        cb = bucket["candidates_b"]
        if ca and cb:
            cor = correlation_for_pair_family(ca, cb, min_n=min_n)
            if cor.get("available"):
                depmap_r = float(cor["r"])
                depmap_n = int(cor["n"])
                stats["edges_with_depmap"] += 1

        mentions = bucket["mentions"]
        weight = edge_weight(mentions, depmap_r)
        if weight is None:
            stats["edges_excluded"] += 1
            continue

        rows.append({
            "source": sym_a,
            "target": sym_b,
            "source_symbol": sym_a,
            "target_symbol": sym_b,
            "mentions": mentions,
            "depmap_r": depmap_r,
            "depmap_n": depmap_n,
            "weight": weight,
        })
        stats["edges_kept"] += 1

    # Build pyarrow Table column-by-column to keep nullable typing.
    table = pa.table({
        "source":         pa.array([r["source"] for r in rows], type=pa.string()),
        "target":         pa.array([r["target"] for r in rows], type=pa.string()),
        "source_symbol":  pa.array([r["source_symbol"] for r in rows], type=pa.string()),
        "target_symbol":  pa.array([r["target_symbol"] for r in rows], type=pa.string()),
        "mentions":       pa.array([r["mentions"] for r in rows], type=pa.int32()),
        "depmap_r":       pa.array([r["depmap_r"] for r in rows], type=pa.float64()),
        "depmap_n":       pa.array([r["depmap_n"] for r in rows], type=pa.int32()),
        "weight":         pa.array([r["weight"] for r in rows], type=pa.float64()),
    })
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, output_path)

    stats["output_path"] = str(output_path)
    logger.info(
        f"Edge index written: {stats['edges_kept']:,} edges "
        f"({stats['edges_with_depmap']:,} with DepMap, "
        f"{stats['edges_excluded']:,} excluded) -> {output_path}"
    )
    return stats


def load_edge_index(path: Path = DEFAULT_PATH) -> pa.Table:
    """Read the parquet edge index back into a pyarrow Table."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Edge index not found at {p}. Run "
            "`python scripts/build_edge_index.py` to create it."
        )
    return pq.read_table(p)
