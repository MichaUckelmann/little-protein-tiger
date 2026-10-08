"""`--hypothesis-policy`: the two-track skill variant, and what the pipeline does with its candidates.

No network and no LLM: the tool conversation, the RCSB entries and the gene resolver are injected.
The point of most of these tests is what must NOT happen: nothing changes with the flag off, the
operator's `--pdb` always wins, an unparseable report is left alone, and a run that is resumed does not
revert to the skill's own pick.
"""
import inspect
import json
from pathlib import Path

import pytest
import yaml

from src import hypothesis_gates as G
from src import skill_variants
from src.pipeline_runner import PipelineError, PipelineResult, PipelineRunner

_ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))

RESPONSES = [
    {"role": "user", "parts": [{"functionResponse": {"name": "get_genetic_codependency",
                                                      "response": {"result": '{"r": 0.3024, "n": 1208}'}}}]},
    {"role": "user", "parts": [{"functionResponse": {"name": "get_fingerprint",
                                                      "response": {"result": '{"doi": "10.1126/science.abj4008"}'}}}]},
]
ENTRIES = {
    "6GK2": {"exists": True, "genes": ["MALT1", "BCL10"], "descriptions": ["MALT1", "BCL10"]},
    "1P9M": {"exists": True, "genes": ["IL6", "IL6R"], "descriptions": ["Interleukin-6"]},
}
CANON = {"tier": "VALIDATED", "complex": "IL6 / IL6R", "track": "canonical", "pdb_ids": ["1P9M"],
         "design_intent": "disrupt", "evidence_chain": []}
NOVEL = {"tier": "HYPOTHESIS", "complex": "MALT1 / BCL10", "track": "corpus_derived", "pdb_ids": ["6GK2"],
         "design_intent": "disrupt", "novelty_score": 0.65,
         "evidence_chain": [{"kind": "depmap_r", "value": "0.3024", "tool": "get_genetic_codependency"},
                            {"kind": "doi", "value": "10.1126/science.abj4008", "tool": "get_fingerprint"}]}
UNGROUNDED = dict(NOVEL, evidence_chain=[{"kind": "depmap_r", "value": "0.9999", "tool": "get_genetic_codependency"}])


def report(choices, preference):
    return ("## PATHWAY BIOLOGY REPORT\n\nbody\n\n### PIPELINE HANDOFF\n"
            f"- pdb_id: 1P9M\n- target_complex: {preference}\n- design_intent: disrupt\n"
            "- structure_query: Analyze PDB 1P9M. Target complex: IL6 / IL6R.\n"
            f"- choices_json: {json.dumps(choices)}\n"
            "\n## MODEL PROVENANCE\n\nmodel x\n\n## CITATION VERIFICATION\n\nnone\n")


def make_runner(policy="novel_if_eligible", monkeypatch=None):
    r = PipelineRunner(config=CONFIG, provider="gemini", workflow="ppi", pathway_mode="wildcard",
                       hypothesis_policy=policy)
    r._last_stage = {"skill": "wildcard-expert", "provider": "gemini", "messages": RESPONSES}
    r.checkpoints = []
    r._binder_checkpoint = lambda *a, **k: r.checkpoints.append(a)
    if monkeypatch:
        monkeypatch.setattr(G, "fetch_rcsb_entries", lambda ids, cache=None, timeout=40: ENTRIES)
        monkeypatch.setattr(G, "default_resolver", lambda: (lambda n: n.strip().upper()))
    return r


def handoff_for(text, runner):
    return runner._parse_handoff(text)


# ------------------------------------------------------------------ the variant
def test_the_two_track_variant_adds_the_evidence_chain_and_demotes_the_skills_pick():
    live = (_ROOT / "skills" / "wildcard-expert" / "SKILL.md").read_text(encoding="utf-8")
    out = skill_variants.two_track_wildcard(live)
    assert "evidence_chain" in out and "Two tracks (this revision)" in out
    assert "advisory" in out.lower() and "Novelty-first rule" not in out
    assert "### PRIMARY RECOMMENDATION" in out and "choices_json" in out      # the parser still finds both


def test_the_variant_fails_loudly_when_the_skill_it_edits_has_changed():
    live = (_ROOT / "skills" / "wildcard-expert" / "SKILL.md").read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        skill_variants.two_track_wildcard(live.replace("### PRIMARY RECOMMENDATION", "### PICK"))


