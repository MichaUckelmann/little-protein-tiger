"""Scale extrapolation: intervals, the zero-hit case, and verdict thresholds."""

from __future__ import annotations

import pytest

from src.campaign_calibration import (
    MIN_HITS_FOR_ESTIMATE, SUCCESS_METRICS, choose_compute, render_report,
    rule_of_three, wilson_interval,
)
from src.campaign_calibration import calibrate as _calibrate
from tests.test_binder_ranking import rec


# ----------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------

def test_the_excellence_bar_is_the_calibrated_value():
    """
    0.5, measured across two complete campaigns rather than guessed: the best
    ipsae_min ever produced was 0.640 (8TAC) / 0.684 (CD79b), so the original
    0.7 was above anything the pipeline has made.
    """
    from src.binder_ranking import EXCELLENT_IPSAE_MIN
    assert EXCELLENT_IPSAE_MIN == 0.5


def test_wilson_brackets_the_point_estimate_and_stays_in_range():
    lo, hi = wilson_interval(44, 28420)
    assert 0.0 < lo < 44 / 28420 < hi < 1.0


def test_wilson_stays_non_negative_at_tiny_k():
    """The normal approximation returns a negative lower bound here."""
    lo, hi = wilson_interval(2, 1200)
    assert lo >= 0.0
    assert hi / max(lo, 1e-12) > 5, "interval should be wide at k=2, not falsely tight"


def test_wilson_of_zero_is_zero_to_something_small():
    lo, hi = wilson_interval(0, 1000)
    assert lo == pytest.approx(0.0, abs=1e-12) and 0 < hi < 0.01


def test_rule_of_three_approximates_three_over_n():
    assert rule_of_three(1200) == pytest.approx(3 / 1200, rel=0.01)


def test_rule_of_three_handles_zero_trials():
    assert rule_of_three(0) == 1.0


# ----------------------------------------------------------------------
# Extrapolation
# ----------------------------------------------------------------------

def _sample(n_backbones, n_seq, n_excellent, ipsae_hit=0.9, ipsae_miss=0.2):
    """n_backbones families of n_seq passing designs, n_excellent above the bar."""
    out = []
    for f in range(n_backbones):
        for d in range(n_seq):
            hit = f < n_excellent and d == 0
            out.append(rec(f"fam{f}_b0_d{d}", family=f"fam{f}",
                           ipsae_min=ipsae_hit if hit else ipsae_miss))
    return out


# These fixtures vary ipsae_min, so they must be calibrated on it. The pipeline
# DEFAULT is iptm — see test_iptm_is_the_default_sizing_metric.
def calibrate(records, **kw):
    kw.setdefault("success_metric", "ipsae_min")
    return _calibrate(records, **kw)


def test_zero_hits_yields_a_lower_bound_never_a_finite_estimate():
    """
    With k = 0 there is no rate to extrapolate from. Reporting a finite required
    scale would be inventing one; the rule-of-three bound on the rate is a LOWER
    bound on the campaign size.
    """
    res = calibrate(_sample(300, 4, 0), target_designs=100, excellence_bar=0.7)
    assert res.backbone_rate.zero_hits
    assert res.central.is_lower_bound
    assert res.central.required_refolds is not None
    assert res.central.basis == "rule-of-three"


def test_a_healthy_rate_gives_a_central_and_a_pessimistic_estimate():
    res = calibrate(_sample(100, 4, 40), target_designs=100, excellence_bar=0.7,
                    disk_budget_gb=10_000, max_campaign_days=100)
    assert not res.backbone_rate.zero_hits
    assert res.central.basis == "point estimate"
    assert res.pessimistic.basis.startswith("Wilson")
    # Pessimism must cost more, not less.
    assert res.pessimistic.required_refolds > res.central.required_refolds
    assert res.verdict == "SCALE_UP"


def test_backbones_not_refolds_are_the_sizing_unit():
    """
    The n_seq sequences on one backbone are correlated, so a refold-level rate
    overstates the effective sample size. Both are reported; sizing uses
    backbones, which is what n_batches actually controls.
    """
    res = calibrate(_sample(100, 4, 10), target_designs=100, excellence_bar=0.7,
                    disk_budget_gb=10_000, max_campaign_days=100)
    assert res.backbone_rate.n == 100
    assert res.refold_rate.n == 400
    assert res.backbone_rate.k == 10
    assert res.backbone_rate.p_hat > res.refold_rate.p_hat


