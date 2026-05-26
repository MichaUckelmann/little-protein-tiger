---
name: wildcard-expert
description: >
  Query the curated literature database to identify non-obvious, potentially novel
  PPI targets for a disease context. Uses a training-knowledge bridge to generate
  creative hypotheses, then validates each strictly against the corpus. Complements
  pathway-expert: use when the user explicitly wants creative or out-of-the-box
  suggestions, or when standard pathway analysis has already been run and the user
  wants a parallel speculative analysis for comparison.
  Output format is identical to pathway-expert and feeds directly into
  complex-structure-analysis and molecular-biology-expert. Requires literature-db MCP server.
  Trigger on: "wildcard", "novel target", "creative suggestions", "out-of-the-box",
  "unexpected PPI", "re-run wildcard", or any explicit request to explore non-obvious targets.
---

# Wildcard Pathway Expert

This skill generates creative, non-obvious PPI target hypotheses by combining a
training-knowledge bridge (Phase 3) with strict corpus re-anchoring (Phase 4). It
is the speculative counterpart to pathway-expert — every candidate in the final
report that makes a factual claim must cite corpus evidence, but the search strategy
itself is driven by parametric reasoning rather than only the terms the corpus returns.

The output is a structured `## PATHWAY BIOLOGY REPORT` with the same format and
`### PIPELINE HANDOFF` block as pathway-expert, so it slots directly into the
downstream pipeline.

---

## Phase 1: Extract Context from User Input

Identify:
- `disease_or_cancer` — e.g. "mesothelioma", "PDAC", "NSCLC", "HCC", "AML"
- `pathway_hint` — optional; e.g. "Hippo", "KRAS signaling", "cGAS-STING"
- `constraint` — optional; e.g. "avoid previously targeted nodes", "extracellular only"

If disease is ambiguous proceed with the broad term — do not block on ambiguity.

---

## Phase 2: Abbreviated Direct Corpus Search

Run **2 queries** (not 4 — save context budget for the creative phases). Use
`study_category="pathway_biology"` with unfiltered fallback if scores < 0.20.

**Query 1 — Disease-pathway mechanism** (top_k=8):
```
search_corpus
  query="<pathway_hint OR disease> signaling dysregulation cancer mechanism"
  study_category="pathway_biology"
  top_k=8
```

**Query 2 — Genetic dependency / essentiality** (top_k=6):
```
search_corpus
  query="<disease> CRISPR screen genetic dependency essentiality <pathway_hint>"
  study_category="pathway_biology"
  top_k=6
```

Deduplicate by DOI. Retrieve `get_fingerprint` for the top 4–6 unique papers from
Phase 2. From each fingerprint, explicitly extract and record:

- `pathway_context.pathways` — which pathways are covered
- `pathway_context.disease_associations` — disease, mechanism, mutation_frequency, source_span
- `pathway_context.target_nodes` — protein, pathway_position, dysregulation,
  genetic_dependency_evidence, prior_therapeutic_targeting, **suggested_pdb_structures**, source_span
- `pathway_context.pathway_logic` — ON/OFF logic summary
- `pathway_context.redundancy_risks`
- `pathway_context.upstream_regulators`, `downstream_effectors`
- `paper_metadata.pdb_accessions` — PDB IDs associated with this paper
- `key_findings` — incidental biochemistry findings that may be relevant

**Record every PDB accession found in `suggested_pdb_structures` or
`paper_metadata.pdb_accessions` at this stage.** These are Phase-2 PDB IDs —
carry them forward explicitly; they must appear in the report even if Phase 4.6
returns no additional hits for those proteins.

These fingerprints form the baseline evidence set and seed the term list for Phase 3.

### Corpus gap fallback

If both queries return scores < 0.20 or empty, re-run without `study_category`.
Always state the fallback in the CORPUS COVERAGE section.

---

## Phase 3: Training Knowledge Bridge

**This is the core creative phase.** You are explicitly permitted to reason from
parametric training knowledge here. Generate **3–5 novel PPI hypotheses** that would
not arise naturally from the Phase 2 corpus results alone.

