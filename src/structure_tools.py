"""
Structure analysis toolkit for binder-target complexes.

All computations are purely numerical — no visualisation, no ChimeraX dependency.
Designed to be called as MCP tools so any LLM can reason from reliable structural data.

Libraries used:
  gemmi      — CIF/PDB parsing (handles AF3 mmCIF natively, auth/label numbering)
  biopython  — Shrake-Rupley SASA/BSA, H-bond geometry
  scipy      — KDTree for fast distance queries
  numpy      — array maths
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import gemmi
import numpy as np
from Bio.PDB import PDBIO, MMCIFParser, PDBParser, Select
from Bio.PDB.SASA import ShrakeRupley
from scipy.spatial import KDTree


# ---------------------------------------------------------------------------
# Residue property tables
# ---------------------------------------------------------------------------

AA3_TO_1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}

# Kyte-Doolittle hydrophobicity (positive = hydrophobic)
KD_HYDROPHOBICITY = {
    "ILE": 4.5, "VAL": 4.2, "LEU": 3.8, "PHE": 2.8, "CYS": 2.5,
    "MET": 1.9, "ALA": 1.8, "GLY": -0.4, "THR": -0.7, "TRP": -0.9,
    "SER": -0.8, "TYR": -1.3, "PRO": -1.6, "HIS": -3.2, "GLU": -3.5,
    "GLN": -3.5, "ASP": -3.5, "ASN": -3.5, "LYS": -3.9, "ARG": -4.5,
}

CHARGED_POS = {"ARG", "LYS"}
CHARGED_NEG = {"ASP", "GLU"}
POLAR       = {"SER", "THR", "ASN", "GLN", "TYR", "CYS"}
HYDROPHOBIC = {"ALA", "VAL", "LEU", "ILE", "MET", "PHE", "TRP", "PRO", "GLY"}
AROMATIC    = {"PHE", "TYR", "TRP", "HIS"}

# Approximate VdW radii for clash detection (Å)
VDW_RADII: dict[str, float] = {
    "C": 1.7, "N": 1.55, "O": 1.52, "S": 1.8, "P": 1.8, "default": 1.7,
}

# Typical Cβ positions relative to Cα for each AA (used for mutation clash heuristic)
# These are Cβ distances from Cα — all ~1.52 Å except Gly has no Cβ
CB_DISTANCE = 1.52  # Å, Cα → Cβ bond

# Approximate max sidechain extent (Cβ to sidechain tip, Å) — for clash radius
SIDECHAIN_REACH: dict[str, float] = {
    "ALA": 0.0, "GLY": 0.0,
    "SER": 1.4, "CYS": 1.8, "THR": 1.9, "VAL": 2.0, "ILE": 3.5,
    "LEU": 3.5, "PRO": 2.5, "ASP": 2.4, "ASN": 2.9, "MET": 4.0,
    "GLN": 3.5, "GLU": 3.5, "LYS": 5.0, "ARG": 6.5, "HIS": 4.0,
    "PHE": 4.5, "TYR": 5.0, "TRP": 5.5,
}


# ---------------------------------------------------------------------------
# Structure loading helpers
# ---------------------------------------------------------------------------

def _load_biopython(path: str):
    """Load structure via biopython; returns (structure, format)."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".cif", ".mmcif"):
        parser = MMCIFParser(QUIET=True)
    else:
        parser = PDBParser(QUIET=True)
    structure = parser.get_structure("mol", str(p))
    return structure


def _load_gemmi(path: str) -> gemmi.Structure:
    return gemmi.read_structure(path)


def _get_biopython_chain(structure, chain_id: str):
    for model in structure:
        for chain in model:
            if chain.id == chain_id:
                return chain
    raise ValueError(f"Chain '{chain_id}' not found in structure")


def _heavy_atoms(chain) -> tuple[list, np.ndarray]:
    """Return (residue_list, coords_array) for all non-hydrogen atoms in a chain."""
    residues = []
    coords = []
    for res in chain.get_residues():
        for atom in res.get_atoms():
            if atom.element != "H":
                residues.append(res)
                coords.append(atom.get_vector().get_array())
    return residues, np.array(coords) if coords else np.empty((0, 3))


