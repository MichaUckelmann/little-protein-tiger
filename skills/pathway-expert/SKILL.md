---
name: pathway-expert
description: >
  Query the curated literature database to characterise pathway biology for a disease
  context, identify dysregulated nodes, and recommend the highest-priority PPI target
  for therapeutic intervention. Use as the first step when the user provides a disease
  or cancer subtype without a specific PPI in mind, or when the question is "which node
  should we target in pathway X in disease Y?". Output feeds directly into
  complex-structure-analysis (provides PDB ID) and molecular-biology-expert (provides complex
  name and disease context). Requires literature-db MCP server.
  Trigger on: "which target in", "pathway analysis for", "what to target in",
  "disease mechanism", "KRAS pathway in", "Hippo pathway in", "what's dysregulated in",
  "target selection for", "which node should we target", or any question pairing a
  pathway name with a disease or cancer subtype without specifying a PDB or protein pair.
---

# Pathway Biology Expert

This skill queries the curated literature corpus to characterise signalling pathway
biology in a disease context, assess candidate target nodes, and recommend the optimal
PPI for the downstream design pipeline. It does not perform structural or biochemistry
analysis — that is the job of complex-structure-analysis and molecular-biology-expert.

The output is a structured `## PATHWAY BIOLOGY REPORT` containing a recommended PPI
and PDB ID(s) ready to pass directly into complex-structure-analysis.

---

## Phase 1: Extract Context from User Input

Identify:
- `disease_or_cancer` — e.g. "mesothelioma", "PDAC", "NSCLC", "HCC", "AML", "Alzheimer's", "rheumatoid arthritis"
- `pathway_hint` — optional; e.g. "Hippo", "KRAS signaling", "cGAS-STING", "SCAP-SREBP"
- `constraint` — optional; e.g. "focus on extracellular PPIs", "cyclic peptide accessible"
- `disease_category` — classify the disease into one of the categories below and resolve the three
  query term variables used in Phase 2. If the disease spans multiple categories, pick the most
  specific match; default to `other` if none fits.

| `disease_category` | `<disease_mechanism_term>` | `<dysregulation_term>` | `<protective_regulator_term>` |
|---|---|---|---|
| `oncology` | `cancer mechanism` | `oncogenic` | `tumor suppressor` |
| `neurodegeneration` | `neurodegeneration mechanism` | `pathological aggregation` | `neuroprotective regulator` |
| `autoimmune` | `autoimmune inflammation mechanism` | `pro-inflammatory` | `immunosuppressive regulator` |
| `metabolic` | `metabolic disease mechanism` | `pathological activation` | `metabolic regulator` |
| `infectious` | `infection pathogenesis mechanism` | `pathogenic` | `host defense regulator` |
| `cardiovascular` | `cardiovascular disease mechanism` | `pathological remodeling` | `cardioprotective regulator` |
| `fibrosis` | `fibrosis mechanism` | `pro-fibrotic` | `anti-fibrotic regulator` |
| `other` | `disease mechanism` | `pathological` | `disease-suppressing regulator` |

If disease is ambiguous (e.g. "lung disease" without subtype), proceed with the broad
term — do not block on ambiguity.

---

## Phase 2: Multi-Query Corpus Search

Run 3–4 `search_corpus` calls with `study_category="pathway_biology"`. Vary the query
angle to capture different aspects of the evidence:

Substitute `<disease_mechanism_term>`, `<dysregulation_term>`, and `<protective_regulator_term>`
with the values resolved from the `disease_category` mapping in Phase 1 before issuing any query.

**Query 1 — Disease-pathway mechanism** (top_k=8):
```
search_corpus
  query="<pathway_hint OR disease> signaling dysregulation <disease_mechanism_term>"
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

**Query 3 — Target node identification** (top_k=6):
```
search_corpus
  query="<pathway_hint> effector transcription factor <dysregulation_term> <disease>"
  study_category="pathway_biology"
  top_k=6
```

**Query 4 — Upstream regulators** (optional, run if pathway_hint is known, top_k=5):
```
search_corpus
  query="<pathway_hint> upstream regulator mutation inactivation <protective_regulator_term> <disease>"
  study_category="pathway_biology"
  top_k=5
