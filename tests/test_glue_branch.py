"""`design_intent: stabilize` is askable, and the paths it cannot take refuse.

Before this, a glue run could only arise from an LLM stage measuring it, and
then died several stages later blaming something else: `div_standard_diabetes`
spent three LLM stages and stopped at hotspot grounding with a message about
"textbook/literature numbering", which is not what was wrong with it.

Two things are pinned here. `--design-intent stabilize` on `--workflow
structure` makes a glue run expressible with no LLM call at all, which is what
gives every later step a zero-cost end-to-end command. And every combination
Stage 3 has not built refuses up front, naming the scope item that would build
it — a refusal is the cheapest possible failure and the only one that says why.

The single-chain guarantee runs through all of it: every new predicate is
gated on `is_glue_intent` or on an absent flag, so a run that does not ask for
glue cannot reach any of this.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.handoff import parse_handoff, parse_hotspot_residues
from src.pipeline_runner import (
    PipelineBlockedError,
    PipelineError,
    PipelineRunner,
    is_glue_intent,
    waives_partner_chain,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_4ZGM = _ROOT / "data/structures/4ZGM_ba1.cif"


def _bare() -> PipelineRunner:
    """A runner with only the attributes the refusals read."""
    r = PipelineRunner.__new__(PipelineRunner)
    r.config = {"paths": {"structures_dir": "data/structures"}}
    # `_boltzgen_backend` is a property over `_design_engine`; set the field.
    r._design_engine = "foundry"
    r._modality = "mini_protein"
    r._trial_sites = 1
    r._workflow = "structure"
    r._design_intent = None
    r._hotspots = None
    return r


# ------------------------------------------------------------------ predicates

def test_is_glue_intent_is_exact():
    assert is_glue_intent("stabilize") and is_glue_intent("  STABILIZE ")
    for other in ("disrupt", "inhibit_active_site", "", None, "stabilise"):
        assert not is_glue_intent(other), other


def test_only_a_single_target_intent_waives_the_partner():
    """A glue needs BOTH chains, so it must not be waived with the monomer."""
    assert waives_partner_chain({"design_intent": "inhibit_active_site"})
    assert not waives_partner_chain({"design_intent": "stabilize"})
    assert not waives_partner_chain({"design_intent": "disrupt"})
    assert not waives_partner_chain({})
    # Accepts the bare string too, because the two call sites hold different
    # shapes of the same fact.
    assert waives_partner_chain("inhibit_active_site")


# ------------------------------------------------------------------- the refusals

@pytest.mark.parametrize("attr,value,needle", [
    ("_design_engine", "boltzgen", "no two-chain target path"),
    ("_modality", "cyclic_peptide", "cyclic_peptide"),
    ("_trial_sites", 3, "--trial-sites"),
    ("_workflow", "ppi", "--workflow structure"),
])
def test_an_unbuilt_glue_combination_refuses_up_front(attr, value, needle):
    r = _bare()
    setattr(r, attr, value)
    with pytest.raises(PipelineBlockedError, match=needle):
        r._refuse_unbuilt_glue_paths("stabilize", source="test")


@pytest.mark.parametrize("attr,value", [
    ("_design_engine", "boltzgen"),
    ("_modality", "cyclic_peptide"),
    ("_trial_sites", 3),
    ("_workflow", "ppi"),
])
@pytest.mark.parametrize("intent", ["disrupt", "inhibit_active_site", "", None])
def test_a_non_glue_run_reaches_none_of_them(attr, value, intent):
    """THE guarantee: every refusal is gated on the intent, not the flag."""
    r = _bare()
    setattr(r, attr, value)
    r._refuse_unbuilt_glue_paths(intent, source="test")  # must not raise


def test_the_refusal_names_the_scope_item_that_would_build_it():
    r = _bare()
    r._design_engine = "boltzgen"
    with pytest.raises(PipelineBlockedError, match="scope item 22"):
        r._refuse_unbuilt_glue_paths("stabilize", source="test")


# ------------------------------------------------------------- the intent resolver

def test_without_the_flag_the_measured_intent_is_returned_unchanged():
    r = _bare()
    for measured in ("disrupt", "inhibit_active_site"):
        assert r._resolve_design_intent(
            measured, source="t", has_partner=True) == measured


def test_the_flag_overrides_what_was_measured():
    r = _bare()
    r._design_intent = "stabilize"
    assert r._resolve_design_intent(
        "disrupt", source="t", has_partner=True) == "stabilize"


def test_stabilize_on_one_chain_is_refused():
    """Not a preference that can be honoured differently — there is no partner.

    A molecular glue holds two proteins together; with one designable chain
    there is no second protein to hold.
    """
    r = _bare()
    r._design_intent = "stabilize"
    with pytest.raises(PipelineBlockedError, match="needs two chains"):
        r._resolve_design_intent("inhibit_active_site", source="t",
                                 has_partner=False)


# ------------------------------------------------------------------- the CLI gate

def test_design_intent_is_refused_off_the_structure_track(tmp_path):
    """Same posture as --hotspots on the PPI track: refuse, do not silently run."""
    cfg = {"paths": {"structures_dir": "data/structures"}}
    for workflow in ("ppi", "binder"):
        with pytest.raises(PipelineError, match="--design-intent applies to"):
            PipelineRunner(cfg, workflow=workflow, design_intent="stabilize",
                           project="x")


def test_an_unknown_intent_is_refused():
    cfg = {"paths": {"structures_dir": "data/structures"}}
    with pytest.raises(PipelineError, match="Invalid design_intent"):
        PipelineRunner(cfg, workflow="structure", design_intent="glue",
                       project="x")


def test_the_cli_exposes_the_flag():
    import scripts.run_pipeline as rp
    action = next(a for a in rp._build_parser()._actions
                  if "--design-intent" in (a.option_strings or []))
    assert set(action.choices) == {"disrupt", "stabilize", "inhibit_active_site"}
    assert action.default is None, "absent by default, so nothing is overridden"


# --------------------------------------------------- the operator-specified epitope

@pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")
def test_a_two_chain_epitope_round_trips_through_every_guard(tmp_path):
    """The Stage 3 end-to-end command, with no LLM, no GPU and no network.

    Operator numbers in; structure-read residue names and atoms out; a report
    on disk; re-parsed with each row on its own chain; grounded. 4ZGM is
    chain A 29-128 (100 residues) and chain B 10-37 (28), so no trim is
    needed and none of the single-chain trim measurements is involved.
    """
    r = _bare()
    intel = {"pdb_id": "4ZGM", "target_chain": "A", "partner_chain": "B",
             "design_intent": "stabilize", "modality": "mini_protein"}

    residues = r._resolve_hotspot_override("A113,A120,B30,B33", "4ZGM", "A",
                                           partner_chain="B")
    assert [(h["chain"], h["residue"], h["auth_seq_id"]) for h in residues] == [
        ("A", "LYS", 113), ("A", "TRP", 120), ("B", "ALA", 30), ("B", "VAL", 33)]

    out = tmp_path / "21_interface.md"
    r._write_binder_report = lambda o, t, b, h: o.write_text(
        f"# {t}\n\n{b}\n\n### PIPELINE HANDOFF\n"
        + "".join(f"- {k}: {v}\n" for k, v in h.items()))
    r._write_override_interface_report(out, intel, residues, "glue 4ZGM")
    text = out.read_text()

    # The select_hotspots block carries each residue's OWN chain. On 4ZGM
    # every partner hotspot number also exists on chain A, so writing them all
    # under the target would be invisible.
    assert "    B30: CB" in text and "    A113: NZ,CE" in text

    handoff = parse_handoff(text)
    assert handoff["target_chains"] == "A, B"
    parsed = json.loads(parse_hotspot_residues(text, handoff))
    assert [(h["chain"], h["auth_seq_id"]) for h in parsed["residues"]] == [
        ("A", 113), ("A", 120), ("B", 30), ("B", 33)]

    r._verify_hotspot_grounding(json.dumps(parsed), "4ZGM")


@pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")
def test_a_partner_hotspot_is_refused_without_a_glue_intent():
    """The partner is a legal hotspot chain for a glue and for nothing else."""
    r = _bare()
    with pytest.raises(PipelineBlockedError, match="Hotspots are the epitope ON"):
        r._resolve_hotspot_override("A113,B30", "4ZGM", "A")


@pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")
def test_a_third_chain_is_refused_even_for_a_glue():
    r = _bare()
    with pytest.raises(PipelineBlockedError, match="spans exactly the two chains"):
        r._resolve_hotspot_override("A113,C5", "4ZGM", "A", partner_chain="B")


@pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")
def test_the_single_chain_report_form_is_unchanged(tmp_path):
    """Gated on the residues handed in, so a disrupt override is byte-identical."""
    r = _bare()
    residues = r._resolve_hotspot_override("A113,A120", "4ZGM", "A")
    out = tmp_path / "21_interface.md"
    r._write_binder_report = lambda o, t, b, h: o.write_text(b)
    r._write_override_interface_report(
        out, {"pdb_id": "4ZGM", "target_chain": "A", "partner_chain": "B",
              "design_intent": "disrupt"}, residues, "q")
    text = out.read_text()
    assert "Target chain A — Region 1: operator-specified" in text
    assert "Chain A — Region 1" not in text, "one chain keeps the old heading"


# --------------------------------------------------------- the trim partner check

def test_a_disrupt_run_that_lost_its_partner_refuses_at_the_trim():
    """Previously it degraded silently into single-target mode.

    `_binder_sites` makes this check against target-intel's handoff, but is
    reached only via --trial-sites or --stop-after trial|spec; a plain
    single-site run is stopped here instead. Two dicts, so the check has to
    exist in both or the failure just moves a stage later.

    Measured unreachable for every run already on disk: of the 111 stage
    handoffs in `projects/`, none has a blank partner without also carrying a
    single-target intent.
    """
    assert not waives_partner_chain({"design_intent": "disrupt"})
    assert not waives_partner_chain({"design_intent": "stabilize"})


def test_no_shipped_run_would_newly_refuse_at_the_trim():
    """The survey behind the claim above, run as a test rather than asserted."""
    from src.pipeline_runner import chain_id_or_blank
    files = (list(_ROOT.glob("projects/*/runs/*/**/21_interface.md"))
             + list(_ROOT.glob("projects/*/runs/*/02_structure.md")))
    if not files:
        pytest.skip("no shipped runs in this checkout")
    offenders = []
    for f in files:
        h = parse_handoff(f.read_text(encoding="utf-8", errors="replace"))
        if (h.get("target_chain") and not chain_id_or_blank(h.get("partner_chain"))
                and not waives_partner_chain(h)):
            offenders.append(str(f.relative_to(_ROOT)))
    assert offenders == [], f"these shipped runs would newly refuse: {offenders}"
