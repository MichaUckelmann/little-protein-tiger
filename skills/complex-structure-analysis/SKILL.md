---
name: complex-structure-analysis
description: >
  Analyse protein-protein interaction interfaces to identify surface hotspots for
  disruption by de novo cyclic peptides and mini-proteins. Computes BSA, SASA,
  H-bonds, and pairwise contacts using the structure-tools MCP server (no ChimeraX
  required). Accepts local CIF/PDB files or RCSB PDB IDs (download first).
  Trigger on: "interaction interface", "contact residues", "buried surface",
  "hydrophobic patch", "hotspot", "disrupt interaction", "PPI analysis", "design a
  binder", "cyclic peptide target", "mini-protein target", or when a multi-chain
  structure is provided and the user asks which surface to target. Also trigger for
  known complexes (YAP-TEAD, PD-1/PD-L1, p53-MDM2). Requires structure-tools MCP.
---

# PPI Interface Analysis for Peptide / Mini-Protein Design

Systematic interface analysis using the `structure-tools` MCP server. All geometric
quantities (BSA, SASA, contacts, H-bonds) are computed directly from atomic
coordinates — not inferred from residue names. Reasoning is applied on top of
reliable numerical outputs.

Output: `## PPI ANALYSIS REPORT` with MODEL-READY HOTSPOT formats for BoltzGen and RFD3.

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

From the result, extract and note:
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

### Step 1 — Rank residues by BSA contribution

From `interface.bsa_per_residue`, sort the target chain residues by `bsa_A2` descending.
The top BSA contributors are the most energetically important candidates.

Identify clusters: residues within ~8 Å Cα-Cα of each other (use contact data —
residues that share contacts with the same partner residues are spatially proximate).

### Step 2 — Score candidate patches

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

Search for published mutagenesis or inhibitor data:
- `<complex_name> hotspot residues alanine scanning`
- `<complex_name> peptide inhibitor interface`
- `<PDB_ID> interface mutagenesis`

Upgrade candidates confirmed by ΔΔG data. Note any prior therapeutic targeting.

---

## Phase 3: Model-Ready Hotspot Formats

### Get sequence numbering map

```
mcp__structure-tools__tool_get_sequence_map
  file_path = "<path>"
  chain     = "<target_chain>"
```

Returns `auth_to_label` map: `{auth_seq_id → label_seq_id}`.
- **label_seq_id** is required for BoltzGen `binding` specs
- **auth_seq_id** is required for RFD3 `select_hotspots` specs

Both are now available from a single tool call.

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
- Structure source: <experimental | af3_boltz | rfdiffusion>
- Target chain: <id> (<protein name>)
- Partner chain: <id> (<protein name>)
- BSA total: <value> Å²
- H-bonds across interface: <n_hbonds>
- Interface residues: <n_contacts_chain_a> on target, <n_contacts_chain_b> on partner
- Design modality: <cyclic_peptide / mini_protein / either> — rationale

### TARGET CHAIN INTERFACE RESIDUES
Chain <id>: Hydrophobic: <comma-separated list> | Aromatic: <list> | Charged: <list> | Polar: <list>

### PARTNER CHAIN INTERFACE RESIDUES
Chain <id>: Hydrophobic: <list> | Aromatic: <list> | Charged: <list> | Polar: <list>

### H-BONDS AT INTERFACE (top 12 by distance)
| Donor | Donor atom | Acceptor | Acceptor atom | Distance (Å) |
|---|---|---|---|---|
| <chain:ResNum> | <atom> | <chain:ResNum> | <atom> | <dist> |

### HOTSPOT REGIONS (top 2, ranked by suitability)

#### Region N: <name> — <Excellent/Good/Marginal/Poor>
- Residues: <list with auth_seq_id>
- BSA contributions (Å²): <per-residue from bsa_per_residue>
- Hydrophobic fraction: <from score_surface_patch>
- Spatial spread (Cα RMSD): <Å>
- Mean KD hydrophobicity: <score>
- Partner contacts: <which partner residues engage this region>
- Literature evidence: <mutagenesis data, inhibitor data, or "not found">
- Design note: <what the binder must mimic>
- Separability: <"Independent — separate design submission required" if Cα-Cα centroid
  distance to other region > 15 Å, otherwise "Combined with Region X feasible">

### DESIGN RECOMMENDATIONS
- Recommended modality: <cyclic_peptide / mini_protein / either>
- Primary target region: <region name and rating>
- Key target residues: <list>
- Partner residues to mimic: <list>
- Confidence / B-factors: <"pLDDT not applicable — experimental structure (B-factors in file)" |
  "pLDDT not applicable — RFDiffusion output (B-factors are placeholders)" |
  list of low-pLDDT residues (< 70) if af3_boltz>
- Caveats: <disorder, deep pockets inaccessible to peptides, missing loops, etc.>

### MODEL-READY HOTSPOTS

**If two regions are flagged "Independent" above, repeat this entire section once
per region, labelled `### MODEL-READY HOTSPOTS — Region 1` and
`### MODEL-READY HOTSPOTS — Region 2`. Do NOT merge residues across independent
regions. Protein-design-script generates a separate submission for each.**

Target chain <id> — Region <N>: <name> — selected <M> residues:

| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |
|---|---|---|---|
| <name> | <auth> | <label> | <atom1>,<atom2> |

#### BoltzGen binding
```yaml
binding: <label_seq_id_1>,<label_seq_id_2>,...
```
#### RFD3 select_hotspots
```yaml
select_hotspots:
    <chain><auth_resnum>: <atom1>,<atom2>
    <chain><auth_resnum>: <atom1>,<atom2>
```

### PIPELINE HANDOFF
- pdb_id: <PDB accession used>
- target_chain: <chain ID>
- partner_chain: <chain ID>
- target_complex: <ProteinA / ProteinB>
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
  these guesses propagate silently into wrong BoltzGen specs. If you are unsure,
  call `get_sequence_map` again; it is cheap.
