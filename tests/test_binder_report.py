"""binder_report.py — the deterministic HTML campaign report generator.

Two kinds of coverage:
  - real-data (skipped if the checkout doesn't have the trial_kras project):
    an end-to-end smoke test against an actual completed calibration trial,
    the strongest guard against "works on synthetic fixtures, breaks on the
    first real campaign" regressions.
  - synthetic: genericity checks (missing target_intel/candidates/trim_map,
    no refolds at all) that a KRAS-only fixture set could never catch, plus
    fast pure-function unit tests for the markdown-section extractors.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from src import handoff
from src.binder_report import (
    ReportError,
    _extract_citation_section,
    _extract_hotspot_narrative,
    _histogram,
    _scatter_sample,
    _section_before_handoff,
    build_report,
)

_ROOT = Path(__file__).resolve().parents[1]
_KRAS_DIR = _ROOT / "projects/trial_kras/runs/round-1/binder/sites/raf1_rbd/binder"
_PDL1_DIR = _ROOT / "projects/pdl1_e2e/runs/round-1/binder"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------
# real-data end-to-end
# ------------------------------------------------------------------

@pytest.mark.skipif(not _KRAS_DIR.exists(), reason="trial_kras project not present in this checkout")
class TestRealKrasTrial:
    def test_builds_a_substantial_self_contained_page(self, config, tmp_path):
        out = build_report(_KRAS_DIR, out_path=tmp_path / "report.html", cfg=config)
        assert out.exists()
        html = out.read_text(encoding="utf-8")
        # Mol* (~5MB) plus real structure data inlined — this is not a stub page.
        assert len(html) > 5_000_000
        assert "<title>" in html
        assert "molstar.Viewer.create" in html
        assert "loadStructureFromData" in html

    def test_report_data_is_well_formed_and_reflects_the_real_campaign(self, config, tmp_path):
        out = build_report(_KRAS_DIR, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")

        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])

        assert report["hero"]["title"]
        assert "KRAS" in report["rail"]["target"]
        assert report["site_decision"]["pdb_id"] == "6VJJ"
        assert len(report["candidates"]) == 8
        # Region 1 (9) + Region 2 (6) — both offered as combinable per the
        # interface stage's own separability note.
        assert len(report["hotspots"]) == 15
        assert all(h["retained"] for h in report["hotspots"])
        assert report["calibration"]["verdict"] == "SCALE_UP"
        assert report["calibration"]["backbone_rate"]["k"] == 176
        assert report["calibration"]["backbone_rate"]["n"] == 481
        assert report["metrics"]["n_total"] == 1924
        assert len(report["top_designs"]) == 5
        # Every one of the top 5 must actually engage every hotspot handed
        # to RFD3 — this is the report's whole "did it bind where asked"
        # claim; if this regresses the report is silently lying.
        for d in report["top_designs"]:
            assert d["hotspot_engagement"] == 1.0
            assert d["iptm"] >= 0.7
        assert report["site_narrative_html"]  # the model's own "why this site" prose
        assert "RAF1" in report["site_narrative_html"]
        assert report["hotspot_narrative_html"]
        assert report["citation_html"]  # this run's citation was NOT IN CORPUS

        start = html.index("const STRUCTS = ") + len("const STRUCTS = ")
        end = html.index(";\n", start)
        structs = json.loads(html[start:end])
        assert "native" in structs
        assert structs["native"]["target_chain"] == "A"
        design_keys = [k for k in structs if k.startswith("design_")]
        assert len(design_keys) == 5
        for k in design_keys:
            assert structs[k]["target_chain"] == "B"
            assert structs[k]["binder_chain"] == "A"
            assert len(structs[k]["b64"]) > 1000

    def test_funnel_matches_the_hand_verified_kras_gate_attrition(self, config, tmp_path):
        """Pinned against numbers independently hand-computed from the raw
        CSV while building the first illustrated report for this campaign —
        catches any drift in the reused src.binder_ranking gate logic."""
        out = build_report(_KRAS_DIR, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])

        by_criterion = {r["criterion"]: r["pct"] for r in report["funnel"]["passing_alone"]}
        assert by_criterion["binder_rmsd_dock <= 5"] == pytest.approx(69.4, abs=0.1)
        assert by_criterion["no clash"] == pytest.approx(47.3, abs=0.1)
        assert by_criterion["hotspot_engagement >= 1"] == pytest.approx(59.3, abs=0.1)

    def test_is_idempotent_and_deterministic_given_the_same_inputs(self, config, tmp_path):
        out1 = build_report(_KRAS_DIR, out_path=tmp_path / "a.html", cfg=config)
        out2 = build_report(_KRAS_DIR, out_path=tmp_path / "b.html", cfg=config)
        # The only intentional non-determinism is the report's own generated
        # timestamp; excise it before comparing.
        a = out1.read_text(encoding="utf-8").split("Generated 2", 1)[0]
        b = out2.read_text(encoding="utf-8").split("Generated 2", 1)[0]
        assert a == b


@pytest.mark.skipif(not _PDL1_DIR.exists(), reason="pdl1_e2e project not present in this checkout")
class TestRealPdl1Campaign:
    """
    Regression for a real bug caught by visual inspection of this exact
    campaign's report: the structure-explorer highlighted the correct
    residues on the native structure but the wrong ones on every design —
    RFD3 renumbers the target chain in its own output (a uniform -17 shift
    on this campaign's trim), and the viewer was highlighting the NATIVE
    auth_seq_id list on the design structures too, which silently selects
    different, unrelated residues once the numbering has shifted.
    """

    def test_design_hotspot_numbering_differs_from_native_and_is_present(self, config, tmp_path):
        out = build_report(_PDL1_DIR, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])

        native_ids = [h["auth_seq_id"] for h in report["hotspots"]]
        design_ids = report["design_hotspot_auth_seq_ids"]

        assert native_ids  # sanity: this campaign really has hotspots
        assert design_ids is not None, (
            "no design sidecar found — the fix has nothing to remap against")
        assert len(design_ids) == len(native_ids), (
            "the remap must carry every native hotspot through, not drop any")
        assert design_ids != native_ids, (
            "this campaign's trim genuinely shifts numbering — identical "
            "lists here would mean the remap silently no-op'd")

    def test_viewer_uses_design_numbering_not_native_numbering(self, config, tmp_path):
        """The actual bug was in app.js's hotspotResidueIds(), not just the
        Python data — assert the shared shell wires the key through so the
        design branch is reachable, not just that the data exists."""
        out = build_report(_PDL1_DIR, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        assert "hotspotResidueIds(key)" in html  # base.js passes the key through
        assert "REPORT.design_hotspot_auth_seq_ids" in html  # app.js branches on it


# ------------------------------------------------------------------
# genericity — synthetic fixtures a KRAS-only test set would never exercise
# ------------------------------------------------------------------

_SCORE_FIELDS = [
    "name", "design_family", "iptm", "ipsae_min", "binder_rmsd_dock",
    "binder_rmsd_fold", "binder_plddt", "hotspot_engagement", "epitope_recall",
    "has_clash", "clash_severe", "iface_pae", "binder_len", "binder_seq",
    "refold_cif", "error",
]


def _write_scores(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_SCORE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _synthetic_row(i: int, excellent: bool) -> dict:
    return {
        "name": f"design_{i}", "design_family": f"design_{i // 2}",
        "iptm": 0.85 if excellent else 0.2,
        "ipsae_min": 0.8 if excellent else 0.1,
        "binder_rmsd_dock": 1.0 if excellent else 12.0,
        "binder_rmsd_fold": 0.5, "binder_plddt": 0.9,
        "hotspot_engagement": 1.0 if excellent else 0.0,
        "epitope_recall": 0.95, "has_clash": "False", "clash_severe": "0",
        "iface_pae": 5.0, "binder_len": 80, "binder_seq": "A" * 80,
        "refold_cif": "", "error": "",
    }


class TestGenericity:
    def test_raises_a_clear_error_when_no_refolds_exist_anywhere(self, config, tmp_path):
        binder_dir = tmp_path / "binder"
        binder_dir.mkdir()
        with pytest.raises(ReportError, match="run at least a calibration trial"):
            build_report(binder_dir, cfg=config)

    def test_builds_a_minimal_report_with_only_a_calibration_csv(self, config, tmp_path):
        """No target_intel.md, no candidates.json, no trim_map.json, no
        21_interface.md — everything the real pipeline would have written
        by the time a report is worth generating is absent here. The report
        must degrade gracefully (empty lists/None), not crash."""
        binder_dir = tmp_path / "binder"
        rows = [_synthetic_row(i, excellent=(i < 4)) for i in range(20)]
        _write_scores(binder_dir / "calibration" / "refold_scores.csv", rows)

        out = build_report(binder_dir, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])

        assert report["candidates"] is None
        assert report["hotspots"] == []
        assert report["site_narrative_html"] == ""
        assert report["calibration"] is None  # no calibration.json written
        assert report["metrics"]["n_total"] == 20
        assert "calibration trial" in report["source_label"]

        start = html.index("const STRUCTS = ") + len("const STRUCTS = ")
        end = html.index(";\n", start)
        structs = json.loads(html[start:end])
        assert structs == {}  # no native structure, no refold_cif files on disk

    def test_prefers_production_scoring_output_over_calibration_when_both_exist(self, config, tmp_path):
        binder_dir = tmp_path / "binder"
        cal_rows = [_synthetic_row(i, excellent=False) for i in range(5)]
        _write_scores(binder_dir / "calibration" / "refold_scores.csv", cal_rows)
        prod_rows = [_synthetic_row(i, excellent=True) for i in range(50)]
        _write_scores(binder_dir / "scoring" / "refold_scores.csv", prod_rows)
        _write_scores(binder_dir / "scoring" / "top_k.csv", prod_rows[:3])

        out = build_report(binder_dir, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])
        assert report["source_label"] == "production campaign"
        assert report["metrics"]["n_total"] == 50  # production rows, not the 5 calibration ones


# ------------------------------------------------------------------
# pure-function unit tests
# ------------------------------------------------------------------

class TestMarkdownSectionExtractors:
    def test_section_before_handoff_stops_at_the_handoff_marker(self):
        text = "Some narrative.\n\nMore narrative.\n\n### PIPELINE HANDOFF\n- key: value\n"
        assert _section_before_handoff(text) == "Some narrative.\n\nMore narrative.\n\n"

    def test_section_before_handoff_returns_everything_if_no_handoff(self):
        text = "Just narrative, no handoff block."
        assert _section_before_handoff(text) == text

    def test_extract_hotspot_narrative_stops_before_model_ready_hotspots(self):
        text = (
            "### HOTSPOT REGIONS (top 2)\n"
            "some prose\n"
            "### DESIGN RECOMMENDATIONS\n"
            "more prose\n"
            "### MODEL-READY HOTSPOTS\n"
            "| A | 1 | 1 | CA |\n"
        )
        section = _extract_hotspot_narrative(text)
        assert "some prose" in section
        assert "more prose" in section
        assert "MODEL-READY HOTSPOTS" not in section

    def test_extract_hotspot_narrative_handles_glue_pockets_variant(self):
        text = "### GLUE POCKETS (top results)\nprose\n### MODEL-READY HOTSPOTS\n"
        assert "prose" in _extract_hotspot_narrative(text)

    def test_extract_hotspot_narrative_empty_when_absent(self):
        assert _extract_hotspot_narrative("no relevant headings here") == ""

    def test_citation_section_suppressed_when_everything_verified(self):
        text = "## CITATION VERIFICATION\n- Citations checked: 2\n- Verified in corpus: 2\n- NOT IN CORPUS (0):\n"
        assert _extract_citation_section(text) is None

    def test_citation_section_surfaced_when_something_failed(self):
        text = "## CITATION VERIFICATION\n- Citations checked: 1\n- NOT IN CORPUS (1): 10.1038/example\n"
        html = _extract_citation_section(text)
        assert html is not None
        assert "10.1038/example" in html
        # heading itself is not duplicated — the callout supplies its own tag
        assert "CITATION VERIFICATION" not in html.upper()


class TestChartData:
    def test_histogram_bins_values_into_the_right_edges(self):
        h = _histogram([0.0, 0.1, 0.5, 0.5, 0.99, 1.0], 0.0, 1.0, 10)
        assert sum(h["counts"]) == 6
        assert h["counts"][0] >= 1  # the 0.0 and 0.1 values
        assert h["counts"][-1] >= 1  # the 1.0 value clamped into the last bin

    def test_scatter_sample_flags_only_survivors_meeting_the_excellence_bar(self):
        rows = [_synthetic_row(i, excellent=(i < 3)) for i in range(10)]
        survivor_ids = {id(r) for r in rows if float(r["binder_rmsd_dock"]) <= 5}
        pts = _scatter_sample(rows, survivor_ids, "iptm", 0.7, n=10, seed=1)
        excellent_count = sum(p[2] for p in pts)
        assert excellent_count == 3


# ------------------------------------------------------------------
# src.handoff — the pipeline_runner.py extraction this report depends on
# ------------------------------------------------------------------

class TestHandoffParsing:
    def test_parse_handoff_canonical_dash_format(self):
        text = "### PIPELINE HANDOFF\n- pdb_id: 6VJJ\n- target_chain: A\n"
        fields = handoff.parse_handoff(text)
        assert fields == {"pdb_id": "6VJJ", "target_chain": "A"}

    def test_parse_handoff_bare_format_and_code_fences(self):
        text = "### PIPELINE HANDOFF\n```\npdb_id: 6VJJ\n```\ntarget_chain: A\n"
        fields = handoff.parse_handoff(text)
        assert fields == {"pdb_id": "6VJJ", "target_chain": "A"}

    def test_parse_handoff_missing_block_returns_empty(self):
        assert handoff.parse_handoff("no handoff here") == {}

    def test_parse_hotspot_residues_dedupes_across_sections(self):
        text = (
            "### MODEL-READY HOTSPOTS\n"
            "| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |\n"
            "|---|---|---|---|\n"
            "| ILE | 21 | 22 | CD1,CG2 |\n"
            "| ILE | 21 | 22 | CD1,CG2 |\n"
            "| ASP | 38 | 39 | CG,OD1 |\n"
        )
        raw = handoff.parse_hotspot_residues(text, {"target_chain": "A", "partner_chain": "B"})
        data = json.loads(raw)
        assert data["target_chain"] == "A"
        assert len(data["residues"]) == 2

    def test_parse_hotspot_residues_falls_back_to_auth_seq_id_for_bad_label(self):
        text = (
            "### MODEL-READY HOTSPOTS\n"
            "| ILE | 21 | **UNVERIFIED** | CD1,CG2 |\n"
        )
        raw = handoff.parse_hotspot_residues(text, {})
        residue = json.loads(raw)["residues"][0]
        assert residue["label_seq_id"] == residue["auth_seq_id"] == 21

    def test_parse_hotspot_residues_none_when_absent(self):
        assert handoff.parse_hotspot_residues("nothing here", {}) is None


class TestCleanAtomList:
    """
    src.handoff._clean_atom_list strips explanatory prose the LLM sometimes
    inlines into the "RFD3 sidechain atoms" table cell — RFD3's select_hotspots
    takes the cell verbatim as an atom name, so unstripped prose reaches
    validate_spec as a malformed atom and fails a real design (observed on a
    PD-L1 GLY119/ALA121 hotspot).
    """

    def test_strips_trailing_parenthetical_prose(self):
        raw = "CA (no sidechain — backbone contact only)"
        assert handoff._clean_atom_list(raw) == "CA"

    def test_clean_multi_atom_list_passes_through_unchanged(self):
        assert handoff._clean_atom_list("CA,CB,CG") == "CA,CB,CG"

    def test_empty_string_returns_empty_string(self):
        assert handoff._clean_atom_list("") == ""

    def test_single_atom_name_passes_through(self):
        assert handoff._clean_atom_list("CA") == "CA"

    def test_prose_with_multiple_leading_atoms_keeps_only_the_atoms(self):
        raw = "CA,CB (backbone contact only, no sidechain reach)"
        assert handoff._clean_atom_list(raw) == "CA,CB"

    def test_no_recognisable_atom_tokens_falls_back_to_stripped_raw(self):
        # Nothing before the first "(" looks like an atom name at all — the
        # function has no salvageable atom list, so it returns the raw text
        # (stripped) rather than silently emitting an empty cell.
        raw = "  no atoms specified  "
        assert handoff._clean_atom_list(raw) == "no atoms specified"

    def test_end_to_end_through_parse_hotspot_residues(self):
        """The table parser must apply the same cleanup, not just the unit fn."""
        text = (
            "### MODEL-READY HOTSPOTS\n"
            "| GLY | 119 | 120 | CA (no sidechain — backbone contact only) |\n"
        )
        raw = handoff.parse_hotspot_residues(text, {"target_chain": "A", "partner_chain": "B"})
        residue = json.loads(raw)["residues"][0]
        assert residue["rfd3_atoms"] == "CA"
