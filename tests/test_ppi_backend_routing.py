"""C1: which route a PPI run takes, and what each engine gives up by it.

`--design-engine boltzgen` spent its first release on the LEGACY PPI stages
because the hand-off after go/no-go asked `== "foundry"`. Nothing failed — the
run simply had no trim, no measured production size, no `--stop-after` and no
per-stage manifest, and reported a NO_GO from ten designs. These tests pin the
routing itself, which is the only part of that a suite can see without a GPU.

Deliberately behavioural rather than source-inspecting where it can be: the
question is which method a run enters, not how the branch is spelt.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import src.pipeline_runner as pr
from src.pipeline_runner import PipelineRunner

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BRIDGED = ("foundry", "boltzgen")


@pytest.fixture
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


def _runner(config: dict, engine: str, **kw) -> PipelineRunner:
    return PipelineRunner(config, workflow="ppi", design_engine=engine, **kw)


# ── the routing predicate ───────────────────────────────────────────────────

@pytest.mark.parametrize("engine,bridged", [
    ("foundry", True), ("boltzgen", True), ("boltzgen_legacy", False),
])
def test_both_real_generators_bridge_and_only_legacy_does_not(config, engine,
                                                              bridged):
    assert _runner(config, engine)._bridges_to_binder_track is bridged


def test_the_boltzgen_backend_flag_is_not_the_routing_flag(config):
    """Two different questions. `_boltzgen_backend` selects which GENERATOR a
    binder-track stage dispatches to; `_bridges_to_binder_track` selects
    whether a PPI run reaches those stages at all. `boltzgen_legacy` answers
    False to both — it neither bridges nor dispatches — and conflating them
    would send it into the new stages under the old name."""
    legacy = _runner(config, "boltzgen_legacy")
    assert legacy._boltzgen_backend is False
    assert legacy._bridges_to_binder_track is False

    bg = _runner(config, "boltzgen")
    assert bg._boltzgen_backend is True and bg._bridges_to_binder_track is True

    fo = _runner(config, "foundry")
    assert fo._boltzgen_backend is False and fo._bridges_to_binder_track is True


# ── the hand-off after go/no-go ─────────────────────────────────────────────

def _go_context(tmp_path: pathlib.Path) -> pathlib.Path:
    """A literature handoff on disk, so `run()` can be entered at the stage
    right after the go/no-go decision without stubbing the three LLM stages
    (and without needing a structure on disk for stage 1.5). `tractability`
    is what routes it into `literature_handoff` — see `run()`'s seeding
    heuristic."""
    path = tmp_path / "01_literature.md"
    path.write_text(
        "# Literature\n\n### PIPELINE HANDOFF\n"
        "- go_recommendation: GO\n"
        "- go_rationale: a clear, well-evidenced PPI\n"
        "- tractability: HIGH\n"
        "- design_intent: disrupt\n"
        "- modality: mini_protein\n",
        encoding="utf-8")
    return path


@pytest.mark.parametrize("engine", _BRIDGED)
def test_a_go_decision_enters_the_bridge_on_either_generator(
        config, tmp_path, monkeypatch, engine):
    """The bug C1 fixes, stated as a test: BoltzGen must take the same route
    foundry does, so that trim / spec / calibration / production / scoring all
    dispatch through `_run_binder_track` rather than being skipped."""
    seen: dict = {}
    monkeypatch.setattr(
        PipelineRunner, "_bridge_ppi_to_binder_track",
        lambda self, q, rd, res, **kw: seen.setdefault("engine",
                                                       self._design_engine))
    monkeypatch.setattr(PipelineRunner, "_init_ledger", lambda self, rd: None)
    monkeypatch.setattr(PipelineRunner, "_stage_design",
                        lambda self, *a, **k: pytest.fail(
                            "a bridged engine must not reach the legacy "
                            "design stage"))

    r = _runner(config, engine, project=object(), output_dir=tmp_path / "run")
    r.run("q", start_from="design", context_file=_go_context(tmp_path))
    assert seen["engine"] == engine


def test_legacy_still_reaches_the_old_design_stage(config, tmp_path,
                                                   monkeypatch):
    """Decision 2: the legacy path stays alive as the regression check for the
    one it replaced, so it must remain genuinely reachable — by name."""
    reached: list[str] = []
    monkeypatch.setattr(
        PipelineRunner, "_bridge_ppi_to_binder_track",
        lambda self, *a, **k: pytest.fail("legacy must not bridge"))
    monkeypatch.setattr(PipelineRunner, "_init_ledger", lambda self, rd: None)
    for stage in ("_stage_design", "_stage_execution", "_stage_analysis",
                  "_stage_summary"):
        monkeypatch.setattr(PipelineRunner, stage,
                            lambda self, *a, s=stage, **k: reached.append(s))
    monkeypatch.setattr(PipelineRunner, "_generate_ppi_report",
                        lambda self, rd: None)

    r = _runner(config, "boltzgen_legacy", project=object(),
                output_dir=tmp_path / "run")
    r.run("q", start_from="design", context_file=_go_context(tmp_path))
    assert reached == ["_stage_design", "_stage_execution", "_stage_analysis",
                       "_stage_summary"]


# ── resuming mid-campaign ───────────────────────────────────────────────────

@pytest.mark.parametrize("engine", _BRIDGED)
def test_a_binder_stage_resume_dispatches_straight_into_the_track(
        config, tmp_path, monkeypatch, engine):
    """A fresh process resuming `--start-from production` has no PPI stage to
    re-enter. This worked for foundry and silently did not exist for BoltzGen,
    whose legacy path has no stage by that name at all."""
    captured: dict = {}
    monkeypatch.setattr(PipelineRunner, "_run_binder_track",
                        lambda self, q, rd, res, **kw: captured.update(kw) or res)
    monkeypatch.setattr(PipelineRunner, "_init_ledger", lambda self, rd: None)

    r = _runner(config, engine, project=object(), output_dir=tmp_path / "run")
    r.run("q", start_from="production")
    assert captured.get("start_from") == "production"


def test_a_binder_stage_name_is_refused_under_the_legacy_engine(
        config, tmp_path, monkeypatch):
    """It has no stage called "production", so the alternative to refusing is
    starting at pathway and re-paying for every LLM stage."""
    monkeypatch.setattr(PipelineRunner, "_init_ledger", lambda self, rd: None)
    r = _runner(config, "boltzgen_legacy", project=object(),
                output_dir=tmp_path / "run")
    with pytest.raises(pr.PipelineError, match="production"):
        r.run("q", start_from="production")


# ── the CLI's own gates ─────────────────────────────────────────────────────

def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "run_pipeline.py"), *argv],
        capture_output=True, text=True)


def test_the_legacy_engine_is_no_longer_offered_by_the_cli():
    """Step 0 of LEGACY_RETIREMENT_SCOPE.md: the name is refused at the CLI,
    on every track, rather than accepted or silently mapped onto `boltzgen`.

    Mapping it would be the worse failure: the two are not the same campaign
    — the legacy stages honour neither --stop-after nor a calibration verdict
    — so an operator who asked for one and got the other would be told
    nothing. The flag-specific refusals this replaces (--stop-after,
    --compute/--n-gpus on legacy) are subsumed: you cannot reach them.
    """
    proc = _cli("--workflow", "ppi", "--query", "x", "--project", "p",
                "--design-engine", "boltzgen_legacy",
                "--stop-after", "calibration")
    assert proc.returncode != 0
    # argparse rejects the value before the body runs, so either message is a
    # pass; what matters is that no run starts.
    assert ("invalid choice" in proc.stderr or "retired" in proc.stderr)


def test_a_config_supplied_legacy_backend_is_refused_too():
    """`design.backend` can still name it, which is why the body's check
    survives the choices list losing the value."""
    import textwrap

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("design", {})["backend"] = "boltzgen_legacy"
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        alt = Path(td) / "config.yaml"
        alt.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, yaml, pathlib
                sys.path.insert(0, {str(_ROOT)!r})
                sys.argv = ["run_pipeline.py", "--workflow", "ppi",
                            "--query", "x", "--project", "p"]
                import scripts.run_pipeline as rp
                rp._load_config = lambda *a, **k: yaml.safe_load(
                    pathlib.Path({str(alt)!r}).read_text())
                rp.main()
            """)], capture_output=True, text=True, cwd=str(_ROOT))
    assert proc.returncode != 0
    assert "retired" in (proc.stderr + proc.stdout), proc.stderr[-400:]


