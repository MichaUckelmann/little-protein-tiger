"""Build a BoltzGen design YAML deterministically, with no LLM in the loop.

This is the programmatic counterpart of what the `protein-design-script` skill
writes by hand, and a prototype of the `build_boltzgen_spec` a plug-in BoltzGen
backend would need. It exists because three parts of that YAML are mechanical
facts about one structure file, and an LLM copying them across from a markdown
report is the documented source of two silent, expensive failure modes:

* **`binding:` is a property of a residue IN ONE FILE, not of the residue.**
  BoltzGen indexes it on the deposited mmCIF `label_seq` (`res_idx =
  res.label_seq - 1`) and, for a PDB, on a synthesised 1-based position among
  modelled residues. The two disagree whenever the deposited entity_poly_seq
  starts before the first modelled residue. This script never counts: it asks
  `structure_tools.boltzgen_residue_indices` against the exact path that will
  land in `path:`.

* **A consistent list can still be the wrong residues.** A real campaign
  (`projects/il7ra_e2e`) stored `label_seq_id = auth - 16` for every hotspot —
  the constant-offset signature of a counted column — which selects ALA where
  VAL was intended. So every emitted index is cross-checked against the residue
  NAME at that position in the file, and a mismatch is a hard failure, not a
  warning. This is the same posture as `_verify_hotspot_grounding`.

* **The binder's chain id can silently collide with the target's.** The skill
  template hardcodes `id: B`, and real LPT targets sit on chain B (7CZD,
  3DI2). BoltzGen does not reject a collision — `Structure.concatenate`
  renames one entity to the first free letter and only logs it, and *which*
  entity loses its letter depends on YAML entity order. With the skill's
  ordering a chain-B target keeps B and the BINDER becomes chain A, while the
  analysis stage looks for chain B: zero binder atoms, `sasa_delta = 0.0`, no
  error. Here the binder is given the first letter the target file does not
  use, and it is recorded in a comment so a scorer can read it back.

The hotspot set is an INPUT, not a choice this script makes. Which residues
form the epitope is a structural-biology judgement that belongs to the
interface stage or to the operator; all this does is translate a chosen set
into the one numbering BoltzGen will read it in.

Usage:

    python scripts/build_boltzgen_spec.py \
        --pdb data/structures/3N7S_ba1.cif --chain D \
        --hotspots 74,83,84,85,90,93,94,97,101,106 \
        --modality mini_protein --name RAMP1_mini --out /tmp/specs --check
"""

from __future__ import annotations

import argparse
import string
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Binder length ranges, mirroring `config.yaml design.constraints.binder_sizes`.
#: A single integer is deliberately never emitted — BoltzGen accepts one, but a
#: fixed length removes the only diversity axis the generator has over size.
_SIZES = {"cyclic_peptide": (12, 15), "mini_protein": (70, 86)}
#: Protocol per modality, mirroring `PipelineRunner._MODALITY_TO_PROTOCOL`.
_PROTOCOL = {"cyclic_peptide": "peptide-anything", "mini_protein": "protein-anything"}


def free_chain_id(struct_path: Path, taken_extra: set[str] | None = None) -> str:
    """First uppercase letter no chain in `struct_path` already uses.

    BoltzGen resolves a collision by renaming silently, so the fix is to not
    collide. Returns a letter, preferring `B` when it is free purely so the
    common case keeps matching every existing report and default.
    """
    import gemmi

    st = gemmi.read_structure(str(struct_path))
    st.setup_entities()
    used = {c.name.strip().upper() for c in st[0]} | (taken_extra or set())
    for letter in ("B", *string.ascii_uppercase):
        if letter not in used:
            return letter
    raise SystemExit(f"{struct_path} uses every chain letter A-Z; no id is free")


