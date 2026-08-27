#!/usr/bin/env python3
"""
CLI entry point for the LittleProteinTiger programmatic design pipeline.

Chains the stages automatically, in PipelineRunner.STAGE_ORDER order:
  pathway-expert -> molecular-biology-expert -> complex-structure-analysis
    -> the design backend selected by design.backend / --design-engine
       (foundry by default: bridges into the binder track's RFD3 ->
       solubleMPNN -> RF3 stages; boltzgen: protein-design-script ->
       design_runner -> ranking -> design-analyst)

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
from src.env_config import load_env
from loguru import logger

load_env(_ROOT / ".env")


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
            # ppi-workflow stages, in PipelineRunner.STAGE_ORDER order
            "pathway", "literature", "structure", "design",
            "execution", "analysis", "summary",
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
        "--modality",
        choices=["mini_protein", "cyclic_peptide"],
        default="mini_protein",
        help=(
            "What to design (default: mini_protein, 70-86 residues). "
            "'cyclic_peptide' (12-15 residues) is OPT-IN: cyclic peptides need "
            "specialised synthesis, cost substantially more, and have a thinner "
            "experimental record than mini-protein binders. Choosing it also "
            "selects --design-engine boltzgen, since RFD3/foundry has no "
            "cyclic-peptide path. Whatever the LLM stages propose, this decides."
        ),
    )
    p.add_argument(
        "--design-engine",
        choices=["boltzgen", "foundry"],
        default=None,
        dest="design_engine",
        help=(
            "--workflow ppi only (--workflow binder always runs foundry "
            "regardless of this). 'foundry' (the default) hands the "
            "PPI-discovered target off to the same RFD3->solubleMPNN->RF3 "
            "stage machine --workflow binder uses, right after the PPI "
            "structure stage — it requires --project, since it enters "
            "multi-day GPU stages. 'boltzgen' selects the older PPI design/"
            "execution/analysis path unchanged, and is selected automatically "
            "by --modality cyclic_peptide, which RFD3 cannot build. Default "
            "without this flag: design.backend in config.yaml (itself "
            "'foundry' unless changed)."
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
        "--compute",
        choices=["auto", "local", "cluster"],
        default="auto",
        help=(
            "Binder workflow GPU stages: 'auto' (default) runs pilot/"
            "calibration locally, then at the calibration gate compares the "
            "pessimistic single-GPU production estimate against "
            "--max-local-hours and picks 'local' or 'cluster' for the "
            "production stage only. 'local' forces every GPU stage onto "
            "this workstation's GPU regardless of size; 'cluster' forces "
            "every GPU stage to stage inputs + a launch script onto shared "
            "storage (src/cluster_runner.py, design.cluster in "
            "config.yaml) and pause for a human to submit — this machine "
            "cannot reach a SLURM scheduler directly."
        ),
    )
    p.add_argument(
        "--max-local-hours",
        type=float,
        default=None,
        dest="max_local_hours",
        help=(
            "--compute auto only: max estimated single-GPU production hours "
            "before the pipeline stages a cluster package instead of running "
            "locally (default: design.foundry.max_local_hours in "
            "config.yaml, currently 48)."
        ),
    )
    p.add_argument(
        "--n-gpus",
        type=int,
        default=None,
        dest="n_gpus",
        help=(
            "Cluster compute only: GPUs available to the campaign at once "
            "(overrides design.cluster.n_gpus). Also used by --compute auto "
            "to estimate cluster wall-clock. Ignored for --compute local."
        ),
    )
    p.add_argument(
        "--site",
        metavar="SITE_ID", default=None,
        help=(
            "Binder workflow: resume ONE site's own calibration campaign "
            "under binder/sites/<site_id>/ (from a prior --trial-sites N "
            "run), instead of the top-level binder/ stage files --start-from "
            "normally targets. Requires --start-from calibration and "
            "--n-batches set to EXACTLY the value the site was originally "
            "staged with (the normal --trial-sites/--trial-backbones "
            "re-entry recomputes n_batches from --trial-backbones and "
            "targets every candidate site, not just this one). Forces "
            "--compute cluster — this bypass exists for a cluster campaign "
            "staged for that site and submitted by a human. Shares its "
            "implementation with the standalone "
            "scripts/resume_cluster_calibration.py."
        ),
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
        choices=["spec", "trial", "calibration"], default=None, dest="stop_after",
        help=(
            "Binder workflow: 'spec' prepares and validates everything up to "
            "the GPU and stops, so specs can be reviewed before committing "
            "days of compute; 'trial' stops after the design trial and site "
            "comparison, before a production campaign; 'calibration' stops "
            "after the calibration verdict (SCALE_UP/SCALE_UP_PARTIAL) on a "
            "plain single-target run — resume with --start-from production "
            "once the verdict and estimated GPU-hours/disk look right."
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
        default="gemini",
        help="LLM provider. Default: gemini (gemini-3.7-flash) — cheaper "
             "and does not hit the 'bio'-category safety refusals "
             "claude-sonnet-5 routinely triggers on target-intel/interface.",
    )
    p.add_argument(
        "--model-id",
        metavar="MODEL",
        default=None,
        dest="model_id",
        help="Override model ID (e.g. claude-opus-5). Default: provider default.",
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

    config = _load_config()
    # Effective design engine: an explicit --design-engine wins; otherwise
    # config.yaml's design.backend decides (itself "boltzgen" unless changed —
    # see PipelineRunner.__init__ for the same precedence applied again on
    # the runner side, which is what actually matters for a library caller
    # that skips this CLI).
    design_engine = args.design_engine or (config.get("design") or {}).get(
        "backend", "foundry")

    # Cyclic peptides are a BoltzGen-only modality: RFD3/foundry has no cyclic
    # path, and `binder_sizes.cyclic_peptide` (12-15 residues) fed to RFD3 asks
    # for something it cannot build — quietly, not loudly. So opting into the
    # modality selects the engine that can actually do it.
    if args.modality == "cyclic_peptide":
        if args.design_engine == "foundry":
            parser.error(
                "--modality cyclic_peptide cannot run on --design-engine "
                "foundry: RFD3 has no cyclic-peptide path. Drop "
                "--design-engine to let the modality pick boltzgen, or design "
                "a mini_protein instead.")
        if is_binder:
            parser.error(
                "--workflow binder always runs foundry, which cannot build "
                "cyclic peptides. Use --workflow ppi for a cyclic-peptide "
                "campaign.")
        if design_engine != "boltzgen":
            logger.info(
                "--modality cyclic_peptide: using the boltzgen design engine "
                "(foundry/RFD3 has no cyclic-peptide path)")
        design_engine = "boltzgen"

    is_foundry_bridge = (not is_binder) and design_engine == "foundry"

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

    if is_foundry_bridge and not args.project:
        parser.error(
            "--design-engine foundry requires --project: the foundry stages "
            "it hands off to are multi-day GPU campaigns that need the same "
            "round-based, resumable manifest --workflow binder requires.")

    if args.site:
        if not is_binder:
            parser.error("--site applies to --workflow binder only.")
        if args.start_from != "calibration":
            parser.error(
                "--site currently only supports --start-from calibration "
                "(production has no single owning method the same way — "
                "see PipelineRunner.resume_site_stage).")
        if args.n_batches is None:
            parser.error(
                "--site requires --n-batches, set to EXACTLY the value the "
                "site was originally staged with.")
        if args.compute not in ("auto", "cluster"):
            parser.error(
                f"--site forces --compute cluster (the only case this "
                f"per-site resume bypass exists for); got --compute "
                f"{args.compute!r}.")
        args.compute = "cluster"

    # Validate resume arguments. A binder-stage --start-from needs neither
    # --pdb nor --context regardless of workflow: --workflow binder always
    # resumes this way, and a --design-engine foundry ppi run resumes the
    # same way once the bridge has already handed off once (see
    # PipelineRunner._bridge_ppi_to_foundry / _run_binder_track).
    if (not is_binder and args.start_from not in _BINDER_STAGES
            and args.start_from != "pathway" and not args.pdb and not args.context):
        parser.error(
            f"--start-from {args.start_from!r} requires either --pdb or --context "
            "(a path to a prior stage output file)."
        )

    # A --workflow ppi run with design.backend: foundry bridges into the
    # binder track, so these two reach the same stages a binder run does.
    # Every other binder-track flag below is already passed unconditionally.
    runs_foundry = is_binder or design_engine == "foundry"
    if runs_foundry and args.success_metric:
        config.setdefault("design", {}).setdefault(
            "binder_ranking", {})["success_metric"] = args.success_metric
    if runs_foundry and args.n_gpus:
        config.setdefault("design", {}).setdefault(
            "cluster", {})["n_gpus"] = args.n_gpus

    from src.pipeline_runner import (
        PipelineBlockedError,
        PipelineError,
        PipelinePausedError,
        PipelineRunner,
    )

    # Optional persistent project. A new round is started for a fresh run
    # (start-from pathway); a resume reuses the latest round.
    project = None
    round_id = None
    output_dir = args.output_dir
    if args.project:
        from src.project import Project
        project = Project.create(args.project, query=query, workflow=args.workflow)
        _first_stage = "target_intel" if args.workflow == "binder" else "pathway"
        if args.start_from == _first_stage or project.latest_round() is None:
            rnd = project.new_round(note=query[:80])
        else:
            rnd = project.latest_round()
        round_id = rnd["run_id"]
        # Stage files must land where every earlier stage of this round wrote
        # theirs, or a resume silently starts a fresh, disconnected
        # outputs/<slug>_<date>/ tree and immediately fails to find the prior
        # stage's handoff. --output-dir still wins when explicitly given.
        output_dir = output_dir or project.run_dir(round_id)
        logger.info(
            f"Project: {project.slug}  round: {round_id}  dir: {output_dir}"
        )

    runner = PipelineRunner(
        config=config,
        provider=args.provider,
        model_id=args.model_id,
        output_dir=output_dir,
        max_iter=args.max_iter,
        max_tokens=args.max_tokens,
        project=project,
        round_id=round_id,
        workflow=args.workflow,
        budget_usd=args.budget,
        budget_mode=args.budget_mode,
        detach=args.detach,
        n_batches=args.n_batches,
        compute=args.compute,
        max_local_hours=args.max_local_hours,
        trial_sites=args.trial_sites,
        trial_backbones=args.trial_backbones,
        escalate_to=(args.escalate_to or None),
        stop_after=args.stop_after,
        design_engine=design_engine,
        modality=args.modality,
    )

    logger.info(f"Query: {query[:120]}{'...' if len(query) > 120 else ''}")
    logger.info(f"Provider: {args.provider}  |  Model: {runner.model_id}")
    if args.pdb:
        logger.info(f"PDB override: {args.pdb}")
    if args.start_from != "pathway":
        logger.info(f"Resuming from stage: {args.start_from}")

    if args.site:
        # Per-site resume: go straight to that site's own stage files
        # (binder/sites/<site_id>/) rather than the top-level run's, and
        # with the exact n_batches the site was staged with — the same
        # underlying call scripts/resume_cluster_calibration.py makes.
        logger.info(f"Resuming site {args.site!r} at stage {args.start_from!r} "
                    f"(n_batches={args.n_batches})")
        try:
            calib = runner.resume_site_stage(
                output_dir, args.site, args.start_from, args.n_batches,
                attach=True)
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

        dirs = runner._binder_dirs(output_dir)
        site_dirs = runner._binder_dirs(dirs["sites"] / args.site)
        res = calib["result"]
        print()
        print("=" * 60)
        print(f"CALIBRATION VERDICT: {res.verdict}")
        print("=" * 60)
        print(res.verdict_reason)
        print(f"report: {site_dirs['binder'] / runner._BINDER_STAGE_FILES['calibration']}")
        print("=" * 60)
        return 0

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
