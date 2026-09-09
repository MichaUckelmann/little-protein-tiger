---
name: wildcard-expert
description: >
  Invoke ONLY when the user explicitly asks for it by name or clearly
  requests this specific workflow; do not trigger it from a general
  question, which you can answer better from your own knowledge than from
  this narrow corpus.
  Query the curated literature database to identify non-obvious, potentially novel
  PPI targets for a disease context. Uses a training-knowledge bridge plus
  graph-driven novelty triage (interaction_hubs, shortest_interaction_path,
  novelty_signal) to generate hypotheses, then validates each strictly against
  the corpus. Complements pathway-expert: use when the user explicitly wants
  creative or out-of-the-box suggestions, or when standard pathway analysis has
  already been run and the user wants a parallel speculative analysis for comparison.
  Output format is identical to pathway-expert and feeds directly into
  complex-structure-analysis and molecular-biology-expert.
  Works across disease-driven contexts (cancer indications, autoimmune,
  metabolic) AND basic-biology contexts (signalling pathway exploration,
  protein-complex assembly, organelle / compartment biology) — the
  "novelty" mandate does not assume a disease anchor.
  Tools: search_corpus, get_fingerprint, find_pdb_structures, search_rcsb_pdb,
  interaction_hubs, shortest_interaction_path, novelty_signal,
  get_interactions_for, find_quantitative_evidence, export_subgraph,
  get_genetic_codependency, find_cocorrelated_genes, cluster_for_protein.
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
- `biological_context` — the system being explored. May be a disease
  ("mesothelioma", "PDAC", "NSCLC", "HCC", "AML", "ALS", "type-2 diabetes"),
  a pathway in normal physiology ("unfolded protein response",
  "DNA replication initiation", "hematopoietic stem-cell quiescence"),
  or a process ("ciliogenesis", "macroautophagy initiation", "spindle assembly
  checkpoint"). The novelty mandate applies equally to both — many tractable
  PPIs in basic biology have no disease anchor yet.
- `pathway_hint` — optional; a pathway name supplied by the caller. Use it only if
  given; never invent one, and never carry an example from this prompt into a report.
- `mode` — derived: `disease_anchored` if a disease/indication was named,
  `basic_biology` if only a pathway/process was named. This determines whether
  downstream queries include disease-essentiality terms or focus on
  biochemistry / co-essentiality / structural biology.
- `constraint` — optional; e.g. "avoid previously targeted nodes",
  "extracellular only", "phase-separating proteins only"

If the context is ambiguous proceed with the broad term — do not block on ambiguity.

---

## Phase 2: Abbreviated Direct Corpus Search

Run **2 queries** (not 4 — save context budget for the creative phases). Use
`study_category="pathway_biology"` with unfiltered fallback if scores < 0.20.

**Query 1 — Pathway mechanism** (top_k=20):
```
search_corpus
  query="<pathway_hint OR biological_context> signaling molecular mechanism regulation"
  study_category="pathway_biology"
  top_k=20
```
Notes: do NOT include "dysregulation" or "cancer" / "disease" keywords unless
`mode = disease_anchored`. In `basic_biology` mode "regulation" / "activation" /
"complex formation" give more relevant hits.

**Query 2 — Genetic dependency / essentiality** (top_k=10):

For `mode = disease_anchored`:
```
search_corpus
  query="<biological_context> CRISPR screen genetic dependency essentiality <pathway_hint>"
  study_category="pathway_biology"
  top_k=10
```

For `mode = basic_biology`:
```
search_corpus
  query="<pathway_hint> essential genes CRISPR screen co-essentiality"
  study_category="pathway_biology"
  top_k=10
```
DepMap-style essentiality data is informative regardless of disease anchor —
the goal is to find proteins whose loss-of-function phenocopies the pathway
state of interest.

**Query 3 — Biochemistry / interface (mode-conditional, optional, top_k=8):**

In `basic_biology` mode (or when the disease anchor is weak), add a third
query to surface structural / mechanistic literature that pure
disease-essentiality searches miss:
```
search_corpus
  query="<pathway_hint> protein-protein interaction interface structure biochemistry"
  top_k=8
```

Deduplicate by DOI. Retrieve `get_fingerprint` for the top 12–16 unique papers from
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

## Phase 2.5: Graph-Driven Novelty Triage  *(MANDATORY)*

Before Phase 3's creative reasoning, anchor your search in the corpus
interaction graph and the novelty signal. This phase produces a "hub
residuals" table — well-connected nodes the literature has NOT yet
saturated — and seeds the hypothesis space for Phase 3 with proteins
your training-knowledge bridge alone would miss.

**Required calls (do not skip):**

**Call 1 — Establish prior-art hubs** (always):
```
interaction_hubs
  top_n=20
  min_mentions=3
```
Record the top 5 protein names as **HIGH PRIOR ART nodes** — these are the
proteins everyone is already chasing. Candidates that match this list need
extraordinary justification for inclusion as novel hypotheses.

**Call 2 — Score every emerging candidate**:

For each candidate protein already surfaced in Phase 2 (proteins from
`pathway_context.target_nodes[].protein`, plus the canonical driver if
known from `disease_or_cancer`), call:
```
novelty_signal
  protein=<GENE_SYMBOL>
```
Record `novelty_score` ∈ [0, 1] and `counts.mentions` per candidate.

**Call 3 — Connectivity to the anchor node** (for each non-anchor candidate):

Identify an `anchor_node` for connectivity queries:
- `disease_anchored` mode → the canonical driver gene of the disease (e.g.
  YAP1 for Hippo / mesothelioma; KRAS for KRAS-driven cancers; NF2 for
  NF2-loss tumours).
- `basic_biology` mode → the most-mentioned hub node in the Phase-2
  fingerprints that participates in the pathway of interest (e.g. ATG1 /
  ULK1 for autophagy initiation, PERK for the UPR).

For each non-anchor candidate, call:
```
shortest_interaction_path
  protein_a=<anchor_node>
  protein_b=<candidate>
  max_hops=4
  k=1
```
Record whether a path exists and the `min_mentions_along_path`. A path of
length ≤ 3 with `min_mentions_along_path ≥ 2` is **mechanistically connected**.
A missing path or a 1-mention weak link is **disconnected** (still worth
considering for UNCHARTED tier but flag the gap explicitly).

**Call 4 — DepMap co-essentiality vs. top-3 hubs** *(MANDATORY for each candidate)*:

DepMap co-essentiality is the closest thing to ground-truth functional
co-dependency we have — it reflects loss-of-function phenocopying across
~1,200 cell lines, independent of literature attention. A high-r pair is
strong evidence of functional coupling even if no paper has explicitly
connected the two genes.

**Use three reference hubs, not one.** Literature attention crowns one
canonical driver per pathway, but functional coupling does not respect
that framing — a candidate uncoupled from the named anchor may still be
strongly coupled to a hub the corpus underweights. Take the top-3 hub
nodes from Call 1 (`interaction_hubs`); in `disease_anchored` mode,
substitute the disease driver for one of them so it is always tested.
Call `get_genetic_codependency` for each candidate against each of the
three hubs (3 calls per candidate, runnable in parallel):
```
get_genetic_codependency
  gene_a=<hub_i>
  gene_b=<candidate>
```
Record `pearson_r` and `n_cell_lines` for each of the three pairings,
take the **maximum r** across the three hubs as the candidate's
`depmap_max_r_to_hubs`, and **remember which hub gave that maximum**
(the candidate's strongest hub-codep partner). Interpretation applies
to `max_r`:
- `max_r ≥ 0.4` and `n ≥ 200`: **co-essential to at least one hub** —
  a strong functional signal even when the literature path is absent.
- `0.2 ≤ max_r < 0.4`: **weakly co-essential** — supports but does not
  establish functional coupling.
- `max_r < 0.2` or `n < 100` for all three: **uncoupled from the
  hub set** by DepMap criteria.

**Call 5 — Co-essential partners of the anchor** *(MANDATORY once)*:

Surface unexpected co-essential partners of the anchor that did NOT appear
in Phase-2 fingerprints — these are the highest-yield hypothesis seeds
because they combine functional ground truth with literature absence:
```
find_cocorrelated_genes
  gene=<anchor_node>
  top_n=15
  min_r=0.3
```
Cross-reference the returned gene symbols against (a) Phase-2 proteins,
(b) Phase-2 fingerprints' `target_nodes`, (c) the `interaction_hubs`
top-20. Any gene that appears here with `r ≥ 0.35` but is missing from
ALL three above is a **DepMap residual** — flag prominently for Phase 3.

**Call 6 — Cluster context (optional, per top candidate)**:

For each candidate scoring CONNECTED-NOVEL / PERIPHERY-NOVEL / UNCHARTED,
optionally call:
```
cluster_for_protein
  protein=<candidate>
```
Note the cluster's `hub`, top members, and whether the cluster contains
the anchor. Co-cluster membership without direct literature path is itself
a hypothesis seed.

**Call 7 — Candidate-edge DepMap sweep** *(MANDATORY for the top-2 candidates by
classification rank)*:

Calls 4 and 5 view the codep landscape from the anchor's vantage point —
"is this candidate coupled to a hub I already care about?" Call 7 inverts
that: it surveys the candidate's OWN functional neighborhood. This catches
the highest-value wildcard finding — "the novel pick is strongly
codependent with a cancer-relevant protein the corpus has not yet linked
to it" — which Calls 4–5 will miss whenever the candidate sits in a
separate module from the anchor.

Rank the candidates by classification (DEPMAP-COUPLED > PERIPHERY-NOVEL >
UNCHARTED > CONNECTED-NOVEL > MID-NOVEL > SATURATED). For the top 2, run
both of the following (they batch into parallel tool calls):

(a) **Global DepMap residuals for the candidate** — top-k codependent
    genes regardless of corpus presence:
```
find_cocorrelated_genes
  gene=<candidate>
  top_n=10
  min_r=0.3
```

(b) **Corpus-graph neighbors of the candidate** — what the literature says
    it interacts with:
```
get_interactions_for
  protein=<candidate>
  top_n=10
```

Cross-reference the two result sets and categorise each surfaced partner:
- **Graph-confirmed codep partners** — present in BOTH (a) and (b). The
  literature already names the interaction AND DepMap validates functional
  coupling. Strongest single signal you can produce; cite both sources.
- **Codep residual for the candidate** — present in (a) but NOT (b). Pure
  functional connection the literature has not caught. Cross-reference
  against the Call 1 `interaction_hubs` top-20: a candidate strongly
  codependent (r ≥ 0.4) with a corpus hub it is not edge-connected to is
  the **highest-yield wildcard finding** (functional coupling to a
  literature-attended driver, without literature recognition of the link).
  Flag prominently.
- **Corpus-only partner** — present in (b) but NOT (a) with r ≥ 0.3.
  Literature-claimed interaction without independent DepMap support.
  Discount when assessing functional importance.

Record the top 3 codep partners per candidate (highest `pearson_r` with
`n ≥ 200`) as the candidate's `depmap_neighborhood`. Each entry:
`{partner, r, n_cell_lines, in_corpus_graph: bool, in_hub_top20: bool}`.
This feeds the CANDIDATE NODE ASSESSMENT line and the `choices_json`
handoff, and should inform PRIMARY RECOMMENDATION rationale ("the pick
is functionally coupled to <hub> at r=0.55 — an unrecognised axis").

### Synthesise: hub residuals table

Combine all six calls into a single table that drives Phase 3 hypothesis
generation. The DepMap r is your strongest non-literature signal — surface
it as its own column, and surface the top codep partner (from Call 4's
hub sweep) so the reviewer can see WHERE the coupling is:

| Protein | hub rank | novelty_score | path to anchor (hops, min_mentions) | DepMap max_r vs hubs (partner) | top codep neighbour (Call 7, if run) | cluster | classification |
|---------|----------|---------------|-------------------------------------|--------------------------------|--------------------------------------|---------|----------------|
| ...     | ...      | ...           | ...                                 | ...                            | ...                                  | ...     | ...            |

**Classification rules** (assign one per candidate; first matching rule wins):

| Class | Rule |
|---|---|
| SATURATED        | hub rank in top-20 AND novelty_score < 0.4 |
| CONNECTED-NOVEL  | hub rank in top-20 AND novelty_score ≥ 0.6 |
| PERIPHERY-NOVEL  | not a hub AND novelty_score ≥ 0.6 AND literature path to anchor exists |
| DEPMAP-COUPLED   | not a hub AND novelty_score ≥ 0.5 AND (max_r vs hubs ≥ 0.4 OR Call-7 top neighbour r ≥ 0.4) — functional coupling without literature recognition; this is the highest-yield wildcard class. The DepMap signal may come from any of the 3 hubs (Call 4) or from the candidate-edge sweep (Call 7); record which |
| UNCHARTED        | novelty_score ≥ 0.8 AND no literature path AND max_r < 0.2 (or n too small to call) |
| MID-NOVEL        | otherwise (0.4 ≤ novelty_score < 0.6, or borderline cases) |

`CONNECTED-NOVEL`, `PERIPHERY-NOVEL`, `DEPMAP-COUPLED`, and `UNCHARTED` are
the **interesting** classifications. `SATURATED` candidates are
pathway-expert's job, not yours; the wildcard mandate is novelty.

Carry `novelty_score`, `classification`, the max_r-to-hubs (with partner),
and the Call-7 `depmap_neighborhood` (for the top 2 candidates) per
candidate forward into Phase 5 tier assignment and into the
`choices_json` handoff.

---

## Phase 3: Hypothesis Generation — Corpus + Graph First, Training Knowledge as Gap-Filler

Generate **3–5 novel PPI hypotheses**. The goal is hypotheses the corpus + graph
have already *implied* but no individual paper has *stated* — these are the
highest-value outputs of this skill. Training knowledge is allowed only as a
last resort to fill specific mechanism / residue gaps after the corpus and
graph have done their work.

**Step order** (do all three; do them in this order):

### Step 3a — Mine Phase 2.5 graph patterns (PRIMARY hypothesis source)

Look at the hub-residuals table for these patterns:
- **DEPMAP-COUPLED candidates**: a `DEPMAP-COUPLED` row is a hypothesis on a
  plate — high co-essentiality (DepMap r ≥ 0.4) but no direct literature path
  means "these two genes phenocopy each other across hundreds of cell lines
  but nobody has connected them in writing yet". Lift this candidate verbatim
  as a hypothesis; the mechanism rationale is the DepMap r value.
- **CONNECTED-NOVEL with long path**: a candidate with novelty_score ≥ 0.6
  and a 3-hop path to the anchor through low-mention edges. The intermediate
  hop proteins are themselves hypothesis seeds (often more interesting than
  the endpoint).
- **Cluster residuals**: a `cluster_for_protein` result containing a member
  the disease-driver literature never mentions but the cluster hub does.
- **`find_cocorrelated_genes` flagged residuals**: genes that came back with
  r ≥ 0.35 against the anchor but appeared in zero Phase-2 fingerprints. The
  Phase-2.5 call already flagged these — promote them to full hypotheses here.

### Step 3b — Mine non-obvious connections in Phase 2 fingerprints

Look across the Phase-2 fingerprint set for:
- **Single-paper bridges**: two proteins co-mentioned in exactly one fingerprint
  with `key_findings.protein_pair` — the corpus has *seen* this pair once but
  no one has built a programme around it.
- **Asymmetric pathway membership**: a protein appearing in
  `pathway_context.target_nodes` but with `prior_therapeutic_targeting=null`
  in every fingerprint that mentions it.
- **Effector / regulator mismatches**: an `upstream_regulator` in one
  fingerprint that doesn't appear in any other fingerprint's pathway map —
  suggests an incompletely described axis.

### Step 3c — Training knowledge ONLY to fill gaps

Use parametric knowledge to ANNOTATE the corpus/graph-derived hypotheses
above — add mechanism, propose specific interface residues from family
structural biology, suggest a tractable assay. Do NOT introduce entirely new
proteins via training knowledge.

**HARD RULE**: if your hypothesis introduces a protein that did NOT appear in
ANY Phase-2 fingerprint, ANY Phase-2.5 graph output, OR the DepMap correlated
set, you are reasoning from training knowledge alone — drop the hypothesis
unless Phase 4 re-anchors it. The pipeline's value-add over "ask Claude
directly" is the corpus + graph; hypotheses untethered to those forfeit
that value-add.

### Hypothesis archetypes to consider

These are LENSES for interpreting the Step 3a/3b signals, not free-form
brainstorming prompts:

- **Co-essential dark partner** (DepMap-driven): two genes are co-essential
  across cell lines but no paper has co-cited them in this context.
  Most-likely interpretation: shared complex, shared substrate, or
  parallel-pathway redundancy.
- **Cross-context transfer**: a PPI well-characterised in a different
  biological context (different disease, different tissue, different
  developmental stage) that should generalise here. Requires explicit
  reasoning about WHY the transfer works.
- **Paralog vulnerability**: a less-studied paralog whose interaction with
  a shared scaffold becomes load-bearing when the canonical paralog is lost
  or suppressed.
- **Upstream rewiring**: an adaptor / co-chaperone whose interaction with
  a known node has been described in biochemistry but not yet as a target
  in this context.
- **Phase-separation / condensate residency**: a protein known to localise
  to a relevant condensate / membrane-less compartment but never characterised
  as a PPI target there.

Disease-anchored archetypes (use only when `mode = disease_anchored`):
- **Synthetic lethality**: a PPI that becomes essential only because of a
  co-occurring alteration present in the disease.
- **Cross-indication transfer**: a PPI well-characterised in a related disease
  that may be relevant despite sparse direct evidence in the target indication.

Write each hypothesis in this block (internal reasoning — shown before the report):

```
## WILDCARD HYPOTHESIS GENERATION
[Generated from Phase 2.5 graph patterns + Phase 2 corpus connections; training
knowledge used only for annotation. Citations to corpus/DepMap data are
permitted here; bare training-knowledge claims are not.]

HYPOTHESIS 1: <ProteinA / ProteinB>
  Source signal: <Step 3a graph pattern / Step 3b corpus connection / DepMap r value>
  Mechanism: <why this PPI matters in the biological context — 1–2 sentences>
  Novelty: <what makes this non-obvious — 1 sentence>
  Why corpus might support it: <indirect evidence path — what to search for in Phase 4>
  Search terms: ["<term1>", "<term2>", "<term3>"]

HYPOTHESIS 2: <ProteinA / ProteinB>
  ...
```

**Hard rules for Phase 3:**
- Do not repeat any PPI already surfaced by Phase 2 fingerprints as a known
  pathway node.
- Do not include a hypothesis unless you can name *both* proteins specifically.
- Every hypothesis must cite at least ONE Phase-2.5 graph signal or Phase-2
  corpus connection in its `Source signal` line. Bare training knowledge is
  not a valid Source signal.
- Do not include a hypothesis that introduces a protein absent from all
  Phase-2 / Phase-2.5 outputs UNLESS the Phase 4 search succeeds in finding
  corpus evidence for it.

---

## Phase 4: Hypothesis-Driven Corpus Search

Convert each hypothesis into targeted corpus queries and run them. Cap at **5
queries total** across all hypothesis + cross-pathway searches below.

**For each hypothesis** (run at most 3 hypothesis queries):
```
search_corpus
  query="<ProteinA> <ProteinB> <mechanism_keyword from hypothesis> <biological_context>"
  top_k=5
```
Drop `<biological_context>` from the query if the hypothesis is a basic-biology
PPI without a strong disease anchor — including a weak disease term often
suppresses good biochemistry hits.

**Co-dependency / cross-pathway query** (always run, top_k=6):

For `mode = disease_anchored`:
```
search_corpus
  query="<biological_context> synthetic lethality co-dependency pathway compensation"
  top_k=6
```
For `mode = basic_biology`:
```
search_corpus
  query="<pathway_hint> functional redundancy compensation parallel pathway"
  top_k=6
```

**Adjacent-context transfer query** (run if pathway_hint is known, top_k=5):
```
search_corpus
  query="<pathway_hint OR anchor_node> <adjacent_context> mechanism interaction"
```
Use the closest well-characterised context neighbour you identified in Phase 3:
- Disease mode: e.g. if context is PDAC and pathway is Hippo, try "mesothelioma"
  or "HCC".
- Basic-biology mode: a different tissue / cell type / developmental stage where
  the same pathway has been better characterised (e.g. UPR in plasma cells if
  the context is UPR in pancreatic β-cells).

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
  query="<new_gene_symbol_1> [<new_gene_symbol_2>] <biological_context> mechanism dependency"
  study_category="pathway_biology"
  top_k=5
```
In `basic_biology` mode replace `<biological_context>` with the pathway / process
keyword.

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

### Membrane receptors: which site a designed binder can actually reach

A binder is a folded protein in solution. It can only engage surface that is
solvent-exposed on ONE face of the membrane. This rules out large parts of a
GPCR, and the constraint is chemical, not a limitation of the pipeline — so
choose the site accordingly rather than proposing one that will be refused.

**Reachable, and clinically validated:**

- **Class B ECD** (GCGR, GLP1R, CALCRL, PTH1R, CRHR). These use a two-domain
  mechanism: the peptide hormone's C-terminus binds a genuine extracellular
  domain, its N-terminus then inserts into the helical bundle. The ECD contact
  is a real protein-protein interface and is fully extracellular. Erenumab
  blocks the CALCRL/RAMP1 receptor this way and is an approved migraine drug;
  anti-GCGR antibodies are in trials for diabetes.
- **Class C Venus flytrap** (mGluR, CaSR, GABA-B) — the orthosteric site is
  entirely extracellular and large.
- **Class F CRD** (Frizzled, SMO).
- **N-terminus and extracellular loops of PEPTIDE-binding class A receptors**
  (chemokine receptors, angiotensin, opioid peptide receptors). The ligand has a
  large extracellular footprint, and antibodies against these surfaces work
  (mogamulizumab/CCR4, leronlimab/CCR5).
- **Receptor / accessory-protein interfaces** such as CALCRL-RAMP1, where the
  interface itself sits outside the membrane.

**Not reachable — do not propose these:**

- **The orthosteric pocket of a receptor whose ligand is a lipid or a
  lipophilic small molecule** (cannabinoid, S1P, LPA, prostaglandin, free fatty
  acid). These pockets are inside the helical bundle and there is strong
  structural evidence that the ligand enters LATERALLY, from within the
  bilayer, through gaps between transmembrane helices — S1P1 is capped by its
  own N-terminal helix, and CB1 has lipid-facing portals. Nothing arriving from
  solution can occupy that site.
- **Deep aminergic pockets** (adrenergic, muscarinic, dopaminergic). Reached
  from the extracellular side, but far too enclosed for a 70-86 residue
  mini-protein; these are small-molecule sites.
- **Any transmembrane surface at all.** In an isolated structure a TM helix
  looks like an attractive hydrophobic patch; in a cell it is buried in lipid.

A practical test before proposing a membrane target: *would an antibody work
here?* If the answer is no because the site is inside the bundle, a designed
mini-protein will not work either.

The pipeline enforces this deterministically further down — UniProt topology is
mapped into author numbering and the trim drops the transmembrane span and the
opposite face, and a hotspot annotated transmembrane fails the run outright. So
a receptor whose only druggable site is intramembrane is not a target for this
pipeline: say so and pick another node rather than spending stages on it.

**When the best target has no experimental structure, an AlphaFold model is a
legal answer.** Emit `pdb_id: AF-<UniProt accession>` (e.g. the accession you
resolved for that protein) and the pipeline fetches the predicted model. Two
conditions, both hard:

1. **Only with `design_intent: inhibit_active_site`.** An AlphaFold model is a
   single chain, so there is no partner in it to disrupt or stabilise. A run
   that pairs `AF-` with `disrupt` or `stabilize` is refused before any spend.
2. **Only when no experimental co-complex exists.** An experimental structure of
   the real complex always wins; check with `search_rcsb_pdb` first and say in
   the report that you looked.

This matters most for the case that used to dead-end: a well-evidenced target
with no PDB entry. Recommending a *downstream* complex instead is a real option,
but it answers a different question than the user asked — if you do that, say so
in one clause in PRIMARY RECOMMENDATION and keep the original target in the
landscape at its own tier.

---

## Phase 5: Inferred PPI Reasoning

For every candidate node, run the same four-condition reasoning check as
pathway-expert Phase 4a (interaction necessity, therapeutic mechanism, consequence
of disruption or stabilization, interaction knowability).

The mechanism-of-action step (covers both therapeutic intent AND probe
experiments — the same `design_intent` field serves both):
- `disrupt` — breaking the interaction attenuates the pathway-relevant output.
  In disease mode this is "loss of oncogenic signalling" or "loss of pathological
  signal relay"; in basic-biology mode it's "loss of the predicted regulatory
  step" — useful as a probe to demonstrate that the predicted regulatory step
  is in fact load-bearing.
- `stabilize` — a normally protective or autoinhibitory interaction is weakened
  (by mutation, expression loss, or experimental challenge) and reinforcing
  it restores the unperturbed state. Examples: restoring an autoinhibitory
  complex, re-engaging a sequestered OFF-state, protecting a tumour-suppressor
  complex from degradation, or clamping an autoinhibited kinase in its OFF
  conformation as a probe of pathway function.

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

2. Every candidate in TARGET OPPORTUNITY LANDSCAPE has these additional bullets:
   - `**Novelty rationale**` — why this target/PPI is non-obvious (1 sentence)
   - `**Novelty signal**` — `novelty_score=<value>, classification=<class>`
   - `**Hypothesis**` — the mechanistic claim being tested (1 sentence)
   - `**Predicted consequence**` — biological outcome if hypothesis is true and
     the interaction is disrupted (1 sentence, named readout)
   - `**Falsifying readout**` — assay + threshold that decides (1 sentence)

3. HYPOTHESIS-tier entries have an additional bullet:
   `- **Corpus support**: <what was found, or "Nothing found in corpus">`

4. The CORPUS COVERAGE section has additional lines:
   - `- Wildcard hypotheses generated: <N> total; <N> corpus-supported; <N> weakly supported; <N> corpus-absent; <N> graph/DepMap-derived (Step 3a)`
   - `- Hub residuals table: <N candidates>; counts per class (SATURATED / CONNECTED-NOVEL / PERIPHERY-NOVEL / DEPMAP-COUPLED / UNCHARTED / MID-NOVEL)`
   - `- DepMap signals used: <N> get_genetic_codependency calls (Call 4 multi-hub + Call 7 sweep); <N> find_cocorrelated_genes calls (Call 5 anchor + Call 7 per candidate); <N> cluster_for_protein calls; <N> get_interactions_for calls (Call 7 candidate-edge sweep)`
   - `- Call 7 candidate-edge sweep ran on: <candidate_1>, <candidate_2> — strongest neighbourhood finding: <partner (r=<value>); "none above r=0.4" if no hit>`

```
## PATHWAY BIOLOGY REPORT

### BIOLOGICAL CONTEXT
- Mode: <disease_anchored | basic_biology>
- Disease / indication: <name, or "Not disease-anchored — basic biology" if mode = basic_biology>
- Pathway(s) / process(es): <comma-separated list>
- Primary mechanism of interest: <one sentence — cite DOI + source_span. In
  disease mode this is the dysregulation mechanism; in basic-biology mode this
  is the regulatory question or process being interrogated.>
- Frequency / prevalence: <mutation_frequency if stated; otherwise omit in basic-biology mode>

### PATHWAY MAP
- Upstream regulators: <max 3 items; include alteration type only in disease mode>
- Core cascade: <one sentence of pathway logic>
- Key effectors / downstream nodes: <max 3 items>

### CANDIDATE NODE ASSESSMENT

For each candidate target node — max 5 nodes total (wildcard allows one extra),
6–7 bullet lines per node:

#### <ProteinName (gene symbol)>
- Pathway position: <pathway_position>
- State / dysregulation: <one sentence — cite source_span + DOI. In disease mode
  describe the dysregulation; in basic-biology mode describe the regulatory role.>
- Genetic dependency: <evidence — DepMap or CRISPR — or omit if absent>
- DepMap codep vs. top-3 hubs: <max_r=<value> with <hub> (n=<n>); or "uncoupled (max_r=<value>)"; or "not run">
- DepMap neighborhood (Call 7, top candidates only): <"<partner_1> (r=<value>, in_corpus_graph=<bool>, in_hub_top20=<bool>); <partner_2> (...); <partner_3> (...)"; or omit if Call 7 not run for this candidate>
- Prior therapeutic / probe targeting: <prior targeting, or omit if absent>
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
- **Novelty signal**: novelty_score=<value>, classification=<SATURATED | CONNECTED-NOVEL | PERIPHERY-NOVEL | DEPMAP-COUPLED | UNCHARTED | MID-NOVEL>, DepMap max_r vs hubs=<value> with <hub>, neighbourhood top=<partner (r=<value>) or "—" if Call 7 not run>
- **Hypothesis**: <one sentence — the mechanistic claim. "PROTEIN_X drives DISEASE via interaction with PROTEIN_Y in CONTEXT.">
- **Predicted consequence**: <one sentence — what should happen biologically if the hypothesis is true and we disrupt the interaction. Name the cellular or molecular readout. e.g. "CTGF and CYR61 transcript reduction ≥ 50% at 24h in NF2-null cells">
- **Falsifying readout**: <one sentence — the assay + threshold that decides. e.g. "qPCR of CTGF/CYR61 at 24h post-treatment; ≥50% reduction = consistent with hypothesis; ≤20% = falsifies">
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

State which candidate is recommended for the downstream pipeline. Wildcard
mode INVERTS pathway-expert's preference: novelty is the value-add of this
skill, so high-prior-art targets are deliberately deprioritised. Apply this
rule **in order** — first match wins:

1. **Novelty-first rule.** If a `HYPOTHESIS`, `CROSS_INDICATION_TRANSFER`,
   or `SYNTHETIC_LETHALITY` candidate satisfies **all three** of:
     (a) a PDB ID exists in the corpus or via `find_pdb_structures` /
         `search_rcsb_pdb`,
     (b) the structural interface is interpretable (the partner protein
         or a homologous interface is present in the PDB entry),
     (c) at least one corpus fingerprint mentions the candidate (or a
         paralog) in a disease context,
   recommend that candidate. This is the wildcard mandate.

2. **Fallback.** If no candidate satisfies rule 1, recommend the
   highest-tier remaining candidate (VALIDATED > BIOLOGICALLY_JUSTIFIED >
   PATHWAY_INFERRED) and state explicitly that the wildcard run found no
   tractable novel candidate.

3. **Rationale must cite `novelty_score` AND the strongest DepMap
   neighbourhood signal (when Call 7 ran on the pick).** State both in
   one sentence — e.g. "novelty_score=0.72 — PERIPHERY-NOVEL,
   mechanistically connected to KRAS via SOS1 (3-hop path, min mentions
   = 4); DepMap-coupled to RAF1 at r=0.58 (n=1208) — an interaction the
   corpus has not yet recognised but DepMap validates functionally."
   When the Call-7 sweep produced no partner with r ≥ 0.4, state that
   explicitly rather than omitting the line.

**Tie-break** under rule 1: higher `novelty_score` wins.

**Anti-pattern guard.** A high-prior-art VALIDATED candidate with
`novelty_score < 0.3` should NOT be recommended by this skill unless rule 1
is unsatisfiable — those targets belong to pathway-expert. If the wildcard
run keeps picking the same VALIDATED targets pathway-expert would pick,
the skill has failed at its job.

- **Target complex**: <ProteinA / ProteinB>
- **Evidence tier**: <tier label>
- **Novelty**: novelty_score=<value> (<classification>)
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
  - `novelty_score`: float in `[0.0, 1.0]` from `novelty_signal` for the candidate's primary protein
  - `classification`: one of `"SATURATED"`, `"CONNECTED-NOVEL"`, `"PERIPHERY-NOVEL"`, `"DEPMAP-COUPLED"`, `"UNCHARTED"`, `"MID-NOVEL"`
  - `depmap_r_to_anchor`: float (Pearson r vs the Phase-2.5 anchor / primary hub) or `null` if not applicable / not computed — kept for backward compatibility with existing consumers; mirror `depmap_max_r_to_hubs` here if no single anchor is meaningful
  - `depmap_max_r_to_hubs`: object `{r: float, hub: str, n_cell_lines: int}` or `null` — best Pearson r from the Call-4 top-3-hub sweep, with the hub that produced it
  - `depmap_neighborhood`: array of up to 3 objects `{partner: str, r: float, n_cell_lines: int, in_corpus_graph: bool, in_hub_top20: bool}` from Call 7 — only populated for the top-2 candidates that received the sweep; empty array `[]` otherwise
  - `predicted_consequence`: one sentence — what disruption should produce biologically; name the readout
  - `falsifying_readout`: one sentence — the assay + threshold that falsifies the hypothesis

  `predicted_consequence` and `falsifying_readout` are REQUIRED for wildcard
  mode (picking a non-VALIDATED candidate without articulating the testable
  claim defeats the skill's purpose). They are the JSON-extracted forms of
  the `Hypothesis` / `Predicted consequence` / `Falsifying readout` bullets
  in the TARGET OPPORTUNITY LANDSCAPE block.

  Example (must be on ONE line):
  `- choices_json: [{"tier":"PERIPHERY_NOVEL","complex":"VGLL4 / TEAD4","pdb_ids":[],"evidence_basis":"Training knowledge plus Phase-4 hit: VGLL4 competes with YAP at the TEAD interface; corpus has 2 papers in gastric cancer.","key_uncertainty":"No mesothelioma-specific evidence; whether stabilising VGLL4-TEAD displaces YAP in NF2-null context is untested.","design_intent":"stabilize","novelty_score":0.71,"classification":"PERIPHERY-NOVEL","depmap_r_to_anchor":0.33,"depmap_max_r_to_hubs":{"r":0.41,"hub":"TEAD1","n_cell_lines":1208},"depmap_neighborhood":[{"partner":"TEAD1","r":0.41,"n_cell_lines":1208,"in_corpus_graph":true,"in_hub_top20":true},{"partner":"YAP1","r":0.36,"n_cell_lines":1208,"in_corpus_graph":true,"in_hub_top20":true},{"partner":"MOB1A","r":0.32,"n_cell_lines":1208,"in_corpus_graph":false,"in_hub_top20":false}],"predicted_consequence":"VGLL4-TEAD stabilisation reduces YAP-TEAD chromatin occupancy by >50% and rescues NF2-null cell-cycle arrest.","falsifying_readout":"ChIP-seq YAP signal at canonical TEAD-binding sites 24h post-treatment; <20% reduction falsifies."}]`

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
