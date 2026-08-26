---
name: molecular-biology-expert
description: >
  Invoke ONLY when the user explicitly asks for it by name or clearly
  requests this specific workflow; do not trigger it from a general
  question, which you can answer better from your own knowledge than from
  this narrow corpus.
  Query the curated scientific literature database to assess the biology, prior art,
  feasibility, and design challenges for a candidate binder target — either a
  protein-protein interaction (PPI; default) or a single-protein active-site /
  allosteric-pocket target when the pathway-expert handoff specifies
  design_intent=inhibit_active_site. Runs BEFORE complex-structure-analysis in the
  pipeline: emits literature-derived residue hints (target_site_hint) that the
  structure-analysis stage uses to focus its geometry pass. Also uses corpus graph
  and DepMap tools when the target is a cancer / disease pathway node.
  Trigger on: "what does the literature say about", "prior art for", "known inhibitors
  of", "has anyone targeted", "biology of [target]", "feasibility of targeting",
  "target feasibility", "molecular biology analysis", or as stage 1 of the
  pathway → mol-bio → structure → design pipeline. Requires the literature-db MCP
  server (search_corpus, get_fingerprint, graph tools).
---

# Molecular Biology Expert: Target Feasibility Analysis

This skill queries the curated biochemistry literature database to characterize a
candidate binder target. It works for both PPI targets (the default — characterise
both partners, the interface, and prior PPI-disruptor or stabilizer art) and
single-protein direct-inhibition targets (active site or allosteric pocket,
characterise the pocket residues and prior peptide/macrocycle inhibitor art).

The output is a structured report covering biological context, inhibitor prior art,
literature-derived candidate residues, and a design feasibility assessment.
Crucially, the report emits a `target_site_hint` field in the PIPELINE HANDOFF —
this is consumed by the next stage (complex-structure-analysis) to focus its
geometry pass on the residues literature has already implicated.

## Prerequisites

Verify that `search_corpus`, `get_fingerprint`, and the graph tools (`cluster_for_protein`,
`get_genetic_codependency`, `find_cocorrelated_genes`, `shortest_interaction_path`)
are available — literature-db MCP server must be connected.

You run **before** complex-structure-analysis in the pipeline. There is no
structure report yet to cross-reference against. Your job is to give the structure
expert the best literature-derived starting point.

---

## Phase 1: Identify the Target

Extract the following from the user's request or the pathway-expert handoff:

- **For PPI targets** (most common):
  - **ProteinA** and **ProteinB** — the two proteins forming the complex
  - **Target protein** — the protein whose surface the binder will engage
- **For direct-inhibition targets** (pathway-expert handoff sets `design_intent=inhibit_active_site`):
  - **Target protein** — the single enzyme / pocket-bearing protein
  - **Site type** — active site / allosteric pocket / cryptic pocket
- **Organism** — human unless specified otherwise
- **Disease context** — cancer, viral, inflammatory, etc. (if known)
- **design_intent** from pathway-expert handoff: `disrupt`, `stabilize`, or `inhibit_active_site`

If the target is ambiguous (e.g., "KRAS" without specifying which effector), ask the
user to clarify before proceeding.

**Common name normalisation:** Use canonical protein names in queries. Examples:
- YAP1 / YAP / hYAP → "YAP"
- TEAD1/2/3/4 → "TEAD" (search broadly, then filter to isoform if needed)
- p53 / TP53 / tumor protein p53 → "p53"

---

## Phase 2: Multi-Query Corpus Search

A single query is insufficient. Run **3–5 targeted `search_corpus` calls** covering
different aspects of the target. Collect all results and deduplicate by DOI.

### Required queries (always run)

Adapt the query wording to the design_intent:

**Query 1 — Target biology:**
```
# PPI target:
search_corpus  query="<ProteinA> <ProteinB> interaction mechanism binding"  top_k=8
# Direct-inhibition target:
search_corpus  query="<ProteinName> active site catalytic mechanism structure"  top_k=8
```

**Query 2 — Inhibitor prior art:**
```
# PPI target:
search_corpus  query="<ProteinA> <ProteinB> inhibitor peptide Ki Kd affinity"  top_k=8
# Direct-inhibition target:
search_corpus  query="<ProteinName> macrocyclic peptide inhibitor Ki IC50 active site"  top_k=8
```

