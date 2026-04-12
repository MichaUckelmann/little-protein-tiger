---
name: complex-structure-analysis
description: >
  Analyse protein-protein interaction interfaces to identify surface hotspots for
  disruption OR stabilization by de novo cyclic peptides and mini-proteins. In
  disrupt mode: identifies single-chain interface hotspots. In stabilize (molecular
  glue) mode: identifies periinterface patches on BOTH chains for a bridging binder
  design. Computes BSA, SASA, H-bonds, pairwise contacts, and periinterface geometry
  using the structure-tools MCP server (no ChimeraX required). Accepts local CIF/PDB
  files or RCSB PDB IDs (download first).
  Trigger on: "interaction interface", "contact residues", "buried surface",
  "hydrophobic patch", "hotspot", "disrupt interaction", "stabilize interaction",
  "molecular glue", "PPI stabilizer", "PPI analysis", "design a binder",
  "cyclic peptide target", "mini-protein target", or when a multi-chain structure is
  provided and the user asks which surface to target. Also trigger for known complexes
  (YAP-TEAD, PD-1/PD-L1, p53-MDM2). Requires structure-tools MCP.
---

# PPI Interface Analysis for Peptide / Mini-Protein Design

Systematic interface analysis using the `structure-tools` MCP server. All geometric
quantities (BSA, SASA, contacts, H-bonds) are computed directly from atomic
coordinates — not inferred from residue names. Reasoning is applied on top of
reliable numerical outputs.

**Two operating modes**, selected by the `design_intent` field in the PIPELINE HANDOFF
from pathway-expert or wildcard-expert:
- **`disrupt` mode** (default): identify single-chain interface hotspots; binder
  competes with the partner chain to break the interaction.
- **`stabilize` mode (molecular glue)**: identify periinterface patches on *both*
  chains flanking the interface; binder bridges across and reinforces the complex.

Output: `## PPI ANALYSIS REPORT` with MODEL-READY HOTSPOT formats for BoltzGen and RFD3.

---

## CITATION POLICY — READ FIRST

**This report has NO access to the literature corpus.** All geometric facts come from
tool results. All reference facts must come from Step 3 web search results.

### Rules — enforced throughout this entire report:

1. **No author names, journal names, or years** — ever. These are unverifiable in this
   context and will be hallucinated from training knowledge. The sentence
   `"Smith et al. 2023 showed..."` or `"Nature 2024"` or `"Ratti et al."` is
   **always wrong** unless the text was returned verbatim by a web search tool call.

2. **DOI-only citation format.** The only permitted citation is a DOI string, e.g.
   `10.7554/eLife.77415`. Write this only if a web search tool returned it explicitly.
   Never construct or guess a DOI from memory.

3. **Biological facts without a DOI: state the fact without any citation.** If you
   know a residue is a disease hotspot from training knowledge, you may state that
   biological fact — but omit any reference entirely. A fact with no citation is
   correct. A fact with a fabricated citation is a data integrity error.

4. **`Literature evidence: not found`** is the correct and acceptable value when Step
   3 returned nothing. Do not fill this field from training knowledge.

5. **Do not add report sections not present in the template below.** Sections like
   `### BIOLOGICAL CONTEXT`, `### CLINICAL SIGNIFICANCE`, or similar are not in this
   skill's report format and must not be added — they are a vector for hallucinated
   references. All biological context belongs in the designated fields within the
   template sections.

6. **Structure source classification** comes from the file path or the user's
   description only — never from a paper attribution. Write `experimental (cryo-EM)`,
   `experimental (X-ray)`, `af3_boltz`, or `rfdiffusion`. Never add `"; Author et al.
   Year / Journal"` — you do not have the publication metadata.

---

## Pre-flight: Obtain a Local Structure File

Before calling any tools, identify the **structure source** from the user's message:
- `experimental` — X-ray crystallography, cryo-EM, NMR, or any RCSB PDB ID not explicitly described as a prediction. **This is the default if not stated.**
- `af3_boltz` — explicitly described as AlphaFold3, Boltz, or another confidence-scored prediction model
- `rfdiffusion` — RFDiffusion or other diffusion-based design output (B-factors are placeholders, not confidence)

The B-factor column in experimental structures contains crystallographic B-factors, not pLDDT. **Never interpret B-factors as confidence scores unless the user explicitly states this is an AF3/Boltz output.**