```

Deduplicate by DOI across all queries. Retain the highest score per paper.

### Corpus gap fallback

If all `study_category="pathway_biology"` results have scores < 0.20 or return empty,
re-run the same queries **without** the `study_category` filter. Many existing
biochemistry papers have incidental pathway context in their situational_context_hook.

**Always state the fallback explicitly in the report** — see CORPUS COVERAGE section.

---

## Phase 3: Retrieve Full Fingerprints

Call `get_fingerprint` for the top 5–8 unique papers from Phase 2. Prioritise papers
where the search result indicates a non-null `pathway_context` (check if the
`study_category` field in the result is "pathway_biology").

From each fingerprint, extract:
- `pathway_context.pathways` — which pathways are covered
- `pathway_context.disease_associations` — disease, mechanism, mutation_frequency, source_span
- `pathway_context.target_nodes` — protein, pathway_position, dysregulation, genetic_dependency_evidence, prior_therapeutic_targeting, suggested_pdb_structures, source_span
- `pathway_context.pathway_logic` — ON/OFF logic summary
- `pathway_context.redundancy_risks`
- `pathway_context.upstream_regulators`, `downstream_effectors`
- `key_findings` — incidental biochemistry findings that may be relevant
- `paper_metadata` — title, doi, study_type

---

## Phase 3.5: Term-Expansion Search (Query Expansion Round)

After retrieving fingerprints, extract corpus-specific terms that did **not** appear in
any of the Phase 2 query strings. These are high-value expansion candidates:

| Source field | What to extract |
|---|---|
| `pathway_context.target_nodes[].protein` | Gene symbols of newly surfaced target proteins |
| `pathway_context.upstream_regulators` | Regulator gene/protein names not in original query |
| `pathway_context.downstream_effectors` | Effector gene/protein names not in original query |
| `pathway_context.disease_associations[].mutation_frequency` | Specific mutation types (e.g. "NF2 loss", "LATS1/2 deletion") |

**Always run Phase 3.5.** The only exception is when Phase 2 returned ≥ 12 unique papers
with scores ≥ 0.40 — in that case the corpus is saturated and expansion yields diminishing
returns. In all other situations, run it.

Otherwise, identify the **2–3 most specific new terms** (prefer gene symbols over pathway
names, which were likely already queried). Run at most **2 follow-up searches**:

**Expansion Query A — Specific node in disease context** (top_k=6):
```
search_corpus
  query="<new_gene_symbol_1> [<new_gene_symbol_2>] <disease> mechanism dependency"
  study_category="pathway_biology"
  top_k=6
```

**Expansion Query B — Node interaction / complex** (top_k=5, only if a second distinct
gene symbol was identified):
```
search_corpus
  query="<new_gene_symbol_1> <new_gene_symbol_2> interaction complex <disease>"
  top_k=5
```

Deduplicate expansion results against all DOIs already collected from Phase 2. For any
**new** papers (DOIs not seen before) with score ≥ 0.25, call `get_fingerprint` and
merge their extracted fields into the working set before Phase 4.

**State in the CORPUS COVERAGE section** how many new unique papers the
expansion round added and which expansion terms triggered them.

---

## Phase 3.6: Corpus-Wide PDB Lookup

After completing Phases 3 and 3.5, call `find_pdb_structures` **once** with every
candidate target protein identified across all phases:

```
find_pdb_structures
  proteins=["<ProteinA>", "<ProteinB>", "<ProteinC>", ...]
```

Include all proteins from `target_nodes`, `upstream_regulators`, and any PPI candidates
you intend to include in the TARGET OPPORTUNITY LANDSCAPE — typically 4–8 gene symbols.

This scans the **entire** fingerprint corpus, including papers not retrieved in Phase 2–3,
and returns PDB accessions from two sources: `suggested_pdb_structures` in pathway
fingerprints and `pdb_accessions` from any paper mentioning the protein.

When multiple structures are returned for a protein, select the best one using these
criteria **in priority order**:

1. **Complex present** — `protein_chain_count ≥ 2` and the `entities[].description`
   fields mention both the target protein AND its binding partner. A co-complex is
   far more useful than a monomer for interface-based design.
2. **Method quality** — prefer `X-RAY DIFFRACTION` > `ELECTRON MICROSCOPY` > `NMR`
   (NMR structures lack resolution values and are generally not suitable for interface
   design; cryo-EM is acceptable above 4 Å).
3. **Resolution** — for X-ray structures, lower `resolution_A` is better; prefer < 2.5 Å
   when available. For cryo-EM, prefer < 4.0 Å.
4. **Human organism** — prefer entries where `entities[].organism_taxid = 9606`.

If `metadata_available` is `false` in the tool response (cache not yet populated),
use the PDB IDs as-is and note in the report that resolution/method data is unavailable.

- Add any newly discovered PDB IDs to the relevant node before writing the report.
- Use the selected ID verbatim in `Suggested PDB ID(s)` fields and in the
  `### PIPELINE HANDOFF` `pdb_id` line.