**Query 3 — Key residues:**
```
# PPI target:
search_corpus  query="<ProteinA> <ProteinB> mutagenesis alanine scanning hotspot residues ΔΔG"  top_k=5
# Direct-inhibition target:
search_corpus  query="<ProteinName> catalytic residues active site mutation activity"  top_k=5
```

### Conditional queries (run if applicable)

**Query 4 — Disease / in vivo relevance** (if disease context is known):
```
search_corpus  query="<target> <cancer/tumor/signaling> in vivo cellular"  top_k=5
```

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
4. Papers with `key_amino_acid_residues` populated — these residue lists are exactly
   what feeds the `target_site_hint` handoff to the structure stage.

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

## Phase 4: Pathway / DepMap context (when applicable)

This phase runs only when the target is a human protein in a disease context
(cancer, immune, metabolic, etc.). For viral / structural / academic-mechanism
targets, skip and proceed to Phase 5.

The graph tools answer questions that semantic search can't:

- **`cluster_for_protein(<target>)`** — what co-functional module does this
  protein sit in? Hub gene, internal/external edge counts, max internal
  DepMap correlation. Use this to surface unsuspected functional partners
  the user might want to consider as alternative targets or co-targets.
- **`get_genetic_codependency(<target>, <partner>)`** — for PPI targets, this
  is the Pearson correlation between the two genes' DepMap dependency scores.
  |r| ≥ 0.3 is meaningful (the genes co-essential across cell lines), |r| ≥ 0.5
  is strong corroboration that the PPI is biologically load-bearing. **A
  high-confidence PPI without DepMap codependency is a yellow flag** — the
  interaction may not be functionally important in the cell lines DepMap
  covers.
- **`find_cocorrelated_genes(<target>, top_k=10)`** — top co-essential
  partners. Use this to discover unsuspected functional partners not surfaced
  by literature.
- **`shortest_interaction_path(<target>, <disease-gene>)`** — connects the
  target to a known disease driver. Surface to the user when the path is
  short (≤ 2 hops); it justifies why the target matters in the disease
  context.

Run **at most 2–3 graph tool calls** here — they are not free and the
literature queries above are already substantial. Pick the ones that
materially change the tractability verdict.

**For inhibit_active_site targets**, the graph tools are less central
(enzymes often have weak DepMap signal because of redundancy), but
`cluster_for_protein` is still useful to surface paralog risk.

---

## Phase 5: Synthesise and Generate Report

Analyse across all retrieved fingerprints + graph results. Do not fabricate
information — if a section cannot be filled from the corpus, write "Not found
in corpus." and note whether the absence is informative (e.g., no prior
inhibitors = novel target) or simply a corpus coverage gap.

**Citation policy:** All cited claims must come from retrieved fingerprints.
- Citation format: DOI string only (e.g. `10.7554/eLife.25068`) + source_span.
  The `Source:` field in the KNOWN INHIBITORS table and the `Source (DOI, span)`
  column in the TARGET-SITE INSIGHTS table must use this format.
- No author names, journal names, or years. Write the DOI, not "Smith et al. 2023
  Nature" or "eLife 2024".
- If a fact is known from training knowledge but no corpus DOI exists for it, state
  the fact without any citation. Do not invent or guess a reference.
- DepMap and graph tool results are not literature citations — cite them as
  e.g. "(DepMap codependency r=0.42)" without a DOI.

The job of this report is to give the downstream structure-analysis stage the
best literature-derived starting point. Collect every key residue mentioned in
the corpus with mutagenesis or structural evidence — these go into the
`target_site_hint` field in PIPELINE HANDOFF (the structure expert uses them
to focus its geometry pass).

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

### TARGET-SITE INSIGHTS FROM LITERATURE

#### Confirmed key residues (mutagenesis / structural data)
For PPIs: interface residues with ΔΔG or alanine-scan effect on binding.
For inhibit_active_site: catalytic residues + pocket-lining residues from
inhibitor co-crystal structures.

| Residue | Protein | Role / effect | Method | Source (DOI, span) |
|---------|---------|---------------|--------|--------------------|
| ...     | ...     | ...           | ...    | ...                |

