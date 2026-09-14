"""The size ceiling is a TOKEN ceiling, and it is measured.

`design.foundry.target_residue_budget` was 220, set from one ~175-token
complex, and its own config comment admitted so and asked for a bisection.
That bisection ran on 2026-09-14 against both GPU stages on this card
(32.6 GB, idle but for a 2.0 GB desktop):

    RF3, one refold   195 tok  4.1 GB net  ...  698 tok 30.0 GB net, fits with
                      0.59 GB spare;  848 tok -> CUDA OOM, "tried to allocate
                      9.09 GiB, 7.93 GiB free".
    RFD3, batch 4     589 tok 25.4 GB peak, 7.2 GB spare
                      665 tok 32.0 GB peak, 0.58 GB spare

Two things follow, and both are what this file pins.

**RFD3 is the binding stage, not RF3** — it holds one diffusion trajectory per
`diffusion_batch_size` member — so a ceiling derived from RF3 alone would be
~30 % too generous.

**The residue budget cannot express the limit.** It counts the TARGET only and
never the binder, so it is ~28 % short of the real complex across modalities
by construction. With the budget at 500 and `target_budget_overshoot: 0.15`,
a 575-residue target validates — 665 tokens, which measured 1.8 % of the card
free. That is not headroom: one more GPU tenant turns it into an OOM hours
into a campaign. `max_complex_tokens` is what refuses it at spec-build time.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from src.foundry_spec import SpecError, validate_spec

_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def foundry_cfg():
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    return (cfg["design"]["foundry"])


# ── the config now encodes a measurement ───────────────────────────────────

def test_the_budget_is_the_measured_operating_point(foundry_cfg):
    """500 target residues is 586-590 tokens with an 86-90mer binder, which
    measured 25.4 GB peak on RFD3 — 22 % of the card free."""
    assert foundry_cfg["target_residue_budget"] == 500


def test_a_token_ceiling_exists_and_binds_before_the_overshoot(foundry_cfg):
    """The overshoot multiplies the residue budget (500 -> 575 residues = 665
    tokens, measured at 32.0 of 32.6 GB). The token cap must be the tighter
    of the two, or the unsafe case still validates."""
    budget = foundry_cfg["target_residue_budget"]
    overshoot = foundry_cfg["target_budget_overshoot"]
    cap = foundry_cfg["max_complex_tokens"]
    residues_the_overshoot_allows = round(budget * (1 + overshoot))
    # 86 is `binder_sizes.mini_protein`'s upper bound — the default modality.
    assert cap < residues_the_overshoot_allows + 86, (
        "the token cap does not bind before the overshoot, so a 665-token "
        "spec — measured at 1.8% of the card free — would still validate")
    assert cap - 86 >= budget, (
        "the cap must still permit a full-budget target with the longest "
        "default binder, or the budget is unreachable")


# ── enforcement ────────────────────────────────────────────────────────────

def _spec(tmp_path, binder: str, target_res: int, struct: pathlib.Path):
    """A spec over `struct`'s own numbering, so only size is under test."""
    import gemmi

    st = gemmi.read_structure(str(struct))
    st.setup_entities()
    ch = st[0]["B"] if "B" in [c.name for c in st[0]] else st[0][0]
    res = [r for r in ch if r.find_atom("CA", "*")][:target_res]
    nums = [r.seqid.num for r in res]
    assert len(nums) == target_res, "structure has too few residues"
    name = "sizecheck"
    # A hotspot whose atom actually exists: the first residue may be a glycine,
    # and `validate_spec` checks atoms against the structure (which is the
    # whole point of that check).
    hs = next(r for r in res if r.find_atom("CB", "*"))
    hot = {f"{ch.name}{hs.seqid.num}": "CB"}
    spec = {name: {"dialect": 2, "input": str(struct),
                   "contig": f"{binder},/0,{ch.name}{nums[0]}-{nums[-1]}",
                   "select_hotspots": hot}}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def big_pdb():
    """A trimmed PDB with >=575 contiguous residues, written by the benchmark.
    Skipped when this checkout has not produced one."""
    hits = sorted(pathlib.Path("/tmp").glob(
        "claude-*/**/rfd3_575/rung_575/trimmed.pdb"))
    if not hits:
        pytest.skip("no >=575-residue trimmed PDB in this checkout")
    return hits[0]


def test_a_spec_over_the_token_cap_is_refused(tmp_path, big_pdb):
    """The 665-token case: 575 target residues with a 90mer binder. It passes
    the RESIDUE budget (575 <= 500*1.15) and must fail the TOKEN cap."""
    p = _spec(tmp_path, "90-90", 575, big_pdb)
    with pytest.raises(SpecError, match="token ceiling"):
        validate_spec(p, max_target_residues=575, max_complex_tokens=600)


def test_the_same_spec_passes_without_a_token_cap(tmp_path, big_pdb):
    """Proves the refusal comes from the new check and not from something
    else about the spec — the old behaviour let this through."""
    p = _spec(tmp_path, "90-90", 575, big_pdb)
    out = validate_spec(p, max_target_residues=575)
    assert out["designs"]["sizecheck"]["n_tokens"] == 665


def test_the_intended_operating_point_passes(tmp_path, big_pdb):
    """500 residues + a 90mer binder = 590 tokens, the measured-safe point."""
    p = _spec(tmp_path, "90-90", 500, big_pdb)
    out = validate_spec(p, max_target_residues=575, max_complex_tokens=600)
    assert out["designs"]["sizecheck"]["n_tokens"] == 590


def test_the_binder_counts_toward_the_ceiling(tmp_path, big_pdb):
    """The whole point of a token cap: the same target with a longer binder
    must be refused, which a target-residue budget can never express."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    short = _spec(tmp_path / "a", "40-40", 520, big_pdb)
    long_ = _spec(tmp_path / "b", "120-120", 520, big_pdb)
    assert validate_spec(short, max_complex_tokens=600
                         )["designs"]["sizecheck"]["n_tokens"] == 560
    with pytest.raises(SpecError, match="token ceiling"):
        validate_spec(long_, max_complex_tokens=600)


def test_the_error_says_what_was_measured(tmp_path, big_pdb):
    """An operator hitting this needs to know it is a real GPU limit and what
    to do, not just that a number was exceeded."""
    p = _spec(tmp_path, "90-90", 575, big_pdb)
    with pytest.raises(SpecError) as exc:
        validate_spec(p, max_complex_tokens=600)
    msg = str(exc.value)
    assert "665 tokens" in msg and "90 binder + 575 target" in msg
    assert "OOM" in msg
    assert "max_complex_tokens" in msg, "must name the knob"
