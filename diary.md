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

---

## Session 2026-04-02 — CLI Skill Runner, Structure Tools, Token Management

### Core architectural shift: IDE-independent pipeline

The pipeline previously required Claude Desktop or Claude Code — MCP tools were only accessible via the IDE. Removing the ChimeraX dependency (see below) cleared the last blocker: all tools are now pure Python, so skills can run via direct API calls without any IDE.

### Pure Python structure analysis (`src/structure_tools.py`, `src/structure_tools_server.py`)

Replaced the ChimeraX MCP dependency for all geometric analysis with a standalone Python library:

- **`src/structure_tools.py`** — five public functions: `analyze_interface`, `get_residue_contacts`, `check_mutation_clash`, `get_sequence_map`, `score_surface_patch`
  - Parsing: gemmi (mmCIF auth_seq_id → label_seq_id mapping, .pdb fallback via biopython)
  - SASA/BSA: `Bio.PDB.SASA.ShrakeRupley` (replaces freesasa — no C compiler needed on Windows)
  - Distances: scipy KDTree for fast heavy-atom contact queries
  - Interaction classification: electrostatic, h_bond, h_bond_candidate, hydrophobic, pi_stacking_candidate, vdw_contact
  - Clash detection: Cβ heuristic with sidechain reach radii table
  - Patch scoring: KD hydrophobicity, spatial spread (Cα RMSD), Excellent/Good/Marginal/Poor rating
- **`src/structure_tools_server.py`** — FastMCP wrapper; tool names prefixed `tool_` to distinguish from literature tools
- **`scripts/launch_structure_tools.py`** — venv-aware launcher, mirrors `launch_mcp.py` pattern
- **`.mcp.json`** updated: `structure-tools` server added alongside `literature-db`

**Note on freesasa**: failed to build on Windows (`Microsoft Visual C++ 14.0 required`). `Bio.PDB.SASA.ShrakeRupley` is equivalent algorithm with pre-built wheel.

**Key advantage over ChimeraX**: Claude receives exact numerical outputs (BSA floats, contact distances, interaction types) rather than parsing ChimeraX text output. More reliable and model-agnostic.

### Skills updated

- **`skills/chimerax-ppi-analysis/`** renamed → **`skills/complex-structure-analysis/`** — all cross-skill references updated via sed
- **`skills/complex-structure-analysis/SKILL.md`** rewritten: all ChimeraX tool calls replaced with structure-tools MCP tools; `tool_analyze_interface` single call covers full interface data; `tool_score_surface_patch` for quantitative hotspot scoring; pre-flight RCSB download via curl
- **`skills/binder-optimizer/SKILL.md`** — new skill: takes a predicted binder-target complex, proposes 4 independent single-point mutations (no combined round-1 submissions — non-additivity makes them uninterpretable), validates with `tool_check_mutation_clash`, emits 4 AF3 JSON objects; 5 mutation reasoning rules + hard constraints (no PRO, no GLY in turns, binder chain only)
- **`skills/pathway-expert/SKILL.md`** — added Phase 3.5 query expansion: extracts gene symbols and mutation terms from retrieved fingerprints, runs ≤2 follow-up searches with genuinely new terms only
- **`skills/complex-expert/SKILL.md`** — added Phase 2.5 query expansion: extracts pathway names and disease context terms (not gene symbols, which were the original query input)
- **`skills/complex-structure-analysis/SKILL.md`** — fixed B-factor/pLDDT confusion: skill now identifies `structure_source` (experimental / af3_boltz / rfdiffusion) from user input; defaults to `experimental`; never thresholds or flags B-factors on experimental structures
- **`skills/chimerax-visualization/SKILL.md`** — new skill: generates a commented `.cxc` ChimeraX script from a prior analysis report; target chain in focus (steel blue cartoon + amber hotspot surfaces), binder washed out (`transparency 70 cartoons` + `transparency 85 surfaces`), H-bonds in gold, labels on top hotspot residues, `supersample 3` save. No MCP tools needed — single LLM call, cheapest skill in the pipeline.

### CLI skill runner (`src/skill_runner.py`, `scripts/run_skill.py`)

- **`src/skill_runner.py`** — `SkillRunner` class: loads `skills/<name>/SKILL.md` as system prompt; orchestrator mode appends all sub-skill SKILL.mds; agentic loop with separate Claude (Anthropic SDK) and Gemini (REST API) paths; tool definitions in one list (`_TOOL_DEFS`), converted to `input_schema` (Claude) or `functionDeclarations` (Gemini) format; 7 tools routed to direct Python calls (no MCP subprocess); lazy VectorStore init; AA normalisation for `tool_check_mutation_clash`; DOI prefix normalisation for `get_fingerprint`
- **`scripts/run_skill.py`** — argparse CLI: `--skill`, `--query` (`@file` redirect), `--model claude|gemini`, `--model-id`, `--context`, `--output`, `--max-iter`, `--max-tokens`
- Default models: `claude-sonnet-4-6` / `gemini-3.1-flash-lite-preview`

### Token management

- **Rate limit retry**: `anthropic.RateLimitError` caught in `_run_claude`; exponential backoff (65s, 130s); re-raises after 3 attempts
- **`search_corpus` token reduction**: `execute_search_tool()` returns `{result_text, papers}`; stripped `papers` array in `_execute_tool` — the structured list duplicates the formatted text and adds ~1,500 tokens/paper × ~28 papers per pathway-expert run (~42k tokens saved per run)
- **`get_fingerprint` token reduction**: stripped `curation_metadata`, `methodology`, `contradictions_and_negative_results` — never read by any skill; saves ~500–1k tokens per fingerprint
- **Per-call token budget** (`max_input_tokens`, default 100k): API response token counts logged after every call; if `input_tokens > max_input_tokens`, run aborts with clear error message showing cumulative usage and remediation steps. Exposed as `--max-tokens` in CLI.
- **Architecture note**: context between pipeline stages should be passed via `--context` (final report, 3–5k tokens), not by chaining in a single conversation (tool-call history, 100k+ tokens). Each skill run starts with a fresh context window.

### README updated

- Title: `Literature Search Agent` → `Little Protein Tiger`
- Added `GEMINI_API_KEY` to `.env` table
- Updated pipeline overview diagram
- New section 5: full `run_skill.py` documentation with skill table, examples, options table
- Updated project structure

---

## TODO

