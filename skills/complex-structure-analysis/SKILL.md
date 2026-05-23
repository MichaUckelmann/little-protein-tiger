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

**Three operating modes**, selected by the `design_intent` field in the PIPELINE
HANDOFF from pathway-expert (and confirmed by mol-bio-expert):
- **`disrupt` mode** (default): identify single-chain interface hotspots; binder
  competes with the partner chain to break the interaction. **Two-chain input.**
- **`stabilize` mode (molecular glue)**: identify periinterface patches on *both*
  chains flanking the interface; binder bridges across and reinforces the complex.
  **Two-chain input.**
- **`inhibit_active_site` mode**: single-protein target — enzyme active site,
  allosteric pocket, or other ligand-binding cleft. Hotspots come primarily from
  the literature-stage handoff (`target_site_hint.priority_residues`), with
  structural confirmation of pocket geometry per residue. **Single-chain input.**

When the mol-bio-expert handoff includes a `target_site_hint` JSON object with
`priority_residues`, treat those as the authoritative starting set in **any**
mode — they are the residues literature already implicated. The job of this
skill is to confirm geometry, not to re-derive residue importance from scratch.

Output: `## TARGET ANALYSIS REPORT` with MODEL-READY HOTSPOT formats for BoltzGen
and RFD3.

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

## Phase 0: Detect Design Mode + Parse Literature Hint

Before any tool calls, read the `design_intent` from the PIPELINE HANDOFF (passed in
the query from the orchestrator). Set the operating mode for this entire run:

- `design_intent: disrupt` → **DISRUPT mode** — proceed with standard Phase 1–3 below.
- `design_intent: stabilize` → **STABILIZE mode** — follow the STABILIZE branches in
  Phases 1–3; skip Phase 2 (surface patch scoring) and use `tool_find_glue_pockets`
  instead.
- `design_intent: inhibit_active_site` → **INHIBIT_ACTIVE_SITE mode** — single-chain
  input. Skip Phase 1 (no two-chain interface to analyse) and Phase 2 (no patch
  scoring); go straight to Phase 1B (per-residue context) using the literature-supplied
  priority residues.
- Not present → default to **DISRUPT mode**.

Record the mode explicitly: write `<!-- MODE: DISRUPT -->`, `<!-- MODE: STABILIZE -->`,
or `<!-- MODE: INHIBIT_ACTIVE_SITE -->` at the top of your scratchpad so it stays
visible throughout the analysis.

**Also parse `target_site_hint`** (if present in the query). It looks like:
```
target_site_hint: {"mode":"ppi_interface","target_protein":"TEAD4","priority_residues":["F69","L91","R89"],"notes":"…"}
```
The `priority_residues` list comes from the mol-bio-expert literature pass and should
be the authoritative starting set for hotspot selection — confirm their geometry but
do not silently drop residues from this list. If the list is empty, fall back to
purely geometry-driven hotspot selection.

**Confirm the structure is actually your target — BEFORE any geometry pass.**
The corpus stores paper-level `entities.proteins` and `paper_metadata.pdb_accessions`
as two independent lists. A paper that mentions protein X and deposits a PDB
of paralog Y will resolve "find PDBs for X" → Y's PDB. The orchestrator now
fails the run at `_ensure_structure` if the expected target name doesn't
appear in the CIF title or polymer entity descriptions — but you should also
do a first-line check so the rejection comes with a structural explanation,
not just a hard abort.

In Phase 1, **as the very first thing you do** with the loaded structure,
quote each chain's `pdbx_description` (from the mmCIF header — the
orchestrator already surfaces these in the query under "Chain entity
descriptions and sizes") and compare them word-for-word against the
expected target name in the query. Examples:

- Expected `ENPP1` vs entity `"Ectonucleotide pyrophosphatase/phosphodiesterase family member 1"` → match (note both the abbreviation and the long form name in your report).
- Expected `ENPP1` vs entity `"... family member 2"` → **mismatch**. Stop. Emit a NO_GO PIPELINE HANDOFF naming both the expected target and the actual entity, and recommend either (a) selecting a different PDB or (b) re-running the pathway stage with a stricter target.
- Expected `YAP1 / TEAD4` vs entities containing both names → match.
- Expected `YAP1 / TEAD4` vs entities containing only one → match for the one present, flag the missing partner; partner-chain absence is recoverable (you can sometimes still design against the single chain), but call it out.

When the names look ambiguous (e.g. "EGFR" vs "Epidermal growth factor receptor"
— same protein, different naming convention), proceed and note it as
expected-equivalent in your report. When the names refer to clearly different
paralogs (ENPP1 vs ENPP2, JAK1 vs JAK2), do not proceed.

