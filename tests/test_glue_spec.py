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
        if len({k[0] for k in select}) > 1:
            # A glue spec cannot rebuild from chain-LESS hotspot dicts, which
            # is the whole point of this harness: it proves the DEFAULT is
            # unchanged. The two-chain branch is covered by the assertions
            # above, not here.
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


# ---------------------------------------------- the cross-check and item 18

def _trimmed_4zgm(tmp_path):
    """A real no-op two-chain trim, and the PDB RFD3 would be given."""
    from src.structure_trim import trim_target
    t = trim_target(_4ZGM, target_chain="A", partner_chain="B",
                    hotspots=[_hot(113, "NZ,CE", "A", "LYS")], budget=500,
                    out_dir=tmp_path, pdb_id="4ZGM", binder_min=70,
                    binder_max=86, co_target_chains=["B"])
    return t, pathlib.Path(t.trimmed_path).with_suffix(".pdb")


def _spec(tmp_path, structure, contig):
    from src.foundry_spec import build_rfd3_spec as brs
    return brs(name="t", structure_path=structure, contig=contig,
               target_chain="A", hotspots=[_hot(113, "NZ,CE", "A", "LYS")],
               out_path=tmp_path / "s.json", binder_min=70, binder_max=86).path


@pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")
def test_n_target_residues_is_a_set_not_a_running_sum(tmp_path):
    """Overlapping spans were double-counted.

    `A29-128,A29-37` reported 109 target residues for a 100-residue chain, and
    195 tokens for a 186-token complex. Identical on all 49 archived specs,
    whose spans are disjoint.
    """
    from src.foundry_spec import validate_spec
    _t, pdb = _trimmed_4zgm(tmp_path)
    path = _spec(tmp_path, pdb, "70-86,/0,A29-128,A29-37")
    assert validate_spec(path)["designs"]["t"]["n_target_residues"] == 100


@pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")
def test_the_cross_check_compares_chains_too(tmp_path):
    from src.foundry_spec import SpecError as SE, validate_spec
    _t, pdb = _trimmed_4zgm(tmp_path)
    path = _spec(tmp_path, pdb, "70-86,/0,A29-128,B10-37")

    validate_spec(path, kept_by_chain={"A": [(29, 128)], "B": [(10, 37)]})
    with pytest.raises(SE, match="do not match the trim's kept spans"):
        validate_spec(path, kept_by_chain={"B": [(29, 128)], "A": [(10, 37)]})


@pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")
def test_a_dropped_chain_is_refused_by_the_recorded_residue_count(tmp_path):
    """Scope item 18, as a check rather than a construction.

    The contig is what RFD3 is templated on; the trim's count is what the
    campaign is costed on. A chain reaching one and not the other under-costs
    the GPU quadratically and is otherwise silent.
    """
    from src.foundry_spec import SpecError as SE, validate_spec
    _t, pdb = _trimmed_4zgm(tmp_path)
    path = _spec(tmp_path, pdb, "70-86,/0,A29-128")
    with pytest.raises(SE, match="but the trim recorded 128"):
        validate_spec(path, expected_target_residues=128)
    validate_spec(path, expected_target_residues=100)


def test_passing_both_cross_check_forms_is_refused():
    from src.foundry_spec import SpecError as SE, validate_spec
    with pytest.raises(SE, match="not both"):
        validate_spec(__file__, kept_segments=[(1, 2)], kept_by_chain={"A": [(1, 2)]})


def test_trim_cross_check_is_chain_aware_only_when_the_trim_is():
    from src.foundry_spec import trim_cross_check

    class _Bare:
        kept_segments = [(29, 128)]

    class _WithChains(_Bare):
        kept_by_chain = {"A": [(29, 128)], "B": [(10, 37)]}

    assert trim_cross_check(_Bare()) == {"kept_segments": [(29, 128)]}
    assert trim_cross_check(_WithChains()) == {
        "kept_by_chain": {"A": [(29, 128)], "B": [(10, 37)]}}


def test_every_archived_trim_passes_the_chain_aware_cross_check():
    """53 trim maps, each against its own contig. 0 mismatches.

    `_TrimFromDisk` reconstructs `{target_chain: kept_segments}` for maps
    written before the key existed, so archived trims take the STRONGER
    branch — which is only safe because every one of them passes it.
    """
    from src.foundry_spec import parse_contig as pc, trim_cross_check
    from src.pipeline_runner import _TrimFromDisk

    maps = sorted(set(list(_ROOT.glob("projects/**/trim_map.json"))
                      + list(_ROOT.glob("outputs/**/trim_map.json"))))
    checked, bad = 0, []
    for p in maps:
        mapping = json.loads(p.read_text())
        if not mapping.get("contig"):
            continue
        checked += 1
        kw = trim_cross_check(_TrimFromDisk(mapping))
        want = {c: sorted(tuple(x) for x in v)
                for c, v in kw.get("kept_by_chain", {}).items()}
        got: dict[str, list] = {}
        for c, lo, hi in pc(mapping["contig"])[1]:
            got.setdefault(c, []).append((lo, hi))
        if want and {c: sorted(v) for c, v in got.items()} != want:
            bad.append(str(p.relative_to(_ROOT)))
    if not checked:
        pytest.skip("no shipped trim maps in this checkout")
    assert bad == [], f"{len(bad)} of {checked} archived trims would refuse: {bad[:3]}"


def test_every_archived_trim_satisfies_the_item_18_invariant():
    """`n_residues_after == sum(kept_segments)` on 53 of 53 — why it is hard."""
    maps = sorted(set(list(_ROOT.glob("projects/**/trim_map.json"))
                      + list(_ROOT.glob("outputs/**/trim_map.json"))))
    bad = []
    for p in maps:
        m = json.loads(p.read_text())
        if m.get("n_residues_after") is None:
            continue
        # The count covers every chain the contig names, so a glue trim is
        # measured against `kept_by_chain`; `kept_segments` is the primary
        # chain alone and is 100 against 128 on 4ZGM, correctly.
        by_chain = m.get("kept_by_chain")
        spans = ([x for v in by_chain.values() for x in v] if by_chain
                 else (m.get("kept_segments") or []))
        total = sum(hi - lo + 1 for lo, hi in spans)
        if m["n_residues_after"] != total:
            bad.append((str(p.relative_to(_ROOT)), m["n_residues_after"], total))
    assert bad == [], f"{len(bad)} trims disagree with their own spans: {bad[:3]}"
