# Literature Search Agent — Development Diary

## Sprint 1 Goals
- [ ] Automated discovery of open-access PDFs from PubMed Central and bioRxiv
- [ ] Europe PMC as unified search layer
- [ ] SQLite for local metadata tracking
- [ ] Deduplication across keywords and re-runs
- [ ] PDF validation (magic bytes check)
- [ ] CLI entry point with dry-run mode

## Architecture Decisions

### Europe PMC as search layer (chosen over direct PubMed/bioRxiv APIs)
- Single API covers PubMed Central, bioRxiv, and medRxiv preprints
- Native `OPEN_ACCESS:Y` filter removes papers we can't download
- Returns PDF URLs directly in `fullTextUrlList` (no secondary lookup needed)
- Cursor-based pagination handles large result sets cleanly
- No API key required (rate limits are lenient for polite crawlers)

### SQLite for metadata store
- Zero infrastructure — single file in `data/`
- Easy to inspect with any SQLite viewer
- Upsert logic prevents duplicate rows across re-runs
- Will be extended in Sprint 2 for curation status tracking

### Pydantic v2 models
- Strong typing for Paper objects, enums for status fields
- Validated at ingestion, safe to serialize/deserialize from DB rows

### No Biopython dependency
- Europe PMC makes Biopython's Entrez layer unnecessary for Sprint 1
- NCBI credentials in `.env` reserved for Sprint 2 supplemental metadata

---

## Progress Log

### 2026-03-30 — Sprint 1 Implementation + Verification COMPLETE