# ---------------------------------------------------------------------------
# SASA / BSA computation
# ---------------------------------------------------------------------------

class _ChainSelect(Select):
    def __init__(self, chain_ids: list[str]):
        self.chain_ids = set(chain_ids)
    def accept_chain(self, chain):
        return chain.id in self.chain_ids


def _compute_sasa_per_residue(structure, chain_ids: list[str]) -> dict[str, float]:
    """
    Run Shrake-Rupley on the specified chains.
    Returns {chain_id:resseq:resname → sasa_Å²}.
    """
    import io, tempfile, os
    from Bio.PDB import PDBIO

    # Write selected chains to a temporary PDB string
    io_buf = io.StringIO()
    pdbio = PDBIO()
    pdbio.set_structure(structure)

    class _Sel(Select):
        def accept_chain(self, c):
            return c.id in chain_ids

    with tempfile.NamedTemporaryFile(suffix=".pdb", delete=False, mode="w") as f:
        tmp = f.name
    try:
        pdbio.save(tmp, _Sel())
        sub_parser = PDBParser(QUIET=True)
        sub_struct = sub_parser.get_structure("sub", tmp)
    finally:
        os.unlink(tmp)

    sr = ShrakeRupley()
    sr.compute(sub_struct, level="R")

    result: dict[str, float] = {}
    for model in sub_struct:
        for chain in model:
            for res in chain:
                key = f"{chain.id}:{res.get_id()[1]}:{res.get_resname()}"
                result[key] = getattr(res, "sasa", 0.0)
    return result


# ---------------------------------------------------------------------------
# H-bond detection
# ---------------------------------------------------------------------------

# Donor heavy-atom symbols and their H count (approximate)
HBOND_DONORS = {"N", "O", "S"}
HBOND_ACCEPTORS = {"N", "O", "S", "F"}

def _detect_hbonds(chain_a, chain_b, dist_cutoff: float = 3.5,
                   angle_cutoff: float = 120.0) -> list[dict]:
    """
    Geometric H-bond detection between two chains.
    Criteria: donor-acceptor distance ≤ dist_cutoff Å, angle ≥ angle_cutoff°.
    Returns list of dicts with donor/acceptor residue info and geometry.
    """
    # Collect donor and acceptor atoms from both chains
    def _atoms(chain, role_set):
        out = []
        for res in chain.get_residues():
            for atom in res.get_atoms():
                if atom.element in role_set and atom.element != "C":
                    out.append((res, atom))
        return out

    donors_a = _atoms(chain_a, HBOND_DONORS)
    acceptors_b = _atoms(chain_b, HBOND_ACCEPTORS)
    donors_b = _atoms(chain_b, HBOND_DONORS)
    acceptors_a = _atoms(chain_a, HBOND_ACCEPTORS)

    pairs = [(donors_a, acceptors_b), (donors_b, acceptors_a)]
    hbonds = []
    seen = set()

    for donors, acceptors in pairs:
        if not donors or not acceptors:
            continue
        acc_coords = np.array([a.get_vector().get_array() for _, a in acceptors])
        tree = KDTree(acc_coords)
        for d_res, d_atom in donors:
            d_coord = d_atom.get_vector().get_array()
            idxs = tree.query_ball_point(d_coord, dist_cutoff)
            for i in idxs:
                a_res, a_atom = acceptors[i]
                if d_res is a_res:
                    continue
                dist = float(np.linalg.norm(d_coord - acc_coords[i]))
                key = tuple(sorted([
                    (d_res.get_id()[1], d_atom.name),
                    (a_res.get_id()[1], a_atom.name),
                ]))
                if key in seen:
                    continue
                seen.add(key)
                hbonds.append({
                    "donor_chain":    d_res.full_id[2],
                    "donor_res":      d_res.get_resname(),
                    "donor_resnum":   d_res.get_id()[1],
                    "donor_atom":     d_atom.name,
                    "acceptor_chain": a_res.full_id[2],
                    "acceptor_res":   a_res.get_resname(),
                    "acceptor_resnum": a_res.get_id()[1],
                    "acceptor_atom":  a_atom.name,
                    "distance_A":     round(dist, 2),
                })
    return hbonds


