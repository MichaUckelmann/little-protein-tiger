"""A glue pocket spans two chains, and every row must carry its own.

`parse_hotspot_residues` took `target_chain` from the handoff and stamped it
on every row of every table, with a `(residue, auth_seq_id)` dedup key. On a
molecular-glue target that is wrong twice over: four of `div_standard_
diabetes`'s seven 5VAI rows belong to chain P and were attributed to R, and
the same residue name at the same author number on two chains collapsed into
one hotspot.

Neither failure is loud. Every mis-attributed residue on 5VAI is a REAL
residue at that author number on the chain it was moved to — auth 30 is ALA
on P and VAL on R — so what a wrong attribution produces is a table that
grounds, trims and validates cleanly against the wrong molecule.

The other half of what these tests pin is the thing that must NOT change.
`chain_blocks` splits only when the chosen section carries two or more
DISTINCT chain headings, which is what keeps every single-chain report
byte-identical: 65 of 108 shipped reports declare a `partner_chain` of
literally "A" or "B" that differs from their `target_chain`, so a positional
reading of a lone legacy "Chain A" heading would resolve to the PARTNER. The
discriminator makes that case unreachable rather than merely unlikely.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.handoff import chain_blocks, parse_handoff, parse_hotspot_residues

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_DIABETES = _ROOT / "projects/div_standard_diabetes/runs/round-1/02_structure.md"
_TNBC = _ROOT / "projects/div_wildcard_tnbc/runs/round-1/02_structure.md"

_HEADER = ("| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |\n"
           "|---|---|---|---|\n")


def _rows(rows: list[tuple[str, int]]) -> str:
    return "".join(f"| {res} | {auth} | {auth} | CB,CA |\n" for res, auth in rows)


def _parse(text: str, handoff: dict) -> dict:
    return json.loads(parse_hotspot_residues(text, handoff))


# --------------------------------------------------------------- the real files

@pytest.mark.skipif(not _DIABETES.exists(), reason="5VAI glue run not in this checkout")
def test_the_5vai_glue_table_attributes_each_row_to_its_own_chain():
    """The reproduction case, and the report carries its own ground truth.

    `div_standard_diabetes`'s `02_structure.md` ends with an `RFD3
    select_hotspots (combined)` block the model wrote itself, listing exactly
    R66/R67/R70/P30/P35/P36/P37. That is the answer the parser has to produce,
    written by a different part of the same report.
    """
    text = _DIABETES.read_text(encoding="utf-8")
    parsed = _parse(text, parse_handoff(text))

    got = [(r["chain"], r["residue"], r["auth_seq_id"]) for r in parsed["residues"]]
    assert got == [
        ("R", "PHE", 66), ("R", "ASP", 67), ("R", "ALA", 70),
        ("P", "ALA", 30), ("P", "GLY", 35), ("P", "ARG", 36), ("P", "GLY", 37),
    ]
    assert parsed["target_chains"] == ["R", "P"]
    # target_chain/partner_chain keep their exact prior meaning.
    assert parsed["target_chain"] == "R"
    assert parsed["partner_chain"] == "P"


@pytest.mark.skipif(not _DIABETES.exists(), reason="5VAI glue run not in this checkout")
def test_the_5vai_headings_are_read_positionally_because_the_report_is_inconsistent():
    """"Chain A"/"Chain B" against real ids R/P — resolved, not taken literally.

    A STABILIZE report routinely heads its sub-tables A and B whatever the
    structure's chains are called. Reading the token literally would attribute
    every row to a chain that is not in the complex.
    """
    text = _DIABETES.read_text(encoding="utf-8")
    assert "Chain A (GLP-1R)" in text and "Chain B (GLP-1)" in text
    chains = {r["chain"] for r in _parse(text, parse_handoff(text))["residues"]}
    assert chains == {"R", "P"}


@pytest.mark.skipif(not _TNBC.exists(), reason="6JJW glue run not in this checkout")
def test_the_6jjw_glue_table_splits_on_a_slashed_heading():
    """"Chain B / U (PTPN14)" — the token is B, the chain is U.

    A second, independent glue run with a different heading form and a
    `target_chain` that IS literally "A", so it exercises the literal branch
    on one heading and the positional branch on the other in one file.
    """
    text = _TNBC.read_text(encoding="utf-8")
    parsed = _parse(text, parse_handoff(text))

    assert parsed["target_chains"] == ["A", "U"]
    by_chain: dict[str, int] = {}
    for r in parsed["residues"]:
        by_chain[r["chain"]] = by_chain.get(r["chain"], 0) + 1
    assert by_chain == {"A": 11, "U": 6}


# --------------------------------------------------- the single-chain guarantee

def test_a_section_with_one_chain_heading_does_not_split():
    """THE regression guard. One heading must never consult the chain resolver.

    This is the shape that would otherwise resolve to the partner: a legacy
    positional "Chain A" heading on a report whose `partner_chain` is
    literally "A" and whose `target_chain` is something else.
    """
    section = ("### MODEL-READY HOTSPOTS\n\n"
               "Chain A (some protein) periinterface patch:\n\n"
               + _HEADER + _rows([("LEU", 8), ("PRO", 9)]))
    handoff = {"target_chain": "W", "partner_chain": "A"}

    blocks = chain_blocks(section, handoff, handoff["target_chain"])
    assert blocks == [("W", section)], "one heading must yield one default block"

    parsed = _parse(section, handoff)
    assert {r["chain"] for r in parsed["residues"]} == {"W"}
    assert parsed["target_chains"] == ["W"]


def test_a_section_with_no_chain_heading_does_not_split():
    section = "### MODEL-READY HOTSPOTS\n\n" + _HEADER + _rows([("LEU", 8)])
    handoff = {"target_chain": "B", "partner_chain": "A"}
    assert chain_blocks(section, handoff, "B") == [("B", section)]
    assert _parse(section, handoff)["target_chains"] == ["B"]


def test_repeating_the_same_heading_is_not_two_chains():
    """Two headings naming ONE chain is a formatting habit, not a glue table."""
    section = ("### MODEL-READY HOTSPOTS\n\n"
               "Chain A — first patch:\n\n" + _HEADER + _rows([("LEU", 8)]) +
               "\nChain A — second patch:\n\n" + _HEADER + _rows([("PRO", 9)]))
    handoff = {"target_chain": "W", "partner_chain": "A"}
    assert chain_blocks(section, handoff, "W") == [("W", section)]


# ------------------------------------------------------------------ the dedup key

def test_the_same_residue_number_on_two_chains_survives_as_two_hotspots():
    """The chain-less key collapsed these into one; on 4ZGM that is routine.

    Every partner-chain hotspot number on 4ZGM also exists on chain A, so a
    `(residue, auth)` key silently deletes one of each colliding pair and
    leaves the survivor carrying the wrong chain's atoms.
    """
    section = ("### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket 1]\n\n"
               "Chain A (target) patch:\n\n" + _HEADER + _rows([("LEU", 32)]) +
               "\nChain B (co-target) patch:\n\n" + _HEADER + _rows([("LEU", 32)]))
    handoff = {"target_chain": "A", "partner_chain": "B"}

    residues = _parse(section, handoff)["residues"]
    assert [(r["chain"], r["auth_seq_id"]) for r in residues] == [("A", 32), ("B", 32)]


def test_a_repeated_row_on_one_chain_is_still_deduped():
    """The key gained a prefix; it did not stop deduping."""
    section = ("### MODEL-READY HOTSPOTS\n\n" + _HEADER
               + _rows([("LEU", 8), ("LEU", 8), ("PRO", 9)]))
    handoff = {"target_chain": "A", "partner_chain": "B"}
    residues = _parse(section, handoff)["residues"]
    assert [(r["residue"], r["auth_seq_id"]) for r in residues] == [("LEU", 8), ("PRO", 9)]


# ---------------------------------------------------------------- a pocket is a region

def test_a_glue_pocket_is_labelled_as_a_region():
    """`region` read "" on every glue report, because the label is bracketed.

    Two things went wrong at once: `Region` did not match `Glue Pocket`, and
    the label's terminator set had no `]`, so even once it matched it would
    have captured `Glue Pocket 1]`.
    """
    section = ("### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket 1]\n\n"
               + _HEADER + _rows([("LEU", 8)]))
    handoff = {"target_chain": "A", "partner_chain": "B"}
    assert _parse(section, handoff)["region"] == "Glue Pocket 1"


def test_primary_target_selects_the_named_pocket_not_the_first():
    """Latent on all four real glue reports; live on the first multi-pocket run.

    `Primary target: Glue Pocket 2` used to fall through to the first section,
    silently designing against the pocket the report ranked second.
    """
    def pocket(n: int, res: str, auth: int) -> str:
        return (f"### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket {n}]\n\n"
                + _HEADER + _rows([(res, auth)]) + "\n")

    text = (pocket(1, "LEU", 8) + pocket(2, "TRP", 44)
            + "## DESIGN RECOMMENDATIONS\n\nPrimary target: Glue Pocket 2 — Excellent\n")
    parsed = _parse(text, {"target_chain": "A", "partner_chain": "B"})

    assert parsed["regions_declared"] == 2
    assert parsed["region"] == "Glue Pocket 2"
    assert [r["auth_seq_id"] for r in parsed["residues"]] == [44]


def test_a_numbered_region_still_selects_the_way_it_always_did():
    """The `Region` alternative is added, not replaced."""
    def region(n: int, res: str, auth: int) -> str:
        return (f"### MODEL-READY HOTSPOTS\n\nTarget chain A — Region {n}: Core\n\n"
                + _HEADER + _rows([(res, auth)]) + "\n")

    text = region(1, "LEU", 8) + region(2, "TRP", 44) + "Primary target: Region 2\n"
    parsed = _parse(text, {"target_chain": "A", "partner_chain": "B"})
    assert parsed["region"].startswith("Region 2")
    assert [r["auth_seq_id"] for r in parsed["residues"]] == [44]


# ------------------------------------------------------------------- target_chains

def test_target_chains_is_a_single_entry_for_an_ordinary_run():
    section = "### MODEL-READY HOTSPOTS\n\n" + _HEADER + _rows([("LEU", 8), ("PRO", 9)])
    parsed = _parse(section, {"target_chain": "B", "partner_chain": "D"})
    assert parsed["target_chains"] == ["B"]


def test_target_chains_is_first_appearance_order():
    section = ("### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket 1]\n\n"
               "Chain B (co-target) patch:\n\n" + _HEADER + _rows([("LEU", 8)]) +
               "\nChain A (target) patch:\n\n" + _HEADER + _rows([("PRO", 9)]))
    parsed = _parse(section, {"target_chain": "A", "partner_chain": "B"})
    assert parsed["target_chains"] == ["B", "A"], "order follows the table, not the handoff"


# ------------------------------------------------- the SKILL.md <-> parser contract

_SKILL = _ROOT / "skills/complex-structure-analysis/SKILL.md"


def _stabilize_template() -> str:
    """The STABILIZE MODEL-READY HOTSPOTS fence, lifted out of SKILL.md."""
    text = _SKILL.read_text(encoding="utf-8")
    marker = "### MODEL-READY HOTSPOTS [STABILIZE"
    start = text.index(marker)
    return text[start:text.index("\n```", start)]


