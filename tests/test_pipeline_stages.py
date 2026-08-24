"""Stage <-> skill resolution, model config layering, and budget wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.pipeline_runner import (
    _DEFAULT_MODELS, _STAGE_TO_SKILL, PipelineError, PipelinePausedError,
    PipelineRunner, _stage_for_skill, _TrimFromDisk,
)

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def runner(config) -> PipelineRunner:
    # Explicit provider="claude": this fixture backs tests that specifically
    # validate Claude-path model resolution (haiku/thinking upgrades, the
    # structure/interface stage inversion under the claude default model) —
    # not "whatever the pipeline's overall default provider happens to be".
    # See test_pipeline_defaults_to_gemini for that.
    return PipelineRunner(config, workflow="ppi", provider="claude")


# ----------------------------------------------------------------------
# The shared-skill collision
# ----------------------------------------------------------------------

def test_shared_skills_exist_and_make_the_inversion_ambiguous():
    """
    Several skills serve more than one stage. _stage_for_skill is first-match-
    wins, so it CANNOT distinguish them — which is why callers pass stage=.
    """
    from collections import Counter

    counts = Counter(_STAGE_TO_SKILL.values())
    shared = {sk for sk, n in counts.items() if n > 1}
    assert "complex-structure-analysis" in shared   # structure | interface
    assert "design-analyst" in shared               # summary | binder_summary


def test_explicit_stage_beats_the_ambiguous_inversion(runner):
    """
    Without stage=, 'interface' would silently resolve to 'structure' (declared
    first) and take the structure stage's model and manifest slot.
    """
    assert _stage_for_skill("complex-structure-analysis") == "structure"
    for stage in ("structure", "interface"):
        model, _, provider = runner._resolve_stage(
            "complex-structure-analysis", stage)
        assert model == runner._default_model
        assert provider == "claude"

    # design-analyst defaults to Haiku on `summary` and `binder_summary`, but
    # the ambiguous inversion only ever finds `summary`.
    assert "haiku" in runner._resolve_stage("design-analyst", "binder_summary")[0]
    assert "haiku" in runner._resolve_stage("design-analyst", "summary")[0]


def test_every_binder_llm_stage_maps_to_a_skill():
    for stage in ("target_intel", "interface", "binder_summary"):
        assert stage in _STAGE_TO_SKILL


def test_deterministic_stages_have_no_skill_entry():
    """Matches the execution/analysis convention for non-LLM stages."""
    for stage in ("trim", "binder_spec", "pilot", "calibration",
                  "production", "binder_scoring", "execution", "analysis"):
        assert stage not in _STAGE_TO_SKILL


# ----------------------------------------------------------------------
# Model configuration
# ----------------------------------------------------------------------

def test_config_models_block_supplies_the_default(config, runner):
    assert runner._default_model == config["models"]["claude"]["default"]
    assert runner._default_model == "claude-sonnet-5"


def test_explicit_model_id_overrides_config(config):
    r = PipelineRunner(config, model_id="claude-opus-5")
    assert r._resolve_stage("pathway-expert", "pathway")[0] == "claude-opus-5"


def test_falls_back_to_the_code_table_without_a_models_block(config):
    stripped = {k: v for k, v in config.items() if k != "models"}
    # No explicit provider -> exercises the actual default (gemini).
    assert PipelineRunner(stripped)._default_model == _DEFAULT_MODELS["gemini"]


def test_pipeline_defaults_to_gemini(config):
    """gemini-3.7-flash is the pipeline-wide default now: cheaper, and this
    pipeline's own ledger shows it answering target-intel/interface prompts
    claude-sonnet-5 routinely refuses (category 'bio')."""
    r = PipelineRunner(config)
    assert r.provider == "gemini"
    assert r._default_model == "gemini-3.7-flash"
    model, _, provider = r._resolve_stage("pathway-expert", "pathway")
    assert provider == "gemini"
    assert model == "gemini-3.7-flash"


def test_extended_thinking_upgrades_off_haiku(config):
    # Extended thinking is a Claude-only mechanic (use_thinking is gated on
    # provider == "claude" in _resolve_stage) — explicit provider here tests
    # that mechanic specifically, not the pipeline's overall default.
    r = PipelineRunner(config, provider="claude", extended_thinking_stages={"summary"})
    model, thinking, _ = r._resolve_stage("design-analyst", "summary")
    assert thinking is True
    assert "haiku" not in model.lower()


def test_no_stale_model_ids_in_the_config():
    """budget_tokens was removed on these models; 4-6 ids predate that change."""
    for name in ("config.yaml",
                 "config_flagship_journals.yaml", "config_search_expansion.yaml",
                 "config_with_complexes.yaml"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "claude-sonnet-4-6" not in text, name
        assert "claude-haiku-4-5-20251001" not in text, name


def test_no_stale_model_ids_in_docs_and_cli_help():
    """
    Same stale-id ban as test_no_stale_model_ids_in_the_config, extended to
    README.md and the run_pipeline.py --model-id help text — an example
    model id in prose/CLI help goes stale exactly like a config value does,
    and users copy-paste from both.
    """
    for name in ("README.md", "scripts/run_pipeline.py"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "claude-sonnet-4-6" not in text, name
        assert "claude-opus-4-6" not in text, name
        assert "claude-haiku-4-5-20251001" not in text, name


def test_thinking_uses_adaptive_not_a_removed_token_budget():
    """
    `budget_tokens` is rejected with a 400 on claude-sonnet-5 / claude-opus-5.
    This guards the one change that would break every extended-thinking stage.
    """
    src = (_ROOT / "src" / "skill_runner.py").read_text(encoding="utf-8")
    assert '"budget_tokens"' not in src
    assert '{"type": "adaptive"}' in src


# ----------------------------------------------------------------------
# Workflow guard + budget
# ----------------------------------------------------------------------

@pytest.mark.parametrize("workflow", ["ppi", "binder"])
def test_known_workflows_are_accepted(config, workflow):
    assert PipelineRunner(config, workflow=workflow)._workflow == workflow


def test_unknown_workflow_is_rejected(config):
    with pytest.raises(ValueError, match="Invalid workflow"):
        PipelineRunner(config, workflow="nonsense")


def test_ledger_is_created_beside_the_run_and_replays(config, tmp_path):
    from src.token_budget import Usage

    r = PipelineRunner(config, output_dir=tmp_path / "run", budget_usd=5.0)
    r._init_ledger(tmp_path / "run")
    assert r._ledger is not None and r._ledger.cap_usd == 5.0
    r._ledger.record(stage="pathway", skill="pathway-expert", provider="claude",
                     model="claude-sonnet-5", usage=Usage(input_tokens=1_000_000))
    assert (tmp_path / "run" / "ledger.jsonl").exists()

    r2 = PipelineRunner(config, output_dir=tmp_path / "run", budget_usd=5.0)
    r2._init_ledger(tmp_path / "run")
    assert r2._ledger.spent_usd == pytest.approx(r._ledger.spent_usd)


def test_budget_overrun_pauses_with_a_working_resume_command(config, tmp_path):
    from src.token_budget import BudgetExceeded, Usage

    r = PipelineRunner(config, output_dir=tmp_path, budget_usd=0.01)
    r._init_ledger(tmp_path)
    exc = BudgetExceeded(spent_usd=0.02, projected_usd=1.0, cap_usd=0.01,
                         stage="after:structure")
    with pytest.raises(PipelinePausedError) as raised:
        r._budget_pause(exc)
    payload = raised.value.payload
    assert raised.value.pause_point == "budget_exceeded"
    assert payload["next_stage"] == "structure"     # the "after:" prefix is stripped
    assert "--start-from structure" in payload["resume"]


def test_no_budget_means_no_cap(config, tmp_path):
    r = PipelineRunner(config, output_dir=tmp_path)
    r._init_ledger(tmp_path)
    assert r._ledger.cap_usd is None
    assert r._ledger.remaining_usd is None


# ----------------------------------------------------------------------
# Binder track wiring
# ----------------------------------------------------------------------

def test_binder_stage_order_and_files_agree(config):
    r = PipelineRunner(config, workflow="binder")
    assert set(r.BINDER_STAGE_ORDER) == set(r._BINDER_STAGE_FILES)
    # File prefixes must sort in execution order, so a run directory reads
    # top-to-bottom the way the pipeline ran.
    names = [r._BINDER_STAGE_FILES[s] for s in r.BINDER_STAGE_ORDER]
    assert names == sorted(names)


def test_binder_deterministic_stages_have_no_skill(config):
    r = PipelineRunner(config, workflow="binder")
    for stage in r._BINDER_DETERMINISTIC:
        assert stage not in _STAGE_TO_SKILL
    for stage in ("target_intel", "interface", "binder_summary"):
        assert stage in _STAGE_TO_SKILL


def test_binder_resume_rejects_a_non_binder_stage(config, tmp_path):
    from src.pipeline_runner import PipelineError, PipelineResult

    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder")
    with pytest.raises(PipelineError, match="Unknown binder start_from"):
        r._run_binder_track("q", tmp_path, PipelineResult(run_dir=tmp_path),
                            start_from="theozyme")


def test_binder_summary_context_withholds_sequences(config, tmp_path):
    """
    Sequences are omitted on purpose: they are not needed to review a ranking,
    and including them reliably triggers a biosecurity refusal. The orderable
    FASTA is written deterministically instead.
    """
    import csv

    csv_path = tmp_path / "top_k.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "ipsae_min", "binder_seq",
                                           "binder_len", "binder_rmsd_dock"])
        w.writeheader()
        w.writerow({"name": "d1", "ipsae_min": "0.61", "binder_seq": "ACDEFGHIKL",
                    "binder_len": "10", "binder_rmsd_dock": "1.2"})

    slim = PipelineRunner._slim_binder_top_k(csv_path)
    assert "ACDEFGHIKL" not in slim
    assert "binder_seq" not in slim
    assert "ipsae_min" in slim and "binder_len" in slim

    fasta = PipelineRunner._write_binder_fasta(csv_path, tmp_path / "top_k.fasta")
    assert fasta is not None
    assert "ACDEFGHIKL" in fasta.read_text()


def test_binder_track_requires_a_project_via_the_cli():
    """Multi-day GPU campaigns are only resumable through the manifest."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "run_pipeline.py"),
         "--workflow", "binder", "--target", "KRAS"],
        capture_output=True, text=True)
    assert proc.returncode != 0
    assert "requires --project" in proc.stderr


