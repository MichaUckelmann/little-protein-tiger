"""`binder_report` renders a BoltzGen campaign, in BoltzGen's own vocabulary.

Before this, `_load_scores` required foundry's `refold_scores.csv` and a
finished cyclic-peptide campaign got `ReportError: No refold scores found ...
run at least a calibration trial` — on `projects/pdl1_macrocycle`, which had
4,329 scored designs and 1,700 gate survivors on disk.

Four things had to be true for the report to be honest rather than merely
non-crashing, and each is pinned here:

1. The full population is read, not just the survivors. `ranked.csv` holds the
   1,700 that passed; drawing the histograms over it would make every
   distribution look like the passing tail.
2. Survivors come from BoltzGen's own `pass_filters`, never from
   `filter_records` with foundry thresholds — every foundry column is absent
   from these rows and a missing gated column FAILS its gate, so the foundry
   path would report zero survivors for a campaign that had 1,700.
3. The funnel is deserialized from the frozen `scoring/filter_stats.txt` the
   run wrote, not re-derived against today's config.
4. `complex_plddt` is not quietly relabelled as `binder_plddt`: BoltzGen
   reports whole-complex confidence and foundry the binder alone.

Plus the modality bug the first rendered report exposed: the hero read
`20_target_intel.md`'s PROPOSAL, so a 15-residue macrocycle campaign was
titled "Does a de novo mini_protein occupy ...?" — naming the modality the
run had explicitly rejected.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src import binder_report as br

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_RANKED = (
    "design_id,final_rank,design_to_target_iptm,min_design_to_target_pae,"
    "complex_plddt,pass_filters,designed_chain_sequence,quality_score\n"
    "cd274_boltzgen_4175,1,0.74113,2.25397,0.7841,True,SLPEELKAVAPDSKM,1.0\n"
    "cd274_boltzgen_3709,2,0.67339,2.8642,0.74864,True,RKVTLSNGEVLDFG,0.99\n"
)

_FROZEN_STATS = """input:     4,329
survivors: 1,700

dropped by first failing criterion:
  boltzgen_pass                 2,603
  ipae_max                         26

passing each criterion alone:
  boltzgen_pass                 1,726  ( 39.9%)
  ipae_max                      4,178  ( 96.5%)

ranked by BoltzGen's own final_rank (maximin over six per-metric ranks)
"""


@pytest.fixture
def bg_dir(tmp_path):
    d = tmp_path / "binder"
    (d / "scoring").mkdir(parents=True)
    (d / "scoring" / "ranked.csv").write_text(_RANKED, encoding="utf-8")
    (d / "scoring" / "top_k.csv").write_text(_RANKED, encoding="utf-8")
    (d / "scoring" / "filter_stats.txt").write_text(_FROZEN_STATS,
                                                    encoding="utf-8")
    return d


# ── loading ────────────────────────────────────────────────────────────────

def test_a_boltzgen_scoring_dir_is_recognised(bg_dir):
    out = br._load_boltzgen_designs(bg_dir)
    assert out is not None, "a finished BoltzGen campaign was not recognised"
    rows, label, top_rows, stats = out
    assert len(rows) == 2 and len(top_rows) == 2
    assert "BoltzGen" in label


def test_a_foundry_scoring_dir_is_not_mistaken_for_boltzgen(tmp_path):
    d = tmp_path / "binder"
    (d / "scoring").mkdir(parents=True)
    (d / "scoring" / "ranked.csv").write_text(
        "name,iptm,ipsae_min\nx,0.9,0.7\n", encoding="utf-8")
    (d / "scoring" / "top_k.csv").write_text(
        "name,iptm,ipsae_min\nx,0.9,0.7\n", encoding="utf-8")
    assert br._load_boltzgen_designs(d) is None


def test_the_frozen_funnel_is_read_not_recomputed(bg_dir):
    """4,329 input and 1,700 survivors come from the file the run wrote — the
    two rows in ranked.csv here cannot produce those numbers, which is the
    point: a re-derivation would silently report 2 and 2."""
    _, _, _, stats = br._load_boltzgen_designs(bg_dir)
    assert stats.n_input == 4329
    assert stats.n_survivors == 1700
    assert stats.dropped["boltzgen_pass"] == 2603
    assert stats.passing_alone["ipae_max"] == 4178


def test_a_missing_frozen_funnel_falls_back_to_counting_pass_filters(bg_dir):
    (bg_dir / "scoring" / "filter_stats.txt").unlink()
    _, _, _, stats = br._load_boltzgen_designs(bg_dir)
    assert stats.n_input == 2 and stats.n_survivors == 2
    assert "boltzgen_pass" in stats.passing_alone


def test_the_frozen_parser_accepts_both_wordings(tmp_path):
    """`binder_ranking` writes `input:  4,329` with separators; the legacy
    `design_ranking` wording `input designs: 4329` is what `ppi_report`
    parses. One reader serves both."""
    p = tmp_path / "s.txt"
    p.write_text("input designs: 1352\nsurvivors: 317\n", encoding="utf-8")
    stats = br._parse_frozen_filter_stats(p)
    assert (stats.n_input, stats.n_survivors) == (1352, 317)


# ── the column adapter ─────────────────────────────────────────────────────

def test_columns_are_renamed_only_where_they_correspond():
    row = {"design_id": "d1", "design_to_target_iptm": "0.741",
           "min_design_to_target_pae": "2.25", "complex_plddt": "0.784",
           "pass_filters": "True", "designed_chain_sequence": "SLPEELKAVAPDSKM"}
    out = br._boltzgen_row(row)
    assert out["iptm"] == 0.741
    assert out["iface_pae_min"] == 2.25
    assert out["binder_len"] == 15
    assert out["binder_seq"] == "SLPEELKAVAPDSKM"
    assert out["pass_filters"] is True
    # A design IS its family on this track: one refold per design.
    assert out["design_family"] == "d1"


def test_complex_plddt_is_not_relabelled_as_binder_plddt():
    """They are different measurements — whole complex against binder alone —
    and putting one under the other's label would misreport it."""
    out = br._boltzgen_row({"design_id": "d", "complex_plddt": "0.784"})
    assert out["complex_plddt"] == 0.784
    assert out.get("binder_plddt") is None


