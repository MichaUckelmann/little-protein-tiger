"""A single site adopts the interface analysis already on disk.

`--stop-after spec|trial` (and `--trial-sites N`) route into
`_run_site_trials`, which used to call `_stage_binder_interface`
unconditionally. On a PPI-bridged run that is a SECOND
`complex-structure-analysis` call, because `_bridge_ppi_to_binder_track` has
already copied the structure stage's own report to the parent
`21_interface.md` and entered at `trim` precisely so it is not paid for twice.

Found on `projects/e2e_foundry_r2` round-4 (2026-09-13): the two calls
DISAGREED about which region was primary — the structure stage kept the
Central Hydrophobic Core and dropped the Basic/Aromatic Flank, the second call
did the reverse — and the spec was built from the later one, i.e. against the
region the structure stage had rejected. `_prepared_site` then reuses that
spec on every resume, so the divergence would have outlived the run that
created it. The cost ($0.09/call) was the lesser half: `--stop-after spec`
exists to preview the campaign, and it was previewing a different one.
"""

from __future__ import annotations

import inspect
import json
import pathlib

import pytest
import yaml

from src.pipeline_runner import PipelineResult, PipelineRunner

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_ARTIFACT = """## PPI ANALYSIS REPORT

### MODEL-READY HOTSPOTS

Target chain A — Region 1: Central Hydrophobic Core — selected 3 of 18 \
interface residues:

| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |
|---|---|---|---|
| PHE | 314 | 122 | CD2,CZ |
| VAL | 318 | 126 | CG1,CG2 |
| VAL | 322 | 130 | CG1,CG2 |

### MODEL-READY HOTSPOTS

Target chain A — Region 2: Basic/Aromatic Flank — selected 2 of 14 \
interface residues:

| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |
|---|---|---|---|
| GLU | 240 | 48 | CD,OE1 |
| TYR | 406 | 214 | CD2,OH |

### PIPELINE HANDOFF
- pdb_id: 3KYS
- target_chain: A
- partner_chain: B
- design_intent: disrupt
"""


@pytest.fixture
def runner():
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    return PipelineRunner(cfg, workflow="structure", design_engine="foundry")


def _dirs(tmp_path, name):
    d = tmp_path / name / "binder"
    d.mkdir(parents=True, exist_ok=True)
    return {"binder": d}


def _no_guards(runner, monkeypatch):
    """The two guards read real structures; they have their own tests."""
    monkeypatch.setattr(runner, "_verify_target_chain_assignment",
                        lambda *a, **k: None)
    monkeypatch.setattr(runner, "_verify_hotspot_grounding",
                        lambda *a, **k: None)


def test_the_parent_artifact_is_adopted_and_the_primary_region_wins(
        runner, tmp_path, monkeypatch):
    _no_guards(runner, monkeypatch)
    parent, site = _dirs(tmp_path, "parent"), _dirs(tmp_path, "site")
    (parent["binder"] / "21_interface.md").write_text(_ARTIFACT,
                                                      encoding="utf-8")
    result = PipelineResult(run_dir=site["binder"], pdb_id="3KYS")

    out = runner._adopt_parent_interface(
        parent, site, {"pdb_id": "3KYS", "target_chain": "A"}, result)

    assert out is not None
    ids = [r["auth_seq_id"] for r in json.loads(out)["residues"]]
    assert ids == [314, 318, 322], "Region 1 only — not the union, not Region 2"
    # The site keeps its own copy, so `--start-from` stays resumable and the
    # site report says what it designed against.
    assert (site["binder"] / "21_interface.md").exists()
    assert result.hotspot_residues_json == out


def test_no_parent_artifact_means_run_the_stage(runner, tmp_path, monkeypatch):
    """A `--workflow binder` run has nothing to adopt, and must not be
    blocked — the caller falls through to the interface stage."""
    _no_guards(runner, monkeypatch)
    parent, site = _dirs(tmp_path, "parent"), _dirs(tmp_path, "site")
    assert runner._adopt_parent_interface(
        parent, site, {"pdb_id": "3KYS"},
        PipelineResult(run_dir=site["binder"])) is None


def test_an_unparsable_artifact_falls_through_rather_than_failing(
        runner, tmp_path, monkeypatch):
    """A file with no hotspot table is a real problem, but re-running the
    stage is the right recovery — not a hard stop before the GPU."""
    _no_guards(runner, monkeypatch)
    parent, site = _dirs(tmp_path, "parent"), _dirs(tmp_path, "site")
    (parent["binder"] / "21_interface.md").write_text(
        "## PPI ANALYSIS REPORT\n\nno table here\n", encoding="utf-8")
    assert runner._adopt_parent_interface(
        parent, site, {"pdb_id": "3KYS"},
        PipelineResult(run_dir=site["binder"])) is None


def test_the_guards_still_run_on_an_adopted_artifact(runner, tmp_path,
                                                     monkeypatch):
    """An adopted file is exactly what a stale artifact could poison, so the
    chain-assignment and grounding checks must not be skipped."""
    parent, site = _dirs(tmp_path, "parent"), _dirs(tmp_path, "site")
    (parent["binder"] / "21_interface.md").write_text(_ARTIFACT,
                                                      encoding="utf-8")
    called = []
    monkeypatch.setattr(runner, "_verify_target_chain_assignment",
                        lambda *a, **k: called.append("chain"))
    monkeypatch.setattr(runner, "_verify_hotspot_grounding",
                        lambda *a, **k: called.append("grounding"))
    runner._adopt_parent_interface(
        parent, site, {"pdb_id": "3KYS", "target_chain": "A"},
        PipelineResult(run_dir=site["binder"]))
    assert called == ["chain", "grounding"]


def test_adoption_is_single_site_only():
    """With `--trial-sites N` every site is a DIFFERENT epitope and must get
    its own analysis — that is the whole point of comparing sites on measured
    yield. Checked by source inspection, the same way
    `tests/test_audit_fixes.py` pins the designable-size gate: the condition
    lives in the caller, and a future edit that drops it would silently make
    every site design against site 1's epitope.
    """
    src = inspect.getsource(PipelineRunner._run_site_trials)
    assert "_adopt_parent_interface" in src
    i = src.index("_adopt_parent_interface")
    assert "len(sites) == 1" in src[i:i + 400], (
        "the single-site condition must gate adoption")
