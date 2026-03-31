---
name: molecular-biology-expert
description: >
  Query the curated scientific literature database to assess the biology, prior art,
  feasibility, and design challenges for a protein-protein interaction (PPI) target.
  Use whenever the user asks about the biology of a target complex, known inhibitors,
  what the literature says about a protein pair, or as the second step in the
  structural → literature → design pipeline after chimerax-ppi-analysis.
  Trigger on: "what does the literature say about", "prior art for", "known inhibitors
  of", "has anyone targeted", "biology of [complex name]", "feasibility of targeting",
  "molecular biology analysis", or when a chimerax-ppi-analysis report is present and
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

If a `chimerax-ppi-analysis` report is present in the conversation, read it before
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

If chimerax hotspot residues are present, explicitly cross-reference them against
`key_amino_acid_residues` from each fingerprint. Normalise naming conventions when
comparing (e.g., "Phe69" = "F69" = "PHE69" = "hYAP Phe69").

Use `source_span` values from fingerprints to cite provenance (e.g., "Page 3,
Para 1" of DOI 10.7554/eLife.25068).

---

## Report Format

Use the exact section headers below — they are parsed by the downstream design
agent and the orchestrator skill.

```
## MOLECULAR BIOLOGY REPORT

### COMPLEX OVERVIEW
- Target complex: <ProteinA / ProteinB>
- Organism: <human / mouse / etc.>
- Disease context: <oncology / antiviral / inflammatory / other>
- Biological role: <what the interaction does — 1–2 sentences>
- Drug target validation: <genetic knockdown, patient data, animal models — cite source_span>
- Papers analysed: <N full fingerprints> from <M total search hits>

### KNOWN INHIBITORS AND PRIOR ART

> If no inhibitors are found: "No inhibitors found in corpus. This may indicate a
> novel target or a corpus coverage gap — consider supplementing with a web search."

For each inhibitor class or compound series found:

#### <Inhibitor class / compound name>
- Modality: <small molecule / linear peptide / cyclic peptide / stapled peptide /
  antibody / nanobody / protein domain>
- Best reported affinity: <Kd or Ki, include units; e.g. 18 nM Kd by SPR>
- Source: <DOI, source_span>
- Assay: <SPR / ITC / FP / AlphaScreen / BLI / etc.>
- Key residues engaged on target: <list, if reported>
- Cell/in vivo efficacy: <IC50 in cells, animal data — or "not reported">
- Limitations noted in paper: <selectivity, permeability, metabolic stability, etc.>

### INTERFACE INSIGHTS FROM LITERATURE

Residues experimentally validated as important for binding:

#### Confirmed hotspots (mutagenesis / structural data)
| Residue | Protein | ΔΔG or effect | Method | Source (DOI, span) |
|---------|---------|---------------|--------|--------------------|
| ...     | ...     | ...           | ...    | ...                |

#### Residues tolerant of mutation (not hotspots)
List residues that were mutated with minimal effect on binding, if reported.
These should be avoided as design anchors.

#### Cross-reference with structural report
If a chimerax-ppi-analysis report is present, explicitly state:
- Which chimerax hotspot residues are **confirmed** by literature
- Which are **not found** in the corpus (structural prediction only, no experimental validation)
- Any additional hotspots from literature **not identified** by ChimeraX

If no structural report is present, write: "No chimerax-ppi-analysis report present —
cross-referencing not applicable."

### FEASIBILITY ASSESSMENT
- Target tractability: <Excellent / Good / Marginal / Poor>
  Use this rubric:
  - **Excellent** — Known hotspot validated by mutagenesis; prior peptide or protein
    binder with ≤ 100 nM affinity reported
  - **Good** — Interface biochemically characterised; some inhibitor precedent (even
    if weak or small-molecule only); surface character suitable for binder engagement
  - **Marginal** — Limited interface data; only small-molecule or stapled peptide
    precedent; flat/polar surface; or only computational predictions
  - **Poor** — No interface characterisation; no inhibitor precedent; intrinsically
    disordered target; or clear failure modes reported
- Corpus confidence: <High (≥ 5 relevant papers) / Medium (2–4) / Low (0–1)>
- Design challenges:
  - <List specific challenges: isoform redundancy, intracellular localisation,
    resistance mutations, flat/polar interface, glycosylation, flexibility, etc.>
- Negative results from literature:
  - <What has been tried and failed, and why — cite source_span>
- Missing data gaps:
  - <What is not in the corpus that would be important to know>

### DESIGN RECOMMENDATIONS
- Suggested binder modality: <cyclic peptide / mini-protein / stapled peptide / other>
  Reasoning: <based on prior art modality, interface size from chimerax if available,
  and surface character>
- Residues to prioritise (literature-validated hotspots): <list with confidence>
- Residues to avoid as design anchors: <tolerant-of-mutation residues, if known>
- Binding epitope to mimic: <if a partner peptide or known binder co-crystal exists,
  describe what the binder must replicate>
- Suggested affinity target: <based on best reported prior art; e.g. "aim for ≤ 50 nM
  Kd — best prior art is 18 nM by SPR (eLife.25068)">
- Key risk: <the single most important design challenge in one sentence>

### LITERATURE SOURCES
| # | Title (truncated to 60 chars) | DOI | Study type | Score | Key quantitative finding |
|---|-------------------------------|-----|------------|-------|--------------------------|
| 1 | ...                           | ... | ...        | ...   | ...                      |
```

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
- **Residue name normalisation.** When cross-referencing chimerax hotspots against
  fingerprint `key_amino_acid_residues`, treat "Phe69", "F69", "PHE 69", and
  "hYAP Phe69" as equivalent. Match on residue number + one-letter or three-letter
  amino acid code.
- **Corpus bias.** The corpus is focused on biochemistry and biophysics. In vivo
  validation, clinical data, and ADMET properties are rarely in the corpus — flag
  their absence rather than treating it as evidence of absence.