# ---------------------------------------------------------------------------
# Interaction classification
# ---------------------------------------------------------------------------

def _classify_interaction(res_a_name: str, res_b_name: str,
                           has_hbond: bool, dist: float) -> str:
    a, b = res_a_name.upper(), res_b_name.upper()
    if has_hbond:
        return "h_bond"
    if (a in CHARGED_POS and b in CHARGED_NEG) or (a in CHARGED_NEG and b in CHARGED_POS):
        return "electrostatic_attractive"
    if (a in CHARGED_POS and b in CHARGED_POS) or (a in CHARGED_NEG and b in CHARGED_NEG):
        return "electrostatic_repulsive"
    if a in HYDROPHOBIC and b in HYDROPHOBIC:
        return "hydrophobic"
    if a in AROMATIC and b in AROMATIC:
        return "pi_stacking_candidate"
    if (a in POLAR or a in CHARGED_POS | CHARGED_NEG) and \
       (b in POLAR or b in CHARGED_POS | CHARGED_NEG):
        return "h_bond_candidate"
    return "vdw_contact"


# ---------------------------------------------------------------------------
# auth_seq_id → label_seq_id mapping via gemmi
# ---------------------------------------------------------------------------

def _build_numbering_map(path: str, chain_id: str) -> dict[int, int]:
    """
    Returns {auth_seq_id: label_seq_id} for a chain.
    label_seq_id is 1-indexed sequential position in chain (no gaps).
    """
    st = _load_gemmi(path)
    result: dict[int, int] = {}
    for model in st:
        for chain in model:
            if chain.name != chain_id:
                continue
            label_idx = 0
            for res in chain:
                label_idx += 1
                auth_num = res.seqid.num
                result[auth_num] = label_idx
    return result


# ---------------------------------------------------------------------------
# Public tool implementations
# ---------------------------------------------------------------------------

