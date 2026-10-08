# Phase 2: does the corpus change what the target-selection stage produces?

Written 2026-10-07, **before any Phase 2 output existed**. Scoring and decision
rules below are fixed; changing them after reading results must be said in the
paper.

## Why this design

Phase 0/1 findings (`scripts/audit_citations_external.py`,
`scripts/ablate_noise_floor.py`, `TARGET_SELECTION_AUDIT.md`):

- Re-running an identical live configuration gave the same top pick in 2 of 6
  queries (alias-normalised). Removing the corpus gave 3 of 6. Identity of the
  top pick is dominated by run-to-run noise and cannot show a corpus effect.
- Fixing the target and comparing evidence was rejected: it turns discovery
  into reverse justification, a different task.
- So the comparison is on the **whole report**, over repeated runs, on prompts
  that differ in how well the model and the corpus know the topic.

## Design

`pathway-expert`, `gemini-3.7-flash`, current prompts. Five prompts x 3 repeats
x 2 arms = 30 runs, shuffled order (seed 20261007).

| prompt | stratum | corpus papers on the key genes |
|---|---|---|
| sting | niche, dense | STING1 332, CGAS 292, TBK1 215 |
| alt | niche, dense (corpus core theme) | ATRX 97, DAXX 87, HIRA 69, CHAF1A 63 |
| tau | niche, moderate | HSP90AA1 90, HSPA1A 69, MAPT 72, STUB1 26 |
| kras | obvious | KRAS 331, RAF1 111, PIK3CA 157 |
| ra | obvious | TNF 189, IL6 182 |

Arms: `live` (corpus tools unchanged) and `blank` (every corpus-derived tool
returns "no results"; DepMap and RCSB stay live). Same tool surface, same
prompt. In `blank` the model is still told how to cite and that an uncited fact
is acceptable.

## Outcomes, in three tiers

**A. Deterministic (no LLM), per report.**
1. DOIs cited; fraction resolving in Crossref (after the `j.` repair
   documented in `audit_citations_external.py`); fraction not resolving.
2. PDB IDs cited; fraction that exist in RCSB and whose entry names the
   protein it is cited for.
3. Gene symbols in the candidate table; fraction valid HGNC symbols.
4. Cost, tool calls, corpus calls, report length.
5. Primary target and gene-set overlap between repeats (stability), and how
   often YAP1/TEAD1 is chosen for a prompt unrelated to Hippo biology. These
   two genes are in about 540 curated papers each, far more than most targets,
   and fresh live runs chose them for cancer, cardiac and fibrosis queries.
   Whether corpus density pulls the stage toward its own strongest topic is a
   risk to measure, not assume.

**B. Claim support (LLM judge, evidence supplied).** Up to 8 cited claims per
report are sampled (sentence containing a DOI, DOI-bearing claims only). The
judge sees the claim and the cited paper's abstract (Europe PMC, or the
fingerprint for corpus papers) and labels it `supported`, `partly`,
`unsupported` or `cannot_assess`. Unit of analysis is the report: unsupported
claims per report, not per claim.

**C. Report quality (LLM judge, blinded).** Rubric, 1 to 5 each, anchors in the
judge prompt: (1) specificity and correctness of the target rationale,
(2) evidence traceability, (3) calibration (states what is unknown or
contested), (4) prior-art accuracy, (5) usefulness for choosing a binder
target. Reports are stripped of the CITATION VERIFICATION and MODEL PROVENANCE
sections and of tool-trace text, order randomised, arm hidden. In addition, 15
matched pairs (same prompt, same repeat index, live vs blank) are judged
pairwise in both presentation orders; a pair counts only if both orders agree.

Judges: **Claude** (a different family from the generator) as primary; a second
judge from **OpenAI** on tiers B and C for agreement. Report Cohen's kappa
between judges. A refusal by a judge is recorded as missing for that item and
is not retried on another model: the same rule the pipeline follows for its own
stages.

