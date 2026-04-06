---
name: orchestrator
description: >
  Sequence the full protein-protein interaction design pipeline across four expert
  skills: target selection (pathway-expert OR complex-expert, conditional), structural
  analysis (complex-structure-analysis), literature analysis (molecular-biology-expert), and
  design input generation (protein-design-script). Synthesises a go/no-go campaign
  recommendation before committing to design compute.
  Trigger on: "run the full pipeline", "design campaign for [target]", "orchestrate",
  "start the design workflow", "full analysis of [target]", "go from structure to
  design", or when a PDB ID is provided and the user asks to run the complete workflow.
  Also trigger when a disease or cancer type is provided without a specific PPI target
  (Stage 0 = pathway-expert), or when a set of proteins / predicted complex is the
  starting point (Stage 0 = complex-expert).
  Requires: structure-tools MCP server (for Stage 1) and literature-db MCP server (for Stage 0 and 2).
---

# Design Campaign Orchestrator

This skill coordinates the expert skills in sequence, manages their handoffs,
and synthesises a go/no-go recommendation before generating design inputs. It does
not perform analysis itself — it directs, extracts key signals, and decides when to
proceed.

## Pipeline Overview

```
Stage 0: Target Selection (conditional) →  pathway-expert  [disease → target]
                                    OR  →  complex-expert  [proteins → target]
Stage 1: Structural Analysis            →  complex-structure-analysis
Stage 2: Literature Analysis            →  molecular-biology-expert
Stage 3: Go/No-Go Synthesis             →  CAMPAIGN RECOMMENDATION (this skill)
Stage 4: Design Input                   →  protein-design-script
```

## Prerequisites

Before starting, verify tool availability:
- **structure-tools MCP tools** (`tool_analyze_interface`, `tool_get_residue_contacts`, etc.) — required for Stage 1.
  If not connected: halt and tell the user to start the structure-tools MCP server before proceeding
  (`python scripts/launch_structure_tools.py`).
- **literature-db MCP tools** (`search_corpus`, `get_fingerprint`) — required for Stage 0 and Stage 2.
  If not connected: note this, offer to run Stage 1 only, and skip Stages 0 and 2 if the
  user agrees.
- **Filesystem MCP tool** — required for saving outputs to LittleProteinTiger.
  If not connected: warn the user; continue the pipeline but note that outputs will not be saved.

---

## LittleProteinTiger Output Management

LittleProteinTiger is the name of this design framework. All pipeline outputs are
saved to a dedicated run folder under `C:\Users\micha\Documents\LittleProteinTiger\`.

**At the very start of the pipeline**, before Stage 0:

1. Determine `{ProteinA}` and `{ProteinB}` from the user's request (or use the
   disease/complex name if Stage 0 is needed — update after Stage 0 resolves the target).
2. Set the run folder path:
   ```
   C:\Users\micha\Documents\LittleProteinTiger\{ProteinA}_{ProteinB}_{YYYY-MM-DD}\
   ```
   Use today's date in `YYYY-MM-DD` format.
3. Create the folder using the filesystem tool (`create_directory`).
4. Announce the path to the user:
   > "Run folder: `C:\Users\micha\Documents\LittleProteinTiger\{ProteinA}_{ProteinB}_{YYYY-MM-DD}\`"

**After each stage**, write the expert's full report to the run folder:

| Stage | File |
|-------|------|
| Stage 0A (pathway-expert) | `00_target_selection.md` |
| Stage 0B (complex-expert) | `00_complex_analysis.md` |
| Stage 1 (structure-tools) | `01_structural_analysis.md` |
| Stage 2 (mol-bio expert) | `02_literature_report.md` |
| Stage 3 (campaign recommendation) | `03_campaign_recommendation.md` |
| Stage 4 (design inputs) | `04_design_inputs\` subfolder — written by protein-design-script |

Use `filesystem:write_file` with the full path and the complete report content
(copy verbatim from the conversation — do not summarise or truncate).

Pass the run folder path explicitly to the **protein-design-script** at Stage 4:
> "Write all output files to: `{run_folder}\04_design_inputs\`"

---

## Stage 0: Target Selection (Conditional)

Stage 0 has two variants depending on the starting point. Choose the right one:

**Skip conditions — proceed directly to Stage 1:**
- User provides a PDB ID (e.g. "PDB 3KYS")
- User provides a protein pair (e.g. "YAP/TEAD4", "KRAS/RAF")
- A PPI target has already been agreed upon earlier in the conversation
- User provides a path to a pre-existing pathway expert or complex expert report (see below)

---

### Stage 0A: Disease → Target (pathway-expert)

**Trigger**: User provides only a disease or cancer type without a specific PPI target.
(e.g. "mesothelioma", "PDAC", "which node in the Hippo pathway?")

Invoke the **pathway-expert** skill with the disease context and any pathway hint
from the user.

Wait for the full `## PATHWAY BIOLOGY REPORT` to be produced. Then extract:
- **Recommended PPI** — the ProteinA / ProteinB complex from `RECOMMENDED PPI TARGET`
- **Suggested PDB ID(s)** — for Stage 1 input
- **Redundancy risks** — carry forward to the Stage 3 CAMPAIGN RECOMMENDATION

