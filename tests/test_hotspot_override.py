"""Operator-specified hotspots: "design against RING1B residues 50, 52, 54".

Chain-level steering was already the operator's (`--chains`); residue-level was
not — the interface skill chose the epitope and the only existing residue hint
(`target_site_hint.priority_residues`) is authored by another LLM stage and
explicitly advisory. `--hotspots` makes it deterministic: the stage makes NO
model call, and the epitope is exactly what was asked for.

The design rule these tests exist to protect: the operator supplies ONLY the
numbers. Residue names, sidechain atoms and label_seq_ids are read from the
structure, because a user-typed residue name would make
`_verify_hotspot_grounding` tautological — grounding exists to prove the residue
at auth 83 in THIS file is the one intended, and it can only do that if the
name came from the file rather than from the same person who typed the number.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from src.pipeline_runner import (
    PipelineBlockedError,
    PipelineResult,
    PipelineRunner,
)

_ROOT = Path(__file__).resolve().parents[1]
_STRUCTURES = _ROOT / "data" / "structures"
# PD-L1 / anti-PD-L1 VHH. Chain B is PD-L1; 56/66/115 are TYR/GLN/MET in it.
PDL1 = _STRUCTURES / "7CZD_ba1.cif"


@pytest.fixture
def staged(tmp_path):
    """A structure-first run staged up to (but not through) the interface stage."""
    if not PDL1.is_file():
        pytest.skip(f"{PDL1.name} not in this checkout")
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))

    made = []

    def build(hotspots: str):
        runner = PipelineRunner(config=config, provider="gemini",
                                workflow="structure", hotspots=hotspots)
        src = tmp_path / f"t{len(made)}.cif"
        shutil.copy(PDL1, src)
        pdb_id = runner.ingest_local_structure(src)
        made.append(pdb_id)
        run_dir = tmp_path / f"run{len(made)}"
        run_dir.mkdir()
        dirs = runner._binder_dirs(run_dir)
        result = PipelineResult(run_dir=run_dir)
        intel = runner._stage_structure_intel(
            pdb_id, "Disrupt the interface.", dirs, result)
        return runner, intel, dirs, result

    yield build
    for pdb_id in made:
        (_STRUCTURES / f"{pdb_id}.cif").unlink(missing_ok=True)


# ── the happy path ───────────────────────────────────────────────────────────

def test_the_stage_makes_no_model_call_and_still_produces_hotspots(staged, monkeypatch):
    runner, intel, dirs, result = staged("B56,B66,B115")

    def explode(*a, **kw):
        raise AssertionError("_run_stage was called; the override must not "
                             "spend an LLM call")

    monkeypatch.setattr(runner, "_run_stage", explode)
    _handoff, hotspots_json = runner._stage_binder_interface(intel, dirs, result)
    residues = json.loads(hotspots_json)["residues"]
    assert [r["auth_seq_id"] for r in residues] == [56, 66, 115]


def test_residue_names_come_from_the_structure_not_the_operator(staged):
    """The operator types numbers. Names are looked up — otherwise grounding
    checks the user's own claim against itself."""
    runner, intel, dirs, result = staged("B56,B66,B115")
    _h, hotspots_json = runner._stage_binder_interface(intel, dirs, result)
    got = {r["auth_seq_id"]: r["residue"] for r in json.loads(hotspots_json)["residues"]}
    assert got == {56: "TYR", 66: "GLN", 115: "MET"}


def test_sidechain_atoms_are_derived_per_residue_type(staged):
    """`validate_spec` checks every named atom exists on the residue, but it
    runs after the trim — deriving them here means the operator hears about a
    bad atom before a multi-day campaign is staged, not after."""
    runner, intel, dirs, result = staged("B56,B66,B115")
    _h, hotspots_json = runner._stage_binder_interface(intel, dirs, result)
    atoms = {r["residue"]: r["rfd3_atoms"]
             for r in json.loads(hotspots_json)["residues"]}
    assert atoms == {"TYR": "CD2,OH", "GLN": "CD,OE1", "MET": "CG,SD"}


