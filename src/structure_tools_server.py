"""MCP server exposing structural analysis tools to Claude Code."""
import json
import sys
from pathlib import Path

from fastmcp import FastMCP
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
_log_file = ROOT / "data" / "structure_tools.log"
logger.remove()
logger.add(_log_file, level="DEBUG", rotation="5 MB", enqueue=True)

sys.path.insert(0, str(ROOT))
from src.structure_tools import (
    analyze_interface,
    check_mutation_clash,
    find_glue_pockets,
    get_residue_contacts,
    get_sequence_map,
    score_surface_patch,
)

mcp = FastMCP("structure-tools")


def _resolve(file_path: str) -> str:
    """Resolve relative or root-relative paths against the project root.

    Handles three cases:
    - Fully-qualified absolute path (C:\\... on Windows, /... on Linux) → pass through
    - Root-relative with leading slash (/data/structures/x.cif) → strip slash, join ROOT
    - Plain relative path (data/structures/x.cif) → join ROOT

    The leading-slash strip is critical on Windows: pathlib's / operator interprets
    a slash-prefixed path as drive-relative, producing C:\\data\\... instead of
    ROOT\\data\\...
    """
    import sys
    p = Path(file_path)
    if p.is_absolute() and (sys.platform != "win32" or bool(p.drive)):
        return str(p)
    return str(ROOT / Path(file_path.lstrip("/\\")))