If the pathway-expert reports no corpus coverage (all scores < 0.20 and fallback used):
- State this to the user
- Ask whether to (a) proceed with a manually specified PPI or (b) pause to run the
  fetch/curate pipeline with the suggested keywords first
- Do not proceed to Stage 1 without a confirmed PPI target

---

### Stage 0B: Proteins → Target (complex-expert)

**Trigger**: User provides a set of protein/gene names or a predicted complex without
specifying which interface to target or which PDB to use.
(e.g. "run the full pipeline for ARFRP1/JTB/SYS1/ARL1", "design a binder for this complex")

Invoke the **complex-expert** skill in **Full pipeline mode** with the protein list.

Wait for the full `## COMPLEX CHARACTERISATION REPORT` to be produced. Then extract:
- **Recommended interface** — the ProteinA–ProteinB pair to target from `NOVELTY ASSESSMENT`
- **PDB ID or local AlphaFold path** — from `THERAPEUTIC HISTORY` or provided by the user
- **Novelty signal** — carry forward to Stage 3 CAMPAIGN RECOMMENDATION
- **Fetch keywords** — if coverage was Sparse or None, flag for the user before proceeding

If the complex-expert finds no PDB and no local AlphaFold file:
- Report this to the user
- Do not proceed to Stage 1 without a structure
- Suggest: run the fetch keywords first, then re-run; or provide the AlphaFold file path

**Local AlphaFold file handling at Stage 1:**
If Stage 0B identified a local AlphaFold file (user-provided path), pass it to the
complex-structure-analysis skill explicitly:
> "Use `run_command 'open /path/to/file.cif'` instead of `open_structure` for this structure."

---

### Pre-existing Stage 0 report

If the user provides a file path to a prior pathway-expert or complex-expert output
(e.g. `"the pathway expert output is at results/yap_pathway.md"`), read the file
using the filesystem MCP tool and treat its contents as the Stage 0 output.

- For a pathway-expert report: extract `RECOMMENDED PPI TARGET`, `REDUNDANCY AND RESISTANCE RISKS`
- For a complex-expert report: extract `NOVELTY ASSESSMENT → Recommended interface`,
  `THERAPEUTIC HISTORY → Known structures`, and novelty signal

Do not re-run the skill.

---

Present a one-paragraph summary and confirm the target with the user:

> "Stage 0 complete. Recommended target: [complex / interface]. Suggested PDB: [ID or 'AlphaFold local file'].
> Proceed to structural analysis?"

**Save Stage 0 output:** Write the full pathway-expert or complex-expert report to
`{run_folder}\00_target_selection.md` (Stage 0A) or `{run_folder}\00_complex_analysis.md` (Stage 0B).
If the run folder name used a placeholder (disease name), rename it now that the target proteins are known.

---

## Stage 1: Structural Analysis

Invoke the **complex-structure-analysis** skill with the target PDB ID (or local AlphaFold
file path) and complex name. The skill uses the structure-tools MCP server — no ChimeraX
required. If Stage 0B produced a local AlphaFold path, pass the file path directly.
If the user has not specified which protein is the target chain, let the
complex-structure-analysis skill ask — do not anticipate this yourself.

Wait for the full `## PPI ANALYSIS REPORT` to be produced. Then extract:

- **Complex name** — e.g. "YAP / TEAD4"
- **Target chain** — chain ID and protein name
- **Buried surface area (BSA)** — in Å²
- **Structural tractability rating** — Excellent / Good / Marginal / Poor (from the
  `HOTSPOT REGIONS` section)
