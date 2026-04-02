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

```
fetch_papers.py          Search + download XMLs/PDFs → literature.db
      ↓
curate_papers.py         Claude extracts structured fingerprint JSONs
      ↓
ingest_vectors.py        Embed fingerprints into LanceDB vector store
      ↓
      ├── mcp_server.py            MCP tools for Claude Desktop skills
      │                            (search_corpus, get_fingerprint)
      │
      └── run_skill.py             CLI: run any expert skill via API
                                   (Claude or Gemini, no IDE required)
```

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
| `pathway-expert` | Searches the literature corpus to characterise a signalling pathway in a disease context and recommend the best PPI target node |
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

Default models: `claude-sonnet-4-6` for Claude, `gemini-3.1-flash-lite-preview` for Gemini.

Token usage is logged after every LLM call. If the per-call input token count exceeds `--max-tokens`, the run is aborted with a clear error showing cumulative usage.

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
│   └── run_skill.py             # CLI: run any expert skill via Claude/Gemini API
│
├── skills/                      # Expert skill definitions (SKILL.md = system prompt)
│   ├── pathway-expert/          # Disease pathway analysis + PPI target selection
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