#### Residues tolerant of mutation
<Only include if data exists — omit sub-section entirely if not found in corpus.>

#### Pathway / DepMap context
<Include if Phase 4 produced results — otherwise omit this sub-section.>
- Co-functional cluster: <hub gene + top 2–3 members from cluster_for_protein>
- Genetic codependency: <Pearson r with named partners, e.g. "TARGET / PARTNER: r=0.42 (DepMap)">
- Notable co-essential genes: <top 3 from find_cocorrelated_genes if surprising / informative>
- Disease-driver connectivity: <shortest_interaction_path summary if used — omit otherwise>

### FEASIBILITY ASSESSMENT
- Target tractability / informativeness: <Excellent / Good / Marginal / Poor>
  - **Excellent**: structural interface clear (PDB with both partners, or convincing
    homology); literature names at least 2 interface residues; modality precedent
    in the literature (peptide / mini-protein / antibody-fragment for ANY member of
    the same family is sufficient), **OR** a clearly stated falsifiable hypothesis
    from the pathway stage where disrupting the interaction predicts a specific
    biological readout. Quantitative anchors (Kd / Ki / ΔΔG) reported when present
    but NOT required.
  - **Good**: structural interface clear AND at least one mutagenesis or
    interface-residue claim in the corpus, OR a modality precedent on a paralog,
    OR a coherent mechanistic prediction with a named readout.
  - **Marginal**: structural interface clear but no residue-level data, no modality
    precedent, AND no clear falsifiable prediction.
  - **Poor**: structural interface unresolved (partner absent from PDB and no
    homologue available); cannot be designed against until structure work is done.

  Absence of ΔΔG or Kd in the corpus is NOT grounds for downgrading. Novel or
  hypothesis-tier targets typically lack these values. If the structural rationale
  is sound AND the pathway stage articulated a falsifiable hypothesis (a
  `predicted_consequence` + `falsifying_readout` in the upstream handoff), GO
  stands. Quantitative anchors are HIGHLIGHTED when present (always cite the DOI
  + value), but they are reportable evidence, never gates.

  When the upstream pathway-stage choice carries `predicted_consequence` /
  `falsifying_readout` fields (wildcard mode emits these), restate them at the
  TOP of this FEASIBILITY ASSESSMENT section so the design stages downstream see
  the testable claim, not just the target name.

- Corpus confidence: <High (≥ 5 papers) / Medium (2–4) / Low (0–1)>
- Design challenges: <bullet list — isoform redundancy, localisation, flat surface, etc.>
- Negative results: <what failed and why — cite source_span; omit if none>
- Missing data gaps: <what is absent from corpus that matters>

### DESIGN RECOMMENDATIONS
- Suggested modality: <cyclic peptide / mini-protein / stapled peptide / other> — reasoning in one sentence
- Residues to prioritise: <literature-validated hotspots>
- Residues to avoid: <tolerant-of-mutation residues — omit if none found>
- Epitope to mimic: <partner peptide or co-crystal epitope, if known>
- Affinity target — use the FIRST rule that applies, and state in one sentence which rule fired:
  1. **Corpus has measured Kd or Ki** (use `find_quantitative_evidence`) → aim for one
     half-log tighter than the tightest cited value. E.g. corpus has 31 nM SPR → "aim
     for ≤ 10 nM Kd (one half-log tighter than the 31 nM cyclic probe in DOI X)".
  2. **VALIDATED or BIOLOGICALLY_JUSTIFIED tier** (from pathway-expert handoff) →
     "aim for ≤ 30 nM Kd, peptide-binding heuristic for druggable PPIs".
  3. **HYPOTHESIS / CROSS_INDICATION_TRANSFER / SYNTHETIC_LETHALITY / PATHWAY_INFERRED
     without quant anchor** → "aim for ≤ 100 nM Kd, peptide-binding heuristic for
     novel targets — refine after first design round".
- Key risk: <single most important design challenge — one sentence>

### LITERATURE SOURCES (max 8 rows)
| # | Title (truncated to 60 chars) | DOI | Study type | Score | Key finding |
|---|-------------------------------|-----|------------|-------|-------------|
| 1 | ...                           | ... | ...        | ...   | ...         |

