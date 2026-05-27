# Little Protein Tiger

An end-to-end pipeline for PPI drug target discovery. Covers automated paper discovery, Claude-powered structured extraction, a vector database for semantic search, and a suite of AI expert skills for pathway analysis, structural interface analysis, and binder design. Skills run either inside Claude Desktop (via MCP) or from the CLI using the Claude or Gemini API directly.

**Current corpus state (2026-04-01):** ~9,865 papers indexed · 1,944 downloaded · 988 curated fingerprints

---

## Requirements

- Python 3.10+
- Virtual environment (`.venv` recommended)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt
```

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
  model: "claude-haiku-4-5-20251001"

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
python scripts/ask_corpus.py --model claude-sonnet-4-6 --top-k 10
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

# Use Gemini instead of Claude
python scripts/run_skill.py \
    --skill molecular-biology-expert \
    --query "What is known about the YAP-TEAD interaction interface and hotspot residues?" \
    --model gemini

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
| `--model` | `claude` | Provider: `claude` or `gemini` |
| `--model-id` | provider default | Override model (e.g. `claude-opus-4-6`) |
| `--context` | — | Path to a prior report `.md` to include as context |
| `--output` | stdout | Write final report to this file |
| `--max-iter` | 30 | Max LLM calls per run |
| `--max-tokens` | 100,000 | Abort if any single call exceeds N input tokens |
| `--interactive`, `-i` | off | After the initial query (or with no `--query`), drop into a REPL for multi-turn follow-ups |
| `--trace` | — | Write a conversation trace (raw JSON + rendered Markdown) to this directory |

Default models: `claude-sonnet-4-6` for Claude, `gemini-3.1-flash-lite-preview` for Gemini.

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

Configuration lives under `design:` in `config.yaml`:

- `design.workstation.boltzgen_executable` — absolute path to the BoltzGen entry
  point (we use the entry script's own shebang to invoke its conda/uv env, no
  `conda activate` needed).
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
- `design.pyrosetta.python_executable` — absolute path to a conda env where
  PyRosetta imports cleanly (typically Python 3.11; see
  `/home/.../pyrosetta/SETUP_NOTES.md`).

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
and set the absolute path in `config.yaml`. For PyRosetta, see
`/home/m.uckelmann_cbs-niob.local/pyrosetta/SETUP_NOTES.md` — the LPT venv
itself does NOT need PyRosetta installed; the orchestrator subprocesses out
to a dedicated env via `scripts/_sasa_worker.py`.

See `diary.md` for design notes, known failure modes, and the long-term
plan for a ground-truth PDB→protein lookup table.

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
├── requirements.txt
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
pip install -r requirements.txt
```

**2. Copy data directories**

Transfer `data/literature.db`, `data/fingerprints/`, and `data/vectors/` to the same paths on the new machine. Optionally add `data/pdfs/` if you want curation capability.

**3. Recreate `.env`**

Copy `.env.example` to `.env` and fill in your API keys.

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
