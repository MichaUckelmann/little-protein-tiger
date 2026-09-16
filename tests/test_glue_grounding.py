"""Grounding looks each hotspot up on the chain it was attributed to.

`_verify_hotspot_grounding` read every row against `target_chain`. On a
molecular-glue table that reports real residues of the co-target as absent or
misnamed — and it is what actually ends a `design_intent: stabilize` run
today, before any of the trim/spec blockers is even reached.

The half of this that matters more is what does NOT change. A row with no
`chain` key is a legacy table that never said which chain it meant, and still
groups under `target_chain`, so the cross-chain refusal is preserved for
exactly the shape that has not answered the question and relaxed only for a
table that has. Both branches of that refusal are pinned below against the two
real reports, by stripping the `chain` key off rows that are otherwise
identical — same file, same residues, same numbers, opposite verdict.

Measured with gemmi on the files in `data/structures/`:

    5VAI R: 66->PHE 67->ASP 70->ALA      (auth 30/35/36/37 are VAL/THR/VAL/GLN)
    5VAI P: 30->ALA 35->GLY 36->ARG 37->GLY
    6JJW U: 447..453 present             (6JJW A carries none of them)
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.handoff import parse_handoff, parse_hotspot_residues
from src.pipeline_runner import PipelineError, PipelineRunner

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STRUCTURES = _ROOT / "data/structures"

_CASES = {
    "5VAI": _ROOT / "projects/div_standard_diabetes/runs/round-1/02_structure.md",
    "6JJW": _ROOT / "projects/div_wildcard_tnbc/runs/round-1/02_structure.md",
}


def _runner() -> PipelineRunner:
    r = PipelineRunner.__new__(PipelineRunner)
    r.config = {"paths": {"structures_dir": "data/structures"}}
    return r


def _available(pdb: str) -> bool:
    return _CASES[pdb].exists() and (_STRUCTURES / f"{pdb}_ba1.cif").exists()


def _hotspots(pdb: str) -> str:
    text = _CASES[pdb].read_text(encoding="utf-8")
    return parse_hotspot_residues(text, parse_handoff(text))


def _strip_chains(hotspots_json: str) -> str:
    """The same table as a legacy report would have carried it."""
    data = json.loads(hotspots_json)
    for row in data["residues"]:
        row.pop("chain", None)
    data.pop("target_chains", None)
    return json.dumps(data)


# ------------------------------------------------------ an attributed table grounds

@pytest.mark.parametrize("pdb", ["5VAI", "6JJW"])
def test_an_attributed_glue_table_grounds(pdb):
    """Both real glue runs, both previously fatal here."""
    if not _available(pdb):
        pytest.skip(f"{pdb} fixture not in this checkout")
    _runner()._verify_hotspot_grounding(_hotspots(pdb), pdb)


# ---------------------------------------------- a chain-LESS table still refuses

def test_a_chain_less_table_still_fails_on_the_mismatch_branch():
    """5VAI's four chain-P rows are real residues at real numbers on P.

    On chain R those same numbers are VAL/THR/VAL/GLN, so a table that has not
    said which chain it means still fails — and must, because nothing in it
    distinguishes a glue pocket from a stage that got the chain wrong.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    with pytest.raises(PipelineError, match="not grounded in 5VAI's actual numbering"):
        _runner()._verify_hotspot_grounding(_strip_chains(_hotspots("5VAI")), "5VAI")


def test_a_chain_less_table_still_fails_on_the_absent_branch():
    """6JJW's chain-U rows do not exist on A at all, and the hint still names U."""
    if not _available("6JJW"):
        pytest.skip("6JJW fixture not in this checkout")
    with pytest.raises(PipelineError) as exc:
        _runner()._verify_hotspot_grounding(_strip_chains(_hotspots("6JJW")), "6JJW")
    assert "do not exist in 6JJW chain A" in str(exc.value)
    assert "present under those names in chain U" in str(exc.value)


# ------------------------------------------------- attribution is not a waiver

