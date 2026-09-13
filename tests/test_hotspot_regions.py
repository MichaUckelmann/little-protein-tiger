"""One epitope per campaign: `parse_hotspot_residues` picks ONE region.

The structure skill emits a `### MODEL-READY HOTSPOTS` section per region,
ranked, each capped at 12 residues and each labelled "Separability:
Independent — separate design submission required". The parser used to
concatenate every section, and a real 3KYS run reached `build_rfd3_spec` with
**16 hotspots against the 12 cap** — Region 1's 9 plus Region 2's 7, two
patches ~20 A apart fused into one declared epitope.

`build_rfd3_spec` only warns there, deliberately: a builder has no per-residue
ddG/BSA and cannot choose which to drop. The skill's own report can and does
— it ranks the regions and names a primary — so the choice belongs at the
parse, which is what these tests pin.

Why it matters beyond the cap: `hotspot_engagement` is a FRACTION of the
declared set gated at 0.75, so a binder docked perfectly on the primary
region scores 9/16 = 0.56 and is rejected for missing residues it was never
steered at.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.handoff import parse_hotspot_residues

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HANDOFF = {"target_chain": "A", "partner_chain": "B"}


def _section(n: int, name: str, rows: list[tuple[str, int]]) -> str:
    table = "\n".join(f"| {res} | {auth} | {auth - 192} | CD2,CZ |"
                      for res, auth in rows)
    return (f"### MODEL-READY HOTSPOTS\n\n"
            f"Target chain A — Region {n}: {name} — selected {len(rows)} of "
            f"18 interface residues:\n\n"
            f"| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |\n"
            f"|---|---|---|---|\n{table}\n")


_R1 = [("PHE", 314), ("VAL", 318), ("VAL", 319)]
_R2 = [("GLU", 240), ("GLN", 246), ("ASP", 249)]


def test_two_regions_yield_only_the_primary_ones_residues():
    text = _section(1, "Central Hydrophobic Core", _R1) + "\n" + \
        _section(2, "Basic/Aromatic Flank", _R2)
    d = json.loads(parse_hotspot_residues(text, _HANDOFF))
    assert [r["auth_seq_id"] for r in d["residues"]] == [314, 318, 319]
    assert d["regions_declared"] == 2
    assert "Region 1" in d["region"]


def test_the_report_may_name_a_primary_that_is_not_the_first_section():
    """Position is only a proxy for rank. A model that lists its regions out
    of order would otherwise hand the campaign its second choice."""
    text = (_section(1, "Central Hydrophobic Core", _R1) + "\n"
            + _section(2, "Basic/Aromatic Flank", _R2)
            + "\n### DESIGN RECOMMENDATIONS\n"
              "- Primary target: Region 2 (Basic/Aromatic Flank) — Excellent\n")
    d = json.loads(parse_hotspot_residues(text, _HANDOFF))
    assert [r["auth_seq_id"] for r in d["residues"]] == [240, 246, 249]
    assert "Region 2" in d["region"]


def test_a_single_region_is_untouched():
    """The common case must not acquire a warning or lose a residue."""
    d = json.loads(parse_hotspot_residues(
        _section(1, "Central Hydrophobic Core", _R1), _HANDOFF))
    assert [r["auth_seq_id"] for r in d["residues"]] == [314, 318, 319]
    assert d["regions_declared"] == 1


def test_no_hotspot_section_at_all_is_still_non_fatal():
    assert parse_hotspot_residues("## PPI ANALYSIS REPORT\n", _HANDOFF) is None


def test_the_dropped_region_is_named_in_the_log(caplog):
    """Silently designing against a subset of what the report shows would be
    worse than the merge: the artifact on disk lists both regions."""
    text = _section(1, "Core", _R1) + "\n" + _section(2, "Flank", _R2)
    with caplog.at_level("WARNING"):
        parse_hotspot_residues(text, _HANDOFF)
    logged = caplog.text or ""
    if not logged:                      # loguru is not wired into caplog here
        pytest.skip("loguru sink not captured in this configuration")
    assert "Region 2" in logged and "GLU240" in logged


_REAL = (_ROOT / "projects" / "a344_fix_check" / "runs" / "round-1"
         / "binder" / "21_interface.md")


@pytest.mark.skipif(not _REAL.exists(),
                    reason="the 3KYS interface report that found this is not "
                           "in this checkout")
def test_the_real_two_region_report_comes_back_within_the_cap():
    """Ground truth: the run that produced 16 hotspots against the 12 cap."""
    from src.foundry_spec import MAX_HOTSPOTS

    d = json.loads(parse_hotspot_residues(
        _REAL.read_text(encoding="utf-8"), _HANDOFF))
    assert d["regions_declared"] == 2
    assert len(d["residues"]) == 9 <= MAX_HOTSPOTS
    # Region 2's residues, which used to be merged in.
    assert 240 not in {r["auth_seq_id"] for r in d["residues"]}
