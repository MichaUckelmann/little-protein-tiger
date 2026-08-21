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


_BINDER_STAGES = (
    "target_intel", "interface", "trim", "binder_spec",
    "pilot", "calibration", "production", "binder_scoring", "binder_summary",
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Run the LittleProteinTiger design pipeline end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--query", "-q",
        required=False,
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
            # binder-workflow stages (used with --workflow binder)
            "target_intel", "interface", "trim", "binder_spec",
            "pilot", "calibration", "production", "binder_scoring",
            "binder_summary",
        ],
        default="pathway",
        dest="start_from",
        help="Stage to begin at. Default: pathway.",
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
        choices=["ppi", "binder"],
        default="ppi",
        help="Workflow track. 'ppi' (default) = literature-driven binder design; "
             "'binder' = target-name-first binder design on the local GPU "
             "(skips discovery, runs foundry, requires --project).",
    )
    p.add_argument(
        "--target",
        metavar="NAME",
        default=None,
        help=(
            "Protein to design against, e.g. --target KRAS. Binder workflow only. "
            "Makes --query optional: the query then just carries the intent "
            "(\"disrupt downstream interactions\")."
        ),
    )
    p.add_argument(
        "--detach",
        action="store_true",
        help=(
            "Binder workflow: launch each GPU stage and return immediately "
            "instead of waiting. A production campaign runs for days; resume "
            "later with --start-from <stage>. Progress: scripts/campaign_status.py"
        ),
    )
    p.add_argument(
        "--n-batches",
        type=int,
        default=None,
        dest="n_batches",
        help="Binder workflow: override the RFD3 batch count for GPU stages.",
    )
    p.add_argument(
        "--trial-sites",
        type=int, default=1, metavar="N", dest="trial_sites",
        help=(
            "Binder workflow: run a design trial against the first N candidate "
            "sites the target-intel stage proposes and compare their measured "
            "yields, instead of arguing about which epitope is better. Each "
            "extra site costs a full trial campaign."
        ),
    )
    p.add_argument(
        "--trial-backbones",
        type=int, default=300, metavar="N", dest="trial_backbones",
        help=(
            "Binder workflow: RFD3 backbones per trial (default 300). Measured "
            "on the reference campaign, 300 gave a usable rate estimate in 0/10 "
            "seeds and 1000 in 8/10 — hence --escalate-to."
        ),
    )
    p.add_argument(
        "--escalate-to",
        type=int, default=1000, metavar="N", dest="escalate_to",
        help=(
            "Binder workflow: re-run a trial at this size when it produced too "
            "few hits to size a campaign. 0 disables escalation."
        ),
    )
    p.add_argument(
        "--stop-after",
        choices=["spec", "trial"], default=None, dest="stop_after",
        help=(
            "Binder workflow: 'spec' prepares and validates everything up to "
            "the GPU and stops, so specs can be reviewed before committing "
            "days of compute; 'trial' stops after the design trial and site "
            "comparison, before a production campaign."
        ),
    )
    p.add_argument(
        "--success-metric",
        choices=["iptm", "ipsae_min"], default=None, dest="success_metric",
        help=(
            "Binder workflow: which metric sizes the campaign. 'iptm' (>0.7, "
            "target 50) is 5-20x more common than 'ipsae_min' (>0.5, target 100) "
            "and is therefore measurable from a trial-sized sample. Both are "
            "always computed; ranking uses the full composite either way."
        ),
    )
    p.add_argument(
        "--budget",
        metavar="USD",
        type=float,
        default=None,
        dest="budget",
        help=(
            "Hard cap on API spend for this PROJECT, in USD (e.g. --budget 5.00). "
            "Spend is cumulative across rounds and resumes; a stage whose "
            "projected cost would pass the cap pauses the run instead of "
            "starting. Re-run with a higher --budget to continue. "
            "Governs API cost only, not GPU time or disk."
        ),
    )
    p.add_argument(
        "--budget-mode",
        choices=["hard", "warn"],
        default="hard",
        dest="budget_mode",
        help="'hard' (default) pauses on overrun; 'warn' only logs.",
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

    is_binder = args.workflow == "binder"

    if not args.query and not (is_binder and args.target):
        parser.error("--query is required (or --target, for --workflow binder).")

    try:
        # For the binder track the target IS the objective when no query is
        # given: "design binders against KRAS" adds nothing --target does not.
        query = _resolve_query(args.query) if args.query else (
            f"Design binders against {args.target}.")
    except FileNotFoundError as exc:
        parser.error(str(exc))

    if not query.strip():
        parser.error("Query is empty.")

    if is_binder:
        if not args.project:
            parser.error(
                "--workflow binder requires --project: the track iterates in "
                "rounds, and the manifest is what makes a multi-day GPU campaign "
                "resumable.")
        if args.start_from == "pathway":
            args.start_from = "target_intel"
        if args.start_from not in _BINDER_STAGES:
            parser.error(
                f"--start-from {args.start_from!r} is not a binder stage; "
                f"choose one of {', '.join(_BINDER_STAGES)}.")
    elif args.target:
        parser.error("--target applies to --workflow binder only.")

    # Validate resume arguments
    if (not is_binder and args.start_from != "pathway"
            and not args.pdb and not args.context):
        parser.error(
            f"--start-from {args.start_from!r} requires either --pdb or --context "
            "(a path to a prior stage output file)."
        )

    config = _load_config()
    if is_binder and args.success_metric:
        config.setdefault("design", {}).setdefault(
            "binder_ranking", {})["success_metric"] = args.success_metric

    from src.pipeline_runner import (
        PipelineBlockedError,
        PipelinePausedError,
        PipelineRunner,
    )

    # Optional persistent project. A new round is started for a fresh run
    # (start-from pathway); a resume reuses the latest round.
    project = None
    round_id = None
    if args.project:
        from src.project import Project
        project = Project.create(args.project, query=query, workflow=args.workflow)
        _first_stage = "target_intel" if args.workflow == "binder" else "pathway"
        if args.start_from == _first_stage or project.latest_round() is None:
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
        budget_usd=args.budget,
        budget_mode=args.budget_mode,
        detach=args.detach,
        n_batches=args.n_batches,
        trial_sites=args.trial_sites,
        trial_backbones=args.trial_backbones,
        escalate_to=(args.escalate_to or None),
        stop_after=args.stop_after,
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
            target=args.target,
        )
    except PipelinePausedError as exc:
        # Currently: budget_exceeded. Paused, not failed — state is checkpointed.
        print()
        print("=" * 60)
        print(f"PIPELINE PAUSED: {exc.pause_point}")
        print("=" * 60)
        for key, val in (exc.payload or {}).items():
            print(f"  {key}: {val}")
        print("=" * 60)
        return 4
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
    if runner._ledger is not None and runner._ledger.entries:
        led = runner._ledger
        cap = f" / ${led.cap_usd:.2f} cap" if led.cap_usd is not None else ""
        print(f"  API spend:       ${led.spent_usd:.4f}{cap}")
        for stage_name, usd in sorted(led.by_stage().items(), key=lambda kv: -kv[1]):
            print(f"    {stage_name:<18} ${usd:.4f}")
        if led.has_unpriced:
            print("    (UNDERESTIMATE — a model used has no price table entry)")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
