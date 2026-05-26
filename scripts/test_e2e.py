#!/usr/bin/env python
"""End-to-end pipeline test for any user prompt.

Drives the full pathway → literature → structure → design → execution →
analysis → summary pipeline with capture_traces=True so every LLM stage
dumps its full conversation under <run_dir>/traces/<stage>/.

BoltzGen pilot/production sizes can be dialed down via --pilot/--production
for fast verification runs.

Usage:
    .venv/bin/python scripts/test_e2e.py \\
        --prompt "Design cancer therapeutics targeting the YAP-TEAD interaction." \\
        --slug yap_tead
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.pipeline_runner import (
    PipelineRunner,
    PipelineError,
    PipelinePausedError,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", type=str, required=True,
                    help="Free-text design objective passed to pathway-expert.")
    ap.add_argument("--slug", type=str, required=True,
                    help="Short slug for the run dir name (e.g. 'yap_tead').")
    ap.add_argument("--pilot", type=int, default=50)
    ap.add_argument("--production", type=int, default=100)
    ap.add_argument("--no-trace", action="store_true",
                    help="Skip per-stage trace dumps.")
    ap.add_argument("--pathway-mode", choices=["standard", "wildcard"], default=None,
                    dest="pathway_mode",
                    help="Stage-0 skill: 'standard' (pathway-expert, validated-target-biased) "
                         "or 'wildcard' (wildcard-expert, novelty-driven). "
                         "Default: read from config.yaml (design.pathway.mode).")
    args = ap.parse_args()

    run_dir = _ROOT / "outputs" / f"e2e_{args.slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"[setup] run_dir: {run_dir}")

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())

    cfg["design"]["pilot"]["num_designs"] = args.pilot
    cfg["design"]["pilot"]["budget"] = max(5, args.pilot // 5)
    cfg["design"]["production"]["num_designs"] = args.production
    cfg["design"]["production"]["budget"] = max(10, args.production // 5)

    # Loosen analysis-stage hard filters so the top-K is non-empty even on
    # small batches with weak metrics. Production runs keep config defaults.
    cfg["design"]["thresholds"]["iptm_min"] = 0.10
    cfg["design"]["thresholds"]["ipae_max"] = 25.0
    cfg["design"]["thresholds"]["hotspot_sasa_delta_min"] = 0.0
    cfg["design"]["thresholds"]["require_boltzgen_pass"] = False

    runner = PipelineRunner(
        config=cfg,
        provider="claude",
        output_dir=run_dir,
        max_iter=30,
        max_tokens=120_000,
        capture_traces=not args.no_trace,
        pathway_mode=args.pathway_mode or "standard",
    )

    print(f"[start]  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[query]  {args.prompt}")
    print(f"[budget] pilot={args.pilot}, production={args.production}")
    print()

    t0 = time.time()
    try:
        result = runner.run(
            query=args.prompt,
            start_from="pathway",
            auto_mode=True,
        )
    except PipelinePausedError as exc:
        elapsed = time.time() - t0
        print(f"\n[pause] {exc.pause_point} after {elapsed:.0f}s")
        print(f"  payload: {exc.payload}")
        return 1
    except PipelineError as exc:
        elapsed = time.time() - t0
        print(f"\n[error] {exc} after {elapsed:.0f}s")
        return 1
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"\n[unexpected] {type(exc).__name__}: {exc} after {elapsed:.0f}s")
        import traceback
        traceback.print_exc()
        return 1

    elapsed = time.time() - t0
    print()
    print("=" * 72)
    print(f"END-TO-END PASS  ({elapsed:.0f}s = {elapsed/60:.1f} min)")
    print(f"  run_dir:           {result.run_dir}")
    print(f"  stages_completed:  {result.stages_completed}")
    print(f"  go_recommendation: {result.go_recommendation}")
    print(f"  go_rationale:      {(result.go_rationale or '')[:200]}")
    print(f"  target_complex:    {result.target_complex}")
    print(f"  pdb_id:            {result.pdb_id}")
    print()
    skip = {"intermediate_designs", "intermediate_designs_inverse_folded",
            "config", "previous-config-1", "previous-config-2"}
    print("Artifacts (excluding boltzgen intermediates):")
    for f in sorted(run_dir.rglob("*")):
        if not f.is_file() or any(part in skip for part in f.parts):
            continue
        rel = f.relative_to(run_dir)
        print(f"  {rel}  ({f.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
