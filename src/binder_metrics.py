"""
Score RF3 refolds of foundry binder designs.

One row per refold, combining RF3's own confidence numbers with geometric
agreement between the refold and the RFD3 design it came from, plus ipSAE.

Ported from ``data/BCR/scripts/score_refolds.py`` (validated row-for-row against
its output on the 28 420-refold CD79b campaign) and extended with ipSAE.  Runs
natively in LPT's venv — biotite is a direct dependency here, so unlike the BCR
original this needs no ``cd foundry && uv run`` wrapper.

## Why confidence alone is not enough

RF3 templating cannot convey a docked pose: in ``rf3/data/ground_truth_template``
every inter-molecule token pair is masked out of the template distogram
(``p_provide_inter_molecule_distances`` is 0.0 at inference, with no Hydra key to
change it).  Docking is therefore always de novo, and iPTM only reports how sure
RF3 is about the interface it *chose*.  On the CD79b campaign the top-iPTM refold
(0.72) was docked ~30 residues from the designed epitope.  Ranking on confidence
alone selects confidently mis-docked binders; ``binder_rmsd_dock``,
``epitope_recall`` and hotspot engagement are what catch them.

ipSAE improves on iPTM but does NOT fix this — it is still a confidence metric,
and a mis-docked interface that the model is sure about scores well.  Use it
alongside the geometric gates, never instead of them.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from loguru import logger

BACKBONE = ("N", "CA", "C", "O")
POLAR = ("N", "O", "S")
MPNN_SUFFIX = re.compile(r"_b\d+_d\d+$")

# ipSAE defaults.  The PAE cutoff is the one free parameter of the method; 10 A
# is the reference implementation's worked-example value.
IPSAE_PAE_CUTOFF = 10.0
IPSAE_VARIANT = "d0res_max_pae10"


# ----------------------------------------------------------------------
# Structure IO
# ----------------------------------------------------------------------

def read_structure(path: Path, plddt_scale: float = 1.0):
    """
    Parse a CIF (optionally .gz) or PDB and drop NaN-coordinate atoms.

    MPNN writes terminal OXT atoms it never placed as NaN; left in, they poison
    every distance and superposition downstream.

    `plddt_scale` normalises the B-factor column to RF3's 0-1 convention at the
    source, so every downstream consumer (mean_plddt, thresholds calibrated on
    0-1 values) needs no per-backend awareness. RF3 already writes 0-1
    (scale=1.0, a no-op); Protenix writes 0-100 (scale=100.0).
    """
    import biotite.structure as struc  # noqa: F401  (needed for the array API)

    path = Path(path)
    if path.suffix == ".pdb":
        from biotite.structure.io.pdb import PDBFile, get_structure as pdb_get_structure
        pdb = PDBFile.read(str(path))
        try:
            a = pdb_get_structure(pdb, model=1, extra_fields=["b_factor"])
        except Exception:
            a = pdb_get_structure(pdb, model=1)
    else:
        from biotite.structure.io.pdbx import CIFFile, get_structure
        if path.suffix == ".gz":
            with gzip.open(path, "rt") as fh:
                cif = CIFFile.read(fh)
        else:
            cif = CIFFile.read(path)
        # b_factor is not a default annotation, and RF3 stores per-atom pLDDT
        # there. Fall back gracefully for inputs with no B-factor column at all
        # (RFD3 design CIFs) so geometry-only scoring still works.
        try:
            a = get_structure(cif, model=1, extra_fields=["b_factor"])
        except Exception:
            a = get_structure(cif, model=1)
    a = a[~np.isnan(a.coord).any(axis=1)]
    if plddt_scale != 1.0 and "b_factor" in a.get_annotation_categories():
        a.b_factor = a.b_factor / plddt_scale
    return a


def ca(atoms, chain: str):
    """CA atoms of one chain, ordered by residue id."""
    x = atoms[(atoms.chain_id == chain) & (atoms.atom_name == "CA")]
    return x[np.argsort(x.res_id)]


def matched_ca(a, b, chain: str):
    """CA arrays for `chain`, restricted to residue ids present in both."""
    xa, xb = ca(a, chain), ca(b, chain)
    shared = np.intersect1d(xa.res_id, xb.res_id)
    return xa[np.isin(xa.res_id, shared)], xb[np.isin(xb.res_id, shared)]


def matched_backbone(a, b, chain: str):
    """
    Backbone atoms of `chain` paired on (res_id, atom_name).

    Pairing positionally with zip() silently compares an N to a CA the moment
    one structure is missing an atom in a residue.
    """
    xa = a[(a.chain_id == chain) & np.isin(a.atom_name, BACKBONE)]
    xb = b[(b.chain_id == chain) & np.isin(b.atom_name, BACKBONE)]
    ka = {(r, n): i for i, (r, n) in enumerate(zip(xa.res_id, xa.atom_name))}
    kb = {(r, n): i for i, (r, n) in enumerate(zip(xb.res_id, xb.atom_name))}
    shared = sorted(ka.keys() & kb.keys())
    if not shared:
        return xa[[]], xb[[]]
    return xa[[ka[k] for k in shared]], xb[[kb[k] for k in shared]]


def mean_plddt(atoms, chain: str) -> float | str:
    """
    Mean per-residue pLDDT of a chain.

    RF3 stores pLDDT **per atom** (0-1) in the CIF B-factor column.  Averaging
    atoms directly weights a Trp roughly twice a Gly and de-calibrates any
    threshold set on per-residue values, so average within each residue first.
    """
    x = atoms[atoms.chain_id == chain]
    if x.array_length() == 0 or "b_factor" not in x.get_annotation_categories():
        return ""
    _, inv = np.unique(x.res_id, return_inverse=True)
    per_res = np.bincount(inv, weights=x.b_factor) / np.bincount(inv)
    return round(float(per_res.mean()), 4)


def binder_sequence(atoms, chain: str) -> str:
    import biotite.structure as struc

    x = ca(atoms, chain)
    return "".join(struc.info.one_letter_code(r) or "X" for r in x.res_name)


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------

def epitope(atoms, binder: str, target: str, cutoff: float,
            binder_backbone_only: bool = True) -> set[int]:
    """
    Target residue ids within `cutoff` of a binder atom.

    `binder_backbone_only` restricts the binder side to N/CA/C/O.  That is right
    for the design-vs-refold *comparison* (epitope_jaccard / epitope_recall):
    RFD3's binder sidechains belong to RFD3's own sequence, not the MPNN sequence
    being refolded, so an all-atom comparison would contrast sidechains that were
    never part of the design under test.

    It is wrong for hotspot engagement, which asks about one structure on its own
    and where the sidechains are exactly what packs the hotspot.  Measured on 400
    RFD3 designs all *built* on their hotspots (so ~100% is ground truth),
    backbone-only scored 70% engaging vs 99% all-atom, and all-atom still
    separates cleanly on refolds (96.4% on-epitope vs 2.8% off-epitope).
    """
    bd = atoms[atoms.chain_id == binder]
    if binder_backbone_only:
        bd = bd[np.isin(bd.atom_name, BACKBONE)]
    tg = atoms[atoms.chain_id == target]
    if bd.array_length() == 0 or tg.array_length() == 0:
        return set()
    d = np.linalg.norm(tg.coord[:, None, :] - bd.coord[None, :, :], axis=-1)
    return set(tg.res_id[(d < cutoff).any(axis=1)].tolist())


def clashes(atoms, binder: str, target: str) -> tuple[int, int]:
    """
    Inter-chain steric violations on the raw prediction: (violations, severe).

    `severe` counts heavy-atom pairs closer than 2.2 A — too close for any
    chemistry, salt bridges included.  `violations` uses a polar-aware cutoff:
    an N/O/S pair may legitimately reach 2.5 A as an H-bond, anything else needs
    3.2 A.

    RF3's own `has_clash` does not catch these — it was False for all 400 designs
    in the reference set, 159 of which had a sub-2.2 A contact — so this is the
    only clash signal available on the prediction itself.  A couple of violations
    is prediction noise, not a bad design.
    """
    bd = atoms[atoms.chain_id == binder]
    tg = atoms[atoms.chain_id == target]
    if bd.array_length() == 0 or tg.array_length() == 0:
        return 0, 0
    d = np.linalg.norm(bd.coord[:, None, :] - tg.coord[None, :, :], axis=-1)
    polar_pair = (np.isin(bd.element, POLAR)[:, None]
                  & np.isin(tg.element, POLAR)[None, :])
    limit = np.where(polar_pair, 2.5, 3.2)
    return int((d < limit).sum()), int((d < 2.2).sum())


# ----------------------------------------------------------------------
# ipSAE
# ----------------------------------------------------------------------

def calc_d0(L: float, min_value: float = 1.0) -> float:
    """
    TM-score distance normaliser (reference ``calc_d0``).

    Below L = 27 the cube-root expression goes negative, so it clamps instead.
    """
    L = float(L)
    if L > 27.0:
        return max(min_value, 1.24 * (L - 15.0) ** (1.0 / 3.0) - 1.8)
    return max(min_value, 1.0)


def calc_d0_array(L: np.ndarray, min_value: float = 1.0) -> np.ndarray:
    """
    Vectorised d0 for the per-residue variant (reference ``calc_d0_array``).

    Deliberately NOT the same function as :func:`calc_d0`: the reference floors
    the *length* at 26 and then evaluates the cube root, rather than branching on
    L > 27.  The two agree everywhere except L = 27, where this returns 1.0389
    and calc_d0 returns 1.0.  Kept separate so ipsae_d0res matches the reference
    exactly.
    """
    L = np.maximum(26.0, np.asarray(L, dtype=float))
    return np.maximum(min_value, 1.24 * (L - 15.0) ** (1.0 / 3.0) - 1.8)


def _ptm(x: np.ndarray, d0: float) -> np.ndarray:
    return 1.0 / (1.0 + (x / d0) ** 2.0)


@dataclass
class IpsaeResult:
    ipsae_min: float
    ipsae_max: float
    ipsae_binder: float
    ipsae_target: float
    ipsae_d0chn: float
    ipsae_d0dom: float
    n_ipsae_pairs: int
    variant: str = IPSAE_VARIANT


def ipsae_from_confidences(
    conf: dict,
    binder_chain: str = "A",
    target_chain: str = "B",
    pae_cutoff: float = IPSAE_PAE_CUTOFF,
) -> IpsaeResult | None:
    """
    ipSAE from an RF3 ``<id>_confidences.json``.

    ipSAE is a pTM-style score restricted to inter-chain residue pairs whose PAE
    clears a cutoff, with d0 derived from the number of surviving partners rather
    than from total complex length.  That is the whole point versus iPTM: iPTM's
    global-length d0 inflates the score whenever the target is large, which is
    how a 30 A mis-dock scored 0.72 on the reference campaign.

    Reference implementation: github.com/DunbrackLab/IPSAE
    ("Res ipSAE loquuntur", bioRxiv 2025).

        ptm(x, d0) = 1 / (1 + (x/d0)^2)
        d0(L)      = max(1.0, 1.24*(L-15)^(1/3) - 1.8)  for L > 27, else 1.0

        direction(A, B):
            for residue i in A:
                S_i   = { j in B : PAE[i][j] < cutoff }        # n0res
                ptm_i = mean over j in S_i of ptm(PAE[i][j], d0(|S_i|))
            return max_i ptm_i

    PAE[i][j] is the error at token j when the structure is aligned on token i,
    so "align on A, read error at B" means rows in A and columns in B — the
    matrix is NOT symmetric and the two directions are genuinely different
    numbers.

    Returns the canonical ``ipsae_max`` (the published definition) alongside
    ``ipsae_min``, the stricter min-of-both-directions used as this pipeline's
    primary metric: it fails a design that is confident in only one direction,
    which is the failure mode RF3 produces here.  ``d0chn`` (d0 from total chain
    lengths) and ``d0dom`` (d0 from residues with any sub-cutoff pair) are also
    emitted for comparability with published numbers.

    Returns None when the file lacks the fields or either chain is absent.
    """
    try:
        pae = np.asarray(conf["pae"], dtype=float)
        chains = np.asarray(
            [str(c).split("_")[0] for c in conf["token_chain_ids"]]
        )
    except (KeyError, TypeError, ValueError):
        return None
    if pae.ndim != 2 or pae.shape[0] != pae.shape[1] or pae.shape[0] != chains.size:
        return None

    a_idx = np.flatnonzero(chains == binder_chain)
    b_idx = np.flatnonzero(chains == target_chain)
    if a_idx.size == 0 or b_idx.size == 0:
        return None

    def direction(rows: np.ndarray, cols: np.ndarray) -> tuple[float, int]:
        block = pae[np.ix_(rows, cols)]          # rows = aligned-on chain
        valid = block < pae_cutoff
        n0res = valid.sum(axis=1)
        best, n_pairs = 0.0, int(n0res.sum())
        d0_byres = calc_d0_array(n0res)
        for i in np.flatnonzero(n0res):
            score = float(_ptm(block[i][valid[i]], d0_byres[i]).mean())
            if score > best:
                best = score
        return best, n_pairs

    ab, n_ab = direction(a_idx, b_idx)
    ba, n_ba = direction(b_idx, a_idx)

    # d0chn / d0dom differ from d0res only in where d0 comes from; the reduction
    # is the same — per-residue mean over valid partners, then max over residues.
    # (Averaging the whole block instead would drag every value down with the
    # non-interface rows.)
    def fixed_d0(rows: np.ndarray, cols: np.ndarray, d0: float) -> float:
        blk = pae[np.ix_(rows, cols)]
        ok = blk < pae_cutoff
        scored = _ptm(blk, d0)
        best = 0.0
        for i in np.flatnonzero(ok.sum(axis=1)):
            best = max(best, float(scored[i][ok[i]].mean()))
        return best

    d0chn = calc_d0(a_idx.size + b_idx.size)
    block = pae[np.ix_(a_idx, b_idx)]
    valid = block < pae_cutoff
    n_dom = int(valid.any(axis=1).sum() + valid.any(axis=0).sum())
    d0dom = calc_d0(n_dom)
    mean_chn = max(fixed_d0(a_idx, b_idx, d0chn), fixed_d0(b_idx, a_idx, d0chn))
    mean_dom = max(fixed_d0(a_idx, b_idx, d0dom), fixed_d0(b_idx, a_idx, d0dom))

    return IpsaeResult(
        ipsae_min=round(min(ab, ba), 4),
        ipsae_max=round(max(ab, ba), 4),
        ipsae_binder=round(ab, 4),
        ipsae_target=round(ba, 4),
        ipsae_d0chn=round(mean_chn, 4),
        ipsae_d0dom=round(mean_dom, 4),
        n_ipsae_pairs=n_ab + n_ba,
    )


def ipsae_from_pae_matrix(
    pae: np.ndarray,
    n_binder: int,
    binder_chain: str = "A",
    target_chain: str = "B",
    pae_cutoff: float = IPSAE_PAE_CUTOFF,
) -> IpsaeResult | None:
    """
    ipSAE from a plain N x N PAE matrix plus a chain-boundary token count.

    Same math as :func:`ipsae_from_confidences` (RF3's `<id>_confidences.json`,
    which instead carries per-token chain labels) — built for cluster-path
    backends (e.g. Protenix's `<id>_pae.npy`) that write the PAE as a bare
    array with no per-token metadata. `n_binder` is the binder's token count;
    tokens [0, n_binder) are assumed chain A (binder), [n_binder, N) chain B
    (target) — the convention every backend in the cluster pipeline's
    `build_fold_yaml.py` uses (binder listed first). Delegates to
    `ipsae_from_confidences` by synthesising the token_chain_ids it expects,
    so the two stay identical by construction rather than by copied logic.
    """
    pae = np.asarray(pae, dtype=float)
    if pae.ndim != 2 or pae.shape[0] != pae.shape[1] or not (0 < n_binder < pae.shape[0]):
        return None
    n = pae.shape[0]
    chain_ids = [binder_chain] * n_binder + [target_chain] * (n - n_binder)
    return ipsae_from_confidences(
        {"pae": pae, "token_chain_ids": chain_ids},
        binder_chain, target_chain, pae_cutoff,
    )


# ----------------------------------------------------------------------
# Hotspot remapping
# ----------------------------------------------------------------------

def hotspots_from_rfd3(path: Path, target_chain: str = "B") -> list[int]:
    """
    Read ``select_hotspots`` from an RFD3 sidecar JSON, in OUTPUT numbering.

    The spec lists hotspots in *input* numbering (e.g. ``C76``); a design sidecar
    also carries ``diffused_index_map`` giving the input->output relabelling
    (``C76 -> B33``).  **Always pass a design sidecar, never the input spec** —
    the input spec has no map, so the residue numbers are taken as-is and the
    scoring silently measures the wrong residues.
    """
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if "specification" in d:                       # design sidecar
        spec, imap = d["specification"], d.get("diffused_index_map", {})
    else:                                          # input spec: keyed by design
        spec, imap = next(iter(d.values())), {}
    hs = spec.get("select_hotspots") or {}
    if not imap and hs:
        logger.warning(
            f"{Path(path).name} has no diffused_index_map (this is an RFD3 INPUT "
            f"spec, not a design sidecar) — hotspot residue numbers are being "
            f"taken as-is and their input chain letters ignored. Scores may refer "
            f"to the wrong residues. Pass a *_model_*.json sidecar instead."
        )
    out = []
    for key in hs:
        mapped = imap.get(key, key)
        if imap and mapped[0] != target_chain:
            continue
        out.append(int(mapped[1:]))
    return sorted(out)


def find_design(design_dir: Path, name: str) -> Path:
    """
    Locate the RFD3 design a refold `name` came from.

    Refold ids are MPNN filenames ``<design>_b<b>_d<d>``.  Tries that name first
    (an MPNN output dir), then the RFD3 name with the sampling suffix stripped.
    Returns the last candidate if none exist, so the caller reports a readable
    missing-file error rather than a None.
    """
    design_dir = Path(design_dir)
    stems = [name]
    base = MPNN_SUFFIX.sub("", name)
    if base != name:
        stems.append(base)
    cands = [design_dir / f"{s}{ext}" for s in stems for ext in (".cif", ".cif.gz")]
    return next((c for c in cands if c.exists()), cands[-1])


def design_family(name: str) -> str:
    """The RFD3 backbone a refold belongs to (its `_b<b>_d<d>` siblings share it)."""
    return MPNN_SUFFIX.sub("", name)


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------

FIELDS = [
    "name", "design_family", "pass", "fail_reason",
    # ipSAE
    "ipsae_min", "ipsae_binder", "ipsae_target", "ipsae_max",
    "ipsae_d0chn", "ipsae_d0dom", "n_ipsae_pairs", "ipsae_variant",
    # consistency vs the design
    "binder_rmsd_dock", "binder_rmsd_fold", "binder_rmsd_bb", "binder_tm",
    "target_rmsd",
    "epitope_jaccard", "epitope_recall", "hotspots_refold", "hotspots_design",
    "n_hotspots", "hotspot_engagement",
    "clash_violations", "clash_severe",
    "n_epitope_design", "n_epitope_refold", "n_epitope_shared",
    # RF3 confidence
    "iptm", "ptm", "plddt", "binder_plddt", "binder_ptm", "target_ptm",
    "iface_pae", "iface_pae_min", "iface_pde_min", "has_clash", "ranking_score",
    # RFD3 sidecar
    "rfd3_n_chainbreaks", "rfd3_sc_clashes", "rfd3_bb_clashes",
    "rfd3_non_loop_fraction", "rfd3_rog", "sampled_contig",
    # bookkeeping
    "binder_len", "binder_seq", "refold_cif", "design_cif", "error",
]


@dataclass(frozen=True)
class ScoreConfig:
    binder_chain: str = "A"
    target_chain: str = "B"
    contact_cutoff: float = 8.0
    ipsae_pae_cutoff: float = IPSAE_PAE_CUTOFF


def _pair(summary: dict, key: str):
    """
    Interface value out of a chain_pair_* matrix.

    These are upper-triangular with None on and below the diagonal, so the
    interface value is [0][1] — indexing the diagonal returns None.
    """
    m = summary.get(key)
    try:
        return m[0][1]
    except (TypeError, IndexError, KeyError):
        return None


def score_one(
    name: str,
    pred_path: Path,
    design_path: Path,
    summary: dict,
    hotspots: Sequence[int],
    cfg: ScoreConfig = ScoreConfig(),
    conf: dict | None = None,
    sidecar: dict | None = None,
    plddt_scale: float = 1.0,
) -> dict:
    """
    Score one refold. Raises on unreadable structures; the caller records it.

    `plddt_scale` is the only backend-specific knob: RF3 (default 1.0, B-factor
    already 0-1) vs Protenix (100.0, B-factor is 0-100). Everything else here —
    RMSD, TM-score, epitope/hotspot geometry, clashes — reads structure
    coordinates only and is backend-agnostic; `summary`/`conf` carry whatever
    confidence numbers the caller's backend produced.
    """
    import biotite.structure as struc

    B, T = cfg.binder_chain, cfg.target_chain
    row: dict = {
        "name": name,
        "design_family": design_family(name),
        "refold_cif": str(pred_path),
        "design_cif": str(design_path),
        "n_hotspots": len(hotspots),
        "ipsae_variant": IPSAE_VARIANT,
    }

    cp = summary.get("chain_ptm") or [None, None]
    row.update({
        "iptm": summary.get("iptm"), "ptm": summary.get("ptm"),
        "plddt": summary.get("overall_plddt"),
        "binder_ptm": cp[0] if len(cp) > 0 else None,
        "target_ptm": cp[1] if len(cp) > 1 else None,
        "iface_pae": _pair(summary, "chain_pair_pae"),
        "iface_pae_min": _pair(summary, "chain_pair_pae_min"),
        "iface_pde_min": _pair(summary, "chain_pair_pde_min"),
        "has_clash": summary.get("has_clash"),
        "ranking_score": summary.get("ranking_score"),
    })

    if conf is not None:
        ips = ipsae_from_confidences(conf, B, T, cfg.ipsae_pae_cutoff)
        if ips is not None:
            row.update({
                "ipsae_min": ips.ipsae_min, "ipsae_max": ips.ipsae_max,
                "ipsae_binder": ips.ipsae_binder, "ipsae_target": ips.ipsae_target,
                "ipsae_d0chn": ips.ipsae_d0chn, "ipsae_d0dom": ips.ipsae_d0dom,
                "n_ipsae_pairs": ips.n_ipsae_pairs,
            })

    if sidecar is not None:
        m = sidecar.get("metrics") or {}
        row.update({
            "rfd3_n_chainbreaks": m.get("n_chainbreaks"),
            # RFD3 sidecar metrics are FLAT DOTTED KEYS, not nested objects:
            # "n_clashing.interresidue_clashes_w_sidechain" is one string. Read
            # as nested this silently yields None for every design ever scored.
            # `foundry_runner.prefilter_designs` reads the same two keys —
            # keep both spellings identical.
            "rfd3_sc_clashes": m.get("n_clashing.interresidue_clashes_w_sidechain"),
            "rfd3_bb_clashes": m.get("n_clashing.interresidue_clashes_w_backbone"),
            "rfd3_non_loop_fraction": m.get("non_loop_fraction"),
            "rfd3_rog": m.get("radius_of_gyration"),
            "sampled_contig": ((sidecar.get("specification") or {}).get("extra")
                               or {}).get("sampled_contig"),
        })

    des = read_structure(design_path)
    prd = read_structure(pred_path, plddt_scale=plddt_scale)

    dA, pA = matched_ca(des, prd, B)
    dT, pT = matched_ca(des, prd, T)
    if dA.array_length() == 0 or dT.array_length() == 0:
        raise ValueError(f"no shared residues for chain {B}/{T}")

    _, tf_t = struc.superimpose(dT, pT)               # fit on the TARGET only
    row["target_rmsd"] = round(float(struc.rmsd(dT, tf_t.apply(pT))), 3)
    # Binder RMSD *in the target frame*: no binder-on-binder fitting, so this
    # measures the docked pose rather than just the fold. The primary gate.
    row["binder_rmsd_dock"] = round(float(struc.rmsd(dA, tf_t.apply(pA))), 3)
    fit_a, _ = struc.superimpose(dA, pA)              # binder -> binder
    row["binder_rmsd_fold"] = round(float(struc.rmsd(dA, fit_a)), 3)

    bA, bP = matched_backbone(des, prd, B)
    if bA.array_length():
        fit_bb, _ = struc.superimpose(bA, bP)
        row["binder_rmsd_bb"] = round(float(struc.rmsd(bA, fit_bb)), 3)
    else:
        row["binder_rmsd_bb"] = ""

    # TM-score uses a TM-optimal superposition, not the RMSD-optimal one above:
    # RMSD fitting lets a few badly-placed termini drag the whole alignment and
    # understates the score (0.72 vs 0.91 on one reference design).
    try:
        sup, _, ri, si = struc.superimpose_structural_homologs(dA, pA, max_iterations=5)
        row["binder_tm"] = round(
            float(struc.tm_score(dA, sup, ri, si, reference_length="reference")), 4)
    except Exception:
        row["binder_tm"] = ""

    ed = epitope(des, B, T, cfg.contact_cutoff)
    ep = epitope(prd, B, T, cfg.contact_cutoff)
    # Hotspot engagement is a per-structure question, so it uses the full binder
    # including sidechains — see epitope().
    ed_hot = epitope(des, B, T, cfg.contact_cutoff, binder_backbone_only=False)
    ep_hot = epitope(prd, B, T, cfg.contact_cutoff, binder_backbone_only=False)
    shared = ed & ep
    row["n_epitope_design"] = len(ed)
    row["n_epitope_refold"] = len(ep)
    row["n_epitope_shared"] = len(shared)
    row["epitope_jaccard"] = round(len(shared) / len(ed | ep), 3) if (ed | ep) else ""
    row["epitope_recall"] = round(len(shared) / len(ed), 3) if ed else ""
    row["hotspots_refold"] = sum(1 for h in hotspots if h in ep_hot)
    row["hotspots_design"] = sum(1 for h in hotspots if h in ed_hot)
    row["hotspot_engagement"] = (
        round(row["hotspots_refold"] / len(hotspots), 3) if hotspots else ""
    )

    v, sev = clashes(prd, B, T)
    row["clash_violations"] = v
    row["clash_severe"] = sev

    row["binder_len"] = dA.array_length()
    row["binder_seq"] = binder_sequence(prd, B)
    row["binder_plddt"] = mean_plddt(prd, B)
    return row


# ----------------------------------------------------------------------
# Protenix (cluster refold backend) adapter
# ----------------------------------------------------------------------
#
# The cluster pipeline's folding-repo backends (Protenix, IntelliFold2, ...)
# write a differently-SHAPED output than RF3: `<id>.pdb` (pLDDT 0-100 in
# B-factor, vs RF3's `<id>_model.cif` at 0-1), `<id>_scores.json` (vs RF3's
# `<id>_summary_confidences.json`), and `<id>_pae.npy` — a bare N x N array,
# written by some backends and not others, rather than RF3's
# `<id>_confidences.json` with its own token_chain_ids/token_res_ids.
#
# NOT independently verified against a real cluster-produced Protenix run
# (this workstation has no SLURM access) — `_scores.json`'s exact key names
# below are the best reading of g-groups/.../binder_pipeline's own
# `score_designs.py` + `docs/refold_backends.md` at the time this was written.
# Confirmed keys: `iptm_per_chain_pair` (dict, "A_B"/"B_A"), `plddt_mean`
# (whole-complex, NOT used here — see mean_plddt for the binder-only value).
# Everything else degrades to `None`/missing rather than raising, exactly like
# `score_one`'s own handling of an RF3 summary missing a field, so a first
# real run will show up as thinner rows (still scored on RMSD/epitope/
# hotspot/ipSAE) rather than a crash — smoke-test one real campaign's rows
# before trusting `iptm`/`ptm` columns from this path.

def protenix_confidence_adapter(
    scores: dict,
    n_binder: int,
    n_target: int,
    pae: np.ndarray | None,
) -> tuple[dict, dict | None]:
    """
    Translate one Protenix `<id>_scores.json` (+ optional `<id>_pae.npy`) into
    the RF3-shaped `(summary, conf)` dicts `score_one` already knows how to
    read, so the RMSD/epitope/clash logic and ipSAE never need a second
    implementation.

    Schema CONFIRMED against a real cluster-produced file (2026-08-23,
    g-groups/.../binder_pipeline gem_vegf_a calibration run): a flat dict —
    `plddt_mean`, `plddt_per_residue`, `ptm`, `iptm`, `pae_mean`, `runtime_s`,
    `iptm_per_chain_pair` (e.g. `{"A_B": 0.31}`). No `binder_ptm`/`target_ptm`,
    no `iface_pae`/`iface_pae_min`, no `has_clash`, no `ranking_score` —
    earlier revisions of this adapter guessed at those keys (this pipeline's
    own docs describe a narrower, differently-shaped score file than what it
    actually writes); they are computed here instead of guessed:
    `iface_pae` from the raw PAE matrix's binder-target block, the rest left
    absent exactly like an RF3 summary missing a field. `pae_mean` (whole-
    structure) is deliberately NOT used for `iface_pae` — it dilutes the
    interface signal with two mostly-unrelated intra-chain blocks.
    """
    def _iptm_pair(d: dict) -> float | None:
        ipc = d.get("iptm_per_chain_pair") or {}
        for key in ("A_B", "B_A"):
            if key in ipc:
                return ipc[key]
        return d.get("iptm")

    iface_pae = None
    if pae is not None and pae.shape[0] == n_binder + n_target and n_binder and n_target:
        block = np.asarray(pae, dtype=float)[:n_binder, n_binder:]
        iface_pae = float(block.mean())

    iptm = _iptm_pair(scores)
    summary = {
        "iptm": iptm,
        "ptm": scores.get("ptm"),
        "overall_plddt": scores.get("plddt_mean"),
        "chain_ptm": [None, None],
        "chain_pair_pae": [[None, iface_pae], [None, None]],
        "chain_pair_pae_min": [[None, None], [None, None]],
        "chain_pair_pde_min": [[None, None], [None, None]],
        "has_clash": None,
        "ranking_score": iptm,
    }
    conf = None
    if pae is not None:
        n = pae.shape[0]
        if n == n_binder + n_target:
            conf = {"pae": pae, "token_chain_ids": ["A"] * n_binder + ["B"] * n_target}
    return summary, conf


def score_one_protenix(
    name: str,
    pred_path: Path,
    design_path: Path,
    scores: dict,
    hotspots: Sequence[int],
    cfg: ScoreConfig = ScoreConfig(),
    pae: np.ndarray | None = None,
    sidecar: dict | None = None,
) -> dict:
    """
    Score one Protenix (or other folding-repo backend) refold.

    `n_binder`/`n_target` for the PAE chain boundary come from the PREDICTION
    structure itself (its own chain A / chain B CA counts) rather than a
    caller-supplied guess, so a design whose binder length varies design-to-
    design (RFD3 samples length per design) is always read correctly.
    """
    prd = read_structure(pred_path, plddt_scale=100.0)
    n_binder = ca(prd, cfg.binder_chain).array_length()
    n_target = ca(prd, cfg.target_chain).array_length()
    summary, conf = protenix_confidence_adapter(scores, n_binder, n_target, pae)
    return score_one(name, pred_path, design_path, summary, hotspots, cfg,
                     conf=conf, sidecar=sidecar, plddt_scale=100.0)


def iter_protenix_refolds(refold_dir: Path) -> Iterable[Path]:
    """
    Yield every `<id>_scores.json` under a Protenix (folding-repo) output tree.

    Per-design SUBDIRECTORY layout, same shape as RF3's own
    `<id>/<id>_summary_confidences.json` (see `iter_refolds`): `<id>/<id>.pdb`
    + `<id>/<id>_scores.json` [+ `<id>/<id>_pae.npy`] — NOT flat, despite an
    earlier secondhand read of that pipeline's own docs/scorer suggesting
    otherwise. Confirmed by direct inspection of a real cluster-produced
    campaign (g-groups/.../binder_pipeline/outputs/<run>/run_N/protenix/).
    `os.scandir` for the same reason as `iter_refolds` — a production-scale
    directory is large.
    """
    import os

    refold_dir = Path(refold_dir)
    if not refold_dir.is_dir():
        return
    with os.scandir(refold_dir) as top:
        for entry in top:
            if not entry.is_dir():
                continue
            cand = Path(entry.path) / f"{entry.name}_scores.json"
            if cand.exists():
                yield cand


def _score_task_protenix(args: tuple) -> dict:
    """Worker entry point — must be module-level and picklable."""
    (scores_path, design_dir, hotspots, cfg) = args
    scores_path = Path(scores_path)
    name = scores_path.name[: -len("_scores.json")]
    pred = scores_path.with_name(f"{name}.pdb")
    design = find_design(Path(design_dir), name)
    pae_path = scores_path.with_name(f"{name}_pae.npy")
    pae = np.load(pae_path) if pae_path.exists() else None
    sidecar_path = Path(design_dir) / f"{design_family(name)}.json"
    sidecar = _load_json(sidecar_path) if sidecar_path.exists() else None
    try:
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
        row = score_one_protenix(name, pred, design, scores, hotspots, cfg,
                                 pae=pae, sidecar=sidecar)
        row["error"] = ""
    except Exception as exc:
        row = {
            "name": name, "design_family": design_family(name),
            "refold_cif": str(pred), "design_cif": str(design),
            "error": f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:200],
        }
    return row


def score_campaign_protenix(
    refold_dir: Path,
    design_dir: Path,
    *,
    hotspots: Sequence[int],
    cfg: ScoreConfig = ScoreConfig(),
    workers: int = 1,
    limit: int = 0,
    progress_every: int = 2000,
) -> list[dict]:
    """Protenix-backend counterpart of `score_campaign`; same shape, same FIELDS."""
    files = sorted(iter_protenix_refolds(refold_dir))
    if limit:
        files = files[:limit]
    if not files:
        logger.warning(f"No Protenix outputs found under {refold_dir}")
        return []
    logger.info(f"Scoring {len(files):,} Protenix refolds | hotspots "
               f"{list(hotspots) or 'none'} | contact cutoff {cfg.contact_cutoff} A")

    tasks = [(str(s), str(design_dir), tuple(hotspots), cfg) for s in files]
    rows: list[dict] = []
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for i, row in enumerate(pool.map(_score_task_protenix, tasks, chunksize=32), 1):
                rows.append(row)
                if progress_every and i % progress_every == 0:
                    logger.info(f"  scored {i:,}/{len(tasks):,}")
    else:
        for i, task in enumerate(tasks, 1):
            rows.append(_score_task_protenix(task))
            if progress_every and i % progress_every == 0:
                logger.info(f"  scored {i:,}/{len(tasks):,}")

    n_err = sum(1 for r in rows if r.get("error"))
    logger.info(f"Scored {len(rows):,} Protenix refolds ({n_err} errors)")
    return rows


# ----------------------------------------------------------------------
# Campaign-level driver
# ----------------------------------------------------------------------

def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def iter_refolds(rf3_dir: Path) -> Iterable[Path]:
    """
    Yield every ``*_summary_confidences.json`` under an RF3 output tree.

    Uses os.scandir rather than glob: an RF3 production dir holds 50-100k
    subdirectories, where a shell glob is `Argument list too long` and
    Path.glob's sorted materialisation costs real time and memory.
    """
    import os

    rf3_dir = Path(rf3_dir)
    if not rf3_dir.is_dir():
        return
    with os.scandir(rf3_dir) as top:
        for entry in top:
            if not entry.is_dir():
                continue
            cand = Path(entry.path) / f"{entry.name}_summary_confidences.json"
            if cand.exists():
                yield cand


def _score_task(args: tuple) -> dict:
    """Worker entry point — must be module-level and picklable."""
    (summary_path, design_dir, hotspots, cfg, want_ipsae) = args
    summary_path = Path(summary_path)
    name = summary_path.name[: -len("_summary_confidences.json")]
    pred = summary_path.with_name(f"{name}_model.cif")
    design = find_design(Path(design_dir), name)
    conf = (_load_json(summary_path.with_name(f"{name}_confidences.json"))
            if want_ipsae else None)
    sidecar_path = Path(design_dir) / f"{design_family(name)}.json"
    sidecar = _load_json(sidecar_path) if sidecar_path.exists() else None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        row = score_one(name, pred, design, summary, hotspots, cfg,
                        conf=conf, sidecar=sidecar)
        row["error"] = ""
    except Exception as exc:
        row = {
            "name": name, "design_family": design_family(name),
            "refold_cif": str(pred), "design_cif": str(design),
            "error": f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:200],
        }
    return row


def score_campaign(
    rf3_dir: Path,
    design_dir: Path,
    *,
    hotspots: Sequence[int],
    cfg: ScoreConfig = ScoreConfig(),
    workers: int = 1,
    limit: int = 0,
    with_ipsae: bool = True,
    progress_every: int = 2000,
) -> list[dict]:
    """
    Score every refold under `rf3_dir` against its design in `design_dir`.

    Confidence JSONs are loaded lazily one at a time and discarded — a
    production campaign has ~48k of them and each PAE matrix is N^2 floats.
    """
    summaries = sorted(iter_refolds(rf3_dir))
    if limit:
        summaries = summaries[:limit]
    if not summaries:
        logger.warning(f"No RF3 outputs found under {rf3_dir}")
        return []
    logger.info(
        f"Scoring {len(summaries):,} refolds | hotspots {list(hotspots) or 'none'} "
        f"| contact cutoff {cfg.contact_cutoff} A | ipSAE "
        f"{'on (PAE < %.0f)' % cfg.ipsae_pae_cutoff if with_ipsae else 'off'}"
    )

    tasks = [(str(s), str(design_dir), tuple(hotspots), cfg, with_ipsae)
             for s in summaries]
    rows: list[dict] = []
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for i, row in enumerate(pool.map(_score_task, tasks, chunksize=32), 1):
                rows.append(row)
                if progress_every and i % progress_every == 0:
                    logger.info(f"  scored {i:,}/{len(tasks):,}")
    else:
        for i, task in enumerate(tasks, 1):
            rows.append(_score_task(task))
            if progress_every and i % progress_every == 0:
                logger.info(f"  scored {i:,}/{len(tasks):,}")

    n_err = sum(1 for r in rows if r.get("error"))
    logger.info(f"Scored {len(rows):,} refolds ({n_err} errors)")
    return rows


def write_scores(rows: list[dict], out_path: Path,
                 sort_by: str = "binder_rmsd_dock") -> Path:
    """Write scored rows to CSV in the canonical FIELDS order."""
    out_path = Path(out_path)
    desc = sort_by in ("iptm", "ptm", "plddt", "binder_ptm", "target_ptm",
                       "ranking_score", "epitope_jaccard", "epitope_recall",
                       "ipsae_min", "ipsae_max", "binder_plddt", "binder_tm")
    rows = sorted(
        rows,
        key=lambda r: (r.get(sort_by) is None,
                       -(r.get(sort_by) or 0) if desc else (r.get(sort_by) or 1e9)),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    logger.info(f"Wrote {len(rows):,} rows -> {out_path}")
    return out_path


def prune_confidences(rf3_dir: Path, keep_names: set[str]) -> int:
    """
    Delete ``*_confidences.json`` for refolds not in `keep_names`.

    The PAE matrices are the bulk of an RF3 output tree (~2.5 MB per design dir,
    ~120 GB for a full production campaign).  Run this only AFTER scoring —
    ipSAE cannot be recomputed once they are gone.
    """
    import os

    removed = 0
    for summary in iter_refolds(rf3_dir):
        name = summary.name[: -len("_summary_confidences.json")]
        if name in keep_names:
            continue
        conf = summary.with_name(f"{name}_confidences.json")
        try:
            if conf.exists():
                size = conf.stat().st_size
                os.remove(conf)
                removed += size
        except OSError as exc:
            logger.warning(f"could not prune {conf}: {exc}")
    logger.info(f"Pruned {removed / 1e9:.2f} GB of PAE matrices")
    return removed


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rf3_dir", type=Path, help="RF3 output directory (<id>/ per design).")
    ap.add_argument("design_dir", type=Path, help="RFD3 output directory.")
    ap.add_argument("-o", "--out", type=Path, default=Path("refold_scores.csv"))
    ap.add_argument("--binder-chain", default="A")
    ap.add_argument("--target-chain", default="B")
    ap.add_argument("--hotspots", default="",
                    help="Target residue ids in OUTPUT numbering, comma-separated.")
    ap.add_argument("--hotspots-from-rfd3", type=Path, default=None,
                    help="Read select_hotspots from an RFD3 design SIDECAR "
                         "(*_model_*.json) and remap through its "
                         "diffused_index_map. Never pass the input spec.")
    ap.add_argument("--contact-cutoff", type=float, default=8.0)
    ap.add_argument("--ipsae-pae-cutoff", type=float, default=IPSAE_PAE_CUTOFF)
    ap.add_argument("--no-ipsae", action="store_true",
                    help="Skip ipSAE (avoids loading the PAE matrices).")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sort-by", default="binder_rmsd_dock")
    args = ap.parse_args(argv)

    if args.hotspots_from_rfd3:
        hotspots = hotspots_from_rfd3(args.hotspots_from_rfd3, args.target_chain)
    else:
        hotspots = [int(h) for h in args.hotspots.replace(",", " ").split()]

    cfg = ScoreConfig(binder_chain=args.binder_chain, target_chain=args.target_chain,
                      contact_cutoff=args.contact_cutoff,
                      ipsae_pae_cutoff=args.ipsae_pae_cutoff)
    rows = score_campaign(args.rf3_dir, args.design_dir, hotspots=hotspots, cfg=cfg,
                          workers=args.workers, limit=args.limit,
                          with_ipsae=not args.no_ipsae)
    if not rows:
        return 1
    write_scores(rows, args.out, sort_by=args.sort_by)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
