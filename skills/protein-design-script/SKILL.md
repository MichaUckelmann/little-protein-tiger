---
name: protein-design-script
description: >
  Generates input YAML/JSON for protein design models BoltzGen and RFDiffusion3
  (RFD3). Input is a PPI analysis report with hotspot residues. Trigger on:
  "generate protein design input", "RFD3 script", "boltzgen script", "protein
  design script", or when user uploads a PPI analysis .md file.
---
# Generation of input scripts for protein designing AI models to generate cyclic peptides or mini-protein binders targeting specified PPI sites.
 
Should be used together with a PPI analysis uploaded as .md file from the chimerax-ppi-analysis skill. Hotspots and target chain are specified in the input analysis. User should provide the path to the PDB/CIF file holding the target chain.
 
## Prerequisites
Input is a PPI analysis report from the chimerax-ppi-analysis skill. The report
contains a MODEL-READY HOTSPOTS section with pre-formatted hotspot data for both
BoltzGen and RFD3 (correct indexing and atom names). Use these directly — do not
re-derive residue indices.
 
## Design defaults, unless otherwise specified by user
- Use BoltzGen model as default, unless otherwise specified
- Number of designed residues defaults: cyclic peptides = 14, mini-protein binder = 90
- Number of designs (`num_designs`): 20000
- Budget (final diversity-optimized set): 50
- Hotspot residues: 2–8 residues, as specified in the MODEL-READY HOTSPOTS section, for cyclic peptides 2-5 residues, specified in subsection BoltzGen cyclic peptide binding_types
 
## Output Location

If invoked by the orchestrator, a `run_folder` path will be provided in the handoff
message (e.g. `C:\Users\micha\Documents\LittleProteinTiger\YAP_TEAD4_2026-04-01\04_design_inputs\`).

- Create the `04_design_inputs\` subfolder using the filesystem tool if it does not exist.
- Write all output files (YAML, JSON, submission scripts) to that folder using `filesystem:write_file`.

If invoked standalone (no run_folder in context), ask the user where to save output files,
or write to the current working directory if they confirm.

## Workflow
1. Parse the PPI analysis report: identify target chain, partner chain, recommended modality, and hotspot residues
2. Read the MODEL-READY HOTSPOTS section to get pre-formatted `binding_types` (BoltzGen) or `select_hotspots` (RFD3)
3. Determine design modality (cyclic peptide or mini-protein) from the report's recommendation, or as specified by user
4. Select the appropriate model (BoltzGen default, or RFD3 if user specifies)
5. Write the input file (YAML for BoltzGen, JSON for RFD3) using the correct format from references
6. Write the SLURM submission script (see Shell Context below)
7. Save all files to the run folder (or working directory if standalone)
8. Brief report to user: which target selected, which hotspots, which protein design model, which modality, and the full paths of all saved files
 
## BoltzGen Input Generation
 
### Protocol selection
- Cyclic peptide: `--protocol peptide-anything`
- Mini-protein binder: `--protocol protein-anything`
 
### YAML structure
Use the `binding_types` block from MODEL-READY HOTSPOTS directly. The residue
numbers in that block are already `label_seq_id` (1-indexed mmCIF), which is
what BoltzGen requires.
 
For cyclic peptides, include `cyclic: True` on the designed protein entity.
 
### Validation
After writing the YAML, instruct the user to run `boltzgen check <yaml_path>`
to verify the design specification before submitting the full job.
 
### Example YAML (cyclic peptide)
See references/boltzgen_example_yaml_cyclic_peptide.yaml
 
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
 
## Reference
 
For BoltzGen input YAML structure see references/boltzgen_reference.md
For BoltzGen cyclic peptide example see references/boltzgen_example_yaml_cyclic_peptide.yaml
For RFD3 input specification see references/RFD3input.md
For RFD3 protein binder design examples see references/RFD3_protein_binder_design.md