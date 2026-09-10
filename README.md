# Little Protein Tiger

**Point it at a disease and it comes back with designed protein binders — or
point it at a target you already have and skip to the design.** LPT reads the
literature, picks a target and a structure, chooses the epitope, generates and
folds candidate binders on a GPU, and gates them on measured geometry rather
than model confidence alone.

It is two pipelines sharing one corpus: a literature ETL (search → download →
LLM extraction → vector index + interaction graph) and a 16-stage resumable
design orchestrator. Skills run from the CLI against the Gemini, Claude or
OpenAI API, or conversationally inside Claude Desktop / Claude Code over MCP.

**Corpus:** **14,517 curated fingerprints**, shipped prebuilt with the vector
index and interaction graph.

> **Beta.** The corpus archive is published as
> [v0.1.0](../../releases/tag/v0.1.0); `python scripts/fetch_corpus.py`
> installs it. Remaining release state is tracked in
> [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## What it produces

A completed campaign leaves a self-contained `report.html` — site rationale in
the model's own words, confidence distributions, the filter funnel, and an
embedded Mol* viewer over the top designs' *actual* refolded structures, plus
an appendix with every stage report in full. Alongside it:

```
projects/<name>/runs/round-1/
  00_pathway.md  01_literature.md  02_structure.md   why this target, this structure, this epitope
  binder/2*.md                                        trim, spec, calibration verdict, scoring
  binder/scoring/{ranked.csv,top_k.fasta}             the designs themselves
  binder/report.html                                  ← open this
```

Real numbers from a real unattended run (`projects/mesothelioma_showcase`): the
one-sentence prompt *"Design cancer therapeutics to target key nodes in
mesothelioma."* → YAP1/TEAD1 on 3KYS → 317 gated survivors of 1,352 refolds,
best ipTM 0.937 / dock-RMSD 0.63 Å, for **$0.79 of API spend** and ~22 GPU-h.

Four illustrated walkthroughs built from runs in this repository — two PPI
discovery campaigns (CGRP receptor, YAP1/TEAD1), a PD-L1 binder campaign and a
corpus-explorer session — are published at
**[michauckelmann.github.io/little-protein-tiger](https://michauckelmann.github.io/little-protein-tiger/showcase/)**.
Their source is [`docs/showcase/`](docs/showcase/): every figure is extracted
from a run directory at build time and committed beside the page, so the
numbers can be audited rather than taken on trust.

## Start here

| If you are… | Go to |
|---|---|
| **new, or beta testing** | **[docs/beta-testing.md](docs/beta-testing.md)** — clean machine to first design run and first literature query, with prompts to try and what each costs |
| **using a coding agent to set up** | **[SETUP_AGENT.md](SETUP_AGENT.md)** — paste it into Claude Code; it interviews you, installs only what you need, verifies each step |
| **just curious it works** | `python scripts/quickstart.py` — ~8 s, no API key, no GPU, no corpus |
| **without a GPU** | Add `--stop-after spec`: both tracks' full reasoning path — target resolution, structure choice, epitope selection, trimming, spec generation — on an API key alone |
| **on a Claude subscription** | Register the MCP servers and use the corpus conversationally, no API key — **[docs/mcp.md](docs/mcp.md)** |
| **stuck** | `python scripts/doctor.py` — readiness per track, and the command that fixes each problem |

## Contents

- [Requirements](#requirements) · [Configuration](#configuration)
- [Pipeline overview](#pipeline-overview) — the two tracks and what each stage does
- [Usage](#usage) — [fetch](#1-fetch-papers) · [curate](#2-curate-papers) · [ingest](#3-ingest-vectors) · [ask the corpus](#4-ask-the-corpus-a-question) · [run a skill](#5-run-expert-skills-from-the-cli) · [corpus explorer](#6-corpus-explorer-conversational) · [PPI pipeline](#7-run-the-binder-design-pipeline-end-to-end) · [from a target name](#7b-run-the-binder-pipeline-from-a-target-name) · [on a cluster](#7c-scale-a-campaign-onto-a-slurm-cluster) · [PyMOL](#8-visualise-top-k-designs-in-pymol)
- [Inspecting the database](#inspecting-the-database) · [MCP](#use-it-from-claude-desktop--claude-code) · [Project structure](#project-structure)
- [Migrating to a new machine](#migrating-to-a-new-machine) · [Journal filtering](#journal-filtering--read-this-before-building-a-corpus)
- [Responsible use](#responsible-use) · [Licence](#licence-and-third-party-tools) — noncommercial; see [docs/licensing.md](docs/licensing.md) · [Further reading](#further-reading)

---

## Requirements

Python **3.12–3.14** (`pyproject.toml` pins `>=3.12,<3.15`). No compiler, no
conda, no system packages — every base dependency ships prebuilt wheels.

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate

pip install -e ".[dev]"           # base: structure + design tracks
pip install -e ".[corpus,dev]"    # ALSO the literature track (~3 GB: torch,
                                  # lancedb, sentence-transformers)

cp .env.example .env              # then add your keys
python scripts/fetch_reference_data.py    # ~52 MB, public, no key — required
                                          # before any ppi/binder run
python scripts/doctor.py                  # what this machine can run
```

Or `./scripts/setup.sh` (`--with-corpus` also installs the extra and fetches
the corpus archive; `--check` re-runs the readiness report only) to do all of
it in one step.

**`GEMINI_API_KEY` is the only key you need** — it is the default provider for
every pipeline stage, for curation, and for `ask_corpus.py`.
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are both optional, and not
interchangeable. `--provider claude` or `--provider openai` runs every stage on
that provider instead; beyond that, `ANTHROPIC_API_KEY` is also what Gemini
falls back to when a safety classifier declines a stage, so a run with only
`GEMINI_API_KEY` has no fallback left. If you use `--provider openai`, replace
the placeholder `gpt-5.6-*` rates in `config.yaml` with your account's before
trusting `--budget`.

`.env.example` documents every other variable — NCBI and Semantic Scholar rate
limits, the corporate-proxy CA settings, and the tool paths (`LPT_FOUNDRY_ROOT`,
`LPT_FOUNDRY_CKPT_DIR`, `LPT_BOLTZGEN_EXECUTABLE`, `LPT_PYROSETTA_PYTHON`,
`LPT_CLUSTER_PIPELINE_ROOT`, `LPT_CLUSTER_PROTENIX_REPO`,
`LPT_CLUSTER_SUBMIT_INSTRUCTIONS`).

**Running designs on a local workstation** — we suggest a CUDA GPU with at
least **24 GB VRAM**; more VRAM allows larger designs and targets. Budget
**~15 GB free disk** for a typical production campaign and up to ~90 GB for a
large target run at the un-calibrated default — measured across six campaigns,
the biggest of which came to 15 GB all-in. A local install of the
[foundry protein design suite](https://github.com/RosettaCommons/foundry) is
necessary — LPT neither ships nor installs it.

**[docs/beta-testing.md](docs/beta-testing.md)** walks all of this
step-by-step, including what to do when a step fails.
**[docs/environment_setup.md](docs/environment_setup.md)** covers the external
GPU tools and their licences.

---

## Configuration

All parameters live in `config.yaml`. To run a targeted search expansion without touching the main config, copy or write a separate config file and pass it with `--config`.

Key sections:

```yaml
keywords:
  - "TEAD[Title/Abstract] AND YAP[Title/Abstract]"
  - "macrocyclic peptide[Title/Abstract] AND protein interaction[Title/Abstract]"

search:
  max_results_per_query: 200
  ncbi_pmc: true
  europepmc_sources: ["ppr"]   # ppr = bioRxiv/medRxiv preprints

quality:
  require_tiered_journal: true
  min_score_to_download: 0.0

curation:
  provider: "gemini"                      # "claude" | "gemini" | "local"
  gemini_model: "gemini-3.1-flash-lite"   # used when provider is gemini
  model: "claude-haiku-4-5"               # used when provider is claude

rate_limits:
  europepmc_delay_s: 0.5
  ncbi_delay_s: 0.34
  download_delay_s: 1.0

vector_store:
  db_path: "data/vectors"
  embedding_model: "NeuML/pubmedbert-base-embeddings"
```

---

## Pipeline overview

Two related pipelines share the corpus + skill catalogue:

**Literature corpus (left side)** — build a curated, vector-indexed
biochemistry database, queryable by skills and humans:

```
fetch_papers.py          Search + download XMLs/PDFs → literature.db
      ↓
curate_papers.py         the curator extracts structured fingerprint JSONs
      ↓
ingest_vectors.py        Embed fingerprints into LanceDB vector store
      ↓
      ├── mcp_server.py            MCP tools for Claude Desktop skills
      │                            (search_corpus, get_fingerprint, …)
      │
      └── run_skill.py             CLI: run any expert skill via API
                                   (Claude or Gemini, no IDE required)
```

**Binder design (right side)** — an autonomous 7-stage pipeline that
takes a free-text design objective and produces a ranked top-K of
designed cyclic-peptide or mini-protein binders, with audit traces at
every stage:

```
PipelineRunner.run(query="Design therapeutics for ...")

  stage 0  pathway-expert OR          → target complex + PDB
           wildcard-expert (alt mode)   (novelty-driven; see --pathway-mode)
  stage 1  molecular-biology-expert   → tractability + target_site_hint
  stage 2  complex-structure-analysis → MODEL-READY HOTSPOTS (auth + label_seq)

  ── then one of two design backends (`--design-engine`, `design.backend`) ──

  foundry (DEFAULT) — hands the discovered target straight to the binder
  track's own stage machine, unchanged, from its `trim` stage onward:
      trim → binder_spec → pilot → calibration → production
           → binder_scoring → binder_summary       (RFD3 → solubleMPNN → RF3)
  Requires `--project`: these are multi-day GPU campaigns and the manifest
  is what makes them resumable. See **[Usage](#usage)** below.

  boltzgen (`--design-engine boltzgen`; also selected automatically by
  `--modality cyclic_peptide`, which RFD3 cannot build):
  stage 3  protein-design-script      → BoltzGen YAML + RFD3 JSON
  stage 4  design_runner              → BoltzGen pilot → gate → production
                                          (workstation GPU subprocess)
  stage 5  design_metrics + ranking   → enrich top-K with pyrosetta hotspot
                                          SASA, MMR-rank by composite score
  stage 6  design-analyst             → final candidate review + FASTA
```

Stage 0 has two modes (`--pathway-mode`, or `design.pathway.mode` in
`config.yaml`): `standard` runs `pathway-expert`, which ranks validated
drug targets with clinical precedent; `wildcard` runs `wildcard-expert`,
which triages on graph novelty and DepMap co-essentiality instead and
deliberately favours targets the literature has not converged on. Use
wildcard when you want a candidate nobody is already working on.

Stages 0-3 and 6 are LLM-driven skills; 4 and 5 are deterministic Python.
The orchestrator owns everything that must not be left to a model:
`auth_seq_id` / `label_seq_id` numbering, chain identity resolved from the
mmCIF rather than emitted by a skill, PDB-identity sanity checks, modality
reconciliation, and BoltzGen's output renumbering. `src/pipeline_runner.py`
is the state machine; [CLAUDE.md](CLAUDE.md) documents the wildcard triage
internals and every guard, with the incident each one was added after.


End-to-end driver: `scripts/run_pipeline.py --workflow ppi --query "..."
--project runname`. (`scripts/test_e2e.py` wraps the same runner with
per-stage trace capture and dialled-down BoltzGen batch sizes.)
Configuration lives under `design:` in `config.yaml` (workstation
executable, pilot/production batch sizes, hard filters, ranking weights,
pyrosetta env path).

**Reports.** Every run that reaches stage 5 gets an illustrated,
self-contained `report.html` — pathway/target rationale, prior art and
tractability, hotspot evidence, the design-generation stats, the hard-gate
funnel, top-K design cards, and an interactive [Mol*](https://molstar.org)
viewer over the top-ranked designs' actual BoltzGen refolds, plus the
design-analyst's final verdict rendered in full. Generated automatically as
a side effect (never a gate) after `analysis` and again after `summary`;
regenerate by hand with `scripts/generate_ppi_report.py outputs/<slug>` (or
`--project <slug> --round round-1`). No LLM and no GPU: it reads the same
markdown/CSV files the pipeline already writes. Shares its whole visual
design system — palette, layout, the Mol* explorer — with the binder
track's `report.html` (`src/binder_report.py`) via
`src/report_templates/_shared/`, so a run from either track reads as the
same product.

---

## Usage

All commands are run from the repository root with the venv active.

### 1. Fetch papers

Search all keywords in `config.yaml`, upsert metadata to `data/literature.db`, and download available XMLs/PDFs.

```bash
# Full run
python scripts/fetch_papers.py

# Dry run — search and index only, no downloads (hits are still upserted)
python scripts/fetch_papers.py --dry-run

# Use a separate config (e.g. a topic expansion)
python scripts/fetch_papers.py --config config_search_expansion.yaml

# Override keywords inline
python scripts/fetch_papers.py --keywords "MDM2 p53 inhibitor peptide" --max 100
```

Re-running is safe — already-downloaded papers are skipped.

### 2. Curate papers

Extract structured fingerprints from downloaded papers using Claude (or Gemini).

```bash
# Curate all downloaded-but-not-yet-curated papers
python scripts/curate_papers.py

# Limit batch size
python scripts/curate_papers.py --limit 50

# Dry run — list pending papers without calling the API
python scripts/curate_papers.py --dry-run

# Reprocess already-curated papers (e.g. after prompt update)
python scripts/curate_papers.py --reprocess --limit 20

# Curate a single paper by its DB key
python scripts/curate_papers.py --paper-key "doi:10.1101/2024.01.01.123456"

# Use a different provider
python scripts/curate_papers.py --provider gemini
```

Fingerprints are saved to `data/fingerprints/<paper_key>.json`.

### 3. Ingest vectors

Embed fingerprints into LanceDB for semantic search. Run after any new curation batch.

```bash
# Incremental — only embeds fingerprints not yet in the vector store
python scripts/ingest_vectors.py

# Full rebuild — drop and re-embed everything (e.g. after a schema change)
python scripts/ingest_vectors.py --rebuild
```

### 4. Ask the corpus a question

A conversational loop on **gemini-3.7-flash**, with the full corpus toolset —
semantic search, the interaction graph, DepMap co-essentiality, clusters,
quantitative evidence — so "what interacts with SPT16, and at what affinity?"
comes back as a sourced table with DOIs.

```bash
python scripts/ask_corpus.py "How does FACT reposition the H2A-H2B dimer?"
python scripts/ask_corpus.py                          # start empty, then prompt
python scripts/ask_corpus.py --provider claude --top-k 10
```

The corpus is weighted to chromatin, histone chaperones and structural
biology. A thin answer outside that is the corpus's coverage, not the field's —
`docs/beta-testing.md` lists what it answers well and what it does not.

### 5. Run expert skills from the CLI

`scripts/run_skill.py` runs any expert skill as a self-contained agentic loop — no Claude Desktop or IDE required. The skill's `SKILL.md` becomes the system prompt; tool calls are routed directly to Python (no MCP subprocess).

```
usage: run_skill.py --skill SKILL --query QUERY
                    [--model {claude,gemini}] [--model-id MODEL_ID]
                    [--context PATH] [--output PATH]
                    [--max-iter N] [--max-tokens N]
```

**Available skills:**

| Skill | What it does |
|---|---|
| `corpus-explorer` | **Conversational** research assistant for free-form corpus exploration: relational queries ("which proteins interact with X?"), pathway construction with affinities, and competing-hypothesis generation. Inline DOI citations, no pipeline handoff. Pairs with `--interactive` for multi-turn sessions |
| `pathway-expert` | Searches the literature corpus to characterise a signalling pathway in a disease context and recommend the best PPI target node — tier-ranked on validated drug-target evidence (clinical precedent, prior peptide binders, mutagenesis-validated hotspots) |
| `wildcard-expert` | Speculative counterpart to pathway-expert: graph-driven novelty triage (corpus interaction graph, DepMap co-essentiality, novelty_signal) to surface mechanistically connected but literature-under-explored PPI candidates. Disease-anchored AND basic-biology contexts. Emits hypothesis fields (`predicted_consequence`, `falsifying_readout`) so the designed binder doubles as a research probe |
| `complex-structure-analysis` | Analyses a PDB/CIF structure, computes BSA + hotspot patches, and outputs BoltzGen/RFD3-ready residue specs |
| `binder-optimizer` | Takes a predicted binder-target complex, proposes 4 single-point mutations, validates clashes, and emits AF3 submission JSONs |
| `molecular-biology-expert` | Queries the corpus for biochemical detail on a specific protein pair (binding affinities, hotspot residues, inhibitor data) |
| `complex-expert` | Corpus search focused on a named protein complex — mechanism, structure, existing inhibitors |
| `protein-design-script` | Generates RFdiffusion3 / BoltzGen run scripts from a hotspot spec |
| `chimerax-visualization` | Generates a ChimeraX `.cxc` script to visualise the interface: target in focus, binder washed out, hotspot patches highlighted |
| `orchestrator` | End-to-end multi-stage run: pathway → interface → design → optimization |
| `binder-target-intel` | Stage 0 of the binder-track pipeline: given a named target and design intent plus a pre-computed candidate-interface table, picks which structure/chain-pair/interface to design a binder against. Invoke only when the target is already named and no literature discovery is wanted |
| `design-analyst` | Terminal stage of the design pipeline: reviews a ranked top-K of computationally designed binders against the design intent and hotspots, flags methodological red flags, and issues a GO / CONDITIONAL_GO / NO_GO recommendation. Summarisation over a metrics table — no MCP tools, no generative work |

The `.zip` next to each skill directory is a packaged artifact for distribution — regenerate it after editing a `SKILL.md`, don't hand-edit the zip: `python scripts/package_skills.py`.

**Examples:**

```bash
# Pathway target selection
python scripts/run_skill.py \
    --skill pathway-expert \
    --query "Hippo pathway in mesothelioma — which node should we target?"

# PPI interface analysis (structure file required)
python scripts/run_skill.py \
    --skill complex-structure-analysis \
    --query "Analyse the interface between chain A (TEAD4) and chain B (YAP) in /tmp/7D9M.cif." \
    --output reports/ppi_analysis.md

# Binder optimization — pass prior PPI report as context
python scripts/run_skill.py \
    --skill binder-optimizer \
    --query "Suggest 4 mutations on chain B to improve affinity. Structure source is AF3." \
    --context reports/ppi_analysis.md \
    --output reports/mutations.md

# Use Claude instead of the default Gemini
python scripts/run_skill.py \
    --skill molecular-biology-expert \
    --query "What is known about the YAP-TEAD interaction interface and hotspot residues?" \
    --model claude

# Load a long query from a file
python scripts/run_skill.py --skill pathway-expert --query @queries/my_query.txt

# Full orchestrator run with a higher token budget
python scripts/run_skill.py \
    --skill orchestrator \
    --query "Full pipeline for YAP/TEAD4 in mesothelioma." \
    --max-tokens 150000
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--model` | `gemini` | Provider: `claude` or `gemini` |
| `--model-id` | provider default | Override model (e.g. `claude-opus-5`) |
| `--context` | — | Path to a prior report `.md` to include as context |
| `--output` | stdout | Write final report to this file |
| `--max-iter` | 30 | Max LLM calls per run |
| `--max-tokens` | 100,000 | Abort if any single call exceeds N input tokens |
| `--interactive`, `-i` | off | After the initial query (or with no `--query`), drop into a REPL for multi-turn follow-ups |
| `--trace` | — | Write a conversation trace (raw JSON + rendered Markdown) to this directory |

Default models: `gemini-3.7-flash` for Gemini (the pipeline-wide default provider — see
"Safety-classifier refusals" in CLAUDE.md for why), `claude-sonnet-5` for Claude.

Token usage is logged after every LLM call. If the per-call input token count exceeds `--max-tokens`, the run is aborted with a clear error showing cumulative usage.

### 6. Corpus explorer (conversational)

`corpus-explorer` is the skill to reach for when you want to *think out loud* against the corpus rather than produce a structured pipeline report. It uses inline DOI citations, three loose output modes (relational table, annotated cascade, ranked competing hypotheses), and a required "What the corpus does NOT say" closing. It does **not** emit a `PIPELINE HANDOFF` — output is for the human.

Beyond `search_corpus` and `get_fingerprint`, it has access to a set of corpus-aware tools that semantic search misses:

**Direct retrieval over the fingerprint set:**

- **`get_interactions_for(protein, depth)`** — walks `key_findings[].protein_pair` across every fingerprint and returns ranked partners with mention counts, supporting DOIs, and Kd/Ki anchors. Aliases are normalised (`YAP` matches `YAP1`/`hYAP`); paralogs stay distinct (`TEAD1` ≠ `TEAD2`, but `TEAD` matches all four).
- **`find_quantitative_evidence(protein_pair, metric)`** — pulls every `key_findings` entry with a measured `Kd` or `Ki` for a specific pair, sorted tightest-binder first.

**Graph queries** (NetworkX-backed undirected weighted graph, built once per MCP server lifetime from the same `protein_pair` data; ~0.5 s for 5k fingerprints):

- **`shortest_interaction_path(a, b, max_hops, k)`** — top-k shortest paths between two proteins. Each edge carries mention count, supporting DOIs, and tightest measured Kd/Ki. Returns `min_mentions_along_path` and `weak_links_count` so the model can flag low-confidence edges. Use for "is X connected to Y?" / "draw the cascade from X to Y".
- **`interaction_hubs(top_n, min_mentions)`** — highest-degree nodes after filtering single-paper edges. Caveat surfaced in every response: hub rank reflects literature attention, not biological importance.
- **`export_subgraph(seeds, output_path, depth)`** — writes a Cytoscape.js JSON neighbourhood (`.cyjs`) to disk for visual exploration. Each seed expands to all matching nodes (seed `'TEAD'` pulls in TEAD1/2/3/4). The graph goes to disk, not into the conversation.

The graph tools are gated to the four skills that reason over the corpus — `corpus-explorer`, `pathway-expert`, `molecular-biology-expert`, `wildcard-expert` (`skill_runner._GRAPH_TOOL_SKILLS`); design and structure-analysis skills do not see them. All five tools live in `src/_corpus_graph.py`.

**One-shot usage:**

```bash
# Relational query
python scripts/run_skill.py --skill corpus-explorer \
    --query "Which proteins interact directly with KRAS, and which interactions have measured Kd values?"

# Pathway / cascade
python scripts/run_skill.py --skill corpus-explorer \
    --query "Draw the LPA → LPAR1 signalling pathway with affinity values for each step."

# Hypothesis generation
python scripts/run_skill.py --skill corpus-explorer \
    --query "Knockout of NF1 leads to overactivation of the MAPK pathway. Generate competing hypotheses with discriminating experiments."

# Graph: shortest path
python scripts/run_skill.py --skill corpus-explorer \
    --query "What is the shortest interaction path from LPAR1 to YAP in the corpus? Flag any weak links."

# Graph: hubs
python scripts/run_skill.py --skill corpus-explorer \
    --query "Which proteins are the top hubs in the chromatin-modifier corpus? Filter to edges with at least 5 supporting papers."

# Graph: visual export
python scripts/run_skill.py --skill corpus-explorer \
    --query "Export a depth-2 subgraph around STING to data/graph_exports/sting.cyjs."
```

**Interactive (multi-turn) usage:**

```bash
# Start a REPL with no initial query
python scripts/run_skill.py --skill corpus-explorer -i

# Or: seed it with a first question and continue
python scripts/run_skill.py --skill corpus-explorer -i \
    --query "What's known about KRAS-RAF1 binding?" \
    --trace runs/kras_session
```

Inside the REPL:

| Command | Action |
|---|---|
| `<text>` | Send as a follow-up turn |
| `@path/to/file.txt` | Load a long query from a file |
| `/exit`, `/quit`, Ctrl+D / Ctrl+Z | Leave the loop |
| `/reset` | Clear conversation history (keeps the loaded skill + tools) |
| `/tokens` | Show cumulative + last-call input tokens |
| `/save <dir>` | Dump trace (raw JSON + rendered Markdown) to `<dir>` |

After each turn, if the most recent call's input tokens exceed 70 % of `--max-tokens`, you'll see an auto-warning suggesting `/reset` — that's the cue that the next follow-up will likely trip the per-call limit.

**Extending the corpus from a conversation:**

When the skill flags gaps in the "What the corpus does NOT say" closing, you can ask it directly:

> propose search keywords for those gaps

It responds with a YAML block in `config.yaml`'s NCBI Title/Abstract format, each keyword annotated with the gap it targets:

```yaml
keywords:
  # Gap: no Kd captured for KRAS / PIK3CG or KRAS / RALGDS effector binding
  - "KRAS[Title/Abstract] AND PIK3CG[Title/Abstract] AND binding[Title/Abstract]"
  - "KRAS[Title/Abstract] AND RALGDS[Title/Abstract] AND affinity[Title/Abstract]"
```

Save those into a fresh `config_search_expansion.yaml` (or any sibling config) and run the standard fetch → curate → ingest cycle:

```bash
python scripts/fetch_papers.py --config config_search_expansion.yaml
python scripts/curate_papers.py --limit 100
python scripts/ingest_vectors.py
```

Restart Claude Desktop to pick up the new fingerprints via MCP.

### 7. Run the binder design pipeline end-to-end

The design pipeline drives `PipelineRunner` (see `src/pipeline_runner.py`)
from a free-text prompt: pathway → literature → structure discovery, then the
design backend selected by `design.backend` / `--design-engine`. On the
default (`foundry`) it bridges into the binder track's RFD3 → solubleMPNN →
RF3 stages and **`--project` is required**; on `--design-engine boltzgen` it
continues into BoltzGen design/execution/analysis instead.

```bash
# Standard run (pathway-expert, validated-target-biased — picks YAP1/TEAD1-class
# targets with clinical precedent and a known PDB)
python scripts/run_pipeline.py --workflow ppi \
  --query "Design cancer therapeutics to target key nodes in mesothelioma." \
  --project mesothelioma

# Wildcard run (wildcard-expert, novelty-driven — graph + DepMap triage,
# prefers HYPOTHESIS / SYNTHETIC_LETHALITY / CROSS_INDICATION_TRANSFER /
# DEPMAP-COUPLED candidates over VALIDATED when a tractable novel target
# exists). Works on basic-biology prompts too:
python scripts/run_pipeline.py --workflow ppi \
  --query "Identify a novel tractable PPI in the unfolded protein response." \
  --project upr_wildcard \
  --pathway-mode wildcard

# The BoltzGen path, with a small pilot, and per-stage conversation traces
# captured for audit:
.venv/bin/python scripts/test_e2e.py \
  --prompt "Design cancer therapeutics to target key nodes in mesothelioma." \
  --slug mesothelioma \
  --pilot 50 --production 100
```

Configuration lives under `design:` in `config.yaml`, except for
machine-specific paths (BoltzGen, PyRosetta, foundry, cluster staging), which
are set via env vars instead — see
[Environment setup](docs/environment_setup.md) for the full list and why
they don't live in the tracked `config.yaml`:

- `design.workstation.boltzgen_executable` (or `LPT_BOLTZGEN_EXECUTABLE`) —
  absolute path to the BoltzGen entry point (we use the entry script's own
  shebang to invoke its conda/uv env, no `conda activate` needed).
- `design.workstation.cuda_device` / `timeout_hours` — GPU and time limits.
- `design.pilot` / `design.production` — `num_designs` + `budget` per phase. The
  pilot result gates the production run (raises `PipelinePausedError`
  `pilot_failed` if completion + final-fill rates fall below threshold).
- `design.thresholds` — hard filters in stage 5: `iptm_min`, `ipae_max`,
  `hotspot_sasa_delta_min`, `require_boltzgen_pass`.
- `design.ranking` — `enrich_top_k` (how many designs get pyrosetta SASA),
  composite `weights`, `mmr` diversity params, `top_k`.
- `design.constraints` — target-size limits (`max_target_residues`,
  `target_residues_warn`) and binder size ranges (`cyclic_peptide` 12..15,
  `mini_protein` 70..86 by default).
- `design.pyrosetta.python_executable` (or `LPT_PYROSETTA_PYTHON`) — absolute
  path to a conda env where PyRosetta imports cleanly (typically Python 3.11;
  see `docs/pyrosetta_setup.md`).

Run outputs land under `outputs/<slug>/`:

- `0X_<stage>.md` — markdown report from each LLM-driven stage.
- `traces/<stage>/{trace_raw.json, trace_rendered.md}` — full conversation
  history per LLM stage (only when `capture_traces=True`).
- `03_design_inputs/*.yaml` + `*_submit.sh` — BoltzGen design YAMLs.
  Stage 3 may emit multiple YAMLs (e.g. Region 1 + Region 2 for a wide
  interface); stage 4 currently executes only the alphabetically first.
- `04_execution_outputs/` — BoltzGen run dir (CIFs + `boltzgen.log` +
  `final_ranked_designs/all_designs_metrics.csv`). When stage 3 emitted
  multiple YAMLs, a `multi_region_skipped.txt` file lists the YAMLs that
  were generated but not run; the design-analyst surfaces this in
  `06_summary.md` so the human can manually run the unsampled regions.
- `05_metrics_enriched.csv` — every design with hotspot SASA appended.
- `05_ranking/{ranked.csv, top_k.csv, filter_stats.txt}` — filtered, ranked,
  MMR-diversified output.
- `06_summary.md` + `06_top_k.fasta` — analyst review + deterministic FASTA
  for ordering.

A first-time setup also needs the RCSB metadata cache (used by
`find_pdb_structures` to surface PDB titles + entity descriptions to the
LLM, catching corpus paper-level protein/PDB conflations):

```bash
python scripts/fetch_pdb_metadata.py   # writes data/pdb_metadata.json
```

The pipeline expects BoltzGen and PyRosetta envs already configured. For
BoltzGen, install per its README (`pip install boltzgen` or `uv pip install`)
and set `LPT_BOLTZGEN_EXECUTABLE` in `.env`. For PyRosetta, see
`docs/pyrosetta_setup.md` and set `LPT_PYROSETTA_PYTHON` — the LPT venv
itself does NOT need PyRosetta installed; the orchestrator subprocesses out
to a dedicated env via `scripts/_sasa_worker.py`. See
[Environment setup](docs/environment_setup.md) for the full list of
machine-specific env vars (foundry, cluster staging included) and why they
live in `.env` rather than the tracked `config.yaml`.

See `diary.md` for design notes, known failure modes, and the long-term
plan for a ground-truth PDB→protein lookup table.

#### Project directories

The binder workflow can write into a persistent project at `projects/<slug>/`
(`--project <slug>`), the source of truth for an iterative campaign:

```
projects/<slug>/
  manifest.json           # rounds, per-stage status + handoffs, open checkpoints
  shared/
    structures/  ligands/             # reusable assets
  runs/<round-N>/
    00_pathway.md ...                  # PPI stage files (binder workflow)
    scratch/                           # disposable intermediates
```

`src/project.py` manages it (atomic `manifest.json` writes). The binder pipeline
requires `--project` — the track iterates in rounds, and the manifest is what
makes a multi-day GPU campaign resumable.

### 7b. Run the binder pipeline from a target name

When the target is already decided, the literature-discovery stages are the wrong
tool. `--workflow binder` skips them: it resolves the name, picks an interface,
trims the target to fit the GPU, and runs a foundry campaign
(RFD3 → solubleMPNN → RF3) on the local workstation.

```bash
# Full run. --project is required: the track iterates in rounds, and the manifest
# is what makes a multi-day GPU campaign resumable.
python scripts/run_pipeline.py --workflow binder \
    --target TEAD1 \
    --query "design binders to TEAD1 to disrupt downstream interactions" \
    --project tead1-binders \
    --budget 5.00

# --target alone is enough; the query then just carries intent.
python scripts/run_pipeline.py --workflow binder --target KRAS --project kras

# Launch the GPU stages and return immediately (a production campaign runs for
# days). Resume with --start-from; every stage is skip-existing.
python scripts/run_pipeline.py --workflow binder --target KRAS --project kras --detach
python scripts/campaign_status.py projects/kras
python scripts/run_pipeline.py --workflow binder --target KRAS --project kras --start-from calibration

# A cheap smoke test: 8 designs, ~4 minutes of GPU.
python scripts/run_pipeline.py --workflow binder --target KRAS --project kras_smoke \
    --n-batches 2 --budget 2.00
```

### 7c. Run from a structure you already have

When you have the structure, both discovery questions are already answered —
there is no target to find and no entry to choose. `--workflow structure` skips
straight to hotspot analysis: it enumerates the chains, **measures** the largest
interface, writes a deterministic target-intel artifact (no LLM call), and enters
the same foundry stage machine at `interface`. One reasoning stage instead of
four; everything from `trim` onward is identical to a `--workflow binder`
campaign.

```bash
# Your own file — a construct, a prediction, anything gemmi can read. It is
# copied into data/structures/ as LOCAL-<name>.cif and addressed by that id.
python scripts/run_pipeline.py --workflow structure \
    --structure ~/models/my_complex.cif \
    --project my-complex --budget 3.00

# An RCSB entry, without paying for the target-intel stage to choose it.
python scripts/run_pipeline.py --workflow structure --pdb 7CZD --project pdl1

# Name the chains yourself instead of taking the measured default. One chain
# selects single-target mode (design_intent: inhibit_active_site).
python scripts/run_pipeline.py --workflow structure --structure my.cif \
    --project mine --chains B,A
```

**Name the epitope yourself with `--hotspots`.** By default the interface
stage picks the hotspots; `--hotspots B74,B83,B84` (or `74,83,84` for the
target chain) uses exactly those residues and makes **no LLM call** for that
stage:

```bash
python scripts/run_pipeline.py --workflow binder --target RING1B \
    --hotspots B50,B52,B54 --project ring1b
```

You supply only the numbers — in the structure file's author numbering, which
often differs from the canonical isoform's. Residue names, RFD3 sidechain atoms
and `label_seq_id`s are read from the structure, and every guard still runs, so
a number that is not in the chain is refused before anything is staged. At most
12 residues, and they should be one compact patch: a set spread across two
faces will fail the trim, correctly. Supported on the `binder` and `structure`
tracks.

**Pass `--uniprot` if you know the accession.** Three checks are keyed to
identity rather than geometry, and all three fail open without one:
`_verify_target_chain_assignment` (the guard that caught a campaign designed
against an anti-PD-L1 nanobody instead of PD-L1), the organism/ortholog warning,
and membrane topology — **without it, transmembrane residues are not stripped**,
and on a receptor that is how you get designs that bind a lipid-facing
hydrophobic slab. The run says so in `20_target_intel.md` either way. Hotspot
grounding is unaffected; it reads residue names straight from the coordinates.

Which chain is "the target" is an operator's choice, not a fact about the file:
either side of a two-chain complex is a legitimate thing to design against. The
default takes the larger chain of the largest measured interface — the
substantial surface rather than the peptide or nanobody usually on the other
side — and the report always states what it chose and how to swap it.

Stages:

```
  target_intel     binder-target-intel      → PDB + chain pair + interface, chosen from a
                   (+ src/target_resolve)     deterministically pre-computed candidate table
  interface        complex-structure-analysis → MODEL-READY HOTSPOTS (atom level)
  trim             src/structure_trim       → domain-aware crop to the GPU residue budget
  binder_spec      src/foundry_spec         → RFD3 spec + pre-flight validation
  pilot            src/foundry_runner       → small run; proves the spec works
  calibration      src/campaign_calibration → MEASURE the scale production needs
       ⏸ verdict   SCALE_UP / SCALE_UP_PARTIAL / ITERATE / STOP
  production       src/foundry_runner       → campaign sized by the calibration
  binder_scoring   src/binder_metrics       → ipSAE, dock RMSD, epitope recall, …
                   + src/binder_ranking       → filter, composite, diversity, top-K
  binder_summary   design-analyst           → final review + top_k.fasta
```

Only three stages call an LLM. Everything else is deterministic Python, which is
why a full run costs cents rather than dollars.

**Calibration, not guesswork.** A production campaign is a multi-day, ~100 GB
commitment, so the pipeline never scales straight to it: a ~300-backbone trial
*measures* the rate of designs clearing the bar, then extrapolates with a Wilson
interval and reports a SCALE_UP / SCALE_UP_PARTIAL / ITERATE / STOP verdict.
That verdict is always a pause point.

```bash
# Compare candidate epitopes instead of arguing about them — one trial each,
# winner picked on measured yield.
python scripts/run_pipeline.py --workflow binder --target CD79B \
  --project cd79b --trial-sites 3 --stop-after trial

# A cheap smoke test: 8 designs, ~4 minutes of GPU.
python scripts/run_pipeline.py --workflow binder --target KRAS \
  --project kras_smoke --trial-backbones 8 --stop-after trial
```

**Budget.** `--budget 5.00` is a hard cap on API spend, cumulative across
rounds; a stage whose projected cost would breach it pauses with a resumable
checkpoint rather than overrunning. GPU-hours and disk are governed separately
(`design.foundry.max_local_hours`, `disk_budget_gb`).

**Reports.** Every trial and every scored campaign writes a self-contained
illustrated `report.html` — site rationale, confidence distributions, an
embedded Mol* viewer over the top designs' actual refolds, and an appendix
carrying every stage report in full. No LLM, no GPU; regenerate any time with
`scripts/generate_binder_report.py`.

> **Why these defaults are what they are** — the sizing metric, the adaptive
> bar, the geometric gates, membrane-topology handling, the chain-assignment
> guards, safety-classifier refusal fallbacks, and the row-for-row scorer
> validation are all documented with their measurements in
> [CLAUDE.md](CLAUDE.md) ("Non-obvious facts the binder track depends on").
> Read that before changing a threshold.

### 7c. Scale a campaign onto a SLURM cluster

`--compute auto` (the default) makes the local-vs-cluster call **for you**, once,
at the calibration gate: `pilot` and `calibration` always run locally (they're
deliberately small), and once calibration measures how big `production` needs to
be, `src/campaign_calibration.choose_compute()` compares the pessimistic
single-GPU estimate against `--max-local-hours` (default 48h,
`design.foundry.max_local_hours` in `config.yaml`) and picks `local` or `cluster`
for `production` only. `--compute local` / `--compute cluster` still force every
GPU stage onto one path unconditionally, exactly as before `auto` existed — use
these to override the automatic call.

The cluster path itself: this machine cannot reach a SLURM scheduler directly, so
`src/cluster_runner.py` *stages* the campaign — spec, trimmed structure, a real
fetched target MSA — onto a `g-groups/.../binder_pipeline`-shaped checkout
(`design.cluster.pipeline_root` in `config.yaml`), writes a ready-to-run
`launch.sh`, and pauses. A human runs that script from the cluster; resuming
reads the results back off shared storage. It reuses that pipeline's own
SLURM/container machinery entirely (RFD3 → solubleMPNN → refold, one array task
per GPU) rather than reimplementing it.

```bash
# Default: let the pipeline decide. Runs pilot + calibration locally; if the
# measured production estimate exceeds 48h on this GPU, it stages a cluster
# package for production and pauses with submit instructions instead of
# running unattended for days. Otherwise production just runs locally.
python scripts/run_pipeline.py --workflow binder \
    --target KRAS --project kras --budget 5.00

# Same, but with a tighter local budget (e.g. only free for the weekend) and
# a known 8-GPU allocation for the cluster estimate:
python scripts/run_pipeline.py --workflow binder \
    --target KRAS --project kras --max-local-hours 24 --n-gpus 8

# Force everything onto the cluster regardless of size — e.g. to stage a
# calibration-scale run across 6 GPUs. Pauses immediately with a
# launch_script path and submit instructions — this machine cannot run it.
python scripts/run_pipeline.py --workflow binder \
    --target KRAS --project kras --start-from calibration \
    --compute cluster --n-gpus 6 --stop-after calibration

# ... on the cluster login node: ...
#   bash examples/<run_name>/launch.sh

# Resume once the SLURM jobs finish — reads results off shared storage,
# scores with LPT's own full metrics (ipSAE, epitope recall, hotspot
# engagement — not the cluster pipeline's own narrower scorer), writes the
# same calibration verdict a local run would. --compute cluster here forces
# the SAME path the campaign was staged on; a resumed --compute auto run
# instead re-reads the compute decision calibration.json already made.
python scripts/run_pipeline.py --workflow binder \
    --target KRAS --project kras --start-from calibration --compute cluster

# A multi-site trial's per-site data has no CLI resume path yet (see
# CLAUDE.md) — use this script directly instead, with the exact n_batches
# the campaign was staged with:
python scripts/resume_cluster_calibration.py --project kras \
    --site raf1_rbd --n-batches 591 --n-gpus 6
```

> **Cluster mechanics worth knowing before you stage one** — the refold
> backend and its MSA source, why `NB` is designs *per GPU array task* rather
> than a campaign total (a misreading of which once undercounted a campaign by
> 6x), the per-design output layout, and the GPU hardware faults that occur at
> this scale and look like bugs: [CLAUDE.md](CLAUDE.md), "Non-obvious facts the
> cluster compute path depends on".


### 8. Visualise top-K designs in PyMOL

`scripts/pymol_show_topk.py` loads the top-K binders from any run's
`05_ranking/top_k.csv`, superimposes them on a single target frame, highlights
the per-design hotspots, and overlays the native complex so you can eyeball
whether the designs hit the right interface. Styling follows the lab's
`pymol_functions_cycle.py` (black bg, gold target, ambient occlusion).

It is **run-agnostic** — nothing is hardcoded. The target CIF + chain and the
hotspot residue list are read from each design's BoltzGen YAML in
`03_design_inputs/`, so it works on any future campaign whose YAMLs follow the
standard schema.

```bash
pymol
```
```
topk                                  # ~/.pymolrc alias: loads the script
show_topk outputs/e2e_mesothelioma    # path relative to cwd, or absolute
show_topk <run>, 5                    # top-5 instead of the default 10
show_topk <run>, 10, A, B             # explicit target / binder chain IDs
```

`show_topk` defaults the run dir to the current working directory and K to 10.
Once the script is loaded, `show_topk` stays registered for the session, so you
can point it at another run without re-running `topk`.

Controls (bound on load):

- **F1 / F2** — previous / next design. Only the target + native + current
  binder are shown; the on-target hotspot sticks/labels update per design.
- **F3** — overlay all K binders at once with the union of hotspots.
- `toggle_native` — hide / show the native-complex overlay (purple partner
  chain; its target chain is hidden to avoid a duplicate cartoon).

Pre-made selections: `hotspots`, `binder_interface`, `target_chain`,
`native_complex`.

> Numbering note: BoltzGen output CIFs carry the input `label_seq_id` as their
> `auth_seq_id`, which is exactly what the YAML `binding:` field lists — so the
> hotspot selection lines up without any remapping (see the 2026-05-23 diary
> entry on the chain/numbering convention).

---

## Inspecting the database

`data/literature.db` is plain SQLite if you want to look at corpus state
directly — see **[docs/database.md](docs/database.md)** for ready-made
queries. For "is my corpus healthy", `python scripts/doctor.py` answers
that without any SQL.

## Use it from Claude Desktop / Claude Code

Two MCP servers expose **23 tools** — 15 over the corpus (semantic search,
fingerprints, the interaction graph, clusters, DepMap co-essentiality, PDB
discovery) and 8 structure calculations that need no corpus and no API key.
Driven by your Claude subscription rather than an API key, this is the
cheapest way to use LPT and often the better one for exploratory work.

```bash
python scripts/setup_mcp_json.py     # writes .mcp.json for this checkout
```

Claude Code picks it up from the project root. Claude Desktop needs the
printed block copied into its config and **a restart**. Full setup, the
complete tool list, and troubleshooting:
**[docs/mcp.md](docs/mcp.md)**.

## Project structure

```
little_protein_tiger/
├── config.yaml                  # Main config: keywords, limits, paths, curation settings
├── curation_prompt.md           # System prompt for Claude curation
├── extraction_schema.json       # Target JSON schema for fingerprints (v2.0)
├── pyproject.toml               # Single source of truth for dependencies (pip install -e .)
├── .env.example
│                                # (.mcp.json is generated per checkout, not tracked —
│                                #  see scripts/setup_mcp_json.py)
│
├── src/
│   ├── pipeline_runner.py       # The stage machine: both ppi and binder tracks
│   ├── models.py                # Pydantic Paper + CurationStatus models
│   ├── database.py              # SQLite wrapper (upsert, status tracking)
│   ├── search.py                # Europe PMC + NCBI search clients
│   ├── downloader.py            # PDF/XML fetcher with retry logic
│   ├── ranking.py               # Paper scoring/prioritisation
│   ├── text_extractor.py        # PDF (pymupdf) + XML (lxml) text extraction
│   ├── curator.py               # Claude/Gemini API curation, Pydantic validation
│   ├── fingerprint_store.py     # Save/load fingerprint JSONs
│   ├── vector_store.py          # LanceDB wrapper + PubMedBERT embeddings
│   ├── mcp_server.py            # FastMCP: search_corpus + get_fingerprint (MCP)
│   ├── structure_tools.py       # Pure-Python interface analysis (BSA, contacts, SASA)
│   ├── structure_tools_server.py# FastMCP wrapper for structure_tools (MCP)
│   └── skill_runner.py          # Agentic loop: loads SKILL.md, calls Claude/Gemini API
│
├── scripts/                     # (not exhaustive — run any with --help)
│   ├── run_pipeline.py          # CLI: THE entry point — ppi and binder tracks
│   ├── setup.sh                 # Bootstrap a fresh checkout
│   ├── doctor.py                # Per-track readiness report + the fix for each gap
│   ├── quickstart.py            # A real analysis in ~7 s, no key, no GPU
│   ├── fetch_corpus.py          # Install the pre-built literature corpus
│   ├── fetch_reference_data.py  # UniProt id-mapping + HGNC (ppi/binder need it)
│   ├── setup_mcp_json.py        # Generate .mcp.json for this checkout
│   ├── fetch_papers.py          # CLI: search + download
│   ├── curate_papers.py         # CLI: Claude curation pipeline
│   ├── ingest_vectors.py        # CLI: embed fingerprints into LanceDB
│   ├── ask_corpus.py            # CLI: interactive conversational search
│   ├── launch_mcp.py            # Launcher for literature-db MCP server
│   ├── launch_structure_tools.py# Launcher for structure-tools MCP server
│   ├── run_skill.py             # CLI: run any expert skill via Claude/Gemini API
│   ├── test_e2e.py              # PPI-track driver with per-stage trace capture
│   ├── compare_providers.py     # Sandboxed claude/gemini/local provider bake-off
│   ├── score_provider_compare.py# Scorecard + REPORT.md across providers
│   └── pymol_show_topk.py       # PyMOL viewer for top-K designs (run inside PyMOL)
│
├── skills/                      # Expert skill definitions (SKILL.md = system prompt)
│   ├── pathway-expert/          # Disease pathway analysis + PPI target selection (validated-target-biased)
│   ├── wildcard-expert/         # Speculative counterpart: graph + DepMap-driven novelty triage
│   ├── complex-structure-analysis/ # PDB/CIF interface hotspot analysis
│   ├── binder-optimizer/        # Point mutation proposals + AF3 JSON generation
│   ├── molecular-biology-expert/# Corpus search for a specific protein pair
│   ├── complex-expert/          # Corpus search for a named complex
│   ├── protein-design-script/   # RFdiffusion3 / BoltzGen script generation
│   ├── chimerax-visualization/  # ChimeraX .cxc script for interface figures
│   ├── corpus-explorer/         # Conversational free-form corpus exploration
│   ├── binder-target-intel/     # Binder track stage 0: pick structure + interface
│   ├── design-analyst/          # Terminal stage: review the ranked top-K, GO/NO_GO
│   └── orchestrator/            # End-to-end multi-stage pipeline
│
└── data/                        # Gitignored
    ├── literature.db            # SQLite paper metadata + status tracking
    ├── fingerprints/            # One JSON fingerprint per curated paper
    ├── vectors/                 # LanceDB vector store
    └── pdfs/                    # Downloaded PDF/XML files
```

---

## Migrating to a new machine

`data/` and `.mcp.json` are gitignored, so a fresh clone has neither the
corpus nor its MCP registration. The short version: clone, install, run
`python scripts/fetch_corpus.py`, run `python scripts/setup_mcp_json.py`,
then `python scripts/doctor.py` to confirm.

Full walkthrough, including the GPU tools and what is safe to copy versus
re-fetch: **[docs/migration.md](docs/migration.md)**.

## Journal filtering — read this before building a corpus

`fetch_papers.py` only downloads papers from a tier-1/tier-2 journal list.
Search indexes every hit; the gate decides what gets fetched and curated. A
journal spelled a way the list does not contain is a **silent** 100%
exclusion, not a warning — so if you are building your own corpus in a field
this list was not written for, read
**[docs/journal-filtering.md](docs/journal-filtering.md)** first. It has the
measured pass rates, the full excluded list, and how to widen or disable the
gate.

```yaml
# config.yaml
quality:
  require_tiered_journal: true    # the default
  tier2_extra: []                 # add journals here rather than disabling
```

### After adding new search terms

```bash
python scripts/fetch_papers.py --config config_my_topic.yaml   # 1. search + download
python scripts/curate_papers.py --limit 200                    # 2. curate (self-runs 3-4)
```

Curation self-runs identifier normalisation, the graph rebuild and vector
ingest, so `search_corpus` sees new papers with no further step. Restart
Claude Desktop if you are using MCP.

## Responsible use

Little Protein Tiger designs de novo protein binders. Every design it emits is
an **unvalidated computational hypothesis** — a sequence and a predicted pose,
not a working binder. If you synthesize anything derived from it, screening the
sequence is your responsibility; use a synthesis provider that screens orders
(see the [IGSC Harmonized Screening Protocol](https://genesynthesisconsortium.org/)).

Read **[docs/responsible-use.md](docs/responsible-use.md)** before using the
design tracks. It covers intended use, what the confidence metrics do and do
not tell you, biosecurity expectations, and this project's position on safety
classifiers (short version: model fallback is fine, prompt-engineering around a
safety check is not).

## Licence and third-party tools

LPT is licensed under [PolyForm Noncommercial
1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) (see
[`LICENSE`](LICENSE)) and comes with no warranty. **Free for academic,
nonprofit and personal research — including on industry-funded grants, which
the licence covers explicitly. Commercial use needs a commercial licence**;
contact the copyright holder. This is the same arrangement PyRosetta uses.

Three separate sets of terms apply to three separate things — the code, the
corpus, and the tools LPT drives. [`docs/licensing.md`](docs/licensing.md) is
the full picture, including what the corpus archive does and does not contain.

**LPT's own licence covers this repository only.** LPT orchestrates external
models and tools that it does not ship and grants no rights to. Some are free
for academic use but **restricted for commercial use** — check each one against
your own use case before relying on it. The few third-party files LPT *does*
redistribute keep their own licences, reproduced in
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md):

| Tool | Needed for | Licence — check before commercial use |
|---|---|---|
| [RFdiffusion3 / solubleMPNN / RF3 (foundry)](https://github.com/RosettaCommons/foundry) | `--workflow binder`, and `--workflow ppi` by default | **BSD 3-Clause** (repository, verified 2026-08-27). Model weights are a separate download through foundry's own checkpoint registry (`~/pip_rcfoundry_ckpt` by default; point LPT elsewhere with `LPT_FOUNDRY_CKPT_DIR`) — confirm their terms yourself |
| [BoltzGen](https://github.com/HannesStark/boltzgen) | `--workflow ppi --design-engine boltzgen`; also `--modality cyclic_peptide` | **MIT** (repository, verified 2026-08-27). Model weights download separately — confirm their terms yourself |
| PyRosetta (**optional**) | hotspot SASA, Rosetta composite terms — see below | **free for academic / non-commercial only**; commercial licence via `license@uw.edu` — see [docs/pyrosetta_setup.md](docs/pyrosetta_setup.md) |
| Protenix | cluster refold backend (optional) | see upstream repository |
| ChimeraX / PyMOL | optional visualisation | separate licences |

**PyRosetta is optional.** It is used in exactly two places, both *after*
designs already exist — per-design hotspot SASA in the PPI track's analysis
stage, and relax + InterfaceAnalyzer on gate survivors in the binder track's
scoring stage. Nothing generative depends on it. With the default
`design.pyrosetta.enabled: auto`, a machine without it runs both tracks
end-to-end, skips those metrics, does **not** apply the hotspot-SASA filter,
and says so in the stage report. Set `enabled: true` to require it (fail
loudly instead) or `false` to never use it.

**foundry is not optional** for the binder track — see
[docs/environment_setup.md](docs/environment_setup.md#what-each-tool-actually-is-and-how-to-get-one) for
install notes, GPU/disk requirements, and the checkpoint-registry gotcha.

Bundled third-party material — see
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) for the full texts:

- **Mol\*** (`assets/vendor/molstar/`) — MIT, vendored so generated reports have
  no runtime network dependency. Licence and pinned version in
  `assets/vendor/molstar/`.
- **RFdiffusion3 documentation** (`skills/protein-design-script/RFD3_*.md`) —
  BSD 3-Clause, © 2025 Institute for Protein Design, University of Washington.
  Vendored because the `protein-design-script` skill reads them as in-context
  reference and they pin the contig/spec format `src/foundry_spec.py` validates
  against. Neither the IPD, the University of Washington, nor the foundry
  contributors endorse LPT.
- **BoltzGen documentation and example spec**
  (`skills/protein-design-script/boltzgen_*`) — MIT, © 2025 Hannes Stärk.

Python dependencies are MIT/BSD/Apache, with one to be aware of: **PyMuPDF is
AGPL-3.0-or-later**. It is imported at runtime by `src/text_extractor.py` for
PDF parsing and is not linked into anything distributed here, but if you
redistribute a derivative that bundles it, read its terms.

---

## Further reading

| Doc | For |
|---|---|
| **[docs/beta-testing.md](docs/beta-testing.md)** | Clean machine → first design run → first literature query. Start here. |
| **[SETUP_AGENT.md](SETUP_AGENT.md)** | Setup script for a coding agent to execute |
| [docs/environment_setup.md](docs/environment_setup.md) | Env vars, and the external GPU tools (foundry, BoltzGen, PyRosetta) |
| [docs/mcp.md](docs/mcp.md) | Claude Desktop / Claude Code registration, the 23-tool menu |
| [docs/migration.md](docs/migration.md) | Moving an install to another machine |
| [docs/database.md](docs/database.md) | SQL for inspecting corpus state |
| [docs/journal-filtering.md](docs/journal-filtering.md) | The tier gate — read before building your own corpus |
| [docs/pyrosetta_setup.md](docs/pyrosetta_setup.md) | PyRosetta's Python-ABI trap, and its licence |
| [docs/licensing.md](docs/licensing.md) | Code, corpus and third-party terms — and what the corpus archive actually contains |
| [docs/responsible-use.md](docs/responsible-use.md) | Scope and limits |
| [the showcase site](https://michauckelmann.github.io/little-protein-tiger/showcase/) | Illustrated walkthroughs of real runs — source in [docs/showcase/](docs/showcase/) |
| [CLAUDE.md](CLAUDE.md) | Architecture, and every non-obvious fact with the measurement behind it. Read before changing a threshold. |
| [diary.md](diary.md) | Development log — what was tried, what failed, why |
