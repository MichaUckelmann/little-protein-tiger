# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` already documents commands, environment setup, the migration checklist, and the full skill catalogue. This file covers the architectural big picture and project-specific conventions that aren't obvious from a single file.

## Two intertwined systems

The repo combines two pipelines that share a corpus and a set of MCP tools:

1. **Literature corpus pipeline** (`src/` + `scripts/`):
   `fetch_papers.py` → SQLite (`data/literature.db`) → `curate_papers.py` → fingerprint JSONs (`data/fingerprints/`) → LanceDB (`data/vectors/`) + edge index (`data/depmap_edges.parquet`) + clusters (`data/clusters.json`). Each stage is incremental and idempotent — papers carry `download_status` and `curation_status` columns that gate reruns.

   `curate_papers.py` self-runs three post-curation hooks at the end of each batch when at least one paper was curated successfully:
   1. **identifier normalization** (`run_backfill`) — adds the `protein_identifiers` sidecar block. Skip with `--skip-normalize`.
   2. **graph rebuild** (`build_and_cluster`) — refreshes `data/depmap_edges.parquet` and `data/clusters.json` so cluster tools see new papers. Skip with `--skip-graph-rebuild`.
   3. **vector ingestion** (`run_ingest`) — embeds new fingerprints into LanceDB so `search_corpus` can find them. Skip with `--skip-vector-ingest`.

   All three are idempotent and skip cleanly when there's nothing to do. The standalone scripts (`normalize_identifiers.py`, `cluster_corpus.py`, `ingest_vectors.py`) remain available for migrations, force-rebuilds, and debugging. The in-process `_GRAPH_CACHE` in `src/_corpus_graph.py` is mtime-invalidated so a long-running MCP server picks up new fingerprints without restart.

2. **Expert skill execution** (`skills/` + `src/skill_runner.py` + `src/pipeline_runner.py`):
   Each `skills/<name>/SKILL.md` is a system prompt plus tool-call protocol. Skills run in **two modes**:
   - **Claude Desktop / Claude Code**: via the two MCP servers in `.mcp.json` (`literature-db`, `structure-tools`).
   - **CLI / web backend**: via `scripts/run_skill.py`, which loads `SKILL.md` as the system prompt and routes tool calls **directly to Python functions** (no MCP subprocess). Same skill, same tools, different transport.

   `src/pipeline_runner.py` chains skills end-to-end (pathway → structure → literature → design) by parsing `### PIPELINE HANDOFF` blocks out of each skill's markdown output. Editing handoff format in one skill requires updating the consumers.

3. **Web platform** (`web/`): FastAPI + Celery backend (`web/backend/`), React/Vite frontend (`web/frontend/`). Celery wraps `pipeline_runner` as a background task. BYOK API keys are Fernet-encrypted (`web/backend/crypto.py`).

## Two design workflows in one orchestrator

`pipeline_runner.run()` drives **two parallel tracks**, selected by the `workflow` arg (`"ppi"` default, `"enzyme"`):

- **PPI / binder track** (the original): `pathway → literature → structure → design → execution(BoltzGen) → analysis → summary`. Stages 0–3 and 6 are LLM skills; 4–5 are deterministic Python.
- **Enzyme track** (`_run_enzyme_track`, `ENZYME_STAGE_ORDER`): `substrate → theozyme → theozyme_diagnose → grafting(optional) → enzyme_design → enzyme_validation`. It is a *fully parallel* sequence, not a branch midway through the PPI run — `run()` dispatches to it at the top when `workflow=="enzyme"`.

Enzyme **compute model = "LPT preps + validates"**: LPT writes ORCA inputs (`src/enzyme_build.py`) and RFD3/LigandMPNN specs deterministically, and validates returned designs (`src/enzyme_validation.py`, ported foundry gates). The heavy ORCA/GPU runs happen **outside LPT** — those hand-offs raise `PipelineExternalStepError` (a `PipelinePausedError` subclass) with `inputs`/`expected_outputs`/`instructions`/`resume_stage`; the user runs the step and resumes via `--start-from <resume_stage>`. Do **not** add ORCA/GPU subprocess runners to the enzyme track. Grafting is optional (skips gracefully with no holo structure); designs are validated/iterated at small-scale pilot before scale-up.

## Persistent project layer

