"""Pricing arithmetic and hard-cap behaviour."""

from __future__ import annotations

import json

import pytest

from src.token_budget import (
    BudgetExceeded, Rates, TokenLedger, Usage, load_pricing, price, rates_for,
)


def test_cache_buckets_are_priced_separately_not_summed():
    """
    The API reports uncached input, cache-write and cache-read as SEPARATE
    fields. Summing them into one input number triple-counts a cached prefix —
    here it would inflate the bill ~3x.
    """
    u = Usage(input_tokens=100_000, output_tokens=10_000,
              cache_creation_tokens=20_000, cache_read_tokens=500_000)
    expected = ((100_000 + 20_000 * 1.25 + 500_000 * 0.10) / 1e6) * 3.00 \
        + (10_000 / 1e6) * 15.00
    assert price("claude-sonnet-5", u) == pytest.approx(expected)

    naive = ((100_000 + 20_000 + 500_000) / 1e6) * 3.00 + (10_000 / 1e6) * 15.00
    assert naive > price("claude-sonnet-5", u) * 2.5


def test_usage_addition_is_per_bucket():
    a = Usage(1, 2, 3, 4)
    b = Usage(10, 20, 30, 40)
    assert (a + b) == Usage(11, 22, 33, 44)


def test_dated_snapshot_prices_off_its_undated_base():
    assert rates_for("claude-haiku-4-5-20251001") == rates_for("claude-haiku-4-5")


def test_unknown_model_is_zero_and_flagged_never_raises(tmp_path):
    led = TokenLedger(tmp_path / "l.jsonl")
    entry = led.record(stage="s", skill="k", provider="claude",
                       model="model-from-the-future", usage=Usage(input_tokens=1_000_000))
    assert entry.usd == 0.0
    assert entry.unpriced and led.has_unpriced


def test_config_pricing_overrides_the_code_table():
    load_pricing({"models": {"pricing": {"claude-sonnet-5": {"input": 1.0, "output": 2.0}}}})
    try:
        assert rates_for("claude-sonnet-5") == Rates(1.0, 2.0)
    finally:
        load_pricing({})           # restore the built-in table
    assert rates_for("claude-sonnet-5").input_per_mtok == 3.00


def test_malformed_pricing_entry_is_ignored_not_fatal():
    load_pricing({"models": {"pricing": {"x": {"input": "not-a-number"}}}})
    assert rates_for("x") is None
    load_pricing({})


def test_hard_cap_raises_and_warn_mode_does_not(tmp_path):
    big = Usage(input_tokens=10_000_000)
    hard = TokenLedger(tmp_path / "h.jsonl", cap_usd=1.0, mode="hard")
    with pytest.raises(BudgetExceeded) as exc:
        hard.preflight(stage="structure", model="claude-sonnet-5", estimated=big)
    assert exc.value.stage == "structure"
    assert exc.value.cap_usd == 1.0

    TokenLedger(tmp_path / "w.jsonl", cap_usd=1.0, mode="warn").preflight(
        stage="structure", model="claude-sonnet-5", estimated=big)


def test_no_cap_never_blocks(tmp_path):
    TokenLedger(tmp_path / "n.jsonl").preflight(
        stage="s", model="claude-opus-5", estimated=Usage(input_tokens=10**9))


def test_resume_replays_cumulative_spend(tmp_path):
    """A resumed run must inherit prior spend, not a fresh allowance."""
    path = tmp_path / "ledger.jsonl"
    u = Usage(input_tokens=1_000_000, output_tokens=100_000)
    first = TokenLedger(path, cap_usd=10.0)
    first.record(stage="pathway", skill="p", provider="claude",
                 model="claude-sonnet-5", usage=u)
    second = TokenLedger(path, cap_usd=10.0)
    assert second.spent_usd == pytest.approx(first.spent_usd)
    assert second.spent_usd > 0


def test_torn_final_line_from_a_kill_does_not_break_replay(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = TokenLedger(path)
    led.record(stage="a", skill="s", provider="claude",
               model="claude-haiku-4-5", usage=Usage(input_tokens=1000))
    path.write_text(path.read_text() + '{"ts": "trunc", "usd":\n')
    assert len(TokenLedger(path).entries) == 1


def test_manifest_sink_receives_a_rollup(tmp_path):
    seen = []
    led = TokenLedger(tmp_path / "l.jsonl", cap_usd=5.0, manifest_sink=seen.append)
    led.record(stage="pathway", skill="p", provider="claude",
               model="claude-sonnet-5", usage=Usage(input_tokens=1_000_000))
    assert seen
    block = seen[-1]
    assert block["cap_usd"] == 5.0
    assert block["spent_usd"] > 0
    assert "pathway" in block["by_stage"]
    json.dumps(block)      # must be manifest-serialisable


def test_check_cap_fires_after_an_overshooting_stage(tmp_path):
    led = TokenLedger(tmp_path / "l.jsonl", cap_usd=0.01, mode="hard")
    led.record(stage="a", skill="s", provider="claude",
               model="claude-opus-5", usage=Usage(output_tokens=1_000_000))
    with pytest.raises(BudgetExceeded):
        led.check_cap(next_stage="b")


def test_invalid_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        TokenLedger(tmp_path / "l.jsonl", mode="soft")
