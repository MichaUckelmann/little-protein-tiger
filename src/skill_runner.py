"""
CLI-compatible agentic skill runner.

Loads a SKILL.md as the system prompt, runs the standard tool-calling loop,
and routes tool calls directly to Python functions — no MCP subprocess required.

Supports Claude (Anthropic SDK) and Gemini (REST API).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import time

import anthropic
import requests
from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(_ROOT))

from src.env_config import load_env  # noqa: E402
load_env(_ROOT / ".env")


from src._path_resolve import resolve as _resolve_path


def _resolve(file_path: str) -> str:
    """Resolve a file path against the project root (`_ROOT`).

    Thin wrapper around `src._path_resolve.resolve` — see that function's
    docstring for the full resolution order (existing-path fast path with
    root confinement, absolute-path tail-walk recovery, canonical-directory
    fallback, final rejoin-under-root). Kept as a local wrapper so call
    sites in this module don't need to thread `_ROOT` through everywhere.
    """
    return _resolve_path(file_path, root=_ROOT)


# ---------------------------------------------------------------------------
# AA normalisation (mirrors structure_tools_server.py)
# ---------------------------------------------------------------------------

_ONE_TO_THREE: dict[str, str] = {
    "A": "ALA", "R": "ARG", "N": "ASN", "D": "ASP", "C": "CYS",
    "Q": "GLN", "E": "GLU", "G": "GLY", "H": "HIS", "I": "ILE",
    "L": "LEU", "K": "LYS", "M": "MET", "F": "PHE", "P": "PRO",
    "S": "SER", "T": "THR", "W": "TRP", "Y": "TYR", "V": "VAL",
}


# ---------------------------------------------------------------------------
# Tool definitions — one source of truth, converted per-provider
# ---------------------------------------------------------------------------

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "search_corpus",
        "description": (
            "Semantically search the curated scientific literature corpus. "
            "Returns top-k paper fingerprints ranked by similarity, each with "
            "situational context, key quantitative findings (Kd, Ki), protein "
            "lists, DOI, study type, and study category. Use for proteins, "
            "mechanisms, assay results, inhibitors, or binding affinities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language query. Include protein names, mechanisms, "
                        "assay types, or quantitative terms for best results."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (1-20). Default 5.",
                },
                "study_type": {
                    "type": "string",
                    "description": "Optional methodology filter.",
                    "enum": [
                        "experimental_in_vitro", "experimental_in_vivo",
                        "experimental_structural", "computational", "review", "case_study",
                    ],
                },
                "study_category": {
                    "type": "string",
                    "description": (
                        "Optional domain filter. 'pathway_biology' for disease mechanism "
                        "papers; 'biochemistry' for binding assay papers."
                    ),
                    "enum": [
                        "biochemistry", "pathway_biology", "structural_biology",
                        "host_pathogen", "clinical", "review",
                    ],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_fingerprint",
        "description": (
            "Retrieve the complete fingerprint for a specific paper by DOI or paper_key. "
            "Returns all key findings, methodology, contradictions, and entity lists. "
            "Use after search_corpus identifies a paper of interest."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": (
                        "DOI (e.g. '10.1021/jacs.5c12876') or paper_key "
                        "(e.g. 'doi:10.1021/jacs.5c12876'). Both forms accepted."
                    ),
                },
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "find_pdb_structures",
        "description": (
            "Search the entire corpus fingerprint database for PDB structure accessions "
            "linked to specific proteins. Checks two sources across ALL fingerprints: "
            "(1) pathway_context.target_nodes.suggested_pdb_structures — PDB IDs curators "
            "associated with a target protein in pathway biology papers; "
            "(2) paper_metadata.pdb_accessions — PDB IDs mentioned in any paper where "
            "the protein appears in key findings or entity lists. "
            "Call once with all candidate target proteins before writing the PIPELINE HANDOFF. "
            "Use returned IDs verbatim — they are corpus-sourced, never hallucinated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "proteins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Gene symbols of proteins to look up, e.g. ['YAP1', 'TEAD4', 'NF2']. "
                        "Include all proteins from all candidate PPIs in one call."
                    ),
                },
            },
            "required": ["proteins"],
        },
    },
    {
        "name": "search_rcsb_pdb",
        "description": (
            "Search RCSB PDB for structures containing specific proteins. "
            "Use as a fallback ONLY when find_pdb_structures returns total_found=0 "
            "(corpus has no PDB IDs for your target proteins). "
            "Performs a full-text search on RCSB and returns up to 5 entries per protein "
            "with title, method, resolution, chain count, and entity descriptions. "
            "Results are NOT corpus-sourced — check entity descriptions to confirm "
            "the complex you want is actually present before using a PDB ID."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "proteins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Gene symbols of proteins to search for, e.g. ['YAP1', 'TEAD4']. "
                        "Focus on the primary target proteins — 2–3 gene symbols is sufficient."
                    ),
                },
            },
            "required": ["proteins"],
        },
    },
    {
        "name": "resolve_protein_identifier",
        "description": (
            "Resolve a gene symbol, protein name or alias to its human UniProt "
            "accession, offline from the bundled ID mapping. Use to confirm what "
            "a named target actually is before looking for its structures."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Gene symbol or protein name, e.g. KRAS"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "find_complex_structures",
        "description": (
            "PDB entries containing a given UniProt accession TOGETHER WITH at "
            "least one other protein entity, with per-chain entity descriptions, "
            "lengths and accessions. Unlike search_rcsb_pdb (full-text only) this "
            "is a structured query and can express 'a complex containing P01116'. "
            "Use when the supplied candidate table is empty or you suspect the "
            "complex you need is missing from it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "uniprot": {"type": "string", "description": "UniProt accession, e.g. P01116"},
                "rows": {"type": "integer", "description": "Max entries to return (default 25)"},
            },
            "required": ["uniprot"],
        },
    },
    {
        "name": "tool_find_glue_pockets",
        "description": (
            "Paired peri-interface pockets flanking an interface edge — the "
            "molecular-glue mode. Returns candidate pocket pairs a small binder "
            "could bridge, with per-pocket SASA and bridge span."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to structure file"},
                "chain_a": {"type": "string", "description": "Chain ID of first chain"},
                "chain_b": {"type": "string", "description": "Chain ID of second chain"},
                "periinterface_radius": {"type": "number", "description": "Å from the interface edge (default 10)"},
                "top_n": {"type": "integer", "description": "Number of pocket pairs (default 3)"},
            },
            "required": ["file_path", "chain_a", "chain_b"],
        },
    },
    {
        "name": "tool_analyze_interface",
        "description": (
            "Full interface analysis between two chains of a structure file. "
            "Computes BSA (total + per-residue), interface residues with type "
            "classification, H-bonds with geometry, pairwise contact map with "
            "interaction classification, gap residue flags, and pLDDT scores. "
            "Accepts .cif and .pdb files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to structure file (.cif or .pdb)"},
                "chain_a":   {"type": "string", "description": "Chain ID of first chain"},
                "chain_b":   {"type": "string", "description": "Chain ID of second chain"},
                "cutoff":    {"type": "number", "description": "Heavy-atom distance cutoff in Å (default 4.5)"},
            },
            "required": ["file_path", "chain_a", "chain_b"],
        },
    },
    {
        "name": "tool_get_residue_contacts",
        "description": (
            "All contacts between a single residue and a partner chain within the cutoff. "
            "Returns per-contact distances, interaction classification, H-bond details, "
            "and gap flag."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path":     {"type": "string", "description": "Absolute path to structure file"},
                "chain":         {"type": "string", "description": "Chain ID containing the residue"},
                "resnum":        {"type": "integer", "description": "Residue number (auth_seq_id)"},
                "partner_chain": {"type": "string", "description": "Chain ID to check contacts against"},
                "cutoff":        {"type": "number", "description": "Heavy-atom distance cutoff in Å (default 4.5)"},
            },
            "required": ["file_path", "chain", "resnum", "partner_chain"],
        },
    },
    {
        "name": "tool_check_mutation_clash",
        "description": (
            "Estimates whether a point mutation would clash with the partner chain. "
            "Uses a Cβ heuristic. Returns clash severity: none / minor / major. "
            "Accepts 1-letter or 3-letter amino acid codes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path":     {"type": "string", "description": "Absolute path to structure file"},
                "chain":         {"type": "string", "description": "Chain ID of the residue to mutate"},
                "resnum":        {"type": "integer", "description": "Residue number (auth_seq_id)"},
                "new_aa":        {"type": "string", "description": "Proposed amino acid (1-letter or 3-letter)"},
                "partner_chain": {"type": "string", "description": "Chain ID to check clashes against"},
            },
            "required": ["file_path", "chain", "resnum", "new_aa", "partner_chain"],
        },
    },
    {
        "name": "tool_get_sequence_map",
        "description": (
            "Returns the amino acid sequence of a chain with numbering maps for AF3 JSON. "
            "Provides: 1-letter sequence string, auth_seq_id → string index map, "
            "and auth_seq_id → label_seq_id map."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to structure file"},
                "chain":     {"type": "string", "description": "Chain ID"},
            },
            "required": ["file_path", "chain"],
        },
    },
    {
        "name": "tool_score_surface_patch",
        "description": (
            "Characterises a set of residues as a potential binding surface. "
            "Computes spatial spread (Cα RMSD), mean KD hydrophobicity, residue "
            "type breakdown, hydrophobic fraction, and a suitability rating "
            "(Excellent / Good / Marginal / Poor)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path":    {"type": "string", "description": "Absolute path to structure file"},
                "chain":        {"type": "string", "description": "Chain ID of the target surface"},
                "residue_list": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of residue numbers (auth_seq_id) defining the patch",
                },
            },
            "required": ["file_path", "chain", "residue_list"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write text content to a file on disk. Use this to create BoltzGen YAMLs, "
            "RFD3 JSONs, SLURM submission scripts, and any other output files. "
            "Parent directories are created automatically. Returns the absolute path "
            "written so you can confirm the file was created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Destination file path. Use the output directory supplied in "
                        "the task description. Relative paths are resolved against the "
                        "project root."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full text content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "get_interactions_for",
        "description": (
            "Aggregate the interaction partners of a protein across the entire corpus. "
            "Walks key_findings[].protein_pair in every fingerprint and returns a "
            "deduplicated partner list with mention counts, supporting DOIs, and any "
            "quantitative anchors (Kd / Ki) from the same key_findings entry. "
            "Reach for this tool early on 'which proteins interact with X?' questions — "
            "search_corpus misses the long tail because top-k is small. Aliases are "
            "normalised (YAP matches YAP1 / hYAP); paralogs stay distinct."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "protein": {
                    "type": "string",
                    "description": "Protein name (gene symbol or common name).",
                },
                "depth": {
                    "type": "integer",
                    "description": (
                        "1 (default) for direct partners only; 2 to also return "
                        "partners-of-partners (capped at 50)."
                    ),
                },
                "min_mentions": {
                    "type": "integer",
                    "description": (
                        "Drop partners mentioned fewer than this many times across "
                        "the corpus. Default 1."
                    ),
                },
                "human_only": {
                    "type": "boolean",
                    "description": (
                        "Default true — partner list is restricted to human-"
                        "resolvable proteins (covers human-native and ortholog-"
                        "mapped). Pass false for host-pathogen, yeast, bacterial, "
                        "or comparative biology queries."
                    ),
                },
                "taxa": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Optional NCBI taxon allow-list, e.g. [9606, 10090] for "
                        "human + mouse comparative work. Overrides human_only."
                    ),
                },
            },
            "required": ["protein"],
        },
    },
    {
        "name": "find_quantitative_evidence",
        "description": (
            "Pull every key_findings entry with a measured Kd or Ki for a specific "
            "protein pair, sorted tightest-binder first. Use when the user asks for "
            "the affinity of a specific pair or wants to know what's been measured "
            "experimentally. Pair matching is order-insensitive. ΔΔG is not currently "
            "captured by the schema and cannot be queried."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "protein_pair": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exactly two protein names. Order-insensitive — "
                        "['KRAS','RAF1'] matches stored ['RAF1','KRAS']."
                    ),
                },
                "metric": {
                    "type": "string",
                    "description": "Which metric to require non-null. Default 'Kd'.",
                    "enum": ["Kd", "Ki", "both"],
                },
            },
            "required": ["protein_pair"],
        },
    },
    {
        "name": "shortest_interaction_path",
        "description": (
            "Top k shortest paths between two proteins in the corpus interaction "
            "graph. Each edge carries mention count, supporting DOIs, and tightest "
            "measured Kd/Ki. Returns min_mentions_along_path and weak_links_count "
            "so you can flag low-confidence steps. Use for 'is X connected to Y?' "
            "or 'draw the cascade from X to Y' questions. Use get_interactions_for "
            "instead for 'what does X bind?'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "protein_a": {"type": "string", "description": "Source protein."},
                "protein_b": {"type": "string", "description": "Target protein."},
                "max_hops":   {"type": "integer", "description": "Maximum path length in edges. Default 4."},
                "k":          {"type": "integer", "description": "Number of distinct shortest paths to return. Default 1."},
                "human_only": {"type": "boolean", "description": "Default true — restrict path intermediates to human-resolvable proteins. Source/target are exempt."},
                "taxa":       {"type": "array", "items": {"type": "integer"}, "description": "Optional NCBI taxon allow-list for intermediates. Overrides human_only."},
            },
            "required": ["protein_a", "protein_b"],
        },
    },
    {
        "name": "interaction_hubs",
        "description": (
            "Highest-degree proteins in the corpus interaction graph (degree "
            "filtered by min_mentions per edge so single-paper noise doesn't "
            "inflate rank). Returns sample partners and DOIs per hub. Caveat: "
            "hub rank is a research-attention proxy, not biological importance — "
            "well-studied proteins (KRAS, p53, EGFR) dominate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "top_n":        {"type": "integer", "description": "Number of hubs to return. Default 20."},
                "min_mentions": {"type": "integer", "description": "Edge mention threshold. Default 3."},
                "human_only":   {"type": "boolean", "description": "Default true — only human-resolvable nodes are eligible hubs. Set false for non-mammalian."},
                "taxa":         {"type": "array", "items": {"type": "integer"}, "description": "Optional NCBI taxon allow-list (overrides human_only)."},
            },
        },
    },
    {
        "name": "novelty_signal",
        "description": (
            "Deterministic corpus-coverage novelty score for a candidate target. "
            "Higher score (closer to 1.0) means less prior art in the corpus. "
            "Returns the four input counts (mentions, pdb_papers, prior_targeting, "
            "quantitative_findings) alongside the score so a reviewer can recompute "
            "by hand. Use during wildcard-expert Phase 2.5 to triage candidates: "
            "well-characterised targets (TP53, KRAS, YAP1) score near 0.0; novel "
            "or under-explored proteins score near 1.0. Do NOT hard-threshold — "
            "paralog-substring matching can inflate counts for short queries; "
            "check the caveats field."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "protein": {"type": "string", "description": "Gene symbol or protein name. Queries < 2 chars after alias normalisation are rejected."},
            },
            "required": ["protein"],
        },
    },
    {
        "name": "export_subgraph",
        "description": (
            "Write a depth-bounded interaction neighbourhood around seed proteins "
            "to disk as Cytoscape.js JSON for visual exploration. Each seed expands "
            "to all matching nodes (seed 'TEAD' pulls TEAD1/2/3/4). The graph goes "
            "to disk, not the conversation — only a small confirmation dict is "
            "returned. Use when the user asks for a visual / external view of an "
            "interaction neighbourhood. Optional `with_depmap=True` attaches "
            "DepMap co-essentiality (Pearson r) to every edge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "seeds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Protein names to seed the BFS from.",
                },
                "output_path": {
                    "type": "string",
                    "description": "Destination .cyjs file. Relative paths resolve to project root.",
                },
                "depth":        {"type": "integer", "description": "BFS depth in edges. Default 1."},
                "max_nodes":    {"type": "integer", "description": "Hard cap on nodes. Default 200."},
                "with_depmap":  {"type": "boolean", "description": "Attach DepMap r/n to each edge. Default False."},
                "depmap_min_n": {"type": "integer", "description": "Minimum overlapping cell lines per correlation. Default 100."},
                "human_only":   {"type": "boolean", "description": "Default true — BFS only steps into human-resolvable partners. Set false for host-pathogen exploration."},
                "taxa":         {"type": "array", "items": {"type": "integer"}, "description": "Optional NCBI taxon allow-list (overrides human_only)."},
            },
            "required": ["seeds", "output_path"],
        },
    },
    {
        "name": "get_genetic_codependency",
        "description": (
            "Pearson correlation of CRISPR essentiality between two proteins "
            "(DepMap Chronos scores). Use this on top of literature-graph "
            "queries to corroborate functional relationships: high |r| "
            "supports a real pathway link, near-zero r suggests the "
            "interaction is mutation-conditional or not a fitness-shared "
            "module. Sign carries meaning — positive r = co-essential "
            "(same pathway / heterodimer); negative r often = compensatory "
            "or synthetic-lethal-style. Family-head names (AKT, RPA) "
            "expand to all paralogs; tightest |r| is reported."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "protein_a": {"type": "string", "description": "First protein. Order-insensitive."},
                "protein_b": {"type": "string", "description": "Second protein."},
                "min_n":     {"type": "integer", "description": "Minimum overlapping cell lines. Default 100."},
            },
            "required": ["protein_a", "protein_b"],
        },
    },
    {
        "name": "cluster_for_protein",
        "description": (
            "Return the co-functional cluster that contains a protein. "
            "Clusters are Louvain communities on the literature graph "
            "weighted by mentions * |DepMap r| — they reflect functional "
            "co-essentiality plus literature co-mention. Use for "
            "'what pathway / module is X in?' or 'show me the co-essential "
            "neighbourhood of X' queries. Complements find_cocorrelated_genes "
            "(top-K pairwise) by giving the consensus module."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "protein": {"type": "string", "description": "Protein name / gene symbol / alias."},
            },
            "required": ["protein"],
        },
    },
    {
        "name": "cluster_members",
        "description": (
            "Full record for one cluster by numeric ID — members, hub, "
            "internal/external edge counts, max internal r. Pair with "
            "cluster_for_protein when you have a cluster ID and want "
            "the full member list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "integer", "description": "Numeric cluster ID."},
            },
            "required": ["cluster_id"],
        },
    },
    {
        "name": "find_clusters_by_keyword",
        "description": (
            "List clusters whose member gene symbols contain a substring "
            "(case-insensitive). Useful for hypothesis-led navigation — "
            "'show me kinase clusters' (query='kinase' won't work; query='CDK' "
            "or query='MAP' will), 'clusters containing HDAC genes', etc. "
            "Sorted by cluster size descending."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Substring to match in member gene symbols."},
                "max_results": {"type": "integer", "description": "Maximum clusters to return. Default 20."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_cocorrelated_genes",
        "description": (
            "Top-k DepMap co-essential / anti-correlated genes for a target "
            "protein. Vectorised scan across 18k+ DepMap genes. Surfaces "
            "candidate same-pathway partners (positive r) AND compensatory / "
            "synthetic-lethal candidates (negative r) in one call. Use for "
            "hypothesis generation: 'which genes are most co-essential with "
            "KRAS?' is a single tool call away. Family-head inputs only "
            "query the dominant resolution; call again with paralogs to "
            "explore each separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "protein":   {"type": "string", "description": "Target protein. Resolved via HGNC + curated aliases."},
                "top_k":     {"type": "integer", "description": "Maximum partners to return. Default 25."},
                "min_abs_r": {"type": "number",  "description": "Minimum |Pearson r| to include. Default 0.2."},
                "min_n":     {"type": "integer", "description": "Minimum overlapping cell lines. Default 100."},
            },
            "required": ["protein"],
        },
    },
]


# Only design/optimizer skills should write files — analysis skills produce
# their output as return text which PipelineRunner writes to disk.
# Restricting at the schema level is more reliable than prompt instructions alone.
# Skills that work purely from their context_text and emit a markdown report
# — no MCP tools should be exposed, since extras just confuse the model and
# waste tokens. Add a skill here when it has no genuine tool needs.
_NO_TOOL_SKILLS = {"design-analyst"}

_WRITE_FILE_SKILLS = {"protein-design-script", "binder-optimizer"}

# Skills that need the full residue index maps for AF3/BoltzGen JSON construction
_NEEDS_INDEX_MAPS = {"protein-design-script", "binder-optimizer",
                     "complex-structure-analysis", "binder-target-intel"}

# Skills that have access to the corpus-wide PDB lookup tool. wildcard-expert's
# Phase 4.6 calls find_pdb_structures — without the allowlist entry the call
# was being silently filtered out of the tool surface and the skill emitted
# NOT_FOUND more often than it should.
_PDB_LOOKUP_SKILLS = {"pathway-expert", "wildcard-expert", "complex-expert", "orchestrator",
                      "binder-target-intel"}

# Skills that have access to the NetworkX-backed graph tools (path, hubs, export,
# novelty). Other skills don't need them and shouldn't pay the system-prompt
# overhead. wildcard-expert is in the set because its Phase 2.5 (graph-driven
# novelty triage) requires interaction_hubs + shortest_interaction_path +
# novelty_signal.
_GRAPH_TOOL_SKILLS = {"corpus-explorer", "pathway-expert", "molecular-biology-expert", "wildcard-expert"}
_GRAPH_TOOLS = {
    "shortest_interaction_path", "interaction_hubs", "export_subgraph",
    "novelty_signal",
    "get_genetic_codependency", "find_cocorrelated_genes",
    "cluster_for_protein", "cluster_members", "find_clusters_by_keyword",
}

# Structured target resolution for the binder track: gene symbol -> UniProt
# (offline) and UniProt -> PDB complexes. search_rcsb_pdb is full-text only and
# cannot express "two protein entities, one of which is P01116".
_IDENTIFIER_TOOL_SKILLS = {"binder-target-intel"}
_IDENTIFIER_TOOLS = {"resolve_protein_identifier", "find_complex_structures"}

# find_glue_pockets is what complex-structure-analysis STABILIZE mode is told to
# call; it existed on the MCP server and in structure_tools but was missing from
# _TOOL_DEFS, so the in-process transport silently had no such tool.
_GLUE_TOOL_SKILLS = {"complex-structure-analysis", "binder-target-intel"}

_RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
_RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"
_RCSB_GRAPHQL_QUERY = """
query EntriesMetadata($ids: [String!]!) {
  entries(entry_ids: $ids) {
    rcsb_id
    struct { title }
    rcsb_entry_info {
      resolution_combined
      experimental_method
      polymer_entity_count_protein
    }
    polymer_entities {
      rcsb_polymer_entity { pdbx_description }
      rcsb_entity_source_organism { ncbi_taxonomy_id ncbi_scientific_name }
    }
  }
}
"""


def _parse_rcsb_entry(entry: dict) -> dict:
    """Distil an RCSB GraphQL entry to selection-relevant fields."""
    info = entry.get("rcsb_entry_info") or {}
    res_list = [r for r in (info.get("resolution_combined") or []) if r is not None]
    return {
        "title": (entry.get("struct") or {}).get("title"),
        "method": info.get("experimental_method"),
        "resolution_A": round(min(res_list), 2) if res_list else None,
        "protein_chain_count": info.get("polymer_entity_count_protein") or 0,
        "entities": [
            {
                "description": (pe.get("rcsb_polymer_entity") or {}).get("pdbx_description") or "",
                "organism_taxid": ((pe.get("rcsb_entity_source_organism") or [{}])[0]).get("ncbi_taxonomy_id"),
                "organism_name": ((pe.get("rcsb_entity_source_organism") or [{}])[0]).get("ncbi_scientific_name"),
            }
            for pe in (entry.get("polymer_entities") or [])
        ],
    }


def _search_rcsb_pdb(proteins: list[str], fingerprint_dir: Path) -> dict:
    """
    Search RCSB PDB for structures containing the given proteins.

    Uses RCSB full-text search (one request per protein, top 5 results each),
    then batch-fetches title/method/resolution/chain metadata via GraphQL for
    any IDs not already in the local pdb_metadata.json cache.

    Call this as a fallback when find_pdb_structures returns total_found=0.
    Results are from RCSB, not the corpus — verify entity descriptions match
    your target complex before using a PDB ID.
    """
    metadata_cache: dict[str, dict] = {}
    metadata_path = fingerprint_dir.parent / "pdb_metadata.json"
    if metadata_path.exists():
        try:
            metadata_cache = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    queries = [p.strip() for p in proteins if p.strip()]
    by_protein: dict[str, list[str]] = {}

    for protein in queries:
        # Search across title, keywords, and entity description for broad coverage
        payload = {
            "query": {
                "type": "group",
                "logical_operator": "or",
                "nodes": [
                    {"type": "terminal", "service": "text", "parameters": {
                        "attribute": "struct.title", "operator": "contains_words", "value": protein}},
                    {"type": "terminal", "service": "text", "parameters": {
                        "attribute": "struct_keywords.text", "operator": "contains_words", "value": protein}},
                    {"type": "terminal", "service": "text", "parameters": {
                        "attribute": "rcsb_polymer_entity.pdbx_description", "operator": "contains_words", "value": protein}},
                ],
            },
            "return_type": "entry",
            "request_options": {"paginate": {"start": 0, "rows": 5}},
        }
        try:
            resp = requests.post(_RCSB_SEARCH_URL, json=payload, timeout=15)
            resp.raise_for_status()
            ids = [hit["identifier"] for hit in resp.json().get("result_set", [])]
            by_protein[protein] = ids
        except Exception as e:
            logger.warning(f"[search_rcsb_pdb] {protein}: {e}")
            by_protein[protein] = []

    # Batch-fetch metadata for IDs not already in local cache
    new_ids = list(dict.fromkeys(
        pid for ids in by_protein.values() for pid in ids
        if pid not in metadata_cache
    ))
    if new_ids:
        try:
            resp = requests.post(
                _RCSB_GRAPHQL_URL,
                json={"query": _RCSB_GRAPHQL_QUERY, "variables": {"ids": new_ids}},
                timeout=30,
            )
            resp.raise_for_status()
            for entry in (resp.json().get("data") or {}).get("entries") or []:
                metadata_cache[entry["rcsb_id"]] = _parse_rcsb_entry(entry)
        except Exception as e:
            logger.warning(f"[search_rcsb_pdb] GraphQL batch fetch failed: {e}")

    def _enrich(pdb_id: str) -> dict:
        result: dict = {"pdb_id": pdb_id}
        result.update(metadata_cache.get(pdb_id, {}))
        return result

    return {
        "by_protein": {p: [_enrich(pid) for pid in ids] for p, ids in by_protein.items()},
        "total_found": sum(len(ids) for ids in by_protein.values()),
        "note": (
            "Results from RCSB full-text search — not corpus-sourced. "
            "Check entity descriptions to confirm your target proteins are present "
            "and apply the same selection criteria as for corpus results."
        ),
    }


def _filter_tools(defs: list[dict], skill_name: str) -> list[dict]:
    """Return the tool list for a given skill, removing tools the skill shouldn't have."""
    if skill_name in _NO_TOOL_SKILLS:
        return []
    if skill_name not in _WRITE_FILE_SKILLS:
        defs = [d for d in defs if d["name"] != "write_file"]
    if skill_name not in _PDB_LOOKUP_SKILLS:
        defs = [d for d in defs if d["name"] != "find_pdb_structures"]
        defs = [d for d in defs if d["name"] != "search_rcsb_pdb"]
    if skill_name not in _GRAPH_TOOL_SKILLS:
        defs = [d for d in defs if d["name"] not in _GRAPH_TOOLS]
    if skill_name not in _IDENTIFIER_TOOL_SKILLS:
        defs = [d for d in defs if d["name"] not in _IDENTIFIER_TOOLS]
    if skill_name not in _GLUE_TOOL_SKILLS:
        defs = [d for d in defs if d["name"] != "tool_find_glue_pockets"]
    return defs


