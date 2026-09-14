# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` already documents commands, environment setup, the migration checklist, and the full skill catalogue. This file covers the architectural big picture and project-specific conventions that aren't obvious from a single file.

## Two intertwined systems

The repo combines two pipelines that share a corpus and a set of MCP tools:

1. **Literature corpus pipeline** (`src/` + `scripts/`):
   `fetch_papers.py` → SQLite (`data/literature.db`) → `curate_papers.py` → fingerprint JSONs (`data/fingerprints/`) → LanceDB (`data/vectors/`) + edge index (`data/depmap_edges.parquet`) + clusters (`data/clusters.json`). Each stage is incremental and idempotent — papers carry `download_status` and `curation_status` columns that gate reruns.

   **`fetch_papers.py` gates downloads on a journal tier list**
   (`quality.require_tiered_journal`, default true; lists in `src/ranking.py`).
   Search indexes every hit, but only tier 1/2 journals are downloaded — 32% of
   indexed papers on the reference corpus. Matching is EXACT against a
   normalised name, so a journal spelled a way the list doesn't contain is a
   silent 100% exclusion, not a warning. See `docs/journal-filtering.md`.

   `curate_papers.py` self-runs three post-curation hooks at the end of each batch when at least one paper was curated successfully:
   1. **identifier normalization** (`run_backfill`) — adds the `protein_identifiers` sidecar block. Skip with `--skip-normalize`.
   2. **graph rebuild** (`build_and_cluster`) — refreshes `data/depmap_edges.parquet` and `data/clusters.json` so cluster tools see new papers. Skip with `--skip-graph-rebuild`.
   3. **vector ingestion** (`run_ingest`) — embeds new fingerprints into LanceDB so `search_corpus` can find them. Skip with `--skip-vector-ingest`.

   All three are idempotent and skip cleanly when there's nothing to do. Vector
   ingestion additionally **re-embeds a fingerprint whose text has changed**
   (dedup keys on `paper_key` + the embedded text, not the key alone — on the
   shipped corpus 704 rows carry a vector older than their fingerprint), and
   reports rows whose fingerprint file is gone; deleting those needs an explicit
   `--prune-orphans`. The standalone scripts (`normalize_identifiers.py`, `cluster_corpus.py`, `ingest_vectors.py`) remain available for migrations, force-rebuilds, and debugging. The in-process `_GRAPH_CACHE` in `src/_corpus_graph.py` is mtime-invalidated so a long-running MCP server picks up new fingerprints without restart.

2. **Expert skill execution** (`skills/` + `src/skill_runner.py` + `src/pipeline_runner.py`):
   Each `skills/<name>/SKILL.md` is a system prompt plus tool-call protocol. Skills run in **two modes**:
   - **Claude Desktop / Claude Code**: via the two MCP servers in `.mcp.json` (`literature-db`, `structure-tools`).
   - **CLI / web backend**: via `scripts/run_skill.py`, which loads `SKILL.md` as the system prompt and routes tool calls **directly to Python functions** (no MCP subprocess). Same skill, same tools, different transport.

   `src/pipeline_runner.py` chains skills end-to-end (pathway → literature → structure → design) by parsing `### PIPELINE HANDOFF` blocks out of each skill's markdown output. Editing handoff format in one skill requires updating the consumers.

3. **Web platform** (`web/`) — **EXCLUDED FROM THE RELEASE, DO NOT WORK ON IT** unless explicitly asked. FastAPI + Celery + React. It is untracked (see `.gitignore`) and unmaintained: `_TrackedRunner._run_stage` in `web/backend/tasks.py` has a stale signature against `PipelineRunner._run_stage` (which gained a keyword-only `stage`), so it raises `TypeError` on the first PPI stage; it also knows nothing about `--workflow binder` and handles 4 of the 11 pause points the runner now raises. Branch `archive/web-platform` marks the last commit that tracked it. The CLI is the supported entry point.

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

  **`src/ppi_report.py`** is the same idea for the PPI track — but read
  the next sentence before assuming a PPI run produces it: it is reachable
  ONLY from the `boltzgen_legacy` fall-through (`_generate_ppi_report` is
  called at `run()`'s `:980`/`:988`, after `_stage_analysis`/`_stage_summary`,
  and a BRIDGED PPI run returns at `:952` and never reaches them — it gets
  `binder_report` instead), and it hard-requires artifacts only
  `_stage_analysis` writes. It stays the renderer for the archived legacy runs
  in `outputs/`. It is: one
  illustrated `report.html` over `00_pathway.md` .. `06_summary.md` (target
  rationale, prior art, hotspots, the BoltzGen design-generation stats,
  filter-gate funnel, top-K design cards, and the design-analyst's final
  verdict rendered verbatim), plus a Mol* explorer over the top boltzgen
  refolds. `_run_binder_track`'s sibling `run()` calls it as a best-effort
  side effect after `analysis` and after `summary`; regenerate by hand with
  `scripts/generate_ppi_report.py`. It shares its whole design system with
  the binder report — see the "Two reports, one design system" note below —
  and shares `src/report_common.py`'s markdown/citation-section readers
  rather than re-parsing `### PIPELINE HANDOFF` / `## CITATION
  VERIFICATION` blocks a second way. It re-derives nothing about *what
  passed*: `_parse_filter_stats` deserializes the run's own frozen
  `05_ranking/filter_stats.txt` (written by `design_ranking.
  write_ranking_outputs` at analysis time) instead of re-running
  `filter_records` against today's `config.yaml` thresholds, which may have
  changed since the run executed — the one place this report's numbers
  could otherwise silently drift from what the pipeline actually decided.

  **Two reports, one design system.** `src/report_templates/_shared/`
  (`base.css` + `base.js`) holds the palette, typography, and every layout
  primitive (rail nav, hero, stat grid, blocks, prose, callouts, tables,
  chart primitives, verdict banner, design cards) plus the Mol* structure-
  explorer harness (`createStructureExplorer` — structure loading, hotspot
  highlighting, water/ion hiding), used by both `binder_report`'s and
  `ppi_report`'s `shell.html`/`app.js`. A track's own `app.js` holds only
  what's genuinely track-specific (which sections exist, what the funnel/
  scatter axes mean, what a design card shows). Extend the shared files
  when a new report needs a layout primitive that's already generic;
  extend a track's own `app.js` when the content is genuinely specific to
  that track — don't grow one at the expense of the other's readability.

  **Every report ends with the stage reports themselves.** The narrative
  sections each render ONE slice of a stage's markdown (the prose before its
  handoff, the hotspot rationale, the verdict); the `#appendix` section
  renders every stage file WHOLE, so a single `report.html` is the complete
  run record and not a set of quoted excerpts —
  `report_common.stage_documents` builds it and each track only decides which
  files go in the list. A PPI-bridged campaign's binder report therefore also
  carries `00_pathway.md`/`01_literature.md`/`02_structure.md` from one level
  up, which is where the "why this target" reasoning actually lives. Two
  presentational transforms are needed because a stage report is written to
  be read in a TERMINAL first: handoff bullets become a two-column table, and
  the two-space-indented cost ladders (`25_calibration.md`) are fenced, since
  markdown's own code-block rule is four spaces and it otherwise collapses an
  aligned ladder into a run-on sentence of numbers. Both act on the rendered
  copy only — `handoff.parse_handoff` still reads the file on disk.

  **`report_common.safe_json`, not a local copy.** Escaping `<!--` as `<\!--`
  (both reports used to) produces a payload the BROWSER accepts — JavaScript
  drops the backslash from an unknown escape — that is not valid JSON, so
  every test reading `REPORT` back out of a built report breaks the moment a
  stage's own HTML comment reaches the blob. The shared version uses `\/` and
  `\u003c`, which are valid in both.

## One bridge, three engines (`design_engine`, foundry by default)

Scoped in `UNIFY_DESIGN_BACKEND_NOTES.md` (foundry) and
`UNIFY_BOLTZGEN_BACKEND_NOTES.md` (BoltzGen). `design.backend` is
**`foundry` by default**, and `--workflow ppi` hands a PPI-discovered target
off to the SAME stage machine `--workflow binder` uses. Flipped after a real
KRAS/RAF1 campaign validated the bridge end-to-end on GPU (82 min, top design
iPTM 0.923 / dock-RMSD 0.39 A).

**`--design-engine` names TWO engines, and both take the bridge.** `foundry`
(RFD3->solubleMPNN->RF3) and `boltzgen` differ only in which generator
spec/pilot/calibration/production/scoring dispatch to (`_boltzgen_backend`).
**`boltzgen_legacy` — the older PPI-only design/execution/analysis chain — was
RETIRED on 2026-09-13**: the CLI and `PipelineRunner.__init__` both refuse the
name (`_RETIRED_ENGINES`), and `_stage_design`/`_stage_execution`/
`_stage_analysis`/`_stage_summary`, `src/design_runner.py`, its own e2e
driver script and `STAGE_ORDER`'s indices 3-6 are deleted.
`STAGE_ORDER` is now `["pathway", "literature", "structure"]` — a PPI run has
no stage name past `structure`, because it bridges from there, so a PPI
campaign resumes at a BINDER stage name. What discharged decision 2 of the
BoltzGen notes, and the staged plan, are in `LEGACY_RETIREMENT_SCOPE.md`;
`src/design_ranking.py` and `src/design_metrics.py` survive it and are LIVE
(see the "Common file pairs" note).

- **`_bridges_to_binder_track` and `_boltzgen_backend` answer different
  questions**, and the first is now a TAUTOLOGY (`_BRIDGED_ENGINES` ==
  `_DESIGN_ENGINES`, since the one engine that did not bridge is gone). It is
  kept deliberately, with the defensive `raise` after it: the first decides
  whether a PPI run reaches the binder-track stages at all, the second which
  generator those stages use, and `tests/test_audit_fixes.py` asserts by
  source inspection that the first gates `_designable_chain_sizes` — the
  lesson being that relaxing a designable-size limit is only sound where a
  trim actually follows. Keep them separate: `== "foundry"` in the hand-off is
  exactly what left `--design-engine boltzgen` on the legacy path for its
  first release, and nothing failed — the run just silently had no trim, no
  measured production size, no `--stop-after` and no per-stage manifest.
- **The bridge itself reads no engine at all** (a test pins this): it composes
  a target_intel handoff and an interface artifact, and `_run_binder_track`
  dispatches from there. A backend-specific line in it would be a second
  dispatch waiting to disagree with the first.
- **`--compute`/`--n-gpus` are refused on `boltzgen`**, since
  `_run_boltzgen_stage` has no cluster path and would otherwise run the whole
  campaign locally while the operator waited for a submission script. (The
  near-identically worded refusal that used to sit beside it, for
  `boltzgen_legacy`, went with the retirement — do not confuse the two if you
  read an older diff: the surviving one is the live guard.)
- **`--success-metric` writes the block its engine reads.** BoltzGen's gate
  and bar live in `design.boltzgen_ranking` (BoltzGen-native columns, via
  `resolve_boltzgen_ranking`), foundry's in `design.binder_ranking`; writing
  into the wrong one is silent — the campaign is simply sized at the default
  bar. `--success-metric ipsae_min` is refused on BoltzGen outright: it writes
  no PAE matrix, so its own ipsae column spans 0.0000-0.0289 against an
  RF3-calibrated bar of 0.5 and every campaign would size to STOP.
- **One binder-track path is NOT dispatched: the multi-site trial.**
  `_run_site_trials` calls `_stage_binder_spec`/`_stage_calibration`
  unconditionally, so a BoltzGen run reaching it would build an RFD3 contig
  JSON where a BoltzGen YAML was asked for and then wait for RF3 output that
  never arrives. `_refuse_undispatched_site_trials` blocks `--trial-sites > 1`
  and `--stop-after spec|trial` there and points at `--stop-after
  calibration`, which takes the dispatched single-site route. The guard is
  self-contained rather than trusting its caller's `if`, and runs BEFORE
  `_binder_sites` — refusing after it would already have resolved chains and
  logged a site header, which reads as though the trial had begun.
- **`--project` is required on every track and every engine**, checked in
  both `run()` and the CLI. The GPU stages each track reaches are multi-hour
  to multi-day and checkpoint into the manifest, which is also what
  `--start-from` reads. (`boltzgen_legacy` was the last combination that ran
  without one; it is retired, and the requirement stands on every track.)

**Modality is the operator's choice, not the model's.** `--modality` defaults
to `mini_protein`; `cyclic_peptide` is opt-in and automatically selects
BoltzGen, because RFD3 has no cyclic-peptide path and
`binder_sizes.cyclic_peptide` (12-15 residues) fed to RFD3 asks for something
it cannot build — quietly. `PipelineRunner._resolve_modality` is the single
place that reconciles what a stage PROPOSED against what the operator CHOSE;
every consumer goes through it, and the skill prompts no longer present
modality as a menu.