**Local file** (CIF or PDB): use the path directly.

**RCSB PDB ID**: check `data/structures/<ID>.cif` first — structures cited in the
literature corpus are pre-downloaded there by `scripts/download_pdb_structures.py`.
If not present, download on the fly:
```
! curl -o data/structures/<ID>.cif https://files.rcsb.org/download/<ID>.cif
```
(PDB format if needed: replace `.cif` with `.pdb`)

---

## Phase 0: Detect Design Mode

Before any tool calls, read the `design_intent` from the PIPELINE HANDOFF (passed in
the query from the orchestrator). Set the operating mode for this entire run:

- `design_intent: disrupt` → **DISRUPT mode** — proceed with standard Phase 1–3 below.
- `design_intent: stabilize` → **STABILIZE mode** — follow the STABILIZE branches in
  Phases 1–3; skip Phase 2 (surface patch scoring) and use `tool_find_glue_pockets`
  instead.
- Not present → default to **DISRUPT mode**.

Record the mode explicitly: write `<!-- MODE: DISRUPT -->` or `<!-- MODE: STABILIZE -->`
at the top of your scratchpad so it stays visible throughout the analysis.

---

## Phase 1: Interface Analysis

Call once — this returns everything needed for hotspot reasoning:

```
mcp__structure-tools__tool_analyze_interface
  file_path = "<absolute_path>"
  chain_a   = "<target_chain>"
  chain_b   = "<partner_chain>"
  cutoff    = 4.5
```

If chain assignments are unclear, check model info from the file and confirm with user
before proceeding — swapping target and partner changes the entire analysis.

**[STABILIZE mode only]** — after `tool_analyze_interface` completes, also call
`tool_find_glue_pockets`:

```
mcp__structure-tools__tool_find_glue_pockets
  file_path            = "<absolute_path>"
  chain_a              = "<chain_a>"
  chain_b              = "<chain_b>"
  periinterface_radius = 10.0
  max_bridge_span      = 20.0
  min_periface_sasa    = 5.0
  top_n                = 3
```

From the glue pockets result, extract and note:
- `glue_pockets[]` — ranked list of cross-chain patch pairs; each entry has:
  - `rank`, `combined_rating`, `centroid_separation_A`, `bridgeable`, `design_note`
  - `chain_a_patch.residues[]` — residue nums + SASA on chain A
  - `chain_b_patch.residues[]` — residue nums + SASA on chain B
- `interface_summary.bsa_total_A2` — from the inner interface analysis
- Top-1 pocket `centroid_separation_A` → select design modality:
  - ≤ 12 Å: bicyclic or large cyclic peptide
  - 13–20 Å: mini-protein recommended (needs structural scaffold to bridge)
  - > 20 Å (`bridgeable: false`): very long span — note as challenging, flag for user

From the `tool_analyze_interface` result, extract and note:
- `interface.bsa_total_A2` — total BSA; use to select design modality:
  - < 500 Å²: crystal packing, likely not biological
  - 500–1000 Å²: small — cyclic peptide
  - 1000–2000 Å²: typical PPI — cyclic peptide or mini-protein
  - > 2000 Å²: large — mini-protein recommended
- `interface.n_hbonds` and `interface.hbonds[]` — H-bond list with donor/acceptor/distance
- `chain_a_interface_residues[]` — per-residue: type, hydrophobicity, contacts, gap_flag, BSA
- `chain_a_categories` — pre-computed hydrophobic/aromatic/charged/polar breakdown
- `interface.bsa_per_residue[]` — BSA contribution per residue (key for hotspot ranking)
- `plddt_at_interface` — B-factor column values. Interpret as pLDDT **only** if
  `structure_source` is `af3_boltz`. For `experimental` structures these are
  crystallographic B-factors — do not flag, threshold, or reason over them as
  confidence scores. For `rfdiffusion` they are placeholders — ignore entirely.
- `low_confidence_residues` — pre-flagged list; relevant only for `af3_boltz` sources.

### If chain IDs are unknown

Inspect the file with:
```
mcp__structure-tools__tool_get_sequence_map
  file_path = "<path>"
  chain     = "<try each chain letter>"
```
The `length` field helps identify which chain is which. For designed binders,
the shorter chain is usually the binder.

---

## Phase 2: Hotspot Identification

**[DISRUPT mode]** — follow all steps below.

