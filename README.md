> ### ⚠️ Not yet public-ready
>
> **The literature corpus has not been packaged and released yet.**
> `scripts/fetch_corpus.py` — which the setup docs tell users to run — will
> find nothing until it is. See **[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)**
> before flipping this repository to public.

# Little Protein Tiger

An end-to-end pipeline for PPI drug target discovery. Covers automated paper discovery, Claude-powered structured extraction, a vector database for semantic search, and a suite of AI expert skills for pathway analysis, structural interface analysis, and binder design. Skills run either inside Claude Desktop (via MCP) or from the CLI using the Claude or Gemini API directly.

**Current corpus state (2026-04-01):** ~9,865 papers indexed · 1,944 downloaded · 988 curated fingerprints

---


> **Setting up with a coding agent?** Paste
> **[SETUP_AGENT.md](SETUP_AGENT.md)** into Claude Code from a fresh clone and
> it will interview you, install only the tracks you need, and verify each step.
>
> **Just want to see it work?** `python scripts/quickstart.py` — about 7
> seconds, no API key, no GPU, no corpus.
>
> **Want the literature corpus?** `python scripts/fetch_corpus.py` — ~83 MB,
> free, about a minute. 10,981 curated papers, ready to search. You only pay
> if you later extend it with your own search terms.
>
> **Something not working?** `python scripts/doctor.py` reports readiness per
> track and prints the command that fixes each problem.

## Requirements

- Python 3.12+
- Virtual environment (`.venv` recommended)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS
pip install -e .              # core dependencies
```

Or run `scripts/setup.sh` to do all of the above (venv creation, dependency
install, `.env` bootstrap, PDB metadata cache fetch, and a GPU-tool
diagnostic) in one step — see [Bootstrap script](#bootstrap-script) below.

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

`.env` keys:
| Key | Required for |
|-----|---|
| `ANTHROPIC_API_KEY` | Curation, CLI skills with `--model claude` |
| `GEMINI_API_KEY` | CLI skills with `--model gemini` |
| `NCBI_EMAIL` | Polite crawling (NCBI rate limits) |
| `NCBI_API_KEY` | Higher NCBI rate limit (optional) |

---

## Bootstrap script

`scripts/setup.sh` automates the manual steps above for a fresh checkout:
creates `.venv` if it doesn't exist, installs the project with dev extras
(`pip install -e ".[dev]"`), copies `.env.example` to `.env` (without
overwriting an existing one), fetches the RCSB PDB metadata cache, and
prints a diagnostic of which external GPU tools (BoltzGen, PyRosetta,
foundry) are configured and actually found on this machine.

```bash
./scripts/setup.sh
```

It's optional — everything it does is also documented step-by-step in this
README — but it's the fastest way to get a new checkout usable. GPU tools
still need manual installation per their own setup docs; the script only
reports what it finds, it never fails because one is missing.

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
  provider: "claude"           # "claude" | "gemini" | "local"
  model: "claude-haiku-4-5"

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
curate_papers.py         Claude extracts structured fingerprint JSONs
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
  stage 3  protein-design-script      → BoltzGen YAML + RFD3 JSON
  stage 4  design_runner              → BoltzGen pilot → gate → production
                                          (workstation GPU subprocess)
  stage 5  design_metrics + ranking   → enrich top-K with pyrosetta hotspot
                                          SASA, MMR-rank by composite score
  stage 6  design-analyst             → final candidate review + FASTA
```

