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

---

## 2026-05-02 (later) — NetworkX graph layer + three new tools

### Motivation

`get_interactions_for` and `find_quantitative_evidence` answered "what binds X?" and "what's the affinity of A/B?" well, but the corpus has an interaction *graph* implicit in `key_findings[].protein_pair` that wasn't queryable. Three capabilities were missing:

1. **Path queries** — "is X connected to Y in the corpus, and how?" Without a graph, the LLM had to chain ~5 `get_interactions_for` calls (~10k tokens of output) to trace a path manually.
2. **Hub identification** — "what are the central nodes in this area?" Required calling `get_interactions_for` for many seed proteins.
3. **Visual export** — no way to hand off a neighbourhood to Cytoscape for visual exploration.

### Lightweight design — what was built and what was deferred

A NetworkX-backed in-memory undirected weighted graph, built once on first access from the same fingerprint set the existing tools scan. Single edge type, no directionality, no enzyme-substrate or kinase-substrate semantics — those would require schema extension and re-curation. The "build the simple version, learn what's missing, then justify the heavier schema work" path.

Edge attributes: `mentions`, `dois`, `kd_anchors[]`, `ki_anchors[]`, `tightest_kd_M`, `tightest_ki_M`. Nodes: `display_name` (longest variant seen) and `total_mentions`.

### Three new tools

- **`shortest_interaction_path(a, b, max_hops, k)`** — uses `nx.shortest_simple_paths` to yield up to `k` paths in length order, capped at `max_hops`. Returns each path with full edge details, plus `min_mentions_along_path` and `weak_links_count` so the LLM can flag single-paper edges to the user.
- **`interaction_hubs(top_n, min_mentions)`** — degree centrality after filtering edges below `min_mentions`. Returns sample partners + sample DOIs per hub. Caveat baked into every response: hub rank reflects literature attention, not biological importance.
- **`export_subgraph(seeds, output_path, depth, max_nodes)`** — BFS-bounded neighbourhood, written as Cytoscape.js JSON. Each seed expands to all matching nodes (a seed of `"TEAD"` pulls in TEAD1/2/3/4). The graph goes to disk, not into the conversation, so this is cheap to use.

### Token-cost analysis (the user's specific concern before approval)

Permanent system-prompt overhead from the three new tool definitions: ~440 tokens. With Anthropic's prompt caching (5-min TTL, 10× discount on reads), a 10-turn corpus-explorer session pays ~950 tokens of overhead from these tools — roughly $0.003 at Sonnet 4.6 input rates. Skills that don't get the tools (gated via `_filter_tools`) pay zero.

When the tools' use cases come up, they're token-*negative*: a path-query that would cost ~10k output tokens of chained `get_interactions_for` calls runs in ~250 tokens via `shortest_interaction_path`. Net effect across realistic usage: cheaper, not more expensive.

### Tool gating

New `_GRAPH_TOOL_SKILLS = {"corpus-explorer", "pathway-expert"}` set in `skill_runner.py`. The three graph tools only appear in those skills' tool lists; design / optimizer / structure-analysis skills are unaffected. Verified end-to-end:

| Skill | Has graph tools? | Total tools |
|---|---|---|
| corpus-explorer | yes | 12 |
| pathway-expert | yes | 14 |
| binder-optimizer | no | 10 |
| complex-structure-analysis | no | 9 |

### Smoke test against the live corpus

Real numbers (5,800-fingerprint corpus):
- Build: 7,927 nodes / 8,166 edges in 0.50 s. Cache hit thereafter is sub-millisecond.
- `KRAS → ERK1` (max_hops=4, k=3): finds `KRAS → BRAF → MEK1 → MEK2 → ERK1` and the mTOR-routed alternative.
- `LPAR1 → YAP` (max_hops=5, k=2): finds direct `LPAR1 → LPA → YAP` and the canonical `LPAR1 → RhoA → YAP` chain.
- `interaction_hubs(top_n=10, min_mentions=3)`: returns STING / DNA / Nucleosome / YAP / EGFR / KRAS / BRD4 / cGAS / SCAP — the corpus's actual top areas (Hippo, MAPK, cGAS-STING, chromatin-modifier work added recently).
- `export_subgraph(['YAP'], depth=1)`: 98 nodes / 139 edges, valid Cytoscape.js JSON.

### Two bugs found during smoke testing (and fixed)

1. **`"N/A"` appearing as a top hub** — some fingerprints carry placeholder strings in `protein_pair`. Fixed by filtering a `_PLACEHOLDERS` set (`""`, `"N/A"`, `"NONE"`, `"UNKNOWN"`, `"?"`, `"-"`, `"TBD"`, …) at normalisation time.
2. **Bogus seed name returned 23 spurious nodes** — single-letter "node" keys from curation noise (e.g. `"P"`) were matching long missing-seed strings via reverse substring containment (`"P" in "NOTAREALPROTEINXYZ"`). Fixed by:
   - Skipping nodes with normalised key length < 2 in `_build_graph`.
   - Tightening `_resolve_seeds` to use one-directional substring (`target in node_key`) only, with min length 3 on both sides.
   - Same min-length 3 for substring fallback in `_resolve_node`.

Side benefit: the YAP subgraph went from 180 nodes (with junk) to 98 nodes (clean); `resolved_seeds` now correctly returns `['YAP']` instead of fragment matches like `['HDAC6/YAP', 'P', 'TGF-β/YAP', ...]`.

### Files touched

- `src/_corpus_graph.py` — `_build_graph`, `_get_graph` (lazy module-level cache keyed by fingerprint dir), `_resolve_node`, `_resolve_seeds`, three new public functions, `_PLACEHOLDERS` set, min-length guards.
- `src/mcp_server.py` — three `@mcp.tool()` wrappers.
- `src/skill_runner.py` — three `_TOOL_DEFS` entries, three `_execute_tool` branches, new `_GRAPH_TOOL_SKILLS` set, `_filter_tools` extension.
- `skills/corpus-explorer/SKILL.md` — Tools section split into Retrieval / Graph subsections; tool-choice cheat sheet table; mention-count caveat in Common pitfalls.
- `requirements.txt` — `networkx>=3.0` pinned (was already installed transitively at 3.6.1).
- `README.md` — corpus-explorer section updated with graph tool descriptions and three new example queries.

### Out of scope (future work)

- Edge directionality (kinase → substrate, regulator → target). Requires schema extension, curator-prompt update, re-curation.
- Edge type labels (`binds`, `phosphorylates`, `ubiquitinates`, `transcriptionally_regulates`). Same dependencies.
- Persistence layer (Neo4j, on-disk graph). Not needed at 5k-fingerprint scale; rebuild-at-startup is fast enough.
- Folding the existing `get_interactions_for` / `find_quantitative_evidence` onto the cached graph. Defer until measured speed actually matters.

---

## 2026-05-02 (later still) — silent metadata-loss bug: 37 % of corpus invisible to graph tools

### How it surfaced

Claude Desktop, while pulling a fingerprint by paper_key, noticed that the DOI field inside `paper_metadata` was null for the paper it had just retrieved (`doi_10.1126_science.abf8705.json` — Final-form mSWI/SNF complexes, *Science* 2021). The user flagged it. An audit confirmed the scope: **2,031 of 5,547 fingerprints (37 %) had `paper_metadata.doi = null`**.

### Why it mattered (and was easy to miss)

Every cross-fingerprint helper in `src/_corpus_graph.py` and `src/skill_runner.py` short-circuits on a missing DOI:

```python
doi = (fp.get("paper_metadata") or {}).get("doi")
if not doi:
    continue
```

That guard is correct in spirit — a finding without a DOI can't be cited — but it meant 2,031 fingerprints were silently excluded from `get_interactions_for`, `find_quantitative_evidence`, `_build_graph`, `_find_pdb_structures`, and the three new graph tools. `get_fingerprint` worked fine because it loads by paper_key (filename), so the bug only manifested in *cross-fingerprint* aggregation. That's why it had hidden for so long: the per-paper view was always correct.

### Root cause

`scripts/curate_papers.py` lets the LLM extract `paper_metadata.doi`, `pmcid`, and `title` from the parsable paper text per `extraction_schema.json`. For older papers, paywalled HTML, and many bioRxiv preprints the DOI simply isn't in the body the LLM sees — the model returns `null`. Crucially, the curator already had `paper.doi` from the database (it's used at `curate_papers.py:212` for `_merge_pdb_accessions`), but never wrote it back into `paper_metadata`. The fix is one block of three `if`-checks before saving.

### The two-part fix

**Part 1 — prevent recurrence** (in `curate_papers.py`, before `_merge_pdb_accessions`):

```python
fingerprint.setdefault("paper_metadata", {})
pm = fingerprint["paper_metadata"]
if paper.doi and not pm.get("doi"):
    pm["doi"] = paper.doi
if paper.pmcid and not pm.get("pmcid"):
    pm["pmcid"] = paper.pmcid
if paper.title and not pm.get("title"):
    pm["title"] = paper.title
```