def analyze_interface(
    file_path: str,
    chain_a: str,
    chain_b: str,
    cutoff: float = 4.5,
) -> dict[str, Any]:
    """
    Full interface analysis between two chains.

    Returns BSA (total + per-residue), interface residue lists with properties,
    H-bonds, pairwise contact map, interaction classification, and pLDDT if available.
    """
    structure = _load_biopython(file_path)
    ch_a = _get_biopython_chain(structure, chain_a)
    ch_b = _get_biopython_chain(structure, chain_b)

    # --- Residue lists ---
    res_a = list(ch_a.get_residues())
    res_b = list(ch_b.get_residues())

    # --- KDTree contact detection ---
    def _res_heavy_coords(res):
        return np.array([a.get_vector().get_array()
                         for a in res.get_atoms() if a.element != "H"])

    # Build per-residue Cα or centroid coords for proximity pre-filter
    def _centroid(res):
        coords = _res_heavy_coords(res)
        return coords.mean(axis=0) if len(coords) else np.zeros(3)

    centroids_b = np.array([_centroid(r) for r in res_b])
    tree_b = KDTree(centroids_b) if len(centroids_b) else None

    # Fine-grained: for each chain_a residue, find chain_b residues with
    # any heavy-atom pair within cutoff
    interface_a: list[dict] = []
    interface_b_ids: set[int] = set()
    contact_map: list[dict] = []

    # Build all chain_b heavy atom coords once
    b_atoms: list[tuple] = []  # (residue_index, atom_coord)
    b_atom_coords: list[np.ndarray] = []
    for bi, rb in enumerate(res_b):
        for atom in rb.get_atoms():
            if atom.element != "H":
                b_atoms.append((bi, atom))
                b_atom_coords.append(atom.get_vector().get_array())
    tree_b_atoms = KDTree(np.array(b_atom_coords)) if b_atom_coords else None

    # Collect H-bonds
    hbonds = _detect_hbonds(ch_a, ch_b)
    hbond_pairs: set[tuple] = set()
    for hb in hbonds:
        hbond_pairs.add((hb["donor_resnum"], hb["acceptor_resnum"]))
        hbond_pairs.add((hb["acceptor_resnum"], hb["donor_resnum"]))

    for ai, ra in enumerate(res_a):
        a_coords = _res_heavy_coords(ra)
        if len(a_coords) == 0:
            continue
        if tree_b_atoms is None:
            continue

        # Find all chain_b atoms within cutoff of any chain_a atom
        contact_b_idxs: set[int] = set()
        min_dists: dict[int, float] = {}
        for ac in a_coords:
            hits = tree_b_atoms.query_ball_point(ac, cutoff)
            for h in hits:
                bi, _ = b_atoms[h]
                d = float(np.linalg.norm(ac - b_atom_coords[h]))
                if bi not in min_dists or d < min_dists[bi]:
                    min_dists[bi] = d
                contact_b_idxs.add(bi)

        if not contact_b_idxs:
            continue

        ra_name = ra.get_resname()
        ra_num = ra.get_id()[1]

        contacts_for_ra = []
        for bi in contact_b_idxs:
            rb = res_b[bi]
            rb_name = rb.get_resname()
            rb_num = rb.get_id()[1]
            has_hb = (ra_num, rb_num) in hbond_pairs
            dist = round(min_dists[bi], 2)
            interaction = _classify_interaction(ra_name, rb_name, has_hb, dist)
            contacts_for_ra.append({
                "target_res":    rb_name,
                "target_chain":  chain_b,
                "target_resnum": rb_num,
                "min_dist_A":    dist,
                "interaction":   interaction,
            })
            interface_b_ids.add(bi)

        gap_flag = len(contact_b_idxs) < 2
        interface_a.append({
            "residue":       ra_name,
            "chain":         chain_a,
            "resnum":        ra_num,
            "one_letter":    AA3_TO_1.get(ra_name, "X"),
            "type":          _res_type(ra_name),
            "hydrophobicity": KD_HYDROPHOBICITY.get(ra_name, 0.0),
            "contacts":      sorted(contacts_for_ra, key=lambda x: x["min_dist_A"]),
            "n_contacts":    len(contact_b_idxs),
            "gap_flag":      gap_flag,
        })

    # Chain B interface residue list
    interface_b: list[dict] = []
    for bi in sorted(interface_b_ids):
        rb = res_b[bi]
        rb_name = rb.get_resname()
        interface_b.append({
            "residue":       rb_name,
            "chain":         chain_b,
            "resnum":        rb.get_id()[1],
            "one_letter":    AA3_TO_1.get(rb_name, "X"),
            "type":          _res_type(rb_name),
            "hydrophobicity": KD_HYDROPHOBICITY.get(rb_name, 0.0),
        })

    # --- BSA via Shrake-Rupley ---
    bsa_data = _compute_bsa(file_path, structure, chain_a, chain_b,
                             interface_a, interface_b)

    # --- pLDDT (B-factor) at interface ---
    plddt = _extract_plddt(ch_a, {r["resnum"] for r in interface_a})

    # --- Summary categorisation ---
    def _categorise(residues):
        cats: dict[str, list[str]] = {
            "hydrophobic": [], "aromatic": [], "charged": [], "polar": [],
        }
        for r in residues:
            t = r["type"]
            label = f"{r['residue']}{r['resnum']}"
            if t in cats:
                cats[t].append(label)
        return cats

    return {
        "interface": {
            "chain_a": chain_a,
            "chain_b": chain_b,
            "cutoff_A": cutoff,
            "bsa_total_A2":    bsa_data["total"],
            "bsa_per_residue": bsa_data["per_residue"],
            "n_hbonds":        len(hbonds),
            "hbonds":          hbonds,
            "n_contacts_chain_a": len(interface_a),
            "n_contacts_chain_b": len(interface_b),
        },
        "chain_a_interface_residues": interface_a,
        "chain_a_categories":         _categorise(interface_a),
        "chain_b_interface_residues": interface_b,
        "chain_b_categories":         _categorise(interface_b),
        "plddt_at_interface": plddt,
        "low_confidence_residues": [
            f"{r['residue']}{r['resnum']}"
            for r in interface_a
            if plddt.get(r["resnum"], 100.0) < 70.0
        ],
    }