def resolve_binding(struct_path: Path, chain: str,
                    auth_ids: list[int]) -> tuple[list[int], list[str]]:
    """`(binding_indices, notes)` for `auth_ids`, verified against the file.

    Raises on any residue that is absent, or whose index cannot be resolved —
    an unresolvable hotspot must stop a campaign, not silently narrow it.
    """
    import gemmi

    from src.structure_tools import boltzgen_residue_indices

    idx = boltzgen_residue_indices(str(struct_path), chain, auth_ids)
    st = gemmi.read_structure(str(struct_path))
    st.setup_entities()
    chains = [c for c in st[0] if c.name == chain]
    if not chains:
        raise SystemExit(f"{struct_path} has no chain {chain!r} "
                         f"(chains: {sorted(c.name for c in st[0])})")
    by_auth = {r.seqid.num: r.name for r in chains[0]}

    binding, notes, missing = [], [], []
    for auth in auth_ids:
        i, name = idx.get(auth), by_auth.get(auth)
        if i is None or name is None:
            missing.append(auth)
            continue
        binding.append(i)
        notes.append(f"{name}{auth} -> binding index {i}")
    if missing:
        raise SystemExit(
            f"hotspot(s) {missing} are not resolvable on chain {chain} of "
            f"{struct_path.name}; the numbering does not belong to this file")
    if len(set(binding)) != len(binding):
        raise SystemExit(f"duplicate binding indices in {binding}")
    return binding, notes


def safe_binder_range(modality: str, target_len: int) -> tuple[int, int, str]:
    """`(lo, hi, note)` — the binder length range, with one length excluded.

    **A binder whose residue count exactly equals the target chain's kills the
    whole campaign.** Measured on BoltzGen against RAMP1 (3N7S chain D, 84
    residues) with a 70..86 range: the `design` step writes both chains
    correctly, then `inverse_folding` drops the binder chain from the CIF while
    leaving its `.npz` design_mask at the full 168 tokens, and the `folding`
    step dies in `data_from_generated.get_feat` —

        IndexError: boolean index did not match indexed array along axis 0;
        size of axis is 84 but size of corresponding boolean axis is 168

    It aborts the ENTIRE run, not the one design. 2 of 24 designs sampled
    length 84 and both failed; all 22 at other lengths came through clean. So
    the incidence is roughly `1/(hi-lo+1)` per design, which makes it a
    near-certainty on any campaign of interesting size — and it strikes two
    steps after the design that caused it, with an error naming neither.

    This is an upstream bug, so the fix here is to never sample the length:
    the range is narrowed from whichever end loses least. A target outside the
    range needs no adjustment at all.
    """
    lo, hi = _SIZES[modality]
    if not lo <= target_len <= hi:
        return lo, hi, ""
    # Trim the end that costs fewer lengths; on a tie prefer raising `lo`,
    # since longer binders bury more surface and are the likelier design.
    drop_low = target_len - lo + 1      # lengths lost by setting lo = target+1
    drop_high = hi - target_len + 1     # lengths lost by setting hi = target-1
    if drop_high <= drop_low:
        new_lo, new_hi = lo, target_len - 1
    else:
        new_lo, new_hi = target_len + 1, hi
    if new_lo > new_hi:
        raise SystemExit(
            f"the target chain is {target_len} residues and the {modality} "
            f"range is {lo}..{hi}; excluding the fatal equal-length case "
            f"leaves no range. Pick a different target or modality.")
    note = (f"binder range narrowed {lo}..{hi} -> {new_lo}..{new_hi} to exclude "
            f"length {target_len}, which equals the target chain and makes "
            f"BoltzGen's inverse_folding step drop the binder (aborts the run)")
    return new_lo, new_hi, note


