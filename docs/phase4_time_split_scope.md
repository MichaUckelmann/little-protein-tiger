# Phase 4 scope: time-split test of "discovery or coverage bias?"

Scoping only. Nothing here has been run or spent. Written 2026-10-07.

## The question

In Phase 2 and the noise-floor runs, live (tool-using) picks sometimes moved away
from consensus targets toward topics the corpus is dense in (rheumatoid arthritis:
BRD4/E2F1 instead of IL-6 or TNF; YAP1/TEAD1 on cancer, cardiac and fibrosis
queries). Two readings fit:

- **Discovery:** the corpus holds findings absent from the model's training, and
  reasoning over them proposes candidates a closed model would not. If so, picks
  that differ from the no-tools consensus should be disproportionately the ones
  that later gain support, and the effect should grow with corpus size.
- **Coverage bias:** the pick follows where the corpus is dense, whatever the
  therapeutic merit. Picks that differ from consensus would later gain no more
  support than the alternatives.

The test: give the pipeline only the corpus as of a past date T, let it pick, then
measure, with a source that is NOT the corpus, whether its picks gained support
after T.

## What was checked (free, done)

Curated papers published up to each cutoff, of 14,517:

| cutoff | kept | after |
|---|---|---|
| 2018 | 6,439 (44%) | 8,078 |
| 2019 | 7,419 (51%) | 7,098 |
| **2020** | **8,542 (59%)** | **5,975** |
| 2022 | 10,872 (75%) | 3,645 |

At a 2020 cutoff the earlier prompts still keep workable coverage (STING1 109
papers, KRAS 156, BRD4 131, ATRX 68, HIRA 44, MAPT 27, STUB1 14; each roughly a
third to a half of its full-corpus count). **Recommended T = 2020**: 59% of the
corpus, and a 2021 to 2026 window for outcomes. T = 2022 keeps more corpus but a
shorter window.

## Leaks that must be closed first

A restricted corpus is only restricted if every tool is. Found by reading the code:

| channel | problem | fix |
|---|---|---|
| fingerprint directory | set by `paths.fingerprint_dir` in config | point it at a directory holding only papers up to T (symlinks) |
| vector index | `vector_store.db_path` in config | build a filtered copy of the existing table; no re-embedding needed |
| graph tools | built from the fingerprint directory | follow the restricted directory automatically |
| cluster tools | read `data/clusters.json`, a hardcoded full-corpus file | blank them, or add a path override and rebuild from the restricted edge index |
| `search_rcsb_pdb` | returns structures deposited after T | blank it, or filter by release date |
| DepMap tools | a data resource that postdates T | blank in every arm of this study, so the arms differ only in corpus |
| `pdb_metadata.json` | read from the parent of the fingerprint directory | give the restricted tree its own copy |

A per-run trace check, like the Phase 2 one, must assert that no DOI in any tool
response belongs to a paper published after T, and the leak audit must be re-run
against the restricted tree with a positive control.

## Arms

- **asof-T**: pipeline tools over the corpus as of T, with the leaks above closed.
- **no tools**: the Phase 2 baseline, unchanged.
- **full** (optional): the current full corpus, same blanking of DepMap and RCSB. This
  is the corpus-size contrast: asof-T against full holds everything else fixed.

No "assume it is 2020" instruction. The generator's own training postdates T and
telling it to pretend otherwise changes the task. Contamination is controlled
differentially instead: both asof-T and no-tools share the model's knowledge of
later developments, so what separates them is the pre-T corpus content. Closed-book
results (0 of 48 on 2024+ niche facts) suggest the model does not know recent
niche specifics, but it does know headline later developments, and that is a
stated risk.

## Prompts

To avoid choosing prompts that suit the answer: derive disease areas from the
fingerprints' `pathway_context.disease_associations` with at least 30 pathway
papers dated up to T, and draw 15 with a fixed seed. Written down before any run.

## Outcome (the open choice)

Per report, the primary pick (alias-normalised pair), and, for a within-report
control, the report's runner-up candidates. The outcome must come from a source
independent of the corpus, otherwise it is circular: a pick that follows corpus
density would also look well supported by later corpus papers.

Candidate indicators, each imperfect:
- later growth in external literature activity on the pair, relative to the field's
  overall growth (measures attention, not validity);
- registered clinical development against the target after T (closer to validity,
  but sparse for most targets).

**Not verified:** whether either source can be queried from this workstation. A
check was started and stopped (see the note at the end); the choice of source is
open and should be settled, and tested for reachability, before anything is built.

## Operational definition of the two readings

Cross each pick with two flags: differs from the no-tools consensus (not among the
no-tools picks for the same prompt), and later support above the median of all
candidates in the set.

- differs and high later support: the **discovery** signature;
- differs and low later support: the **coverage-bias** signature.

Reported as a 2 x 2 count with bootstrap intervals over prompts, plus the
asof-T versus full contrast if that arm is run.

## Cost and power

Live report about $0.107, no-tools report about $0.019, no judging needed (the
outcome is automatic). About $4.7 remains of the $20 (an earlier draft of this line said $3.9, a slip).

| option | design | cost |
|---|---|---|
| A (fits) | 15 prompts x 2 repeats x (asof-T, no tools) | about $3.8 |
| B | A plus the full-corpus arm | about $7.0, needs about $3 more |
| C | 25 prompts x 2 repeats x 2 arms | about $6.3, needs about $2.5 more |

The unit is the prompt. With 15 prompts only large effects (roughly 40 percentage
points on a binary outcome) can be told from chance; the paper must say so.
Option A leaves the corpus-size question open.

## Risks

- Attention is not validity: growth in activity is partly fashion.
- Contamination by the generator's later knowledge cannot be removed, only
  differenced out.
- Cutoff-based corpora are thinner, so asof-T reports may be worse for lack of
  material, which would bias against discovery.
- The pre-registered prompt draw may include areas where nothing changed after T;
  those add noise, not signal.

## Before spending anything

1. Settle the outcome source and confirm it is reachable from this machine.
2. Build the as-of-T tree and index, and run the leak audit with a positive control.
3. Print pre-T coverage for the 15 drawn prompts and drop any with too little.
4. Pre-register the outcome definition and the thresholds, as in Phases 2 and 3.

## Note on the interrupted check

While checking outcome-source reachability, a step was stopped by a safety
classifier. It was not retried or reworded. Reachability therefore remains
unverified, and the source is the first decision to settle.