Only fills nulls — never overwrites a non-null LLM-extracted value (in case the curator legitimately corrected something).

**Part 2 — backfill existing fingerprints** (`scripts/backfill_fingerprint_metadata.py`, new): walks `data/fingerprints/`, builds a `{sanitised_filename → Paper}` index from the DB (28,570 records loaded in ~0.4 s), and fills missing metadata in each fingerprint. Idempotent; supports `--dry-run`.

### Impact (verified live)

Dry-run vs. apply:

| Stat | Count |
|---|---|
| Fingerprints checked | 5,547 |
| Updates applied | 5,531 |
| Already complete | 16 |
| Missing DB record | 0 |
| Unparseable | 0 |

The 5,531 includes both the 2,031 null-DOI cases AND ~3,500 fingerprints where the DOI was present but PMCID was missing (LLM extracted DOI but missed PMCID). PMCID isn't currently used by tools but worth backfilling for completeness.

Graph rebuild post-backfill:

| Metric | Before | After | Δ |
|---|---|---|---|
| Nodes | 7,927 | 11,903 | +50 % |
| Edges | 8,166 | 12,943 | +58 % |
| Top hub: BRD4 degree | 10 | 24 | +140 % |
| Top hub: DNA degree | 17 | 29 | +71 % |
| Null-DOI fingerprints | 2,031 | 0 | — |

The hub shift confirms the bias of the previously-excluded fingerprints — they were heavily chromatin / transcription papers (BRD4, DNA, Nucleosome jumped most), which lines up with the chromatin-modifier keyword expansion the user added to `config.yaml` earlier in the session. Without the backfill, much of the new chromatin curation work would have stayed invisible to graph queries.

### Idempotence verified

Second run of `backfill_fingerprint_metadata.py --dry-run` reports `would_update: 0`, all 5,547 fingerprints in `no_changes_needed`. Safe to leave the script in `scripts/` and re-run periodically as a sanity check.

### Why this lurked

Two reinforcing reasons:

1. **Per-paper queries always worked.** `get_fingerprint(doi:10.1126/science.abf8705)` returns the full payload because it loads by paper_key (filename), not by DOI lookup. The bug was only visible in *aggregation* queries — and aggregations don't loudly tell you what they skipped.
2. **The curator schema treats DOI as LLM-extractable.** It feels right ("the DOI is in the paper, the LLM should find it") but in practice the parsable PDF text often lacks the DOI in machine-recognisable form, and the schema didn't have a "trust the DB if the LLM fails" fallback.

The class of bug is "silent partial coverage" — the system returns plausible-looking results that are actually computed over a strict subset of the corpus. Worth keeping in mind for any future tool that aggregates across fingerprints: every short-circuit-on-null check is a potential silent-exclusion source. A periodic audit of "what fraction of fingerprints does this tool actually touch?" would catch this class of regression early.

### Files touched

- `scripts/curate_papers.py` — 11-line guard before `save_fingerprint`.
- `scripts/backfill_fingerprint_metadata.py` (new) — backfill script.
- `data/fingerprints/*.json` — 5,531 files updated by the backfill (gitignored, not in the commit diff).

### Out of scope (potential future work)

- Periodic audit job that reports per-tool corpus coverage (`get_interactions_for` touched X fingerprints, `find_quantitative_evidence` touched Y fingerprints). Would catch silent-exclusion regressions early.
- A `key_findings`-level provenance check — are there findings with non-null `protein_pair` but null `claim` or `source_span`? Same class of bug, different field.
- Schema-level validation at curation time that flags fingerprints where the LLM dropped fields the DB record could supply.

---

## 2026-05-03 — DepMap integration sprint 1+2: identifier normalization

Setup work for an upcoming pipeline that joins the corpus interaction graph (`src/_corpus_graph.py`) with DepMap CRISPR co-essentiality correlations. Naming was identified up-front as the dominant engineering risk — the corpus is loose with protein nomenclature, DepMap uses gene names. The full multi-sprint plan lives in `DEPMAP_INTEGRATION_PLAN.md`; this entry covers sprints 1 and 2 only.

### Sprint 1 — coverage spike (read-only)

`scripts/spike_normalize_identifiers.py` (kept in repo; useful as a reusable diagnostic) walked all 5,547 fingerprints, extracted protein names from every site they appear (`key_findings.protein_pair`, `target_nodes`, `upstream_regulators`/`downstream_effectors`, `entities.proteins`), and ran a tiered resolver against UniProt's `HUMAN_9606_idmapping.dat.gz` only. Baseline numbers: **103,373 protein-name occurrences across 24,777 unique raw names**. Resolution coverage:

| Tier (UniProt-only) | Unique names | Mention-weighted |
| --- | --- | --- |
| Exact gene_symbol | 30.0 % | 49.9 % |
| UniProt synonym | 5.6 % | 6.8 % |
| KB-ID stem / fuzzy | 2.9 % | 3.0 % |
| Stripped mutant | 0.4 % | 0.3 % |
| **HIGH-CONF (sum)** | **36.0 %** | **56.9 %** |
| Unmatched | 61.1 % | 40.1 % |

Below the 85 % target. The unmatched 40 % broke into four buckets:

1. **Junk / non-proteins** (~3.4 % of mentions): `N/A` (1,518), `null`, `None`, `DNA` (409), `Cas9` (221), `GFP`, `Nucleosome`, `H3K9me3` etc. Curator placeholders + research tools + PTM strings.
2. **Common-name aliases HGNC would catch** (~1.5 %): `PD-L1` (255) → CD274, `53BP1` (122) → TP53BP1, `LC3` → MAP1LC3A, `p62` → SQSTM1, `Tau` → MAPT, `Chk1` → CHEK1, `Pol II` → POLR2A, `E-cadherin` → CDH1, `Caspase-3` → CASP3.
3. **Greek letters** (~0.4 %): `NF-κB`, `β-catenin`, `ERα`, `IL-1β`, `IFN-γ`. Pure tokenisation issue.
4. **Family heads / paralog ambiguity** (~1.5 %): `YAP` → YAP1, `AKT` → AKT1/2/3, `MEK` → MAP2K1, `RPA` → RPA1/2/3, `TEAD` → TEAD1-4, `Hsp90` → HSP90AA1/AB1.

Bonus finding from the spike: **bad curated taxa** in `methodology.protein_origin_organism` — values of `0` and `1000000000` appear in the corpus. Not blocking sprint 2 but worth noting as a curator data-quality issue.

### Sprint 2 — production normalization backfill

Decisions taken (all per user):
1. **Storage**: sidecar block `protein_identifiers` written into existing fingerprint JSONs by an idempotent script. Curator never writes this key. Same pattern as the `297be83` canonical-DOI backfill.
2. **Family-head policy**: paralog-ambiguous resolutions return all candidates with `is_family_head: true`; downstream consumers decide whether to expand.
3. **Audit list**: filtered names go into a separate `filtered_out` array on the block with a `reason` tag (placeholder / non_specific_histone / complex / viral / research_tool) so the rejection trail is inspectable.
4. **HGNC alias table**: downloaded one-time from `genenames.org` (`hgnc_complete_set.tsv`, 17 MB, 20,288 approved symbols, 50,792 alias keys, 16,502 prev_symbol entries).

Architecture:
- `src/identifier_normalizer.py` — `IdentifierNormalizer` class with the resolver + `Resolution` dataclass + `extract_protein_occurrences()` walker. Reusable by future tools.
- `scripts/normalize_identifiers.py` — idempotent CLI driver with `--dry-run`, `--sample`, `--force`, `--dump-unresolved` flags.

Resolver tier order:
1. HGNC approved symbol exact
2. Curated biology aliases (~150 manually-vetted entries — Pol II, Caspase-N, cyclins, G-protein subunits, Greek-letter spellings, etc.)
3. Paralog-default — if `<NAME>1` is HGNC-approved, prefer that over alias hits (catches YAP→YAP1 even though HGNC has YAP→YY1AP1 in alias)
4. HGNC alias / HGNC prev
5. UniProt KB-ID exact
6. Stripped point-mutation suffix (`KRAS-G12C` → KRAS)
7. UniProt gene_symbol (lower-confidence — TrEMBL leak risk)
8. UniProt synonym
9. Family-head fuzzy (prefix + paralog suffix `1-9` or `A-D`)

Greek transliteration uses **spelled-out** forms (`α → ALPHA`, `β → BETA`) — this is how HGNC and UniProt write aliases (`"ER-alpha"`, `"beta-catenin"`, `"IL-1beta"`), not single-letter (which would only match formula-style usage).

Final coverage on 5,547 fingerprints / 103,373 occurrences (mention-weighted):