def test_label_seq_ids_are_looked_up_never_derived(staged):
    """Written as `**UNVERIFIED**` and filled by `_correct_label_seq_ids` from
    gemmi — the mechanism this repo already trusts for the model's own tables.
    A counted label_seq_id makes BoltzGen constrain the wrong residues."""
    runner, intel, dirs, result = staged("B56,B66,B115")
    _h, hotspots_json = runner._stage_binder_interface(intel, dirs, result)
    labels = {r["auth_seq_id"]: r["label_seq_id"]
              for r in json.loads(hotspots_json)["residues"]}
    assert labels == {56: 39, 66: 49, 115: 98}
    assert all(isinstance(v, int) for v in labels.values())


def test_the_written_report_is_resumable_and_says_who_chose(staged):
    """`--start-from trim` re-parses `21_interface.md` off disk, so the
    override has to write a real artifact rather than short-circuit past it."""
    runner, intel, dirs, result = staged("B56,B66")
    runner._stage_binder_interface(intel, dirs, result)
    path = dirs["binder"] / runner._BINDER_STAGE_FILES["interface"]
    text = path.read_text(encoding="utf-8")
    assert "specified by the operator" in text
    assert "### MODEL-READY HOTSPOTS" in text
    assert "select_hotspots" in text
    # The pipeline's own parser must accept it, not just a regex of our own.
    from src.handoff import parse_handoff, parse_hotspot_residues
    handoff = parse_handoff(text)
    assert handoff["hotspot_source"] == "operator"
    assert json.loads(parse_hotspot_residues(text, handoff))["residues"]


def test_bare_numbers_mean_the_target_chain(staged):
    runner, intel, dirs, result = staged("56,66")
    _h, hotspots_json = runner._stage_binder_interface(intel, dirs, result)
    parsed = json.loads(hotspots_json)
    assert parsed["target_chain"] == "B"
    assert [r["auth_seq_id"] for r in parsed["residues"]] == [56, 66]


# ── refusals ─────────────────────────────────────────────────────────────────

def test_a_residue_absent_from_the_chain_is_refused(staged):
    """The commonest real mistake: canonical-isoform numbering pasted against
    a construct-numbered crystal."""
    runner, intel, dirs, result = staged("B999")
    with pytest.raises(PipelineBlockedError, match="not a residue of chain"):
        runner._stage_binder_interface(intel, dirs, result)


def test_hotspots_on_the_partner_chain_are_refused(staged):
    """Hotspots are the epitope ON the target. Accepting a partner-chain
    residue would design a binder against the molecule being displaced."""
    runner, intel, dirs, result = staged("A56")
    with pytest.raises(PipelineBlockedError, match="target chain"):
        runner._stage_binder_interface(intel, dirs, result)


def test_over_the_cap_is_an_error_for_an_operator(staged):
    """`build_rfd3_spec` only WARNS when a skill overshoots, because the
    builder cannot know which to drop. An operator can, and more hotspots is
    not stricter — RFD3's hit rate falls as the set grows, which weakens the
    engagement gate."""
    runner, intel, dirs, result = staged(",".join(f"B{n}" for n in range(19, 40)))
    with pytest.raises(PipelineBlockedError, match="the cap is 12"):
        runner._stage_binder_interface(intel, dirs, result)


@pytest.mark.parametrize("spec", ["B5x", "", "   ", ","])
def test_malformed_specs_are_refused(spec):
    with pytest.raises(PipelineBlockedError):
        PipelineRunner.parse_hotspot_spec(spec)


def test_the_cap_matches_foundry_spec(staged):
    """One source for the cap. A second copy here would drift from the one
    `build_rfd3_spec` enforces."""
    from src.foundry_spec import MAX_HOTSPOTS

    runner, intel, dirs, result = staged(
        ",".join(f"B{n}" for n in range(19, 19 + MAX_HOTSPOTS)))
    runner._stage_binder_interface(intel, dirs, result)   # exactly at the cap


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_ppi_track_refuses_rather_than_ignoring_the_flag():
    """Accepting `--hotspots` on a track that cannot honour it would run a
    whole campaign against a model-chosen epitope while the operator believed
    otherwise."""
    import scripts.run_pipeline as rp

    parser = rp._build_parser()
    assert any(a.dest == "hotspots" for a in parser._actions)


def test_the_structure_first_track_passes_the_query_to_the_interface_stage(staged):
    """Without `structure_query` the interface goal falls back to a generic
    sentence and the operator's brief never reaches the stage that chooses the
    epitope — the one stage on that track where it matters."""
    runner, intel, _dirs, _result = staged("B56")
    assert intel["structure_query"] == "Disrupt the interface."
