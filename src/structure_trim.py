"""
Domain-aware truncation of a design target.

RFD3 and RF3 scale with target size — RF3 attention is O(N^2) in tokens — so a
multi-domain target has to be cropped before a campaign is affordable. Cropping
naively splits domains, exposes hydrophobic cores, and produces a target that
folds differently from the real protein. This module cuts on domain boundaries
instead, keeps every hotspot, and verifies the interface survived.

## Where domain boundaries come from

**Foldseek cannot do this.** It is a structural *search* engine — it answers
"what does this look like", never "where does this domain end". Its real jobs
here are the post-trim sanity check (:func:`verify_fold_retained`) and, given a
downloaded classified database, transferring boundaries from a homolog.

The resolution order, cheapest first:

1. **RCSB-served CATH / SCOP2 / ECOD** — curated, authoritative, free, and it
   reuses the GraphQL plumbing already in the repo. Hits for most deposited
   structures of a studied target.
2. **Chainsaw** (Wells/Bordin/Orengo, *Bioinformatics* 2024) — a CNN parser for
   unclassified entries and predicted models; matches CATH boundaries on 78% of
   domains, ahead of Merizo/UniDoc/PUU/SWORD2. Optional, run as a subprocess in
   its own env (it pins torch), exactly like `src/pyrosetta_sasa.py`.
3. **Geometric partition** — always available: a CA contact graph cut by
   spectral bisection, recursed while a part exceeds the budget. Pure
   numpy/scipy.

Every cut is then snapped to a secondary-structure boundary, kept away from the
hotspots, and checked for connectivity — a domain assignment alone will happily
slice a helix that straddles two domains.

## Numbering

The trimmed structure **preserves the original ``auth_seq_id``**. The hotspot
ids parsed out of ``### MODEL-READY HOTSPOTS`` therefore survive verbatim into
the RFD3 ``select_hotspots`` keys, and `trim_map.json` records the identity
mapping explicitly so downstream code never has to assume it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from loguru import logger

_ROOT = Path(__file__).resolve().parents[1]

# Cut points are snapped away from helices/strands, but only this far — beyond
# it we are no longer trimming where the domain assignment said to.
MAX_SSE_SNAP = 8
# Never cut within this radius of a hotspot heavy atom.
HOTSPOT_SHELL_A = 10.0
# CA-CA distance defining the residue contact graph.
CONTACT_A = 8.0
# Terminal runs shorter than this are not worth trimming separately.
MIN_DANGLE = 5
# A geometric partition below this is not a domain.
MIN_DOMAIN_RESIDUES = 40
# ...but a terminal tail is not a domain either, and is routinely much shorter.
# Requiring both halves of a split to clear MIN_DOMAIN_RESIDUES would make a
# 36-residue TM/cytoplasmic tail unsheddable.
MIN_SPLIT_TAIL = 15
# Segments shorter than this are noise, not structure: a two-residue island
# contributes nothing a binder can engage but does cost RFD3 a chain break.
MIN_SEGMENT = 6
# Sequence gaps at most this long between kept segments are filled in rather
# than left as chain breaks — a 3-residue hole is a worse target than no hole.
BRIDGE_GAP = 12


class TrimError(RuntimeError):
    """The trim could not be produced, or produced something unusable."""


class TrimBudgetError(TrimError):
    """The hotspots alone do not fit the residue budget."""


@dataclass
class Domain:
    index: int
    start_auth: int
    end_auth: int
    n_residues: int
    source: str            # cath | scop | ecod | chainsaw | geometric
    label: str = ""
    # Residues in this domain that survive the topology restriction.
    n_designable: int = 0
    n_hotspots: int = 0
    bsa_contributed_A2: float = 0.0

    def contains(self, auth: int) -> bool:
        return self.start_auth <= auth <= self.end_auth


@dataclass
class TrimResult:
    trimmed_path: Path
    mapping_path: Path
    kept_segments: list[tuple[int, int]]
    n_segments: int
    contig: str
    n_residues_before: int
    n_residues_after: int
    domains: list[Domain]
    method: str
    hotspots_retained: list[dict]
    hotspots_lost: list[dict]
    # NOTE the two bases. `interface_bsa_before_A2` is the whole interface
    # (BOTH chains) — the conventional "how big is this interface" number.
    # `interface_bsa_target_side_A2` counts only the chain being trimmed, and is
    # the one comparable with `bsa_dropped_A2` / `bsa_retention`. Mixing them is
    # what made the drop warning fire on every trim.
    interface_bsa_before_A2: float = 0.0
    interface_bsa_target_side_A2: float = 0.0
    interface_bsa_after_A2: float = 0.0
    # Fraction of the interface area of the RETAINED residues that survives the
    # trim. Deliberately not "fraction of the whole native interface" — see
    # trim_target.
    bsa_retention: float = 1.0
    # Interface area carried by residues the trim removed, reported so a second
    # interface being discarded on purpose is visible rather than silent.
    bsa_dropped_A2: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hotspots_lost


# ----------------------------------------------------------------------
# Residue inventory
# ----------------------------------------------------------------------

def _model(path: Path):
    import gemmi

    st = gemmi.read_structure(str(path))
    st.setup_entities()
    st.remove_alternative_conformations()
    st.remove_hydrogens()
    return st


def chain_residues(path: Path, chain: str) -> list[dict]:
    """
    Ordered polymer residues of `chain`: auth_seq_id, name and CA coordinates.

    Residues without a CA (waters, ligands, badly disordered) are dropped — every
    downstream step is CA-based and a None coordinate would poison it.
    """
    import gemmi

    st = _model(path)
    ch = None
    for model in st:
        for c in model:
            if c.name == chain:
                ch = c
                break
        if ch is not None:
            break
    if ch is None:
        raise TrimError(f"chain {chain!r} not found in {path}")

    from src.structure_tools import is_chain_residue

    out: list[dict] = []
    for res in ch:
        # Backbone-based, not name-based. gemmi's chemical-component table does
        # not know every modification a depositor may make — 3KYS A344 is P1L,
        # S-palmitoyl-cysteine, reported as kind=UNKNOWN — and skipping it here
        # both drops the modification and splits the chain into an extra
        # segment, which then costs a chain break downstream.
        if not is_chain_residue(res):
            continue
        ca = res.find_atom("CA", "*")
        if ca is None:
            continue
        out.append({
            "auth": int(res.seqid.num),
            "icode": (res.seqid.icode or " ").strip(),
            "name": res.name,
            "ca": np.array([ca.pos.x, ca.pos.y, ca.pos.z]),
        })
    if not out:
        raise TrimError(f"chain {chain!r} in {path} has no amino-acid residues with CA")

    # Insertion codes break the one-residue-per-number assumption that author
    # numbering, RFD3 contigs (`A12-98`) and hotspot ids all rest on. Antibody
    # chains numbered by Kabat/Chothia are the common case. Refuse rather than
    # emit a contig with repeated residue numbers, which RFD3 accepts and
    # silently mis-models.
    inserted = sorted({r["auth"] for r in out if r["icode"]})
    if inserted:
        raise TrimError(
            f"chain {chain!r} in {Path(path).name} uses insertion codes at "
            f"residues {inserted[:8]}{'...' if len(inserted) > 8 else ''} "
            f"({len(inserted)} positions). Author numbering is not unique, so an "
            f"RFD3 contig cannot address these residues. Renumber the chain "
            f"sequentially before designing against it."
        )
    dupes = len(out) - len({r["auth"] for r in out})
    if dupes:
        raise TrimError(
            f"chain {chain!r} in {Path(path).name} has {dupes} duplicate residue "
            f"number(s) with no insertion code — the file is inconsistent and "
            f"cannot be addressed by author numbering.")
    return out


def _segments(auths: Sequence[int]) -> list[tuple[int, int]]:
    """Collapse a residue-id list into contiguous [start, end] spans."""
    auths = sorted(set(int(a) for a in auths))
    if not auths:
        return []
    out, start, prev = [], auths[0], auths[0]
    for a in auths[1:]:
        if a != prev + 1:
            out.append((start, prev))
            start = a
        prev = a
    out.append((start, prev))
    return out


# ----------------------------------------------------------------------
# Tier 1 — RCSB CATH / SCOP2 / ECOD
# ----------------------------------------------------------------------

_RCSB_GRAPHQL = "https://data.rcsb.org/graphql"
_DOMAIN_FEATURE_TYPES = ("CATH", "SCOP2", "SCOP", "ECOD")

_DOMAIN_QUERY = """{
  entry(entry_id:"%s") {
    polymer_entities {
      polymer_entity_instances {
        rcsb_polymer_entity_instance_container_identifiers { auth_asym_id }
        rcsb_polymer_instance_feature {
          type name feature_positions { beg_seq_id end_seq_id }
        }
      }
    }
  }
}"""


def _nearest_observed(label: int, label_to_auth: dict[int, int],
                      direction: int, max_walk: int = 200) -> int | None:
    """
    Author id for a label position, walking `direction` until one is observed.

    Domain boundaries are annotated on the entity sequence; unobserved termini
    mean the endpoint often has no coordinates.
    """
    if label in label_to_auth:
        return label_to_auth[label]
    if not label_to_auth:
        return None
    lo, hi = min(label_to_auth), max(label_to_auth)
    cur = min(max(label, lo), hi)
    for _ in range(max_walk):
        if cur in label_to_auth:
            return label_to_auth[cur]
        cur += direction
        if cur < lo or cur > hi:
            return None
    return None


def rcsb_domains(pdb_id: str, chain: str, auth_to_label: dict[int, int],
                 timeout: float = 30.0) -> list[Domain]:
    """
    Domain spans from RCSB's CATH / SCOP2 / ECOD instance features.

    Feature positions are ``label_seq_id``; they are converted back to author
    numbering through the map from :func:`src.structure_tools.get_sequence_map`,
    because everything else in this pipeline (hotspots, RFD3 contigs) is author
    numbered.
    """
    import requests

    label_to_auth = {v: k for k, v in auth_to_label.items()}
    try:
        resp = requests.post(_RCSB_GRAPHQL,
                             json={"query": _DOMAIN_QUERY % pdb_id.upper()},
                             timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(f"RCSB domain lookup failed for {pdb_id}: {exc}")
        return []
    if payload.get("errors"):
        logger.warning(f"RCSB domain lookup errors for {pdb_id}: {payload['errors'][:1]}")
        return []

    entry = (payload.get("data") or {}).get("entry") or {}
    best: list[Domain] = []
    for source in _DOMAIN_FEATURE_TYPES:            # preference order
        spans: list[tuple[int, int, str]] = []
        for pe in entry.get("polymer_entities") or []:
            for inst in pe.get("polymer_entity_instances") or []:
                ids = inst.get(
                    "rcsb_polymer_entity_instance_container_identifiers") or {}
                if ids.get("auth_asym_id") != chain:
                    continue
                for feat in inst.get("rcsb_polymer_instance_feature") or []:
                    if feat.get("type") != source:
                        continue
                    for pos in feat.get("feature_positions") or []:
                        beg, end = pos.get("beg_seq_id"), pos.get("end_seq_id")
                        if beg is None or end is None:
                            continue
                        # Domain ranges are given on the full ENTITY sequence, so
                        # they routinely run past the observed residues (missing
                        # termini, disordered loops). Clamp to what is actually in
                        # the model — dropping the span instead silently discards
                        # the annotation and falls through to a worse tier.
                        a_beg = _nearest_observed(beg, label_to_auth, +1)
                        a_end = _nearest_observed(end, label_to_auth, -1)
                        if a_beg is None or a_end is None or a_beg > a_end:
                            continue
                        spans.append((a_beg, a_end, feat.get("name") or source))
        if spans:
            spans.sort()
            best = [
                Domain(index=i, start_auth=b, end_auth=e, n_residues=e - b + 1,
                       source=source.lower(), label=str(name))
                for i, (b, e, name) in enumerate(spans)
            ]
            logger.info(
                f"{pdb_id} chain {chain}: {len(best)} {source} domain(s) "
                + ", ".join(f"{d.start_auth}-{d.end_auth}" for d in best)
            )
            break
    return best


# ----------------------------------------------------------------------
# Tier 2 — Chainsaw
# ----------------------------------------------------------------------

def chainsaw_domains(path: Path, chain: str, chainsaw_cmd: Sequence[str] | None,
                     timeout: float = 300.0) -> list[Domain]:
    """
    Domain spans from Chainsaw, run as a subprocess in its own environment.

    Chainsaw pins torch, so it must not be installed into LPT's venv; the
    separate-interpreter pattern here mirrors `src/pyrosetta_sasa.py`. Returns
    [] (never raises) when Chainsaw is not configured or fails — it is one tier
    of a fallback chain, not a hard dependency.

    Its `chopping` output looks like ``12-98_150-190,200-260``: comma between
    domains, underscore between discontiguous segments of one domain.
    """
    if not chainsaw_cmd:
        return []
    exe = shutil.which(chainsaw_cmd[0]) or (
        chainsaw_cmd[0] if Path(chainsaw_cmd[0]).exists() else None)
    if exe is None:
        logger.warning(f"Chainsaw not found at {chainsaw_cmd[0]!r}; skipping tier 2")
        return []

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "chainsaw.tsv"
        cmd = [*chainsaw_cmd, "--structure_file", str(path), "--output", str(out)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"Chainsaw failed to run: {exc}")
            return []
        if proc.returncode != 0 or not out.exists():
            logger.warning(
                f"Chainsaw exited {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}")
            return []
        chopping = ""
        for line in out.read_text(encoding="utf-8").splitlines():
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 4 and not line.startswith("chain_id"):
                chopping = fields[-2] if fields[-1].isdigit() else fields[-1]
                break
    if not chopping or chopping in ("NULL", "-"):
        logger.info("Chainsaw returned no domains")
        return []

    domains: list[Domain] = []
    for i, dom in enumerate(chopping.split(",")):
        bounds = []
        for seg in dom.split("_"):
            if "-" not in seg:
                continue
            a, b = seg.split("-")[:2]
            try:
                bounds.append((int(a), int(b)))
            except ValueError:
                continue
        if not bounds:
            continue
        # A discontiguous Chainsaw domain is represented by its span; the
        # connectivity filter later drops anything that is not actually joined.
        start = min(b[0] for b in bounds)
        end = max(b[1] for b in bounds)
        domains.append(Domain(index=i, start_auth=start, end_auth=end,
                              n_residues=end - start + 1, source="chainsaw",
                              label=dom))
    if domains:
        logger.info(f"Chainsaw: {len(domains)} domain(s) — {chopping}")
    return domains


# ----------------------------------------------------------------------
# Tier 3 — geometric partition
# ----------------------------------------------------------------------

def geometric_domains(residues: Sequence[dict], budget: int,
                      min_size: int = MIN_DOMAIN_RESIDUES) -> list[Domain]:
    """
    Recursive spectral bisection of the CA contact graph.

    The always-available fallback. Cuts where the contact graph is weakest, which
    for a real multi-domain chain is the inter-domain linker. Recursion stops
    when a part fits the budget or would split below `min_size`.
    """
    from scipy.sparse.csgraph import laplacian

    coords = np.array([r["ca"] for r in residues])
    auths = [r["auth"] for r in residues]
    d = np.linalg.norm(coords[:, None, :] - coords[None, :, :], axis=-1)
    adj = (d < CONTACT_A).astype(float)
    np.fill_diagonal(adj, 0.0)

    def split(idx: np.ndarray) -> list[np.ndarray]:
        # Stop once a part fits the budget, or is too small to halve without
        # producing something below min_size (which would not be a domain).
        if idx.size <= budget or idx.size < min_size + MIN_SPLIT_TAIL:
            return [idx]
        sub = adj[np.ix_(idx, idx)]
        if sub.sum() == 0:
            return [idx]
        try:
            lap = laplacian(sub, normed=True)
            vals, vecs = np.linalg.eigh(lap)
            fiedler = vecs[:, np.argsort(vals)[1]]
        except np.linalg.LinAlgError:
            return [idx]
        # Contiguous split: the linker is a position along the chain, not an
        # arbitrary bipartition, so cut at the sign change of the Fiedler vector.
        sign = fiedler >= 0
        changes = np.flatnonzero(sign[:-1] != sign[1:]) + 1
        valid = [c for c in changes
                 if min(c, idx.size - c) >= MIN_SPLIT_TAIL
                 and max(c, idx.size - c) >= min_size]
        if not valid:
            return [idx]
        # Prefer the cut that leaves the largest part closest to the budget,
        # rather than the most balanced one — a domain plus a tail is the common
        # shape here, and halving it would be wrong.
        cut = min(valid, key=lambda c: abs(max(c, idx.size - c) - budget))
        return split(idx[:cut]) + split(idx[cut:])

    parts = split(np.arange(len(residues)))
    domains = []
    for i, part in enumerate(sorted(parts, key=lambda p: p[0])):
        domains.append(Domain(
            index=i, start_auth=auths[part[0]], end_auth=auths[part[-1]],
            n_residues=part.size, source="geometric",
            label=f"contact-graph part {i + 1}"))
    logger.info(
        f"geometric partition: {len(domains)} part(s) — "
        + ", ".join(f"{d.start_auth}-{d.end_auth}" for d in domains))
    return domains


def segment_domains(
    structure_path: Path,
    chain: str,
    *,
    budget: int,
    pdb_id: str | None = None,
    auth_to_label: dict[int, int] | None = None,
    method: str = "auto",
    chainsaw_cmd: Sequence[str] | None = None,
    residues: Sequence[dict] | None = None,
) -> tuple[list[Domain], str]:
    """
    Domain spans for one chain, trying each source in turn.

    Returns (domains, method_used). Never raises for a missing optional tool —
    an absent Chainsaw or an RCSB timeout falls through to the next tier and is
    logged.
    """
    residues = residues or chain_residues(structure_path, chain)

    if method in ("auto", "rcsb") and pdb_id and auth_to_label:
        doms = rcsb_domains(pdb_id, chain, auth_to_label)
        if doms:
            return doms, doms[0].source
        if method == "rcsb":
            raise TrimError(f"no RCSB domain annotation for {pdb_id} chain {chain}")

    if method in ("auto", "chainsaw"):
        doms = chainsaw_domains(structure_path, chain, chainsaw_cmd)
        if doms:
            return doms, "chainsaw"
        if method == "chainsaw":
            raise TrimError("Chainsaw produced no domains")

    return geometric_domains(residues, budget), "geometric"


# ----------------------------------------------------------------------
# Secondary structure
# ----------------------------------------------------------------------

def sse_by_residue(structure_path: Path, chain: str) -> dict[int, str]:
    """
    Per-residue secondary structure: auth_seq_id -> 'a' | 'b' | 'c'.

    biotite's P-SEA implementation, which needs only CA coordinates — so it
    works on trimmed fragments and predicted models alike, and avoids a DSSP
    dependency. Returns {} on failure; the caller then simply does not snap,
    which is a worse cut, not a wrong one.
    """
    try:
        import biotite.structure as struc
        from biotite.structure.io.pdbx import CIFFile, get_structure

        path = Path(structure_path)
        if path.suffix.lower() in (".pdb", ".ent"):
            from biotite.structure.io.pdb import PDBFile
            atoms = PDBFile.read(str(path)).get_structure(model=1)
        else:
            atoms = get_structure(CIFFile.read(str(path)), model=1)
        atoms = atoms[(atoms.chain_id == chain) & struc.filter_amino_acids(atoms)]
        atoms = atoms[~np.isnan(atoms.coord).any(axis=1)]
        if atoms.array_length() == 0:
            return {}
        sse = struc.annotate_sse(atoms)
        res_ids = np.unique(atoms.res_id)
        if len(sse) != len(res_ids):
            return {}
        return {int(r): str(s) for r, s in zip(res_ids, sse)}
    except Exception as exc:
        logger.warning(f"secondary-structure annotation unavailable: {exc}")
        return {}


def _snap_to_coil(auth: int, sse: dict[int, str], residues_by_auth: dict[int, dict],
                  direction: int, max_snap: int = MAX_SSE_SNAP) -> tuple[int, bool]:
    """
    Walk a cut point outward until it leaves a helix or strand.

    Cutting mid-helix leaves a frayed end that refolds differently from the real
    protein. `direction` is -1 to walk toward the N-terminus (for a start cut)
    and +1 toward the C-terminus (for an end cut). Returns (auth, moved).
    """
    if not sse or sse.get(auth, "c") == "c":
        return auth, False
    cur = auth
    for _ in range(max_snap):
        nxt = cur + direction
        if nxt not in residues_by_auth:
            return cur, cur != auth
        cur = nxt
        if sse.get(cur, "c") == "c":
            return cur, True
    return auth, False          # refuse to walk further; keep the raw boundary


# ----------------------------------------------------------------------
# Trimming
# ----------------------------------------------------------------------

def _hotspot_auths(hotspots: Sequence[dict]) -> list[int]:
    out = []
    for h in hotspots:
        v = h.get("auth_seq_id") if isinstance(h, dict) else h
        if v is not None:
            out.append(int(v))
    return out


def plan_trim(
    residues: Sequence[dict],
    domains: Sequence[Domain],
    hotspots: Sequence[dict],
    budget: int,
    *,
    per_residue_bsa: dict[int, float] | None = None,
    sse: dict[int, str] | None = None,
    allowed_auth: set[int] | None = None,
) -> tuple[list[int], list[str]]:
    """
    Choose which residues to keep. Returns (kept auth ids, warnings).

    1. Score each domain by hotspot count and interface BSA contributed.
    2. Take the minimal domain set covering EVERY hotspot; if that alone busts
       the budget, raise rather than silently drop a hotspot.
    3. Accrete further domains by descending BSA while within budget.
    4. Prefer a contiguous answer: a multi-segment target pushes RFD3's
       n_chainbreaks above 1 and breaks the prefilter downstream.
    """
    warnings: list[str] = []
    per_residue_bsa = per_residue_bsa or {}
    by_auth = {r["auth"]: r for r in residues}
    hot = _hotspot_auths(hotspots)

    def designable(auths: Iterable[int]) -> list[int]:
        """Drop residues the topology restriction excludes."""
        if not allowed_auth:
            return sorted(set(auths))
        return sorted(a for a in set(auths) if a in allowed_auth)

    for d in domains:
        d.n_hotspots = sum(1 for h in hot if d.contains(h))
        d.bsa_contributed_A2 = sum(
            v for a, v in per_residue_bsa.items()
            if d.contains(a) and (not allowed_auth or a in allowed_auth))
        d.n_designable = len(designable(
            r["auth"] for r in residues if d.contains(r["auth"])))

    orphan = [h for h in hot if not any(d.contains(h) for d in domains)]
    if orphan:
        warnings.append(
            f"hotspots {orphan} fall outside every domain span; "
            f"their residues are force-kept")

    core = [d for d in domains if d.n_hotspots > 0]
    core_size = sum(d.n_residues for d in core)
    if core_size > budget and len(domains) == 1:
        # Segmentation found no boundary at all, so "the domain" is just the
        # whole chain and the budget error would be misleading. Fall back to a
        # crop centred on the epitope in 3D — the honest "no domains found, keep
        # what packs against the hotspots" answer.
        warnings.append(
            f"no domain boundary was found in {domains[0].n_residues} residues; "
            f"cropped around the hotspots in 3D instead of on a domain edge")
        auths = _hotspot_centred_crop(residues, hot, budget)
        core, core_size = [], len(auths)
    else:
        auths = None
    if core_size > budget:
        raise TrimBudgetError(
            f"the domains carrying the hotspots are {core_size} residues, over the "
            f"{budget}-residue budget. Raise design.foundry.target_residue_budget, "
            f"or pick an interface on a smaller domain — dropping a hotspot to fit "
            f"would silently change what is being designed against."
        )

    kept_idx = {id(d) for d in core}
    _ = kept_idx
    total = core_size
    for d in sorted((d for d in domains if id(d) not in kept_idx),
                    key=lambda x: -x.bsa_contributed_A2):
        if d.bsa_contributed_A2 <= 0:
            continue
        if total + d.n_residues <= budget:
            kept_idx.add(id(d))
            total += d.n_residues
    # Spend any budget left over on domains adjacent to what we already keep.
    # Preferring sequence-adjacent ones keeps the target contiguous; a chain
    # break costs more downstream than a slightly larger target.
    if core:
        core_lo = min(d.start_auth for d in core)
        core_hi = max(d.end_auth for d in core)
        for d in sorted((d for d in domains if id(d) not in kept_idx),
                        key=lambda x: min(abs(x.end_auth - core_lo),
                                          abs(x.start_auth - core_hi))):
            if total + d.n_residues > budget:
                continue
            kept_idx.add(id(d))
            total += d.n_residues

    kept = [d for d in domains if id(d) in kept_idx]

    if auths is None:
        auths = sorted({r["auth"] for r in residues
                        if any(d.contains(r["auth"]) for d in kept)} | set(orphan))
    else:
        auths = sorted(set(auths) | set(orphan))

    # Now that the domains are known, drop what topology forbids.
    before = len(auths)
    auths = designable(auths)
    if len(auths) < before:
        warnings.append(
            f"dropped {before - len(auths)} residue(s) outside the designable "
            f"side of the membrane (transmembrane and inward-facing residues "
            f"are never part of the target)")
    if not auths:
        raise TrimError(
            "no designable residues remain after the topology restriction")

    # Snap every cut off a helix/strand.
    sse = sse or {}
    snapped: set[int] = set(auths)
    for lo, hi in _segments(sorted(snapped)):
        new_lo, moved_lo = _snap_to_coil(lo, sse, by_auth, -1)
        new_hi, moved_hi = _snap_to_coil(hi, sse, by_auth, +1)
        if moved_lo:
            snapped |= {a for a in by_auth if new_lo <= a < lo}
        if moved_hi:
            snapped |= {a for a in by_auth if hi < a <= new_hi}
    auths = sorted(snapped)

    # Never cut inside the hotspot shell.
    hot_coords = [by_auth[h]["ca"] for h in hot if h in by_auth]
    if hot_coords:
        shell = {r["auth"] for r in residues
                 if min(float(np.linalg.norm(r["ca"] - c)) for c in hot_coords)
                 <= HOTSPOT_SHELL_A}
        auths = designable(set(auths) | shell)

    # Bridge short sequence gaps, then discard islands. The hotspot shell adds
    # residues that are close in 3D but scattered in sequence; left alone they
    # become one-residue "segments", each of which is a chain break RFD3 has to
    # model and the prefilter then rejects.
    auths = designable(_bridge_gaps(auths, by_auth, budget))
    auths = _drop_islands(auths, hot, warnings)

    # Drop unattached terminal runs.
    auths = _drop_dangles(auths, by_auth, hot)

    # Keep only the contact-graph component holding the hotspots.
    auths = _largest_component(auths, by_auth, hot, warnings)

    # Prefer one contiguous span, decided LAST so nothing above re-fragments it.
    # Every extra segment is an RFD3 chain break, and the sidecar prefilter
    # rejects designs whose n_chainbreaks exceeds the target's segment count.
    designable_residues = ([r for r in residues if r["auth"] in allowed_auth]
                           if allowed_auth else residues)
    auths = _prefer_contiguous(auths, designable_residues, hot, budget, warnings)

    if len(auths) > budget:
        warnings.append(
            f"after boundary refinement the trim is {len(auths)} residues, over "
            f"the {budget} budget — the extra residues are hotspot shell or SSE "
            f"integrity and were not dropped")
    return auths, warnings


def _hotspot_centred_crop(residues: Sequence[dict], hot: Sequence[int],
                          budget: int) -> list[int]:
    """
    Keep the `budget` residues packing closest to the hotspots, in 3D.

    Used only when domain segmentation found no boundary. Ranking by distance to
    the hotspot centroid rather than by sequence position is what lets this drop
    a membrane-proximal tail that sits far from the epitope but is sequence-
    adjacent to it.
    """
    if not hot:
        return [r["auth"] for r in residues][:budget]
    by_auth = {r["auth"]: r for r in residues}
    anchors = np.array([by_auth[h]["ca"] for h in hot if h in by_auth])
    scored = []
    for r in residues:
        d = float(np.min(np.linalg.norm(anchors - r["ca"], axis=1)))
        scored.append((d, r["auth"]))
    scored.sort()
    return sorted(a for _, a in scored[:budget])


def _prefer_contiguous(auths: Sequence[int], residues: Sequence[dict],
                       hot: Sequence[int], budget: int,
                       warnings: list[str]) -> list[int]:
    """
    Collapse a fragmented selection to the best single span, when one exists.

    Slides a window of at most `budget` residues over the chain and takes the one
    holding every hotspot and the most already-selected residues. Falls back to
    the fragmented selection if no window can hold all the hotspots — losing a
    hotspot is never worth a tidier contig.
    """
    segs = _segments(list(auths))
    if len(segs) <= 1 or not hot:
        return list(auths)

    order = [r["auth"] for r in residues]
    pos = {a: i for i, a in enumerate(order)}
    if not all(h in pos for h in hot):
        return list(auths)
    lo_i, hi_i = min(pos[h] for h in hot), max(pos[h] for h in hot)
    if hi_i - lo_i + 1 > budget:
        warnings.append(
            f"the hotspots span {hi_i - lo_i + 1} residues in sequence, more than "
            f"the {budget}-residue budget, so the target stays fragmented "
            f"({len(segs)} segments)")
        return list(auths)

    selected = set(auths)
    best, best_score = None, -1
    for start in range(0, len(order)):
        end = start + budget
        if start > lo_i:
            break
        if end <= hi_i:
            continue
        window = order[start:end]
        score = sum(1 for a in window if a in selected)
        if score > best_score:
            best, best_score = window, score
    if best is None:
        return list(auths)
    dropped = len(selected - set(best))
    if dropped:
        warnings.append(
            f"collapsed {len(segs)} segments into one span "
            f"{best[0]}-{best[-1]}, dropping {dropped} residue(s) that sat outside "
            f"it: each extra segment is an RFD3 chain break")
    return sorted(best)


def _bridge_gaps(auths: Sequence[int], by_auth: dict[int, dict],
                 budget: int, max_gap: int = BRIDGE_GAP) -> list[int]:
    """Fill sequence gaps shorter than `max_gap`, budget permitting."""
    kept = set(auths)
    segs = _segments(sorted(kept))
    for (_, end), (start, _) in zip(segs, segs[1:]):
        gap = [a for a in range(end + 1, start) if a in by_auth]
        if 0 < len(gap) <= max_gap and len(kept) + len(gap) <= budget:
            kept |= set(gap)
    return sorted(kept)


def _drop_islands(auths: Sequence[int], hot: Sequence[int],
                  warnings: list[str], min_len: int = MIN_SEGMENT) -> list[int]:
    """Remove segments too short to be worth a chain break, unless they hold a hotspot."""
    kept, dropped = [], 0
    for lo, hi in _segments(list(auths)):
        span = list(range(lo, hi + 1))
        if (hi - lo + 1) >= min_len or any(h in span for h in hot):
            kept.extend(a for a in auths if lo <= a <= hi)
        else:
            dropped += 1
    if dropped:
        warnings.append(
            f"dropped {dropped} fragment(s) shorter than {min_len} residues; each "
            f"would have cost a chain break for almost no interface")
    return sorted(kept)


def _drop_dangles(auths: Sequence[int], by_auth: dict[int, dict],
                  hot: Sequence[int]) -> list[int]:
    """Remove leading/trailing runs with no contact to the retained core."""
    segs = _segments(list(auths))
    if len(segs) <= 1 and len(auths) < MIN_DANGLE * 2:
        return list(auths)
    keep = set(auths)
    core = np.array([by_auth[a]["ca"] for a in auths if a in by_auth])
    for lo, hi in segs:
        run = [a for a in range(lo, hi + 1) if a in keep]
        if len(run) >= MIN_DANGLE and any(h in run for h in hot):
            continue
        for end in (run[:MIN_DANGLE], run[-MIN_DANGLE:]):
            if not end or any(h in end for h in hot):
                continue
            others = np.array([by_auth[a]["ca"] for a in keep
                               if a in by_auth and a not in end])
            if others.size == 0:
                continue
            pts = np.array([by_auth[a]["ca"] for a in end if a in by_auth])
            if pts.size == 0:
                continue
            d = np.linalg.norm(pts[:, None, :] - others[None, :, :], axis=-1)
            if d.min() > CONTACT_A:
                keep -= set(end)
    return sorted(keep)


def _largest_component(auths: Sequence[int], by_auth: dict[int, dict],
                       hot: Sequence[int], warnings: list[str]) -> list[int]:
    """
    Keep only residues connected to the hotspots through the contact graph.

    Kills floating crystallographic fragments that a span-based domain
    definition happily includes.
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    idx = [a for a in auths if a in by_auth]
    if len(idx) < 2:
        return list(auths)
    coords = np.array([by_auth[a]["ca"] for a in idx])
    d = np.linalg.norm(coords[:, None, :] - coords[None, :, :], axis=-1)
    adj = csr_matrix((d < CONTACT_A).astype(int))
    n, labels = connected_components(adj, directed=False)
    if n == 1:
        return list(auths)
    hot_labels = {labels[idx.index(h)] for h in hot if h in idx}
    if not hot_labels:
        sizes = np.bincount(labels)
        hot_labels = {int(np.argmax(sizes))}
    kept = [a for a, lab in zip(idx, labels) if lab in hot_labels]
    warnings.append(
        f"dropped {len(idx) - len(kept)} residue(s) in {n - len(hot_labels)} "
        f"disconnected fragment(s) not joined to the hotspot region")
    return sorted(kept)


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------

