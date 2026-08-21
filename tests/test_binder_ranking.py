"""Filter attribution, composite scoring, and the three diversity levels."""

from __future__ import annotations

import pytest

from src.binder_ranking import (
    DEFAULT_THRESHOLDS, cap_per_backbone, composite_score, filter_records,
    rank_designs,
)


def rec(name="d1", family=None, **kw):
    """A record that passes every default gate; override to make it fail one."""
    base = dict(
        name=name, design_family=family or name.split("_b")[0],
        error="", binder_rmsd_dock=1.0, epitope_recall=0.9, hotspot_engagement=1.0,
        binder_rmsd_fold=1.0, binder_plddt=0.85, ipsae_min=0.6, iptm=0.8,
        iface_pae=8.0, has_clash=False, clash_severe=0, binder_seq="ACDEFGHIKL",
    )
    base.update(kw)
    return base


def test_a_clean_record_survives():
    survivors, stats = filter_records([rec()])
    assert len(survivors) == 1
    assert stats.n_survivors == 1


def test_drop_is_attributed_to_the_FIRST_failing_criterion():
    """
    Dock RMSD is checked first because it is the gate that matters most; a
    design failing several must be counted against it, not against whichever
    check happened to run last.
    """
    _, stats = filter_records([rec(binder_rmsd_dock=99.0, iptm=0.1, binder_plddt=0.1)])
    assert list(stats.dropped) == ["binder_rmsd_dock <= 5"]


def test_passing_alone_counts_every_criterion_independently():
    _, stats = filter_records([rec(binder_rmsd_dock=99.0)])
    assert stats.passing_alone["binder_rmsd_dock <= 5"] == 0
    assert stats.passing_alone["iptm >= 0.5"] == 1


def test_a_missing_metric_fails_its_gate_rather_than_passing():
    """A blank column is not evidence of quality."""
    survivors, stats = filter_records([rec(binder_rmsd_dock="")])
    assert not survivors
    assert "binder_rmsd_dock <= 5" in stats.dropped


def test_scoring_errors_are_dropped_first():
    _, stats = filter_records([rec(error="ValueError: boom")])
    assert stats.dropped == {"scoring error": 1}


def test_ipsae_gate_is_off_by_default_and_can_be_enabled():
    """Uncalibrated on this pipeline — enabling it blind would empty the set."""
    assert DEFAULT_THRESHOLDS["ipsae_min_min"] is None
    low = rec(ipsae_min=0.1)
    assert filter_records([low])[0]
    assert not filter_records([low], {"ipsae_min_min": 0.3})[0]


def test_severe_clash_fails_even_when_rf3_reports_no_clash():
    """RF3's has_clash never fired on the reference set despite sub-2.2 A contacts."""
    assert not filter_records([rec(has_clash=False, clash_severe=3)])[0]


def test_zero_variance_column_contributes_zero_not_a_nan():
    recs = [rec(f"a{i}", iptm=0.8, ipsae_min=0.1 * i) for i in range(1, 4)]
    composite_score(recs, {"iptm": 1.0, "ipsae_min": 1.0})
    assert all(r["iptm_z"] == 0.0 for r in recs)
    assert all(isinstance(r["composite_score"], float) for r in recs)


def test_neg_prefix_inverts_the_sense_of_a_lower_is_better_column():
    good = rec("good", binder_rmsd_dock=1.0)
    bad = rec("bad", binder_rmsd_dock=4.0)
    composite_score([good, bad], {"neg_binder_rmsd_dock": 1.0})
    assert good["composite_score"] > bad["composite_score"]


def test_a_weight_on_an_absent_column_is_dropped_not_counted_as_zero():
    recs = [rec("a", iptm=0.8), rec("b", iptm=0.9)]
    used = composite_score(recs, {"iptm": 1.0, "nonexistent_column": 5.0})
    assert used == ["iptm"]


def test_backbone_cap_keeps_the_best_of_each_family():
    """
    The n_seq sequences on one RFD3 backbone share a pose; without this cap a
    top-20 is drawn from a handful of backbones.
    """
    sibs = [rec(f"bb1_b0_d{i}", family="bb1", ipsae_min=0.5 + 0.1 * i) for i in range(3)]
    other = rec("bb2_b0_d0", family="bb2", ipsae_min=0.55)
    recs = sibs + [other]
    composite_score(recs, {"ipsae_min": 1.0})
    kept = cap_per_backbone(recs, max_per_backbone=1)
    assert len(kept) == 2
    assert {r["design_family"] for r in kept} == {"bb1", "bb2"}
    assert next(r for r in kept if r["design_family"] == "bb1")["name"] == "bb1_b0_d2"


def test_mmr_drops_near_identical_sequences():
    a = rec("a", family="fa", binder_seq="ACDEFGHIKLMNPQRSTVWY")
    b = rec("b", family="fb", binder_seq="ACDEFGHIKLMNPQRSTVWY")   # identical
    c = rec("c", family="fc", binder_seq="WYVTSRQPNMLKIHGFEDCA")   # different
    result = rank_designs([a, b, c], top_k=3,
                          mmr={"lambda_": 0.5, "seq_identity_cap": 0.7})
    names = {r["name"] for r in result.top_k}
    assert len(result.top_k) == 2
    assert "c" in names


def test_end_to_end_reports_survivors_backbones_and_stats():
    recs = [rec(f"f{i}_b0_d0", family=f"f{i}", ipsae_min=0.3 + 0.05 * i,
                binder_seq="A" * 10 + "CDEFG"[i % 5] * 5) for i in range(5)]
    recs.append(rec("bad_b0_d0", family="bad", binder_rmsd_dock=50.0))
    result = rank_designs(recs, top_k=3)
    assert len(result.survivors) == 5
    assert result.n_backbones == 5
    assert len(result.top_k) <= 3
    assert result.filter_stats.n_input == 6
    assert "binder_rmsd_dock <= 5" in result.filter_stats.dropped


def test_empty_input_is_handled():
    result = rank_designs([], top_k=5)
    assert result.survivors == [] and result.top_k == []
