#!/usr/bin/env python
"""End-to-end pipeline test: cGAS-STING cancer therapeutics.

Drives the full pathway → literature → structure → design → execution → analysis
→ summary pipeline with capture_traces=True so every LLM stage dumps its full
conversation (raw JSON + rendered markdown) under <run_dir>/<NN_stage>/traces/.

BoltzGen pilot/production sizes are dialed down to 50/100 (same as chunk-3
stress test) so total wall time is bounded to ~1h.

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
    ap.add_argument("--pilot", type=int, default=50)
    ap.add_argument("--production", type=int, default=100)
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

    # Bound the GPU spend for a test. Same sizes as the chunk-3 stress test.
    cfg["design"]["pilot"]["num_designs"] = args.pilot
    cfg["design"]["pilot"]["budget"] = max(5, args.pilot // 5)
    cfg["design"]["production"]["num_designs"] = args.production
    cfg["design"]["production"]["budget"] = max(10, args.production // 5)

    # Loosen the analysis-stage hard filters for the test — we want the
    # pipeline to produce a non-empty top-K even if the small batch yields
    # weak metrics. Real production runs keep the defaults from config.
    cfg["design"]["thresholds"]["iptm_min"] = 0.10
    cfg["design"]["thresholds"]["ipae_max"] = 25.0
    cfg["design"]["thresholds"]["hotspot_sasa_delta_min"] = 0.0
    # require_boltzgen_pass is LEFT AT THE SHIPPED DEFAULT (True). It was
    # overridden to False here, which discarded the single most informative
    # column BoltzGen writes: `pass_filters` is dominated by its
    # design-vs-refold RMSD check (<= 2 A), and on e2e_cgas_sting 0 of the 20
    # designs this pipeline reported had passed it -- BoltzGen was signalling
    # "none of these are acceptable" and the override suppressed it.
    #
    # EXPECTED CONSEQUENCE, not a regression: on this target only 1 of 100
    # designs passes, so the ranking path downstream runs with n=1 and MMR /
    # the backbone cap / the top-K are all degenerate. That is a weaker smoke
    # test than the old 100-survivor run, and the right place to exercise the
    # shipped thresholds properly is a CPU-only regression over an archived
    # campaign, not a GPU e2e.

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
        # auto_mode=True should not pause normally; this is a real fault
        # (e.g. pilot_failed). Dump and exit non-zero so the caller sees it.
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
