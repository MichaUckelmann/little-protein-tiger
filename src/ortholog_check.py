"""
Is this chain the human target, an ortholog of it, or a different molecule?

Three outcomes, and the pipeline must treat them differently:

- **match** — the chain IS the human protein. Nothing to do.
- **mismatch** — the chain is some other molecule (the PD-L1 incident: a
  target chain assigned to the anti-PD-L1 VHH). Campaign-burning; hard-fail.
- **ortholog** — the chain is the same protein from another organism. 5GRS is
  the *S. pombe* SREBP-SCAP complex; every chain sits at 27-50% identity to
  human SCAP/SREBF1. Sequence identity ALONE cannot tell this apart from
  `mismatch` — an unrelated nanobody scores in the same band by chance — so
  the discriminator is the entity's own metadata: RCSB's description names the
  protein, and its source organism is not human.

An ortholog is not automatically wrong. A target site conserved between the
ortholog and the human protein is a legitimate thing to design against, and it
is often the only structure that exists: no human SCAP/SREBP complex has ever
been solved. What makes it wrong is designing against a site that ISN'T
conserved — the binder is then optimised for residues the human protein does
not have. So an ortholog routes to :func:`hotspot_conservation`, which aligns
the two sequences and reports, per hotspot, whether the human protein carries
the same residue.

The human AlphaFold model (:func:`fetch_alphafold_model`) is the other half of
the answer: with the epitope's human numbering in hand from the same alignment,
a campaign can be re-pointed at the human monomer instead of the ortholog
crystal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

ALPHAFOLD_URL = "https://alphafold.ebi.ac.uk/files/AF-{acc}-F1-model_v{ver}.cif"
ALPHAFOLD_API = "https://alphafold.ebi.ac.uk/api/prediction/{acc}"
HUMAN_TAXID = 9606

#: Above this, the chain is the target itself and no ortholog reasoning applies.
SAME_PROTEIN_IDENTITY = 0.80
#: Below this, even a name match is not enough to call it an ortholog — a
#: 10%-identity hit against a 1200-residue reference is noise, and RCSB
#: descriptions are free text that can name a binding partner in passing.
MIN_ORTHOLOG_IDENTITY = 0.20
#: Fraction of declared hotspots that must be identical in the human protein
#: for an ortholog-derived epitope to be worth designing against.
MIN_HOTSPOT_CONSERVATION = 0.60

MATCH = "match"
ORTHOLOG = "ortholog"
MISMATCH = "mismatch"
UNKNOWN = "unknown"

# Biopython one-letter codes, grouped so a substitution can be called
# conservative rather than simply "different".
_SIMILAR_GROUPS = (
    "AGSTP", "ILVMC", "FYW", "DENQ", "KRH",
)


def _similar(a: str, b: str) -> bool:
    return any(a in g and b in g for g in _SIMILAR_GROUPS)


@dataclass
class ChainVerdict:
    """What a structure chain actually is, relative to the intended target."""

    verdict: str = UNKNOWN            # match | ortholog | mismatch | unknown
    identity: float | None = None     # local-alignment identity to the reference
    organism: str = ""                # source organism, scientific name
    taxid: int | None = None
    description: str = ""             # RCSB entity description
    chain_uniprot: str = ""           # the chain's OWN accession (SIFTS)
    reason: str = ""

    @property
    def is_ortholog(self) -> bool:
        return self.verdict == ORTHOLOG


@dataclass
class HotspotConservation:
    """Per-hotspot conservation of an ortholog epitope in the human protein."""

    rows: list[dict] = field(default_factory=list)
    conserved: int = 0
    similar: int = 0
    different: int = 0
    unaligned: int = 0
    method: str = ""
    #: True when the mapping came from a fragment alignment rather than SIFTS.
    low_confidence: bool = False

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def fraction_conserved(self) -> float:
        return self.conserved / self.total if self.total else 0.0

    @property
    def fraction_conserved_or_similar(self) -> float:
        return ((self.conserved + self.similar) / self.total) if self.total else 0.0

    def summary(self) -> str:
        return (f"{self.conserved}/{self.total} hotspots identical in the human "
                f"protein ({self.fraction_conserved:.0%}), {self.similar} "
                f"conservatively substituted, {self.different} different, "
                f"{self.unaligned} not alignable")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def uniprot_entry(accession: str, timeout: float = 20.0) -> dict:
    """
    `{taxid, organism, gene, full_name}` for an accession, or `{}`.

    `full_name` is UniProt's recommended protein name, and it is the signal
    that identifies an ortholog: O43043 (*S. pombe* Scp1) and Q12770 (human
    SCAP) carry the byte-identical name "Sterol regulatory element-binding
    protein cleavage-activating protein", while their gene symbols (`scp1`
    vs `SCAP`) and their sequences (29% identity) do not obviously agree.
    An RCSB entity description is free text and often spells the name out in
    full, so it is checked against this too.
    """
    import requests

    acc = str(accession).upper().strip()
    if not acc:
        return {}
    url = (f"https://rest.uniprot.org/uniprotkb/{acc}.json"
           f"?fields=protein_name,gene_names,organism_name")
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        d = resp.json()
    except Exception as exc:
        logger.warning(f"UniProt lookup failed for {acc}: {exc}")
        return {}
    org = d.get("organism") or {}
    desc = d.get("proteinDescription") or {}
    rec = (desc.get("recommendedName") or {}).get("fullName") or {}
    if not rec.get("value"):
        subs = desc.get("submissionNames") or []
        rec = (subs[0].get("fullName") if subs else {}) or {}
    genes = [(g.get("geneName") or {}).get("value")
             for g in (d.get("genes") or [])]
    return {
        "accession": acc,
        "taxid": org.get("taxonId"),
        "organism": org.get("scientificName") or "",
        "gene": next((g for g in genes if g), ""),
        "full_name": rec.get("value") or "",
    }


def _same_protein_name(a: str, b: str) -> bool:
    """
    Do two protein names denote the same molecule? Punctuation-insensitive.

    Hyphens become spaces rather than vanishing, so "Caspase-2" and "Caspase 2"
    agree while "Caspase-2" and "Caspase-6" still do not — deleting the
    separator outright would collapse both to `caspase2`/`caspase6`, which
    happens to work, and to `caspase2` vs `caspase 2`, which does not.

    A containment match is allowed for the common case where one source states
    a longer form of the same name, but only when the shorter name is most of
    the longer one. Without that ratio, the bare word "Caspase" would match
    every caspase in the PDB.
    """
    def norm(s: str) -> str:
        cleaned = "".join(c if (c.isalnum() or c.isspace()) else " "
                          for c in (s or "").lower())
        return " ".join(cleaned.split())

    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return short in long_ and len(short) >= 0.6 * len(long_)


def classify_chain(
    *,
    identity: float | None,
    description: str = "",
    gene: str = "",
    uniprot: str = "",
    chain_accessions: list[str] | None = None,
) -> ChainVerdict:
    """
    Decide whether a chain is the target, an ortholog of it, or something else.

    `identity` is local-alignment identity of the chain's MODELLED residues
    against the human reference sequence (`structure_tools.sequence_identity`),
    or None when no sequence comparison was possible. `chain_accessions` are
    the chain's own SIFTS accessions, from `target_resolve.entry_metadata`.

    Identity alone cannot separate `ortholog` from `mismatch` — a *S. pombe*
    ortholog and an unrelated nanobody both land in the 20-40% band. The
    discriminator is the NAME: the chain's own UniProt entry is fetched and its
    recommended protein name compared to the human target's. Same name,
    different organism, homologous sequence — that is an ortholog. A different
    name is a mismatch however high the identity looks.
    """
    v = ChainVerdict(identity=identity, description=description)

    accs = [str(a).upper() for a in (chain_accessions or []) if a]
    if uniprot and uniprot.upper() in accs:
        # SIFTS maps this chain to the human accession itself; whatever the
        # modelled fragment's identity, this is the protein.
        v.verdict = MATCH
        v.reason = f"SIFTS maps chain to {uniprot}"
        return v
    if identity is not None and identity >= SAME_PROTEIN_IDENTITY:
        v.verdict = MATCH
        v.reason = f"{identity:.0%} identical to {uniprot or gene}"
        return v
    if identity is None:
        v.verdict = UNKNOWN
        v.reason = "no sequence comparison was possible"
        return v

    human = uniprot_entry(uniprot) if uniprot else {}
    ref_name = human.get("full_name") or ""
    for acc in accs:
        chain_entry = uniprot_entry(acc)
        if not chain_entry:
            continue
        v.organism = chain_entry.get("organism") or v.organism
        v.taxid = chain_entry.get("taxid") or v.taxid
        v.chain_uniprot = acc
        name_hit = (
            _same_protein_name(chain_entry.get("full_name", ""), ref_name)
            or _same_protein_name(chain_entry.get("full_name", ""), description)
            or (bool(gene) and gene.upper() == (chain_entry.get("gene") or "").upper())
        )
        if name_hit and identity >= MIN_ORTHOLOG_IDENTITY:
            v.verdict = ORTHOLOG
            v.reason = (
                f"chain maps to {acc} ({chain_entry.get('full_name')!r}, "
                f"{chain_entry.get('organism')}, taxid {chain_entry.get('taxid')}) "
                f"— the same protein as human {gene or uniprot} in another "
                f"organism, {identity:.0%} identical"
            )
            return v

    # No accession settled it. Fall back to the RCSB description, which for many
    # entries spells the protein's full name out.
    if ref_name and _same_protein_name(description, ref_name) \
            and identity >= MIN_ORTHOLOG_IDENTITY:
        v.verdict = ORTHOLOG
        v.reason = (
            f"RCSB describes this chain as {description!r}, which is the same "
            f"protein name as human {gene or uniprot}, at {identity:.0%} "
            f"identity — an ortholog, not the human protein"
        )
        return v

    v.verdict = MISMATCH
    v.reason = (
        f"{identity:.0%} identical to human {gene or uniprot}"
        + (f", and RCSB describes it as {description!r}" if description else "")
        + " — this is a different molecule, not an ortholog"
    )
    return v


# --------------------------------------------------------------------------
# Conservation of a declared epitope
# --------------------------------------------------------------------------

def hotspot_conservation(
    pdb_id: str,
    chain: str,
    hotspots: list[dict],
    *,
    human_uniprot: str,
    chain_uniprot: str = "",
    chain_sequence: str = "",
    auth_to_string_idx: dict | None = None,
) -> HotspotConservation:
    """
    For each declared hotspot on an ortholog chain, what does the human
    protein carry at the equivalent position?

    Two hops, and the first one matters more than it looks. The hotspots are
    in the structure's author numbering; going straight from a MODELLED
    FRAGMENT to a full-length human sequence by pairwise alignment is
    unreliable at ortholog-level identity — measured on 5GRS chain A vs human
    SCAP, a local alignment covers one propeller blade and leaves every
    hotspot unmapped, while a global one with free reference end-gaps smears a
    375-residue domain across all 1279 residues and maps Trp568 onto Met1.

    So instead: author id -> the CHAIN's own UniProt position, using RCSB's
    deposited SIFTS correspondence (`membrane_topology.uniprot_to_auth`, not
    guesswork), then ortholog full-length -> human full-length by global
    alignment. Two genuine full-length orthologs align reliably where a domain
    fragment against a multi-domain parent does not.

    Falls back to fragment-vs-human local alignment only when the chain has no
    SIFTS accession; rows it cannot place are reported as `unaligned` and count
    AGAINST conservation rather than being quietly dropped.

    Each row carries `human_auth`, the 1-based position in the human canonical
    sequence, so an epitope found on an ortholog crystal can be restated in
    human numbering — which is what makes a human AlphaFold model usable as the
    design target.
    """
    from src.target_resolve import fetch_uniprot_sequence

    out = HotspotConservation()
    if not hotspots or not human_uniprot:
        return out
    human_sequence = fetch_uniprot_sequence(human_uniprot)
    if not human_sequence:
        logger.warning(f"no UniProt sequence for {human_uniprot}; "
                       f"cannot check ortholog conservation")
        return out

    auth_to_ref: dict[int, int] = {}      # author id -> 0-based index in `src_seq`
    src_seq = ""
    if chain_uniprot:
        from src.membrane_topology import uniprot_to_auth

        sifts = uniprot_to_auth(pdb_id, chain, chain_uniprot)
        src_seq = fetch_uniprot_sequence(chain_uniprot) or ""
        if sifts and src_seq:
            auth_to_ref = {auth: pos - 1 for pos, auth in sifts.items()}
            out.method = f"SIFTS {chain_uniprot} -> global alignment to {human_uniprot}"
    if not auth_to_ref:
        if not chain_sequence:
            logger.warning(
                f"no SIFTS accession and no chain sequence for {pdb_id} chain "
                f"{chain}; cannot check ortholog conservation")
            return out
        src_seq = chain_sequence
        auth_to_ref = {int(k): int(v)
                       for k, v in (auth_to_string_idx or {}).items()}
        out.method = "modelled fragment -> local alignment (no SIFTS accession)"
        out.low_confidence = True

    idx_map = _aligned_index_map(src_seq, human_sequence,
                                 local=out.low_confidence)

    for h in hotspots:
        try:
            auth = int(h.get("auth_seq_id"))
        except (TypeError, ValueError):
            continue
        si = auth_to_ref.get(auth)
        row = {
            "auth_seq_id": auth,
            "residue": str(h.get("residue", "")).upper(),
            "ortholog_aa": (src_seq[si] if si is not None and si < len(src_seq)
                            else ""),
            "human_aa": "",
            "human_auth": None,
            "status": "unaligned",
        }
        hi = idx_map.get(si) if si is not None else None
        if hi is None or hi >= len(human_sequence):
            out.unaligned += 1
        else:
            human_aa = human_sequence[hi]
            row["human_aa"] = human_aa
            row["human_auth"] = hi + 1
            if human_aa == row["ortholog_aa"]:
                row["status"] = "conserved"
                out.conserved += 1
            elif _similar(human_aa, row["ortholog_aa"]):
                row["status"] = "similar"
                out.similar += 1
            else:
                row["status"] = "different"
                out.different += 1
        out.rows.append(row)
    return out


def _aligned_index_map(a: str, b: str, *, local: bool = False) -> dict[int, int]:
    """
    `{index into a -> index into b}` for the best alignment.

    Global by default: `a` is a full-length ortholog sequence and `b` the
    full-length human one, so the whole of both should be accounted for.
    `local=True` is the degraded path for a modelled fragment with no SIFTS
    accession, where anchoring the ends would be meaningless.
    """
    from Bio.Align import PairwiseAligner, substitution_matrices

    aligner = PairwiseAligner()
    aligner.mode = "local" if local else "global"
    aligner.substitution_matrix = substitution_matrices.load("BLOSUM62")
    aligner.open_gap_score = -11
    aligner.extend_gap_score = -1
    try:
        alignments = aligner.align(a, b)
        aln = alignments[0]
    except Exception as exc:                       # pragma: no cover - defensive
        logger.warning(f"ortholog alignment failed: {exc}")
        return {}
    mapping: dict[int, int] = {}
    # `aligned` is ((a_start, a_end), ...), ((b_start, b_end), ...) per block.
    for (a0, a1), (b0, b1) in zip(aln.aligned[0], aln.aligned[1]):
        for off in range(int(a1) - int(a0)):
            mapping[int(a0) + off] = int(b0) + off
    return mapping


# --------------------------------------------------------------------------
# AlphaFold
# --------------------------------------------------------------------------

def fetch_alphafold_model(
    accession: str,
    out_dir: Path | str,
    *,
    versions: tuple[int, ...] = (6, 5, 4, 3, 2),
    timeout: float = 60.0,
) -> Path | None:
    """
    Download the AlphaFold DB model for a UniProt accession, as mmCIF.

    The API endpoint is asked first, because it names the current file URL and
    so cannot go stale. The direct per-version URLs are the fallback for when
    the API is unreachable — and the version list is descending for a reason:
    AFDB currently serves **v6 only**, and a build that hardcoded `_v4` (the
    long-lived previous version, and the one every tutorial still shows)
    returns 404 for every accession and looks exactly like "AlphaFold has no
    model for this protein".

    Returns the local path, or None when AFDB genuinely has no model. The file
    is written as `AF-<ACC>-F1.cif` and is a no-op if already present, so this
    is safe to call on every resume.
    """
    import requests

    acc = str(accession).upper().strip()
    if not acc:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"AF-{acc}-F1.cif"
    if dest.exists() and dest.stat().st_size > 0:
        logger.debug(f"AlphaFold model already on disk: {dest}")
        return dest

    urls: list[str] = []
    try:
        api = requests.get(ALPHAFOLD_API.format(acc=acc), timeout=timeout)
        if api.status_code == 200 and api.content:
            for entry in api.json() or []:
                if entry.get("cifUrl"):
                    urls.append(entry["cifUrl"])
    except Exception as exc:
        logger.debug(f"AlphaFold API lookup failed for {acc}: {exc}")
    urls += [ALPHAFOLD_URL.format(acc=acc, ver=v) for v in versions]

    for url in urls:
        try:
            resp = requests.get(url, timeout=timeout)
        except Exception as exc:
            logger.warning(f"AlphaFold fetch failed for {acc}: {exc}")
            continue
        if resp.status_code == 404:
            continue
        if resp.status_code != 200 or not resp.content:
            logger.warning(
                f"AlphaFold returned HTTP {resp.status_code} for {acc}")
            continue
        dest.write_bytes(resp.content)
        logger.info(f"AlphaFold model for {acc} -> {dest} ({url.rsplit('/', 1)[-1]})")
        return dest

    logger.warning(f"AlphaFold DB has no model for {acc}")
    return None