- [ ] **Local Qwen3.5-9B curation provider** — code is in place (`--provider local`), needs a GPU with ~18–20 GB VRAM (fp16) or ~10 GB (fp8 quantized). Server command: `vllm serve Qwen/Qwen3.5-9B --port 8000 --reasoning-parser qwen3 --language-model-only [--quantization fp8]`
- [x] **Expert system: molecular biology expert skill** — implemented
- [x] **Expert system: orchestrator skill** — implemented
- [x] **Refine chimerax-ppi-analysis skill** — replaced with `complex-structure-analysis` (pure Python structure tools, no ChimeraX dependency)
- [ ] **Expert system: results/iteration expert** — future skill for analysing design run outputs and experimental results to close the design-test-iterate loop.
- [ ] **`get_fingerprint` usability** — the MCP tool currently requires an exact DOI. Consider adding a fallback that calls `search_corpus` with `top_k=1` when no exact match is found, so Claude can resolve fuzzy paper references.
- [ ] **Ingest workflow documentation** — add a note to README or CLAUDE.md: after `curate_papers.py --reprocess`, run `ingest_vectors.py --rebuild` to refresh the vector index.
- [ ] **Expert system: clinical trial expert (planned sprint)** — given a target protein identified by the pipeline, looks up clinical trials via a two-hop query: (1) OpenTargets GraphQL API to map target → known drugs/compounds, (2) ClinicalTrials.gov API v2 to fetch trials by compound. Implemented as a new MCP tool `search_clinical_trials(target_protein)` returning structured trial data (phase, status, primary endpoints, outcomes). Skill synthesises only from API response — prompt must explicitly forbid drawing on training-data knowledge of trials. Output block: trials found (by phase), status breakdown, endpoints used, outcomes met/not met, failure modes if terminated. Slots into pipeline after mol-bio expert, before orchestrator GO/NO-GO. Key interpretation rule: "no trials found" = possible white space, not a red flag; distinguish clearly from "tried and failed".
- [ ] **PMC local archive as bulk ingestion path (deferred)** — a local copy of the PMC Open Access corpus lives in `PMC_database/` as batched `.tar.gz` + `.csv` pairs (`PMC000xxxxxx`, `PMC001xxxxxx`, etc.). Each CSV maps AccessionID → Article File path inside the archive; XMLs can be streamed out with `tar --to-stdout` without full extraction. Implementation would be: (1) index all CSVs into a PMCID lookup table, (2) a `src/pmc_local.py` retriever, (3) wire into `downloader.py` as a local fallback before HTTP. Deferred in favour of targeted, high-quality searches — revisit if broad coverage becomes a priority.

---

## Sprint 5 — Complex Expert + Database Expansion

### Session 2026-03-31

#### New skills and scripts

**`skills/complex-expert/SKILL.md`** — new skill for characterising predicted or novel protein complexes from a gene list. Two modes:
- *Summary mode* (default, ~4–6 tool calls): rapid triage — pathway, disease relevance, novelty signal, fetch keywords. For screening many complexes.
- *Full pipeline mode*: deep characterisation + handoff to `chimerax-ppi-analysis`. Passes local AlphaFold `.cif` path via `run_command "open ..."` (not `open_structure` which is RCSB-only).
- Novelty signal logic: sparse/no corpus coverage on a predicted assembly = High novelty signal (possible new biology), not a failure.

**`skills/chimerax-ppi-analysis/SKILL.md`** — added Phase 1 section for local AlphaFold file handling: use `chimerax:run_command command="open /path/to/file.cif"` instead of `open_structure` for local files. Added note on pLDDT < 70 = lower interface geometry confidence.

**`skills/orchestrator/SKILL.md`** — Stage 0 now has two variants:
- Stage 0A: disease → target (pathway-expert, unchanged)
- Stage 0B: protein list → target (complex-expert, new). Handles local AlphaFold path passthrough to Stage 1. CAMPAIGN RECOMMENDATION template extended with `Novelty signal` and `Structure source` fields.

**`scripts/convert_complex_list.py`** — converts the project's complex metadata JSON (`info/meta_for_website_v20_041125_with_hash_corrected.json`, 518 complexes) into NCBI keyword queries. Uses GO terms and disease associations already embedded in the metadata — no UniProt API calls needed. Generates per-complex queries: pairwise interaction, full-complex AND, GO-term pathway, disease association, structure. Output: clean `complex_list.json` + `complex_keywords.yaml`.

**`scripts/generate_complex_keywords.py`** — generic keyword generator for arbitrary complex lists (CSV or JSON input). Uses UniProt API for GO-term lookup when the metadata doesn't already have it. Slower alternative to `convert_complex_list.py` for new complex lists.

#### Database expansion plan

Input metadata: `info/meta_for_website_v20_041125_with_hash_corrected.json`
- 518 predicted/known complexes, with `GeneNames`, `UniProtIDs`, `cluster_top5GO`, `Cluster_OT_top10_Disease` already populated

Generated keyword files (committed to `info/`):
- `info/complex_list.json` — clean complex list (id, proteins, go_terms, diseases)
- `info/complex_keywords.yaml` — 4499 unique NCBI queries

**Dry run benchmark** (100 keywords, `--max 50`, `require_tiered_journal: true`):
- 94 seconds search time
- 49 unique papers found (heavy deduplication — many queries overlap)
- Extrapolated full run: ~70 min search, ~500–2000 new papers before curation

**To run the full fetch overnight** — see instructions at end of this entry.

