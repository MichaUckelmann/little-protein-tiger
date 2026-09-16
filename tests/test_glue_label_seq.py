"""label_seq_id is resolved against the chain each row is actually on.

`_resolve_unverified_label_seq_ids` built ONE gemmi auth->label map, for
`target_chain`, and resolved every row against it. On a molecular-glue table
the co-target's rows are real residues on their OWN chain, so the map it was
holding described a different residue at that auth id.

It did not silently corrupt them — a residue-NAME guard added in 9965bb6
leaves a mismatched row alone — so the failure is that the co-target's
label_seq column could never be RESOLVED, not that it was rewritten wrong.
That guard is narrow, though: if the other chain happens to carry the same
residue name at the same auth id, the name check passes and the wrong chain's
label is written. Both properties are pinned here.

The `binding:` lines matter more than they look. BoltzGen reads `binding:` as
label_seq, so a wrong value constrains the binder to the wrong residues — the
documented cause of zero hotspot occlusion on the YAP-TEAD run. A glue report
writes them as `Chain <id> binding: ...`, and today's `^\\s*binding:` pattern
does not match that form at all, so a glue campaign shipped the model's own
counted numbers. Measured on 5VAI: the model wrote `Chain A binding: 38,39,42`
for residues whose real label ids are 105,106,109.

Measured with gemmi on the files in `data/structures/`:

    5VAI R: 66->105 67->106 70->109
    5VAI P: 30->24  35->29  36->30  37->31
    6JJW A: 8->10 ... 37->39      6JJW U: 447->24 ... 453->30
"""

from __future__ import annotations

import pathlib
import re

import pytest

from src.handoff import parse_handoff
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


def _resolve(pdb: str, *, with_handoff: bool = True):
    text = _CASES[pdb].read_text(encoding="utf-8")
    handoff = parse_handoff(text)
    kw = {"handoff": handoff} if with_handoff else {}
    return _runner()._resolve_unverified_label_seq_ids(
        text, _STRUCTURES / f"{pdb}_ba1.cif",
        handoff.get("target_chain") or "A", **kw)


def _rows(text: str) -> list[tuple[str, int, str]]:
    return [(m.group(1), int(m.group(2)), m.group(3).strip())
            for m in re.finditer(r"^\|\s*([A-Z]{3})\s*\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|",
                                 text, re.MULTILINE)]


def _binding(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if "binding:" in ln]


# ------------------------------------------------ each row against its own chain

def test_5vai_resolves_both_chains_against_their_own_maps():
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    fixed, _ = _resolve("5VAI")
    assert _rows(fixed) == [
        ("PHE", 66, "105"), ("ASP", 67, "106"), ("ALA", 70, "109"),
        ("ALA", 30, "24"), ("GLY", 35, "29"), ("ARG", 36, "30"), ("GLY", 37, "31"),
    ]


def test_6jjw_resolves_both_chains_against_their_own_maps():
    if not _available("6JJW"):
        pytest.skip("6JJW fixture not in this checkout")
    fixed, _ = _resolve("6JJW")
    rows = {(n, a): lbl for n, a, lbl in _rows(fixed)}
    assert rows[("THR", 447)] == "24" and rows[("LYS", 453)] == "30"
    assert rows[("LEU", 8)] == "10" and rows[("PRO", 37)] == "39"


def test_the_co_targets_rows_were_previously_unresolvable():
    """Without the split they hit the residue-NAME guard and were left alone.

    5VAI chain P auth 30/35/36/37 are ALA/GLY/ARG/GLY; on chain R the same
    numbers are VAL/THR/VAL/GLN. So the old single-map code produced four
    NAME-mismatch warnings and corrected nothing.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    text = _CASES["5VAI"].read_text(encoding="utf-8")
    _, warns = _runner()._resolve_unverified_label_seq_ids(
        text, _STRUCTURES / "5VAI_ba1.cif", "R")  # no handoff => one map
    assert sum("NAME mismatch" in w for w in warns) == 4

    _, warns_split = _resolve("5VAI")
    assert not any("NAME mismatch" in w for w in warns_split)


def test_a_wrong_name_on_its_own_chain_is_still_left_alone():
    """The 9965bb6 name guard survives the split; it just applies per chain."""
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    text = _CASES["5VAI"].read_text(encoding="utf-8").replace(
        "| ALA | 30 | 69 |", "| TRP | 30 | 69 |")
    handoff = parse_handoff(text)
    fixed, warns = _runner()._resolve_unverified_label_seq_ids(
        text, _STRUCTURES / "5VAI_ba1.cif", "R", handoff=handoff)
    assert any("NAME mismatch at chain P" in w for w in warns)
    assert "| TRP | 30 | 69 |" in fixed, "a mismatched row must not be rewritten"


# ----------------------------------------------------------- the binding: lines

def test_each_chains_binding_line_is_rewritten_from_its_own_rows():
    """The line's chain token is resolved the same way the headings are.

    5VAI heads its sub-tables "Chain A"/"Chain B" where the real ids are R and
    P, so a literal lookup matches nothing and leaves the counted numbers.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    before = _binding(_CASES["5VAI"].read_text(encoding="utf-8"))
    assert "Chain A binding: 38,39,42" in before, "fixture changed"

    fixed, _ = _resolve("5VAI")
    assert _binding(fixed) == [
        "Chain A binding: 105,106,109",
        "Chain B binding: 24,29,30,31",
        "Chain A only: binding: 105,106,109",
        "Chain B only: binding: 24,29,30,31",
    ]