def _find_pdb_structures(proteins: list[str], fingerprint_dir: Path) -> dict:
    """
    Scan all corpus fingerprints for PDB accessions associated with the given proteins.

    Two sources are checked for each protein:
    - pathway_context.target_nodes[].suggested_pdb_structures  (pathway biology papers)
    - paper_metadata.pdb_accessions of papers where the protein appears in
      entities.proteins or key_findings.protein_pair

    Matching is case-insensitive substring: query "YAP1" matches stored "YAP1",
    stored "YAP/TAZ", etc. Queries shorter than 3 chars are ignored.
    """
    import re as _re

    def _matches(stored: str, query: str) -> bool:
        if len(query) < 3:
            return False
        s, q = stored.upper(), query.upper()
        return q in s or s.startswith(q)

    queries = [p.strip() for p in proteins if p.strip()]
    # {query → ordered list of PDB IDs}
    by_protein: dict[str, list[str]] = {q: [] for q in queries}

    for fp_file in fingerprint_dir.glob("*.json"):
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Only valid 4-char PDB IDs from paper_metadata
        pdb_accessions = [
            p for p in (fp.get("paper_metadata", {}).get("pdb_accessions") or [])
            if p and len(p) == 4
        ]
        target_nodes = (fp.get("pathway_context") or {}).get("target_nodes") or []
        entity_proteins = fp.get("entities", {}).get("proteins") or []
        key_findings = fp.get("key_findings") or []

        for query in queries:
            hits: set[str] = set()

            # Source 1: pathway_context.target_nodes.suggested_pdb_structures
            for node in target_nodes:
                if _matches(node.get("protein", ""), query):
                    hits.update(
                        p for p in (node.get("suggested_pdb_structures") or []) if p
                    )

            # Source 2: pdb_accessions from papers mentioning this protein
            if pdb_accessions:
                in_entities = any(_matches(ep, query) for ep in entity_proteins)
                in_findings = any(
                    any(_matches(pp, query) for pp in (kf.get("protein_pair") or []) if pp)
                    for kf in key_findings
                )
                if in_entities or in_findings:
                    hits.update(pdb_accessions)

            # Merge deduplicating while preserving order
            seen = set(by_protein[query])
            for h in sorted(hits):
                if h not in seen:
                    by_protein[query].append(h)
                    seen.add(h)

    # Load RCSB metadata cache if available
    metadata_cache: dict[str, dict] = {}
    metadata_path = fingerprint_dir.parent / "pdb_metadata.json"
    if metadata_path.exists():
        try:
            metadata_cache = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _enrich(pdb_id: str) -> dict:
        meta = metadata_cache.get(pdb_id.upper(), {})
        result: dict = {"pdb_id": pdb_id}
        if meta:
            result["title"] = meta.get("title")
            result["method"] = meta.get("method")
            result["resolution_A"] = meta.get("resolution_A")
            result["protein_chain_count"] = meta.get("protein_chain_count")
            result["entities"] = [
                {
                    "description": e.get("description"),
                    "organism_taxid": e.get("organism_taxid"),
                    "organism_name": e.get("organism_name"),
                }
                for e in (meta.get("entities") or [])
            ]
        return result

    all_ids = sorted({p for ids in by_protein.values() for p in ids})

    def _named_in_metadata(entry: dict, query: str) -> bool:
        """Does RCSB's own metadata actually name the protein we asked for?"""
        blob = f"{entry.get('title', '')} {entry.get('entities', '')}".upper()
        q = query.upper()
        # "YAP1" should match an entity called "Transcriptional coactivator YAP1"
        # and also the "YAP" of a YAP/TAZ construct.
        return q in blob or (len(q) > 3 and q[:-1] in blob)

    def _rank(entries: list[dict], query: str) -> list[dict]:
        """Confirmed hits first, and say which is which.

        A corpus paper's `pdb_accessions` now includes structures it merely
        CITES, not only ones it deposited — higher recall, lower precision. For
        "YAP1" that means 41 hits of which RCSB's metadata names YAP in exactly
        1; the rest are methods references from papers that mention YAP1 in
        passing. Unranked, a model sees "De novo designed TIM barrel" first and
        has to reason its way past four irrelevant entries.
        """
        for e in entries:
            e["query_named_in_metadata"] = _named_in_metadata(e, query)
        return sorted(entries, key=lambda e: not e["query_named_in_metadata"])

    return {
        "by_protein": {q: _rank([_enrich(pid) for pid in ids], q)
                       for q, ids in by_protein.items()},
        "all_pdb_ids": all_ids,
        "total_found": len(all_ids),
        "metadata_available": bool(metadata_cache),
        "note": (
            "Entries where RCSB's own metadata names the queried protein are "
            "listed FIRST and flagged query_named_in_metadata=true. The rest "
            "come from papers that cite the structure without it being the "
            "paper's subject — usable, but verify the entity descriptions "
            "before designing against one."
        ),
    }


