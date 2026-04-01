# Literature Search Agent

An end-to-end pipeline for building a curated scientific literature corpus, focused on protein-protein interaction (PPI) therapeutics for drug target discovery. Covers automated paper discovery, Claude-powered structured extraction, vector database ingestion, and an MCP server for LLM agent access.

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
| `ANTHROPIC_API_KEY` | Curation (`curate_papers.py`) |
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
mcp_server.py            Serve search_corpus + get_fingerprint as MCP tools
                         (used by expert skills in Claude Desktop)
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
    WHERE curation_status = 'completed' AND title LIKE '%TEAD%'
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

The server is registered in `~/.config/Claude/claude_desktop_config.json` (macOS/Linux) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Restart Claude Desktop after any config change.

To debug connection issues, check `data/mcp_server.log`. Known fix for sentence_transformers import hang on Windows: ensure `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, and `TOKENIZERS_PARALLELISM=false` are set in the server env block.

---

## Project structure

```
literature_search_agent/
├── config.yaml                  # Main config: keywords, limits, paths, curation settings
├── config_search_expansion.yaml # Topic-specific search expansion (run separately)
├── curation_prompt.md           # System prompt for Claude curation
├── extraction_schema.json       # Target JSON schema for fingerprints (v2.0)
├── requirements.txt
├── .env.example
├── .mcp.json                    # MCP server registration for Claude Code
├── diary.md                     # Development log and architectural decisions
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
│   └── mcp_server.py            # FastMCP server (search_corpus, get_fingerprint)
│
├── scripts/
│   ├── fetch_papers.py          # CLI: search + download
│   ├── curate_papers.py         # CLI: Claude curation pipeline
│   ├── ingest_vectors.py        # CLI: embed fingerprints into LanceDB
│   └── ask_corpus.py            # CLI: interactive conversational search
│
├── skills/                      # Claude Desktop expert skills (upload as zip)
│   ├── molecular-biology-expert/
│   ├── orchestrator/
│   └── protein-design-script/
│
└── data/                        # Gitignored
    ├── literature.db            # SQLite paper metadata + status tracking
    ├── fingerprints/            # One JSON fingerprint per curated paper
    ├── vectors/                 # LanceDB vector store
    └── pdfs/                    # Downloaded PDF/XML files
```

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