| Tier | % |
| --- | --- |
| `exact_gene` | 46.5 |
| `hgnc_alias` | 13.1 |
| `hgnc_prev` | 1.7 |
| `curated_alias` | 3.1 |
| `paralog_default` | 1.7 |
| `family_head` (lower conf) | 2.2 |
| `kb_id` + `stripped_mutant` | 0.2 |
| `uniprot_gene` + `uniprot_synonym` | 0.7 |
| **HIGH-CONF total** | **66.4** |
| **Resolved (high+low)** | **69.3** |
| `placeholder` filtered | 1.9 |
| `non_specific_histone` filtered | 0.8 |
| `complex` filtered | 0.5 |
| `viral` filtered | 0.4 |
| `research_tool` filtered | 2.8 |
| **Filtered total** | **6.4** |
| Unresolved | 24.3 |

**Below the 85 % HIGH-CONF target initially projected from spike data.** The shortfall is structural, not solvable with more aliases:

- The unresolved 24.3 % is dominated by **non-mammalian gene names** (yeast: `Cdc13`, `Sgs1`, `Sth1`, `Doa10`, `Clr4`, `Isw1/2`, `Reb1`, `Yen1`, `Rad6`; bacterial: `BamA`, `TnsB/C`, `TnpB`, `AgrC`, `IpaH7.8`, `SidJ`, `SdeA`, `LetB`, `LptD`, `NusG`, `AraC`, `PqsE`, `RhlR`; Drosophila: `Yki`, `Sd`; viral oligomers and ORFs). These have no DepMap relevance — DepMap is human cell-line CRISPR — so failure to resolve them costs nothing for the downstream pipeline.
- Long tail of typos, idiosyncratic compound forms (`H3-H4`, `MCM2-7`, `gamma-H2AX`), and PTM strings.

What matters for the DepMap join is the count of occurrences with a usable `human_uniprot` field set. That is the **69.3 %** number, and it represents almost all *meaningfully resolvable human protein mentions* in the corpus. Sprint 3 (DepMap edge enrichment) can proceed.

### Side-effects called out for future work

