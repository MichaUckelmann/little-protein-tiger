---
name: chimerax-ppi-analysis
description: >
  Analyze protein-protein interaction interfaces using ChimeraX to identify surface
  sites for disruption by de novo cyclic peptides and mini-proteins. Use whenever the
  user asks to find interface residues, hydrophobic patches, targetable hotspots for
  peptide binder design, or prepare structural data for a protein design pipeline.
  Trigger on: "interaction interface", "contact residues", "buried surface",
  "hydrophobic patch", "hotspot", "disrupt interaction", "PPI analysis", "design a
  binder", "cyclic peptide target", "mini-protein target", or when a multi-chain PDB
  is opened and the user asks which surface to target. Also trigger for known complexes
  (YAP-TEAD, PD-1/PD-L1, p53-MDM2) targeted with peptide/protein biologics. Requires
  ChimeraX MCP tools.
---

# ChimeraX PPI Interface Analysis for Peptide/Mini-Protein Design

This skill guides a systematic analysis of protein-protein interaction interfaces
using ChimeraX MCP tools. The goal is to identify and characterize surface sites
on a target protein that can be disrupted by de novo designed cyclic peptides or
mini-proteins.

The output is a structured report of targetable hotspot residues, ready to be
consumed by a downstream protein design agent or skill.

## Command Batching — Read First

**Always batch multiple ChimeraX commands into a single `run_command` call using
semicolons.** ChimeraX executes them sequentially in the same session.

```
chimerax:run_command  command="cmd1 ; cmd2 ; cmd3"
```

This is critical for staying within tool-use limits. Do not make a separate
`run_command` call for each step unless the output of one call is needed to
construct the next command.

**`chimerax:color_models` counts as a separate tool call — prefer `color` inside
`run_command` instead:**
```
chimerax:run_command  command="color #1/A lightblue ; color #1/B salmon"
```

---

## Design Modality Context

The binder modalities under consideration are **cyclic peptides** and
**de novo mini-proteins** (e.g. designed using RFdiffusion, ProteinMPNN, or
similar tools). This shapes what constitutes a good target site:

- **Surface-exposed hydrophobic patches are ideal.** These provide strong binding
  energy through hydrophobic contacts and are well-suited to the broad, slightly
  concave binding surfaces that peptides and mini-proteins present.
- **Broad, shallow pockets and flat hydrophobic surfaces are accessible.** Unlike
  small molecules that need deep, enclosed pockets, peptide/protein binders can
  drape across open surfaces and engage distributed contact networks.
- **Very deep, narrow pockets are NOT ideal.** A cyclic peptide or mini-protein
  cannot reach into a deep cleft the way a small molecule can. If the only hotspot
  is buried in a narrow channel, note this as a limitation.
- **Polar anchor points adjacent to hydrophobic patches add specificity.** A ring
  of charged or polar residues surrounding a hydrophobic core helps orient the
  designed binder and adds selectivity.

## Prerequisites

The user must have ChimeraX MCP tools connected. Verify that tools like
`chimerax:open_structure`, `chimerax:run_command`, and `chimerax:get_model_info`
are available.

## Workflow Overview

1. **Load and inspect** — Open structure, identify chains, determine the target
2. **Compute interfaces** — Buried surface area, contact residues, H-bonds, contacts
3. **Characterize interface residues** — Categorize in-head from the residue list
4. **Identify hotspots** — Cross-reference with literature, assess surface character
5. **Generate structured report** — Machine-readable output for design handoff

---

## Phase 1: Load and Inspect

### Open the structure and inspect in two calls

```
chimerax:open_structure  identifier=<PDB_ID>
chimerax:get_model_info  model_id=#1
```

From the model info, determine:
- How many chains are present and what each one is (use UniProt descriptions)
- Whether there are duplicate copies in the asymmetric unit
- Which chain pairs form the biologically relevant complex

If there are duplicate copies (e.g. chains A+B and C+D are two copies of the same
complex), hide the extra chains in one batched call:

```
chimerax:run_command  command="hide #1/<extra_chains> cartoons ; hide #1/<extra_chains> atoms"
```

### Determine the target chain

The user may specify which protein in the complex should be the **target** — the
protein whose surface will be engaged by the designed binder.

**If the user specifies a target:** Identify which chain corresponds to that
protein and designate it as the target.

**If the user does not specify a target:** Suggest the best option based on fold
stability, surface character, and conservation. Present reasoning and ask for
confirmation before proceeding.

---

## Phase 2: Compute Interfaces

### Step 1 — Buried surface area

```
chimerax:run_command  command="interfaces #1/<chainA>,<chainB>"
```

Interpretation:
- < 500 Å²: crystal packing contact, probably not biological
- 500–1000 Å²: small interface — cyclic peptide likely sufficient
- 1000–2000 Å²: typical stable PPI — cyclic peptide or small mini-protein
- > 2000 Å²: large interface — mini-protein recommended

### Step 2 — Select interface residues, name selection, list all residues, H-bonds and contacts in one batched call