**[STABILIZE mode]** — `tool_find_glue_pockets` already returned scored periinterface
patches from Phase 1. **Skip Steps 1–2 entirely.** Go directly to Step 3 (literature
cross-reference), then proceed to Phase 3 using the glue pocket output.

### Step 1 — Rank residues by BSA contribution

*[DISRUPT mode only]*

From `interface.bsa_per_residue`, sort the target chain residues by `bsa_A2` descending.
The top BSA contributors are the most energetically important candidates.

Identify clusters: residues within ~8 Å Cα-Cα of each other (use contact data —
residues that share contacts with the same partner residues are spatially proximate).

### Step 2 — Score candidate patches

*[DISRUPT mode only]*

For each cluster of 3–6 candidate hotspot residues, call:

```
mcp__structure-tools__tool_score_surface_patch
  file_path    = "<path>"
  chain        = "<target_chain>"
  residue_list = [<resnum1>, <resnum2>, ...]
```

Returns: hydrophobic fraction, mean KD score, spatial spread (Cα RMSD), and a
qualitative rating (Excellent / Good / Marginal / Poor). Use this to rank patches
and select the primary design target.

**Good patch characteristics for peptide/mini-protein:**
- Hydrophobic fraction ≥ 0.4
- Spatial spread ≤ 12 Å (compact, not dispersed)
- Mean KD score > 1.0
- Several residues with high individual BSA (> 30 Å² each)

**Poor patch flags:**
- Mostly charged/polar with no hydrophobic core → limited anchor energy
- Very deep, narrow geometry (high BSA but few residues with many contacts) → 
  small molecule territory, not peptide-accessible

### Step 3 — Literature cross-reference (web search)

**[DISRUPT mode]** — search for published mutagenesis or inhibitor data:
- `<complex_name> hotspot residues alanine scanning`
- `<complex_name> peptide inhibitor interface`
- `<PDB_ID> interface mutagenesis`

**[STABILIZE mode]** — search for published stabilizer/glue data:
- `<complex_name> stabilizer molecular glue PPI stabilization`
- `<ProteinA> <ProteinB> periinterface residues cooperative binding`
- `<complex_name> ternary complex binder bridging`

**Citation rules for Step 3 results** (CITATION POLICY §2):
- Record only DOIs that appear in the search result text, e.g. `10.1038/s41589-024-01234-5`.
- Do NOT construct DOIs from author/title/year — this always produces hallucinations.
- If no DOI is visible in the search result, cite the URL instead, or write `Literature: not found`.
- Never write author names, journal names, or publication years — not even if you
  recognize the protein/complex from training knowledge.
- **If the web search returns no result for this complex, write `Literature: not found`
  and stop.** Do not substitute training-knowledge facts with invented citations.

---

## Phase 3: Model-Ready Hotspot Formats

### Get sequence numbering map

**[Both modes]** — call for every chain that contributes hotspot residues.

**[DISRUPT mode]**: call once for the target chain.
**[STABILIZE mode]**: call for **both** `chain_a` and `chain_b` — the binder engages
residues on both, so both `auth_to_label` maps are required.

```
mcp__structure-tools__tool_get_sequence_map
  file_path = "<path>"
  chain     = "<chain_id>"
```

Returns `auth_to_label` map: `{auth_seq_id → label_seq_id}`.
- **label_seq_id** is required for BoltzGen `binding` specs
- **auth_seq_id** is required for RFD3 `select_hotspots` specs

Both are now available from a single tool call. If you already called this for a
chain earlier in the session (e.g. during Pre-flight), do not call it again —
look up the result already in context.

**Critical rule**: Use the `auth_to_label` map verbatim — never compute, estimate,
or infer the mapping from sequence comparison, chain start residues, or
observed gaps. If a residue's `auth_seq_id` is not a key in the map, omit it
from the MODEL-READY HOTSPOTS table and note it as unmapped.

### Sidechain atoms for RFD3

For each hotspot residue, select 2 interface-facing sidechain heavy atoms using
this lookup (from the residue type and its known contact geometry):

| Residue | Atoms |
|---|---|
| ILE/LEU | CD1,CG2 / CD1,CD2 |
| VAL | CG1,CG2 |
| PHE | CD2,CZ |
| TYR | CD2,OH |
| TRP | CD2,NE1 |
| MET | CG,SD |
| ARG/LYS | CZ,NH1 / NZ,CE |
| ASP/GLU | CG,OD1 / CD,OE1 |

