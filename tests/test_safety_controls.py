"""The release's safety controls, and the exact boundaries they draw.

Four things are pinned here, each of which is a decision rather than an
implementation detail — which is why they get tests rather than comments:

1. **A refusal is terminal, and no other model is asked.** There used to be
   a fallback chain; it was shortened once (it ended in a smaller model that
   answered an interface stage both Sonnet and Opus had declined) and then
   removed, because the argument against the last rung is the argument
   against all of them: any automatic retry is the pipeline going looking for
   a model that will produce what the operator's chosen model declined to,
   and no reader of the output can tell that apart from a legitimate
   workaround for a miscalibrated classifier.
2. **A refusal reaches the record**, not just a log line — the manifest for a
   run that stopped, the stage report for every stage that was written. The
   report is what circulates.
3. **The select-agent screen is advisory and name-based**, and its two
   hardest cases behave correctly: SARS-CoV-2 is not a select agent while
   SARS-CoV is, and *Ricinus communis* is not one while ricin is.
4. **Every report says its designs are unvalidated**, in static markup that
   survives a JavaScript failure.

Nothing here needs network, GPU, API keys or the corpus.
"""

from __future__ import annotations

import contextlib
import json
import pathlib

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "src" / "report_templates"


@contextlib.contextmanager
def captured_logs(level: str = "WARNING"):
    """Collect loguru messages emitted inside the block.

    Not `caplog`: this project logs through loguru, which does not propagate to
    the stdlib `logging` handlers pytest installs, so `caplog.records` is empty
    no matter what was logged. Both assertions below are about text an operator
    has to actually see, so capturing the real sink is the point.
    """
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]),
                         level=level)
    try:
        yield messages
    finally:
        logger.remove(sink_id)


# ── 1. a refusal is terminal ─────────────────────────────────────────────────

def test_the_fallback_machinery_is_gone_not_merely_unused():
    """A leftover constant is an invitation to re-wire it.

    Asserted by name because these three were the whole mechanism, and a
    later change that reintroduces any of them should have to delete this
    test and explain why in the diff.
    """
    import src.pipeline_runner as pr

    for name in ("_REFUSAL_FALLBACK_MODELS", "MAX_REFUSALS_BEFORE_STOP",
                 "_split_fallback", "_record_refusals"):
        assert not hasattr(pr, name) and not hasattr(pr.PipelineRunner, name), (
            f"{name} is back — automatic refusal fallback was removed "
            f"deliberately, see docs/responsible-use.md")


@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
def test_no_shipped_config_names_a_fallback_chain(provider):
    """The key is not read any more; leaving it set in the shipped config
    would tell an operator the opposite of what happens."""
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    block = cfg["models"].get(provider) or {}
    assert "refusal_fallbacks" not in block, (
        f"models.{provider}.refusal_fallbacks is still in config.yaml")


def test_a_stale_config_key_warns_rather_than_being_silently_ignored():
    """The worst outcome is an operator believing a retry will happen.

    A config carried over from before this change still lists the key, and
    nothing reads it — so it has to say so out loud, once, per run.
    """
    import src.pipeline_runner as pr

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["models"]["gemini"]["refusal_fallbacks"] = ["claude-opus-5"]
    with captured_logs("WARNING") as messages:
        pr.PipelineRunner(config=cfg, provider="gemini")
    assert any("refusal_fallbacks" in m and "NO LONGER" in m
               for m in messages), (
        f"no warning for a stale refusal_fallbacks key: {messages}")


def _always_refuses(asked: list[str]):
    """A SkillRunner stand-in that declines and records who was asked."""
    from src.skill_runner import SkillRefusedError

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

    return AlwaysRefuses


@pytest.mark.parametrize("provider", ["claude", "gemini"])
def test_exactly_one_model_is_asked_and_the_stage_writes_nothing(
        provider, tmp_path, monkeypatch):
    """The real behaviour, not a config assertion.

    Both providers, because the two used to configure different chains and a
    surviving chain on either would show up as a second model being asked.
    """
    import src.pipeline_runner as pr
    from src.skill_runner import SkillRefusedError

    asked: list[str] = []
    monkeypatch.setattr(pr, "SkillRunner", _always_refuses(asked))
    runner = pr.PipelineRunner(config=yaml.safe_load(
        (_ROOT / "config.yaml").read_text(encoding="utf-8")),
        provider=provider, output_dir=tmp_path)

    with pytest.raises(SkillRefusedError):
        runner._run_stage("complex-structure-analysis", "q", [],
                          tmp_path / "out.md", stage="interface")

    assert len(asked) == 1, f"{len(asked)} models asked, must be 1: {asked}"
    assert not (tmp_path / "out.md").exists(), (
        "a refused stage must not write a stage report")


def test_the_refusal_survives_the_process_in_the_manifest(tmp_path,
                                                          monkeypatch):
    """The stage report was never written and the process is about to exit,
    so the manifest checkpoint is the entire audit trail — and it is what a
    later `--start-from <stage>` on another model gets read against."""
    import src.pipeline_runner as pr
    from src.skill_runner import SkillRefusedError

    recorded: list[tuple] = []

    class _Project:
        def set_checkpoint(self, cp_id, round_id, stage, kind, payload=None):
            recorded.append((cp_id, stage, kind, payload))

    monkeypatch.setattr(pr, "SkillRunner", _always_refuses([]))
    runner = pr.PipelineRunner(config=yaml.safe_load(
        (_ROOT / "config.yaml").read_text(encoding="utf-8")),
        provider="gemini", output_dir=tmp_path)
    runner._project, runner._round_id = _Project(), "round-1"

    with pytest.raises(SkillRefusedError):
        runner._run_stage("complex-structure-analysis", "q", [],
                          tmp_path / "out.md", stage="interface")

    assert recorded, "a refusal that stopped the run left no manifest record"
    cp_id, stage, _kind, payload = recorded[-1]
    assert cp_id == "refusal:interface" and stage == "interface"
    assert payload["declined_by"] == "gemini-3.7-flash"
    assert payload["category"] == "bio"
    assert payload["automatic_fallback"] is False