- If `total_found` is 0, call `search_rcsb_pdb` with the 2–3 primary target proteins
  as a fallback. Apply the same selection criteria to the RCSB results. If a suitable
  structure is found, use its PDB ID in the handoff. If no suitable structure is found
  from either source, write `NOT_FOUND`.

---

## Phase 4: Synthesise and Output Report

### 4a — Inferred PPI reasoning (run before writing the report)

For every dysregulated node where `prior_therapeutic_targeting` is null or "None found
in corpus", explicitly reason through the following before writing the report:

1. **Interaction necessity**: Does this protein's known pathway role require it to
   physically engage a specific partner to exert its disease-relevant activity?
   Use `pathway_logic`, `upstream_regulators`, `downstream_effectors`, and
   `key_findings` as evidence sources.
2. **Therapeutic mechanism — disrupt or stabilize?** Determine which design intent
   applies to this node:
   - `disrupt` — breaking the interaction attenuates the disease-relevant output
     (e.g. loss of complex formation → loss of oncogenic signalling, loss of
     transcriptional co-activation, failure to relay a pathological signal)
   - `stabilize` — the disease mechanism is that a normally protective or
     autoinhibitory interaction is *lost* or *weakened*; reinforcing it restores
     the healthy state (e.g. restoring an autoinhibitory complex, re-engaging a
     sequestered OFF-state, protecting a tumour suppressor complex from degradation)
   Record one `design_intent` per node. Default to `disrupt` if ambiguous.
3. **Interaction knowability**: Is the specific binding partner named or strongly
   implied in the corpus (e.g. a co-activator, scaffold, receptor partner)?
   Do not infer a generic "this protein must bind something" — name the partner.

If all three conditions are met, label this node **[PATHWAY INFERRED]** in the report.
If genetic dependency is also confirmed, label it **[BIOLOGICALLY JUSTIFIED]**.
If prior therapeutic targeting is documented in the corpus, label it **[VALIDATED]**.

A node may carry only one label — use the highest tier supported by evidence.

---

Produce the full `## PATHWAY BIOLOGY REPORT` using **only information retrieved from
the corpus**. Do not hallucinate pathway details. If a section cannot be filled from
retrieved fingerprints, omit the line entirely — do not write "Not found in corpus"
placeholders for empty fields.

**Citation policy:** Every cited claim must come from a retrieved fingerprint.
- Citation format: `DOI + source_span` (e.g. `10.7554/eLife.77415, Page 3 Para 2`).
- No author names, journal names, or years — ever. Write the DOI only.
- If you know a biological fact from training knowledge but have no corpus DOI for it,
  state the fact without any citation — do not invent a reference.
- A fact with no citation is acceptable. A fact with a fabricated citation is not.

**Token budget:** The full PATHWAY BIOLOGY REPORT must fit in 3,000–4,500 words.
Write bullet points, not prose paragraphs. Each section should be a tight list.

### Report Format

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

For each candidate target node — max 4 nodes total, 6 bullet lines per node:

#### <ProteinName (gene symbol)>
- Pathway position: <pathway_position>
- Dysregulation: <one sentence — cite source_span + DOI>
- Genetic dependency: <evidence, or omit if absent>
- Prior therapeutic strategies: <prior targeting, or omit if absent>
- Suggested PDB structures: <IDs, or omit if absent>
- Inferred PPI opportunity: <max 2 sentences: named partner, evidence, consequence of disruption — or "Insufficient evidence." if Phase 4a conditions not met>

### TARGET OPPORTUNITY LANDSCAPE

Present 2–3 PPI candidates, each assigned an evidence tier from Phase 4a.
List highest tier first. Within the same tier, rank by PDB availability.

For each candidate:

#### [<TIER>] <ProteinA / ProteinB>
- **Evidence basis**: <1–2 sentences citing the specific corpus evidence — genetic
  dependency result, pathway logic, prior drug program, or inferred interaction necessity>
- **What makes it attractive**: <therapeutic rationale — pathway position, druggable interface, unmet need>
- **Key uncertainty**: <what is not yet established — no therapeutic precedent, no structure, redundancy risk>
- **Suggested PDB ID(s)**: <verbatim from fingerprint fields only; "Not found in corpus" if absent>

