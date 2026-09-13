#!/usr/bin/env python
"""End-to-end pipeline test: cGAS-STING cancer therapeutics.

Drives the full pathway → literature → structure pipeline and the binder
track it bridges into, with capture_traces=True so every LLM stage dumps its
full conversation (raw JSON + rendered markdown) under
<run_dir>/<NN_stage>/traces/.

There are no campaign-size flags. `--pilot`/`--production` used to write
`design.pilot`/`design.production`, which only the retired boltzgen_legacy
chain read, so they had been inert since that chain went; those config keys
are now gone too (step 4 of LEGACY_RETIREMENT_SCOPE.md) and the flags with
them. Both live engines size themselves from a measured calibration run —
use `run_pipeline.py --stop-after calibration` / `--n-batches` to bound GPU
spend, not this driver.

Usage:
    .venv/bin/python scripts/test_e2e_cgas_sting.py
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


PROMPT = "Design cancer therapeutics targeting the cGAS-STING pathway."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--run-dir",
        type=Path,
        default=_ROOT / "outputs" / "e2e_cgas_sting",
    )
    ap.add_argument("--project", type=str, default="cgas_sting",
                    help="Project slug for the persistent manifest — required "
                         "by the runner whenever the PPI track uses the "
                         "foundry design engine (the config default).")
    ap.add_argument("--provider", choices=["gemini", "claude"], default="gemini",
                    help="LLM provider for every stage. Default: gemini.")
    args = ap.parse_args()

    from src.project import Project

    project = Project.create(args.project, query=PROMPT, workflow="ppi")
    rnd = project.new_round(note=PROMPT[:80])
    round_id = rnd["run_id"]
    run_dir = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"[setup] project: {project.slug}  round: {round_id}")
    print(f"[setup] run_dir: {run_dir}")

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())

    # No config overrides. The four `design.thresholds` values this used to
    # loosen gated the retired legacy analysis stage only; the gate a bridged
    # run actually passes through is `design.boltzgen_ranking.thresholds`
    # (BoltzGen) or `design.binder_ranking.thresholds` (foundry), and
    # re-pointing these writes at either would be inventing a new sizing
    # behaviour for a smoke-test driver rather than removing a dead one. The
    # shipped thresholds are exercised properly by
    # tests/test_design_ranking_regression.py over an archived campaign, on
    # CPU, which is where that belongs anyway.

    runner = PipelineRunner(
        config=cfg,
        provider=args.provider,
        output_dir=run_dir,
        max_iter=30,
        max_tokens=120_000,
        capture_traces=True,  # dump per-stage trace_raw.json + trace_rendered.md
        project=project,
        round_id=round_id,
    )

    print(f"[start] {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[query] {PROMPT}")
    print()

    t0 = time.time()
    try:
        result = runner.run(
            query=PROMPT,
            start_from="pathway",
            auto_mode=True,         # no UI pauses; run end-to-end
        )
    except PipelinePausedError as exc:
        elapsed = time.time() - t0
        print(f"\n[pause] {exc.pause_point} after {elapsed:.0f}s")
        print(f"  payload: {exc.payload}")
        # auto_mode=True skips the UI pauses, but the binder-track stages
        # still raise the ones that are CHECKPOINTS rather than prompts —
        # `calibration_verdict` above all, which is where a campaign is sized
        # and is meant to stop for a human. Dump and exit non-zero so the
        # caller sees which one, and resume with `run_pipeline.py
        # --start-from production --project <slug>`.
        return 1
    except PipelineError as exc:
        elapsed = time.time() - t0
        print(f"\n[error] {exc} after {elapsed:.0f}s")
        return 1
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"\n[unexpected error] {type(exc).__name__}: {exc} after {elapsed:.0f}s")
        import traceback
        traceback.print_exc()
        return 1

    elapsed = time.time() - t0

    print()
    print("=" * 72)
    print(f"END-TO-END PASS  ({elapsed:.0f}s total)")
    print(f"  run_dir:          {result.run_dir}")
    print(f"  stages_completed: {result.stages_completed}")
    print(f"  go_recommendation:{result.go_recommendation}")
    print(f"  go_rationale:     {result.go_rationale[:200]}")
    print(f"  target_complex:   {result.target_complex}")
    print(f"  pdb_id:           {result.pdb_id}")
    print()
    print("Artifacts (excluding large boltzgen intermediates):")
    skip_dirs = {"intermediate_designs", "intermediate_designs_inverse_folded", "config", "previous-config-1", "previous-config-2"}
    for f in sorted(run_dir.rglob("*")):
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue
        rel = f.relative_to(run_dir)
        print(f"  {rel}  ({f.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
