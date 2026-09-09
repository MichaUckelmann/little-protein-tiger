"""A structure switch has to outlive the process that made it.

`_select_designable_structure` can override the entry the pathway stage chose —
on CALCRL/RAMP1 it replaced 6E3Y (3.3 Å, 7 chains, a nanobody and a G protein in
the way) with 3N7S (2.1 Å, the ectodomain complex alone). Two ways that decision
used to leak away afterwards, both found by reading a finished run's artifacts
rather than the code:

  - **The literature stage re-introduced the old entry.** It runs after the
    switch, but reads `00_pathway.md` as context, and that report recommends the
    replaced entry throughout its body — the correction `_note_structure_switch`
    appends lands after it. So the stage wrote its own `design_query` naming
    6E3Y, and `_stage_design` passes `design_query` verbatim to the
    design-script skill: on `--design-engine boltzgen` the designer was handed
    the wrong entry, not merely a stale sentence.

  - **A resume reverted to it.** The switch only re-runs while `start_idx <= 1`,
    and `00_pathway.md`'s handoff block still reads `- pdb_id: 6E3Y`, which is
    what a resume parses. `--start-from structure` — the ordinary way back into
    a run after a stage failure or a prompt edit — therefore went and analysed
    the entry the pipeline had already measured as the wrong one.

The decision is in the manifest either way, so both fixes read it back rather
than re-deriving it (which would cost another RCSB round-trip) or trusting the
file on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.pipeline_runner import PipelineResult, PipelineRunner
from src.project import Project

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def runner(config) -> PipelineRunner:
    return PipelineRunner(config, workflow="ppi", provider="gemini")


# ------------------------------------------------------------------
# retargeting a handoff's instructions
# ------------------------------------------------------------------

class TestRetargetStaleStructure:
    def test_instruction_fields_are_pointed_at_the_chosen_entry(self, runner):
        handoff = {
            "structure_query": "Analyze PDB 6E3Y at data/structures/6E3Y.cif.",
            "design_query": "Generate mini_protein design inputs for CALCRL / RAMP1, PDB 6E3Y.",
            "literature_query": "Find inhibitors described against 6E3Y.",
        }
        runner._retarget_stale_structure(handoff, "6E3Y", "3N7S")
        assert "6E3Y" not in " ".join(handoff.values())
        assert handoff["design_query"].count("3N7S") == 1
        assert "3N7S.cif" in handoff["structure_query"]

    def test_claims_about_the_evidence_are_left_alone(self, runner):
        """Rewriting these would corrupt the record, not correct it.

        `go_rationale` naming the cryo-EM structure is a true statement about
        what the evidence WAS. Worse, a sentence pairing an id with that
        entry's paper would end up attributing the DOI to a different
        structure.
        """
        handoff = {
            "go_rationale": "High-resolution human cryo-EM structure (PDB 6E3Y) "
                            "and literature-validated hotspots.",
            "target_site_hint": '{"notes":"epitope from cryo-EM 6E3Y '
                                'doi:10.1038/s41586-018-0535-y"}',
            "design_query": "Design against PDB 6E3Y.",
        }
        runner._retarget_stale_structure(handoff, "6E3Y", "3N7S")
        assert "6E3Y" in handoff["go_rationale"]
        assert "doi:10.1038/s41586-018-0535-y" in handoff["target_site_hint"]
        assert "6E3Y" in handoff["target_site_hint"]
        assert handoff["design_query"] == "Design against PDB 3N7S."

    def test_case_insensitive_and_idempotent(self, runner):
        handoff = {"design_query": "PDB 6e3y and 6E3Y."}
        runner._retarget_stale_structure(handoff, "6E3Y", "3N7S")
        assert handoff["design_query"] == "PDB 3N7S and 3N7S."
        runner._retarget_stale_structure(handoff, "6E3Y", "3N7S")
        assert handoff["design_query"] == "PDB 3N7S and 3N7S."

    def test_a_no_op_switch_changes_nothing(self, runner):
        handoff = {"design_query": "PDB 3N7S."}
        for stale, better in (("", "3N7S"), ("3N7S", ""), ("3N7S", "3n7s")):
            runner._retarget_stale_structure(handoff, stale, better)
        assert handoff["design_query"] == "PDB 3N7S."

    def test_a_missing_field_is_not_invented(self, runner):
        handoff = {"design_query": "PDB 6E3Y."}
        runner._retarget_stale_structure(handoff, "6E3Y", "3N7S")
        assert "structure_query" not in handoff
        assert "literature_query" not in handoff


# ------------------------------------------------------------------
# surviving a restart
# ------------------------------------------------------------------

class TestCheckpointLookup:
    def test_a_resolved_checkpoint_is_still_findable(self, tmp_path):
        """`open_checkpoints` only lists pending ones, which is the wrong
        question for "what did this run already decide"."""
        proj = Project.create("sw", root=tmp_path)
        rid = proj.new_round()["run_id"]
        proj.set_checkpoint("structure_switched", rid, "pathway", "choice",
                            payload={"from": "6E3Y", "to": "3N7S"})
        proj.resolve_checkpoint("structure_switched", rid)
        assert proj.open_checkpoints(rid) == []
        cp = proj.checkpoint("structure_switched", rid)
        assert cp and cp["payload"]["to"] == "3N7S"

    def test_absent_checkpoint_is_none_not_an_error(self, tmp_path):
        proj = Project.create("sw2", root=tmp_path)
        rid = proj.new_round()["run_id"]
        assert proj.checkpoint("structure_switched", rid) is None


class TestReapplyOnResume:
    def _runner_with_switch(self, config, tmp_path, **payload):
        proj = Project.create("resume", root=tmp_path)
        rid = proj.new_round()["run_id"]
        proj.set_checkpoint("structure_switched", rid, "pathway", "choice",
                            payload={"from": "6E3Y", "to": "3N7S",
                                     "reason": "4 scaffolding chains", **payload})
        r = PipelineRunner(config, workflow="ppi", provider="gemini")
        r._project, r._round_id = proj, rid
        return r

    def _result(self, pdb):
        res = PipelineResult(run_dir=Path("."))
        res.pdb_id = pdb
        return res

    def test_a_resumed_stale_entry_is_restored(self, config, tmp_path):
        r = self._runner_with_switch(config, tmp_path)
        res = self._result("6E3Y")
        handoff = {"pdb_id": "6E3Y",
                   "design_query": "Design against PDB 6E3Y."}
        r._reapply_recorded_structure_switch(res, handoff)
        assert res.pdb_id == "3N7S"
        assert handoff["pdb_id"] == "3N7S"
        assert handoff["design_query"] == "Design against PDB 3N7S."
        assert res.structure_switch == {"from": "6E3Y", "to": "3N7S",
                                        "reason": "4 scaffolding chains"}

    def test_an_already_correct_entry_is_untouched(self, config, tmp_path):
        r = self._runner_with_switch(config, tmp_path)
        res = self._result("3N7S")
        handoff = {"pdb_id": "3N7S"}
        r._reapply_recorded_structure_switch(res, handoff)
        assert res.pdb_id == "3N7S"
        assert res.structure_switch is None      # nothing to restore

    def test_an_unrelated_entry_is_not_hijacked(self, config, tmp_path):
        """Only the recorded stale id is corrected. A run resumed against some
        third structure is the operator's business, not a switch to undo."""
        r = self._runner_with_switch(config, tmp_path)
        res = self._result("7CZD")
        handoff = {"pdb_id": "7CZD"}
        r._reapply_recorded_structure_switch(res, handoff)
        assert res.pdb_id == "7CZD"

    def test_no_project_is_not_an_error(self, config):
        r = PipelineRunner(config, workflow="ppi", provider="gemini")
        res = self._result("6E3Y")
        r._reapply_recorded_structure_switch(res, {})
        assert res.pdb_id == "6E3Y"               # fail-open, no crash


def test_the_instruction_field_list_covers_every_query_a_stage_is_handed():
    """A new `*_query` handoff field must be considered here.

    Every field named `<something>_query` is prose one stage writes and another
    is given as its instruction — exactly what goes stale after a switch. This
    fails when one is added, so the choice to include or exclude it is made
    deliberately rather than by omission.
    """
    import re
    src = Path("src/pipeline_runner.py").read_text(encoding="utf-8")
    known = set(PipelineRunner._STRUCTURE_INSTRUCTION_FIELDS)
    # Fields the pipeline READS off a handoff, not ones it merely mentions.
    read = set(re.findall(r'(?:handoff|intel|lit)[a-z_]*\.get\(\s*"(\w+_query)"', src))
    missing = read - known
    assert not missing, (
        f"handoff query field(s) {sorted(missing)} are read as stage instructions "
        f"but not listed in _STRUCTURE_INSTRUCTION_FIELDS — decide whether a "
        f"structure switch should rewrite them")