def write_trimmed(
    structure_path: Path,
    out_path: Path,
    keep: dict[str, Sequence[int] | None],
) -> Path:
    """
    Write a structure containing only the requested chains and residues.

    `keep` maps chain -> auth ids to retain, or None to keep the whole chain.
    **Author numbering is preserved**, so hotspot ids stay valid; gemmi is used
    rather than biotite because it round-trips mmCIF entity/label bookkeeping
    that RFD3's parser relies on.
    """
    import gemmi

    from src.structure_tools import is_chain_residue, is_solvent_or_additive

    src = _model(structure_path)
    dropped: dict[str, int] = {}
    wanted = {c: (None if v is None else set(int(x) for x in v))
              for c, v in keep.items()}

    # Build a fresh structure rather than mutating in place: gemmi.Chain has no
    # remove_residue, and deleting by index while iterating is how you silently
    # drop the wrong residues.
    st = gemmi.Structure()
    st.name = src.name
    st.spacegroup_hm = src.spacegroup_hm
    st.cell = src.cell
    model_out = gemmi.Model("1")
    for ch in src[0]:
        if ch.name not in wanted:
            continue
        allowed = wanted[ch.name]
        ch_out = gemmi.Chain(ch.name)
        for res in ch:
            if allowed is not None and int(res.seqid.num) not in allowed:
                continue
            # Backbone-based, not name-based: a modified residue gemmi's table
            # does not know (P1L, and anything else a depositor invents) is
            # still chain, and deleting it opens a spurious segment break.
            if allowed is not None and not is_chain_residue(res):
                continue
            # Applies to every kept chain, not just the trimmed one: waters and
            # cryoprotectant have no business in the structure RFD3 conditions
            # on or that the interface is measured from. is_solvent_or_additive
            # checks the polypeptide first, so a modified residue is never hit.
            if is_solvent_or_additive(res.name):
                dropped[res.name] = dropped.get(res.name, 0) + 1
                continue
            ch_out.add_residue(res)
        if len(ch_out):
            model_out.add_chain(ch_out)
    st.add_model(model_out)
    st.setup_entities()
    if dropped:
        logger.info(
            f"dropped {sum(dropped.values())} solvent/additive residues "
            f"({', '.join(f'{k}x{v}' for k, v in sorted(dropped.items()))})")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".pdb":
        st.write_pdb(str(out_path))
    else:
        st.make_mmcif_document().write_file(str(out_path))
    return out_path