`src/project.py` manages `projects/<slug>/` with an atomic `manifest.json` (the filesystem source of truth for stage/checkpoint state + artifact pointers; `web.db` stays authoritative for the web UI). Layout: `shared/{structures,ligands,orca}/` + `runs/<round-N>/{enzyme,scratch}/`. `PipelineRunner(project=, round_id=)` mirrors stage state into the manifest (`_record_stage`) and computes `run_dir` from it. The binder track works without a project (legacy `outputs/<slug>_<date>/`); the enzyme track requires one (multi-round iteration). When adding an enzyme pause point, set a manifest checkpoint (`_enzyme_checkpoint`) so resume state survives.

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
- **Closed enums**: `study_type` and `study_category` are validated against the schema — adding a new category means updating both the schema and any pathway-expert / complex-expert skill prompts that filter on it. Current categories include `enzymology` / `biocatalysis` / `computational_chemistry` (also listed in `src/vector_store.py`'s `SEARCH_TOOL_DEFINITION` enum and the `search_corpus` def in `src/skill_runner.py`).
- **Nested context blocks**: `pathway_context` (pathway_biology papers) and `enzyme_context` (enzyme/chemistry/comp-chem papers — reactions/EC/SMILES, catalytic residues with roles, kinetics in normalised units kcat s⁻¹/Km M/kcat·Km M⁻¹s⁻¹, QM methods) are each populated only for their categories. **Pydantic drops unknown fields by default**, so any new fingerprint field must be added to the Pydantic models in `src/curator.py` (`EnzymeContext` etc.) or it is silently discarded on `model_dump`, AND folded into `vector_store._build_embed_text` to be searchable.
- **Curator output is strict JSON only**, no prose. Parsing in `src/curator.py` will fail loudly otherwise.

## Configuration layering

`config.yaml` is the main config; alternates (`config_search_expansion.yaml`, `config_flagship_journals.yaml`, `config_with_complexes.yaml`, `config_enzyme_chemistry.yaml`) are passed via `--config` to `fetch_papers.py` for targeted searches without polluting the primary keyword list. The current main config is focused on hypertrophic cardiomyopathy / sarcomere biology — keyword sets rotate as research focus shifts. `curate_papers.py` now also takes `--config` (so an enzyme-corpus run can use a different provider/prompt). For a topical sweep that shouldn't pull the whole pending backlog, `fetch_papers.py --fetched-only --max-downloads N` restricts downloads to the current search's hits.

## MCP launchers (Windows gotcha)

`scripts/launch_mcp.py` and `scripts/launch_structure_tools.py` set `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false`, and `HF_HUB_OFFLINE=1` before importing `sentence_transformers`. Without these, the MCP server hangs at import on Windows. Don't bypass the launcher when registering with Claude Desktop.

`.mcp.json` paths are absolute and **machine-specific** (currently pointing at `C:\Users\micha\Documents\little_protein_tiger\...`, which differs from this checkout's path). When working on this machine, expect MCP servers to potentially be stale until paths are reconciled — `README.md` "Migrating to a new machine" §4 has the canonical fix.

## Common file pairs to keep in sync

- `extraction_schema.json` ⇄ `src/curator.py` (Pydantic `Fingerprint`/`EnzymeContext`/…) ⇄ `curation_prompt.md` ⇄ `src/vector_store.py` (`_build_embed_text` + the search enum) — schema, validator, prompt, and what's embedded/searchable must agree.
- `src/mcp_server.py` / `src/structure_tools_server.py` (MCP) ⇄ `src/skill_runner.py` `_TOOL_DEFS` + `_execute_tool` dispatch + `_filter_tools` gating sets — same tool surface, two transports. A skill that references a tool must also be in the right gating set (e.g. `_LIGAND_TOOL_SKILLS` for the enzyme ligand/active-site tools).
- `src/structure_tools.py` (pure logic) ⇄ `src/structure_tools_server.py` (MCP wrapper) — server is a thin shim; logic lives in the former.
- `src/pipeline_runner.py` ⇄ each skill's "PIPELINE HANDOFF" output block — including the enzyme stages: `_run_enzyme_track` reads keys (`forming_bond`, `acceptors`, `donors_json`, `ts_xyz_path`, `prior_art_pdbs`, …) from the enzyme skills' handoffs, and `_STAGE_TO_SKILL` must map every enzyme stage. `src/enzyme_build.py` / `src/enzyme_validation.py` are deterministic stage helpers (called directly, like `design_runner.py`), not LLM tools.
- Configs: alternates passed via `--config` to `fetch_papers.py` (and now `curate_papers.py`). `config_enzyme_chemistry.yaml` writes into the SAME corpus paths as `config.yaml`, so the broadened `curation_prompt.md` must stay compatible with both.

## Frontend

`web/frontend/` is Vite + React 19 + TypeScript. Commands run from that directory: `npm run dev` (port 5173), `npm run build`, `npm run lint`. The backend's `FRONTEND_URL` env var must match the dev server origin for CORS.
