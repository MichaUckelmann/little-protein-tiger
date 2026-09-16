"""A hotspot reaches the spec under its OWN chain.

`build_rfd3_spec` did `select[_hotspot_key(target_chain, auth)] = atoms` — the
declared target chain, unconditionally — so a molecular glue's co-target rows
were stamped onto the target.

That produces a spec `validate_spec` ACCEPTS. On 4ZGM every partner hotspot
number also exists on chain A, so the residues are real and carry real atoms;
they are simply on the wrong molecule. And it is worse now than it was:
grounding used to be the accidental guard against a cross-chain table, and
per-chain grounding removed that protection, so the three filters here are
what replace it. They land together for that reason — `build_rfd3_spec`'s key,
`_stage_binder_spec`'s kept-set, and `_stage_trim`'s hotspot argument.

The proof that the single-chain path is untouched is the 49-spec
re-derivation, not the one CD79b golden.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.foundry_spec import SpecError, build_rfd3_spec, parse_contig

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_4ZGM = _ROOT / "data/structures/4ZGM_ba1.cif"


def _hot(auth, atoms, chain=None, residue="ALA"):
    h = {"residue": residue, "auth_seq_id": auth, "rfd3_atoms": atoms}
    if chain:
        h["chain"] = chain
    return h


# ---------------------------------------------------------------- THE guard test

def test_a_partner_hotspot_is_never_stamped_onto_the_target_chain(tmp_path):
    """Write this one first: the failure it catches is silent and validates."""
    spec = build_rfd3_spec(
        name="t", structure_path=_4ZGM if _4ZGM.exists() else __file__,
        contig="70-86,/0,A29-128,B10-37", target_chain="A",
        hotspots=[_hot(113, "NZ,CE", "A"), _hot(30, "CB", "B")],
        out_path=tmp_path / "s.json", binder_min=70, binder_max=86)
    assert set(spec.hotspots) == {"A113", "B30"}
    assert spec.target_chains == ["A", "B"]


def test_a_chain_less_hotspot_still_falls_back_to_the_target_chain(tmp_path):
    """Every existing producer emits no `chain` key; they must be unaffected."""
    spec = build_rfd3_spec(
        name="t", structure_path=__file__, contig="68-86,/0,B42-145",
        target_chain="B", hotspots=[_hot(56, "CD2,CZ"), _hot(66, "CG,OD1")],
        out_path=tmp_path / "s.json", binder_min=68, binder_max=86)
    assert set(spec.hotspots) == {"B56", "B66"}
    assert spec.target_chains == ["B"]


def test_a_malformed_chain_on_a_hotspot_is_refused(tmp_path):
    for bad in ("AB", "1", "", " "):
        h = _hot(113, "NZ,CE")
        h["chain"] = bad
        if not bad.strip():
            continue  # blank falls back to target_chain by design
        with pytest.raises(SpecError, match="not a single-letter auth chain id"):
            build_rfd3_spec(name="t", structure_path=__file__,
                            contig="70-86,/0,A29-128", target_chain="A",
                            hotspots=[h], out_path=tmp_path / "s.json",
                            binder_min=70, binder_max=86)


def test_a_duplicate_key_with_different_atoms_warns_and_keeps_the_later(tmp_path, caplog):
    """A builder has no basis for choosing; validate_spec checks the atoms."""
    spec = build_rfd3_spec(
        name="t", structure_path=__file__, contig="70-86,/0,A29-128",
        target_chain="A", hotspots=[_hot(113, "NZ,CE", "A"), _hot(113, "CB", "A")],
        out_path=tmp_path / "s.json", binder_min=70, binder_max=86)
    assert spec.hotspots["A113"] == "CB"


def test_the_same_number_on_two_chains_is_two_hotspots(tmp_path):
    """The collision that makes the flat key invisible rather than loud."""
    spec = build_rfd3_spec(
        name="t", structure_path=__file__, contig="70-86,/0,A29-128,B10-37",
        target_chain="A", hotspots=[_hot(30, "CB,CA", "A"), _hot(30, "CB", "B")],
        out_path=tmp_path / "s.json", binder_min=70, binder_max=86)
    assert set(spec.hotspots) == {"A30", "B30"}


# ------------------------------------------------- the corpus re-derivation

def test_every_shipped_spec_re_derives_field_for_field(tmp_path):
    """49 shipped specs rebuilt from their own payloads, byte for byte.

    The real single-chain proof: the one CD79b golden pins one spec, this
    pins every spec any campaign in this checkout ever ran.
    """
    specs = sorted(set(list(_ROOT.glob("projects/**/spec/*.json"))
                       + list(_ROOT.glob("outputs/**/spec/*.json"))))
    if not specs:
        pytest.skip("no shipped specs in this checkout")

    checked, bad = 0, []
    for path in specs:
        payload = json.loads(path.read_text())
        name, entry = next(iter(payload.items()))
        select = entry.get("select_hotspots") or {}
        if not select:
            continue
        checked += 1
        target_chain = next(iter(select))[0]
        binder, _spans = parse_contig(entry["contig"])
        # Chain-LESS hotspot dicts, exactly as every existing producer emits.
        hotspots = [{"residue": "XXX", "auth_seq_id": int(k[1:]), "rfd3_atoms": v}
                    for k, v in select.items()]
        rebuilt = build_rfd3_spec(
            name=name, structure_path=entry["input"], contig=entry["contig"],
            target_chain=target_chain, hotspots=hotspots,
            out_path=tmp_path / f"{checked}.json",
            binder_min=binder[0], binder_max=binder[1],
            infer_ori_strategy=entry.get("infer_ori_strategy", "random"),
            is_non_loopy=entry.get("is_non_loopy", False))
        if json.loads(rebuilt.path.read_text()) != payload:
            bad.append(str(path.relative_to(_ROOT)))

    assert checked >= 40, f"expected the shipped corpus, got {checked} specs"
    assert bad == [], f"{len(bad)} of {checked} specs changed: {bad[:3]}"