@pytest.mark.parametrize("engine", _BRIDGED)
def test_stop_after_is_accepted_on_both_bridged_engines(engine):
    """It reaches the binder-track stages on either, so it must not be
    refused there. Checked by the absence of a parse error, not by running —
    the next thing this invocation would do is spend tokens."""
    proc = _cli("--workflow", "ppi", "--query", "x", "--project", "p",
                "--design-engine", engine, "--stop-after", "calibration",
                "--help")
    assert proc.returncode == 0


def test_the_legacy_engine_is_refused_on_every_track_by_the_cli():
    """Was "--workflow ppi only"; retirement makes it every track."""
    proc = _cli("--workflow", "binder", "--target", "KRAS", "--project", "p",
                "--design-engine", "boltzgen_legacy")
    assert proc.returncode != 0
    assert ("invalid choice" in proc.stderr or "retired" in proc.stderr)


def test_the_runner_still_accepts_it_for_a_library_caller_on_the_ppi_track():
    """Step 0 is a DE-ADVERTISEMENT, not a removal, and this pins the seam.

    `UNIFY_BOLTZGEN_BACKEND_NOTES.md`'s decision 2 asked that the path stay
    genuinely reachable while it is kept, and `scripts/e2e_ppi_boltzgen.py`
    — documented there as the only safe way to run it, since a bare CLI
    invocation would run 332 GPU-h uncapped — constructs `PipelineRunner`
    directly. So the runner-level refusal stays scoped to non-PPI tracks
    until the stage chain itself is deleted (step 2 of the scope). When that
    happens, DELETE THIS TEST rather than loosening it.
    """
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    runner = PipelineRunner(cfg, workflow="ppi",
                            design_engine="boltzgen_legacy",
                            project=None, round_id=None)
    assert runner._design_engine == "boltzgen_legacy"
    assert runner._boltzgen_backend is False
    assert runner._bridges_to_binder_track is False

    with pytest.raises(ValueError, match="ppi only"):
        PipelineRunner(cfg, workflow="binder",
                       design_engine="boltzgen_legacy")


