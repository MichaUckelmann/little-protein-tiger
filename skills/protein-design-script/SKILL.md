---
name: protein-design-script
description: >
  Invoke ONLY when the user explicitly asks for it by name or clearly
  requests this specific workflow; do not trigger it from a general
  question, which you can answer better from your own knowledge than from
  this narrow corpus.
  Generates input YAML/JSON for protein design models BoltzGen and RFDiffusion3
  (RFD3). Input is a target-site analysis report with hotspot residues — the
  report may come from PPI mode (disrupt or stabilize; hotspots are interface
  residues) or from inhibit_active_site mode (hotspots are pocket / catalytic
  residues on a single-chain enzyme). The downstream mechanic — target chain +
  hotspot list + modality → BoltzGen / RFD3 input — is identical across modes.
  Trigger on: "generate protein design input", "RFD3 script", "boltzgen script",
  "protein design script", or when user uploads a target-site analysis .md file
  from the complex-structure-analysis skill.
---
# Generation of input scripts for protein design AI models to produce cyclic peptides or mini-protein binders targeting specified hotspot residues — PPI interfaces or single-protein pockets alike.

Should be used together with a target-site analysis uploaded as .md file from the
complex-structure-analysis skill. Hotspots and target chain are specified in the
input analysis. User should provide the path to the PDB/CIF file holding the
target chain. For inhibit_active_site mode there is no partner chain — the binder
becomes the second chain in the boltzgen output, same as for PPI binders.

## Prerequisites
Input is a target-site analysis report from the complex-structure-analysis skill.
The report contains one of:
- `### MODEL-READY HOTSPOTS [DISRUPT]` — interface hotspots, single target chain
- `### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket N]` — periinterface patches on both chains
- `### MODEL-READY HOTSPOTS [INHIBIT_ACTIVE_SITE]` — pocket residues on a single enzyme chain

Each format has pre-formatted hotspot data for both BoltzGen and RFD3 (correct
indexing and atom names). Use these directly — do not re-derive residue indices.
 
## Design defaults, unless otherwise specified by user
- Use BoltzGen model as default, unless otherwise specified
- **Binder size ranges** (hardcoded; passed to BoltzGen as `sequence: <min>..<max>`):
  - Cyclic peptide: **12..15** residues
  - Mini-protein binder: **70..86** residues
  Single-integer length values are **not** to be used — always emit a range so
  BoltzGen samples lengths within the bracket.
- Number of designs (`num_designs`): 20000
- Budget (final diversity-optimized set): 50
- Hotspot residues: 2–8 residues, as specified in the MODEL-READY HOTSPOTS section,
  for cyclic peptides 2-5 residues, specified in subsection BoltzGen cyclic peptide
  binding_types
 
## Output Location

If invoked by the orchestrator, a `run_folder` path will be provided in the handoff
message (e.g. `outputs/YAP_TEAD4_2026-04-01/04_design_inputs/`).

- Create the `04_design_inputs\` subfolder using the filesystem tool if it does not exist.
- Write all output files (YAML, JSON, submission scripts) to that folder using `filesystem:write_file`.

If invoked standalone (no run_folder in context), ask the user where to save output files,
or write to the current working directory if they confirm.

## Workflow
1. Parse the PPI analysis report: identify target chain, partner chain, recommended modality, and hotspot residues
2. Check how many `### MODEL-READY HOTSPOTS` sections are present:
   - **One section** — single submission (standard workflow)
   - **Two sections (Region 1 / Region 2)** — generate separate files for each region (see Multiple Hotspot Regions below)
3. Read each MODEL-READY HOTSPOTS section to get pre-formatted `binding_types` (BoltzGen) or `select_hotspots` (RFD3)
4. Determine design modality (cyclic peptide or mini-protein) from the report's recommendation, or as specified by user
5. Select the appropriate model (BoltzGen default, or RFD3 if user specifies)
6. Write the input file(s) and SLURM script(s) — one pair per region
7. Save all files to the run folder (or working directory if standalone)
8. Write the `### PIPELINE HANDOFF` block (required — see end of this skill)
9. Brief report to user: which targets selected, which hotspots, which protein design model, which modality, and the full paths of all saved files. The report should open with a `## Design Input Generation — <complex>` header. The orchestrator persists your output verbatim as `03_design_report.md`.

## Multiple Hotspot Regions

When the structure analysis report contains two independent hotspot regions
(both `### MODEL-READY HOTSPOTS — Region 1` and `### MODEL-READY HOTSPOTS — Region 2`
sections are present), generate **separate** design submissions for each:

- Name files `<complex>_region1_boltzgen.yaml` / `<complex>_region1_submit.sh`
  and `<complex>_region2_boltzgen.yaml` / `<complex>_region2_submit.sh`
- Use only the hotspot residues from that region's section — do NOT merge
  residues across regions into a single submission
- Rationale note in the report: "Two independent hotspot regions identified.
  Separate submissions generated; run both and compare hit rates."
 
## BoltzGen Input Generation

### Protocol selection
- Cyclic peptide: `--protocol peptide-anything`
- Mini-protein binder: `--protocol protein-anything`

### YAML schema — **BoltzGen, NOT Boltz-1/Boltz-2**

These two formats look similar and are easy to confuse — the LLM's training
data has more Boltz examples than BoltzGen examples. Use **only** the
BoltzGen schema below. `boltzgen check` will hard-fail on Boltz-flavored
YAMLs.