---

## Phase 4: Output Report

**Do not call `write_file`.** All output belongs in the report text below.
The pipeline captures this report automatically; `protein-design-script` reads
the MODEL-READY HOTSPOTS section from it in Stage 4.

Populate from tool outputs only. Do not infer distances, BSA values, or interaction
types from residue names — all of these are now in the tool results.

**Token budget:** The full PPI ANALYSIS REPORT must fit in 3,000–4,000 words.
- Interface residue categories: one comma-separated line per category, not sub-tables.
- H-bond table: max 12 rows — keep the 12 shortest distances (strongest bonds).
- Hotspot regions: max 2 (primary + one alternative). If more were scored, note the
  ratings in a single line and focus the write-up on the top 2.

```
## PPI ANALYSIS REPORT

### COMPLEX OVERVIEW
- Structure: <file path or PDB ID>
- Structure source: <experimental (X-ray) | experimental (cryo-EM) | experimental (NMR) | af3_boltz | rfdiffusion>
  — source type only; do NOT add author, journal, or year (see CITATION POLICY §6)
- Chain A: <id> (<protein name>)
- Chain B: <id> (<protein name>)
- Design mode: <DISRUPT | STABILIZE (molecular glue)>
- BSA total: <value> Å²
- H-bonds across interface: <n_hbonds>
- Interface residues: <n_contacts_chain_a> on chain A, <n_contacts_chain_b> on chain B
- Design modality: <cyclic_peptide / mini_protein / either> — rationale

### CHAIN A INTERFACE RESIDUES
Chain <id>: Hydrophobic: <comma-separated list> | Aromatic: <list> | Charged: <list> | Polar: <list>

### CHAIN B INTERFACE RESIDUES
Chain <id>: Hydrophobic: <list> | Aromatic: <list> | Charged: <list> | Polar: <list>

### H-BONDS AT INTERFACE (top 12 by distance)
| Donor | Donor atom | Acceptor | Acceptor atom | Distance (Å) |
|---|---|---|---|---|
| <chain:ResNum> | <atom> | <chain:ResNum> | <atom> | <dist> |
```

---

### [DISRUPT mode] HOTSPOT REGIONS (top 2, ranked by suitability)

*Write this section only in DISRUPT mode. In STABILIZE mode, replace with GLUE POCKETS section below.*

```
### HOTSPOT REGIONS (top 2, ranked by suitability)

#### Region N: <name> — <Excellent/Good/Marginal/Poor>
- Residues: <list with auth_seq_id>
- BSA contributions (Å²): <per-residue from bsa_per_residue>
- Hydrophobic fraction: <from score_surface_patch>
- Spatial spread (Cα RMSD): <Å>
- Mean KD hydrophobicity: <score>
- Partner contacts: <which partner residues engage this region>
- Literature evidence: <DOI from Step 3 web search only, e.g. "10.1038/..." — or "not found". No author/journal/year.>
- Design note: <what the binder must mimic — biological rationale without citations>
- Separability: <"Independent — separate design submission required" if Cα-Cα centroid
  distance to other region > 15 Å, otherwise "Combined with Region X feasible">
```

---

### [STABILIZE mode] GLUE POCKETS

*Write this section only in STABILIZE mode. In DISRUPT mode, replace with HOTSPOT REGIONS above.*

Report the top-N glue pockets from `tool_find_glue_pockets`. Focus on the top-1 pocket
for design unless top-2 has a meaningfully better combined_rating.

```
### GLUE POCKETS (top results from periinterface analysis)

#### Glue Pocket <rank>: <combined_rating> — centroid separation <X.X> Å (<bridgeable?>)
- Design note: <design_note from tool result>
- Chain A patch (<ProteinA>): residues <list>, hydrophobic fraction <f>, suitability <rating>
- Chain B patch (<ProteinB>): residues <list>, hydrophobic fraction <f>, suitability <rating>
- Literature evidence: <DOI from Step 3 web search only — or "not found". No author/journal/year.>
- Design challenge: <any flags — e.g. long span, poor hydrophobicity, shallow patches>
```

---

### [Both modes] DESIGN RECOMMENDATIONS