#### Rate limiting (confirmed safe for overnight run)
NCBI requests are rate-limited in `src/search.py`:
- Without `NCBI_API_KEY`: 0.34s delay between requests (~3 req/s — NCBI's stated limit)
- With `NCBI_API_KEY` in `.env`: auto-speeds to 0.1s delay (~10 req/s — NCBI's key tier)
- Europe PMC: 0.5s delay per request
- Both clients use `requests.Session` with `User-Agent: LiteratureSearchAgent/1.0`

Setting `NCBI_API_KEY` in `.env` would reduce the 70-min search time to ~25 min. Free key available at: https://www.ncbi.nlm.nih.gov/account/

#### Keyword generation workflow (for future reference)

Starting from the project's complex metadata JSON:

```powershell
# 1. Generate clean complex list + keyword YAML from complex metadata
python scripts/convert_complex_list.py `
    --input info/meta_for_website_v20_041125_with_hash_corrected.json `
    --output-list info/complex_list.json `
    --output-keywords info/complex_keywords.yaml

# 2. Merge complex keywords into a config copy (deduplicates against existing)
python scripts/merge_config_keywords.py
# Output: config_with_complexes.yaml  (base keywords + 4499 complex queries)

# 3. Dry run to check paper counts before committing to a full fetch
python scripts/fetch_papers.py --config config_with_complexes.yaml --dry-run --max 50

# 4. Full overnight fetch + curate + vector rebuild
python scripts/fetch_papers.py --config config_with_complexes.yaml
python scripts/curate_papers.py
python scripts/ingest_vectors.py --rebuild
```

For a generic protein list not in the complex metadata (e.g. a new set from a collaborator):
```powershell
python scripts/generate_complex_keywords.py --input my_proteins.csv --output-keywords my_keywords.yaml
python scripts/merge_config_keywords.py --keywords my_keywords.yaml
python scripts/fetch_papers.py --config config_with_complexes.yaml
```

#### Known issue: keyword specificity causing zero results

Several generated keywords returned 0 results, likely because:
- Queries combine two gene symbols AND a pathway/function term, all restricted to `[Title/Abstract]`
- Gene symbols for less-studied proteins rarely appear together in a paper's title or abstract
- GO-derived phrases (e.g. `"semaphorin-plexin signaling pathway"`) are often too verbose for T/A matching

**To revisit:** Consider a less restrictive fallback strategy in `convert_complex_list.py`:
- Drop the GO-phrase queries (keep pairwise interaction + cancer + structure queries which are simpler)
- Or switch some queries from `[Title/Abstract]` to full-text / no-field-restriction for obscure gene pairs
- Or add a `[MeSH Terms]` alternative for disease terms
- Run a post-hoc analysis: after a full fetch, identify which queries returned 0 hits and flag those complexes for alternative search strategies (e.g. UniProt function text search, STRING database, preprint servers)

#### Pending after overnight run
1. Run `curate_papers.py` on new downloads
2. Run `ingest_vectors.py --rebuild` (required to migrate LanceDB schema to include `study_category` column — not yet done)
3. Test `complex-expert` skill against newly curated papers
4. Audit zero-hit keywords and refine query strategy if coverage is thin

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

---

## Sprint 4 — Programmatic Pipeline Orchestrator

### Session 2026-04-05

#### CLI pipeline orchestrator (`src/pipeline_runner.py`, `scripts/run_pipeline.py`)

**Goal:** automate the full expert chain from a single terminal command, without requiring an interactive Claude session.

**Design:**
- `PipelineRunner` is a plain synchronous class — no async, no subprocess, no stdout. All output goes to files and a returned `PipelineResult` dataclass. This makes it trivially wrappable in FastAPI background tasks later without changes.
- Each stage calls `SkillRunner` directly (existing agentic loop), writes a `.md` report, and parses a `### PIPELINE HANDOFF` block from the output.
- Handoff blocks use `- key: value` bullet lines parsed by regex. Models sometimes wrap this in a code fence — parser handles both formats robustly.

**Stage sequence:**
```
Stage 0: pathway-expert       → 00_pathway.md    (PDB ID, target complex)
Stage 1: complex-structure-analysis → 01_structure.md  (hotspots, BSA, modality)
Stage 2: molecular-biology-expert   → 02_literature.md (GO/NO_GO, design_query)
Stage 3: go/no-go decision    → stops or continues (writes 02_campaign_recommendation.md on NO_GO)
Stage 4: protein-design-script → 03_design_inputs/ (BoltzGen YAML, RFD3 JSON)
```

**Resume / partial runs:** `--start-from {pathway|structure|literature|design}` with `--context path/to/prior.md` re-seeds the handoff from an existing report. `--pdb ACCESSION` skips the pathway stage entirely.

**Output layout:**
```
outputs/{slug}_{date}/
  00_pathway.md
  01_structure.md
  02_literature.md
  02_campaign_recommendation.md   (only on NO_GO)
  03_design_inputs/
    {complex}_boltzgen.yaml
    {complex}_rfd3.json
```

**Bug fixed:** first real run showed pathway expert wrote the `### PIPELINE HANDOFF` section inside a markdown code fence, breaking the regex. Fixed in two places:
1. `_parse_handoff()` now strips ``` fence lines and accepts bare `key: value` in addition to `- key: value`
2. Added explicit `**IMPORTANT:** Do NOT wrap in a code fence` warnings to all three SKILL.md files

**CLI usage:**
```bash
python scripts/run_pipeline.py --query "design PPI inhibitors for antibiotic resistant S. aureus"
python scripts/run_pipeline.py --query "..." --pdb 4U6V   # skip pathway stage
python scripts/run_pipeline.py --query "..." --start-from structure \
    --context outputs/my_run/00_pathway.md --output-dir outputs/my_run/
```

Exit codes: 0 = success, 1 = pipeline error, 2 = blocked (PDB not found, needs user input).

#### `### PIPELINE HANDOFF` spec added to all skill SKILL.md files
Each skill now writes a machine-readable block at the end of every report:
- `pathway-expert`: emits `pdb_id`, `target_complex`, `structure_query`
- `complex-structure-analysis`: emits `pdb_id`, `target_chain`, `partner_chain`, `target_complex`, `modality`, `bsa_A2`, `tractability`, `literature_query`
- `molecular-biology-expert`: emits `target_complex`, `tractability`, `go_recommendation`, `go_rationale`, `modality`, `design_query`

`go_recommendation` is the primary go/no-go signal: `GO | CONDITIONAL_GO | NO_GO`.

#### Web deployment plan

**Architecture:** FastAPI + Celery/Redis + SQLModel/SQLite + React/Vite frontend + Mol* structure viewer. B2B SaaS, BYOK (user supplies Anthropic API key). Deployed on Hetzner CX32 (~€13/mo).

**Project model:** Each project represents a disease/target area and accumulates corpus + run history over time. Runs are file-based (`web/runs/{id}/`), metadata in SQLite (`web/web.db`). Project-private corpus namespaced by `project_id` in LanceDB.

**Extended workflow (beyond first-pass design):**
- Design run (pipeline stages 0–4) → experimental result uploaded (SPR/ITC Kd) → binder-optimizer run → AF3 JSONs for next round
- Optimizer runs are child `Run` records with `parent_run_id`; lineage tree shown in UI

**LLM cost:** BYOK for private beta (zero cost liability). Later: prepaid credits via Stripe.

**Hosting:** Hetzner CX32 for private beta. Scale to CX42 at >5 concurrent pipelines. Redis via WSL or Docker locally; native on Linux server.

**Security baseline:** Fernet-encrypted API keys, JWT auth (8h expiry), GitHub OAuth, path-traversal guards on all file routes, audit log table, no query content in logs.

#### Web platform sprint plan (started 2026-04-06)

**Sprint 1 — Backend Foundation** ✅ COMPLETE (2026-04-06)
- FastAPI app (`web/backend/app.py`) with 22 routes
- SQLModel schema: User, Project, Run, ExperimentalMeasurement, AuditEvent (`web/backend/models_db.py`)
- Celery worker tasks: `run_pipeline_task`, `run_optimizer_task` skeleton (`web/backend/tasks.py`)
- GitHub OAuth + JWT (`web/backend/auth.py`)
- Fernet BYOK encryption (`web/backend/crypto.py`)
- Auth-protected CIF serving with PDB-ID validation (`web/backend/routers/structures.py`)
- SSE status stream at `GET /runs/{id}/status`
- Dev stack confirmed working: uvicorn + celery --pool=solo + Redis (WSL)

**Sprint 2 — Frontend Core** ✅ COMPLETE (2026-04-06)
- React + Vite scaffold with react-router-dom, TanStack Query, react-markdown
- Pages: Login, AuthCallback (/oauth route), Projects, ProjectDetail, RunDetail, Settings
- Stage progress stepper (StagePanel) + per-stage markdown rendering
- SSE hook (useRunStatus) via fetch+ReadableStream for live status updates
- BYOK API key entry in Settings page
- OAuth flow debugged: Vite proxy intercept issue → fixed with /oauth route + window.location.replace

**Pipeline token issues found and fixed (2026-04-06):**
- Structure stage was receiving full 00_pathway.md as context (~8k tokens) — now passes empty context (structure_query handoff field is sufficient)
- Literature stage now passes only 01_structure.md (not pathway + structure combined)
- `tool_get_sequence_map` was returning full auth_to_string_idx + auth_to_label_idx dicts (~15k tokens per chain) for ALL skills — now stripped to sequence+length for all skills except protein-design-script and binder-optimizer (which actually need the index maps to build AF3 JSONs)
- max_tokens raised to 200k in tasks.py (structure analysis legitimately hits 100k+ due to tool_analyze_interface output)
- Rate limiting (429) hits frequently at tier 1 (40k TPM) — consider Anthropic tier upgrade or Haiku/flash-lite for cheaper stages

**TODO — Model selection (implement next session):**

Allow users to choose model per run. Cheapest/fastest option matters for long pipelines.

Available models to expose:
- `claude-sonnet-4-6` — default, best quality (~$3/MTok in)
- `claude-haiku-4-5-20251001` — ~10x cheaper, faster, good for corpus-heavy stages
- `gemini-3.1-flash-lite-preview` — very cheap, already supported in SkillRunner

Implementation plan:
1. **Backend `Run` model already has `provider` and `model_id` fields** — no schema change needed
2. **`ProjectDetail.tsx` RunNew form** — add a model selector dropdown:
   - "Sonnet 4.6 (best quality)"
   - "Haiku 4.5 (fast + cheap)"
   - "Gemini Flash Lite (experimental)"
   - Maps to provider+model_id pairs passed to `POST /projects/{id}/runs`
3. **`tasks.py` `run_pipeline_task`** — already reads `run.provider` and `run.model_id` and passes to `_TrackedRunner`. No change needed — it already works.
4. **Gemini BYOK** — currently only Anthropic key stored. For Gemini, add `gemini_key_enc` field to `User` model and a second key entry in Settings. Or: use a single `provider_keys: JSON` field to store multiple keys.
5. **Per-stage model** — longer-term: allow cheap model for pathway/literature (corpus search, summarisation) and expensive model only for structure (spatial reasoning). Would require changing `PipelineRunner` to accept per-stage model config.

Quick win: just expose the three model options in the run form for now, all stages use the same model. Per-stage routing is a later optimisation.

**Sprint 3 — Structure Viewer** ✅ COMPLETE (2026-04-08)
- Mol* embedded in run detail
- Hotspot residues selected/highlighted from stage 1 handoff
- Contact table sidebar

**Sprint 4 — Binder Optimizer Workflow**
- Experimental data entry (SPR/ITC)
- Optimizer child run submission
- Run lineage tree component

**Sprint 5 — Corpus Expansion**
- DOI/PubMed query form → curation Celery task
- Project-private LanceDB namespace

**Sprint 6 — Production Hardening**
- Docker Compose bundle (api + worker + redis + nginx)
- Security headers, rate limiting, audit log middleware
- Deploy to Hetzner CX32

#### Key rotation (2026-04-06)

JWT_SECRET and FERNET_KEY were accidentally committed in .env.example and have been rotated. GitHub OAuth client secret was also regenerated. .env updated with new values.

**Action required:** Fernet key changed → all previously encrypted API keys in web.db are unreadable. Re-upload Anthropic API key in Settings before running any pipeline jobs.

---

## Sprint 3 — Structure Viewer

### Session 2026-04-08

#### Overview

Embedded Mol* 3D structure viewer into the run detail page, displayed below the Structure Analysis stage card once that stage completes. A contact table sidebar lists each hotspot residue's name, PDB sequence number, and RFD3 atom spec. Hotspot residues are persistently selected in the viewer so they are visually distinguished. No LLM calls — everything is derived programmatically from stage output.

#### Backend: parsing `hotspot_residues`

`Run.hotspot_residues` was defined in the DB model but never populated. The complex-structure-analysis skill writes a `### MODEL-READY HOTSPOTS` markdown table at the end of `01_structure.md`; the new `_parse_hotspot_residues(text, handoff)` method in `PipelineRunner` reads it:

- `re.findall(r"###\s+MODEL.READY HOTSPOTS.*?(?=\n###|\Z)", text, re.DOTALL|re.IGNORECASE)` — handles multiple hotspot regions
- Row regex: `r"^\|\s*([A-Z]+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|"` → columns residue, auth_seq_id, label_seq_id, rfd3_atoms
- Deduplication by `(residue, auth_seq_id)` key across all sections
- Returns JSON string `{"target_chain": "A", "partner_chain": "B", "residues": [...]}` or `None` if section absent

Called in all three `_TrackedRunner._run_stage()` overrides (run, resume, retry tasks) immediately after `super()._run_stage()` returns for `stage == "structure"`. Fires whether the run pauses at `structure_choice` or continues — `hotspot_residues` is populated in both cases.

`hotspot_residues` added to the SSE payload in `routers/runs.py` and to the `RunStatus` interface in `sse.ts`.

#### Frontend: components

**`ContactTable.tsx`** — parses `run.hotspot_residues` JSON, renders a compact table (Residue | PDB # | RFD3 Atoms) with an orange dot (`#E67E22`) matching the hotspot color, chain summary in the subheader. Shows "No hotspot data yet" placeholder when null.

**`StructureViewer.tsx`** — loads Mol* and renders the structure. The interesting part is how this was solved — see pitfalls below.

**`StagePanel.tsx`** — lazy-loads `StructureViewer` via `React.lazy()`, wraps it in a `ViewerErrorBoundary` class component, and renders the viewer + table in a `flex` row after the structure stage card whenever `stageFiles["01_structure.md"]` and `run.pdb_id` are both present.

#### Pitfall: Vite 8 (rolldown) + Mol* ESM module initialisation order

This was the main implementation challenge — 5 failed iterations before finding the solution.

**What broke:** `Uncaught TypeError: Cannot read properties of undefined (reading 'registerDefault')` inside `PluginUIContext.initBuiltInBehavior`. Mol*'s plugin context tries to call `PluginBehaviors.Representation.registerDefault(...)` during initialisation; when `PluginBehaviors.Representation` came from a separate Vite chunk, it was `undefined` at call time.

**Root cause:** Vite 8 uses rolldown for dependency bundling. Unlike Vite 4/5's esbuild prebundler, rolldown splits large packages like Mol* into multiple dep chunks (`molstar_lib_apps_viewer_app.js`, `context-CT9cJygy.js`, etc.). These chunks do not guarantee ES module evaluation order across the chunk boundary that Mol*'s internal `registerDefault` pattern requires.

**Things that did NOT fix it:**
- `optimizeDeps.exclude: ['molstar']` → exposed raw CJS `mutative/dist/index.js` → missing named ESM export `create`
- `resolve.alias` pointing `mutative` to `.esm.mjs` → failed on subpath imports
- `optimizeDeps.include: ['mutative']` → same CJS issue different path
- Parallel `Promise.all([import("molstar/lib/apps/viewer/app"), ...])` → race condition
- Sequential imports (app first, then others) → still hit chunk ordering issue
- Headless `PluginContext` + `initViewerAsync` (no React root, no Viewer.create) → blank canvas, 0×0 WebGL context, missing extension registration

**React root crash (earlier issue):** `Viewer.create()` internally calls `createRoot(container)`, creating a second React root inside the component's container div. When Mol*'s inner root threw an error, React 19 propagated it to the outer app root → white page. Fixed by adding `ViewerErrorBoundary` (class component, catches errors at the React tree level) and `React.lazy()` + `Suspense`.

**Solution that worked:** Use the **pre-built Mol* IIFE bundle** (`node_modules/molstar/build/viewer/molstar.js`). This is the same bundle used by `embedded.html` in the Mol* package itself. It sets `window.molstar` and handles all module initialisation ordering internally (webpack built it in correct dependency order). A custom Vite plugin serves it:

```typescript
// vite.config.ts — molstarBundlePlugin()
configureServer(server) {
  server.middlewares.use('/molstar.js', (_req, res) => {
    res.setHeader('Content-Type', 'application/javascript')
    fs.createReadStream(path.join(molstarDir, 'molstar.js')).pipe(res)
  })
  // same for /molstar.css
},
generateBundle() {
  this.emitFile({ type: 'asset', fileName: 'molstar.js', source: fs.readFileSync(...) })
  // same for molstar.css
}
```

`StructureViewer` loads the bundle via a dynamically-injected `<script>` tag, cached via a singleton Promise so it only loads once regardless of how many viewer instances mount:

```typescript
let _molstarPromise: Promise<void> | null = null;
function loadMolstarBundle(): Promise<void> { ... }
// then: await loadMolstarBundle(); window.molstar!.Viewer.create(container, options)
```

This eliminates all Vite/rolldown involvement in Mol*'s initialisation — the 4.85 MB bundle is served as a plain static file and parses itself.

**Build result:** `StructureViewer` ESM chunk is 2.75 kB (no Mol* imports at all). Total build: ~350ms.

#### Hotspot selection

The `Viewer` public API exposes `structureInteractivity({ expression, action })`. The `expression` callback receives `typeof MolScriptBuilder` as its argument, so no separate ESM import of `MolScriptBuilder` is needed:

```typescript
viewer.structureInteractivity({
  action: "select",
  expression: (MS) => MS.struct.generator.atomGroups({
    "chain-test": MS.core.rel.eq([MS.ammp("auth_asym_id"), targetChain]),
    "residue-test": MS.core.logic.or(residueNums.map(n =>
      MS.core.rel.eq([MS.ammp("auth_seq_id"), n])
    )),
  }),
});
```

`action: "select"` is persistent (unlike `"highlight"` which is hover-transient). The selection color is Mol*'s default teal/cyan, not orange — persistent orange overpaint (`setStructureOverpaint`) is not in the `Viewer` public API and would require accessing internal plugin state. The contact table sidebar with orange dots provides the color association; the viewer selection shows which residues are structurally relevant.

Note: `setStructureOverpaint` IS available in Mol*'s `mol-plugin-state/helpers/structure-overpaint` module, but importing it via ESM in Vite 8 hits the same chunk ordering issue. Future option: call it through `viewer.plugin` directly using the state transformer API (`window.molstar.lib.plugin.StateTransforms.Representation`).

#### Hotspot data for old runs

Runs that completed before this sprint have `hotspot_residues = NULL` in the DB (the parsing code didn't exist yet). The UI handles this gracefully: `ContactTable` shows "No hotspot data yet", and `structureInteractivity` is simply not called. This is expected — re-running the structure stage on an old run would populate it.

---

## Sprint 2 continued — Interactive Pause Points + Bug Sprint

### Session 2026-04-07

#### Interactive pause points (`PAUSED` status)

Goal: let users review pathway-expert output and select a target before committing to expensive structure + literature + design stages.

**New run lifecycle state: `PAUSED`**
Unlike `BLOCKED` (terminal error), `PAUSED` is a resumable wait state. The pipeline raises `PipelinePausedError` (a new exception subclass) instead of continuing; the Celery task catches it and writes DB fields, then returns normally.

**Four new DB columns on `Run`:**
- `auto_mode: bool = True` — existing runs stay fully automatic; new runs default to `False` (interactive)
- `pause_point: str | None` — `"pathway_choice"` or `"structure_choice"` while paused
- `pathway_choices_json: str | None` — JSON list of parsed target cards shown in UI
- `structure_next_step: str | None` — user's choice: `"literature_and_design"` / `"design_only"` / `"stop"`

Migration: `web/backend/migrate.py` extended with four `_add_column_if_missing` calls.

**Choices JSON approach (chosen after markdown parsing proved too fragile):**
The pathway-expert SKILL.md now requires a `choices_json` field in the `### PIPELINE HANDOFF` block — a single-line JSON array with tier, complex, pdb_ids, evidence_basis, key_uncertainty for each candidate. `_parse_pathway_choices()` in `pipeline_runner.py` tries JSON first (Path A), falls back to markdown regex (Path B). Eliminates the fragility of bold tier labels, nested headers, etc.

**`_parse_pathway_choices()` implementation:**
- Reads `choices_json` from primary_handoff dict first
- Falls back to markdown `TARGET OPPORTUNITY LANDSCAPE` regex parser
- `_annotate_choices()` helper: adds `index`, `structure_query`, `chain_ids_inferred` to each choice
- `primary_claimed` flag prevents multiple choices from claiming primary status when they share a PDB ID

**New API endpoints:**
- `POST /runs/{id}/resume` — `ResumeRunBody(chosen_target_index, next_step)` — validates pause state, writes chosen pdb_id/target_complex, re-queues `resume_pipeline_task`
- `POST /runs/{id}/retry` — for FAILED/BLOCKED runs; auto-detects start stage from `stage_current` (if pdb_id is set but stage is "pathway", starts from structure to preserve user's selection)

**New Celery tasks:**
- `resume_pipeline_task` — re-enters pipeline from `start_from` stage determined by `pause_point`
- `retry_run_task(run_id, start_from)` — general retry with explicit start stage; handles all three PipelineError types

**Frontend:**
- `PathwayChoicePanel.tsx` — amber card grid with tier badges (VALIDATED=green, BIOLOGICALLY_JUSTIFIED=amber, PATHWAY_INFERRED=gray); cards without PDB IDs are disabled
- `StructureChoicePanel.tsx` — three buttons: Run Full Pipeline / Skip Literature → Design / Stop Here
- `StagePanel.tsx` — `"paused"` StageStatus (amber, ⏸ icon); `StageStepper` renders choice panels after the relevant stage card
- `RunDetail.tsx` — PAUSED status pill, "Waiting for your input" banner, `handleResumed()` invalidates query cache
- `ProjectDetail.tsx` — "Skip review pauses" checkbox (default unchecked = interactive mode)

#### Reliability and output token fixes

**max_tokens raised to 24,000** (skill_runner.py, both Claude and Gemini). Anthropic SDK enforces streaming for `max_tokens ≥ ~16k` — replaced `client.messages.create()` with `client.messages.stream()` context manager + `stream.get_final_message()`.

**Skill conciseness instructions added** (all three main skills):
- pathway-expert: 3,000–4,500 word budget; max 4 target nodes × 6 bullets
- complex-structure-analysis: 3,000–4,000 words; H-bond table max 12 rows; max 2 hotspot regions
- molecular-biology-expert: 2,500–3,500 words; max 4 inhibitor classes × 5 bullets; max 8 sources rows

#### Bug: SQLModel + Pydantic v2 serializes table models as `{}`

**Root cause:** FastAPI returns SQLModel table model instances inside plain dicts. With SQLModel + Pydantic v2, `jsonable_encoder(run)` / `model_dump()` silently returns `{}` for table-backed models loaded from the DB session. Affects all endpoints returning `run` or `project` objects.

**Fix:** `_run_dict(run)` helper in `routers/runs.py` that iterates `run.__table__.columns` via SQLAlchemy and calls `getattr(run, col.key)` directly. Datetimes are `.isoformat()`'d. Same pattern applied in `routers/projects.py` as `_model_dict(obj)`. Applied to all endpoints returning Run or Project instances.

**Impact:** This was silently breaking the entire frontend — `data.run: {}` meant status, pause_point, pathway_choices_json etc. were all `undefined` in the browser.

#### Bug: pause_point cleared before Celery task reads it

**Root cause:** `resume_run` endpoint set `run.pause_point = None` then committed, then queued `resume_pipeline_task`. By the time the task ran and did `pause_point = run.pause_point`, the value was already `None` → "Cannot resume: unknown pause_point None".

**Fix:** Don't clear `pause_point` in the endpoint. The task already clears it at the start via `_set_run_fields(run_id, status="RUNNING", pause_point=None)`.

#### Bug: SSE payload missing pathway_choices_json

Live-watching runs: SSE fires PAUSED event → frontend sets `liveStatus` → overlay constructs `run` from `{ ...data.run (stale), ...liveStatus }`. The `pathway_choices_json` field only exists in the GET response, which hasn't refreshed yet. 

**Fix:** Add `pathway_choices_json` to SSE payload; add it to the `liveStatus` overlay in `RunDetail.tsx`. Panel renders immediately when SSE arrives.

#### Retry button

`POST /runs/{id}/retry` queues `retry_run_task` with auto-detected `start_from`. Logic: use `stage_current`, but if `stage_current = "pathway"` and `pdb_id` is already set (user previously selected a target), start from `"structure"` instead. Retry button rendered in the FAILED and BLOCKED error banners.

#### Skill quality fixes (observed in live runs)

**complex-structure-analysis — sequence numbering:**
Model was estimating auth→label offset from sequence comparison ("auth ≈ label for chain D given gap at C-terminus") instead of reading it from `get_sequence_map`. Added hard rule: use `auth_to_label` map verbatim, never estimate. Added Common Pitfall section on this. Also added `Separability` line to HOTSPOT REGIONS format and a `MODEL-READY HOTSPOTS — Region N` split instruction for independent regions.

**protein-design-script — multiple hotspots and missing PIPELINE HANDOFF:**
- When two independent hotspot regions exist (spatial spread > 15 Å between them), the skill now generates separate YAML + SLURM files per region rather than merging residues
- Added `### PIPELINE HANDOFF` section to the skill — eliminates "No PIPELINE HANDOFF block found" warning and sets `go_recommendation: GO` for the design stage completion
- `pipeline_runner.py`: design stage now reads its own handoff to update `result.go_recommendation`; when literature is skipped and `go_recommendation` is still "INCOMPLETE", defaults to `"GO"`

---

## Binder Campaign Management

### Session 2026-04-09

#### Overview

The design pipeline produces BoltzGen/RFDiffusion CSV result files and matching CIF structure files. Previously there was no way to import these into the platform, track experimental binding data, or run iterative optimization from the web UI. This session adds a **Binder Campaign** concept — a lightweight container for external design run results, sitting outside the AI pipeline — with full CRUD, affinity tracking, and optimizer integration.

#### New database tables (`web/backend/models_db.py`)

Three new SQLModel tables:

**`BinderCampaign`** — top-level container linked to a Project. Stores campaign name, optional target descriptor, and the original CSV filename once imported.

**`Binder`** — individual binder entry with:
- `sequence` — the designed/mutated sequence
- `design_to_target_iptm`, `min_design_to_target_pae`, `filter_rmsd` — quality metrics from the BoltzGen CSV
- `parent_id` (self-referencing FK) — enables a lineage tree: root binders are CSV imports, children are optimizer-generated or manually-entered mutants
- `source` enum — `csv_import | optimizer | manual`
- `mutation_label` — e.g. `A265E` for children
- `cif_path`, `optimizer_report_path` — file pointers stored as relative paths

**`BinderMeasurement`** — SPR/ITC/FP result per binder. Kd and Ki stored in Molar (UI accepts nM for convenience and converts on submission).

`init_db()` in `web/backend/db.py` updated to explicitly import `models_db` before calling `create_all`, so new tables are discovered on startup without manual migration steps.

#### New REST API (`web/backend/routers/binders.py`)

Ten endpoints covering the full lifecycle:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/projects/{id}/campaigns` | Create campaign |
| GET | `/projects/{id}/campaigns` | List campaigns |
| GET | `/campaigns/{id}` | Campaign + all binders + measurements |
| POST | `/campaigns/{id}/upload-csv` | Parse CSV → Binder rows |
| POST | `/binders/{id}/upload-cif` | Store CIF at `data/binders/{id}.cif` |
| GET | `/binders/{id}/cif` | Serve CIF file |
| GET | `/binders/{id}/optimizer-report` | Serve optimizer report markdown |
| POST | `/binders/{id}/measurements` | Add BinderMeasurement |
| DELETE | `/binders/{id}/measurements/{mid}` | Delete measurement |
| POST | `/binders/{id}/run-optimizer` | Queue Celery task |
| POST | `/binders/{id}/add-mutation` | Manual child binder |

CSV parsing logic: `csv.DictReader` on the uploaded file, required column `designed_sequence`, optional metrics columns. Row `id` / `name` / `design_id` used as binder name if present, otherwise row index.

Ownership guard pattern (`_get_owned_campaign`, `_get_owned_binder`) mirrors the existing runs router — looks up Project → checks `owner_id == user_id`, returns 404 for any mismatch.

#### Celery task (`web/backend/tasks.py`)

`run_binder_optimizer_task(binder_id, user_id, cif_abs_path)` — runs the `binder-optimizer` skill as a background job:

1. Loads Binder from DB, sets up API key from User
2. Builds prompt: `"Optimize binder at {cif_path}, binder chain A, target chain B (Boltz output). Sequence: {sequence}"`
3. Runs `SkillRunner("binder-optimizer", ...)`, writes full report to `data/binders/optimizer_{binder_id}.md`
4. Parses `### MUTATION OUTPUT` JSON block (see skill changes below) via regex — extracts list of `{mutation, mutated_sequence}` dicts
5. Creates 4 child `Binder` rows with `parent_id=binder_id`, `source="optimizer"`, `mutation_label` set
6. Stores `optimizer_report_path` on the parent binder

On failure: logs error, raises (Celery retries per its policy). No DB status field added to Binder — the parent row persists; children simply don't appear if the task fails.

#### binder-optimizer skill changes (`skills/binder-optimizer/SKILL.md`)

**Phase 6 rewritten:** At this scale, binders will be tested experimentally rather than pre-filtered by AF3 reprediction. The AF3 JSON construction section (4 JSON objects per mutation with `useStructureTemplate: false`, `modelSeeds`, `dialect`, etc.) is removed.

Replaced with: after selecting top 4 mutations and calling `tool_get_sequence_map` to get the binder sequence, the skill applies each mutation independently and emits a **`### MUTATION OUTPUT`** fenced JSON block:

```
### MUTATION OUTPUT
```json
[
  {"mutation": "A265E", "mutated_sequence": "<full binder sequence with substitution>"},
  ...
]
```
```

This block is both human-readable in the report and machine-parseable by the Celery task.

**Phase 7 output report:** `### AF3 JSON OUTPUT` section replaced with `### MUTATION OUTPUT` block.

**Frontmatter and handoff contract** updated to remove all AF3 references. Common pitfall entry "No combined JSONs in round 1" updated to "No combined mutants in round 1" (AF3-agnostic framing). Round 2 note now reads: "re-invoke on uploaded CIF of best experimentally-validated mutant" rather than "best AF3 prediction".

#### Frontend (`web/frontend/src/`)

**`api.ts`** — added `BinderCampaign`, `Binder`, `BinderMeasurement` TypeScript interfaces plus `api.campaigns.*` (list, create, get, uploadCsv) and `api.binders.*` (uploadCif, cifUrl, optimizerReportUrl, addMeasurement, deleteMeasurement, runOptimizer, addMutation) method groups.

**`ProjectDetail.tsx`** — added `BinderCampaignsSection` component rendered below the design runs list. Shows campaigns as link rows; "New Campaign" button opens an inline creation form with name + optional target fields.

**`BinderCampaign.tsx`** (new page, route `/campaigns/:id`):
- Header: campaign name + target, "Upload CSV" / "Reimport CSV" button
- Binder table: Name | Sequence (truncated monospace + clipboard copy button) | iPTM | PAE | RMSD | Best Kd | Actions
- **Lineage tree**: each row has a ▶/▼ toggle if it has children; expanding shows child rows indented by depth level with mutation label and source badges (AI / manual)
- **Expanded state**: shows inline measurement table (method, Kd, Ki, notes, delete button) + "+ Add Measurement" button
- **Actions per row**: Upload CIF / CIF ✓ | Measurements (N) | Run Optimizer (disabled without CIF) | + Mutation | View Report (when optimizer has run)
- **Polling**: `refetchInterval: 5000` — campaign data refreshes every 5 s to pick up optimizer children as they are created by the Celery task; "Run Optimizer" button also triggers a 10-minute polling interval via `queryClient.invalidateQueries`
- **Modals**: CSV upload, CIF upload, Add Measurement (nM input → Molar storage), Add Mutation (label + full sequence), Optimizer Report viewer (fetches markdown text, renders in `<pre>`)

**`App.tsx`** — `/campaigns/:id` route added.

**`vite.config.ts`** — `/campaigns` and `/binders` proxy rules added to forward dev requests to the FastAPI backend.

#### Bug: "Campaign not found" on first navigation

After creating a campaign and clicking through to its page, the BinderCampaign component showed "Campaign not found." The root cause was missing Vite proxy entries: requests to `/campaigns/{id}` were hitting the Vite dev server (which returns the HTML shell), `apiFetch` failed to parse it as JSON, React Query put the query in error state with `data = undefined`, and the `if (!campaign)` guard fired. Fixed by adding `/campaigns` and `/binders` to `vite.config.ts`.

#### Commit

`dfa4192` — 1,461 insertions across 11 files, 2 new files. Pushed to `main`.

---

## 2026-05-02 — `corpus-explorer` skill + interactive CLI

### Motivation

`pathway-expert` and `molecular-biology-expert` are tuned for the binder-design pipeline — they emit `PIPELINE HANDOFF` blocks, run fixed query plans, and converge on a single go/no-go. Open-ended exploratory questions ("which proteins interact with KRAS?", "draw the LPA→LPAR1 cascade with affinities", "explain why knockout of A overactivates pathway B and propose discriminating experiments") were a poor fit for those rigid templates and the conversation always terminated after one answer.

### What was added

**`skills/corpus-explorer/SKILL.md`** — a conversational research-collaborator skill. Three loose output modes (relational, pathway/cascade, competing-hypothesis), inline DOI citation contract with `[uncited]` tag for training-knowledge claims, required "What the corpus does NOT say" closing, opt-in keyword-proposal mode that emits NCBI Title/Abstract YAML for corpus extension. Explicitly does not emit `PIPELINE HANDOFF` — the skill is a dead end for the orchestrator.

**`src/_corpus_graph.py`** — two corpus-wide retrieval helpers that semantic search misses:
- `get_interactions_for(protein, depth, min_mentions)` walks `key_findings[].protein_pair` across all fingerprints and returns ranked partners with DOIs and Kd/Ki anchors. Light alias normalisation (strip species prefix, drop letter-digit hyphens) plus substring/prefix containment so `YAP` matches `YAP1` / `hYAP`. Paralogs stay distinct (`TEAD1` ≠ `TEAD2`); query `TEAD` hits all four. Self-matches filtered (`["KRAS-G12C","KRAS"]` doesn't show KRAS as a partner of KRAS).
- `find_quantitative_evidence(protein_pair, metric)` returns all `key_findings` with non-null `Kd` / `Ki` for a pair, order-insensitive match, sorted tightest-binder first. Schema doesn't capture ΔΔG, so the metric enum is `Kd | Ki | both`.

Both helpers wired through `src/mcp_server.py` (auto-registered by the literature-db MCP server) and `src/skill_runner.py` `_TOOL_DEFS` + `_execute_tool`. Single source of truth — no logic duplicated between transports.

**`scripts/run_skill.py` `--interactive`** — REPL for multi-turn sessions. `SkillRunner.run()` is now resumable: if `self._messages` is populated the new query is appended to prior history; otherwise it starts fresh. `context_text` is only injected on the first turn so it doesn't duplicate. New `reset()` method clears history without reloading the SKILL.md or tool list. Per-turn auto-warning when the last call's input tokens exceed 70 % of `--max-tokens`. REPL commands: `/exit`, `/reset`, `/tokens`, `/save <dir>`, `@file.txt`.

### Smoke-tested against the live corpus

- `get_interactions_for("KRAS")` returns BRAF, RAF1, SOS1, EGFR — biologically correct top hits.
- `get_interactions_for("YAP")` returns TEAD/TEAD1/TEAD4, TAZ, LATS1/2.
- `find_quantitative_evidence(["YAP","TEAD1"], "Kd")` returns 4 hits sorted 60 nM → 48 µM.
- Empty case (`MYBPC3` / `titin`) returns 0 with caveats explaining the absence.
- Resumable `run()` invariants verified end-to-end: continuation preserves prior history, context is skipped on follow-ups, `reset()` clears state, post-reset run injects context again.

### Why no schema changes

ΔΔG values aren't currently captured by `extraction_schema.json`. Adding them would touch the schema, the curation prompt, the Pydantic model, and force re-curation of affected papers. Out of scope for this batch — flagged in the tool's caveats so the LLM can tell the user when ΔΔG would be the right metric and isn't available.

### Why one skill, three output modes

A single skill that branches internally is easier to maintain than three near-duplicate skills, and it lets the model fluidly switch between modes mid-conversation ("draw that as a cascade" → "now generate hypotheses for X"). If the prompt drifts and starts to do all three modes badly, splitting is straightforward — the SKILL.md sections are already siloed.

### Token-cost discipline

The keyword-proposal feature is opt-in (trigger on user request), not automatic, so it costs zero passive tokens. The "What the corpus does NOT say" closing is required but capped at one short paragraph. The new tools' caveat strings are part of the JSON return payload deliberately — they keep the LLM honest about negative results without adding system-prompt overhead.

### Files touched

- `skills/corpus-explorer/SKILL.md` (new)
- `src/_corpus_graph.py` (new)
- `src/mcp_server.py` — two new `@mcp.tool()` wrappers
- `src/skill_runner.py` — two new `_TOOL_DEFS` entries, two new `_execute_tool` branches, resumable `run()`, `reset()` method, `_last_input_tokens` field
- `scripts/run_skill.py` — `--interactive` flag, `_resolve_query` / `_emit` / `_interactive_loop` helpers
- `README.md` — corpus-explorer section, options table updates
- `CLAUDE.md` — restored / refreshed

### Out of scope (future work)

- ΔΔG capture in the curation schema.
- A `propose_search_keywords` MCP tool that writes config-fragment YAML to disk (current opt-in instruction is cheaper; defer until friction is real).
- `cache_control: ephemeral` markers on message blocks for very long REPL sessions.
- A canonical UniProt-backed protein-name resolver (current normalisation handles common cases).
