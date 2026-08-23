"""ppi_report.py — the deterministic HTML PPI-track run report generator.

Two kinds of coverage, mirroring tests/test_binder_report.py:
  - real-data (skipped if the checkout doesn't have outputs/e2e_cgas_sting):
    an end-to-end smoke test against an actual completed PPI run, the
    strongest guard against "works on synthetic fixtures, breaks on the
    first real run" regressions.
  - synthetic: genericity checks (no analysis output yet, no stage markdown
    at all) that a single real fixture could never catch, plus fast
    pure-function unit tests for the PPI-specific text extractors.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from src.ppi_report import (
    ReportError,
    _extract_choices,
    _extract_verdict,
    _parse_filter_stats,
    _text_before,
    build_report,
)

_ROOT = Path(__file__).resolve().parents[1]
_ENPP1_DIR = _ROOT / "outputs" / "e2e_cgas_sting"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------
# real-data end-to-end
# ------------------------------------------------------------------

@pytest.mark.skipif(not _ENPP1_DIR.exists(), reason="outputs/e2e_cgas_sting not present in this checkout")
class TestRealEnpp1Run:
    def test_builds_a_substantial_self_contained_page(self, config, tmp_path):
        out = build_report(_ENPP1_DIR, out_path=tmp_path / "report.html", cfg=config)
        assert out.exists()
        html = out.read_text(encoding="utf-8")
        # Mol* (~5MB) plus the native structure and 5 refold CIFs inlined.
        assert len(html) > 5_000_000
        assert "<title>" in html
        assert "molstar.Viewer.create" in html
        assert "loadStructureFromData" in html
        # no leftover template placeholders
        assert "/*@@BASE_CSS@@*/" not in html
        assert "/*@@APP_JS@@*/" not in html
        assert "@@TITLE@@" not in html

    def test_report_data_reflects_the_real_run(self, config, tmp_path):
        out = build_report(_ENPP1_DIR, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])

        assert report["hero"]["title"]
        assert "ENPP1" in report["rail"]["target"]
        assert report["site_decision"]["pdb_id"] == "5DLT"
        assert len(report["choices"]) == 4
        assert len(report["hotspots"]) == 6
        assert report["metrics"]["n_total"] == 100
        assert report["metrics"]["n_survivors"] == 100  # filter_stats.txt: all 100 survive
        assert report["funnel"] == []  # "(none)" dropped in the real run
        assert report["verdict"] == "GO"
        assert "top-K" in report["verdict_reason"] or "top-k" in report["verdict_reason"].lower() \
            or report["verdict_reason"]  # non-empty either way
        assert len(report["top_designs"]) == 5
        assert report["top_designs"][0]["name"] == "ENPP1_5DLT_cyclic_peptide_boltzgen_74"
        assert report["pathway_narrative_html"]
        assert report["literature_narrative_html"]
        assert report["structure_narrative_html"]

        start = html.index("const STRUCTS = ") + len("const STRUCTS = ")
        end = html.index(";\n", start)
        structs = json.loads(html[start:end])
        assert "native" in structs
        assert structs["native"]["target_chain"] == "A"
        design_keys = [k for k in structs if k.startswith("design_")]
        assert len(design_keys) == 5
        for k in design_keys:
            assert len(structs[k]["b64"]) > 1000

    def test_is_idempotent_and_deterministic_given_the_same_inputs(self, config, tmp_path):
        out1 = build_report(_ENPP1_DIR, out_path=tmp_path / "a.html", cfg=config)
        out2 = build_report(_ENPP1_DIR, out_path=tmp_path / "b.html", cfg=config)
        a = out1.read_text(encoding="utf-8").split("Generated 2", 1)[0]
        b = out2.read_text(encoding="utf-8").split("Generated 2", 1)[0]
        assert a == b


# ------------------------------------------------------------------
# genericity — synthetic fixtures a single real run could never exercise
# ------------------------------------------------------------------

_ENRICHED_FIELDS = [
    "design_id", "id", "designed_chain_sequence", "design_to_target_iptm",
    "min_design_to_target_pae", "complex_plddt", "lpt_hotspot_sasa_delta",
    "pass_filters", "composite_score", "composite_rank", "cif_path",
]


def _write_csv(path: Path, rows: list[dict], fields: list[str] = _ENRICHED_FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _synthetic_row(i: int) -> dict:
    return {
        "design_id": f"design_{i}", "id": f"design_{i}",
        "designed_chain_sequence": "A" * 12,
        "design_to_target_iptm": 0.8, "min_design_to_target_pae": 3.0,
        "complex_plddt": 0.85, "lpt_hotspot_sasa_delta": 20.0,
        "pass_filters": "True", "composite_score": 5.0, "composite_rank": i + 1,
        "cif_path": "",
    }


def _write_minimal_analysis(run_dir: Path, n: int = 10) -> None:
    rows = [_synthetic_row(i) for i in range(n)]
    _write_csv(run_dir / "05_metrics_enriched.csv", rows)
    _write_csv(run_dir / "05_ranking" / "top_k.csv", rows[:3])
    (run_dir / "05_ranking" / "filter_stats.txt").write_text(
        "# Filter stats (chunk 3 / stage 5)\n"
        f"input designs:     {n}\n"
        f"survivors:         {n}\n"
        "top_k selected:    3\n\n"
        "## Drop reasons\n  (none)\n\n"
        "## Composite columns used: []\n",
        encoding="utf-8",
    )


class TestGenericity:
    def test_raises_a_clear_error_when_no_analysis_output_exists(self, config, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        with pytest.raises(ReportError, match="run at least through the analysis stage"):
            build_report(run_dir, cfg=config)

    def test_builds_a_minimal_report_with_only_analysis_output(self, config, tmp_path):
        """No 00_pathway.md .. 04_execution.md, no 06_summary.md — everything
        upstream of stage 5 is absent. The report must degrade gracefully
        (empty narratives/lists/None verdict), not crash."""
        run_dir = tmp_path / "run"
        _write_minimal_analysis(run_dir, n=10)

        out = build_report(run_dir, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])

        assert report["choices"] == []
        assert report["hotspots"] == []
        assert report["pathway_narrative_html"] == ""
        assert report["literature_narrative_html"] == ""
        assert report["structure_narrative_html"] == ""
        assert report["verdict"] is None
        assert report["metrics"]["n_total"] == 10
        assert report["metrics"]["n_survivors"] == 10
        assert len(report["top_designs"]) == 3

        start = html.index("const STRUCTS = ") + len("const STRUCTS = ")
        end = html.index(";\n", start)
        structs = json.loads(html[start:end])
        assert structs == {}  # no native structure, no cif_path files on disk

    def test_dropped_designs_produce_a_nonempty_funnel(self, config, tmp_path):
        run_dir = tmp_path / "run"
        rows = [_synthetic_row(i) for i in range(10)]
        _write_csv(run_dir / "05_metrics_enriched.csv", rows)
        _write_csv(run_dir / "05_ranking" / "top_k.csv", rows[:3])
        (run_dir / "05_ranking" / "filter_stats.txt").write_text(
            "# Filter stats (chunk 3 / stage 5)\n"
            "input designs:     10\n"
            "survivors:         7\n"
            "top_k selected:    3\n\n"
            "## Drop reasons\n  iptm: 2\n  ipae: 1\n\n"
            "## Composite columns used: []\n",
            encoding="utf-8",
        )
        out = build_report(run_dir, out_path=tmp_path / "report.html", cfg=config)
        html = out.read_text(encoding="utf-8")
        start = html.index("const REPORT = ") + len("const REPORT = ")
        end = html.index(";\nconst STRUCTS")
        report = json.loads(html[start:end])
        by_criterion = {r["criterion"]: r["n"] for r in report["funnel"]}
        assert by_criterion == {"iptm": 2, "ipae": 1}
        assert report["metrics"]["n_survivors"] == 7


# ------------------------------------------------------------------
# pure-function unit tests
# ------------------------------------------------------------------

class TestTextExtractors:
    def test_text_before_stops_at_marker(self):
        text = "intro\nmore intro\n### MODEL-READY HOTSPOTS\ntable\n"
        assert _text_before(text, r"###\s+MODEL.READY HOTSPOTS") == "intro\nmore intro\n"

    def test_text_before_returns_everything_if_marker_absent(self):
        text = "just narrative"
        assert _text_before(text, r"###\s+NOWHERE") == text

    def test_extract_choices_parses_the_tier_table(self):
        handoff = {"choices_json": json.dumps([
            {"tier": "VALIDATED", "complex": "ENPP1", "pdb_ids": ["5DLT"]},
            {"tier": "BIOLOGICALLY_JUSTIFIED", "complex": "STING1", "pdb_ids": ["6NT5"]},
        ])}
        choices = _extract_choices(handoff)
        assert len(choices) == 2
        assert choices[0]["complex"] == "ENPP1"

    def test_extract_choices_handles_missing_or_malformed_json(self):
        assert _extract_choices({}) == []
        assert _extract_choices({"choices_json": "not json"}) == []
        assert _extract_choices({"choices_json": "{}"}) == []  # valid json, not a list


class TestVerdictExtraction:
    def test_extracts_go_and_a_reason(self):
        text = (
            "# Design Analyst Review\n\n"
            "## 1. Executive verdict\n\n"
            "**GO.** The top-K shows strong performance. More detail follows.\n\n"
            "## 2. Quality of the top-K\n\n..."
        )
        verdict, reason = _extract_verdict(text)
        assert verdict == "GO"
        assert "strong performance" in reason

    def test_extracts_no_go(self):
        text = "## 1. Executive verdict\n\n**NO_GO.** Nothing cleared the bar.\n\n## 2. Next\n"
        verdict, _ = _extract_verdict(text)
        assert verdict == "NO_GO"

    def test_extracts_no_go_with_a_hyphen_variant(self):
        text = "## 1. Executive verdict\n\n**NO-GO.** Nothing cleared the bar.\n"
        verdict, _ = _extract_verdict(text)
        assert verdict == "NO_GO"

    def test_returns_none_when_no_verdict_marker_present(self):
        text = "## 1. Executive verdict\n\nSome unmarked prose with no bold verdict.\n"
        verdict, _ = _extract_verdict(text)
        assert verdict is None


class TestFilterStatsParsing:
    def test_parses_input_survivors_and_drop_reasons(self, tmp_path):
        p = tmp_path / "filter_stats.txt"
        p.write_text(
            "# Filter stats (chunk 3 / stage 5)\n"
            "input designs:     100\n"
            "survivors:         93\n"
            "top_k selected:    20\n\n"
            "## Drop reasons\n"
            "  boltzgen_pass: 5\n"
            "  iptm: 2\n\n"
            "## Composite columns used: ['design_to_target_iptm']\n",
            encoding="utf-8",
        )
        stats = _parse_filter_stats(p)
        assert stats.n_input == 100
        assert stats.n_survivors == 93
        assert stats.dropped == {"boltzgen_pass": 5, "iptm": 2}

    def test_none_drop_reasons_yields_an_empty_dict(self, tmp_path):
        p = tmp_path / "filter_stats.txt"
        p.write_text(
            "input designs:     50\nsurvivors:         50\ntop_k selected:    20\n\n"
            "## Drop reasons\n  (none)\n\n## Composite columns used: []\n",
            encoding="utf-8",
        )
        stats = _parse_filter_stats(p)
        assert stats.n_input == 50
        assert stats.n_survivors == 50
        assert stats.dropped == {}


# ------------------------------------------------------------------
# src.handoff — the pipeline_runner.py extraction this report depends on
# (already covered end-to-end by TestRealEnpp1Run against real handoff
# blocks; no need to re-test the parser itself here, see
# tests/test_binder_report.py::TestHandoffParsing.)
# ------------------------------------------------------------------