Tier definitions for the header labels:
- [VALIDATED]           — Prior therapeutic targeting documented in corpus
- [BIOLOGICALLY JUSTIFIED] — Genetic dependency confirmed; interaction necessity supported
                             by pathway evidence; no therapeutic precedent in corpus
- [PATHWAY INFERRED]    — No genetic dependency data; but named interaction is required
                          for disease-relevant pathway output per corpus logic

### PRIMARY RECOMMENDATION

State which candidate from the TARGET OPPORTUNITY LANDSCAPE is recommended for
the downstream pipeline, and why (1–2 sentences balancing evidence strength,
structural tractability, and novelty value). If the user has provided a constraint
(e.g. "focus on unexplored targets"), weight accordingly.

- **Target complex**: <ProteinA / ProteinB>
- **Evidence tier**: <[VALIDATED] / [BIOLOGICALLY JUSTIFIED] / [PATHWAY INFERRED]>
- **Suggested PDB ID(s)**: <verbatim from corpus; "Not found in corpus" if absent —
  provide RCSB search terms and ask user to confirm before proceeding>
- **Proposed next step**: Run complex-structure-analysis on PDB <ID>
  (only if PDB confirmed from corpus; otherwise await user input)

Selection criteria (apply in order, but surface all tiers in the landscape regardless):
1. Genetic dependency confirmed (CRISPR essential in disease-relevant lines) — strongest evidence
2. Pathway position as a convergence node (multiple upstream alterations feed into it)
3. Prior therapeutic targeting attempts — validates the interface is engageable
4. Inferred interaction necessity — named partner, corpus-supported pathway logic,
   predictable consequence of disruption (see Phase 4a)
5. Known PDB structure available — required for the downstream chimerax step
6. Accessible interface — prefer extracellular or surface-exposed over buried enzymatic sites

If no recommendation can be made from corpus data alone, state this explicitly and
suggest the user run `python scripts/fetch_papers.py` with specific pathway keywords.

### REDUNDANCY AND RESISTANCE RISKS
- <max 3 bullet points from redundancy_risks — compensatory proteins, dual-targeting need, resistance gaps>

### CORPUS COVERAGE
- Papers analysed: <N fingerprints> from <M search hits>
- Fallback used: <Yes / No — if yes, which queries>
- Expansion round: <Yes / No — terms used, new papers added>
- Critical gaps: <specific missing nodes or diseases, one line each — omit section if none>
- Keywords to add (if gaps are critical): `python scripts/fetch_papers.py --keywords "<keyword>"`

### PATHWAY SOURCES
| # | Title (truncated to 60 chars) | DOI | Key node |
|---|-------------------------------|-----|----------|
| 1 | ...                           | ... | ...      |

### PIPELINE HANDOFF
- pdb_id: <PDB accession from corpus (pdb_accessions or suggested_pdb_structures fields only), or NOT_FOUND>
- target_complex: <ProteinA / ProteinB>
- design_intent: <disrupt | stabilize — from Phase 4a reasoning for the PRIMARY RECOMMENDATION>
- structure_query: <one sentence — e.g. "Analyze PDB {pdb_id} at data/structures/{pdb_id}.cif. Target chain {chain} ({ProteinA}). Partner chain {chain} ({ProteinB}). Identify hotspot residues for {modality} design.">
- choices_json: <compact JSON array — see format below>