def test_target_flag_is_rejected_outside_the_binder_track():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "run_pipeline.py"),
         "--workflow", "ppi", "--query", "x", "--target", "KRAS"],
        capture_output=True, text=True)
    assert proc.returncode != 0
    assert "--target applies to --workflow binder only" in proc.stderr


# ----------------------------------------------------------------------
# Safety-classifier refusals
# ----------------------------------------------------------------------

def test_a_refusal_raises_rather_than_returning_an_empty_report():
    """
    The old behaviour warned and returned "", which was written to disk as a
    0-byte stage report; the run then failed three stages later with a confusing
    "no MODEL-READY HOTSPOTS" error. Refusals must surface where they happen.
    """
    from src.skill_runner import SkillRefusedError

    exc = SkillRefusedError(skill="complex-structure-analysis",
                            model="claude-sonnet-5", category="bio", iteration=1)
    assert "refused" in str(exc)
    assert "bio" in str(exc)
    assert exc.model == "claude-sonnet-5"


def test_refusal_fallback_chain_is_configured_and_excludes_the_default(config):
    """
    Refusals are model- AND query-dependent: claude-sonnet-5 refuses the
    structure-analysis prompt outright, claude-opus-5 answers for some targets
    and declines for others. Hence a chain, and none of it may be the model that
    already refused.
    """
    from src.pipeline_runner import _REFUSAL_FALLBACK_MODELS

    r = PipelineRunner(config, workflow="binder")
    chain = r._models_cfg.get("refusal_fallbacks") or _REFUSAL_FALLBACK_MODELS
    assert len(chain) >= 2
    assert r._default_model not in chain


