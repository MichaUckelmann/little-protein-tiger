#!/usr/bin/env python3
"""Diagnostic: does `wildcard-expert` actually use the graph and quantitative tools, and what does it ground its hypotheses on?

    .venv/bin/python scripts/phase3_wildcard.py --max-usd 1.5      # run the five Phase 2 prompts, once each
    .venv/bin/python scripts/phase3_wildcard.py --report           # tool use and grounding check, no API calls

`pathway-expert` was found to use the corpus as a paper-retrieval engine (graph tools 0 to 0.5 calls
per run). `wildcard-expert` is the skill written to reason over graph edges and quantitative data, and
it was never measured: the earlier 16-run sweep only showed that its picks differ from the standard
skill's. This runs it on the same prompts with traces kept and Gemini thought summaries on (read-only,
see phase3_thoughts.py), then checks two things from the traces alone:

  1. which tools it called, and whether the mandatory graph phase fired;
  2. whether the numbers its report states (Kd, correlation, mention counts) appear in some tool
     response in the same run. A figure that appears in no response was not read from a tool.

Output: outputs/phase3_wildcard/<prompt>__live.{md,json,trace}.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import ablate_corpus as ac  # noqa: E402
import phase2_corpus_eval as P  # noqa: E402  (prompt list; installs the arm patch)

OUT = _ROOT / "outputs" / "phase3_wildcard"
ac.OUT = OUT
ac.SKILL = "wildcard-expert"
_patch = ac.patch


def _p(runner, arm, cache):
    _patch(runner, arm, cache)
    runner.include_thoughts = True


ac.patch = _p
GRAPH = {"interaction_hubs", "shortest_interaction_path", "novelty_signal", "get_interactions_for",
         "find_quantitative_evidence", "export_subgraph", "get_genetic_codependency",
         "find_cocorrelated_genes", "cluster_for_protein", "cluster_members", "find_clusters_by_keyword"}


def trace_stats(slug: str) -> dict:
    tr = json.loads((OUT / f"{slug}__live.trace" / "trace_raw.json").read_text(encoding="utf-8"))
    calls, responses = collections.Counter(), []
    for m in tr:
        for part in m.get("parts", []):
            if "functionCall" in part:
                calls[part["functionCall"]["name"]] += 1
            fr = part.get("functionResponse")
            if fr:
                responses.append((fr["response"] or {}).get("result", ""))
    return {"calls": calls, "responses": "\n".join(responses)}


_NUM = re.compile(r"(?:Kd|KD|Ki|r|R)\s*[=≈~:]?\s*([0-9]*\.?[0-9]+(?:\s*[eE][-+]?\d+)?)\s*(pM|nM|µM|μM|uM|mM)?|([0-9]+(?:\.[0-9]+)?)\s*(pM|nM|µM|μM|uM|mM)\b")


def stated_numbers(text: str):
    out = set()
    for m in _NUM.finditer(text):
        v = (m.group(1) or m.group(3) or "").strip()
        u = m.group(2) or m.group(4) or ""
        if v and (u or "." in v):
            out.add((v, u))
    return out


def report() -> None:
    rows = []
    for slug, _s, _q, _c in P.PROMPTS:
        if not (OUT / f"{slug}__live.json").is_file():
            continue
        meta = json.loads((OUT / f"{slug}__live.json").read_text())
        st = trace_stats(slug)
        text = (OUT / f"{slug}__live.md").read_text(encoding="utf-8")
        nums = stated_numbers(text)
        ungrounded = [(v, u) for v, u in nums if v not in st["responses"] and f"{v}{u}" not in st["responses"]]
        g = {t: n for t, n in st["calls"].items() if t in GRAPH}
        rows.append((slug, meta, st["calls"], g, nums, ungrounded, "WILDCARD HYPOTHESIS" in text))
        print(f"\n{slug}: ${meta['usd']:.2f}  {sum(st['calls'].values())} calls  graph/quantitative calls {sum(g.values())}  "
              f"hypothesis block {'yes' if rows[-1][-1] else 'NO'}  primary: {meta.get('primary')}")
        print("   calls:", dict(st["calls"].most_common()))
        print(f"   numeric claims in report {len(nums)}; not found in any tool response: {len(ungrounded)} {ungrounded[:5]}")
    if rows:
        agg = collections.Counter()
        for r in rows:
            agg.update(r[2])
        print("\nTOTAL tool calls over", len(rows), "runs:", dict(agg.most_common()))
        print("graph/quantitative calls per run:", round(sum(v for t, v in agg.items() if t in GRAPH) / len(rows), 1),
              "| spend $%.2f" % sum(r[1]["usd"] for r in rows))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-usd", type=float, default=1.5)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
        return 0
    import yaml
    from src.env_config import load_env
    load_env()
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    for slug, _s, q, _c in P.PROMPTS:
        if spent >= a.max_usd:
            print("stopping at cap")
            break
        m = ac.cell(slug, "live", q, config, force=False)
        spent += m.get("usd") or 0
        if m.get("error"):
            print("error, stopping:", m["error"])
            break
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
