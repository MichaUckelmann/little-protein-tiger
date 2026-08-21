#!/usr/bin/env python3
"""
Interactive corpus analysis agent: Claude + semantic search tool.

Usage:
    python scripts/ask_corpus.py [--config CONFIG] [--model MODEL] [--top-k N]

Options:
    --config CONFIG   Path to config.yaml (default: repo root config.yaml)
    --model MODEL     Claude model to use (default: claude-sonnet-5)
    --top-k N         Default number of search results per tool call

Type a question to query the corpus. Press Enter on an empty line or
type 'quit' / 'exit' to end the session.
"""
import argparse
import json
import sys
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.vector_store import VectorStore

SYSTEM_PROMPT = """\
You are a scientific literature analysis assistant with access to a curated \
corpus of biochemistry papers focused on protein-protein interactions, \
inhibitor development, and structural biology for drug target discovery.

When the user asks about specific proteins, compounds, assay types, \
quantitative findings (Kd, Ki, IC50), or study designs, use the \
search_corpus tool to retrieve relevant papers before answering.

Always cite specific papers by title and DOI when referring to findings. \
Be precise about quantitative values and their experimental contexts. \
If search results do not contain information relevant to the query, \
say so explicitly rather than speculating.\
"""


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def extract_text(response) -> str:
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    )


def run_agentic_loop(
    client: anthropic.Anthropic,
    store: VectorStore,
    messages: list[dict],
    model: str,
    top_k_default: int,
) -> str:
    """
    Run one complete agentic turn (may involve multiple tool calls).
    Mutates ``messages`` in-place. Returns the final text response.
    """
    tools = [store.SEARCH_TOOL_DEFINITION]

    while True:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=tools,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return extract_text(response)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_input = dict(block.input)
                if "top_k" not in tool_input:
                    tool_input["top_k"] = top_k_default

                query_display = tool_input.get("query", "")
                st_display = tool_input.get("study_type")
                print(
                    f"\n[search: {query_display!r}"
                    + (f", study_type={st_display!r}" if st_display else "")
                    + f", top_k={tool_input['top_k']}]"
                )

                result_json = store.execute_search_tool(tool_input)

                try:
                    n = len(json.loads(result_json).get("papers", []))
                    print(f"[{n} result(s) returned]\n")
                except Exception:
                    pass

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_json,
                })

            messages.append({"role": "user", "content": tool_results})
            # Loop — let Claude respond to the tool results

        else:
            return extract_text(response)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive corpus analysis agent (Claude + semantic search)."
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.yaml"),
        help="Path to config.yaml (default: repo root config.yaml)",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-5",
        help="Claude model (default: claude-sonnet-5)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Default number of search results per tool call (overrides config)",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    vs_cfg = config.get("vector_store", {})
    db_path = ROOT / vs_cfg.get("db_path", "data/vectors")
    embedding_model = vs_cfg.get("embedding_model", "NeuML/pubmedbert-base-embeddings")
    top_k_default = args.top_k or vs_cfg.get("top_k_default", 5)

    store = VectorStore(db_path=db_path, embedding_model=embedding_model)
    client = anthropic.Anthropic()

    print(f"Corpus agent ready. Model: {args.model}  Top-k: {top_k_default}")
    print("Ask a question, or press Enter / type 'quit' to exit.\n")

    messages: list[dict] = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input or user_input.lower() in ("quit", "exit"):
            print("Goodbye.")
            break

        messages.append({"role": "user", "content": user_input})

        try:
            reply = run_agentic_loop(
                client=client,
                store=store,
                messages=messages,
                model=args.model,
                top_k_default=top_k_default,
            )
        except anthropic.APIError as exc:
            print(f"[API error: {exc}]")
            messages.pop()
            continue

        print(f"\nAssistant: {reply}\n")


if __name__ == "__main__":
    main()