def test_refusal_is_retried_on_the_fallback_model(config, tmp_path, monkeypatch):
    from src.skill_runner import SkillRefusedError, SkillRunner

    calls: list[str] = []
    original_init = SkillRunner.__init__

    def fake_init(self, *a, **kw):
        original_init(self, *a, **kw)
        calls.append(self.model_id)

    def fake_run(self, query, context_text=None, trace_path=None):
        if self.model_id == "claude-sonnet-5":
            raise SkillRefusedError(skill=self.skill_name, model=self.model_id,
                                    category="bio", iteration=1)
        return "ok\n\n### PIPELINE HANDOFF\n- go_recommendation: GO\n"

    monkeypatch.setattr(SkillRunner, "__init__", fake_init)
    monkeypatch.setattr(SkillRunner, "run", fake_run)
    monkeypatch.setattr(SkillRunner, "usage", lambda self: __import__(
        "src.token_budget", fromlist=["Usage"]).Usage())

    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder", provider="claude")
    handoff = r._run_stage("complex-structure-analysis", "q", [],
                           tmp_path / "out.md", stage="interface")
    assert handoff.get("go_recommendation") == "GO"
    assert calls[0] == "claude-sonnet-5"
    first_fallback = (r._models_cfg.get("refusal_fallbacks") or [])[0]
    assert calls[1] == first_fallback.split(":", 1)[-1]   # strip a "provider:" prefix


def test_thinking_blocks_round_trip_with_their_signature():
    """
    A replayed thinking block without its signature is rejected with
    `messages.N.content.0.thinking.signature: Field required`, which killed every
    extended-thinking stage.
    """
    src = (_ROOT / "src" / "skill_runner.py").read_text(encoding="utf-8")
    assert '"signature"' in src
    assert "redacted_thinking" in src


def test_placeholder_chain_ids_are_rejected_at_parse_time(config):
    """
    The skill has emitted `target_chain: TBD (PD-L1)`. Passing that through fails
    four stages later inside gemmi with an unhelpful "chain not found".
    """
    from src.pipeline_runner import PipelineBlockedError

    r = PipelineRunner(config, workflow="binder")
    good = r._binder_sites({
        "pdb_id": "6VJJ", "target_chain": "A", "partner_chain": "B",
        "sites_json": json.dumps([
            {"site_id": "ok", "pdb_id": "6VJJ", "target_chain": "A",
             "partner_chain": "B"},
            {"site_id": "bad", "pdb_id": "7CZD", "target_chain": "TBD (PD-L1)",
             "partner_chain": "H"},
        ]),
    }, limit=5)
    assert [s["site_id"] for s in good] == ["ok"]

    with pytest.raises(PipelineBlockedError, match="usable chains"):
        r._binder_sites({"pdb_id": "7CZD", "target_chain": "TBD (PD-L1)",
                         "partner_chain": "H"}, limit=1)


