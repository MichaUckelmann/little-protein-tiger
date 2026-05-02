# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` already documents commands, environment setup, the migration checklist, and the full skill catalogue. This file covers the architectural big picture and project-specific conventions that aren't obvious from a single file.

## Two intertwined systems

The repo combines two pipelines that share a corpus and a set of MCP tools:

1. **Literature corpus pipeline** (`src/` + `scripts/`):
   `fetch_papers.py` → SQLite (`data/literature.db`) → `curate_papers.py` → fingerprint JSONs (`data/fingerprints/`) → `ingest_vectors.py` → LanceDB (`data/vectors/`). Each stage is incremental and idempotent — papers carry `download_status` and `curation_status` columns that gate reruns.

2. **Expert skill execution** (`skills/` + `src/skill_runner.py` + `src/pipeline_runner.py`):
   Each `skills/<name>/SKILL.md` is a system prompt plus tool-call protocol. Skills run in **two modes**:
   - **Claude Desktop / Claude Code**: via the two MCP servers in `.mcp.json` (`literature-db`, `structure-tools`).
   - **CLI / web backend**: via `scripts/run_skill.py`, which loads `SKILL.md` as the system prompt and routes tool calls **directly to Python functions** (no MCP subprocess). Same skill, same tools, different transport.

   `src/pipeline_runner.py` chains skills end-to-end (pathway → structure → literature → design) by parsing `### PIPELINE HANDOFF` blocks out of each skill's markdown output. Editing handoff format in one skill requires updating the consumers.

3. **Web platform** (`web/`): FastAPI + Celery backend (`web/backend/`), React/Vite frontend (`web/frontend/`). Celery wraps `pipeline_runner` as a background task. BYOK API keys are Fernet-encrypted (`web/backend/crypto.py`).

## Skill execution model

- A skill's source of truth is its `SKILL.md` frontmatter + body. The `.zip` siblings in `skills/` are packaged artifacts — regenerate them, don't hand-edit.
- The same `SKILL.md` must work under both transports, so tool names referenced in a skill must exist in **both** the MCP server (`src/mcp_server.py`, `src/structure_tools_server.py`) and the in-process dispatch table inside `src/skill_runner.py`. When adding a tool, wire it in both places.
- `skill_runner.py` enforces a per-call input-token ceiling (`--max-tokens`) and aborts the agentic loop with cumulative-usage telemetry on overrun. Use this — long-running skills can drift into runaway context.

## Path resolution (Windows-specific)

LLMs frequently emit Linux-style absolute paths (e.g. `/data/structures/7d9m.cif`) even on Windows. Both `src/skill_runner.py` and `src/structure_tools_server.py` define a `_resolve()` helper that:
- Passes through true absolute paths (drive-letter on Windows, `/` on POSIX).
- Strips a leading `/` or `\` and re-joins root-relative paths against the project root.

If you add a new tool that takes a file path argument, route it through `_resolve()` — otherwise pathlib silently produces `C:\data\...` instead of `<repo>\data\...` on Windows.

## Curation contract

The fingerprint extraction is governed by `curation_prompt.md` + `extraction_schema.json` (currently schema v2.0). Non-obvious rules that downstream code relies on:

- **Strict provenance**: every claim carries a `source_span` (e.g. `"Page 4, Para 2"`). Tool consumers may reject fingerprints without it.
- **Units are normalised at extraction time**: `affinities_kd_Molar` and `inhibitory_constant_Ki` are floats in **Molar** (not nM/µM). `protein_origin_organism` is an **NCBI taxonomy integer ID** (e.g. 9606). `confidence_score` is 0.0–1.0.
- **Closed enums**: `study_type` and `study_category` are validated against the schema — adding a new category means updating both the schema and any pathway-expert / complex-expert skill prompts that filter on it.
- **Curator output is strict JSON only**, no prose. Parsing in `src/curator.py` will fail loudly otherwise.

## Configuration layering

`config.yaml` is the main config; alternates (`config_search_expansion.yaml`, `config_flagship_journals.yaml`, `config_with_complexes.yaml`) are passed via `--config` to `fetch_papers.py` for targeted searches without polluting the primary keyword list. The current main config is focused on hypertrophic cardiomyopathy / sarcomere biology — keyword sets rotate as research focus shifts.

## MCP launchers (Windows gotcha)

`scripts/launch_mcp.py` and `scripts/launch_structure_tools.py` set `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false`, and `HF_HUB_OFFLINE=1` before importing `sentence_transformers`. Without these, the MCP server hangs at import on Windows. Don't bypass the launcher when registering with Claude Desktop.

`.mcp.json` paths are absolute and **machine-specific** (currently pointing at `C:\Users\micha\Documents\little_protein_tiger\...`, which differs from this checkout's path). When working on this machine, expect MCP servers to potentially be stale until paths are reconciled — `README.md` "Migrating to a new machine" §4 has the canonical fix.

## Common file pairs to keep in sync

- `extraction_schema.json` ⇄ `src/models.py` (Pydantic) ⇄ `curation_prompt.md` — schema, validator, and prompt must agree.
- `src/mcp_server.py` ⇄ `src/skill_runner.py` tool dispatch — same tool surface, two transports.
- `src/structure_tools.py` (pure logic) ⇄ `src/structure_tools_server.py` (MCP wrapper) — server is a thin shim; logic lives in the former.
- `src/pipeline_runner.py` ⇄ each skill's "PIPELINE HANDOFF" output block.

## Frontend

`web/frontend/` is Vite + React 19 + TypeScript. Commands run from that directory: `npm run dev` (port 5173), `npm run build`, `npm run lint`. The backend's `FRONTEND_URL` env var must match the dev server origin for CORS.
