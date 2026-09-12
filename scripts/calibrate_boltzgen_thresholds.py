"""Measure a BoltzGen campaign's hit rate across a ladder of thresholds.

The shipped `design.thresholds` block has never gated a real run — every e2e
driver overrode all four values, so the numbers in `config.yaml` were inherited
rather than measured, and applying them as shipped produces an empty top-K
without crashing. This script is how a bar gets chosen from data instead: it
walks each gate's ladder over a completed campaign's own
`all_designs_metrics.csv` and reports, per rung, the hit count, the rate, and a
Wilson 95% interval.

It reuses `campaign_calibration.wilson_interval` / `rule_of_three` rather than
re-deriving them, for the same reason the calibration stage does: at tiny k and
large n the normal approximation returns negative lower bounds, and after k = 0
the only defensible statement is the rule-of-three upper bound — which is a
LOWER bound on the campaign size you would need, never an estimate of the rate.
`MIN_HITS_FOR_ESTIMATE` (5) is likewise imported, so "is this rung
measurable?" means the same thing here as it does for a foundry campaign.

Scoring is BoltzGen's own. Every column read is one BoltzGen wrote; nothing
here recomputes a metric or consults the binder track. Re-scoring the same
directory with binder-track metrics is a separate question, answered by
`scripts/measure_boltzgen_binder_gates.py`.

Two reporting choices worth stating:

* **Gates are reported INDEPENDENTLY and then jointly.** A per-gate rate says
  which threshold is doing the work; only the joint rate sizes a campaign. On
  the probe data these disagreed sharply by modality — cyclic peptides sailed
  through `ipae <= 10` and failed `iptm >= 0.60`, mini-proteins failed both —
  so a single scalar per gate cannot serve both modalities.
* **`pass_filters` is reported but never silently folded in.** It is
  BoltzGen's own opaque composite, and on archived runs it alone discarded 99
  of 100 designs. Whether to gate on it is a decision, so it appears as its
  own row rather than as an invisible multiplier on everything else.

Usage:

    python scripts/calibrate_boltzgen_thresholds.py outputs/bz_calib_ramp1_cyclic
    python scripts/calibrate_boltzgen_thresholds.py outputs/bz_calib_ramp1_mini \
        --compare outputs/bz_calib_ramp1_cyclic
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.campaign_calibration import (  # noqa: E402
    MIN_HITS_FOR_ESTIMATE,
    rule_of_three,
    wilson_interval,
)

#: Per-gate ladders. Each is (column, comparison, rungs) where comparison is
#: ">=" or "<=". The rungs bracket the shipped `design.thresholds` value so the
#: shipped choice can be read off the same table as its neighbours.
_LADDERS: tuple[tuple[str, str, tuple[float, ...]], ...] = (
    ("design_to_target_iptm", ">=", (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80)),
    ("min_design_to_target_pae", "<=", (4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0)),
    ("complex_plddt", ">=", (0.60, 0.65, 0.70, 0.75, 0.80)),
)
#: The shipped values, for annotation only (config.yaml design.thresholds).
_SHIPPED = {"design_to_target_iptm": 0.60, "min_design_to_target_pae": 10.0}


def read_metrics(run_dir: Path) -> list[dict]:
    """Every design's row from BoltzGen's own metrics table."""
    for cand in (run_dir / "final_ranked_designs" / "all_designs_metrics.csv",
                 run_dir / "04_execution_outputs" / "final_ranked_designs"
                 / "all_designs_metrics.csv"):
        if cand.is_file():
            with cand.open(newline="", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
    raise SystemExit(f"no all_designs_metrics.csv under {run_dir}")


def _floats(rows: list[dict], col: str) -> list[float]:
    out = []
    for r in rows:
        try:
            out.append(float(r[col]))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def _passes(row: dict, col: str, cmp: str, lim: float) -> bool | None:
    """None when the column is absent — distinct from failing it."""
    try:
        v = float(row[col])
    except (KeyError, TypeError, ValueError):
        return None
    return v >= lim if cmp == ">=" else v <= lim


def _rate_line(k: int, n: int) -> str:
    """`k/n` with a Wilson interval, or the rule-of-three bound when k == 0."""
    if n == 0:
        return "n=0"
    if k == 0:
        return (f"{k:5d}/{n:<5d}  0.00%   p < {100 * rule_of_three(n):.2f}% "
                f"(rule of three; no estimate possible)")
    lo, hi = wilson_interval(k, n)
    flag = "" if k >= MIN_HITS_FOR_ESTIMATE else f"  [k<{MIN_HITS_FOR_ESTIMATE}, not measurable]"
    return (f"{k:5d}/{n:<5d} {100 * k / n:6.2f}%   95% CI "
            f"[{100 * lo:.2f}%, {100 * hi:.2f}%]{flag}")


def report(run_dir: Path, rows: list[dict]) -> str:
    n = len(rows)
    out = [f"# {run_dir.name} — BoltzGen-scored threshold calibration", "",
           f"designs: {n}"]

    lens = _floats(rows, "num_design")
    toks = _floats(rows, "num_tokens")
    if lens:
        out.append(f"binder length: {int(min(lens))}-{int(max(lens))} "
                   f"(median {int(statistics.median(lens))})")
    if toks:
        out.append(f"complex tokens: {int(min(toks))}-{int(max(toks))} "
                   f"(median {int(statistics.median(toks))})")

    pf = [(r.get("pass_filters") or "").strip().lower() for r in rows]
    n_pf = sum(1 for x in pf if x in {"true", "1", "yes"})
    out += ["", "## BoltzGen's own composite filter", "",
            f"`pass_filters`: {_rate_line(n_pf, n)}",
            "",
            "Reported separately because it is opaque and modality-dependent: "
            "on the archived cyclic-peptide run it alone discarded 99 of 100 "
            "designs. Gating on it is a decision, not a default."]

    out += ["", "## Per-gate ladders (each gate alone)", ""]
    for col, cmp, rungs in _LADDERS:
        vals = _floats(rows, col)
        if not vals:
            out += [f"### `{col}` — column absent", ""]
            continue
        out += [f"### `{col}` {cmp} x",
                f"distribution: min {min(vals):.3f} / median "
                f"{statistics.median(vals):.3f} / max {max(vals):.3f}", ""]
        for lim in rungs:
            k = sum(1 for r in rows if _passes(r, col, cmp, lim) is True)
            mark = "  <- SHIPPED" if _SHIPPED.get(col) == lim else ""
            out.append(f"  {cmp} {lim:<6g} {_rate_line(k, n)}{mark}")
        out.append("")

    # Joint rate — the only one that sizes a campaign.
    out += ["## Joint rate (all three gates together, BoltzGen filter aside)", ""]
    for iptm_bar in (0.40, 0.45, 0.50, 0.55, 0.60):
        for ipae_cap in (10.0, 15.0):
            k = sum(1 for r in rows
                    if _passes(r, "design_to_target_iptm", ">=", iptm_bar) is True
                    and _passes(r, "min_design_to_target_pae", "<=", ipae_cap) is True
                    and _passes(r, "complex_plddt", ">=", 0.70) is True)
            out.append(f"  iptm>={iptm_bar:.2f} & ipae<={ipae_cap:<5g} & plddt>=0.70  "
                       f"{_rate_line(k, n)}")
    out += ["",
            "The strictest rung clearing "
            f"{MIN_HITS_FOR_ESTIMATE} hits is the most defensible bar for this "
            "modality on this target — the same rule `campaign_calibration."
            "calibrate` applies when it picks `suggested_bar`."]
    return "\n".join(out)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", type=Path, help="a completed BoltzGen run")
    ap.add_argument("--compare", type=Path, default=None,
                    help="a second run to report alongside (e.g. the other "
                         "modality on the same target)")
    ap.add_argument("--out", type=Path, default=None,
                    help="also write the report here")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    chunks = [report(args.run_dir, read_metrics(args.run_dir))]
    if args.compare:
        chunks.append(report(args.compare, read_metrics(args.compare)))
    text = "\n\n---\n\n".join(chunks)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
