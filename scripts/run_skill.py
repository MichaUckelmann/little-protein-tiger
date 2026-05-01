#!/usr/bin/env python3
"""
CLI entry point for running little_protein_tiger skills via Claude or Gemini API.

Examples
--------
# Pathway analysis (Claude, default model)
python scripts/run_skill.py \\
    --skill pathway-expert \\
    --query "Hippo pathway in mesothelioma — which node should we target?"

# PPI interface analysis → write report to file
python scripts/run_skill.py \\
    --skill complex-structure-analysis \\
    --query "Analyze interface between chain A (TEAD4) and chain B (YAP) in /tmp/7D9M.cif." \\
    --output /tmp/ppi_report.md

# Binder optimization with prior report as context (Gemini)
python scripts/run_skill.py \\
    --skill binder-optimizer \\
    --query "Suggest 4 point mutations on chain B to improve affinity. Structure source is AF3." \\
    --context /tmp/ppi_report.md \\
    --model gemini \\
    --output /tmp/mutations.md

# Query from a text file
python scripts/run_skill.py --skill pathway-expert --query @queries/hippo_mesa.txt
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml
from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from src.skill_runner import SkillRunner

_DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-6",
    "gemini": "gemini-3.1-flash-lite-preview",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a pipeline skill via Claude or Gemini API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Skills: complex-structure-analysis, binder-optimizer, pathway-expert,\n"
            "        molecular-biology-expert, complex-expert, protein-design-script, orchestrator"
        ),
    )

    parser.add_argument(
        "--skill",
        required=True,
        help="Skill name (directory under skills/)",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Query string, or @/path/to/file.txt to read from a file",
    )
    parser.add_argument(
        "--model",
        choices=["claude", "gemini"],
        default="claude",
        help="LLM provider (default: claude)",
    )
    parser.add_argument(
        "--model-id",
        default=None,
        metavar="MODEL_ID",
        help=(
            f"Override model ID "
            f"(defaults: claude={_DEFAULT_MODELS['claude']}, gemini={_DEFAULT_MODELS['gemini']})"
        ),
    )
    parser.add_argument(
        "--context",
        default=None,
        metavar="PATH",
        help="Path to a prior report .md to include as context",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Write final report to this file (default: stdout)",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=30,
        metavar="N",
        help="Max LLM calls per run (default: 30)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=100_000,
        metavar="N",
        help=(
            "Abort if any single LLM call receives more than N input tokens "
            "(default: 100,000). Prevents runaway cost on long conversations."
        ),
    )
    parser.add_argument(
        "--trace",
        default=None,
        metavar="DIR",
        help=(
            "Write a conversation trace to this directory after the run. "
            "Produces trace_raw.json (full history) and trace_rendered.md (annotated markdown)."
        ),
    )

    args = parser.parse_args()

    # Load config
    config_path = _ROOT / "config.yaml"
    config: dict = {}
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # Resolve model ID
    model_id = args.model_id or _DEFAULT_MODELS[args.model]

    # Resolve query (file redirect with @)
    query = args.query
    if query.startswith("@"):
        query_file = Path(query[1:])
        if not query_file.exists():
            print(f"ERROR: query file not found: {query_file}", file=sys.stderr)
            sys.exit(1)
        query = query_file.read_text(encoding="utf-8").strip()

    # Resolve context
    context_text: str | None = None
    if args.context:
        context_path = Path(args.context)
        if not context_path.exists():
            print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
            sys.exit(1)
        context_text = context_path.read_text(encoding="utf-8")

    # Run
    runner = SkillRunner(
        skill_name=args.skill,
        provider=args.model,
        model_id=model_id,
        config=config,
        max_iter=args.max_iter,
        max_input_tokens=args.max_tokens,
    )

    result = runner.run(query, context_text=context_text, trace_path=args.trace)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result, encoding="utf-8")
        print(f"Report written to {output_path}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
