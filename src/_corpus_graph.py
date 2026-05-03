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
import time
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
    human_only: bool = True,
    taxa: list[int] | None = None,
) -> dict[str, Any]:
    """Aggregate interaction partners for ``protein`` across the corpus.

    Walks ``key_findings[].protein_pair`` in every fingerprint. For each
    matching finding, the *other* member of the pair becomes a partner and
    is aggregated with mention count, supporting DOIs, and any non-null
    Kd/Ki values from the same finding.

    Filtering (``human_only``, ``taxa``) applies to PARTNERS only — the
    query protein is never filtered, since the user asked for it
    explicitly. See module-level ``_node_passes_filter`` docstring for
    semantics.
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
    direct = _apply_partner_filter(direct, fingerprint_dir, human_only, taxa)
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
        second_raw = _apply_partner_filter(second_raw, fingerprint_dir, human_only, taxa)
        second_degree = _shape_partners(second_raw, min_mentions, max_partners)

    caveats = [
        "Matching uses light alias normalisation (case-insensitive, species "
        "prefix stripped, internal hyphens dropped) plus substring/prefix "
        "containment — paralogs like TEAD1/TEAD2/TEAD3/TEAD4 are kept distinct, "
        "but a query of 'TEAD' will match all of them.",
        "Corpus is biochemistry-biased; in vivo and clinical interaction "
        "partners may be undercounted. Absence of a partner here is not "
        "evidence that the interaction does not exist in the wider literature.",
    ]
    if taxa:
        caveats.append(
            f"Partner list filtered to taxa={sorted(set(taxa))}; nodes whose "
            "native_taxon was not curated will be excluded."
        )
    elif human_only:
        caveats.append(
            "Partner list filtered to human-resolvable proteins (default). "
            "Pass human_only=False to include yeast/bacterial/Drosophila partners."
        )

    return {
        "query_protein": protein,
        "depth": depth,
        "corpus_size": len(files),
        "direct_partners": direct_partners,
        "second_degree_partners": second_degree if depth >= 2 else [],
        "filter_applied": {"human_only": human_only, "taxa": taxa},
        "caveats": caveats,
    }


def _apply_partner_filter(
    partners: dict[str, dict[str, Any]],
    fingerprint_dir: Path,
    human_only: bool,
    taxa: list[int] | None,
) -> dict[str, dict[str, Any]]:
    """Drop entries from ``_collect_partners`` output that fail the graph filter.

    No-op when ``human_only=False`` and ``taxa`` is None (preserves the
    pre-filter behaviour). When a filter is active, partner names are
    looked up in the cached graph; entries whose normalised key is not a
    graph node (e.g. junk filtered at graph-build time) are also dropped.
    """
    if not human_only and not taxa:
        return partners
    g = _get_graph(fingerprint_dir)
    out: dict[str, dict[str, Any]] = {}
    for partner_key, entry in partners.items():
        if partner_key not in g:
            continue  # filtered as junk at graph-build time
        if not _node_passes_filter(g, partner_key, human_only, taxa):
            continue
        out[partner_key] = entry
    return out


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
#
# Cache key includes a version tag so existing in-process caches built before
# the sprint-2 ``protein_identifiers`` backfill rebuild automatically on
# first query. Bump on schema changes that affect node/edge attributes.

_GRAPH_CACHE_VERSION = "v3-with-native-taxa"
# Cache key tuple: (resolved_dir, schema_version, max_fingerprint_mtime).
# Mtime in the key means a long-running MCP server picks up newly-curated
# papers automatically — see ``_get_graph`` and ``_max_fingerprint_mtime``.
_GRAPH_CACHE: dict[tuple[str, str, float], nx.Graph] = {}


def _index_resolved_identifiers(fp: dict) -> tuple[dict, set[str]]:
    """Index a fingerprint's ``protein_identifiers`` block for fast lookup.

    Returns ``(by_raw_name, filtered_raw_names)``:
      - ``by_raw_name``: ``{raw_name: entry_dict}`` for resolved entries.
      - ``filtered_raw_names``: set of raw_names the sprint-2 backfill
        marked as junk/research_tool/complex/non_specific_histone/viral.

    A fingerprint that pre-dates the backfill returns ``({}, set())``;
    those nodes flow through with no annotations (existing behaviour).
    """
    pi = fp.get("protein_identifiers") or {}
    by_raw: dict[str, dict] = {
        e["raw_name"]: e for e in (pi.get("entries") or []) if e.get("raw_name")
    }
    filtered: set[str] = {
        f["raw_name"] for f in (pi.get("filtered_out") or []) if f.get("raw_name")
    }
    return by_raw, filtered


def _resolve_compound_name(raw_name: str, by_raw: dict) -> dict | None:
    """Annotation lookup with compound-name fallback.

    The sprint-2 walker splits ``"YAP/TAZ"`` into separate ``YAP`` and ``TAZ``
    entries before resolving — the literal compound string therefore won't
    appear as a key. When direct lookup fails on a compound name, use the
    same splitter sprint 2 used (which handles paralog shorthand like
    ``"MEK1/2"`` -> ``["MEK1", "MEK2"]``) and merge the resolved components
    into a synthetic family-head entry. All sides must resolve for the
    compound to count; partial matches return None.
    """
    direct = by_raw.get(raw_name)
    if direct is not None:
        return direct
    if "/" not in raw_name:
        return None

    # Reuse sprint-2's splitter so compound expansion is consistent across
    # the curation backfill and the graph annotation lookup.
    from src.identifier_normalizer import split_compound
    parts = split_compound(raw_name)
    if len(parts) < 2 or parts == [raw_name]:
        return None

    component_entries = [by_raw.get(p) for p in parts]
    if any(e is None for e in component_entries):
        return None
    # Merge: union of candidate_uniprots, take first gene_symbol as representative,
    # mark family_head True since this is a multi-gene compound mention.
    merged_uniprots: list[str] = []
    seen_up: set[str] = set()
    rep_symbol: str | None = None
    for e in component_entries:
        for up in e.get("candidate_uniprots") or []:
            if up not in seen_up:
                seen_up.add(up)
                merged_uniprots.append(up)
        if rep_symbol is None and e.get("human_gene_symbol"):
            rep_symbol = e["human_gene_symbol"]
    return {
        "raw_name": raw_name,
        "human_gene_symbol": rep_symbol,
        "candidate_uniprots": merged_uniprots,
        "is_family_head": True,
        "match_confidence": "compound",
    }


def _build_graph(fingerprint_dir: Path) -> nx.Graph:
    """Walk every fingerprint and build an undirected weighted protein graph.

    Nodes are keyed by ``_normalize_protein(name)``; the longest variant seen
    in the corpus is preserved as ``display_name``. Edges aggregate mention
    counts, supporting DOIs, and any non-null Kd/Ki anchors from
    ``key_findings[].protein_pair``.

    With sprint 2 in place, this builder additionally:
      - Skips edges where either endpoint was filtered (Cas9, GFP, PRC2, ...).
      - Attaches resolved ``human_gene_symbol`` / ``human_uniprot`` /
        ``candidate_uniprots`` / ``is_family_head`` to each node, aggregated
        across all fingerprints that mention it. The dominant gene_symbol
        wins when multiple papers disagree.
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

        resolved_by_raw, filtered_raw = _index_resolved_identifiers(fp)

        for finding in fp.get("key_findings") or []:
            pair = [p for p in (finding.get("protein_pair") or []) if p]
            if len(pair) != 2:
                continue

            # Drop edges where either endpoint was identified as junk by the
            # sprint-2 normaliser. This silently shrinks the graph (no more
            # 'DNA', 'Cas9', 'PRC2' as graph nodes), which is the desired
            # behaviour — those names never resolved to a gene anyway.
            if pair[0] in filtered_raw or pair[1] in filtered_raw:
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
                    g.add_node(
                        key,
                        display_name=name,
                        total_mentions=0,
                        # Aggregated identifier annotations — populated below
                        # from any matching protein_identifiers entry.
                        gene_symbol_counts={},
                        uniprot_candidates=set(),
                        native_taxa=set(),  # union of NCBI taxon IDs across mentions
                        is_family_head=False,
                        confidence_tiers={},
                    )
                elif len(name) > len(g.nodes[key]["display_name"]):
                    g.nodes[key]["display_name"] = name
                g.nodes[key]["total_mentions"] += 1

                # Attach resolved-identifier evidence from this fingerprint.
                entry = _resolve_compound_name(name, resolved_by_raw)
                if entry:
                    nd = g.nodes[key]
                    sym = entry.get("human_gene_symbol")
                    if sym:
                        nd["gene_symbol_counts"][sym] = nd["gene_symbol_counts"].get(sym, 0) + 1
                    for cand in entry.get("candidate_uniprots") or []:
                        nd["uniprot_candidates"].add(cand)
                    if entry.get("is_family_head"):
                        nd["is_family_head"] = True
                    tier = entry.get("match_confidence")
                    if tier:
                        nd["confidence_tiers"][tier] = nd["confidence_tiers"].get(tier, 0) + 1
                    # native_taxon comes from this fingerprint's protein_identifiers
                    # entry (set per-fingerprint, may be None for old fingerprints
                    # that pre-dated taxon resolution).
                    nt = entry.get("native_taxon")
                    if isinstance(nt, int):
                        nd["native_taxa"].add(nt)

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

    # Finalise node annotations: pick a dominant gene_symbol per node and
    # convert mutable structures to JSON-friendly forms.
    for _, nd in g.nodes(data=True):
        sym_counts: dict[str, int] = nd.get("gene_symbol_counts") or {}
        if sym_counts:
            # Most-cited gene_symbol wins; ties broken by lex order for stability.
            best_sym = sorted(sym_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            nd["human_gene_symbol"] = best_sym
        else:
            nd["human_gene_symbol"] = None
        cands = sorted(nd.get("uniprot_candidates") or [])
        nd["candidate_uniprots"] = cands
        nd["human_uniprot"] = cands[0] if cands else None
        nd["uniprot_candidates"] = cands  # serialisable form replaces the set
        nd["native_taxa"] = sorted(nd.get("native_taxa") or [])

    # Convert sets to sorted lists so the graph is JSON-serialisable for export.
    for _, _, data in g.edges(data=True):
        data["dois"] = sorted(data["dois"])

    return g


# TTL cache for the mtime scan itself: walking ~5k files costs ~100 ms,
# which adds up across many graph queries. A short TTL means new
# fingerprints become visible to a running MCP server within ~5 s of the
# curation hook completing — invisible to the user during interactive use.
_MTIME_TTL_SECONDS = 5.0
_MTIME_CACHE: dict[str, tuple[float, float]] = {}  # path -> (max_mtime, observed_at)


def _max_fingerprint_mtime(fingerprint_dir: Path) -> float:
    """Latest mtime across all fingerprint JSONs in a directory.

    Used as part of the graph cache key so a long-running MCP server
    automatically rebuilds when curate_papers writes new fingerprints —
    no restart required. The scan itself is TTL-cached (~5 s) so repeated
    graph queries don't repeatedly stat thousands of files.
    """
    key = str(fingerprint_dir.resolve())
    now = time.time()
    cached = _MTIME_CACHE.get(key)
    if cached and (now - cached[1]) < _MTIME_TTL_SECONDS:
        return cached[0]
    latest = 0.0
    for fp in fingerprint_dir.glob("*.json"):
        m = fp.stat().st_mtime
        if m > latest:
            latest = m
    _MTIME_CACHE[key] = (latest, now)
    return latest


def _get_graph(fingerprint_dir: Path) -> nx.Graph:
    """Lazy module-level cache, invalidated by both schema version AND fingerprint mtime.

    Cache key is ``(path, schema_version, max_fingerprint_mtime)``:
      - bumping ``_GRAPH_CACHE_VERSION`` forces a rebuild on the next query
        (used when node/edge attributes change shape);
      - any fingerprint newer than the last build also forces a rebuild,
        so newly-curated papers become visible without restarting the
        MCP server.
    """
    fingerprint_dir = Path(fingerprint_dir)
    resolved = str(fingerprint_dir.resolve())
    latest_mtime = _max_fingerprint_mtime(fingerprint_dir)
    key = (resolved, _GRAPH_CACHE_VERSION, latest_mtime)
    if key in _GRAPH_CACHE:
        return _GRAPH_CACHE[key]
    # Drop any older entries for this same directory + schema (different mtime
    # only) so the cache doesn't grow unbounded as fingerprints get added.
    stale_keys = [
        k for k in _GRAPH_CACHE
        if k[0] == resolved and k[1] == _GRAPH_CACHE_VERSION
    ]
    for k in stale_keys:
        del _GRAPH_CACHE[k]
    _GRAPH_CACHE[key] = _build_graph(fingerprint_dir)
    return _GRAPH_CACHE[key]


_HUMAN_TAXON = 9606


def _node_passes_filter(
    g: nx.Graph,
    node_key: str,
    human_only: bool,
    taxa: list[int] | None,
) -> bool:
    """True if a graph node should be visible under the given filter.

    ``human_only=True`` includes:
      - Nodes that resolved to a human gene_symbol (sprint-2 backfill), which
        covers human-native proteins AND non-human proteins ortholog-mapped
        to a human gene.
      - Excludes yeast / bacterial / Drosophila / unresolved nodes.

    ``taxa`` is a stricter override — when non-empty, the node's
    ``native_taxa`` set must intersect it. Use for "mouse-only" or
    comparative work. ``human_only`` is ignored when ``taxa`` is given.

    Empty ``native_taxa`` (curator omitted the field) under a ``taxa``
    filter is treated as a non-match. Under a plain ``human_only`` filter,
    presence of ``human_gene_symbol`` is the gate, so empty ``native_taxa``
    is fine — most resolved nodes are human-native and were tagged at
    backfill time.
    """
    nd = g.nodes[node_key]
    if taxa is not None and len(taxa) > 0:
        node_taxa = set(nd.get("native_taxa") or [])
        return bool(node_taxa & set(taxa))
    if human_only:
        # Resolved gene_symbol implies "this is a (human-mappable) gene product".
        return bool(nd.get("human_gene_symbol"))
    return True


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
    human_only: bool = True,
    taxa: list[int] | None = None,
) -> dict[str, Any]:
    """Top ``k`` shortest paths between two proteins, up to ``max_hops`` long.

    Returns nodes + edges with mention counts, supporting DOIs, and tightest
    Kd available per edge — plus ``min_mentions_along_path`` and
    ``weak_links_count`` so the consumer can flag low-confidence steps.

    Path search is performed over the filter-restricted subgraph: under the
    default ``human_only=True`` only human-resolvable nodes can serve as
    intermediates, so paths can't route through yeast / bacterial proteins.
    Source and target are exempted — the user asked for them by name.
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

    # Build a filter-restricted view: source/target always allowed; other
    # nodes must pass the filter to serve as intermediates.
    if human_only or taxa:
        allowed_nodes = {
            n for n in g.nodes()
            if n in {src, dst} or _node_passes_filter(g, n, human_only, taxa)
        }
        search_graph = g.subgraph(allowed_nodes)
    else:
        search_graph = g

    paths_out: list[dict[str, Any]] = []
    try:
        for path in nx.shortest_simple_paths(search_graph, src, dst):
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
    if taxa:
        caveats.append(
            f"Path search restricted to taxa={sorted(set(taxa))}; routing "
            "through nodes outside this taxa set was disallowed."
        )
    elif human_only:
        caveats.append(
            "Path search restricted to human-resolvable intermediates "
            "(default). If no path is found, retry with human_only=False to "
            "allow routing through unresolved or non-mammalian nodes."
        )

    return {
        "query": [protein_a, protein_b],
        "resolved_nodes": [g.nodes[src]["display_name"], g.nodes[dst]["display_name"]],
        "max_hops": max_hops,
        "paths": paths_out,
        "result_count": len(paths_out),
        "filter_applied": {"human_only": human_only, "taxa": taxa},
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# Tool 4: interaction hubs
# ---------------------------------------------------------------------------


def interaction_hubs(
    fingerprint_dir: Path,
    top_n: int = 20,
    min_mentions: int = 3,
    human_only: bool = True,
    taxa: list[int] | None = None,
) -> dict[str, Any]:
    """Top-degree nodes after filtering edges below ``min_mentions``.

    The hub itself AND its qualifying neighbours are required to pass the
    filter. So an unresolved bacterial protein won't appear as a hub under
    ``human_only=True``, and a human protein with mostly bacterial partners
    will get a degree of zero (and drop out) under the same filter.
    """
    g = _get_graph(fingerprint_dir)

    hubs: list[dict[str, Any]] = []
    for node, data in g.nodes(data=True):
        if not _node_passes_filter(g, node, human_only, taxa):
            continue
        # Filter edges by min_mentions; degree = count of qualifying neighbours
        # that ALSO pass the visibility filter (so a hub's degree under
        # human_only=True is its degree among human partners).
        qualified = [
            (nbr, g[node][nbr]) for nbr in g.neighbors(node)
            if g[node][nbr]["mentions"] >= min_mentions
            and _node_passes_filter(g, nbr, human_only, taxa)
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

    caveats = [
        "Hub rank reflects literature attention, not biological importance "
        "— well-studied proteins like KRAS, p53, EGFR will dominate any "
        "literature-derived hub list.",
        "Degree is filtered by min_mentions; raise this threshold to drop "
        "single-paper edges and emphasise robustly-supported hubs.",
    ]
    if taxa:
        caveats.append(
            f"Filtered to taxa={sorted(set(taxa))}: only nodes (and partners) "
            "whose native_taxa intersect this set are visible."
        )
    elif human_only:
        caveats.append(
            "Filtered to human-resolvable proteins (default). Hub degree is "
            "counted only over human-resolved neighbours. Pass human_only=False "
            "to include unresolved / non-mammalian nodes."
        )

    return {
        "top_n": top_n,
        "min_mentions": min_mentions,
        "hubs": hubs[:top_n],
        "graph_size": {"nodes": g.number_of_nodes(), "edges": g.number_of_edges()},
        "filter_applied": {"human_only": human_only, "taxa": taxa},
        "caveats": caveats,
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
    with_depmap: bool = False,
    depmap_min_n: int = 100,
    human_only: bool = True,
    taxa: list[int] | None = None,
) -> dict[str, Any]:
    """BFS from each seed up to ``depth``; write Cytoscape.js JSON to disk.

    Each seed is expanded to all matching nodes (so a seed of 'TEAD' pulls
    in all four TEAD paralogs). The combined neighbourhood is capped at
    ``max_nodes`` (BFS-order); the cap is reported in the caveats when hit.

    Seeds themselves are always included regardless of filter — the user
    asked for them by name. BFS expansion only steps into neighbours that
    pass the ``human_only`` / ``taxa`` filter, so the exported neighbourhood
    won't drift into yeast / bacterial space when the user is exploring
    human biology.

    When ``with_depmap=True``, every edge in the exported subgraph also
    carries DepMap CRISPR co-essentiality (Pearson ``r`` and ``n`` cell
    lines). Endpoints are resolved to gene symbols via the same identifier
    normaliser used in sprint 2; family-head endpoints try the cross-
    product of candidate genes and report the tightest |r|. Edges where
    either endpoint is not in DepMap or the overlap is below
    ``depmap_min_n`` get ``depmap_r: null`` and a ``depmap_reason`` tag.
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
    # Seeds are always admitted; expansion respects the visibility filter
    # so the neighbourhood doesn't drift into out-of-scope nodes.
    seed_keys = set(seeds_resolved)
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
            if nbr not in seed_keys and not _node_passes_filter(g, nbr, human_only, taxa):
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

    # Optional DepMap enrichment — done before serialisation so the JSON
    # carries depmap fields. Lazy-loaded on first access; subsequent calls
    # share the cached singleton.
    depmap_stats: dict[str, int] = {"available": 0, "missing": 0, "low_overlap": 0}
    if with_depmap:
        from src.depmap import correlation_for_pair_family
        from src.identifier_normalizer import get_normalizer

        norm = get_normalizer()

        # Resolve each node's display_name to candidate gene symbols.
        # Family-head nodes (e.g. 'TEAD') yield multiple candidates.
        node_to_gene_candidates: dict[str, list[str]] = {}
        for n in node_set:
            display_name = g.nodes[n]["display_name"]
            res = norm.resolve(display_name)
            cands: list[str] = []
            if res.match_confidence and res.candidate_uniprots:
                # We have UniProt accessions; reverse-lookup via HGNC to gene symbols.
                # Cheaper: re-resolve to harvest ALL gene symbols mapped from those
                # UniProts via HGNC's symbol→uniprot index inverse.
                for sym, ups in norm._hgnc_symbol_to_uniprot.items():
                    if any(u in ups for u in res.candidate_uniprots):
                        cands.append(sym)
            # Fallback to the dominant gene_symbol if reverse-lookup yielded nothing.
            if not cands and res.human_gene_symbol:
                cands = [res.human_gene_symbol]
            node_to_gene_candidates[n] = cands

    edge_records: list[dict[str, Any]] = []
    for u, v, data in sub.edges(data=True):
        rec = {
            "source": u,
            "target": v,
            "mentions": data["mentions"],
            "dois": ";".join(data["dois"]),
            "doi_count": len(data["dois"]),
            "tightest_kd_M": data["tightest_kd_M"],
            "tightest_ki_M": data["tightest_ki_M"],
        }
        if with_depmap:
            ca = node_to_gene_candidates.get(u) or []
            cb = node_to_gene_candidates.get(v) or []
            if not ca or not cb:
                rec["depmap_r"] = None
                rec["depmap_n"] = None
                rec["depmap_reason"] = "unresolved_endpoint"
                depmap_stats["missing"] += 1
            else:
                cor = correlation_for_pair_family(ca, cb, min_n=depmap_min_n)
                if cor.get("available"):
                    rec["depmap_r"] = cor["r"]
                    rec["depmap_n"] = cor["n"]
                    rec["depmap_best_pair"] = list(cor["best_pair"])
                    rec["depmap_family_ambiguity"] = cor["family_ambiguity"]
                    depmap_stats["available"] += 1
                else:
                    rec["depmap_r"] = None
                    rec["depmap_n"] = None
                    rec["depmap_reason"] = cor.get("reason", "unknown")
                    if cor.get("reason") == "low_overlap":
                        depmap_stats["low_overlap"] += 1
                    else:
                        depmap_stats["missing"] += 1
        edge_records.append(rec)

    elements = {
        "nodes": [
            {"data": {
                "id": n,
                "label": g.nodes[n]["display_name"],
                "total_mentions": g.nodes[n]["total_mentions"],
                "is_seed": n in seeds_resolved,
                "human_gene_symbol": g.nodes[n].get("human_gene_symbol"),
                "human_uniprot": g.nodes[n].get("human_uniprot"),
                "is_family_head": g.nodes[n].get("is_family_head", False),
            }}
            for n in node_set
        ],
        "edges": [{"data": rec} for rec in edge_records],
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

    result: dict[str, Any] = {
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
    if with_depmap:
        result["depmap_enrichment"] = depmap_stats
        result["caveats"].append(
            "DepMap correlations are Pearson r over CRISPR Chronos essentiality "
            "profiles across cell lines. Positive r = co-essential (often same "
            "pathway / heterodimer / co-functional module); negative r often "
            "= compensatory or synthetic-lethal-style relationship. Magnitude "
            "is the signal — sign carries different biological meanings."
        )
    if taxa:
        result["caveats"].append(
            f"BFS restricted to taxa={sorted(set(taxa))}; non-matching nodes "
            "are excluded from neighbourhood expansion (seeds always admitted)."
        )
    elif human_only:
        result["caveats"].append(
            "BFS restricted to human-resolvable proteins (default). Pass "
            "human_only=False to allow yeast / bacterial / unresolved nodes "
            "in the export — useful for host-pathogen or comparative work."
        )
    result["filter_applied"] = {"human_only": human_only, "taxa": taxa}
    return result


# ---------------------------------------------------------------------------
# Tool 6: pairwise genetic codependency
# ---------------------------------------------------------------------------


def get_genetic_codependency(
    protein_a: str,
    protein_b: str,
    fingerprint_dir: Path,
    min_n: int = 100,
) -> dict[str, Any]:
    """Pearson correlation of CRISPR essentiality between two proteins.

    Resolves each input name to candidate human gene symbols via the same
    normaliser the sprint-2 backfill used (HGNC + curated aliases). For
    family-head names ('AKT' → AKT1/2/3) every candidate cross-product is
    evaluated; the tightest |r| is reported with ``family_ambiguity=True``.

    Returns ``available=False`` with a ``reason`` when:
      - either name does not resolve to a human gene symbol
      - either gene is not present in DepMap CRISPRGeneEffect
      - the cell-line overlap is below ``min_n``

    Use this on top of literature-graph queries to ask: "the corpus says
    X interacts with Y — does that hold up in DepMap co-essentiality?"
    A high |r| corroborates a functional relationship; a near-zero r is
    evidence the literature interaction is mutation-conditional or is
    not a fitness-shared pathway.
    """
    from src.depmap import correlation_for_pair_family
    from src.identifier_normalizer import get_normalizer

    norm = get_normalizer()
    ra = norm.resolve(protein_a)
    rb = norm.resolve(protein_b)

    def _candidates(res: Any, raw: str) -> tuple[list[str], dict[str, Any] | None]:
        """Return (gene_symbol_list, error_dict)."""
        if res.filtered_reason:
            return [], {
                "available": False,
                "reason": "filtered_input",
                "raw_name": raw,
                "filter_reason": res.filtered_reason,
            }
        if not res.match_confidence:
            return [], {
                "available": False,
                "reason": "unresolved_input",
                "raw_name": raw,
            }
        # Reverse-lookup all candidate gene_symbols from the candidate UniProts
        # so family heads are correctly expanded.
        if not res.candidate_uniprots:
            return ([res.human_gene_symbol] if res.human_gene_symbol else []), None
        cands: list[str] = []
        for sym, ups in norm._hgnc_symbol_to_uniprot.items():
            if any(u in ups for u in res.candidate_uniprots):
                cands.append(sym)
        if not cands and res.human_gene_symbol:
            cands = [res.human_gene_symbol]
        return cands, None

    cands_a, err_a = _candidates(ra, protein_a)
    if err_a:
        return err_a
    cands_b, err_b = _candidates(rb, protein_b)
    if err_b:
        return err_b

    cor = correlation_for_pair_family(cands_a, cands_b, min_n=min_n)
    cor["query_a"] = protein_a
    cor["query_b"] = protein_b
    cor["resolved_a"] = ra.human_gene_symbol
    cor["resolved_b"] = rb.human_gene_symbol
    cor["caveats"] = [
        "Pearson r is on Chronos CRISPR essentiality scores across "
        f"DepMap cell lines (n requires >= {min_n} overlapping non-NaN). "
        "Co-essential != direct binding; treat as orthogonal corroboration "
        "of a functional relationship.",
        "Negative r often signals compensatory / synthetic-lethal-style "
        "relationships, not absence of interaction. Magnitude is the signal.",
    ]
    if cor.get("family_ambiguity"):
        cor["caveats"].append(
            "Family-head input expanded to multiple paralogs; reported r is "
            "the tightest |r| across all evaluated pairs. See ``all_results`` "
            "for the full landscape."
        )
    return cor


# ---------------------------------------------------------------------------
# Tool 7: top-k co-correlated genes for a single protein
# ---------------------------------------------------------------------------


def cluster_for_protein(protein: str) -> dict[str, Any]:
    """Return the co-functional module a protein belongs to.

    Resolves the protein name to a human gene symbol, looks it up in the
    persisted cluster registry (``data/clusters.json``, produced by
    ``scripts/cluster_corpus.py``), and returns the cluster as a dict.

    Use this when the user asks "what pathway / module is X part of?",
    "what's co-essential with X across cell lines?", or wants to expand
    a single seed into its functional neighbourhood. Complements
    ``find_cocorrelated_genes`` (top-K pairwise) — clusters give the
    consensus module rather than top-K of one gene.

    Returns ``{"available": False, "reason": ...}`` when the input doesn't
    resolve, the gene isn't in any cluster (typically because it had no
    DepMap-supported or literature-supported edges meeting the threshold),
    or the cluster registry is missing.
    """
    from src.clustering import load_clusters, get_cluster_indexes
    from src.identifier_normalizer import get_normalizer
    norm = get_normalizer()
    res = norm.resolve(protein)
    if res.filtered_reason:
        return {"available": False, "reason": "filtered_input",
                "raw_name": protein, "filter_reason": res.filtered_reason}
    if not res.match_confidence or not res.human_gene_symbol:
        return {"available": False, "reason": "unresolved_input", "raw_name": protein}
    sym = res.human_gene_symbol.upper()
    try:
        payload = load_clusters()
        _, sym_to_cluster = get_cluster_indexes()
    except FileNotFoundError as exc:
        return {"available": False, "reason": "no_cluster_registry", "detail": str(exc)}
    cid = sym_to_cluster.get(sym)
    if cid is None:
        return {
            "available": False, "reason": "not_clustered",
            "raw_name": protein, "resolved_symbol": sym,
            "hint": "Gene resolved but not in any cluster — likely had no DepMap-supported "
                    "edges meeting the threshold, or had no literature partners.",
        }
    cluster = next(c for c in payload["clusters"] if c["id"] == cid)
    return {
        "available": True,
        "query": protein,
        "resolved_symbol": sym,
        "cluster": cluster,
        "registry_meta": {
            "algorithm": payload.get("algorithm"),
            "resolution": payload.get("resolution"),
            "weight_formula": payload.get("weight_formula"),
            "computed_at": payload.get("computed_at"),
        },
        "caveats": [
            "Clusters are Louvain communities on the literature graph weighted "
            "by mentions * |DepMap r| (with a small default for DepMap-unavailable "
            "edges). They reflect FUNCTIONAL co-essentiality + co-mention modules, "
            "not pathway topology — KRAS/BRAF can land in different clusters "
            "because their CRISPR essentiality is mutation-conditional.",
            "Cluster size capped at 50; oversized clusters were recursively "
            "re-clustered at higher resolution.",
        ],
    }


def cluster_members(cluster_id: int) -> dict[str, Any]:
    """Return the full record for one cluster by ID, plus convenience indexes.

    Use after ``cluster_for_protein`` returned a cluster — or when you have
    a cluster ID from another tool — to enumerate members in detail.
    """
    from src.clustering import load_clusters
    try:
        payload = load_clusters()
    except FileNotFoundError as exc:
        return {"available": False, "reason": "no_cluster_registry", "detail": str(exc)}
    cluster = next((c for c in payload["clusters"] if c["id"] == cluster_id), None)
    if cluster is None:
        return {"available": False, "reason": "unknown_cluster_id", "cluster_id": cluster_id}
    return {
        "available": True,
        "cluster": cluster,
        "registry_meta": {
            "algorithm": payload.get("algorithm"),
            "resolution": payload.get("resolution"),
            "computed_at": payload.get("computed_at"),
        },
    }


def find_clusters_by_keyword(query: str, max_results: int = 20) -> dict[str, Any]:
    """Return clusters whose member gene symbols contain ``query`` (case-insensitive).

    Substring match — ``"CDK"`` finds clusters containing CDK1 / CDK2 / CDK4
    / CDK6 etc. Useful for hypothesis-led navigation: "show me the kinase
    clusters", "which clusters contain HDAC genes?".

    Sorted by cluster size descending so the biggest module surfaces first.
    Truncated to ``max_results``.
    """
    from src.clustering import load_clusters
    try:
        payload = load_clusters()
    except FileNotFoundError as exc:
        return {"available": False, "reason": "no_cluster_registry", "detail": str(exc)}
    q = (query or "").strip().upper()
    if not q:
        return {"available": False, "reason": "empty_query"}

    matching: list[dict[str, Any]] = []
    for c in payload.get("clusters", []):
        hits = [s for s in (c.get("member_symbols") or []) if q in s.upper()]
        if hits:
            matching.append({
                "cluster_id": c["id"],
                "size": c["size"],
                "hub_member_symbol": c.get("hub_member_symbol"),
                "matching_members": sorted(hits),
                "all_members": c.get("member_symbols") or [],
            })

    matching.sort(key=lambda x: -x["size"])
    return {
        "available": True,
        "query": query,
        "result_count": len(matching),
        "results": matching[:max_results],
    }


def find_cocorrelated_genes(
    protein: str,
    top_k: int = 25,
    min_abs_r: float = 0.2,
    min_n: int = 100,
) -> dict[str, Any]:
    """Top-k DepMap co-essential / anti-correlated genes for a target protein.

    Resolves ``protein`` to a single human gene symbol and returns the
    genes whose CRISPR essentiality profile most closely tracks (or
    inversely tracks) the target's across DepMap cell lines.

    For family-head inputs, the dominant resolution is used (same gene
    that ``human_gene_symbol`` would have returned in sprint 2). To
    explore each paralog separately, query the specific gene name.

    Returns ``available=False`` with a ``reason`` when input cannot be
    resolved or the gene is absent from DepMap.
    """
    from src.depmap import correlations_for_gene
    from src.identifier_normalizer import get_normalizer

    norm = get_normalizer()
    res = norm.resolve(protein)
    if res.filtered_reason:
        return {
            "available": False,
            "reason": "filtered_input",
            "raw_name": protein,
            "filter_reason": res.filtered_reason,
        }
    if not res.match_confidence or not res.human_gene_symbol:
        return {
            "available": False,
            "reason": "unresolved_input",
            "raw_name": protein,
        }

    out = correlations_for_gene(
        res.human_gene_symbol,
        top_k=top_k,
        min_abs_r=min_abs_r,
        min_n=min_n,
    )
    out["query"] = protein
    out["resolved_symbol"] = res.human_gene_symbol
    if res.is_family_head:
        out["family_head_warning"] = (
            f"Input '{protein}' resolves to a family head; only the dominant "
            f"member ({res.human_gene_symbol}) was queried. Call again with "
            "specific paralogs to explore each separately."
        )
    out["caveats"] = [
        "Pearson r on CRISPR Chronos essentiality across DepMap cell lines.",
        "Positive r = co-essential (often same pathway / heterodimer / "
        "co-functional module).",
        "Negative r often = compensatory or synthetic-lethal-style "
        "relationship. Both directions are biologically informative.",
        f"Filtered to |r| >= {min_abs_r:.2f} and n >= {min_n} overlapping cell lines.",
    ]
    return out