def test_a_binding_line_is_not_replaced_by_the_unavailable_marker():
    """The trap the per-block design walked into.

    `binding:` lines live in a `#### BoltzGen binding` subsection that carries
    no table rows, so splitting the section per chain and running the
    single-chain body per block gives every binding-carrying block zero local
    residues — and the no-label_seq branch REPLACES the line, deleting the
    value it was meant to correct.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    fixed, _ = _resolve("5VAI")
    assert "UNAVAILABLE" not in fixed
    assert len(_binding(fixed)) == 4


def test_a_chain_less_binding_line_in_a_glue_section_is_left_pooled_and_warned():
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    text = _CASES["5VAI"].read_text(encoding="utf-8").replace(
        "Chain A binding: 38,39,42", "binding: 38,39,42", 1)
    handoff = parse_handoff(text)
    fixed, warns = _runner()._resolve_unverified_label_seq_ids(
        text, _STRUCTURES / "5VAI_ba1.cif", "R", handoff=handoff)
    assert "binding: 38,39,42" in fixed, "a pooled line means pooled; leave it"
    assert any("chain-less `binding:` line" in w for w in warns)


# ------------------------------------------------------- the legacy signature

def test_without_a_handoff_a_single_chain_report_is_unchanged():
    """101 of the 105 reports on disk with a local structure; the guarantee."""
    section = ("## PPI ANALYSIS REPORT\n\n### MODEL-READY HOTSPOTS\n\n"
               "| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |\n"
               "|---|---|---|---|\n| ALA | 30 | **UNVERIFIED** | CB,CA |\n")
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    a, _ = _runner()._resolve_unverified_label_seq_ids(
        section, _STRUCTURES / "5VAI_ba1.cif", "P")
    b, _ = _runner()._resolve_unverified_label_seq_ids(
        section, _STRUCTURES / "5VAI_ba1.cif", "P", handoff={})
    assert a == b
    assert "| ALA | 30 | 24 |" in a


def test_without_a_handoff_an_unresolvable_guessed_chain_fails_open():
    """A missing argument must not turn into a failed run.

    6JJW holds chains A and U only, so a positional "Chain B" heading read
    with no handoff names a chain that is not there. Refusing on a guess would
    abort; `_verify_hotspot_grounding` owns that diagnosis and knows what each
    row claims.
    """
    if not _available("6JJW"):
        pytest.skip("6JJW fixture not in this checkout")
    fixed, warns = _resolve("6JJW", with_handoff=False)
    assert any("inferred from a table heading" in w for w in warns)
    assert "| THR | 447 | 14 |" in fixed, "guessed-chain rows are left as written"


def test_a_guessed_chain_that_exists_but_is_the_wrong_molecule_corrupts_nothing():
    """The nastiest shape, and it is real: 5VAI HAS chains A and B.

    They are the Gs heterotrimer's alpha and beta subunits — the structure's
    chains are R, P, A, B, G, N — so a positional heading read with no handoff
    names real chains that are entirely the wrong molecules, and the
    not-in-the-file fail-open above never fires. What holds here is the
    residue-NAME guard: chain B's auth 66/67/70 are ASP/SER/LEU against the
    table's PHE/ASP/ALA, so every row mismatches and is left as written. No
    value is corrected, and none is corrupted.

    This is the argument for passing `handoff=` everywhere, not a licence to
    skip it.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    original = _CASES["5VAI"].read_text(encoding="utf-8")
    fixed, warns = _resolve("5VAI", with_handoff=False)

    assert _rows(fixed) == _rows(original), "no row may be rewritten from a wrong chain"
    assert any("NAME mismatch" in w or "not present in structure" in w for w in warns)


def test_a_declared_chain_that_is_not_in_the_file_still_refuses():
    """With a handoff we had the information, so an empty map is an error."""
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    text = _CASES["5VAI"].read_text(encoding="utf-8")
    handoff = dict(parse_handoff(text))
    handoff["partner_chain"] = "ZZ"      # declared, and not in 5VAI
    handoff["target_chains"] = "R, ZZ"
    with pytest.raises(PipelineError, match="auth->label map for chain ZZ"):
        _runner()._resolve_unverified_label_seq_ids(
            text, _STRUCTURES / "5VAI_ba1.cif", "R", handoff=handoff)


def test_the_chain_index_ignores_headings_outside_a_hotspot_section():
    """COMPLEX OVERVIEW writes `Chain A: Hydrophobic: ...` — prose, not a table.

    An index over the whole document would attribute rows from it.
    """
    if not _available("5VAI"):
        pytest.skip("5VAI fixture not in this checkout")
    text = _CASES["5VAI"].read_text(encoding="utf-8")
    assert re.search(r"^- Chain A:", text, re.MULTILINE), "fixture changed"
    fixed, _ = _resolve("5VAI")
    assert ("PHE", 66, "105") in _rows(fixed)
