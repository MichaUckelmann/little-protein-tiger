# Retiring `boltzgen_legacy`

Scope, 2026-09-13. Written against the condition the request set: *retire it
only if there is no chance of damaging the currently working pipeline.*

## Verdict

**The PATH can be retired safely. The MODULES cannot be deleted.**

Those are two different jobs, and conflating them is the whole risk here. The
stage chain (`design → execution → analysis → summary`), its four CLI
refusals, its config blocks and its three driver scripts are cleanly
separable — nothing live reads them. But `src/design_ranking.py` and
`src/design_metrics.py`, which every document in this repo describes as the
legacy scoring path, are **load-bearing for both live engines**:

    src/binder_ranking.py:40   from src.design_ranking import _seq_identity, mmr_select
    src/binder_ranking.py:397      picks = mmr_select(...)          # foundry's diversity pick
    src/pipeline_runner.py:4229    from src.design_ranking import (  # bridged BoltzGen gate
    src/pipeline_runner.py:4365     gate_boltzgen_records, resolve_boltzgen_ranking)
    src/pipeline_runner.py:4203    from src.design_metrics import parse_boltzgen_outputs
    src/pipeline_runner.py:4206    for rec in parse_boltzgen_outputs(paths.campaign_dir)

`mmr_select` is imported beside a private name (`_seq_identity`), which reads
like an internal detail nobody depends on, and `binder_ranking` defines its
OWN `rank_designs` / `write_ranking_outputs`, so a grep for those names
returns hits in both modules and makes the legacy pair look shared while the
genuinely shared function looks private. That is the trap.

So: retire by **de-advertising and then deleting the chain**, and treat the
module layer as a separate, surgical, last step — or not at all.

## Is the condition for keeping it discharged?

`UNIFY_BOLTZGEN_BACKEND_NOTES.md:98-100` (decision 2) keeps the path alive
"until the new path is proven unnecessary", and `:150` (C4) puts retirement
out of scope pending the acceptance runs. Those runs have now happened:

| what | evidence |
|---|---|
| bridged BoltzGen on the **PPI** track, through the bridge | `projects/e2e_boltzgen` round-2, finished 2026-09-13 06:50: pathway → literature → structure → bridge → trim → spec → pilot → calibration, paused at `calibration_verdict` **SCALE_UP** — 6/588 backbones excellent (1.02%, 95% CI 0.47-2.21%), adaptive bar raised 0.5 → 0.6, sized at 44.9 GPU-h / 10.4 GB |
| bridged BoltzGen on the **binder** track, including a macrocycle | `projects/pdl1_macrocycle`: `20_target_intel.md` .. `25_calibration.md` on disk, then production (1,891 refolds before a deliberate pause) |
| the legacy chain itself | **no stage report exists anywhere** — `03_design.md` is absent from every directory under `projects/` and `outputs/` written since the refactor, and its last invocation died in one second on `'str' object has no attribute 'root'` |

The macrocycle capability that used to be legacy's alone (`scripts/run_pipeline.py:522-529`
still exempts it from the modality coercion, noting "both shipped cyclic
campaigns ran on it") is therefore no longer exclusive.

**One honest gap:** no single bridged-BoltzGen run has yet gone PPI →
production → scoring in one pass. PPI is proven to calibration and the binder
track to production; the two halves have not been proven end to end together.
That is an argument for doing step 0 (reversible) before steps 3-7, not an
argument for keeping a path that has never run.

## Legacy-only, safe to remove

- **Stage methods**, `src/pipeline_runner.py:6217-7075`: `_stage_design`,
  `_stage_execution`, `_stage_analysis`, `_stage_summary` and their private
  helpers (`_find_design_yaml`, `_render_execution_report`,
  `_SUMMARY_CONTEXT_COLS`, `_slim_top_k_for_context`, `_write_top_k_fasta`,
  `_boltzgen_output_chains`, `_render_analysis_report`).
- **`STAGE_ORDER` indices 3-6** (`:349`) and `_STAGE_TO_SKILL["design"]` /
  `["summary"]` (`:159-160`). Indices 0-2 are shared PPI discovery.
- **`src/design_runner.py`** in full — its only importers are
  `_stage_execution` and `scripts/run_boltzgen_campaign.py`. Zero test imports.
- **Scripts**: `scripts/e2e_ppi_boltzgen.py`, `scripts/resume_e2e_cgas_sting.py`,
  `scripts/stress_test_chunk3.py`, `scripts/run_boltzgen_campaign.py`,
  `scripts/calibrate_boltzgen_thresholds.py`.