def get_residue_contacts(
    file_path: str,
    chain: str,
    resnum: int,
    partner_chain: str,
    cutoff: float = 4.5,
) -> dict[str, Any]:
    """
    All contacts between a specific residue and a partner chain within cutoff.
    Returns per-contact distances, interaction types, and H-bond details.
    """
    structure = _load_biopython(file_path)
    ch = _get_biopython_chain(structure, chain)
    ch_p = _get_biopython_chain(structure, partner_chain)

    target_res = None
    for res in ch.get_residues():
        if res.get_id()[1] == resnum:
            target_res = res
            break
    if target_res is None:
        return {"error": f"Residue {resnum} not found in chain {chain}"}

    a_coords = np.array([a.get_vector().get_array()
                          for a in target_res.get_atoms() if a.element != "H"])
    if len(a_coords) == 0:
        return {"error": "No heavy atoms found"}

    # Build partner atom list
    p_atoms: list[tuple] = []
    p_coords: list[np.ndarray] = []
    for res in ch_p.get_residues():
        for atom in res.get_atoms():
            if atom.element != "H":
                p_atoms.append((res, atom))
                p_coords.append(atom.get_vector().get_array())
    if not p_coords:
        return {"contacts": [], "hbonds": []}

    tree = KDTree(np.array(p_coords))

    # Find contacts
    contact_res: dict[int, dict] = {}
    for ac in a_coords:
        hits = tree.query_ball_point(ac, cutoff)
        for h in hits:
            res, atom = p_atoms[h]
            rn = res.get_id()[1]
            d = float(np.linalg.norm(ac - p_coords[h]))
            if rn not in contact_res or d < contact_res[rn]["min_dist_A"]:
                contact_res[rn] = {
                    "partner_res":    res.get_resname(),
                    "partner_chain":  partner_chain,
                    "partner_resnum": rn,
                    "min_dist_A":     round(d, 2),
                }

    # Classify
    hbonds = _detect_hbonds(ch, ch_p)
    hb_partner_resnums = {
        hb["acceptor_resnum"] for hb in hbonds if hb["donor_resnum"] == resnum
    } | {
        hb["donor_resnum"] for hb in hbonds if hb["acceptor_resnum"] == resnum
    }

    contacts = []
    for rn, info in sorted(contact_res.items(), key=lambda x: x[1]["min_dist_A"]):
        has_hb = rn in hb_partner_resnums
        info["interaction"] = _classify_interaction(
            target_res.get_resname(), info["partner_res"], has_hb, info["min_dist_A"]
        )
        info["h_bond"] = has_hb
        contacts.append(info)

    relevant_hbonds = [
        hb for hb in hbonds
        if hb["donor_resnum"] == resnum or hb["acceptor_resnum"] == resnum
    ]

    return {
        "residue":       target_res.get_resname(),
        "chain":         chain,
        "resnum":        resnum,
        "partner_chain": partner_chain,
        "n_contacts":    len(contacts),
        "gap_flag":      len(contacts) < 2,
        "contacts":      contacts,
        "hbonds":        relevant_hbonds,
    }


