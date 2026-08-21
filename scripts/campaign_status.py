#!/usr/bin/env python3
"""
Progress of a running (or finished) foundry binder campaign.

    scripts/campaign_status.py projects/<slug>/runs/round-1/binder/campaign/production
    scripts/campaign_status.py projects/<slug>            # every stage it finds

Reads disk, not logs. A production stage directory holds 50-100k entries, so
every count uses os.scandir — `ls | wc -l` in a pipeline silently reports 0,
which reads as "this stage produced nothing".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.foundry_runner import (  # noqa: E402
    CampaignPlan, FoundryPaths, progress, render_progress,
)
from src.job_registry import JobRegistry  # noqa: E402

_STAGES = ("pilot", "calibration", "production")


def _plan_for(paths: FoundryPaths) -> CampaignPlan:
    """Reload the plan written at launch, or fall back to what is on disk."""
    f = paths.campaign_dir / "plan.json"
    if f.exists():
        try:
            return CampaignPlan(**json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            pass
    from src.foundry_runner import count_mpnn, count_rfd3

    return CampaignPlan(
        mode="unknown", n_batches=0, diffusion_batch_size=0, n_seq=0,
        expected_rfd3=count_rfd3(paths.rfd3_dir), prefilter_rate=0.0,
        expected_mpnn=count_mpnn(paths.mpnn_dir),
        expected_rf3=count_mpnn(paths.mpnn_dir),
        est_gpu_hours=0.0, est_disk_gb=0.0, free_disk_gb=0.0)


def _report(campaign_dir: Path) -> None:
    paths = FoundryPaths.under(campaign_dir)
    plan = _plan_for(paths)
    registry = (JobRegistry(paths.registry_path)
                if paths.registry_path.exists() else None)
    print(f"\n=== {campaign_dir} ===")
    print(render_progress(progress(paths, plan, registry)))


def _find_campaigns(root: Path) -> list[Path]:
    if (root / "rf3_out").is_dir() or (root / "rfd3").is_dir():
        return [root]
    found = []
    for stage in _STAGES:
        for cand in root.rglob(f"campaign/{stage}"):
            if cand.is_dir():
                found.append(cand)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path,
                    help="A campaign stage directory, or a project/run to search.")
    args = ap.parse_args(argv)

    campaigns = _find_campaigns(args.path)
    if not campaigns:
        print(f"No campaign directories under {args.path}", file=sys.stderr)
        return 1
    for c in campaigns:
        _report(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
