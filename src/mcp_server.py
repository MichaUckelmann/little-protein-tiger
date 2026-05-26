"""MCP server exposing the literature vector DB to Claude Code."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

# Log to file so we can debug subprocess issues (stdout is reserved for MCP stdio)
_log_file = ROOT / "data" / "mcp_server.log"
logger.remove()
logger.add(_log_file, level="DEBUG", rotation="5 MB", enqueue=True)

from src._corpus_graph import (
    cluster_for_protein as _cluster_for_protein,
    cluster_members as _cluster_members,
    export_subgraph as _export_subgraph,
    find_clusters_by_keyword as _find_clusters_by_keyword,
    find_cocorrelated_genes as _find_cocorrelated_genes,
    find_quantitative_evidence as _find_quantitative_evidence,
    get_genetic_codependency as _get_genetic_codependency,
    get_interactions_for as _get_interactions_for,
    interaction_hubs as _interaction_hubs,
    novelty_signal as _novelty_signal,
    shortest_interaction_path as _shortest_interaction_path,
)
from src.fingerprint_store import load_fingerprint
from src.vector_store import VectorStore

logger.info("MCP server starting up — pre-importing sentence_transformers...")
import sentence_transformers  # noqa: F401 — must import on main thread before FastMCP starts
                               # its thread pool; importing from inside run_in_executor causes
                               # an OpenMP/MKL deadlock with the asyncio event loop.
logger.info("sentence_transformers imported. Starting MCP server.")
mcp = FastMCP("literature-db")

# Paths from env (set in .mcp.json); fall back to config defaults
_VECTOR_DB_PATH  = os.getenv("VECTOR_DB_PATH",  str(ROOT / "data/vectors"))
_FINGERPRINT_DIR = os.getenv("FINGERPRINT_DIR", str(ROOT / "data/fingerprints"))
_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL",  "NeuML/pubmedbert-base-embeddings")

_store: VectorStore | None = None


def _get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore(db_path=_VECTOR_DB_PATH, embedding_model=_EMBEDDING_MODEL)
    return _store


@mcp.tool()
def search_corpus(query: str, top_k: int = 5, study_type: str = "", study_category: str = "") -> str:
    """
    Semantically search the curated scientific literature corpus.

    Returns the top-k most relevant paper fingerprints ranked by similarity.
    Each result includes the situational context, key quantitative findings
    (Kd, Ki), protein lists, DOI, study type, and study category.

    Use this tool when asked about proteins, mechanisms, assay results,
    inhibitors, binding affinities, or study designs present in the corpus.

    Args:
        query:          Natural language query. Include protein names, assay types,
                        or quantitative terms for best results.
        top_k:          Number of results to return (1-20). Default 5.
        study_type:     Optional methodology filter — one of: experimental_in_vitro,
                        experimental_in_vivo, experimental_structural,
                        computational, review, case_study. Leave empty for no filter.
        study_category: Optional domain filter — one of: biochemistry,
                        pathway_biology, structural_biology, host_pathogen,
                        clinical, review. Use pathway_biology to find disease
                        mechanism and target selection papers. Use host_pathogen
                        for bacterial/viral virulence and AMR papers. Leave
                        empty for no filter.
    """
    tool_input: dict = {"query": query, "top_k": top_k}
    if study_type:
        tool_input["study_type"] = study_type
    if study_category:
        tool_input["study_category"] = study_category
    return _get_store().execute_search_tool(tool_input)


@mcp.tool()
def get_fingerprint(identifier: str) -> str:
    """
    Retrieve the complete fingerprint for a specific paper.

    Returns the full structured fingerprint JSON including all key findings,
    methodology details, contradictions, and entity lists. Use this after
    search_corpus identifies a paper of interest and you need complete detail.

    Args:
        identifier: DOI (e.g. "10.1021/jacs.5c12876") or paper_key
                    (e.g. "doi:10.1021/jacs.5c12876"). Both forms accepted.
    """
    if not identifier.startswith("doi:") and not identifier.startswith("pmcid:"):
        paper_key = f"doi:{identifier}"
    else:
        paper_key = identifier

    fp = load_fingerprint(paper_key, _FINGERPRINT_DIR)
    if fp is None:
        return json.dumps({"error": f"No fingerprint found for '{identifier}'"})
    return json.dumps(fp, ensure_ascii=False, indent=2)


@mcp.tool()
def get_interactions_for(
    protein: str, depth: int = 1, min_mentions: int = 1,
    human_only: bool = True, taxa: list[int] | None = None,
) -> str:
    """
    Aggregate the interaction partners of a protein across the entire corpus.

    Walks key_findings[].protein_pair in every fingerprint and returns a
    deduplicated partner list with mention counts, supporting DOIs, and any
    quantitative anchors (Kd / Ki) reported in the same key_findings entry.

    Use this when the user asks "which proteins interact with X?" or wants a
    network around a target — semantic search via search_corpus misses the
    long tail of the interactome because top-k is small. Reach for this tool
    early in relational queries rather than running multiple search_corpus
    calls.

    Args:
        protein:      Protein name (gene symbol or common name). Matching is
                      case-insensitive and aliases are normalised, so "YAP"
                      will match "YAP1" / "hYAP". Paralogs are kept distinct
                      (TEAD1 ≠ TEAD2) but a query of "TEAD" hits all four.
        depth:        1 (default) returns direct partners only. 2 also returns
                      partners-of-partners (capped at 50).
        min_mentions: Filter out partners mentioned fewer than this many times
                      across the corpus. Default 1.
        human_only:   Default True — partner list is restricted to proteins
                      that resolved to a human gene symbol (covers human-native
                      and ortholog-mapped). Set False for host-pathogen,
                      yeast / bacterial / Drosophila exploration.
        taxa:         Optional list of NCBI taxon IDs to include explicitly
                      (e.g. [9606, 10090] for human + mouse comparative).
                      Overrides human_only when set.
    """
    result = _get_interactions_for(
        protein=protein,
        fingerprint_dir=Path(_FINGERPRINT_DIR),
        depth=int(depth),
        min_mentions=int(min_mentions),
        human_only=bool(human_only),
        taxa=list(taxa) if taxa else None,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def find_quantitative_evidence(protein_pair: list[str], metric: str = "Kd") -> str:
    """
    Pull every key_findings entry with a measured Kd or Ki for a specific pair.

    Scans the entire corpus and returns only findings where the requested
    metric is non-null, sorted by metric value ascending (tightest binder
    first). Use this when the user asks for the affinity of a specific pair
    or wants to know what's been measured experimentally.

    Args:
        protein_pair: List of exactly two protein names. Order-insensitive —
                      ["KRAS","RAF1"] matches stored ["RAF1","KRAS"]. Same
                      alias normalisation as get_interactions_for.
        metric:       "Kd" (default), "Ki", or "both". ΔΔG is not currently
                      captured by the curation schema and cannot be queried.
    """
    result = _find_quantitative_evidence(
        protein_pair=list(protein_pair or []),
        fingerprint_dir=Path(_FINGERPRINT_DIR),
        metric=metric,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def shortest_interaction_path(
    protein_a: str, protein_b: str, max_hops: int = 4, k: int = 1,
    human_only: bool = True, taxa: list[int] | None = None,
) -> str:
    """
    Top k shortest interaction paths between two proteins in the corpus graph.

    Walks an undirected weighted graph built once from key_findings.protein_pair
    entries across all fingerprints. Each edge carries mention count, supporting
    DOIs, and tightest measured Kd/Ki. Returns paths up to max_hops long with
    min_mentions_along_path and weak_links_count so you can flag low-confidence
    steps when reporting to the user.

    Use this for "is X connected to Y?" / "draw the cascade from X to Y"
    questions. Use get_interactions_for instead for "what does X bind?".

    Args:
        protein_a:  Source protein. Aliases are normalised (YAP matches YAP1).
        protein_b:  Target protein.
        max_hops:   Maximum path length in edges. Default 4.
        k:          Number of distinct shortest paths to return. Default 1.
        human_only: Default True — paths are routed through human-resolvable
                    intermediates only. Source / target are exempt. Set False
                    if a host-pathogen or yeast cascade requires non-human
                    intermediates.
        taxa:       Optional NCBI taxon ID allow-list for intermediate nodes
                    (overrides human_only).
    """
    result = _shortest_interaction_path(
        protein_a=protein_a,
        protein_b=protein_b,
        fingerprint_dir=Path(_FINGERPRINT_DIR),
        max_hops=int(max_hops),
        k=int(k),
        human_only=bool(human_only),
        taxa=list(taxa) if taxa else None,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def interaction_hubs(
    top_n: int = 20, min_mentions: int = 3,
    human_only: bool = True, taxa: list[int] | None = None,
) -> str:
    """
    Highest-degree proteins in the corpus interaction graph.

    Computes degree centrality after filtering edges with fewer than
    min_mentions supporting fingerprints — keeps single-paper noise from
    inflating hub rank. Returns top_n proteins with sample partners and DOIs
    per node.

    Caveat: hub rank reflects literature attention, not biological importance.
    KRAS, p53, EGFR will dominate any literature-derived hub list. Surface this
    to the user when interpreting results.

    Args:
        top_n:        Number of hubs to return. Default 20.
        min_mentions: Drop edges with fewer than this many supporting DOIs
                      before computing degree. Default 3.
        human_only:   Default True — only human-resolvable nodes are eligible
                      hubs, and degree is counted over human-resolved partners.
                      Set False to include unresolved or non-mammalian hubs.
        taxa:         Optional NCBI taxon allow-list (overrides human_only).
    """
    result = _interaction_hubs(
        fingerprint_dir=Path(_FINGERPRINT_DIR),
        top_n=int(top_n),
        min_mentions=int(min_mentions),
        human_only=bool(human_only),
        taxa=list(taxa) if taxa else None,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def novelty_signal(protein: str) -> str:
    """
    Deterministic corpus-coverage novelty score for a candidate target.

    Higher score = less prior art = more novel. Score is in [0.0, 1.0] where
    well-studied targets (TP53, KRAS) score near 0.0 and proteins absent from
    the corpus score near 1.0. The four input signals (mention count, PDB
    paper count, prior-targeting paper count, quantitative-finding count)
    are also returned so a reviewer can recompute the score by hand.

    Used by wildcard-expert to triage candidate targets in Phase 2.5. Do NOT
    hard-threshold on the score in deterministic code — paralog-substring
    matching can inflate counts for short queries (e.g. "RAS"); the caveats
    field flags this.

    Args:
        protein: Gene symbol or protein name. Queries < 2 chars after alias
                 normalisation are rejected (returns novelty=1.0 with caveat).
    """
    result = _novelty_signal(
        protein=protein,
        fingerprint_dir=Path(_FINGERPRINT_DIR),
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def export_subgraph(
    seeds: list[str], output_path: str, depth: int = 1, max_nodes: int = 200,
    with_depmap: bool = False, depmap_min_n: int = 100,
    human_only: bool = True, taxa: list[int] | None = None,
) -> str:
    """
    Export a depth-bounded neighbourhood around seed proteins as Cytoscape.js JSON.

    Each seed expands to all matching nodes (so a seed of 'TEAD' pulls in
    TEAD1/2/3/4). BFS up to `depth`; capped at `max_nodes` (BFS-order). The
    output file opens in Cytoscape Desktop or any Cytoscape.js viewer.

    Use when the user asks for a visual exploration of an interaction
    neighbourhood. Returns a small confirmation dict (the graph itself goes
    to disk, not into the conversation) so it doesn't burn output tokens.

    Args:
        seeds:        List of protein names to seed the BFS from.
        output_path:  Destination .cyjs file. Relative paths resolve to project root.
        depth:        BFS depth in edges. Default 1.
        max_nodes:    Hard cap on nodes in the export. Default 200.
        with_depmap:  If True, attach DepMap CRISPR co-essentiality (Pearson r,
                      n cell lines) to each edge. Endpoints are resolved to
                      gene symbols; family heads try cross-products. Adds a few
                      seconds for the initial DepMap load. Default False.
        depmap_min_n: Minimum overlapping non-NaN cell lines for a correlation
                      to be reported. Default 100.
        human_only:   Default True — BFS only steps into human-resolvable
                      partners (seeds always admitted). Set False for
                      host-pathogen / yeast / Drosophila exploration.
        taxa:         Optional NCBI taxon allow-list (overrides human_only).
    """
    result = _export_subgraph(
        seeds=list(seeds or []),
        fingerprint_dir=Path(_FINGERPRINT_DIR),
        output_path=output_path,
        depth=int(depth),
        max_nodes=int(max_nodes),
        with_depmap=bool(with_depmap),
        depmap_min_n=int(depmap_min_n),
        human_only=bool(human_only),
        taxa=list(taxa) if taxa else None,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def get_genetic_codependency(
    protein_a: str, protein_b: str, min_n: int = 100,
) -> str:
    """
    Pearson correlation of CRISPR essentiality between two proteins (DepMap).

    Resolves each name to candidate human gene symbols via HGNC + curated
    aliases. Family-head names ('AKT' → AKT1/2/3) try every candidate
    cross-product and report the tightest |r| with `family_ambiguity=True`.

    Use this on top of literature-graph queries to ask: "the corpus says X
    interacts with Y — does that hold up in DepMap co-essentiality?". A
    high |r| corroborates a functional relationship; a near-zero r is
    evidence the literature interaction is mutation-conditional or not a
    fitness-shared pathway.

    Sign convention:
      - Positive r = co-essential across cell lines (often same pathway,
        heterodimer, or co-functional module).
      - Negative r often = compensatory or synthetic-lethal-style
        relationship, NOT absence of interaction.
      - Magnitude is the signal; sign carries different biological meanings.

    Args:
        protein_a: First protein name (gene symbol or alias).
        protein_b: Second protein name. Order-insensitive.
        min_n:     Minimum overlapping non-NaN cell lines required.
                   Below this, the result is `available=False` with
                   `reason="low_overlap"`. Default 100.
    """
    result = _get_genetic_codependency(
        protein_a=protein_a,
        protein_b=protein_b,
        fingerprint_dir=Path(_FINGERPRINT_DIR),
        min_n=int(min_n),
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def find_cocorrelated_genes(
    protein: str, top_k: int = 25, min_abs_r: float = 0.2, min_n: int = 100,
) -> str:
    """
    Top-k DepMap co-essential / anti-correlated genes for a target protein.

    Vectorised scan across all 18k+ DepMap genes. Useful for hypothesis
    generation: "what genes are most co-essential with KRAS?" surfaces
    candidate same-pathway partners (positive r) AND compensatory /
    synthetic-lethal candidates (negative r) in one query.

    For family-head inputs, only the dominant resolution is queried. Call
    again with specific paralogs to explore each separately.

    Args:
        protein:   Protein name. Resolved via HGNC + curated aliases.
        top_k:     Maximum number of partners to return. Default 25.
        min_abs_r: Minimum |Pearson r| to include. Default 0.2.
        min_n:     Minimum overlapping non-NaN cell lines. Default 100.
    """
    result = _find_cocorrelated_genes(
        protein=protein,
        top_k=int(top_k),
        min_abs_r=float(min_abs_r),
        min_n=int(min_n),
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def cluster_for_protein(protein: str) -> str:
    """
    Return the co-functional cluster that contains a protein.

    Resolves the protein name to a human gene symbol and looks it up in
    the persisted Louvain clustering of the literature graph (weighted by
    mentions * |DepMap r|). Returns the full cluster record with members,
    hub, internal/external edge counts, and max internal correlation.

    Use this when the user asks "what pathway / module is X part of?" or
    wants the consensus co-essential neighbourhood — complements
    `find_cocorrelated_genes` (top-K pairwise) which returns one gene's
    nearest neighbours rather than the whole module.

    Caveat: clusters reflect functional co-essentiality + literature
    co-mention. Pathway components with mutation-conditional essentiality
    (e.g. KRAS / BRAF) may land in different clusters even though they're
    in the same canonical pathway.

    Args:
        protein: Protein name, gene symbol, or alias (resolved via HGNC +
                 curated aliases, same as get_genetic_codependency).
    """
    result = _cluster_for_protein(protein)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def cluster_members(cluster_id: int) -> str:
    """
    Full record for one cluster by ID. Pair with cluster_for_protein when
    you want to enumerate the members of the cluster a query resolved to.

    Args:
        cluster_id: Numeric cluster ID from a previous cluster_for_protein
                    or find_clusters_by_keyword call.
    """
    result = _cluster_members(int(cluster_id))
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def find_clusters_by_keyword(query: str, max_results: int = 20) -> str:
    """
    List clusters whose member gene symbols contain a substring.

    Case-insensitive substring match — `"CDK"` finds clusters with
    CDK1/2/4/6, `"HDAC"` finds chromatin-modifier clusters. Use for
    hypothesis-led navigation: "show me the kinase clusters", "which
    clusters contain ribosomal genes?".

    Args:
        query:       Substring to match (case-insensitive). Required.
        max_results: Maximum number of clusters to return. Default 20.
    """
    result = _find_clusters_by_keyword(query, max_results=int(max_results))
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