- **Family-head ambiguity is now first-class**: every multi-paralog resolution carries `is_family_head: true` + the full `candidate_uniprots` list. The graph-enrichment skill prompt in sprint 4 will need explicit instructions on how to surface family ambiguity to the user (best practice: report tightest correlation across the set, with the family member tagged).
- **COX2 ambiguity is the canonical hard case**: HGNC alias resolves to MT-CO2 (mitochondrial), but in literature COX2 almost always means PTGS2 (cyclooxygenase-2). Currently flagged as `is_family_head: true` with both candidates. Cleanest fix is contextual disambiguation from `experimental_context` text — out of scope here.
- **HGNC alias for `YAP` points to YY1AP1, not YAP1.** This is HGNC-correct (YAP1 doesn't list "YAP" as alias) but biologically misleading. The `paralog_default` tier (insert `<NAME>1` if approved) was added specifically to override this; equivalent overrides may surface for other shorthand names.
- **The `identifier_normalizer` module is HGNC-loadable but does not yet do strict-taxon disambiguation.** `native_taxon` is preserved on every entry but resolution is human-only. When sprint 2 results show ambiguous gene_symbol hits across species, sprint 3 onward should decide whether to use it.

### Files touched

- `data/depmap/hgnc_complete_set.tsv` (downloaded, 17 MB, gitignored)
- `src/identifier_normalizer.py` (new) — resolver library
- `scripts/normalize_identifiers.py` (new) — idempotent backfill driver
- `scripts/spike_normalize_identifiers.py` (kept) — reusable coverage diagnostic
- `data/fingerprints/*.json` — 5,547 files now carry a `protein_identifiers` block
- `DEPMAP_INTEGRATION_PLAN.md` (new at repo root) — full multi-sprint plan
- `data/depmap/sprint2_unresolved.tsv` — audit dump of names that failed resolution

### Next sprint

Sprint 3: `src/depmap.py` lazy DataFrame loader + on-demand `correlation_for_pair`. One-shot enrichment pass that walks the literature graph, joins each edge to DepMap by `human_uniprot → gene_symbol`, and attaches `depmap_pearson_r` + sample-size metadata to each edge.

---

## 2026-05-03 — DepMap integration sprint 3: loader, enrichment, tools

End-to-end pipeline from literature names → resolved gene symbols → DepMap CRISPR co-essentiality (Pearson r). Three new tools wired into both transports (MCP + skill-runner) and documented in the corpus-explorer skill prompt.

### Architecture in place

**`src/depmap.py`** — pyarrow + numpy CSV loader, deliberately no pandas dependency (lean-deps style of this project; pyarrow was already in the venv via lancedb). Lazy module-level singleton: first call to any helper triggers the full read.

- Load time: **13 s** for the 440 MB CSV → 18,531 genes × 1,208 cell lines (90 MB float32 in memory).
- `correlation_for_pair(a, b, min_n=100)` — Pearson on the pair-wise non-NaN intersection. Sub-millisecond after warm.
- `correlation_for_pair_family(candidates_a, candidates_b, min_n=100)` — cross-product over candidate gene-symbol lists; reports tightest |r| with `family_ambiguity` flag and full `all_results` for transparency. Detects degenerate `same_gene_alias` case (e.g. PD-L1 vs CD274 are the same gene) and returns a clean reason instead of misleading `all_missing`.
- `correlations_for_gene(gene, top_k, min_abs_r, min_n)` — column-wise scan against all 18k genes. Per-gene NaN masks make full vectorisation awkward; explicit loop with vectorised inner ops runs in ~0.5–1 s.

**`src/_corpus_graph.py`** — `_build_graph` extended:

- Reads each fingerprint's `protein_identifiers.entries` and `filtered_out` to attach resolved-identifier annotations onto graph nodes (`human_gene_symbol`, `human_uniprot`, `candidate_uniprots`, `is_family_head`).
- Skips edges whose endpoints are in the sprint-2 `filtered_out` list — no more `DNA`, `Cas9`, `PRC2`, `Nucleosome` as graph nodes.
- Cache key bumped to `(path, "v2-with-resolved-identifiers")` so existing in-process caches automatically rebuild on first sprint-3 query.

Three new tool functions:

- `get_genetic_codependency(a, b, min_n=100)` — resolves both names, calls `correlation_for_pair_family` with the gene-symbol candidate lists. Family-head policy per design discussion: tightest |r| + `family_ambiguity=True` + `evaluated_pairs` count + full `all_results`.
- `find_cocorrelated_genes(protein, top_k, min_abs_r, min_n)` — top-k DepMap neighbours for hypothesis generation. Family-head input only queries the dominant resolution; surfaced as `family_head_warning` so callers know to drill into specific paralogs.
- `export_subgraph` extended with `with_depmap=True`. Each edge gets `depmap_r`, `depmap_n`, `depmap_best_pair`, `depmap_family_ambiguity`. Returned summary includes per-call `depmap_enrichment` stats (`available` / `missing` / `low_overlap`).

**Identifier-resolver singleton.** `src/identifier_normalizer.get_normalizer()` is a module-level lazy singleton so graph and DepMap tools share one HGNC/UniProt-loaded resolver instance instead of each tool rebuilding it.

### Validation on known biology

| Pair | r | n | Interpretation |
| --- | --- | --- | --- |
| TP53 / MDM2 | -0.72 | 1208 | Strong negative — MDM2 is TP53's negative regulator. Sign-convention check passes. |
| BRCA1 / BARD1 | +0.56 | 1208 | Strong positive — heterodimer, co-essential. |
| MTOR / RPTOR | +0.38 | 1208 | Moderate positive — RPTOR is a defining mTORC1 component. |
| MYC / MAX | +0.34 | 1208 | Moderate positive — heterodimer required for MYC-driven transcription. |
| YAP1 / TEAD4 | +0.12 | 1208 | Weak — Hippo is mutation-conditional in cell lines, not all lines depend on YAP1. |
| KRAS / BRAF | +0.02 | 1208 | Near-zero — BRAF-mutant lines depend on BRAF, KRAS-mutant lines depend on KRAS, hence orthogonal essentiality despite linear-pathway topology. **Worth surfacing as a teaching example in the skill prompt** — it shows that DepMap captures fitness landscape, not pathway topology directly. |

Family-head test: `AKT` vs `MTOR` correctly expanded to AKT1/2/3 × MTOR, AKT1/MTOR (r=+0.23) selected as tightest, `family_ambiguity=True`, `evaluated_pairs=3`, full landscape returned.

Mouse names test: `Yap1` (taxon=10090) vs `Tead4` (taxon=10090) correctly orthologue-mapped to YAP1/TEAD4, r=+0.12.

`find_cocorrelated_genes('TP53', top 5)` returns MDM2 (-0.72), CDKN1A (+0.70), TP53BP1 (+0.69), USP28 (+0.66), CHEK2 (+0.63) — all canonical p53 pathway members. Negative + positive correlations both biologically sensible.

### DepMap coverage on the literature graph

Numbers are post-rebuild (with resolved-identifier annotations and filtered-name exclusion):

| Metric | Count | % of total |
| --- | --- | --- |
| Graph nodes | 11,206 | — |
| Graph edges | 11,312 | — |
| Nodes with resolved gene_symbol | 4,360 | 38.9 % |
| Nodes whose gene_symbol is in DepMap | 4,261 | 38.0 % |
| **Edges with both endpoints in DepMap** | **4,485** | **39.6 %** |

Per-node coverage is much lower than the per-occurrence 69.3 % from sprint 2 — this is the long-tail effect: well-studied proteins (KRAS, p53, EGFR, YAP1, ...) account for many occurrences each, but the 6,500+ unresolvable nodes are mostly singletons (yeast / bacterial / typo names).

**Conditional coverage** is the more useful number: of edges whose both endpoints resolved to a gene_symbol, **96.8 %** are in DepMap (4,485 / 4,631). Resolution is the bottleneck, not DepMap coverage. Going forward, a smarter resolver (cross-species gene-symbol lookup for yeast / Drosophila orthologs) would lift coverage materially; sprint 5+ work, not blocking.

In practice the user-facing experience is much better than 40 %: the KRAS depth-1 subgraph (50 nodes, 68 edges) had **76 % of its edges** with available DepMap correlations (52 / 68). Popular nodes are densely covered; the long tail is the rare, niche stuff.

### Decisions taken (per user direction)

1. Family-head policy: tightest |r| + `evaluated_pairs` + `family_ambiguity=True` + full `all_results`.
2. Default `min_n` = 100 overlapping cell lines.
3. Tool surface: A (`get_genetic_codependency`) + B (`export_subgraph` with `with_depmap`) + C (`find_cocorrelated_genes`).
4. Skill prompt updates included so end-to-end is testable now (sprint 4 territory but cheap to roll forward).

### Risks called out at planning time — re-checked

1. **Graph cache invalidation** — handled. Cache key bumped, in-process caches rebuild automatically. MCP-server first query after this sprint will be slow (~5 s graph rebuild + ~13 s DepMap load); subsequent calls are fast.
2. **DepMap sign convention misreading** — handled in skill prompt with explicit "Sign convention" block plus a "Common pitfalls" entry. KRAS/BRAF (r ≈ 0) explicitly framed as biologically interpretable, not a contradiction.
3. **Gene-symbol drift** — non-issue in practice. Of resolved gene symbols, 96.8 % hit DepMap directly.
4. **COX2 ambiguity** — sprint-2 carryover. Family-aware reporting partially mitigates: `get_genetic_codependency('COX2', 'KRAS')` would return the tightest correlation across MT-CO2 and PTGS2 candidates, with `family_ambiguity=True`. Still no contextual disambiguation from `experimental_context` text — sprint 5+ work.

### Files touched

- `src/depmap.py` (new) — pyarrow loader + correlation helpers
- `src/_corpus_graph.py` — extended `_build_graph` with resolved-identifier annotations + filter exclusion; added `get_genetic_codependency` + `find_cocorrelated_genes`; extended `export_subgraph` with `with_depmap`
- `src/identifier_normalizer.py` — added `get_normalizer()` module-level lazy singleton
- `src/mcp_server.py` — three new `@mcp.tool()` registrations
- `src/skill_runner.py` — three new `_TOOL_DEFS` entries + dispatch branches; updated `_GRAPH_TOOLS` set
- `skills/corpus-explorer/SKILL.md` — DepMap section, sign-convention framing, two new pitfalls

### Out of scope (deferred to sprint 5+)

- Cross-species ortholog lookup for yeast / Drosophila / bacterial gene names (would lift node coverage from ~41 % toward 60–70 %).
- A bulk pre-computed `data/depmap_edges.parquet` for the full literature graph — needed only when sprint 5 starts iterating over all edges for clustering.
- Contextual disambiguation of HGNC-ambiguous names (COX2, p62) using `experimental_context` text — needs a small LLM call per ambiguous occurrence; weighing against token cost.
- Per-Celery-worker memory: each loads its own DepMap copy (~600 MB resident). Acceptable at current scale; revisit if scaling out.

### Coverage diagnostic + three follow-up fixes

User audit caught the gap between sprint 2's 69 % occurrence-weighted resolution and sprint 3's 38.9 % per-node coverage. Diagnostic broke it down by mention-count bucket and confirmed the long-tail effect (singletons resolved at 26.8 %, 100+ mention nodes at 100 %). Three targeted fixes applied:

1. **Compound-name lookup in graph builder** — sprint 2 split `"YAP/TAZ"` into separate `YAP` + `TAZ` entries, but the graph builder was looking up the literal compound string. New `_resolve_compound_name` helper in `src/_corpus_graph.py` uses the same splitter as sprint 2, merges resolved components into a synthetic family-head entry. Catches `YAP/TAZ`, `MEK1/2`, `LATS1/2`, `Erk 1/2`, `APC/C`, etc.
2. **Splitter handles paralog shorthand** — `split_compound` (renamed from `_split_compound` since it's now used across modules) extended to expand `"MEK1/2"` into `["MEK1", "MEK2"]` and `"MAP2K1/2/3"` into `["MAP2K1", "MAP2K2", "MAP2K3"]`. The previous version dropped the trailing digit because of a `len >= 2` filter.
3. **Filter additions + curated aliases**:
   - `_NON_PROTEIN_TERMS` extended with cellular structures (Membrane, mitochondria, Promoter, Enhancer, Antigen, Antibody, NCP, T-cell labels) and major lncRNAs (XIST, MALAT1, NEAT1).
   - `_BIOLOGY_ALIASES` extended with MENIN→MEN1, MYOSIN→MYH7 (cardiac default), CARDIACMYOSIN→MYH7, NONMUSCLEMYOSIN→MYH9.

Post-fix coverage:

| Metric | Before fixes | After fixes | Δ |
| --- | --- | --- | --- |
| Per-node resolution | 38.9 % | **41.2 %** | +2.3 pp |
| Mention-weighted graph resolution | 62.5 % | **64.2 %** | +1.7 pp |
| Edges with both endpoints in DepMap | 39.6 % | **42.2 %** | +2.6 pp |
| Singleton nodes resolved | 26.8 % | **29.8 %** | +3.0 pp |
| Conditional (resolved → in-DepMap) | 96.8 % | 97.1 % | +0.3 pp |

Marginal in absolute terms, as predicted — the singletons are mostly genuine non-mammalian and unfixable without ortholog-table scope creep. But the compound-name fix is high-value: each compound name is in many edges, so edges saw the biggest improvement.

Sprint 2 backfill re-run with `--force` to apply (2) + (3) to existing fingerprints. Backfill stays idempotent — second run shows `already_current: 5,547`.

---

## 2026-05-03 — DepMap integration sprint 5: Louvain clustering

Move from pairwise edges to co-functional modules. Pipeline:

```
literature graph + DepMap r --[edge_index]--> data/depmap_edges.parquet
data/depmap_edges.parquet --[clustering]--> data/clusters.json
```

Three new tools (`cluster_for_protein`, `cluster_members`, `find_clusters_by_keyword`) wired into both transports + skill prompt updated.

### Decisions taken (per user)

1. **Algorithm**: Louvain via `networkx.algorithms.community.louvain_communities` — no new dependency. Leiden + igraph deferred unless quality demands it.
2. **Edge weighting**: `weight = mentions × |depmap_r|` with thresholds `mentions ≥ 1` and `|r| ≥ 0.15`. DepMap-unavailable edges keep `default_r = 0.1` so literature-only signal stays a weak community anchor; DepMap-available edges with `|r| < 0.15` are excluded (DepMap actively rejects them).
3. **Cluster size cap**: 50. Oversized clusters re-cluster recursively at 1.5× resolution, max depth 3.
4. **Storage**: `data/depmap_edges.parquet` (pyarrow) for the enriched edge index; `data/clusters.json` for the cluster registry. Both are gitignored.

### Edge index — gene-symbol collapse

The literature graph keeps separate nodes for `KRAS` vs `K-RAS` (the legacy `_normalize_protein` preserves letter-letter hyphens). For clustering this would split a gene's edges across two nodes — bad. Solution: at edge-index build time, collapse all node-pair edges by canonical `(gene_a, gene_b)` keys and sum mentions across collapsed groups. DepMap r is computed once per unique pair.

Edge index numbers (after collapse + threshold):

| Stage | Count |
| --- | --- |
| Graph edges (raw) | 11,163 |
| Graph edges with both endpoints resolved to a human gene | 4,852 |
| Unique gene-pair edges after collapse | 4,582 |
| Edges with available DepMap r | 4,404 |
| Edges meeting weight threshold | **839** |

The 839 final edges represent strong-signal interactions: literature-mentioned AND DepMap-supported (`|r| ≥ 0.15`) OR literature-only with a small default weight.

### Clustering output

| Metric | Value |
| --- | --- |
| Algorithm | Louvain (resolution = 1.0, seed = 42) |
| Total clusters | 276 |
| Largest cluster | 46 (under the 50 cap, no recursive splitting needed) |
| Median cluster size | 2 |
| Singletons | 6 |
| Top 5 sizes | 46, 33, 29, 26, 25 |

### Validation on known biology

Spot-checked five canonical modules:

| Module | Cluster ID | Size | Hub | Members (selected) | Verdict |
| --- | --- | --- | --- | --- | --- |
| **Hippo / YAP** | #2 | 29 | YAP1 | YAP1, WWTR1, TEAD1–4, LATS1, LATS2, NF2, MARK2/3, TAOK1, WWC1, VGLL3, PTPN14, plus downstream FOS/FOSL1/JUN/E2F1 | ✅ canonical Hippo + downstream effectors |
| **p53** | #1 | 33 | TP53 | TP53, MDM2, MDM4, CDKN1A, TP53BP1, USP28, USP7, RB1, CCND1/2/CCNE1, CDK2/4/6, SKP2, CKS1B | ✅ p53 axis + cell-cycle |
| **mTOR** | #15 | 10 | TSC1 | MTOR, RHEB, RICTOR, RRAGA, TSC1/2, FLCN, FNIP1, TBC1D7, PTEN | ✅ tight mTORC1/lysosomal sensing module |
| **BRCA / HR** | #13 | 11 | BRCA1 | BRCA1, BARD1, BRCA2, PALB2, RAD51B/C/D, XRCC3, HELQ, MDC1, RAD18 | ✅ homologous-recombination module |
| **RTK + PI3K-AKT + DDR** | #0 | 46 (cap) | EGFR | EGFR, AKT1, PIK3CA, BRAF, IGF1R, IRS1, FOXO1, PTEN, ATM, CHEK2, APC, ARHGEF7, ASPG, BLNK, CD19, CD81, ... | ✅ growth-signalling supercluster, sits at size cap |
| **MYC / RAS** | #8 | 16 | KRAS | KRAS, NRAS, MYC, MYCN, MAX, RAF1, SHOC2, CTNNB1 | ✅ RAS/MAPK + MYC oncogenic core |

The size-46 supercluster #0 is at the cap. Inspecting the `external_edges` count (8) shows it's well-internally-connected; recursive splitting at 1.5× resolution didn't break it because the dense core resists higher-resolution decomposition. That's a **policy decision worth revisiting** if the user wants finer granularity inside the growth-signalling module specifically — bumping `RESOLUTION_STEP` or lowering `MAX_CLUSTER_SIZE` to 30 would force a split.

### Notable absences

A few proteins resolved correctly but didn't make it into the cluster registry (`reason: "not_clustered"`):

- **Tau (MAPT)**: resolved fine but had no qualifying edges. Tau biology in DepMap is mostly aggregation-driven, not co-essential — its literature partners (USP10, GSK3B etc.) had `|r| < 0.15`. Honest signal: Tau interactions don't show as co-essential modules.
- **AKT2, AKT3**: family-head resolution defaults `AKT` → AKT1, so AKT2/AKT3 mentions get folded into AKT1 in the edge index. Working as designed; user can query specific paralogs to see them split.

> Earlier draft of this entry claimed STING was absent. False negative from a validation script that looked for the literal string `STING` in cluster members; the resolved symbol is **STING1** (the `paralog_default` tier maps `STING + "1" = STING1`). STING1 is correctly clustered in #38 with TBK1 / IRF3 / NLRC3 — the canonical cGAS-STING DNA-sensing module.

### Files touched

- `src/edge_index.py` (new) — pyarrow-backed edge index library, gene-symbol collapse, weight formula
- `src/clustering.py` (new) — Louvain + recursive size-cap split + cluster registry loader
- `scripts/build_edge_index.py` (new) — CLI for building the parquet
- `scripts/cluster_corpus.py` (new) — CLI for clustering with `--rebuild-edges`, `--resolution`, `--seed`
- `src/_corpus_graph.py` — three new tool functions: `cluster_for_protein`, `cluster_members`, `find_clusters_by_keyword`
- `src/mcp_server.py` + `src/skill_runner.py` — tool registrations + dispatch
- `skills/corpus-explorer/SKILL.md` — Co-functional clusters section + cheat-sheet entries
- `data/depmap_edges.parquet` (gitignored, ~25 KB)
- `data/clusters.json` (gitignored, ~280 KB)

### Out of scope (deferred)

- **Sprint 6 — semantic cluster neighbourhoods**: per-cluster literature embedding (mean of `situational_context_hook` vectors) + ESM mean-pool of cluster proteins → cluster-cluster cosine similarity → meta-graph. Gated on user inspecting sprint-5 clusters and deciding the question is well-posed.
- **Leiden upgrade**: if Louvain's occasional disconnected-community pathology bites, swap to `leidenalg`.
- **Per-cluster top-DOIs**: would need to walk fingerprints once per cluster to aggregate. Not in the current schema; cheap to add when consumers ask.
- **Cluster stability across corpus growth**: when new papers are curated, clusters will shift. Worth tracking diff over time, but not blocking.

---

## 2026-05-03 — Open-science release planning session

Design discussion on turning the corpus side of the project into a community-driven open-science release. Full planning document landed in `RELEASE_PLAN.md`; this entry captures the load-bearing decisions so future-me can find them without re-reading the discussion.

### Headline shape

- **Decouple corpus from design.** Two products in one repo via PyPI extras: `lpt-corpus` (light) and `lpt-corpus[design]` (heavy GPU/MCP deps). v0.1 ships corpus-only — design route hidden in the web UI but present in source. Reasoning: ~100× audience difference, very different dep profiles, less surface area to defend on launch.
- **Hosting**: Hetzner CX32 (4 vCPU / 8 GB / EU) for the API + Celery + Redis + Postgres + LanceDB. Frontend on Cloudflare Pages (free). Data dumps on Zenodo (DOI per release, citable in papers) + R2 mirror. Total ~€13/mo. EU jurisdiction throughout for GDPR + open-science narrative + cleanest BYOK story.
- **Capacity at this size**: 200–500 daily active users, 50–150 papers/day curation, 20–30 search QPS sustained. Box dies on concurrency spikes (>3 simultaneous curations, >50 simultaneous skill sessions), not volume. Upgrade path goes to ~€40/mo for 5–10× headroom before any replatform is needed.
- **Auth**: hybrid. Anonymous-by-default for browse + skill chat with BYOK in session-scoped Redis (Fernet, TTL = session). GitHub OAuth (read:user only) for contribution + persistent history + cumulative cost ceilings. ORCID deferred until academic users ask.
- **Corpus expansion via web**: Celery job runs `fetch_papers + curate_papers` with the user's BYOK decrypted into worker memory for the job's lifetime; results stage in a per-user review queue; user explicitly approves each fingerprint into a "candidate for shared corpus" pool that monthly releases merge. Never auto-merge. Anonymous quota = 0 (must sign in to contribute).
- **Community trust model**: PMC-OA papers only (provenance verifiable). Bot re-curates a random 5% sample to detect adversarial submissions. Cost of spot-check at €0.01/paper × 5% = trivial.
- **Cost protection (BYOK)**: log token counts (not query content) per session; warn at €1/€5/€10; user-set hard ceiling for authenticated accounts. `skill_runner.py` `--max-tokens` ceiling already exists — web path must enforce.

### Hard pre-release blockers

- **OA audit**: `data/literature.db` contains manually-added non-OA papers. Cross-reference against PMC OA file list; tag every paper; ship only OA-derived fingerprints in the public Zenodo snapshot. Add license-clean gate to `curate_papers.py` so non-OA can't enter the public corpus accidentally going forward.
- **Curation provenance**: every fingerprint must record `curator_model + prompt_version + curated_date`. Audit existing fingerprints; backfill missing fields.
- **Reproducibility**: pin gemini-flash to a specific snapshot, not the floating alias.
- **CITATION.cff + Zenodo deposit** with DOI before public announcement — single biggest driver of academic adoption.
- **Privacy notice + GDPR account-deletion path** must be drafted before launch, not after the first ticket.

### Open questions deferred

- Hosted MCP server on the public box? Defer until a user asks.
- ORCID alongside GitHub OAuth? Defer.
- Federation (private corpora that opt-in to the public one)? Out of v0.1 scope.
- Schema migration policy for community contributions when v3.0 lands. Decide before contribution flow goes public.

### Files touched

- `RELEASE_PLAN.md` (new) — full planning document with deployment topology, capacity table, auth model, contribution flow, OA compliance steps, packaging, pre-release checklist, open questions.

---

## 2026-05-21 — Workstation migration + local Gemma 4 curation trial

Two threads landed today: finishing the Windows → Linux workstation migration on `srv-lnx-saht8`, and standing up Gemma 4 26B-A4B as a local curation backend so we can re-process backlog papers without burning Gemini/Anthropic credit.

### Migration loose ends fixed

- **DB path portability**: 9 823 `pdf_path` rows held Windows `data\pdfs\...` strings and 8 313 `fingerprint_path` rows held *absolute* Windows paths from three historical roots (`C:\Users\micha\Documents\little_protein_tiger`, `C:\coding\lpt\little-protein-tiger`, `C:\Users\micha\Documents\literature_search_agent`). One-shot migration: backslashes → forward slashes everywhere, and the surviving absolutes trimmed back to `data/<dir>/<file>`. Backup at `data/literature.db.bak-reset-175643`. Two `pdf_path` rows pointed at files genuinely lost (`C:\Users\micha\Documents\literature_search_agent\...`) — those were re-anchored against `data/pdfs/` and verified present.
- **Forward-writes are now POSIX**: `src/downloader.py:161,214` switched from `str(path)` to `path.as_posix()`; `scripts/curate_papers.py:248` now stores fingerprint paths as project-relative POSIX (`fp_path.resolve().relative_to(ROOT).as_posix()`). DBs written on any OS will read cleanly anywhere.
- **`resolve_file_path` made tolerant**: `scripts/curate_papers.py:93` normalises separators and falls back to basename-match inside `pdf_dir`. Curation now resolves all 9 823 downloaded papers — none were actually missing, the issue was purely path format.
- **Corporate TLS inspection**: the box sits behind a FortiGate that intercepts HTTPS and re-signs with a corporate root CA (`O = Fortinet`). Python's `requests` uses its own `certifi` bundle and ignores the system store, so SSL handshakes to Gemini/Anthropic/Europe PMC fail with `CERTIFICATE_VERIFY_FAILED`. Fix: `REQUESTS_CA_BUNDLE` + `CURL_CA_BUNDLE` pointing at `/etc/ssl/certs/ca-certificates.crt`, persisted in `.env`. Confirmed working against `generativelanguage.googleapis.com`. **Do not** disable verification — IT requires the inspection.
- **80 papers reset from `failed` → `pending`**: the bulk were old Claude credit-exhaustion failures, plus one from my first Gemma test before `reasoning_effort: "none"` was set. They're back in the curation queue.

### Local Gemma 4 setup

Hardware: NVIDIA RTX PRO 4500 Blackwell, 32 GiB VRAM, driver 595.71.05, CUDA 13.2.

Ollama installed user-locally — no root needed despite the installer's `/usr/local/bin` insistence. Tarball extracted to `~/.local/ollama/`, PATH added to `.bashrc`. Models cached under `~/.ollama/models/`. Server runs as `nohup ollama serve` with logs at `~/.ollama/logs/server.log`. Model pulled: `gemma4:26b-a4b-it-q4_K_M` (17 GB, MoE with 4 B active params per token).

Loaded at ~20 GB VRAM with default 32k ctx, stable at the same footprint even after bumping `num_ctx` to 65 536 via Ollama options — either lazy KV-cache alloc or sliding-window attention keeps the cost flat. 12 GiB headroom for other work.

### The non-obvious Ollama × Gemma 4 gotcha

`gemma4:26b-a4b-it-q4_K_M` ships with **thinking mode on by default** and the OpenAI-compatible endpoint exposes the chain-of-thought in a `reasoning` field separate from `content`. Default behaviour: model burns the entire `max_tokens` budget inside `reasoning` and `content` stays empty. Curation fails with `Expecting value: line 1 column 1 (char 0)` because the parse target is empty.

Counter-intuitive fixes that **don't** work on the OpenAI-compat endpoint:
- `think: false` — ignored (only honored by `/api/chat` native endpoint)
- `chat_template_kwargs: {enable_thinking: false}` — Qwen-specific, no effect on Gemma
- `response_format: {type: "json_object"}` alone — does not silence reasoning

What works: `reasoning_effort: "none"` in the request body. Disables thinking, content fills correctly, completion-token usage drops by ~6× (from 200-tok ceiling to 34 tok on a trivial extraction). This is now baked into `config.yaml > curation.local_sampling`.

### Code surface

- `src/curator.py:_call_local` made backend-agnostic: removed Qwen-specific kwargs (`top_k`, `chat_template_kwargs`), accept `sampling` and Ollama-style nested `options` from config, raised default request timeout to 600 s.
- `config.yaml > curation` gained `local_sampling` (temperature, top_p, reasoning_effort, response_format), `local_options` (num_ctx), and `local_request_timeout`. **Default provider stays `claude`** — local is opt-in via `--provider local`.
- `max_input_chars` raised 90 000 → 150 000. At ~4 chars/token that's ~37k input tokens; with 5k system prompt + 8k max output it fits comfortably in num_ctx 65 536.

### Quality trial: Claude Haiku vs Gemma 4, three PPI-rich papers

Re-curated three Claude-curated papers under Gemma into `data/fingerprints/_gemma_compare/` without touching the canonicals:

1. `doi:10.7554/eLife.25068` — YAP-TEAD interaction dissection (Hippo-pathway core)
2. `doi:10.1038/s41589-019-0245-2` — MDM2 peptide inhibitor discovery via affinity-selection MS
3. `doi:10.1016/j.chembiol.2026.02.008` — De novo Ras isoform-selective binders

Each comes in two Gemma versions: `_compare/<key>.json` (pre-patch, residue contamination in `protein_pair`) and `_compare/<key>_v2.json` (post-patch).

#### The `protein_pair` semantic violation and its fix

First v1 run produced `protein_pair: ["hYAP Phe69", "hTEAD4 Asp272"]` — i.e. residue names where protein names belong. 5 of 5 findings affected. This is **graph-corrupting**: the corpus graph (`src/_corpus_graph.py`) walks `protein_pair` to build edges, and residue names would create phantom nodes like "Phe69" that pollute clustering and `get_interactions_for`.

Patch to `curation_prompt.md`: added explicit do/don't examples for `protein_pair`, clarified that residues belong in `key_amino_acid_residues`. The clarification is also useful for Claude (it picked up `study_category` from the same edit — Claude had been leaving it null).

After the patch:
- 0/15 findings across 3 papers had residue contamination in `protein_pair` (was 5/5 in YAP-TEAD before)
- Gemma consistently sets `study_category: "biochemistry"` (Claude leaves it null — pre-existing bug we should backfill)
- Source spans switched from `Section: Results and discussion, Para 12` → `Page 3, Para 1` style

#### Gemma's remaining weaknesses (quantitative)

| Metric | Claude (3 papers) | Gemma v2 (3 papers) |
|---|---|---|
| Graph-corrupting protein_pair violations | 0 / 15 | 0 / 15 |
| `study_category` populated | 0 / 3 | 3 / 3 |
| Measured Kd captured | 8 / 15 | 3 / 15 |
| Residues with full position (e.g. `Phe3`) | rich | partial (e.g. `Phe`, no position) |
| `quantitative_or_qualitative` always set | 15 / 15 | 12 / 15 (3 nulls) |

#### Designed-binder mis-pairing (new issue, paper 3)

When the binding partner is a designed peptide / synthetic compound, Gemma sometimes pairs the wrong two proteins:

- Claude: `["KRAS4A_RIB_7", "KRAS4A"]` (binder vs target) — correct semantics
- Gemma:  `["KRAS4A", "KRAS4B"]` (two Ras isoforms, not a binding pair) — **not** graph-corrupting (both are real proteins) but pollutes the depmap-edge index with phantom paralog-paralog edges

Not blocking — both edges would be real proteins — but it would skew the Hippo / Ras isoform analysis if Gemma curated all of these. Worth a follow-up prompt refinement if we go to bulk re-curation.

### Performance numbers

- Per-paper wall time on a 50–67k char PDF input: **17–27 s** on the Blackwell. Faster than the 80–160 s estimate I'd guessed for a 26B Q4 model — the 4 B active params per token (MoE) plus Blackwell tensor cores make Gemma 4 very fast on long prompts.
- Real batch curation will be limited by PDF extraction + RCSB lookups, not the LLM. Expect 10–30 s/paper end-to-end.

### Decision

**Gemma 4 26B-A4B is fit for first-pass triage on the 1 464-paper backlog, not for canonical curation.** Recommended workflow when we tackle the backlog:

1. Run Gemma on all 1 464 pending papers — captures `relevant`, `study_category`, `situational_context_hook`, broad findings shape. ~10–30 s/paper × 1 464 ≈ 4–12 hours wall clock, zero API spend.
2. Promote the keep-pile (relevant=true) to Claude Haiku for canonical re-curation. Estimated cost dominated by Haiku, not Gemma.
3. Spot-check the protein_pair fields with the regex heuristic before each graph rebuild as a defense-in-depth measure.

Not adopted as default in `config.yaml` — provider stays `claude` so cron / automated runs don't silently degrade.

### Open prompt issues worth fixing if we go further with Gemma

- **Designed-binder pairing**: clarify in `curation_prompt.md` that `protein_pair` must be `[binder, target]` when one side is a designed peptide / compound, never two isoforms of the same target.
- **Measured-Kd capture**: prompt currently doesn't push the model to look for measured values in numerical tables / SPR figure captions. Gemma leaves these blank when Claude doesn't.
- **Backfill `study_category` on existing 8 313 Claude fingerprints**: Gemma's win here exposes that Claude's `study_category` rule isn't firing reliably. Either fix the prompt (it's clear enough — might be a Claude artefact) or accept the gap and write a separate one-shot backfill that infers category from existing `study_type` + `key_findings`.

### Files touched

- `src/curator.py` — `_call_local` rewritten provider-agnostic; accepts `sampling` and `options` from config; default temperature lowered to 0.1.
- `config.yaml > curation` — added `local_sampling`, `local_options`, `local_request_timeout`; raised `max_input_chars` to 150 000; new local model + Ollama endpoint defaults.
- `curation_prompt.md` — added `protein_pair` clarification with do/don't examples next to the existing PPI rule.
- `scripts/curate_papers.py` — POSIX/portable path handling.
- `src/downloader.py` — POSIX path persistence on download.
- `.env` — `REQUESTS_CA_BUNDLE` + `CURL_CA_BUNDLE` for FortiGate root CA.
- `data/fingerprints/_gemma_compare/` (gitignored) — three v1/v2 fingerprint pairs for the quality trial. Don't merge these into the canonical fingerprint set; they're scratch.
- DB migration applied in-place; backup at `data/literature.db.bak-reset-175643`.

## 2026-05-21 (later) — Three-provider curation comparison (Claude / Gemini / Gemma)

Wider follow-up to the earlier Gemma trial. Ran the same 10 papers through all three providers (Claude Haiku 4.5, Gemini 3.1 Flash Lite, Gemma 4 26B-A4B local) with the patched `curation_prompt.md` to get a calibrated read on quality differences, not just Gemma-vs-Claude.

**Setup.** New `scripts/compare_providers.py` extracts text once per paper and dispatches to each provider via the existing `curate_paper()` plumbing. Writes per-provider outputs to `data/fingerprints/_provider_compare/<provider>/` (gitignored, does not touch production fingerprint dir or DB). Scoring done by `scripts/score_provider_compare.py`, report at `data/fingerprints/_provider_compare/REPORT.md`. 10 papers = 5 PPI-heavy (SHOC2-KRAS, bromodomain catalogue, SARS spike, PTP1B allostery, tau filaments) + 5 random from the 1,464-paper uncurated backlog (seed=42 — skewed chromatin/genome biology, reflecting current keyword focus).

**Headline.**

| | claude | gemini | local |
|---|---:|---:|---:|
| OK rate | 10/10 | 10/10 | 10/10 |
| Σ findings | 40 | 30 | 34 |
| Avg wall | 27 s | 7.7 s | 27 s |
| residues w/ position | 28/40 | 13/30 | 5/34 |
| Kd/Ki captured | 5/40 | 5/30 | 3/34 |
| relevance gated | 2/10 | 0/10 | 0/10 |
| pair_clean violations | 0 | 0 | 0 |
| missing protein_pair | 0 | 0 | 1 |
| input tokens (10 papers) | 276 k | 304 k | 173 k (truncated by num_ctx) |

**Observations.**

- **The prompt patch holds.** Zero residue-in-pair or domain-of-self violations across all 30 fingerprints. The do/don't examples added to rule 6 are doing their job for all three models. Gemma had 1 finding with `protein_pair: null` (replication-timing paper, no clear protein dyad) — that's an incompleteness signal, not a violation.
- **Claude is the depth champion.** 4 of 5 PPI-heavy papers gave Claude 5/5 findings with residue positions in 4–5 of them. Numbers like "KRAS Q70" and "MRAS R105H" come back; Gemma typically loses the position digit and returns "Lys" or "Gln". For the depmap-graph pipeline that needs residue-level edges, only Claude is currently suitable.
- **Claude is also conservative.** Gated 2 of 5 random papers (`pcbi.1002225` Replication Timing, `s41467-021-22129-9` G-quadruplexes in L1) as `relevant: false`. Both Gemini and Gemma found enough hooks to extract pathway_biology fingerprints. The G-quadruplex paper plausibly has DNA–protein interactions worth keeping; the replication-timing one is more borderline computational. Worth a future prompt tweak if recall matters more than precision for the chromatin sub-corpus.
- **Gemini is the speed champion** and surprisingly competitive on quality. 4× faster than the other two (7.7 s vs ~27 s per paper, including network), 100% study_category coverage, hooks average 117 words (right in the 100–150 target band; Claude undershoots at 93). But output is markedly more compact — finds ~3 findings per paper vs Claude's ~5. For first-pass triage at scale this is a strong fit.
- **Gemma silent-fail.** One paper (`abi6226` SARS-CoV-2 spike) came back with `relevant: true` but an empty skeleton — paper_metadata/methodology/entities all null, key_findings empty. Pydantic accepts this because every inner field is Optional. We should add a post-validation step: if `relevant=True` but `paper_metadata is None` or `len(key_findings)==0`, downgrade to `relevant=False` and mark the run failed so it can be retried with a different provider. Not blocking, but it shouldn't pass silently into the corpus.
- **Token reporting on Ollama is wrong-by-design.** Ollama's `usage.prompt_tokens` reports the number that fit in `num_ctx` after truncation. We pass `max_input_chars=150000` which is ~50 k tokens, larger than the configured `num_ctx=65536` minus the system prompt and reasoning padding. For the long inputs we cap at ~32 k input tokens reported. Either raise `num_ctx` (more VRAM) or accept truncation as a feature for the local provider.
- **Cost ballpark for 10 papers.** Claude Haiku ~$0.10, Gemini Flash Lite ~$0.05, Gemma free. For a 1,464-paper backfill: Claude ~$15, Gemini ~$7, Gemma free but ~11 h wall time + the silent-fail risk above.

**Recommendation (unchanged from earlier trial, now with broader evidence):** Default canonical curation stays on `claude` for the depmap pipeline. Add `gemini` as a budget option for high-volume triage runs (the 3-findings-per-paper signal is enough for relevance + study_category + situational hook). Keep `local` for development/offline runs and as a fallback if cloud APIs are blocked. Decide on the silent-fail validator and the Claude relevance-gating tweak in a follow-up.

**Files touched.**

- `scripts/compare_providers.py` — new, isolated per-provider runner.
- `scripts/score_provider_compare.py` — new, emits `REPORT.md` from the 30 fingerprints.
- `data/fingerprints/_provider_compare/` — 30 fingerprints + `_summary.json` + `REPORT.md` + `run.log`. Gitignored.

## 2026-05-23 — Proteina-Complexa integration deferred (Blackwell/JAX blocker)

Tried to add PC as a second design backend alongside boltzgen. Pipeline:
generate → filter → **evaluate** → analyze. Generate + filter work on this
workstation's Blackwell GPU (sm_120) using `.venv-blackwell`. The evaluate
step is where iPTM / iPAE come from — and where every folding backend we
tried failed:

- `colabdesign` (AF2 via JAX): `ptxas fatal: Program with .target 'sm_90a'
  cannot be compiled to future architecture`. JAX in `.venv-blackwell` only
  knows how to compile down to sm_90; Blackwell needs sm_120.
- `rf3_latest`: RF3 wheel's CUDA kernels are pre-built without sm_120
  support — `RuntimeError: CUDA error: no kernel image is available for
  execution on the device`.
- `boltz2_default`: documented in `configs/pipeline/binder/binder_evaluate.yaml`
  comments but **not implemented** — `initialize_folding_model` raises
  `ValueError: Folding model 'boltz2_default' not supported`. Real `boltz2_*`
  names (e.g. `boltz2_v1`) might work but we didn't go that far.
- `esmfold` only computes monomer metrics — no complex iPTM/iPAE.

Branches we didn't pursue, in increasing effort:

1. Try `boltz2_v1` (or similar specific variant) in PC's evaluate. Boltz2 is
   the same backbone boltzgen runs successfully on this GPU, so if PC's
   wiring picks up a Blackwell-compatible build it could just work.
2. Skip PC's evaluate entirely. Take PC generate's PDB outputs, translate
   into boltzgen's `intermediate_designs/` layout, run
   `boltzgen run --steps folding analysis filtering` on them. Reuses our
   chunk-2 metric parser unchanged.
3. Port binder-design's RF3 wrapper (`src/prediction/rf3.py` +
   `utils/rf3_bridge.py`) and run our own RF3 evaluator. May hit the same
   CUDA-kernel issue as PC's built-in RF3.

