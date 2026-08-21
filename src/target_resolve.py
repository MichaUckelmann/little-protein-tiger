"""
Resolve a bare protein name to a ranked table of designable interfaces.

This is the deterministic half of the binder track's target-intel stage, and it
exists to keep the LLM cheap. Name -> UniProt -> candidate PDB complexes ->
downloaded assemblies -> per-chain-pair interface analysis all happen here, in
Python, at zero API cost. The skill then receives a compact markdown table and
does the one thing only a model can: decide which interface carries downstream
signalling, given what the user actually asked for.

Without this the skill makes ~15 tool round-trips to assemble the same facts.
With it, 0-3.

Name resolution reuses `src/identifier_normalizer.py`, which already maps gene
symbols to human UniProt accessions offline from the bundled ID mapping — there
is no UniProt web call and no key.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from loguru import logger

_ROOT = Path(__file__).resolve().parents[1]

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"

# Below this, a chain pair is far more likely a crystal-packing contact than a
# biological interface.
MIN_BIOLOGICAL_BSA_A2 = 500.0


class TargetResolutionError(RuntimeError):
    """The target could not be resolved to something designable."""


@dataclass
class ResolvedTarget:
    query: str
    gene: str | None
    uniprot: str | None
    match_confidence: str | None
    candidates: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.uniprot)


@dataclass
class InterfaceCandidate:
    pdb_id: str
    method: str
    resolution_A: float | None
    target_chain: str
    target_entity: str
    target_len: int
    partner_chain: str
    partner_entity: str
    partner_len: int
    bsa_A2: float
    n_interface_residues: int
    n_hbonds: int
    hydrophobic_fraction: float
    score: float = 0.0
    rank: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------
# Name -> UniProt
# ----------------------------------------------------------------------

@lru_cache(maxsize=256)
def fetch_uniprot_sequence(accession: str, timeout: float = 15.0) -> str | None:
    """
    The canonical amino-acid sequence for a UniProt accession, FASTA plain text.

    One small GET, cached per-process — this is the ground truth a chain's
    modelled sequence is checked against in
    `PipelineRunner._verify_target_chain_assignment`, so it is called at least
    once per interface stage and must not re-fetch on every call.
    """
    import requests

    try:
        resp = requests.get(f"https://rest.uniprot.org/uniprotkb/{accession}.fasta",
                            timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"could not fetch UniProt sequence for {accession}: {exc}")
        return None
    lines = resp.text.splitlines()
    seq = "".join(l.strip() for l in lines if l and not l.startswith(">"))
    return seq or None


def resolve_target(name: str) -> ResolvedTarget:
    """Gene symbol or alias -> human UniProt accession. Offline."""
    from src.identifier_normalizer import get_normalizer

    res = get_normalizer().resolve(name)
    out = ResolvedTarget(
        query=name, gene=res.human_gene_symbol, uniprot=res.human_uniprot,
        match_confidence=res.match_confidence,
        candidates=list(res.candidate_uniprots or []),
    )
    if not out.ok:
        logger.warning(f"could not resolve {name!r} to a human UniProt accession")
    else:
        logger.info(
            f"{name!r} -> {out.gene} / {out.uniprot} ({out.match_confidence})")
    return out


# ----------------------------------------------------------------------
# UniProt -> PDB complexes
# ----------------------------------------------------------------------

def find_complex_structures(
    uniprot: str,
    *,
    rows: int = 100,
    min_protein_entities: int = 2,
    timeout: float = 30.0,
) -> list[str]:
    """
    PDB entries containing this accession AND at least one other protein entity.

    `search_rcsb_pdb` in the skill layer is full-text only and cannot express
    "two protein entities, one of which is P01116" — hence this structured
    query against the sequence-identifier index.
    """
    import requests

    query = {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": [
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers"
                                     ".reference_sequence_identifiers"
                                     ".database_accession",
                        "operator": "exact_match",
                        "value": uniprot.upper(),
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers"
                                     ".reference_sequence_identifiers.database_name",
                        "operator": "exact_match",
                        "value": "UniProt",
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_entry_info.polymer_entity_count_protein",
                        "operator": "greater_or_equal",
                        "value": min_protein_entities,
                    },
                },
            ],
        },
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": rows},
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }
    try:
        resp = requests.post(RCSB_SEARCH_URL, json=query, timeout=timeout)
        if resp.status_code == 204:          # RCSB's "no hits"
            return []
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(f"RCSB complex search failed for {uniprot}: {exc}")
        return []
    ids = [r["identifier"] for r in payload.get("result_set", [])]
    logger.info(f"{uniprot}: {len(ids)} complex structure(s) — {', '.join(ids[:8])}"
                + (" ..." if len(ids) > 8 else ""))
    return ids


_ENTRY_QUERY = """{
  entries(entry_ids: %s) {
    rcsb_id
    exptl { method }
    rcsb_entry_info { resolution_combined polymer_entity_count_protein }
    struct { title }
    polymer_entities {
      rcsb_polymer_entity { pdbx_description }
      entity_poly { rcsb_sample_sequence_length }
      rcsb_polymer_entity_container_identifiers {
        reference_sequence_identifiers { database_accession database_name }
      }
      polymer_entity_instances {
        rcsb_polymer_entity_instance_container_identifiers { auth_asym_id }
      }
    }
  }
}"""


def entry_metadata(pdb_ids: Sequence[str], timeout: float = 30.0) -> dict[str, dict]:
    """Per-entry method/resolution and per-chain entity descriptions + accessions."""
    import requests

    if not pdb_ids:
        return {}
    ids = json.dumps([p.upper() for p in pdb_ids])
    try:
        resp = requests.post(RCSB_GRAPHQL_URL,
                             json={"query": _ENTRY_QUERY % ids}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(f"RCSB metadata fetch failed: {exc}")
        return {}
    if payload.get("errors"):
        logger.warning(f"RCSB metadata errors: {payload['errors'][:1]}")

    out: dict[str, dict] = {}
    for entry in (payload.get("data") or {}).get("entries") or []:
        info = entry.get("rcsb_entry_info") or {}
        res = info.get("resolution_combined") or []
        chains: dict[str, dict] = {}
        for pe in entry.get("polymer_entities") or []:
            desc = (pe.get("rcsb_polymer_entity") or {}).get("pdbx_description") or ""
            length = (pe.get("entity_poly") or {}).get("rcsb_sample_sequence_length")
            accs = [
                r.get("database_accession")
                for r in ((pe.get("rcsb_polymer_entity_container_identifiers") or {})
                          .get("reference_sequence_identifiers") or [])
                if r.get("database_name") == "UniProt"
            ]
            for inst in pe.get("polymer_entity_instances") or []:
                cid = (inst.get(
                    "rcsb_polymer_entity_instance_container_identifiers") or {}
                ).get("auth_asym_id")
                if cid:
                    chains[cid] = {"description": desc, "length": length,
                                   "uniprots": accs}
        out[entry["rcsb_id"].upper()] = {
            "method": ((entry.get("exptl") or [{}])[0]).get("method", ""),
            "resolution_A": (res[0] if res else None),
            "title": (entry.get("struct") or {}).get("title", ""),
            "n_protein_entities": info.get("polymer_entity_count_protein"),
            "chains": chains,
        }
    return out


# ----------------------------------------------------------------------
# Interface ranking
# ----------------------------------------------------------------------

def _zscores(values: Sequence[float]) -> list[float]:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return []
    std = arr.std()
    if std < 1e-12:
        return [0.0] * arr.size
    return list((arr - arr.mean()) / std)


def rank_interfaces(candidates: list[InterfaceCandidate]) -> list[InterfaceCandidate]:
    """
    Score and order candidate interfaces.

    Larger, better-hydrogen-bonded, more hydrophobic interfaces on smaller target
    chains rank higher: a bigger buried area is more likely to be biological and
    more tractable to compete with, and a smaller target chain is cheaper to
    design against. This is a shortlist for the model to judge, not a verdict.
    """
    if not candidates:
        return []
    z_bsa = _zscores([c.bsa_A2 for c in candidates])
    z_hb = _zscores([c.n_hbonds for c in candidates])
    z_phob = _zscores([c.hydrophobic_fraction for c in candidates])
    z_len = _zscores([c.target_len for c in candidates])
    for c, a, b, d, e in zip(candidates, z_bsa, z_hb, z_phob, z_len):
        c.score = round(0.5 * a + 0.2 * b + 0.2 * d - 0.1 * e, 4)
    ordered = sorted(candidates, key=lambda c: -c.score)
    # Demote repeats of a partner already represented, so the table the model
    # reads spans distinct biology rather than several chains of one complex.
    seen: set[str] = set()
    primary, repeats = [], []
    for c in ordered:
        key = f"{c.partner_entity[:30]}"
        (repeats if key in seen else primary).append(c)
        seen.add(key)
    ordered = primary + repeats
    for i, c in enumerate(ordered, 1):
        c.rank = i
    return ordered


def analyse_entry(
    structure_path: Path,
    pdb_id: str,
    meta: dict,
    uniprot: str,
    *,
    min_bsa: float = MIN_BIOLOGICAL_BSA_A2,
) -> list[InterfaceCandidate]:
    """
    Every chain pair in one entry where one chain is our target.

    Both directions are NOT emitted: the target is whichever chain carries our
    accession, and the other chain is the partner whose binding we want to
    block or mimic.
    """
    from src.structure_tools import analyze_interface

    chains = meta.get("chains") or {}
    ours = []
    for c, info in chains.items():
        accs = {str(a).upper() for a in (info.get("uniprots") or [])}
        if uniprot.upper() not in accs:
            continue
        if int(info.get("length") or 0) < MIN_TARGET_LENGTH:
            logger.debug(
                f"{pdb_id} chain {c}: target present as a "
                f"{info.get('length')}-residue fragment; skipping")
            continue
        ours.append(c)
    if not ours:
        return []

    out: list[InterfaceCandidate] = []
    for target_chain in ours:
        for partner_chain, pinfo in chains.items():
            if partner_chain == target_chain:
                continue
            try:
                res = analyze_interface(str(structure_path), target_chain,
                                        partner_chain)
            except Exception as exc:
                logger.debug(f"{pdb_id} {target_chain}/{partner_chain}: {exc}")
                continue
            iface = res.get("interface") or {}
            bsa = float(iface.get("bsa_total_A2") or 0.0)
            if bsa < min_bsa:
                continue          # almost certainly crystal packing
            residues = res.get("chain_a_interface_residues") or []
            types = [r.get("type") for r in residues]
            phobic = (sum(1 for t in types if t in ("hydrophobic", "aromatic"))
                      / len(types)) if types else 0.0
            tinfo = chains.get(target_chain) or {}
            out.append(InterfaceCandidate(
                pdb_id=pdb_id,
                method=meta.get("method", ""),
                resolution_A=meta.get("resolution_A"),
                target_chain=target_chain,
                target_entity=(tinfo.get("description") or "")[:60],
                target_len=int(tinfo.get("length") or 0),
                partner_chain=partner_chain,
                partner_entity=(pinfo.get("description") or "")[:60],
                partner_len=int(pinfo.get("length") or 0),
                bsa_A2=round(bsa, 1),
                n_interface_residues=len(residues),
                n_hbonds=int(iface.get("n_hbonds") or 0),
                hydrophobic_fraction=round(phobic, 3),
            ))
    return out


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

def render_candidate_table(candidates: Sequence[InterfaceCandidate],
                           limit: int = 40) -> str:
    """
    A compact markdown table for the skill prompt.

    ~40 rows is 2-3k input tokens — about $0.01 at Sonnet 5 rates, and cheaper
    than the tool round-trips it replaces.
    """
    if not candidates:
        return "_No candidate interfaces found._"
    head = ("| # | PDB | method | res Å | target | len | partner | len | "
            "BSA Å² | iface res | H-bonds | φ frac |")
    sep = "|" + "---|" * 12
    rows = [head, sep]
    for c in candidates[:limit]:
        res = f"{c.resolution_A:.2f}" if c.resolution_A else "—"
        rows.append(
            f"| {c.rank} | {c.pdb_id} | {c.method[:12]} | {res} | "
            f"{c.target_chain} {c.target_entity[:26]} | {c.target_len} | "
            f"{c.partner_chain} {c.partner_entity[:26]} | {c.partner_len} | "
            f"{c.bsa_A2:.0f} | {c.n_interface_residues} | {c.n_hbonds} | "
            f"{c.hydrophobic_fraction:.2f} |")
    if len(candidates) > limit:
        rows.append(f"\n_({len(candidates) - limit} further candidates omitted.)_")
    return "\n".join(rows)


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

# A partner shorter than this is a peptide, a designed mini-binder or a
# crystallisation chaperone fragment — not a signalling effector.
MIN_PARTNER_LENGTH = 50

# The TARGET chain must be a real fragment of the protein, not a peptide of it
# appearing in someone else's structure. Several of the highest-scoring KRAS
# "complexes" are farnesyltransferase entries in which KRAS is an 11-residue
# CAAX substrate peptide — a legitimate KRAS structure, and useless to design a
# binder against.
MIN_TARGET_LENGTH = 50


def _partner_chains(meta_entry: dict, uniprot: str) -> list[dict]:
    """Chains in an entry that are a DIFFERENT protein from the target."""
    up = uniprot.upper()
    out = []
    for info in (meta_entry.get("chains") or {}).values():
        accs = {str(a).upper() for a in (info.get("uniprots") or [])}
        if up in accs:
            continue
        out.append(info)
    return out


def _triage_entries(pdb_ids: Sequence[str], meta: dict[str, dict], uniprot: str,
                    max_entries: int) -> list[str]:
    """
    Pick which entries are worth downloading and analysing.

    Sorting on resolution alone is actively misleading for a well-studied target:
    the highest-resolution KRAS complexes are inhibitor, designed-binder and
    nanobody-chaperone structures, while the effector complexes that matter for
    "disrupt downstream signalling" — RAF1, SOS1, PI3K — sit further down. So
    entries carrying a substantial, distinct protein partner come first, and
    partner diversity is preferred over several views of the same pair.
    """
    scored: list[tuple[tuple, str]] = []
    for pid in pdb_ids:
        m = meta.get(pid.upper())
        if not m:
            continue
        partners = _partner_chains(m, uniprot)
        best_len = max((int(p.get("length") or 0) for p in partners), default=0)
        has_real_partner = 0 if best_len >= MIN_PARTNER_LENGTH else 1
        target_len = max(
            (int(i.get("length") or 0) for i in (m.get("chains") or {}).values()
             if uniprot.upper() in {str(a).upper() for a in (i.get("uniprots") or [])}),
            default=0)
        target_is_full = 0 if target_len >= MIN_TARGET_LENGTH else 1
        method = (m.get("method") or "").upper()
        method_rank = 0 if "X-RAY" in method else (1 if "ELECTRON" in method else 2)
        scored.append((
            (target_is_full, has_real_partner, method_rank,
             m.get("resolution_A") or 99.0),
            pid.upper(),
        ))
    scored.sort()

    # Round-robin over distinct partner identities so one heavily-solved complex
    # cannot fill the whole shortlist.
    chosen: list[str] = []
    seen_partners: set[str] = set()
    for _, pid in scored:
        names = {(p.get("description") or "")[:40]
                 for p in _partner_chains(meta[pid], uniprot)}
        if names and names <= seen_partners:
            continue
        seen_partners |= names
        chosen.append(pid)
        if len(chosen) >= max_entries:
            return chosen
    for _, pid in scored:                    # top up if diversity ran out
        if pid not in chosen:
            chosen.append(pid)
        if len(chosen) >= max_entries:
            break
    return chosen


def ensure_assembly(pdb_id: str, structures_dir: Path,
                    timeout: float = 60.0) -> Path | None:
    """
    Fetch biological assembly 1 for an entry, cached on disk.

    Assembly 1 rather than the asymmetric unit: the ASU can split a biological
    dimer across symmetry copies or contain several copies of it, and either way
    the chain pair you measure is not the one that exists in solution.
    """
    import gzip

    import requests

    structures_dir = Path(structures_dir)
    structures_dir.mkdir(parents=True, exist_ok=True)
    dest = structures_dir / f"{pdb_id.upper()}_ba1.cif"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = f"https://files.rcsb.org/download/{pdb_id.upper()}-assembly1.cif.gz"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        dest.write_bytes(gzip.decompress(resp.content))
        return dest
    except Exception as exc:
        logger.warning(f"could not fetch {pdb_id} assembly 1: {exc}")
        return None


def build_candidate_table(
    name: str,
    *,
    structures_dir: Path,
    max_entries: int = 8,
    rows: int = 100,
    min_bsa: float = MIN_BIOLOGICAL_BSA_A2,
) -> tuple[ResolvedTarget, list[InterfaceCandidate]]:
    """
    Name -> ranked interface candidates. The whole deterministic pre-pass.

    Entries are triaged by resolution and method BEFORE download, so only the
    most usable handful are fetched and analysed — interface analysis is the
    expensive step here, not the network.
    """
    target = resolve_target(name)
    if not target.ok:
        raise TargetResolutionError(
            f"{name!r} does not resolve to a human UniProt accession. Check the "
            f"gene symbol, or pass a PDB id directly.")

    pdb_ids = find_complex_structures(target.uniprot, rows=rows)
    if not pdb_ids:
        raise TargetResolutionError(
            f"no PDB entry contains {target.gene} ({target.uniprot}) together with "
            f"another protein. There may be no experimentally solved complex; "
            f"supply a structure explicitly.")

    meta = entry_metadata(pdb_ids)

    chosen = _triage_entries(pdb_ids, meta, target.uniprot, max_entries)
    logger.info(f"analysing {len(chosen)} entr(ies): {', '.join(chosen)}")

    candidates: list[InterfaceCandidate] = []
    for pid in chosen:
        path = ensure_assembly(pid, structures_dir)
        if path is None:
            continue
        candidates.extend(
            analyse_entry(path, pid.upper(), meta[pid.upper()], target.uniprot,
                          min_bsa=min_bsa))
    ranked = rank_interfaces(candidates)
    logger.info(f"{len(ranked)} candidate interface(s) above {min_bsa:.0f} A^2")
    return target, ranked


def write_candidates(target: ResolvedTarget, candidates: Sequence[InterfaceCandidate],
                     out_dir: Path) -> dict[str, Path]:
    """Persist candidates.json + candidates.md; the latter goes into the prompt."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / "candidates.json"
    j.write_text(json.dumps({
        "target": asdict(target),
        "candidates": [c.as_dict() for c in candidates],
    }, indent=2), encoding="utf-8")
    m = out_dir / "candidates.md"
    m.write_text(render_candidate_table(candidates), encoding="utf-8")
    return {"json": j, "markdown": m}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="Gene symbol or protein name, e.g. KRAS")
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("."))
    ap.add_argument("--structures-dir", type=Path,
                    default=_ROOT / "data" / "structures")
    ap.add_argument("--max-entries", type=int, default=8)
    ap.add_argument("--min-bsa", type=float, default=MIN_BIOLOGICAL_BSA_A2)
    args = ap.parse_args(argv)

    target, candidates = build_candidate_table(
        args.name, structures_dir=args.structures_dir,
        max_entries=args.max_entries, min_bsa=args.min_bsa)
    paths = write_candidates(target, candidates, args.out_dir)
    print(render_candidate_table(candidates))
    print(f"\n-> {paths['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
