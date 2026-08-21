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
    analyze_active_site_geometry,
    analyze_interface,
    check_mutation_clash,
    extract_ligand_contacts,
    find_glue_pockets,
    get_residue_contacts,
    get_sequence_map,
    score_surface_patch,
)

mcp = FastMCP("structure-tools")


def _resolve(file_path: str) -> str:
    """Resolve a file path against the project root, with recovery for
    model-emitted absolute paths that point outside the repo.

    Mirrors `src/skill_runner.py:_resolve` — see that docstring for the
    full resolution order. The two helpers stay in sync because the same
    skills hit them under two different transports (MCP server vs.
    direct Python dispatch). On change, update both.
    """
    import sys
    p = Path(file_path)
    if p.exists():
        return str(p)
    is_real_absolute = p.is_absolute() and (sys.platform != "win32" or bool(p.drive))
    if is_real_absolute:
        recovered = _recover_under_root(p)
        if recovered is not None:
            return str(recovered)
    return str(ROOT / Path(file_path.lstrip("/\\")))


def _recover_under_root(p: Path) -> "Path | None":
    """Find an existing file under `ROOT` whose tail matches `p`."""
    parts = list(p.parts)
    if parts and (parts[0] in ("/", "\\") or parts[0].endswith(":\\") or parts[0].endswith(":/")):
        parts = parts[1:]
    for i in range(len(parts)):
        candidate = ROOT.joinpath(*parts[i:])
        if candidate.exists():
            return candidate
    basename = p.name
    for canonical in (ROOT / "data" / "structures" / basename, ROOT / basename):
        if canonical.exists():
            return canonical
    return None


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


@mcp.tool()
def tool_extract_ligand_contacts(
    file_path: str,
    ligand_resname: str = "",
    ligand_chain: str = "",
    cutoff: float = 4.5,
    min_heavy_atoms: int = 6,
    max_ligands: int = 3,
) -> str:
    """
    Find bound non-polymer ligand(s) in a holo structure and the protein residues
    lining them — the substrate-grafting input for enzyme active-site design.

    For each substrate-like ligand (water + common crystallisation additives are
    ignored; catalytic metals like Zn/Mg/Mn are reported separately, never as the
    substrate) returns the first-shell interacting residues with the ligand atoms
    they contact, so those interactions can be grafted into a de novo active site.

    This is an OPTIONAL enrichment step: when no holo structure exists for the
    substrate the result's `ligands` list is empty and the workflow should proceed
    from the chemistry/QM active-site build (diffusion generates the surrounding
    residues). Accepts .cif and .pdb.

    Args:
        file_path:       Absolute path to a holo structure file (.cif or .pdb)
        ligand_resname:  Restrict to this ligand 3-letter code (optional)
        ligand_chain:    Restrict to this chain (optional)
        cutoff:          Heavy-atom contact cutoff in Å (default 4.5)
        min_heavy_atoms: Ignore ligands smaller than this (drops ions/fragments; default 6)
        max_ligands:     Max ligands to report, largest first (default 3)
    """
    try:
        result = extract_ligand_contacts(
            _resolve(file_path),
            ligand_resname=ligand_resname or None,
            ligand_chain=ligand_chain or None,
            cutoff=cutoff,
            min_heavy_atoms=min_heavy_atoms,
            max_ligands=max_ligands,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("extract_ligand_contacts failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_analyze_active_site_geometry(file_path: str, catalytic_spec: dict) -> str:
    """
    Measure catalytic-constellation distances/angles from an explicit spec.

    Generic geometry reporter for diagnosing a grafted or designed active site
    (donor→acceptor distances, catalytic-triad angles, residue→ligand contacts).
    Atoms are referenced by [chain, resnum (auth_seq_id), atom_name].

    catalytic_spec = {
      "distances": [{"label": str, "a": [chain,resnum,atom], "b": [chain,resnum,atom]}, ...],
      "angles":    [{"label": str, "a": [...], "b": [...], "c": [...]}]   # angle at b
    }

    Args:
        file_path:      Absolute path to structure file (.cif or .pdb)
        catalytic_spec: Distances/angles to measure (see format above)
    """
    try:
        result = analyze_active_site_geometry(_resolve(file_path), catalytic_spec)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("analyze_active_site_geometry failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_resolve_protein_identifier(name: str) -> str:
    """
    Resolve a gene symbol, protein name or alias to its human UniProt accession.

    Offline — reads the bundled UniProt ID mapping, no network call and no key.

    Args:
        name: Gene symbol or protein name, e.g. "KRAS"
    """
    try:
        from dataclasses import asdict

        from src.target_resolve import resolve_target

        return json.dumps(asdict(resolve_target(name)), indent=2)
    except Exception as e:
        logger.exception("resolve_protein_identifier failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def tool_find_complex_structures(uniprot: str, rows: int = 25) -> str:
    """
    PDB entries containing this accession together with another protein entity.

    A structured RCSB query, unlike the full-text `search_rcsb_pdb`: it can
    express "a complex containing P01116", which full text cannot. Returns the
    entry ids plus per-chain entity descriptions, lengths and accessions.

    Args:
        uniprot: UniProt accession, e.g. "P01116"
        rows:    Maximum entries to return (default 25)
    """
    try:
        from src.target_resolve import entry_metadata, find_complex_structures

        ids = find_complex_structures(uniprot, rows=rows)
        return json.dumps({"pdb_ids": ids, "entries": entry_metadata(ids[:15])},
                          indent=2)
    except Exception as e:
        logger.exception("find_complex_structures failed")
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