- **`skills/protein-design-script/`** — but see the tails in the next section.
- **Config**: `design.pilot.*`, `design.production.*`, `design.thresholds.*`,
  `design.ranking.{enrich_top_k,weights,mmr}`, `design.workstation.timeout_hours`,
  and the phantom `design.workstation.binder_chain` (read at `:6584`, never
  present in `config.yaml`).
- **Refusals that become dead code**: the legacy-off-PPI `ValueError`
  (`src/pipeline_runner.py:527-539`) and its CLI twin, the `--stop-after`
  refusal (`scripts/run_pipeline.py:549-560`) and the legacy
  `--compute`/`--n-gpus` refusal (`:561-563`).
- **`PipelineResult.design_files`** (`:286`), printed at
  `scripts/run_pipeline.py:833-835`.
- **Artifacts**: `03_design_report.md`, `03_design_inputs/`, `04_execution.md`,
  `04_execution_outputs/`, `05_metrics_enriched.csv`, `05_analysis.md`,
  `05_ranking/`, `06_summary.md`, `06_top_k.fasta`. Note the collision trap:
  the bridged tracks also write `filter_stats.txt` and `top_k.csv`, under
  `binder/scoring/` (`:4386-4400`, `:5076`) — different directory, different
  columns.

No manifest field is legacy-only in a schema sense: `Project.update_stage`
(`src/project.py:241-266`) upserts free-form keys, and the legacy stages never
call `_record_stage` or `_binder_checkpoint` themselves. The `stages["design"]`
/ `stages["summary"]` keys simply stop appearing, and nothing reads them back.

## Looks legacy-only, is load-bearing — do NOT touch

1. `design_ranking._seq_identity` / `mmr_select` → foundry's ranker (verified).
2. `design_ranking.resolve_boltzgen_ranking` / `gate_boltzgen_records` /
   `DEFAULT_BOLTZGEN_*` / `BoltzGenRankingConfig` (`:513-638`) → bridged
   BoltzGen calibration and scoring. Appended to a legacy module deliberately
   (`UNIFY_BOLTZGEN_BACKEND_NOTES.md:51`).
3. `design_metrics.parse_boltzgen_outputs` and `DesignRecord` → bridged
   BoltzGen, and the chain in (1).
4. `src/boltzgen_runner.py`, `src/boltzgen_spec.py`,
   `scripts/build_boltzgen_spec.py`, `design.boltzgen.*`,
   `design.boltzgen_ranking.*` — these are the REPLACEMENT, not the thing
   being retired. The name collision is the main hazard for a careless grep.
5. `design.ranking.top_k` (`config.yaml:784`) — the rest of that block is
   legacy-only, but `top_k` is read by bridged BoltzGen scoring
   (`:4385`, verified). Deleting the `ranking:` block silently re-defaults it to 20.
6. `design.workstation.boltzgen_executable` / `cuda_device` — read by the
   bridged path at `:4092-4093`.
7. `--project` required (`:611`, `scripts/run_pipeline.py:606`) — guards all
   three tracks now; legacy was only the last hold-out.
8. `--hotspots` refused on `--workflow ppi` (`:577-582`) — engine-independent.
9. `_refuse_undispatched_site_trials` (`:3932`) — gated on `_boltzgen_backend`,
   so it guards **bridged** BoltzGen. Reads as legacy-era; it is the opposite.
10. The `--compute cluster` / `--n-gpus` refusal at `scripts/run_pipeline.py:565-575`
    — near-identical wording to the legacy one two lines above, but it applies
    to bridged BoltzGen. Deleting the wrong one lets a BoltzGen campaign run
    locally while the operator waits for a submission script.
11. `_boltzgen_backend` (`:3907`) — half its docstring is about legacy; the
    property is the live foundry-vs-BoltzGen dispatch seam.
12. ~~`PipelineRunner._MODALITY_TO_PROTOCOL`~~ — RESOLVED at step 2 by
    deleting both the table and the cross-check and recording the lesson at
    `boltzgen_spec.PROTOCOL_BY_MODALITY`, which is now the single source of
    the modality→protocol mapping.
13. `src/pyrosetta_sasa.{check_available,resolve_interpreter,pyrosetta_mode}` —
    live. Only `compute_hotspot_sasa` (`:153`) becomes orphaned.
14. `src/report_common.py` + `src/report_templates/_shared/` — shared with
    `binder_report`.
15. `skills/design-analyst/` — serves `summary` (legacy) AND `binder_summary`.
16. `tests/test_ppi_backend_routing.py` — 8 of its 19 tests are the bridge's
    primary routing coverage. Do not delete the file as a unit.

