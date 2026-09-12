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


# --- hotspot engagement is a FRACTION, and the bar is not 1.0 ---------------
# Gating on "every declared hotspot" rejects designs for residues RFD3 itself
# never reached: on the 12-hotspot YAP1/TEAD1 calibration only 49% of backbones
# contacted all twelve, while 92.5% of refolds engaged at least as many as their
# own design did. 0.75 recovered +33% survivors for -0.005 median ipTM.

def test_engagement_gate_is_a_fraction_below_one():
    from src.binder_ranking import DEFAULT_THRESHOLDS as D
    bar = D["hotspot_engagement_min"]
    assert 0 < bar < 1.0, "1.0 demands every hotspot; see config.yaml for why"
    assert bar == 0.75


def test_a_design_missing_a_couple_of_hotspots_still_passes():
    from src.binder_ranking import DEFAULT_THRESHOLDS as D
    bar = D["hotspot_engagement_min"]
    assert 9 / 12 >= bar          # 9 of 12 survives
    assert 7 / 12 < bar           # 7 of 12 does not
    assert 7 / 9 >= bar           # the same fraction scales to a 9-hotspot set


def test_the_spec_builder_caps_hotspots_at_the_same_number():
    """The skill caps a region at 12; the builder warns above it."""
    from src.foundry_spec import MAX_HOTSPOTS
    assert MAX_HOTSPOTS == 12


# --- one outlier metric must not carry a design ------------------------------
# The composite is an unbounded weighted z-sum, so before `z_clip` a design far
# clear on a single heavily-weighted column banked an arbitrarily large score
# that no deficit elsewhere could offset, and the top-K filled with one-metric
# specialists. Measured on two real campaigns: the unclipped rank-1 design was a
# `binder_plddt` outlier (z = +3.07 and +2.91), and clipping promoted designs
# docking 42% and 54% better (1.054 -> 0.614 A, 1.258 -> 0.572 A) with equal or
# better epitope recall, for a few thousandths of ipSAE/pLDDT.
#
# The idea is borrowed from BoltzGen's own ranker, which is a MAXIMIN over six
# per-metric ranks; clipping is the softer version that keeps LPT's calibrated
# weights while removing the regime where one metric dominates.

def test_z_clip_is_on_by_default_and_bounds_one_metric():
    from src.binder_ranking import DEFAULT_Z_CLIP
    assert DEFAULT_Z_CLIP == 2.0, "config.yaml documents the 2.0 choice"


def test_clipping_collapses_a_one_metric_outlier_s_dominance():
    """The regime `z_clip` exists to remove, and the honest size of the effect.

    `spike` is a 3.3-sigma outlier on the heaviest column (ipsae_min, weight
    2.0) and mediocre on the other two; `rounded` is solidly good on those two
    and unremarkable on ipSAE. The fillers are SPREAD on iptm/epitope_recall and
    TIGHT on ipsae, which is what makes spike an outlier there and rounded merely
    above-average elsewhere — the real shape of the problem.

    Note what clipping does and does not do: it shrinks spike's margin from
    +2.69 to +0.06, but does NOT by itself flip the order, because at weight 2.0
    with a 2.0 clip the ipSAE term still tops out at 4.0 — exactly equal to
    iptm + epitope_recall maxed together. The weights still matter; clipping
    removes the unbounded regime, it does not override the weighting.
    """
    from src.binder_ranking import composite_score

    weights = {"ipsae_min": 2.0, "iptm": 1.0, "epitope_recall": 1.0}
    fillers = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
    survivors = [
        dict(name="spike", ipsae_min=0.95, iptm=0.55, epitope_recall=0.55),
        dict(name="rounded", ipsae_min=0.50, iptm=0.88, epitope_recall=0.92),
        *[dict(name=f"f{i}", ipsae_min=0.50, iptm=v, epitope_recall=v)
          for i, v in enumerate(fillers)],
    ]

    def margin(z_clip):
        rows = [dict(r) for r in survivors]
        composite_score(rows, weights, z_clip=z_clip)
        by = {r["name"]: r["composite_score"] for r in rows}
        return by["spike"] - by["rounded"]

    unclipped, clipped = margin(None), margin(2.0)
    assert unclipped > 2.0, "precondition: unbounded, the outlier dominates"
    assert clipped < 0.2, "clipping must collapse that dominance"
    assert abs(clipped) < unclipped / 10, "by at least an order of magnitude"


def test_clipping_lets_a_slightly_better_rounded_design_win():
    """One notch better on the other metrics and the order actually flips."""
    from src.binder_ranking import composite_score

    weights = {"ipsae_min": 2.0, "iptm": 1.0, "epitope_recall": 1.0}
    fillers = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
    survivors = [
        dict(name="spike", ipsae_min=0.95, iptm=0.55, epitope_recall=0.55),
        dict(name="rounded", ipsae_min=0.50, iptm=0.90, epitope_recall=0.95),
        *[dict(name=f"f{i}", ipsae_min=0.50, iptm=v, epitope_recall=v)
          for i, v in enumerate(fillers)],
    ]

    def winner(z_clip):
        rows = [dict(r) for r in survivors]
        composite_score(rows, weights, z_clip=z_clip)
        return max(rows, key=lambda r: r["composite_score"])["name"]

    assert winner(None) == "spike", "precondition: the outlier wins unbounded"
    assert winner(2.0) == "rounded"


def test_z_clip_never_changes_which_designs_survive_the_gate():
    """It is a RANKING control, not a gate — `filter_records` never sees it."""
    from src.binder_ranking import rank_designs

    rows = [rec(name=f"d{i}", ipsae_min=0.3 + i / 100) for i in range(12)]
    a = rank_designs([dict(r) for r in rows], z_clip=None)
    b = rank_designs([dict(r) for r in rows], z_clip=2.0)
    assert a.filter_stats.n_survivors == b.filter_stats.n_survivors
    assert {r["name"] for r in a.survivors} == {r["name"] for r in b.survivors}


def test_buried_unsatisfied_polars_outweigh_no_favourable_hbond_term():
    """A liability we trust beats a reward we do not.

    `vbuns` counts polars the interface buries WITHOUT satisfying — a real
    affinity cost. `hbonds_int` counts favourable H-bonds off a predicted
    structure, which over-trusts sidechain placement, so it stays an unweighted
    reported column. `sbuns` is the sidechain-only subset of `vbuns`; weighting
    both would double-count.
    """
    import yaml
    from pathlib import Path

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    w = cfg["design"]["binder_ranking"]["rosetta"]["weights"]
    assert w["neg_rosetta_vbuns"] == 1.0
    assert "rosetta_hbonds_int" not in w and "hbonds_int" not in w
    assert "neg_rosetta_sbuns" not in w
