#!/usr/bin/env python3
"""Build the two-track draft of `wildcard-expert` from the live skill by asserted edits.

    .venv/bin/python scripts/build_wildcard_v2.py        # writes docs/skill_drafts/wildcard-expert-v2.md

What changes, and why (see src/hypothesis_gates.py for the observed failure):
  1. Output has TWO tracks: a short canonical shortlist (Track A) and up to three
     corpus-derived hypotheses (Track B), each with a verbatim EVIDENCE CHAIN.
  2. The skill no longer decides what goes forward. PRIMARY RECOMMENDATION keeps its
     name and fields (the parser and downstream stages read it) but is the skill's
     ADVISORY preference; the orchestrator applies deterministic gates and an
     operator policy.
  3. `choices_json` gains `track` and `evidence_chain`.
  4. A rule against naming a partner the evidence contradicts.

Everything else is unchanged. Each edit asserts its anchor exists exactly once, so
a change to the skill that moves an anchor makes this fail, not diverge.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.skill_variants import two_track_wildcard as build  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "skills" / "wildcard-expert" / "SKILL.md"
OUT = ROOT / "docs" / "skill_drafts" / "wildcard-expert-v2.md"


def main() -> int:
    new = build(SRC.read_text(encoding="utf-8"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(new, encoding="utf-8")
    old = SRC.read_text(encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(old)} -> {len(new)} chars ({len(new) - len(old):+d})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