def test_cost_scales_with_the_target():
    kw = dict(excellence_bar=0.7, disk_budget_gb=10**9, max_campaign_days=10**6)
    small = calibrate(_sample(100, 4, 20), target_designs=10, **kw)
    big = calibrate(_sample(100, 4, 20), target_designs=100, **kw)
    assert big.central.required_refolds == pytest.approx(
        10 * small.central.required_refolds)


# ----------------------------------------------------------------------
# Verdicts
# ----------------------------------------------------------------------

def test_an_unaffordable_but_nonzero_rate_reports_what_IS_affordable():
    res = calibrate(_sample(200, 4, 2), target_designs=100, excellence_bar=0.7,
                    disk_budget_gb=50, max_campaign_days=2)
    assert res.verdict in ("SCALE_UP_PARTIAL", "ITERATE")
    if res.verdict == "SCALE_UP_PARTIAL":
        assert 0 < res.affordable_designs < 100


def test_a_systematically_failed_gate_recommends_ITERATE_not_more_gpu():
    """
    A criterion almost nothing passes on its own is a target problem. More
    sampling cannot fix designs that all miss the epitope.
    """
    recs = [rec(f"f{i}_b0_d0", family=f"f{i}", ipsae_min=0.2,
                hotspot_engagement=0.0) for i in range(300)]
    res = calibrate(recs, target_designs=100, excellence_bar=0.7)
    assert res.verdict == "ITERATE"
    assert "hotspot_engagement" in res.verdict_reason


def test_best_ipsae_is_surfaced_so_zero_hits_is_interpretable():
    """"0 hits" cannot distinguish a near miss from nowhere near."""
    res = calibrate(_sample(50, 4, 0, ipsae_miss=0.68), target_designs=100,
                    excellence_bar=0.7)
    assert res.best_ipsae_min_overall == pytest.approx(0.68)


def test_report_renders_and_names_the_verdict():
    res = calibrate(_sample(100, 4, 5), target_designs=100, excellence_bar=0.7)
    text = render_report(res)
    assert res.verdict in text
    assert "Wilson" in text or "rule of three" in text
    assert "backbone" in text


def test_result_is_json_serialisable_for_the_manifest():
    import json
    res = calibrate(_sample(100, 4, 5), target_designs=100, excellence_bar=0.7)
    json.dumps(res.as_dict())


# ----------------------------------------------------------------------
# The zero-hit inversion this logic exists to prevent
# ----------------------------------------------------------------------

def test_a_stricter_bar_never_reports_a_cheaper_campaign():
    """
    Regression for a real defect. With k = 0 the rule-of-three bound is built on
    an OPTIMISTIC rate, so its implied campaign came out smaller than the point
    estimate at a looser bar — reading as "the stricter bar is cheaper", the
    opposite of the truth. Zero-hit results must be diagnosed, never compared
    against budget as if they were estimates.
    """
    sample = _sample(2000, 4, 40, ipsae_hit=0.45, ipsae_miss=0.05)
    scales = {}
    for bar in (0.3, 0.4, 0.5, 0.6):
        res = calibrate(sample, target_designs=100, excellence_bar=bar,
                        disk_budget_gb=120, max_campaign_days=5)
        scales[bar] = (res.pessimistic.required_refolds,
                       res.pessimistic.is_lower_bound, res.verdict)

    estimates = [(b, s) for b, (s, lower, _) in scales.items() if not lower]
    for (b1, s1), (b2, s2) in zip(estimates, estimates[1:]):
        assert s2 >= s1, f"bar {b2} looks cheaper than {b1}: {s2} < {s1}"

    # Anything that is only a bound must say so rather than claim a verdict
    # derived from comparing it to the budget.
    for bar, (_, lower, verdict) in scales.items():
        if lower:
            assert verdict in ("ITERATE", "STOP")


def test_too_few_hits_refuses_to_size_a_campaign():
    """One hit gives an interval spanning ~30x; that cannot justify four days."""
    res = calibrate(_sample(300, 4, 1), target_designs=100, excellence_bar=0.7,
                    disk_budget_gb=10_000, max_campaign_days=1000)
    assert res.backbone_rate.k == 1
    assert res.verdict == "ITERATE"
    assert "usable estimate" in res.verdict_reason


