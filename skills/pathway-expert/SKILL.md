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
- `disease_or_cancer` — e.g. "mesothelioma", "PDAC", "NSCLC", "HCC", "AML"
- `pathway_hint` — optional; e.g. "Hippo", "KRAS signaling", "cGAS-STING", "SCAP-SREBP"
- `constraint` — optional; e.g. "focus on extracellular PPIs", "cyclic peptide accessible"

If disease is ambiguous (e.g. "lung cancer" without subtype), proceed with the broad
term — do not block on ambiguity.

---

## Phase 2: Multi-Query Corpus Search

Run 3–4 `search_corpus` calls with `study_category="pathway_biology"`. Vary the query
angle to capture different aspects of the evidence:

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

**Query 3 — Target node identification** (top_k=6):
```
search_corpus
  query="<pathway_hint> effector transcription factor oncogenic <disease>"
  study_category="pathway_biology"
  top_k=6
```

**Query 4 — Upstream regulators** (optional, run if pathway_hint is known, top_k=5):
```
search_corpus
  query="<pathway_hint> upstream regulator mutation inactivation tumor suppressor <disease>"
  study_category="pathway_biology"
  top_k=5
```

Deduplicate by DOI across all queries. Retain the highest score per paper.

### Corpus gap fallback

If all `study_category="pathway_biology"` results have scores < 0.20 or return empty,
re-run the same queries **without** the `study_category` filter. Many existing
biochemistry papers have incidental pathway context in their situational_context_hook.

**Always state the fallback explicitly in the report** — see CORPUS COVERAGE ASSESSMENT.

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

**Skip Phase 3.5** entirely if:
- The user's original query already named a specific protein pair or pathway node, AND
- Phase 2 returned ≥ 8 unique papers with scores ≥ 0.30

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

**State in the CORPUS COVERAGE ASSESSMENT section** how many new unique papers the
expansion round added and which expansion terms triggered them.

---

## Phase 4: Synthesise and Output Report

Produce the full `## PATHWAY BIOLOGY REPORT` using **only information retrieved from
the corpus**. Do not hallucinate pathway details. If a section cannot be filled from
retrieved fingerprints, write "Not found in corpus."

### Report Format

```
## PATHWAY BIOLOGY REPORT

### DISEASE CONTEXT
- Disease / cancer subtype: <name>
- Pathway(s) implicated: <list from pathway_context.pathways across papers>
- Primary oncogenic mechanism: <mechanism from disease_associations — cite DOI + source_span>
- Frequency of pathway activation: <mutation_frequency if stated, else "not stated in corpus">

### PATHWAY MAP
- Upstream suppressors / regulators: <list with mutation types in disease — from upstream_regulators + disease_associations>
- Core cascade logic: <pathway_logic from retrieved fingerprints, or reconstruct from disease_associations>
- Effectors / transcription factors: <from downstream_effectors>
- Known downstream transcriptional targets: <from downstream_effectors or key_findings>

### DYSREGULATED NODES ASSESSMENT

For each candidate target node aggregated from target_nodes across all fingerprints:

#### <ProteinName (gene symbol)>
- Pathway position: <pathway_position>
- Dysregulation in disease: <dysregulation — cite source_span + DOI>
- Genetic dependency evidence: <genetic_dependency_evidence, or "Not found in corpus">
- Prior therapeutic strategies: <prior_therapeutic_targeting, or "None found in corpus">
- Suggested PDB structures: <suggested_pdb_structures, or "None mentioned">
- Targetability note: <assess: intracellular vs extracellular; PPI vs enzymatic; accessible interface?>

### RECOMMENDED PPI TARGET

Based on the evidence above, the highest-priority PPI for intervention is:

- **Target complex**: <ProteinA / ProteinB>
- **Rationale**: <2–3 sentences: why this node, why this interaction, what evidence supports it>
- **Suggested PDB ID(s)**: <from suggested_pdb_structures or known structures — list all available>
- **Proposed next step**: Run complex-structure-analysis on PDB <ID>, target chain = <protein>

Selection criteria (apply in order):
1. Genetic dependency confirmed (CRISPR essential in disease-relevant lines) — strongest evidence
2. Pathway position as a convergence node (multiple upstream suppressors feed into it)
3. Prior therapeutic targeting attempts — validates the surface is engageable
4. Known PDB structure available — required for the downstream chimerax step
5. Extracellular or interface-accessible — prefer over deep intracellular enzymatic sites

If no clear recommendation can be made from corpus data alone, state this explicitly and
suggest the user run `python scripts/fetch_papers.py` with specific pathway keywords.

### REDUNDANCY AND RESISTANCE RISKS
- <List from pathway_context.redundancy_risks across all retrieved fingerprints>
- <Note any compensatory proteins that might rescue loss of the recommended target>
- <Flag if dual-targeting may be required>

### CORPUS COVERAGE ASSESSMENT
- Papers with study_category=pathway_biology found: <N>
- Papers with pathway_context populated: <N>
- Pathways covered in corpus: <list>
- Coverage gaps: <diseases or nodes not found in corpus — be specific>
- Fallback used: <Yes / No — if yes, explain which queries fell back to unfiltered search>
- Expansion round run: <Yes / No — if skipped, state why (early-exit condition met)>
- Expansion terms used: <gene symbols or mutation terms that triggered follow-up queries>
- New papers added by expansion: <N — unique DOIs not found in Phase 2>
- Recommended keywords to add (if gaps are critical):
  - "<keyword 1 in NCBI format>"
  - "<keyword 2 in NCBI format>"
  Run: `python scripts/fetch_papers.py --keywords "<keyword>"` then re-curate and re-ingest.

### PATHWAY SOURCES
| # | Title (truncated) | DOI | Study type | Category | Key node |
|---|-------------------|-----|------------|----------|----------|
| 1 | ...               | ... | ...        | ...      | ...      |
```

---

## Handoff Contract

The `RECOMMENDED PPI TARGET` section is the primary handoff to downstream skills:

- **→ complex-structure-analysis**: use `Suggested PDB ID(s)` and `Target complex` as inputs
- **→ molecular-biology-expert**: use `Target complex` as the protein pair to query
- **→ orchestrator**: use the full report for Stage 0 summary; extract complex + PDB

If the pathway-expert is invoked from within the orchestrator (Stage 0), the orchestrator
extracts the `RECOMMENDED PPI TARGET` block and passes it to Stage 1 automatically.

---

## Common Pitfalls

- If corpus has zero pathway_biology papers, the `study_category` filter will return
  nothing — always fall back to unfiltered search and report the gap transparently.
- Do not fill `suggested_pdb_structures` by guessing PDB IDs — only report what is
  explicitly stated in retrieved fingerprints or well-known structures you are certain of.
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