### PIPELINE HANDOFF
- target_complex: <ProteinA / ProteinB for PPI, or single-protein label for inhibit_active_site>
- design_intent: <disrupt | stabilize | inhibit_active_site — pass through from pathway-expert handoff>
- tractability: <Excellent | Good | Marginal | Poor>
- go_recommendation: <GO | CONDITIONAL_GO | NO_GO>
- go_rationale: <one sentence — the single most decisive reason for the recommendation>
- modality: mini_protein          # default; the operator opts into cyclic_peptide at kickoff
- target_site_hint: <compact JSON object — see format below; consumed by complex-structure-analysis to focus the geometry pass>
- design_query: <one sentence — e.g. "Generate {modality} design inputs for {complex}, PDB {pdb_id}. Priority hotspots: {res_list}. Affinity target: {kd}. {key constraint if any}." DO NOT include chain letters (A/B/...) anywhere in this query — you have not inspected the mmCIF at this stage and any chain assignment will be a guess. Chain identity is resolved by the structure-analysis stage from the mmCIF entity descriptions and flows downstream from there.>

**IMPORTANT:** Write the `### PIPELINE HANDOFF` section as plain bullet lines exactly as shown above.
Do NOT wrap it in a code fence (no ``` before or after). Do NOT omit the `- ` prefix.
The programmatic orchestrator parses these lines with a regex — any deviation breaks the pipeline.

Rules for `### PIPELINE HANDOFF`:
- `go_recommendation` must be exactly one of: `GO`, `CONDITIONAL_GO`, or `NO_GO`. Use the tractability rubric: Excellent/Good → GO; Marginal → CONDITIONAL_GO (still produces designs, with the key risk surfaced); Poor → NO_GO. Note: missing ΔΔG / Kd values are not a downgrade trigger — see the Tractability rubric in FEASIBILITY ASSESSMENT.
- `go_rationale` is a single sentence — the programmatic orchestrator displays this directly to the user.
- `design_query` is the verbatim query string passed to protein-design-script; include PDB ID, top 3–5 hotspot residues, and suggested affinity target. Do NOT include chain letters — chain assignment is handled by the structure-analysis stage downstream.
- `target_site_hint` is a single-line JSON object with these keys (use straight double-quotes, no trailing commas):
  - `mode`: `"ppi_interface"` (for disrupt / stabilize) or `"single_protein_pocket"` (for inhibit_active_site)
  - `target_protein`: the protein whose surface the binder engages
  - `priority_residues`: array of residue identifiers from the corpus (numbers and/or one-letter+number, e.g. `["F69", "L91", "R89"]` or `["245", "247", "250"]`); leave empty `[]` if literature gave no specific residues
  - `notes`: short string explaining the source of the residues AND the numbering convention used. If the literature numbers are from a paralog or full-length canonical sequence and the PDB structure may be a truncated construct or a different family member, say so. Examples: `"alanine scan ΔΔG > 2 kcal/mol from doi:..., YAP1 numbering"`, `"hTEAD4 canonical numbering — mapping to PDB target chain must be confirmed by structure stage"`, `"catalytic triad from inhibitor co-crystal doi:..."`.

  Example: `- target_site_hint: {"mode":"ppi_interface","target_protein":"TEAD4","priority_residues":["F69","L91","R89"],"notes":"Alanine scan ΔΔG > 1.5 kcal/mol from doi:10.7554/eLife.25068; YAP1 numbering"}`

**Residue-numbering guardrail.** Do NOT claim that residue numbers from one
paralog (e.g. hTEAD4 D272) map directly to another paralog (e.g. hTEAD1 in
PDB 3KYS) without structural verification. TEAD1/2/3/4 share high sequence
identity but have offset numbering when crystallised as truncated
constructs; literature numbers are corpus-side identifiers, not PDB
auth_seq_ids. State the numbering convention in `target_site_hint.notes`;
do NOT assert PDB-residue equivalence in the report body. The structure
stage downstream owns the literature→PDB residue mapping (by contact
geometry against the actual mmCIF) and will surface the equivalent
auth_seq_ids in its MODEL-READY HOTSPOTS block.

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
