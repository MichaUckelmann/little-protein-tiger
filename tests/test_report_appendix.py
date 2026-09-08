"""The full-stage-report appendix both HTML reports carry.

`report_common.stage_documents` renders every stage's own markdown WHOLE, so
one report file is the complete run record rather than a set of quoted
excerpts. Two things it has to get right are easy to regress silently:

  - a stage report is written to be read in a TERMINAL first, so its
    handoff blocks are bullets and its cost ladders are two-space-indented
    column alignment. Rendered as plain markdown both collapse — the ladder
    into an unreadable run-on sentence of numbers.
  - a PPI-bridged campaign has two stage files with identical bytes
    (`_bridge_ppi_to_foundry` copies 02_structure.md as 21_interface.md
    rather than paying for a second call to the same skill), which read as
    the interface stage having repeated the structure stage.

Plus real-data end-to-end checks (skipped when the projects aren't in the
checkout) that both tracks' reports actually carry the appendix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.report_common import (
    _handoff_block_to_table,
    _preserve_aligned_blocks,
    display_root,
    stage_documents,
    stage_markdown_html,
)

_ROOT = Path(__file__).resolve().parents[1]
_PAIN_BINDER = _ROOT / "projects/pain_receptors_v3/runs/round-1/binder"
_CGAS_RUN = _ROOT / "outputs/e2e_cgas_sting"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


def _report_data(html: str) -> dict:
    start = html.index("const REPORT = ") + len("const REPORT = ")
    return json.loads(html[start:html.index(";\nconst STRUCTS")])


# ------------------------------------------------------------------
# handoff blocks
# ------------------------------------------------------------------

class TestHandoffTable:
    def test_a_field_list_becomes_a_table(self):
        out = _handoff_block_to_table(
            "# Trim\n\nprose\n\n### PIPELINE HANDOFF\n"
            "- contig: 70-86,/0,D27-110\n- n_segments: 1\n")
        assert "| field | value |" in out
        assert "| `contig` | 70-86,/0,D27-110 |" in out
        assert "| `n_segments` | 1 |" in out

    def test_a_pipe_in_a_value_cannot_break_the_table(self):
        out = _handoff_block_to_table(
            "### PIPELINE HANDOFF\n- target_complex: CALCRL | RAMP1\n")
        assert r"CALCRL \| RAMP1" in out
        html = stage_markdown_html("### PIPELINE HANDOFF\n- target_complex: A | B\n")
        # One two-column row, not three cells.
        assert html.count("<td>") == 2

    def test_a_block_carrying_prose_is_left_alone(self):
        """Half-converting a block a model wrote prose into would lose the
        prose; leaving it as bullets loses nothing."""
        text = ("### PIPELINE HANDOFF\n- contig: A1-9\n"
                "This one is not a field at all.\n")
        assert _handoff_block_to_table(text) == text

    def test_conversion_stops_at_the_next_heading(self):
        out = _handoff_block_to_table(
            "### PIPELINE HANDOFF\n- contig: A1-9\n\n"
            "## CITATION VERIFICATION\n- Citations checked: 5\n")
        assert "| `contig` | A1-9 |" in out
        assert "- Citations checked: 5" in out  # untouched, different section

    def test_the_pipelines_own_parser_still_reads_the_original(self):
        """This is presentation only. `handoff.parse_handoff` runs over the
        file on disk, never over the rewritten text."""
        from src import handoff
        text = "### PIPELINE HANDOFF\n- contig: A1-9\n- n_segments: 2\n"
        assert handoff.parse_handoff(text) == {"contig": "A1-9", "n_segments": "2"}


# ------------------------------------------------------------------
# column-aligned blocks
# ------------------------------------------------------------------

class TestAlignedBlocks:
    _LADDER = (
        "### Required scale\n\n"
        "  point estimate             ~ 293 designs / 1,065 refolds  ->  3 GPU-h\n"
        "  Wilson 95% lower bound     ~ 351 designs / 1,275 refolds  ->  3 GPU-h\n"
    )

    def test_an_aligned_ladder_is_fenced(self):
        out = _preserve_aligned_blocks(self._LADDER)
        assert "```" in out
        assert "<pre>" in stage_markdown_html(self._LADDER)

    def test_alignment_survives_to_html(self):
        html = stage_markdown_html(self._LADDER)
        assert "point estimate             ~ 293" in html

    def test_a_blank_line_inside_one_ladder_keeps_it_one_block(self):
        text = ("### Yield\n\n"
                "  iptm > 0.7                       420  (19.92%)\n"
                "  iptm > 0.85                      150  ( 7.12%)\n"
                "\n"
                "  best iptm, any refold:  0.9203\n")
        assert _preserve_aligned_blocks(text).count("```") == 2

    def test_an_indented_list_is_not_fenced(self):
        text = "Heading\n\n  - one item\n  - another item\n"
        assert _preserve_aligned_blocks(text) == text

    def test_a_single_indented_line_is_not_fenced(self):
        text = "para\n\n  just  one  line\n"
        assert _preserve_aligned_blocks(text) == text

    def test_a_lazy_continuation_of_a_paragraph_is_not_fenced(self):
        """Two indented lines directly under a prose line are that
        paragraph's continuation, not a ladder."""
        text = "Some prose sentence.\n  continued  here\n  and  here\n"
        assert _preserve_aligned_blocks(text) == text

    def test_content_already_fenced_is_untouched(self):
        text = "### Gate\n\n```\n  no clash    619\n  iptm        437\n```\n"
        assert _preserve_aligned_blocks(text) == text

    @pytest.mark.skipif(not _PAIN_BINDER.exists(), reason="pain_receptors_v3 not in this checkout")
    def test_only_the_ladders_are_touched_in_a_real_calibration_report(self):
        text = (_PAIN_BINDER / "25_calibration.md").read_text(encoding="utf-8")
        out = _preserve_aligned_blocks(text)
        # Required scale, yield-at-softer-bars, and where-to-run-it — no more.
        assert (out.count("```") - text.count("```")) // 2 == 3
        # Every original line survives; only fences were added.
        strip = lambda s: [ln for ln in s.splitlines() if ln.strip() != "```"]
        assert strip(out) == strip(text)