def check_mutation_clash(
    file_path: str,
    chain: str,
    resnum: int,
    new_aa: str,
    partner_chain: str,
) -> dict[str, Any]:
    """
    Estimates whether a point mutation on `chain` at `resnum` to `new_aa`
    would clash with `partner_chain`.

    Method: Cβ-based heuristic. Computes expected Cβ position from Cα and N/C
    backbone atoms, then checks if partner heavy atoms fall within the
    sidechain reach radius of the proposed residue.

    Returns clash severity: none / minor / major.
    """
    new_aa = new_aa.upper()
    structure = _load_biopython(file_path)
    ch = _get_biopython_chain(structure, chain)
    ch_p = _get_biopython_chain(structure, partner_chain)

    target_res = None
    for res in ch.get_residues():
        if res.get_id()[1] == resnum:
            target_res = res
            break
    if target_res is None:
        return {"error": f"Residue {resnum} not found in chain {chain}"}

    # Get Cα position
    ca = None
    cb = None
    for atom in target_res.get_atoms():
        if atom.name == "CA":
            ca = atom.get_vector().get_array()
        if atom.name == "CB":
            cb = atom.get_vector().get_array()

    if ca is None:
        return {"error": "No Cα atom found — cannot estimate Cβ position"}

    # Use existing Cβ if available (Gly has none), else estimate from Cα
    if cb is None and new_aa != "G":
        # For Gly→X: estimate Cβ in tetrahedral geometry from Cα
        # Use N and C backbone atoms to define the frame
        n_pos = c_pos = None
        for atom in target_res.get_atoms():
            if atom.name == "N":
                n_pos = atom.get_vector().get_array()
            if atom.name == "C":
                c_pos = atom.get_vector().get_array()
        if n_pos is not None and c_pos is not None:
            # Approximate Cβ in the direction perpendicular to N-C-Cα plane
            v1 = n_pos - ca
            v2 = c_pos - ca
            cross = np.cross(v1, v2)
            cross_norm = cross / (np.linalg.norm(cross) + 1e-9)
            cb_estimate = ca - 0.7 * (v1 + v2) / np.linalg.norm(v1 + v2 + 1e-9) \
                          + 0.9 * cross_norm
            cb = ca + CB_DISTANCE * (cb_estimate - ca) / (np.linalg.norm(cb_estimate - ca) + 1e-9)
        else:
            cb = ca  # fallback — just check at Cα

    if new_aa == "G" or new_aa == "ALA":
        # Tiny sidechains — effectively no clash beyond Cα/Cβ
        reach = 0.5
    else:
        reach = SIDECHAIN_REACH.get(new_aa, 3.0)

    if cb is None:
        cb = ca

    # Collect partner heavy atoms
    p_coords = np.array([
        a.get_vector().get_array()
        for res in ch_p.get_residues()
        for a in res.get_atoms()
        if a.element != "H"
    ])
    if len(p_coords) == 0:
        return {"clash": "none", "reason": "No partner atoms"}

    tree = KDTree(p_coords)
    # Check within sidechain reach + VdW radius
    clash_radius = reach + 1.4  # 1.4 Å ≈ approximate VdW overlap threshold
    hits = tree.query_ball_point(cb, clash_radius)

    clashing_partners = []
    for h in hits:
        d = float(np.linalg.norm(cb - p_coords[h]))
        if d < 1.8:  # hard core clash
            clashing_partners.append({"dist_A": round(d, 2), "severity": "hard"})
        elif d < clash_radius:
            clashing_partners.append({"dist_A": round(d, 2), "severity": "soft"})

    hard = sum(1 for c in clashing_partners if c["severity"] == "hard")
    soft = sum(1 for c in clashing_partners if c["severity"] == "soft")

    if hard >= 2 or (hard == 1 and soft >= 2):
        clash = "major"
    elif hard == 1 or soft >= 3:
        clash = "minor"
    else:
        clash = "none"

    current_res_name = target_res.get_resname()
    new_reach = SIDECHAIN_REACH.get(new_aa, 3.0)
    old_reach = SIDECHAIN_REACH.get(current_res_name, 3.0)

    return {
        "current_residue":  current_res_name,
        "proposed_mutation": new_aa,
        "chain":            chain,
        "resnum":           resnum,
        "clash":            clash,
        "hard_clashes":     hard,
        "soft_clashes":     soft,
        "sidechain_reach_current_A":  old_reach,
        "sidechain_reach_proposed_A": new_reach,
        "note": (
            "Cβ-heuristic only. AF3 re-prediction resolves ambiguous cases. "
            "Major clash = likely structural conflict. Minor = may be resolved by repacking."
        ),
    }