**Human check.** You score 6 blinded pairs (2 per stratum draw) using the same
rubric; report agreement with each judge.

## Decision rules (fixed now)

Analysis is paired by prompt (5 pairs), with report-level bootstrap intervals;
with 15 reports per arm only large effects are detectable, and the paper must
say so. No p-value is claimed from five prompts.

- **Hallucination claim holds** if the live arm has lower unresolvable-DOI
  fraction AND lower unsupported-claims-per-report than blank, with both
  intervals excluding 0. A tie, or the blank arm citing too little to compare,
  is reported as "no measurable difference", not as support.
- **Quality claim holds** if the paired live minus blank mean on the rubric
  is at least 0.5 points (of 5) on BOTH judges and the sign agrees on at least
  4 of 5 prompts.
- **Stratum prediction (descriptive only):** the corpus helps more on `niche`
  than on `obvious`. Three versus two prompts cannot test this; report the
  differences.
- **Neither claim holds:** the corpus is described as a resource for
  traceability and provenance, not as an accuracy or quality mechanism, and the
  paper's weight moves to the pipeline, the structural guards and the
  end-to-end result.
- **Harm check:** if live picks YAP1/TEAD1 (or the same two or three genes) on
  3 or more of the unrelated prompts, report corpus-density bias as a finding.

## Budget

Generation about $3.9. Judging estimated at $5 to $6 (claim support about 240
items, rubric 30 reports, pairwise 30 judgments, second judge on the first two).
Spent so far on the whole evaluation: $1.51 of $20. Hard stop at $8 for this
phase, enforced per script.

## Limits that go in the paper

One generator model, one stage, 15 reports per arm, five prompts. The judges
are models. Citation checks establish that a DOI exists and what it says, not
whether a mechanistic claim is true. The earlier ablation showed run-to-run
variation as large as the effect of removing the corpus on the top pick, so
single-run comparisons anywhere in the paper should not be read as effects.

## Amendments after the first results (2026-10-07), in order

The decision rules above were not changed. What changed, and why:

1. **The blank arm leaked.** `find_pdb_structures` reads the fingerprints and was
   not in `ablate_corpus.CORPUS_TOOLS`, so the original blank arm kept a
   corpus-derived PDB channel. Found when a blank report quoted a coverage figure
   the trace contradicted. Fixed; pinned by `tests/test_ablate_corpus_blanking.py`;
   verified by `scripts/audit_blanking.py` (tripwire on file opens, with a positive
   control) and by a per-run trace check. The 15 blank cells were re-run and every
   judgement that depended on the old ones was discarded. The leaky arm and its
   judgements are kept in `outputs/phase2/archive_v1_find_pdb_structures_leak/`.
   **Two earlier observations did not replicate in the corrected arm** and must not
   be cited: "6 of 6 literature DOIs a blank report supplied from memory were wrong"
   (the corrected arm supplies none, only one invented dataset DOI), and "2 of 15
   blank reports state corpus coverage the trace contradicts" (now 0 of 15).
2. **A second baseline was added at the user's request.** The `blank` arm keeps the
   DepMap and RCSB tools live by design, so it answers "what does the corpus add
   inside the pipeline". The `notools` arm (no tools at all; skill text with the
   search phases replaced and the corpus-specific report sections removed, see
   `scripts/phase2_baseline.py` and `outputs/phase2/notools_system_prompt.md`)
   answers "does the pipeline beat a plain LLM". Both comparisons use the same
   rules; the analysis is run once per baseline (`phase2_analyse.py blank|notools`).
3. **The rubric rule has a built-in confound.** The no-tools arm cites nothing, so
   `traceability` favours the pipeline by construction. The pre-registered mean
   includes it. A sensitivity excluding it is reported alongside (post hoc).
4. **Post-hoc, labelled as such:** separating PDB-record DOIs (`10.2210/pdb...`) from
   literature DOIs; the PDB "does the entry name the cited protein" heuristic.