@pytest.mark.parametrize("raw,expected", [
    ("True", True), ("true", True), ("1", True),
    ("False", False), ("", False), ("no", False),
])
def test_pass_filters_is_coerced_from_the_csv_string(raw, expected):
    """`read_scores` is a raw DictReader, so every value arrives as a string
    and `bool("False")` is True."""
    assert br._boltzgen_row({"pass_filters": raw})["pass_filters"] is expected


# ── the per-track metric list ──────────────────────────────────────────────

def test_each_track_gets_its_own_design_metrics():
    bgm = br._design_metric_list(
        {"iptm": 0.741, "iface_pae_min": 2.25, "complex_plddt": 0.784,
         "pass_filters": True}, "boltzgen")
    assert [m["label"] for m in bgm] == [
        "ipTM", "iPAE min", "complex pLDDT", "BoltzGen filters"]
    assert bgm[1]["value"] == "2.25 Å"

    fdm = br._design_metric_list(
        {"iptm": 0.914, "ipsae_min": 0.751, "binder_rmsd_dock": 0.63,
         "epitope_recall": 1.0}, "foundry")
    assert [m["label"] for m in fdm] == [
        "ipTM", "ipSAE min", "RMSD dock", "Epitope recall"]
    assert fdm[3]["value"] == "100%"


def test_a_missing_metric_shows_a_dash_not_a_zero():
    m = br._design_metric_list({"iptm": 0.5}, "foundry")
    assert m[1]["value"] == "—" and m[2]["value"] == "—"


# ── the modality the run actually used ─────────────────────────────────────

def test_the_frozen_modality_beats_the_stages_proposal(tmp_path):
    """`--modality cyclic_peptide` overrides what `binder-target-intel`
    proposed, so the proposal must not name the campaign."""
    d = tmp_path / "binder"
    d.mkdir()
    assert br._run_modality(
        d, {"modality": "mini_protein"},
        {"modality": "cyclic_peptide"}) == "cyclic_peptide"


def test_the_spec_handoff_is_the_second_source(tmp_path):
    d = tmp_path / "binder"
    d.mkdir()
    (d / "23_binder_spec.md").write_text(
        "# spec\n\n### PIPELINE HANDOFF\n- modality: cyclic_peptide\n",
        encoding="utf-8")
    assert br._run_modality(d, {"modality": "mini_protein"}, None) == (
        "cyclic_peptide")


def test_the_proposal_is_used_only_when_nothing_was_frozen(tmp_path):
    d = tmp_path / "binder"
    d.mkdir()
    assert br._run_modality(d, {"modality": "mini_protein"}, {}) == (
        "mini_protein")
    assert br._run_modality(d, {}, None) == "binder"


# ── end to end on the real campaign, when it is in this checkout ───────────

_REAL = _ROOT / "projects" / "pdl1_macrocycle" / "runs" / "round-1" / "binder"


@pytest.mark.skipif(not (_REAL / "scoring" / "ranked.csv").exists(),
                    reason="the macrocycle campaign is not in this checkout")
def test_the_real_macrocycle_campaign_renders(tmp_path):
    import yaml

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    out = br.build_report(_REAL, out_path=tmp_path / "r.html", cfg=cfg)
    html = out.read_text(encoding="utf-8")
    data = json.loads(html.split("const REPORT = ", 1)[1]
                      .split(";\n", 1)[0])
    assert data["vocab"]["track"] == "boltzgen"
    assert data["vocab"]["has_geometry"] is False
    # The full population, not the 1,700 survivors.
    assert data["metrics"]["n_total"] > 4000
    assert sum(data["metrics"]["second_hist"]["counts"]) == data["metrics"]["n_total"]
    # The headline names the modality that RAN.
    assert "cyclic_peptide" in data["hero"]["title"]
    assert "mini_protein" not in data["hero"]["title"]
    # The frozen funnel, not a re-derivation.
    assert any(d["criterion"] == "boltzgen_pass" and d["n"] == 2603
               for d in data["funnel"]["dropped_by"])