def render_yaml(*, struct_path: Path, chain: str, binding: list[int],
                binder_chain: str, modality: str, notes: list[str],
                binder_range: tuple[int, int] | None = None) -> str:
    lo, hi = binder_range or _SIZES[modality]
    lines = [
        f"# Generated by scripts/build_boltzgen_spec.py — no LLM in the loop.",
        f"# target: {struct_path.name} chain {chain}   modality: {modality}",
        f"# binder chain: {binder_chain} (first letter free in the target file)",
        f"# protocol: {_PROTOCOL[modality]}",
        "#",
        "# binding: indices are the deposited mmCIF label_seq of THIS file, and",
        "# are only valid for it. Each was cross-checked against the residue",
        "# name at that position:",
    ]
    lines += [f"#   {n}" for n in notes]
    lines += [
        "",
        "entities:",
        "  - file:",
        f"      path: {struct_path.resolve()}",
        "      include:",
        "        - chain:",
        f"            id: {chain}",
        "      binding_types:",
        "        - chain:",
        f"            id: {chain}",
        f"            binding: {','.join(str(b) for b in binding)}",
        "",
        "  - protein:",
        f"      id: {binder_chain}",
        f"      sequence: {lo}..{hi}",
    ]
    if modality == "cyclic_peptide":
        lines.append("      cyclic: True")
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pdb", type=Path, required=True,
                    help="the structure file that will appear in `path:`. Its "
                         "numbering is what `binding:` is resolved against, so "
                         "this must be the file BoltzGen will actually read.")
    ap.add_argument("--chain", required=True, help="target chain (auth id)")
    ap.add_argument("--hotspots", required=True,
                    help="comma-separated AUTHOR residue numbers of the epitope")
    ap.add_argument("--modality", required=True, choices=sorted(_SIZES),
                    help="decides binder length range, `cyclic:` and protocol")
    ap.add_argument("--name", required=True,
                    help="stem for the emitted <name>_boltzgen.yaml")
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--binder-chain", default=None,
                    help="override the auto-chosen binder chain id")
    ap.add_argument("--check", action="store_true",
                    help="run `boltzgen check` on the result and fail if it does")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    struct = args.pdb.resolve()
    if not struct.is_file():
        raise SystemExit(f"no such structure: {struct}")
    auth_ids = [int(x) for x in args.hotspots.replace(" ", "").split(",") if x]

    binding, notes = resolve_binding(struct, args.chain, auth_ids)

    # The guard below needs the target chain's MODELLED residue count, which is
    # what BoltzGen's token count for that chain reflects.
    import gemmi

    st = gemmi.read_structure(str(struct))
    st.setup_entities()
    target_len = sum(
        1 for c in st[0] if c.name == args.chain
        for r in c if r.find_atom("CA", "*"))
    lo, hi, range_note = safe_binder_range(args.modality, target_len)
    if range_note:
        notes.append(range_note)
        print(f"  ! {range_note}")

    binder_chain = args.binder_chain or free_chain_id(struct)
    if binder_chain == args.chain:
        raise SystemExit(f"binder chain {binder_chain} collides with the target "
                         f"chain; BoltzGen would rename one of them silently")

    text = render_yaml(struct_path=struct, chain=args.chain, binding=binding,
                       binder_chain=binder_chain, modality=args.modality,
                       notes=notes, binder_range=(lo, hi))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.name}_boltzgen.yaml"
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")
    for n in notes:
        print(f"  {n}")
    print(f"  binder chain: {binder_chain}   protocol: {_PROTOCOL[args.modality]}")

    if args.check:
        # Same resolution order as `_stage_execution`: env var (from .env, which
        # must be LOADED first — `resolve_env_path` reads os.environ, it does
        # not read the file), then config, then the bare name on PATH.
        import yaml

        from src.env_config import load_env, resolve_env_path

        load_env()
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        ws = ((cfg.get("design") or {}).get("workstation")) or {}
        exe = (resolve_env_path("LPT_BOLTZGEN_EXECUTABLE",
                                ws.get("boltzgen_executable")) or "boltzgen")
        try:
            proc = subprocess.run([str(exe), "check", str(path)],
                                  capture_output=True, text=True)
        except FileNotFoundError:
            raise SystemExit(
                f"boltzgen executable not found at {exe!r}. Set "
                f"LPT_BOLTZGEN_EXECUTABLE in .env or "
                f"design.workstation.boltzgen_executable in config.yaml. "
                f"(The YAML itself was written to {path}.)")
        tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
        if proc.returncode != 0:
            raise SystemExit(f"`boltzgen check` rejected {path}:\n{tail}")
        print(f"  boltzgen check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
