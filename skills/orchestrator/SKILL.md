---
name: orchestrator
description: >
  Sequence the full protein-protein interaction design pipeline across three expert
  skills: structural analysis (chimerax-ppi-analysis), literature analysis
  (molecular-biology-expert), and design input generation (protein-design-script).
  Synthesises a go/no-go campaign recommendation before committing to design compute.
  Trigger on: "run the full pipeline", "design campaign for [target]", "orchestrate",
  "start the design workflow", "full analysis of [target]", "go from structure to
  design", or when a PDB ID is provided and the user asks to run the complete workflow.
  Requires: ChimeraX MCP tools (for Stage 1) and literature-db MCP server (for Stage 2).
---

# Design Campaign Orchestrator

This skill coordinates the three expert skills in sequence, manages their handoffs,
and synthesises a go/no-go recommendation before generating design inputs. It does
not perform analysis itself — it directs, extracts key signals, and decides when to
proceed.

## Pipeline Overview

```
Stage 1: Structural Analysis    →  chimerax-ppi-analysis
Stage 2: Literature Analysis    →  molecular-biology-expert
Stage 3: Go/No-Go Synthesis     →  CAMPAIGN RECOMMENDATION (this skill)
Stage 4: Design Input           →  protein-design-script
```

## Prerequisites

Before starting, verify tool availability:
- **ChimeraX MCP tools** (`chimerax:open_structure`, etc.) — required for Stage 1.
  If not connected: halt and tell the user to connect ChimeraX before proceeding.
- **literature-db MCP tools** (`search_corpus`, `get_fingerprint`) — required for Stage 2.
  If not connected: note this, offer to run Stage 1 only, and skip Stage 2 if the
  user agrees.

---

## Stage 1: Structural Analysis

Invoke the **chimerax-ppi-analysis** skill with the target PDB ID and complex name.
If the user has not specified which protein is the target chain, let the chimerax skill
ask — do not anticipate this yourself.

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

---

## Stage 3: Go/No-Go Synthesis

This is the orchestrator's primary contribution. Before generating any design inputs,
synthesise signals from both reports and produce a `CAMPAIGN RECOMMENDATION`.

### Cross-reference hotspots

Compare the chimerax MODEL-READY HOTSPOTS against the literature
`INTERFACE INSIGHTS FROM LITERATURE` validated residues. Normalise naming conventions
when comparing (e.g. "Phe69" = "F69" = "PHE69" = "hYAP Phe69" — match on residue
number + amino acid identity).

Classify each hotspot residue as:
- **Cross-validated** — confirmed by both ChimeraX interface analysis AND published
  mutagenesis/structural data in the literature corpus
- **Structurally predicted only** — in the ChimeraX hotspot list but not found in
  literature corpus (novel, or corpus coverage gap)
- **Literature-validated only** — in the corpus as a validated residue but not in the
  ChimeraX hotspot list (may be outside the primary pocket, or from a different structure)

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
complex, interface BSA < 300 Å², or no druggable surface identified by ChimeraX.
Absence of prior art alone is NEVER a NO-GO reason.

For NO-GO: explain the specific blocker, suggest alternatives (different interface region,
different target protein, different approach), and do not proceed to Stage 4.

### CAMPAIGN RECOMMENDATION output format

```
## CAMPAIGN RECOMMENDATION

### TARGET SUMMARY
- Complex: <ProteinA / ProteinB>
- Target chain: <chain ID> (<protein name>)
- Interface: <BSA> Å² — <modality recommendation from chimerax>
- Structural tractability: <Excellent / Good / Marginal / Poor>
- Literature tractability: <Excellent / Good / Marginal / Poor>
- Corpus confidence: <High / Medium / Low> (<N> relevant papers found)

### CROSS-VALIDATED HOTSPOTS

Confirmed by BOTH structural analysis and literature:
| Residue | ChimeraX evidence | Literature evidence (DOI) | Confidence |
|---------|-------------------|---------------------------|------------|
| ...     | interface contact, hydrophobic core | alanine scan ΔΔG=X kcal/mol | High/Med |

Structurally predicted only (no literature confirmation):
- <list — note: may be valid, just not yet studied>

Literature-validated only (not in primary ChimeraX hotspot list):
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

After presenting the CAMPAIGN RECOMMENDATION, for GO or CONDITIONAL GO ask:

> "Proceed to generate design inputs?"

---

## Stage 4: Design Input Generation

Invoke the **protein-design-script** skill. Provide it with the combined context:

- From the chimerax report: `MODEL-READY HOTSPOTS` table (with BoltzGen and RFD3
  formatted blocks), target chain ID, PDB ID, modality recommendation
- From the mol-bio report: literature-validated residues to prioritise, any residues
  to avoid, known binding epitope to mimic, suggested affinity target

The design skill will generate BoltzGen YAML and/or RFD3 JSON inputs. Do not
anticipate or pre-fill these yourself — hand off the context and let the design skill
apply its own logic.

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
- **ChimeraX tools missing at Stage 1 = hard stop.** Structural analysis is the
  foundation; the pipeline cannot proceed without it.
- **literature-db missing at Stage 2 = soft stop.** Offer to run structural-only mode:
  skip Stage 2, produce a limited CAMPAIGN RECOMMENDATION based on structural signals
  only, and flag the missing literature context explicitly.
- **Residue naming normalisation.** When cross-referencing hotspots, treat "Phe69",
  "F69", "PHE69", and "hYAP Phe69" as equivalent.
