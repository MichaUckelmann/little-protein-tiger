"""Run-time variants of a skill's system prompt, built by asserted edits.

`two_track_wildcard` turns the live `wildcard-expert` SKILL.md into the two-track form: a canonical
shortlist plus corpus-derived hypotheses, each with a verbatim evidence chain, and the skill's own pick
demoted to advisory (the pipeline decides, see `src/hypothesis_gates.py`). The production skill file is
NOT edited. The variant is built from whatever SKILL.md currently says, and every edit asserts that its
anchor occurs exactly once, so a change to the skill that moves an anchor makes this fail instead of
quietly producing a different prompt.

Used by `PipelineRunner` when `--hypothesis-policy` is on, and by `scripts/build_wildcard_v2.py`, which
writes the result to docs/skill_drafts/ for review.
"""
from __future__ import annotations

def replace_once(text: str, old: str, new: str, label: str) -> str:
    assert text.count(old) == 1, f"anchor for {label!r} found {text.count(old)} times; the skill changed"
    return text.replace(old, new)


def replace_span(text: str, start: str, end: str, new: str, label: str) -> str:
    assert text.count(start) == 1 and text.count(end) == 1, f"span anchors for {label!r} not unique"
    a, b = text.index(start), text.index(end) + len(end)
    assert a < b
    return text[:a] + new + text[b:]


TWO_TRACK_INTRO = """
**Two tracks (this revision).** The report presents two kinds of candidate side by
side and does NOT decide between them:

- **Track A, canonical**: one or two well-established targets for the context
  (consensus knowledge, corpus-supported where the corpus has it). Tiers VALIDATED,
  BIOLOGICALLY_JUSTIFIED or PATHWAY_INFERRED.
- **Track B, corpus-derived**: up to three hypotheses that exist because of what
  the graph, DepMap or the fingerprints showed, and that Track A does not already
  contain. Each states an EVIDENCE CHAIN copied from tool output.

The orchestrator checks every candidate in code and applies the operator's policy
(default: forward the best eligible Track B hypothesis, else Track A). You state a
preference; you do not control the outcome. So do not argue for a candidate you
could not defend against those checks.
"""

LANDSCAPE_INTRO_OLD = """Present 3–4 PPI candidates (wildcard allows one extra vs. pathway-expert).
List highest tier first. Within the same tier, rank by: corpus support strength,
then PDB availability.
"""
LANDSCAPE_INTRO_NEW = """Present TWO tracks. Track A: one or two canonical candidates. Track B: up to three
corpus-derived hypotheses (the ones from your WILDCARD HYPOTHESIS GENERATION block
that survived Phase 4). List Track A first, then Track B. Within a track, highest
tier first, then corpus support strength, then PDB availability.
"""

CAND_HEADER_OLD = "For each candidate:\n\n#### [<TIER>] <ProteinA / ProteinB>\n"
CAND_HEADER_NEW = """For each candidate:

#### [<TIER>] <ProteinA / ProteinB>
- **Track**: <canonical | corpus_derived>
- **Evidence chain** (REQUIRED for corpus_derived; optional for canonical): one bullet per item, each copied
  VERBATIM from a tool response in this run and naming the tool, in the form
  `<kind>: <value> (<tool>)`. Kinds: `depmap_r` (a correlation exactly as returned), `novelty_score`,
  `quantitative` (a Kd or Ki exactly as returned, with units), `mentions` (a mention count),
  `graph_path` (the gene symbols along a path the graph tool returned), `doi` (a DOI a tool returned).
  Never round, re-derive, average or recall a number; an item you cannot copy from a tool response
  does not belong in the chain.
"""

