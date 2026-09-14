"""`scripts/benchmark_trimming.py` — the parts that can be wrong silently.

The benchmark exists because the trim's quality guards have never judged a
real cut in a real campaign: 22 of the 25 trims in `projects/` are no-ops, so
every one measures 0.0% exposure and 100% retention and no threshold in
`structure_trim` can be calibrated from anything on disk.

It runs in two phases for a reason. The expensive phase pays one LLM stage
per target (`--workflow structure`, ~$0.13 and ~90 s) to get a REAL epitope
— grounded residue names, real sidechain atoms, every chain-assignment guard
passed. The free phase trims that epitope across a budget ladder, ungated, as
often as you like. What is tested here is the free phase's arithmetic and the
accounting that decides whether the expensive phase runs at all, because both
fail quietly: a mis-ordered verdict mis-attributes refusals, and a spend
reader that reads the wrong field either stops a benchmark early or overruns
the cap.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

import benchmark_trimming as bt  # noqa: E402


# ── verdict precedence ─────────────────────────────────────────────────────

def test_near_epitope_exposure_outranks_everything():
    """Any near-epitope exposure raises unconditionally, and it raises FIRST
    — before the away fraction and before the BSA floor."""
    assert bt.production_verdict(True, True, "over_fraction") == "REFUSED_NEAR"
    assert bt.production_verdict(False, True, "ok") == "REFUSED_NEAR"


def test_the_fraction_outranks_the_retention_floor():
    """Retention is checked LAST, after `write_trimmed`, not with the
    interface arithmetic that computes it. I had this backwards first and
    this is the assertion that caught it."""
    assert bt.production_verdict(True, False, "over_fraction") == (
        "REFUSED_EXPOSURE")
    assert bt.production_verdict(False, False, "over_count") == (
        "REFUSED_EXPOSURE")


def test_retention_is_the_last_word_not_the_first():
    assert bt.production_verdict(True, False, "ok") == "REFUSED_RETENTION"


def test_a_clean_cut_passes_on_every_axis():
    assert bt.production_verdict(False, False, "ok") == "PASS"


def test_the_precedence_matches_trim_targets_own_order():
    """Source inspection against the implementation this mirrors, by
    character offset. A benchmark that orders these differently from
    `trim_target` does not mis-count refusals, it MIS-ATTRIBUTES them — and
    in one direction: a cut bad enough to open core beside the epitope has
    usually damaged the interface too, so both flags set together is the
    common case, and putting retention first relabels a large share of
    REFUSED_NEAR as REFUSED_RETENTION and makes the exposure guard look like
    it catches far less than it does.
    """
    import inspect

    from src import structure_trim

    src = inspect.getsource(structure_trim.trim_target)
    i_near = src.index("if near:")
    i_frac = src.index('if verdict == "over_fraction"')
    i_count = src.index('if verdict == "over_count"')
    i_ret = src.index("below the")          # the retention refusal's own text
    assert i_near < i_frac < i_count < i_ret, (
        "benchmark_trimming.production_verdict encodes near -> fraction -> "
        "count -> retention; trim_target no longer refuses in that order")


# ── the ladder ─────────────────────────────────────────────────────────────

def test_the_ladder_starts_at_the_no_op_control():
    """The 1.0 rung is what separates "this cut opened core" from "this
    structure reads as exposed however you treat it" — a no-op must measure
    0.0%, and without the control there is nothing to check that against."""
    assert bt.LADDER[0] == 1.0


def test_the_ladder_descends_and_is_fine_near_the_top():
    assert list(bt.LADDER) == sorted(bt.LADDER, reverse=True)
    assert len(set(bt.LADDER)) == len(bt.LADDER)
    # The window where a cut is possible at all can be narrow: below the
    # hotspot-bearing domain set every budget raises TrimBudgetError, at or
    # above the whole chain it is a no-op. Measured on 7CZD (one 116-residue
    # Ig domain) that interval is EMPTY.
    assert bt.LADDER[1] >= 0.95, "too coarse to find a narrow cut window"


# ── spend accounting ───────────────────────────────────────────────────────

def _project(tmp_path, monkeypatch, name, manifest=None, ledger=()):
    d = tmp_path / "projects" / name
    d.mkdir(parents=True)
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest),
                                         encoding="utf-8")
    if ledger:
        (d / "ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in ledger), encoding="utf-8")
    monkeypatch.setattr(bt, "ROOT", tmp_path)
    return d


def test_spend_comes_from_the_manifest_rollup(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, "p",
             manifest={"budget": {"spent_usd": 0.1246}})
    assert bt._project_spend("p") == pytest.approx(0.1246)


def test_the_ledger_fallback_counts_actuals_only(tmp_path, monkeypatch):
    """The ledger also carries pre-flight `estimate` rows, and the estimate
    runs ~2.3x the actual here ($0.36 projected against $0.16 measured on
    7CZD). Summing every row would roughly triple the reported spend, and
    this is the number that decides whether the next entry runs."""
    _project(tmp_path, monkeypatch, "p", ledger=[
        {"kind": "estimate", "usd": 0.3611},
        {"kind": "actual", "usd": 0.1246},
        {"kind": "actual", "usd": 0.0310},
    ])
    assert bt._project_spend("p") == pytest.approx(0.1556)


def test_a_project_that_never_ran_spends_nothing(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, "p")
    assert bt._project_spend("p") == 0.0
    monkeypatch.setattr(bt, "ROOT", tmp_path)
    assert bt._project_spend("never_created") == 0.0


def test_a_torn_ledger_line_does_not_abort_the_batch(tmp_path, monkeypatch):
    """Append-only JSONL can end mid-write. One bad line must not stop a
    20-entry run that has already spent money."""
    d = _project(tmp_path, monkeypatch, "p")
    (d / "ledger.jsonl").write_text(
        json.dumps({"kind": "actual", "usd": 0.10}) + "\n{\"kind\": \"act",
        encoding="utf-8")
    assert bt._project_spend("p") == pytest.approx(0.10)


# ── artifact discovery ─────────────────────────────────────────────────────

def test_the_epitope_is_found_wherever_the_track_put_it(tmp_path, monkeypatch):
    """A single-site structure run writes `21_interface.md` under
    `sites/primary/binder/`; a plain binder run writes it one level up."""
    d = _project(tmp_path, monkeypatch, "p")
    nested = d / "runs" / "round-1" / "binder" / "sites" / "primary" / "binder"
    nested.mkdir(parents=True)
    (nested / "21_interface.md").write_text("x", encoding="utf-8")
    assert bt._interface_artifact("p") == nested / "21_interface.md"


def test_no_epitope_reads_as_none_not_an_error(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, "p")
    assert bt._interface_artifact("p") is None
    assert bt._interface_artifact("absent") is None


def test_the_interface_phase_is_idempotent():
    """A rerun after a network failure must cost nothing for the entries that
    already landed — the batch spends real money and is restarted often."""
    import inspect

    src = inspect.getsource(bt.interface_phase)
    assert "_interface_artifact(project) is not None" in src
    assert "skipped" in src