def test_the_skill_template_parses_as_two_chains():
    """The template the model is shown must be the shape the parser reads.

    Written the other way round — parser first, template later — the parser
    ships for a week against a template it does not match, and the failure is
    a silently single-chain glue table rather than an error.

    The placeholders are substituted with real values because the parser
    matches on residue names and integers; what is under test is the template's
    SHAPE, which is what the model copies.
    """
    filled = (_stabilize_template()
              .replace("<chain_a_id>", "R").replace("<chain_b_id>", "P")
              .replace("<ProteinA>", "GLP-1R").replace("<ProteinB>", "GLP-1")
              .replace("<rank>", "1").replace("<M>", "1"))
    # One real row per sub-table, in place of the placeholder row.
    rows = iter(["| PHE | 66 | 105 | CD2,CZ |", "| ALA | 30 | 69 | CB,CA |"])
    filled = "\n".join(
        next(rows) if line.startswith("| <name> |") else line
        for line in filled.splitlines())

    parsed = _parse(filled, {"target_chain": "R", "partner_chain": "P",
                             "target_chains": "R, P"})
    assert [(r["chain"], r["residue"], r["auth_seq_id"]) for r in parsed["residues"]] \
        == [("R", "PHE", 66), ("P", "ALA", 30)]
    assert parsed["target_chains"] == ["R", "P"]
    assert parsed["region"] == "Glue Pocket 1"