For each hypothesis consider:
- **Cross-pathway convergence**: a node in a different pathway that feeds the same
  disease-relevant output (e.g. an mTORC1 effector that co-activates the primary
  transcription factor via a non-canonical route)
- **Synthetic lethality / co-dependency**: a PPI that becomes essential only because
  of a co-occurring alteration present in the disease (e.g. a backup pathway that is
  the sole remaining route when the primary is blocked by mutation)
- **Paralog vulnerability**: a less-studied paralog of a known target whose interaction
  with a shared scaffold is required when the primary paralog is lost
- **Cross-indication transfer**: a PPI well-characterized in a related disease (e.g.
  another solid tumour, a metabolic disease with shared pathway logic) that may be
  relevant here despite sparse direct evidence
- **Upstream rewiring**: an upstream adaptor or co-chaperone whose interaction with
  the disease driver has been described in biochemistry but not yet as a therapeutic
  target in this indication

Write each hypothesis in this block (internal reasoning — shown before the report):

```
## WILDCARD HYPOTHESIS GENERATION
[Training knowledge — not corpus citations. Do not cite these as evidence.]

HYPOTHESIS 1: <ProteinA / ProteinB>
  Mechanism: <why this PPI is disease-relevant — 1–2 sentences using pathway logic>
  Novelty: <what makes this non-obvious — 1 sentence>
  Why corpus might support it: <indirect evidence path — what to search for>
  Search terms: ["<term1>", "<term2>", "<term3>"]

HYPOTHESIS 2: <ProteinA / ProteinB>
  ...
```

**Hard rules for Phase 3:**
- Do not repeat any PPI already surfaced by Phase 2 fingerprints.
- Do not include a hypothesis unless you can name *both* proteins specifically.
- Mark this entire block clearly as training knowledge — it must NOT appear as a
  citation or evidence source anywhere in the final report.

---

## Phase 4: Hypothesis-Driven Corpus Search

Convert each hypothesis into targeted corpus queries and run them. Cap at **5
queries total** across all hypothesis + cross-pathway searches below.

**For each hypothesis** (run at most 3 hypothesis queries):
```
search_corpus
  query="<ProteinA> <ProteinB> <mechanism_keyword from hypothesis> <disease>"
  top_k=5
```

**Cross-pathway synthetic lethality query** (always run, top_k=6):
```
search_corpus
  query="<disease> synthetic lethality co-dependency pathway compensation"
  top_k=6
```

**Adjacent-indication transfer query** (run if pathway_hint is known, top_k=5):
```
search_corpus
  query="<pathway_hint OR primary_target> <adjacent_indication> mechanism inhibition"
```
Use the closest well-characterised disease neighbour you identified in Phase 3
(e.g. if disease is PDAC and the pathway is Hippo, try "mesothelioma" or "HCC").

Deduplicate all Phase 4 results against Phase 2 DOIs. For each **new** paper with
score ≥ 0.20, call `get_fingerprint` and merge into the working evidence set.

Map each new paper back to the hypothesis that generated its search query.
A hypothesis that produces ≥ 1 paper with score ≥ 0.30 is **corpus-supported**;
one with only weak hits (0.20–0.29) is **weakly supported**; one with nothing
found is **corpus-absent** (still included in the report with that label).

---

## Phase 4.5: Term-Expansion Search

After completing Phase 2 and Phase 4, extract corpus-specific terms that did not
appear in any previous query string. Same logic as pathway-expert Phase 3.5:

| Source field | What to extract |
|---|---|
| `pathway_context.target_nodes[].protein` | Gene symbols of newly surfaced proteins |
| `pathway_context.upstream_regulators` | Regulator names not in original query |
| `pathway_context.downstream_effectors` | Effector names not in original query |

**Skip Phase 4.5** only if Phase 2 + Phase 4 combined returned ≥ 14 unique papers
with scores ≥ 0.35. Otherwise always run it.

Run at most **1 follow-up search** (not 2 as in pathway-expert — Phase 4 has
already done the creative expansion):