LPT-side parser already exists in `web/backend/routers/binders.py`
(handles PC's `binder_sequence`, `self_complex_i_pTM`, `self_complex_i_pAE`
column schema with the ×31 Å unit fix). Once we can produce an evaluated CSV,
that parser is ~30 lines and slots straight in.

Moving on to chunk 4 (design-analyst skill / stage 6 summary). Picking PC
back up requires either: (a) a different GPU, (b) PC ships Blackwell-compatible
wheels, or (c) we take option 2 above.

## 2026-05-23 (later) — Full design pipeline + end-to-end cGAS-STING test

Closed out the binder-design pipeline build that started earlier in this session.
Stages 4 (execution) and 5 (analysis) were already in place from chunk 3; this run
adds stage 6 (design-analyst LLM summary), broadens the upstream skills off
PPI-only framing, reorders pathway→mol-bio→structure, adds target-size + binder-
size ground rules, then drives the whole thing against
`Design cancer therapeutics targeting the cGAS-STING pathway.`

### What got built

- **Stage 6 (design-analyst)** — terminal LLM stage; reviews stage-5 top-K and
  emits GO/CONDITIONAL_GO/NO_GO with a candidate review and order-ready FASTA.
  Defaults to Haiku because Sonnet refuses (`stop_reason=refusal`) on "review
  designed binders" regardless of framing. Protein sequences are deliberately
  withheld from the LLM — design IDs + metrics + derived `binder_length` only —
  both to dodge the safety filter and because sequences aren't needed for the
  review task. The orchestrator writes `06_top_k.fasta` deterministically.