### `src/ppi_report.py` is legacy-only, and CLAUDE.md currently implies otherwise

CLAUDE.md's "Two reports, one design system" reads as though the PPI report is
live. It is not, for two independent reasons: its only callers are
`:980`/`:988`, after `_stage_analysis` and `_stage_summary`, and a bridged PPI
run `return`s at `:952` and never reaches them (verified) — it gets
`src/binder_report.py` instead; and `_resolve_designs` (`src/ppi_report.py:229-238`)
raises unless `05_metrics_enriched.csv`, `05_ranking/top_k.csv` and
`05_ranking/filter_stats.txt` all exist, all written only by `_stage_analysis`.
It remains the only renderer for the two archived legacy runs in `outputs/`,
which four test files and `docs/showcase/build_ppi.py` read. **That documentation
correction is worth making whether or not anything is retired.**

## Staged plan

Each step is independently revertable and leaves the suite green.

**Step 0 — de-advertise. DONE 2026-09-13.**
Dropped `"boltzgen_legacy"` from `--design-engine` choices
(`scripts/run_pipeline.py:250`) and replaced the `if is_legacy:` block with
one `parser.error()` naming the retirement and pointing at
`--design-engine boltzgen`. The check survives the choices list losing the
value because `design.backend` in `config.yaml` can still name it.

**Corrected while doing it:** this step originally also said "widen the
runner's `ValueError` to refuse it on every workflow", which CONTRADICTS the
same step's promise that `scripts/e2e_ppi_boltzgen.py` stays reachable — that
driver constructs `PipelineRunner` directly, so a runner-level refusal blocks
it. The runner-level widening belongs to step 2, where the stage chain goes
and the driver is deleted with it. A test now pins the seam deliberately
(`test_the_runner_still_accepts_it_for_a_library_caller_on_the_ppi_track`) and
says to delete itself at step 2. Nothing else moved: every module, config key
and artifact reader stays, so the live paths cannot regress. Test cost:
re-point `tests/test_ppi_backend_routing.py:168-179` and `:191-204`, and drop
the engine from two parametrisations in `tests/test_pipeline_stages.py:351`/`:373`.
`scripts/e2e_ppi_boltzgen.py` still reaches the path by constructing
`PipelineRunner` directly, which keeps decision 2's "genuinely reachable"
promise in the one place documented as the only safe way to run it. This step
is a de-advertisement, not a retirement.

**Step 1 — delete the dead drivers. DONE 2026-09-13.**
`scripts/resume_e2e_cgas_sting.py`, `scripts/stress_test_chunk3.py`. Zero
importers, zero tests — re-verified by grep before deleting (the only other
mentions anywhere are in `diary.md`, which is a record and stays).

**Step 2 — delete the stage chain. DONE 2026-09-13.**
Deleted `src/design_runner.py`, `scripts/run_boltzgen_campaign.py`,
`scripts/e2e_ppi_boltzgen.py`, `scripts/calibrate_boltzgen_thresholds.py`; the
stage methods and their helpers; `_MODALITY_TO_PROTOCOL`; the `run()`
fall-through; `_generate_ppi_report`; `PipelineResult.design_files`;
`STAGE_ORDER` indices 3-6; `_STAGE_TO_SKILL["design"]`/`["summary"]`. The
runner-level refusal is widened to every workflow and names the retirement.
`_bridges_to_binder_track`, `_BRIDGED_ENGINES` and `_boltzgen_backend` are
kept exactly as they were, and so is the lazy `design_ranking`/`design_metrics`
import trio inside the bridged-BoltzGen stages. `src/ppi_report.py` and its
templates stay (step 5).

**Done differently from the plan, or found wrong in it:**

- The methods are at `:6217-7076`, not `:6217-7075` (the plan's count was one
  line short — inventory line numbers, as its own Provenance section warns).
- `_MODALITY_TO_PROTOCOL` was **not** carried forward. It was a duplicate of
  `boltzgen_spec.PROTOCOL_BY_MODALITY`, so carrying it would have kept the
  drift hazard the cross-check existed to catch while removing its only
  consumer. `tests/test_boltzgen_spec.py::test_protocols_match_the_pipeline_runners_table`
  is deleted and the lesson it carried is written at
  `PROTOCOL_BY_MODALITY`'s own definition, which is now the single source.
- **The top-level `from src.design_metrics import (...)` went entirely**, not
  just the two names the plan listed. `parse_boltzgen_outputs` was the third,
  and its only top-level consumer was `_stage_analysis`; the live use is the
  LAZY import inside `_boltzgen_records`, which is kept. A
  top-level import of it would now be dead.
