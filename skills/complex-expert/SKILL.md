---
name: complex-expert
description: >
  Characterise a predicted or known protein complex from a set of gene/protein names.
  Searches the curated literature corpus to determine pathway involvement, disease
  relevance, and therapeutic targeting potential. Operates in two modes: Summary mode
  (default — brief biology blurb, ~4–6 tool calls) for rapid triage of many complexes,
  and Full pipeline mode (deep follow-up — ends with a chimerax-ppi-analysis handoff)
  for high-value hits. Invoke ONLY when the user explicitly asks for analysis starting
  from a set of proteins or a predicted complex (e.g. "what do we know about the
  ARFRP1/JTB/SYS1/ARL1 complex?", "characterise this complex", "what pathway is this
  in?"). Do NOT invoke for disease-first questions (use pathway-expert) or when a PDB
  ID is the starting point (use chimerax-ppi-analysis directly).
  Requires: literature-db MCP server.
---

# Protein Complex Expert

This skill reasons from a set of proteins outward to pathway biology, disease context,
and therapeutic opportunity. It is the inverse of pathway-expert: instead of starting
from a disease and finding a target, it starts from a (possibly novel) complex and
determines what biology it participates in.

The key novelty signal: **complexes with little or no corpus coverage may represent
new biology**. Low coverage is not a failure — it is a flag to investigate further
and enrich the database.

---

## Pre-flight: Determine Mode

Before running any queries, determine the operating mode from user intent:

**Summary mode** (default) — user wants rapid triage:
- "what do we know about X?"
- "give me a brief summary of this complex"
- "screen this list of complexes"
- No request for structure or design

**Full pipeline mode** — user wants deep characterisation:
- "give me the full analysis"
- "I want to take this to the design stage"
- "run the complete pipeline starting from this complex"
- User explicitly mentions ChimeraX, structural analysis, or design inputs

If unclear, run Summary mode and offer Full pipeline at the end.

---

## Phase 1: Literature Search

Run 3–4 `search_corpus` calls. Vary query angle to triangulate coverage.

**Query 1 — Direct complex / pairwise interaction** (top_k=6):
Search for the proteins as a group and pairwise. For a complex [A, B, C, D], run:
```
search_corpus
  query="<A> <B> <C> complex interaction"
  top_k=6
```
Then for the most likely functional pair (first two, or anchor + suspected partner):
```
search_corpus
  query="<A> <B> protein interaction binding"
  top_k=5
```

**Query 2 — Pathway / functional context** (top_k=6):
Without knowing the pathway, use broad functional terms. Rotate through candidates:
```
search_corpus
  query="<A> <B> signaling trafficking membrane transport"
  top_k=6
```

**Query 3 — Disease / therapeutic relevance** (top_k=5):
```
search_corpus
  query="<A> cancer oncogene tumor suppressor disease"
  top_k=5
```

**Query 4 — Pathway biology filter** (top_k=5):
```
search_corpus
  query="<A> <B> pathway mechanism dysregulation"
  study_category="pathway_biology"
  top_k=5
```

Deduplicate results by DOI. Retain highest score per paper.

### Coverage Assessment (do this immediately after queries)

Classify corpus coverage:
- **Good** — ≥3 papers with score ≥ 0.35, at least one directly mentions both proteins
- **Sparse** — 1–2 papers with score ≥ 0.35, or no paper mentions the complex as a unit
- **None** — all scores < 0.25 or fewer than 2 results total

For **None** or **Sparse** coverage: this is a novelty signal. Note it prominently in
the output and generate fetch keywords (see Phase 3). Do NOT treat low coverage as an
error — it may indicate unstudied biology.

---

## Phase 2: Fingerprint Retrieval

Retrieve full fingerprints for the top 3–5 unique papers from Phase 1. Prioritise:
1. Papers where both (or multiple) complex members appear in `protein_pair` or `entities.proteins`
2. Papers with `study_category = "pathway_biology"` (richer target_node data)
3. Papers with `key_findings` containing direct binding or functional data

From each fingerprint, extract:
- **Pathway involvement** — from `pathway_context.pathways`, `pathway_context.pathway_logic`,
  or inferred from `situational_context_hook`
- **Disease associations** — from `pathway_context.disease_associations` or `key_findings`
- **Functional role of each protein** — from `pathway_context.target_nodes` or `entities`
- **Any reported interactions** — from `protein_pair`, `key_findings`, or `methodology`
- **Genetic dependency evidence** — CRISPR/RNAi essentiality data if present
- **Prior therapeutic targeting** — from `pathway_context.target_nodes[].prior_therapeutic_targeting`
- **Suggested PDB structures** — from `pathway_context.target_nodes[].suggested_pdb_structures`

---

## Phase 3: Knowledge Gap Assessment

Before generating output, assess what is NOT known:

1. **Is this a characterised complex?** — If all members appear together in ≥2 papers, it is
   known. If only individual proteins appear, it may be a newly predicted assembly.

2. **Is the pathway known?** — If pathway context could be inferred from any fingerprint,
   state it. If not, flag as "pathway not established in corpus."

3. **Generate fetch keywords** — Always produce a set of suggested NCBI keywords for
   database expansion, even when coverage is Good. Use this template:
   ```
   "<GeneA>[Title/Abstract] AND <GeneB>[Title/Abstract] AND interaction[Title/Abstract]"
   "<GeneA>[Title/Abstract] AND <GeneB>[Title/Abstract] AND complex[Title/Abstract]"
   "<GeneA>[Title/Abstract] AND cancer[Title/Abstract]"
   ```
   Where GeneA/GeneB are the most informative pair (anchor protein + suspected partner).

---

## Phase 4a: Summary Mode Output

Produce a `## COMPLEX SUMMARY` block. Keep it concise — this is for triage.

```
## COMPLEX SUMMARY

### COMPLEX: <ProteinA / ProteinB / ProteinC / ...>

**Known / predicted**: <Known complex (characterised in corpus) | Predicted assembly (limited corpus coverage — possible new biology)>

**Corpus coverage**: <Good / Sparse / None> (<N> relevant papers found, best score: <X>)

**Pathway involvement**:
<1–2 sentences: which pathway(s), what role, cite DOI if from corpus. If not found: "Pathway not established in corpus — likely unstudied or novel context.">

**Disease relevance**:
<1–2 sentences: cancer types, mutation data, genetic dependency if any. "Not found in corpus" if absent.>

**Targeting potential**:
<1 sentence assessment: any prior therapeutic attempts? PDB available? Interface accessible?>

**Novelty signal**: <High / Medium / Low>
<High = sparse/no coverage AND predicted assembly AND no therapeutic history — strong candidate for new biology>
<Medium = some coverage but pathway unclear or complex not characterised as a unit>
<Low = well-characterised complex with known pathway and disease role>

**Suggested fetch keywords** (run before deeper analysis):
- "<keyword 1>"
- "<keyword 2>"
- "<keyword 3>"

**Recommended next step**: <one of:>
- "Proceed to full pipeline (structure + design)" — for high novelty + disease relevance
- "Expand database first, then re-run" — for high novelty but no structural data available
- "Run pathway-expert for [disease context]" — when disease is known but pathway unclear
- "Archive — low therapeutic relevance" — for well-known non-oncogenic complexes
```

After the summary, offer:
> "Want me to run the full pipeline for this complex (literature deep-dive + structural analysis)?"

---

## Phase 4b: Full Pipeline Mode Output

Produce the full `## COMPLEX CHARACTERISATION REPORT`, then hand off to downstream skills.

```
## COMPLEX CHARACTERISATION REPORT

### COMPLEX IDENTITY
- Members: <ProteinA (gene) / ProteinB (gene) / ...>
- Status: <Known complex | Predicted assembly | Novel — not characterised in corpus>
- Corpus coverage: <Good / Sparse / None> (<N> papers, best score: <X>)

### PATHWAY BIOLOGY
- Pathway(s): <from corpus, or "Not established">
- Pathway position: <upstream regulator / effector / scaffold / adaptor / other>
- Pathway logic: <ON/OFF logic if known, or "Not found in corpus">
- Upstream regulators: <from pathway_context or inferred>
- Downstream effectors: <from pathway_context or inferred>

### DISEASE ASSOCIATIONS
- Relevant disease(s): <cancer types, mechanisms — cite DOI + source_span>
- Mutation/dysregulation: <specific mutations or expression changes, or "Not found">
- Genetic dependency: <CRISPR/RNAi essentiality evidence, or "Not found in corpus">

### THERAPEUTIC HISTORY
- Prior targeting attempts: <modality, target protein, outcome — or "None found in corpus">
- Known structures: <PDB IDs from suggested_pdb_structures, or "None mentioned in corpus">
- Interface accessibility: <extracellular / intracellular / unknown>

### NOVELTY ASSESSMENT
- Novel biology signal: <High / Medium / Low>
  Reasoning: <why this complex may or may not represent new biology>
- Recommended interface to target: <ProteinA–ProteinB interface, or "unclear — structural analysis needed">

### CORPUS SOURCES
| # | Title (truncated) | DOI | Score | Key finding |
|---|-------------------|-----|-------|-------------|
| 1 | ...               | ... | ...   | ...         |

### FETCH KEYWORDS FOR DATABASE EXPANSION
Run: `python scripts/fetch_papers.py --keywords "<keyword>"`
- "<keyword 1>"
- "<keyword 2>"
- "<keyword 3>"
- "<keyword 4>"
```

### Handoff to downstream skills

After the COMPLEX CHARACTERISATION REPORT, determine the handoff:

**If a known PDB ID was found in corpus:**
> "Proceeding to structural analysis. Invoking chimerax-ppi-analysis with PDB [ID],
> target interface: [ProteinA / ProteinB]."
Then invoke **chimerax-ppi-analysis** with that PDB ID and the recommended interface pair.

**If no PDB ID found but complex has therapeutic potential:**
> "No PDB structure found in corpus. Before structural analysis, the database should
> be expanded using the fetch keywords above. Once curated, run AlphaFold structure
> analysis: invoke chimerax-ppi-analysis with the local .cif file path."
Provide the local AlphaFold path if the user has specified it.

**If novelty signal is High:**
> "This complex shows a high novelty signal — sparse corpus coverage suggests it may
> represent unstudied biology. Recommend: (1) expand database with fetch keywords,
> (2) re-run this skill, (3) if confirmed, proceed to full structural analysis using
> the AlphaFold prediction."

---

## AlphaFold Local File Handling

If the user provides a local AlphaFold file path (e.g. `/data/alphafold/complex_001.cif`),
include it in the chimerax handoff:

> "For ChimeraX structural analysis with the local AlphaFold prediction, use:
> `run_command 'open /data/alphafold/complex_001.cif'`
> rather than `open_structure` (which is for RCSB IDs only)."

The chimerax-ppi-analysis skill will handle the rest once the structure is open.

---

## Novelty Signal Calibration

**High novelty** (prioritise for full pipeline):
- Predicted assembly (not previously co-purified or co-crystallised)
- ≤1 paper mentioning any member in a complex context
- No therapeutic history
- At least one member is disease-associated individually (suggests the complex context may be the missing piece)

**Medium novelty**:
- Some members are well-studied but not in this combination
- Pathway is known but complex role is unclear
- 2–4 papers mention the proteins but not as a functional unit

**Low novelty**:
- Well-characterised complex (≥5 papers, complex named, structure available)
- Pathway, disease role, and therapeutic history all established

---

## Common Pitfalls

- **Do not conflate individual protein function with complex function.** A protein may
  have a known role in isolation, but the complex it forms may have a different or
  emergent function. Report individual and complex-level evidence separately.
- **Sparse corpus ≠ unimportant.** Flag low coverage as novelty signal, not as a dead end.
- **Do not guess PDB IDs.** Only report structures explicitly mentioned in retrieved
  fingerprints or that you are certain exist (well-known structures like 6NB6 for
  TEAD/YAP). If uncertain, say "None found in corpus — check RCSB manually."
- **Keep Summary mode summaries short.** The whole point is rapid triage — one paragraph
  per section maximum.
- **Always generate fetch keywords**, even for well-covered complexes. The corpus is
  never complete and new structures / mutagenesis data may be missing.