def test_zero_at_the_bar_but_hits_lower_down_blames_the_bar_not_the_target():
    sample = _sample(2000, 4, 60, ipsae_hit=0.45, ipsae_miss=0.05)
    res = calibrate(sample, target_designs=100, excellence_bar=0.8)
    assert res.backbone_rate.zero_hits
    assert res.suggested_bar is not None and res.suggested_bar <= 0.4
    assert "not the target" in res.verdict_reason
    assert res.verdict == "ITERATE"


def test_nothing_anywhere_on_the_ladder_is_STOP():
    res = calibrate(_sample(2000, 4, 0, ipsae_miss=0.0), target_designs=100,
                    excellence_bar=0.7)
    assert res.verdict == "STOP"
    assert "Nothing here is working" in res.verdict_reason


def test_suggested_bar_is_the_strictest_the_sample_can_support():
    sample = _sample(1000, 4, 200, ipsae_hit=0.55, ipsae_miss=0.05)
    res = calibrate(sample, target_designs=100, excellence_bar=0.9)
    assert res.suggested_bar == 0.5


def test_a_marginal_budget_overshoot_is_not_a_hard_stop():
    """Overshooting by a few percent is noise against a 30x interval."""
    sample = _sample(1000, 4, 300, ipsae_hit=0.9)
    res = calibrate(sample, target_designs=100, excellence_bar=0.7,
                    disk_budget_gb=0.001, max_campaign_days=0.001)
    assert res.verdict in ("SCALE_UP_PARTIAL", "ITERATE")
    assert res.verdict != "STOP"


def test_zero_hit_report_labels_the_number_as_a_lower_bound():
    res = calibrate(_sample(300, 4, 0), target_designs=100, excellence_bar=0.7)
    text = render_report(res)
    assert "LOWER BOUND" in text
    assert "at least this large" in text


# ----------------------------------------------------------------------
# Which metric sizes the campaign
# ----------------------------------------------------------------------

def _iptm_sample(n_backbones, n_seq, n_excellent, hit=0.85, miss=0.55):
    out = []
    for f in range(n_backbones):
        for d in range(n_seq):
            is_hit = f < n_excellent and d == 0
            out.append(rec(f"fam{f}_b0_d{d}", family=f"fam{f}",
                           iptm=hit if is_hit else miss, ipsae_min=0.2))
    return out


def test_iptm_is_the_default_sizing_metric():
    """
    Per 1000 backbones the reference campaigns produced 13.2 / 2.0 designs at
    iPTM > 0.7 versus 2.5 / 0.1 at ipsae_min > 0.5. A trial-sized sample can
    measure the first and not the second.
    """
    # adaptive_bar off: this test is about the METRIC choice, not the bar level
    # (see TestAdaptiveBar for that) — _iptm_sample's hit rate is deliberately
    # high enough to otherwise raise the bar and change this assertion.
    res = _calibrate(_iptm_sample(300, 4, 20), adaptive_bar=False)
    assert res.success_metric == "iptm"
    assert res.target_designs == 50
    assert "iptm > 0.7" in res.excellence_bar
    assert res.backbone_rate.k == 20


def test_success_metric_defaults_come_from_one_table():
    assert SUCCESS_METRICS["iptm"] == ("iptm", 0.7, 50)
    assert SUCCESS_METRICS["ipsae_min"] == ("ipsae_min", 0.5, 100)


def test_ipsae_min_can_still_size_a_campaign():
    res = _calibrate(_sample(300, 4, 30), success_metric="ipsae_min")
    assert res.success_metric == "ipsae_min"
    assert res.target_designs == 100
    assert res.backbone_rate.k == 30


def test_an_unknown_success_metric_is_rejected():
    with pytest.raises(ValueError, match="success_metric"):
        _calibrate(_sample(10, 4, 1), success_metric="plddt")


def test_the_two_metrics_use_different_bar_ladders():
    """iPTM lives at 0.5-0.9; ipsae_min at 0.1-0.6. One ladder cannot serve both."""
    iptm = _calibrate(_iptm_sample(100, 4, 10))
    ips = _calibrate(_sample(100, 4, 10), success_metric="ipsae_min")
    assert any("iptm > 0.85" in k for k in iptm.softer_rates)
    assert any("ipsae_min > 0.2" in k for k in ips.softer_rates)