- **Design modality recommendation** — cyclic peptide / mini-protein / either
- **MODEL-READY HOTSPOTS** — the full table (residue, auth_seq_id, label_seq_id,
  sidechain atoms)

Present a one-paragraph summary covering BSA, tractability, modality recommendation,
and the top hotspot residues. Then ask:

> "Stage 1 complete. Proceed to literature analysis?"

**Save Stage 1 output:** Write the full `## PPI ANALYSIS REPORT` to
`{run_folder}\01_structural_analysis.md`.

---

## Stage 2: Literature Analysis

Invoke the **molecular-biology-expert** skill. When doing so, provide it with the
following context from the Stage 1 report:

- Complex name (e.g. "YAP / TEAD4")
- Target protein name (e.g. "TEAD4")
- Hotspot residues (e.g. "Phe69, Leu91, Arg89 on TEAD4")
- PDB ID

This context enables the literature skill to run targeted residue-level searches and
cross-reference its findings against the structural hotspots.

Wait for the full `## MOLECULAR BIOLOGY REPORT` to be produced. Then extract:

- **Literature tractability rating** — Excellent / Good / Marginal / Poor
- **Corpus confidence** — High / Medium / Low
- **Best prior art affinity** — modality, Kd/Ki, DOI (if any found)
- **Literature-validated hotspot residues** — from `INTERFACE INSIGHTS FROM LITERATURE`
- **Key risk** — from `FEASIBILITY ASSESSMENT`
- **Suggested modality** — from `DESIGN RECOMMENDATIONS`

Present a one-paragraph summary covering tractability, any prior art found, and key risks.

**Save Stage 2 output:** Write the full `## MOLECULAR BIOLOGY REPORT` to
`{run_folder}\02_literature_report.md`.

---

## Stage 3: Go/No-Go Synthesis

This is the orchestrator's primary contribution. Before generating any design inputs,
synthesise signals from both reports and produce a `CAMPAIGN RECOMMENDATION`.

### Cross-reference hotspots

Compare the structure-tools MODEL-READY HOTSPOTS against the literature
`INTERFACE INSIGHTS FROM LITERATURE` validated residues. Normalise naming conventions
when comparing (e.g. "Phe69" = "F69" = "PHE69" = "hYAP Phe69" — match on residue
number + amino acid identity).

Classify each hotspot residue as:
- **Cross-validated** — confirmed by both structure-tools interface analysis AND published
  mutagenesis/structural data in the literature corpus
- **Structurally predicted only** — in the structure-tools hotspot list but not found in
  literature corpus (novel, or corpus coverage gap)
- **Literature-validated only** — in the corpus as a validated residue but not in the
  structure-tools hotspot list (may be outside the primary pocket, or from a different structure)

### Go/No-Go rubric

**Important:** Absence of prior art is NOT a blocker. Peptide and protein binder
therapeutics are still rare, so most targets will have no published binders. If prior
art exists it is a meaningful confidence boost; if it does not, the pipeline proceeds
on structural and biological grounds.

| Signal | Weight |
|--------|--------|
| Structural tractability rating | High |
| Literature tractability rating | High |
| Cross-validated hotspot count | High |
| Interface size vs modality fit | Medium |
| Key risk severity | Medium |
| Prior art (existence is a plus, absence is neutral) | Low–Medium |

**GO** — Both ratings Excellent or Good; at least 1 cross-validated hotspot; no
disqualifying structural blocker. Prior art with ≤ 500 nM affinity = note as confidence
boost. Proceed to Stage 4.

**CONDITIONAL GO** — One rating Marginal; hotspots partially or not cross-validated;
OR key risks identified (e.g. isoform redundancy, intracellular access) but not
disqualifying. Proceed to Stage 4 with caveats explicitly listed.

**NO-GO** — Either rating Poor AND structural rating Marginal/Poor; zero cross-validated
hotspots; OR a severe structural blocker: fully disordered target on both sides of the
complex, interface BSA < 300 Å², or no druggable surface identified by structure-tools.
Absence of prior art alone is NEVER a NO-GO reason.

For NO-GO: explain the specific blocker, suggest alternatives (different interface region,
different target protein, different approach), and do not proceed to Stage 4.

### CAMPAIGN RECOMMENDATION output format