def test_refusal_chain_reports_every_model_that_declined(config, tmp_path,
                                                         monkeypatch):
    """"Two other models declined too" is information, not a reason to retry on."""
    from src.skill_runner import SkillRefusedError, SkillRunner

    def always_refuse(self, query, context_text=None, trace_path=None):
        raise SkillRefusedError(skill=self.skill_name, model=self.model_id,
                                category="bio", iteration=1)

    monkeypatch.setattr(SkillRunner, "run", always_refuse)
    monkeypatch.setattr(SkillRunner, "usage", lambda self: __import__(
        "src.token_budget", fromlist=["Usage"]).Usage())

    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder")
    with pytest.raises(SkillRefusedError) as exc:
        r._run_stage("complex-structure-analysis", "q", [], tmp_path / "o.md",
                     stage="interface")
    assert "claude-opus-5" in str(exc.value)


def test_oversized_tool_results_are_truncated(config, monkeypatch):
    """
    analyze_interface on a large complex returned 130k+ tokens of per-residue
    contacts, which blew the per-call input limit three calls into the loop.
    A result the model cannot read is worse than a truncated one it can.
    """
    from src.skill_runner import SkillRunner

    r = SkillRunner("complex-structure-analysis", "claude", "claude-sonnet-5",
                    config)
    monkeypatch.setattr(SkillRunner, "_execute_tool_inner",
                        lambda self, name, d: "x" * 500_000)
    out = r._execute_tool("tool_analyze_interface", {})
    assert len(out) < 500_000
    assert "TRUNCATED" in out
    assert "500,000 characters" in out


def test_normal_tool_results_pass_through_untouched(config, monkeypatch):
    from src.skill_runner import SkillRunner

    r = SkillRunner("complex-structure-analysis", "claude", "claude-sonnet-5",
                    config)
    monkeypatch.setattr(SkillRunner, "_execute_tool_inner",
                        lambda self, name, d: '{"ok": true}')
    assert r._execute_tool("tool_analyze_interface", {}) == '{"ok": true}'


def test_a_malformed_hotspot_table_fails_at_the_trim_not_inside_gemmi(config,
                                                                      tmp_path):
    """
    The hotspot table is parsed out of markdown; a malformed one yielded an empty
    chain id that surfaced four stages later as "chain '' not found".
    """
    from src.pipeline_runner import PipelineError, PipelineResult

    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder")
    dirs = r._binder_dirs(tmp_path)
    res = PipelineResult(run_dir=tmp_path, pdb_id="3KYS")
    for bad in ('{"target_chain": "", "partner_chain": "B", "residues": [{}]}',
                '{"target_chain": "TBD (X)", "partner_chain": "B", "residues": [{}]}'):
        with pytest.raises(PipelineError, match="auth chain id"):
            r._stage_trim({}, bad, dirs, res)
    with pytest.raises(PipelineError, match="no residues"):
        r._stage_trim({}, '{"target_chain": "A", "partner_chain": "B", '
                          '"residues": []}', dirs, res)


def test_a_stage_can_be_routed_to_another_provider(config):
    """
    `provider:model` on a per-stage override. This is how a stage whose prompt
    one provider's safety classifier declines gets routed elsewhere without
    moving the whole pipeline — the binder track's `interface` stage is refused
    by claude-sonnet-5 with category "bio".
    """
    cfg = json.loads(json.dumps(config))
    cfg["models"]["claude"]["stages"]["interface"] = "gemini:gemini-3.7-flash"
    r = PipelineRunner(cfg, workflow="binder", provider="claude")

    model, thinking, provider = r._resolve_stage("complex-structure-analysis",
                                                 "interface")
    assert (model, provider) == ("gemini-3.7-flash", "gemini")
    # Extended thinking is Claude-only and must not leak across.
    assert thinking is False

    # Other stages are untouched.
    assert r._resolve_stage("complex-structure-analysis", "structure")[2] == "claude"


def test_gemini_models_are_priced():
    """An unpriced model silently understates the ledger."""
    from src.token_budget import rates_for

    r = rates_for("gemini-3.7-flash")
    assert r is not None and r.input_per_mtok > 0


