"""Build and validate a BoltzGen design YAML, deterministically.

The BoltzGen counterpart of :mod:`src.foundry_spec`, and the same division of
labour: this module turns a chosen epitope plus a trim decision into the one
input file the generator reads, and refuses rather than guessing when the
inputs cannot be expressed. Today that YAML is written by the
``protein-design-script`` LLM skill; every field here is a mechanical fact
about one structure file, which is the wrong job for a model and the source of
two expensive silent failures (see ``resolve_binding`` and
``safe_binder_range``).

## One numbering, and it is the deposited ``label_seq``

BoltzGen indexes both ``binding:`` and ``res_index:`` on the deposited mmCIF
``label_seq`` (``mmcif.py``: ``res_idx = res.label_seq - 1``), 1-based. For a
PDB file, which carries no ``label_seq``, it synthesises one as the position
among that chain's MODELLED residues -- so the two file formats disagree
whenever ``entity_poly_seq`` starts before the first modelled residue, and a
``binding:`` list is only ever meaningful relative to ONE file.
:func:`src.structure_tools.boltzgen_residue_indices` reproduces both branches;
nothing here counts residues itself.

Verified empirically against ``boltzgen check``: with ``res_index: 53..69`` and
``binding: 53,62,63,64`` on 3N7S chain D, the visualisation CIF marks exactly
labels 53/62/63/64. So **``binding:`` is absolute, not renumbered relative to
the crop** -- one space for both fields.

## Why the trim is expressed as ``res_index``, not as a trimmed file

``structure_trim.write_trimmed`` produces a CIF that BoltzGen CANNOT PARSE:
gemmi's ``make_mmcif_document()`` emits ``_entity_poly`` but no
``_entity_poly_seq`` loop, and BoltzGen's parser requires it
(``RuntimeError: _entity_poly_seq.entity_id not found in block``). The sibling
``trimmed.pdb`` parses, but then ``binding:`` means the position among the
TRIMMED chain's modelled residues -- a third numbering, neither the deposited
``label_seq`` nor the author ids the hotspot table carries. Measured on
``projects/il7ra_e2e``: auth 58/77/138 are labels 62/81/142 in the deposited
CIF and indices 33/52/113 in ``trimmed.pdb``.

Pointing at the DEPOSITED file and expressing the trim as ``res_index`` keeps
exactly one numbering in play, needs no change to ``write_trimmed``, and lets
the trim stage keep doing what it is good at (segmentation, TM stripping, the
BSA and exposed-hydrophobic refusals) while contributing only
``kept_segments``.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from loguru import logger

#: BoltzGen normalises its OUTPUT chain ids: target -> A, design -> B, whatever
#: the input letters were. Measured: 3N7S chain D (84 aa, auth 27-110) comes
#: back as chain A (auth 6-89). The mirror image of RFD3's binder-A/target-B
#: convention, and the reason `pipeline_runner._boltzgen_output_chains` grounds
#: the output chains rather than trusting the input letters.
BOLTZGEN_OUT_TARGET_CHAIN = "A"
BOLTZGEN_OUT_BINDER_CHAIN = "B"

#: Binder length ranges by modality, mirroring
#: `config.yaml design.constraints.binder_sizes`. A caller normally passes its
#: own; these are the fallback and the source of `safe_binder_range`'s window.
DEFAULT_SIZES: dict[str, tuple[int, int]] = {
    "cyclic_peptide": (12, 15),
    "mini_protein": (70, 86),
}

#: Modality -> BoltzGen protocol, mirroring
#: `PipelineRunner._MODALITY_TO_PROTOCOL`. Only these two are reachable from
#: LPT; BoltzGen also ships protein-small_molecule / antibody / nanobody /
#: protein-redesign protocols that nothing here selects.
PROTOCOL_BY_MODALITY: dict[str, str] = {
    "cyclic_peptide": "peptide-anything",
    "mini_protein": "protein-anything",
}


class SpecError(RuntimeError):
    """A spec that cannot be built or does not describe what was asked for."""


@dataclass(frozen=True)
class BoltzGenSpec:
    path: Path
    name: str
    modality: str
    protocol: str
    #: The file named in `path:` — the one `binding` and `res_index` index into.
    structure_path: Path
    #: INPUT chain ids. BoltzGen renames both in its output; see the constants.
    target_chain: str
    binder_chain: str
    #: Deposited `label_seq` values, 1-based and absolute.
    binding: tuple[int, ...]
    #: `(lo, hi)` label_seq spans kept from the target, or None for whole chain.
    res_index: tuple[tuple[int, int], ...] | None
    binder_min: int
    binder_max: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The three refusals
# ---------------------------------------------------------------------------

def safe_binder_range(modality: str, target_len: int,
                      sizes: dict[str, tuple[int, int]] | None = None,
                      ) -> tuple[int, int, str]:
    """`(lo, hi, note)` — the binder length window, with one length excluded.

    **A binder whose residue count exactly equals the target chain's kills the
    whole campaign.** BoltzGen's `design` step writes both chains correctly;
    `inverse_folding` then drops the binder chain from the CIF while leaving its
    `.npz` design_mask at the full token count; and `folding` dies in
    `data_from_generated.get_feat` with

        IndexError: boolean index did not match indexed array along axis 0;
        size of axis is 84 but size of corresponding boolean axis is 168

    It aborts the ENTIRE run, not the one design, two steps after the design
    that caused it, naming neither. Measured on RAMP1 (3N7S chain D, 84
    residues) with a 70..86 window: 2 of 24 designs sampled 84 and both failed;
    all 22 at other lengths were clean. Incidence is ~1/(hi-lo+1) per design, so
    it is a near-certainty at campaign scale and invisible on a small probe
    unless it happens to sample the number.

    Upstream bug, so the fix is to never sample the length: the window is
    narrowed from whichever end loses fewer lengths. A target outside the window
    needs no adjustment.
    """
    sizes = sizes or DEFAULT_SIZES
    if modality not in sizes:
        raise SpecError(f"unknown modality {modality!r}; expected one of "
                        f"{sorted(sizes)}")
    lo, hi = sizes[modality]
    if not lo <= target_len <= hi:
        return lo, hi, ""
    drop_low = target_len - lo + 1
    drop_high = hi - target_len + 1
    if drop_high <= drop_low:
        new_lo, new_hi = lo, target_len - 1
    else:
        new_lo, new_hi = target_len + 1, hi
    if new_lo > new_hi:
        raise SpecError(
            f"the target chain is {target_len} residues and the {modality} "
            f"window is {lo}..{hi}; excluding the fatal equal-length case "
            f"leaves nothing. Pick a different modality or target.")
    note = (f"binder window narrowed {lo}..{hi} -> {new_lo}..{new_hi} to exclude "
            f"length {target_len}, which equals the target chain and makes "
            f"BoltzGen's inverse_folding step drop the binder (aborts the run)")
    return new_lo, new_hi, note


def free_chain_id(structure_path: Path, prefer: str = "B") -> str:
    """The first uppercase letter no chain in `structure_path` uses.

    BoltzGen does not reject a chain-id collision: ``Structure.concatenate``
    renames one entity to the first free letter and only LOGS it, and which
    entity loses its letter depends on YAML entity order. With the skill
    template's ordering (file first) a chain-B target keeps B and the BINDER
    silently becomes A — after which anything matching the binder by id finds
    no atoms. So the fix is to not collide.
    """
    import gemmi

    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    used = {c.name.strip().upper() for c in st[0]}
    for letter in (prefer, *string.ascii_uppercase):
        if letter and letter not in used:
            return letter
    raise SpecError(f"{structure_path.name} uses every chain letter A-Z")


def resolve_binding(structure_path: Path, chain: str,
                    hotspots: Sequence[dict]) -> tuple[list[int], list[str]]:
    """`(label_seq indices, notes)` for `hotspots`, verified by residue NAME.

    A consistent list can still address the wrong residues. Measured on
    ``projects/il7ra_e2e``, whose interface report stored
    ``label_seq_id = auth - 16`` for every row — the constant-offset signature
    of a counted column rather than a looked-up one — which selects ALA where
    VAL was intended. So every index is cross-checked against the name of the
    residue actually at that position, and a mismatch is a hard failure. Same
    posture as ``_verify_hotspot_grounding``: an unverifiable numbering is not a
    warning.
    """
    import gemmi

    from src.structure_tools import boltzgen_residue_indices

    auth_ids = [int(h["auth_seq_id"]) for h in hotspots]
    if not auth_ids:
        raise SpecError("no hotspots given; a spec with no binding constraint "
                        "designs against the whole target surface")
    idx = boltzgen_residue_indices(str(structure_path), chain, auth_ids)

    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    chains = [c for c in st[0] if c.name == chain]
    if not chains:
        raise SpecError(f"{structure_path.name} has no chain {chain!r} "
                        f"(has {sorted(c.name for c in st[0])})")
    by_auth = {r.seqid.num: r.name.upper() for r in chains[0]}

    binding, notes, bad = [], [], []
    for hs in hotspots:
        auth = int(hs["auth_seq_id"])
        want = str(hs.get("residue") or "").upper()
        got, i = by_auth.get(auth), idx.get(auth)
        if i is None or got is None:
            bad.append(f"{want or '?'}{auth} (not in the file)")
            continue
        if want and got != want:
            bad.append(f"expected {want}{auth}, file has {got}{auth}")
            continue
        binding.append(int(i))
        notes.append(f"{got}{auth} -> binding index {i}")
    if bad:
        raise SpecError(
            f"hotspot grounding failed against {structure_path.name} chain "
            f"{chain}: " + "; ".join(bad) + ". The numbering does not belong to "
            f"this file — recompute it against the path that will be designed.")
    if len(set(binding)) != len(binding):
        raise SpecError(f"duplicate binding indices {binding}")
    return binding, notes


# ---------------------------------------------------------------------------
# Trim -> res_index
# ---------------------------------------------------------------------------

def kept_segments_to_res_index(
    structure_path: Path, chain: str,
    kept_segments: Sequence[Sequence[int]],
) -> tuple[tuple[int, int], ...]:
    """Author-numbered `kept_segments` as absolute `label_seq` spans.

    The trim decides in author numbering (so hotspot ids stay valid) while
    BoltzGen indexes `res_index` on `label_seq`, so the spans have to be
    translated through the file's own map rather than offset by a constant --
    an author range is not contiguous in label space whenever the deposited
    entity has unmodelled residues inside it.

    Emits one span per contiguous run of label indices, which is why a
    three-segment author trim can come back as more or fewer spans.
    """
    from src.structure_tools import boltzgen_residue_indices

    wanted: list[int] = []
    for seg in kept_segments:
        lo, hi = int(seg[0]), int(seg[1])
        wanted.extend(range(lo, hi + 1))
    idx = boltzgen_residue_indices(str(structure_path), chain, wanted)
    labels = sorted({v for v in idx.values() if v is not None})
    if not labels:
        raise SpecError(
            f"none of the kept segments {list(map(list, kept_segments))} "
            f"resolve to residues on chain {chain} of {structure_path.name}")

    spans: list[tuple[int, int]] = []
    start = prev = labels[0]
    for v in labels[1:]:
        if v == prev + 1:
            prev = v
            continue
        spans.append((start, prev))
        start = prev = v
    spans.append((start, prev))
    return tuple(spans)


# ---------------------------------------------------------------------------
# Build + validate
# ---------------------------------------------------------------------------

def _render(spec: BoltzGenSpec, notes: Sequence[str]) -> str:
    lines = [
        "# Generated by src/boltzgen_spec.py — no LLM in the loop.",
        f"# target: {spec.structure_path.name} chain {spec.target_chain}",
        f"# modality: {spec.modality}   protocol: {spec.protocol}",
        f"# binder chain: {spec.binder_chain} "
        f"(first letter free in the target file)",
        "#",
        "# NOTE BoltzGen renames both chains in its OUTPUT: target -> "
        f"{BOLTZGEN_OUT_TARGET_CHAIN}, design -> {BOLTZGEN_OUT_BINDER_CHAIN},",
        "# and rewrites the target's numbering so label_seq becomes auth_seq_id.",
        "#",
        "# binding: indices are the deposited mmCIF label_seq of THIS file and",
        "# are valid only for it. Each was cross-checked by residue name:",
    ]
    lines += [f"#   {n}" for n in notes]
    for w in spec.warnings:
        lines.append(f"# WARNING {w}")
    lines += [
        "",
        "entities:",
        "  - file:",
        f"      path: {spec.structure_path}",
        "      include:",
        "        - chain:",
        f"            id: {spec.target_chain}",
    ]
    if spec.res_index:
        spans = ",".join(f"{lo}..{hi}" for lo, hi in spec.res_index)
        lines.append(f"            res_index: {spans}")
    lines += [
        "      binding_types:",
        "        - chain:",
        f"            id: {spec.target_chain}",
        f"            binding: {','.join(str(b) for b in spec.binding)}",
        "",
        "  - protein:",
        f"      id: {spec.binder_chain}",
        f"      sequence: {spec.binder_min}..{spec.binder_max}",
    ]
    if spec.modality == "cyclic_peptide":
        lines.append("      cyclic: True")
    return "\n".join(lines) + "\n"


def build_boltzgen_spec(
    *,
    name: str,
    structure_path: Path,
    target_chain: str,
    hotspots: Sequence[dict],
    out_path: Path,
    modality: str,
    kept_segments: Sequence[Sequence[int]] | None = None,
    binder_chain: str | None = None,
    sizes: dict[str, tuple[int, int]] | None = None,
) -> BoltzGenSpec:
    """Write a BoltzGen YAML for one epitope and return what it describes.

    `structure_path` must be the DEPOSITED file, not a trimmed one — see the
    module docstring. `kept_segments` is the trim's author-numbered decision and
    becomes `res_index`; omit it to design against the whole chain.
    """
    import gemmi

    structure_path = Path(structure_path)
    if not structure_path.is_file():
        raise SpecError(f"no such structure: {structure_path}")
    if modality not in PROTOCOL_BY_MODALITY:
        raise SpecError(f"unknown modality {modality!r}")

    binding, notes = resolve_binding(structure_path, target_chain, hotspots)
    res_index = (kept_segments_to_res_index(structure_path, target_chain,
                                            kept_segments)
                 if kept_segments else None)

    # The binder-length window is chosen against the DESIGNABLE target length —
    # what res_index actually keeps — because that is the chain BoltzGen builds
    # against and therefore the length the equal-length crash keys on.
    if res_index:
        target_len = sum(hi - lo + 1 for lo, hi in res_index)
    else:
        st = gemmi.read_structure(str(structure_path))
        st.setup_entities()
        target_len = sum(1 for c in st[0] if c.name == target_chain
                         for r in c if r.find_atom("CA", "*"))
    lo, hi, range_note = safe_binder_range(modality, target_len, sizes)

    warnings = [range_note] if range_note else []
    spec = BoltzGenSpec(
        path=Path(out_path), name=name, modality=modality,
        protocol=PROTOCOL_BY_MODALITY[modality],
        structure_path=structure_path.resolve(),
        target_chain=target_chain,
        binder_chain=binder_chain or free_chain_id(structure_path),
        binding=tuple(binding), res_index=res_index,
        binder_min=lo, binder_max=hi, warnings=tuple(warnings),
    )
    validate_spec(spec)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(_render(spec, notes), encoding="utf-8")
    for w in spec.warnings:
        logger.warning(f"  boltzgen spec: {w}")
    logger.info(
        f"  boltzgen spec -> {out_path} "
        f"({len(spec.binding)} hotspots, binder {lo}..{hi} on chain "
        f"{spec.binder_chain}"
        + (f", {len(res_index)} target span(s)" if res_index else ", whole chain")
        + ")")
    return spec


def validate_spec(spec: BoltzGenSpec) -> None:
    """Refuse a spec that does not describe what the caller asked for.

    The load-bearing check is the last one. **A binding index outside the
    `res_index` crop is silently ignored** — verified against `boltzgen check`,
    which accepted `binding: 1` with `res_index: 53..69` and simply marked
    nothing, producing a campaign with no binding constraint at all and no
    error anywhere. That is the same class of failure as every other item in
    this module: a run that completes, costs GPU hours, and answers a different
    question than the one asked.
    """
    if not spec.binding:
        raise SpecError("spec has no binding indices")
    if any(b < 1 for b in spec.binding):
        raise SpecError(f"binding indices are 1-based; got {spec.binding}")
    if spec.binder_min > spec.binder_max:
        raise SpecError(f"binder window {spec.binder_min}..{spec.binder_max} "
                        f"is empty")
    if spec.binder_chain == spec.target_chain:
        raise SpecError(
            f"binder chain {spec.binder_chain} collides with the target chain; "
            f"BoltzGen would rename one of them silently")
    if spec.res_index:
        covered = set()
        for lo, hi in spec.res_index:
            if lo > hi:
                raise SpecError(f"res_index span {lo}..{hi} is inverted")
            covered.update(range(lo, hi + 1))
        outside = sorted(b for b in spec.binding if b not in covered)
        if outside:
            spans = ",".join(f"{lo}..{hi}" for lo, hi in spec.res_index)
            raise SpecError(
                f"binding indices {outside} fall outside res_index {spans}, so "
                f"BoltzGen would silently ignore them and design against an "
                f"unconstrained surface. The trim removed residues the epitope "
                f"needs.")
        target_len = len(covered)
        if spec.binder_min <= target_len <= spec.binder_max:
            raise SpecError(
                f"the binder window {spec.binder_min}..{spec.binder_max} "
                f"contains the designable target length {target_len}; a binder "
                f"of exactly that length makes inverse_folding drop the binder "
                f"chain and aborts the run (see safe_binder_range)")
