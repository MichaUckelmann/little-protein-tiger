"""An ITERATE verdict has to say HOW MUCH, not just "enlarge the run".

The verdict used to end with "Enlarge the calibration run or loosen the bar",
which diagnoses without answering the operator's actual question. Measured on
the real PPI+BoltzGen end-to-end run (`projects/ppi_boltzgen_e2e`, 2026-09-15):
1 of 946 backbones cleared `iptm > 0.5`, the 95% interval was 0.02%-0.60%, and
what the operator needed to know was that reaching the 50-design target costs
~360 GPU-h at the observed rate — i.e. that this is the wrong engine for this
target, not a run to nudge.

Every number in the options comes from the run's own measurement (the observed
rate and the `CostModel` the campaign was costed with), so they are exactly as
good as the trial and no better. Where the interval is wide enough to change
the answer BOTH ends are given, because a too-wide interval is the whole
reason the verdict fired.
"""

from __future__ import annotations

import pytest

from src.campaign_calibration import (MIN_HITS_FOR_ESTIMATE, CalibrationResult,
                                      CostModel, RateEstimate, ScaleEstimate,
                                      _cost, _hours_for_backbones,
                                      _ways_forward)

#: BoltzGen on TEAD1/3KYS: one scored design per unit of work, 27.4 s each
#: (measured — 7.20 h for 946 designs).
COST = CostModel.boltzgen(sec_per_design=27.4, bytes_per_design=1.0e6)


def _result(k, n, p_hat, p_low, p_high, target=50, suggested=None):
    rate = RateEstimate(k=k, n=n, p_hat=p_hat, p_low=p_low, p_high=p_high,
                        unit="backbone")

    def est(p):
        if p <= 0:
            return ScaleEstimate("x", None, None, None, None, True)
        b = target / p
        d, h, g = _cost(b * COST.n_seq * COST.prefilter_rate, cost=COST)
        return ScaleEstimate("x", round(d), round(b), round(h, 1), round(g, 1))

    return CalibrationResult(
        target_designs=target, excellence_bar="iptm>0.5", success_metric="iptm",
        n_refolds_scored=n, n_backbones_scored=n, prefilter_rate=1.0, n_seq=1,
        refold_rate=rate, backbone_rate=rate,
        central=est(p_hat), pessimistic=est(p_low), suggested_bar=suggested)


def _real():
    """The e2e run's actual numbers."""
    return _result(k=1, n=946, p_hat=1 / 946, p_low=0.0002, p_high=0.0060)


def _render(res):
    return _ways_forward(res, cost=COST, column="iptm", excellence_bar=0.5,
                         n_gpus_cluster=8)


# ── the three options exist and are numbered ───────────────────────────────

def test_the_options_are_numbered_and_present():
    text = _render(_real())
    assert "Suggested ways forward:" in text
    for n in ("1.", "2.", "3."):
        assert n in text


def test_option_one_sizes_the_next_calibration_from_the_observed_rate():
    """5 hits at 1/946 needs ~4,730 designs; the cost follows from the
    measured 27.4 s/design."""
    text = _render(_real())
    assert "4,730 designs" in text
    assert "36 GPU-h" in text
    assert str(MIN_HITS_FOR_ESTIMATE) in text


def test_option_one_also_gives_the_pessimistic_size():
    """The interval is why the verdict fired, so one number would be a
    misleadingly confident answer. At the 0.02% bound it is ~25,000."""
    text = _render(_real())
    assert "25,000" in text and "190 GPU-h" in text


def test_option_two_prices_the_whole_campaign():
    """The decisive number: 50 designs over the bar at the observed rate is
    ~47,300 designs / ~360 GPU-h. That is what tells an operator this target
    is not worth scaling rather than worth nudging."""
    text = _render(_real())
    assert "47,300 designs" in text
    assert "360 GPU-h" in text
    assert "250,000" in text, "the pessimistic budget must be stated too"


def test_option_three_is_wall_clock_not_a_discount():
    """Cluster GPU-hours are not cheaper, only parallel — 360/8 = 45 h. Saying
    otherwise would misrepresent the trade."""
    text = _render(_real())
    assert "45 h wall-clock" in text and "8 GPUs" in text
    assert "same GPU-hours" in text
    assert "--compute cluster" in text


# ── it adapts rather than printing boilerplate ─────────────────────────────

def test_a_softer_bar_is_offered_only_when_the_sample_supports_one():
    assert "Loosen the bar" not in _render(_real())
    text = _render(_result(k=1, n=946, p_hat=1 / 946, p_low=0.0002,
                           p_high=0.006, suggested=0.4))
    assert "Loosen the bar" in text and "iptm > 0.4" in text


def test_zero_hits_gives_a_floor_and_says_so():
    """With no hits the rule-of-three bound is an OPTIMISTIC rate, so the
    implied sample is a floor. Presenting it as an estimate would promise a
    campaign size the data cannot support."""
    text = _render(_result(k=0, n=946, p_hat=0.0, p_low=0.0, p_high=3 / 946))
    assert "FLOOR" in text
    assert "can only be larger" in text
    # 5 / (3/946) = 1,577
    assert "1,577" in text


def test_no_cluster_option_when_there_is_one_gpu():
    text = _ways_forward(_real(), cost=COST, column="iptm",
                         excellence_bar=0.5, n_gpus_cluster=1)
    assert "wall-clock" not in text
    assert "1." in text and "2." in text


def test_nothing_is_claimed_when_nothing_can_be_computed():
    """A result with no rate and no estimate must produce no options at all
    rather than zeros."""
    res = _result(k=0, n=0, p_hat=0.0, p_low=0.0, p_high=0.0)
    assert _ways_forward(res, cost=COST, column="iptm", excellence_bar=0.5,
                         n_gpus_cluster=8) == ""


# ── the cost inverse ───────────────────────────────────────────────────────

def test_hours_for_backbones_inverts_the_funnel_for_foundry():
    """foundry's cost runs through prefilter -> n_seq -> a refold each, so N
    backbones is not N units of work. Single-stage generators collapse to
    N * sec_per_unit, and both must come out of one function."""
    foundry = CostModel(n_seq=4, prefilter_rate=0.5, sec_per_unit=10.0,
                        sec_per_backbone=6.0, sec_per_seq=0.0)
    # 100 backbones -> 50 survivors -> 200 refolds: 100*6 + 200*10 = 2600 s
    assert _hours_for_backbones(100, foundry) == pytest.approx(2600 / 3600)
    single = CostModel.boltzgen(sec_per_design=27.4, bytes_per_design=1.0)
    assert _hours_for_backbones(946, single) == pytest.approx(946 * 27.4 / 3600,
                                                              rel=1e-6)


def test_the_measured_run_reproduces_its_own_wall_clock():
    """946 BoltzGen designs at 27.4 s is the 7.20 h the job actually took."""
    assert _hours_for_backbones(946, COST) == pytest.approx(7.20, abs=0.05)
