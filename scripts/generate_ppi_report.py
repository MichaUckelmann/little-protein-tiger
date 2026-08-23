#!/usr/bin/env python3
"""
Generate (or regenerate) the illustrated HTML run report for a PPI-track run.

Deterministic — no LLM call, no GPU, just reads what the pipeline already
wrote (00_pathway.md .. 06_summary.md, 05_ranking/*) and renders a
self-contained report.html with an embedded Mol* structure explorer. Safe
to re-run any time a run has progressed (e.g. after `summary` completes and
supersedes an analysis-only report).

    # legacy outputs/<slug>_<date>/ layout
    scripts/generate_ppi_report.py outputs/e2e_cgas_sting

    # project layout
    scripts/generate_ppi_report.py --project my_project --round round-1
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

from src.ppi_report import ReportError, build_report  # noqa: E402
from src.project import Project  # noqa: E402


def _resolve_run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir)
    if not args.project:
        raise SystemExit("pass either a run_dir path or --project")
    project = Project(Project.path_for(args.project))
    return project.run_dir(args.round)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", type=Path,
                    help="the run's output directory (legacy outputs/<slug>_<date>/ or a project's runs/<round>/)")
    ap.add_argument("--project", help="project slug (alternative to a bare path)")
    ap.add_argument("--round", default="round-1", help="round id (default: round-1)")
    ap.add_argument("--out", type=Path, default=None, help="output path (default: <run_dir>/report.html)")
    ap.add_argument("--top-n", type=int, default=5, help="how many top designs to embed structures for")
    args = ap.parse_args(argv)

    run_dir = _resolve_run_dir(args)
    if not run_dir.is_dir():
        logger.error(f"not a directory: {run_dir}")
        return 1

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    try:
        out = build_report(run_dir, out_path=args.out, cfg=cfg, top_n_structures=args.top_n)
    except ReportError as e:
        logger.error(str(e))
        return 1

    logger.info(f"report -> {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