# ------------------------------------------------------------------ the flag's refusals
@pytest.mark.parametrize("kwargs,needle", [
    (dict(pathway_mode="standard"), "wildcard"),
    (dict(provider="claude"), "gemini"),
    (dict(workflow="binder"), "ppi"),
])
def test_the_policy_is_refused_where_it_cannot_work(kwargs, needle):
    base = dict(config=CONFIG, provider="gemini", workflow="ppi", pathway_mode="wildcard",
                hypothesis_policy="novel_if_eligible")
    base.update(kwargs)
    with pytest.raises(ValueError) as e:
        PipelineRunner(**base)
    assert needle in str(e.value)


def test_an_unknown_policy_is_refused_and_off_needs_nothing():
    with pytest.raises(ValueError):
        PipelineRunner(config=CONFIG, provider="gemini", workflow="ppi", pathway_mode="wildcard", hypothesis_policy="bold")
    PipelineRunner(config=CONFIG, provider="claude", workflow="ppi", pathway_mode="standard")   # default: no constraints


def test_with_the_flag_off_the_stage_is_what_it_was(tmp_path, monkeypatch):
    r = PipelineRunner(config=CONFIG, provider="gemini", workflow="ppi", pathway_mode="wildcard")
    seen = {}

    def fake_run_stage(skill, query, ctx, out, *, stage=None, prompt_variant=None):
        seen["skill"], seen["variant"] = skill, prompt_variant
        return {"pdb_id": "1ABC", "target_complex": "A / B"}

    monkeypatch.setattr(r, "_run_stage", fake_run_stage)
    monkeypatch.setattr(r, "_apply_hypothesis_policy", lambda *a, **k: pytest.fail("policy ran with the flag off"))
    res = PipelineResult(run_dir=tmp_path)
    r._stage_pathway("q", tmp_path, res)
    assert seen == {"skill": "wildcard-expert", "variant": None} and res.hypothesis_decision is None


# ------------------------------------------------------------------ applying a decision
def test_an_eligible_corpus_derived_hypothesis_is_forwarded_and_recorded(tmp_path, monkeypatch):
    r = make_runner(monkeypatch=monkeypatch)
    f = tmp_path / "00_pathway.md"
    f.write_text(report([CANON, NOVEL], "IL6 / IL6R"), encoding="utf-8")
    handoff = handoff_for(f.read_text(), r)
    res = PipelineResult(run_dir=tmp_path)
    r._apply_hypothesis_policy(handoff, f, res, pinned=False)

    assert handoff["target_complex"] == "MALT1 / BCL10" and handoff["pdb_id"] == "6GK2"
    assert handoff["structure_query"] == ""        # blank => the stage builds it for THIS candidate from choices_json
    assert res.hypothesis_decision["changed"] and res.hypothesis_decision["track"] == "corpus_derived"
    assert r.checkpoints and r.checkpoints[0][0] == "hypothesis_forwarded"
    assert r.checkpoints[0][3]["to_complex"] == "MALT1 / BCL10" and r.checkpoints[0][3]["from_complex"] == "IL6 / IL6R"
    text = f.read_text()
    # the note goes in BEFORE the sections that are matched through to end-of-file
    assert 0 < text.index("## HYPOTHESIS GATES") < text.index("## MODEL PROVENANCE") < text.index("## CITATION VERIFICATION")
    assert "CHANGED from the skill's preference" in text


def test_the_operators_pdb_pin_is_never_overridden(tmp_path, monkeypatch):
    r = make_runner(monkeypatch=monkeypatch)
    f = tmp_path / "00_pathway.md"
    f.write_text(report([CANON, NOVEL], "IL6 / IL6R"), encoding="utf-8")
    before = f.read_text()
    handoff = handoff_for(before, r)
    res = PipelineResult(run_dir=tmp_path)
    r._apply_hypothesis_policy(handoff, f, res, pinned=True)
    assert handoff["target_complex"] == "IL6 / IL6R" and f.read_text() == before
    assert res.hypothesis_decision is None and r.checkpoints == []


def test_when_nothing_passes_the_skills_own_pick_goes_forward_and_says_it_was_ungated(tmp_path, monkeypatch):
    r = make_runner(monkeypatch=monkeypatch)
    f = tmp_path / "00_pathway.md"
    f.write_text(report([UNGROUNDED], "MALT1 / BCL10"), encoding="utf-8")
    handoff = handoff_for(f.read_text(), r)
    res = PipelineResult(run_dir=tmp_path)
    r._apply_hypothesis_policy(handoff, f, res, pinned=False)
    assert handoff["target_complex"] == "MALT1 / BCL10" and r.checkpoints == []
    assert res.hypothesis_decision["gated"] is False and "UNGATED" in f.read_text()