- **Two extra things in `run()` died with the chain** and the plan did not
  list them: the `start_idx > 3` hotspot-recovery block (it existed so
  `_stage_analysis` could compute hotspot SASA on a resume, and STAGE_ORDER no
  longer HAS an index past 3), and the `force_production` kwarg (its only
  consumer was `_stage_execution`; nothing in the repo ever passed it).
- **The PPI track now has no stage name between `structure` and the bridge.**
  `--start-from design` used to be the way to enter a run right after the
  go/no-go decision, and `tests/test_ppi_backend_routing.py` used it for
  exactly that. That test now stubs `_stage_structure` instead. Worth knowing
  before adding another test that wants that entry point: a PPI campaign
  resumes at a BINDER stage name, never at a PPI one past `structure`.
- `scripts/run_pipeline.py`'s `is_legacy`/`runs_binder_stages` are gone; the
  `--success-metric` and `--n-gpus` blocks they gated are now unconditional,
  which is correct (every surviving track runs the binder-track stages). The
  `--compute cluster`/`--n-gpus` refusal for **bridged** BoltzGen — item 10 of
  "do NOT touch" — is untouched, as are `--hotspots` and `--project`.
- `scripts/test_e2e.py` and `scripts/test_e2e_cgas_sting.py` survive but their
  `--pilot`/`--production` flags are now inert: they write `design.pilot` /
  `design.production`, which only the legacy chain read. Said so in their
  docstrings rather than removing the flags, which belongs with step 4's
  config pruning.

**Step 3 — delete `skills/protein-design-script/`. DONE 2026-09-13.**
The six reference files inside it were `git mv`'d to **`docs/engine-references/`**
(not dropped — they pin the contig/spec formats `src/foundry_spec.py` and
`src/boltzgen_spec.py` validate against, for the two LIVE engines), and every
attribution repointed: `THIRD_PARTY_LICENSES.md` (both blocks) and
`README.md`'s bundled-material list. Deleted `SKILL.md`, the directory and
`skills/protein-design-script.zip`; pruned the member from
`skill_runner._WRITE_FILE_SKILLS` / `_NEEDS_INDEX_MAPS` (both sets kept, both
still have members, and the CLI-only-`write_file` exception is intact) and
from `pipeline_runner._STAGE_CALL_PRIOR`. Repackaged the zips.

**Done differently from the plan, or found wrong in it:**

- The test to delete is `tests/test_ortholog_check.py:984`
  (`test_the_design_skill_no_longer_says_copy_verbatim`), not `:985` — and
  the plan under-counted the tails. Two more needed editing:
  `test_release_fixes.py::test_lpt_does_not_expose_a_file_writing_tool_over_mcp`
  named the skill in its docstring as one of the two askers for
  `filesystem:write_file` (the rule is unchanged; only `orchestrator` asks
  now), and `test_packaged_skill_zips_match_their_source` **`continue`d past a
  zip whose source directory was gone**, so `protein-design-script.zip` could
  have kept shipping an installable retired prompt silently. That check is now
  an error there and in `scripts/package_skills.py` (which exits non-zero on
  an orphan).
- **Six files, not "RFD3_reference.md, boltzgen_reference.md and four
  others" as though all six were third-party.**
  `example_output_KRAS_RAF1_PPI_analysis_6XHB.md` is LPT's OWN
  `complex-structure-analysis` output, attributed nowhere and licensed like
  the rest of the repo; it moved with the set because it is the worked example
  of the report format the deterministic spec builders parse.
