#!/usr/bin/env python3
"""
Conversational corpus explorer: ask the curated literature questions in
plain language and get answers with inline DOI citations.

    python scripts/ask_corpus.py "How does FACT reposition the H2A-H2B dimer?"
    python scripts/ask_corpus.py                    # start empty, then prompt

This is a thin front-end over `SkillRunner` running the `corpus-explorer`
skill — the same skill, prompt and tool surface the MCP transport exposes.
It used to carry its OWN agentic loop, hardcoded to Anthropic, with
`search_corpus` as its only tool. That second implementation cost it
everything the shared runner had already learned: Gemini support (and with
it the cheaper, less refusal-prone default every other entry point uses),
connection/429 retries, safety-refusal fallback across providers, token
accounting, and the other seventeen corpus tools — the interaction graph,
DepMap co-essentiality, clusters, quantitative-evidence lookup — which are
most of what makes exploring a corpus different from searching it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.env_config import load_env  # noqa: E402
load_env(ROOT / ".env")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.skill_runner import SkillRunner  # noqa: E402
# Imported, not restated: the per-provider default model belongs in one place,
# and run_skill.py is where every other CLI entry point reads it from.
from scripts.run_skill import _DEFAULT_MODELS  # noqa: E402

SKILL = "corpus-explorer"

# `--model` used to take a Claude model id (`--model claude-sonnet-5`), so a
# bare model id still has to mean something sensible rather than erroring.
_PROVIDER_OF_MODEL_PREFIX = {"claude": "claude", "gemini": "gemini",
                            "gpt": "openai"}


def _provider_for(model_id: str) -> str | None:
    for prefix, provider in _PROVIDER_OF_MODEL_PREFIX.items():
        if model_id.startswith(prefix):
            return provider
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ask the curated literature corpus questions in plain language. "
            "Runs the corpus-explorer skill with the full corpus toolset "
            "(semantic search, interaction graph, DepMap co-essentiality, "
            "clusters, quantitative evidence). Defaults to Gemini, so "
            "GEMINI_API_KEY alone is enough."),
        epilog=(
            "The corpus is one lab's reading list, weighted to chromatin, "
            "histone chaperones and structural biology. It answers well "
            "inside that and thinly outside it — a thin answer is the "
            "corpus's coverage, not the field's."),
    )
    parser.add_argument(
        "query", nargs="?", default=None,
        help="Optional first question. Without it, the agent starts empty and "
             "prompts for one. Either way it stays interactive afterwards.")
    parser.add_argument(
        "--provider", choices=["claude", "gemini", "openai"], default="gemini",
        help=f"LLM provider (default: gemini — {_DEFAULT_MODELS['gemini']}, "
             f"~4x cheaper on input and the default for every other entry "
             f"point; claude uses {_DEFAULT_MODELS['claude']})")
    parser.add_argument(
        "--model", dest="model_id", default=None, metavar="MODEL_ID",
        help="Specific model id (e.g. gemini-3.7-flash, claude-sonnet-5). "
             "The provider is inferred from the name when it is unambiguous.")
    parser.add_argument(
        "--config", default=str(ROOT / "config.yaml"),
        help="Path to config.yaml (default: repo root config.yaml)")
    parser.add_argument(
        "--top-k", type=int, default=None,
        help="Default number of search results per tool call (overrides config)")
    parser.add_argument(
        "--max-iter", type=int, default=30,
        help="Max tool-calling iterations per question (default: 30)")
    parser.add_argument(
        "--max-tokens", type=int, default=100_000,
        help="Abort a turn whose input exceeds this many tokens (default: 100000)")
    args = parser.parse_args()

    provider = args.provider
    if args.model_id:
        inferred = _provider_for(args.model_id)
        if inferred:
            # An explicit --provider still wins; a bare model id sets it.
            provider = inferred if "--provider" not in sys.argv else provider

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.top_k:
        config.setdefault("vector_store", {})["top_k_default"] = args.top_k

    runner = SkillRunner(
        skill_name=SKILL,
        config=config,
        provider=provider,
        model_id=args.model_id or _DEFAULT_MODELS[provider],
        max_iter=args.max_iter,
        max_input_tokens=args.max_tokens,
    )

    print(f"Corpus explorer ready — {runner.provider} / {runner.model_id}")
    print("Ask a question, or press Enter / type 'quit' to exit.\n")

    seed = args.query
    while True:
        if seed is not None:
            question, seed = seed.strip(), None
            print(f"You: {question}")
        else:
            try:
                question = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return
        if not question or question.lower() in ("quit", "exit"):
            print("Goodbye.")
            return

        try:
            # SkillRunner.run() resumes its own conversation when called
            # again, so each turn sees the previous ones.
            print(f"\nAssistant: {runner.run(question)}\n")
        except Exception as exc:                      # noqa: BLE001
            # One bad turn should not end the session — the corpus is slow to
            # load and the user may just want to rephrase.
            print(f"[error: {exc}]\n", file=sys.stderr)


if __name__ == "__main__":
    main()
