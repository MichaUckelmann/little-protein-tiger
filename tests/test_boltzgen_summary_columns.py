"""A BoltzGen campaign's final write-up must read BoltzGen's columns.

Found on `projects/pdl1_macrocycle` (2026-09-14), a completed 6.6 GPU-h
cyclic-peptide campaign: `27_scoring.md` recorded **1,700 of 4,329 designs
passing the gate** and `scoring/top_k.csv` held 20 real macrocycles (top one
`SLPEELKAVAPDSKM`, iPTM 0.741, min-PAE 2.25 A, complex_pLDDT 0.784) — and the
analyst stage reported "the campaign returned an empty candidate set", wrote
NO_GO, and the process exited 1.

Three separate defects, all in the summary stage, all silent:

1. `_slim_binder_top_k` intersected the header with foundry's column names.
   BoltzGen writes none of them, so the intersection was EMPTY and
   `csv.DictWriter` emitted a table with no columns — which the analyst read,
   reasonably, as no candidates.
2. `_write_binder_fasta` keyed on `binder_seq`; BoltzGen's column is
   `designed_chain_sequence`, so NO orderable FASTA was written for the one
   kind of campaign whose entire product is orderable peptides.
3. The prompt's first line hardcoded "Track: foundry ... The columns below are
   foundry metrics", so the analyst diagnosed a BoltzGen run in foundry's
   vocabulary — recommending more solubleMPNN sequences per backbone for a
   run that uses neither solubleMPNN nor backbones.

The verdict is the analyst's to make; being shown the designs is not.
"""

from __future__ import annotations

import pathlib

import pytest

from src.pipeline_runner import PipelineRunner

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_BOLTZGEN_TOP_K = (
    "design_id,final_rank,design_to_target_iptm,min_design_to_target_pae,"
    "complex_plddt,pass_filters,designed_chain_sequence,quality_score,"
    "cif_path\n"
    "cd274_boltzgen_4175,1,0.74113,2.25397,0.7841,True,SLPEELKAVAPDSKM,1.0,"
    "/x/cd274_boltzgen_4175\n"
    "cd274_boltzgen_3709,2,0.67339,2.8642,0.74864,True,RKVTLSNGEVLDFG,0.99,"
    "/x/cd274_boltzgen_3709\n"
)

_FOUNDRY_TOP_K = (
    "name,composite_rank,ipsae_min,iptm,binder_rmsd_dock,binder_plddt,"
    "hotspot_engagement,binder_seq,binder_len\n"
    "kras_001_b0_d0,1,0.931,0.923,0.39,0.88,1.0,SPEELKAVAPDSK,79\n"
)


