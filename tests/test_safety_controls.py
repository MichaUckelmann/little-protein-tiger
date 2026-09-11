"""The release's safety controls, and the exact boundaries they draw.

Four things are pinned here, each of which is a decision rather than an
implementation detail — which is why they get tests rather than comments:

1. **The refusal chain stops at two frontier models.** It used to end in a
   smaller model that did answer an interface stage both Sonnet and Opus had
   declined. Falling back across providers once probes an inconsistently
   calibrated classifier; continuing until something answers is shopping for
   a permissive verdict, and no reader of the output could tell them apart.
2. **A refusal reaches the deliverable**, not just a log line. The report is
   what circulates.
3. **The select-agent screen is advisory and name-based**, and its two
   hardest cases behave correctly: SARS-CoV-2 is not a select agent while
   SARS-CoV is, and *Ricinus communis* is not one while ricin is.
4. **Every report says its designs are unvalidated**, in static markup that
   survives a JavaScript failure.

Nothing here needs network, GPU, API keys or the corpus.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "src" / "report_templates"


# ── 1. the refusal chain stops ───────────────────────────────────────────────

def test_two_models_is_the_cap_and_it_is_enforced_in_code():
    """A config listing five fallbacks must not be able to walk past two."""
    from src.pipeline_runner import MAX_REFUSALS_BEFORE_STOP

    assert MAX_REFUSALS_BEFORE_STOP == 2


@pytest.mark.parametrize("provider", ["claude", "gemini"])
def test_no_configured_chain_falls_back_to_a_smaller_model(provider):
    """claude-haiku-4-5 was the last rung of both chains and is gone.

    It is the one model that answered the PD-L1 interface stage after two
    frontier models declined it, which is exactly why it cannot be in a
    refusal chain: reaching it means the run obtained content that two better
    models refused to produce.
    """
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    chain = (cfg["models"][provider] or {}).get("refusal_fallbacks") or []
    assert chain, f"models.{provider}.refusal_fallbacks is empty"
    assert not any("haiku" in entry.lower() for entry in chain), (
        f"models.{provider}.refusal_fallbacks falls back to a smaller model: "
        f"{chain}")


def test_the_code_default_chain_also_stops_at_one_fallback():
    """`_REFUSAL_FALLBACK_MODELS` applies when config names no chain."""
    from src.pipeline_runner import _REFUSAL_FALLBACK_MODELS

    assert not any("haiku" in m.lower() for m in _REFUSAL_FALLBACK_MODELS)


def test_a_third_model_is_never_asked(tmp_path, monkeypatch):
    """The real behaviour, not a config assertion.

    `--provider claude` configures TWO fallbacks, so without the cap this
    would instantiate three runners. It must stop after two and raise.
    """
    import src.pipeline_runner as pr
    from src.skill_runner import SkillRefusedError

    asked: list[str] = []

    class _ZeroUsage:
        input_tokens = output_tokens = 0
        cache_creation_tokens = cache_read_tokens = 0

    class AlwaysRefuses:
        def __init__(self, *, skill_name, provider, model_id, **kw):
            self.skill_name, self.model_id = skill_name, model_id
            asked.append(f"{provider}:{model_id}")

        def run(self, query, context_text=None):
            raise SkillRefusedError(skill=self.skill_name, model=self.model_id,
                                    category="bio", iteration=7)

        def usage(self):
            return _ZeroUsage()

    monkeypatch.setattr(pr, "SkillRunner", AlwaysRefuses)
    runner = pr.PipelineRunner(config=yaml.safe_load(
        (_ROOT / "config.yaml").read_text(encoding="utf-8")),
        provider="claude", output_dir=tmp_path)
    # Two fallbacks configured; the cap must still stop at two models total.
    assert len(runner._models_cfg.get("refusal_fallbacks") or []) == 2

    with pytest.raises(SkillRefusedError):
        runner._run_stage("complex-structure-analysis", "q", [],
                          tmp_path / "out.md", stage="interface")

    assert len(asked) == 2, (
        f"{len(asked)} models were asked, cap is 2: {asked}")
    assert not (tmp_path / "out.md").exists(), (
        "a refused stage must not write a stage report")


# ── 2. a refusal reaches the deliverable ─────────────────────────────────────

def test_the_clean_path_also_names_its_model():
    """"Written by the first model asked" is what makes "and this one was
    not" mean anything. A block that only appears after a refusal is a block
    a reader has no baseline for."""
    import src.pipeline_runner as pr

    note = pr.PipelineRunner._stage_provenance_note(
        pr.PipelineRunner.__new__(pr.PipelineRunner),
        "complex-structure-analysis", "gemini", "gemini-3.7-flash", [])
    assert "## MODEL PROVENANCE" in note
    assert "gemini:gemini-3.7-flash" in note
    assert "the first model asked" in note


def test_a_refusal_is_named_in_the_stage_report_and_round_trips():
    import src.pipeline_runner as pr
    from src.report_common import parse_provenance
    from src.skill_runner import SkillRefusedError

    declined = [SkillRefusedError(skill="s", model="claude-sonnet-5",
                                  category="bio", iteration=7)]
    note = pr.PipelineRunner._stage_provenance_note(
        pr.PipelineRunner.__new__(pr.PipelineRunner),
        "complex-structure-analysis", "gemini", "gemini-3.7-flash", declined)

    assert "claude-sonnet-5" in note and "bio" in note
    parsed = parse_provenance(note)
    assert parsed["written_by"] == "gemini:gemini-3.7-flash"
    assert parsed["declined"] == [{"model": "claude-sonnet-5",
                                  "category": "bio"}]
    assert parsed["asked"] == 2


def test_provenance_goes_in_before_the_citation_block():
    """`extract_citation_section` matches to END OF FILE, so a section
    appended after it is rendered inside the citation block in both HTML
    reports. Ordering is load-bearing, so it is pinned."""
    from src.report_common import extract_citation_section, parse_provenance

    text = ("# Stage\n\nbody\n\n## MODEL PROVENANCE\n\n"
            "- skill: `x`\n- written by: **gemini:gemini-3.7-flash** — the "
            "first model asked\n\n## CITATION VERIFICATION\n\n- checked: 3\n")
    assert parse_provenance(text) is not None
    citations = extract_citation_section(text)
    assert citations and "MODEL PROVENANCE" not in citations


def test_provenance_record_collects_a_real_campaign(tmp_path):
    """A synthetic run directory in the binder layout."""
    from src import run_provenance

    (tmp_path / "20_target_intel.md").write_text(
        "# Intel\n\n### PIPELINE HANDOFF\n- target_gene: CD274\n"
        "- pdb_id: 8ZNL\n- target_chain: B\n\n## MODEL PROVENANCE\n\n"
        "- skill: `binder-target-intel`\n"
        "- written by: **gemini:gemini-3.7-flash** — the first model asked\n",
        encoding="utf-8")
    (tmp_path / "21_interface.md").write_text(
        "# Interface\n\n## MODEL PROVENANCE\n\n- skill: `x`\n"
        "- written by: **claude-opus-5** — model 2 of 2 asked\n"
        "- declined first (1):\n"
        "    - `gemini-3.7-flash` — safety classifier, category `bio` "
        "(call #4)\n", encoding="utf-8")

    rec = run_provenance.collect(tmp_path)
    assert rec["target"]["target_gene"] == "CD274"
    assert rec["target"]["pdb_id"] == "8ZNL"
    assert rec["models"]["any_refusal"] is True
    assert rec["models"]["declined"][0]["model"] == "gemini-3.7-flash"
    assert rec["select_agent_screen"]["flagged"] is False
    assert "unvalidated computational hypothesis" in rec["disclaimer"]

    out = run_provenance.write(tmp_path)
    assert out and json.loads(out.read_text(encoding="utf-8"))["schema_version"]

    footer = run_provenance.footer_html(rec)
    assert "claude-opus-5" in footer and "gemini-3.7-flash" in footer


def test_a_campaign_with_no_provenance_blocks_still_collects(tmp_path):
    """Campaigns that predate the block must not crash the collector — they
    get an explicit "not recorded", not a silent gap."""
    from src import run_provenance

    (tmp_path / "22_trim.md").write_text("# Trim\n\nno provenance here\n",
                                         encoding="utf-8")
    rec = run_provenance.collect(tmp_path)
    assert rec["stages"][0]["written_by"] is None
    assert "no model provenance recorded" in rec["stages"][0]["note"]
    assert run_provenance.footer_html(rec) == ""


# ── 3. the select-agent screen ───────────────────────────────────────────────

def test_the_list_is_transcribed_completely_and_without_duplicates():
    from src.select_agents import AGENTS

    names = [a.name for a in AGENTS]
    assert len(names) == len(set(names)), "duplicate entries"
    # 36 HHS + 8 overlap + 13 USDA VS + 6 USDA PPQ on the 2025-01-14 list,
    # with the two tick-borne subtypes and the two variola species folded
    # into one entry each and Junin/Machupo/etc. listed separately.
    assert len(AGENTS) >= 55, f"only {len(AGENTS)} entries — list truncated?"
    assert all(a.patterns for a in AGENTS), "an entry with no pattern is dead"


@pytest.mark.parametrize("text", [
    "Crystal structure of ricin A chain",
    "Bacillus anthracis protective antigen",
    "Botulinum neurotoxin serotype A light chain",
    "Burkholderia pseudomallei surface protein",
    "highly pathogenic avian influenza H5N1 hemagglutinin",
    "design a binder against Nipah virus G glycoprotein",
    "Yersinia pestis F1 antigen",
    "SARS-CoV/SARS-CoV-2 chimeric virus spike",
])
def test_named_select_agents_are_flagged(text):
    from src.select_agents import screen

    assert screen([text]), f"not flagged: {text!r}"


@pytest.mark.parametrize("text", [
    # The two cases that would make this check useless if they were wrong.
    "SARS-CoV-2 spike glycoprotein receptor binding domain",
    "design binders against SARS-CoV-2 to block ACE2",
    "Ricinus communis agglutinin structure",
    # Routine work that must never fire.
    "Design binders against PD-L1 to block its interaction with PD-1",
    "design novel inhibitors for pain receptors — CALCRL / RAMP1",
    "design inhibitors for YAP/TEAD in mesothelioma",
    "influenza hemagglutinin H3N2 seasonal vaccine strain",
    "KRAS / RAF1 interface",
])
def test_routine_targets_are_not_flagged(text):
    from src.select_agents import screen

    hits = screen([text])
    assert not hits, f"false positive on {text!r}: {hits}"


def test_sars_cov_2_is_excluded_but_sars_cov_is_not():
    """The exclusion is the whole reason this check is usable: COVID
    structures are everywhere, SARS-CoV-2 is not a select agent, and a screen
    that fired on all of them would be switched off within a day."""
    from src.select_agents import screen

    assert screen(["Crystal structure of SARS coronavirus main protease"])
    assert not screen(["SARS-CoV-2 nucleocapsid"])


def test_the_screen_is_advisory_and_says_so():
    from src.select_agents import describe, screen

    text = describe(screen(["Crystal structure of ricin A chain"]))
    assert "does NOT stop the run" in text
    assert "institutional approval" in text
    assert "selectagents.gov" in text
    # It must not overstate itself: a name screen is not a clearance.
    assert "Name screening only" in text


def test_the_screen_is_actually_called_from_both_tracks():
    """A control nothing calls is not a control.

    The same reflection trick `test_audit_fixes` uses for the chain-assignment
    and hotspot-grounding guards, for the same reason: a refactor can delete a
    call site without touching the function, and the tests that assert the
    function *works* all still pass. Both tracks must screen before they
    reach a GPU stage.
    """
    import inspect

    import src.pipeline_runner as pr

    ppi = inspect.getsource(pr.PipelineRunner.run)
    binder = inspect.getsource(pr.PipelineRunner._run_binder_track)
    for name, src in (("ppi run()", ppi), ("_run_binder_track", binder)):
        assert "_screen_select_agents" in src, (
            f"{name} no longer screens for select agents")


def test_the_screen_never_blocks_a_run():
    """Pinned as a contract, not an accident: the checkpoint payload says
    `blocking: False` and the function returns hits rather than raising."""
    import inspect

    import src.pipeline_runner as pr

    src = inspect.getsource(pr.PipelineRunner._screen_select_agents)
    assert "never blocks" in src
    assert '"blocking": False' in src
    assert "raise" not in src.replace("must never raise", "")


# ── 4. every report says its designs are unvalidated ─────────────────────────

@pytest.mark.parametrize("track", ["binder_report", "ppi_report"])
def test_both_reports_carry_the_unvalidated_notice(track):
    import re

    shell = (_TEMPLATES / track / "shell.html").read_text(encoding="utf-8")
    assert "safetynotice" in shell, f"{track} has no safety notice"
    # Whitespace-normalised: the markup is hard-wrapped, so these sentences
    # span newlines in the file.
    flat = re.sub(r"\s+", " ", shell)
    assert "unvalidated computational hypothesis" in flat
    assert "screening the sequence is your responsibility" in flat
    assert "responsible-use.md" in flat


@pytest.mark.parametrize("track", ["binder_report", "ppi_report"])
def test_the_notice_is_static_markup_not_rendered_by_javascript(track):
    """It is the one element on the page that must survive a JS exception, so
    it lives in the shell rather than being written by app.js."""
    app = (_TEMPLATES / track / "app.js").read_text(encoding="utf-8")
    assert "safetynotice" not in app, (
        f"{track}/app.js renders the safety notice — a JS error would then "
        f"remove it from the page")


def test_the_notice_is_styled_in_both_themes():
    """A colour defined only inside a dark-mode block leaves the notice
    unreadable in the other theme — the classic broken-report bug."""
    css = (_TEMPLATES / "_shared" / "base.css").read_text(encoding="utf-8")
    assert ".safetynotice{" in css
    # Built from palette tokens, which are redefined per theme, rather than
    # from literals — a hex value here would be correct in one theme only.
    block = css.split(".safetynotice{", 1)[1].split("}", 1)[0]
    assert "var(--warn" in block, f"no themed colour in .safetynotice: {block}"
    assert "#" not in block, f"hard-coded colour in .safetynotice: {block}"
