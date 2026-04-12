---
name: molecular-biology-expert
description: >
  Query the curated scientific literature database to assess the biology, prior art,
  feasibility, and design challenges for a protein-protein interaction (PPI) target.
  Use whenever the user asks about the biology of a target complex, known inhibitors,
  what the literature says about a protein pair, or as the second step in the
  structural → literature → design pipeline after complex-structure-analysis.
  Trigger on: "what does the literature say about", "prior art for", "known inhibitors
  of", "has anyone targeted", "biology of [complex name]", "feasibility of targeting",
  "molecular biology analysis", or when a complex-structure-analysis report is present and
  the user asks to proceed with the pipeline. Requires the literature-db MCP server
  (search_corpus and get_fingerprint tools).
---

# Molecular Biology Expert: Literature-Based PPI Feasibility Analysis

This skill queries the curated biochemistry literature database to characterize a
protein-protein interaction target. The output is a structured report covering the
target's biological context, inhibitor prior art, experimentally validated interface
residues, and a design feasibility assessment — ready for consumption by a downstream
protein design agent.

## Prerequisites

Verify that `search_corpus` and `get_fingerprint` tools are available (literature-db
MCP server must be connected). Both tools will be used extensively.

If a `complex-structure-analysis` report is present in the conversation, read it before
starting — the PDB ID, hotspot residues, and interface characterization will sharpen
the literature queries and enable cross-referencing in the report.

---

## Phase 1: Identify the Target

Extract the following from the user's request or from a preceding chimerax report:

- **ProteinA** and **ProteinB** — the two proteins forming the complex
- **Target protein** — the protein whose surface the binder will engage (may differ
  from ProteinA/ProteinB ordering)
