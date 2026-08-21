#!/usr/bin/env python3
"""
Generate (or regenerate) the illustrated HTML campaign report for a binder run.

Deterministic — no LLM call, no GPU, just reads what the pipeline already
wrote and renders a self-contained report.html with an embedded Mol*
structure explorer. Safe to re-run any time a campaign has progressed
(e.g. after `binder_scoring` completes and supersedes a trial-only report).

    # by path — works for either a top-level or a per-site binder dir
    scripts/generate_binder_report.py \\
        projects/trial_kras/runs/round-1/binder/sites/raf1_rbd/binder

    # by project/round/site
    scripts/generate_binder_report.py --project trial_kras --round round-1 --site raf1_rbd
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402
from loguru import logger  # noqa: E402

from src.binder_report import ReportError, build_report  # noqa: E402
from src.project import Project  # noqa: E402


def _resolve_binder_dir(args: argparse.Namespace) -> Path:
    if args.binder_dir:
        return Path(args.binder_dir)
    if not args.project:
        raise SystemExit("pass either a binder_dir path or --project")
    project = Project(Project.path_for(args.project))
    run_dir = project.run_dir(args.round)
    binder_dir = run_dir / "binder"
    if args.site:
        binder_dir = binder_dir / "sites" / args.site / "binder"
    return binder_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binder_dir", nargs="?", type=Path,
                    help="the run's binder/ directory (top-level or sites/<id>/binder)")
    ap.add_argument("--project", help="project slug (alternative to a bare path)")
    ap.add_argument("--round", default="round-1", help="round id (default: round-1)")
    ap.add_argument("--site", help="site id, if this project ran a multi-site trial")
    ap.add_argument("--out", type=Path, default=None, help="output path (default: <binder_dir>/report.html)")
    ap.add_argument("--top-n", type=int, default=5, help="how many top designs to embed structures for")
    args = ap.parse_args(argv)

    binder_dir = _resolve_binder_dir(args)
    if not binder_dir.is_dir():
        logger.error(f"not a directory: {binder_dir}")
        return 1

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    try:
        out = build_report(binder_dir, out_path=args.out, cfg=cfg, top_n_structures=args.top_n)
    except ReportError as e:
        logger.error(str(e))
        return 1

    logger.info(f"report -> {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