```
chimerax:run_command  command="interfaces select #1/<chainA> contacting #1/<chainB> bothSides true ; name frozen iface sel ; info residues sel ; hbonds #1/<chainA> restrict #1/<chainB> reveal true color yellow ; contacts #1/<chainA> restrict #1/<chainB> reveal true color orange overlapCutoff -0.4 hbondAllowance 0.4"
```

The `info residues sel` output lists every interface residue with its chain, residue
name, and residue number. **Categorize these in-head** — do not make separate
`run_command` calls to filter by residue type. Use this lookup:

| Category | Residue names |
|---|---|
| Hydrophobic | ALA VAL LEU ILE MET PHE TRP PRO |
| Aromatic | PHE TYR TRP HIS |
| Charged | ASP GLU ARG LYS |
| Polar uncharged | SER THR ASN GLN TYR CYS |

Build the per-chain inventories (all / hydrophobic / aromatic / charged / polar) from
the single `info residues sel` output. This replaces 8 separate residue-type queries.

---

## Phase 3: Visualize

Set up a clean view with interface residues as sticks. Do this in **one batched call**:

```
chimerax:run_command  command="cartoon #1/<chainA>,<chainB> ; color #1/<target_chain> lightblue ; color #1/<partner_chain> salmon ; display iface & ~:hoh ; style iface stick ; color iface byhet ; label #1/<target_chain>:<key_residues> residues ; select clear ; set bgColor white ; lighting soft ; view #1/<chainA>,<chainB>"
```

Replace `<key_residues>` with a comma-separated list of the most important interface
residues identified in Phase 2 (e.g. `276,314,350`).

---

## Phase 4: Identify Hotspots

### Cross-reference with literature

Use `web_search` to find published data on this complex:
- Mutagenesis studies identifying critical binding residues
- Known inhibitors or peptidomimetics targeting this PPI
- Alanine scanning data showing which residues contribute most to ΔΔG

Search queries:
- `<complex_name> hotspot residues mutagenesis`
- `<PDB_ID> interface alanine scanning`
- `<complex_name> peptide inhibitor OR peptidomimetic`

### Assess surface character for peptide/mini-protein targeting

For each discrete interface region on the **target chain**, evaluate:

1. **Surface exposure** — Are the hydrophobic residues solvent-accessible when the
   partner is removed?
2. **Topography** — Broad and shallow or gently concave surfaces are excellent.
   Very deep, narrow pockets are a limitation — flag as better suited to small
   molecules.
3. **Hydrophobic patch size** — Clusters of 3+ hydrophobic residues in spatial
   proximity form a patch. Larger patches (5+) may warrant a mini-protein.
4. **Polar framing** — Polar/charged residues flanking the hydrophobic patch provide
   specificity anchor points.
5. **Literature validation** — Do mutagenesis data confirm these as energetic hotspots?

### Select final hotspot residues (2–8 residues)

Prioritize:
1. Hydrophobic/aromatic residues at the core of the interface
2. Residues validated by mutagenesis or literature as energetic hotspots
3. Residues that are surface-exposed and spatially clustered
4. If the interface is large, make a sub-selection for cyclic peptide binders: 3–5
   residues in a tight cluster

### Rank interfaces by suitability

- **Excellent** — Broad, surface-exposed hydrophobic patch with polar framing,
  validated by mutagenesis.
- **Good** — Moderate hydrophobic character with surface exposure.
- **Marginal** — Mostly polar/charged interface or deep narrow pocket.
- **Poor** — Featureless surface with no clear hotspot.

---

## Phase 4b: Prepare Model-Ready Hotspot Formats

### Get label_seq_id for all hotspot residues in one call

BoltzGen uses `label_seq_id` (1-indexed mmCIF numbering), not `auth_seq_id`.
Query **all hotspot residues at once** using a comma-separated residue spec:

```
chimerax:run_command  command="info residues #1/<target_chain>:<res1>,<res2>,<res3>,... attribute label_seq_id"
```

Example: `info residues #1/C:242,247,250,305,320 attribute label_seq_id`

Record both `auth_seq_id` and `label_seq_id` for every hotspot residue.

### Get sidechain atom names for all hotspot residues in one call

RFD3 `select_hotspots` requires 2 sidechain heavy atom names per residue.
Query all at once:

```
chimerax:run_command  command="info atoms #1/<target_chain>:<res1>,<res2>,<res3>,... & sidechain"
```

Select atoms that are interface-facing, heavy (no hydrogens), and on the sidechain
tip or branch. Use this rule of thumb per residue type:

- **ILE/LEU/VAL** — `CD1,CG2` / `CD1,CD2` / `CG1,CG2`
- **PHE** — `CD2,CZ` or `CE1,CZ`
- **TYR** — `CD2,OH` or `CE2,OH`
- **TRP** — `CD2,NE1` or `CZ2,CH2`
- **MET** — `CG,SD` or `SD,CE`
- **ARG/LYS** — `CZ,NH1` or `NZ,CE`
- **ASP/GLU** — `CG,OD1` or `CD,OE1`

### Assemble model-ready blocks

**BoltzGen format** — comma-separated `label_seq_id` values:
```yaml
binding: <label_seq_id_1>,<label_seq_id_2>,...
```
**BoltzGen cyclic peptide format** — sub-selection of tightly clustered hotspots:
```yaml
binding: <label_seq_id_1>,<label_seq_id_2>,...
```