def test_a_wrong_residue_name_still_fails_on_its_own_chain():
    """Per-chain lookup is not per-chain trust. The guard's whole job survives."""
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    data = json.loads(_hotspots("5VAI"))
    for row in data["residues"]:
        if row["chain"] == "P" and row["auth_seq_id"] == 30:
            row["residue"] = "TRP"  # P30 is ALA
    with pytest.raises(PipelineError, match=r"TRP30 \(structure has ALA30\)"):
        _runner()._verify_hotspot_grounding(json.dumps(data), "5VAI")


def test_a_residue_absent_from_its_own_chain_still_fails():
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    data = json.loads(_hotspots("5VAI"))
    data["residues"].append({"residue": "ALA", "auth_seq_id": 99999,
                             "label_seq_id": 99999, "rfd3_atoms": "CB,CA",
                             "chain": "P"})
    with pytest.raises(PipelineError, match="do not exist in 5VAI chain P"):
        _runner()._verify_hotspot_grounding(json.dumps(data), "5VAI")


def test_a_row_attributed_to_a_chain_that_is_not_in_the_file_fails():
    """Distinct from the declared-target_chain refusal, and says so."""
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    data = json.loads(_hotspots("5VAI"))
    data["residues"].append({"residue": "ALA", "auth_seq_id": 30,
                             "label_seq_id": 30, "rfd3_atoms": "CB,CA",
                             "chain": "ZZ"})
    with pytest.raises(PipelineError, match=r"attributes residues to chain 'ZZ'"):
        _runner()._verify_hotspot_grounding(json.dumps(data), "5VAI")


def test_both_chains_are_reported_at_once_not_just_the_first():
    """A two-chain table that is wrong on both sides says so in one message.

    The caller owns the refusal precisely so the operator does not fix one
    chain, re-run, and discover the other.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    data = json.loads(_hotspots("5VAI"))
    data["residues"] = [
        {"residue": "ALA", "auth_seq_id": 99998, "label_seq_id": 1,
         "rfd3_atoms": "CB,CA", "chain": "R"},
        {"residue": "ALA", "auth_seq_id": 99999, "label_seq_id": 1,
         "rfd3_atoms": "CB,CA", "chain": "P"},
    ]
    with pytest.raises(PipelineError) as exc:
        _runner()._verify_hotspot_grounding(json.dumps(data), "5VAI")
    assert "chain R" in str(exc.value) and "chain P" in str(exc.value)


def test_the_chain_is_named_per_entry_only_when_two_are_involved():
    """A single-chain message must stay byte-identical; a two-chain one must not."""
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    runner = _runner()

    one = {"target_chain": "R", "residues": [
        {"residue": "TRP", "auth_seq_id": 66, "label_seq_id": 105,
         "rfd3_atoms": "CD2,CZ", "chain": "R"}]}
    with pytest.raises(PipelineError) as exc:
        runner._verify_hotspot_grounding(json.dumps(one), "5VAI")
    assert "on chain" not in str(exc.value), "one chain: no per-entry chain label"

    two = {"target_chain": "R", "residues": one["residues"] + [
        {"residue": "TRP", "auth_seq_id": 30, "label_seq_id": 24,
         "rfd3_atoms": "CD2,CZ", "chain": "P"}]}
    with pytest.raises(PipelineError) as exc:
        runner._verify_hotspot_grounding(json.dumps(two), "5VAI")
    assert "on chain R" in str(exc.value) and "on chain P" in str(exc.value)


# ------------------------------------------------------------ the numbering frame

def test_the_frame_check_gets_the_accession_for_the_target_chain_only(monkeypatch):
    """A co-target translated through the target's alignment is worse than silent.

    `_hotspot_numbering_frame` returns immediately without an accession, so
    passing None for the co-target makes it quiet; passing the target's would
    print canonical ids belonging to a different protein.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    seen: list[tuple[str, str | None, int]] = []

    def spy(self, rows, c, pdb_id, uniprot):
        seen.append((c, uniprot, len(rows)))

    monkeypatch.setattr(PipelineRunner, "_hotspot_numbering_frame", spy)
    _runner()._verify_hotspot_grounding(_hotspots("5VAI"), "5VAI", uniprot="P43220")

    assert seen == [("R", "P43220", 3), ("P", None, 4)]
