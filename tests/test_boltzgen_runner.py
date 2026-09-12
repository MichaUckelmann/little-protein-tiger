"""`src.boltzgen_runner` — the cost laws, and progress that is not a lie.

The cost assertions pin the laws against the measurements they were fitted to,
with the honest tolerances: the disk law is tight (±2%) because BoltzGen writes
no PAE matrix and disk is therefore linear in complex size, while the time law
has real scatter (three at-scale points, one off by 41%) and exists as a
fallback behind an observed rate.

The progress assertions cover the trap this module was written around: under
`--reuse` a campaign is normally EXTENDED from a smaller earlier invocation,
which leaves a complete metrics table behind.
"""

from __future__ import annotations

import pathlib

import pytest

from src.boltzgen_runner import (
    BYTES_PER_DESIGN_REF, BoltzGenPaths, DISK_EXPONENT, MIN_DESIGNS_FOR_RATE,
    PROTEIN_PROTOCOL_FACTOR, REF_TOKENS, SEC_PER_DESIGN_REF, build_argv,
    count_metrics_rows, count_refolds, design_bytes, plan_campaign, progress,
    sec_per_design_observed, seconds_per_design,
)

#: (tokens, s/design, MB/design) — AT-SCALE slices only. A 24-design probe of
#: the anchor campaign read 16.0 s/design against its at-scale 7.8, which is
#: why small slices are excluded from the fit and from these assertions.
_MEASURED = [(99, 7.82, 0.348), (229, 17.00, 0.791), (817, 122.30, 2.657)]


def _campaign(tmp_path: pathlib.Path, *, designs=0, invfold=0, refolds=0,
              metrics_rows: int | None = None, step: str = "") -> BoltzGenPaths:
    p = BoltzGenPaths.under(tmp_path)
    for d, n in ((p.design_dir, designs), (p.invfold_dir, invfold),
                 (p.refold_dir, refolds)):
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (d / f"d{i}.cif").write_text("x", encoding="utf-8")
    if metrics_rows is not None:
        p.final_dir.mkdir(parents=True, exist_ok=True)
        p.metrics_csv.write_text(
            "id,iptm\n" + "".join(f"d{i},0.5\n" for i in range(metrics_rows)),
            encoding="utf-8")
    if step:
        p.log_path.write_text(f"blah\r[Step {step}] folding\rmore\n",
                              encoding="utf-8")
    return p


# ── the laws ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tokens,mb", [(t, mb) for t, _, mb in _MEASURED])
def test_the_disk_law_reproduces_every_measurement_within_two_percent(tokens, mb):
    """Tight because it is near-linear, and near-linear for a reason: BoltzGen
    persists coordinates and SCALAR confidences, never a PAE matrix. RF3's
    exponent is 1.49 because half its bytes are that O(N^2) matrix."""
    got = design_bytes(tokens) / 1e6
    assert abs(got - mb) / mb < 0.02, f"{got:.3f} vs measured {mb:.3f} MB"


def test_the_disk_law_is_essentially_linear():
    assert 0.90 < DISK_EXPONENT < 1.05


def test_the_time_law_reproduces_the_anchor_and_the_large_end():
    """The two points the law is trusted on. The 229-token point is off by
    ~39% and is NOT asserted tightly — that scatter is why an observed rate
    takes precedence over this law everywhere it is used."""
    assert seconds_per_design(99) == pytest.approx(7.82, rel=0.01)
    assert seconds_per_design(817) == pytest.approx(122.3, rel=0.10)


def test_the_time_law_is_monotonic_in_complex_size():
    vals = [seconds_per_design(t) for t in (99, 161, 229, 400, 817)]
    assert vals == sorted(vals)


def test_an_unknown_size_falls_back_to_the_anchor():
    assert seconds_per_design(None) == SEC_PER_DESIGN_REF
    assert seconds_per_design(0) == SEC_PER_DESIGN_REF
    assert design_bytes(None) == BYTES_PER_DESIGN_REF


def test_the_protein_protocol_costs_more_at_the_same_size():
    """It runs SIX steps to peptide's five; the extra one refolds the binder
    alone. Confirmed live — the cyclic run logs [Step 3/5], the mini [Step 3/6].

    The measured margin is ~18%, not the ~80% a probe suggested: the extra
    step refolds the BINDER alone (70-83 residues against the complex's 160),
    so it is the cheapest of the six.
    """
    for t in (99, 161, 817):
        pep = seconds_per_design(t, "peptide-anything")
        pro = seconds_per_design(t, "protein-anything")
        assert pro == pytest.approx(pep * PROTEIN_PROTOCOL_FACTOR)
        assert pro > pep
    # Omitting the protocol must not silently apply the multiplier.
    assert seconds_per_design(161) == seconds_per_design(161, "peptide-anything")


def test_the_protein_factor_reproduces_the_at_scale_mini_campaign():
    """The one at-scale protein-protocol point there is: the RAMP1 mini
    campaign, 970 designs at a 154-167-token complex, END-TO-END 17.32
    s/design from its own `campaign_timing.jsonl`.

    Its 24-design PROBE read 26.52 s/design, and a factor fitted to THAT
    over-costs a campaign by ~53% — the same startup effect that made the
    cyclic probe read 16.04 against an at-scale 7.82. A probe cannot set this
    constant, which is the whole reason `MIN_DESIGNS_FOR_RATE` exists.
    """
    assert seconds_per_design(160, "protein-anything") == pytest.approx(
        17.32, rel=0.02)
    # And the peptide law at the same size, which the factor multiplies.
    assert seconds_per_design(160, "peptide-anything") == pytest.approx(
        14.74, rel=0.02)