PRIMARY_RULE_START = "State which candidate is recommended for the downstream pipeline. Wildcard\nmode INVERTS"
PRIMARY_RULE_END = "the skill has failed at its job.\n"
PRIMARY_RULE_NEW = """State which candidate YOU prefer. This is advisory: the orchestrator evaluates every
candidate in code (evidence chain grounded in tool output; a PDB entry that contains BOTH
named proteins; differs from Track A; some grounded support) and applies the operator's
policy, so a different candidate may be forwarded if yours fails a check.

Your preference should be a Track B candidate when one satisfies all of:
  (a) a PDB ID exists in the corpus or via `find_pdb_structures` / `search_rcsb_pdb`,
  (b) that entry contains both proteins you name, or a homologous interface you say so for,
  (c) at least one corpus fingerprint mentions the candidate (or a paralog) in a disease context,
  (d) the partner you name is the partner your evidence supports.
Tie-break: higher `novelty_score`. Otherwise prefer the best Track A candidate, and say
that the run found no tractable corpus-derived candidate.

**Name the pair the evidence supports.** If a corpus finding, a measured affinity or the
deposited structure shows that a DIFFERENT partner binds your protein (higher affinity, or
the only partner in the entry), name that partner, or state the mismatch under
`Key uncertainty`. Do not name the pair that is easier to argue for. (Observed: a report
recommended SHOC2 / KRAS on a structure of the MRAS-SHOC2-PP1C complex while quoting
that MRAS binds SHOC2 more tightly than KRAS.)

The rationale must cite `novelty_score` AND the strongest DepMap neighbourhood signal
(when Call 7 ran on the pick), both copied from tool output. When Call 7 produced no
partner with r >= 0.4, say so explicitly.
"""

CHOICES_ANCHOR = "  - `falsifying_readout`: one sentence — the assay + threshold that falsifies the hypothesis\n"
CHOICES_EXTRA = CHOICES_ANCHOR + """  - `track`: `"canonical"` or `"corpus_derived"` (REQUIRED)
  - `evidence_chain`: array of objects `{"kind": ..., "value": ..., "tool": ...}`, one per item of
    the candidate's Evidence chain, values copied verbatim from tool output (REQUIRED, non-empty,
    for `corpus_derived`; may be `[]` for `canonical`). `kind` is one of `depmap_r`, `novelty_score`,
    `quantitative`, `mentions`, `graph_path`, `doi`; `tool` is the tool that returned it.
"""

CONTRACT_OLD = """- **→ complex-structure-analysis**: use `Suggested PDB ID(s)` and `Target complex` from PRIMARY RECOMMENDATION
- **→ molecular-biology-expert**: use `Target complex` as the protein pair to query
- **→ orchestrator**: use the full report for Stage 0 summary; extract complex + PDB from PRIMARY RECOMMENDATION

The `TARGET OPPORTUNITY LANDSCAPE` is for human review — the orchestrator and downstream
skills consume only the `PRIMARY RECOMMENDATION` block."""
CONTRACT_NEW = """- **→ complex-structure-analysis / molecular-biology-expert**: the candidate the orchestrator
  FORWARDS, which is chosen by deterministic checks and an operator policy from `choices_json`.
  Your PRIMARY RECOMMENDATION is the default and is forwarded only if it passes those checks.
- **→ orchestrator**: reads `choices_json` (including `track` and `evidence_chain`) and the
  PRIMARY RECOMMENDATION block (your preference) from the full report.

The `TARGET OPPORTUNITY LANDSCAPE` is for human review; the machine-readable record of every
candidate is `choices_json`."""


def two_track_wildcard(text: str) -> str:
    text = replace_once(text, "downstream pipeline.\n", "downstream pipeline.\n" + TWO_TRACK_INTRO, "two-track intro")
    text = replace_once(text, LANDSCAPE_INTRO_OLD, LANDSCAPE_INTRO_NEW, "landscape intro")
    text = replace_once(text, CAND_HEADER_OLD, CAND_HEADER_NEW, "candidate header")
    text = replace_span(text, PRIMARY_RULE_START, PRIMARY_RULE_END, PRIMARY_RULE_NEW, "primary-recommendation rule")
    text = replace_once(text, "### PRIMARY RECOMMENDATION\n", "### PRIMARY RECOMMENDATION\n\n*(The skill's advisory preference; see TWO-TRACK above. The heading and fields are unchanged because downstream parsers read them.)*\n", "primary heading note")
    text = replace_once(text, CHOICES_ANCHOR, CHOICES_EXTRA, "choices_json fields")
    text = replace_once(text, CONTRACT_OLD, CONTRACT_NEW, "handoff contract")
    return text