```
## CAMPAIGN RECOMMENDATION

### TARGET SUMMARY
- Complex: <ProteinA / ProteinB>
- Target chain: <chain ID> (<protein name>)
- Interface: <BSA> Å² — <modality recommendation from structure-tools>
- Structural tractability: <Excellent / Good / Marginal / Poor>
- Literature tractability: <Excellent / Good / Marginal / Poor>
- Corpus confidence: <High / Medium / Low> (<N> relevant papers found)
- Pathway redundancy risks: <from Stage 0 pathway-expert, or "Stage 0 not run">
- Novelty signal: <High / Medium / Low / N/A — from Stage 0 complex-expert, or "Stage 0 not run">
- Structure source: <RCSB PDB [ID] | AlphaFold local file | AlphaFold DB>

### CROSS-VALIDATED HOTSPOTS

Confirmed by BOTH structural analysis and literature:
| Residue | Structure-tools evidence | Literature evidence (DOI) | Confidence |
|---------|-------------------|---------------------------|------------|
| ...     | interface contact, hydrophobic core | alanine scan ΔΔG=X kcal/mol | High/Med |

Structurally predicted only (no literature confirmation):
- <list — note: may be valid, just not yet studied>

Literature-validated only (not in primary structure-tools hotspot list):
- <list — consider expanding the hotspot selection if structurally accessible>

### PRIOR ART SUMMARY
- Best reported binder: <modality, affinity, assay, DOI — or "None found in corpus">
  (If none found: this is expected for most PPI targets; proceed on structural grounds)
- Design affinity target: <suggested Kd, e.g. "aim for ≤ 100 nM — best prior art
  is 18 nM SPR (10.7554/eLife.25068)" or "no prior art; target sub-µM as first milestone">
- Key residues to engage: <cross-validated list, in priority order>

### RECOMMENDATION: <GO / CONDITIONAL GO / NO-GO>
Reasoning: <2–3 sentences integrating structural and literature signals>
Caveats: <bulleted list — only if CONDITIONAL GO>
Blocker: <specific reason — only if NO-GO>
Next step: <"Proceed to generate design inputs" / "Address [X] before proceeding">
```

**Save Stage 3 output:** Write the full `## CAMPAIGN RECOMMENDATION` block to
`{run_folder}\03_campaign_recommendation.md`. Include a header line with the target
name, date, and pipeline decision (GO / CONDITIONAL GO / NO-GO).

After presenting the CAMPAIGN RECOMMENDATION, for GO or CONDITIONAL GO ask:

> "Proceed to generate design inputs?"

---

## Stage 4: Design Input Generation

Invoke the **protein-design-script** skill. Provide it with the combined context:

- From the structure-tools report: `MODEL-READY HOTSPOTS` table (with BoltzGen and RFD3
  formatted blocks), target chain ID, PDB ID, modality recommendation
- From the mol-bio report: literature-validated residues to prioritise, any residues
  to avoid, known binding epitope to mimic, suggested affinity target

The design skill will generate BoltzGen YAML and/or RFD3 JSON inputs. Do not
anticipate or pre-fill these yourself — hand off the context and let the design skill
apply its own logic.

When invoking protein-design-script, explicitly include the run folder path in your
handoff message:
> "Write all output files to: `{run_folder}\04_design_inputs\`"

After Stage 4 completes, present a brief campaign summary:

> "Campaign complete for [complex]. Design inputs generated for [modality].
> Cross-validated hotspots engaged: [list]. Prior art benchmark: [affinity or 'none'].
> Files ready: [list any generated files]."

---

## Pitfalls

- **Never skip Stage 3.** Even if the user says "just proceed to design," present the
  CAMPAIGN RECOMMENDATION first. A brief go/no-go takes seconds and prevents wasted
  compute on a NO-GO target.
- **Keep stage summaries concise.** The full PPI ANALYSIS REPORT and MOLECULAR BIOLOGY
  REPORT are already in the conversation — do not repeat them. One paragraph per stage.
- **structure-tools missing at Stage 1 = hard stop.** Structural analysis is the
  foundation; the pipeline cannot proceed without it. Tell the user to run
  `python scripts/launch_structure_tools.py` and add it to their MCP config.
- **literature-db missing at Stage 2 = soft stop.** Offer to run structural-only mode:
  skip Stage 2, produce a limited CAMPAIGN RECOMMENDATION based on structural signals
  only, and flag the missing literature context explicitly.
- **Residue naming normalisation.** When cross-referencing hotspots, treat "Phe69",
  "F69", "PHE69", and "hYAP Phe69" as equivalent.