```
search_corpus
  query="<new_gene_symbol_1> [<new_gene_symbol_2>] <disease> mechanism dependency"
  study_category="pathway_biology"
  top_k=5
```

Deduplicate; call `get_fingerprint` for new papers with score ≥ 0.25.

State in CORPUS COVERAGE how many papers this added.

---

## Phase 4.6: Corpus-Wide PDB Lookup

After completing Phases 2–4.5, call `find_pdb_structures` **once** with every
candidate target protein across all phases:

```
find_pdb_structures
  proteins=["<ProteinA>", "<ProteinB>", ...]
```

Build the protein list in this order:
1. **Proteins from Phase 2 fingerprints** (`target_nodes`, `upstream_regulators`,
   `downstream_effectors`) — list these first
2. **Hypothesis PPI partners** from Phase 3 that are new (not already in list above)
3. **Any other PPI candidates** you intend to include in the landscape

Typically 4–10 gene symbols total.

This scans the **entire** fingerprint corpus, including papers not retrieved in
Phases 2–4, and returns PDB accessions from two sources: `suggested_pdb_structures`
in pathway fingerprints and `pdb_accessions` from any paper mentioning the protein.

When multiple structures are returned for a protein, select the best one using these
criteria **in priority order**:

1. **Complex present** — `protein_chain_count ≥ 2` with both target protein AND
   binding partner in the `entities[].description` fields
2. **Method quality** — prefer `X-RAY DIFFRACTION` > `ELECTRON MICROSCOPY` > `NMR`
3. **Resolution** — for X-ray, prefer < 2.5 Å; for cryo-EM, prefer < 4.0 Å
4. **Human organism** — prefer `entities[].organism_taxid = 9606`

If `metadata_available` is `false`, use PDB IDs as-is and note it in the report.

- **Add any newly discovered PDB IDs to the relevant node before writing the report.**
  This includes results for Phase 2 proteins as well as hypothesis proteins.
- **Use the selected ID verbatim** in `Suggested PDB ID(s)` fields and in the
  `### PIPELINE HANDOFF` `pdb_id` line.
- If `total_found` is 0, call `search_rcsb_pdb` with the 2–3 primary target
  proteins (Phase 2 proteins first) as a fallback. Apply the same selection
  criteria. If a suitable structure is found, use its PDB ID in the handoff.
  If no suitable structure found from either source, write `NOT_FOUND`.

---

## Phase 5: Inferred PPI Reasoning

For every candidate node, run the same four-condition reasoning check as
pathway-expert Phase 4a (interaction necessity, therapeutic mechanism, consequence
of disruption or stabilization, interaction knowability).

The therapeutic mechanism step:
- `disrupt` — breaking the interaction attenuates the disease-relevant output (loss of
  complex formation → loss of oncogenic signalling, failure to relay a pathological signal)
- `stabilize` — the disease mechanism is that a normally protective or autoinhibitory
  interaction is *lost* or *weakened*; reinforcing it restores the healthy state (e.g.
  restoring an autoinhibitory complex, re-engaging a sequestered OFF-state, protecting
  a tumour suppressor complex from degradation)
Record one `design_intent` per node. Default to `disrupt` if ambiguous.