**Overriding the modality has to carry the LENGTHS with it, and for one
release it did not.** A stage sizes `binder_length_min`/`max` for the modality
it proposed, so once `_resolve_modality` rejects that proposal those numbers
describe a campaign the run is not running — but `_binder_length_range`
preferred the handoff's explicit numbers over the `binder_sizes` table, which
defeated the coercion while leaving it looking correct. Caught on the first
real PD-L1 macrocycle run: `binder-target-intel` proposed `mini_protein` with
70-86, `--modality cyclic_peptide` overrode the modality, and the trim contig
recorded a 78-residue binder for a 13-residue campaign. The spec was still
right (it reads its own table), so what broke was the COMPLEX SIZE — 195
tokens against 130, over-costing both BoltzGen laws by 74%, with no observed
rate to supersede them because a 24-design pilot is below
`MIN_DESIGNS_FOR_RATE`. Those feed `campaign_calibration.calibrate`'s budget
check, i.e. the SCALE_UP/STOP verdict. **The reverse is worse and is the same
line**: a stage proposing `cyclic_peptide` with 12-15 on a default foundry run
would have handed RFD3 a 12-15mer it cannot build, quietly, with the coercion
applied and apparently working. The handoff's numbers are now honoured only
when its own modality survived the override — a stage's deliberately narrowed
range (74-80 inside 70-86) still is — and the last-resort constants are
per-modality too. Requires `--project` — same reasoning as
the binder track's own requirement: the foundry stages downstream are
multi-day GPU campaigns that need a resumable manifest — now required on
every track, see above. `design.backend` was
a dead config key before this (nothing read it — confirmed by grep); do not
assume any *other* currently-unread config key in this file does something
just because it looks wired.

`_bridge_ppi_to_binder_track` (`src/pipeline_runner.py`) is entered right after
the go/no-go decision, once PPI's own pathway/literature/structure stages
have already run unchanged. It does NOT re-run `_run_binder_track` from its
own `"interface"` stage — PPI's `_stage_structure` already calls the
identical `complex-structure-analysis` skill (see the shared-skill note
below) and, since the verify-gap fix in the same change, runs the same
`_verify_ppi_chain_assignment` / `_verify_hotspot_grounding` guards
`_stage_binder_interface` does. Re-running it would just pay for a second,
redundant LLM call. Instead: PPI's `02_structure.md` is copied verbatim as
the binder track's `21_interface.md` artifact (byte-identical shape, same
skill), a synthetic *deterministic* `20_target_intel.md` is written from the
literature/structure handoffs (translating `target_complex` — "ProteinA /
ProteinB" — into `target_gene`/`partner_name` via
`_split_target_complex_names`, and resolving a UniProt accession offline via
`target_resolve.resolve_target`, best-effort), and `_run_binder_track` is
entered at `"trim"` — one stage past its own `"interface"`. Everything from
`trim` onward (spec -> pilot -> calibration -> production -> scoring ->
summary) runs completely unmodified, inheriting every non-obvious fact in
the next section for free. A process resuming a later stage
(`--start-from production`) has no PPI stage to re-enter, so `run()`
dispatches a binder-stage `--start-from` straight into `_run_binder_track`
for either bridged engine, exactly like `--workflow binder` resumes.

**Two guards were binder-only until this change, and PPI-track chain
assignment has no single pre-declared answer to check against the way
binder's does.** Binder's `target_intel` names one unambiguous target gene
up front; PPI's `target_complex` names BOTH proteins in a PPI pair, and
either one is a legitimate `target_chain` choice — the structure stage
itself decides which, fresh, every run. `_verify_ppi_chain_assignment`
handles this by resolving each named protein and trying
`_verify_target_chain_assignment` against each in turn, accepting the
assignment as soon as one candidate doesn't flag it as backwards; it
hard-fails only when target_chain matches NEITHER named protein (the actual
shape of the PD-L1 incident: a chain assigned to a molecule outside the
intended pair entirely), and stays silent — fail-open, like every other
verify check here — when a candidate is merely inconclusive. Both guards now
run inside `_stage_structure` too (previously binder-`interface`-only),
deliberately outside the try/except that swallows hotspot-parse failures as
warnings: a real chain-swap or grounding mismatch must halt the run, not
degrade to a log line.