def build_contig(segments: Sequence[tuple[int, int]], chain: str,
                 binder_min: int, binder_max: int) -> str:
    """
    RFD3 contig for a trimmed target: ``68-86,/0,A12-98``.

    A designed binder length range, a chain break, then each retained target
    span. More than one span means more than one chain break, which is why
    :func:`plan_trim` prefers a contiguous answer.
    """
    spans = ",".join(f"{chain}{lo}-{hi}" for lo, hi in segments)
    return f"{binder_min}-{binder_max},/0,{spans}"


def _per_residue_bsa(structure_path: Path, target_chain: str,
                     partner_chain: str) -> tuple[dict[int, float], float]:
    """Per-residue buried surface for the target chain, and the interface total."""
    from src.structure_tools import analyze_interface, is_solvent_or_additive

    try:
        res = analyze_interface(str(structure_path), target_chain, partner_chain)
    except Exception as exc:
        logger.warning(f"interface analysis unavailable: {exc}")
        return {}, 0.0
    iface = res.get("interface") or {}
    total = float(iface.get("bsa_total_A2") or 0.0)
    per: dict[int, float] = {}
    for e in iface.get("bsa_per_residue") or []:
        # bsa_per_residue covers BOTH chains; keep only the target's side, or the
        # partner's buried area would inflate every domain's score.
        if not isinstance(e, dict) or e.get("chain") != target_chain:
            continue
        # ...and only the POLYPEPTIDE's side. Ordered waters carry the target
        # chain's id and their own numbering, so they landed in the per-residue
        # total but could never be in `kept_set` — every interface water was
        # counted as a residue the trim had removed. On 7CZD that was 20 waters
        # worth 435 A^2, enough to open a spurious trim_gate on a trim that
        # removed nothing at all.
        if is_solvent_or_additive(str(e.get("residue") or "")):
            continue
        auth, bsa = e.get("resnum"), e.get("bsa_A2")
        if auth is None or bsa is None:
            continue
        per[int(auth)] = per.get(int(auth), 0.0) + float(bsa)
    return per, total


