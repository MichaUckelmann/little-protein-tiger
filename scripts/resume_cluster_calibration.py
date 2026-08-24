#!/usr/bin/env python3
"""
Resume a cluster-staged binder calibration: check completion, and if the
SLURM run finished, score it and write the calibration verdict.

Why this exists rather than `scripts/run_pipeline.py --start-from calibration`:
that CLI path resumes the TOP-LEVEL `binder/` stage files, but a per-site
trial's spec/trim/calibration live under `binder/sites/<site_id>/binder/`
instead — `_run_site_trials` only re-enters through `--trial-sites N` with
N > 1 (or `--stop-after trial`), and even then recomputes `n_batches` from
`--trial-backbones`, which must exactly match what was originally staged or
the completion check (`plan.expected_rf3`) silently checks against the wrong
target.

The CLI has since grown real per-site resume support
(`run_pipeline.py --start-from calibration --site <id> --n-batches <n>`) —
this script and that flag are now both thin callers of the same
`PipelineRunner.resume_site_stage`, so a fix to the underlying logic never
has to be made twice. This script remains for backward compatibility and
for anyone scripting around it directly.

Safe to run repeatedly: if the cluster run hasn't finished, this pauses again
(same PipelinePausedError as the original staging call) without touching
anything. Idempotent either way.

Usage:
    python scripts/resume_cluster_calibration.py --project gem_vegf_a \\
        --site vegfa_flt1_primary --n-batches 909 --n-gpus 6
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml
from dotenv import load_dotenv
from loguru import logger

load_dotenv(_ROOT / ".env")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, metavar="SLUG")
    ap.add_argument("--round", default=None,
                    help="Round id, e.g. round-1. Default: the project's latest round.")
    ap.add_argument("--site", required=True, metavar="SITE_ID",
                    help="Site directory name under binder/sites/, e.g. vegfa_flt1_primary")
    ap.add_argument("--stage", default="calibration", choices=["calibration"],
                    help="Only calibration is wired up (production has no "
                         "single owning method the same way — ask if you need it).")
    ap.add_argument("--n-batches", type=int, required=True,
                    help="MUST match the n_batches the campaign was staged with "
                         "(printed when you staged it, or read plan.n_batches "
                         "from the campaign's launch.sh NB= line).")
    ap.add_argument("--n-gpus", type=int, default=None,
                    help="Overrides design.cluster.n_gpus (cosmetic once staged — "
                         "does not resize an already-submitted job).")
    ap.add_argument("--target", default="", help="Free-text query, cosmetic only.")
    args = ap.parse_args(argv)

    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    if args.n_gpus:
        config.setdefault("design", {}).setdefault("cluster", {})["n_gpus"] = args.n_gpus

    from src.project import Project
    from src.pipeline_runner import (
        PipelineError, PipelinePausedError, PipelineRunner,
    )

    project = Project.create(args.project, query=args.target or f"Resume {args.project}",
                             workflow="binder")
    round_id = args.round or project.latest_round()["run_id"]
    run_dir = project.run_dir(round_id)

    runner = PipelineRunner(config=config, project=project, round_id=round_id,
                            workflow="binder", compute="cluster")

    try:
        calib = runner.resume_site_stage(
            run_dir, args.site, args.stage, args.n_batches, attach=True)
    except PipelinePausedError as exc:
        print()
        print("=" * 60)
        print(f"STILL WAITING: {exc.pause_point}")
        print("=" * 60)
        for key, val in (exc.payload or {}).items():
            print(f"  {key}: {val}")
        print("=" * 60)
        return 3
    except PipelineError as exc:
        logger.error(str(exc))
        return 1

    dirs = runner._binder_dirs(run_dir)
    site_dirs = runner._binder_dirs(dirs["sites"] / args.site)
    res = calib["result"]
    print()
    print("=" * 60)
    print(f"CALIBRATION VERDICT: {res.verdict}")
    print("=" * 60)
    print(res.verdict_reason)
    print(f"report: {site_dirs['binder'] / runner._BINDER_STAGE_FILES['calibration']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