def test_the_sizing_metric_does_not_change_what_is_ranked():
    """
    Sizing on iPTM must not stop ipsae_min being computed — it is still the
    heaviest ranking weight.
    """
    from src.binder_ranking import DEFAULT_WEIGHTS

    assert DEFAULT_WEIGHTS["ipsae_min"] == max(DEFAULT_WEIGHTS.values())
    rows = _iptm_sample(50, 4, 5)
    assert all("ipsae_min" in r for r in rows)


def test_a_trial_of_300_backbones_is_usually_too_small_to_size_a_campaign():
    """
    Measured on the reference campaign: at its real rate, 300 backbones gave a
    usable estimate in 0/10 seeds and 1000 in 8/10. This is why trials escalate.
    """
    # ~5 hits per 1000 backbones, the observed 8TAC rate
    small = _calibrate(_iptm_sample(300, 4, 1))
    assert small.backbone_rate.k < MIN_HITS_FOR_ESTIMATE
    assert small.verdict == "ITERATE"
    assert "usable estimate" in small.verdict_reason or \
           "too small" in small.verdict_reason

    bigger = _calibrate(_iptm_sample(1000, 4, 5), disk_budget_gb=10_000,
                        max_campaign_days=1000)
    assert bigger.backbone_rate.k >= MIN_HITS_FOR_ESTIMATE
    assert bigger.verdict.startswith("SCALE_UP")


# ----------------------------------------------------------------------
# Adaptive bar — raise the sizing bar when the trial clearly supports it
# ----------------------------------------------------------------------
# _sample()'s hits sit at ipsae_min=0.9, comfortably above every rung on the
# ladder ([0.2 .. 0.8]) — real KRAS/RAF1 data hit 0.931, so this is not an
# unrealistic fixture.

class TestAdaptiveBar:
    def test_raises_the_bar_when_the_trial_clearly_supports_it(self):
        res = calibrate(_sample(300, 4, 20), target_designs=50, excellence_bar=0.5,
                        disk_budget_gb=10_000, max_campaign_days=100)
        assert res.requested_bar == 0.5
        assert res.bar_raised_to == 0.8   # top of the ipsae_min ladder
        assert "ipsae_min > 0.8" in res.excellence_bar
        assert res.verdict == "SCALE_UP"

    def test_never_lowers_the_bar_below_what_was_requested(self):
        assert calibrate(
            _sample(300, 4, 20), target_designs=50, excellence_bar=0.5,
            disk_budget_gb=10_000, max_campaign_days=100).requested_bar == 0.5

    def test_disabled_reproduces_the_pre_adaptive_behaviour(self):
        res = calibrate(_sample(300, 4, 20), target_designs=50, excellence_bar=0.5,
                        disk_budget_gb=10_000, max_campaign_days=100,
                        adaptive_bar=False)
        assert res.bar_raised_to is None
        assert "ipsae_min > 0.5" in res.excellence_bar

    def test_does_not_raise_past_what_the_budget_affords(self):
        """A generous hit rate at a tiny budget must not raise the bar — an
        upgrade only fires when it is comfortably affordable, not desperately
        squeezed in the way SCALE_UP_PARTIAL's slack allowance is."""
        res = calibrate(_sample(300, 4, 20), target_designs=50, excellence_bar=0.5,
                        disk_budget_gb=0.001, max_campaign_days=0.001)
        assert res.bar_raised_to is None

    def test_too_few_hits_at_a_stricter_rung_does_not_raise(self):
        """Only 2 hits — below MIN_HITS_FOR_ESTIMATE at every rung above the
        requested bar — so there is nothing to raise TO."""
        res = calibrate(_sample(300, 4, 2), target_designs=50, excellence_bar=0.5,
                        disk_budget_gb=10_000, max_campaign_days=100)
        assert res.bar_raised_to is None

    def test_zero_hit_trials_are_not_eligible_for_raising(self):
        res = calibrate(_sample(300, 4, 0), target_designs=50, excellence_bar=0.5)
        assert res.bar_raised_to is None
        assert res.backbone_rate.zero_hits

    def test_raising_reuses_the_same_wilson_machinery_not_a_multiplier_table(self):
        """The raised bar's pessimistic estimate must be internally consistent
        with a fresh Wilson interval at that bar's own k/n — not a fudge factor
        applied to the original estimate."""
        res = calibrate(_sample(300, 4, 20), target_designs=50, excellence_bar=0.5,
                        disk_budget_gb=10_000, max_campaign_days=100)
        assert res.bar_raised_to == 0.8
        lo, hi = wilson_interval(res.backbone_rate.k, res.backbone_rate.n)
        assert res.backbone_rate.p_low == pytest.approx(lo)
        # required_backbones is RFD3 designs needed pre-prefilter: (target/p)/prefilter_rate.
        default_prefilter_rate = 0.59
        assert res.pessimistic.required_backbones == pytest.approx(
            (50 / lo) / default_prefilter_rate, rel=0.02)

    def test_render_report_surfaces_the_raise(self):
        res = calibrate(_sample(300, 4, 20), target_designs=50, excellence_bar=0.5,
                        disk_budget_gb=10_000, max_campaign_days=100)
        report = render_report(res)
        assert "Bar raised" in report
        assert "0.8" in report
        assert "raised" in res.verdict_reason.lower()