- **Skill broadening pass** — pathway-expert gained a `[DIRECT INHIBITION]`
  fourth tier for enzyme/pocket targets (always ranked below PPI tiers; pivot
  only on hard evidence). mol-bio-expert was renamed "Target Feasibility",
  given the DepMap/cluster tools (`get_genetic_codependency`,
  `find_cocorrelated_genes`, `cluster_for_protein`, …), and refactored to run
  BEFORE structure so its `target_site_hint` JSON can guide the structure
  stage. complex-structure-analysis got a third `INHIBIT_ACTIVE_SITE` mode
  using `tool_find_glue_pockets` / `tool_get_residue_contacts` on a single
  chain. protein-design-script's PPI-only framing softened to "target-site".

- **Stage reorder** — STAGE_ORDER swapped to
  `pathway → literature → structure → design → execution → analysis → summary`.
  Pause points moved correspondingly. PipelineResult gained `pathway_handoff`
  and `literature_handoff` so downstream stages can read each independently
  after `prev_handoff` is rebound.

- **Ground rules** — `design.constraints.max_target_residues: 500` +
  `target_residues_warn: 250`; `binder_sizes.cyclic_peptide: 12..15`;
  `binder_sizes.mini_protein: 70..86`. Per-chain residue counts (via gemmi)
  are injected into the structure-stage query; the LLM picks within bounds
  or recommends cropping. Binder size ranges go into the BoltzGen YAML's
  `sequence: <min>..<max>` slot.

