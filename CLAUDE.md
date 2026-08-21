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

`pipeline_runner.run()` drives **two parallel tracks**, selected by the `workflow` arg (`"ppi"` default, `"binder"`):

- **PPI / binder track** (the original): `pathway → literature → structure → design → execution(BoltzGen) → analysis → summary`. Stages 0–3 and 6 are LLM skills; 4–5 are deterministic Python.

- **Binder track** (`_run_binder_track`, `BINDER_STAGE_ORDER`): target-name-first.
  `target_intel → interface → trim → binder_spec → pilot → calibration → production
  → binder_scoring → binder_summary`. Dispatched at the top of `run()`, skipping
  pathway/literature discovery entirely since the target is already named.
  Requires `--project`. Only three stages call an LLM
  (`binder-target-intel`, `complex-structure-analysis`, `design-analyst`); the rest
  is deterministic Python.

  Compute model = **"LPT drives the GPU"**. `src/foundry_runner.py`
  generates `run_campaign.sh` (a port of `data/BCR/scripts/run_production.sh`) and
  launches it **detached** via `src/job_registry.py`; progress is read from **disk
  counts**, never from logs or process state. RF3 runs for days, so a blocking
  `subprocess.run` inside a Celery task would hit the visibility timeout and be
  redelivered — two campaigns racing one GPU. `--detach` returns immediately and
  `--start-from <stage>` re-attaches.

  **Never scale straight to production.** The `calibration` stage refolds ~300
  backbones × 4 sequences and *measures* the rate of designs clearing the success
  bar, then extrapolates with a Wilson interval (rule-of-three when there are no
  hits). Its verdict — SCALE_UP / SCALE_UP_PARTIAL / ITERATE / STOP — is always a
  manifest checkpoint.

  **`--trial-sites N`** runs one trial per candidate epitope the target-intel
  stage proposes, under `binder/sites/<site_id>/`, and picks the winner on
  measured yield rather than argument. A prepared site is reused on resume
  (`_prepared_site`), so a multi-day campaign never re-pays for the LLM stages.

  **`src/binder_report.py`** renders an illustrated, self-contained
  `report.html` (structure/site rationale, hotspot rationale, confidence
  histograms, and an embedded Mol* viewer over the top-ranked designs'
  actual refolds) for a completed trial or campaign. It is deterministic —
  no LLM, no GPU — and reuses the pipeline's own parsers and gate/ranking
  logic (`src/handoff.py`, `src/binder_ranking.rank_designs`) rather than
  re-deriving them, and renders the target-intel/interface skills' own
  markdown prose (via the `markdown` package) for the "why" sections instead
  of inventing new narrative — a deterministic stage has no business writing
  copy the LLM stages already wrote for a human reader. `_run_binder_track`
  calls it as a best-effort side effect (wrapped so a report bug can never
  fail a campaign) after every trial and after scoring; regenerate by hand
  any time with `scripts/generate_binder_report.py`. Mol* itself
  (`assets/vendor/molstar/`, MIT-licensed, ~5 MB) is vendored and inlined at
  build time — a generated report has no runtime network dependency.

## Non-obvious facts the binder track depends on

These were each established by reproducing a real campaign; changing code near
them without re-reading this list is how they get silently reverted.

- **RFD3 output chains are always binder = A, target = B**, whatever the input
  chains were named. The input→output renumbering lives in `diffused_index_map`,
  which exists **only in a design sidecar** (`*_model_*.json`) — never in the input
  spec. Scoring must remap hotspots from a sidecar; using the spec silently scores
  the wrong residues.
- **RFD3 sidecar metrics use flat dotted keys**:
  `"n_clashing.interresidue_clashes_w_sidechain"` is one string, not a nested
  object. Reading it as nested disables the clash gates and the prefilter keeps
  7816/12000 instead of the correct **7105/12000**.
- **RF3 `<id>_confidences.json`**: `token_chain_ids` carry an entity suffix
  (`"A_1"`, not `"A"`), and `token_res_ids` are **global 0-based token indices**,
  not per-chain residue numbers. `chain_pair_*` matrices are upper-triangular with
  `null` on and below the diagonal — the interface value is `[0][1]`.
- **RF3 stores pLDDT per ATOM** (0–1) in the CIF B-factor column. Average within
  residue *first*: an atom mean weights a Trp ~2× a Gly and de-calibrates the 0.75
  threshold (the correct implementation reproduces 22333/28420).