- **No surviving skill referenced the vendored files by path** (checked:
  `binder-optimizer`, `complex-structure-analysis`, all of `skills/`), so
  nothing dangles on the prompt side. Three skills DID name the deleted skill
  in prose and all three would have instructed a model to invoke it:
  `orchestrator` (a whole Stage 4 — replaced with a "hand off to
  `scripts/run_pipeline.py`" section, and the pipeline overview is now three
  expert skills ending at the go/no-go recommendation),
  `molecular-biology-expert` (two handoff notes) and
  `complex-structure-analysis` (one line claiming the design skill reads its
  hotspot table "in Stage 4" — it is `src/foundry_spec.py` /
  `src/boltzgen_spec.py` that parse it now, which is worth stating precisely
  because it makes the table's exact shape load-bearing).
- **`src/ppi_report.py:337` still names it, deliberately.** That is the
  provenance footer of the two ARCHIVED legacy runs, where "the design spec
  through `protein-design-script`" is a true claim about how that run was
  produced. Rewriting it would misdescribe the archived report.
  `README.md:379`'s retirement note and `CLAUDE.md`'s `_STAGE_TO_SKILL` note
  name it historically too, and stay.
- `UNIFY_BOLTZGEN_BACKEND_NOTES.md:29` ("the `protein-design-script` skill
  [is] not retired yet") is now false, and is left alone as a dated decision
  record — the same treatment `diary.md` gets.

**Step 4 — config pruning. DONE 2026-09-13.**
Removed `design.pilot.*`, `design.production.*`,
`design.ranking.{enrich_top_k,weights,mmr}` and
`design.workstation.timeout_hours`. Kept `design.ranking.top_k` (re-verified:
read at `pipeline_runner.py:4345-4346`, bridged BoltzGen scoring) and
`design.workstation.{boltzgen_executable,cuda_device}`, each with a comment
saying which live reader keeps it. Fixed the `design.backend` comment, which
named `PipelineRunner._bridge_ppi_to_foundry` — a method that does not exist
(it is `_bridge_ppi_to_binder_track`) — the stale `# Stage 5 (analysis +
ranking).` header on `design.boltzgen`, the `boltzgen_ranking` header's claim
that the blocks below are live legacy, and `design.constraints`' "at the
protein-design-script step". `README.md`'s config list and `CLAUDE.md`'s
`enrich_top_k` mention followed.

**`design.thresholds` is KEPT**, annotated as surviving for one reader.
Reasoning, since the plan listed it as legacy-only: `src/ppi_report.py:456-457`
reads `iptm_min` and `hotspot_sasa_delta_min` to draw the histogram gate
markers for the two archived legacy runs, and `ppi_report` survives until step
5. Its in-code fallbacks (`0.6`, `30.0`) happen to equal the shipped values,
so deleting the block would not move a marker *today* — but it would silently
transfer the archived reports' gate lines to two literals in `ppi_report`,
where the next edit to either has nothing to disagree with, and
`tests/test_design_ranking_regression.py` reads the block as the
shipped-defaults contract for `filter_records` (still live, called by
`ppi_report`). Both go together at step 5, or not at all.

`scripts/test_e2e.py` and `scripts/test_e2e_cgas_sting.py` lost their
`--pilot`/`--production` flags and all four `cfg["design"][...]` writes
(`pilot`, `production`, and the three `thresholds` loosenings), with the
docstrings saying why. Deliberately NOT repointed at
`design.boltzgen_ranking` / `design.binder_ranking`: that would invent new
sizing behaviour for a smoke-test driver. `test_e2e_cgas_sting.py`'s pause
handler also named `pilot_failed`, a pause point that no longer exists; it
now names `calibration_verdict` and says how to resume.

**Step 5 — `src/ppi_report.py` and its templates.** LAST, and only after
deciding the two archived runs need no renderer: it costs ~35 tests across
three files and the showcase's side-by-side narrative.

**Step 6 — the module layer. Surgical or not at all.** `design_metrics` can be
reduced to `DesignRecord`, `parse_boltzgen_outputs`, `_coerce_value`,
`_REFOLD_DIR`; `design_ranking`'s legacy half (`filter_records`,
`composite_score`, `rank_designs`, `write_ranking_outputs`, `FilterStats`,
`RankingResult`) can go if step 5 happened. `_seq_identity` / `mmr_select` and
the whole `resolve_boltzgen_ranking` block must survive — ideally moved into
`binder_ranking` and a new `boltzgen_ranking` module respectively, which is a
refactor with its own regression risk and deserves its own scope. **Stopping
after step 4 is a perfectly good outcome.**

## Checks per step

Not just `pytest`: the live paths have cheap non-test proofs, and they are what
would catch a silent regression.

    .venv/bin/python -m pytest tests/ -q                    # 1239 green today
    .venv/bin/python scripts/foundry_regression_baseline.py  # foundry ranker + gates
    .venv/bin/python scripts/doctor.py                       # engine/env resolution

plus, after step 2 and after step 6, a no-GPU spec build on both live engines
(`--stop-after spec`) against an existing project, and a re-score of an
archived campaign — the composite ranking is where a `mmr_select` regression
would show up as a reordering rather than an error.

## Provenance

The shared-vs-legacy split came from a full-repo inventory; the seven claims
the plan's safety rests on (items 1-3, 5, 12 above, the `ppi_report` call
sites, and `tests/test_ortholog_check.py:985`) were each re-verified by hand
against the source before being written here. Everything else — line numbers
in the legacy-only lists, the test-cost estimates — is from that inventory and
should be re-checked as each step is taken, not trusted as a count.