# ----------------------------------------------------------------------
# choose_compute: local vs. cluster placement for a SCALE_UP campaign
# ----------------------------------------------------------------------

class TestChooseCompute:
    def test_no_scale_up_means_nothing_to_place(self):
        res = calibrate(_sample(300, 4, 0), target_designs=100, excellence_bar=0.7)
        assert res.verdict != "SCALE_UP"
        assert choose_compute(res) is None

    def test_a_cheap_campaign_stays_local(self):
        """Healthy rate, modest target -> a few hours, well under the default
        48h -> stays on this workstation's GPU."""
        res = calibrate(_sample(100, 4, 40), target_designs=100, excellence_bar=0.7,
                        disk_budget_gb=10_000, max_campaign_days=100)
        assert res.verdict == "SCALE_UP"
        assert res.pessimistic.est_gpu_hours < 48
        choice = choose_compute(res)
        assert choice is not None
        assert choice.compute == "local"
        assert choice.local_hours <= choice.max_local_hours

    def test_an_expensive_campaign_goes_to_the_cluster(self):
        """A rare rate against a big target blows past the local-hours budget,
        so it must be staged for the cluster instead of run unattended for days."""
        res = calibrate(_sample(300, 4, 5), target_designs=100, excellence_bar=0.7,
                        disk_budget_gb=10**9, max_campaign_days=10**6)
        assert res.verdict == "SCALE_UP"
        assert res.pessimistic.est_gpu_hours > 48
        choice = choose_compute(res)
        assert choice is not None
        assert choice.compute == "cluster"
        assert choice.local_hours > choice.max_local_hours

    def test_cluster_hours_divide_by_gpu_count(self):
        res = calibrate(_sample(100, 4, 40), target_designs=100, excellence_bar=0.7,
                        disk_budget_gb=10_000, max_campaign_days=100)
        choice = choose_compute(res, n_gpus_cluster=8)
        assert choice.cluster_hours == pytest.approx(choice.local_hours / 8, rel=0.02)

    def test_max_local_hours_is_the_threshold_not_a_fixed_verdict(self):
        """Same result, evaluated at two thresholds either side of its hours,
        must flip local <-> cluster purely on the threshold."""
        res = calibrate(_sample(300, 4, 5), target_designs=100, excellence_bar=0.7,
                        disk_budget_gb=10**9, max_campaign_days=10**6)
        hours = res.pessimistic.est_gpu_hours
        assert choose_compute(res, max_local_hours=hours + 1).compute == "local"
        assert choose_compute(res, max_local_hours=max(hours - 1, 0)).compute == "cluster"

    def test_render_report_includes_the_compute_section(self):
        res = calibrate(_sample(100, 4, 40), target_designs=100, excellence_bar=0.7,
                        disk_budget_gb=10_000, max_campaign_days=100)
        choice = choose_compute(res)
        report = render_report(res, choice)
        assert "Where to run it" in report
        assert "Decision: local" in report

    def test_render_report_without_compute_omits_the_section(self):
        res = calibrate(_sample(100, 4, 40), target_designs=100, excellence_bar=0.7,
                        disk_budget_gb=10_000, max_campaign_days=100)
        report = render_report(res)
        assert "Where to run it" not in report