**Implementation:**
- Created full file structure: `src/`, `scripts/`, `data/`
- Implemented `models.py`: `Paper`, `Source`, `DownloadStatus` enums
- Implemented `database.py`: SQLite wrapper with `paper_key` PK (doi > pmcid > title hash), upsert, dedup, stats
- Implemented `search.py`: Europe PMC client with cursor pagination, NCBI PMC OA service PDF URL resolution (ftp:// → https://ftp.ncbi.nlm.nih.gov/), bioRxiv fallback URL construction
- Implemented `downloader.py`: streaming download, PDF magic bytes validation, exponential backoff retry
- Implemented `scripts/fetch_papers.py`: CLI with `--dry-run`, `--max`, `--keywords` flags, UTF-8 stdout fix for Windows
- Created `config.yaml`, `requirements.txt`, `.env.example`, `.gitignore`

**Bugs fixed during implementation:**
- Composite PK in SQLite doesn't support `ON CONFLICT(doi)` — switched to single `paper_key` TEXT PK
- Europe PMC `?pdf=render` and NCBI `?tool=EBI` URLs both return non-PDF content — added `_UNRELIABLE_PDF_HOSTS` filter
- Windows cp1252 encoding errors for Unicode titles — added `sys.stdout.reconfigure(encoding='utf-8')`
- Europe PMC quoted phrase queries returned 0 results — removed surrounding quotes

**Verification results (2026-03-30):**
- `--max 20 --dry-run`: 92 papers listed (5 keywords × 20, deduplicated), no downloads
- `--max 20`: 37 PDFs downloaded to `data/pdfs/`, 0/37 invalid PDF magic bytes
- SQLite DB: 92 rows, `downloaded=37`, `pending=55` (papers without accessible PDF URLs)
- Re-run: 0 papers re-downloaded (dedup confirmed)
- Sources covered: PMC (62), bioRxiv (29), medRxiv (1)

---

## Known Issues / Limitations

- bioRxiv preprints: Europe PMC sometimes doesn't include a direct PDF URL for very new preprints. The fallback URL constructor uses `v1` which may not always be the latest version.
- Some PMC papers serve HTML "landing pages" instead of PDFs when the direct URL is accessed without certain headers — the magic bytes check will catch and mark these as failed.
- No retry logic on search requests (only on downloads) — a transient network error during search will result in that keyword being skipped.

---

---

## Sprint 2 — Claude Curation Pipeline

### Goals
- Extract full text from downloaded PDFs and XML files
- Claude API curation: structured JSON "fingerprints" per paper
- Validate fingerprints with Pydantic, persist to `data/fingerprints/`
- SQLite tracks curation status; fingerprint JSON is source of truth

### Implementation (2026-03-30)

**New files:**
- `src/text_extractor.py`: pymupdf (PDF) + lxml (XML) extraction with `[Page N, Para M]` / `[Section: X, Para N]` markers
- `src/curator.py`: Claude API call, Pydantic `Fingerprint` model validation, up to 3 retries on parse failure
- `src/fingerprint_store.py`: save/load/query fingerprint JSONs; dot-notation filter support
- `scripts/curate_papers.py`: CLI — `--limit N`, `--dry-run`, `--reprocess`, `--paper-key`

**Schema v2.0 changes** (`extraction_schema.json`):
- Fixed `inhibitory_constant_Ki` typo
- New `study_type` enum (biochemistry-appropriate: `experimental_in_vitro`, `experimental_structural`, etc.)
- `protein_origin_organism` changed to list (multi-organism papers)
- Added `protein_pair`, `experimental_context` fields to `key_findings`
- Added `schema_version`, `relevant` flag, `curation_metadata`

**Curation prompt additions** (`curation_prompt.md`):
- Relevance gate: `{"relevant": false}` for entirely off-topic papers only
- Unit conversion rule: all Kd/Ki must be Molar floats (5 nM → 5e-9)
- Confidence score rubric: 0.9–1.0 direct measurement with replicates → <0.5 computational
- Source span format: PDF = "Page N, Para M"; XML = "Section: X, Para N"
- Length caps: max 5 key_findings, 40-word claims, 3 contradictions, 10 entities each

**DB migration:** 6 new columns — `curation_status`, `curated_at`, `fingerprint_path`, `curation_model`, `curation_tokens`, `curation_error`

### Verification (2026-03-30)
- `--limit 5 --dry-run`: all 5 papers extracted correctly (mix of PDF/XML), no API calls
- `--limit 5`: 5 fingerprints saved to `data/fingerprints/`; 1 validation warning (null in `protein_pair`) handled by retry
- Cost: ~$1.20 for 5 papers (~$0.24/paper) at full 150k char input

### Cost optimisation
- Reduced `max_input_chars`: 150,000 → 70,000 (cuts input tokens ~50%; methods/results front-loaded in most papers)
- Output length caps in prompt keep fingerprints concise (navigation aids, not paper substitutes)
- Estimated full corpus cost at 70k chars: ~$50 for 429 papers (down from ~$100)

### Known issues
- Claude occasionally returns `null` as second element of `protein_pair` — Pydantic model updated to `Optional[str]`; retry resolves it

---

### Session 2026-03-30 (evening) — Gemini Provider + Corpus Quality Work

#### Gemini API integration (`scripts/curate_papers.py`, `src/curator.py`)
- Added `--provider claude|gemini` CLI flag to `curate_papers.py`; overrides `config.yaml`
- Added `provider`, `gemini_model` fields to `config.yaml` under `curation:`
- Implemented Gemini support in `curator.py` using the **REST API directly** (not the SDK)
  - `google-generativeai` (0.8.x) and `google-genai` (1.69.x) both caused silent `os._exit()` when imported after `anthropic` in the same process — namespace/dependency conflict
  - Pure `requests` POST to `generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` avoids the conflict entirely
  - Multi-turn retry: conversation history built as `contents` list and re-sent each attempt
- Refactored Claude retry logic to use shared `_append_retry_turn()` helper (cleaner parity between providers)
- `curation_metadata.model` correctly records whichever model was used

**Debugging notes:**
- Root cause of silent exit: `os._exit()` called inside google SDK during import — uncatchable by `except Exception` or even `except BaseException`; diagnosed by adding DEBUG logging to pinpoint where execution stopped
- Lazy import pattern (moving SDK import inside the function) didn't help since the exit happened at import time regardless

#### Journal tier list improvements (`src/ranking.py`)
- Added NLM abbreviations missing from tier lists: `sci adv`, `sci transl med`, `nucleic acids res`, `commun biol`, and others
- Added new journals based on corpus analysis:
  - **Tier 1**: `Cancer Discovery` / `Cancer Discov`
  - **Tier 2**: `Clin Cancer Res`, `Acta Pharm Sin B` (J Mol Biol was already present)
- Added `is_tiered_journal()` as a public helper function

#### Download quality filter (`src/ranking.py`, `scripts/fetch_papers.py`, `config.yaml`)
- Added `require_tiered_journal: true` to `config.yaml` (under `quality:`)
- `fetch_papers.py` now filters the pending download queue to tier 1/2 journals only when flag is set
- Score-based threshold was insufficient (unknown journals can score ~0.67 with good recency/pub type, overlapping tier 2); direct `is_tiered_journal()` check is unambiguous
- TODO in config: set to `false` once initial high-quality corpus is curated, then re-run for broader coverage

#### Corpus state (end of session)
- **Total DB**: grew to 687+ papers across 262 journals with new keyword (`de novo protein design`) and additional fetch runs
- **Tiered papers**: 250 total (171 T1, 79 T2), 241 downloaded (96%)
- **9 persistent failures**: ACS/Cell Press journals (JACS, J Med Chem, ACS Chem Biol, Cell, Sci Transl Med, J Biol Chem ×2, Biochemistry, Structure) — paywalled, no open-access PMC full text

---

## Session 2026-03-31 (evening) — Pathway Biology Expert + Manual Paper Ingestion

### Pathway biology expert implementation

Full implementation across 8 files (see git commit `215d3ce`):

- **`extraction_schema.json`** + **`src/curator.py`**: added `study_category` enum (`biochemistry`, `pathway_biology`, `structural_biology`, `clinical`, `review`) and `pathway_context` block (`disease_associations`, `target_nodes`, `pathway_logic`, `redundancy_risks`, `upstream_regulators`, `downstream_effectors`). All new fields `Optional` — 82 existing fingerprints stay valid.
- **`curation_prompt.md`**: added STUDY CATEGORY CLASSIFICATION and PATHWAY CONTEXT EXTRACTION sections (conditional on `study_category == "pathway_biology"`).
- **`src/vector_store.py`**: `study_category` added to LanceDB schema; `_build_embed_text()` enriched with pathway fields for better semantic retrieval; `study_category` filter added to `search()` and `execute_search_tool()`.
- **`src/mcp_server.py`**: `study_category` parameter added to `search_corpus` MCP tool.
- **`config.yaml`**: 12 new pathway biology keyword queries covering Hippo/YAP-TAZ, KRAS, cGAS-STING, SCAP-SREBP, MARCH E3 ligase, PROTAC pathways.
- **`skills/pathway-expert/SKILL.md`**: new skill — disease-driven target selection via corpus; 3-tier corpus gap fallback; outputs `## PATHWAY BIOLOGY REPORT` with recommended PPI + PDB ID.
- **`skills/orchestrator/SKILL.md`**: conditional Stage 0 (pathway-expert) added before structural analysis; triggers when disease provided without a PPI target.

**Next steps before pathway expert is useful:**
1. `python scripts/ingest_vectors.py --rebuild` — migrate LanceDB schema to include `study_category`
2. `python scripts/fetch_papers.py` with new pathway keywords — pull pathway biology papers
3. `python scripts/curate_papers.py` on new papers — populate `pathway_context`
4. Re-run `ingest_vectors.py --rebuild` to index new fingerprints

### Manual paper ingestion workflow

Some papers exist as open-access PDFs but are not indexed in Europe PMC or PubMed (e.g. recent Annual Reviews articles). The automated fetch pipeline will never find these. Tested with: *"Development of PROTAC Degrader Drugs for Cancer"*, Annual Review of Cancer Biology, DOI `10.1146/annurev-cancerbio-061824-105806`.

**Manual ingestion process:**

1. **Find the free PDF** — check [Unpaywall](https://unpaywall.org/products/api): `GET https://api.unpaywall.org/v2/<doi>?email=<email>`. Look for `oa_locations[].url_for_pdf` with `host_type=repository`.

2. **Download the PDF** to `data/pdfs/` with a filename derived from the DOI (replace `/` and `:` with `_`):
   ```bash
   # example filename: doi_10.1146_annurev-cancerbio-061824-105806.pdf
   ```

3. **Insert into SQLite** using `Database.upsert_paper()` with `download_status=DownloadStatus.downloaded` and `priority_score=1.0`. Set `pdf_path` to the absolute path.

4. **Curate** as normal:
   ```bash
   python scripts/curate_papers.py --paper-key "doi:<doi>"
   ```

5. **Ingest into vector DB**:
   ```bash
   python scripts/ingest_vectors.py
   ```

**Curation result for the PROTAC review:** `study_category=review`, `pathway_context=null` (correct — it's a clinical overview, not a pathway dysregulation paper). Key findings captured clinical trial milestones, degradation efficacy data, and resistance mechanism taxonomy.

**Note on Annual Reviews:** Does not deposit to PMC; papers only appear via author/institutional open-access deposits. Unpaywall is the reliable way to find these.

---

## TODO

- [ ] **Local Qwen3.5-9B curation provider** — code is in place (`--provider local`), needs a GPU with ~18–20 GB VRAM (fp16) or ~10 GB (fp8 quantized). Server command: `vllm serve Qwen/Qwen3.5-9B --port 8000 --reasoning-parser qwen3 --language-model-only [--quantization fp8]`
- [ ] **Expert system: molecular biology expert skill** — write `skills/molecular-biology-expert/SKILL.md` that uses `search_corpus` + `get_fingerprint` MCP tools to reason over biological effects, feasibility, prior art, and drawbacks for a given target complex. Mirror the structured-report output format of `chimerax-ppi-analysis`.
- [ ] **Expert system: orchestrator skill** — a meta-skill that sequences the three experts (structural → literature → design), defines handoff points, and synthesises a go/no-go recommendation for a design campaign.
- [ ] **Expert system: results/iteration expert** — future skill for analysing design run outputs and experimental results to close the design-test-iterate loop.
- [ ] **Refine chimerax-ppi-analysis skill** — review in light of the multi-expert architecture; ensure its structured report output is explicitly formatted for consumption by both the molecular biology expert and the protein-design-script skill.
- [ ] **`get_fingerprint` usability** — the MCP tool currently requires an exact DOI. Consider adding a fallback that calls `search_corpus` with `top_k=1` when no exact match is found, so Claude can resolve fuzzy paper references.
- [ ] **Ingest workflow documentation** — add a note to README or CLAUDE.md: after `curate_papers.py --reprocess`, run `ingest_vectors.py --rebuild` to refresh the vector index.

---

## Sprint 4 — Expert Skills

### Session 2026-03-31

#### Skills implemented
- `skills/molecular-biology-expert/SKILL.md` — queries `search_corpus` + `get_fingerprint` MCP tools to assess biology, prior art, feasibility, and design challenges for a PPI target. Outputs `## MOLECULAR BIOLOGY REPORT` with 6 sections mirroring the chimerax report format.
- `skills/orchestrator/SKILL.md` — meta-skill sequencing chimerax → molecular biology → design. Synthesises a `## CAMPAIGN RECOMMENDATION` with cross-validated hotspot table and GO / CONDITIONAL GO / NO-GO decision before committing to design compute.

#### MCP server fix (sentence_transformers thread deadlock)
- **Root cause**: `from sentence_transformers import SentenceTransformer` hangs when called from inside FastMCP's thread pool executor (OpenMP/MKL deadlock with the async event loop).
- **Fix**: pre-import `sentence_transformers` at module level in `src/mcp_server.py` (before `FastMCP(...)` is instantiated), so the import happens on the main thread at server startup, not lazily inside a tool call.
- **Added**: file-based logger in `mcp_server.py` (`data/mcp_server.log`) for subprocess debugging. Env vars in `.mcp.json`: `HF_HUB_OFFLINE=1`, `CUDA_VISIBLE_DEVICES=-1`, full PATH.

#### Testing instructions (after Claude Code restart)

**Skills must be uploaded to claude.ai as a zip before testing** (Claude Code does not auto-discover skills from the `skills/` directory at runtime).

**Step 1 — Test MCP server** (already working; confirm after restart):
- Restart Claude Code, reconnect literature-db via `/mcp`
- Ask: "Search the corpus for YAP TEAD binding affinity" — should return results within ~15s (model loads at server startup, not on first call)

**Step 2 — Test molecular-biology-expert**:
- Upload zip of `skills/molecular-biology-expert/` to claude.ai
- Trigger: "What does the literature say about YAP-TEAD? Run the molecular biology expert."
- Expect: 3+ `search_corpus` calls, 3–5 `get_fingerprint` calls, then a full `## MOLECULAR BIOLOGY REPORT` with all 6 sections
- Verify: CROSS-VALIDATED HOTSPOTS section cross-references residues correctly if a chimerax report is in context

**Step 3 — Test orchestrator** (requires ChimeraX MCP connected):
- Upload zip of `skills/orchestrator/` to claude.ai
- Trigger: "Run the full design pipeline for YAP-TEAD, PDB 3KYS, target TEAD"
- Expect: chimerax-ppi-analysis runs → one-paragraph summary + confirm → molecular-biology-expert runs → one-paragraph summary + confirm → CAMPAIGN RECOMMENDATION with cross-validated hotspot table → GO/CONDITIONAL GO/NO-GO → protein-design-script runs

---

## Sprint 3 — Vector DB + MCP Server

### Session 2026-03-31

#### Vector DB ingestion (`src/vector_store.py`, `scripts/ingest_vectors.py`)
- **LanceDB** chosen over ChromaDB (no server, disk-based, simpler setup)
- **Embedding model**: `NeuML/pubmedbert-base-embeddings` (PubMedBERT, 768-dim, biomedical-specific)
- **One vector per paper**: concatenation of `situational_context_hook` + all `claim` values
- **Schema**: paper_key, doi, title, study_type, embed_text (retained for debugging), vector, fingerprint_json
- **Incremental ingest**: existing keys read via `table.to_arrow()` (pandas not in venv)
- **Dedup confirmed**: second run reports "Nothing new to ingest"
- 82 fingerprints indexed in ~20s on CPU

**Bugs fixed during implementation:**
- `table.to_pandas(columns=["paper_key"])` fails in LanceDB 0.30 — switched to `table.to_arrow()` + `.to_pylist()`
- `table.to_pandas()` requires pandas (not installed) — same fix
- `search_builder.to_pandas()` in `search()` — replaced with `to_arrow()` + row-by-row `as_py()` access
- pyarrow silently called `os._exit()` in Anaconda base env — resolved by using a clean `.venv`
- HuggingFace Hub network check on every model load — fixed with `local_files_only=True` fallback

#### Interactive agent (`scripts/ask_corpus.py`)
- Multi-turn Claude conversation with `search_corpus` as a tool_use tool
- Agentic loop: handles `tool_use` stop_reason, executes all tool calls, appends `tool_result`, continues
- Prints `[search: "query", top_k=N]` per call so search activity is visible
- Default model: `claude-sonnet-4-6`; pass `--model claude-haiku-4-5-20251001` for ~20× cost reduction

#### MCP server (`src/mcp_server.py`, `.mcp.json`)
- **FastMCP** server exposing two tools:
  - `search_corpus(query, top_k, study_type)` — wraps `VectorStore.execute_search_tool()`
  - `get_fingerprint(identifier)` — loads full fingerprint JSON via `fingerprint_store.load_fingerprint()`; accepts bare DOI or `doi:` prefixed paper_key
- Config via env vars in `.mcp.json` (venv Python path hardcoded to avoid Windows PATH issues)
- Lazy `VectorStore` init on first tool call (embedding model load ~10s on first use, cached after)

#### Architecture decision: multi-expert system
The longer-term vision is a **single Claude orchestrator** with three specialist skills:
1. **Structural biology expert** (`chimerax-ppi-analysis`) — identifies interface hotspots
2. **Molecular biology expert** (TODO) — uses literature DB MCP to assess biology, feasibility, prior art
3. **Protein design expert** (`protein-design-script`) — generates model inputs for BoltzGen / RFD3

Skills serve as "expert mode prompts"; the MCP server makes the literature DB available to all of them without per-conversation setup. Structured report formats (`.md` handoff documents) are the communication medium between experts. True multi-agent coordination deferred until context window becomes a bottleneck.

#### Corpus state (end of session)
- 82 fingerprints indexed in vector DB
- MCP server implemented, pending first live test after Claude Code restart