def test_gemini_is_the_immediate_fallback_no_other_claude_model_is_tried(
        config, tmp_path, monkeypatch):
    """
    Regression for real ledger waste: on three separate interface-stage
    refusals for one target, claude-sonnet-5 refused, then claude-opus-5 ALSO
    refused (same category, ~$0.13 spent for nothing each time) before
    claude-haiku-4-5 finally answered. Crossing providers on the FIRST refusal
    avoids paying for a same-family retry that has never once succeeded here —
    so exactly one fallback attempt (Gemini) must be made, never opus or haiku.
    """
    from src.skill_runner import SkillRefusedError, SkillRunner

    seen: list[tuple[str, str]] = []
    original_init = SkillRunner.__init__

    def fake_init(self, *a, **kw):
        original_init(self, *a, **kw)
        seen.append((self.provider, self.model_id))

    def fake_run(self, query, context_text=None, trace_path=None):
        if self.provider == "claude" and self.model_id == "claude-sonnet-5":
            raise SkillRefusedError(skill=self.skill_name, model=self.model_id,
                                    category="bio", iteration=1)
        if self.provider != "gemini":
            raise AssertionError(
                f"wasted a call on {self.provider}:{self.model_id} — the "
                f"immediate fallback should have gone straight to Gemini")
        return "ok\n\n### PIPELINE HANDOFF\n- go_recommendation: GO\n"

    monkeypatch.setattr(SkillRunner, "__init__", fake_init)
    monkeypatch.setattr(SkillRunner, "run", fake_run)
    monkeypatch.setattr(SkillRunner, "usage", lambda self: __import__(
        "src.token_budget", fromlist=["Usage"]).Usage())

    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder", provider="claude")
    handoff = r._run_stage("complex-structure-analysis", "q", [],
                           tmp_path / "o.md", stage="interface")
    assert handoff.get("go_recommendation") == "GO"
    # Exactly two SkillRunners: the original refusal, then Gemini. Never opus,
    # never haiku.
    assert seen == [("claude", "claude-sonnet-5"), ("gemini", "gemini-3.7-flash")]


def test_gemini_leads_the_configured_fallback_chain(config):
    """
    Gemini must be tried before any same-provider Claude fallback — that
    ordering is what makes the immediate-switch behaviour above the DEFAULT,
    not an incidental mock artifact.
    """
    chain = config["models"]["claude"]["refusal_fallbacks"]
    assert chain, "no fallback chain configured"
    assert chain[0].startswith("gemini"), (
        "Gemini must lead the chain — trying another Claude model first pays "
        "for a same-family refusal that measurably never succeeds")


def test_if_gemini_also_refuses_the_chain_still_falls_through(config, tmp_path,
                                                              monkeypatch):
    """Defense in depth: a Gemini-side decline must not strand the stage."""
    from src.skill_runner import SkillRefusedError, SkillRunner

    def fake_run(self, query, context_text=None, trace_path=None):
        if self.model_id in ("claude-sonnet-5", "gemini-3.7-flash"):
            raise SkillRefusedError(skill=self.skill_name, model=self.model_id,
                                    category="bio", iteration=1)
        return "ok\n\n### PIPELINE HANDOFF\n- go_recommendation: GO\n"

    monkeypatch.setattr(SkillRunner, "run", fake_run)
    monkeypatch.setattr(SkillRunner, "usage", lambda self: __import__(
        "src.token_budget", fromlist=["Usage"]).Usage())

    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder")
    handoff = r._run_stage("complex-structure-analysis", "q", [],
                           tmp_path / "o.md", stage="interface")
    assert handoff.get("go_recommendation") == "GO"


def test_gemini_prompt_level_block_is_a_refusal_not_a_crash(monkeypatch):
    """
    A prompt Gemini declines outright returns an EMPTY candidates list and a
    top-level promptFeedback.blockReason — no content to index into. Without
    detecting this, `_run_gemini` would crash on a raw IndexError instead of
    letting the fallback chain handle it like any other refusal.
    """
    import sys
    sys.path.insert(0, str(_ROOT))
    from src.skill_runner import SkillRefusedError, SkillRunner
    import requests as _requests

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}

    r = SkillRunner.__new__(SkillRunner)
    r.skill_name = "complex-structure-analysis"
    r.model_id = "gemini-3.7-flash"
    r.system_prompt = "x"
    r.max_iter = 3
    monkeypatch.setattr(_requests, "post", lambda *a, **k: FakeResp())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(SkillRefusedError, match="SAFETY"):
        r._run_gemini([{"role": "user", "parts": [{"text": "q"}]}])


def test_gemini_per_candidate_safety_stop_is_a_refusal_not_a_crash(monkeypatch):
    """
    A per-candidate safety stop looks like a normal candidate but carries
    finishReason SAFETY/PROHIBITED_CONTENT/... and no `content` key — indexing
    into it directly raises a raw KeyError instead of a handleable refusal.
    """
    import sys
    sys.path.insert(0, str(_ROOT))
    from src.skill_runner import SkillRefusedError, SkillRunner
    import requests as _requests

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"candidates": [{"finishReason": "PROHIBITED_CONTENT"}]}

    r = SkillRunner.__new__(SkillRunner)
    r.skill_name = "complex-structure-analysis"
    r.model_id = "gemini-3.7-flash"
    r.system_prompt = "x"
    r.max_iter = 3
    monkeypatch.setattr(_requests, "post", lambda *a, **k: FakeResp())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(SkillRefusedError, match="PROHIBITED_CONTENT"):
        r._run_gemini([{"role": "user", "parts": [{"text": "q"}]}])


