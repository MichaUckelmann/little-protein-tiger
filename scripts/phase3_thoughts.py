#!/usr/bin/env python3
"""Run a few pathway-stage cells with Gemini thought summaries switched on.

    .venv/bin/python scripts/phase3_thoughts.py --max-usd 1.0

The Phase 2 traces hold what the model searched and what came back, not why: the
runner did not request thought summaries, so only opaque `thoughtSignature`
tokens and a reasoning-token count were kept. Here the summaries are requested
(`SkillRunner.include_thoughts`) and stay in `trace_raw.json` as parts flagged
`thought: true`. They are summaries written by the API, not the raw reasoning,
and asking for them can change what the model does, so these cells are for
reading, not for scoring against the Phase 2 reports.

Cells: live (full pipeline tools) and notools (plain LLM), 3 prompts each.
Output: outputs/phase3_thoughts/<prompt>__<arm>.{md,json,trace}.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import phase2_corpus_eval as P  # noqa: E402  (installs the notools arm)
ac = P.ac
OUT = _ROOT / "outputs" / "phase3_thoughts"
ac.OUT = OUT
_patch = ac.patch


def patch(runner, arm, cache):
    _patch(runner, arm, cache)
    runner.include_thoughts = True


ac.patch = patch


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--prompts", nargs="+", default=["sting", "alt", "ra"])
    a = ap.parse_args()
    import yaml
    P.preflight()
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    queries = {slug: q for slug, _s, q, _c in P.PROMPTS}
    spent = 0.0
    for slug in a.prompts:
        for arm in ("live", "notools"):
            if spent >= a.max_usd:
                print("stopping at cap")
                return 0
            m = ac.cell(f"{slug}", arm, queries[slug], config, force=False)
            spent += m.get("usd") or 0
    print(f"spent ${spent:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