```
### DESIGN RECOMMENDATIONS
- Design mode: <DISRUPT | STABILIZE (molecular glue)>
- Recommended modality: <cyclic_peptide / mini_protein / either>
- Primary target: <[DISRUPT] region name and rating | [STABILIZE] Glue Pocket rank + combined_rating>
- Key chain A residues: <list with auth_seq_id — interface hotspots [DISRUPT] or periinterface patch [STABILIZE]>
- Key chain B residues: <[DISRUPT] partner residues to mimic | [STABILIZE] periinterface patch residues>
- Centroid separation: <[STABILIZE only] Å — design modality rationale based on span>
- Confidence / B-factors: <"pLDDT not applicable — experimental structure (B-factors in file)" |
  "pLDDT not applicable — RFDiffusion output (B-factors are placeholders)" |
  list of low-pLDDT residues (< 70) if af3_boltz>
- Caveats: <disorder, deep pockets, missing loops, ternary complex format caveats, etc.>
```

**[STABILIZE mode caveat]** — BoltzGen ternary complex support: BoltzGen/RFD3 typically
model binder-against-one-chain. For a molecular glue targeting both chains simultaneously,
note: *"Ternary complex (binder + chain A + chain B) design may require BoltzGen v2
multi-chain binding spec or RFD3 with both chain hotspots. Verify current tool support
before submitting. Single-chain hotspot submissions per chain are provided as fallback."*

---

### MODEL-READY HOTSPOTS

**[DISRUPT mode only]** — standard single-chain format:

**If two regions are flagged "Independent" above, repeat this entire section once
per region, labelled `### MODEL-READY HOTSPOTS — Region 1` and
`### MODEL-READY HOTSPOTS — Region 2`. Do NOT merge residues across independent
regions. Protein-design-script generates a separate submission for each.**

```
### MODEL-READY HOTSPOTS [DISRUPT]

Target chain <id> — Region <N>: <name> — selected <M> residues:

| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |
|---|---|---|---|
| <name> | <auth> | <label> | <atom1>,<atom2> |

#### BoltzGen binding
binding: <label_seq_id_1>,<label_seq_id_2>,...

#### RFD3 select_hotspots
select_hotspots:
    <chain><auth_resnum>: <atom1>,<atom2>
    <chain><auth_resnum>: <atom1>,<atom2>
```

**[STABILIZE mode only]** — dual-chain format for molecular glue:

Both chains contribute to the binding surface. List chain A and chain B residues
separately. Use `auth_to_label` maps for both chains (both retrieved in Phase 3).

```
### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket <rank>]

Chain A (<ProteinA>) periinterface patch — selected <M> residues:

| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |
|---|---|---|---|
| <name> | <auth> | <label> | <atom1>,<atom2> |

Chain B (<ProteinB>) periinterface patch — selected <M> residues:

| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |
|---|---|---|---|
| <name> | <auth> | <label> | <atom1>,<atom2> |

#### BoltzGen binding (ternary complex — verify tool support)
Chain A binding: <label_seq_id_a1>,<label_seq_id_a2>,...
Chain B binding: <label_seq_id_b1>,<label_seq_id_b2>,...

#### BoltzGen binding (fallback — single-chain submissions)
Chain A only: binding: <label_seq_id_a1>,<label_seq_id_a2>,...
Chain B only: binding: <label_seq_id_b1>,<label_seq_id_b2>,...

#### RFD3 select_hotspots (combined — verify tool support)
select_hotspots:
    <chainA><auth_resnum>: <atom1>,<atom2>
    <chainB><auth_resnum>: <atom1>,<atom2>
```

### PIPELINE HANDOFF
- pdb_id: <PDB accession used>
- chain_a: <chain ID>
- chain_b: <chain ID>
- target_complex: <ProteinA / ProteinB>
- design_intent: <disrupt | stabilize>
- modality: <cyclic_peptide | mini_protein | stapled_helix | either>
- bsa_A2: <integer BSA in Å²>
- tractability: <Excellent | Good | Marginal | Poor>
- literature_query: <one sentence — e.g. "Search for published inhibitors and mutagenesis data for {ProteinA}/{ProteinB}. Cross-reference hotspot residues {res1}, {res2}, {res3} on {target_chain_protein}.">