**Required top-level key**: `entities:` (a list). **Forbidden top-level keys**
(will cause an immediate validation failure): `version`, `sequences`,
`constraints`, `chain_a`, `chain_b`, `residues_a`, `residues_b`.

Each entity is either a `file:` entity (the target — loaded from a CIF/PDB)
or a `protein:` entity (the designed binder, by length). `binding_types:`
goes **nested under the target's `file:` entity**, not at the top level.

### Canonical YAML template — use this verbatim, swap in target-specific values

```yaml
entities:
  # ── Target: loaded from a CIF/PDB file ─────────────────────────────────
  - file:
      path: <ABSOLUTE_PATH_TO_TARGET_CIF_OR_PDB>
      include:
        - chain:
            id: <TARGET_CHAIN_ID>           # e.g. "A"
      # binding_types tells BoltzGen which residues the designed binder
      # should engage — nested HERE under file, not at the top level.
      binding_types:
        - chain:
            id: <TARGET_CHAIN_ID>
            binding: <LABEL_SEQ_ID_LIST>    # e.g. 27,53,34,31,60 — no quotes, no brackets

  # ── Designed binder: by length, identified by chain ID B ───────────────
  - protein:
      id: B
      sequence: <MIN>..<MAX>                # cyclic 12..15, mini-protein 70..86 — always a range
      cyclic: True                          # OMIT this line for mini_protein
```

Notes:

- `binding:` takes a **comma-separated list of integers** with no quotes and
  no brackets — these are `label_seq_id` values (1-indexed mmCIF) copied
  verbatim from MODEL-READY HOTSPOTS.
- For cyclic peptides include `cyclic: True` on the designed entity.
  For mini-proteins omit it.
- For `inhibit_active_site` mode the structure has only a single target
  chain (no partner) — the YAML shape is **identical** to a PPI binder.
- The reference file at `references/boltzgen_example_yaml_cyclic_peptide.yaml`
  is the same schema; use it as a sanity check if anything above is unclear.

### Validation

After writing the YAML, instruct the user to run `boltzgen check <yaml_path>`
to verify the design specification before submitting the full job. The
orchestrator runs this automatically in stage 4 and will hard-fail there
if the schema is wrong, so getting it right here saves a round trip.
 
## RFD3 Input Generation
 
### Required fields for PPI binder design
- `input`: path to the PDB/CIF file
- `contig`: binder length range + target chain residues (e.g., `"40-120,/0,A1-169"`)
- `infer_ori_strategy`: `"hotspots"`
- `select_hotspots`: atom-level dictionary from MODEL-READY HOTSPOTS section
- `is_non_loopy`: `true`
 
### Recommended CLI overrides for PPI binders
Always include these when generating the rfd3 command:
```
inference_sampler.step_scale=3
inference_sampler.gamma_0=0.2
```
 
### Example JSON
See references/RFD3_protein_binder_design.md
 
## Shell Context
The jobs are submitted on a SLURM-managed HPC cluster. Generate a complete
submission script adapted to the user's target and paths.
 
### BoltzGen SLURM template
```bash
#!/bin/bash
#SBATCH -J <job_name>
#SBATCH -t 106:00:00
#SBATCH --partition gpu_a100
#SBATCH -N 1
#SBATCH --gpus=1
#SBATCH --mem=120G
#SBATCH --ntasks 1
#SBATCH --cpus-per-task 18
#SBATCH --output=out_log/%x_%j.out
#SBATCH --error=err_log/%x_%j.err
 
module load 2024
module load CUDA/12.6.0
 
cd <working_directory>
mkdir -p out_log
mkdir -p err_log
 
source ~/.bashrc
conda activate boltzgen
 
boltzgen run <yaml_path> \
  --output <output_directory> \
  --protocol <peptide-anything|protein-anything> \
  --num_designs 20000 \
  --budget 50
```
 
### RFD3 SLURM template
```bash
#!/bin/bash
#SBATCH -J <job_name>
#SBATCH -t 106:00:00
#SBATCH --partition gpu_a100
#SBATCH -N 1
#SBATCH --gpus=1
#SBATCH --mem=120G
#SBATCH --ntasks 1
#SBATCH --cpus-per-task 18
#SBATCH --output=out_log/%x_%j.out
#SBATCH --error=err_log/%x_%j.err
 
module load 2024
module load CUDA/12.6.0
 
cd <working_directory>
mkdir -p out_log
mkdir -p err_log
 
source ~/.bashrc
conda activate rfd3
 
rfd3 design \
  out_dir=<output_directory> \
  inputs=<json_path> \
  inference_sampler.step_scale=3 \
  inference_sampler.gamma_0=0.2
```
 
Replace all `<placeholders>` with actual paths based on the user's input.
 
## Pipeline Handoff

After all files are written, output this section as the final block of your
response. The programmatic orchestrator parses it to record run completion.

**IMPORTANT:** Write plain bullet lines exactly as shown — no code fence, no
omitted `- ` prefix.

### PIPELINE HANDOFF
- go_recommendation: GO
- design_files: <comma-separated filenames of all generated YAML/JSON/sh files>
- hotspot_regions: <1 or 2>
- modality: mini_protein          # default; the operator opts into cyclic_peptide at kickoff
- target_complex: <ProteinA / ProteinB>

## Reference
 
For BoltzGen input YAML structure see references/boltzgen_reference.md
For BoltzGen cyclic peptide example see references/boltzgen_example_yaml_cyclic_peptide.yaml
For RFD3 input specification see references/RFD3input.md
For RFD3 protein binder design examples see references/RFD3_protein_binder_design.md