For **hypothesis-tier candidates**: replace the four-condition check with:
1. Name the two proteins specifically.
2. State the corpus support level (corpus-supported / weakly supported / corpus-absent).
3. Give the mechanism rationale in 1–2 sentences from Phase 3.
4. Identify any indirect corpus evidence (e.g. "Phase 4 search found a paper on ProteinA
   in an adjacent indication suggesting the interaction exists").

Tier assignment for Phase 5 — use the **first tier from this list that applies**:

| Tier | Condition |
|---|---|
| `[VALIDATED]` | Prior therapeutic targeting documented in corpus |
| `[BIOLOGICALLY_JUSTIFIED]` | Genetic dependency confirmed; interaction necessity corpus-supported |
| `[PATHWAY_INFERRED]` | No genetic dependency; named interaction required per corpus pathway logic |
| `[SYNTHETIC_LETHALITY]` | Corpus supports a co-dependency; PPI exploitable given a co-occurring alteration present in the disease |
| `[CROSS_INDICATION_TRANSFER]` | Strong corpus evidence in adjacent indication; transferability argued with explicit reasoning |
| `[HYPOTHESIS]` | Generated in Phase 3; corpus support is weak or absent; included for creative value |

A candidate may carry only one tier — use the highest supported. HYPOTHESIS is the
lowest tier and must be used when no stronger corpus evidence was found.

---

## Phase 6: Synthesise and Output Report

### WILDCARD HYPOTHESIS GENERATION block

Write the Phase 3 block **above** the main report, clearly labeled as training
knowledge. This is informational — it shows the creative reasoning. It is **not**
part of the parseable pipeline output.

### PATHWAY BIOLOGY REPORT

Produce the full report using **only information retrieved from the corpus** (plus
the clearly-labeled hypothesis block above). Do not hallucinate pathway details.
Omit lines that cannot be filled — do not write placeholder text.

**Citation policy:** Every cited claim in the PATHWAY BIOLOGY REPORT must come from
a retrieved fingerprint. The WILDCARD HYPOTHESIS GENERATION block above is the only
place where training knowledge is acceptable without a citation.
- Citation format: `DOI + source_span` (e.g. `10.7554/eLife.77415, Page 3 Para 2`).
- No author names, journal names, or years — ever. Write the DOI only.
- If you know a biological fact from training knowledge but have no corpus DOI for it,
  state the fact without any citation — do not invent a reference.
- A fact with no citation is acceptable. A fact with a fabricated citation is not.

**Token budget:** 3,500–5,000 words for the combined output (hypothesis block +
PATHWAY BIOLOGY REPORT).

### Report Format

All sections identical to pathway-expert **except**:

1. The `#### [<TIER>]` header in TARGET OPPORTUNITY LANDSCAPE uses the expanded
   tier set (including SYNTHETIC_LETHALITY, CROSS_INDICATION_TRANSFER, HYPOTHESIS).

2. Every candidate in TARGET OPPORTUNITY LANDSCAPE has an additional bullet:
   `- **Novelty rationale**: <why this target/PPI is non-obvious — 1 sentence>`

3. HYPOTHESIS-tier entries have an additional bullet:
   `- **Corpus support**: <what was found, or "Nothing found in corpus">`

4. The CORPUS COVERAGE section has an additional line:
   `- Wildcard hypotheses generated: <N> total; <N> corpus-supported; <N> weakly supported; <N> corpus-absent`

```
## PATHWAY BIOLOGY REPORT

### DISEASE CONTEXT
- Disease / indication: <name>
- Pathway(s) implicated: <comma-separated list>
- Primary disease mechanism: <one sentence — cite DOI + source_span>
- Frequency of pathway dysregulation: <mutation_frequency if stated>

### PATHWAY MAP
- Upstream regulators: <max 3 items with alteration type>
- Core cascade: <one sentence of pathway logic>
- Key effectors / transcription factors: <max 3 items>

### DYSREGULATED NODES ASSESSMENT

For each candidate target node — max 5 nodes total (wildcard allows one extra),
6–7 bullet lines per node:

#### <ProteinName (gene symbol)>
- Pathway position: <pathway_position>
- Dysregulation: <one sentence — cite source_span + DOI>
- Genetic dependency: <evidence, or omit if absent>
- Prior therapeutic strategies: <prior targeting, or omit if absent>
- Suggested PDB structures: <IDs, or omit if absent>
- Inferred PPI opportunity: <max 2 sentences: named partner, evidence, consequence — or "Insufficient evidence.">
- Novelty note: <one sentence on what makes this node non-obvious, or omit if it is a standard target>

### TARGET OPPORTUNITY LANDSCAPE

Present 3–4 PPI candidates (wildcard allows one extra vs. pathway-expert).
List highest tier first. Within the same tier, rank by: corpus support strength,
then PDB availability.

For each candidate:

#### [<TIER>] <ProteinA / ProteinB>
- **Evidence basis**: <1–2 sentences citing specific corpus evidence, or Phase 3 reasoning for HYPOTHESIS>
- **What makes it attractive**: <therapeutic rationale — pathway position, druggable interface, unmet need>
- **Key uncertainty**: <what is not yet established>
- **Novelty rationale**: <why this is non-obvious — 1 sentence>
- **Corpus support**: <for HYPOTHESIS only — what was found, or "Nothing found in corpus">
- **Suggested PDB ID(s)**: <verbatim from fingerprint fields only; "Not found in corpus" if absent>

Tier definitions:
- [VALIDATED]                — Prior therapeutic targeting documented in corpus
- [BIOLOGICALLY_JUSTIFIED]   — Genetic dependency confirmed + interaction necessity corpus-supported
- [PATHWAY_INFERRED]         — No genetic dependency; named interaction required per corpus pathway logic
- [SYNTHETIC_LETHALITY]      — Co-dependency supported by corpus; requires co-occurring alteration
- [CROSS_INDICATION_TRANSFER]— Strong corpus evidence in adjacent indication; transferability argued
- [HYPOTHESIS]               — Phase 3 origin; weak or no corpus support; speculative

### PRIMARY RECOMMENDATION

State which candidate is recommended for the downstream pipeline, and why (1–2
sentences balancing evidence strength, structural tractability, and novelty value).

Prefer VALIDATED or BIOLOGICALLY_JUSTIFIED if available. If the most novel candidate
is HYPOTHESIS tier but has an available PDB, it may be recommended with an explicit
caveat: "Speculative — corpus support is absent; proceed only if user accepts the
higher biological uncertainty."

- **Target complex**: <ProteinA / ProteinB>
- **Evidence tier**: <tier label>
- **Suggested PDB ID(s)**: <verbatim from corpus; "Not found in corpus" if absent>
- **Proposed next step**: Run complex-structure-analysis on PDB <ID>
  (only if PDB confirmed from corpus; otherwise await user input)

### REDUNDANCY AND RESISTANCE RISKS
- <max 3 bullet points from redundancy_risks — compensatory proteins, dual-targeting need, resistance gaps>

### CORPUS COVERAGE
- Papers analysed: <N fingerprints> from <M search hits>
- Fallback used: <Yes / No — if yes, which queries>
- Expansion round: <Yes / No — terms used, new papers added>
- Wildcard hypotheses generated: <N> total; <N> corpus-supported; <N> weakly supported; <N> corpus-absent
- Critical gaps: <specific missing nodes or diseases — omit if none>
- Keywords to add (if gaps are critical): `python scripts/fetch_papers.py --keywords "<keyword>"`

### PATHWAY SOURCES
| # | Title (truncated to 60 chars) | DOI | Key node |
|---|-------------------------------|-----|----------|
| 1 | ...                           | ... | ...      |

### PIPELINE HANDOFF
- pdb_id: <PDB accession from corpus (pdb_accessions or suggested_pdb_structures fields only), or NOT_FOUND>
- target_complex: <ProteinA / ProteinB>
- design_intent: <disrupt | stabilize — from Phase 5 reasoning for the PRIMARY RECOMMENDATION>
- structure_query: <one sentence — e.g. "Analyze PDB {pdb_id} at data/structures/{pdb_id}.cif. Target complex: {ProteinA} / {ProteinB}. Identify hotspot residues for {modality} design." DO NOT include chain letters (A/B/...) anywhere in this query — at this stage you have not inspected the mmCIF and any chain assignment you write will be a guess. Chain identity is resolved by the downstream structure-analysis stage, which reads the mmCIF header directly.>
- choices_json: <compact JSON array — see format below>

**IMPORTANT:** Write the `### PIPELINE HANDOFF` section as plain bullet lines exactly as shown above.
Do NOT wrap it in a code fence (no ``` before or after). Do NOT omit the `- ` prefix.
The programmatic orchestrator parses these lines with a regex — any deviation breaks the pipeline.

Rules for `### PIPELINE HANDOFF`:
- `pdb_id` must come from `paper_metadata.pdb_accessions`, `pathway_context.target_nodes[].suggested_pdb_structures`,
  **or the `find_pdb_structures` tool result from Phase 4.6**. Write `NOT_FOUND` if nothing found — never guess.
- `structure_query` is the verbatim query string passed to complex-structure-analysis; make it self-contained.
- `choices_json` lists every candidate from TARGET OPPORTUNITY LANDSCAPE in order. Each element:
  - `tier`: one of `"VALIDATED"`, `"BIOLOGICALLY_JUSTIFIED"`, `"PATHWAY_INFERRED"`,
    `"SYNTHETIC_LETHALITY"`, `"CROSS_INDICATION_TRANSFER"`, or `"HYPOTHESIS"`
  - `complex`: protein pair name exactly as in the `####` header
  - `pdb_ids`: array of PDB accession strings from corpus only — empty array `[]` if none
  - `evidence_basis`: one sentence summary (no newlines, no quotes inside)
  - `key_uncertainty`: one sentence summary (no newlines, no quotes inside)
  - `design_intent`: `"disrupt"` or `"stabilize"` from Phase 5 reasoning for this candidate

  Example (must be on ONE line):
  `- choices_json: [{"tier":"PATHWAY_INFERRED","complex":"YAP1 / TEAD4","pdb_ids":["5GN0"],"evidence_basis":"Mesothelioma corpus supports YAP nuclear accumulation requiring TEAD4 co-activation.","key_uncertainty":"TAZ paralog redundancy may require dual targeting.","design_intent":"disrupt"},{"tier":"HYPOTHESIS","complex":"VGLL4 / TEAD4","pdb_ids":[],"evidence_basis":"Training knowledge: VGLL4 competes with YAP for TEAD binding; no direct corpus evidence in this indication.","key_uncertainty":"No corpus evidence in mesothelioma; interaction knowability unverified.","design_intent":"stabilize"}]`

---

## Handoff Contract

Identical to pathway-expert:
- **→ complex-structure-analysis**: use `Suggested PDB ID(s)` and `Target complex` from PRIMARY RECOMMENDATION
- **→ molecular-biology-expert**: use `Target complex` as the protein pair to query
- **→ orchestrator**: use the full report for Stage 0 summary; extract complex + PDB from PRIMARY RECOMMENDATION

The `TARGET OPPORTUNITY LANDSCAPE` is for human review — the orchestrator and downstream
skills consume only the `PRIMARY RECOMMENDATION` block.

---

## Common Pitfalls

- **Training knowledge ≠ corpus citation.** Every factual claim in the report body
  (outside the clearly-labeled WILDCARD HYPOTHESIS GENERATION block) must cite a
  DOI and source_span from a retrieved fingerprint. The hypothesis block is the only
  place where training knowledge is acceptable without citation.
- **Never guess PDB accession codes.** Same rule as pathway-expert — only report PDB
  IDs verbatim from fingerprint fields or `find_pdb_structures` tool result.
- **HYPOTHESIS tier is not a failure.** It is a feature — the value of wildcard mode
  is surfacing targets that the corpus does not yet validate but that have a reasoned
  mechanistic basis. Always include them.
- **Don't inflate tiers.** Weak indirect corpus support (one paper tangentially
  mentioning a protein) is NOT enough for CROSS_INDICATION_TRANSFER or SYNTHETIC_LETHALITY.
  Those tiers require the corpus to explicitly describe the mechanism in a related context.
  Genuine ambiguity defaults to HYPOTHESIS.
- **Primary recommendation may be a standard tier.** If Phase 2 returns strong
  VALIDATED or BIOLOGICALLY_JUSTIFIED evidence, recommend that — novelty for its own
  sake is not the goal. The wildcard hypotheses augment the landscape; they do not
  replace well-supported candidates.
- **[PATHWAY INFERRED] guardrail from pathway-expert still applies.** Name a specific
  binding partner supported by the corpus — do not assign this tier to vague "must
  interact with something" statements.
- **Do not run more than 5 Phase 4 queries total.** If Phase 3 generates 5 hypotheses,
  pick the 3 most specific for individual hypothesis queries, then run the 2 standard
  cross-pathway/adjacent-indication queries. Do not scale queries linearly with hypotheses.