def trim_target(
    structure_path: Path,
    *,
    target_chain: str,
    partner_chain: str | None,
    hotspots: Sequence[dict],
    budget: int,
    out_dir: Path,
    pdb_id: str | None = None,
    binder_min: int = 68,
    binder_max: int = 86,
    method: str = "auto",
    chainsaw_cmd: Sequence[str] | None = None,
    min_bsa_retention: float = 0.90,
    allowed_auth: set[int] | None = None,
) -> TrimResult:
    """
    Crop `target_chain` to `budget` residues on domain boundaries.

    Writes ``trimmed.cif``, ``trimmed.pdb`` (RFD3's input) and ``trim_map.json``
    into `out_dir`. The partner chain is retained in full so the interface can be
    re-measured; the RFD3 spec uses the target chain alone.

    Raises TrimBudgetError if the hotspot-bearing domains do not fit, and
    TrimError if the trim loses a hotspot — neither is recoverable by trying
    harder, and silently proceeding would change what is being designed against.
    """
    from src.structure_tools import get_sequence_map

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    residues = chain_residues(structure_path, target_chain)

    # The topology restriction is applied AFTER domain segmentation, not before.
    # Removing the transmembrane and cytoplasmic residues up front also removes
    # the contact-density drop that marks the ectodomain boundary, so the
    # segmenter can no longer find it — on CD79B that turned a clean 42-145 Ig
    # domain into 58-159, trailing the membrane-proximal stalk.
    if allowed_auth:
        n_out = sum(1 for r in residues if r["auth"] not in allowed_auth)
        if n_out:
            logger.info(
                f"topology restriction: {n_out} of {len(residues)} residues in "
                f"chain {target_chain} are not designable and will be dropped "
                f"after domain segmentation")
        if n_out == len(residues):
            raise TrimError(
                "the topology restriction excludes every residue of "
                f"chain {target_chain}; nothing is left to design against")
    by_auth = {r["auth"]: r for r in residues}
    hot = _hotspot_auths(hotspots)
    missing = [h for h in hot if h not in by_auth]
    if missing:
        raise TrimError(
            f"hotspot residues {missing} are not present in chain {target_chain} "
            f"of {structure_path} — check the auth/label numbering upstream")

    try:
        auth_to_label = get_sequence_map(str(structure_path), target_chain).get(
            "auth_to_label") or {}
    except Exception:
        auth_to_label = {}

    domains, method_used = segment_domains(
        structure_path, target_chain, budget=budget, pdb_id=pdb_id,
        auth_to_label=auth_to_label, method=method, chainsaw_cmd=chainsaw_cmd,
        residues=residues)

    per_bsa, bsa_before = ({}, 0.0)
    if partner_chain:
        per_bsa, bsa_before = _per_residue_bsa(
            structure_path, target_chain, partner_chain)

    sse = sse_by_residue(structure_path, target_chain)
    keep, warnings = plan_trim(residues, domains, hotspots, budget,
                               per_residue_bsa=per_bsa, sse=sse,
                               allowed_auth=allowed_auth)
    if not keep:
        raise TrimError("trim planning kept no residues")

    segments = _segments(keep)
    keep_map: dict[str, Sequence[int] | None] = {target_chain: keep}
    if partner_chain:
        keep_map[partner_chain] = None
    cif_path = write_trimmed(structure_path, out_dir / "trimmed.cif", keep_map)
    write_trimmed(structure_path, out_dir / "trimmed.pdb", keep_map)

    kept_set = set(keep)
    retained = [h for h in hotspots if int(h.get("auth_seq_id", -1)) in kept_set]
    lost = [h for h in hotspots if int(h.get("auth_seq_id", -1)) not in kept_set]

    # Retention is measured over the residues we KEPT, not over the whole native
    # interface. Dropping a second interface on purpose (7XQ8's CD79A/CD79B pair
    # buries ~60% of its area through the transmembrane helices, which an
    # extracellular-domain campaign deliberately discards) is the trim working,
    # not the trim failing. What must not happen is the residues we kept losing
    # the contacts they had.
    bsa_after, retention, dropped_bsa = 0.0, 1.0, 0.0
    target_side_before = 0.0
    per_after: dict[int, float] = {}
    if partner_chain and bsa_before > 0:
        per_after, bsa_after = _per_residue_bsa(cif_path, target_chain, partner_chain)
        # Compare like with like. `bsa_before` is analyze_interface's
        # bsa_total_A2, which covers BOTH chains, while `per_bsa` is filtered to
        # the TARGET's side (see _per_residue_bsa). Subtracting one from the
        # other reported roughly half the interface as "dropped" on every trim
        # — including a no-op that removed a single cloning-artifact residue,
        # which warned "removed 1138 A^2 (60%)" and opened a trim_gate
        # checkpoint. Measured on 3KYS: total 3,401.7 vs target-side 1,652.1.
        target_side_before = sum(per_bsa.values())
        kept_before = sum(v for a, v in per_bsa.items() if a in kept_set)
        kept_after = sum(v for a, v in per_after.items() if a in kept_set)
        dropped_bsa = max(0.0, target_side_before - kept_before)
        retention = kept_after / kept_before if kept_before > 0 else 1.0
        if target_side_before > 0 and dropped_bsa > 0.05 * target_side_before:
            warnings.append(
                f"the trim removed residues carrying {dropped_bsa:.0f} A^2 "
                f"({dropped_bsa / target_side_before:.0%}) of the native "
                f"{target_chain}-{partner_chain} interface. That is expected when "
                f"the target has more than one interface and you are designing "
                f"against a single one — confirm it is the one you meant.")

    # A hotspot that gets buried by a freshly exposed cut face is unusable even
    # though it is still present, and nothing else here would notice.
    warnings += _hotspot_exposure_warnings(
        structure_path, cif_path, target_chain, retained)

    # A disulfide whose partner was cut leaves a free cysteine that will not
    # behave like the deposited structure. Surfaced, never auto-mutated.
    warnings += _disulfide_warnings(structure_path, target_chain, kept_set)

    result = TrimResult(
        trimmed_path=cif_path,
        mapping_path=out_dir / "trim_map.json",
        kept_segments=segments,
        n_segments=len(segments),
        contig=build_contig(segments, target_chain, binder_min, binder_max),
        n_residues_before=len(residues),
        n_residues_after=len(keep),
        domains=list(domains),
        method=method_used,
        hotspots_retained=list(retained),
        hotspots_lost=list(lost),
        interface_bsa_before_A2=round(bsa_before, 1),
        interface_bsa_target_side_A2=round(target_side_before, 1),
        interface_bsa_after_A2=round(bsa_after, 1),
        bsa_retention=round(retention, 4),
        bsa_dropped_A2=round(dropped_bsa, 1),
        warnings=warnings,
    )
    _write_mapping(result, structure_path, pdb_id, target_chain, partner_chain,
                   budget, hotspots)

    if result.hotspots_lost:
        raise TrimError(
            f"trim lost hotspot(s) "
            f"{[h.get('auth_seq_id') for h in result.hotspots_lost]} — "
            f"the RFD3 spec would point at residues that are no longer there")
    if partner_chain and bsa_before > 0 and retention < min_bsa_retention:
        raise TrimError(
            f"the residues kept by the trim retained only {retention:.1%} of the "
            f"interface area they had before it, below the "
            f"{min_bsa_retention:.0%} floor — cutting has damaged the epitope "
            f"itself, not merely removed a different interface")
    logger.info(
        f"trim [{method_used}]: {len(residues)} -> {len(keep)} residues, "
        f"{len(segments)} segment(s), BSA retention {retention:.1%}")
    return result


