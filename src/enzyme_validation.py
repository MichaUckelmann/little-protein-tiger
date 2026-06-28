#!/usr/bin/env python
"""
enzyme_validation.py -- deterministic de novo enzyme-design validators.

Ported (faithfully) from foundry/current_best_practice:

  * validate/validate_design.py        -- THE canonical raw-RFD3 michaelase validator
                                          (TIER1 CAT / TIER2 GEOM / TIER3 STITCH / TIER4 LIG)
  * validate/validate_transferase.py   -- adds the rotamer-REACHABILITY gate
                                          (CA -> fixed functional atom, e.g. CA->CG ~2.55 A)
  * validate/scan_emergent_hole.py     -> scan_emergent_oxyanion_hole()
  * refold/analyze_rf3_michaelase.py   -> analyze_refold(mode="rf3")
  * refold/analyze_chai_refold.py      -> analyze_refold(mode="chai")
  * refold/rf3_to_pdb.py:convert       -> fix_lcolon_ligand()
  * refold/fix_chai_pdb.py:fix_file    -> fix_chai_overflow()

The original scripts hard-code the michaelase/transferase atom names (O15, N5, C0, C18,
C14, B452, MOTIF_KEYS, ...). Here those are generalized into a caller-supplied ``catspec``
dict so the same gate logic works for any theozyme/TS scaffold. The original numeric
thresholds are preserved verbatim in the module-level ``TH`` dict, with the differences
between validate_design.py and validate_transferase.py reconciled and documented inline.

Pure biotite + numpy. NO pyrosetta. Reads both ``.cif`` and ``.cif.gz``.

-------------------------------------------------------------------------------
control_calibrate.py CONCLUSION (ported as a comment, NOT as code -- it imports pyrosetta):

  Raw whole-structure cart_bonded / fa_rep are UNINFORMATIVE on un-relaxed RFD3 all-atom
  output. Measured median worst-residue cart_bonded ~165 (michaelase), ~172 (the 'good'
  gemdime transferase control), ~214 (cortisol); it relaxes out to <5 at 0.8-1.1 A
  CA-RMSD. The strain lives in RFD3's PLACEHOLDER SIDECHAINS, which LigandMPNN redesigns.
  control_calibrate also used CA-C-O ideal = 120.8 deg (vs 120.5 in the gate scripts); the
  gate scripts' 120.5 is adopted below.

  => Therefore this module gates ONLY on stage-appropriate, pure-geometric criteria:
     backbone covalent geometry + backbone/CB steric clashes + motif stitching +
     catalytic ORIENTATION + rotamer REACHABILITY (CA -> fixed functional atom).
     Sidechain clashes / un-relaxed sidechain strain are reported as INFO, never gated.

-------------------------------------------------------------------------------
The 7 critical validation lessons (current_best_practice/README.md) that this module obeys:
  1. Distance-only catalytic checks are a tautology (RFD3 pins motif+ligand). Gate on
     ORIENTATION / REACHABILITY -- and locate catalytic residues by INDEX (diffused_index_map),
     never by proximity.
  2. Raw cart_bonded / fa_rep are uninformative on un-relaxed output -> backbone geometry only.
  3. Catalytic viability = rotamer-REACHABILITY (CA->fixed functional atom), not RFD3's output
     sidechain bonds. CB is rebuilt by MPNN. If CB fixed -> test CA-CB; if only deeper atoms
     fixed (e.g. Glu CG,CD,OE) -> test CA->CG ~2.55 A (rotamer-invariant).
  4. The decisive gate is the refold and it is harsh; raw pass is necessary, not sufficient.
     For a TS ligand you MUST fix/template it (Chai/RF3 fold from SMILES, never a TS).
  5. Check refold ligand integrity NAME-INDEPENDENTLY (Chai renumbers atoms via canonical
     SMILES) -- bond-graph degree sequence / detached-atom count, not name-mapped bonds.
  6. A "cloud of atoms" in a viewer is usually a file-format bug, not an explosion -> the
     two viewer fixers (fix_lcolon_ligand / fix_chai_overflow) re-emit compliant coordinates.
  7. Validate the validator -- be as skeptical of illusory failures as of illusory passes.

-------------------------------------------------------------------------------
``catspec`` schema (caller-supplied; only ``catalytic`` is strictly required for CAT):

    catspec = {
      "ligand_resname": "L:G",            # optional; filter hetero atoms by this resname
      "forming_bond":   ["C0", "C18"],    # ligand atom pair defining the partial/forming bond (LIG tier)
      "acceptors":      ["O15", "N5"],    # ligand atoms that develop charge / accept H-bonds
      "oxyanion_atom":  "O15",            # the acceptor that develops the oxyanion (gets the
                                          #   sc orientation + approach gate). default acceptors[0].
      "approach_atom":  "C14",            # ligand C adjacent to oxyanion_atom; defines the
                                          #   approach angle (approach_atom - oxyanion - donor).
      "protein_chain":  "A",              # chain used for backbone GEOM/clash tiers + nres
      "catalytic": {                      # motif-key (as in diffused_index_map) -> contact spec
        "A1": {"donor": "ND2", "acc": "N5",  "mode": "sc"},          # imine-organizing Asn
        "A2": {"donor": "OH",  "acc": "O15", "mode": "sc"},          # oxyanion-hole Tyr
        "A3": {"donor": "NE2", "acc": "O15", "mode": "sc"},          # oxyanion-hole His
        # optional per-entry keys:
        #   "orient": bool      -- force-enable/disable the orientation gate
        #                          (default: bb -> True; sc -> acc == oxyanion_atom)
        #   "reach_atom": str   -- shallowest FIXED functional atom for the reachability
        #                          test (default "CB"). "CB" -> CA-CB window (severed if
        #                          CA-CB > 1.90, gated); any deeper atom (e.g. "CG") ->
        #                          CA->atom window [2.1, 3.0] (gated, transferase-style).
        #   "reach_lo"/"reach_hi": override the reachability window.
        #   "resn": str         -- residue type; only used by the closest-of-type fallback
        #                          when diffused_index_map is absent.
      },
    }
"""
from __future__ import annotations

import csv
import glob
import gzip
import io
import json
import os
import re

import numpy as np
import biotite.structure.io.pdbx as pdbx