**On tool failures and UNVERIFIED label_seq_ids.** If `tool_get_sequence_map`
or another structure tool returns an error (e.g. a transient MCP dependency
issue), you may write `**UNVERIFIED**` in the `label_seq_id` column of
MODEL-READY HOTSPOTS and the orchestrator will resolve those tokens
post-hoc via gemmi before passing the report to downstream stages. **Do
not invent workaround scripts in your report** — the auth_seq_id + the
literal `UNVERIFIED` token is enough; the orchestrator handles the rest.
The orchestrator also runs a residue-name sanity check (e.g. confirming
that `THR238` is actually a threonine at auth_seq_id 238 in the structure)
and logs a warning if there's a mismatch — useful for catching
mouse↔human numbering offsets that look correct on paper.

**Respect the target size policy.** The orchestrator injects per-chain residue
counts plus the configured `max_target_residues` (default 500) and
`target_residues_warn` (default 250) into your query. Before committing to a
target chain assignment:
- If the chosen target chain ≤ `target_residues_warn`: proceed normally.
- If between warn and max: proceed but **note in the report** that the binder
  pass rate may drop and flag this for the user.
- If above `max_target_residues`: **do not proceed with the full analysis**.
  Instead, produce a short report recommending one of:
    1. Cropping to a binding domain — name the residue range if literature or
       the mmCIF header suggests one.
    2. Selecting a different PDB or chain — name an alternative if you can.
    3. Re-running the pathway stage with a constraint to avoid this target.
  Emit a stub PIPELINE HANDOFF with `go_recommendation: NO_GO` and
  `go_rationale` naming the size violation. The downstream design stage will
  not run.

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
- `chain_a_interface_residues[]` — per-residue: type, hydrophobicity, contacts, n_contacts, gap_flag, BSA, ddg_estimate_kcal_mol
- `chain_b_interface_residues[]` — same schema as chain_a; both chains now receive full ΔΔG scoring
- `chain_a_categories`, `chain_b_categories` — pre-computed hydrophobic/aromatic/charged/polar breakdown
- `interface.bsa_per_residue[]` — BSA contribution per residue (key for hotspot ranking)
- `plddt_at_interface` — B-factor column values. Interpret as pLDDT **only** if
  `structure_source` is `af3_boltz`. For `experimental` structures these are
  crystallographic B-factors — do not flag, threshold, or reason over them as
  confidence scores. For `rfdiffusion` they are placeholders — ignore entirely.
- `low_confidence_residues` — pre-flagged list; relevant only for `af3_boltz` sources.

### Validate chain assignment: receptor groove vs. ligand surface

**[DISRUPT mode — do this before Phase 2]**

Before ranking hotspots, confirm that chain_a is the *design target* — the chain
whose surface the designed binder will engage. In a helix-on-groove interaction one
chain provides a concave hydrophobic receptor groove (the better design target) and
the other contributes a single helix or short loop.

Compare the two chains using the tool result:

| Signal | Receptor groove (→ should be chain_a) | Ligand helix/loop |
|---|---|---|
| `n_contacts` | More interface residues (6–12+) | Fewer (3–7) |
| BSA distribution | Many residues with moderate BSA (50–150 Å²) | Few residues with concentrated BSA |
| Per-residue ΔΔG | Moderate (−1 to −3 kcal/mol), spread broadly | Concentrated hotspots (−3 to −6 kcal/mol) at 2–5 positions |
| Secondary structure | Groove/sheet/loop surface | Often a single α-helix |

**If chain_b matches the receptor groove profile** (more contacts, distributed BSA,
broadly spread ΔΔG), the chains are assigned backwards for design purposes.
Re-call `tool_analyze_interface` with chain_a and chain_b swapped, then proceed.
This matters because both chains now receive full ΔΔG scoring, so the analysis is
equally valid regardless of which chain you call chain_a — but the hotspot workflow
in Phase 2 focuses on chain_a, so the groove must be chain_a.

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

## Phase 1B: Single-protein pocket analysis

**[INHIBIT_ACTIVE_SITE mode only]** — skip if in DISRUPT or STABILIZE mode.

The input is a single-chain enzyme or pocket-bearing protein. There is no partner
chain to analyse. The hotspot set is sourced from `target_site_hint.priority_residues`
(literature-derived catalytic / pocket residues from the mol-bio stage) and confirmed
by single-residue geometric context.

For each residue in `priority_residues`, gather geometric context:

```
mcp__structure-tools__tool_get_residue_contacts
  file_path = "<absolute_path>"
  chain     = "<target_chain>"
  residue   = <auth_seq_id>
  cutoff    = 4.5
```

Record per residue:
- `residue.name` and `residue.auth_seq_id` — canonical identifier
- `residue.sasa_A2` — solvent accessibility (a buried residue is a poor binder
  target; flag any priority_residue with `sasa_A2 < 5` as unlikely to be
  pocket-facing — note for the user but keep in the hotspot list)