def _hotspot_exposure_warnings(original: Path, trimmed: Path, chain: str,
                               hotspots: Sequence[dict],
                               max_drop: float = 0.30) -> list[str]:
    """
    Flag hotspots that lost solvent exposure because of the trim.

    Cutting a domain away exposes a new face; a hotspot packed against that face
    can end up buried by it. It is still in the structure, so hotspot-retention
    passes, but a binder can no longer reach it.
    """
    try:
        from src.structure_tools import _compute_sasa_per_residue, _load_biopython
    except ImportError:
        return []

    def sasa(path: Path) -> dict[int, float]:
        # _compute_sasa_per_residue takes a Bio.PDB structure and a chain LIST,
        # and keys its result "<chain>:<resseq>:<resname>".
        raw = _compute_sasa_per_residue(_load_biopython(str(path)), [chain])
        out: dict[int, float] = {}
        for key, val in raw.items():
            parts = key.split(":")
            if len(parts) >= 2 and parts[0] == chain:
                try:
                    out[int(parts[1])] = float(val)
                except ValueError:
                    continue
        return out

    try:
        before, after = sasa(original), sasa(trimmed)
    except Exception as exc:
        logger.debug(f"hotspot exposure check unavailable: {exc}")
        return []
    out = []
    for h in hotspots:
        auth = h.get("auth_seq_id")
        if auth is None:
            continue
        b, a = before.get(int(auth)), after.get(int(auth))
        if not b or a is None or b <= 0:
            continue
        if (b - a) / b > max_drop:
            out.append(
                f"hotspot {h.get('residue', '')}{auth} lost {(b - a) / b:.0%} of its "
                f"solvent exposure to the trim ({b:.0f} -> {a:.0f} A^2) — it is "
                f"retained but may no longer be reachable by a binder")
    return out