def test_a_prepared_site_is_reused_rather_than_re_run(config, tmp_path):
    """
    The interface stage is the expensive, refusal-prone one. A resumed run must
    reach the GPU without paying for it again — and a multi-day campaign will be
    resumed.
    """
    r = PipelineRunner(config, workflow="binder")
    dirs = r._binder_dirs(tmp_path)
    assert r._prepared_site(dirs) is None            # nothing there yet

    # A spec with no trim map cannot be cross-checked, so it is not "prepared".
    (dirs["spec"] / "x.json").write_text("{}")
    assert r._prepared_site(dirs) is None

    # A corrupt pair is rebuilt rather than trusted.
    (dirs["trim"] / "trim_map.json").write_text('{"kept_segments": [[1, 10]]}')
    assert r._prepared_site(dirs) is None


# ----------------------------------------------------------------------
# Hotspot grounding
# ----------------------------------------------------------------------

def test_hotspots_must_be_grounded_in_the_actual_structure(config):
    """
    Real failure, caught during the four-target trial: the interface stage was
    asked to analyse 8ZNL and returned PD-L1's canonical literature numbering
    (Tyr56, Gln66, ...) verbatim, but 8ZNL uses a different numbering offset —
    chain B residue 56 there is VAL, not TYR. `validate_spec` caught this
    instance only by luck (the stated atoms didn't exist on VAL); a mismatch
    that happened to share atom names would have silently designed against the
    wrong residues.
    """
    r = PipelineRunner(config, workflow="binder")
    hs = json.dumps({
        "target_chain": "B", "partner_chain": "A",
        "residues": [
            {"residue": "TYR", "auth_seq_id": 56, "label_seq_id": 39,
             "rfd3_atoms": "CD2,CZ"},
            {"residue": "GLN", "auth_seq_id": 66, "label_seq_id": 49,
             "rfd3_atoms": "CD,OE1"},
        ],
    })
    with pytest.raises(PipelineError, match="not grounded"):
        r._verify_hotspot_grounding(hs, "8ZNL")


def test_a_genuinely_correct_hotspot_table_passes(config):
    """No false positives — 5C3T really does have TYR at chain A residue 56."""
    r = PipelineRunner(config, workflow="binder")
    hs = json.dumps({
        "target_chain": "A", "partner_chain": "B",
        "residues": [{"residue": "TYR", "auth_seq_id": 56, "label_seq_id": 39,
                     "rfd3_atoms": "CD2,CZ"}],
    })
    r._verify_hotspot_grounding(hs, "5C3T")   # must not raise


def test_grounding_check_is_silent_when_it_cannot_verify(config, tmp_path):
    """A missing structure or empty table must not block the run — it is a
    correctness check, not a network dependency."""
    r = PipelineRunner(config, workflow="binder")
    r._verify_hotspot_grounding(
        json.dumps({"target_chain": "A", "partner_chain": "B", "residues": []}),
        "5C3T")
    r._verify_hotspot_grounding(
        json.dumps({"target_chain": "A", "partner_chain": "B",
                    "residues": [{"residue": "TYR", "auth_seq_id": 1}]}),
        "0000")   # nonexistent PDB id — logs a warning, does not raise


# ----------------------------------------------------------------------
# Target/partner chain-assignment swap
# ----------------------------------------------------------------------

def test_a_target_partner_chain_swap_is_caught(config):
    """
    Real failure from the four-target trial: for PD-L1 (7CZD, PD-L1 on RCSB
    chains B/D), the interface stage assigned target_chain=A and wrote hotspots
    on the anti-PD-L1 VHH nanobody's own CDR loop instead of PD-L1's IgV
    domain — its own summary even said "Target chain A (VHH)". The hotspots
    were perfectly grounded (real, correctly-numbered VHH residues), so
    `_verify_hotspot_grounding` alone could never catch this: the swap is about
    which MOLECULE the chain letter points at, not whether the residue exists.
    A full multi-hour GPU campaign ran against the wrong protein before this
    guard existed.
    """
    r = PipelineRunner(config, workflow="binder")
    with pytest.raises(PipelineError, match="BACKWARDS"):
        r._verify_target_chain_assignment(
            {"target_gene": "CD274", "target_uniprot": "Q9NZQ7"},
            {"target_chain": "A", "partner_chain": "B"}, "7CZD")