Stage 0 has two modes (`design.pathway.mode` in `config.yaml` or
`--pathway-mode` CLI flag): `standard` runs `pathway-expert` (tier-ranked
on validated drug targets — VT3989-style precedent) and `wildcard` runs
`wildcard-expert` (graph-driven novelty triage using `interaction_hubs`,
`novelty_signal`, and DepMap `get_genetic_codependency` / `find_cocorrelated_genes`
to surface mechanistically connected but literature-under-explored
candidates). Wildcard Phase 2.5 runs codep against the **top-3 hubs**
rather than a single anchor (catches candidates that sit outside the
named driver's module) and a per-candidate **candidate-edge DepMap
sweep** for the top-2 picks — `find_cocorrelated_genes(candidate)`
cross-referenced against `get_interactions_for(candidate)` — to surface
the highest-yield finding: a novel pick strongly codependent (r ≥ 0.4)
with a corpus hub the literature has not yet linked it to. The wildcard
branch supports both disease-anchored and basic-biology contexts and
emits forward-compatible `predicted_consequence` / `falsifying_readout`
fields on each candidate so a future probe-mode design-analyst can use
the designed binder as a research probe, not just a therapeutic.

Stages 0–3 and 6 are LLM-driven (skills); stages 4 and 5 are deterministic
Python. The orchestrator handles `auth_seq_id ↔ label_seq_id` numbering,
chain identity (resolved deterministically from the mmCIF in stage 2; no
upstream skill emits chain letters), PDB-identity sanity checks (catches
paper-level protein/PDB conflations in the corpus), modality reconciliation
between mol-bio and structure stages, and BoltzGen's output renumbering
quirks. See `src/pipeline_runner.py` for the full state machine and
`diary.md` for the design notes and known failure modes.

End-to-end driver: `scripts/test_e2e.py --prompt "..." --slug runname`.
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

# Dry run — search only, no downloads, no DB writes
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

### 4. Interactive corpus search

Query the corpus in a conversational loop using Claude + semantic search.

```bash
python scripts/ask_corpus.py

# Options
python scripts/ask_corpus.py --model claude-sonnet-5 --top-k 10
```

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
| `protein-design-script` | Generates RFDiffusion / BoltzDesign run scripts from a hotspot spec |
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

The graph tools are gated to `corpus-explorer` and `pathway-expert` only; design and structure-analysis skills don't see them. All five tools live in `src/_corpus_graph.py`.

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

The 7-stage design pipeline drives `PipelineRunner` (see `src/pipeline_runner.py`)
from a free-text prompt. Stages 0–3 + 6 are LLM-driven skills; stage 4
runs BoltzGen on the local GPU; stage 5 enriches the top-K with hotspot
SASA via PyRosetta and ranks. The wrapper script in `scripts/test_e2e.py`
takes a prompt + slug and captures per-stage conversation traces for audit.

```bash
# Standard run (pathway-expert, validated-target-biased — picks YAP1/TEAD1-class
# targets with clinical precedent and a known PDB)
.venv/bin/python scripts/test_e2e.py \
  --prompt "Design cancer therapeutics to target key nodes in mesothelioma." \
  --slug mesothelioma \
  --pilot 50 --production 100

# Wildcard run (wildcard-expert, novelty-driven — graph + DepMap triage,
# prefers HYPOTHESIS / SYNTHETIC_LETHALITY / CROSS_INDICATION_TRANSFER /
# DEPMAP-COUPLED candidates over VALIDATED when a tractable novel target
# exists). Works on basic-biology prompts too:
.venv/bin/python scripts/test_e2e.py \
  --prompt "Identify a novel tractable PPI in the unfolded protein response." \
  --slug upr_wildcard \
  --pilot 50 --production 100 \
  --pathway-mode wildcard
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
python scripts/run_pipeline.py --workflow binder --project kras --start-from calibration

# A cheap smoke test: 8 designs, ~4 minutes of GPU.
python scripts/run_pipeline.py --workflow binder --target KRAS --project kras_smoke \
    --n-batches 2 --budget 2.00
```

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
commitment. The calibration stage refolds ~300 backbones × 4 sequences, measures
how many designs clear the success bar, and extrapolates with a Wilson interval
(a rule-of-three bound when there are no hits). It reports the required scale as a
*range* and sizes cost against the pessimistic end. Backbones, not refolds, are the
sampling unit — the sequences sharing one RFD3 backbone are correlated.

Campaigns are **sized on iPTM and ranked on ipSAE**. Per 1000 backbones the
reference campaigns produced 13.2 / 2.0 designs at `iPTM > 0.7` versus 2.5 / 0.1
at `ipsae_min > 0.5`; the latter is too rare for a trial-sized sample to measure,
so every verdict would be "enlarge the sample". Both are computed on every design
and `ipsae_min` carries the heaviest ranking weight. The geometric gates are never
optional — of designs with `iPTM > 0.7`, only 45 % (8TAC) and 8 % (CD79b) are
actually docked on target.

A 300-backbone trial is often too small: at the 8TAC rate it gave a usable rate
estimate in 0/10 random seeds, and 1000 backbones in 8/10. `--escalate-to 1000`
(on by default) re-runs a trial that came back unmeasurable.

**Adaptive bar.** `design.binder_ranking.adaptive_bar` (on by default) raises the
sizing bar for a target that turns out unusually good, instead of sizing every
campaign to the same fixed default. It walks the same bar ladder `suggested_bar`
is drawn from — strictest first — and takes the strictest rung that both has
enough hits for a real Wilson estimate and still fits the budget at its
pessimistic bound; on real KRAS/RAF1 data it raised the iptm bar from 0.7 to
0.85 while staying comfortably SCALE_UP. It only ever raises the bar, never
lowers it below what was requested, and it reuses the same interval math the
base bar is already sized with rather than a hand-tuned multiplier table.

**Comparing epitopes.** `--trial-sites N` runs a separate trial per candidate site
the target-intel stage proposes and picks the winner on measured yield rather than
argument — reasoning cannot settle which of two defensible epitopes is more
designable, but a few hundred backbones can.

**Membrane proteins.** Topology comes from UniProt and is mapped into the
structure's author numbering. Design defaults to the extracellular side, and
transmembrane residues are excluded whichever side you pick: in an isolated
structure a TM helix is an exposed hydrophobic slab that preferentially attracts
binders which cannot work in a cell, where that surface is buried in lipid.

**Rosetta.** `design.binder_ranking.rosetta` scores gate survivors only (capped at
300) and contributes to the final composite, never to the gate — Rosetta cannot
tell a real complex from a confidently wrong one, so a mis-docked binder still
returns a well-defined, meaningless ddG.

**Refusals.** The interface (and sometimes target_intel) stage is routinely
declined by Claude's safety classifier with category `bio` — this is exactly why
`--provider`/`--model` defaults to **gemini** (`gemini-3.7-flash`) for every
pipeline stage now, not claude. `models.claude.refusal_fallbacks` is a chain and
crosses providers on the FIRST refusal — Gemini goes first, not another Claude
model: measured across three separate refusals on one target, `claude-opus-5`
refused right after `claude-sonnet-5` every single time (same category), which
is pure wasted spend, not a second chance. `models.gemini.refusal_fallbacks`
covers the (rarer) case Gemini itself declines, falling through to Claude.

**Correctness guards.** Two checks run after every interface-stage call, since
a wrong answer here wastes days of GPU time downstream, not just tokens:
`_verify_hotspot_grounding` confirms each hotspot's stated residue actually
exists at that position in the real structure (catches literature/textbook
numbering reported for the wrong deposited structure); `_verify_target_chain_assignment`
confirms `target_chain` is genuinely the target protein by aligning its modelled
sequence against UniProt, not the partner (catches a target/partner swap, which
grounding cannot — the residues are real, just on the wrong molecule). Both were
written after live trials hit each failure mode for real.

**Scoring.** Ported from the reference campaign scorers and validated **row-for-row
against two complete campaigns** (CD79b: 28,420 refolds; 8TAC: 32,000 — zero
differences across 25 columns), plus a fresh ipSAE implementation checked against
[DunbrackLab/IPSAE](https://github.com/DunbrackLab/IPSAE). Note that RF3 templating
cannot convey a docked pose, so iPTM and ipSAE are confidence in *whatever*
interface the model chose: `binder_rmsd_dock` and `epitope_recall` are what
actually separate on-target designs.

**Reports.** Every trial and every scored campaign gets an illustrated,
self-contained `report.html` — structure/site selection rationale, hotspot
rationale, confidence-metric charts, and an interactive [Mol*](https://molstar.org)
viewer over the top-ranked designs' actual refolded structures, so a human can
visually confirm a design landed on the intended epitope rather than trusting
ipTM alone. It is generated automatically as a side effect (never a gate — a
report bug cannot fail a campaign) and can be regenerated any time with
`scripts/generate_binder_report.py --project <slug> --round round-1 [--site <id>]`.
No LLM and no GPU: it reads the same structured files the pipeline already
writes and renders the target-intel/interface stages' own markdown prose
verbatim for the narrative sections, rather than inventing new copy.

**Budget.** `--budget 5.00` is a hard cap on API spend, cumulative across rounds.
A stage whose projected cost would exceed it pauses with a resumable checkpoint
instead of starting. It governs API cost only — GPU time is limited separately by
the disk budget in `design.foundry`.

**Optional tools.** `design.trim.chainsaw_cmd` and `design.trim.foldseek_bin` improve
domain segmentation and add a post-trim fold check. Both are optional: without them
trimming falls back to RCSB CATH/SCOP2/ECOD annotations and a contact-graph
partition. Foldseek is a structural *search* tool and cannot parse domains — it is
not the domain parser here.

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

**Refold backend.** `design.cluster.refold_backend` defaults to `protenix`, which
co-folds the target de novo from sequence — unlike RF3, it has no template
support at all, so `design.cluster.use_msa: true` (also the default) is not an
accuracy nice-to-have: an un-MSA'd target refolds ~11 Å wrong on the reference
campaign (vs. RF3's 0.4 Å with a template), which lands straight in
`binder_rmsd_bb`. The MSA itself is fetched locally and for free via a separate
Protenix checkout's own hosted MMseqs2 search (`design.cluster.protenix_repo`,
subprocessed like every other foreign-venv tool in this codebase — PyRosetta,
BoltzGen, foundry) and cached forever by sequence hash, not searched on the
cluster.

**`NB` is per GPU, not a total.** `design.cluster.n_gpus` × the sizing math's
own batch count both matter: the cluster pipeline's own `n_batches` knob runs
independently on every one of the `n_gpus` array tasks, so a campaign sized for
2,000 backbones on 6 GPUs actually produces designs from all 6 × that count.
`src/cluster_runner.plan_campaign` accounts for this; see CLAUDE.md's cluster
section for the full story (this was wrong once, silently, against a real
campaign).

**Hardware faults are a real operational fact at this scale.** A single GPU
array task or refold shard can die to a node-level CUDA ECC error uncorrelated
with anything in the staged inputs. `scripts/campaign_status.py`-style progress
checks won't distinguish "still running" from "one shard failed and the rest
finished a day ago" — check the SLURM logs under the campaign's `logs/` for
`CUDA error` when a run looks stalled.

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

## Database queries

Quick inspection commands using Python's sqlite3:

```python
import sqlite3
conn = sqlite3.connect('data/literature.db')

# --- Status summary ---
for r in conn.execute("SELECT download_status, COUNT(*) FROM papers GROUP BY download_status"):
    print(r)

for r in conn.execute("SELECT curation_status, COUNT(*) FROM papers GROUP BY curation_status"):
    print(r)

# --- Total counts ---
conn.execute("SELECT COUNT(*) FROM papers").fetchone()
conn.execute("SELECT COUNT(*) FROM papers WHERE download_status = 'downloaded'").fetchone()
conn.execute("SELECT COUNT(*) FROM papers WHERE curation_status = 'completed'").fetchone()

# --- Downloaded but not yet curated (ready to process) ---
conn.execute("""
    SELECT COUNT(*) FROM papers
    WHERE download_status = 'downloaded' AND curation_status != 'completed'
""").fetchone()

# --- Papers by source ---
for r in conn.execute("SELECT source, COUNT(*) FROM papers GROUP BY source"):
    print(r)

# --- Recent papers ---
for r in conn.execute("""
    SELECT title, journal, year, curation_status
    FROM papers ORDER BY year DESC LIMIT 10
"""):
    print(r)

# --- Browse fingerprints by keyword ---
for r in conn.execute("""
    SELECT title, fingerprint_path FROM papers
    WHERE curation_status = 'completed' AND title LIKE '%SYS1%'
"""):
    print(r)

# --- Curation failures ---
for r in conn.execute("""
    SELECT paper_key, curation_error FROM papers
    WHERE curation_status = 'failed'
"""):
    print(r)
```

Or use the sqlite3 CLI directly:

```bash
sqlite3 data/literature.db "SELECT curation_status, COUNT(*) FROM papers GROUP BY curation_status"
```

---

## MCP server (Claude Desktop integration)

The MCP server exposes two tools to LLM agents:

| Tool | Description |
|------|-------------|
| `search_corpus` | Semantic search over curated fingerprints |
| `get_fingerprint` | Retrieve full fingerprint JSON by DOI or paper key |

The server is launched via `scripts/launch_mcp.py`, which auto-detects the venv and sets all data paths relative to the project root. Registration lives in `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/.config/Claude/claude_desktop_config.json` (macOS/Linux). Restart Claude Desktop after any config change.

To debug connection issues, check `data/mcp_server.log`. Known fix for sentence_transformers import hang on Windows: `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, and `TOKENIZERS_PARALLELISM=false` — these are set automatically by the launcher.

---

## Project structure

```
little_protein_tiger/
├── config.yaml                  # Main config: keywords, limits, paths, curation settings
├── curation_prompt.md           # System prompt for Claude curation
├── extraction_schema.json       # Target JSON schema for fingerprints (v2.0)
├── pyproject.toml               # Single source of truth for dependencies (pip install -e .)
├── .env.example
├── .mcp.json                    # MCP server registration for Claude Code
│
├── src/
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
├── scripts/
│   ├── fetch_papers.py          # CLI: search + download
│   ├── curate_papers.py         # CLI: Claude curation pipeline
│   ├── ingest_vectors.py        # CLI: embed fingerprints into LanceDB
│   ├── ask_corpus.py            # CLI: interactive conversational search
│   ├── launch_mcp.py            # Launcher for literature-db MCP server
│   ├── launch_structure_tools.py# Launcher for structure-tools MCP server
│   ├── run_skill.py             # CLI: run any expert skill via Claude/Gemini API
│   ├── test_e2e.py              # Driver for the full binder-design pipeline
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
│   ├── protein-design-script/   # RFDiffusion / BoltzDesign script generation
│   ├── chimerax-visualization/  # ChimeraX .cxc script for interface figures
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

### What to transfer

| Item | Size | Notes |
|------|------|-------|
| Git repo | small | `git clone` or copy |
| `data/literature.db` | ~15 MB | full paper catalog |
| `data/fingerprints/` | ~10 MB | curated JSON fingerprints |
| `data/vectors/` | ~10 MB | LanceDB semantic index |
| `data/pdfs/` | ~11 GB | only needed for re-curation |
| `.env` | — | recreate manually (never committed) |

If the new machine is **query/MCP use only**, skip `data/pdfs/` — the MCP server only needs the fingerprints and vectors.

### Steps

**1. Clone the repo and install dependencies**
```bash
git clone <repo> little_protein_tiger
cd little_protein_tiger
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS
pip install -e .              # core dependencies
```

**2. Copy data directories**

Transfer `data/literature.db`, `data/fingerprints/`, and `data/vectors/` to the same paths on the new machine. Optionally add `data/pdfs/` if you want curation capability.

**3. Recreate `.env`**

Copy `.env.example` to `.env` and fill in your API keys. If you use the
binder/design track, also fill in the `LPT_BOLTZGEN_EXECUTABLE` /
`LPT_PYROSETTA_PYTHON` / `LPT_FOUNDRY_ROOT` / `LPT_CLUSTER_*` vars for
wherever those tools live on the new machine — see
[Environment setup](docs/environment_setup.md). `config.yaml` itself needs
no path edits; it never carries machine-specific values.

**4. Update MCP configs — one path each**

The MCP server is launched via `scripts/launch_mcp.py`, which auto-derives all data paths from its own location. The only hardcoded value is the project root (`cwd`).

In **`.mcp.json`** (Claude Code):
```json
{
  "mcpServers": {
    "literature-db": {
      "command": "/path/to/little_protein_tiger/.venv/Scripts/python.exe",
      "args": ["/path/to/little_protein_tiger/scripts/launch_mcp.py"]
    }
  }
}
```

In **`%APPDATA%\Claude\claude_desktop_config.json`** (Claude Desktop, Windows) or **`~/.config/Claude/claude_desktop_config.json`** (macOS/Linux):
```json
"literature-db": {
  "command": "/path/to/little_protein_tiger/.venv/Scripts/python.exe",
  "args": ["/path/to/little_protein_tiger/scripts/launch_mcp.py"]
}
```

> Use absolute paths for both `command` and `args`. Claude Desktop does not reliably honour `cwd` on Windows — relative paths resolve to `C:\Windows\System32`. Point `command` directly at the venv Python so the launcher's re-exec logic is a no-op.

**5. Handle the embedding model cache**

The MCP server runs with `HF_HUB_OFFLINE=1`, so it uses a locally cached copy of `NeuML/pubmedbert-base-embeddings`. Two options:
- **Copy the cache**: transfer `~/.cache/huggingface/` from the old machine
- **Re-download**: temporarily remove `HF_HUB_OFFLINE` from `.mcp.json`, start the server once to trigger the download, then add it back

**6. Verify**

```bash
python -c "from src.vector_store import VectorStore; v = VectorStore('data/vectors'); print(v.count(), 'vectors')"
```

Restart Claude Desktop after updating its config.

---

## Workflow after adding new search terms

```bash
# 1. Run the new searches
python scripts/fetch_papers.py --config config_search_expansion.yaml

# 2. Curate the new downloads
python scripts/curate_papers.py --limit 100

# 3. Refresh the vector index
python scripts/ingest_vectors.py

# 4. Restart Claude Desktop to pick up new fingerprints via MCP
```

## Journal filtering — read this before building a corpus

**By default LPT downloads only papers from journals on a curated tier list.**
On the shipped corpus that gate passes **32% of indexed papers** and blocks the
other 68% — *PLoS One*, *bioRxiv*, *Scientific Reports* and *IJMS* are the
largest exclusions. This is a deliberate quality judgement, and it shapes the
corpus, the vector search, the interaction graph, and every target a discovery
workflow proposes.

The tier lists live in `src/ranking.py` (`_TIER1_JOURNALS`, `_TIER2_JOURNALS`)
and reflect a **molecular / structural / chemical biology** focus. If your field
sits elsewhere you will want to extend them via `quality.tier1_extra` /
`tier2_extra`, or switch the gate off:

```yaml
# config.yaml
quality:
  require_tiered_journal: false   # ~3.6x more downloads, matching cost + disk
```

**→ [docs/journal-filtering.md](docs/journal-filtering.md)** — the tier lists,
what is excluded and why, the scoring ladder, known gaps, and how to extend it.

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

LPT itself is MIT-licensed (see `LICENSE`) and comes with no warranty.

**The MIT licence covers this repository only.** LPT orchestrates external
models and tools that it does not ship, does not redistribute, and grants no
rights to. Several are free for academic use but **restricted for commercial
use** — check each one against your own use case before relying on it:

| Tool | Needed for | Licence — check before commercial use |
|---|---|---|
| [RFdiffusion3 / solubleMPNN / RF3 (foundry)](https://github.com/RosettaCommons/foundry) | `--workflow binder`, `--design-engine foundry` | RosettaCommons terms — read before commercial use |
| BoltzGen | `--workflow ppi` design + execution stages | see upstream repository |
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
[docs/environment_setup.md](docs/environment_setup.md#external-tools) for
install notes, GPU/disk requirements, and the checkpoint-registry gotcha.

Bundled third-party code:

- **Mol\*** (`assets/vendor/molstar/`) — MIT, vendored so generated reports have
  no runtime network dependency. Licence and pinned version in
  `assets/vendor/molstar/`.

Python dependencies are MIT/BSD/Apache, with one to be aware of: **PyMuPDF is
AGPL-3.0-or-later**. It is imported at runtime by `src/text_extractor.py` for
PDF parsing and is not linked into anything distributed here, but if you
redistribute a derivative that bundles it, read its terms.