def test_an_unparseable_report_changes_nothing_and_says_so(tmp_path, monkeypatch):
    r = make_runner(monkeypatch=monkeypatch)
    f = tmp_path / "00_pathway.md"
    f.write_text("a free-form review with no handoff at all\n", encoding="utf-8")
    handoff = {"target_complex": "X / Y", "pdb_id": "1ABC"}
    res = PipelineResult(run_dir=tmp_path)
    r._apply_hypothesis_policy(handoff, f, res, pinned=False)
    assert handoff == {"target_complex": "X / Y", "pdb_id": "1ABC"} and r.checkpoints == []
    assert "no parseable choices_json" in res.hypothesis_decision["reason"]


def test_a_skill_pick_that_passes_is_left_alone(tmp_path, monkeypatch):
    r = make_runner(monkeypatch=monkeypatch)
    f = tmp_path / "00_pathway.md"
    f.write_text(report([CANON, NOVEL], "MALT1 / BCL10"), encoding="utf-8")
    handoff = handoff_for(f.read_text(), r)
    r._apply_hypothesis_policy(handoff, f, PipelineResult(run_dir=tmp_path), pinned=False)
    assert handoff["target_complex"] == "MALT1 / BCL10" and r.checkpoints == []


# ------------------------------------------------------------------ resume
class FakeProject:
    def __init__(self, payload):
        self.payload = payload

    def checkpoint(self, cp_id, round_id):
        return {"payload": self.payload} if cp_id == "hypothesis_forwarded" else None


PAYLOAD = {"policy": "novel_if_eligible", "from_complex": "IL6 / IL6R", "to_complex": "MALT1 / BCL10",
           "to_pdb": "6GK2", "design_intent": "disrupt", "reason": "r"}


def test_a_resumed_run_restores_the_forwarded_hypothesis_not_the_skills_pick():
    r = make_runner()
    r._project, r._round_id = FakeProject(PAYLOAD), "round-1"
    handoff = {"target_complex": "IL6 / IL6R", "pdb_id": "1P9M", "structure_query": "old"}
    res = PipelineResult(run_dir=Path("."))
    res.target_complex, res.pdb_id = "IL6 / IL6R", "1P9M"
    r._reapply_recorded_hypothesis_forward(res, handoff)
    assert (res.target_complex, res.pdb_id) == ("MALT1 / BCL10", "6GK2")
    assert handoff["target_complex"] == "MALT1 / BCL10" and handoff["structure_query"] == ""
    assert res.hypothesis_decision["restored_on_resume"] is True


def test_a_resume_that_already_names_the_forwarded_target_is_left_alone():
    r = make_runner()
    r._project, r._round_id = FakeProject(PAYLOAD), "round-1"
    handoff = {"target_complex": "MALT1 / BCL10", "pdb_id": "6GK2", "structure_query": "keep"}
    res = PipelineResult(run_dir=Path("."))
    res.target_complex, res.pdb_id = "MALT1 / BCL10", "6GK2"
    r._reapply_recorded_hypothesis_forward(res, handoff)
    assert handoff["structure_query"] == "keep" and res.hypothesis_decision is None


def test_without_a_project_a_resume_changes_nothing():
    r = make_runner()
    r._project, r._round_id = None, None
    handoff = {"target_complex": "IL6 / IL6R"}
    r._reapply_recorded_hypothesis_forward(PipelineResult(run_dir=Path(".")), handoff)
    assert handoff == {"target_complex": "IL6 / IL6R"}


def test_the_forwarded_target_is_restored_before_the_structure_switch_on_resume():
    """The structure switch was decided on the forwarded target, so its recorded 'from' is the one to restore."""
    src = inspect.getsource(PipelineRunner.run)
    assert src.index("_reapply_recorded_hypothesis_forward") < src.index("_reapply_recorded_structure_switch")


def test_the_note_does_not_leak_into_the_handoff_and_the_file_still_names_the_skills_pick(tmp_path, monkeypatch):
    """Resume re-parses the report. The note's bullets must not become handoff fields, and the block on disk
    keeps naming the skill's pick: that is exactly why the decision is also kept in a checkpoint."""
    r = make_runner(monkeypatch=monkeypatch)
    f = tmp_path / "00_pathway.md"
    f.write_text(report([CANON, NOVEL], "IL6 / IL6R"), encoding="utf-8")
    handoff = handoff_for(f.read_text(), r)
    keys = sorted(handoff)
    r._apply_hypothesis_policy(handoff, f, PipelineResult(run_dir=tmp_path), pinned=False)
    again = handoff_for(f.read_text(), r)
    assert sorted(again) == keys
    assert again["target_complex"] == "IL6 / IL6R"