def test_the_skill_template_keeps_the_four_column_row_shape():
    """A leading `| Chain |` column would break every shipped report.

    `handoff._ROW` and `_resolve_unverified_label_seq_ids`'s row regex both
    read the four-column form, and `_correct_label_seq_ids` re-parses reports
    off disk on every `--start-from trim`. This is a live resume property.
    """
    template = _stabilize_template()
    assert "| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |" in template
    assert "| Chain |" not in template


def test_the_skill_template_asks_for_literal_chain_ids():
    """Positional headings are what made the shipped glue reports ambiguous."""
    text = _SKILL.read_text(encoding="utf-8")
    assert "Chain <chain_a_id> (<ProteinA>) periinterface patch" in text
    assert "Chain A (<ProteinA>) periinterface patch" not in text


def test_the_handoff_template_declares_target_chains():
    text = _SKILL.read_text(encoding="utf-8")
    assert "- target_chains:" in text
    handoff = parse_handoff(text[text.index("### PIPELINE HANDOFF"):])
    assert "target_chains" in handoff, "parse_handoff must accept the new bullet"


def test_a_declared_target_chains_beats_the_positional_reading():
    """The authoritative disambiguator, for every run written from now on."""
    section = ("### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket 1]\n\n"
               "Chain R (ProteinA) patch:\n\n" + _HEADER + _rows([("PHE", 66)]) +
               "\nChain P (ProteinB) patch:\n\n" + _HEADER + _rows([("ALA", 30)]))
    parsed = _parse(section, {"target_chain": "R", "partner_chain": "P",
                              "target_chains": "R, P"})
    assert [r["chain"] for r in parsed["residues"]] == ["R", "P"]
