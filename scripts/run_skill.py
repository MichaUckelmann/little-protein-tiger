#!/usr/bin/env python3
"""
CLI entry point for running little_protein_tiger skills via Claude or Gemini API.

Examples
--------
# Pathway analysis (Gemini, default provider)
python scripts/run_skill.py \\
    --skill pathway-expert \\
    --query "Hippo pathway in mesothelioma — which node should we target?"

# PPI interface analysis → write report to file
python scripts/run_skill.py \\
    --skill complex-structure-analysis \\
    --query "Analyze interface between chain A (TEAD4) and chain B (YAP) in /tmp/7D9M.cif." \\
    --output /tmp/ppi_report.md

# Binder optimization with prior report as context (explicit Claude)
python scripts/run_skill.py \\
    --skill binder-optimizer \\
    --query "Suggest 4 point mutations on chain B to improve affinity. Structure source is AF3." \\
    --context /tmp/ppi_report.md \\
    --model claude \\
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
    "claude": "claude-sonnet-5",
    "gemini": "gemini-3.7-flash",
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
        required=False,
        default=None,
        help=(
            "Query string, or @/path/to/file.txt to read from a file. "
            "Optional when --interactive is set."
        ),
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help=(
            "After the initial query (or with no --query at all), drop into "
            "a REPL for follow-up turns. Commands: /exit, /quit, /reset, "
            "/tokens, /save <path>. Use @file.txt to load a query from a file."
        ),
    )
    parser.add_argument(
        "--model",
        choices=["claude", "gemini"],
        default="gemini",
        help="LLM provider (default: gemini — gemini-3.7-flash, cheaper "
             "and less prone to safety-classifier refusals on these "
             "prompts than claude-sonnet-5)",
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

    if not args.interactive and not args.query:
        parser.error("--query is required (or pass --interactive to start a REPL)")

    # Load config
    config_path = _ROOT / "config.yaml"
    config: dict = {}
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # Resolve model ID
    model_id = args.model_id or _DEFAULT_MODELS[args.model]

    # Resolve initial query (file redirect with @), if any
    query = _resolve_query(args.query) if args.query else None

    # Resolve context
    context_text: str | None = None
    if args.context:
        context_path = Path(args.context)
        if not context_path.exists():
            print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
            sys.exit(1)
        context_text = context_path.read_text(encoding="utf-8")

    # Build the runner
    runner = SkillRunner(
        skill_name=args.skill,
        provider=args.model,
        model_id=model_id,
        config=config,
        max_iter=args.max_iter,
        max_input_tokens=args.max_tokens,
    )

    # In interactive mode, defer trace writing until session end so we don't
    # rewrite it after every turn.
    initial_trace = None if args.interactive else args.trace

    # Initial turn (if --query was provided)
    if query is not None:
        result = runner.run(query, context_text=context_text, trace_path=initial_trace)
        _emit(result, args.output)

    if args.interactive:
        _interactive_loop(runner, args)

    # Session-end trace write for interactive runs
    if args.interactive and args.trace:
        runner.write_trace(Path(args.trace))


def _resolve_query(raw: str) -> str:
    """Expand @path-style query redirection to file contents."""
    if raw.startswith("@"):
        query_file = Path(raw[1:])
        if not query_file.exists():
            print(f"ERROR: query file not found: {query_file}", file=sys.stderr)
            sys.exit(1)
        return query_file.read_text(encoding="utf-8").strip()
    return raw


def _emit(result: str, output_path: str | None) -> None:
    """Print to stdout, and also write/append to output_path if given."""
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result, encoding="utf-8")
        print(f"Report written to {path}", file=sys.stderr)
    else:
        print(result)


def _interactive_loop(runner, args) -> None:
    """REPL for follow-up turns.

    Commands:
      /exit, /quit       — leave the loop
      /reset             — clear conversation history (keeps system prompt)
      /tokens            — show cumulative token usage
      /save <path>       — write trace (raw JSON + rendered MD) to <path>
      @path/to/file.txt  — load the next query from a file
    """
    print(
        "\n[interactive mode — Ctrl+D / Ctrl+Z to exit, "
        "/exit /reset /tokens /save <dir> available, "
        "@file.txt to load a query]\n",
        file=sys.stderr,
    )

    # Heuristic: warn when the most recent call's input tokens used a large
    # fraction of --max-tokens, since the next turn's input will be at least
    # that big plus the new follow-up.
    warn_threshold = 0.7 * args.max_tokens

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break

        if not line:
            continue

        if line in ("/exit", "/quit"):
            break

        if line == "/reset":
            runner.reset()
            print("[history cleared]", file=sys.stderr)
            continue

        if line == "/tokens":
            print(
                f"[tokens: {runner._total_input_tokens:,} in / "
                f"{runner._total_output_tokens:,} out — "
                f"last call: {runner._last_input_tokens:,} in]",
                file=sys.stderr,
            )
            continue

        if line.startswith("/save"):
            rest = line[len("/save"):].strip()
            if not rest:
                print("[usage: /save <path>]", file=sys.stderr)
                continue
            try:
                runner.write_trace(Path(rest))
                print(f"[trace written under {rest}]", file=sys.stderr)
            except Exception as exc:
                print(f"[save failed: {exc}]", file=sys.stderr)
            continue

        if line.startswith("@"):
            try:
                line = _resolve_query(line)
            except SystemExit:
                # _resolve_query exits on missing file; in REPL we'd rather
                # just report and continue.
                continue

        try:
            result = runner.run(line)
        except KeyboardInterrupt:
            print("\n[interrupted — partial turn discarded]", file=sys.stderr)
            continue
        except Exception as exc:
            print(f"[error: {type(exc).__name__}: {exc}]", file=sys.stderr)
            continue

        print()
        _emit(result, args.output)
        print()

        if runner._last_input_tokens > warn_threshold:
            print(
                f"[warn: last call used {runner._last_input_tokens:,} input tokens "
                f"({runner._last_input_tokens / args.max_tokens * 100:.0f}% of "
                f"--max-tokens={args.max_tokens:,}) — consider /reset]",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
