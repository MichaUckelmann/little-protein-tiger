"""The structure-first track: an operator's own structure, no discovery.

`--workflow ppi` starts from a question and `--workflow binder` from a protein
name. Both spend LLM stages answering "what should we design against?" — which
is already answered when someone hands you a structure. This track skips that:
stage 0 is deterministic (chains enumerated, interface MEASURED), and the
binder stage machine is entered at `interface`, so a model still chooses the
epitope from real coordinates but nothing searches for a target.

These tests pin the parts that can silently produce a plausible, wrong campaign:
which chain becomes the target, that a local file is addressable at all, and
that the three identity-keyed guards announce themselves when they go inactive.
They need no network, no GPU and no API key.
"""

from __future__ import annotations

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

# 7CZD is the PD-L1 / anti-PD-L1 VHH complex the chain-assignment guard exists
# because of: an LLM stage once assigned target_chain to the NANOBODY and ran a
# full campaign against it. Chain B is PD-L1, chain A is the VHH.
PDL1 = _STRUCTURES / "7CZD_ba1.cif"


def _runner(**kw) -> PipelineRunner:
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    return PipelineRunner(config=config, provider="gemini",
                          workflow="structure", **kw)


@pytest.fixture
def ingested(tmp_path):
    """A copy of PD-L1/VHH ingested under a name of the operator's choosing."""
    if not PDL1.is_file():
        pytest.skip(f"{PDL1.name} not in this checkout")
    runner = _runner()
    src = tmp_path / "my_target.cif"
    shutil.copy(PDL1, src)
    pdb_id = runner.ingest_local_structure(src)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    yield runner, pdb_id, runner._binder_dirs(run_dir), run_dir
    (_STRUCTURES / f"{pdb_id}.cif").unlink(missing_ok=True)


def _intel(ingested, **kw) -> dict:
    runner, pdb_id, dirs, run_dir = ingested
    return runner._stage_structure_intel(
        pdb_id, "Design a binder.", dirs, PipelineResult(run_dir=run_dir), **kw)


def test_a_local_file_becomes_an_addressable_pseudo_id(ingested):
    """Every later stage builds `<structures_dir>/<ID>.cif`, so the file has to
    land under an id, not keep its own name."""
    _runner_, pdb_id, _dirs, _run = ingested
    assert pdb_id.startswith("LOCAL-")
    assert (_STRUCTURES / f"{pdb_id}.cif").is_file()


def test_ensure_structure_does_not_go_to_rcsb_for_a_local_id(ingested):
    """`LOCAL-MY_TARGET` is not an accession. Falling through to the download
    path would 404 and fail a run whose structure is already on disk."""
    runner, pdb_id, _dirs, _run = ingested
    assert runner._ensure_structure(pdb_id) == _STRUCTURES / f"{pdb_id}.cif"


def test_the_target_chain_is_measured_not_guessed(ingested):
    """The larger side of the largest measured interface becomes the target.

    On this structure that is chain B — PD-L1 — and chain A, the VHH, becomes
    the partner. It is the assignment an LLM stage got backwards on this exact
    entry, which is why it is derived from coordinates here rather than prose.
    """
    intel = _intel(ingested)
    assert intel["target_chain"] == "B"
    assert intel["partner_chain"] == "A"
    assert intel["design_intent"] == "disrupt"
    assert "2,4" in intel["interface_rationale"]      # ~2,449 A^2 buried


def test_the_operator_can_override_the_chain_choice(ingested):
    """Which side of a two-chain complex is 'the target' is a choice, not a
    fact about the structure, so `--chains` has to win."""
    intel = _intel(ingested, chains="A,B")
    assert (intel["target_chain"], intel["partner_chain"]) == ("A", "B")


def test_one_named_chain_selects_single_target_mode(ingested):
    """`disrupt` and `stabilize` both need two chains to compute an interface
    from; a lone chain is a pocket, not an interface."""
    intel = _intel(ingested, chains="B")
    assert intel["partner_chain"] == ""
    assert intel["design_intent"] == "inhibit_active_site"


def test_a_chain_that_is_not_there_is_refused(ingested):
    """Silently ignoring an unknown chain would run the campaign against
    whatever the measured default happened to pick."""
    with pytest.raises(PipelineBlockedError, match="not in"):
        _intel(ingested, chains="Z")


def test_the_inactive_guards_are_named_in_the_report(ingested):
    """Failing open is right for a file no database describes. Failing open
    QUIETLY is not: one of these is the guard that caught a multi-hour campaign
    designed against the wrong molecule."""
    runner, _pdb, dirs, _run = ingested
    _intel(ingested)
    report = (dirs["binder"] / runner._BINDER_STAGE_FILES["target_intel"]).read_text(
        encoding="utf-8")
    assert "_verify_target_chain_assignment" in report
    assert "transmembrane residues will NOT be stripped" in report
    assert "--uniprot" in report