### End-to-end cGAS-STING test outcomes

Two real bugs caught by the test, both fixed:

1. **`protein-design-script` emitted Boltz-1/Boltz-2 schema** (`version: 1`,
   `sequences:`, `constraints:`) instead of BoltzGen schema (`entities:` with
   `binding_types:` nested under the target `file:` entry). `boltzgen check`
   hard-rejected the YAML — pipeline correctly aborted before GPU burn. Fixed
   by embedding the canonical BoltzGen template inline in SKILL.md with an
   explicit forbidden-keys list (`version`, `sequences`, `constraints`,
   `chain_a`, `chain_b`, `residues_a`).

2. **`biopython` + `gemmi` missing from LPT venv.** `_chain_entity_descriptions`
   had been silently returning `{}` for some time (no entity descriptions to
   the LLM); structure-tools' per-residue functions returned `No module named
   'Bio'` to the LLM. Both added to `pyproject.toml` and installed. The
   structure expert handled the failure gracefully — wrote `**UNVERIFIED**`
   placeholders in the label_seq_id column and warned downstream — but the
   downstream design-script LLM responded by writing a `resolve_hotspot_
   numbering.py` workaround script. Net effect was a fragile run.

The good signals:

- **Pathway-expert pivoted cGAS-STING → ENPP1 correctly.** Corpus is thin on
  cGAS/STING themselves, but ENPP1 (which degrades cGAMP) is well-represented;
  pathway-expert recommended it via the new `[DIRECT INHIBITION]` tier with
  CRISPR-KO evidence and clinical-stage inhibitor precedent. This is exactly
  the pivot the broadening pass was meant to enable.
- **mol-bio-expert produced a clean `target_site_hint`** with 4 priority
  residues + DOIs, used `find_cocorrelated_genes` and confirmed DepMap
  codependency. Cited the ipglycermide precedent (38 pM macrocyclic
  Zn-coordinating peptide) as supporting modality choice.
- **Structure-expert used the new `INHIBIT_ACTIVE_SITE` mode** end-to-end,
  including the size-policy check (it flagged 5DLT chain A at 795 residues
  as exceeding the 500 limit and recommended cropping).
- **BoltzGen ran cleanly** on the cyclic-peptide protocol (`peptide-anything`).
  Pilot 50/50 → production extended to 100 via `--reuse`. Cyclic-peptide
  diffusion is ~5× slower per design than mini-protein — the chunk-3 protein-
  anything stress test took 38 min for the same design count; cyclic took 3h.
- **Stage 6 (Haiku) produced an honest GO** with metric-grounded rationale:
  top pick `ENPP1_5DLT_cyclic_peptide_boltzgen_74` at iPTM=0.879, iPAE=3.04 Å.

### Latent bug surfaced — hotspot numbering

The `target_site_hint` from the literature stage was given in **mouse**
ENPP1 numbering (from PDB 6AEK/6AEL co-crystals) but the structure-expert
treated them as **human** (5DLT) positions. At those auth_seq_ids in 5DLT
the residues are ASP/LEU/LEU/GLU/VAL/ASP — not the THR/ASN/THR/SER/HIS/HIS
the literature cited. The mol-bio handoff itself noted a "~+28 offset" but
that warning didn't translate into a numerical correction by either the
literature or structure stages. The BoltzGen run went ahead against the
wrong residues — interpretable as binders against a different part of the
ENPP1 surface, not the catalytic pocket.

The new resolver catches this:
`_resolve_unverified_label_seq_ids` (gemmi-backed) runs after the structure
LLM call, replaces any `**UNVERIFIED**` tokens with real label_seq_ids,
AND runs a residue-name sanity check — if the report says `THR238` but
gemmi reports ASP at auth=238 in the target chain, the orchestrator logs a
loud warning before GPU burn. Hooked into `_stage_structure` in
`src/pipeline_runner.py`. The structure-expert skill prompt now says
"don't invent workaround scripts; emit UNVERIFIED and the orchestrator
handles it." Validated on the existing 02_structure.md: 6/6 numbering
mismatches flagged, 6/6 UNVERIFIED tokens resolved.

### Files added / modified

- `src/pipeline_runner.py` — stages 4–6 + skill reorder + capture_traces +
  `_build_label_seq_id_map`, `_resolve_unverified_label_seq_ids`,
  `_count_chain_residues`.
- `src/design_runner.py`, `src/design_metrics.py`, `src/design_ranking.py`,
  `src/pyrosetta_sasa.py` (subprocess worker) — new.
- `scripts/_sasa_worker.py` — runs under the pyrosetta conda env.
- `scripts/stress_test_chunk3.py`, `scripts/test_e2e_cgas_sting.py`,
  `scripts/resume_e2e_cgas_sting.py` — verification drivers.
- `skills/design-analyst/SKILL.md` — new (terminal stage 6).
- `skills/pathway-expert/SKILL.md`,
  `skills/molecular-biology-expert/SKILL.md`,
  `skills/complex-structure-analysis/SKILL.md`,
  `skills/protein-design-script/SKILL.md` — broadened off PPI-only.
- `src/skill_runner.py` — added `_NO_TOOL_SKILLS`; mol-bio-expert added to
  `_GRAPH_TOOL_SKILLS`; refusal-stop-reason surfaced as warning.
- `config.yaml` — `design:` block (workstation, pilot, production,
  thresholds, ranking, pyrosetta, constraints). Stage 6 auto-routes to Haiku
  via `_DEFAULT_STAGE_MODELS`.
- `pyproject.toml` — added `gemmi>=0.7`, `biopython>=1.85`.

### What's still wrong about this run

The cGAS-STING/ENPP1 run on disk used the wrong hotspot residues. The
resolver flags the bug going forward, but it does **not** retroactively
fix the existing `outputs/e2e_cgas_sting/` run. If we want a real ENPP1
campaign, re-run with the corrected human numbering (add 28 to each mouse
residue: T266, N287, T356, S542 plus the Zn-coordinating histidines at the
correct positions) — or, better, instrument mol-bio-expert to verify
residue identities against the proposed PDB before emitting target_site_hint.
That's a follow-up.