**IMPORTANT:** Write the `### PIPELINE HANDOFF` section as plain bullet lines exactly as shown above.
Do NOT wrap it in a code fence (no ``` before or after). Do NOT omit the `- ` prefix.
The programmatic orchestrator parses these lines with a regex — any deviation breaks the pipeline.

Rules for `### PIPELINE HANDOFF`:
- `pdb_id` must come from `paper_metadata.pdb_accessions`, `pathway_context.target_nodes[].suggested_pdb_structures` in retrieved fingerprints, **or the `find_pdb_structures` tool result from Phase 3.6**. Write `NOT_FOUND` if nothing found — never guess.
- `structure_query` is the verbatim query string passed to complex-structure-analysis by the programmatic orchestrator; make it self-contained (include the local file path `data/structures/{pdb_id}.cif`).
- `choices_json` must be a single-line JSON array listing every candidate from TARGET OPPORTUNITY LANDSCAPE in the same order. Each element has exactly these keys:
  - `tier`: one of `"VALIDATED"`, `"BIOLOGICALLY_JUSTIFIED"`, or `"PATHWAY_INFERRED"`
  - `complex`: the protein pair name exactly as written in the `####` header (e.g. `"YAP1 / TEAD4"`)
  - `pdb_ids`: array of PDB accession strings from corpus only — empty array `[]` if none found
  - `evidence_basis`: one sentence summary of the evidence (no newlines, no quotes inside the string)
  - `key_uncertainty`: one sentence summary of the key uncertainty (no newlines, no quotes inside the string)
  - `design_intent`: `"disrupt"` or `"stabilize"` from Phase 4a reasoning for this candidate

  Example (must be on ONE line, no line breaks inside):
  `- choices_json: [{"tier":"VALIDATED","complex":"YAP1 / TEAD4","pdb_ids":["5GN0","8J9A"],"evidence_basis":"Mesothelioma xenograft regression confirmed upon YAP-TEAD inhibition.","key_uncertainty":"TAZ paralog redundancy may require dual targeting."},{"tier":"BIOLOGICALLY_JUSTIFIED","complex":"NF2 / LATS1","pdb_ids":[],"evidence_basis":"CRISPR dependency confirmed in NF2-null cell lines.","key_uncertainty":"No structural data in corpus."}]`

  Use straight double-quotes only. No trailing commas. Escape any double-quote inside a string value as `\"`.

---

## Handoff Contract

The `PRIMARY RECOMMENDATION` (within TARGET OPPORTUNITY LANDSCAPE) is the primary
handoff to downstream skills:

- **→ complex-structure-analysis**: use `Suggested PDB ID(s)` and `Target complex` from PRIMARY RECOMMENDATION
- **→ molecular-biology-expert**: use `Target complex` as the protein pair to query
- **→ orchestrator**: use the full report for Stage 0 summary; extract complex + PDB from PRIMARY RECOMMENDATION

The `TARGET OPPORTUNITY LANDSCAPE` is for human review only — the orchestrator and
downstream skills consume only the `PRIMARY RECOMMENDATION` block.

If the pathway-expert is invoked from within the orchestrator (Stage 0), the orchestrator
extracts the `PRIMARY RECOMMENDATION` block and passes it to Stage 1 automatically.

---

## Common Pitfalls

- If corpus has zero pathway_biology papers, the `study_category` filter will return
  nothing — always fall back to unfiltered search and report the gap transparently.
- **Never guess PDB accession codes.** Only report PDB IDs that appear verbatim in
  `pathway_context.target_nodes[].suggested_pdb_structures` or `paper_metadata.pdb_accessions`
  of retrieved fingerprints. Do not supply codes from your training knowledge — LLM recall
  of 4-character accession codes is unreliable (e.g. confusing 4B7F with 4U6V). If no PDB
  is present in any fingerprint for the recommended complex, write `"Not found in corpus"`
  and provide the user with RCSB search terms to look it up manually.
- Redundancy risks are easy to miss — always check `pathway_context.redundancy_risks`
  across ALL retrieved fingerprints, not just the top-scoring one.
- The recommended PPI must be a protein-protein interaction, not a single protein or
  enzymatic active site — the downstream chimerax skill requires a two-chain complex.
- In Phase 3.5, do not re-query with terms already present in any Phase 2 query string —
  this yields near-duplicate results and wastes calls. Only use genuinely new gene symbols
  or mutation terms surfaced by the fingerprints.
- Do not run more than 2 expansion queries. If Phase 3.5 surfaces a large number of new
  terms, pick the 2–3 most specific (gene symbols > pathway names > disease terms) and
  stop. More expansion rounds have sharply diminishing returns on a focused corpus.
- **[PATHWAY INFERRED] guardrail**: an inferred PPI target must name a *specific* binding
  partner supported by the corpus (e.g. co-activator, scaffold, receptor subunit). Do not
  assign this tier to vague statements like "this protein must interact with a partner" or
  "complex formation is implied" — name the partner or write "Insufficient evidence."
- **Do not conflate tiers**: the absence of a drug program ([VALIDATED] missing) does not
  itself make a target [BIOLOGICALLY JUSTIFIED] — that tier requires confirmed genetic
  dependency. Pathway inference alone is [PATHWAY INFERRED], regardless of how compelling
  the biology looks.
- **Disease agnosticism**: avoid disease-specific framing (e.g. "tumour suppressor",
  "oncogenic"). Use neutral language: "pathway suppressor", "disease-relevant activity",
  "pathological signalling output", "dysregulated node". The pipeline applies to any
  indication, not only cancer.
