"""
Louvain community detection over the DepMap-enriched literature graph.

Reads ``data/depmap_edges.parquet`` (built by ``src.edge_index``), runs
Louvain on the weighted graph, post-processes oversized clusters with a
higher-resolution re-cluster, and writes ``data/clusters.json``.

The clustering uses ``networkx.algorithms.community.louvain_communities``
(stdlib NetworkX, no new dependency). Leiden via ``leidenalg`` would give
slightly better partitions and avoid the rare disconnected-community
pathology, but Louvain is sufficient for the first pass and lets us ship
without adding deps.

Cluster size cap
----------------
Any community with > ``MAX_CLUSTER_SIZE`` members is recursively
re-clustered at higher resolution (1.5x per recursion step, max 3 levels)
until all sub-clusters fit. Recursion stops early if a step doesn't make
progress (cluster is too dense to split further at any reasonable
resolution); those rare clusters retain their oversize.

Output schema
-------------
::

    {
      "schema_version": "1.0",
      "computed_at": "2026-05-03T...",
      "algorithm": "louvain",
      "resolution": 1.0,
      "weight_formula": "mentions * |depmap_r|, default_r=0.1",
      "filter": {"human_only": true, "min_mentions": 1, "min_abs_r": 0.15},
      "graph_size": {"nodes": 1150, "edges": 939},
      "max_cluster_size_cap": 50,
      "clusters": [
        {
          "id": 0,
          "size": 8,
          "members": ["YAP", "TAZ", "TEAD1", ...],         # node keys
          "member_symbols": ["YAP1", "WWTR1", "TEAD1", ...], # gene symbols
          "hub_member": "YAP",
          "internal_edges": 14,
          "external_edges": 6,
          "internal_weight": 145.2,
          "max_internal_r": 0.62,
          "top_dois": ["10.1016/...", ...]
        },
        ...
      ]
    }
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import networkx as nx
from loguru import logger
from networkx.algorithms.community import louvain_communities

from src.edge_index import (
    DEFAULT_PATH as DEFAULT_EDGES_PATH,
    DEFAULT_R, MIN_ABS_R, MIN_MENTIONS,
    load_edge_index,
)


_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CLUSTERS_PATH = _ROOT / "data" / "clusters.json"

MAX_CLUSTER_SIZE = 50
MAX_SPLIT_DEPTH = 3
RESOLUTION_STEP = 1.5


def _edges_are_stale(edges_path: Path, fingerprint_dir: Path) -> bool:
    """True if the edge index needs to be rebuilt — missing or older than any fingerprint."""
    if not edges_path.exists():
        return True
    edges_mtime = edges_path.stat().st_mtime
    for fp in fingerprint_dir.glob("*.json"):
        if fp.stat().st_mtime > edges_mtime:
            return True
    return False


def _build_nx_graph_from_index(table) -> nx.Graph:
    """Turn the edge-index pyarrow Table into a weighted NetworkX graph."""
    g = nx.Graph()
    src_col = table["source"].to_pylist()
    tgt_col = table["target"].to_pylist()
    w_col = table["weight"].to_pylist()
    sym_a = table["source_symbol"].to_pylist()
    sym_b = table["target_symbol"].to_pylist()
    r_col = table["depmap_r"].to_pylist()
    n_col = table["depmap_n"].to_pylist()
    m_col = table["mentions"].to_pylist()

    # Track per-node attributes (gene_symbol, max-mention) as we add edges.
    sym_seen: dict[str, str] = {}
    mention_seen: dict[str, int] = {}

    for i, (u, v, w) in enumerate(zip(src_col, tgt_col, w_col)):
        if w is None:
            continue
        g.add_edge(u, v,
                   weight=float(w),
                   mentions=int(m_col[i]),
                   depmap_r=r_col[i],
                   depmap_n=n_col[i])
        if sym_a[i] and u not in sym_seen:
            sym_seen[u] = sym_a[i]
        if sym_b[i] and v not in sym_seen:
            sym_seen[v] = sym_b[i]
        mention_seen[u] = mention_seen.get(u, 0) + int(m_col[i])
        mention_seen[v] = mention_seen.get(v, 0) + int(m_col[i])

    for n in g.nodes():
        g.nodes[n]["human_gene_symbol"] = sym_seen.get(n)
        g.nodes[n]["edge_mention_total"] = mention_seen.get(n, 0)
    return g


def _louvain(g: nx.Graph, resolution: float, seed: int) -> list[set]:
    """Single Louvain pass; communities returned as a list of node-key sets."""
    return louvain_communities(g, weight="weight", resolution=resolution, seed=seed)


def _split_oversized(
    community: set,
    g: nx.Graph,
    resolution: float,
    depth: int,
    seed: int,
) -> list[set]:
    """Recursively re-cluster oversized communities at higher resolution.

    Stops when (a) every sub-community is at or below ``MAX_CLUSTER_SIZE``,
    (b) recursion depth hits ``MAX_SPLIT_DEPTH``, or (c) a step doesn't
    actually split the input (degenerate dense subgraph).
    """
    if len(community) <= MAX_CLUSTER_SIZE or depth >= MAX_SPLIT_DEPTH:
        return [community]
    sub = g.subgraph(community).copy()
    if sub.number_of_edges() == 0:
        return [community]
    sub_communities = _louvain(sub, resolution=resolution, seed=seed)
    if len(sub_communities) <= 1:
        return [community]  # didn't split — give up
    out: list[set] = []
    for sub_c in sub_communities:
        out.extend(_split_oversized(sub_c, g, resolution * RESOLUTION_STEP, depth + 1, seed))
    return out


def _shape_cluster(
    cluster_id: int, members: set, g: nx.Graph, full_g: nx.Graph
) -> dict[str, Any]:
    """Build the JSON record for one cluster, with top-DOIs and edge stats."""
    member_list = sorted(members)
    member_symbols = sorted({
        g.nodes[m].get("human_gene_symbol") for m in member_list
        if g.nodes[m].get("human_gene_symbol")
    })
    # Hub member: highest edge_mention_total within the community.
    hub_member = max(member_list, key=lambda m: g.nodes[m].get("edge_mention_total", 0))

    internal_edges = 0
    external_edges = 0
    internal_weight = 0.0
    max_internal_r = 0.0
    doi_pool: dict[str, int] = {}

    member_set = set(member_list)
    for u, v, data in full_g.edges(data=True):
        if u in member_set and v in member_set:
            internal_edges += 1
            internal_weight += float(data.get("weight", 0.0))
            r = data.get("depmap_r")
            if r is not None and abs(float(r)) > max_internal_r:
                max_internal_r = abs(float(r))
        elif (u in member_set) ^ (v in member_set):
            external_edges += 1

    return {
        "id": cluster_id,
        "size": len(member_list),
        "members": member_list,
        "member_symbols": member_symbols,
        "hub_member": hub_member,
        "hub_member_symbol": g.nodes[hub_member].get("human_gene_symbol"),
        "internal_edges": internal_edges,
        "external_edges": external_edges,
        "internal_weight": round(internal_weight, 3),
        "max_internal_abs_r": round(max_internal_r, 4) if max_internal_r else None,
    }


def cluster_graph(
    edges_path: Path = DEFAULT_EDGES_PATH,
    output_path: Path = DEFAULT_CLUSTERS_PATH,
    *,
    resolution: float = 1.0,
    seed: int = 42,
) -> dict[str, Any]:
    """Run Louvain on the persisted edge index and write clusters.json.

    Returns the same payload that's written to disk.
    """
    table = load_edge_index(edges_path)
    g = _build_nx_graph_from_index(table)
    n_nodes = g.number_of_nodes()
    n_edges = g.number_of_edges()
    logger.info(f"Clustering graph: {n_nodes:,} nodes, {n_edges:,} edges")

    raw_communities = _louvain(g, resolution=resolution, seed=seed)
    logger.info(f"Initial Louvain: {len(raw_communities):,} communities")

    # Split oversized communities recursively.
    final_communities: list[set] = []
    for c in raw_communities:
        final_communities.extend(_split_oversized(c, g, resolution * RESOLUTION_STEP, 0, seed))

    # Sort by size descending so cluster_id ordering is stable + readable.
    final_communities.sort(key=lambda s: (-len(s), sorted(s)[0] if s else ""))

    clusters = [
        _shape_cluster(i, members, g, full_g=g)
        for i, members in enumerate(final_communities)
    ]

    over_cap = sum(1 for c in clusters if c["size"] > MAX_CLUSTER_SIZE)
    singletons = sum(1 for c in clusters if c["size"] == 1)
    logger.info(
        f"Final clusters: {len(clusters):,} "
        f"(largest={clusters[0]['size'] if clusters else 0}, "
        f"singletons={singletons}, "
        f"still-over-cap={over_cap})"
    )

    payload = {
        "schema_version": "1.0",
        "computed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "algorithm": "louvain",
        "resolution": resolution,
        "seed": seed,
        "weight_formula": (
            f"mentions * |depmap_r| if available else mentions * {DEFAULT_R}; "
            f"edges with available |r| < {MIN_ABS_R} excluded; "
            f"min_mentions = {MIN_MENTIONS}"
        ),
        "max_cluster_size_cap": MAX_CLUSTER_SIZE,
        "graph_size": {"nodes": n_nodes, "edges": n_edges},
        "cluster_count": len(clusters),
        "clusters": clusters,
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Wrote clusters -> {output_path}")
    return payload


# ---------------------------------------------------------------------------
# Lazy-loaded cluster registry for tools
# ---------------------------------------------------------------------------

_CLUSTERS_CACHE: dict[str, Any] | None = None
_PROTEIN_TO_CLUSTER: dict[str, int] | None = None
_SYMBOL_TO_CLUSTER: dict[str, int] | None = None
_CACHE_PATH: Path | None = None


def load_clusters(path: Path = DEFAULT_CLUSTERS_PATH) -> dict[str, Any]:
    """Lazy-load clusters.json into memory + build name-indexes for tool queries."""
    global _CLUSTERS_CACHE, _PROTEIN_TO_CLUSTER, _SYMBOL_TO_CLUSTER, _CACHE_PATH
    p = Path(path)
    if _CLUSTERS_CACHE is not None and _CACHE_PATH == p:
        return _CLUSTERS_CACHE
    if not p.exists():
        raise FileNotFoundError(
            f"clusters.json not found at {p}. Run "
            "`python scripts/cluster_corpus.py` to create it."
        )
    payload = json.loads(p.read_text(encoding="utf-8"))
    protein_to_cluster: dict[str, int] = {}
    symbol_to_cluster: dict[str, int] = {}
    for c in payload.get("clusters", []):
        cid = c["id"]
        for m in c.get("members") or []:
            protein_to_cluster[m] = cid
        for s in c.get("member_symbols") or []:
            symbol_to_cluster[s.upper()] = cid

    _CLUSTERS_CACHE = payload
    _PROTEIN_TO_CLUSTER = protein_to_cluster
    _SYMBOL_TO_CLUSTER = symbol_to_cluster
    _CACHE_PATH = p
    return payload


def get_cluster_indexes(path: Path = DEFAULT_CLUSTERS_PATH) -> tuple[dict, dict]:
    """Return (protein_to_cluster, symbol_to_cluster) — lazy-loaded together."""
    load_clusters(path)
    assert _PROTEIN_TO_CLUSTER is not None and _SYMBOL_TO_CLUSTER is not None
    return _PROTEIN_TO_CLUSTER, _SYMBOL_TO_CLUSTER


# ---------------------------------------------------------------------------
# Pipeline orchestrator — used by both the CLI and curate_papers post-hook
# ---------------------------------------------------------------------------


def build_and_cluster(
    fingerprint_dir: Path,
    *,
    edges_path: Path | None = None,
    clusters_path: Path | None = None,
    resolution: float = 1.0,
    seed: int = 42,
    force_rebuild_edges: bool = False,
) -> dict[str, Any]:
    """Refresh edge index + clusters for the corpus, with mtime-based staleness checks.

    Used by ``scripts/cluster_corpus.py`` and the ``curate_papers`` post-curation
    hook. Cheap when nothing changed (parquet up to date → just re-clusters
    from cached parquet, ~1 s); expensive when the corpus grew (rebuilds
    parquet, ~1–2 min depending on number of new pairs).

    Returns a dict with:
        edges_rebuilt: bool
        edges_stats: dict | None    — only present if edges were rebuilt
        cluster_count: int
        clusters_path: str
        edges_path: str
    """
    from src.edge_index import build_edge_index, DEFAULT_PATH as DEFAULT_EDGES_PATH

    edges_path = Path(edges_path) if edges_path else DEFAULT_EDGES_PATH
    clusters_path = Path(clusters_path) if clusters_path else DEFAULT_CLUSTERS_PATH
    fingerprint_dir = Path(fingerprint_dir)

    edges_stats: dict[str, Any] | None = None
    if force_rebuild_edges or _edges_are_stale(edges_path, fingerprint_dir):
        logger.info(
            f"Rebuilding edge index at {edges_path} "
            f"(force={force_rebuild_edges}, stale={_edges_are_stale(edges_path, fingerprint_dir)}) ..."
        )
        edges_stats = build_edge_index(fingerprint_dir, edges_path)

    payload = cluster_graph(
        edges_path=edges_path,
        output_path=clusters_path,
        resolution=resolution,
        seed=seed,
    )

    # Invalidate the in-process cluster cache so subsequent load_clusters()
    # in the same process picks up the freshly-written file.
    global _CLUSTERS_CACHE, _PROTEIN_TO_CLUSTER, _SYMBOL_TO_CLUSTER, _CACHE_PATH
    _CLUSTERS_CACHE = None
    _PROTEIN_TO_CLUSTER = None
    _SYMBOL_TO_CLUSTER = None
    _CACHE_PATH = None

    return {
        "edges_rebuilt": edges_stats is not None,
        "edges_stats": edges_stats,
        "cluster_count": payload["cluster_count"],
        "clusters_path": str(clusters_path),
        "edges_path": str(edges_path),
    }
