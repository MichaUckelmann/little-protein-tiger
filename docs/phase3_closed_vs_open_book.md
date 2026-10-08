# Phase 3: can the corpus answer what the model's own knowledge cannot?

Written 2026-10-07, before any question was built or asked.

Phase 2 asked whether target-selection reports are better with the pipeline's
tools. The answer was: more traceable, not measurably better on rationale,
calibration, prior art or usefulness. That test has a blind spot. A judge and a
generator share training knowledge, and a consensus question ("what should we
target in rheumatoid arthritis") is exactly what training knowledge covers. A
corpus earns its place on questions whose answer is in the papers and not in the
model. This phase asks those.

## Design

Questions are built from the curated fingerprints, restricted to papers from
2024 or later and to protein pairs that occur in exactly one paper in the corpus,
so the gold answer is unambiguous and the topic is unlikely to be famous.

| type | n | question | gold | scored by |
|---|---|---|---|---|
| `kd` | 16 | Kd reported for the interaction of A and B, with the paper's stated context | the fingerprint's `affinities_kd_Molar` | automatic: within 3-fold |
| `residues` | 16 | which residues were reported important for the A-B interaction | `key_amino_acid_residues` | automatic: at least half of the gold residues named |
| `recent` | 16 | what a 2026 study found about A and B in the stated context | the finding's `claim` | two LLM judges (Claude, OpenAI), blind to arm |

Two arms, same model (`gemini-3.7-flash`), same system prompt, one fresh run per
question:
- **closed**: no tools.
- **open**: the pipeline's corpus tools (`search_corpus`, `get_fingerprint`,
  `find_quantitative_evidence`, `get_interactions_for`, graph tools).

The prompt says: answer the question; if you cannot determine the answer, reply
UNKNOWN on the first line; nothing else about guessing, so that abstention is the
model's own choice and the same in both arms.

Each answer is `correct`, `wrong` (an answer was given and it is not correct) or
`unknown` (abstained). `wrong` in the closed arm is a fabricated specific.

## Decision rules (fixed now)

- **The corpus adds knowledge the model lacks** if open minus closed correct rate
  is at least 0.25 on at least two of the three types. With 16 questions per
  type, differences below about 0.25 are not distinguishable from chance.
- **No evidence it adds knowledge** otherwise.
- Report the closed arm's `wrong` rate separately. It measures how often the model
  invents a specific when it has none, which Phase 2 could not observe.
- Report the closed arm's `correct` rate per type as the training-knowledge
  ceiling for those questions. A high value on `recent` would mean the model
  has seen papers the corpus was built from, or that the question leaks its answer.

## What the gold is, and is not

The gold is what the corpus's fingerprint says, extracted by a model from the
paper. It is not independently verified against the paper. The open arm reads
the same fingerprint, so an extraction error there would be reproduced, not
detected; the closed arm is unaffected. The paper must say so. A hand check of a
sample of gold answers against the source papers is the remedy and is not part of
this run.

## Budget

About $2 (open arm about $1.5, closed $0.15, judging $0.3). Hard stop $3.
