"""enzyme_build.py — deterministic de-novo enzyme BUILD/PREP module.

This is the "current best practice" de-novo enzyme-design BUILD/PREP logic from the
foundry repo (``current_best_practice/build/{build_inputs,build_mpnn_config,check_built,
build_nest}.py``), ported into little-protein-tiger and **merged** with the pyrosetta-free
theozyme geometry helpers that already live in
``skills/enzyme-active-site-modeling/scripts/{theozyme_tools,mode_tools}.py``.

What this module does, end to end, for a converged QM transition state (TS):
  1. ``parse_ts``          — read the TS ``.xyz`` and pull out named substrate /
                             catalytic-stand-in coordinates (caller supplies the
                             atom -> role mapping; nothing is hard-coded).
  2. ``write_orca_inputs`` — emit ORCA ``.inp`` for the relax/OptTS/Freq/scan/IRC
                             stages from the templates in ``orca_templates.md``.
  3. ``emit_rfd3_spec``    — write the motif PDB (+ colon-escaped ``L:G`` copy), the
                             RFD3 spec JSON, and ``motif_meta.json``.
  4. ``build_mpnn_config`` — build a LigandMPNN config that fixes each design's
                             catalytic residues (read per-design from the
                             ``diffused_index_map``).
  5. ``check_built``       — pre-diffusion geometry self-check of a built motif.
  6. ``read_pltvib`` / ``classify_ts`` / ``top_movers`` / ``bond_component`` are
     re-exported from ``mode_tools`` so the theozyme stage can diagnose an ORCA
     frequency without a second import.

Determinism / dependencies
---------------------------
Depends only on ``numpy`` + ``rdkit``. **No pyrosetta.** The foundry scripts used
pyrosetta in exactly two places, both replaced here:

  * *ideal residue templates* (``pose_from_sequence(...).residue(1)``) ->
    :func:`ideal_residue`, which builds an MMFF-optimised single residue with RDKit
    (via ``theozyme_tools.embed_fragment``) and assigns standard PDB atom names from a
    per-residue SMILES whose atom order is fixed. Deterministic (fixed RNG seed).
  * *poly-glycine backbone dihedral placement* for the oxyanion "nest"
    (``pose_from_sequence("G"*n); set_phi/set_psi``) -> :func:`build_polygly_backbone`,
    a minimal NeRF (Natural Extension Reference Frame) builder that places ideal
    backbone atoms (N, CA, C, O) + the amide H from standard bond lengths/angles and
    the requested phi/psi. The amide-H placement matches the idealised H used by
    :func:`check_built`, so the two stages agree by construction.

Import choice
-------------
``skills/enzyme-active-site-modeling/scripts/`` is **not** an importable package, so we
compute the repo root from ``__file__`` and insert that scripts dir on ``sys.path``,
then import the theozyme/mode helpers by name. This keeps a single source of truth for
``kabsch`` / ``embed_fragment`` / the ORCA-mode readers rather than vendoring copies.

Generalisation vs. the foundry originals
----------------------------------------
The foundry ``build_inputs.py`` hard-coded the michaelase substrate (``LIG_NAMES``,
71-atom assertion, ``XYZ=``/``OUT=`` paths, the ``L:G`` literal). Here:
  * the TS atom-name -> role mapping is a **caller-supplied** ``atom_map`` argument;
  * all input/output paths are **function parameters**;
  * the ligand residue name (and therefore its colon alias) is a parameter;
  * the RFD3 ``unindex`` contig and ``select_unfixed_sequence`` are **derived
    deterministically** from geometry + the fixed-atom spec instead of being hand-
    written per strategy (see :func:`emit_rfd3_spec`).
"""
from __future__ import annotations

import glob
import itertools
import json
import math
import os
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

# --------------------------------------------------------------------------------------
# Import the theozyme / ORCA-mode helpers from the (non-package) skill scripts dir.
# repo root = parents[1] of this file (src/enzyme_build.py -> <repo>/src -> <repo>).
# --------------------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILL_SCRIPTS = _REPO_ROOT / "skills" / "enzyme-active-site-modeling" / "scripts"
if str(_SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_SCRIPTS))

try:
    # geometry helpers (rdkit + numpy) — single source of truth for kabsch / fragment build
    from theozyme_tools import (  # type: ignore
        kabsch,
        embed_fragment,
        coords as _rd_coords,
        align_vec,
        rot_about,
        read_xyz,
        write_xyz,
    )
    # ORCA normal-mode helpers (numpy only) — re-exported below
    from mode_tools import (  # type: ignore
        read_pltvib,
        classify_ts,
        top_movers,
        bond_component,
    )
except Exception as exc:  # pragma: no cover - surfaced loudly with a useful hint
    raise ImportError(
        f"enzyme_build could not import the theozyme/mode helpers from {_SKILL_SCRIPTS!r}. "
        f"Ensure rdkit+numpy are installed in the active interpreter. Original error: {exc}"
    ) from exc

__all__ = [
    # public API
    "parse_ts",
    "write_orca_inputs",
    "emit_rfd3_spec",
    "build_mpnn_config",
    "check_built",
    # pyrosetta replacements (the BUILD primitives)
    "ideal_residue",
    "build_polygly_backbone",
    # ported strategy builders (pyrosetta-free)
    "build_asn",
    "place_sc_donor",
    "build_bb_dipeptide",
    "find_nest",
    "place_nest",
    "build_michaelase_inputs",
    # geometry / io re-exports
    "kabsch",
    "colon_escape",
    "write_motif",
    "read_pdb",
    # mode_tools re-exports
    "read_pltvib",
    "classify_ts",
    "top_movers",
    "bond_component",
]

# default ORCA functional/basis line (without the per-stage task keyword); see
# skills/enzyme-active-site-modeling/reference/orca_templates.md
DEFAULT_METHOD = "wB97X-D3 def2-TZVP def2/J RIJCOSX"
HOST_CKPT = os.path.expanduser("~/pip_rcfoundry_ckpt/ligandmpnn_v_32_010_25.pt")


# ======================================================================================
# small linear algebra (kept local to match build_inputs/check_built semantics exactly)
# ======================================================================================
def U(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


def dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))