def test_cluster_flags_are_refused_on_boltzgen_rather_than_run_locally():
    """`_run_boltzgen_stage` has no cluster path — only the foundry stages
    stage onto shared storage. Accepting --compute cluster would run the whole
    campaign on the local GPU while the operator waited for a submission
    script."""
    for extra in (("--compute", "cluster"), ("--n-gpus", "6")):
        proc = _cli("--workflow", "ppi", "--query", "x", "--project", "p",
                    "--design-engine", "boltzgen", *extra)
        assert proc.returncode != 0, extra
        assert "foundry-only" in proc.stderr, proc.stderr


def test_the_success_metric_flag_writes_the_block_its_engine_reads():
    """Two ranking blocks. BoltzGen's gate is BoltzGen-native columns
    (`design.boltzgen_ranking`, `resolve_boltzgen_ranking`); foundry's is
    `design.binder_ranking`. Writing the flag into the wrong one is silent —
    the campaign is simply sized at the default bar."""
    import scripts.run_pipeline as rp

    src = pathlib.Path(rp.__file__).read_text(encoding="utf-8")
    assert '"boltzgen_ranking" if design_engine == "boltzgen"' in src


def test_ipsae_is_refused_as_a_boltzgen_sizing_metric():
    """BoltzGen writes no PAE matrix, so ipSAE cannot be computed from its
    output: its own ipsae column spans 0.0000-0.0289 against an
    RF3-calibrated bar of 0.5, and every campaign would size to STOP."""
    proc = _cli("--workflow", "ppi", "--query", "x", "--project", "p",
                "--design-engine", "boltzgen", "--success-metric", "ipsae_min")
    assert proc.returncode != 0 and "foundry-only" in proc.stderr


# ── what the bridge hands over ──────────────────────────────────────────────

def test_the_bridge_reads_no_design_engine_at_all():
    """It is one route for both generators, so a backend-specific line in it
    is a second dispatch waiting to disagree with `_run_binder_track`'s."""
    import inspect

    src = inspect.getsource(PipelineRunner._bridge_ppi_to_binder_track)
    assert "_design_engine" in src, "it may NAME the engine in a message"
    assert "_boltzgen_backend" not in src
    assert '== "foundry"' not in src and '== "boltzgen"' not in src


def test_the_bridged_binder_length_comes_from_the_modality(config, tmp_path,
                                                           monkeypatch):
    """A cyclic-peptide campaign is 12-15 residues and a mini-protein one
    70-86. The bridge used to hardcode `size.get("min", 70)`, which is the
    mistake commit 5c17881 fixed once already one level up — and BoltzGen is
    the engine that can actually be asked for a macrocycle, so the bridge is
    now a path that reaches it."""
    monkeypatch.setattr(PipelineRunner, "_run_binder_track",
                        lambda self, q, rd, res, **kw: res)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    structure_md = run_dir / "02_structure.md"
    structure_md.write_text(
        "# S\n\n### PIPELINE HANDOFF\n- target_chain: D\n- partner_chain: A\n",
        encoding="utf-8")

    for modality, expected in (("cyclic_peptide", (12, 15)),
                               ("mini_protein", (70, 86))):
        r = PipelineRunner(config, workflow="ppi", design_engine="boltzgen",
                           modality=modality)
        result = pr.PipelineResult(run_dir=run_dir, pdb_id="3N7S",
                                   target_complex="CALCRL / RAMP1")
        result.stage_files["structure"] = structure_md
        result.hotspot_residues_json = json.dumps(
            {"target_chain": "D", "residues": [{"residue": "TRP",
                                                "auth_seq_id": 74}]})
        r._bridge_ppi_to_binder_track("q", run_dir, result, auto_mode=True)
        intel = r._load_binder_handoff(
            r._binder_dirs(run_dir)["binder"], "target_intel")
        got = (int(intel["binder_length_min"]), int(intel["binder_length_max"]))
        assert got == expected, f"{modality}: {got}"