def _disulfide_warnings(structure_path: Path, chain: str,
                        kept: set[int]) -> list[str]:
    import gemmi

    st = _model(structure_path)
    sg: dict[int, np.ndarray] = {}
    for model in st:
        for ch in model:
            if ch.name != chain:
                continue
            for res in ch:
                if res.name != "CYS":
                    continue
                atom = res.find_atom("SG", "*")
                if atom is not None:
                    sg[int(res.seqid.num)] = np.array(
                        [atom.pos.x, atom.pos.y, atom.pos.z])
    broken = []
    for a, ca_ in sg.items():
        for b, cb in sg.items():
            if a >= b or float(np.linalg.norm(ca_ - cb)) > 2.5:
                continue
            if (a in kept) != (b in kept):
                broken.append((a, b))
    return [f"disulfide {a}-{b} was cut; the retained cysteine is now free "
            f"(not mutated — decide explicitly)" for a, b in broken]


def _write_mapping(result: TrimResult, source: Path, pdb_id: str | None,
                   target_chain: str, partner_chain: str | None,
                   budget: int, hotspots: Sequence[dict]) -> None:
    kept = set()
    for lo, hi in result.kept_segments:
        kept |= set(range(lo, hi + 1))
    payload = {
        "pdb_id": pdb_id,
        "source": str(source),
        # Recorded so a resumed run can rebuild what it needs from this file
        # alone, without re-running the trim.
        "trimmed_path": str(result.trimmed_path),
        "trimmed_pdb_path": str(Path(result.trimmed_path).with_suffix(".pdb")),
        "method": result.method,
        "target_chain": target_chain,
        "partner_chain": partner_chain,
        "budget": budget,
        "kept_segments": [list(s) for s in result.kept_segments],
        "n_segments": result.n_segments,
        "contig": result.contig,
        "n_residues_before": result.n_residues_before,
        "n_residues_after": result.n_residues_after,
        # Author numbering is preserved, so the mapping is the identity. Recorded
        # explicitly so downstream code can assert it rather than assume it.
        "identity_numbering": True,
        "auth_in_to_auth_out": {},
        "hotspots": [
            {**h, "retained": int(h.get("auth_seq_id", -1)) in kept}
            for h in hotspots
        ],
        "domains": [asdict(d) for d in result.domains],
        "bsa_before_A2": result.interface_bsa_before_A2,
        "bsa_before_target_side_A2": result.interface_bsa_target_side_A2,
        "bsa_after_A2": result.interface_bsa_after_A2,
        # Over the KEPT residues only — see trim_target.
        "bsa_retention": result.bsa_retention,
        "bsa_dropped_with_removed_residues_A2": result.bsa_dropped_A2,
        "warnings": result.warnings,
    }
    result.mapping_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_mapping(mapping_path: Path) -> dict[str, Any]:
    return json.loads(Path(mapping_path).read_text(encoding="utf-8"))


