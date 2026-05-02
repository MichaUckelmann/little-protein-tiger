"""
Corpus-wide graph and quantitative-evidence helpers over fingerprint JSONs.

Used by both transports — `src/mcp_server.py` exposes them as MCP tools,
`src/skill_runner.py` dispatches them in-process. Single source of truth
to avoid drift.

Two retrieval styles co-exist:
- Direct file scan (`get_interactions_for`, `find_quantitative_evidence`):
  re-reads all fingerprints on every call (~1 s for 5k files). Used for
  partner lookup and quantitative-evidence extraction.
- Cached NetworkX graph (`shortest_interaction_path`, `interaction_hubs`,
  `export_subgraph`): built once on first access from the same fingerprint
  set. Rebuild requires MCP server restart.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import networkx as nx


# ---------------------------------------------------------------------------
# Protein-name normalisation and matching
# ---------------------------------------------------------------------------
#
# Real fingerprint data has inconsistent protein naming:
#   YAP / YAP1 / hYAP / h-YAP / HYAP1
#   SREBP-1 / SREBP1 / SREBP-1c
#   TEAD1 / TEAD2 / TEAD3 / TEAD4  (paralogs — should NOT be collapsed)
#
# Strategy: light normalisation (uppercase, strip species prefix, drop hyphens
# between letters and digits) followed by substring-or-prefix match. This is
# the same pattern used by _find_pdb_structures in skill_runner.py.

_SPECIES_PREFIX = re.compile(r"^(?:H|M|R|HSA|MMU|RNO)-(?=[A-Z0-9])", re.IGNORECASE)

# Placeholder strings curators sometimes use when no specific protein is named.
# These get treated as empty so they don't inflate hubs or partner counts.
_PLACEHOLDERS = frozenset({
    "", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "UNSPECIFIED",
    "?", "-", "--", "TBD", "NOTAPPLICABLE",
})


def _normalize_protein(name: str) -> str:
    if not name:
        return ""
    s = name.strip().upper()
    s = _SPECIES_PREFIX.sub("", s)
    # Collapse "SREBP-1" → "SREBP1", but leave "ABC-DEF" alone (letters on both sides).
    s = re.sub(r"([A-Z])-(\d)", r"\1\2", s)
    s = re.sub(r"(\d)-([A-Z])", r"\1\2", s)
    s = s.replace(" ", "")
    if s in _PLACEHOLDERS:
        return ""
    return s


def _protein_matches(stored: str, query: str) -> bool:
    """Match a stored fingerprint protein name against a user query.

    Returns True when the normalised query is a substring of the normalised
    stored name, or vice-versa for short queries (e.g. "YAP" should hit
    "YAP1"). Queries shorter than 2 chars are rejected.
    """
    s = _normalize_protein(stored)
    q = _normalize_protein(query)
    if len(q) < 2 or not s:
        return False
    return q in s or s in q


# ---------------------------------------------------------------------------
# Tool 1: corpus-wide partner lookup
# ---------------------------------------------------------------------------


def get_interactions_for(
    protein: str,
    fingerprint_dir: Path,
    depth: int = 1,
    min_mentions: int = 1,
    max_partners: int = 50,
) -> dict[str, Any]:
    """Aggregate interaction partners for ``protein`` across the corpus.

    Walks ``key_findings[].protein_pair`` in every fingerprint. For each
    matching finding, the *other* member of the pair becomes a partner and
    is aggregated with mention count, supporting DOIs, and any non-null
    Kd/Ki values from the same finding.
    """
    fingerprint_dir = Path(fingerprint_dir)
    if not fingerprint_dir.exists():
        return {
            "query_protein": protein,
            "depth": depth,
            "corpus_size": 0,
            "direct_partners": [],
            "second_degree_partners": [],
            "caveats": [f"Fingerprint directory not found: {fingerprint_dir}"],
        }

    files = list(fingerprint_dir.glob("*.json"))

    direct = _collect_partners(files, [protein])
    direct_partners = _shape_partners(direct, min_mentions, max_partners)

    second_degree: list[dict] = []
    if depth >= 2 and direct_partners:
        # Use the canonical partner names that came out of direct match
        # (the names as they appear in fingerprints, not the user query).
        seed_names = [p["partner"] for p in direct_partners]
        # Exclude the original query protein from second-degree results.
        excluded = {_normalize_protein(protein)} | {_normalize_protein(n) for n in seed_names}
        second_raw = _collect_partners(files, seed_names)
        # Filter: drop partners that are the original query or any first-degree partner.
        second_raw = {
            k: v for k, v in second_raw.items() if _normalize_protein(k) not in excluded
        }
        second_degree = _shape_partners(second_raw, min_mentions, max_partners)

    return {
        "query_protein": protein,
        "depth": depth,
        "corpus_size": len(files),
        "direct_partners": direct_partners,
        "second_degree_partners": second_degree if depth >= 2 else [],
        "caveats": [
            "Matching uses light alias normalisation (case-insensitive, species "
            "prefix stripped, internal hyphens dropped) plus substring/prefix "
            "containment — paralogs like TEAD1/TEAD2/TEAD3/TEAD4 are kept distinct, "
            "but a query of 'TEAD' will match all of them.",
            "Corpus is biochemistry-biased; in vivo and clinical interaction "
            "partners may be undercounted. Absence of a partner here is not "
            "evidence that the interaction does not exist in the wider literature.",
        ],
    }


def _collect_partners(
    files: list[Path], queries: list[str]
) -> dict[str, dict[str, Any]]:
    """Walk every fingerprint, accumulate partners keyed by partner name.

    For each ``key_findings`` entry whose ``protein_pair`` contains any of
    ``queries``, the other element becomes a partner. Multiple-query mode is
    used for depth-2 expansion.
    """
    partners: dict[str, dict[str, Any]] = {}
    # Skip self-matches: e.g. protein_pair=["KRAS-G12C","KRAS"] would otherwise
    # report "KRAS" as a partner of "KRAS".
    query_keys = {_normalize_protein(q) for q in queries}

    for fp_file in files:
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        doi = (fp.get("paper_metadata") or {}).get("doi")
        if not doi:
            continue

        for finding in fp.get("key_findings") or []:
            pair = finding.get("protein_pair") or []
            pair = [p for p in pair if p]
            if len(pair) != 2:
                continue

            # Determine which side (if any) matches our queries; the other side is the partner.
            partner_name: str | None = None
            for q in queries:
                if _protein_matches(pair[0], q):
                    partner_name = pair[1]
                    break
                if _protein_matches(pair[1], q):
                    partner_name = pair[0]
                    break
            if partner_name is None:
                continue

            partner_key = _normalize_protein(partner_name)
            if partner_key in query_keys:
                continue  # self-match (e.g. mutant variant paired with WT)

            # Group by normalised partner key, but display the longest seen variant
            # so "YAP1" wins over "YAP" when both appear in the corpus.
            entry = partners.setdefault(
                partner_key,
                {
                    "partner": partner_name,
                    "mention_count": 0,
                    "dois": set(),
                    "quantitative_anchors": [],
                },
            )
            if len(partner_name) > len(entry["partner"]):
                entry["partner"] = partner_name
            entry["mention_count"] += 1
            entry["dois"].add(doi)

            kd = finding.get("affinities_kd_Molar")
            ki = finding.get("inhibitory_constant_Ki")
            if kd is not None or ki is not None:
                entry["quantitative_anchors"].append(
                    {
                        "doi": doi,
                        "kd_M": kd,
                        "ki_M": ki,
                        "source_span": finding.get("source_span"),
                        "experimental_context": finding.get("experimental_context"),
                    }
                )

    return partners


def _shape_partners(
    raw: dict[str, dict[str, Any]],
    min_mentions: int,
    max_partners: int,
) -> list[dict[str, Any]]:
    shaped = [
        {
            "partner": v["partner"],
            "mention_count": v["mention_count"],
            "supporting_dois": sorted(v["dois"]),
            "quantitative_anchors": v["quantitative_anchors"],
        }
        for v in raw.values()
        if v["mention_count"] >= min_mentions
    ]
    shaped.sort(key=lambda x: (-x["mention_count"], x["partner"]))
    return shaped[:max_partners]


# ---------------------------------------------------------------------------
# Tool 2: quantitative evidence for a specific pair
# ---------------------------------------------------------------------------


def find_quantitative_evidence(
    protein_pair: list[str],
    fingerprint_dir: Path,
    metric: str = "Kd",
) -> dict[str, Any]:
    """Return all key_findings with non-null Kd/Ki for the requested pair.

    Pair matching is order-insensitive: ``["KRAS","RAF1"]`` matches a stored
    ``["RAF1","KRAS"]``. Results are sorted by metric value ascending so the
    tightest binder is first.
    """
    fingerprint_dir = Path(fingerprint_dir)
    metric = (metric or "Kd").strip()
    if metric not in {"Kd", "Ki", "both"}:
        return {
            "query_pair": protein_pair,
            "metric": metric,
            "results": [],
            "result_count": 0,
            "caveats": [f"Unknown metric '{metric}'. Use one of: Kd, Ki, both."],
        }

    if not fingerprint_dir.exists():
        return {
            "query_pair": protein_pair,
            "metric": metric,
            "results": [],
            "result_count": 0,
            "caveats": [f"Fingerprint directory not found: {fingerprint_dir}"],
        }

    if len(protein_pair) != 2:
        return {
            "query_pair": protein_pair,
            "metric": metric,
            "results": [],
            "result_count": 0,
            "caveats": ["protein_pair must contain exactly two protein names."],
        }

    a_query, b_query = protein_pair[0], protein_pair[1]
    results: list[dict[str, Any]] = []

    for fp_file in fingerprint_dir.glob("*.json"):
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        doi = (fp.get("paper_metadata") or {}).get("doi")
        if not doi:
            continue

        for finding in fp.get("key_findings") or []:
            pair = finding.get("protein_pair") or []
            pair = [p for p in pair if p]
            if len(pair) != 2:
                continue

            # Order-insensitive pair match.
            if not (
                (_protein_matches(pair[0], a_query) and _protein_matches(pair[1], b_query))
                or (_protein_matches(pair[0], b_query) and _protein_matches(pair[1], a_query))
            ):
                continue

            kd = finding.get("affinities_kd_Molar")
            ki = finding.get("inhibitory_constant_Ki")

            if metric == "Kd" and kd is None:
                continue
            if metric == "Ki" and ki is None:
                continue
            if metric == "both" and kd is None and ki is None:
                continue

            results.append(
                {
                    "doi": doi,
                    "source_span": finding.get("source_span"),
                    "claim": finding.get("claim"),
                    "kd_M": kd,
                    "ki_M": ki,
                    "experimental_context": finding.get("experimental_context"),
                    "key_amino_acid_residues": finding.get("key_amino_acid_residues") or [],
                    "confidence_score": finding.get("confidence_score"),
                    "stored_pair": pair,
                }
            )

    def _sort_key(r: dict[str, Any]) -> float:
        if metric == "Ki":
            return r["ki_M"] if r["ki_M"] is not None else float("inf")
        if metric == "Kd":
            return r["kd_M"] if r["kd_M"] is not None else float("inf")
        # "both": sort by whichever value is present, tightest first
        candidates = [v for v in (r["kd_M"], r["ki_M"]) if v is not None]
        return min(candidates) if candidates else float("inf")

    results.sort(key=_sort_key)

    return {
        "query_pair": protein_pair,
        "metric": metric,
        "results": results,
        "result_count": len(results),
        "caveats": [
            "Corpus quantitative coverage is sparse — most findings have null "
            "affinity. An empty result is therefore weak evidence; it does not "
            "imply that no published value exists.",
            "ΔΔG values are not currently captured by the curation schema; "
            "only Kd and Ki are queryable here.",
            "Pair matching is order-insensitive and uses light alias "
            "normalisation; verify the returned 'stored_pair' field corresponds "
            "to the proteins you actually meant.",
        ],
    }


# ---------------------------------------------------------------------------
# NetworkX graph cache (used by graph-query tools below)
# ---------------------------------------------------------------------------

_GRAPH_CACHE: dict[str, nx.Graph] = {}


def _build_graph(fingerprint_dir: Path) -> nx.Graph:
    """Walk every fingerprint and build an undirected weighted protein graph.

    Nodes are keyed by ``_normalize_protein(name)``; the longest variant seen
    in the corpus is preserved as ``display_name``. Edges aggregate mention
    counts, supporting DOIs, and any non-null Kd/Ki anchors from
    ``key_findings[].protein_pair``.
    """
    g = nx.Graph()

    for fp_file in fingerprint_dir.glob("*.json"):
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        doi = (fp.get("paper_metadata") or {}).get("doi")
        if not doi:
            continue

        for finding in fp.get("key_findings") or []:
            pair = [p for p in (finding.get("protein_pair") or []) if p]
            if len(pair) != 2:
                continue

            a_name, b_name = pair[0], pair[1]
            a_key, b_key = _normalize_protein(a_name), _normalize_protein(b_name)
            if not a_key or not b_key or a_key == b_key:
                continue  # self-loop (mutant-vs-WT) or empty key
            if len(a_key) < 2 or len(b_key) < 2:
                continue  # single-letter "names" are curation noise, not proteins

            kd = finding.get("affinities_kd_Molar")
            ki = finding.get("inhibitory_constant_Ki")
            source_span = finding.get("source_span")
            ctx = finding.get("experimental_context")

            for key, name in ((a_key, a_name), (b_key, b_name)):
                if not g.has_node(key):
                    g.add_node(key, display_name=name, total_mentions=0)
                elif len(name) > len(g.nodes[key]["display_name"]):
                    g.nodes[key]["display_name"] = name
                g.nodes[key]["total_mentions"] += 1

            if g.has_edge(a_key, b_key):
                edge = g[a_key][b_key]
                edge["mentions"] += 1
                edge["dois"].add(doi)
            else:
                g.add_edge(
                    a_key, b_key,
                    mentions=1,
                    dois={doi},
                    kd_anchors=[],
                    ki_anchors=[],
                    tightest_kd_M=None,
                    tightest_ki_M=None,
                )
                edge = g[a_key][b_key]

            if kd is not None:
                edge["kd_anchors"].append(
                    {"doi": doi, "kd_M": kd, "source_span": source_span,
                     "experimental_context": ctx}
                )
                if edge["tightest_kd_M"] is None or kd < edge["tightest_kd_M"]:
                    edge["tightest_kd_M"] = kd
            if ki is not None:
                edge["ki_anchors"].append(
                    {"doi": doi, "ki_M": ki, "source_span": source_span,
                     "experimental_context": ctx}
                )
                if edge["tightest_ki_M"] is None or ki < edge["tightest_ki_M"]:
                    edge["tightest_ki_M"] = ki

    # Convert sets to sorted lists so the graph is JSON-serialisable for export.
    for _, _, data in g.edges(data=True):
        data["dois"] = sorted(data["dois"])

    return g


def _get_graph(fingerprint_dir: Path) -> nx.Graph:
    """Lazy module-level cache. Cache key is the absolute path so multiple
    corpora can coexist (rare but cheap to support)."""
    fingerprint_dir = Path(fingerprint_dir)
    key = str(fingerprint_dir.resolve())
    if key not in _GRAPH_CACHE:
        _GRAPH_CACHE[key] = _build_graph(fingerprint_dir)
    return _GRAPH_CACHE[key]


def _resolve_node(g: nx.Graph, name: str) -> str | None:
    """Resolve a user-provided protein name to a graph node key.

    Tries exact normalised match first; falls back to substring containment.
    Both directions allowed but only when both strings are >= 3 chars to
    avoid spurious hits ('P' in 'KRAS', etc.).
    """
    target = _normalize_protein(name)
    if not target:
        return None
    if target in g:
        return target
    if len(target) < 3:
        return None
    # Prefer "target is a substring of node" first (TEAD → TEAD1)
    for n in g.nodes():
        if len(n) >= 3 and target in n:
            return n
    # Then "node is a substring of target" (less common: query has extra prefix/suffix)
    for n in g.nodes():
        if len(n) >= 3 and n in target:
            return n
    return None


def _resolve_seeds(g: nx.Graph, seeds: list[str]) -> tuple[list[str], list[str]]:
    """Each seed expands to ALL matching nodes (so 'TEAD' picks up TEAD1/2/3/4).

    Matching is one-directional: ``target in node_key`` only, with min length
    3 on both sides. This is critical to avoid a long missing-seed query like
    ``'NOT_A_REAL_PROTEIN_XYZ'`` matching every short node whose key happens
    to be a substring of it.
    """
    resolved: set[str] = set()
    missing: list[str] = []
    for seed in seeds:
        target = _normalize_protein(seed)
        if not target or len(target) < 2:
            missing.append(seed)
            continue
        if target in g:
            resolved.add(target)
            continue
        if len(target) < 3:
            missing.append(seed)
            continue
        hits = [n for n in g.nodes() if len(n) >= 3 and target in n]
        if hits:
            resolved.update(hits)
        else:
            missing.append(seed)
    return sorted(resolved), missing


# ---------------------------------------------------------------------------
# Tool 3: shortest interaction path
# ---------------------------------------------------------------------------


def shortest_interaction_path(
    protein_a: str,
    protein_b: str,
    fingerprint_dir: Path,
    max_hops: int = 4,
    k: int = 1,
) -> dict[str, Any]:
    """Top ``k`` shortest paths between two proteins, up to ``max_hops`` long.

    Returns nodes + edges with mention counts, supporting DOIs, and tightest
    Kd available per edge — plus ``min_mentions_along_path`` and
    ``weak_links_count`` so the consumer can flag low-confidence steps.
    """
    g = _get_graph(fingerprint_dir)

    src = _resolve_node(g, protein_a)
    dst = _resolve_node(g, protein_b)
    if src is None or dst is None:
        missing = [name for name, node in ((protein_a, src), (protein_b, dst)) if node is None]
        return {
            "query": [protein_a, protein_b],
            "max_hops": max_hops,
            "paths": [],
            "result_count": 0,
            "caveats": [
                f"No graph node found for: {', '.join(missing)}. The protein "
                "may not appear in any key_findings.protein_pair entry, or the "
                "alias differs too much from corpus naming.",
            ],
        }
    if src == dst:
        return {
            "query": [protein_a, protein_b],
            "max_hops": max_hops,
            "paths": [],
            "result_count": 0,
            "caveats": ["Source and target resolve to the same graph node."],
        }

    paths_out: list[dict[str, Any]] = []
    try:
        for path in nx.shortest_simple_paths(g, src, dst):
            if len(path) - 1 > max_hops:
                break
            edges_out = []
            min_mentions = float("inf")
            weak = 0
            for u, v in zip(path, path[1:]):
                e = g[u][v]
                edges_out.append({
                    "a": g.nodes[u]["display_name"],
                    "b": g.nodes[v]["display_name"],
                    "mentions": e["mentions"],
                    "dois": e["dois"],
                    "tightest_kd_M": e["tightest_kd_M"],
                    "tightest_ki_M": e["tightest_ki_M"],
                })
                min_mentions = min(min_mentions, e["mentions"])
                if e["mentions"] <= 1:
                    weak += 1
            paths_out.append({
                "nodes": [g.nodes[n]["display_name"] for n in path],
                "edges": edges_out,
                "hops": len(path) - 1,
                "min_mentions_along_path": int(min_mentions),
                "weak_links_count": weak,
            })
            if len(paths_out) >= k:
                break
    except nx.NetworkXNoPath:
        pass

    caveats = [
        "Edge weights are mention counts — a research-attention proxy, not "
        "biological strength.",
        "A 'weak link' (mentions <= 1) is supported by a single fingerprint; "
        "flag these explicitly when reporting paths to the user.",
    ]
    if not paths_out:
        caveats.append(
            f"No path found between '{protein_a}' and '{protein_b}' within "
            f"{max_hops} hops. They may be in disconnected components of the "
            "corpus graph, or no chain of measured interactions connects them."
        )

    return {
        "query": [protein_a, protein_b],
        "resolved_nodes": [g.nodes[src]["display_name"], g.nodes[dst]["display_name"]],
        "max_hops": max_hops,
        "paths": paths_out,
        "result_count": len(paths_out),
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# Tool 4: interaction hubs
# ---------------------------------------------------------------------------


def interaction_hubs(
    fingerprint_dir: Path,
    top_n: int = 20,
    min_mentions: int = 3,
) -> dict[str, Any]:
    """Top-degree nodes after filtering edges below ``min_mentions``."""
    g = _get_graph(fingerprint_dir)

    hubs: list[dict[str, Any]] = []
    for node, data in g.nodes(data=True):
        # Filter edges by min_mentions; degree = count of qualifying neighbours.
        qualified = [
            (nbr, g[node][nbr]) for nbr in g.neighbors(node)
            if g[node][nbr]["mentions"] >= min_mentions
        ]
        degree = len(qualified)
        if degree == 0:
            continue
        # Sample partners and DOIs from the top-mention edges of this node.
        qualified.sort(key=lambda x: -x[1]["mentions"])
        sample_partners = [g.nodes[nbr]["display_name"] for nbr, _ in qualified[:5]]
        sample_dois: list[str] = []
        seen_dois: set[str] = set()
        for _, edge_data in qualified[:5]:
            for d in edge_data["dois"]:
                if d not in seen_dois:
                    sample_dois.append(d)
                    seen_dois.add(d)
                if len(sample_dois) >= 5:
                    break
            if len(sample_dois) >= 5:
                break
        hubs.append({
            "protein": data["display_name"],
            "degree": degree,
            "total_mentions": data["total_mentions"],
            "sample_partners": sample_partners,
            "sample_dois": sample_dois,
        })

    hubs.sort(key=lambda h: (-h["degree"], -h["total_mentions"], h["protein"]))

    return {
        "top_n": top_n,
        "min_mentions": min_mentions,
        "hubs": hubs[:top_n],
        "graph_size": {"nodes": g.number_of_nodes(), "edges": g.number_of_edges()},
        "caveats": [
            "Hub rank reflects literature attention, not biological importance "
            "— well-studied proteins like KRAS, p53, EGFR will dominate any "
            "literature-derived hub list.",
            "Degree is filtered by min_mentions; raise this threshold to drop "
            "single-paper edges and emphasise robustly-supported hubs.",
        ],
    }


# ---------------------------------------------------------------------------
# Tool 5: export subgraph (Cytoscape.js JSON)
# ---------------------------------------------------------------------------


def export_subgraph(
    seeds: list[str],
    fingerprint_dir: Path,
    output_path: str,
    depth: int = 1,
    max_nodes: int = 200,
) -> dict[str, Any]:
    """BFS from each seed up to ``depth``; write Cytoscape.js JSON to disk.

    Each seed is expanded to all matching nodes (so a seed of 'TEAD' pulls
    in all four TEAD paralogs). The combined neighbourhood is capped at
    ``max_nodes`` (BFS-order); the cap is reported in the caveats when hit.
    """
    g = _get_graph(fingerprint_dir)
    seeds_resolved, missing = _resolve_seeds(g, seeds)
    truncated = False

    if not seeds_resolved:
        return {
            "output_path": output_path,
            "node_count": 0,
            "edge_count": 0,
            "seeds": seeds,
            "depth": depth,
            "truncated": False,
            "format": "Cytoscape.js JSON (.cyjs)",
            "missing_seeds": missing,
            "caveats": [f"No graph nodes found for any of: {seeds}"],
        }

    # BFS to bounded depth from the seeds, accumulating nodes in arrival order.
    nodes: list[str] = list(seeds_resolved)
    node_set: set[str] = set(seeds_resolved)
    frontier: list[tuple[str, int]] = [(s, 0) for s in seeds_resolved]
    while frontier:
        node, d = frontier.pop(0)
        if d >= depth:
            continue
        for nbr in g.neighbors(node):
            if nbr in node_set:
                continue
            if len(nodes) >= max_nodes:
                truncated = True
                break
            nodes.append(nbr)
            node_set.add(nbr)
            frontier.append((nbr, d + 1))
        if truncated:
            break

    sub = g.subgraph(node_set)

    elements = {
        "nodes": [
            {"data": {
                "id": n,
                "label": g.nodes[n]["display_name"],
                "total_mentions": g.nodes[n]["total_mentions"],
                "is_seed": n in seeds_resolved,
            }}
            for n in node_set
        ],
        "edges": [
            {"data": {
                "source": u,
                "target": v,
                "mentions": data["mentions"],
                "dois": ";".join(data["dois"]),
                "doi_count": len(data["dois"]),
                "tightest_kd_M": data["tightest_kd_M"],
                "tightest_ki_M": data["tightest_ki_M"],
            }}
            for u, v, data in sub.edges(data=True)
        ],
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"elements": elements}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    caveats: list[str] = []
    if truncated:
        caveats.append(
            f"Subgraph truncated at max_nodes={max_nodes}. Rerun with smaller "
            "depth or a tighter seed set for a complete neighbourhood."
        )
    if missing:
        caveats.append(
            f"Seeds not found in the graph and skipped: {missing}"
        )
    caveats.append(
        "Open in Cytoscape Desktop (File → Import → Network from file) or any "
        "Cytoscape.js viewer. Node IDs are normalised; 'label' carries the "
        "longest display variant seen in the corpus."
    )

    return {
        "output_path": str(out_path),
        "node_count": len(node_set),
        "edge_count": sub.number_of_edges(),
        "seeds": seeds,
        "resolved_seeds": [g.nodes[s]["display_name"] for s in seeds_resolved],
        "depth": depth,
        "truncated": truncated,
        "format": "Cytoscape.js JSON (.cyjs)",
        "caveats": caveats,
    }