@mcp.tool()
def tool_analyze_interface(
    file_path: str,
    chain_a: str,
    chain_b: str,
    cutoff: float = 4.5,
) -> str:
    """
    Full interface analysis between two chains of a structure file.

    Computes buried surface area (BSA total + per-residue), lists all interface
    residues on both chains with type classification (hydrophobic/aromatic/
    charged/polar) and Kyte-Doolittle hydrophobicity, detects H-bonds with
    geometry (donor, acceptor, distance), builds a pairwise contact map with
    interaction classification (electrostatic/h_bond/hydrophobic/pi_stacking/
    vdw_contact), flags gap residues (< 2 contacts), and extracts pLDDT scores
    from B-factor column (valid for AF3/Boltz outputs).

    Use as the primary analysis step for binder-optimizer or chimerax-ppi-analysis
    replacements. Accepts .cif (mmCIF) and .pdb files.

    Args:
        file_path: Absolute path to structure file (.cif or .pdb)
        chain_a:   Chain ID of first chain (e.g. binder = "B")
        chain_b:   Chain ID of second chain (e.g. target = "A")
        cutoff:    Heavy-atom distance cutoff for contact detection in Å (default 4.5)
    """
    try:
        result = analyze_interface(_resolve(file_path), chain_a, chain_b, cutoff)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("analyze_interface failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_get_residue_contacts(
    file_path: str,
    chain: str,
    resnum: int,
    partner_chain: str,
    cutoff: float = 4.5,
) -> str:
    """
    All contacts between a single residue and a partner chain within the cutoff.

    Returns exact per-contact minimum distances, interaction classification,
    H-bond details, and a gap flag if fewer than 2 contacts are found.
    Use for targeted inspection of specific binder positions during mutation
    candidate reasoning.

    Args:
        file_path:     Absolute path to structure file (.cif or .pdb)
        chain:         Chain ID containing the residue of interest
        resnum:        Residue number (auth_seq_id / PDB author numbering)
        partner_chain: Chain ID to check contacts against
        cutoff:        Heavy-atom distance cutoff in Å (default 4.5)
    """
    try:
        result = get_residue_contacts(_resolve(file_path), chain, resnum, partner_chain, cutoff)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("get_residue_contacts failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_check_mutation_clash(
    file_path: str,
    chain: str,
    resnum: int,
    new_aa: str,
    partner_chain: str,
) -> str:
    """
    Estimates whether a point mutation would clash with the partner chain.

    Uses a Cβ-position heuristic with sidechain reach radii. Returns clash
    severity: none / minor / major. Minor clashes may be resolved by AF3
    repacking; major clashes are likely structural conflicts.

    new_aa accepts 1-letter or 3-letter amino acid codes (e.g. "E" or "GLU").

    Args:
        file_path:     Absolute path to structure file (.cif or .pdb)
        chain:         Chain ID of the residue to mutate
        resnum:        Residue number (auth_seq_id)
        new_aa:        Proposed amino acid (1-letter or 3-letter, case insensitive)
        partner_chain: Chain ID to check clashes against
    """
    # Normalise new_aa
    aa = new_aa.strip().upper()
    if len(aa) == 1:
        one_to_three = {v: k for k, v in {
            "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
            "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
            "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
            "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
        }.items()}
        aa = one_to_three.get(aa, aa)
    try:
        result = check_mutation_clash(_resolve(file_path), chain, resnum, aa, partner_chain)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("check_mutation_clash failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_get_sequence_map(file_path: str, chain: str) -> str:
    """
    Returns the full amino acid sequence of a chain with all numbering maps
    needed to construct AF3 JSON submissions.

    Provides: 1-letter sequence string, auth_seq_id → string index map (for
    applying mutations at the correct position in the AF3 JSON), and
    auth_seq_id → label_seq_id map (for BoltzGen/RFD3 hotspot specs).

    Args:
        file_path: Absolute path to structure file (.cif or .pdb)
        chain:     Chain ID
    """
    try:
        result = get_sequence_map(_resolve(file_path), chain)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("get_sequence_map failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_score_surface_patch(
    file_path: str,
    chain: str,
    residue_list: list[int],
) -> str:
    """
    Characterises a set of residues as a potential binding surface for a
    cyclic peptide or mini-protein binder.

    Computes: spatial spread (Cα RMSD from patch centroid), mean Kyte-Doolittle
    hydrophobicity, residue type breakdown, hydrophobic fraction, and a
    qualitative suitability rating (Excellent / Good / Marginal / Poor) with
    rationale.

    Args:
        file_path:    Absolute path to structure file (.cif or .pdb)
        chain:        Chain ID of the target surface
        residue_list: List of residue numbers (auth_seq_id) defining the patch
    """
    try:
        result = score_surface_patch(_resolve(file_path), chain, residue_list)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("score_surface_patch failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_find_glue_pockets(
    file_path: str,
    chain_a: str,
    chain_b: str,
    periinterface_radius: float = 10.0,
    max_bridge_span: float = 20.0,
    min_periface_sasa: float = 5.0,
    top_n: int = 3,
) -> str:
    """
    Identify periinterface "glue pockets" for molecular glue / PPI stabilizer design.

    Computes which surface-exposed residue clusters on each chain flank the
    protein-protein interface, then finds cross-chain pairs whose centroids are
    close enough for a single mini-protein or bicyclic peptide to bridge both
    simultaneously — stabilizing rather than disrupting the interaction.

    Returns a ranked list of glue pockets, each with:
    - chain_a_patch and chain_b_patch: residue lists, SASA, hydrophobic fraction,
      spatial spread, suitability rating
    - centroid_separation_A: distance between the two patch centroids
    - bridgeable: True if within max_bridge_span
    - design_note: recommended modality based on span

    Use this tool instead of tool_score_surface_patch when the design goal is
    to STABILIZE a protein-protein interaction (molecular glue mode).

    Args:
        file_path            : Absolute path to structure file (.cif or .pdb)
        chain_a              : Chain ID of first protein
        chain_b              : Chain ID of second protein
        periinterface_radius : Cα–Cα distance cutoff to nearest interface residue (default 10 Å)
        max_bridge_span      : Max centroid–centroid distance between the two patches (default 20 Å)
        min_periface_sasa    : Min SASA in complex (Å²) to include a residue (default 5 Å²)
        top_n                : Number of top-ranked pockets to return (default 3)
    """
    try:
        result = find_glue_pockets(
            _resolve(file_path), chain_a, chain_b,
            periinterface_radius=periinterface_radius,
            max_bridge_span=max_bridge_span,
            min_periface_sasa=min_periface_sasa,
            top_n=top_n,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("find_glue_pockets failed")
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