def test_correct_chain_assignments_pass(config):
    """No false positives on the two trials that were actually correct."""
    r = PipelineRunner(config, workflow="binder")
    r._verify_target_chain_assignment(
        {"target_gene": "KRAS", "target_uniprot": "P01116"},
        {"target_chain": "A", "partner_chain": "B"}, "6VJJ")
    r._verify_target_chain_assignment(
        {"target_gene": "VEGFA", "target_uniprot": "P15692"},
        {"target_chain": "W", "partner_chain": "X"}, "1FLT")


def test_chain_assignment_check_is_silent_when_it_cannot_verify(config):
    """Missing metadata or an unresolvable chain must warn, not block the run."""
    r = PipelineRunner(config, workflow="binder")
    r._verify_target_chain_assignment(
        {"target_gene": "NOSUCHGENE"}, {"target_chain": "A", "partner_chain": "B"},
        "0000")   # nonexistent PDB id
    r._verify_target_chain_assignment(
        {}, {"target_chain": "A", "partner_chain": "B"}, "6VJJ")   # no target identity given


def test_a_stale_swapped_spec_is_caught_on_resume_not_just_fresh_generation(
        config, tmp_path):
    """
    validate_spec (inside _prepared_site) only confirms atoms/residues are
    real — it has no opinion on which molecule they belong to. Without
    re-checking on every resume, a stale chain-swapped spec generated before
    this guard existed would be trusted forever.
    """
    r = PipelineRunner(config, workflow="binder")
    dirs = r._binder_dirs(tmp_path)
    dirs["trim"].mkdir(parents=True, exist_ok=True)
    (dirs["trim"] / "trim_map.json").write_text(json.dumps({
        "pdb_id": "7CZD", "target_chain": "A", "partner_chain": "B",
        "kept_segments": [[10, 114]], "contig": "70-86,/0,A10-114",
    }))
    trim = _TrimFromDisk(json.loads(
        (dirs["trim"] / "trim_map.json").read_text()))
    assert trim.target_chain == "A" and trim.pdb_id == "7CZD"
    with pytest.raises(PipelineError, match="BACKWARDS"):
        r._verify_target_chain_assignment(
            {"target_gene": "CD274", "target_uniprot": "Q9NZQ7"},
            {"target_chain": trim.target_chain,
             "partner_chain": trim.partner_chain}, trim.pdb_id)


def test_chain_assignment_check_uses_sequence_identity_not_just_metadata(config):
    """
    Sequence identity against the structure's actual modelled residues is the
    primary signal — it is ground truth (what the atoms in the file actually
    are), independent of RCSB's own curation, and works even when an entity
    has no deposited UniProt cross-reference at all. On the real PD-L1 failure
    it gives a decisive, unambiguous margin: 100% vs 20% identity.
    """
    r = PipelineRunner(config, workflow="binder")
    with pytest.raises(PipelineError, match="by sequence.*100%.*20%|BACKWARDS"):
        r._verify_target_chain_assignment(
            {"target_gene": "CD274", "target_uniprot": "Q9NZQ7"},
            {"target_chain": "A", "partner_chain": "B"}, "7CZD")


def test_neither_chain_matching_by_sequence_is_also_a_hard_stop(config,
                                                                monkeypatch):
    """
    If sequence data is available and NEITHER chain looks like the intended
    target, that is stronger evidence of a problem than the metadata-only
    fallback ever had — the interface stage may have picked the wrong PDB
    entry entirely, not just swapped two chains within the right one.
    """
    import src.pipeline_runner as pr

    monkeypatch.setattr(
        pr.PipelineRunner, "_binder_structure_path",
        lambda self, pdb_id: __import__("pathlib").Path("data/structures/3KYS_ba1.cif"))
    monkeypatch.setattr(
        pr.PipelineRunner, "_chain_identity_to_uniprot",
        lambda self, path, chain, ref: 0.05)   # nothing matches, either chain

    r = PipelineRunner(config, workflow="binder")
    with pytest.raises(PipelineError, match="neither chain looks like"):
        r._verify_target_chain_assignment(
            {"target_gene": "CD274", "target_uniprot": "Q9NZQ7"},
            {"target_chain": "A", "partner_chain": "B"}, "3KYS")


def test_falls_back_to_metadata_when_the_structure_is_not_yet_downloaded(
        config, tmp_path):
    """
    The sequence path needs the structure file on disk; when it is not there
    (a fresh run, before _ensure_structure has fetched it, or a network
    failure), the metadata check must still catch an unambiguous swap rather
    than silently skipping verification.
    """
    r = PipelineRunner(config, workflow="binder")
    original = r.config.get("paths", {}).get("structures_dir")
    r.config = {**r.config, "paths": {"structures_dir": str(tmp_path / "nowhere")}}
    with pytest.raises(PipelineError, match="BACKWARDS"):
        r._verify_target_chain_assignment(
            {"target_gene": "CD274", "target_uniprot": "Q9NZQ7"},
            {"target_chain": "A", "partner_chain": "B"}, "7CZD")