- `contacts[]` — neighbouring residues within the cutoff; these are the pocket
  walls. Use them to confirm the residue is part of a coherent pocket
  (≥ 3 close contacts) rather than an isolated surface residue.

If the literature `priority_residues` list is empty (the mol-bio stage couldn't find
specific residues), report this as a degraded run: the structure expert cannot
de-novo identify a binding pocket without either (a) literature guidance or
(b) a co-crystal structure with a bound ligand. Recommend the user re-run
literature search with more targeted queries.

**Score and rank for output**: use a simple composite to pick the top 4–6 residues
for MODEL-READY HOTSPOTS:
- `sasa_A2 ≥ 30` → likely solvent-accessible pocket-facing
- `len(contacts) ≥ 4` → in a coherent pocket
- High residue-type score for designability (HYS, ASP, GLU, ARG, LYS, TYR, TRP, PHE
  preferred over GLY, ALA, SER, etc.)

For INHIBIT_ACTIVE_SITE mode, the MODEL-READY HOTSPOTS section is the same format
as DISRUPT mode but uses the target chain only (no partner_chain — the binder
will be the second chain in boltzgen's design).

---

## Phase 2: Hotspot Identification

**[DISRUPT mode]** — follow all steps below.

**[STABILIZE mode]** — `tool_find_glue_pockets` already returned scored periinterface
patches from Phase 1. **Skip Steps 1–2 entirely.** Go directly to Step 3 (literature
cross-reference), then proceed to Phase 3 using the glue pocket output.

### Step 1 — Rank residues by ΔΔG estimate

*[DISRUPT mode only]*

`tool_analyze_interface` now returns `ddg_estimate_kcal_mol` on every residue in
`chain_a_interface_residues`. Use this as the **primary ranking criterion** — it is
a physically-grounded empirical estimate of the free-energy contribution of each
residue to binding affinity, combining three terms:

| Term | Coefficient | Applied when |
|---|---|---|
| Hydrophobic burial | −0.028 kcal/mol per Å² BSA | Residue is hydrophobic or aromatic |
| H-bond to partner | −1.0 kcal/mol per H-bond | Residue is donor or acceptor |
| Salt bridge to partner | −0.5 kcal/mol per contact | Both residues are charged, attractive |

**Interpretation:**
- `ddg_estimate_kcal_mol` < −2.0 → **strong hotspot candidate** (see `ddg_hotspot_threshold_kcal_mol`)
- −2.0 to −1.0 → moderate contributor
- > −1.0 → minor contributor; deprioritise unless spatially central

The field `interface.top_hotspots_by_ddg` lists the five strongest candidates
across both chains. By this point chain_a is the confirmed design target (validated
in Phase 1), so focus on the chain_a entries. Chain_b entries are useful context
(they show which partner residues anchor the interaction) but are not design targets
in DISRUPT mode.

Use `bsa_A2` from `interface.bsa_per_residue` as a **co-equal criterion alongside
ΔΔG**: when two residues have similar ΔΔG, prefer the one with larger BSA.
High total chain_a BSA distributed across many residues — even when individual
per-residue ΔΔG values are moderate — is a strong indicator that you are targeting
a druggable groove surface.

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
qualitative rating (Excellent / Good / Marginal / Poor). Use this to evaluate
patch **geometry and druggability** — it is complementary to the ΔΔG ranking,
not a replacement for it.

**Good patch characteristics for peptide/mini-protein:**
- Hydrophobic fraction ≥ 0.4
- Spatial spread ≤ 12 Å (compact, not dispersed)
- Mean KD score > 1.0
- Prefer patches where multiple residues have `ddg_estimate_kcal_mol` < −1.0

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

**[INHIBIT_ACTIVE_SITE mode only]** — single-chain format for pocket binders. Same
shape as DISRUPT but the target is a single protein (no partner chain) and the
residues come from `target_site_hint.priority_residues` confirmed by Phase 1B
geometry:

```
### MODEL-READY HOTSPOTS [INHIBIT_ACTIVE_SITE]

Target chain <id> (<ProteinName>) — pocket residues — selected <M> residues:

| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms | SASA (Å²) |
|---|---|---|---|---|
| <name> | <auth> | <label> | <atom1>,<atom2> | <sasa> |

#### BoltzGen binding
binding: <label_seq_id_1>,<label_seq_id_2>,...

#### RFD3 select_hotspots
select_hotspots:
    <chain><auth_resnum>: <atom1>,<atom2>
    <chain><auth_resnum>: <atom1>,<atom2>
```

Flag any residue with SASA < 5 Å² in a note (likely buried; the binder may not be
able to reach it).

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
- target_chain: <chain ID of the design target — the groove/pocket chain confirmed in Phase 1>
- partner_chain: <chain ID of the binding partner — the helix/loop chain>
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