- **RF3's own `skip_existing` keys on `_metrics.csv`**, written only when early
  stopping triggers, so trusting it silently re-folds finished designs. Key on
  `_summary_confidences.json`.
- **RF3 templating cannot convey a docked pose** (`p_provide_inter_molecule_distances`
  is 0.0 at inference, no Hydra key). iPTM and ipSAE only report confidence in the
  interface the model *chose*; ranking on them alone selects confidently mis-docked
  binders. `binder_rmsd_dock` is the gate that does the work.
- **`ipsae_min` is calibrated, not guessed — but the calibration is per-target, not
  universal.** Across the first two complete campaigns the best value ever produced
  was 0.640 (8TAC) and 0.684 (CD79b), which is why `design.binder_ranking.excellence_ipsae_min`
  is **0.5**. KRAS/RAF1 later hit **0.931** — "unreachable" described those two
  targets, not ipsae_min itself. It is high-precision/low-recall (~70 % precise,
  ~4 % recall against the Rosetta-validated set), so it is a heavy ranking weight
  and *not* a hard gate.
- **Campaigns are SIZED on iPTM, ranked on ipsae_min.** Per 1000 backbones:
  `ipsae_min > 0.5` + gates yields 2.5 (8TAC) / 0.1 (CD79b); `iPTM > 0.7` + gates
  yields 13.2 / 2.0. ipsae_min is 5–20× rarer, so a trial-sized sample cannot
  measure it — the interval blows up and every verdict becomes "enlarge the
  sample". `design.binder_ranking.success_metric` defaults to `iptm` (bar 0.7,
  target 50). **The geometric gates are not optional**: of designs with
  iPTM > 0.7, only 45 % (8TAC) and 8 % (CD79b) are docked on target.
- **`design.binder_ranking.adaptive_bar` (on by default) raises the sizing bar
  for unusually good targets instead of sizing every campaign to the same fixed
  default.** `src/campaign_calibration.calibrate()` walks the same bar ladder
  `suggested_bar` is drawn from — strictest first — and takes the strictest rung
  that both clears `MIN_HITS_FOR_ESTIMATE` (5) hits and whose Wilson-pessimistic
  cost still fits the plain (non-slack) disk/time budget; on real KRAS/RAF1 data
  this raised the iptm sizing bar from 0.7 to 0.85 while staying SCALE_UP at
  ~6 GPU-h. Deliberately **not** a `{tier: multiplier}` lookup table — a hardcoded
  "N hits at tier X → scale Yx" table would just be a cruder, uncalibrated version
  of what the existing Wilson-interval math (already used for the base bar)
  computes exactly from the trial's own k/n. Only ever raises the bar, never
  lowers it below what was requested; `src/binder_report.py` reads back whichever
  bar (`bar_raised_to` or `requested_bar`) a campaign was actually sized at, not
  the static config default, so the report's "excellent" highlighting always
  matches the verdict that was made.
- **A 300-backbone trial is often too small to size a campaign**, and that is
  measured, not assumed: at the 8TAC rate, 300 backbones gave a usable estimate in
  0/10 random seeds and 1000 in 8/10. Hence `--escalate-to`.
- **Rosetta runs after the gates, never before.** A mis-docked or low-pLDDT model
  is still a physical pose, so relax and InterfaceAnalyzer return well-defined,
  meaningless numbers for it. `src/rosetta_metrics.py` scores only gate survivors
  (capped at 300) and its terms enter the final composite only.
- **Membrane targets are designed against the extracellular side**, and
  transmembrane residues are dropped on BOTH sides. In an isolated structure a TM
  helix is an exposed hydrophobic slab that preferentially attracts binders which
  cannot work in a cell, where that surface is buried in lipid. Topology comes
  from UniProt (`src/membrane_topology.py`) and is mapped into author numbering
  through the RCSB entity alignment. It is applied AFTER domain segmentation:
  filtering first removes the contact-density drop that marks the ectodomain
  boundary, which turned a clean CD79B 42-145 trim into 58-159.
- **`os.scandir`, never `ls` or a glob.** Stage directories hold 50–100k entries;
  `ls | wc -l` in a pipeline silently reports 0, which reads as "this stage produced
  nothing" and aborts a completed run.