def test_the_refusal_names_the_controls_and_what_using_them_commits_you_to(
        tmp_path, monkeypatch):
    """A terminal refusal that does not say what to do next is a dead end —
    but the message must not read as a sanctioned route past a classifier
    either, which is the trap the docs fell into once already (an efficacy
    claim, "this often works", next to the instructions).

    So both halves are pinned: the controls, AND the accountability. The log
    is what an operator actually reads; the policy is only real if it is here
    too and not just in docs/responsible-use.md.
    """
    import src.pipeline_runner as pr
    from src.skill_runner import SkillRefusedError

    monkeypatch.setattr(pr, "SkillRunner", _always_refuses([]))
    runner = pr.PipelineRunner(config=yaml.safe_load(
        (_ROOT / "config.yaml").read_text(encoding="utf-8")),
        provider="gemini", output_dir=tmp_path)

    with captured_logs("ERROR") as messages:
        with pytest.raises(SkillRefusedError):
            runner._run_stage("complex-structure-analysis", "q", [],
                              tmp_path / "out.md", stage="interface")
    logged = "\n".join(messages)
    for expected in ("--provider", "stages.interface", "--start-from interface",
                     "responsible-use.md"):
        assert expected in logged, f"the refusal log never names {expected!r}"
    # The policy half. Without these the message is just a how-to.
    assert "Do not reword" in logged
    assert "accountable" in logged
    assert "two frontier models decline" in logged
    # And it must not promise that switching works.
    for banned in ("often works", "usually works", "will answer",
                   "least likely to help"):
        assert banned not in logged.lower(), (
            f"the refusal log advertises model-switching as effective: "
            f"{banned!r}")


def test_the_policy_does_not_advertise_model_switching_as_effective():
    """The contradiction this guards against was live in the repo.

    `docs/responsible-use.md` removed the automatic chain and then, two
    headings later, explained how to do it by hand — with an efficacy claim
    ("Refusals are strongly model-dependent, so this often works"), a worked
    example naming the exact rung that had been cut from the chain, and a note
    on which family to skip to save a wasted attempt. That is the deleted
    chain with a person in the loop, and the policy cannot both forbid and
    optimise it.
    """
    policy = (_ROOT / "docs" / "responsible-use.md").read_text(encoding="utf-8")
    lowered = policy.lower()
    # Only phrasings that cannot occur in a negative construction. "which
    # model to try next" is deliberately NOT here: the policy uses it as a
    # negation ("not a map of which model to try next"), and a substring test
    # cannot tell that from the claim itself.
    for banned in ("so this often works", "usually works",
                   "least likely to help"):
        assert banned not in lowered, (
            f"responsible-use.md advertises model-switching: {banned!r}")
    # It has to say the override is the operator's to answer for, and that
    # walking model to model by hand is the same thing as the deleted chain.
    assert "accountable" in lowered
    assert "walking model to model" in lowered
    assert "not acceptable" in lowered or "will be declined" in lowered


# ── 2. a refusal reaches the record ──────────────────────────────────────────

def test_every_stage_report_names_the_model_that_wrote_it():
    """Which model produced a claim has to be answerable from the artifact
    that circulates, not from a log line that is gone."""
    import src.pipeline_runner as pr
    from src.report_common import parse_provenance

    note = pr.PipelineRunner._stage_provenance_note(
        pr.PipelineRunner.__new__(pr.PipelineRunner),
        "complex-structure-analysis", "gemini", "gemini-3.7-flash")
    assert "## MODEL PROVENANCE" in note
    assert "gemini:gemini-3.7-flash" in note
    parsed = parse_provenance(note)
    assert parsed["written_by"] == "gemini:gemini-3.7-flash"
    assert parsed["skill"] == "complex-structure-analysis"
    # Nothing can have declined first: the run would have stopped.
    assert parsed["declined"] == []


def test_the_block_says_no_model_was_substituted():
    """The provenance block is where a reader learns the policy, since it is
    the only part of it that travels with the artifact."""
    import src.pipeline_runner as pr

    note = pr.PipelineRunner._stage_provenance_note(
        pr.PipelineRunner.__new__(pr.PipelineRunner),
        "complex-structure-analysis", "gemini", "gemini-3.7-flash")
    assert "never substitutes" in note
    assert "ends the run" in note


def test_an_old_campaigns_declined_list_is_still_read():
    """`parse_provenance` and `run_provenance` are collectors over whatever is
    on disk. No new run can write a declined list, but campaigns that ran
    under the old automatic-fallback behaviour have reports carrying one, and
    dropping the field would quietly rewrite their history."""
    from src.report_common import parse_provenance

    historical = ("## MODEL PROVENANCE\n\n- skill: `x`\n"
                  "- written by: **claude-opus-5** — model 2 of 2 asked\n"
                  "- declined first (1):\n"
                  "    - `gemini-3.7-flash` — safety classifier, category "
                  "`bio` (call #4)\n")
    parsed = parse_provenance(historical)
    assert parsed["declined"] == [{"model": "gemini-3.7-flash",
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
