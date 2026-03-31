# Literature Search Agent

Automated discovery and download of open-access scientific PDFs from PubMed Central and bioRxiv/medRxiv, focused on protein-protein interaction (PPI) therapeutics. Part of a larger pipeline for AI-powered literature curation (Sprint 2: Claude-based extraction + vector database).

## Requirements

- Python 3.10+
- Conda or pip environment

```bash
pip install -r requirements.txt
```

## Configuration

All tunable parameters live in `config.yaml`:

```yaml
keywords:
  - "protein-protein interaction inhibitor peptide"
  - "stapled peptide protein interaction"
  # ...

search:
  max_results_per_query: 200
  open_access_only: true
  sources: ["pmc", "ppr"]   # pmc = PubMed Central, ppr = bioRxiv/medRxiv

paths:
  pdf_dir: "data/pdfs"
  db_path: "data/literature.db"

rate_limits:
  europepmc_delay_s: 0.5
  download_delay_s: 1.0
  max_retries: 3
```

Copy `.env.example` to `.env` and fill in credentials (optional for Sprint 1):

```bash
cp .env.example .env
```

## Usage

All commands are run from the repository root.

### Dry run — search only, no downloads

```bash
python scripts/fetch_papers.py --dry-run
```

Prints the list of papers found (up to 50 shown, full count reported) and DB stats. Nothing is written to `data/pdfs/`.

### Download papers

```bash
python scripts/fetch_papers.py
```

Searches all keywords in `config.yaml`, upserts metadata to `data/literature.db`, and downloads PDFs to `data/pdfs/`. Re-running is safe — already-downloaded papers are skipped.

### Override keywords or result limit

```bash
# Single custom keyword
python scripts/fetch_papers.py --keywords "BCL2 inhibitor BH3 mimetic" --max 50

# Multiple keywords
python scripts/fetch_papers.py --keywords "MDM2 p53 inhibitor" "KRAS inhibitor peptide" --max 100

# Limit results per keyword (overrides config.yaml)
python scripts/fetch_papers.py --max 20
```

### Use a different config file

```bash
python scripts/fetch_papers.py --config my_config.yaml
```

### Full CLI reference

```
usage: fetch_papers.py [-h] [--config CONFIG] [--keywords KEYWORDS [KEYWORDS ...]]
                       [--max MAX] [--dry-run]

options:
  --config    Path to config YAML (default: config.yaml)
  --keywords  One or more search terms, overriding config keywords
  --max       Max results per keyword (overrides config)
  --dry-run   Search only, print results, skip all downloads
```

## Output

| Path | Contents |
|------|----------|
| `data/pdfs/` | Downloaded PDF files, named `{identifier}_{hash}.pdf` |
| `data/literature.db` | SQLite database with full paper metadata |

### Database schema

The `papers` table mirrors the `Paper` model:

| Column | Type | Notes |
|--------|------|-------|
| `paper_key` | TEXT PK | `doi:{doi}` \| `pmcid:{pmcid}` \| `title:{md5}` |
| `doi`, `pmcid`, `pmid` | TEXT | Identifiers |
| `title`, `authors`, `journal`, `year`, `abstract` | TEXT/INT | Metadata |
| `source` | TEXT | `pmc` \| `biorxiv` \| `medrxiv` |
| `pdf_url` | TEXT | Resolved download URL |
| `pdf_path` | TEXT | Local path after download |
| `download_status` | TEXT | `pending` \| `downloaded` \| `failed` |
| `curated` | INT | Reserved for Sprint 2 (Claude curation) |

Query the DB directly with Python:

```python
import sqlite3
conn = sqlite3.connect('data/literature.db')
conn.row_factory = sqlite3.Row

# Status summary
for r in conn.execute("SELECT download_status, COUNT(*) as n FROM papers GROUP BY download_status"):
    print(dict(r))

# Browse papers
for r in conn.execute("SELECT title, source, download_status FROM papers LIMIT 10"):
    print(dict(r))
```

## How it works

1. **Search** — Queries the [Europe PMC REST API](https://europepmc.org/RestfulWebService) for each keyword with `OPEN_ACCESS:Y` and source filters (`SRC:PMC OR SRC:PPR`). Uses cursor-based pagination. Deduplicates across keywords by DOI.

2. **PDF URL resolution** — For PMC papers, queries the [NCBI PMC Open Access service](https://www.ncbi.nlm.nih.gov/pmc/tools/oa-service/) for a direct FTP PDF link (served over HTTPS). For bioRxiv/medRxiv preprints, uses the URL from Europe PMC's `fullTextUrlList` or constructs it from the DOI.

3. **Download** — Streams each PDF to disk, validates the `%PDF` magic bytes, retries with exponential backoff on failure. Updates the SQLite DB after each paper.

4. **Dedup on re-run** — Papers already in the DB with `status=downloaded` and a file on disk are skipped entirely.

## Project structure

```
literature_search_agent/
├── config.yaml              # Keywords, limits, paths
├── requirements.txt
├── .env.example             # NCBI_EMAIL, NCBI_API_KEY, ANTHROPIC_API_KEY stubs
├── src/
│   ├── models.py            # Pydantic Paper model
│   ├── database.py          # SQLite wrapper
│   ├── search.py            # Europe PMC client
│   └── downloader.py        # PDF fetcher
├── scripts/
│   └── fetch_papers.py      # CLI entry point
├── curation_prompt.md       # System prompt for Sprint 2 Claude curation
├── extraction_schema.json   # Target JSON schema for Sprint 2 output
├── diary.md                 # Development log
└── data/                    # Gitignored
    ├── pdfs/
    └── literature.db
```

## Sprint 2 (upcoming)

- PDF text extraction with `pdfplumber` or `pymupdf`
- Claude-powered curation using `curation_prompt.md` → structured JSON per `extraction_schema.json`
- Vector database (ChromaDB or LanceDB) for semantic search over findings
- Tool-callable search interface for LLM agents