- **Disk, not GPU, is the binding constraint**: ~2.5 MB per RF3 design directory,
  ~120 GB for a full production campaign. `plan_campaign` clamps `n_batches` to the
  disk budget, and `prune_confidences` deletes PAE matrices for non-survivors.
- **Trims preserve author numbering** so hotspot ids stay valid, and prefer a single
  contiguous segment: every extra segment is an RFD3 chain break, and the prefilter's
  `max_chainbreaks` is *derived from the segment count* — a hardcoded 1 against a
  two-segment target rejects every design.
- **Insertion codes are refused, not worked around.** A Kabat-numbered antibody chain
  has repeated author ids, which an RFD3 contig cannot address; emitting `C92-92,C92-92`
  produces a spec RFD3 accepts and silently mis-models.
- **A hotspot table can be internally consistent and still wrong**: the interface
  stage can report a well-known protein's canonical *literature* numbering
  verbatim instead of grounding in the specific structure it was asked to
  analyse. Caught in practice — asked to analyse 8ZNL, it returned PD-L1's
  textbook hotspots (Tyr56, Gln66, ...), correct for a *different* PD-L1
  structure (7CZD) but not for 8ZNL, where chain B residue 56 is actually VAL.
  `validate_spec` caught that one only by luck (the stated atoms don't exist on
  valine); a mismatch that happened to share atom names would have sailed
  through. `_verify_hotspot_grounding` reads the real residue name at each
  hotspot's `auth_seq_id` from the downloaded structure and hard-fails on any
  mismatch, before a trim or spec is ever built.
- **`target_chain`/`partner_chain` can be silently swapped by the interface stage**,
  and hotspot grounding cannot catch it: a swap produces real, correctly-numbered
  residues on the *wrong protein*, not a fabricated residue. Caught in practice —
  for PD-L1 (7CZD, PD-L1 on RCSB chains B/D), the stage assigned `target_chain=A`
  and wrote hotspots on the anti-PD-L1 VHH's own CDR loop instead, and its own
  summary said "Target chain A (VHH)"; a full multi-hour campaign ran against the
  wrong molecule before `_verify_target_chain_assignment` existed.

  It checks two independent signals, sequence first: `_chain_identity_to_uniprot`
  aligns the chain's actual MODELLED residues (already on disk) against UniProt's
  canonical sequence via `structure_tools.sequence_identity` (local alignment,
  BLOSUM62) — ground truth, immune to a curation error, and it gave a decisive
  100%-vs-20% margin on the real PD-L1 case. RCSB entity description/SIFTS
  accession (`entry_metadata`) is the fallback, used only when no UniProt
  accession resolved or the structure isn't downloaded yet. Independent of
  `_verify_hotspot_grounding`, which only confirms a residue is real, not which
  molecule it belongs to.

- **foundry's checkpoint-registry aliases only work for RFD3/RF3, not MPNN.**
  `ckpt_path=rfd3` and `ckpt_path=rf3` resolve through the registry; MPNN's
  config takes a literal path and dies with `checkpoint_path does not exist:
  solublempnn` — *after* RFD3 has already run. `src/foundry_stages.py`'s
  `resolve_checkpoint()` globs `~/pip_rcfoundry_ckpt/` for the alias before
  handing MPNN a config.
- **This foundry build's MPNN type-checks `designed_chains` and rejects a bare
  string** (`"A"`) with `designed_chains must be a list if provided` — it must
  be `["A"]`. `foundry_spec.build_mpnn_configs` always emits a list now
  (`_as_chain_list`).

## API cost accounting

`src/token_budget.py` prices every LLM stage and enforces `--budget` as a hard cap.
The four token buckets are **priced separately** — uncached input, cache-write
(~1.25×), cache-read (~0.10×), output. Summing them into one "input" number
overcharges a cache-heavy stage ~3×. `SkillRunner.usage()` is read in a `finally`
so a stage that dies mid-loop is still billed. The ledger is append-only JSONL at
the project root (authoritative, survives a torn write, safe under concurrent CLI +
Celery writers); a rollup is mirrored into `manifest.json["budget"]` once per stage.

**Extended thinking is adaptive.** `budget_tokens` was removed on `claude-sonnet-5`
/ `claude-opus-5` and returns a 400 — `src/skill_runner.py` sends
`{"type": "adaptive"}` plus `output_config.effort`. Thinking blocks must be
replayed **with their `signature`**, or the next turn is rejected with
`messages.N.content.0.thinking.signature: Field required`.