**Known gaps, not yet addressed** (see `diary.md`'s 2026-08-24 "First step
of the PPI/binder-track unification" entry for the fuller list): no
membrane-topology resolution for a PPI-bridged target beyond whatever
accession `target_resolve` happens to find offline — the bridge writes no
`membrane_side`, so `_stage_trim` infers the side from the hotspots, which is
the right default but is not binder-target-intel's own UniProt-topology
check; `--trial-sites` multi-epitope comparison isn't
wired into the bridge (PPI's structure stage picks exactly one interface).
**The "no real GPU run has proven the bridge" gap is CLOSED** (2026-08-28):
`projects/mesothelioma_showcase` went from the one-sentence query "Design cancer
therapeutics to target key nodes in mesothelioma." through pathway → literature
→ structure → bridge → trim → spec → pilot → calibration → production → scoring
→ summary, unattended, for $0.79 of API spend and ~22 GPU-h. It picked
YAP1/TEAD1 on 3KYS, calibrated at an 18.5% backbone hit rate (95% CI
15.3–22.2%), and returned 317 gated survivors from 1,352 refolds with a best
ipTM of 0.937 / dock-RMSD 0.63 Å. That run is also the first multi-segment trim
(3 segments, 2 chain breaks) — the derived `max_chainbreaks` held, prefilter
kept 86%. BoltzGen's own `design_metrics`/
`design_ranking` path is untouched and stays fully live as a deliberate
escape hatch, not oversight.

## Non-obvious facts the BoltzGen backend depends on

Established by running BoltzGen at scale (a 994-design cyclic-peptide campaign
against RAMP1, `3N7S` chain D), not by reading its docs.

- **A binder whose residue count exactly equals the target chain's kills the
  whole run.** The `design` step writes both chains correctly, then
  `inverse_folding` drops the binder chain from the CIF while leaving its
  `.npz` design_mask at the full token count, and `folding` dies in
  `data_from_generated.get_feat` with `IndexError: boolean index did not match
  indexed array along axis 0; size of axis is 84 but ... 168`. It aborts the
  ENTIRE campaign, not the one design, two steps after the design that caused
  it, naming neither. Measured on RAMP1 (84 residues) with a 70..86 range: 2 of
  24 designs sampled 84 and both failed; all 22 at other lengths were clean. So
  the incidence is ~`1/(hi-lo+1)` per design — a near-certainty at campaign
  scale, and invisible on a small probe unless it happens to sample the number.
  Upstream bug; `build_boltzgen_spec.safe_binder_range` narrows the range from
  whichever end loses least rather than working around it after the fact. The
  two shipped cyclic campaigns never hit it only because 12–15 cannot collide
  with an 84- or 817-residue target.
- **`pass_filters` is the most informative column BoltzGen writes, and it is
  dominated by one check.** It is the AND of nine booleans, of which
  `filter_rmsd <= 2.0 Å` (`refolding_rmsd_threshold` in the generated
  `filtering.yaml`) accounts for 886 of 909 rejections; the amino-acid-fraction
  filters pass 98–100% and are effectively inert. It is a **design-vs-refold
  self-consistency** check, i.e. BoltzGen's own version of `binder_rmsd_fold` —
  not an opaque black box. Every e2e driver used to override
  `require_boltzgen_pass` to False, which is how the shipped
  `design.thresholds` block went years without ever gating a real run.
- **BoltzGen ranks by MAXIMIN, not by a weighted sum.** `max_rank` is the
  **worst** of six per-metric ranks (iptm, design_ptm, neg_ipae, PLIP H-bonds,
  PLIP salt bridges, delta_SASA) — verified 994/994 — and passers are sorted by
  it ascending, with non-passers pushed below every passer. So a design must be
  decent on all six; excellence at one buys nothing.
- **It deliberately does not rank by iPTM, and it is right not to.** Overlap
  between its top-100 and the iPTM-top-100 is 20/100, and the single
  highest-iPTM design in the run (0.633) ranks 126th because it fails
  `filter_rmsd`. Measured dock quality: its top-100 has a median
  design-vs-refold dock RMSD of 3.12 Å with 94% under 5 Å, against 8.89 Å /
  16.3% over the full set and 8.01 Å / 29% for iPTM-ranking. This is what
  `binder_ranking.DEFAULT_Z_CLIP` was learned from.
- **The analyst stage reads a DIFFERENT set of columns on this track, and
  nothing in the run tells it so.** `top_k.csv` here carries
  `design_id`/`final_rank`/`design_to_target_iptm`/
  `min_design_to_target_pae`/`complex_plddt`/`pass_filters`/
  `designed_chain_sequence` — not one foundry column. So the summary stage's
  two readers must both be vocabulary-aware, and were not:
  `_slim_binder_top_k` intersected the header with foundry's names, got an
  EMPTY column list, and handed the analyst a table with no columns; and
  `_write_binder_fasta` keyed on `binder_seq` where BoltzGen writes
  `designed_chain_sequence`, so a completed cyclic campaign got **no orderable
  FASTA at all** — the one artifact a macrocycle run exists to produce.
  Measured on `projects/pdl1_macrocycle`: 1,700 of 4,329 designs passed the
  gate, 20 real macrocycles sat in `top_k.csv`, and the analyst wrote "the
  campaign returned an empty candidate set", NO_GO — which
  `run_pipeline.py` turns into **exit 1** (deliberately, for NO_GO), so a
  successful 6.6 GPU-h campaign looked like a failed run to anything reading
  the exit code. `_top_k_vocabulary` now decides foundry-vs-BoltzGen from the
  COLUMNS rather than from `self._design_engine`, so the track named in the
  prompt is provably the track whose columns the analyst was shown, and
  `--start-from binder_summary` in a fresh process needs no runner state to
  get it right. An unrecognised vocabulary shows the file's own columns and
  warns, because a misread scorer must look like a surprise and not like an
  empty run.
- **`src/binder_report.py` renders a BoltzGen campaign too, and the
  vocabulary decides four things.** It used to hard-require foundry's
  `refold_scores.csv` and raise "No refold scores found ... run at least a
  calibration trial" on a finished cyclic run. `_load_boltzgen_designs` now
  reads the FULL population through `design_metrics.parse_boltzgen_outputs`
  — `scoring/ranked.csv` holds only the gate survivors (1,700 of 4,329), so
  reading that would draw every histogram over the passing tail — takes
  survivors from BoltzGen's own `pass_filters` (foundry's `filter_records`
  fails a record for a MISSING gated column, so it would report zero
  survivors), and deserializes the funnel from the frozen
  `scoring/filter_stats.txt` rather than re-deriving it. `complex_plddt` is
  NOT mapped onto `binder_plddt` — whole complex vs binder alone — so it
  travels under its own name, and the report's second histogram, scatter
  y-axis, design-card metrics and epitope-check prose all come from a
  `vocab` block chosen by the track. The track is read off the ROWS, not a
  config engine key, so a report regenerated for an archived campaign cannot
  disagree with the numbers it renders.
- **A report must state the modality the run RAN, not the one a stage
  proposed.** `20_target_intel.md`'s handoff is a proposal that
  `_resolve_modality` may have overridden, and reading it titled the
  macrocycle showcase's report "Does a de novo mini_protein occupy ...?" over
  a 15-residue cyclic-peptide campaign. `_run_modality` prefers
  `calibration.json`'s frozen value, then the binder-spec handoff, then the
  proposal — the same "read what the run decided" discipline already applied
  to `excellence_bar` and `success_metric`. The site-decision block names the
  proposal only when it was overridden, which is exactly the case a reader
  needs to see.
- **`quality_score` is not a score.** It is 994 evenly-spaced distinct values,
  i.e. `1 - (final_rank-1)/(n-1)` — a rank percentile carrying no information
  beyond the ordering. Fine for sorting — the retired `design.ranking.
  enrich_top_k` used it for exactly that — and meaningless as a quality
  threshold.
- **`ipae` is target-dependent, not universally too loose or too strict.** The
  shipped `ipae_max: 10.0` passes 99.7% on RAMP1 cyclic and dropped 57% on the
  archived mesothelioma run. And `complex_plddt >= 0.70` — currently only a
  composite weight, not a gate — would pass just 3.7% of cyclic designs and
  collapse a 64-hit `iptm >= 0.50` set to zero. Promoting it to a threshold
  would silently empty every cyclic campaign.
- **Per-design cost scales with target AND binder size, and a small probe
  over-costs by ~1.5–2×.** At-scale, end-to-end from each campaign's own
  `campaign_timing.jsonl` over 970 designs: **7.8 s/design at 99 tokens**
  (12–15mer, peptide protocol), **17.3 s/design at 160 tokens** (70–83mer,
  protein protocol), 122 s/design at 817 tokens. The 24-design probes of those
  same two campaigns read **16.0 and 26.5** — 2.05× and 1.53× their own
  at-scale rates — because fixed model-loading dominates, the same reason
  `foundry_runner.sec_per_refold_observed` requires ≥50 samples. Both probe
  numbers were briefly taken for measurements, and the second is why
  `PROTEIN_PROTOCOL_FACTOR` shipped at 1.8 before the campaign finished and
  said **1.175**.
- **The protein protocol's extra step is its cheapest.** `protein-*` runs six
  steps to `peptide-*`'s five, the extra one being `design_folding` — a refold
  of the BINDER ALONE, 70–83 residues against the complex's 160 — so the
  measured end-to-end margin is ~18%, not the ~80% the probe implied. GPU
  utilisation drops to 0% during the CPU-bound `analysis` step, so
  `nvidia-smi` is not a liveness signal.
- **The two modalities differ on the pieces and agree on the product.** Over
  994 designs each against the same target (RAMP1, 3N7S chain D), the rate
  that actually sizes a campaign — clearing the shipped gate AND the 0.50
  iptm bar — is **1.11% mini (95% CI 0.62–1.97) against 1.21% cyclic
  (0.69–2.10)**, i.e. within noise. The components are not: `pass_filters`
  passes 20.3% of mini and 8.6% of cyclic, `ipae <= 10` keeps 83% of mini
  passers and 100% of cyclic ones, `complex_plddt >= 0.70` 39% against 3.7%.
  So `design.boltzgen_ranking`'s base block is right for both and
  `modality.mini_protein` carries no overrides — which is a measurement, not
  an inheritance by default.

## The operator can name the epitope

`--hotspots B74,B83,B84` (or bare `74,83,84` for the target chain) makes the
`interface` stage **make no LLM call at all** and use exactly those residues.
Same posture as `--modality`: stages propose, the operator decides. Binder and
structure tracks only — the PPI track picks hotspots inside `_stage_structure`,
before the foundry hand-off, and the CLI refuses the flag there rather than
accepting it and running a campaign against a model-chosen epitope while the
operator believes otherwise.

- **The operator supplies ONLY the numbers.** `residue`, `rfd3_atoms` and
  `label_seq_id` are all read from the structure. A user-typed residue name
  would make `_verify_hotspot_grounding` tautological — grounding exists to
  prove the residue at auth 83 in THIS file is the intended one, and it can
  only do that when the name came from the file rather than from the same
  person who typed the number. The commonest real mistake is canonical-isoform
  numbering pasted against a construct-numbered crystal, and grounding is
  precisely what catches it.
- **It writes a real `21_interface.md` and falls through to every existing
  guard**, rather than short-circuiting past them: `_correct_label_seq_ids`,
  grounding, chain assignment, partner-chain, ortholog conservation, then the
  trim's own hotspot-retention and exposed-hydrophobic checks. Writing the
  artifact is also what keeps `--start-from trim` resumable, since that branch
  re-parses the file off disk.
- **The table is the DISRUPT four-column form verbatim**, because two separate
  regexes must match it — `_correct_label_seq_ids` (`| RES | auth | label |`,
  three-letter name) and `handoff.parse_hotspot_residues`.
- **Over the cap is an ERROR here, not a warning.** `build_rfd3_spec` only
  warns when a skill overshoots `MAX_HOTSPOTS`, because the builder has no
  per-residue ddG/BSA and cannot choose which to drop. An operator can, and
  more hotspots is not stricter — RFD3's hit rate on a large set falls as the
  set grows, which weakens the engagement gate rather than tightening it.
- **`label_seq_id` is written as `**UNVERIFIED**` and filled by
  `_correct_label_seq_ids`** from gemmi's own auth→label map. That resolution
  path already existed for the skill's tables; reusing it is why nothing new
  derives a label_seq_id by counting. Its summary log used to call every
  filled cell "LLM-provided label_seq disagreeing with gemmi", which
  misdescribed correct skill behaviour as a red flag — a FILLED
  `**UNVERIFIED**` cell and a STATED-but-wrong number are now counted and
  logged separately, and only the second warns.
- **Scoring needs no change.** `hotspot_engagement` is a fraction of whatever
  the spec declared, re-derived at scoring time from the RFD3 design sidecar,
  so an explicit set becomes the denominator automatically.

## A third entry point: structure-first

`--workflow structure` (`_stage_structure_intel`) exists because the other two
tracks both spend LLM stages answering "what should we design against?", and
that is already answered when the operator hands you a structure. It is the
same manoeuvre `_bridge_ppi_to_binder_track` uses — compose the handoff
`binder-target-intel` would have produced, write it to
`_BINDER_STAGE_FILES["target_intel"]`, and enter `_run_binder_track` mid-stream
— with one deliberate difference: the bridge enters at `trim` because PPI's
structure stage already picked hotspots, this enters at **`interface`**, because
nothing has looked at the structure yet and the epitope still has to be chosen
by a model reading real coordinates. Only ONE stage calls an LLM.

- **A local file travels as a `LOCAL-<stem>` pseudo-id**, the same trick as
  `AF-<accession>`: every path in the pipeline is built as
  `<structures_dir>/<ID>.cif`, so the file is copied there and addressed by id
  thereafter. `_ensure_structure` returns it directly — falling through would
  try to download `LOCAL-MY_TARGET` from RCSB and fail a run whose structure is
  on disk. `.pdb` is converted to mmCIF on ingest.
- **The interface prompt names the FILE for a local id, not the id.** An
  unresolvable accession with no path is exactly the shape that made the
  interface skill decide no structure existed and ask for one (the PD-L1/7CZD
  case).
- **Which chain is the target is measured, then stated as a choice.** Contacts
  are counted with a KD-tree over the largest `_MAX_CHAINS_CONSIDERED` chains
  (cheap); BSA is computed once, for the winner. The larger chain of that pair
  becomes the target — on 7CZD that is chain B, PD-L1, and *not* the VHH, which
  is the assignment an LLM stage got backwards on this exact entry. `--chains`
  overrides it and the report always says what was picked and how to swap it.
  A single chain selects `design_intent: inhibit_active_site`, which already
  existed for AlphaFold monomers.
- **`analyze_interface` returns `interface.bsa_total_A2`, nested — there is no
  flat `bsa_total`.** A `.get("bsa_total", 0.0)` read reports "0 A^2 buried" for
  a 2,449 A^2 interface, silently.
- **Three guards go inactive without `--uniprot`, and the report says so.**
  `_verify_target_chain_assignment`, `_check_structure_organism` and membrane
  topology are all keyed to identity, not geometry. Failing open is right — an
  operator's construct or prediction is in no database — but going quiet about
  it is not, because the first of the three is what caught a multi-hour campaign
  designed against a nanobody's CDR loop. **Transmembrane residues are not
  stripped without an accession**; on a receptor that is a design that cannot
  work in a cell. `_verify_hotspot_grounding` is unaffected (coordinates only).

## Non-obvious facts the binder track depends on

These were each established by reproducing a real campaign; changing code near
them without re-reading this list is how they get silently reverted.

- **RFD3 output chains are always binder = A, target = B**, whatever the input
  chains were named. The input→output renumbering lives in `diffused_index_map`,
  which exists **only in a design sidecar** (`*_model_*.json`) — never in the input
  spec. Scoring must remap hotspots from a sidecar; using the spec silently scores
  the wrong residues.
- **RFD3 addresses a contig/hotspot residue by its loader's `res_id`, which is
  the AUTHOR number for a PDB input and `label_seq_id` for an mmCIF one.**
  Serving author-numbered hotspots (`A314: CD2,CZ`) is therefore correct and
  always has been — because `_stage_trim` hands RFD3 `trimmed.pdb`
  (`pipeline_runner`'s `pdb_input`), so `write_trimmed` writing both files is
  not redundancy. The deciding line is in atomworks, not in RFD3:
  `atomworks/io/utils/io_utils.py` calls biotite's `pdbx.get_structure` with
  **`use_author_fields=False`**, and biotite then sources BOTH `res_id` from
  `label_seq_id` and `chain_id` from `label_asym_id`; `auth_seq_id` is loaded
  only as an extra annotation (for non-polymer indexing) and only non-polymer
  `res_id`s are ever written back from it (`update_nonpoly_seq_ids`). The PDB
  branch never reaches that call, so `res_id` stays biotite's `resSeq` — the
  author number. Measured through RFD3's own loader
  (`rfd3.utils.inference.inference_load_`, which
  `DesignInputSpecification.load_input` calls, wrapping `atomworks.io.parse`):
  for `trimmed.pdb` chain A is `res_id` 195..411, the author numbering, with no
  auth annotation at all; for the SAME trim's `trimmed.cif` it is 3..219, and
  the author numbering survives only in a separate `auth_seq_id` annotation
  that nothing in the lookup path reads —
  `foundry/utils/components.py::fetch_mask_from_idx` compares
  `atom_array.res_id` and raises `Residue {chain}{res_id} not found in atom
  array`. So an author-numbered contig against an mmCIF resolves against label
  numbering: on 4ZGM (auth 29-128 / label 6-105) `A29-128` died at
  `[component=A106]` for a residue plainly present as `ATOM ... ALA A ... 106`,
  and that is the LUCKY case — spans inside the label range mis-model in
  silence. `validate_spec` refuses an mmCIF input whose two numberings
  disagree, and one whose contig chain is not its own `label_asym_id`: the
  chain letter shifts by the SAME mechanism, so a construct numbered from 1
  (numbering agreeing, that check silent) on author chains H/L would resolve
  against nothing. One author chain legitimately holds several label
  subchains — deposited 3KYS auth chain A spans label_asym A (polymer) and E
  (non-polymers) — so only the span's own residues are checked. A structure
  numbered from 1 whose chains are its label chains is unaffected. Do NOT read
  this as
  "RFD3 is a label_seq engine" — it is the read that makes BoltzGen and foundry
  differ (BoltzGen's `binding:` really is label_seq), and conflating them is
  how a correct auth-numbered hotspot table would get "fixed" into a wrong one.
- **RFD3 merges contig components that are NOT separated by `/0` into ONE
  output chain, whatever input chains they came from** — `/0` is the
  chain-INCREMENT token (foundry `rfd3/inference/input_parsing.py:1319-1324`).
  Measured on 4ZGM with `70-86,/0,A29-128,B10-37`: the output is exactly two
  chains (binder A = 79 residues, target B = 128) and the sidecar's
  `diffused_index_map` carries 128 entries — 100 keyed `A*`, 28 keyed `B*` —
  every one of them mapping into `B`, numbered 1..128 consecutively. This is
  what makes a two-chain (molecular-glue) target possible without touching
  scoring: `binder_metrics`, `binder_ranking`, the `chain_pair_*` `[0][1]`
  read and ipSAE all keep seeing one binder against one target.
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
- **No single metric may carry a design.** The composite is a weighted z-sum, so
  it was unbounded: a design 3 SD clear on `ipsae_min` banked +6.0 at weight 2.0
  and no realistic deficit elsewhere could offset it, filling the top-K with
  one-metric specialists. `design.binder_ranking.z_clip` (2.0, on by default;
  `null` disables) caps each column's contribution at ±2 SD — roughly the
  2.3/97.7 percentile, so outliers only. Learned from BoltzGen's maximin ranker
  (see that section above for the measurement). Measured over all 13 real
  foundry campaigns: **survivor counts identical everywhere** — it is a ranking
  control and `filter_records` never sees it — Spearman on composite rank
  0.988–1.000, top-20 membership 13/20–20/20 retained, rank-1 changed on 5 of
  13. On two inspected, the old rank-1 was a `binder_plddt` outlier (z = +3.07,
  +2.91) and clipping promoted designs docking 42% and 54% better with equal or
  perfect epitope recall. It **bounds** dominance without overriding the
  weighting: at weight 2.0 with a 2.0 clip the ipSAE term still tops out at 4.0,
  exactly equal to `iptm` + `epitope_recall` maxed together — so if you want
  ipSAE to stop being able to tie two metrics single-handedly, that is a weight
  change, not a clip change.
- **A liability we trust outranks a reward we do not.** `neg_rosetta_vbuns` is
  1.0 (was 0.5): `vbuns` counts polars the interface buries WITHOUT satisfying
  (complex − binder − target, clamped ≥ 0), so a design cannot buy a good ddG by
  burying unsats — the worker's own `iface_score = ddg + w * vbuns` already
  reasons this way. There is deliberately **no positive weight on
  `hbonds_int`**: counting favourable H-bonds and salt bridges off a *predicted*
  structure over-trusts sidechain placement. `sbuns` is the sidechain-only
  subset of `vbuns`, so weighting both double-counts. Both stay reported
  columns.
- **`hotspot_engagement` is a FRACTION of the declared hotspots, and the gate is
  0.75, not 1.0.** Requiring every hotspot sounds strict and is mostly self-harm:
  on the 12-hotspot YAP1/TEAD1 calibration only **49% of RFD3 backbones contacted
  all twelve themselves**, and 92.5% of refolds engaged at least as many hotspots
  as their own design did — so a 1.0 gate rejected refolds for missing residues
  the design never targeted. Over the 1,392 refolds passing every other gate,
  12/12 kept 738 (median iptm 0.847, dock 1.232) and 9/12 kept 978 (0.842, 1.259):
  +33% yield for −0.005 iptm, with identical pLDDT in the discarded band. Being a
  fraction, it scales with however many hotspots a region declares.
- **One region declares at most 12 hotspots** (`foundry_spec.MAX_HOTSPOTS`, and
  the rule the interface skill applies in Phase 2 Step 2b: keep the compact
  hydrophobic cluster, drop rim/polar/backbone-only positions, say what was
  dropped). More is not stricter — RFD3's hit rate on a full set falls as the set
  grows, which weakens the engagement gate rather than tightening it.
  `build_rfd3_spec` warns above the cap rather than truncating: choosing which to
  drop needs the per-residue ΔΔG/BSA the skill had and the builder does not.
- **A 300-backbone trial is often too small to size a campaign**, and that is
  measured, not assumed: at the 8TAC rate, 300 backbones gave a usable estimate in
  0/10 random seeds and 1000 in 8/10. Hence `--escalate-to`.
- **Rosetta runs after the gates, never before.** A mis-docked or low-pLDDT model
  is still a physical pose, so relax and InterfaceAnalyzer return well-defined,
  meaningless numbers for it. `src/rosetta_metrics.py` scores only gate survivors
  (capped at 300) and its terms enter the final composite only.
- **Transmembrane residues are dropped on BOTH sides; WHICH side to design
  against is inferred, not fixed.** In an isolated structure a TM helix is an
  exposed hydrophobic slab that preferentially attracts binders which cannot
  work in a cell, where that surface is buried in lipid — that part is physics
  and unconditional. The side is not: `membrane_side` defaults to `"auto"` and
  `_infer_membrane_side` reads it off where the declared hotspots actually sit,
  because for an intracellular-organelle membrane protein (SCAP in the ER)
  "extracellular" names no real surface at all, and the interface stage has
  already read the structure. Extracellular is where the effect usually lives
  for a cell-surface receptor, so that is what inference usually returns — it is
  not a rule, and a cytoplasmic epitope is designed against normally (the
  operator owes the delivery route, which the four target-selecting skills now
  ask for by name, rather than the pipeline owing a refusal). Only two things
  still fail: a hotspot INSIDE the membrane, and hotspots split across both
  faces — neither is one epitope. Topology comes from UniProt
  (`src/membrane_topology.py`) and is mapped into author numbering through the
  RCSB entity alignment. It is applied AFTER domain segmentation:
  filtering first removes the contact-density drop that marks the ectodomain
  boundary, which turned a clean CD79B 42-145 trim into 58-159.
- **`os.scandir`, never `ls` or a glob.** Stage directories hold 50–100k entries;
  `ls | wc -l` in a pipeline silently reports 0, which reads as "this stage produced
  nothing" and aborts a completed run.
- **`expected_rf3` is an ESTIMATE until MPNN writes, and `4 sequences per design`
  applies to prefilter SURVIVORS, not to every design.** `plan_campaign` computes
  `int(expected_rfd3 * prefilter_rate) * n_seq`, so 392 designs plans as 924
  refolds at the 0.59 default and 1,300 at a measured 0.83. It cannot end a
  campaign early — `progress()` does `expected_rf3 = n_mpnn or plan.expected_rf3`,
  so the real MPNN count supersedes the estimate as soon as it exists — but the
  disk CLAMP is computed from the estimate, so an under-called rate can
  under-clamp a campaign sized near the budget. A stage's own directory is empty
  when it is planned, so `prefilter_rate_observed()` returns 0 there;
  `_persisted_prefilter_rate` reads the rate the trial measured back out of
  `calibration.json` instead of falling through to the default.
- **RF3 refold cost scales with complex size — `SEC_PER_RF3_REFOLD` is an
  anchor, not a flat rate.** Fitted over four campaigns (timed from `rf3_out`
  directory mtimes, not log ticks, which include the driver's retry gaps):
  9.7 s/refold at 195 tokens, 11.0 at 245, 15.7 at 264, 18.1 at 285. The old
  flat 8.4 s was 14% low at the small end and **54% low at the large end**,
  which is how a 3 GPU-h estimate became an 8.8 h run.

  **Both GPU laws are TWO-TERM and were re-fitted on 2026-09-14**, because the
  single power law that replaced the flat constant had the same defect one
  range further out: `9.1 * (tokens/195)**1.62` fitted 195–285 tokens and
  predicted **72 s at 698 tokens against ~144 s measured**, so with the target
  ceiling now at 500 residues it under-costed exactly the campaigns that
  ceiling permits. Now:

      rf3_seconds_per_refold(N)   = 2.91 + 5.60 * (N/195)**2.56
      rfd3_seconds_per_design(N)  = 1.90 + 4.35 * (N/195)**1.643

  The RF3 compute term is fitted from the size sweep's **load-free
  differences** — each point folded one refold in a fresh process, so
  consecutive differences cancel the model load exactly — and the validation
  is that the implied load then comes out CONSTANT at 15.4, 15.3, 15.4, 12.0,
  17.0, 15.5 s across the six points, which nothing forced. The fixed term is
  the residue against the four campaigns' per-refold wall time. RFD3's is
  fitted over 13 Phase A rungs plus two 4-design probes at 589/665 tokens
  whose difference is likewise load-free; rms 0.45 s over 15 points, and its
  fixed term and exponent are stable across any load assumption from 0 to 25 s.
  Net effect on a 4,600-design production: **0.97x at 195 tokens, 1.14x at
  293, 1.90x at 590** — small campaigns are untouched and large ones stop
  being half-priced. The exponent is essentially quadratic-plus now because
  attention dominates once the O(N) parts stop mattering; do not re-flatten it,
  and do not extrapolate past ~700 tokens, which is where the card OOMs anyway.
  Better still, `sec_per_refold_observed`
  reads the rate a PREVIOUS stage of the same campaign actually achieved
  (`_earlier_refold_rate`), which tracked production within 10–17% on all three
  campaigns that ran both. This feeds `est_gpu_hours`, and through
  `choose_compute()` the local-vs-cluster decision.
  **There is now ONE anchor and ONE law, for BOTH stages**: `campaign_calibration`
  imports them from `foundry_runner` instead of holding its own. It kept a
  stale flat 8.4 for RF3 (below) and then kept a flat `SEC_PER_RFD3_DESIGN =
  5.4` for RFD3 right through the RF3 fix — the same bug twice, in the same
  file, and the RFD3 copy is what `calibrate()` spends the gate's budget on
  first. `calibrate()` now derives BOTH rates from `n_tokens`, and
  `CampaignPlan` carries `n_tokens` so `progress()`'s ETA quotes the rates the
  plan was costed at rather than the bare anchors (it told an operator 9 s per
  refold for a campaign the plan had costed at 149).
  (The original RF3 incident: it kept a stale flat 8.4,
  and it is the GATE's budget check, so on MASH/TEAD4 the gate costed 22,197
  refolds at 63 GPU-h — "inside the 120 h budget" — while the planner costed the
  same work at 138 GPU-h). `calibrate()` takes `n_tokens` and applies the size
  law itself, so a caller that passes neither a measured rate nor a size gets a
  warning rather than a silently under-costed SCALE_UP.
- **Disk scales with the complex, like GPU time does.** A refold directory costs
  **0.6-1.9 MB, not a flat 2.5 MB** — `foundry_runner.refold_bytes` is
  `0.97 MB * (tokens/195)**1.49`, fitted over 27,304 refold directories in six
  campaigns (nothing pruned) to within 1.6%. Half the bytes are coordinates
  (O(N) `model.cif`) and half the PAE matrix inside `confidences.json` (O(N^2)),
  which is why the exponent lands just under the runtime law's 1.62, and why
  RF3 writing every artifact TWICE (`seed-0_sample-0/` plus a promoted copy, as
  separate files, not hardlinks) doubles the whole thing rather than one part of
  it. The old flat constant over-called the anchor by 155% and the smallest
  campaign here by 238%, clamping `n_batches` on campaigns that fit comfortably.
  `campaign_calibration` imported the rate law but kept its own stale copy of
  the disk constant — **one anchor and one law now covers both**.
  Measured totals: ~15 GB for a typical production campaign, 15 GB for the
  largest campaign in `projects/` all-in. `plan_campaign` clamps `n_batches` to the
  disk budget, and `prune_confidences` deletes PAE matrices for non-survivors —
  wired into `_stage_binder_scoring` after `write_ranking_outputs`, and **off
  unless `design.foundry.prune_confidences` is set**, because ipSAE cannot be
  recomputed once the matrices are gone. It is skipped for a cluster campaign
  and skipped entirely when nothing survived (that is exactly the run an
  ITERATE verdict tells you to re-gate at a softer bar).
- **Trims preserve author numbering** so hotspot ids stay valid. They prefer a single
  contiguous segment, but NOT because RFD3 has to build across the gaps — it doesn't. The
  target is fixed conditioning: the sidecar's `sampled_contig` reads
  `72P,/0,A195,A196,...`, a diffused binder plus every target residue pinned by index.
  What the segments change is `rfd3_n_chainbreaks`, which counts breaks in the OUTPUT
  structure, so an N-segment target contributes an unavoidable N−1 before the binder is
  looked at. Measured: single-segment campaigns score 0 on ~95% of designs (the rest are
  genuine breaks in the DIFFUSED BINDER, which is the signal worth filtering); the
  3-segment YAP1/TEAD1 target scored exactly 2 on all 400 designs sampled — no variance,
  no information. `max_chainbreaks` is *derived from the segment count* so the budget for
  real binder breaks stays at 1 either way; a hardcoded 1 against a multi-segment target
  rejects every design.
- **A trim is often a no-op, and that is a result.** PD-L1 kept 117 of 117 residues and
  YAP1/TEAD1 207 of 207 — deciding a target is already within budget is as much this
  stage's job as cutting one down, so a multi-segment contig does not imply anything was
  trimmed away. 3KYS has exactly ONE real gap, the disordered 230–238; the campaign's
  third segment was an artifact of the trim deleting A344, below.
- **Chain membership is decided by BACKBONE, not by name** (`structure_tools.
  is_chain_residue`). gemmi's chemical-component table does not know every modification a
  depositor may make: 3KYS A344 is `P1L`, S-palmitoyl-cysteine, reported as
  `kind=UNKNOWN, is_amino_acid=False`, yet it carries a full N/CA/C backbone 3.86 Å and
  3.85 Å from residues 343 and 345. Filtering on the NAME deleted it, which removed the
  palmitoylation the entire TEAD-inhibitor literature is about AND split TEAD1 into a
  third segment that cost a chain break. Both the residue enumeration and `write_trimmed`
  now keep anything carrying N/CA/C, whatever it is called — found by benchmarking the
  trim across 19 complexes, not by a test.
- **Keeping it is only half the job: it must also reach the generator as something
  the generator can PARSE.** A344 is deposited as a **HETATM** record between two
  ATOM records, and RFD3 builds its polymer from ATOM records — so a contig spanning
  it aborted a real campaign ten times with `Residue A344 not found in atom array`,
  for a residue plainly in the file. The unrecognised NAME alone would not have done
  it; the record type is what bit. `write_trimmed` therefore converts a modified
  residue to its parent amino acid — name, atom set trimmed to the parent's own
  atoms, and `het_flag` 'H' → 'A' — so A344 is written as CYS with six atoms and the
  C7–C22 palmitoyl tail dropped (a lipid RFD3 cannot model, buried in TEAD1's central
  pocket rather than on the designable surface). The parent comes from ordered tiers
  and is **never guessed**: the deposited CIF's `mon_nstd_parent_comp_id`
  (authoritative, and absent for every component of 3KYS), gemmi's `one_letter_code`
  (answers for MSE/SEP/TPO/PTR), then `structure_tools._CURATED_PARENTS`. An unknown
  parent is left as deposited and warned about, and `foundry_spec.validate_spec`
  refuses a contig spanning a HETATM residue — failing at spec-build rather than on
  the GPU. **This stayed invisible for weeks** because no foundry run touched 3KYS
  after the keep-A344 fix: `projects/mesothelioma_showcase` predates it and carries
  the old 3-segment contig with A344 excluded, which is exactly why that run worked.
- **Converting it changes what the exposure guard sees, and both sides must match.**
  biotite counts P1L as an amino acid, so dropping the tail from the trimmed
  structure while the deposited one still had it reported MET347 +28.9 Å² and PHE392
  +21.7 Å² — the residues lining the pocket the palmitoyl fills — and refused a trim
  that had cut NOTHING (208 → 208). Same shape as the water bug
  `_exposed_hydrophobic` already documents ("amino acids only and one chain only in
  BOTH"), so its SASA now also restricts both sides to parent-canonical atoms:
  compare only atoms that survive INTO the design structure.
- **The size ceiling is a TOKEN ceiling, and both GPU stages were bisected to
  find it** (2026-09-14, this card: RTX PRO 4500 Blackwell, 32.6 GB, idle but
  for a 2.0 GB desktop). `design.foundry.target_residue_budget` is **500**,
  raised from a 220 that came from ONE ~175-token complex and whose own config
  comment admitted it. Measured, single-tenant:

  | stage | tokens | peak VRAM | spare |
  |---|---|---|---|
  | RF3, one refold | 195 | 6.1 GB | — |
  | RF3 | 568 | 21.5 GB | 11.1 GB |
  | RF3 | 698 | 32.0 GB | 0.59 GB |
  | RF3 | 848 | **CUDA OOM** | "tried to allocate 9.09 GiB, 7.93 GiB free" |
  | RFD3, `diffusion_batch_size: 4` | 589 | 25.4 GB | 7.2 GB |
  | RFD3, batch 4 | 665 | 32.0 GB | 0.58 GB |

  Three facts follow. **RFD3 is the binding stage, not RF3** — it holds one
  diffusion trajectory per batch member, so its ceiling (~674 tokens by its
  fitted `0.0675 MiB/token^2`) arrives before RF3's. **Both are quadratic
  above ~400 tokens** (RF3 net ~= `385 + 0.0588*tok^2` MiB, reproducing the
  698-token point to -3%), so margin collapses rather than tapering. And **the
  residue budget cannot express the limit**: it counts the target only, never
  the binder, so it is ~28% short of the complex across modalities — with the
  budget at 500 and `target_budget_overshoot: 0.15`, a 575-residue target
  validates at 665 tokens, i.e. 1.8% of the card free. `max_complex_tokens:
  600` is the guard that refuses that at spec-build time rather than on the
  GPU hours in, and it is what makes the raise safe; it leaves ~19% free at
  the measured quadratic and permits ~514 residues with an 86mer binder.
  **`rf3_seconds_per_refold` was NOT re-fitted and now under-predicts**: its
  exponent 1.62 was fitted over 195-285 tokens and predicts 72 s at 698
  tokens against ~144 s measured, so `plan_campaign` under-costs a
  large-target campaign by ~2x. Sizing one at the new ceiling without fixing
  that is the mash_e2e failure again (gate costed 63 GPU-h, planner 138).
- **The trim only runs when the target does NOT fit.** It used to reduce to the
  hotspot-carrying domain(s) regardless of size, so a 252-residue chain became 205 and a
  364-residue one became 19 even though the budget was then 220. If the whole chain fits
  `design.foundry.target_residue_budget` it is now kept whole — and at 500 that
  is most single-domain targets, which is the point: not trimming is the best
  outcome, and dropping a second interface is the operator's call,
  not a silent default. Three refusals guard what remains, all measured on a 19-complex
  benchmark: `MIN_TARGET_RESIDUES = 80` (a 19-residue "target" passed every other check
  because its 4 hotspots survived), and the newly-exposed-hydrophobic rules below.
- **A cut may not open hydrophobic core, and the gate is scale-free.** ZERO exposure
  within `EXPOSED_HOTSPOT_CLEARANCE_A = 10` Å of a hotspot — unconditional, because a
  fresh hydrophobic face there competes with the site being designed for at any size —
  and away from the epitope, `MAX_EXPOSED_HYDROPHOBIC_FRACTION = 0.25` of the
  target-side interface BSA. A residue COUNT was the gate and is not scale-free in
  either direction: six residues at +16 Å² read as "6, over the limit" while two at
  +190 Å² read as "2, within tolerance", and the second opens 2.6× more surface.
  `MAX_EXPOSED_HYDROPHOBIC = 2` survives as the fallback where there is no partner
  chain and therefore no denominator (a monomer, `inhibit_active_site`). The two are
  ALTERNATIVES, not an AND — `exposure_verdict` is the one place that decides — since
  keeping both discards the half of the improvement that consists of accepting several
  small exposures. Measured per residue as ΔSASA > 15 Å² between the original and
  trimmed structures, **amino acids only and one chain only in both**: include waters
  and you measure desolvation instead — with solvent stripping on, a no-op trim of
  7CZD "exposed" Met18 by 71 Å². All three are `trim_target` kwargs, like
  `min_bsa_retention`; tests that force an aggressive cut pass
  `max_exposed_hydrophobic=None`, which still disables BOTH measures.
  - **0.25 is a proposal, not a calibration, and the production corpus cannot
    calibrate it.** 22 of the 25 trims in `projects/` are NO-OPS — the target already
    fitted the budget — and every one measures exactly 0.0%, so the guard has
    essentially never judged a real cut in a real campaign. Forcing cuts by sweeping
    the budget gives: 5VAI R→100 (`R29-128`, a real domain boundary) 74.8 Å² = **5.5%**;
    3KYS A→190, shearing the fold, 525.4 Å² = **32.5%**; 5VAI R→200 and →150, accreting
    into the TM bundle, **90.5%** and **92.1%**. Nothing lands between 5.5% and 32.5%,
    so the whole 10–30% band fits equally well. Note the gate only ever decides cuts
    with NO near-epitope exposure, because any at all raises first — which is why the
    older `away + near` totals in `GLUE_PIPELINE_SCOPE.md` (81%, 175%) describe cuts
    that were already refused and cannot set this threshold.
  - **Every trim now RECORDS its exposure**, because nothing did and a calibration has
    to be fitted on something: `exposed_hydrophobic_A2`, `n_exposed_hydrophobic`,
    `exposed_hydrophobic_fraction` and `exposed_hydrophobic_auth` on `TrimResult`, in
    `trim_map.json`, carried by `_TrimFromDisk`, and stated in `22_trim.md` on every
    trim including the clean ones. The area used to exist only inside a warning
    string, so a clean trim recorded no measurement and a refused one recorded none
    either — the gate's own input was absent from the record of every run it judged.
- **The patch is also a per-design scored liability, and `patch_enrichment` is the
  rankable form.** `binder_metrics` writes `n_patch` / `patch_contacts` /
  `patch_contact_fraction` / `patch_enrichment`; `design.binder_ranking.weights` has
  `neg_patch_enrichment: 0.5`. The raw FRACTION scales with the patch's size, which is
  a property of the trim and identical for every design in a campaign — ranking on it
  shifts every design equally and reorders nothing while appearing to work. Enrichment
  divides by the patch's share of the accessible target, so 1.0 is chance and only the
  per-design deviation survives. Measured all-atom on the binder side (`ep_hot`), the
  same reasoning as `hotspot_engagement`. `patch_from_rfd3` remaps the trim's AUTHOR
  ids through the sidecar's `diffused_index_map` — never the spec — and
  `_exposed_patch` reads them from `trim_map.json` rather than a `TrimResult` in
  memory, because scoring is routinely reached by `--start-from binder_scoring` in a
  fresh process days later. Zero, not blank, when there is no patch: `binder_ranking`
  warns about a MISSING column and z-scores a zero-variance one to zeros, so the usual
  no-op-trim campaign contributes nothing instead of being noisy, and the Protenix
  path gets the columns for free by falling through the same branch.
  **This came from Phase B's branch 3 and deliberately implements only half of it.**
  1,784 refolds over 143 matched pairs found no detectable quality cost to patch-heavy
  designs (gate ratios 1.125 [0.711–1.781] and 0.882 [0.488–1.597], every U test null)
  but the patch contacts SURVIVE refolding (1.000 in five of seven arms) — real binding
  to an artificial surface, of unmeasured magnitude, hence a tie-breaker weight. The
  trim REFUSALS stay: both Phase B arms held total contacts fixed, so it tested "does
  having contacts on the patch cost anything" and never "does exposing a patch cost
  anything", and its highest near-epitope dose returned 0 of 40 designs through the
  gates against a control's 23 of 60. Removing a refusal needs the experiment that
  varies exposure itself.
- **Solvent never reaches the design or the interface maths.** `write_trimmed` drops
  waters and crystallisation additives (`structure_tools.is_solvent_or_additive` — a
  conservative denylist that checks `_is_protein_residue` FIRST, so MSE/SEP/TPO/PTR/PCA
  can never be hit, and that keeps ions, metals, sugars, nucleotides and any unrecognised
  ligand). Before this, ordered waters carried the target chain's id into
  `_per_residue_bsa` where they could never match the trim's `kept_set`, so every
  interface water counted as a residue the trim had "removed": on 7CZD, 20 waters worth
  435 Å², 35% of the target-side total, opening a `trim_gate` checkpoint on a trim that
  removed nothing.
- **Insertion codes are refused, not worked around.** A Kabat-numbered antibody chain
  has repeated author ids, which an RFD3 contig cannot address; emitting `C92-92,C92-92`
  produces a spec RFD3 accepts and silently mis-models.
- **A hotspot table can be internally consistent and still wrong**: the interface
  stage can report a well-known protein's canonical *literature* numbering
  verbatim instead of grounding in the specific structure it was asked to
  analyse. Caught in practice — asked to analyse 8ZNL, it returned PD-L1's
  textbook hotspots by their **canonical UniProt numbers** (Tyr56, Gln66,
  Arg113). Those are the right RESIDUES; what was wrong was the numbering
  FRAME. 8ZNL's deposited author numbering runs `canonical + 1`
  (`_struct_ref_seq`: Q9NZQ7 19-132 <-> auth 20-133), so the correct ids
  there are Tyr**57**, Gln**67**, Arg**114** — auth 56 in 8ZNL really is VAL
  — while 7CZD, which the model had evidently memorised, numbers author ==
  canonical and the same numbers are right on it. (Tyr56 is canonical
  precursor numbering, not mature: the signal peptide is 1-18, which would
  make it Tyr38.) The shipped 8ZNL campaign designed against the correct
  epitope — `projects/pdl1_rc1`'s `21_interface.md` lists `TYR57 | 57 | 38`
  — so do not read this as a wrong-epitope incident.
  `validate_spec` caught that one only by luck (the stated atoms don't exist on
  valine); a mismatch that happened to share atom names would have sailed
  through. `_verify_hotspot_grounding` reads the real residue name at each
  hotspot's `auth_seq_id` from the downloaded structure and hard-fails on any
  mismatch, before a trim or spec is ever built.

  **It answers "is the residue at auth N what the report claims", not "is
  auth N the residue the literature means"**, and against a numbering-frame
  error it is therefore a PROXY: measured over 8ZNL chain B's modelled span,
  a uniform +1 offset is caught at 105 of 113 positions and silent at 8,
  where the neighbour happens to share a residue type — ~93% per hotspot, so
  a ten-row table slipping through is ~1e-11 but a single row can. So the
  second question is now answered directly, and STATED rather than inferred:
  `_hotspot_numbering_frame` routes the declared hotspots through
  `membrane_topology.uniprot_to_auth` (RCSB's deposited entity↔UniProt
  alignment — a uniform `{+1}` for 8ZNL chain B, `{0}` for 7CZD) and logs the
  frame plus each hotspot's canonical equivalent, so the ids a reader compares
  against a paper are printed in the paper's own frame.
  - **ADVISORY, never a gate, and the code contains no `raise` (a test pins
    that).** A non-zero offset is ordinary and entirely legal in a deposited
    structure, as 8ZNL is — a run must not end on one. It fails open on
    everything: no accession, no alignment for that accession in this entry, a
    network failure, a hotspot outside the alignment. An offset of 0 logs INFO
    (worth saying: it is the entry where a literature id can be used as-is);
    anything else logs a warning; a multi-offset alignment reports per-hotspot
    translations and says there is no single frame.
  - It runs LAST inside `_verify_hotspot_grounding`, after every hard check,
    so a table that is wrong about its own residues fails on that rather than
    being handed a frame translation of nonsense. It needs an accession:
    binder and site paths pass `intel["target_uniprot"]`, and the PPI path
    passes `_ppi_target_uniprot`, which `_verify_ppi_chain_assignment` sets to
    whichever of the pair's two named proteins the assignment was finally
    accepted against — a PPI complex names both halves and only that loop
    knows which one the target chain turned out to be. Handing it the
    PARTNER's accession is harmless rather than wrong, because
    `uniprot_to_auth` looks the alignment up on the target chain's own entity
    and returns `{}`.
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

## Non-obvious facts the cluster compute path depends on

`src/cluster_runner.py` stages a campaign onto
`g-groups/.../binder_pipeline` for a human to submit (this workstation has no
SLURM login-node access) and reads results back on resume. Every fact below
was established by staging a real campaign, not by reading that pipeline's
own docs — the docs described a narrower/different shape than what the code
actually does in two separate places, and both cost a full campaign before
being caught.

- **`NB` in that pipeline is designs PER GPU ARRAY TASK, not a campaign
  total.** Its own diagnostic line says so (`NARRAY x NB x DBS = designs`,
  `bin/run_pipeline.sh`), but `plan_campaign()` first read it like the local
  foundry convention (where `n_batches` *is* the total) and silently
  undercounted an actual campaign's size by exactly `n_gpus`: reported
  ~8,600 refolds for a campaign that was staged for, ran, and completed at
  ~59,000. Fixed by multiplying `expected_rfd3` by `cluster_cfg.n_gpus`.
- **Protenix (and every other folding-repo backend there) writes a
  per-design SUBDIRECTORY, not flat files** — `run_N/protenix/<id>/<id>.pdb`
  + `<id>_scores.json` [+ `<id>_pae.npy`], the same shape as RF3's own
  `<id>/<id>_summary_confidences.json`. An earlier reading of that
  pipeline's own scorer said flat; `iter_protenix_refolds` and
  `cluster_runner.refold_counts` both scanned for files directly inside the
  backend dir and counted zero refolds against a campaign that had already
  finished and sat idle for over a day.
- **`<id>_scores.json`'s real schema** (confirmed against a live file): only
  `plddt_mean`, `plddt_per_residue`, `ptm`, `iptm`, `pae_mean`, `runtime_s`,
  `iptm_per_chain_pair`. No `binder_ptm`/`target_ptm`, no
  `iface_pae`/`iface_pae_min`, no `has_clash`, no `ranking_score` — an
  earlier revision of `protenix_confidence_adapter` guessed at all of those
  keys from that pipeline's docs/scorer description, and none of them exist.
  `iface_pae` is computed from the raw `<id>_pae.npy` matrix's
  binder-target block instead of trusting a nonexistent field; the file's
  own `pae_mean` is whole-structure and would dilute the interface signal
  with two mostly-unrelated intra-chain blocks. Protenix's PDB B-factor
  column IS the 0–100 scale `binder_metrics.read_structure`'s
  `plddt_scale=100.0` assumes — confirmed against real atom records.
- **A production-scale run's sizing — and its compute placement — must
  survive a process restart.** `_stage_calibration`'s `n_batches`
  recommendation only lived in that process's memory; resuming `--start-from
  production` in a fresh process (the normal case for a multi-hour campaign)
  silently fell back to `config.yaml`'s raw default (3000 batches, ~87 GPU-h)
  instead of the measured ~18 GPU-h recommendation. `_resolve_production_plan`
  (successor to the old `_resolve_production_batches`) re-derives both the
  batch count *and* the local-vs-cluster decision from the persisted
  `calibration.json` on any resume — the same file also stores
  `n_batches_local` and `n_batches_cluster` (they differ by `n_gpus`, per the
  `NB`-is-per-GPU fact above) so re-attaching after `--compute auto` chose
  cluster doesn't need to redo the choice, and can't accidentally undo it by
  reading the local number.
- **`--compute auto` (the default) is a TIME decision, made once, at the
  calibration gate — not a per-stage toggle.** `pilot` and `calibration`
  themselves always run locally (they're deliberately small); only
  `production` is placed by `campaign_calibration.choose_compute()`, which
  compares the pessimistic single-GPU estimate
  (`res.pessimistic.est_gpu_hours` — already single-GPU wall-clock, since the
  `SEC_PER_*` constants it's built from are local-GPU-calibrated) against
  `--max-local-hours` (default 48, `design.foundry.max_local_hours` in
  config.yaml). `cluster_hours = local_hours / design.cluster.n_gpus` is
  explicitly a rough estimate, not a promise — Protenix on the cluster and
  local RF3 have different per-design timing. `--compute local` / `--compute
  cluster` still force every GPU stage onto one path unconditionally, exactly
  as before `auto` existed; `auto` only changes the *default*.
- **GPU hardware faults happen at this scale and look identical to a bug at
  first glance.** A real campaign hit two independent `CUDA error:
  uncorrectable ECC error encountered` failures — one killed an entire
  array task's RFD3 stage (0 designs from that 1/6 of the campaign), one
  killed one of two refold shards for another task (50% yield on that 1/6).
  Both are node-level GPU memory hardware faults, not anything to debug in
  this codebase; the fix is resubmitting the specific failed array index,
  and persistent recurrence is a signal for the cluster administrators, not
  for this pipeline.
- **The cluster pipeline's RFD3 output subdirectory is named `diffuse`, not
  `rfd3`.** Same binary, same design-sidecar filenames (`*_model_*.json`,
  same `diffused_index_map` remap), different parent directory name than
  the local foundry convention. `_cluster_hotspots`'s glob assumed `rfd3/`
  and found nothing against a fully-scored, real campaign.
- **`scripts/resume_cluster_calibration.py` exists only because per-site
  resume has no CLI path.** `--start-from <stage>` operates on the
  TOP-LEVEL `binder/` stage files; a multi-site trial's real data lives
  under `binder/sites/<site_id>/binder/`, and `_run_site_trials` only
  re-enters through `--trial-sites N` (N>1) or `--stop-after trial`, which
  recomputes `n_batches` from `--trial-backbones` — it must be passed the
  exact original value or the completion check silently targets the wrong
  count. The script bypasses this by calling `_stage_calibration` directly
  with the real `site_dirs` and `n_batches`; it is the officially-supported
  way to resume a cluster campaign staged against one specific site until
  the CLI gap is closed.

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

## Three providers, one loop

`claude`, `gemini` and `openai` all run the same agentic loop, refusal
contract and four-bucket ledger. `_run_openai` speaks the **Responses API**
(`/v1/responses`) through `requests`, not a vendor SDK — the wire format is one
JSON POST and `requests` is already a direct dependency. Facts established
against live calls, not docs:

- **`run()`'s provider branch was `if claude: ... else: gemini`.** Any
  unrecognised provider silently sent Gemini-shaped messages to Gemini's
  endpoint. It is a three-way branch now; the trailing `else` stays because
  `curation.provider` also allows `"local"` (Ollama), which speaks the
  Gemini-compatible shape.
- **Every output item is replayed verbatim, `reasoning` included.** Echoing a
  `function_call` back without the `reasoning` item that preceded it is a 400.
  Same class of rule as Claude's "thinking blocks must carry their
  `signature`", and on a reasoning model dropping them degrades multi-turn
  tool use rather than erroring.
- **`input_tokens` INCLUDES the cached and cache-written share** — the Gemini
  convention, not Anthropic's. Measured: a repeated 4,635-token prefix
  reported `input_tokens=4635` with `cache_write=4632`, then `cached=4632`,
  the total unchanged. `Usage.input_tokens` is the UNCACHED share, so both are
  subtracted. **The Gemini path had this wrong** and billed every cached token
  at 1.0x + 0.1x instead of 0.1x — an over-report, so `--budget` was
  conservative rather than permissive, but wrong either way and gemini is the
  default provider.
- **`output_tokens` already contains `reasoning_tokens`.** Adding them again
  double-bills a thinking model, which is the opposite of the Gemini bug
  (`thoughtsTokenCount` there is separate and must be added).
- **The input CEILING reads the whole request; the LEDGER reads the buckets.**
  A 4,635-token request that is 99% cached is still a 4,635-token request.
- **Tools use the FLAT Responses shape** (`{type, name, description,
  parameters}`), not Chat-Completions' nesting under `"function"`, and
  deliberately without `strict: true` — `_TOOL_DEFS` schemas omit
  `additionalProperties: false` and do not list every property in `required`,
  so strict mode rejects all 24.
- **Both REST providers share one pooled `requests.Session` (`_http()`).**
  `requests.post` opens a fresh connection and completes a full TLS handshake
  per call, and an agentic stage makes one call per tool round trip — measured
  on the real OpenAI path, repeat calls went from **1,974 ms to 1,034 ms**, so
  a 15-call interface stage was paying ~14 s in handshakes. Module-level
  because nothing drives `SkillRunner` from threads (`binder_metrics`
  parallelises with separate PROCESSES) and the session is never mutated after
  creation — per-request headers carry the key, which is the part of
  `requests.Session` that is not thread-safe.
  **This moved the test seam**: patching `requests.post` now intercepts
  nothing and would let a test make a real call, so tests patch
  `src.skill_runner._http`. (Measure on an AUTHENTICATED call: a 401 probe
  shows no difference at all and reads as "pooling does not help".)
- **`tests/test_provider_contracts.py` is what stands in for an SDK.** The one
  thing a vendor SDK genuinely buys is tracking the API and failing loudly on
  a renamed field; hand-rolled REST reading raw JSON keys fails SILENTLY — a
  token bucket reads 0, a refusal goes undetected, or a tool loop never
  terminates, none of which raise. So the fields the loops depend on are
  asserted against one cheap live call per provider, marked `network` and
  deselected by default (`-m network` to run). It includes a canary that a
  `function_call` replayed WITHOUT its `reasoning` item is still a 400.
- **TLS to two of three providers is intercepted on the reference
  workstation.** `api.anthropic.com` and
  `generativelanguage.googleapis.com` present Fortinet-signed certificates;
  `api.openai.com` currently does not. The appliance therefore sees those API
  keys in plaintext headers. This is an accepted org decision here (Fortinet
  is the operator's security vendor), and it is noted only because it is
  **independent of REST vs SDK** — an SDK sends the same header over the same
  intercepted connection — so it is not a reason to change transport.
- **A cap now refuses an unpriced model.** `price()` returns $0.00 for a model
  with no rate entry, so `--budget 5` on one enforced nothing at all and only
  a "has_unpriced" advisory hinted at it. `TokenLedger.preflight` raises
  instead. Omitting `--budget` still runs unmetered — the refusal is about an
  unenforceable cap, not about unpriced models. `gpt-5.6-terra` is priced at the
  account's real $2.00/$12.00 per Mtok (2026-09-10) — ~2.7x/3.2x
  gemini-3.7-flash, so an `--provider openai` run costs several times the
  default's. **`gpt-5.6-luna` is still a PLACEHOLDER**; replace it with your
  account's before relying on a budget for it. Note the built-in `_PRICES`
  fallback in `token_budget.py` carries no OpenAI entry at all — every
  metering path calls `load_pricing(config)` first, and `preflight`'s refusal
  covers the case where one does not.

## Changing the default model is a measurement, not a version bump

`scripts/bench_models.py` compares two models on the PRODUCTION code path and
scores them only on things that are objectively checkable: for the `interface`
stage, whether each hotspot's residue NAME matches the structure at that
auth_seq_id, whether the stated `rfd3_atoms` exist on that residue, whether the
label_seq_id is gemmi's, whether the measured chain assignment survived, and
whether `_stage_binder_interface` would have let the campaign proceed; for
`pathway`, the pipeline's own `_verify_citations` DOI-in-corpus rate,
`identifier_normalizer` resolvability, and handoff completeness. 14 cells,
~$2.5, no GPU. `--rescore` re-derives every artifact-based metric from the
reports on disk without an API call, because two of those scorers were wrong
before the table was (see diary 2026-09-10) — a scorer that flags valid output
compresses exactly the difference it is meant to measure.

Verdict on gemini-3.8-flash (2026-09-10): **not switched.** Quality
indistinguishable (7/7 accepted both, zero misnamed hotspots, zero
hallucinated citations, same primary target on every query), cost a wash
(1.10x aggregate but cheaper on 4 of 7 cells, and a repeat of one cell moved
16% — n=1 cannot resolve it), and 1.80x wall clock, slower on 7 of 7. Its
token pattern is 0.82x input / 1.65x output: more of its own reasoning, less
retrieval, which for this pipeline is the wrong direction. It is priced in
config.yaml so `--model-id gemini-3.8-flash` and a `--budget` cap both work.

## One agentic loop, not one per entry point

`scripts/ask_corpus.py` is a thin front-end over `SkillRunner` running the
`corpus-explorer` skill — it does NOT have its own loop. It used to, hardcoded
to Anthropic with `search_corpus` as its only tool, and that second
implementation silently missed everything the shared runner had learned:
Gemini support, connection/429 retries, cross-provider refusal fallback, token
accounting, and the other seventeen corpus tools. Any new conversational entry
point goes through `SkillRunner` too.

**The Gemini key goes in the `x-goog-api-key` HEADER, never the query string.**
`requests` embeds the request URL in every exception it raises, so a key passed
as `params={"key": ...}` is printed in full by any connection error, timeout or
HTTP failure — to the terminal, into logs, and into whatever a user pastes into
a bug report. Observed live: an SSL failure printed a working key.

## foundry's venv name is one machine's accident

`design.foundry.{rfd3,mpnn,rf3}_bin` default to `.venv-blackwell/bin/*` because
this project's reference workstation had to hand-build that venv (its card is
sm_120, which the shipped container's torch was not built for). Nothing else is
Blackwell-specific, and every other GPU's foundry install uses a different venv
name. `foundry_runner._resolve_foundry_bin` therefore globs the checkout for
the binary when the configured path is absent, and refuses to guess between
several candidates — picking one could run a build compiled for a different
GPU. It resolves at `write_campaign_driver` time, so a wrong path fails before
the detached driver launches rather than after ten retries and five minutes.

The weights are resolved the same way: `LPT_FOUNDRY_CKPT_DIR` (or
`design.foundry.ckpt_dir`), falling back to foundry's own
`~/pip_rcfoundry_ckpt`. That path used to be hardcoded in three places, so a
user whose weights lived on a shared lab volume had no way to say so. The
driver runs DETACHED with its own environment, so the value is resolved at
write time and baked into the generated script — reading `$HOME` at run time
would resolve against whatever user the driver ends up running as.

## Safety-classifier refusals are an operational fact of this pipeline

The binder track's `interface` stage (`complex-structure-analysis`) is routinely
declined with `stop_reason: "refusal"`, category `bio`. Ledger evidence from
three separate refusals on one target (PD-L1): `claude-sonnet-5` refuses, then
`claude-opus-5` **also** refuses (same category), every time — a same-provider
retry after a categorised refusal has not once succeeded here, and it is not
free: ~$0.13 spent for nothing per attempt. `claude-haiku-4-5` eventually
answered in that same run, and that observation is now a warning rather than a
feature: see "A refusal is terminal" below.

**This is why `provider` defaults to `"gemini"` (`gemini-3.7-flash`), not
`"claude"`, for every pipeline stage** (`PipelineRunner.__init__`'s
`provider` default, `run_pipeline.py --provider`, `run_skill.py --model`) —
not only ~4× cheaper on input than Sonnet, but a real IL7RA end-to-end run
refused on **both** `target_intel` and `interface` under the old
claude-first default, the `interface` refusal alone burning $1.10 by the
time it fired: the refusal happens on whichever call the model finally
drafts substantive content (here, call #7 of an agentic tool-use loop —
calls #1–6 were pure tool orchestration with nothing for a classifier to
catch), and token usage is billed for that call *before* the
`stop_reason == "refusal"` check in `skill_runner.py` — the compute already
happened. Gemini answered cleanly both times. Since a refusal now ends the
run, which provider a run *starts* on is the whole of the decision, and that
is why this default is worth more than a cost comparison.

`_run_gemini` raises the same `SkillRefusedError` for a Gemini-side safety
block (empty `candidates`, a `promptFeedback.blockReason`, or a per-candidate
`finishReason` of `SAFETY`/`PROHIBITED_CONTENT`/`BLOCKLIST`/`RECITATION`/
`SPII`), and `_run_openai` for a `refusal` content part or an
`incomplete_details.reason` of `content_filter`. One contract, three
providers: the run ends the same way regardless of which declines.

A refusal raises `SkillRefusedError` rather than returning "". The old behaviour
wrote a 0-byte stage report and the run failed three stages later with a
confusing "no MODEL-READY HOTSPOTS" error.

**Do not reword prompts to get around a classifier.** Selecting a different
model deliberately is a legitimate operator decision; prompt engineering aimed
at defeating a safety check is not.

### A refusal is terminal — there is no fallback chain any more

`_run_stage` records the refusal and re-raises. **Nothing retries the stage on
another model.** `models.<provider>.refusal_fallbacks`,
`_REFUSAL_FALLBACK_MODELS` and `MAX_REFUSALS_BEFORE_STOP` are all gone; a
config that still sets the key gets a startup warning from
`PipelineRunner.__init__` rather than being silently ignored, because an
operator believing a retry will happen is the worst outcome.

That boundary moved twice, for one reason. The chain first stopped short of
`claude-haiku-4-5`, which had answered a PD-L1 interface stage Sonnet and Opus
both refused — reaching it meant the pipeline obtained content two better
models declined to produce. The same argument does not stop at the last rung:
**any** automatic retry is the pipeline deciding on its own to go looking for a
model that will produce what the operator's chosen model declined to, and no
reader of the output can tell that apart from a legitimate workaround for a
miscalibrated classifier. Those refusals often *are* miscalibrated for
structural-biology analysis — which is exactly why the judgement belongs to a
person who has read the refusal and can say why the target is legitimate,
rather than to a `for` loop that cannot. If a stage is consistently declined
for a target you believe is legitimate, raise an issue; the fix is never a
retry.

Overriding it is an operator action, and `_run_stage`'s error log names the
controls: `--provider`, `models.<provider>.stages.<stage>` (which may name
another provider as `"provider:model"`), and `--start-from <stage>` to resume
without re-paying for earlier stages. Those are steering controls, not a
sanctioned route past a classifier, and neither the log nor the docs claim
switching models works — walking model to model by hand is the deleted chain
with a person in the loop, and `docs/responsible-use.md` ("If you override a
refusal") says so and says what the override commits the operator to. Do not
add efficacy advice to either.

**`_DEFAULT_STAGE_MODELS` is now empty for every provider, and
`models.<provider>.stages` ships empty too** — LPT routes no stage to a
different model on its own. `summary` and `binder_summary` were pinned to
`claude-haiku-4-5` until this change, because Sonnet-class models decline the
"review of designed binders" task and Haiku answers it; that is a smaller model
producing content a larger one refused, which is what the fallback chain was
deleted for, and pre-declaring it in a config file does not change what it is.
The consequence is real and is documented in `config.yaml`: under `--provider
claude` those two stages now run on `claude-sonnet-5` and **may be refused,
ending the run at the final summary** — the designs and scores are already on
disk, the write-up is what is lost. The default provider answers them cleanly.
The tables stay as the hook (`_resolve_stage` reads them) for an operator who
has read a refusal and made the call themselves.

`_split_model_spec` (formerly `_split_fallback`) survived the deletion because
per-stage overrides have the same hazard the chains did: a bare `claude-opus-5`
under `models.gemini.stages` would inherit the CURRENT provider and be POSTed
to the Gemini endpoint, which 404s — and an `HTTPError` is not a
`SkillRefusedError`, so it kills the run instead of being reported as a
refusal. `_resolve_stage` now routes every per-stage model through it, so the
provider is inferred from the id's prefix (`claude-`/`gemini-`/`gpt-`) and only
an unrecognised id (a local/Ollama model) inherits the current provider.

Two things make a refusal visible rather than a log line: `_record_refusal`
writes a `refusal:<stage>` manifest checkpoint naming the model, category and
call number — the only account of why a run ended once the process is gone,
and what a later `--start-from` is read against — and
`_stage_provenance_note` appends a `## MODEL PROVENANCE` block to every stage
report that *is* written, which `run_provenance.footer_html` renders into both
HTML reports. The block goes in **before** the citation note, because
`report_common.extract_citation_section` matches to end-of-file and would
otherwise swallow it. `parse_provenance` and `run_provenance` still READ a
"declined first" list: no new run can produce one, but campaigns that ran
under the old behaviour have reports that carry it, and those are collectors
over whatever is on disk.

## Advisory select-agent screening, not a viral blocklist

`src/select_agents.py` name-screens the query, target complex, RCSB entry
title and chain descriptions against the Federal Select Agent Program list
before any GPU stage (`_screen_select_agents`, called from `_stage_pathway`'s
target selection and from `_run_binder_track` after target_intel). It
**warns, checkpoints and continues** — never blocks.

- **A "viral targets" filter was considered and rejected**, and the reasoning
  is in that module's docstring: a binder against a viral protein IS an
  antiviral, so the filter is inverted relative to the risk; "viral protein"
  does not partition cleanly; and `--workflow structure` takes any local
  file, so an input-side block is bypassed by renaming one.
- **Two patterns carry the whole check's usability.** SARS-CoV-2 is NOT a
  select agent while SARS-CoV is, so it needs an explicit `excluded_if` or
  every COVID structure fires and the check gets ignored. And matching is
  phrase-with-word-boundaries, never substring: that is what keeps `ricin`
  out of `ricinus` (castor bean, not listed) and `mallei` out of
  `pseudomallei`.
- The transcribed list goes stale — it is amended by rule. `SOURCE_REVIEWED`
  rides along in the checkpoint payload so a run records its vintage.

`src/run_provenance.py` collects one `provenance.json` per report directory
(regenerated whenever a report is, so old campaigns gain one). It is a
**collector**: every field is copied from an artifact that already existed,
and nothing in it re-derives a number.

## Persistent project layer

`src/project.py` manages `projects/<slug>/` with an atomic `manifest.json` (the filesystem source of truth for stage/checkpoint state + artifact pointers; `web.db` stays authoritative for the web UI). Layout: `shared/{structures,ligands}/` + `runs/<round-N>/scratch/`. `PipelineRunner(project=, round_id=)` mirrors stage state into the manifest (`_record_stage`) and computes `run_dir` from it. The binder track requires `--project` — it iterates in rounds, and the manifest is what makes a multi-day GPU campaign resumable. When adding a binder pause point, set a manifest checkpoint (`_binder_checkpoint`) so resume state survives.

## Two transports, opposite trigger rules

The same `SKILL.md` and the same tool surface run under two transports, and they
need OPPOSITE defaults:

- **MCP** (Claude Desktop / Code): must NOT auto-trigger. The model is in a
  general conversation, and the corpus is ~7k papers as released (14.5k in
  the maintainer's own checkout — `mcp_server._curated_papers` READS the
  count rather than stating one, because those differ) weighted to chromatin /
  chaperones — auto-searching it on a general question yields a narrower answer
  than the model's own knowledge, and makes the corpus's blind spots look like
  the field's. Both servers set FastMCP `instructions` saying so, no tool
  docstring contains "use this whenever", and every skill's frontmatter opens
  with "Invoke ONLY when the user explicitly asks".
- **API / pipeline** (`run_pipeline.py`, `run_skill.py`, `ask_corpus.py`): SHOULD
  auto-use. The skill was already explicitly invoked for a corpus task, so
  hesitating would just cost a turn. `skill_runner._TOOL_DEFS` and
  `vector_store.SEARCH_TOOL_DEFINITION` keep their trigger language.

These are separate description tables on purpose. Do not "fix" one to match the
other; tests assert both directions and that they have not converged.

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
- **Closed enums**: `study_type` and `study_category` are validated against the
  schema, and the enums are **parsed out of `extraction_schema.json` at import**
  (`curator.load_schema_enums`) rather than restated in Python — that file is now
  load-bearing, so schema and validator cannot drift. An out-of-enum value raises
  a `ValidationError`, which the retry loop feeds back into the same conversation
  as a correction turn. Adding a category means editing `extraction_schema.json`,
  `curation_prompt.md`, and the `study_category` enum in BOTH tool definitions
  (`skill_runner._TOOL_DEFS`, `vector_store.SEARCH_TOOL_DEFINITION`) — pinned by
  `tests/test_audit_fixes.py`. `study_category` has nine values: the six original
  plus `enzymology`, `biocatalysis` and `computational_chemistry`, which 565
  shipped fingerprints already used before anything validated them.
- **Curator output is strict JSON only**, no prose. Parsing in `src/curator.py` will fail loudly otherwise.

## Identifier resolution: official symbols win, ambiguity is refused

`src/identifier_normalizer.py` resolves a raw protein name to a human gene
symbol through ordered tiers (exact HGNC symbol -> curated biology alias ->
paralog-default -> HGNC alias -> prev_symbol -> UniProt ...). Two rules keep it
from inventing answers, both added after the corpus was found resolving `NAP1`
(a yeast name) to `ACOT8` and `p65` to `GORASP1`:

1. **A synonym may never be another gene's approved symbol.** `BAP1` is listed
   as a synonym of `RNF2` and is also the approved symbol of the deubiquitinase,
   so the synonym link is dropped at load time and `BAP1` always means BAP1.
   ~1,270 such collisions are dropped; `resolution_stats()` reports the count.
2. **If several approved genes still claim a synonym, resolve to nothing.**
   The old behaviour took the alphabetically-first claimant, which was right
   about half the time by luck. A refusal is final — it does NOT fall through to
   the UniProt tiers, which carry unreviewed entries literally gene-named
   `NAP1` (Q540F3) and `P65` (O43245).

`_BIOLOGY_ALIASES` (tier `curated_alias`, which runs BEFORE the alias tier) is
the escape hatch for names where refusing would lose an answer that is not
actually in doubt — `PD-1`, `p21`, `p62`, `p65`, `KAP1`, `NRF2`, `CAF-1` and
others, each commented with the competing symbols. Add to it rather than
loosening the two rules. `NAP1`, `RAS`, `AP-1`, `TFIIH`, `MLL4`, `ISWI` are
deliberately left unresolved.

**The corpus on disk still carries the old resolutions.** These rules apply at
resolve time; `data/fingerprints/*.json` keeps whatever
`normalize_identifiers.py` wrote. Re-running the backfill would change ~2,171
of 74,414 identifier entries (2.9%) — ~390 corrections, the rest becoming
unresolved — and the graph, edge index and clusters would need rebuilding after
it.

## Configuration layering

`config.yaml` is the main config; alternates (`config_search_expansion.yaml`, `config_flagship_journals.yaml`, `config_with_complexes.yaml`) are passed via `--config` to `fetch_papers.py` for targeted searches without polluting the primary keyword list. The current main config is focused on histone chaperones / chromatin biology — keyword sets rotate as research focus shifts.

## MCP launchers (Windows gotcha)

`scripts/launch_mcp.py` sets `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false`, and `HF_HUB_OFFLINE=1` before importing `sentence_transformers`. Without these, the literature-db server hangs at import on Windows. Don't bypass the launcher when registering with Claude Desktop. `scripts/launch_structure_tools.py` sets none of them and does not need to — structure-tools never imports `sentence_transformers`.

`HF_HUB_OFFLINE=1` means the embedding model must already be in the HuggingFace cache: on a machine that has never downloaded it, the first `search_corpus` fails with an HF error that names nothing about LPT. Warm it once with `python -c "from sentence_transformers import SentenceTransformer as S; S('NeuML/pubmedbert-base-embeddings')"`.

`.mcp.json` holds absolute, **machine-specific** paths, so it is NOT tracked in git (see `.gitignore`); each checkout generates its own. Run `python scripts/setup_mcp_json.py` to write one for the current venv and launcher paths — `README.md` "Migrating to a new machine" §4 has the context.

`scripts/setup_mcp_json.py` automates that fix: it regenerates `.mcp.json` for the current checkout's venv Python and launcher paths (backing up the previous file to `.mcp.json.bak`), as an alternative to hand-editing.

## Common file pairs to keep in sync

- `extraction_schema.json` ⇄ `src/curator.py` (the `Fingerprint` / `KeyFinding` /
  `PathwayContext` Pydantic models, validated in `curate_paper`) ⇄ `curation_prompt.md`
  — schema, validator, and prompt must agree. `src/models.py` is the *paper*-level
  model (`Paper`, `DownloadStatus`, `CurationStatus`), not the fingerprint one.
- `src/mcp_server.py` ⇄ `src/skill_runner.py` tool dispatch — same tool surface, two
  transports. `find_pdb_structures` / `search_rcsb_pdb` were CLI-only until this was
  fixed, so three skills silently degraded under Claude Desktop. ONE deliberate
  exception: `write_file` is CLI-only. The skills that want it name
  `filesystem:write_file` (a different server the user configures), and
  `complex-structure-analysis` forbids it — LPT exposing arbitrary file writes over
  MCP would be a security surface for no benefit. Tests pin both the parity rule and
  this exception. See `docs/mcp.md`.
- `src/structure_tools.py` (pure logic) ⇄ `src/structure_tools_server.py` (MCP wrapper) — server is a thin shim; logic lives in the former.
- `src/pipeline_runner.py` ⇄ each skill's "PIPELINE HANDOFF" output block.
- `src/pipeline_runner.py` `_STAGE_TO_SKILL` is **many-to-one** —
  `complex-structure-analysis` serves both `structure` and `interface`. (It used to
  hold two more: `design-analyst` served `summary` as well as `binder_summary`, and
  the retired `protein-design-script` skill served `design`; both of those stages
  went with the `boltzgen_legacy` retirement, so `design-analyst` is single-stage —
  do not let that tempt you into dropping the explicit `stage=`.) The inversion
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
  (`_parse_handoff` / `_parse_hotspot_residues`, now thin wrappers) ⇄ `src/binder_report.py`
  ⇄ `src/ppi_report.py` — extracted so both deterministic report generators can read the
  same "### PIPELINE HANDOFF" / "### MODEL-READY HOTSPOTS" blocks the pipeline itself
  parses, without depending on `PipelineRunner`. All call sites must keep using the
  module, not a re-implementation, or they will silently drift on the next skill-prompt edit.
- `src/report_common.py` (markdown rendering, `### PIPELINE HANDOFF` section splitting,
  `## CITATION VERIFICATION` extraction, histogram binning, the full-stage-report
  appendix, `safe_json`) ⇄ `src/binder_report.py` ⇄
  `src/ppi_report.py` — same reasoning as the `handoff.py` pairing above, one level up:
  both report generators read the SAME shape of markdown-plus-handoff stage output, so the
  reading logic lives once. `src/report_templates/_shared/{base.css,base.js}` is the
  matching pairing on the rendered side — both reports' `shell.html`/`app.js` source their
  palette, layout primitives, and Mol* explorer harness from there; see the binder-track
  section above ("Two reports, one design system").
- Configs: alternates passed via `--config` to `fetch_papers.py`. `curate_papers.py`
  has no `--config` flag; it reads the main `config.yaml`.
- `src/identifier_normalizer.py` (resolution) ⇄ `src/_corpus_graph.py`
  `_resolve_seeds` (alias-aware seeding) ⇄ `src/edge_index.py` (collapses edges by
  gene symbol) — all three must agree on what counts as one gene. A map is drawn on
  RAW graph nodes, one per name, while the persisted edge index collapses aliases
  first; before `_resolve_seeds` became alias-aware a map seeded on `YAP1` saw 169
  neighbours where the index had 291, so the two disagreed about the same data.
- `src/network_svg.py` — the single renderer for every interaction/DepMap map; see
  its module docstring for the visual grammar. Don't hand-roll a second one.
- `_verify_target_chain_assignment` ⇄ `_verify_ppi_chain_assignment` ⇄ `_verify_hotspot_grounding`
  ⇄ `_verify_partner_chain_is_requested` — the PD-L1/8ZNL/6E3Y guards. All called from
  BOTH `_stage_binder_interface` (binder) and `_stage_structure` (PPI); a future stage
  that also calls `complex-structure-analysis` needs the same calls, not a reason to
  skip them. **The partner guard must be given the complex the UPSTREAM stage settled
  on, never `handoff["target_complex"]`**: a stage that renames the complex to whatever
  it actually analysed would otherwise validate its own substitution. Asked for
  CALCRL/RAMP1 in 6E3Y (chain E *is* RAMP1), the structure stage analysed chain P — the
  38-residue CGRP agonist peptide — and renamed the complex to "CALCRL / CGRP"; chain R
  really is CALCRL, so every other guard passed. That substitution is also what put
  three hotspots inside the membrane, since the CGRP vestibule penetrates the TM bundle.
- `_check_structure_organism` runs at TARGET-SELECTION time (after pathway, before
  literature) and only warns: an ortholog is often a fine template, which is why
  `_check_ortholog_conservation` measures rather than assumes. It costs one GraphQL
  call and names the human alternatives — on 5GRS it reports that human SCAP structures
  6M49/7ETW exist, two LLM stages before the conservation gate would refuse the yeast one.
- **An `AF-<accession>` pseudo-id is a legal `pdb_id`** — `_ensure_structure` fetches the
  AlphaFold model — but ONLY with `design_intent: inhibit_active_site`, because the model
  is a monomer with no partner to disrupt. `_check_af_model_intent` enforces that pairing;
  both target-selecting skills now know the id is available, which is what a
  well-evidenced target with no PDB entry needs.
## PPI structure and chain selection is deterministic, not prompted

The binder track has always resolved its target to UniProt and ranked REAL
computed interfaces before an LLM sees anything (`target_resolve.
build_candidate_table`). The PPI track had none of that — it took whichever PDB
the corpus cited and inferred the chain pair from entity descriptions — and three
live runs showed that prompt guidance does not fix a mechanical choice. Four
deterministic steps now sit between the pathway stage and the structure stage:

- **`_select_designable_structure`** (between pathway and literature) prefers an
  entry whose dominant interface IS the requested one. Best-evidenced and
  best-designable are different questions: the corpus cites the landmark paper,
  which for a receptor is the full-length agonist-bound cryo-EM complex. On
  CALCRL it switches 6E3Y (3.3 Å, 7 chains, Gs + Nb35 + CGRP, 490-residue
  7TM target) to 3N7S, the 2.1 Å ectodomain complex — CALCRL ECD 115 aa with
  RAMP1 ECD 96 aa and nothing else. It **must** run here: the structure stage
  cannot switch entries, because by the time it emits a handoff its hotspots
  refer to one. It overrides an evidence-based choice, so it fires only on a
  measurable defect (partner absent, target chain is a fusion construct, or ≥2
  more scaffolding chains at no better resolution) and `--pdb` always wins.
  A switch rewrites the free-text `structure_query`/`design_query` too — leaving
  those stale made the literature stage cite a structure the run was not using.

  **A switch has to outlive the process that made it, and it leaked two ways.**
  Rewriting the *pathway* handoff is not enough: the LITERATURE stage runs after
  the switch and writes its own `design_query`, from a pathway report whose body
  still recommends the replaced entry (11 mentions on CALCRL/RAMP1 —
  `_note_structure_switch`'s correction is appended, so it lands after all of
  them). It duly emitted 6E3Y while the structure stage analysed 3N7S, and
  `_stage_design` passes `design_query` VERBATIM to the design-script skill, so
  on `--design-engine boltzgen_legacy` (then spelt `boltzgen`) the designer was
  handed the wrong entry.
  `_retarget_stale_structure` now runs on the literature handoff too. It rewrites
  only `_STRUCTURE_INSTRUCTION_FIELDS` — the `*_query` fields that are
  instructions to a later stage — and deliberately NOT `go_rationale` or
  `target_site_hint`: "cryo-EM structure (PDB 6E3Y), doi:10.1038/s41586-018-0535-y"
  is a true claim about the evidence, and substituting the id there would
  attribute one entry's paper to another. Rewrite the orders, keep the record.

  The second leak was worse and silent: `_select_designable_structure` only
  re-runs while `start_idx <= 1`, but `00_pathway.md`'s handoff block still reads
  `- pdb_id: 6E3Y`, and that is what a resume parses — so `--start-from structure`
  (the ordinary way back in after a stage failure or a prompt edit) reverted to
  the rejected entry and analysed it. `_reapply_recorded_structure_switch` reads
  the decision back out of the `structure_switched` manifest checkpoint instead,
  via the new `Project.checkpoint()` (`open_checkpoints` returns only PENDING
  ones, which answers "what is this run waiting on", not "what did it already
  decide"). `--pdb` still overrides it, on a resume exactly as on a fresh run.
- **`_ppi_interface_options`** hands the structure stage MEASURED interfaces for
  the chosen entry instead of letting it guess from descriptions. Measured, the
  two interfaces in 6E3Y are the same size (R/P 3858 Å², R/E 3862 Å²), so the
  stage was choosing the most conspicuous, not the largest. Bounded cost:
  interfaces for this entry only, alternatives listed from metadata.
- **`_verify_partner_chain_is_requested`** — see the guard list above.
- **`_check_structure_organism`** and **`_designable_chain_sizes`** — organism
  and size judged at selection time, on the number that will actually be
  designed against (a 574-residue GPCR fusion is 167 designable residues once
  the trim drops the TM span and the cytoplasmic face).

**Membrane targets: which site a binder can reach** is now stated in all four
skills that pick targets or residues (`pathway-expert`, `wildcard-expert`,
`molecular-biology-expert`, `complex-structure-analysis` — the last had no
mention of membranes at all). Reachable and clinically validated: class B ECDs
(erenumab blocks CALCRL/RAMP1), class C Venus flytraps, class F CRDs, and the
N-termini/ECLs of peptide-binding class A receptors. Not reachable: the
orthosteric pocket of a lipid-ligand receptor — inside the bundle and entered
LATERALLY from the bilayer — deep aminergic pockets, and any TM surface.

**`_TrimFromDisk` must carry every attribute the GPU stages read.** It rebuilds a
trim from `trim_map.json` so a resume need not redo it, and it silently lacked
`n_residues_after` from the day `plan_campaign` started sizing RF3 by token
count — which killed EVERY fresh-process `--start-from pilot|calibration|
production` for ten days, the documented normal case after a multi-day campaign.
`tests/test_audit_fixes.py` checks it by reflection over every `trim.<attr>` in
the module, so the next field added to `TrimResult` cannot reintroduce it.

- `_bridge_ppi_to_binder_track` ⇄ `_run_binder_track`'s `"target_intel"`/`"interface"` stage-file
  loading (`_load_binder_handoff`) — the bridge writes synthetic/copied artifacts at those
  exact paths (`_BINDER_STAGE_FILES`) because `_run_binder_track` always reads them off disk
  regardless of `start_from`; changing that stage-file format on one side without the other
  breaks the hand-off silently (a `{}` handoff, not a crash).

## Frontend

`web/frontend/` (Vite + React 19 + TypeScript) is part of the excluded web platform — see "Two intertwined systems" §3. Not part of the release and not maintained; don't wire new pipeline features into it.