def ang(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """Angle a-b-c in degrees."""
    return float(np.degrees(np.arccos(np.clip(np.dot(U(a - b), U(c - b)), -1, 1))))


def rotate(v: np.ndarray, axis: np.ndarray, deg: float) -> np.ndarray:
    """Rotate vector ``v`` about ``axis`` by ``deg`` degrees (Rodrigues)."""
    th = math.radians(deg)
    a = U(axis)
    return v * math.cos(th) + np.cross(a, v) * math.sin(th) + a * np.dot(a, v) * (1 - math.cos(th))


def _dihedral(a, b, c, d) -> float:
    """Signed dihedral a-b-c-d in degrees."""
    b1, b2, b3 = b - a, c - b, d - c
    n1 = np.cross(b1, b2)
    n2 = np.cross(b2, b3)
    m1 = np.cross(n1, U(b2))
    x = np.dot(n1, n2)
    y = np.dot(m1, n2)
    return float(np.degrees(np.arctan2(y, x)))


# ======================================================================================
# PDB io  (fixed-column writer/reader; matches the foundry layout)
# ======================================================================================
def _elem_of(nm: str) -> str:
    return "O" if nm[0] == "O" else ("N" if nm[0] == "N" else ("S" if nm[0] == "S" else "C"))


def _fmt_atom(idx: int, name: str, resn: str, chain: str, resid: int, xyz, elem: str, het: bool) -> str:
    rec = "HETATM" if het else "ATOM  "
    anf = name if len(name) >= 4 else " " + name.ljust(3)
    return (f"{rec}{idx:5d} {anf} {resn:>3} {chain}{resid:4d}    "
            f"{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {elem:>2}\n")


def colon_escape(resname: str) -> str:
    """Return a <=3-char colon-bearing alias for a ligand residue name.

    ``'LIG' -> 'L:G'``. The colon guarantees the name is never a CCD code, forcing
    RFD3 to use the input coordinates as the ligand reference conformer (vendored from
    foundry ``prep_loop_binders.colon_escape``).
    """
    r = resname.strip()
    if len(r) >= 3:
        return f"{r[0]}:{r[2]}"
    if len(r) == 2:
        return f"{r[0]}:{r[1]}"
    return f"{r}:{r}"[:3]


def write_motif(path, residues, substrate, ligand_resname: str = "LIG"):
    """Write a motif PDB (protein residues as ATOM chain A, ligand as HETATM chain X)
    plus the colon-escaped ``*_lcolon.pdb`` copy.

    Parameters
    ----------
    residues : list of ``(resname, resid, {atom_name: xyz}, [ordered_atom_names])``
    substrate : ``{atom_name: xyz}`` — written in dict order as HETATM, chain X, resid 1.
    ligand_resname : 3-char ligand name in the plain PDB (its colon alias goes in the
        ``*_lcolon.pdb``).

    Returns ``(motif_path, lcolon_path)`` as :class:`pathlib.Path`.
    """
    path = Path(path)
    out = []
    idx = 1
    for resn, resid, atoms, order in residues:
        for nm in order:
            if nm in atoms:
                out.append(_fmt_atom(idx, nm, resn, "A", resid, atoms[nm], _elem_of(nm), het=False))
                idx += 1
    for nm, xyz in substrate.items():
        out.append(_fmt_atom(idx, nm, ligand_resname, "X", 1, xyz, _elem_of(nm), het=True))
        idx += 1
    out.append("END\n")
    path.write_text("".join(out))

    colon_name = colon_escape(ligand_resname)
    lcolon = path.with_name(path.stem + "_lcolon" + path.suffix)
    esc = []
    for ln in out:
        if ln.startswith("HETATM") and ln[17:20].strip() == ligand_resname:
            ln = ln[:17] + f"{colon_name:>3}" + ln[20:]
        esc.append(ln)
    lcolon.write_text("".join(esc))
    return path, lcolon


def read_pdb(path):
    """Parse a motif PDB into ``(protein, ligand)``.

    ``protein`` -> ``{(chain, resid, resname): {atom_name: xyz}}`` (ATOM records);
    ``ligand``  -> ``{atom_name: xyz}`` (HETATM records). Robust to either record order
    and to the foundry ``L:G`` ligand resname.
    """
    prot: dict = {}
    lig: dict = {}
    for ln in Path(path).read_text().splitlines():
        if ln.startswith(("ATOM", "HETATM")):
            nm = ln[12:16].strip()
            resn = ln[17:20].strip()
            try:
                rid = int(ln[22:26])
            except ValueError:
                continue
            ch = ln[21]
            xyz = np.array([float(ln[30:38]), float(ln[38:46]), float(ln[46:54])])
            if ln.startswith("HETATM"):
                lig[nm] = xyz
            else:
                prot.setdefault((ch, rid, resn), {})[nm] = xyz
    return prot, lig


# ======================================================================================
# pyrosetta replacement #1 — ideal single-residue templates from RDKit
# ======================================================================================
# Per-residue SMILES with a FIXED heavy-atom order so heavy atom i -> PDB name names[i].
# RDKit preserves SMILES atom order on parse; AddHs appends H after heavy atoms.
# (free neutral amino acids; "OXT" is the second carboxyl O, unused by the builders.)
_RES_TEMPLATES: dict[str, tuple[str, list[str]]] = {
    "GLY": ("NCC(=O)O", ["N", "CA", "C", "O", "OXT"]),
    "ALA": ("N[CH](C(=O)O)C", ["N", "CA", "C", "O", "OXT", "CB"]),
    "SER": ("N[CH](C(=O)O)CO", ["N", "CA", "C", "O", "OXT", "CB", "OG"]),
    "THR": ("N[CH](C(=O)O)[CH](O)C", ["N", "CA", "C", "O", "OXT", "CB", "OG1", "CG2"]),
    "CYS": ("N[CH](C(=O)O)CS", ["N", "CA", "C", "O", "OXT", "CB", "SG"]),
    "ASN": ("N[CH](C(=O)O)CC(=O)N",
            ["N", "CA", "C", "O", "OXT", "CB", "CG", "OD1", "ND2"]),
    "ASP": ("N[CH](C(=O)O)CC(=O)O",
            ["N", "CA", "C", "O", "OXT", "CB", "CG", "OD1", "OD2"]),
    "GLN": ("N[CH](C(=O)O)CCC(=O)N",
            ["N", "CA", "C", "O", "OXT", "CB", "CG", "CD", "OE1", "NE2"]),
    "TYR": ("N[CH](C(=O)O)Cc1ccc(O)cc1",
            ["N", "CA", "C", "O", "OXT", "CB", "CG", "CD1", "CE1", "CZ", "OH", "CE2", "CD2"]),
    "HIS": ("N[CH](C(=O)O)Cc1c[nH]cn1",
            ["N", "CA", "C", "O", "OXT", "CB", "CG", "CD2", "NE2", "CE1", "ND1"]),
}

# one-letter -> three-letter for the convenience builders
_ONE_TO_THREE = {"G": "GLY", "A": "ALA", "S": "SER", "T": "THR", "C": "CYS",
                 "N": "ASN", "D": "ASP", "Q": "GLN", "Y": "TYR", "H": "HIS"}


def ideal_residue(resname: str, seed: int = 0xC0FFEE) -> dict[str, np.ndarray]:
    """Ideal single amino-acid residue as ``{PDB_atom_name: xyz}`` (heavy atoms only).

    RDKit replacement for pyrosetta's ``pose_from_sequence(seq).residue(1)``. The residue
    is embedded + MMFF-optimised deterministically (fixed ``seed``) and heavy atoms are
    named from the fixed SMILES order in ``_RES_TEMPLATES``. Hydrogens are intentionally
    omitted — the build helpers only need heavy-atom positions, and the backbone amide H
    is supplied separately by :func:`build_polygly_backbone`.

    Accepts a 3-letter (``"ASN"``) or 1-letter (``"N"``) code.
    """
    rn = resname.strip().upper()
    if len(rn) == 1:
        rn = _ONE_TO_THREE.get(rn, rn)
    if rn not in _RES_TEMPLATES:
        raise KeyError(f"no ideal-residue template for {resname!r}; "
                       f"known: {sorted(_RES_TEMPLATES)}")
    smiles, names = _RES_TEMPLATES[rn]
    mol = embed_fragment(smiles, seed=seed)
    xyz = _rd_coords(mol)
    n_heavy = sum(1 for a in mol.GetAtoms() if a.GetSymbol() != "H")
    if n_heavy != len(names):
        raise RuntimeError(f"{rn}: SMILES has {n_heavy} heavy atoms but {len(names)} names")
    return {nm: xyz[i] for i, nm in enumerate(names)}


# ======================================================================================
# pyrosetta replacement #2 — minimal NeRF poly-glycine backbone builder
# ======================================================================================
# idealised peptide backbone geometry
_BL = {"N_CA": 1.458, "CA_C": 1.525, "C_N": 1.329, "C_O": 1.231, "N_H": 1.010}
_BA = {"N_CA_C": 111.0, "CA_C_N": 116.2, "C_N_CA": 121.7, "CA_C_O": 120.8}
_OMEGA = 180.0


def _nerf(a: np.ndarray, b: np.ndarray, c: np.ndarray,
          bond: float, angle_deg: float, torsion_deg: float) -> np.ndarray:
    """Place atom d given a,b,c with |c-d|=bond, angle(b,c,d)=angle, dihedral(a,b,c,d)=torsion."""
    th = math.radians(angle_deg)
    # negate so the *measured* dihedral a-b-c-d equals torsion_deg (IUPAC convention)
    ph = -math.radians(torsion_deg)
    bc = U(c - b)
    n = np.cross(b - a, bc)
    nn = np.linalg.norm(n)
    n = n / nn if nn > 1e-9 else np.array([0.0, 0.0, 1.0])
    m = np.cross(n, bc)
    d2 = np.array([-bond * math.cos(th),
                   bond * math.sin(th) * math.cos(ph),
                   bond * math.sin(th) * math.sin(ph)])
    return c + d2[0] * bc + d2[1] * m + d2[2] * n


def build_polygly_backbone(phis: Sequence[float], psis: Sequence[float]) -> list[dict[str, np.ndarray]]:
    """Build an ideal poly-glycine backbone for ``n = len(phis)`` residues.

    NeRF replacement for pyrosetta ``pose_from_sequence("G"*n)`` + ``set_phi/set_psi``.
    ``phis[i]`` / ``psis[i]`` are the phi/psi (degrees) of residue ``i`` (0-based);
    ``phis[0]`` and ``psis[-1]`` are ignored (undefined at chain ends), as in pyrosetta.
    Omega is fixed at 180 deg (trans). Returns one dict per residue with keys
    ``N, CA, C, O`` and (for residues > 0) the amide ``H``.

    The amide H is placed as ``N + 1.01 * -U(U(Cprev-N) + U(CA-N))`` — identical to the
    idealised H used in :func:`check_built`, so backbone-donor geometry is self-consistent.
    """
    n = len(phis)
    assert len(psis) == n and n >= 1, "phis and psis must be equal-length, >=1"
    res: list[dict[str, np.ndarray]] = [dict() for _ in range(n)]

    # seed the first three main-chain atoms (N1, CA1, C1)
    res[0]["N"] = np.array([0.0, 0.0, 0.0])
    res[0]["CA"] = np.array([_BL["N_CA"], 0.0, 0.0])
    # C1 in the xy-plane using N-CA-C angle (dihedral arbitrary -> 0 ref atom along -y)
    ref = np.array([0.0, -1.0, 0.0])
    res[0]["C"] = _nerf(ref, res[0]["N"], res[0]["CA"], _BL["CA_C"], _BA["N_CA_C"], 0.0)

    for i in range(1, n):
        Np = res[i - 1]["N"]; CAp = res[i - 1]["CA"]; Cp = res[i - 1]["C"]
        # N(i): dihedral N(i-1)-CA(i-1)-C(i-1)-N(i) = psi(i-1)
        Ni = _nerf(Np, CAp, Cp, _BL["C_N"], _BA["CA_C_N"], psis[i - 1])
        # CA(i): dihedral CA(i-1)-C(i-1)-N(i)-CA(i) = omega
        CAi = _nerf(CAp, Cp, Ni, _BL["N_CA"], _BA["C_N_CA"], _OMEGA)
        # C(i): dihedral C(i-1)-N(i)-CA(i)-C(i) = phi(i)
        Ci = _nerf(Cp, Ni, CAi, _BL["CA_C"], _BA["N_CA_C"], phis[i])
        res[i]["N"] = Ni; res[i]["CA"] = CAi; res[i]["C"] = Ci

    # carbonyl O(i): dihedral N(i)-CA(i)-C(i)-O = psi(i) + 180 (last residue: 180)
    for i in range(n):
        psi = psis[i] if i < n - 1 else 0.0
        res[i]["O"] = _nerf(res[i]["N"], res[i]["CA"], res[i]["C"],
                            _BL["C_O"], _BA["CA_C_O"], psi + 180.0)
    # amide H(i>=1) from the peptide plane (idealised, matches check_built)
    for i in range(1, n):
        N = res[i]["N"]; CA = res[i]["CA"]; Cprev = res[i - 1]["C"]
        hdir = -U(U(Cprev - N) + U(CA - N))
        res[i]["H"] = N + _BL["N_H"] * hdir
    return res


# ======================================================================================
# 1) parse_ts  (generalised build_inputs.parse_ts)
# ======================================================================================
def _parse_nma(els: Sequence[str], xyz: np.ndarray) -> dict[str, np.ndarray]:
    """Identify the atoms of an N-methylacetamide (NMA) catalytic stand-in by
    connectivity: carbonyl C (closest C to the O), carbonyl O, amide N, amide H,
    N-methyl C and carbonyl-methyl C. Returns ``{Cc, Oc, Nn, Hn, Cmeth_n, Cmeth_c}``."""
    els = list(els)
    Ci = [i for i, e in enumerate(els) if e == "C"]
    Oi = [i for i, e in enumerate(els) if e == "O"][0]
    Ni = [i for i, e in enumerate(els) if e == "N"][0]
    Hi = [i for i, e in enumerate(els) if e == "H"]
    cc = min(Ci, key=lambda i: dist(xyz[i], xyz[Oi]))
    cmeth_n = min([i for i in Ci if i != cc], key=lambda i: dist(xyz[i], xyz[Ni]))
    cmeth_c = [i for i in Ci if i not in (cc, cmeth_n)][0]
    hn = min(Hi, key=lambda i: dist(xyz[i], xyz[Ni]))
    return dict(Cc=xyz[cc], Oc=xyz[Oi], Nn=xyz[Ni], Hn=xyz[hn],
                Cmeth_n=xyz[cmeth_n], Cmeth_c=xyz[cmeth_c])


def parse_ts(xyz_path, atom_map: Mapping) -> dict:
    """Read a converged TS ``.xyz`` and return named substrate / catalytic coordinates.

    Generalises ``build_inputs.parse_ts`` — nothing is hard-coded; the caller supplies
    ``atom_map``.

    ``atom_map`` schema (all keys optional except ``substrate``)::

        {
          # substrate heavy atoms, EITHER a list of names (indices 0..k-1, build_inputs
          # style) OR an explicit {name: 0-based-index} mapping:
          "substrate": ["C0","C1","O2", ...],
          # catalytic stand-ins by role; each role is parsed one of two ways:
          "catalytic": {
             "amine": {"nma_slice": [35, 47]},      # parse this 0-based [start,stop)
             "ox1":   {"nma_slice": [47, 59]},      # block as an NMA by connectivity
             "metal": {"atoms": {"M": 70}},         # OR name explicit atom indices
          },
          "n_atoms": 71,                              # optional sanity assertion
          "elements": ["C","N","O","H","S"],         # optional element whitelist
        }

    Returns::

        {"symbols": [...], "xyz": ndarray(N,3),
         "substrate": {name: xyz}, "catalytic": {role: {atom: xyz}}}
    """
    syms, xyz = read_xyz(str(xyz_path))
    syms = list(syms)
    xyz = np.asarray(xyz, dtype=float)

    whitelist = atom_map.get("elements")
    if whitelist:
        keep = [i for i, s in enumerate(syms) if s in set(whitelist)]
        syms = [syms[i] for i in keep]
        xyz = xyz[keep]

    if "n_atoms" in atom_map:
        assert len(syms) == atom_map["n_atoms"], \
            f"expected {atom_map['n_atoms']} atoms, parsed {len(syms)}"

    sub_spec = atom_map["substrate"]
    if isinstance(sub_spec, Mapping):
        substrate = {name: xyz[idx] for name, idx in sub_spec.items()}
    else:  # list/sequence of names -> indices 0..k-1
        substrate = {name: xyz[i] for i, name in enumerate(sub_spec)}

    catalytic: dict[str, dict[str, np.ndarray]] = {}
    for role, spec in (atom_map.get("catalytic") or {}).items():
        if "nma_slice" in spec:
            lo, hi = spec["nma_slice"]
            catalytic[role] = _parse_nma(syms[lo:hi], xyz[lo:hi])
        elif "atoms" in spec:
            catalytic[role] = {nm: xyz[idx] for nm, idx in spec["atoms"].items()}
        else:
            raise ValueError(f"catalytic role {role!r} needs 'nma_slice' or 'atoms'")

    return {"symbols": syms, "xyz": xyz, "substrate": substrate, "catalytic": catalytic}


# ======================================================================================
# 2) write_orca_inputs  (from orca_templates.md)
# ======================================================================================
_STAGE_TASK = {
    "relax": "Opt TightSCF DefGrid3 SlowConv",
    "optts": "OptTS Freq TightSCF TightOpt DefGrid3 SlowConv",
    "freq": "Freq TightSCF DefGrid3",
    "scan": "Opt TightSCF DefGrid3 SlowConv",
    "irc": "IRC TightSCF DefGrid3",
}


def write_orca_inputs(out_dir, ts_xyz_path, stage: str, constraints: Mapping,
                      method_line: str | None = None, charge: int = 0, mult: int = 1,
                      solvent: str = "water") -> list[Path]:
    """Emit an ORCA ``.inp`` for ``stage`` from the templates in ``orca_templates.md``.

    Parameters
    ----------
    stage : one of ``{"relax", "optts", "freq", "scan", "irc"}``.
    constraints : dict with (all optional)::

        {
          "anchors": [35, 47, 59],     # 0-based atom indices -> frozen "{C i C}"
          "bonds":   [[0, 18]],        # forming bonds -> frozen "{B i j C}" (relax only)
          "scan":    {"bond": [0,18], "start": 2.20, "stop": 3.60, "n": 10},  # scan stage
          "hess_file": "ts_run.hess",  # irc stage (Hess_Filename)
        }

    method_line : the functional/basis/aux portion (default :data:`DEFAULT_METHOD`); the
        per-stage task keyword is appended. ``mult>1`` automatically prepends ``UKS``.

    Returns the list of written paths (one ``<stage>.inp``). The coordinate line refers
    to ``ts_xyz_path`` by basename, so place/copy the ``.xyz`` next to the ``.inp``.
    """
    stage = stage.lower()
    if stage not in _STAGE_TASK:
        raise ValueError(f"unknown stage {stage!r}; choose from {sorted(_STAGE_TASK)}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base = method_line or DEFAULT_METHOD
    if mult != 1 and "UKS" not in base.upper():
        base = "UKS " + base
    keyword_line = f"! {base} {_STAGE_TASK[stage]}"

    anchors = list(constraints.get("anchors", []))
    bonds = [tuple(b) for b in constraints.get("bonds", [])]

    def _anchor_block(indent="      "):
        return "".join(f"{indent}{{C {i} C}}\n" for i in anchors)

    lines = [keyword_line, "%maxcore 4000", "%pal nprocs 8 end"]

    if stage == "relax":
        geom = ["%geom", "   Constraints"]
        for i, j in bonds:
            geom.append(f"      {{B {i} {j} C}}")
        geom.append(_anchor_block().rstrip("\n"))
        geom += ["   end", "   MaxIter 200", "end"]
        lines += [ln for ln in geom if ln]
    elif stage == "optts":
        cons = " ".join(f"{{C {i} C}}" for i in anchors)
        lines += ["%geom",
                  f"   Constraints {cons} end" if cons else "   Constraints end",
                  "   Calc_Hess true",
                  "   Recalc_Hess 5",
                  "   MaxIter 300",
                  "end"]
    elif stage == "freq":
        cons = " ".join(f"{{C {i} C}}" for i in anchors)
        lines += [f"%geom Constraints {cons} end end" if cons else "%geom end"]
    elif stage == "scan":
        sc = constraints.get("scan")
        if not sc:
            raise ValueError("stage 'scan' requires constraints['scan']")
        i, j = sc["bond"]
        lines += ["%geom",
                  f"   Scan B {i} {j} = {sc['start']:.2f}, {sc['stop']:.2f}, {int(sc['n'])} end"]
        cons = " ".join(f"{{C {k} C}}" for k in anchors)
        if cons:
            lines.append(f"   Constraints {cons} end")
        lines += ["   MaxIter 200", "end"]
    elif stage == "irc":
        hess = constraints.get("hess_file", "ts_run.hess")
        lines += ["%irc",
                  "   InitHess read",
                  f'   Hess_Filename "{hess}"',
                  "   MaxIter 100",
                  "end"]

    lines.append(f'%cpcm smd true; SMDsolvent "{solvent}" end')
    lines.append(f"* xyzfile {charge} {mult} {Path(ts_xyz_path).name}")

    inp = out_dir / f"{stage}.inp"
    inp.write_text("\n".join(lines) + "\n")
    return [inp]


# ======================================================================================
# 3) emit_rfd3_spec  (generalised build_inputs main() writer)
# ======================================================================================
def _parse_sel_key(key: str) -> tuple[str, list[int]]:
    """Parse an RFD3 selection key like ``"A1"`` or ``"A1-4"`` -> (chain, [resids])."""
    chain = key[0]
    body = key[1:]
    if "-" in body:
        lo, hi = body.split("-", 1)
        return chain, list(range(int(lo), int(hi) + 1))
    return chain, [int(body)]


def _is_backbone_only(spec: str) -> bool:
    """True if a fixed-atom spec selects only backbone atoms (=> identity is designable)."""
    toks = [t.strip().upper() for t in spec.split(",") if t.strip()]
    return bool(toks) and all(t in {"N", "CA", "C", "O", "BKBN", "BB"} for t in toks)


def _ranges(nums: Sequence[int]) -> list[tuple[int, int]]:
    """Collapse a set of ints into contiguous (lo, hi) integer ranges."""
    out: list[tuple[int, int]] = []
    for x in sorted(set(nums)):
        if out and x == out[-1][1] + 1:
            out[-1] = (out[-1][0], x)
        else:
            out.append((x, x))
    return out


def _contig_str(chain: str, nums: Sequence[int]) -> str:
    return ",".join(f"{chain}{lo}" if lo == hi else f"{chain}{lo}-{hi}"
                    for lo, hi in _ranges(nums))


def _peptide_groups(residues, chain: str = "A", bond_cut: float = 2.0) -> str:
    """Group chain residues into peptide-bonded fragments (C(i)->N(i+1) < bond_cut) and
    return an RFD3 ``unindex`` contig string, e.g. ``"A1-2,A3-4,A5"``."""
    items = sorted(((rid, atoms) for _, rid, atoms, _ in residues), key=lambda x: x[0])
    groups: list[list[int]] = []
    for rid, atoms in items:
        if groups:
            prev_rid = groups[-1][-1]
            prev_atoms = dict(items)[prev_rid]
            bonded = ("C" in prev_atoms and "N" in atoms and rid == prev_rid + 1
                      and dist(prev_atoms["C"], atoms["N"]) < bond_cut)
            if bonded:
                groups[-1].append(rid)
                continue
        groups.append([rid])
    return ",".join(_contig_str(chain, g) for g in groups)


def emit_rfd3_spec(out_dir, name: str, motif_residues, substrate,
                   ligand_resname: str = "LIG", length: str = "100-150",
                   strategy: str = "SC_TyrHis", select_fixed_atoms: Mapping | None = None,
                   hbond_donor: Mapping | None = None,
                   hbond_acceptor: Mapping | None = None) -> dict:
    """Write the motif PDB (+ ``L:G`` colon copy), the RFD3 spec JSON and
    ``motif_meta.json`` for one strategy. Port of ``build_inputs.py`` ``main()``'s writer.

    Parameters
    ----------
    out_dir : output directory (created if needed).
    name : spec name; files are ``motif_<name>.pdb`` / ``motif_<name>_lcolon.pdb`` /
        ``<name>.json`` and the entry key in the merged ``motif_meta.json``.
    motif_residues : list of ``(resname, resid, {atom: xyz}, [ordered_atom_names])`` —
        already-placed motif residues (build the geometry with the theozyme helpers or
        :func:`build_asn` / :func:`place_sc_donor` / :func:`build_bb_dipeptide`).
    substrate : ``{atom_name: xyz}`` ligand/TS atoms.
    ligand_resname : 3-char ligand name; its colon alias is used as the spec ``ligand``
        and in the ``select_hbond_acceptor`` key.
    select_fixed_atoms : ``{"A1": "CB,CG,OD1,ND2", ...}`` (keys may be ranges, ``"A1-4"``).
    hbond_donor / hbond_acceptor : ``{"A1": "ND2"}`` / ``{"L:G": "N5,O15"}``.

    Derivations (deterministic, replace build_inputs' per-strategy hand-writing):
      * ``unindex`` from peptide-bond contiguity of the motif residues;
      * ``select_unfixed_sequence`` from residues whose fixed spec is backbone-only;
      * ``motif_meta`` donor->acceptor pairing by nearest ligand acceptor atom, with
        ``mode = "bb"`` for backbone-N donors else ``"sc"``.

    Returns a dict with ``name``, ``spec``, ``meta``, and the written paths.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    colon_name = colon_escape(ligand_resname)

    motif_path = out_dir / f"motif_{name}.pdb"
    motif_path, lcolon_path = write_motif(motif_path, motif_residues, substrate,
                                          ligand_resname=ligand_resname)

    # --- assemble the spec ---
    spec: dict = {
        "input": str(lcolon_path.resolve()),
        "ligand": colon_name,
        "length": length,
        "unindex": _peptide_groups(motif_residues),
    }
    if select_fixed_atoms:
        spec["select_fixed_atoms"] = dict(select_fixed_atoms)
        # residues fixed backbone-only -> designable identity -> select_unfixed_sequence
        unfixed: list[int] = []
        for key, val in select_fixed_atoms.items():
            if _is_backbone_only(val):
                _, rids = _parse_sel_key(key)
                unfixed += rids
        if unfixed:
            spec["select_unfixed_sequence"] = _contig_str("A", unfixed)
    if hbond_donor:
        spec["select_hbond_donor"] = dict(hbond_donor)
    if hbond_acceptor:
        # translate a key that names the plain ligand resname to its colon alias
        acc = {(colon_name if k == ligand_resname else k): v for k, v in hbond_acceptor.items()}
        spec["select_hbond_acceptor"] = acc

    # --- motif_meta: donor -> nearest acceptor pairing ---
    res_by_id = {rid: atoms for _, rid, atoms, _ in motif_residues}
    acc_atoms: list[str] = []
    for v in (hbond_acceptor or {}).values():
        acc_atoms += [a.strip() for a in v.split(",") if a.strip()]
    meta_entry: dict[str, dict] = {}
    for key, donor_atom in (hbond_donor or {}).items():
        _, rids = _parse_sel_key(key)
        rid = rids[0]
        atoms = res_by_id.get(rid, {})
        if donor_atom not in atoms:
            continue
        dpos = atoms[donor_atom]
        nearest, best = None, 1e18
        for a in acc_atoms:
            if a in substrate:
                dd = dist(dpos, substrate[a])
                if dd < best:
                    best, nearest = dd, a
        mode = "bb" if donor_atom in {"N", "CA", "C", "O"} else "sc"
        meta_entry[key] = {"donor": donor_atom, "acc": nearest, "mode": mode}

    # write <name>.json  (build_inputs wraps the spec under its name)
    spec_json = out_dir / f"{name}.json"
    spec_json.write_text(json.dumps({name: spec}, indent=2))

    # merge into a shared motif_meta.json (build_inputs accumulates all strategies)
    meta_json = out_dir / "motif_meta.json"
    meta_all = {}
    if meta_json.exists():
        try:
            meta_all = json.loads(meta_json.read_text())
        except json.JSONDecodeError:
            meta_all = {}
    meta_all[name] = meta_entry
    meta_json.write_text(json.dumps(meta_all, indent=2))

    return {
        "name": name,
        "strategy": strategy,
        "spec": spec,
        "meta": meta_entry,
        "motif_pdb": motif_path,
        "lcolon_pdb": lcolon_path,
        "spec_json": spec_json,
        "meta_json": meta_json,
    }


# ======================================================================================
# 4) build_mpnn_config  (port of build_mpnn_config.py — pure stdlib)
# ======================================================================================
def build_mpnn_config(design_dir, out_config, mpnn_out_dir, keys=None,
                      temperature: float = 0.1, nseq: int = 8, glob_pat: str = "*.cif.gz",
                      ckpt: str | None = None, chunk_size: int | None = None) -> Path:
    """Build a LigandMPNN config that fixes each design's catalytic residues.

    Per-design ``fixed_residues`` are read from each output's ``diffused_index_map`` so the
    right (per-design) residue ids stay fixed. ``model_type=ligand_mpnn`` so the ligand
    (chain X) conditions design.

    Parameters
    ----------
    keys : motif residue keys to fix, e.g. ``["A1","A2","A3"]`` or the string
        ``"A1,A2,A3"`` (default). Looked up in each design's ``diffused_index_map``.
    chunk_size : if set and exceeded, split into ``<stem>_NNN.json`` configs (mpnn buffers
        all results in RAM until a config finishes).

    Notes
    -----
    ``fixed_residues`` is ALWAYS a list; ``designed_chains`` is NEVER set alongside it —
    LigandMPNN forbids mixing residue- and chain-based constraints.

    Returns the written config :class:`Path` (the first chunk's path in chunk mode).
    """
    if keys is None:
        keys = ["A1", "A2", "A3"]
    elif isinstance(keys, str):
        keys = keys.split(",")
    keys = [k.strip() for k in keys]
    ckpt = ckpt or HOST_CKPT

    inputs = []
    for f in sorted(glob.glob(os.path.join(str(design_dir), glob_pat))):
        js = f.replace(".cif.gz", ".json")
        if not os.path.exists(js):
            continue
        dim = json.load(open(js)).get("diffused_index_map", {})
        fixed = [dim[k] for k in keys if k in dim]   # MUST be a list, e.g. ["A33","A114"]
        if not fixed:
            continue
        inputs.append({
            "structure_path": os.path.abspath(f),
            "name": os.path.basename(f).replace(".cif.gz", ""),
            # residue-based scope ONLY (no designed_chains): keep catalytic residues fixed,
            # design the rest. The ligand (chain X) is context for ligand_mpnn.
            "fixed_residues": fixed,
            "batch_size": nseq, "number_of_batches": 1,
            "temperature": temperature, "seed": 1,
        })
    os.makedirs(str(mpnn_out_dir), exist_ok=True)

    def cfg_for(sub):
        return {"model_type": "ligand_mpnn", "checkpoint_path": ckpt, "is_legacy_weights": True,
                "out_directory": os.path.abspath(str(mpnn_out_dir)),
                "write_fasta": True, "write_structures": True, "inputs": sub}

    out_config = str(out_config)
    if chunk_size and len(inputs) > chunk_size:
        stem, ext = os.path.splitext(out_config)
        chunks = [inputs[i:i + chunk_size] for i in range(0, len(inputs), chunk_size)]
        w = max(3, len(str(len(chunks) - 1)))
        written = []
        for i, sub in enumerate(chunks):
            p = f"{stem}_{i:0{w}d}{ext}"
            json.dump(cfg_for(sub), open(p, "w"), indent=2)
            written.append(Path(p))
        return written[0]
    json.dump(cfg_for(inputs), open(out_config, "w"), indent=2)
    return Path(out_config)


# ======================================================================================
# 5) check_built  (port of check_built.py; atom names generalised via catspec)
# ======================================================================================
DEFAULT_CATSPEC = {
    "forming_bond": ("C0", "C18"),
    "forming_bond_ideal": 2.244,
    # donor atom name -> the ligand acceptor it is meant to contact, in priority order
    "donors": [("ND2", "N5"), ("OG", "O15"), ("OG1", "O15"),
               ("OH", "O15"), ("NE2", "O15"), ("N", "O15")],
    "approach_atom": "C14",   # ligand atom defining the carbonyl-approach / splay geometry
    "splay_acceptor": "O15",
    "splay_qm": 78,
    "bb_donor_resnames": {"GLY"},   # only treat backbone N of these residues as a donor
}


def check_built(motif_pdb, catspec: Mapping | None = None) -> dict:
    """Pre-diffusion geometry self-check of a built motif PDB.

    Reports the forming-bond length, each catalytic donor->acceptor distance, backbone
    amide N-H deviation (idealised H from C(i-1),N,CA), sidechain CB-donor-acceptor angle
    + approach + CA-CB, the donor-acceptor-donor splay, and peptide-bond lengths.
    Atom names are generalised via ``catspec`` (defaults to the michaelase :data:`DEFAULT_CATSPEC`).

    Returns a structured dict (also suitable for assertions); pass nothing to print.
    """
    cs = dict(DEFAULT_CATSPEC)
    if catspec:
        cs.update(catspec)
    prot, lig = read_pdb(motif_pdb)

    approach = lig.get(cs["approach_atom"])
    splay_acc_name = cs["splay_acceptor"]
    splay_acc = lig.get(splay_acc_name)
    bb_res = set(cs.get("bb_donor_resnames", {"GLY"}))

    result: dict = {"motif_pdb": str(motif_pdb), "contacts": [], "splay": None,
                    "peptide_bonds": []}

    fa, fb = cs["forming_bond"]
    if fa in lig and fb in lig:
        result["forming_bond"] = {"atoms": [fa, fb], "distance": dist(lig[fa], lig[fb]),
                                  "ideal": cs.get("forming_bond_ideal")}

    splay_donors: list[np.ndarray] = []
    for (ch, rid, resn), at in sorted(prot.items(), key=lambda x: x[0][1]):
        for don, accn in cs["donors"]:
            if don not in at:
                continue
            if (resn, don) == ("GLY", "ND2"):
                continue
            if don == "N" and resn not in bb_res:
                continue  # only treat backbone N of designated residues as a donor
            acc = lig.get(accn)
            if acc is None:
                continue
            entry = {"residue": f"{resn}{rid}", "donor": don, "acceptor": accn,
                     "distance": dist(at[don], acc)}
            if don == "N":  # backbone amide donor
                entry["mode"] = "bb"
                cprev = None
                for (c2, r2, rn2), a2 in prot.items():
                    if r2 == rid - 1 and "C" in a2:
                        cprev = a2["C"]
                ca = at.get("CA")
                if cprev is not None and ca is not None:
                    hdir = -U(U(cprev - at["N"]) + U(ca - at["N"]))
                    entry["amide_NH_dev"] = ang(acc, at["N"], at["N"] + hdir)
            else:           # sidechain donor
                entry["mode"] = "sc"
                cb = at.get("CB")
                if cb is not None:
                    cba = ang(cb, at[don], acc)
                    entry["cb_donor_acc_angle"] = cba
                    entry["cb_angle_dev"] = abs(cba - 109.5)
                    if accn == splay_acc_name and approach is not None:
                        entry["approach"] = ang(approach, acc, at[don])
                    if "CA" in at:
                        entry["ca_cb"] = dist(at["CA"], cb)
            if accn == splay_acc_name:
                splay_donors.append(at[don])
            result["contacts"].append(entry)
            break

    if len(splay_donors) == 2 and splay_acc is not None:
        result["splay"] = {"acceptor": splay_acc_name,
                           "angle": ang(splay_donors[0], splay_acc, splay_donors[1]),
                           "qm": cs.get("splay_qm")}

    # peptide-bond check: any consecutive residues with a real C(i)->N(i+1) bond
    by_id = {rid: at for (ch, rid, resn), at in prot.items()}
    for rid in sorted(by_id):
        a, b = by_id.get(rid), by_id.get(rid + 1)
        if a and b and "C" in a and "N" in b:
            d = dist(a["C"], b["N"])
            if d < 2.0:
                result["peptide_bonds"].append({"res_i": rid, "res_j": rid + 1,
                                                "distance": d, "ideal": 1.33})
    return result


# ======================================================================================
# ported strategy builders (pyrosetta-free) — used by build_michaelase_inputs
# ======================================================================================
BB = ["N", "CA", "C", "O"]
ASN_ORD = ["N", "CA", "C", "O", "CB", "CG", "OD1", "ND2"]


def _place_by(res: Mapping[str, np.ndarray], names_src, target_pts):
    """Kabsch-fit residue atoms ``names_src`` onto ``target_pts``; transform all atoms."""
    P = np.array([res[n] for n in names_src])
    Q = np.array(target_pts)
    R, t = kabsch(P, Q)
    return {nm: R @ xyz + t for nm, xyz in res.items()}


def build_asn(nma_amine: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Asn sidechain amide CG(=OD1)-ND2 aligned onto the amine NMA's Cc(=Oc)-Nn."""
    asn = ideal_residue("ASN")
    return _place_by(asn, ["CG", "OD1", "ND2"],
                     [nma_amine["Cc"], nma_amine["Oc"], nma_amine["Nn"]])


def place_sc_donor(aa: str, donor: str, nma: Mapping[str, np.ndarray],
                   substrate: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Place a sidechain so its donor heavy atom sits at the QM amide-N position
    (``nma['Nn']``, on the O15 lone pair) and CB is oriented so the donor H can reach
    O15; spin about CB->donor to push the backbone stub away from the ligand."""
    o15 = substrate["O15"]; c14 = substrate["C14"]; c13 = substrate["C13"]
    nrm = U(np.cross(c13 - c14, o15 - c14))
    res = ideal_residue(aa)
    don_pos = nma["Nn"]
    v = U(don_pos - o15)
    TET = math.radians(109.5)
    cb_dir = U(math.cos(math.pi - TET) * v + math.sin(math.pi - TET) * nrm)
    v_ideal = U(res["CB"] - res[donor])
    Rm = align_vec(v_ideal, cb_dir)
    base = {nm: (Rm @ (xyz - res[donor])) + don_pos for nm, xyz in res.items()}
    ligc = np.array(list(substrate.values()))
    best, bestmin = None, -1.0
    for deg in range(0, 360, 12):
        spun = {nm: don_pos + rotate(p - don_pos, cb_dir, deg) for nm, p in base.items()}
        mind = min(np.linalg.norm(ligc - spun[a], axis=1).min()
                   for a in ("N", "CA", "C", "O") if a in spun)
        if mind > bestmin:
            bestmin, best = mind, spun
    return best


def build_bb_dipeptide(nma: Mapping[str, np.ndarray]):
    """Build a Gly-Gly dipeptide whose [res1 C, res2 N, res2 CA] match the NMA's
    [Cc, Nn, Cmeth_n] so res2's backbone amide N-H points at O15 (as the NMA does).
    Returns ``(res1_atoms, res2_atoms)``; fix BKBN of both; donor = res2 N."""
    di = build_polygly_backbone([0.0, 0.0], [0.0, 0.0])
    r1, r2 = di[0], di[1]
    P = np.array([r1["C"], r2["N"], r2["CA"]])
    Q = np.array([nma["Cc"], nma["Nn"], nma["Cmeth_n"]])
    R, t = kabsch(P, Q)
    r1 = {nm: R @ xyz + t for nm, xyz in r1.items()}
    r2 = {nm: R @ xyz + t for nm, xyz in r2.items()}
    return r1, r2


def find_nest(grid_phi=(-120, -90, -60, 60, 90), grid_psi=(-40, -15, 0, 15, 150, 170)):
    """Grid-search poly-Gly dihedrals for two ADJACENT residues whose amide N-H rays
    converge on a common point (a designable backbone oxyanion "nest"). Pyrosetta-free:
    backbones come from :func:`build_polygly_backbone`. Returns the best
    ``(score, residues, i1, i2, convergence_point, splay, NN)`` or ``None``."""
    best = None
    for ph2, ps2, ph3, ps3 in itertools.product(grid_phi, grid_psi, grid_phi, grid_psi):
        res = build_polygly_backbone([0, ph2, ph3, 0, 0], [0, ps2, ps3, 0, 0])
        cand = []
        for idx in (2, 3):
            at = res[idx]
            if "N" not in at or "H" not in at:
                continue
            u = U(at["H"] - at["N"]); P = at["N"] + 2.9 * u
            cand.append((idx, at["N"], u, P))
        if len(cand) < 2:
            continue
        (i1, N1, u1, P1), (i2, N2, u2, P2) = cand[0], cand[1]
        conv = dist(P1, P2)
        splay = ang(N1, (P1 + P2) / 2, N2)
        nn = dist(N1, N2)
        if conv < 1.2 and 35 < splay < 120 and nn > 2.0:
            if best is None or conv < best[0]:
                best = (conv, res, i1, i2, (P1 + P2) / 2, splay, nn)
    return best


def place_nest(res, i1, i2, Pc, substrate):
    """Translate the nest fragment so convergence point ``Pc`` -> O15, then rotate about
    O15 for a good lone-pair approach and minimal ligand clash. Returns
    ``(placed_residues, approach_deg, min_lig_dist)``."""
    o15 = substrate["O15"]; c14 = substrate["C14"]; c13 = substrate["C13"]
    shift = o15 - Pc
    res = [{k: v + shift for k, v in r.items()} for r in res]
    ligc = np.array(list(substrate.values()))
    nrm = U(np.cross(c13 - c14, o15 - c14))
    axes = [np.array([1., 0, 0]), np.array([0, 1., 0]), np.array([0, 0, 1.]), nrm, U(c14 - o15)]
    best = None
    for ax in axes:
        for deg in range(0, 360, 20):
            spun = [{k: o15 + rotate(v - o15, ax, deg) for k, v in r.items()} for r in res]
            d1, d2 = spun[i1], spun[i2]
            app = 0.5 * (ang(c14, o15, d1["N"]) + ang(c14, o15, d2["N"]))
            bbatoms = [r[a] for r in spun for a in ("N", "CA", "C", "O") if a in r]
            mind = min(np.linalg.norm(ligc - b, axis=1).min() for b in bbatoms)
            score = abs(app - 120) + max(0, 2.4 - mind) * 50
            if best is None or score < best[0]:
                best = (score, spun, app, mind)
    return best[1], best[2], best[3]


# ======================================================================================
# integration: full build_inputs.main() + build_nest port (pyrosetta-free)
# ======================================================================================
# default michaelase atom map (the only place the substrate ordering lives now)
MICHAELASE_ATOM_MAP = {
    "substrate": ["C0", "C1", "O2", "O3", "C4", "N5", "C6", "C7", "C8", "C9", "C10",
                  "C11", "C12", "C13", "C14", "O15", "O16", "C17", "C18"],
    "catalytic": {"amine": {"nma_slice": [35, 47]},
                  "ox1": {"nma_slice": [47, 59]},
                  "ox2": {"nma_slice": [59, 71]}},
    "n_atoms": 71,
    "elements": ["C", "N", "O", "H", "S"],
}


def build_michaelase_inputs(xyz_path, out_dir, atom_map: Mapping | None = None,
                            strategies: Iterable[str] = ("BB_frag", "BB_free",
                                                         "SC_SerThr", "SC_TyrHis", "BB_nest"),
                            length: str = "100-150") -> dict:
    """End-to-end port of ``build_inputs.py main()`` + ``build_nest.py`` (no pyrosetta).

    Parses the TS, builds the requested strategy motifs from RDKit ideal residues / the
    NeRF backbone builder, and calls :func:`emit_rfd3_spec` for each. Returns a dict of
    ``{spec_name: emit_rfd3_spec(...) result}``.
    """
    ts = parse_ts(xyz_path, atom_map or MICHAELASE_ATOM_MAP)
    sub, nma = ts["substrate"], ts["catalytic"]
    asn = build_asn(nma["amine"])
    results: dict = {}

    if "BB_frag" in strategies:
        f1a, f1b = build_bb_dipeptide(nma["ox1"])
        f2a, f2b = build_bb_dipeptide(nma["ox2"])
        res = [("GLY", 1, f1a, BB), ("GLY", 2, f1b, BB),
               ("GLY", 3, f2a, BB), ("GLY", 4, f2b, BB), ("ASN", 5, asn, ASN_ORD)]
        results["mich_bb_frag"] = emit_rfd3_spec(
            out_dir, "mich_bb_frag", res, sub, length=length, strategy="BB_frag",
            select_fixed_atoms={"A1": "BKBN", "A2": "BKBN", "A3": "BKBN", "A4": "BKBN",
                                "A5": "CB,CG,OD1,ND2"},
            hbond_donor={"A2": "N", "A4": "N", "A5": "ND2"},
            hbond_acceptor={"L:G": "N5,O15"})

    if "BB_free" in strategies:
        results["mich_bb_free"] = emit_rfd3_spec(
            out_dir, "mich_bb_free", [("ASN", 1, asn, ASN_ORD)], sub, length=length,
            strategy="BB_free", select_fixed_atoms={"A1": "CB,CG,OD1,ND2"},
            hbond_donor={"A1": "ND2"}, hbond_acceptor={"L:G": "N5,O15"})

    if "SC_SerThr" in strategies:
        ser = place_sc_donor("S", "OG", nma["ox1"], sub)
        thr = place_sc_donor("T", "OG1", nma["ox2"], sub)
        res = [("ASN", 1, asn, ASN_ORD),
               ("SER", 2, ser, ["N", "CA", "C", "O", "CB", "OG"]),
               ("THR", 3, thr, ["N", "CA", "C", "O", "CB", "OG1", "CG2"])]
        results["mich_sc_serthr"] = emit_rfd3_spec(
            out_dir, "mich_sc_serthr", res, sub, length=length, strategy="SC_SerThr",
            select_fixed_atoms={"A1": "CB,CG,OD1,ND2", "A2": "CB,OG", "A3": "CB,OG1,CG2"},
            hbond_donor={"A1": "ND2", "A2": "OG", "A3": "OG1"},
            hbond_acceptor={"L:G": "N5,O15"})

    if "SC_TyrHis" in strategies:
        tyr = place_sc_donor("Y", "OH", nma["ox1"], sub)
        his = place_sc_donor("H", "NE2", nma["ox2"], sub)
        tyr_fix = "CB,CG,CD1,CD2,CE1,CE2,CZ,OH"
        his_fix = "CB,CG,ND1,CD2,CE1,NE2"
        res = [("ASN", 1, asn, ASN_ORD),
               ("TYR", 2, tyr, ["N", "CA", "C", "O"] + tyr_fix.split(",")),
               ("HIS", 3, his, ["N", "CA", "C", "O"] + his_fix.split(","))]
        results["mich_sc_tyrhis"] = emit_rfd3_spec(
            out_dir, "mich_sc_tyrhis", res, sub, length=length, strategy="SC_TyrHis",
            select_fixed_atoms={"A1": "CB,CG,OD1,ND2", "A2": tyr_fix, "A3": his_fix},
            hbond_donor={"A1": "ND2", "A2": "OH", "A3": "NE2"},
            hbond_acceptor={"L:G": "N5,O15"})

    if "BB_nest" in strategies:
        nb = find_nest()
        if nb is not None:
            _, res, i1, i2, Pc, splay, nn = nb
            placed, app, mind = place_nest(res, i1, i2, Pc, sub)
            lo = min(i1, i2) - 1; hi = max(i1, i2)
            frag = placed[lo:hi + 1]
            nfrag = len(frag)
            residues = [("GLY", k + 1, frag[k], BB) for k in range(nfrag)]
            asn_resid = nfrag + 1
            residues.append(("ASN", asn_resid, asn, ASN_ORD))
            dN1 = (i1 - lo) + 1; dN2 = (i2 - lo) + 1
            results["mich_bb_nest"] = emit_rfd3_spec(
                out_dir, "mich_bb_nest", residues, sub, length=length, strategy="BB_nest",
                select_fixed_atoms={f"A1-{nfrag}": "BKBN", f"A{asn_resid}": "CB,CG,OD1,ND2"},
                hbond_donor={f"A{dN1}": "N", f"A{dN2}": "N", f"A{asn_resid}": "ND2"},
                hbond_acceptor={"L:G": "N5,O15"})
    return results


if __name__ == "__main__":  # pragma: no cover
    print("enzyme_build: import and call; see module docstring + public API in __all__.")
