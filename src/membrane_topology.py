"""
Membrane topology from UniProt, mapped into a structure's author numbering.

A binder against a cell-surface receptor has to bind the part of it a binder can
reach. Designing against the cytoplasmic tail of PD-L1 produces a molecule that
is correct in every computational sense and cannot work: the epitope is on the
far side of a membrane. Structures do not carry this information — a construct is
often just "the ectodomain", with nothing in the file saying so — but UniProt
annotates it precisely, per residue.

So: pull `Transmembrane` and `Topological domain` features from UniProt, map the
residue ranges through the RCSB entity alignment into the structure's author
numbering, and restrict the design target to ONE face of the membrane.

Which face is not a fixed answer, and the caller decides it. For a cell-surface
receptor it is the extracellular side; for an intracellular-organelle membrane
protein it is not — SCAP sits in the ER membrane and the SREBP-binding WD40
domain it is designed against faces the cytosol, so "extracellular" names no
real surface at all. `PipelineRunner._infer_membrane_side` therefore derives the
side from where the declared hotspots actually sit and passes it in;
`side="any"` disables the restriction. What is NOT optional either way is
dropping the transmembrane segments themselves — see `restriction_for`.

Soluble proteins (KRAS, VEGF-A) simply have no such features, and everything here
degrades to "no restriction".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from loguru import logger

UNIPROT_URL = "https://rest.uniprot.org/uniprotkb/{acc}.json"
RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"

EXTRACELLULAR = "extracellular"
CYTOPLASMIC = "cytoplasmic"
TRANSMEMBRANE = "transmembrane"
SIGNAL = "signal"
LUMENAL = "lumenal"

# Descriptions UniProt uses for the outward-facing side. Lumenal counts: for a
# secretory-pathway or organellar membrane protein it is the topological
# equivalent of extracellular, and it is what an antibody or binder reaches.
_OUTSIDE = ("extracellular", "lumenal", "lumenal, melanosome", "periplasmic",
            "vesicular", "exoplasmic loop")
_INSIDE = ("cytoplasmic", "mitochondrial matrix", "nuclear", "intravirion")


@dataclass
class TopologySegment:
    kind: str          # extracellular | cytoplasmic | transmembrane | signal
    start: int         # UniProt residue numbering, inclusive
    end: int
    description: str = ""

    def __len__(self) -> int:
        return self.end - self.start + 1


@dataclass
class Topology:
    uniprot: str
    segments: list[TopologySegment] = field(default_factory=list)
    fetched: bool = False

    @property
    def is_membrane(self) -> bool:
        return any(s.kind == TRANSMEMBRANE for s in self.segments)

    def of_kind(self, kind: str) -> list[TopologySegment]:
        return [s for s in self.segments if s.kind == kind]

    @property
    def extracellular(self) -> list[TopologySegment]:
        return self.of_kind(EXTRACELLULAR)

    @property
    def cytoplasmic(self) -> list[TopologySegment]:
        return self.of_kind(CYTOPLASMIC)

    def kind_at(self, pos: int) -> str | None:
        for s in self.segments:
            if s.start <= pos <= s.end:
                return s.kind
        return None

    def describe(self) -> str:
        if not self.fetched:
            return "topology unknown"
        if not self.is_membrane:
            return "soluble (no transmembrane segment annotated)"
        parts = [f"{s.kind} {s.start}-{s.end}" for s in self.segments
                 if s.kind != SIGNAL]
        return "membrane protein: " + ", ".join(parts)

    def as_dict(self) -> dict:
        return {"uniprot": self.uniprot, "fetched": self.fetched,
                "is_membrane": self.is_membrane,
                "segments": [asdict(s) for s in self.segments]}


def _classify(feature_type: str, description: str) -> str | None:
    t = (feature_type or "").lower()
    d = (description or "").lower().strip()
    if t == "transmembrane":
        return TRANSMEMBRANE
    if t == "signal":
        return SIGNAL
    if t in ("topological domain", "intramembrane"):
        if any(d.startswith(x) for x in _OUTSIDE):
            return EXTRACELLULAR
        if any(d.startswith(x) for x in _INSIDE):
            return CYTOPLASMIC
        logger.debug(f"unclassified topological domain description: {description!r}")
    return None


def fetch_topology(uniprot: str, timeout: float = 30.0) -> Topology:
    """
    Transmembrane + topological-domain features for one accession.

    A failure here is never fatal: `fetched=False` means "we do not know", which
    downstream code treats as "impose no restriction" rather than guessing.
    """
    import requests

    topo = Topology(uniprot=uniprot)
    try:
        resp = requests.get(
            UNIPROT_URL.format(acc=uniprot),
            params={"fields": "ft_transmem,ft_topo_dom,ft_signal"}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(f"UniProt topology lookup failed for {uniprot}: {exc}")
        return topo

    for feat in payload.get("features", []) or []:
        kind = _classify(feat.get("type"), (feat.get("description") or ""))
        if kind is None:
            continue
        loc = feat.get("location") or {}
        start = (loc.get("start") or {}).get("value")
        end = (loc.get("end") or {}).get("value")
        if start is None or end is None:
            continue
        topo.segments.append(TopologySegment(
            kind=kind, start=int(start), end=int(end),
            description=feat.get("description") or ""))
    topo.segments.sort(key=lambda s: s.start)
    topo.fetched = True
    logger.info(f"{uniprot}: {topo.describe()}")
    return topo


# ----------------------------------------------------------------------
# UniProt numbering -> author numbering
# ----------------------------------------------------------------------

_ALIGN_QUERY = """{
  entry(entry_id:"%s") {
    polymer_entities {
      rcsb_polymer_entity_align {
        reference_database_accession
        aligned_regions { entity_beg_seq_id ref_beg_seq_id length }
      }
      polymer_entity_instances {
        rcsb_polymer_entity_instance_container_identifiers {
          auth_asym_id auth_to_entity_poly_seq_mapping
        }
      }
    }
  }
}"""


def uniprot_to_auth(pdb_id: str, chain: str, uniprot: str,
                    timeout: float = 30.0) -> dict[int, int]:
    """
    Map UniProt residue numbers to author residue ids for one chain.

    Two hops, both from RCSB: the entity<->UniProt alignment gives
    UniProt -> entity_poly_seq index, and `auth_to_entity_poly_seq_mapping` gives
    entity index -> author id. Doing it by sequence alignment locally would be
    guesswork; this is the deposited correspondence.

    Returns {} when the entry has no alignment for that accession.
    """
    import requests

    try:
        resp = requests.post(RCSB_GRAPHQL_URL,
                             json={"query": _ALIGN_QUERY % pdb_id.upper()},
                             timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(f"RCSB alignment lookup failed for {pdb_id}: {exc}")
        return {}

    for pe in ((payload.get("data") or {}).get("entry") or {}).get(
            "polymer_entities") or []:
        auth_map: list[str] | None = None
        for inst in pe.get("polymer_entity_instances") or []:
            ids = inst.get(
                "rcsb_polymer_entity_instance_container_identifiers") or {}
            if ids.get("auth_asym_id") == chain:
                auth_map = ids.get("auth_to_entity_poly_seq_mapping")
                break
        if auth_map is None:
            continue

        for align in pe.get("rcsb_polymer_entity_align") or []:
            if (align.get("reference_database_accession") or "").upper() \
                    != uniprot.upper():
                continue
            out: dict[int, int] = {}
            for region in align.get("aligned_regions") or []:
                e_beg = region.get("entity_beg_seq_id")
                r_beg = region.get("ref_beg_seq_id")
                length = region.get("length")
                if None in (e_beg, r_beg, length):
                    continue
                for offset in range(int(length)):
                    idx = int(e_beg) + offset       # 1-based entity_poly_seq id
                    if idx - 1 >= len(auth_map):
                        break
                    raw = auth_map[idx - 1]
                    try:
                        out[int(r_beg) + offset] = int(raw)
                    except (TypeError, ValueError):
                        continue   # unobserved residues are null in the mapping
            if out:
                logger.debug(f"{pdb_id} chain {chain}: mapped {len(out)} residues "
                             f"to {uniprot}")
                return out
    return {}


# ----------------------------------------------------------------------
# The decision
# ----------------------------------------------------------------------

@dataclass
class TopologyRestriction:
    """Which author residues are designable, and why."""

    applies: bool
    side: str                                # extracellular | cytoplasmic | any
    allowed_auth: set[int] = field(default_factory=set)
    excluded_auth: set[int] = field(default_factory=set)
    n_transmembrane_excluded: int = 0
    note: str = ""

    def filter(self, auths: Iterable[int]) -> list[int]:
        if not self.applies:
            return sorted(set(auths))
        return sorted(a for a in set(auths) if a in self.allowed_auth)


def restriction_for(
    pdb_id: str,
    chain: str,
    uniprot: str,
    *,
    side: str = EXTRACELLULAR,
    topology: Topology | None = None,
) -> TopologyRestriction:
    """
    Author residues on the requested side of the membrane.

    `side="any"` disables the restriction — that is the escape hatch for an
    explicit "design against the intracellular part".

    A soluble protein, an unfetchable accession, or a construct that turns out to
    be entirely one side (an ectodomain-only crystal, which most are) all yield
    `applies=False`: there is nothing to exclude, and pretending otherwise would
    silently shrink the target.
    """
    if side == "any":
        return TopologyRestriction(False, side, note="topology restriction disabled")

    topo = topology or fetch_topology(uniprot)
    if not topo.fetched:
        return TopologyRestriction(False, side, note="topology unavailable")
    if not topo.is_membrane:
        return TopologyRestriction(
            False, side, note="soluble protein — no membrane restriction")

    mapping = uniprot_to_auth(pdb_id, chain, uniprot)
    if not mapping:
        return TopologyRestriction(
            False, side,
            note=f"no UniProt->author alignment for {pdb_id} chain {chain}; "
                 f"topology could not be applied")

    wanted = {a for pos, a in mapping.items() if topo.kind_at(pos) == side}
    # Only genuinely unreachable residues count as excluded. A signal peptide is
    # cleaved off in the mature protein and an unannotated stretch is unknown —
    # neither is a reason to shrink the target, and treating them as exclusions
    # would fire the restriction on ectodomain constructs that need none.
    #
    # TRANSMEMBRANE is excluded on BOTH sides, always. A TM helix is a solvent-
    # exposed hydrophobic slab in an isolated structure, so diffusion parks
    # binders on it preferentially: they score well on every hydrophobic-contact
    # metric and are physiologically impossible, because in a cell that surface
    # is buried in lipid. Leaving it in the target quietly turns a design run
    # into a membrane-mimetic binder generator.
    inaccessible = {TRANSMEMBRANE,
                    CYTOPLASMIC if side == EXTRACELLULAR else EXTRACELLULAR}
    other = {a for pos, a in mapping.items()
             if topo.kind_at(pos) in inaccessible}
    # Residues we cannot place are kept: excluding the unknown is not a decision
    # the topology supports.
    wanted |= {a for pos, a in mapping.items()
               if topo.kind_at(pos) not in inaccessible and a not in wanted}
    if not other:
        return TopologyRestriction(
            False, side,
            note=f"the modelled construct is entirely {side} "
                 f"({len(wanted)} residues) — no restriction needed")
    if not wanted:
        return TopologyRestriction(
            False, side,
            note=f"WARNING: no {side} residues are modelled in {pdb_id} chain "
                 f"{chain}; the restriction was NOT applied because it would "
                 f"leave nothing to design against")

    kinds = sorted({topo.kind_at(p) for p, a in mapping.items() if a in other}
                   - {None})
    n_tm = sum(1 for p, a in mapping.items()
               if a in other and topo.kind_at(p) == TRANSMEMBRANE)
    if n_tm:
        logger.warning(
            f"{pdb_id} chain {chain}: dropping {n_tm} transmembrane residue(s) "
            f"from the design target — an exposed TM helix is a hydrophobic slab "
            f"that preferentially attracts binders which cannot work in a membrane")
    return TopologyRestriction(
        True, side, allowed_auth=wanted, excluded_auth=other,
        n_transmembrane_excluded=n_tm,
        note=(f"restricted to the {side} region: {len(wanted)} residue(s) kept, "
              f"{len(other)} excluded as {'/'.join(kinds)}"
              + (f" (including {n_tm} transmembrane)" if n_tm else "")))


def check_hotspots(hotspots: Sequence[dict], restriction: TopologyRestriction,
                   topo: Topology, mapping: dict[int, int] | None = None
                   ) -> tuple[list[dict], list[str]]:
    """
    Flag hotspots that sit outside the designable side of the membrane.

    A hotspot in a transmembrane helix means the interface that was picked is the
    membrane-embedded one — the campaign would run to completion and produce
    binders for a surface that is lipid in vivo. That is a stop, not a warning.

    Returns (offending hotspots, human-readable reasons).
    """
    if not restriction.applies:
        return [], []
    auth_to_kind: dict[int, str] = {}
    for pos, auth in (mapping or {}).items():
        kind = topo.kind_at(pos)
        if kind:
            auth_to_kind[auth] = kind

    bad, reasons = [], []
    for h in hotspots:
        auth = h.get("auth_seq_id")
        if auth is None or int(auth) in restriction.allowed_auth:
            continue
        kind = auth_to_kind.get(int(auth), "outside the designable region")
        bad.append(h)
        reasons.append(
            f"hotspot {h.get('residue', '')}{auth} is {kind}"
            + (" — an exposed TM helix attracts binders that cannot work in a "
               "membrane" if kind == TRANSMEMBRANE else ""))
    return bad, reasons


def summarise(pdb_id: str, chain: str, uniprot: str,
              side: str = EXTRACELLULAR) -> dict:
    """Topology + restriction as a JSON-able block for a stage report."""
    topo = fetch_topology(uniprot)
    restriction = restriction_for(pdb_id, chain, uniprot, side=side, topology=topo)
    return {
        "topology": topo.as_dict(),
        "restriction": {
            "applies": restriction.applies,
            "side": restriction.side,
            "n_allowed": len(restriction.allowed_auth),
            "n_excluded": len(restriction.excluded_auth),
            "note": restriction.note,
        },
    }
