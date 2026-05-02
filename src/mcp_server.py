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
    find_quantitative_evidence as _find_quantitative_evidence,
    get_interactions_for as _get_interactions_for,
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
def get_interactions_for(protein: str, depth: int = 1, min_mentions: int = 1) -> str:
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
    """
    result = _get_interactions_for(
        protein=protein,
        fingerprint_dir=Path(_FINGERPRINT_DIR),
        depth=int(depth),
        min_mentions=int(min_mentions),
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


if __name__ == "__main__":
    mcp.run()