**IMPORTANT:** Write the `### PIPELINE HANDOFF` section as plain bullet lines exactly as shown above.
Do NOT wrap it in a code fence (no ``` before or after). Do NOT omit the `- ` prefix.
The programmatic orchestrator parses these lines with a regex — any deviation breaks the pipeline.

---

## Handoff Contract

- **→ binder-optimizer**: pass `file_path`, `target_chain`, and the `HOTSPOT REGIONS`
  section — binder-optimizer uses the BSA data and contact geometry directly
- **→ molecular-biology-expert**: pass `Target chain` protein name and `HOTSPOT REGIONS`
  for literature cross-validation of the identified residues
- **→ orchestrator**: `DESIGN RECOMMENDATIONS` is the Stage 1 primary output;
  `MODEL-READY HOTSPOTS` feeds Stage 4 (protein design scripts)

---

## Common Pitfalls

- **Hallucinated citations** — the most common error in this skill. The model recognizes
  a protein or complex from training knowledge and invents plausible-sounding author names,
  journal names, and years. These are always wrong. Enforce the CITATION POLICY at the top:
  DOI-only citations from web search results, or no citation at all. The phrases
  `"Smith et al."`, `"Nature 2024"`, `"NSMB"`, `"eLife 77415"`, and all similar forms are
  **forbidden** unless verbatim-copied from a web search tool result.

- **Spontaneous extra sections** — do not add `### BIOLOGICAL CONTEXT`,
  `### CLINICAL SIGNIFICANCE`, or any other section not in this template. Extra sections
  are where hallucinated citations appear most often. Biological context belongs inside
  the `Design note` bullet of the relevant region, without citations.

- **Chain assignment**: confirm target vs partner with the user if not explicit.
  Mis-assignment swaps all downstream hotspot residue numbers.

- **BSA < 500 Å²**: almost certainly crystal packing, not a biological interface.
  Check if the correct chains were selected before proceeding.

- **gap_flag residues**: binder residues with < 2 contacts are weakly anchored.
  They are low-priority design targets unless BSA is high (buried but few contacts =
  large flat contact, not deeply engaged).

- **B-factor ≠ pLDDT**: the B-factor column is only a confidence score for AF3/Boltz
  outputs. For experimental structures (X-ray, cryo-EM, NMR) and for RFDiffusion outputs,
  never threshold or flag B-factor values — they carry different physical meaning.
  Default to `experimental` if the source is not stated by the user.

- **pLDDT < 70 at interface** (af3_boltz only): flag these regions — interface geometry
  may be unreliable. Weight literature evidence more heavily for those positions.

- **score_surface_patch spread > 15 Å**: the patch is too dispersed for a single
  cyclic peptide to engage. Either sub-select a tighter cluster or recommend
  a mini-protein.

- **H-bond counts from the tool**: computed by heavy-atom donor-acceptor distance
  (no explicit H positions). Values are reliable for identifying H-bonding residue
  pairs; the raw count may be elevated vs X-ray crystallography reports. Use the
  individual H-bond records for reasoning, not the aggregate count as an absolute.

- **auth_seq_id vs label_seq_id**: use `auth_seq_id` for all hotspot identification,
  BSA ranking, and reasoning throughout the report. Only switch to `label_seq_id`
  in the MODEL-READY HOTSPOTS table, and only by direct lookup in the
  `auth_to_label` map from `get_sequence_map`. Never estimate the mapping with
  phrases like "auth = label for this chain" or "offset is approximately N" —
  these guesses propagate silently into wrong BoltzGen specs.

- **Do not re-call `get_sequence_map`**: call it at most once per chain per run.
  The result is already in context — scroll back to find it rather than issuing
  a duplicate tool call. A repeated call adds tokens without new information.

- **[STABILIZE mode] No glue pockets returned**: if `tool_find_glue_pockets` returns
  an empty `glue_pockets` list, report this clearly. Possible causes: interface is
  too large / too buried for periinterface exposure, no cross-chain bridgeable patch
  within 20 Å, or all candidate residues are below the min SASA threshold. Suggest
  relaxing `periinterface_radius` (try 15 Å) or `max_bridge_span` (try 25 Å) and
  re-run. Do NOT fall back to DISRUPT mode silently — flag the issue for the user.

- **[STABILIZE mode] Do NOT analyze single-chain interface residues as binder targets**:
  periinterface patches are surface-exposed residues that flank the interface, not the
  buried interface residues themselves. Using interface residues as the glue target would
  produce a competitive binding design, not a stabilizer. The `tool_find_glue_pockets`
  result already enforces this by filtering for SASA > threshold; trust its output.
