"""
Emit the foundry job specs for a binder campaign: RFD3 design + solubleMPNN.

Purely deterministic — no LLM. Everything here is derived from the trim result
and the hotspot table the structure stage already produced.

`validate_spec` is the analogue of `boltzgen check` in the BoltzGen backend, and
it is the last cheap place to catch the whole auth/label/atom-name class of
error. RFD3 will happily accept a spec naming a residue that does not exist or
an atom that residue does not have, diffuse against nothing in particular for
several days, and produce designs built on the wrong epitope. Run it before
every campaign.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from loguru import logger

# RFD3 always writes the designed binder as chain A and the templated target as
# chain B, whatever the input chains were called.
RFD3_BINDER_CHAIN = "A"
RFD3_TARGET_CHAIN = "B"

_CONTIG_SPAN = re.compile(r"^([A-Za-z])(-?\d+)-(-?\d+)$")


# One region may declare at most this many hotspots; see
# skills/complex-structure-analysis/SKILL.md Phase 2 Step 2b.
MAX_HOTSPOTS = 12

class SpecError(RuntimeError):
    """The spec is malformed, or does not match the structure it points at."""


@dataclass
class RFD3Spec:
    name: str
    path: Path
    payload: dict[str, Any]
    contig: str
    hotspots: dict[str, str]
    target_chain: str
    binder_min: int
    binder_max: int

    @property
    def entry(self) -> dict[str, Any]:
        return self.payload[self.name]


# ----------------------------------------------------------------------
# RFD3
# ----------------------------------------------------------------------

def _hotspot_key(chain: str, auth_seq_id: int) -> str:
    return f"{chain}{int(auth_seq_id)}"


def build_rfd3_spec(
    *,
    name: str,
    structure_path: Path,
    contig: str,
    hotspots: Sequence[dict],
    target_chain: str,
    out_path: Path,
    binder_min: int,
    binder_max: int,
    is_non_loopy: bool = True,
    infer_ori_strategy: str = "hotspots",
) -> RFD3Spec:
    """
    Write the RFD3 input JSON for one design job.

    `hotspots` are the rows parsed out of the structure stage's MODEL-READY
    HOTSPOTS table: each needs `auth_seq_id` and `rfd3_atoms` (a comma-separated
    atom list). Hotspot selection is ATOM level in RFD3 — naming the residue
    alone is not enough, and the atoms chosen are what the binder is steered to
    pack against.

    Note the design count is NOT part of this file: it is `n_batches x
    diffusion_batch_size` on the RFD3 command line.
    """
    if not hotspots:
        raise SpecError(
            "no hotspots — RFD3 would place the binder by centre of mass and the "
            "campaign would not target the intended epitope")

    # The interface skill caps a region at 12 (SKILL.md Phase 2 Step 2b). This
    # is the backstop for a drifted prompt: an oversized set is not an error —
    # the campaign still runs — but it quietly weakens the engagement gate,
    # because RFD3 contacts every hotspot in a 12-set only about half the time
    # and the rate falls as the set grows. Warn loudly rather than truncate:
    # choosing WHICH to drop needs the per-residue ddG/BSA the skill had and
    # this function does not.
    if len(hotspots) > MAX_HOTSPOTS:
        logger.warning(
            f"{len(hotspots)} hotspots declared for {name!r} — above the "
            f"{MAX_HOTSPOTS}-residue cap the interface skill is meant to apply. "
            "The campaign will run, but hotspot_engagement becomes harder to "
            "satisfy and less meaningful; consider splitting into independent "
            "regions or re-running the interface stage.")

    select: dict[str, str] = {}
    for h in hotspots:
        auth = h.get("auth_seq_id")
        atoms = (h.get("rfd3_atoms") or "").strip()
        if auth is None:
            raise SpecError(f"hotspot {h!r} has no auth_seq_id")
        if not atoms:
            raise SpecError(
                f"hotspot {h.get('residue', '')}{auth} has no rfd3_atoms; RFD3 "
                f"hotspot selection is atom-level and cannot use a bare residue")
        select[_hotspot_key(target_chain, auth)] = atoms

    payload = {
        name: {
            "dialect": 2,
            "infer_ori_strategy": infer_ori_strategy,
            "input": str(Path(structure_path).resolve()),
            "contig": contig,
            "select_hotspots": select,
            "is_non_loopy": is_non_loopy,
        }
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        f"RFD3 spec -> {out_path} | contig {contig} | "
        f"{len(select)} hotspot(s): {', '.join(sorted(select))}")
    return RFD3Spec(name=name, path=out_path, payload=payload, contig=contig,
                    hotspots=select, target_chain=target_chain,
                    binder_min=binder_min, binder_max=binder_max)


def parse_contig(contig: str) -> tuple[tuple[int, int], list[tuple[str, int, int]]]:
    """
    Split a contig into (binder length range, target spans).

    ``68-86,/0,B42-145`` -> ((68, 86), [("B", 42, 145)]).
    """
    parts = [p for p in contig.split(",") if p.strip()]
    if not parts:
        raise SpecError(f"empty contig {contig!r}")
    head = parts[0]
    if "-" not in head:
        raise SpecError(f"contig {contig!r} does not start with a binder length range")
    lo, hi = head.split("-", 1)
    try:
        binder = (int(lo), int(hi))
    except ValueError as exc:
        raise SpecError(f"bad binder length range {head!r} in contig") from exc

    spans: list[tuple[str, int, int]] = []
    for p in parts[1:]:
        if p.startswith("/"):        # chain break
            continue
        m = _CONTIG_SPAN.match(p)
        if not m:
            raise SpecError(f"unparseable contig element {p!r} in {contig!r}")
        spans.append((m.group(1), int(m.group(2)), int(m.group(3))))
    if not spans:
        raise SpecError(f"contig {contig!r} names no target span")
    return binder, spans


def validate_spec(
    spec_path: Path,
    *,
    kept_segments: Sequence[tuple[int, int]] | None = None,
    max_target_residues: int | None = None,
) -> dict[str, Any]:
    """
    Check an RFD3 spec against the structure it references.

    Verifies the file parses, `dialect` is 2, the input exists, every contig span
    is present in the structure, every hotspot resolves to a real residue that
    actually has the named atoms, and the hotspots lie inside the contig. Raises
    SpecError on the first problem; returns a summary dict on success.

    This is deliberately strict. Each of these failures produces a campaign that
    runs to completion and is quietly worthless.
    """
    import gemmi

    spec_path = Path(spec_path)
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"cannot read {spec_path}: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise SpecError(f"{spec_path} is not a non-empty JSON object")

    summary: dict[str, Any] = {"spec": str(spec_path), "designs": {}}
    for name, entry in payload.items():
        if not isinstance(entry, dict):
            raise SpecError(f"design {name!r} is not an object")
        if entry.get("dialect") != 2:
            raise SpecError(
                f"design {name!r} has dialect {entry.get('dialect')!r}; RFD3 "
                f"input specs must be dialect 2")
        struct_path = Path(entry.get("input", ""))
        if not struct_path.exists():
            raise SpecError(f"design {name!r} input {struct_path} does not exist")

        binder, spans = parse_contig(entry.get("contig", ""))
        if binder[0] > binder[1] or binder[0] < 1:
            raise SpecError(f"design {name!r} binder range {binder} is not sensible")

        st = gemmi.read_structure(str(struct_path))
        st.setup_entities()
        residues: dict[tuple[str, int], set[str]] = {}
        for ch in st[0]:
            for res in ch:
                residues[(ch.name, int(res.seqid.num))] = {
                    a.name for a in res}

        n_target = 0
        for chain, lo, hi in spans:
            missing = [i for i in range(lo, hi + 1) if (chain, i) not in residues]
            # Gaps inside a span are normal (unmodelled loops); the ENDPOINTS
            # must exist or the span does not mean what it says.
            for endpoint in (lo, hi):
                if (chain, endpoint) not in residues:
                    raise SpecError(
                        f"design {name!r} contig span {chain}{lo}-{hi}: residue "
                        f"{chain}{endpoint} is not in {struct_path.name}")
            n_target += (hi - lo + 1) - len(missing)
            if len(missing) > 0.25 * (hi - lo + 1):
                logger.warning(
                    f"design {name!r} span {chain}{lo}-{hi} is {len(missing)} "
                    f"residues short of contiguous — check for unmodelled loops")

        if max_target_residues and n_target > max_target_residues:
            raise SpecError(
                f"design {name!r} targets {n_target} residues, over the "
                f"{max_target_residues} budget — RF3 cost grows with the square "
                f"of the token count")

        hotspots = entry.get("select_hotspots") or {}
        if not hotspots:
            raise SpecError(f"design {name!r} has no select_hotspots")
        for key, atoms in hotspots.items():
            m = re.match(r"^([A-Za-z])(-?\d+)$", key)
            if not m:
                raise SpecError(
                    f"design {name!r} hotspot key {key!r} is not <chain><resnum>")
            chain, num = m.group(1), int(m.group(2))
            present = residues.get((chain, num))
            if present is None:
                raise SpecError(
                    f"design {name!r} hotspot {key} is not present in "
                    f"{struct_path.name} — the spec would steer the binder at "
                    f"nothing")
            wanted = [a.strip() for a in str(atoms).split(",") if a.strip()]
            if not wanted:
                raise SpecError(f"design {name!r} hotspot {key} lists no atoms")
            absent = [a for a in wanted if a not in present]
            if absent:
                raise SpecError(
                    f"design {name!r} hotspot {key} names atom(s) {absent} that "
                    f"residue does not have (it has {sorted(present)})")
            if not any(lo <= num <= hi for c, lo, hi in spans if c == chain):
                raise SpecError(
                    f"design {name!r} hotspot {key} lies outside every contig "
                    f"span — it was trimmed away or the contig is wrong")

        if kept_segments is not None:
            got = sorted((lo, hi) for _, lo, hi in spans)
            want = sorted(tuple(s) for s in kept_segments)
            if got != want:
                raise SpecError(
                    f"design {name!r} contig spans {got} do not match the trim's "
                    f"kept segments {want}")

        summary["designs"][name] = {
            "binder_range": list(binder),
            "target_spans": [[c, lo, hi] for c, lo, hi in spans],
            "n_target_residues": n_target,
            "n_hotspots": len(hotspots),
            "n_segments": len(spans),
        }
        logger.info(
            f"spec OK: {name} | binder {binder[0]}-{binder[1]} | "
            f"{n_target} target residues in {len(spans)} segment(s) | "
            f"{len(hotspots)} hotspots")
    return summary


# ----------------------------------------------------------------------
# solubleMPNN
# ----------------------------------------------------------------------

def build_mpnn_configs(
    design_files: Sequence[Path],
    out_dir: Path,
    config_dir: Path,
    *,
    checkpoint: str | Path,
    n_seq: int = 4,
    number_of_batches: int = 1,
    temperature: float = 0.1,
    omit: Sequence[str] = ("CYS",),
    bias: dict[str, float] | None = None,
    designed_chains: str = RFD3_BINDER_CHAIN,
    seed: int = 0,
    chunk_size: int = 250,
) -> list[Path]:
    """
    Write chunked solubleMPNN configs, one JSON per chunk.

    Chunking is not an optimisation: the `mpnn` CLI accumulates every result in
    memory and flushes only after its LAST input, so a single config over 8000
    designs shows an empty output directory until the very end, holds everything
    in RAM, and loses all of it on a late crash.

    solubleMPNN is ProteinMPNN retrained on soluble proteins — same architecture,
    different weights — so `model_type` stays `protein_mpnn` and
    `is_legacy_weights` stays true; only the checkpoint changes.

    CYS is omitted by default: free cysteines complicate expression and
    purification, and nothing here pairs them.
    """
    design_files = [Path(f) for f in design_files]
    if not design_files:
        raise SpecError("no design files to run MPNN on")
    out_dir, config_dir = Path(out_dir), Path(config_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    omit_list = ["UNK"] + [a.strip().upper() for a in omit if a and a.strip()]
    chunks = ([design_files] if chunk_size <= 0 else
              [design_files[i:i + chunk_size]
               for i in range(0, len(design_files), chunk_size)])

    written: list[Path] = []
    for i, chunk in enumerate(chunks):
        cfg = {
            "model_type": "protein_mpnn",
            "checkpoint_path": str(checkpoint),
            "is_legacy_weights": True,
            "out_directory": str(out_dir),
            "write_fasta": True,
            "write_structures": True,
            "inputs": [
                {
                    "structure_path": str(f.resolve()),
                    "name": strip_design_suffixes(f.name),
                    "seed": seed,
                    "batch_size": n_seq,
                    "number_of_batches": number_of_batches,
                    "temperature": temperature,
                    "omit": omit_list,
                    **({"bias": bias} if bias else {}),
                    # A LIST, not a string: this foundry build validates the
                    # type and rejects "A" with `designed_chains must be a list
                    # if provided`, after RFD3 has already run.
                    **({"designed_chains": _as_chain_list(designed_chains)}
                       if designed_chains else {}),
                }
                for f in chunk
            ],
        }
        path = config_dir / f"mpnn_batch_{i:03d}.json"
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        written.append(path)
    logger.info(
        f"MPNN: {len(design_files):,} designs x {n_seq} seq -> "
        f"{len(written)} config(s) in {config_dir}")
    return written


def _as_chain_list(chains: str | Sequence[str]) -> list[str]:
    """Accept "A", "A,B" or ["A", "B"] and always emit a list."""
    if isinstance(chains, str):
        return [c.strip() for c in chains.split(",") if c.strip()]
    return [str(c).strip() for c in chains if str(c).strip()]


def strip_design_suffixes(name: str) -> str:
    """Drop trailing .cif.gz / .cif / .pdb so MPNN's `name` is clean."""
    p = Path(name)
    while p.suffix in (".gz", ".cif", ".pdb", ".bcif"):
        p = p.with_suffix("")
    return p.name