def verify_fold_retained(trimmed: Path, original: Path, foldseek_bin: str | None,
                         min_tm: float = 0.5) -> float | None:
    """
    TM-score of the trimmed fragment against the source chain, via Foldseek.

    The honest use of Foldseek in this module: a cheap check that what came out
    is still recognisably the target. Returns None when Foldseek is unavailable —
    it is a sanity check, not a gate.
    """
    if not foldseek_bin:
        return None
    exe = shutil.which(foldseek_bin) or (
        foldseek_bin if Path(foldseek_bin).exists() else None)
    if exe is None:
        logger.info("foldseek not configured; skipping the post-trim fold check")
        return None
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "aln.tsv"
        cmd = [exe, "easy-search", str(trimmed), str(original), str(out),
               str(Path(td) / "tmp"), "--format-output", "alntmscore",
               "-e", "inf", "--exhaustive-search", "1"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"foldseek check failed: {exc}")
            return None
        if proc.returncode != 0 or not out.exists():
            logger.warning(f"foldseek exited {proc.returncode}")
            return None
        vals = [float(l.split()[0]) for l in out.read_text().splitlines() if l.strip()]
    if not vals:
        return None
    tm = max(vals)
    if tm < min_tm:
        logger.warning(
            f"trimmed fragment scores TM {tm:.2f} against the source chain "
            f"(< {min_tm}) — it may no longer fold like the target")
    return tm