**RFD3 format** — YAML-style with chain + auth_seq_id keys:
```yaml
select_hotspots:
    <chain><auth_resnum>: <atom1>,<atom2>
    <chain><auth_resnum>: <atom1>,<atom2>
```

---

## Phase 5: Generate Structured Report

The output report serves two purposes: human readability and machine parsability
for a downstream design agent. Use the exact section headers and field names below.

### Report Format

```
## PPI ANALYSIS REPORT

### COMPLEX OVERVIEW
- PDB ID: <id>
- Complex: <protein1> / <protein2>
- Target chain: <chain_id> (<protein_name>)
- Partner chain: <chain_id> (<protein_name>)
- Buried surface area: <value> Å²
- Interface residues: <count> total (<count_target> on target, <count_partner> on partner)
- Hydrogen bonds across interface: <count>
- VdW contacts across interface: <count>

### TARGET CHAIN INTERFACE RESIDUES
Chain <chain_id> (<protein_name>):
  All: <comma-separated list of resname+resnum, e.g. VAL242, ILE247, ...>
  Hydrophobic: <list>
  Aromatic: <list>
  Charged: <list>
  Polar: <list>

### PARTNER CHAIN INTERFACE RESIDUES
Chain <chain_id> (<protein_name>):
  All: <comma-separated list>
  Hydrophobic: <list>
  Aromatic: <list>
  Charged: <list>
  Polar: <list>

### HOTSPOT REGIONS (ranked by suitability for cyclic peptide / mini-protein)

#### Region <N>: <name> — <rating: Excellent/Good/Marginal/Poor>
- Target residues: <list as chain:resname+resnum>
- Partner residues engaging this region: <list>
- Surface character: <broad_shallow / gently_concave / flat / deep_narrow>
- Hydrophobic patch: <which target residues cluster, approximate spatial extent>
- Polar framing residues: <flanking polar/charged residues on target>
- Literature evidence: <mutagenesis data, known inhibitors, ΔΔG values if available>
- Design notes: <guidance for binder design — what the binder should mimic, which
  partner contacts to replicate, any geometric constraints>

### DESIGN RECOMMENDATIONS
- Recommended modality: <cyclic_peptide / mini_protein / either>
  (cyclic peptide for BSA < ~1200 Å², mini-protein for larger interfaces)
- Primary target region: <region name and rating>
- Key target residues the binder must engage: <list>
- Partner residues to mimic: <list — the designed binder should replicate these
  interactions with the target surface>
- Estimated contact surface needed: <rough Å² based on hotspot extent>
- Approach vector: <which face of the target to approach — describe relative to
  secondary structure elements>
- Caveats: <warnings — deep pockets inaccessible to peptide binders, disorder,
  crystal artifacts, conservation issues>

### MODEL-READY HOTSPOTS (2–8 residues)

Selected hotspot residues on target chain <chain_id>:

| Residue     | auth_seq_id | label_seq_id | Sidechain atoms (for RFD3) |
|-------------|-------------|--------------|----------------------------|
| <resname>   | <auth_num>  | <label_num>  | <atom1>,<atom2>            |
| ...         | ...         | ...          | ...                        |

#### BoltzGen binding_types
```yaml
binding: <label_seq_id_1>,<label_seq_id_2>,...
```
#### BoltzGen cyclic peptide binding_types
```yaml
binding: <label_seq_id_1>,<label_seq_id_2>,...
```
#### RFD3 select_hotspots
```yaml
select_hotspots:
    <chain><auth_resnum>: <atom1>,<atom2>
    <chain><auth_resnum>: <atom1>,<atom2>
```
```

### Handoff Notes

The DESIGN RECOMMENDATIONS section is specifically formatted for consumption by
a protein design agent (e.g. an RFdiffusion/ProteinMPNN pipeline skill). It
contains enough information for the design agent to:
1. Know which chain and residues to define as the target hotspot
2. Understand the surface topography and binding geometry
3. Know which partner residues' interactions to replicate in the binder
4. Select the appropriate binder size (cyclic peptide vs mini-protein)
5. Identify caveats that may affect design feasibility

---

## Reference

For detailed ChimeraX command syntax, atomspec patterns, and PPI-specific command
recipes, see `references/chimerax-ppi-commands.md`.

## Common Pitfalls

- The `interfaces` command requires protein atoms; exclude solvent/ions/ligands if
  they cause noise by specifying `#1/<chain> & protein`
- When the asymmetric unit has multiple copies, always focus on one biological unit
- The `name frozen` command captures the current selection at creation time
- Always `select clear` before presentation to remove green selection outlines
- If the partner chain is a short, disordered peptide (like YAP in 3KYS), the target
  should be the well-folded domain (TEAD) — the designed binder needs a stable surface
- Very deep, narrow pockets should be flagged as unsuitable for peptide/mini-protein
  design, even if they are validated hotspots for small molecules
- **Do not use `chimerax:color_models` — use `color #1/<chain> <colorname>` inside
  `run_command` so it can be batched with other commands**
