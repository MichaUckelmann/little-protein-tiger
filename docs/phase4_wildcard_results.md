# wildcard-expert: current skill against a two-track draft, 2026-10-08

Evaluation of `skills/wildcard-expert/SKILL.md` (v1, unchanged) against the two-track draft
`docs/skill_drafts/wildcard-expert-v2.md` (built by `scripts/build_wildcard_v2.py`), with the standard
`pathway-expert` skill and a no-tools baseline as references. Scripts: `phase3_wildcard_v2.py` (run, gates),
`phase3_wildcard_analyse.py`, `phase2_judge.py`; gates in `src/hypothesis_gates.py`.

Design: 7 prompts (the 5 from Phase 2 plus `epi` and `mash`, user-specified), 2 repeats each for v1 and v2, paired
against the standard and no-tools reports with the same prompt and repeat. Same generator
(gemini-3.7-flash) and tools. Experiment limits: 60 tool rounds, 400k input tokens (production defaults unchanged).
Two blind judges (Claude, OpenAI). Ten to fourteen reports per arm: only large effects are detectable.

## Deterministic checks (14 runs per version)

| | v1 (current) | v2 (two-track) |
|---|---|---|
| corpus-derived candidates | 22 | 38 |
| of which pass every gate | 2 | 8 |
| evidence-chain items found in tool output | 175/193 (91%), chains inferred | 245/256 (96%), stated |
| skill's own preference passes every gate | 5/14 | 8/14 |
| corpus-derived pairs absent from the no-tools candidate lists | 21/22 | 35/38 |
| failed runs | 0 (1 under the old 30-round cap) | 1 format failure (no report) |

Most gate failures are the structure gate: a novel hypothesis often has no deposited structure containing both
proteins. Cost per run about $0.23 (v1) and $0.33 (v2), roughly 40 to 65 tool calls, against about $0.11 and 11 to 14 for the standard skill.

## Judged quality (rubric 1 to 5; difference paired by prompt; 95% bootstrap interval)

| comparison | Claude overall / excl. traceability | OpenAI overall / excl. traceability |
|---|---|---|
| v2 minus standard | -0.01 / +0.09 | -0.05 / +0.00 |
| v2 minus no tools | +0.73 / +0.33 [0.14, 0.53] | +0.58 / +0.17 [-0.04, 0.37] |
| v2 minus v1 | -0.03 / +0.02 | -0.31 [-0.63, 0.00] / -0.30 |

Pairwise (both presentation orders agree; wins : losses : order-inconsistent, 14 pairs): v2 against standard
5:1:8 (Claude), 9:3:2 (OpenAI); against no tools 9:1:4, 8:2:4; against v1 7:4:3 (OpenAI only).

## Known weaknesses

- **Gate G3 is weak.** It compares a hypothesis with the SAME report's canonical top pick, not with canonical
  knowledge. Of the 8 eligible v2 hypotheses, ASF1A/HIRA (novelty 0.31) and EZH2/SUZ12 (0.45) are canonical.
  A novelty floor is the obvious fix and has not been applied.
- v1's evidence chains are inferred from the fields it states; v2 states them. The grounding comparison is
  like for like only for corpus-derived candidates.
- A complex name containing a PDB-style token (`TGFBR2 / 5HCS_TGFBR2_1`) passed the structure gate; names are not validated.
- Claude's claim-support coverage is partial (cost caps); order inconsistency is high for Claude on v2 against
  standard (8 of 14 pairs).
- The Claude and OpenAI judges differ on v2 against v1 (-0.03 against -0.31).
- No ground truth for whether a hypothesis is GOOD. "Absent from closed-book lists" and "passes gates" are
  necessary properties, not evidence of value.

## Bug found and fixed during the evaluation

`graph_path` evidence written as a JSON list was stringified with brackets and quotes and matched nothing, so honest
graph paths were rejected. Fixed in `src/hypothesis_gates.py` with a test; all numbers above are after the fix.
A PDB-structure suspicion about SHOC2/KRAS (entry 9O65) was wrong: that entry contains SHOC2, KRAS and PP1-alpha.