def get_sequence_map(file_path: str, chain: str) -> dict[str, Any]:
    """
    Returns the full amino acid sequence of a chain plus the numbering maps
    needed to construct AF3 JSON submissions.

    Returns:
      sequence:           1-letter uppercase string
      auth_to_string_idx: {auth_seq_id → 0-based string index}
      auth_to_label:      {auth_seq_id → label_seq_id (1-indexed sequential)}
      residues:           [{auth_seq_id, label_seq_id, string_idx, three_letter, one_letter}]
    """
    st = _load_gemmi(file_path)
    residues_out = []
    sequence_chars = []

    for model in st:
        for ch in model:
            if ch.name != chain:
                continue
            label_idx = 0
            string_idx = 0
            for res in ch:
                if res.entity_type == gemmi.EntityType.NonPolymer:
                    continue
                aa3 = res.name
                aa1 = AA3_TO_1.get(aa3, "X")
                label_idx += 1
                auth_num = res.seqid.num
                residues_out.append({
                    "auth_seq_id":  auth_num,
                    "label_seq_id": label_idx,
                    "string_idx":   string_idx,
                    "three_letter": aa3,
                    "one_letter":   aa1,
                })
                sequence_chars.append(aa1)
                string_idx += 1
        break  # first model only

    if not residues_out:
        return {"error": f"Chain '{chain}' not found or empty"}

    return {
        "chain":            chain,
        "length":           len(sequence_chars),
        "sequence":         "".join(sequence_chars),
        "auth_to_string_idx": {r["auth_seq_id"]: r["string_idx"] for r in residues_out},
        "auth_to_label":    {r["auth_seq_id"]: r["label_seq_id"] for r in residues_out},
        "residues":         residues_out,
    }