- **Organism** — human unless specified otherwise
- **Hotspot residues** — from chimerax report if available (e.g., "Phe69, Leu91,
  Arg89 on TEAD4")
- **Disease context** — cancer, viral, inflammatory, etc. (if known)

If the target complex is ambiguous (e.g., "KRAS" without specifying which effector),
ask the user to clarify before proceeding.

**Common name normalisation:** Use canonical protein names in queries. Examples:
- YAP1 / YAP / hYAP → "YAP"
- TEAD1/2/3/4 → "TEAD" (search broadly, then filter to isoform if needed)
- p53 / TP53 / tumor protein p53 → "p53"

---

## Phase 2: Multi-Query Corpus Search

A single query is insufficient. Run **3–5 targeted `search_corpus` calls** covering
different aspects of the target. Collect all results and deduplicate by DOI.

### Required queries (always run)

**Query 1 — Interaction biology:**
```
search_corpus  query="<ProteinA> <ProteinB> interaction mechanism binding"  top_k=8
```

**Query 2 — Inhibitor prior art:**
```
search_corpus  query="<ProteinA> <ProteinB> inhibitor peptide Ki Kd affinity"  top_k=8
```

**Query 3 — Mutagenesis and hotspots:**
```
search_corpus  query="<ProteinA> <ProteinB> mutagenesis alanine scanning hotspot residues ΔΔG"  top_k=5
```

### Conditional queries (run if applicable)

**Query 4 — Disease / in vivo relevance** (if disease context is known):
```
search_corpus  query="<ProteinA> <ProteinB> <cancer/tumor/signaling> in vivo cellular"  top_k=5
```

**Query 5 — Chimerax hotspot residues** (if a structural report is present):
```
search_corpus  query="<ResX> <ResY> <ProteinA> binding interface"  top_k=5
```
Use the 2–3 top hotspot residue names from the chimerax report.

### Deduplication

After all searches, collect unique papers by DOI (`paper_key` field). If the same
paper appears across multiple searches, keep only its highest relevance score.
Note how many total unique papers were found and how many came from each query.

---

## Phase 3: Retrieve Full Fingerprints

For the **top 5–8 unique papers** (ranked by score), retrieve complete fingerprints:

```
get_fingerprint  identifier=<doi>
```

Use the `paper_key` value from search results directly (e.g., `doi:10.1021/...`).
Strip the `doi:` prefix if the tool needs a bare DOI.

### Prioritisation

When choosing which papers to fetch in full, prioritise:
1. Score > 0.35 (high semantic relevance)
2. `study_type`: `experimental_in_vitro` or `experimental_structural` over `review`
   or `computational`
3. Non-null `affinities_kd_Molar` or `inhibitory_constant_Ki` (quantitative data)
4. Papers with `key_amino_acid_residues` that overlap with chimerax hotspots

Always fetch the top 3 papers by score regardless of study type.

### What to extract from each fingerprint

From `paper_metadata`:
- `title`, `doi`, `study_type`, `situational_context_hook`

From `key_findings[]` (for each finding):
- `claim`, `evidence_value`, `affinities_kd_Molar`, `inhibitory_constant_Ki`,
  `key_amino_acid_residues`, `protein_pair`, `experimental_context`,
  `confidence_score`, `source_span`

From `contradictions_and_negative_results[]`:
- `finding`, `conflicts_with_prior_work`, `reasoning`

From `entities`:
- `chemicals` (inhibitors, reagents), `proteins`

From `methodology`:
- `experimental_methods_used`, `instruments_used`

---

## Phase 4: Synthesise and Generate Report

Analyse across all retrieved fingerprints. Do not fabricate information — if a
section cannot be filled from the corpus, write "Not found in corpus." and note
whether the absence is informative (e.g., no prior inhibitors = novel target) or
simply a corpus coverage gap.

**Citation policy:** All cited claims must come from retrieved fingerprints.
- Citation format: DOI string only (e.g. `10.7554/eLife.25068`) + source_span.
  The `Source:` field in the KNOWN INHIBITORS table and the `Source (DOI, span)`
  column in the INTERFACE INSIGHTS table must use this format.
- No author names, journal names, or years. Write the DOI, not "Smith et al. 2023
  Nature" or "eLife 2024".
- If a fact is known from training knowledge but no corpus DOI exists for it, state
  the fact without any citation. Do not invent or guess a reference.

If chimerax hotspot residues are present, explicitly cross-reference them against
`key_amino_acid_residues` from each fingerprint. Normalise naming conventions when
comparing (e.g., "Phe69" = "F69" = "PHE69" = "hYAP Phe69").

Use `source_span` values from fingerprints to cite provenance (e.g., "Page 3,
Para 1" of DOI 10.7554/eLife.25068).

---

## Report Format

Use the exact section headers below — they are parsed by the downstream design
agent and the orchestrator skill.

**Token budget:** The full MOLECULAR BIOLOGY REPORT must fit in 2,500–3,500 words.
- Limit each inhibitor entry to 5 bullet lines.
- If more than 4 inhibitor classes are found, group minor variants under the closest
  class rather than adding new `####` blocks.
- Omit sub-sections that have no content — do not write empty placeholders.
- Literature sources table: max 8 rows.

```
## MOLECULAR BIOLOGY REPORT

### COMPLEX OVERVIEW
- Target complex: <ProteinA / ProteinB>
- Organism: <human / mouse / etc.>
- Disease context: <oncology / antiviral / inflammatory / other>
- Biological role: <one sentence>
- Drug target validation: <genetic knockdown, patient data, animal models — cite source_span>
- Papers analysed: <N full fingerprints> from <M total search hits>

### KNOWN INHIBITORS AND PRIOR ART

> If no inhibitors are found: "No inhibitors found in corpus — novel target or corpus gap."

For each inhibitor class (max 4 classes; group variants; max 5 bullets each):

#### <Inhibitor class / compound name>
- Modality: <small molecule / linear peptide / cyclic peptide / stapled peptide / antibody / protein domain>
- Best affinity: <Kd or Ki with units and assay — e.g. 18 nM Kd by SPR>
- Source: <DOI, source_span>
- Key residues engaged: <list, or omit if not reported>
- Limitations: <selectivity, permeability, stability — or omit if not reported>

### INTERFACE INSIGHTS FROM LITERATURE

#### Confirmed hotspots (mutagenesis / structural data)
| Residue | Protein | ΔΔG or effect | Method | Source (DOI, span) |
|---------|---------|---------------|--------|--------------------|
| ...     | ...     | ...           | ...    | ...                |

#### Residues tolerant of mutation
<Only include if data exists — omit sub-section entirely if not found in corpus.>

#### Cross-reference with structural report
- Confirmed by literature: <hotspot residues from structure report that appear in corpus mutagenesis data>
- Structural only (no experimental validation): <residues from structure report not found in corpus>
- Literature-only hotspots: <residues in corpus not in structure report — or omit if none>

If no structural report is present, omit this sub-section.

### FEASIBILITY ASSESSMENT
- Target tractability: <Excellent / Good / Marginal / Poor>
  - Excellent: mutagenesis-validated hotspot + prior peptide/protein binder ≤ 100 nM
  - Good: interface characterised + some inhibitor precedent
  - Marginal: limited data, only small-molecule or computational precedent
  - Poor: no characterisation, no precedent, or clear failure modes
- Corpus confidence: <High (≥ 5 papers) / Medium (2–4) / Low (0–1)>
- Design challenges: <bullet list — isoform redundancy, localisation, flat surface, etc.>
- Negative results: <what failed and why — cite source_span; omit if none>
- Missing data gaps: <what is absent from corpus that matters>

### DESIGN RECOMMENDATIONS
- Suggested modality: <cyclic peptide / mini-protein / stapled peptide / other> — reasoning in one sentence
- Residues to prioritise: <literature-validated hotspots>
- Residues to avoid: <tolerant-of-mutation residues — omit if none found>
- Epitope to mimic: <partner peptide or co-crystal epitope, if known>
- Affinity target: <e.g. "aim for ≤ 50 nM Kd — best prior art is 18 nM SPR (eLife.25068)">
- Key risk: <single most important design challenge — one sentence>

### LITERATURE SOURCES (max 8 rows)
| # | Title (truncated to 60 chars) | DOI | Study type | Score | Key finding |
|---|-------------------------------|-----|------------|-------|-------------|
| 1 | ...                           | ... | ...        | ...   | ...         |

### PIPELINE HANDOFF
- target_complex: <ProteinA / ProteinB>
- tractability: <Excellent | Good | Marginal | Poor>
- go_recommendation: <GO | CONDITIONAL_GO | NO_GO>
- go_rationale: <one sentence — the single most decisive reason for the recommendation>
- modality: <cyclic_peptide | mini_protein | stapled_helix | either>
- design_query: <one sentence — e.g. "Generate {modality} design inputs for {complex}, PDB {pdb_id}, target chain {chain}. Priority hotspots: {res_list}. Affinity target: {kd}. {key constraint if any}.">

**IMPORTANT:** Write the `### PIPELINE HANDOFF` section as plain bullet lines exactly as shown above.
Do NOT wrap it in a code fence (no ``` before or after). Do NOT omit the `- ` prefix.
The programmatic orchestrator parses these lines with a regex — any deviation breaks the pipeline.

Rules for `### PIPELINE HANDOFF`:
- `go_recommendation` must be exactly one of: `GO`, `CONDITIONAL_GO`, or `NO_GO`. Use the tractability rubric: Excellent/Good → GO; Marginal → CONDITIONAL_GO; Poor → NO_GO.
- `go_rationale` is a single sentence — the programmatic orchestrator displays this directly to the user.
- `design_query` is the verbatim query string passed to protein-design-script; include PDB ID, chain, top 3–5 hotspot residues, and suggested affinity target.

---

## Handoff Notes

The `INTERFACE INSIGHTS FROM LITERATURE` and `DESIGN RECOMMENDATIONS` sections are
formatted for downstream consumption by:
- **protein-design-script** — uses residues to prioritise, suggested modality, and
  affinity target to parameterise BoltzGen / RFD3 inputs
- **Orchestrator skill** (planned) — uses tractability rating and key risk to make
  go/no-go recommendations before committing compute

The `LITERATURE SOURCES` table enables traceability: any claim in the report can be
traced to a DOI and then to a `source_span` within the full fingerprint.

---

## Common Pitfalls

- **Run all queries before synthesising.** A single search misses complementary
  papers. The biology query and the inhibitor query often return different papers.
- **Deduplicate before fetching fingerprints.** Papers appear in multiple search
  results; fetch each DOI only once.
- **Do not fabricate.** If a section cannot be filled from retrieved fingerprints,
  write "Not found in corpus." Do not infer or hallucinate binding affinities,
  residue names, or inhibitor structures.
- **Score threshold.** If all search results score < 0.25, the corpus likely does not
  contain directly relevant papers. State this explicitly and note that the analysis
  is limited.
- **`get_fingerprint` identifier.** Use the `paper_key` value from search results
  (e.g. `doi:10.7554/eLife.25068`). If the tool requires a bare DOI, strip the
  `doi:` prefix.
- **Residue name normalisation.** When cross-referencing structure-tools hotspots
  against fingerprint `key_amino_acid_residues`, treat "Phe69", "F69", "PHE 69",
  and "hYAP Phe69" as equivalent. Match on residue number + one-letter or three-letter
  amino acid code.
- **Corpus bias.** The corpus is focused on biochemistry and biophysics. In vivo
  validation, clinical data, and ADMET properties are rarely in the corpus — flag
  their absence rather than treating it as evidence of absence.