def test_an_accession_switches_the_guards_back_on(ingested):
    intel = _intel(ingested, uniprot="Q9NZQ7")
    runner, _pdb, dirs, _run = ingested
    report = (dirs["binder"] / runner._BINDER_STAGE_FILES["target_intel"]).read_text(
        encoding="utf-8")
    assert intel["target_uniprot"] == "Q9NZQ7"
    assert "Identity checks are ACTIVE" in report


def test_stage_zero_writes_the_artifact_the_stage_machine_resumes_from(ingested):
    """`_run_binder_track` always reads its stage files off disk, whatever
    `start_from` says — so the handoff has to be on disk, not just returned."""
    runner, _pdb, dirs, _run = ingested
    intel = _intel(ingested)
    path = dirs["binder"] / runner._BINDER_STAGE_FILES["target_intel"]
    assert path.is_file()
    loaded = runner._load_binder_handoff(dirs["binder"], "target_intel")
    for key in ("pdb_id", "target_chain", "partner_chain", "design_intent",
                "modality"):
        assert loaded[key] == str(intel[key]), key


class _PromptCaptured(Exception):
    """Carries the query the interface stage would have sent, and stops there."""

    def __init__(self, query: str):
        super().__init__("captured")
        self.query = query


def _interface_prompt(runner, intel, dirs, monkeypatch) -> str:
    """The prompt `_stage_binder_interface` builds, without calling a model."""
    def capture(_skill, query, _files, _out, **_kw):
        raise _PromptCaptured(query)

    monkeypatch.setattr(runner, "_run_stage", capture)
    with pytest.raises(_PromptCaptured) as exc:
        runner._stage_binder_interface(
            intel, dirs, PipelineResult(run_dir=dirs["binder"].parent))
    return exc.value.query


def test_the_interface_prompt_names_the_file_for_a_local_structure(
        ingested, monkeypatch):
    """A local id cannot be looked up, so the prompt has to carry the PATH.

    Given only an unresolvable accession and no path, this skill's documented
    failure is to decide no structure exists and ask for one — which is how the
    PD-L1/7CZD run wasted two stages. Asserted on the built string rather than
    on the source, because a reflection check passes with the branch inverted.
    """
    runner, pdb_id, dirs, _run = ingested
    intel = _intel(ingested)
    prompt = _interface_prompt(runner, intel, dirs, monkeypatch)
    expected = str(runner._binder_structure_path(pdb_id))
    assert expected in prompt, prompt[:200]
    assert f"PDB {pdb_id} (already downloaded" not in prompt


def test_a_real_accession_is_still_described_as_a_pdb_entry(
        ingested, monkeypatch):
    """The local branch must not swallow the normal one: a genuine entry is
    downloaded to a known directory and the skill looks it up by id."""
    runner, _pdb, dirs, _run = ingested
    intel = dict(_intel(ingested), pdb_id="7CZD")
    prompt = _interface_prompt(runner, intel, dirs, monkeypatch)
    assert "PDB 7CZD (already downloaded to data/structures/)" in prompt


def test_the_measured_interface_area_reaches_the_report(ingested):
    """`analyze_interface` returns `interface.bsa_total_A2`, nested; there is no
    flat `bsa_total`. Reading the flat key reported "0 A^2 buried" for a
    2,449 A^2 interface, and `.get`'s default made it silent."""
    import re

    intel = _intel(ingested)
    m = re.search(r"([\d,]+) A\^2 buried", intel["interface_rationale"])
    assert m, intel["interface_rationale"]
    assert int(m.group(1).replace(",", "")) > 2000


def test_the_track_enters_the_binder_machine_at_interface(tmp_path, monkeypatch):
    """It must NOT re-enter at target_intel (stage 0 just ran, deterministically)
    and must NOT skip to trim (nothing has looked at the structure yet, so the
    epitope is still unchosen)."""
    if not PDL1.is_file():
        pytest.skip("7CZD not in this checkout")
    runner = _runner()
    src = tmp_path / "entry_check.cif"
    shutil.copy(PDL1, src)
    pdb_id = runner.ingest_local_structure(src)
    seen = {}

    def fake_track(query, run_dir, result, *, start_from, **kw):
        seen["start_from"] = start_from
        seen["pdb_id"] = result.pdb_id
        return result

    monkeypatch.setattr(runner, "_run_binder_track", fake_track)
    monkeypatch.setattr(runner, "_output_dir_override", tmp_path / "out")
    try:
        runner.run("Design a binder.", pdb_id=pdb_id)
    finally:
        (_STRUCTURES / f"{pdb_id}.cif").unlink(missing_ok=True)
    assert seen["start_from"] == "interface"
    assert seen["pdb_id"] == pdb_id


def test_workflow_choice_is_accepted_by_the_cli_and_the_runner():
    """The parser's choices and the runner's validation must agree, or one of
    them rejects a track the other advertises."""
    import scripts.run_pipeline as rp

    parser = rp._build_parser()
    action = next(a for a in parser._actions if a.dest == "workflow")
    assert "structure" in action.choices
    with pytest.raises(ValueError, match="structure"):
        PipelineRunner(config={}, provider="gemini", workflow="nonsense")