def test_sequence_identity_separates_same_protein_from_unrelated(config):
    """Sanity check on the underlying primitive: a real match scores high,
    an unrelated sequence scores low, both against a real UniProt fetch."""
    from src.structure_tools import sequence_identity
    from src.target_resolve import fetch_uniprot_sequence

    ref = fetch_uniprot_sequence("Q9NZQ7")   # PD-L1
    assert ref and len(ref) > 100
    assert sequence_identity(ref[19:130], ref) > 0.95     # a real fragment
    assert sequence_identity("MKV" * 40, ref) < 0.4        # nonsense sequence


# ----------------------------------------------------------------------
# Production sizing: local-vs-cluster batch counts, and surviving a resume
# ----------------------------------------------------------------------

def _fake_calib(verdict: str, required_backbones: float | None):
    from types import SimpleNamespace
    return SimpleNamespace(
        verdict=verdict,
        pessimistic=SimpleNamespace(required_backbones=required_backbones),
    )


def test_batches_for_returns_none_when_not_scaling_up(config):
    res = _fake_calib("ITERATE", 1000.0)
    assert PipelineRunner._batches_for(res, config) is None


def test_batches_for_local_is_a_campaign_total(config):
    res = _fake_calib("SCALE_UP", 4000.0)
    dbs = config["design"]["foundry"]["rfd3"]["diffusion_batch_size"] \
        if "rfd3" in config["design"]["foundry"] else 4
    n = PipelineRunner._batches_for(res, config, n_gpus=1)
    assert n == max(1, int(4000.0 / max(dbs, 1)))


def test_batches_for_cluster_divides_by_gpu_count(config):
    """NB is per-GPU-array-task on the cluster pipeline, not a campaign
    total — dividing here (not multiplying) is what CLAUDE.md's cluster
    section documents as the fix for a real undercount bug."""
    res = _fake_calib("SCALE_UP", 4000.0)
    local = PipelineRunner._batches_for(res, config, n_gpus=1)
    cluster = PipelineRunner._batches_for(res, config, n_gpus=8)
    assert cluster == max(1, int(local / 8))


def test_resolve_production_plan_prefers_in_memory_result(config, tmp_path):
    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder")
    dirs = {"calibration": tmp_path}
    calib = {"compute": "cluster", "n_batches_cluster": 42, "n_batches_local": 99}
    n_batches, compute = r._resolve_production_plan(calib, dirs, None)
    assert (n_batches, compute) == (42, "cluster")


def test_resolve_production_plan_survives_a_fresh_process_resume(config, tmp_path):
    """`calib` is None (a genuinely fresh process, per `--start-from
    production`) — the plan must come back from calibration.json on disk,
    not silently fall back to config.yaml's raw default."""
    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder")
    calib_dir = tmp_path / "calibration"
    calib_dir.mkdir()
    (calib_dir / "calibration.json").write_text(json.dumps({
        "verdict": "SCALE_UP",
        "n_batches_local": 55,
        "n_batches_cluster": 7,
        "compute_choice": {"compute": "cluster"},
    }), encoding="utf-8")
    dirs = {"calibration": calib_dir}
    n_batches, compute = r._resolve_production_plan(None, dirs, None)
    assert (n_batches, compute) == (7, "cluster")


def test_resolve_production_plan_ignores_a_non_scale_up_verdict_on_disk(config, tmp_path):
    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder")
    calib_dir = tmp_path / "calibration"
    calib_dir.mkdir()
    (calib_dir / "calibration.json").write_text(json.dumps({
        "verdict": "ITERATE", "n_batches_local": 55,
    }), encoding="utf-8")
    dirs = {"calibration": calib_dir}
    n_batches, compute = r._resolve_production_plan(None, dirs, 12)
    assert n_batches == 12
    assert compute == "local"


def test_resolve_production_plan_falls_back_when_no_calibration_json(config, tmp_path):
    r = PipelineRunner(config, output_dir=tmp_path, workflow="binder", compute="local")
    dirs = {"calibration": tmp_path / "nowhere"}
    n_batches, compute = r._resolve_production_plan(None, dirs, 9)
    assert (n_batches, compute) == (9, "local")


def test_compute_defaults_to_auto_and_accepts_local_cluster(config):
    assert PipelineRunner(config, workflow="binder")._compute == "auto"
    assert PipelineRunner(config, workflow="binder", compute="local")._compute == "local"
    assert PipelineRunner(config, workflow="binder", compute="cluster")._compute == "cluster"
    with pytest.raises(ValueError):
        PipelineRunner(config, workflow="binder", compute="nonsense")


def test_max_local_hours_is_stored_and_defaults_to_none(config):
    assert PipelineRunner(config, workflow="binder")._max_local_hours is None
    r = PipelineRunner(config, workflow="binder", max_local_hours=12.0)
    assert r._max_local_hours == 12.0