def _to_claude_tools(defs: list[dict]) -> list[dict]:
    tools = [
        {"name": d["name"], "description": d["description"], "input_schema": d["parameters"]}
        for d in defs
    ]
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def _to_gemini_tools(defs: list[dict]) -> list[dict]:
    return [
        {
            "functionDeclarations": [
                {"name": d["name"], "description": d["description"], "parameters": d["parameters"]}
                for d in defs
            ]
        }
    ]


# ---------------------------------------------------------------------------
# SkillRunner
# ---------------------------------------------------------------------------

_GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


class SkillRunnerError(RuntimeError):
    """Misconfiguration caught before any API call — a missing key, say.

    Distinct from `SkillRefusedError`: nothing was sent, nothing was billed,
    and no model fallback can help. The message is meant to be read by a user
    on a fresh clone, so it names the env var and the file to put it in.
    """


class SkillRefusedError(RuntimeError):
    """A safety classifier declined the request; no content was returned."""

    def __init__(self, *, skill: str, model: str, category: str | None,
                 iteration: int):
        self.skill = skill
        self.model = model
        self.category = category
        self.iteration = iteration
        super().__init__(
            f"{model} refused the {skill!r} request on call #{iteration}"
            + (f" (category: {category})" if category else "")
            + " — no content returned")