# --------------------------------------------------------------------------- #
# geometry helpers (verbatim from the foundry scripts)
# --------------------------------------------------------------------------- #
def U(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


def dist(a, b):
    return float(np.linalg.norm(a - b))


def ang(a, b, c):
    return float(np.degrees(np.arccos(np.clip(np.dot(U(a - b), U(c - b)), -1, 1))))


# --------------------------------------------------------------------------- #
# constants tables (reconciled validate_design.py + validate_transferase.py)
# --------------------------------------------------------------------------- #

# Donor heavy-atom per residue type -- used ONLY by the closest-of-type fallback / auto-detect
# (validate_design.py DONOR_ATOM). NE2 is HIS here (GLN shares NE2; disambiguate via catspec).
DONOR_ATOM = {"SER": "OG", "THR": "OG1", "TYR": "OH", "ASN": "ND2", "GLN": "NE2",
              "HIS": "NE2", "GLY": "N", "ALA": "N"}
# best-effort reverse map (closest-of-type fallback when catspec gives no explicit "resn")
_DONOR2RESN = {"OG": "SER", "OG1": "THR", "OH": "TYR", "ND2": "ASN", "NE2": "HIS"}

# Catalytic-residue covalent ideals. bonds: (a, b, ideal_len); angles: (a, b, c, ideal_deg).
# ASN/SER/THR/TYR/HIS (with angle ideals) are from validate_design.py IDEAL; the additional
# sidechain bond tables (GLU/GLN/LYS/ARG/ILE/PRO/ALA) are from validate_transferase.py
# SC_BONDS (severing detection only -- no angle ideals were defined for those).
IDEAL = {
    "ASN": dict(bonds=[("CA", "CB", 1.530), ("CB", "CG", 1.516), ("CG", "OD1", 1.231), ("CG", "ND2", 1.328)],
                angles=[("CA", "CB", "CG", 113.4), ("CB", "CG", "ND2", 116.5), ("OD1", "CG", "ND2", 122.5)]),
    "SER": dict(bonds=[("CA", "CB", 1.530), ("CB", "OG", 1.417)],
                angles=[("CA", "CB", "OG", 111.1)]),
    "THR": dict(bonds=[("CA", "CB", 1.540), ("CB", "OG1", 1.433), ("CB", "CG2", 1.521)],
                angles=[("CA", "CB", "OG1", 109.6), ("OG1", "CB", "CG2", 110.0)]),
    "TYR": dict(bonds=[("CA", "CB", 1.530), ("CB", "CG", 1.512), ("CZ", "OH", 1.376)],
                angles=[("CA", "CB", "CG", 113.8)]),
    "HIS": dict(bonds=[("CA", "CB", 1.530), ("CB", "CG", 1.497)],
                angles=[("CA", "CB", "CG", 113.8)]),
    # severing-only tables (validate_transferase.py SC_BONDS):
    "GLU": dict(bonds=[("CA", "CB", 1.53), ("CB", "CG", 1.52), ("CG", "CD", 1.52),
                       ("CD", "OE1", 1.25), ("CD", "OE2", 1.25)], angles=[]),
    "GLN": dict(bonds=[("CA", "CB", 1.53), ("CB", "CG", 1.52), ("CG", "CD", 1.52),
                       ("CD", "OE1", 1.23), ("CD", "NE2", 1.33)], angles=[]),
    "LYS": dict(bonds=[("CA", "CB", 1.53), ("CB", "CG", 1.52), ("CG", "CD", 1.52),
                       ("CD", "CE", 1.52), ("CE", "NZ", 1.49)], angles=[]),
    "ARG": dict(bonds=[("CA", "CB", 1.53), ("CB", "CG", 1.52), ("CG", "CD", 1.52),
                       ("CD", "NE", 1.46), ("NE", "CZ", 1.33), ("CZ", "NH1", 1.33),
                       ("CZ", "NH2", 1.33)], angles=[]),
    "ILE": dict(bonds=[("CA", "CB", 1.54), ("CB", "CG1", 1.53), ("CB", "CG2", 1.52),
                       ("CG1", "CD1", 1.51)], angles=[]),
    "PRO": dict(bonds=[("CA", "CB", 1.53), ("CB", "CG", 1.49), ("CG", "CD", 1.50)], angles=[]),
    "ALA": dict(bonds=[("CA", "CB", 1.52)], angles=[]),
}

# Backbone covalent ideals (intra-residue). CA-C-O = 120.5 (validate_design.py &
# validate_transferase.py; control_calibrate.py used 120.8 -- diagnostic only, not adopted).
BB_BONDS = [("N", "CA", 1.458), ("CA", "C", 1.525), ("C", "O", 1.231)]
BB_ANG = [("N", "CA", "C", 111.0), ("CA", "C", "O", 120.5)]
PEP_IDEAL = 1.329                      # ideal peptide C(i)-N(i+1) bond length
BB_SET = ("N", "CA", "C", "O", "CB")   # backbone + CB: the atoms whose clashes are GATED

# Reconciled threshold table. Sources annotated per gate.
TH = dict(
    # ---- TIER1 CAT: contact distances (validate_design.py TH) ----
    cat_dist_max=3.6,         # donor -> oxyanion/H-bond acceptor max distance
    asn_dist_max=3.6,         # donor -> amine/imine acceptor max distance (== cat_dist_max)
    # ---- TIER1 CAT: orientation (validate_design.py TH) ----
    bb_amide_ang_max=40.0,    # backbone amide N-H deviation from pointing at acceptor (mode "bb")
    sc_oh_dev_max=45.0,       # |CB-donor-acceptor angle - 109.5| max (sidechain donor)
    approach_lo=90.0,         # approach_atom - oxyanion - donor angle window (oxyanion hole)
    approach_hi=150.0,
    # ---- TIER1 CAT: rotamer reachability ----
    cacb_lo=1.46, cacb_hi=1.60,   # CA-CB "healthy" window (info; validate_design.py)
    cacb_sever=1.90,              # CA-CB severed cut -> backbone can't support sidechain (gated)
    ca_cg_lo=2.10, ca_cg_hi=3.00, # CA->fixed deeper functional atom window (validate_transferase.py
    ca_cg_ideal=2.55,             #   glu_reach: CA->CG ~2.55 A in EVERY Glu rotamer) -- GATED
    cov_strict=0.15,              # covalent bond dev -> cat_cov_ok False (info)
    cov_sever=0.40,               # covalent bond dev -> severed (validate_transferase.py SEVER, gated)
    cov_ang_tol=20.0,             # covalent angle dev -> cat_cov_ok False (info)
    # ---- TIER2 GEOM: backbone covalent geometry + clashes (validate_design.py TH) ----
    bb_bond_tol=0.10, bb_ang_tol=15.0,
    pep_lo=1.25, pep_hi=1.45, pep_break=1.7,
    clash_cut=2.0, max_bb_clash=0,
    # ---- TIER3 STITCH (validate_design.py TH) ----
    max_ca_dev=1.5,           # RFD3 max_ca_deviation of the fixed motif (GATED)
    join_rmsd_max=1.5,        # defined in validate_design.py TH but NOT used as the gate;
    join_rmsd_gate=6.0,       # the ACTUAL stitch gate is a very loose join_point backstop (6.0)
    # ---- TIER4 LIG (validate_design.py TH) ----
    forming_ideal=2.244, forming_tol=0.12,
    # ---- refold (analyze_rf3_michaelase.py / analyze_chai_refold.py) ----
    refold_cat_max=3.5,       # index-located catalytic donor -> acceptor <= 3.5 A
    refold_plddt_min=0.80,    # rf3: GOOD requires overall pLDDT >= 0.80
    chai_plddt_min=0.70,      # chai: "resembles" requires binder pLDDT >= 0.70
    chai_rmsd_max=2.5,        # chai: "resembles" requires CA-RMSD <= 2.5 A
    lig_bond_lo=1.00, lig_bond_hi=1.95,   # name-independent ligand covalent bond window
    lig_nbond_tol=3,          # |refold bond count - design bond count| <= 3
)


# --------------------------------------------------------------------------- #
# structure IO
# --------------------------------------------------------------------------- #
def _read_structure(path):
    """Load model 1 from a .cif or .cif.gz file as a biotite AtomArray."""
    p = str(path)
    if p.endswith(".gz"):
        with gzip.open(p, "rt") as fh:
            txt = fh.read()
    else:
        with open(p) as fh:
            txt = fh.read()
    return pdbx.get_structure(pdbx.CIFFile.read(io.StringIO(txt)), model=1)


def _design_json_path(cif_path):
    """Sibling <design>.json for a <design>.cif / <design>.cif.gz path."""
    p = str(cif_path)
    if p.endswith(".cif.gz"):
        return p[:-len(".cif.gz")] + ".json"
    if p.endswith(".cif"):
        return p[:-len(".cif")] + ".json"
    return os.path.splitext(p)[0] + ".json"


def _by_res(prot):
    """Map (chain, res_id) -> {atom_name: coord} over a protein AtomArray."""
    d = {}
    for nm, co, rid, ch in zip(prot.atom_name, prot.coord, prot.res_id, prot.chain_id):
        d.setdefault((str(ch), int(rid)), {})[str(nm)] = co
    return d


def _ligand(a, ligand_resname=None):
    """Hetero atoms, optionally filtered to a specific ligand resname."""
    lig = a[a.hetero]
    if ligand_resname is not None:
        sub = lig[lig.res_name == ligand_resname]
        if len(sub):
            return sub
    return lig


def _lig_coord(lig, name):
    m = lig.atom_name == name
    return lig.coord[m][0] if m.any() else None


# --------------------------------------------------------------------------- #
# catalytic-residue location (BY INDEX -- lesson #1)
# --------------------------------------------------------------------------- #
def _parse_index(token):
    """diffused_index_map value 'A86' -> ('A', 86); '86' -> (None, 86)."""
    m = re.match(r"([A-Za-z]*)(-?\d+)$", str(token))
    if not m:
        return None, None
    ch = m.group(1) or None
    return ch, int(m.group(2))


def _closest_of_type(prot_all, resn, donor, acc_coord):
    """Documented FALLBACK only: residue of type ``resn`` whose ``donor`` atom is closest to
    the acceptor. Returns (chain, res_id) or (None, None). Used solely when the
    diffused_index_map is absent (proximity is otherwise a tautology -- lesson #1)."""
    if resn is None or acc_coord is None:
        return None, None
    s = prot_all[(prot_all.res_name == resn) & (prot_all.atom_name == donor)]
    if not len(s):
        return None, None
    j = int(np.argmin(np.linalg.norm(s.coord - acc_coord, axis=1)))
    return str(s.chain_id[j]), int(s.res_id[j])


# --------------------------------------------------------------------------- #
# PUBLIC: validate_design
# --------------------------------------------------------------------------- #
def validate_design(cif_path, catspec, json_path=None):
    """Per-design validator for raw RFD3 enzyme-design output (canonical michaelase logic
    from validate_design.py, generalized via ``catspec`` and extended with the
    validate_transferase.py rotamer-reachability gate).

    Four tiers, all pure-geometric and stage-appropriate (see module docstring + lessons):
      TIER1 CAT     active-site geometry: contact distance + donor ORIENTATION + rotamer
                    REACHABILITY (CA -> fixed functional atom). Sidechain covalent strain
                    and the tight CA-CB window are reported as INFO, not gated.
      TIER2 GEOM    backbone-only covalent geometry + backbone/CB steric clashes
                    (sidechain clashes reported as INFO).
      TIER3 STITCH  motif stitched in: RFD3 n_chainbreaks == 0 + max_ca_deviation small.
      TIER4 LIG     TS ligand forming bond preserved.

    Catalytic residues are located BY INDEX via the design's ``diffused_index_map`` (read from
    the sibling ``<design>.json``), NOT by proximity. If the index map is absent, falls back to
    closest-residue-of-type (and flags ``used_fallback`` in the row).

    Returns a dict row mirroring validate_v2.csv: PASS + GEOM_OK/CAT_OK/STITCH_OK/LIG_OK plus
    all sub-metrics.
    """
    a = _read_structure(cif_path)
    pchain = catspec.get("protein_chain", "A")
    prot_all = a[~a.hetero]
    prot = prot_all[prot_all.chain_id == pchain] if pchain is not None else prot_all
    lig = _ligand(a, catspec.get("ligand_resname"))

    jp = json_path or _design_json_path(cif_path)
    J = json.load(open(jp)) if os.path.exists(jp) else {}
    dim = J.get("diffused_index_map", {})
    M = J.get("metrics", {})

    Rall = _by_res(prot_all)
    R = _by_res(prot)            # protein_chain only (for backbone tier)
    ids = sorted(r for (c, r) in R)

    design = os.path.basename(str(cif_path))
    for suf in (".cif.gz", ".cif"):
        if design.endswith(suf):
            design = design[:-len(suf)]
            break
    row = dict(design=design,
               nres=int(M.get("num_residues", len({r for (c, r) in R}))),
               used_fallback=0)

    acceptors = catspec.get("acceptors", [])
    oxy_atom = catspec.get("oxyanion_atom") or (acceptors[0] if acceptors else None)
    approach_atom = catspec.get("approach_atom")
    appr_coord = _lig_coord(lig, approach_atom) if approach_atom else None

    # ---- TIER 2: backbone covalent geometry + clashes ---------------------- #
    bad_bond = bad_ang = pep_bad = pep_break = 0
    for r in ids:
        at = R[(pchain, r)] if pchain is not None else None
        if at is None:
            continue
        for x, y, ideal in BB_BONDS:
            if x in at and y in at and abs(dist(at[x], at[y]) - ideal) > TH["bb_bond_tol"]:
                bad_bond += 1
        for x, y, z, ideal in BB_ANG:
            if x in at and y in at and z in at and abs(ang(at[x], at[y], at[z]) - ideal) > TH["bb_ang_tol"]:
                bad_ang += 1
        nxt = R.get((pchain, r + 1))
        if nxt is not None and "C" in at and "N" in nxt:
            d = dist(at["C"], nxt["N"])
            if d > TH["pep_break"]:
                pep_break += 1
            elif abs(d - PEP_IDEAL) > (TH["pep_hi"] - PEP_IDEAL):
                pep_bad += 1
    # clashes: backbone+CB only (GATED). sidechain clashes are repacked by MPNN -> INFO.
    bb = prot[np.isin(prot.atom_name, list(BB_SET))]
    bbc = bb.coord
    bbr = np.array([int(x) for x in bb.res_id])
    bb_clash = 0
    for i in range(len(bbc)):
        dd = np.linalg.norm(bbc[i + 1:] - bbc[i], axis=1)
        for k in np.where(dd < TH["clash_cut"])[0]:
            j = i + 1 + k
            if abs(bbr[i] - bbr[j]) > 1:
                bb_clash += 1
    allc = prot.coord
    allr = np.array([int(x) for x in prot.res_id])
    sc_clash = 0
    for i in range(len(allc)):
        dd = np.linalg.norm(allc[i + 1:] - allc[i], axis=1)
        for k in np.where(dd < TH["clash_cut"])[0]:
            j = i + 1 + k
            if allr[i] != allr[j] and abs(allr[i] - allr[j]) != 1:
                sc_clash += 1
    row.update(bb_bad_bond=bad_bond, bb_bad_ang=bad_ang, pep_bad=pep_bad, pep_break=pep_break,
               bb_clash=bb_clash, sc_clash=sc_clash)
    geom_ok = (bad_bond == 0 and bad_ang == 0 and pep_bad == 0 and pep_break == 0
               and bb_clash <= TH["max_bb_clash"])

    # ---- TIER 1: active-site geometry -------------------------------------- #
    catalytic = catspec.get("catalytic", {}) or {}
    cat = {}
    cat_dist_ok = cat_orient_ok = cacb_ok = cat_cov_ok = cat_reach_ok = True
    cat_severed = False
    for key, spec in catalytic.items():
        donor = spec["donor"]
        acc_name = spec["acc"]
        mode = spec.get("mode", "bb" if donor == "N" else "sc")
        acc_coord = _lig_coord(lig, acc_name)
        is_oxy = (acc_name == oxy_atom)

        # ---- locate residue BY INDEX (fallback: closest-of-type) ----
        chain = rid = None
        if key in dim:
            chain, rid = _parse_index(dim[key])
        if rid is None:
            resn_hint = spec.get("resn") or _DONOR2RESN.get(donor)
            chain, rid = _closest_of_type(prot_all, resn_hint, donor, acc_coord)
            if rid is not None:
                row["used_fallback"] = 1
        if chain is None:
            chain = pchain
        at = Rall.get((chain, rid), {}) if rid is not None else {}
        resn = "?"
        if rid is not None:
            s = prot_all[(prot_all.chain_id == chain) & (prot_all.res_id == rid)]
            if len(s):
                resn = str(s.res_name[0])

        e = dict(resn=resn, rid=rid, chain=chain, donor=donor, acc=acc_name)
        dc = at.get(donor)
        if dc is None or acc_coord is None or rid is None:
            e["d"] = 99.0
            cat_dist_ok = False
            cat[key] = e
            continue
        d = dist(dc, acc_coord)
        e["d"] = round(d, 2)
        thr = TH["asn_dist_max"] if not is_oxy else TH["cat_dist_max"]
        if d > thr:
            cat_dist_ok = False

        # ---- orientation gate ----
        orient = spec.get("orient")
        if orient is None:
            orient = True if mode == "bb" else is_oxy
        if mode == "bb":
            ca_i = at.get("CA")
            cprev = Rall.get((chain, rid - 1), {}).get("C")
            if ca_i is not None and cprev is not None:
                # idealized backbone amide H = external bisector of C(i-1)-N-CA:
                #   -(U(N->C(i-1)) + U(N->CA)).  arg order: (cprev - dc) is N->C(i-1).
                hdir = -U(U(cprev - dc) + U(ca_i - dc))
                dev = ang(acc_coord, dc, dc + hdir)
                e["amide_dev"] = round(dev, 0)
                if orient and dev > TH["bb_amide_ang_max"]:
                    cat_orient_ok = False
            else:
                e["amide_dev"] = 99
                if orient:
                    cat_orient_ok = False
        else:  # sidechain donor
            cb = at.get("CB")
            if cb is not None:
                dev = abs(ang(cb, dc, acc_coord) - 109.5)
                e["oh_dev"] = round(dev, 0)
                appr = ang(appr_coord, acc_coord, dc) if (is_oxy and appr_coord is not None) else None
                if appr is not None:
                    e["approach"] = round(appr, 0)
                if orient:
                    if dev > TH["sc_oh_dev_max"]:
                        cat_orient_ok = False
                    if appr is not None and not (TH["approach_lo"] <= appr <= TH["approach_hi"]):
                        cat_orient_ok = False

        # ---- rotamer REACHABILITY (lesson #3) ----
        reach_atom = spec.get("reach_atom", "CB")
        ca_i = at.get("CA")
        ratom = at.get(reach_atom)
        if ca_i is not None and ratom is not None:
            rr = dist(ca_i, ratom)
            e["reach_atom"] = reach_atom
            e["reach"] = round(rr, 2)
            if reach_atom == "CB":
                e["cacb"] = round(rr, 2)
                if not (TH["cacb_lo"] <= rr <= TH["cacb_hi"]):
                    cacb_ok = False           # INFO: repacked downstream
                if rr > TH["cacb_sever"]:
                    cat_severed = True        # GATED: backbone cannot support the sidechain
            else:
                lo = spec.get("reach_lo", TH["ca_cg_lo"])
                hi = spec.get("reach_hi", TH["ca_cg_hi"])
                e["reach_lo"] = lo
                e["reach_hi"] = hi
                if not (lo <= rr <= hi):
                    cat_reach_ok = False      # GATED: no valid rotamer connects to fixed atom
        elif reach_atom != "CB":
            # the fixed deeper atom is required for the reachability gate
            cat_reach_ok = False

        # ---- covalent integrity: strict (INFO) + severed (GATED) ----
        if resn in IDEAL:
            for x, y, ideal in IDEAL[resn]["bonds"]:
                if x in at and y in at:
                    dv = abs(dist(at[x], at[y]) - ideal)
                    if dv > TH["cov_strict"]:
                        cat_cov_ok = False
                    if dv > TH["cov_sever"]:
                        cat_severed = True
            for x, y, z, ideal in IDEAL[resn]["angles"]:
                if x in at and y in at and z in at and abs(ang(at[x], at[y], at[z]) - ideal) > TH["cov_ang_tol"]:
                    cat_cov_ok = False
        cat[key] = e

    # splay: angle (donor1 - oxyanion - donor2) for the two oxyanion-hole donors
    splay = None
    dk = [k for k in cat if cat[k].get("acc") == oxy_atom and cat[k].get("d", 99) < 90]
    oxy_coord = _lig_coord(lig, oxy_atom) if oxy_atom else None
    if len(dk) >= 2 and oxy_coord is not None:
        try:
            e1, e2 = cat[dk[0]], cat[dk[1]]
            p1 = Rall[(e1["chain"], e1["rid"])][e1["donor"]]
            p2 = Rall[(e2["chain"], e2["rid"])][e2["donor"]]
            splay = round(ang(p1, oxy_coord, p2), 0)
        except Exception:
            pass

    # CAT_OK gates on the fatal/at-this-stage-meaningful criteria: contact distance, donor
    # orientation, reachability, and NOT severed. The tight CA-CB window (cacb_ok) and strict
    # covalent offsets (cat_cov_ok) are repacked by LigandMPNN + relax -> reported as INFO only.
    cat_ok = cat_dist_ok and cat_orient_ok and cat_reach_ok and (not cat_severed)

    other_d = [cat[k]["d"] for k in cat if cat[k].get("acc") != oxy_atom and "d" in cat[k]]
    oxy_d = [cat[k]["d"] for k in cat if cat[k].get("acc") == oxy_atom and "d" in cat[k]]
    row.update(cat=json.dumps(cat), splay=splay,
               asn_d=(other_d[0] if other_d else None),
               d2=(oxy_d[0] if oxy_d else None),
               d3=(oxy_d[1] if len(oxy_d) > 1 else None),
               cat_dist_ok=int(cat_dist_ok), cat_orient_ok=int(cat_orient_ok),
               cat_reach_ok=int(cat_reach_ok), cat_severed=int(cat_severed),
               cat_cacb_ok=int(cacb_ok), cat_cov_ok=int(cat_cov_ok))

    # ---- TIER 3: stitching ------------------------------------------------- #
    nbreak = M.get("n_chainbreaks", 99)
    jpt = M.get("join_point_rmsd_by_token", {})
    max_jp = round(max(jpt.values()), 2) if jpt else None
    ca_dev = M.get("max_ca_deviation", 99.0)
    row.update(rfd3_nbreak=nbreak, max_join_rmsd=max_jp, max_ca_dev=round(ca_dev, 2),
               nonloop=round(M.get("non_loop_fraction", 0), 2),
               helix=round(M.get("helix_fraction", 0), 2),
               sheet=round(M.get("sheet_fraction", 0), 2),
               rog=round(M.get("radius_of_gyration", 0), 1),
               ala=round(M.get("alanine_content", 0), 2))
    # NOTE (validate_design.py): join_point_rmsd is NOT a primary gate -- it sits ~2-3 even for
    # good designs (calibration median ~2.5). STITCH = no chain breaks + fixed motif held (low
    # CA dev), with a very loose join_point backstop (join_rmsd_gate=6.0) for the extreme tail.
    stitch_ok = (nbreak == 0 and ca_dev <= TH["max_ca_dev"]
                 and (max_jp is None or max_jp <= TH["join_rmsd_gate"]))

    # ---- TIER 4: ligand ---------------------------------------------------- #
    fb_pair = catspec.get("forming_bond")
    fb = None
    if fb_pair and len(fb_pair) == 2:
        c0, c18 = _lig_coord(lig, fb_pair[0]), _lig_coord(lig, fb_pair[1])
        if c0 is not None and c18 is not None:
            fb = round(dist(c0, c18), 3)
    row["forming_bond"] = fb
    lig_ok = (fb is not None and abs(fb - TH["forming_ideal"]) <= TH["forming_tol"])

    row.update(GEOM_OK=int(geom_ok), CAT_OK=int(cat_ok), STITCH_OK=int(stitch_ok),
               LIG_OK=int(lig_ok),
               PASS=int(geom_ok and cat_ok and stitch_ok and lig_ok))
    return row


def validate_design_dir(run_dir, catspec, limit=0):
    """Run :func:`validate_design` on every ``*.cif.gz`` in ``run_dir``. Returns a list of row
    dicts. Designs that raise are skipped (a stub row with ``error`` is appended)."""
    run = str(run_dir).rstrip("/")
    files = sorted(glob.glob(f"{run}/*.cif.gz"))
    if limit:
        files = files[:limit]
    rows = []
    for f in files:
        try:
            rows.append(validate_design(f, catspec))
        except Exception as ex:
            rows.append(dict(design=os.path.basename(f).replace(".cif.gz", ""),
                             error=str(ex), PASS=0))
    return rows


# --------------------------------------------------------------------------- #
# PUBLIC: scan_emergent_oxyanion_hole  (port of scan_emergent_hole.py)
# --------------------------------------------------------------------------- #
def scan_emergent_oxyanion_hole(cif_path, acceptor_atom, dcut=3.5, angcut=40.0):
    """Detect an EMERGENT backbone oxyanion hole near ``acceptor_atom`` (e.g. "O15") in
    free-backbone designs where the hole is NOT a fixed motif. Scans every backbone amide N
    within ``dcut`` of the acceptor and tests whether the idealized amide N-H points at it
    (deviation < ``angcut``). Pure backbone geometry.

    Returns dict(design, n_donors, best_d, best_dev, splay, nbreak, nonloop, donors) where
    ``donors`` is a list of (res_id, distance, dev). Returns None if the acceptor is absent.
    """
    a = _read_structure(cif_path)
    prot = a[(~a.hetero) & (a.chain_id == "A")]
    lig = a[a.hetero]
    m = lig.atom_name == acceptor_atom
    acc = lig.coord[m][0] if m.any() else None
    if acc is None:
        return None
    R = {}
    for nm, co, rid in zip(prot.atom_name, prot.coord, prot.res_id):
        R.setdefault(int(rid), {})[str(nm)] = co
    donors = []
    for rid in sorted(R):
        at = R[rid]
        if "N" not in at or "CA" not in at:
            continue
        N = at["N"]
        d = dist(N, acc)
        if d > dcut:
            continue
        cprev = R.get(rid - 1, {}).get("C")
        if cprev is None:                       # need preceding carbonyl for the amide H
            continue
        hdir = -U(U(cprev - N) + U(at["CA"] - N))
        dev = ang(acc, N, N + hdir)
        if dev < angcut:
            donors.append((rid, round(d, 2), round(dev, 0)))
    jp = _design_json_path(cif_path)
    M = json.load(open(jp)).get("metrics", {}) if os.path.exists(jp) else {}
    splay = None
    if len(donors) >= 2:
        n1 = R[donors[0][0]]["N"]
        n2 = R[donors[1][0]]["N"]
        splay = round(ang(n1, acc, n2), 0)
    design = os.path.basename(str(cif_path))
    for suf in (".cif.gz", ".cif"):
        if design.endswith(suf):
            design = design[:-len(suf)]
            break
    return dict(design=design, n_donors=len(donors),
                best_dev=min((x[2] for x in donors), default=None),
                best_d=min((x[1] for x in donors), default=None), splay=splay,
                nbreak=M.get("n_chainbreaks", 99),
                nonloop=round(M.get("non_loop_fraction", 0), 2), donors=donors)


# --------------------------------------------------------------------------- #
# refold helpers (analyze_rf3_michaelase.py / analyze_chai_refold.py)
# --------------------------------------------------------------------------- #
def _parse_pdb(path):
    """Tolerant whitespace PDB parser (Chai outputs overflow fixed columns). Returns a list
    of dict(chain, resid, resname, atom, elem, xyz, het)."""
    out = []
    with open(path) as fh:
        for l in fh:
            if not l.startswith(("ATOM", "HETATM")):
                continue
            t = l.split()
            try:
                elem = t[-1]
                x, y, z = float(t[-6]), float(t[-5]), float(t[-4])
                record = t[0]
                atom = t[2]
                resname = t[3]
                chain = t[4]
                resid = int(t[5])
            except (ValueError, IndexError):
                continue
            out.append(dict(chain=chain, resid=resid, resname=resname, atom=atom,
                            elem=elem, xyz=np.array([x, y, z]), het=(record == "HETATM")))
    return out


def _atoms_from_structure(a):
    """Normalize a biotite AtomArray to the same record form as _parse_pdb."""
    atoms = []
    for nm, rn, co, rid, ch, het, el in zip(a.atom_name, a.res_name, a.coord, a.res_id,
                                            a.chain_id, a.hetero, a.element):
        atoms.append(dict(chain=str(ch), resid=int(rid), resname=str(rn), atom=str(nm),
                          elem=str(el), xyz=co, het=bool(het)))
    return atoms


def _ca_map(atoms, chain="A"):
    return {a["resid"]: a["xyz"] for a in atoms
            if (not a["het"]) and a["chain"] == chain and a["atom"] == "CA"}


def _ca_rmsd(d_ca, r_ca):
    """Kabsch CA-RMSD over the common residue ids (validate refold scripts). Returns
    (rmsd, n_common) or (None, n_common) if < 10 common residues."""
    common = sorted(set(d_ca) & set(r_ca))
    if len(common) < 10:
        return None, len(common)
    D = np.array([d_ca[i] for i in common])
    Rr = np.array([r_ca[i] for i in common])
    Dc, Rc = D - D.mean(0), Rr - Rr.mean(0)
    V, S, Wt = np.linalg.svd(Rc.T @ Dc)
    s = np.sign(np.linalg.det(V @ Wt))
    Rot = Rc @ (V @ np.diag([1, 1, s]) @ Wt)
    return round(float(np.sqrt(((Rot - Dc) ** 2).sum(1).mean())), 2), len(common)


def _ligand_integrity(atoms, design_nbonds=None):
    """NAME-INDEPENDENT ligand intactness (lesson #5: Chai/RF3 renumber atoms via canonical
    SMILES, so name-mapped bond checks are invalid). Uses the refold ligand's own covalent
    graph: a real molecule has every atom bonded (none with no neighbour in [lig_bond_lo,
    lig_bond_hi]) and ~the design's bond count. Returns (n_detached_atoms, n_bonds)."""
    L = np.array([a["xyz"] for a in atoms if a["het"]])
    if len(L) < 2:
        return None, None
    D = np.linalg.norm(L[:, None] - L[None], axis=2)
    np.fill_diagonal(D, 9.0)
    adj = (D >= TH["lig_bond_lo"]) & (D <= TH["lig_bond_hi"])
    n_detached = int((D.min(1) > TH["lig_bond_hi"]).sum())
    n_bonds = int(adj.sum() // 2)
    return n_detached, n_bonds


def _lig_bonds_count(atoms):
    """Count ligand covalent bonds (proximity) for a design reference."""
    _, nb = _ligand_integrity(atoms)
    return nb or 0


def _resn_of_donor(donor):
    return _DONOR2RESN.get(donor, "?")


_RF3_SCORE_KEYS = ("overall_plddt", "ptm", "iptm", "overall_pae", "has_clash")
# Chai confidence is parsed from the filename (analyze_chai_refold.py SCORE_RE):
_CHAI_SCORE_RE = re.compile(
    r"agg-(\d+\.\d+).*chain1_plddt-(\d+\.\d+).*iptm_AC-(\d+\.\d+).*--ptm-(\d+\.\d+)--pae-(\d+\.\d+)")


def _chai_scores(fn):
    m = _CHAI_SCORE_RE.search(fn)
    if not m:
        return {}
    return dict(agg=float(m.group(1)), binder_plddt=float(m.group(2)),
                iptm_AC=float(m.group(3)), ptm=float(m.group(4)), pae=float(m.group(5)))


def _resolve_design(refold_path, design_json_or_dir):
    """Return (design_cif_path, design_json_path, design_name) for a refold file.

    ``design_json_or_dir`` may be: a .json file, a .cif/.cif.gz file (sibling json used), or a
    directory (the design name is recovered from the refold filename: rf3 ids strip a
    trailing _b<#>_d<#>; chai filenames split on _b0_d / motif_)."""
    p = str(design_json_or_dir)
    base = os.path.basename(str(refold_path))
    # derive design name from refold filename
    rid = base
    for suf in ("_model.cif", "_model.pdb", ".cif", ".pdb"):
        if rid.endswith(suf):
            rid = rid[:-len(suf)]
            break
    dname = re.sub(r"_b\d+_d\d+$", "", rid)
    if "_b0_d" in base:
        dname = base.split("_b0_d")[0]

    if p.endswith(".json"):
        djson = p
        for cand in (p[:-5] + ".cif.gz", p[:-5] + ".cif"):
            if os.path.exists(cand):
                return cand, djson, dname
        return None, djson, dname
    if p.endswith(".cif.gz") or p.endswith(".cif"):
        return p, _design_json_path(p), dname
    # directory
    for cand in (os.path.join(p, dname + ".cif.gz"), os.path.join(p, dname + ".cif")):
        if os.path.exists(cand):
            return cand, _design_json_path(cand), dname
    return None, os.path.join(p, dname + ".json"), dname


def _parse_donors(donors):
    """Accept donors as a catspec-style dict {key:{donor,acc}} or a string
    'A1=ND2>N5,A2=OH>O15,A3=NE2>O15'. Returns the dict form."""
    if isinstance(donors, dict):
        return donors
    spec = {}
    for tok in str(donors).split(","):
        tok = tok.strip()
        if not tok:
            continue
        key, rest = tok.split("=")
        don, acc = rest.split(">")
        spec[key.strip()] = {"donor": don.strip(), "acc": acc.strip()}
    return spec


# --------------------------------------------------------------------------- #
# PUBLIC: analyze_refold  (unifies analyze_rf3_michaelase.py + analyze_chai_refold.py)
# --------------------------------------------------------------------------- #
def analyze_refold(refold_cif, design_json_or_dir, donors, mode="rf3"):
    """Self-consistency analysis of a refold against its design (lesson #4: the decisive gate).

    Computes, for one refold:
      * RESEMBLANCE: CA-RMSD of the refolded binder (chain A) vs the design backbone.
      * ACTIVE SITE held in the refold: catalytic donor -> acceptor distances, located BY INDEX
        via the design's diffused_index_map (preserved through MPNN + refold), with a
        closest-of-type fallback. Gated at <= ``TH['refold_cat_max']`` (3.5 A).
      * TS forming bond preserved (from the design's ``forming_bond`` if discoverable by name).
      * NAME-INDEPENDENT ligand integrity (bond-graph degree sequence / detached-atom count).
      * Confidence: rf3 -> overall_plddt/ptm/iptm/overall_pae/has_clash from the sibling
        ``*_summary_confidences.json``; chai -> binder_plddt/iptm_AC/ptm/pae parsed from filename.

    Args:
      refold_cif: rf3 ``<id>_model.cif`` (or .gz), or a Chai ``.pdb``.
      design_json_or_dir: design ``.json`` / ``.cif[.gz]`` / a directory containing them.
      donors: catspec ``catalytic`` dict, or a string 'A1=ND2>N5,A2=OH>O15,...'.
      mode: "rf3" (templated initial-guess; gate on pLDDT + active site + no clash; RMSD is
            ~0 by templating and NOT a discriminator) or "chai" (free co-fold; gate on
            resemblance + active site + intact ligand).

    Returns a dict row.
    """
    spec = _parse_donors(donors)
    dcif, djson, dname = _resolve_design(refold_cif, design_json_or_dir)
    dim = json.load(open(djson)).get("diffused_index_map", {}) if (djson and os.path.exists(djson)) else {}

    # ---- load refold ----
    rp = str(refold_cif)
    if rp.endswith(".pdb"):
        ref_atoms = _parse_pdb(rp)
    else:
        ref_atoms = _atoms_from_structure(_read_structure(rp))

    # ---- load design (for CA-RMSD + ligand bond reference + forming-bond names) ----
    d_atoms = _atoms_from_structure(_read_structure(dcif)) if (dcif and os.path.exists(dcif)) else []
    d_ca = _ca_map(d_atoms, "A")
    r_ca = _ca_map(ref_atoms, "A")
    rms, ncom = _ca_rmsd(d_ca, r_ca) if d_ca and r_ca else (None, 0)

    # ---- index-located catalytic donor -> acceptor ----
    def lig_atom(atoms, name):
        for a in atoms:
            if a["het"] and a["atom"] == name:
                return a["xyz"]
        return None

    def atom_at(atoms, chain, resid, name):
        for a in atoms:
            if (not a["het"]) and a["chain"] == chain and a["resid"] == resid and a["atom"] == name:
                return a["xyz"]
        return None

    def resn_at(atoms, chain, resid):
        for a in atoms:
            if (not a["het"]) and a["chain"] == chain and a["resid"] == resid:
                return a["resname"]
        return "?"

    def closest_of_type(atoms, resn, atom, tgt):
        if tgt is None or resn == "?":
            return None
        cs = [a["xyz"] for a in atoms if (not a["het"]) and a["resname"] == resn and a["atom"] == atom]
        if not cs:
            return None
        return round(float(np.min([dist(c, tgt) for c in cs])), 2)

    cat = {}
    cat_ok = True
    for key, sp in spec.items():
        acc = lig_atom(ref_atoms, sp["acc"])
        chain = rid = None
        if key in dim:
            chain, rid = _parse_index(dim[key])
        if chain is None:
            chain = "A"
        dc = atom_at(ref_atoms, chain, rid, sp["donor"]) if rid is not None else None
        resn = resn_at(ref_atoms, chain, rid) if rid is not None else "?"
        if dc is None:
            # fall back to closest-of-type (refold may renumber / mutate at the index)
            resn_hint = sp.get("resn") or _resn_of_donor(sp["donor"])
            dd = closest_of_type(ref_atoms, resn_hint, sp["donor"], acc)
        else:
            dd = round(dist(dc, acc), 2) if acc is not None else None
        cat[key] = dict(resn=resn, rid=rid, d=dd)
        if dd is None or dd > TH["refold_cat_max"]:
            cat_ok = False

    # ---- TS forming bond (by name -- design ligand names survive rf3 templating) ----
    fb_pair = None
    # try to discover the forming pair from the design ligand graph is overkill; rf3 keeps
    # design names so we look up the common michaelase pair when present.
    ts = None
    c0 = lig_atom(ref_atoms, "C0")
    c18 = lig_atom(ref_atoms, "C18")
    if c0 is not None and c18 is not None:
        ts = round(dist(c0, c18), 2)

    # ---- name-independent ligand integrity ----
    d_nbonds = _lig_bonds_count(d_atoms) if d_atoms else None
    lig_detached, lig_nbonds = _ligand_integrity(ref_atoms, d_nbonds)
    lig_intact = (lig_detached is not None and lig_detached == 0
                  and (d_nbonds is None or abs((lig_nbonds or 0) - d_nbonds) <= TH["lig_nbond_tol"]))

    row = dict(refold=os.path.basename(rp), design=dname, mode=mode,
               ca_rmsd=rms, ncommon=ncom, ts_bond=ts,
               cat=json.dumps({k: v["d"] for k, v in cat.items()}), cat_ok=int(cat_ok),
               lig_detached=lig_detached, lig_nbonds=lig_nbonds, design_nbonds=d_nbonds,
               lig_intact=int(lig_intact))
    for k, v in cat.items():
        row[f"cat_{k}"] = v["d"]

    if mode == "rf3":
        sj = re.sub(r"_model\.cif(\.gz)?$", "_summary_confidences.json", rp)
        S = json.load(open(sj)) if os.path.exists(sj) else {}
        plddt = round(S.get("overall_plddt", 0), 3)
        ptm = round(S.get("ptm", 0), 3)
        iptm = round(S.get("iptm", 0), 3)
        pae = round(S.get("overall_pae", 99), 2)
        clash = int(bool(S.get("has_clash", 0)))
        # with full-structure initial-guess templating the binder backbone is reproduced
        # (CA-RMSD ~0), so RMSD is NOT a discriminator -- gate on pLDDT + active site + no clash.
        good = (plddt >= TH["refold_plddt_min"] and cat_ok and not clash)
        row.update(plddt=plddt, ptm=ptm, iptm=iptm, pae=pae, clash=clash, good=int(good))
    else:  # chai
        sc = _chai_scores(os.path.basename(rp))
        plddt = sc.get("binder_plddt")
        row.update(binder_plddt=plddt, iptm_AC=sc.get("iptm_AC"), ptm=sc.get("ptm"),
                   pae=sc.get("pae"), agg=sc.get("agg"))
        resembles = (rms is not None and rms <= TH["chai_rmsd_max"]
                     and plddt is not None and plddt >= TH["chai_plddt_min"])
        good = bool(resembles and cat_ok and lig_intact)
        row.update(resembles=int(resembles), good=int(good))
    return row


# --------------------------------------------------------------------------- #
# PUBLIC: viewer fixers (lesson #6 -- file-format bugs, not chemistry)
# --------------------------------------------------------------------------- #
LIG_BOND_CUT = 1.85   # rf3_to_pdb.py: true covalent distances only (1.2-1.6, up to ~1.8
                      # stretched). A larger cut wrongly bonds 1,3 contacts (~2.1-2.5 A).


def _pdb_line(idx, name, resn, chain, resid, xyz, elem, het):
    rec = "HETATM" if het else "ATOM  "
    anf = name if len(name) >= 4 else " " + name.ljust(3)
    return ("%s%5d %-4s %3s %1s%4d    %8.3f%8.3f%8.3f  1.00  0.00          %2s\n"
            % (rec, idx, anf[:4], resn[:3], (chain or "X")[:1], resid,
               xyz[0], xyz[1], xyz[2], elem[:2]))


def fix_lcolon_ligand(in_path, out_path):
    """Port of rf3_to_pdb.convert. Convert a .cif whose TS ligand is the colon-aliased ``L:G``
    into a clean, viewable PDB: rename the ligand to ``LIG`` (the colon makes PyMOL try to
    fetch a CCD entry from RCSB and fail -> no bonds) and add CONECT records (true covalent
    bonds <= 1.85 A + the explicit C0-C18 forming bond when present). Returns the number of
    ligand atoms written.
    """
    a = pdbx.get_structure(pdbx.CIFFile.read(str(in_path)), model=1)
    out = []
    lig = []
    for i, (nm, rn, co, rid, ch, het, el) in enumerate(zip(
            a.atom_name, a.res_name, a.coord, a.res_id, a.chain_id, a.hetero, a.element), 1):
        resn = "LIG" if het else str(rn)
        out.append(_pdb_line(i, str(nm), resn, str(ch), int(rid), co, str(el), bool(het)))
        if het:
            lig.append((i, co, str(nm)))
    if lig:
        idx = [s for s, _, _ in lig]
        C = np.array([c for _, c, _ in lig])
        sn = {n: s for s, _, n in lig}
        D = np.linalg.norm(C[:, None] - C[None], axis=2)
        for ii in range(len(idx)):
            for jj in range(len(idx)):
                if jj != ii and 1.0 <= D[ii, jj] <= LIG_BOND_CUT:
                    out.append("CONECT%5d%5d\n" % (idx[ii], idx[jj]))
        if "C0" in sn and "C18" in sn:                       # forming Michael bond (partial)
            out.append("CONECT%5d%5d\n" % (sn["C0"], sn["C18"]))
    out.append("END\n")
    with open(out_path, "w") as fh:
        fh.writelines(out)
    return len(lig)


def _clean_atomname(nm):
    return nm.split("_")[0][:4]      # strip Chai _N suffix, clamp to 4 chars


def fix_chai_overflow(in_path, out_path):
    """Port of fix_chai_pdb.fix_file. Re-emit a column-compliant PDB from a Chai refold PDB
    whose ligand uses resname ``LIG3`` (4 chars) + 5-char atom names (``C34_1``) that overflow
    PDB columns and scatter the ligand into a 'cloud' in fixed-column readers. The molecule is
    intact -- only the formatting is broken (lesson #6). Rewrites ligand resname -> ``LIG``,
    strips the ``_N`` suffix, and adds CONECT records (1.0-1.95 A). Returns
    (n_atoms, n_ligand_atoms).
    """
    rows = _parse_pdb(str(in_path))
    out = []
    lig_serials = []
    for i, a in enumerate(rows, 1):
        a = dict(a)
        a["atom"] = _clean_atomname(a["atom"])
        if a["het"]:
            a["resname"] = "LIG"
            a["chain"] = (a["chain"][:1] or "X")
            lig_serials.append((i, a["xyz"]))
        record = "HETATM" if a["het"] else "ATOM  "
        out.append(_chai_fmt(a, i, record))
    if lig_serials:
        idx = [s for s, _ in lig_serials]
        C = np.array([c for _, c in lig_serials])
        D = np.linalg.norm(C[:, None] - C[None], axis=2)
        for ii in range(len(idx)):
            for jj in range(len(idx)):
                if jj != ii and 1.0 <= D[ii, jj] <= 1.95:
                    out.append("CONECT%5d%5d\n" % (idx[ii], idx[jj]))
    out.append("END\n")
    with open(out_path, "w") as fh:
        fh.writelines(out)
    return len(rows), len(lig_serials)


def _chai_fmt(a, serial, record):
    nm = a["atom"]
    anf = nm if len(nm) >= 4 else " " + nm.ljust(3)
    return ("%-6s%5d %-4s %3s %1s%4d    %8.3f%8.3f%8.3f  1.00  0.00          %2s\n"
            % (record.strip().ljust(6), serial, anf[:4], a["resname"][:3], a["chain"][:1],
               a["resid"], a["xyz"][0], a["xyz"][1], a["xyz"][2], a["elem"][:2]))