def _write(tmp_path, text, name="top_k.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ── the design table ────────────────────────────────────────────────────────

def test_boltzgen_designs_reach_the_analyst(tmp_path):
    """The regression itself: BoltzGen's own metric columns must appear."""
    slim = PipelineRunner._slim_binder_top_k(
        _write(tmp_path, _BOLTZGEN_TOP_K))
    header = slim.splitlines()[0]
    assert "design_to_target_iptm" in header
    assert "min_design_to_target_pae" in header
    assert "complex_plddt" in header
    assert "pass_filters" in header
    assert "cd274_boltzgen_4175" in slim, "the designs themselves are missing"
    assert "0.74113" in slim


def test_the_foundry_table_is_unchanged(tmp_path):
    slim = PipelineRunner._slim_binder_top_k(_write(tmp_path, _FOUNDRY_TOP_K))
    header = slim.splitlines()[0]
    assert "ipsae_min" in header and "binder_rmsd_dock" in header
    assert "design_to_target_iptm" not in header


def test_sequences_stay_out_of_the_table_on_both_tracks(tmp_path):
    """Sequences are withheld from LLM context on purpose — a transcription
    slip in an ordered construct is expensive. The FASTA is the channel."""
    for text in (_BOLTZGEN_TOP_K, _FOUNDRY_TOP_K):
        slim = PipelineRunner._slim_binder_top_k(_write(tmp_path, text))
        assert "SLPEELKAVAPDSKM" not in slim
        assert "SPEELKAVAPDSK" not in slim
        assert "designed_chain_sequence" not in slim.splitlines()[0]
        assert "binder_seq" not in slim.splitlines()[0]


def test_an_unknown_vocabulary_shows_its_own_columns_not_an_empty_table(
        tmp_path, caplog):
    """A future scorer with different column names must produce a surprising
    report, not one that looks like an empty campaign."""
    slim = PipelineRunner._slim_binder_top_k(
        _write(tmp_path, "weird_id,weird_score\nfoo,0.5\n"))
    assert "weird_score" in slim.splitlines()[0]
    assert "foo" in slim


def test_an_empty_top_k_still_says_so(tmp_path):
    """The genuine empty case must remain distinguishable from the bug."""
    assert PipelineRunner._slim_binder_top_k(
        _write(tmp_path, "design_id,final_rank\n")) == (
            "(no designs survived ranking)")


# ── the orderable FASTA ─────────────────────────────────────────────────────

def test_a_boltzgen_campaign_gets_its_fasta(tmp_path):
    dest = tmp_path / "top_k.fasta"
    out = PipelineRunner._write_binder_fasta(
        _write(tmp_path, _BOLTZGEN_TOP_K), dest)
    assert out is not None, "no FASTA written for a cyclic-peptide campaign"
    text = dest.read_text(encoding="utf-8")
    assert "SLPEELKAVAPDSKM" in text and "RKVTLSNGEVLDFG" in text
    assert "cd274_boltzgen_4175" in text
    # Annotated with the metrics this track actually wrote.
    assert "iptm=0.74113" in text and "min_pae=2.25397" in text
    assert "ipsae_min=" not in text, "a foundry-only tag leaked in"


def test_a_foundry_campaign_keeps_its_fasta_annotations(tmp_path):
    dest = tmp_path / "top_k.fasta"
    assert PipelineRunner._write_binder_fasta(
        _write(tmp_path, _FOUNDRY_TOP_K), dest) is not None
    text = dest.read_text(encoding="utf-8")
    assert "ipsae_min=0.931" in text and "dock_rmsd=0.39" in text
    assert "SPEELKAVAPDSK" in text


def test_no_sequence_column_writes_no_fasta(tmp_path):
    assert PipelineRunner._write_binder_fasta(
        _write(tmp_path, "design_id,final_rank\nx,1\n"),
        tmp_path / "out.fasta") is None


# ── the track label ─────────────────────────────────────────────────────────

def test_the_prompt_names_the_track_it_is_actually_on():
    """Checked by source inspection, like the designable-size gate in
    `tests/test_audit_fixes.py`: the hardcoded foundry preamble is what made
    the analyst prescribe solubleMPNN tuning for a BoltzGen run."""
    import inspect

    src = inspect.getsource(PipelineRunner._stage_binder_summary)
    assert "_top_k_vocabulary" in src, (
        "the track line must be derived from the columns the analyst is "
        "actually shown, not hardcoded and not from runner state that can "
        "silently disagree with them")
    i = src.index("Track: BoltzGen")
    j = src.index("Track: foundry")
    assert min(i, j) > 0, "both tracks must be describable"
    assert "design_to_target_iptm" in src[i:j] or "design_to_target_iptm" in src, (
        "the BoltzGen branch should name its own columns for the analyst")


@pytest.mark.parametrize("col", ["ipsae_min", "binder_rmsd_dock",
                                 "hotspot_engagement"])
def test_the_boltzgen_branch_warns_those_columns_do_not_exist(col):
    """The analyst asked for `filter_stats.txt` by foundry gate name. Naming
    the absent columns is cheaper than letting it infer them."""
    import inspect

    src = inspect.getsource(PipelineRunner._stage_binder_summary)
    branch = src[src.index("Track: BoltzGen"):src.index("Track: foundry")]
    assert "no ipSAE" in branch or "ipSAE" in branch
    assert "do not ask for them" in branch
