#!/usr/bin/env python3
"""
CLI entry point for the LittleProteinTiger programmatic design pipeline.

Chains four stages automatically:
  pathway-expert -> complex-structure-analysis -> molecular-biology-expert -> protein-design-script

Each stage writes a report to the run directory and passes a machine-readable
'### PIPELINE HANDOFF' block to the next stage.

Examples
--------
# Full pipeline from a disease query
python scripts/run_pipeline.py \\
    --query "design PPI inhibitors for antibiotic resistant S. aureus"

# Skip pathway stage if PDB is known
python scripts/run_pipeline.py \\
    --query "design cyclic peptide inhibitor for Hla/ADAM10" \\
    --pdb 4U6V

# Resume from structure stage with prior pathway report
python scripts/run_pipeline.py \\
    --query "design inhibitors for YAP/TEAD in mesothelioma" \\
    --start-from structure \\
    --context outputs/yap_tead_2025-01-01/00_pathway.md \\
    --output-dir outputs/yap_tead_2025-01-01/

# Use Gemini instead of Claude
python scripts/run_pipeline.py \\
    --query "Hippo pathway inhibitors for mesothelioma" \\
    --provider gemini

# Query from a text file
python scripts/run_pipeline.py --query @queries/my_target.txt
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


def _load_config() -> dict:
    config_path = _ROOT / "config.yaml"
    if not config_path.exists():
        logger.warning(f"config.yaml not found at {config_path} — using empty config.")
        return {}
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_query(raw: str) -> str:
    """Expand @file.txt references."""
    if raw.startswith("@"):
        path = Path(raw[1:])
        if not path.exists():
            # Try relative to cwd, then repo root
            path = _ROOT / raw[1:]
        if not path.exists():
            raise FileNotFoundError(f"Query file not found: {raw[1:]!r}")
        return path.read_text(encoding="utf-8").strip()
    return raw


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Run the LittleProteinTiger design pipeline end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--query", "-q",
        required=True,
        metavar="TEXT|@FILE",
        help=(
            "User query string (e.g. 'design PPI inhibitors for MRSA') "
            "or @path/to/file.txt to read from a file."
        ),
    )
    p.add_argument(
        "--pdb",
        metavar="ACCESSION",
        default=None,
        help=(
            "Known PDB accession (e.g. 4U6V). Skips the pathway-expert stage "
            "and starts directly at complex-structure-analysis."
        ),
    )
    p.add_argument(
        "--start-from",
        choices=[
            "pathway", "structure", "literature", "design",
            # enzyme-workflow stages (used with --workflow enzyme)
            "substrate", "theozyme", "theozyme_diagnose", "grafting",
            "enzyme_design", "enzyme_validation",
        ],
        default="pathway",
        dest="start_from",
        help="Stage to begin at. Default: pathway. For --workflow enzyme, resume "
             "after an external step with e.g. --start-from theozyme_diagnose "
             "(after ORCA) or --start-from enzyme_validation (after the GPU pilot).",
    )
    p.add_argument(
        "--context",
        metavar="PATH",
        default=None,
        type=Path,
        help=(
            "Path to a prior stage output .md file. Required when --start-from "
            "is not 'pathway' and --pdb is not given."
        ),
    )
    p.add_argument(
        "--output-dir",
        metavar="PATH",
        default=None,
        type=Path,
        dest="output_dir",
        help=(
            "Override the default run output directory. "
            "Default: outputs/<query_slug>_<date>/."
        ),
    )
    p.add_argument(
        "--project",
        metavar="SLUG",
        default=None,
        help=(
            "Persistent project name. Creates/loads projects/<slug>/ with a "
            "manifest.json and per-round run dirs. Stage outputs + state are "
            "tracked there so an iterative campaign can be resumed. Omit for the "
            "legacy one-shot outputs/<slug>_<date>/ layout."
        ),
    )
    p.add_argument(
        "--workflow",
        choices=["ppi", "enzyme"],
        default="ppi",
        help="Workflow track. 'ppi' (default) = binder/inhibitor design; "
             "'enzyme' = de novo enzyme active-site design.",
    )
    p.add_argument(
        "--provider",
        choices=["claude", "gemini"],
        default="claude",
        help="LLM provider. Default: claude.",
    )
    p.add_argument(
        "--model-id",
        metavar="MODEL",
        default=None,
        dest="model_id",
        help="Override model ID (e.g. claude-opus-4-6). Default: provider default.",
    )
    p.add_argument(
        "--max-iter",
        type=int,
        default=30,
        metavar="N",
        dest="max_iter",
        help="Maximum LLM iterations per stage. Default: 30.",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=100_000,
        metavar="N",
        dest="max_tokens",
        help="Abort guard: stage aborts if input token count exceeds this. Default: 100000.",
    )
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        query = _resolve_query(args.query)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    if not query.strip():
        parser.error("Query is empty.")

    # Validate resume arguments
    if args.start_from != "pathway" and not args.pdb and not args.context:
        parser.error(
            f"--start-from {args.start_from!r} requires either --pdb or --context "
            "(a path to a prior stage output file)."
        )

    config = _load_config()

    from src.pipeline_runner import (
        PipelineBlockedError,
        PipelineExternalStepError,
        PipelineRunner,
    )

    # Optional persistent project. A new round is started for a fresh run
    # (start-from pathway); a resume reuses the latest round.
    project = None
    round_id = None
    if args.project:
        from src.project import Project
        project = Project.create(args.project, query=query, workflow=args.workflow)
        if args.start_from == "pathway" or project.latest_round() is None:
            rnd = project.new_round(note=query[:80])
        else:
            rnd = project.latest_round()
        round_id = rnd["run_id"]
        logger.info(
            f"Project: {project.slug}  round: {round_id}  "
            f"dir: {project.run_dir(round_id)}"
        )

    runner = PipelineRunner(
        config=config,
        provider=args.provider,
        model_id=args.model_id,
        output_dir=args.output_dir,
        max_iter=args.max_iter,
        max_tokens=args.max_tokens,
        project=project,
        round_id=round_id,
        workflow=args.workflow,
    )

    logger.info(f"Query: {query[:120]}{'...' if len(query) > 120 else ''}")
    logger.info(f"Provider: {args.provider}  |  Model: {runner.model_id}")
    if args.pdb:
        logger.info(f"PDB override: {args.pdb}")
    if args.start_from != "pathway":
        logger.info(f"Resuming from stage: {args.start_from}")

    try:
        result = runner.run(
            query=query,
            start_from=args.start_from,
            pdb_id=args.pdb,
            context_file=args.context,
        )
    except PipelineExternalStepError as exc:
        # Enzyme workflow: a heavy external compute step must run outside LPT.
        print()
        print("=" * 60)
        print(f"EXTERNAL STEP REQUIRED: {exc.step_id}")
        print("=" * 60)
        print(exc.instructions)
        print("\nInputs written:")
        for p in exc.inputs:
            print(f"  {p}")
        print("\nExpected outputs (drop here, then resume):")
        for p in exc.expected_outputs:
            print(f"  {p}")
        print(f"\nResume with: --start-from {exc.resume_stage}")
        print("=" * 60)
        return 3
    except PipelineBlockedError as exc:
        logger.error(f"Pipeline blocked — user input required:\n  {exc}")
        logger.info(
            "Re-run with --pdb <accession> once you have identified the structure, "
            "or add papers via `python scripts/fetch_papers.py` and retry."
        )
        return 2
    except Exception as exc:
        logger.error(f"Pipeline failed: {exc}")
        return 1

    # ── Summary ─────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Run dir:         {result.run_dir}")
    print(f"  Target complex:  {result.target_complex or 'unknown'}")
    print(f"  PDB ID:          {result.pdb_id or 'unknown'}")
    print(f"  Stages done:     {', '.join(result.stages_completed) or 'none'}")
    print(f"  GO/NO-GO:        {result.go_recommendation}")
    if result.go_rationale:
        print(f"  Rationale:       {result.go_rationale}")
    if result.design_files:
        print(f"  Design files:")
        for f in result.design_files:
            print(f"    {f}")
    if result.error:
        print(f"  ERROR:           {result.error}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