# ------------------------------------------------------------------
# stage_documents
# ------------------------------------------------------------------

class TestStageDocuments:
    def test_missing_and_empty_files_are_skipped(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\n\nreal content\n", encoding="utf-8")
        (tmp_path / "b.md").write_text("   \n", encoding="utf-8")
        docs = stage_documents([
            ("00", "A", tmp_path / "a.md"),
            ("01", "B (empty)", tmp_path / "b.md"),
            ("02", "C (absent)", tmp_path / "c.md"),
        ], rel_to=tmp_path)
        assert [d["num"] for d in docs] == ["00"]
        assert docs[0]["path"] == "a.md"
        assert "real content" in docs[0]["html"]

    def test_identical_files_are_shown_once_and_the_other_path_named(self, tmp_path):
        body = "# Structure\n\nsame bytes both places\n"
        (tmp_path / "02_structure.md").write_text(body, encoding="utf-8")
        (tmp_path / "21_interface.md").write_text(body, encoding="utf-8")
        docs = stage_documents([
            ("02", "Structure", tmp_path / "02_structure.md"),
            ("21", "Interface", tmp_path / "21_interface.md"),
        ], rel_to=tmp_path)
        assert len(docs) == 1
        assert docs[0]["num"] == "02"
        assert docs[0]["also"] == ["21_interface.md"]

    def test_files_that_merely_start_the_same_are_both_kept(self, tmp_path):
        (tmp_path / "a.md").write_text("# X\n\nshared opening\n", encoding="utf-8")
        (tmp_path / "b.md").write_text("# X\n\nshared opening\nplus more\n", encoding="utf-8")
        docs = stage_documents([("00", "A", tmp_path / "a.md"),
                                 ("01", "B", tmp_path / "b.md")], rel_to=tmp_path)
        assert len(docs) == 2

    def test_a_path_outside_rel_to_falls_back_to_the_full_path(self, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("# X\n\nbody\n", encoding="utf-8")
        inner = tmp_path / "inner"
        inner.mkdir()
        docs = stage_documents([("00", "A", outside)], rel_to=inner)
        assert docs[0]["path"] == str(outside)

    def test_ids_are_unique_and_anchor_safe(self, tmp_path):
        for name in ("00_pathway.md", "25_calibration.md"):
            (tmp_path / name).write_text(f"# {name}\n\nbody\n", encoding="utf-8")
        docs = stage_documents([("00", "P", tmp_path / "00_pathway.md"),
                                 ("25", "C", tmp_path / "25_calibration.md")])
        ids = [d["id"] for d in docs]
        assert ids == ["doc-00-pathway", "doc-25-calibration"]
        assert len(set(ids)) == len(ids)

    def test_script_in_stage_prose_cannot_reach_the_appendix(self, tmp_path):
        """Same guarantee `markdown_html` gives the narrative sections — the
        appendix must not be a second, unsanitised path to the same file."""
        (tmp_path / "a.md").write_text(
            "# X\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n",
            encoding="utf-8")
        html = stage_documents([("00", "A", tmp_path / "a.md")])[0]["html"]
        assert "<script" not in html
        assert "onerror" not in html


class TestDisplayRoot:
    def test_finds_the_project_root_from_either_depth(self, tmp_path):
        proj = tmp_path / "proj"
        deep = proj / "runs/round-1/binder/sites/s1/binder"
        deep.mkdir(parents=True)
        (proj / "manifest.json").write_text("{}", encoding="utf-8")
        assert display_root(deep) == proj
        assert display_root(proj / "runs/round-1/binder") == proj

    def test_falls_back_to_the_parent_when_there_is_no_marker(self, tmp_path):
        d = tmp_path / "outputs/legacy_run"
        d.mkdir(parents=True)
        assert display_root(d) == d.parent


# ------------------------------------------------------------------
# real-data end-to-end
# ------------------------------------------------------------------

@pytest.mark.skipif(not _PAIN_BINDER.exists(), reason="pain_receptors_v3 not in this checkout")
def test_binder_report_appendix_carries_both_tracks_stage_files(config, tmp_path):
    """A PPI-bridged campaign's record spans two directories: the PPI stages
    that chose the target and the binder stages that designed against it."""
    from src.binder_report import build_report
    out = build_report(_PAIN_BINDER, out_path=tmp_path / "report.html", cfg=config)
    report = _report_data(out.read_text(encoding="utf-8"))
    docs = report["appendix"]
    nums = [d["num"] for d in docs]
    assert nums[:3] == ["00", "01", "02"]      # PPI stages, one level up
    assert "25" in nums and "28" in nums       # binder stages
    assert "21" not in nums                    # identical to 02, folded into it
    struct = next(d for d in docs if d["num"] == "02")
    assert struct["also"] == ["runs/round-1/binder/21_interface.md"]
    assert all(d["html"] for d in docs)
    assert {"href": "#appendix", "label": "05 · Full stage reports"} in report["rail"]["nav"]


@pytest.mark.skipif(not _CGAS_RUN.exists(), reason="e2e_cgas_sting run not in this checkout")
def test_ppi_report_appendix_carries_every_stage(config, tmp_path):
    from src.ppi_report import build_report
    out = build_report(_CGAS_RUN, out_path=tmp_path / "report.html", cfg=config)
    report = _report_data(out.read_text(encoding="utf-8"))
    docs = report["appendix"]
    assert [d["num"] for d in docs] == ["00", "01", "02", "03", "04", "05", "06"]
    assert docs[0]["path"] == "00_pathway.md"  # rel_to the run dir
    assert any(d["href"] == "#appendix" for d in report["rail"]["nav"]
               if isinstance(d, dict) and "href" in d)