# ── progress, and the stale-metrics trap ────────────────────────────────────

def test_counts_come_from_disk(tmp_path):
    p = _campaign(tmp_path, designs=7, invfold=5, refolds=3, metrics_rows=2)
    pr = progress(p, expected=10)
    assert (pr.n_designs, pr.n_inverse_folded, pr.n_refolds) == (7, 5, 3)
    assert pr.n_metrics == 2 and count_refolds(p) == 3


def test_a_stale_metrics_table_does_not_report_complete(tmp_path):
    """The real trap. `--reuse` extends a campaign from a smaller invocation,
    and that invocation left a COMPLETE metrics table behind — so a 994-design
    run sitting at 572 refolds reported COMPLETE off its own 24-design probe's
    table, and an orchestrator would have moved on mid-campaign."""
    p = _campaign(tmp_path, designs=994, invfold=994, refolds=572,
                  metrics_rows=24)
    pr = progress(p, expected=994)
    assert pr.metrics_written is True
    assert pr.complete is False, "a 24-row table cannot finish a 994-design run"


def test_complete_when_the_table_covers_the_campaign(tmp_path):
    p = _campaign(tmp_path, designs=994, invfold=994, refolds=994,
                  metrics_rows=994)
    assert progress(p, expected=994).complete is True


def test_refolds_alone_never_mean_complete(tmp_path):
    """`analysis` and `filtering` run AFTER every refold is on disk and are
    CPU-bound, so a refold-count check calls a campaign done too early."""
    p = _campaign(tmp_path, designs=100, invfold=100, refolds=100)
    assert progress(p, expected=100).complete is False


def test_a_zero_expectation_is_not_complete(tmp_path):
    p = _campaign(tmp_path, metrics_rows=0)
    assert progress(p, expected=0).complete is False


def test_missing_directories_count_zero_rather_than_raising(tmp_path):
    p = BoltzGenPaths.under(tmp_path / "nothing-here")
    pr = progress(p, expected=100)
    assert (pr.n_designs, pr.n_refolds, pr.n_metrics) == (0, 0, 0)
    assert pr.step == "" and pr.complete is False


def test_the_last_reported_step_is_read_through_tqdm_carriage_returns(tmp_path):
    p = _campaign(tmp_path, step="3/6")
    assert progress(p, expected=1).step == "[Step 3/6] folding"


def test_a_short_run_reports_no_observed_rate(tmp_path):
    """Below the threshold a rate is startup-dominated: measured, a 24-design
    slice read 16.0 s/design against the same campaign's at-scale 7.8."""
    p = _campaign(tmp_path, refolds=MIN_DESIGNS_FOR_RATE - 1)
    assert sec_per_design_observed(p) == 0.0


# ── planning ────────────────────────────────────────────────────────────────

def test_planning_says_which_rate_it_used(tmp_path):
    p = _campaign(tmp_path)
    plan = plan_campaign(p, num_designs=100, budget=10, n_tokens=99,
                         protocol="peptide-anything")
    assert "size law" in plan.rate_source and "99 tokens" in plan.rate_source
    assert plan.est_gpu_hours == pytest.approx(100 * 7.82 / 3600, rel=0.02)


def test_a_supplied_measurement_outranks_the_law(tmp_path):
    p = _campaign(tmp_path)
    plan = plan_campaign(p, num_designs=100, budget=10, n_tokens=817,
                         sec_per_design=5.0)
    assert plan.sec_per_design == 5.0 and "supplied" in plan.rate_source


def test_costing_without_a_protocol_warns_about_under_costing(tmp_path):
    p = _campaign(tmp_path)
    plan = plan_campaign(p, num_designs=100, budget=10, n_tokens=161)
    assert any("under-costs" in w for w in plan.warnings)


def test_costing_with_no_size_at_all_warns_loudly(tmp_path):
    """The one case that can be wrong by an order of magnitude — 7.8 s/design
    at the anchor against 122 s at 817 tokens."""
    p = _campaign(tmp_path)
    plan = plan_campaign(p, num_designs=100, budget=10)
    assert plan.rate_source.startswith("bare anchor")
    assert any("lower bound" in w for w in plan.warnings)


def test_the_disk_budget_clamps_the_design_count(tmp_path):
    p = _campaign(tmp_path)
    plan = plan_campaign(p, num_designs=1_000_000, budget=100, n_tokens=817,
                         disk_budget_gb=5.0, min_free_gb=0.0)
    assert plan.num_designs < 1_000_000
    assert plan.est_disk_gb <= 5.0 + 1e-6
    assert any("clamped" in w for w in plan.warnings)
    assert plan.budget <= plan.num_designs, "budget cannot exceed the designs"


# ── the command line ────────────────────────────────────────────────────────

def test_reuse_is_on_so_a_campaign_can_be_extended(tmp_path):
    """`--reuse` is what makes a probe the first slice of the real run, and
    what makes a crash cost only the unfinished designs."""
    p = BoltzGenPaths.under(tmp_path)
    argv = build_argv("boltzgen", pathlib.Path("/tmp/s.yaml"), p,
                      protocol="peptide-anything", num_designs=1000, budget=100)
    assert "--reuse" in argv
    assert argv[:3] == ["boltzgen", "run", "/tmp/s.yaml"]
    assert "--num_designs" in argv and "1000" in argv
    assert str(p.campaign_dir) in argv


def test_reuse_can_be_turned_off(tmp_path):
    argv = build_argv("boltzgen", pathlib.Path("/tmp/s.yaml"),
                      BoltzGenPaths.under(tmp_path),
                      protocol="peptide-anything", num_designs=10, budget=5,
                      reuse=False)
    assert "--reuse" not in argv
