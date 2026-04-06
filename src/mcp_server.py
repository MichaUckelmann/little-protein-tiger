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


if __name__ == "__main__":
    mcp.run()