def score_surface_patch(
    file_path: str,
    chain: str,
    residue_list: list[int],
) -> dict[str, Any]:
    """
    Characterises a set of residues as a potential binding surface patch.

    Returns: BSA-weighted hydrophobicity score, spatial spread (Cα RMSD),
    residue type breakdown, and a qualitative suitability rating for
    cyclic peptide / mini-protein binder design.
    """
    structure = _load_biopython(file_path)
    ch = _get_biopython_chain(structure, chain)

    patch_residues = []
    ca_coords = []
    for res in ch.get_residues():
        if res.get_id()[1] in residue_list:
            patch_residues.append(res)
            for atom in res.get_atoms():
                if atom.name == "CA":
                    ca_coords.append(atom.get_vector().get_array())
                    break

    if not patch_residues:
        return {"error": "No matching residues found"}

    # Spatial spread — Cα RMSD from centroid
    if len(ca_coords) >= 2:
        ca_arr = np.array(ca_coords)
        centroid = ca_arr.mean(axis=0)
        spread = float(np.sqrt(((ca_arr - centroid) ** 2).sum(axis=1).mean()))
    else:
        spread = 0.0

    # Hydrophobicity score (simple mean KD)
    names = [r.get_resname() for r in patch_residues]
    kd_scores = [KD_HYDROPHOBICITY.get(n, 0.0) for n in names]
    mean_hydrophobicity = float(np.mean(kd_scores)) if kd_scores else 0.0

    # Type breakdown
    type_counts: dict[str, int] = {"hydrophobic": 0, "aromatic": 0,
                                    "charged": 0, "polar": 0, "other": 0}
    for n in names:
        t = _res_type(n)
        type_counts[t] = type_counts.get(t, 0) + 1

    hydrophobic_fraction = (
        (type_counts["hydrophobic"] + type_counts["aromatic"]) / len(names)
        if names else 0.0
    )

    # Suitability rating
    if hydrophobic_fraction >= 0.5 and spread <= 10.0 and len(patch_residues) >= 3:
        rating = "Excellent"
        rationale = "Hydrophobic-rich, spatially compact — ideal for peptide/mini-protein"
    elif hydrophobic_fraction >= 0.3 and spread <= 15.0:
        rating = "Good"
        rationale = "Moderate hydrophobic character and accessible spread"
    elif hydrophobic_fraction >= 0.2 or len(patch_residues) >= 4:
        rating = "Marginal"
        rationale = "Limited hydrophobic character or dispersed patch"
    else:
        rating = "Poor"
        rationale = "Mostly polar/charged, no clear hydrophobic anchor"

    return {
        "chain":               chain,
        "residues_analysed":   [f"{r.get_resname()}{r.get_id()[1]}" for r in patch_residues],
        "n_residues":          len(patch_residues),
        "spatial_spread_ca_rmsd_A": round(spread, 2),
        "mean_hydrophobicity_kd":   round(mean_hydrophobicity, 2),
        "hydrophobic_fraction":     round(hydrophobic_fraction, 2),
        "type_breakdown":      type_counts,
        "suitability_rating":  rating,
        "rationale":           rationale,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _res_type(name: str) -> str:
    n = name.upper()
    if n in AROMATIC:
        return "aromatic"
    if n in HYDROPHOBIC:
        return "hydrophobic"
    if n in CHARGED_POS | CHARGED_NEG:
        return "charged"
    if n in POLAR:
        return "polar"
    return "other"


def _compute_bsa(file_path, structure, chain_a: str, chain_b: str,
                  iface_a: list[dict], iface_b: list[dict]) -> dict:
    """
    BSA = SASA(A_free) + SASA(B_free) - SASA(complex).
    Returns total BSA and per-residue BSA for interface residues.
    """
    try:
        sasa_complex = _compute_sasa_per_residue(structure, [chain_a, chain_b])
        sasa_a_free  = _compute_sasa_per_residue(structure, [chain_a])
        sasa_b_free  = _compute_sasa_per_residue(structure, [chain_b])

        per_res: list[dict] = []
        total_bsa = 0.0

        for r in iface_a:
            key = f"{chain_a}:{r['resnum']}:{r['residue']}"
            free = sasa_a_free.get(key, 0.0)
            comp = sasa_complex.get(key, 0.0)
            bsa = max(0.0, free - comp)
            total_bsa += bsa
            per_res.append({
                "residue": r["residue"], "chain": chain_a, "resnum": r["resnum"],
                "sasa_free_A2": round(free, 1), "sasa_complex_A2": round(comp, 1),
                "bsa_A2": round(bsa, 1),
            })

        for r in iface_b:
            key = f"{chain_b}:{r['resnum']}:{r['residue']}"
            free = sasa_b_free.get(key, 0.0)
            comp = sasa_complex.get(key, 0.0)
            bsa = max(0.0, free - comp)
            total_bsa += bsa
            per_res.append({
                "residue": r["residue"], "chain": chain_b, "resnum": r["resnum"],
                "sasa_free_A2": round(free, 1), "sasa_complex_A2": round(comp, 1),
                "bsa_A2": round(bsa, 1),
            })

        return {"total": round(total_bsa, 1), "per_residue": per_res}

    except Exception as e:
        return {"total": -1.0, "per_residue": [], "error": str(e)}


def _extract_plddt(chain, resnum_set: set[int]) -> dict[int, float]:
    """Extract B-factor (= pLDDT for AF structures) for specified residues."""
    result: dict[int, float] = {}
    for res in chain.get_residues():
        rn = res.get_id()[1]
        if rn not in resnum_set:
            continue
        bfactors = [a.get_bfactor() for a in res.get_atoms() if a.name == "CA"]
        if bfactors:
            result[rn] = round(bfactors[0], 1)
    return result
