#!/usr/bin/env python
"""End-to-end stress test for stages 4-5 of the design pipeline.

Drives _stage_execution and _stage_analysis against the boltzgen test YAML
(test_protein_protein.yaml → M3_crop.pdb target). Stages 0-3 are *not*
exercised — we synthesize a PipelineResult with the pieces those stages
would have produced, so we can stress-test the new code in isolation.

Overrides config to 50-design pilot + 100-design production so the full
boltzgen flow completes in minutes rather than hours. Real M3 binding
residues from the YAML (34,36,37,38,39,40 on chain A) are wired into
hotspot_residues_json so stage 5 enrichment exercises a meaningful
filter.

Usage:
    .venv/bin/python scripts/stress_test_chunk3.py [--run-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.pipeline_runner import PipelineRunner, PipelineResult, PipelinePausedError


# Real M3 hotspots from boltzgen/yamls/test_protein_protein.yaml (chain A).
HOTSPOTS = [
    {"auth_seq_id": 34, "residue": "X", "label_seq_id": 34},
    {"auth_seq_id": 36, "residue": "X", "label_seq_id": 36},
    {"auth_seq_id": 37, "residue": "X", "label_seq_id": 37},
    {"auth_seq_id": 38, "residue": "X", "label_seq_id": 38},
    {"auth_seq_id": 39, "residue": "X", "label_seq_id": 39},
    {"auth_seq_id": 40, "residue": "X", "label_seq_id": 40},
]

TEST_YAML = _ROOT.parent / "boltzgen" / "yamls" / "test_protein_protein.yaml"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--run-dir",
        type=Path,
        default=_ROOT / "outputs" / "stress_test_chunk3",
        help="Where to place the synthetic run dir. Will be wiped if it exists.",
    )
    ap.add_argument("--pilot", type=int, default=50, help="pilot num_designs")
    ap.add_argument("--production", type=int, default=100, help="production num_designs")
    args = ap.parse_args()

    run_dir = args.run_dir
    if run_dir.exists():
        print(f"[setup] wiping {run_dir}")
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    # Fake stage-3 output: drop the YAML where _stage_execution will look.
    design_dir = run_dir / "03_design_inputs"
    design_dir.mkdir()
    yaml_dest = design_dir / "test_protein_protein_boltzgen.yaml"
    shutil.copy(TEST_YAML, yaml_dest)
    print(f"[setup] design YAML staged at {yaml_dest}")

    # Loosen execution-stage config for a fast, low-design stress test.
    # Thresholds also loosened so we get a non-empty top-K even on small N.
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    cfg["design"]["pilot"]["num_designs"] = args.pilot
    cfg["design"]["pilot"]["budget"] = max(5, args.pilot // 5)
    cfg["design"]["production"]["num_designs"] = args.production
    cfg["design"]["production"]["budget"] = max(10, args.production // 5)
    cfg["design"]["pilot"]["min_completion_rate"] = 0.3
    cfg["design"]["pilot"]["min_final_fill_rate"] = 0.3
    # Threshold loosening — we want to confirm stage 5 ranks, not gate everything out.
    cfg["design"]["thresholds"]["iptm_min"] = 0.10
    cfg["design"]["thresholds"]["ipae_max"] = 25.0
    cfg["design"]["thresholds"]["hotspot_sasa_delta_min"] = 0.0
    cfg["design"]["thresholds"]["require_boltzgen_pass"] = False

    # PipelineRunner needs a provider; skill-using stages never fire in this test.
    runner = PipelineRunner(config=cfg, provider="claude", max_iter=1, max_tokens=1000)

    # Synthesize stage-1 hotspots + stage-3 modality handoff.
    result = PipelineResult(
        run_dir=run_dir,
        hotspot_residues_json=json.dumps({
            "target_chain": "A",
            "partner_chain": "B",
            "residues": HOTSPOTS,
        }),
        target_complex="M3 / designed binder",
    )
    handoff = {
        "modality": "mini_protein",
        "target_complex": "M3 / designed binder",
        "design_files": "test_protein_protein_boltzgen.yaml",
    }

    print(f"[run] stage 4 (execution) — pilot={args.pilot}, production={args.production}")
    t0 = time.time()
    try:
        runner._stage_execution(handoff, run_dir, result)
    except PipelinePausedError as exc:
        print(f"[stage 4] PAUSED: {exc.pause_point}")
        print(f"  payload: {exc.payload}")
        print("  re-running with force_production=True to continue stress test...")
        try:
            runner._stage_execution(handoff, run_dir, result, force_production=True)
        except Exception as exc2:
            print(f"[stage 4] still failed after force: {exc2}")
            return 1
    except Exception as exc:
        print(f"[stage 4] FAILED: {exc}")
        return 1
    t_exec = time.time() - t0
    print(f"[stage 4] done in {t_exec:.0f}s")

    print(f"[run] stage 5 (analysis)")
    t0 = time.time()
    try:
        runner._stage_analysis(run_dir, result)
    except Exception as exc:
        print(f"[stage 5] FAILED: {exc}")
        return 1
    t_ana = time.time() - t0
    print(f"[stage 5] done in {t_ana:.0f}s")

    print()
    print("=" * 72)
    print("STRESS TEST PASS")
    print(f"  run_dir:    {run_dir}")
    print(f"  stage4 (s): {t_exec:.0f}")
    print(f"  stage5 (s): {t_ana:.0f}")
    print(f"  total (s):  {t_exec + t_ana:.0f}")
    print()
    print(f"Artifacts:")
    for f in sorted(run_dir.rglob("*")):
        if f.is_file() and "intermediate_designs" not in f.parts:
            rel = f.relative_to(run_dir)
            print(f"  {rel}  ({f.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