class SkillRunner:
    """
    Runs one skill against Claude or Gemini via an agentic tool-calling loop.

    Parameters
    ----------
    skill_name : str
        Directory name under ``skills/``.
    provider : str
        ``"claude"`` or ``"gemini"``.
    model_id : str
        Model identifier passed directly to the API.
    config : dict
        Parsed ``config.yaml`` — used for data paths.
    max_iter : int
        Hard cap on LLM calls per run (default 30).
    """

    def __init__(
        self,
        skill_name: str,
        provider: str,
        model_id: str,
        config: dict,
        max_iter: int = 30,
        max_input_tokens: int = 100_000,
        use_extended_thinking: bool = False,
    ) -> None:
        self.skill_name = skill_name
        self.provider = provider
        self.model_id = model_id
        self.config = config
        self.max_iter = max_iter
        self.max_input_tokens = max_input_tokens
        # Extended thinking: Claude only, ignored silently for Gemini.
        # Adaptive (the model picks its own depth); steered via
        # output_config.effort.  See _run_claude.
        self.use_extended_thinking = use_extended_thinking and provider == "claude"

        # Token usage tracking — populated during run().  The four buckets are
        # priced differently (cache reads ~0.1x, cache writes ~1.25x), so they
        # are tracked separately rather than folded into one input total.
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cache_creation_tokens: int = 0
        self._total_cache_read_tokens: int = 0
        # Most recent call's input-token count, for interactive-mode warnings.
        self._last_input_tokens: int = 0

        # Resolve data paths (relative → absolute from project root)
        fp_dir = config.get("paths", {}).get("fingerprint_dir", "data/fingerprints")
        self._fingerprint_dir = (
            Path(fp_dir) if Path(fp_dir).is_absolute() else _ROOT / fp_dir
        )

        vs_path = config.get("vector_store", {}).get("db_path", "data/vectors")
        self._vector_db_path = str(
            Path(vs_path) if Path(vs_path).is_absolute() else _ROOT / vs_path
        )

        self._embedding_model = config.get("vector_store", {}).get(
            "embedding_model", "NeuML/pubmedbert-base-embeddings"
        )

        self._store = None  # lazy — sentence-transformers is slow to import

        # Populated after run() completes — full conversation history for tracing.
        self._messages: list[dict] | None = None

        self._require_api_key()
        self.system_prompt = self._load_system_prompt()
        logger.info(
            f"SkillRunner ready: skill={skill_name}, provider={provider}, model={model_id}"
            + (" [extended thinking]" if self.use_extended_thinking else "")
        )

    _PROVIDER_KEYS = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}

    def _require_api_key(self) -> None:
        """Fail early and legibly when the provider's key isn't configured.

        Without this the missing key surfaces as the provider's own error at
        the first LLM call — for Gemini, `403 Forbidden for url: ...?key=` with
        an empty key, which names nothing about LPT or `.env`. Gemini is the
        default provider for every stage, so this is the very first wall a
        fresh clone hits, and on the binder track it lands only AFTER structure
        download and interface analysis have already run.
        """
        var = self._PROVIDER_KEYS.get(self.provider)
        if var is None or os.environ.get(var):
            return          # local/Ollama needs no key
        raise SkillRunnerError(
            f"{var} is not set, but the {self.provider!r} provider needs it "
            f"(skill={self.skill_name}, model={self.model_id}).\n"
            f"Add it to .env at the project root:\n"
            f"    {var}=...\n"
            f"See .env.example for which key each provider and workflow needs. "
            f"To use a different provider instead, pass --provider."
        )

    # ------------------------------------------------------------------
    # System prompt loading
    # ------------------------------------------------------------------

    def _load_system_prompt(self) -> str:
        skills_root = _ROOT / "skills"
        skill_path = skills_root / self.skill_name / "SKILL.md"
        if not skill_path.exists():
            raise FileNotFoundError(f"SKILL.md not found: {skill_path}")

        system = skill_path.read_text(encoding="utf-8")

        if self.skill_name == "orchestrator":
            for sub_dir in sorted(skills_root.iterdir()):
                if sub_dir.name == "orchestrator" or not sub_dir.is_dir():
                    continue
                sub_md = sub_dir / "SKILL.md"
                if sub_md.exists():
                    system += (
                        f"\n\n---\n## SUB-SKILL: {sub_dir.name}\n"
                        + sub_md.read_text(encoding="utf-8")
                    )
            logger.info("Orchestrator mode: sub-skill SKILL.mds appended to system prompt")

        return system

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _get_store(self):
        if self._store is None:
            from src.vector_store import VectorStore
            logger.info("Initialising VectorStore (lazy, first search_corpus call)…")
            self._store = VectorStore(
                db_path=self._vector_db_path,
                embedding_model=self._embedding_model,
            )
        return self._store

    # Hard ceiling on a single tool result, in characters (~4 chars/token).
    # analyze_interface on a large complex returns per-residue contact lists for
    # every interface residue; on TREM2 and KRAS that grew the conversation
    # 47k -> 84k -> 134k tokens in three calls and blew the per-call input limit.
    # A result the model cannot read is worse than a truncated one it can.
    MAX_TOOL_RESULT_CHARS = 60_000

    def _execute_tool(self, name: str, input_dict: dict) -> str:
        raw = self._execute_tool_inner(name, input_dict)
        if len(raw) <= self.MAX_TOOL_RESULT_CHARS:
            return raw
        kept = raw[: self.MAX_TOOL_RESULT_CHARS]
        logger.warning(
            f"tool {name} returned {len(raw):,} chars (~{len(raw) // 4:,} tokens) "
            f"— truncated to {self.MAX_TOOL_RESULT_CHARS:,}. Narrow the query "
            f"(fewer chains, a smaller cutoff) for the full result.")
        return (
            kept
            + f"\n\n... [TRUNCATED: this result was {len(raw):,} characters, over "
              f"the {self.MAX_TOOL_RESULT_CHARS:,} limit. The summary fields above "
              f"are complete; per-residue detail was cut. Re-run the tool on a "
              f"single chain pair or a tighter cutoff if you need the rest.]"
        )

    def _execute_tool_inner(self, name: str, input_dict: dict) -> str:
        try:
            if name == "search_corpus":
                raw = self._get_store().execute_search_tool(input_dict)
                # Strip the 'papers' JSON array — it duplicates the formatted result_text
                # and can add 40k+ tokens when top_k is large. The model reads result_text
                # and calls get_fingerprint for papers it wants full details on.
                try:
                    parsed = json.loads(raw)
                    parsed.pop("papers", None)
                    return json.dumps(parsed, ensure_ascii=False)
                except (json.JSONDecodeError, AttributeError):
                    return raw

            if name == "get_fingerprint":
                from src.fingerprint_store import load_fingerprint
                identifier = input_dict.get("identifier", "")
                if not identifier.startswith("doi:") and not identifier.startswith("pmcid:"):
                    paper_key = f"doi:{identifier}"
                else:
                    paper_key = identifier
                fp = load_fingerprint(paper_key, self._fingerprint_dir)
                if fp is None:
                    return json.dumps({"error": f"No fingerprint found for '{identifier}'"})
                # Strip fields never used by any skill to reduce token cost.
                # Skills that need methodology or contradictions can override this.
                fp.pop("curation_metadata", None)
                fp.pop("contradictions_and_negative_results", None)
                fp.pop("methodology", None)
                return json.dumps(fp, ensure_ascii=False, indent=2)

            if name == "find_pdb_structures":
                result = _find_pdb_structures(
                    input_dict.get("proteins", []), self._fingerprint_dir
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "search_rcsb_pdb":
                result = _search_rcsb_pdb(
                    input_dict.get("proteins", []), self._fingerprint_dir
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "resolve_protein_identifier":
                from src.target_resolve import resolve_target
                from dataclasses import asdict as _asdict
                return json.dumps(_asdict(resolve_target(input_dict["name"])),
                                  indent=2)

            if name == "find_complex_structures":
                from src.target_resolve import entry_metadata, find_complex_structures
                ids = find_complex_structures(
                    input_dict["uniprot"], rows=int(input_dict.get("rows", 25)))
                return json.dumps(
                    {"pdb_ids": ids, "entries": entry_metadata(ids[:15])}, indent=2)

            if name == "tool_find_glue_pockets":
                from src.structure_tools import find_glue_pockets
                result = find_glue_pockets(
                    _resolve(input_dict["file_path"]),
                    input_dict["chain_a"],
                    input_dict["chain_b"],
                    periinterface_radius=float(
                        input_dict.get("periinterface_radius", 10.0)),
                    top_n=int(input_dict.get("top_n", 3)),
                )
                return json.dumps(result, indent=2)

            if name == "tool_analyze_interface":
                from src.structure_tools import analyze_interface
                result = analyze_interface(
                    _resolve(input_dict["file_path"]),
                    input_dict["chain_a"],
                    input_dict["chain_b"],
                    float(input_dict.get("cutoff", 4.5)),
                )
                return json.dumps(result, indent=2)

            if name == "tool_get_residue_contacts":
                from src.structure_tools import get_residue_contacts
                result = get_residue_contacts(
                    _resolve(input_dict["file_path"]),
                    input_dict["chain"],
                    int(input_dict["resnum"]),
                    input_dict["partner_chain"],
                    float(input_dict.get("cutoff", 4.5)),
                )
                return json.dumps(result, indent=2)

            if name == "tool_check_mutation_clash":
                from src.structure_tools import check_mutation_clash
                aa = str(input_dict["new_aa"]).strip().upper()
                if len(aa) == 1:
                    aa = _ONE_TO_THREE.get(aa, aa)
                result = check_mutation_clash(
                    _resolve(input_dict["file_path"]),
                    input_dict["chain"],
                    int(input_dict["resnum"]),
                    aa,
                    input_dict["partner_chain"],
                )
                return json.dumps(result, indent=2)

            if name == "tool_get_sequence_map":
                from src.structure_tools import get_sequence_map
                result = get_sequence_map(_resolve(input_dict["file_path"]), input_dict["chain"])
                # auth_to_string_idx and auth_to_label_idx are large index dicts.
                # complex-structure-analysis needs auth_to_label to populate the
                # MODEL-READY HOTSPOTS table with correct label_seq_ids (not estimates).
                # Design/optimizer skills need both maps for AF3 JSON construction.
                # Strip both for all other skills (molecular-biology-expert etc.).
                if self.skill_name not in _NEEDS_INDEX_MAPS:
                    result = {"sequence": result["sequence"], "length": len(result["sequence"])}
                return json.dumps(result, indent=2)

            if name == "tool_score_surface_patch":
                from src.structure_tools import score_surface_patch
                result = score_surface_patch(
                    _resolve(input_dict["file_path"]),
                    input_dict["chain"],
                    [int(r) for r in input_dict["residue_list"]],
                )
                return json.dumps(result, indent=2)

            if name == "write_file":
                dest = Path(_resolve(input_dict["path"]))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(input_dict["content"], encoding="utf-8")
                logger.info(f"write_file → {dest}")
                return json.dumps({"written": str(dest)})

            def _coerce_taxa(v):
                if v is None or (isinstance(v, list) and not v):
                    return None
                return [int(t) for t in v]

            if name == "get_interactions_for":
                from src._corpus_graph import get_interactions_for
                result = get_interactions_for(
                    protein=input_dict.get("protein", ""),
                    fingerprint_dir=self._fingerprint_dir,
                    depth=int(input_dict.get("depth", 1)),
                    min_mentions=int(input_dict.get("min_mentions", 1)),
                    human_only=bool(input_dict.get("human_only", True)),
                    taxa=_coerce_taxa(input_dict.get("taxa")),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "find_quantitative_evidence":
                from src._corpus_graph import find_quantitative_evidence
                result = find_quantitative_evidence(
                    protein_pair=list(input_dict.get("protein_pair") or []),
                    fingerprint_dir=self._fingerprint_dir,
                    metric=input_dict.get("metric", "Kd"),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "shortest_interaction_path":
                from src._corpus_graph import shortest_interaction_path
                result = shortest_interaction_path(
                    protein_a=input_dict.get("protein_a", ""),
                    protein_b=input_dict.get("protein_b", ""),
                    fingerprint_dir=self._fingerprint_dir,
                    max_hops=int(input_dict.get("max_hops", 4)),
                    k=int(input_dict.get("k", 1)),
                    human_only=bool(input_dict.get("human_only", True)),
                    taxa=_coerce_taxa(input_dict.get("taxa")),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "interaction_hubs":
                from src._corpus_graph import interaction_hubs
                result = interaction_hubs(
                    fingerprint_dir=self._fingerprint_dir,
                    top_n=int(input_dict.get("top_n", 20)),
                    min_mentions=int(input_dict.get("min_mentions", 3)),
                    human_only=bool(input_dict.get("human_only", True)),
                    taxa=_coerce_taxa(input_dict.get("taxa")),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "novelty_signal":
                from src._corpus_graph import novelty_signal
                result = novelty_signal(
                    protein=str(input_dict.get("protein", "")),
                    fingerprint_dir=self._fingerprint_dir,
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "export_subgraph":
                from src._corpus_graph import export_subgraph
                result = export_subgraph(
                    seeds=list(input_dict.get("seeds") or []),
                    fingerprint_dir=self._fingerprint_dir,
                    output_path=_resolve(input_dict.get("output_path", "")),
                    depth=int(input_dict.get("depth", 1)),
                    max_nodes=int(input_dict.get("max_nodes", 200)),
                    with_depmap=bool(input_dict.get("with_depmap", False)),
                    depmap_min_n=int(input_dict.get("depmap_min_n", 100)),
                    human_only=bool(input_dict.get("human_only", True)),
                    taxa=_coerce_taxa(input_dict.get("taxa")),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "get_genetic_codependency":
                from src._corpus_graph import get_genetic_codependency
                result = get_genetic_codependency(
                    protein_a=input_dict.get("protein_a", ""),
                    protein_b=input_dict.get("protein_b", ""),
                    fingerprint_dir=self._fingerprint_dir,
                    min_n=int(input_dict.get("min_n", 100)),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "cluster_for_protein":
                from src._corpus_graph import cluster_for_protein
                result = cluster_for_protein(protein=input_dict.get("protein", ""))
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "cluster_members":
                from src._corpus_graph import cluster_members
                result = cluster_members(cluster_id=int(input_dict.get("cluster_id", -1)))
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "find_clusters_by_keyword":
                from src._corpus_graph import find_clusters_by_keyword
                result = find_clusters_by_keyword(
                    query=input_dict.get("query", ""),
                    max_results=int(input_dict.get("max_results", 20)),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            if name == "find_cocorrelated_genes":
                from src._corpus_graph import find_cocorrelated_genes
                result = find_cocorrelated_genes(
                    protein=input_dict.get("protein", ""),
                    top_k=int(input_dict.get("top_k", 25)),
                    min_abs_r=float(input_dict.get("min_abs_r", 0.2)),
                    min_n=int(input_dict.get("min_n", 100)),
                )
                return json.dumps(result, ensure_ascii=False, indent=2)

            return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as exc:
            logger.warning(f"Tool '{name}' raised: {exc}")
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        query: str,
        context_text: str | None = None,
        trace_path: str | Path | None = None,
    ) -> str:
        """
        Run the agentic loop and return the final assistant text.

        If a previous ``run()`` already populated ``self._messages``, this
        call resumes the conversation: the new user message is appended to
        the prior history and the agentic loop continues from there. Use
        ``reset()`` to start a fresh conversation while keeping the same
        runner instance (and its loaded system prompt + cached tools).

        Parameters
        ----------
        query : str
            User query for this turn.
        context_text : str | None
            Optional prior report prepended to the query. Only injected on
            the first turn — ignored on follow-ups so it isn't duplicated.
        trace_path : str | Path | None
            If provided, write a conversation trace after the call. Each
            call rewrites the trace with the cumulative history.
        """
        is_continuation = bool(self._messages)

        user_content = query
        if context_text and not is_continuation:
            user_content = (
                f"## Context from prior report\n\n{context_text}\n\n---\n\n{query}"
            )

        if self.provider == "claude":
            if is_continuation:
                messages: list[dict] = list(self._messages)  # type: ignore[arg-type]
                messages.append({"role": "user", "content": user_content})
            else:
                messages = [{"role": "user", "content": user_content}]
            result = self._run_claude(messages)
        else:
            if is_continuation:
                messages = list(self._messages)  # type: ignore[arg-type]
                messages.append({"role": "user", "parts": [{"text": user_content}]})
            else:
                messages = [{"role": "user", "parts": [{"text": user_content}]}]
            result = self._run_gemini(messages)

        if trace_path is not None:
            self.write_trace(Path(trace_path))

        return result

    def usage(self) -> "Usage":
        """
        Cumulative token usage for this runner, split by billing bucket.

        The pipeline builds one runner per stage and discards it, so this is the
        hand-off point for cost accounting — read it before the runner goes out
        of scope (including on the failure path; a stage that dies on iteration
        25 of 30 still spent that money).
        """
        from src.token_budget import Usage
        return Usage(
            input_tokens=self._total_input_tokens,
            output_tokens=self._total_output_tokens,
            cache_creation_tokens=self._total_cache_creation_tokens,
            cache_read_tokens=self._total_cache_read_tokens,
        )

    def reset(self) -> None:
        """Clear conversation history; keeps the loaded system prompt and tool list."""
        self._messages = None
        self._last_input_tokens = 0

    # ------------------------------------------------------------------
    # Trace output
    # ------------------------------------------------------------------

    def write_trace(self, dest: Path) -> None:
        """
        Write the conversation history from the last run() to *dest*.

        Creates two files:
          dest/trace_raw.json     — raw message list (machine-readable)
          dest/trace_rendered.md  — annotated markdown (human-readable)

        Must be called after run().  Raises RuntimeError if no run has completed.
        """
        if self._messages is None:
            raise RuntimeError("No trace available — call run() first.")

        dest.mkdir(parents=True, exist_ok=True)

        # --- raw JSON ---
        raw_path = dest / "trace_raw.json"
        raw_path.write_text(
            json.dumps(self._messages, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(f"Trace raw JSON → {raw_path}")

        # --- rendered markdown ---
        md_path = dest / "trace_rendered.md"
        md_path.write_text(
            self._render_trace(), encoding="utf-8"
        )
        logger.info(f"Trace markdown → {md_path}")

    def _render_trace(self) -> str:
        """Render self._messages as an annotated markdown document."""
        assert self._messages is not None

        lines: list[str] = [
            f"# Conversation Trace — `{self.skill_name}`",
            f"",
            f"**Model:** `{self.model_id}`  ",
            f"**Provider:** {self.provider}  ",
            f"**Total LLM calls:** {self._count_assistant_turns()}  ",
            f"**Tokens:** {self._total_input_tokens:,} in / {self._total_output_tokens:,} out",
            f"",
            "---",
            "",
            "## System Prompt (SKILL.md)",
            "",
            self.system_prompt,
            "",
            "---",
            "",
        ]

        user_turn = 0
        assistant_turn = 0

        for msg in self._messages:
            role = msg.get("role", "")

            # ----------------------------------------------------------
            # Claude format
            # ----------------------------------------------------------
            if role == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    # Initial query (plain string)
                    user_turn += 1
                    lines += [
                        f"## Turn {user_turn} — User Query",
                        "",
                        content,
                        "",
                        "---",
                        "",
                    ]
                elif isinstance(content, list):
                    # Tool results turn
                    user_turn += 1
                    lines += [f"## Turn {user_turn} — Tool Results", ""]
                    for block in content:
                        if block.get("type") == "tool_result":
                            result_text = block.get("content", "")
                            # Pretty-print JSON results; fall back to raw text
                            try:
                                parsed = json.loads(result_text)
                                formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
                            except (json.JSONDecodeError, TypeError):
                                formatted = result_text
                            # Truncate very large results for readability
                            if len(formatted) > 3000:
                                formatted = formatted[:3000] + "\n… [truncated]"
                            lines += [
                                f"**Tool use ID:** `{block.get('tool_use_id', '')}`",
                                "",
                                "```json",
                                formatted,
                                "```",
                                "",
                            ]
                    lines += ["---", ""]

            elif role == "assistant":
                assistant_turn += 1
                content = msg.get("content", [])
                lines += [f"## Turn {assistant_turn} — Assistant", ""]

                for block in content:
                    btype = block.get("type", "")

                    if btype == "thinking":
                        thinking_text = block.get("thinking", "")
                        lines += [
                            "### Thinking",
                            "",
                            "> " + thinking_text.replace("\n", "\n> "),
                            "",
                        ]

                    elif btype == "text":
                        lines += [
                            "### Response Text",
                            "",
                            block.get("text", ""),
                            "",
                        ]

                    elif btype == "tool_use":
                        tool_inputs = json.dumps(
                            block.get("input", {}), indent=2, ensure_ascii=False
                        )
                        lines += [
                            f"### Tool Call — `{block.get('name', '')}`",
                            "",
                            f"**ID:** `{block.get('id', '')}`",
                            "",
                            "```json",
                            tool_inputs,
                            "```",
                            "",
                        ]

                lines += ["---", ""]

            # ----------------------------------------------------------
            # Gemini format (parts-based)
            # ----------------------------------------------------------
            elif role == "model":
                assistant_turn += 1
                lines += [f"## Turn {assistant_turn} — Assistant (Gemini)", ""]
                for part in msg.get("parts", []):
                    if "text" in part:
                        lines += ["### Response Text", "", part["text"], ""]
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        lines += [
                            f"### Tool Call — `{fc.get('name', '')}`",
                            "",
                            "```json",
                            json.dumps(fc.get("args", {}), indent=2, ensure_ascii=False),
                            "```",
                            "",
                        ]
                lines += ["---", ""]

            elif role == "user" and msg.get("parts"):
                # Gemini tool result turn
                user_turn += 1
                lines += [f"## Turn {user_turn} — Tool Results (Gemini)", ""]
                for part in msg.get("parts", []):
                    if "functionResponse" in part:
                        fr = part["functionResponse"]
                        result_str = json.dumps(
                            fr.get("response", {}).get("result", ""),
                            indent=2, ensure_ascii=False,
                        )
                        if len(result_str) > 3000:
                            result_str = result_str[:3000] + "\n… [truncated]"
                        lines += [
                            f"**Function:** `{fr.get('name', '')}`",
                            "",
                            "```json",
                            result_str,
                            "```",
                            "",
                        ]
                lines += ["---", ""]

        return "\n".join(lines)

    def _count_assistant_turns(self) -> int:
        if not self._messages:
            return 0
        return sum(
            1 for m in self._messages
            if m.get("role") in ("assistant", "model")
        )

    # ------------------------------------------------------------------
    # Claude agentic loop
    # ------------------------------------------------------------------

    def _run_claude(self, messages: list[dict]) -> str:
        client = anthropic.Anthropic()
        claude_tools = _to_claude_tools(_filter_tools(_TOOL_DEFS, self.skill_name))

        for iteration in range(self.max_iter):
            logger.info(f"[claude] call #{iteration + 1} — messages={len(messages)}")

            # Extended thinking.  `budget_tokens` was REMOVED on claude-sonnet-5
            # / claude-opus-5 and is rejected with a 400 — adaptive thinking
            # replaces it and lets the model pick its own depth.  Depth is
            # steered with output_config.effort instead of a token budget.
            thinking_param = (
                {
                    "thinking": {"type": "adaptive"},
                    "output_config": {"effort": "high"},
                }
                if self.use_extended_thinking
                else {}
            )

            # Retry up to 3 times on rate-limit and transient connection errors.
            # Use streaming — required by the SDK when max_tokens is large enough
            # that the request could exceed 10 minutes non-streamed.
            for attempt in range(3):
                try:
                    with client.messages.stream(
                        model=self.model_id,
                        max_tokens=24000,
                        system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
                        tools=claude_tools,
                        messages=messages,
                        **thinking_param,
                    ) as stream:
                        response = stream.get_final_message()
                    break
                except anthropic.RateLimitError:
                    if attempt == 2:
                        raise
                    wait = 65 * (attempt + 1)
                    logger.warning(f"Rate limit hit — waiting {wait}s then retrying…")
                    time.sleep(wait)
                except anthropic.APIConnectionError:
                    if attempt == 2:
                        raise
                    wait = 10 * (attempt + 1)
                    logger.warning(f"Connection error on call #{iteration + 1} (attempt {attempt + 1}/3) — retrying in {wait}s")
                    time.sleep(wait)
                except anthropic.AnthropicError as exc:
                    # Catch overloaded_error (529) and retry with backoff.
                    if attempt == 2 or "overloaded" not in str(exc).lower():
                        raise
                    wait = 30 * (attempt + 1)
                    logger.warning(f"API overloaded on call #{iteration + 1} (attempt {attempt + 1}/3) — retrying in {wait}s")
                    time.sleep(wait)

            # Serialise content blocks for history.
            # thinking blocks: must round-trip in message history but are never
            # included in the visible text output — they are the model's scratchpad.
            content_list: list[dict] = []
            text_parts: list[str] = []
            tool_use_blocks = []

            for block in response.content:
                if block.type == "thinking":
                    # Preserve in history; never emit to output text.
                    #
                    # The `signature` is NOT optional: a thinking block replayed
                    # without it is rejected with
                    # `messages.N.content.0.thinking.signature: Field required`.
                    # It is also why the block must be echoed back verbatim
                    # rather than reconstructed — the signature covers the exact
                    # text the model produced.
                    thinking_block: dict[str, Any] = {
                        "type": "thinking", "thinking": block.thinking}
                    signature = getattr(block, "signature", None)
                    if signature:
                        thinking_block["signature"] = signature
                    content_list.append(thinking_block)
                elif block.type == "redacted_thinking":
                    # Opaque, encrypted reasoning. It carries no readable text
                    # but must still round-trip or the turn is rejected.
                    content_list.append({"type": "redacted_thinking",
                                         "data": getattr(block, "data", "")})
                elif block.type == "text":
                    text_parts.append(block.text)
                    content_list.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)
                    content_list.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            cache_created = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            self._total_input_tokens += in_tok
            self._total_output_tokens += out_tok
            self._total_cache_creation_tokens += cache_created
            self._total_cache_read_tokens += cache_read
            self._last_input_tokens = in_tok
            cache_info = f" | cache_created={cache_created:,} / cache_read={cache_read:,}" if (cache_created or cache_read) else ""
            logger.info(
                f"  tokens: {in_tok:,} in / {out_tok:,} out{cache_info} "
                f"(run cumulative: {self._total_input_tokens:,} in / "
                f"{self._total_output_tokens:,} out)"
            )

            if response.stop_reason == "max_tokens":
                logger.warning(
                    f"Response truncated at max_tokens on call #{iteration + 1} "
                    f"({out_tok:,} out) — '### PIPELINE HANDOFF' may be missing."
                )
            elif response.stop_reason == "refusal":
                # A safety classifier declined the request; the response has no
                # content. Raise rather than warn: the old behaviour wrote an
                # empty report and the run failed three stages later with a
                # confusing "no hotspots" error.
                #
                # This is strongly model-dependent — measured on the same
                # structure-analysis prompt, claude-sonnet-5 refuses with
                # category "bio" where claude-opus-5 and claude-haiku-4-5 answer
                # normally — so the caller retries on a fallback model.
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None)
                raise SkillRefusedError(
                    skill=self.skill_name, model=self.model_id,
                    category=category, iteration=iteration + 1)

            if in_tok > self.max_input_tokens:
                raise RuntimeError(
                    f"Input token limit exceeded on call #{iteration + 1}: "
                    f"{in_tok:,} tokens in one request "
                    f"(limit: {self.max_input_tokens:,}). "
                    f"Run total so far: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out. "
                    f"Reduce top_k, use a shorter query, or raise --max-tokens."
                )

            messages.append({"role": "assistant", "content": content_list})

            if not tool_use_blocks:
                logger.info(
                    f"Run complete — total tokens: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out across {iteration + 1} LLM calls"
                )
                self._messages = messages
                return "\n".join(text_parts)

            # Execute all tool calls and batch results
            tool_results: list[dict] = []
            for block in tool_use_blocks:
                logger.info(f"  → {block.name}({list(block.input.keys())})")
                result = self._execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(
            f"Max iterations ({self.max_iter}) exceeded for skill '{self.skill_name}'"
        )

    # ------------------------------------------------------------------
    # Gemini agentic loop
    # ------------------------------------------------------------------

    def _run_gemini(self, messages: list[dict]) -> str:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        url = _GEMINI_GENERATE_URL.format(model=self.model_id)
        gemini_tools = _to_gemini_tools(_filter_tools(_TOOL_DEFS, self.skill_name))

        for iteration in range(self.max_iter):
            logger.info(f"[gemini] call #{iteration + 1} — messages={len(messages)}")

            payload = {
                "system_instruction": {"parts": [{"text": self.system_prompt}]},
                "contents": messages,
                "tools": gemini_tools,
                "generationConfig": {"maxOutputTokens": 24000},
            }
            for attempt in range(4):
                # The status-code retry below only helps once a RESPONSE
                # exists. A dropped connection or a read timeout raises out of
                # requests.post itself, and gemini is the default provider for
                # every stage — so a blip that the Claude path shrugs off
                # (anthropic retries APIConnectionError) used to abort a whole
                # multi-hour run here. Same budget as the status retries.
                try:
                    resp = requests.post(
                        url, params={"key": api_key}, json=payload, timeout=180
                    )
                except (requests.ConnectionError, requests.Timeout) as exc:
                    if attempt == 3:
                        raise
                    wait = 10 * (attempt + 1)
                    logger.warning(
                        f"[gemini] {type(exc).__name__} on call #{iteration + 1} "
                        f"(attempt {attempt + 1}/4) — retrying in {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                    break
                if resp.status_code == 429:
                    # Honour Retry-After if present; otherwise use exponential backoff
                    retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                    wait = int(retry_after) if retry_after and retry_after.isdigit() else 30 * (attempt + 1)
                else:
                    wait = 10 * (attempt + 1)
                logger.warning(
                    f"[gemini] transient {resp.status_code} on call #{iteration + 1} "
                    f"(attempt {attempt + 1}/4) — retrying in {wait}s"
                )
                time.sleep(wait)
            resp.raise_for_status()
            body = resp.json()

            # A prompt Gemini declines outright comes back with an EMPTY
            # candidates list and a top-level promptFeedback.blockReason — no
            # content to index into. A per-candidate safety stop looks like a
            # normal candidate but with finishReason SAFETY/PROHIBITED_CONTENT/
            # BLOCKLIST/RECITATION/SPII and no `content` key. Both must raise
            # SkillRefusedError, the same signal Claude's refusal produces, so
            # the caller's fallback chain handles them uniformly rather than
            # crashing on a raw KeyError/IndexError here.
            candidates = body.get("candidates") or []
            block_reason = (body.get("promptFeedback") or {}).get("blockReason")
            if block_reason or not candidates:
                raise SkillRefusedError(
                    skill=self.skill_name, model=self.model_id,
                    category=block_reason or "blocked_no_candidates",
                    iteration=iteration + 1)

            candidate = candidates[0]
            _SAFETY_FINISH_REASONS = {
                "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION", "SPII"}
            if candidate.get("finishReason") in _SAFETY_FINISH_REASONS:
                raise SkillRefusedError(
                    skill=self.skill_name, model=self.model_id,
                    category=candidate["finishReason"], iteration=iteration + 1)

            # A truncated or empty candidate can carry `content` with no
            # `parts` (a thinking model that hit maxOutputTokens before
            # emitting one). Indexing straight in raised a bare KeyError
            # outside the handled-refusal path, so the run died with a
            # traceback instead of the truncation warning below.
            parts: list[dict] = ((candidate.get("content") or {}).get("parts") or [])

            if candidate.get("finishReason") == "MAX_TOKENS":
                logger.warning(
                    f"Gemini response truncated at maxOutputTokens on call #{iteration + 1} "
                    f"— '### PIPELINE HANDOFF' may be missing."
                )
            if not parts:
                raise RuntimeError(
                    f"Gemini returned no content on call #{iteration + 1} "
                    f"(finishReason={candidate.get('finishReason')!r}). "
                    f"If this is MAX_TOKENS, raise maxOutputTokens or shorten "
                    f"the query; the model produced no usable output.")

            usage = body.get("usageMetadata", {})
            in_tok = usage.get("promptTokenCount", 0)
            # Gemini reports reasoning tokens SEPARATELY in thoughtsTokenCount.
            # They are billed at the output rate and are NOT included in
            # candidatesTokenCount, so counting only the latter under-reported
            # spend on every thinking-model call — and `--budget`, which is
            # documented as a hard cap, under-enforced by the same margin.
            thought_tok = usage.get("thoughtsTokenCount", 0) or 0
            out_tok = (usage.get("candidatesTokenCount", 0) or 0) + thought_tok
            # promptTokenCount INCLUDES cached tokens; surface the cached share
            # so the ledger can price it at the cache-read rate rather than
            # billing the whole prompt as fresh input.
            cached_tok = usage.get("cachedContentTokenCount", 0) or 0
            self._total_input_tokens += in_tok
            self._total_output_tokens += out_tok
            self._total_cache_read_tokens += cached_tok
            self._last_input_tokens = in_tok
            logger.info(
                f"  tokens: {in_tok:,} in / {out_tok:,} out"
                + (f" (incl. {thought_tok:,} reasoning)" if thought_tok else "")
                + (f" / {cached_tok:,} cached" if cached_tok else "")
                + f" (run cumulative: {self._total_input_tokens:,} in / "
                f"{self._total_output_tokens:,} out)"
            )

            if in_tok > self.max_input_tokens:
                raise RuntimeError(
                    f"Input token limit exceeded on call #{iteration + 1}: "
                    f"{in_tok:,} tokens in one request "
                    f"(limit: {self.max_input_tokens:,}). "
                    f"Run total so far: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out. "
                    f"Reduce top_k, use a shorter query, or raise --max-tokens."
                )

            # Append model turn to history
            messages.append({"role": "model", "parts": parts})

            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not function_calls:
                logger.info(
                    f"Run complete — total tokens: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out across {iteration + 1} LLM calls"
                )
                self._messages = messages
                text_parts = [p.get("text", "") for p in parts if "text" in p]
                return "\n".join(text_parts)

            # Execute tools; batch all functionResponses in one user turn
            function_responses: list[dict] = []
            for fc in function_calls:
                name = fc["name"]
                args = fc.get("args", {})
                logger.info(f"  → {name}({list(args.keys())})")
                result = self._execute_tool(name, args)
                function_responses.append({
                    "functionResponse": {
                        "name": name,
                        "response": {"result": result},
                    }
                })

            messages.append({"role": "user", "parts": function_responses})

        raise RuntimeError(
            f"Max iterations ({self.max_iter}) exceeded for skill '{self.skill_name}'"
        )