## Safety-classifier refusals are an operational fact of this pipeline

The binder track's `interface` stage (`complex-structure-analysis`) is routinely
declined with `stop_reason: "refusal"`, category `bio`. Ledger evidence from
three separate refusals on one target (PD-L1): `claude-sonnet-5` refuses, then
`claude-opus-5` **also** refuses (same category), every time — a same-provider
retry after a categorised refusal has not once succeeded here, and it is not
free: ~$0.13 spent for nothing per attempt. `claude-haiku-4-5` eventually
answered in that same run.

So `models.claude.refusal_fallbacks` is a **chain**, and an entry may name another
provider as `"provider:model"` (`_resolve_stage` returns a provider alongside the
model) — and **Gemini goes first**, not last: crossing providers immediately on
the first refusal is cheaper and faster than walking same-family models that
share the same classifier verdict. `gemini-3.7-flash` answers reliably and costs
~4× less on input than Sonnet. `claude-opus-5` / `claude-haiku-4-5` stay in the
chain only as a fallback for the rare case Gemini itself declines or errors —
`_run_gemini` raises the same `SkillRefusedError` for a Gemini-side safety block
(empty `candidates`, a `promptFeedback.blockReason`, or a per-candidate
`finishReason` of `SAFETY`/`PROHIBITED_CONTENT`/`BLOCKLIST`/`RECITATION`/`SPII`),
so the chain degrades the same way regardless of which provider declines.

A refusal raises `SkillRefusedError` rather than returning "". The old behaviour
wrote a 0-byte stage report and the run failed three stages later with a
confusing "no MODEL-READY HOTSPOTS" error.

**Do not reword prompts to get around a classifier.** Model fallback is a
legitimate engineering response; prompt engineering aimed at defeating a safety
check is not.

## Persistent project layer

`src/project.py` manages `projects/<slug>/` with an atomic `manifest.json` (the filesystem source of truth for stage/checkpoint state + artifact pointers; `web.db` stays authoritative for the web UI). Layout: `shared/{structures,ligands}/` + `runs/<round-N>/scratch/`. `PipelineRunner(project=, round_id=)` mirrors stage state into the manifest (`_record_stage`) and computes `run_dir` from it. The binder track requires `--project` — it iterates in rounds, and the manifest is what makes a multi-day GPU campaign resumable. When adding a binder pause point, set a manifest checkpoint (`_binder_checkpoint`) so resume state survives.

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
- `src/pipeline_runner.py` `_STAGE_TO_SKILL` is **many-to-one** — `complex-structure-analysis`
  serves both `structure` and `interface`, `design-analyst` serves both `summary` and
  `binder_summary`, and `protein-design-script` serves `design`. The inversion
  (`_stage_for_skill`) is first-match-wins and cannot tell them apart, so **every call
  passes `stage=` explicitly**. Adding a stage that reuses a skill without it silently
  takes the other stage's model and manifest slot.
- `src/foundry_spec.py` `validate_spec` ⇄ `src/structure_trim.py` `TrimResult.contig`
  / `kept_segments` — the spec is cross-checked against the trim, so changing the
  contig format means changing both.
- `src/binder_metrics.py` `FIELDS` ⇄ `src/binder_ranking.py` thresholds/weights ⇄
  `config.yaml design.binder_ranking` — a renamed column silently drops out of the
  composite (it warns, but the run continues).
- `src/handoff.py` (`parse_handoff` / `parse_hotspot_residues`) ⇄ `src/pipeline_runner.py`
  (`_parse_handoff` / `_parse_hotspot_residues`, now thin wrappers) ⇄ `src/binder_report.py` —
  extracted so the deterministic report generator can read the same "### PIPELINE
  HANDOFF" / "### MODEL-READY HOTSPOTS" blocks the pipeline itself parses, without
  depending on `PipelineRunner`. Both call sites must keep using the module, not a
  re-implementation, or the two will silently drift on the next skill-prompt edit.
- Configs: alternates passed via `--config` to `fetch_papers.py` (and now `curate_papers.py`).

## Frontend

`web/frontend/` is Vite + React 19 + TypeScript. Commands run from that directory: `npm run dev` (port 5173), `npm run build`, `npm run lint`. The backend's `FRONTEND_URL` env var must match the dev server origin for CORS.
