# Making BoltzGen a plug-in backend — working notes

Sibling of `UNIFY_DESIGN_BACKEND_NOTES.md`, which scoped the other direction
(PPI → foundry). This one scopes BoltzGen as an alternative backend for the
**binder** track, and records what was measured getting there.

**Status: phases A, B and C complete and committed (unpushed). The acceptance
runs are next — they were deliberately deferred so they test the final
implementation rather than an intermediate one.**

---

## Operator decisions already taken

1. **BoltzGen keeps its own scoring**, with `binder_metrics` re-scoring as a
   later opt-in. Measured reason: BoltzGen's ranker is a MAXIMIN over six
   per-metric ranks and is a strong dock selector (numbers below).
2. **The legacy PPI BoltzGen stages stay alive** until the new path is proven
   unnecessary. So `design/execution/analysis` and the
   `protein-design-script` skill are not retired yet.
3. **`--project` should be required everywhere**, for consistency. **DONE in
   Phase C** — one check in `run()` and one in the CLI, every track, every
   engine.
4. **The trim was reviewed for macrocycles** and needed one fix (done, commit
   `5c17881`). One item was deliberately left open — see "Open, deliberately".

---

## What is committed (11 commits, on `main`, NOT pushed)

    470340b  affordable e2e driver for the legacy PPI BoltzGen stages
    5da72a8  B4: dispatch the binder track's generator stages
    5d87f50  inject the GATE; split "valid design" from "good design"
    1ca7bab  design.boltzgen_ranking config block, per modality
    ee20c10  B3: pluggable CostModel
    ae1394b  B2: src/boltzgen_runner.py
    25f2ab7  B1: src/boltzgen_spec.py
    5c17881  binder length defaults by modality
    760dbef  Phase A: four silent-zero bugs + the resume that died after the GPU
    4beb25b  CLAUDE.md: non-obvious facts the BoltzGen backend depends on
    f12a752  z_clip, vbuns weight, require_boltzgen_pass in the drivers

`git log --oneline origin/main..HEAD` lists them. 1153 tests pass.

## New modules and where the seam is

| file | role |
|---|---|
| `src/boltzgen_spec.py` | the design YAML, deterministically; three refusals |
| `src/boltzgen_runner.py` | detached campaigns, disk progress, the two cost laws |
| `src/design_ranking.py` | `resolve_boltzgen_ranking`, `gate_boltzgen_records` (appended) |
| `src/campaign_calibration.py` | `CostModel` + injectable `gate=` |
| `src/pipeline_runner.py` | `_boltzgen_backend` + 4 stages + 8 branches in `_run_binder_track` |
| `scripts/build_boltzgen_spec.py` | thin CLI over the spec module |
| `scripts/run_boltzgen_campaign.py` | drive a campaign directly, sized by argument |
| `scripts/calibrate_boltzgen_thresholds.py` | threshold ladder over a finished run |
| `scripts/measure_boltzgen_binder_gates.py` | score a BoltzGen run with BINDER metrics |
| `scripts/e2e_ppi_boltzgen.py` | sized e2e on the LEGACY PPI stages |
| `scripts/foundry_regression_baseline.py` | **the regression net — read its docstring** |

The dispatch is `PipelineRunner._boltzgen_backend` (true when
`design_engine == "boltzgen"`), branched at five stages inside
`_run_binder_track`: spec, pilot, calibration, production, scoring.
`target_intel` / `interface` / `trim` are untouched — already
generator-neutral. **Foundry is always the `else` arm**, so all 7 of its
original calls are visibly unchanged.

---

## Phase C — what was built

**C1. PPI + BoltzGen now takes the bridge.** The hand-off after go/no-go asked
`self._design_engine == "foundry"`, so `--design-engine boltzgen` reached the
LEGACY stages for its whole first release. Nothing failed; the run just
silently had no trim, no measured production size, no `--stop-after` and no
per-stage manifest, and reported NO_GO off ten designs.

The fix is one predicate, not a second bridge:

- `_DESIGN_ENGINES = ("foundry", "boltzgen", "boltzgen_legacy")` and
  `_BRIDGED_ENGINES = ("foundry", "boltzgen")`, module-level in
  `src/pipeline_runner.py`. Written as the positive set so adding a generator
  opts it IN rather than silently leaving it on the legacy path.
- `_bridges_to_binder_track` (new property) is read by the three places that
  ask: the resume dispatch, the hand-off after go/no-go, and
  `_stage_structure`'s designable-size hint (which was gated on foundry and
  therefore quoted a raw 574-residue GPCR at a BoltzGen run that WILL trim it
  to 167).
- It is a DIFFERENT question from `_boltzgen_backend`, which selects the
  generator a binder-track stage dispatches to. `boltzgen_legacy` is False for
  both — it neither bridges nor dispatches — and conflating them would send it
  into the new stages under the old name. A test pins all six combinations.
- `_bridge_ppi_to_foundry` → **`_bridge_ppi_to_binder_track`**, and its
  internals now read no engine at all beyond naming it in a log line and two
  error messages. A test asserts the absence of `_boltzgen_backend`,
  `== "foundry"` and `== "boltzgen"` inside it: a backend-specific line there
  would be a second dispatch waiting to disagree with `_run_binder_track`'s.
- The bridge's hardcoded `size.get("min", 70)` is gone — both handoff
  composers (the bridge and the structure-first track) now go through
  `_binder_length_range`. That is a 5.8x error for a macrocycle, it is the
  mistake commit `5c17881` fixed one level up, and BoltzGen is precisely the
  engine that can be asked for one.

**`boltzgen_legacy` is a real engine name, not a deprecation marker.**
Decision 2 keeps that path alive, so it has to stay genuinely reachable, and
the only honest way to reach it once `boltzgen` means the bridged backend is
to name it. It is refused off the PPI track (both in the CLI and in
`__init__`), because accepting it on the binder track would silently run the
NEW backend — `_boltzgen_backend` is false for it.
`scripts/e2e_ppi_boltzgen.py` now names it explicitly.

**C2. `--project` on every track and every engine.** One check at the top of
`run()` (before a run dir exists or a token is spent) replacing the
foundry-only one, and one in the CLI replacing three per-track ones. Widens
the requirement to `boltzgen_legacy`, which was the single combination that
ran without a project — and the one whose multi-hour campaign had nowhere to
record that it had started.

**C3. `--stop-after` is inherited by PPI + BoltzGen for free** (it is a
binder-track flag and that is now the route), and **REFUSED on
`boltzgen_legacy`** rather than ignored. That matters: the legacy stages run
design → execution → analysis → summary unconditionally, and at the shipped
`design.production.num_designs` that is 332 GPU-h / ~13 days at YAP1/TEAD1
size — for an operator who believed they had capped it.

Three more flags were refused rather than left to do nothing:

- `--compute cluster` / `--n-gpus` on **either** BoltzGen engine.
  `_run_boltzgen_stage` has no cluster path; only the foundry stages stage
  onto shared storage. Accepting it would run the whole campaign on the local
  GPU while the operator waited for a submission script.
- `--success-metric ipsae_min` on BoltzGen. No PAE matrix, so its own ipsae
  column spans 0.0000–0.0289 against an RF3-calibrated bar of 0.5 — every
  campaign sizes to STOP.
- `--success-metric` generally now writes **the block its engine reads**:
  `design.boltzgen_ranking` for BoltzGen, `design.binder_ranking` for
  foundry. Writing into the wrong one is silent; the campaign is simply sized
  at the default bar.

**C4. Legacy retirement remains out of scope** (decision 2).

New tests: `tests/test_ppi_backend_routing.py` (19), plus the `--project`
parametrisations in `tests/test_pipeline_stages.py` and a broadened
designable-size gate test in `tests/test_audit_fixes.py`.

## The acceptance runs (next)

All verified to parse. `auto_mode` defaults to `True`; there is no `--auto`
flag.

    # 1. the B1-B4 acceptance test AND the showcase addition
    python scripts/run_pipeline.py --workflow binder --target PD-L1 \
        --modality cyclic_peptide --project pdl1_macrocycle \
        --budget 5 --stop-after calibration          # ~3.2 GPU-h

    # 2. PPI e2e, foundry, via the bridge
    python scripts/run_pipeline.py --workflow ppi \
        --query "Design cancer therapeutics to target key nodes in mesothelioma." \
        --design-engine foundry --project e2e_foundry \
        --budget 5 --stop-after calibration          # ~2-3 GPU-h

    # 3. PPI e2e, legacy BoltzGen stages
    python scripts/e2e_ppi_boltzgen.py --designs 200 --budget-usd 5
                                                     # ~3.5 GPU-h

    # 4. PPI e2e, BoltzGen through the NEW backend — what C1 exists for
    python scripts/run_pipeline.py --workflow ppi \
        --query "Design cancer therapeutics to target key nodes in mesothelioma." \
        --design-engine boltzgen --project e2e_boltzgen \
        --budget 5 --stop-after calibration          # ~1 GPU-h at 24+1000

**Run 3 must stay the driver, not a CLI invocation.** The legacy path cannot
be sized from the CLI (no `--config`), so
`run_pipeline.py --design-engine boltzgen_legacy` would run to completion at
the shipped 1,000 / 20,000 counts: **332 GPU-h (~13 days)** at YAP1/TEAD1
size. I nearly launched that. The CLI now refuses `--stop-after` there, which
makes the trap loud rather than silent, but it still cannot make that path
small — only the driver can.

Run 4 is the one that exercises `boltzgen_spec` / `boltzgen_runner` / the
per-modality gate on a PPI-discovered target. Size it with `--n-batches`
(BoltzGen reads it as `num_designs`) if the calibration default of 1,000 is
more than the check needs.

---

## Measured facts — do not re-derive these

### BoltzGen cost (fitted on AT-SCALE slices; a 24-design probe read 16.0
s/design against the same campaign's at-scale 7.8)

- time: `7.82 s × (tokens/99)^1.32`. Points: 7.82 @ 99, 17.0 @ 229, 122.3 @
  817. Reproduces the anchor and the large end (+4%); **over-costs 229 by
  39%** — real scatter, hence observed-rate-first.
- disk: `0.348 MB × (tokens/99)^0.97` — **essentially linear, within 2% on all
  three points**, because BoltzGen writes coordinates + SCALAR confidences and
  **no PAE matrix**. RF3's 1.49 exponent comes from that O(N²) matrix being
  half its bytes. Same fact is why `ipsae_min` is unrecoverable here.
- `PROTEIN_PROTOCOL_FACTOR = 1.175` — **now calibrated, no longer
  provisional.** `protein-anything` runs 6 steps to `peptide-anything`'s 5
  (extra = `design_folding`, a binder-alone refold; confirmed from
  `[Step 3/5]` vs `[Step 3/6]`). The mini campaign finished 2026-09-12 and its
  own ledger gives **17.32 s/design end-to-end over 970 designs at 154-167
  tokens**, against the peptide law's 14.74 at the same size — ratio 1.175,
  and the law reproduces the point to 0.5%.
  It REPLACED a 1.8 taken from that campaign's 24-design probe (26.52
  s/design, a 1.53x startup inflation; the cyclic probe read 16.04 against
  7.82, 2.05x). The old note called 1.8 a lower bound; it was a ~53%
  over-estimate. Still ONE at-scale protein point — calibrated, not fitted,
  and `sec_per_design_observed` still takes precedence.

### The RAMP1 994-design campaigns (`outputs/bz_calib_ramp1_{cyclic,mini}`)

Cyclic below; the mini campaign's numbers are in "Open, deliberately" (now
closed) and in CLAUDE.md's BoltzGen section.

- `pass_filters`: 85/994 (8.55%, CI 6.97–10.45)
- `iptm ≥ 0.50`: 64 · `≥ 0.55`: 13 · `≥ 0.60`: **3** (below
  `MIN_HITS_FOR_ESTIMATE`, unmeasurable even at n=1000)
- gated (pass_filters ∩ iptm ≥ 0.50): **12/994 = 1.21%** — the rate that
  SIZES a campaign; sizing 50 needs ~7,200 designs / ~16 GPU-h
- dock ≤ 5 Å: 162/994 (16.3%). **82 of the 85 `pass_filters` designs (96%) are
  also well-docked** — it is very nearly a pure dock-correctness gate
- `ipae ≤ 10` passes 99.7% here and dropped 57% on mesothelioma — target-
  dependent, not loose or strict
- `complex_plddt ≥ 0.70` passes 3.7% and collapses the 64-hit set to **zero**
  — why `plddt_min` ships as `null`

### BoltzGen's own ranking

Two phases: nine boolean filters (`pass_filters` = their AND), then
`max_rank` = the **worst** of six per-metric ranks, passers sorted ascending.

- `filter_rmsd ≤ 2.0 Å` (`refolding_rmsd_threshold`) accounts for **886 of 909
  rejections** — a design-vs-refold self-consistency check, its `binder_rmsd_fold`
- top-100 dock median **3.12 Å, 94% ≤ 5 Å**; full set 8.89 Å / 16.3%;
  iPTM-top-100 8.01 Å / 29%. Overlap of its top-100 with the iPTM top-100 is
  only 20/100, and the best-iPTM design in the run ranks **126th**
- `quality_score` is **not a score** — it is `1 − (rank−1)/(n−1)`, a rank
  percentile with no independent information

### Traps that cost real time

- **Equal-length crash.** Binder residue count == target chain's →
  `inverse_folding` drops the binder chain from the CIF while leaving its
  `.npz` mask at full length → `folding` aborts the WHOLE run two steps later,
  naming neither. 2 of 24 designs hit it on RAMP1. Guarded by
  `boltzgen_spec.safe_binder_range`.
- **`binding:` outside `res_index` is silently ignored.** `boltzgen check`
  ACCEPTS it and marks nothing. Guarded by `boltzgen_spec.validate_spec`.
- **Our trimmed CIF is unparseable by BoltzGen** — gemmi's
  `make_mmcif_document()` writes no `_entity_poly_seq`. The `.pdb` twin parses
  but puts `binding:` in a THIRD numbering (il7ra: auth 58/77/138 = label
  62/81/142 deposited, 33/52/113 in `trimmed.pdb`). Hence trim-as-`res_index`
  against the DEPOSITED file.
- **`binding:` and `res_index:` share one absolute `label_seq` space** —
  binding is NOT renumbered relative to the crop. Verified via `boltzgen
  check`'s own visualisation CIF (B-factor 80 marks binding residues).
- **BoltzGen normalises OUTPUT chains: target → A, binder → B**, whatever went
  in (3N7S chain D → output A). Plus label_seq → auth_seq_id on the target.
  Both handled by `pipeline_runner._boltzgen_output_chains`, which grounds on
  residue NAMES rather than trusting either convention.
- **GPU at 0% is not idle** — the `analysis` step is CPU-bound. Poll disk.
- **A stale metrics CSV makes an in-progress campaign look complete** under
  `--reuse`. `CampaignProgress.complete` requires `n_metrics >= expected`.

---

## Verification protocol (used at every step; keep using it)

    python scripts/foundry_regression_baseline.py > /tmp/before.json
    # change
    python scripts/foundry_regression_baseline.py > /tmp/after.json

`hashes` (41 artifacts) must ALWAYS match. `rederive` (13 campaigns) may change
when ranking changes on purpose — `z_clip` moved all 13 — but **`n_survivors`
must not**, because the gate is separate from the ranking.

For `campaign_calibration` specifically, the stronger check that was used: copy
the module aside, `git checkout HEAD -- src/campaign_calibration.py`, dump
`CalibrationResult.as_dict()` for all 8 campaigns, restore, dump again, diff.
Both B3 and the gate injection came out **bit-identical** that way.

The reference snapshot for this machine lives in the session scratchpad
(`.../scratchpad/bz/baseline_reference.json`); it is NOT committed, because
`projects/` and `outputs/` are gitignored and a fresh clone cannot reproduce it.

---

## Open, deliberately

- **Every deterministic structure guard on the PPI track is INERT for a
  non-human target, silently.** Found by the 2026-09-13 target-diversity
  sweep, in which 2 of 8 prompts produced non-human targets and both ran with
  no structural cross-check at all.

  `target_resolve.resolve_target` is human-only — its own warning says "could
  not resolve 'EsxB' to a human UniProt accession" — and
  `_select_designable_structure` needs TWO resolved accessions before it does
  anything (`if len(accs) < 2: return None`). So for a pathogen protein it
  returns None before reaching its partner-absent check, and the same UniProt
  keying disables `_verify_target_chain_assignment` and
  `_check_structure_organism`.

  Measured consequence. The `tuberculosis` prompt produced target_complex
  "EsxB / p38" with pdb_id 3FLN. The BIOLOGY is real and correctly cited
  (EsxB/CFP-10 disrupting host p38-TAB1, doi:10.1038/s41421-024-00653-4), and
  the id obeyed the prompt's sourcing rule — 3FLN IS in a corpus paper's
  `pdb_accessions`. But that paper is "Drug Design in the Exascale Era", a
  computational-methods paper that used 3FLN as a BENCHMARK, and 3FLN is
  "P38 kinase crystal structure in complex with R1487": one polymer entity
  (MAPK14), one small molecule, no EsxB, and no chain A. The accession rule
  guarantees an id is REAL, never that it contains the claimed complex; with
  no EsxB/p38 co-structure in existence, the stage reached for the only
  p38-bearing accession the corpus had.

  It failed at `_correct_label_seq_ids` ("cannot build the auth->label map for
  chain A") — by luck, on a technicality, rather than by the guard built for
  exactly this shape.

  Fix direction, NOT yet implemented: when a name does not resolve, fall back
  to matching the entry's chain DESCRIPTIONS (`entry_metadata` already returns
  them) for both named proteins, and say loudly in the report that the
  UniProt-keyed guards are inert — which is what `--workflow structure`
  already does and the PPI track does not. Deferred so the sweep's remaining
  runs test one code path.

- **`sec_per_design_observed` measures a DIFFERENT quantity from the law it
  overrides, and under-costs — the one place the estimate is not
  conservative.** It is refold-mtime spacing, i.e. the `folding` step alone;
  the law (`SEC_PER_DESIGN_REF`) was fitted on END-TO-END `boltzgen run` wall
  clock from `campaign_timing.jsonl`. Excluded from the observed rate:
  `design`, `inverse_folding`, `design_folding` (protein protocol only) and
  the CPU-bound `analysis`/`filtering` tail. Measured on the acceptance runs —

      campaign                 refold spacing   end-to-end   ratio
      PD-L1 cyclic (5-step)         7.77          ~10.2      1.31x
      YAP1/TEAD1 mini (6-step)     15.13           26.20     1.73x

  and the observed rate takes PRECEDENCE, so an estimate flips from ~1.4x
  conservative (law) to ~1.7x optimistic the moment a stage clears
  `MIN_DESIGNS_FOR_RATE`. On the YAP1/TEAD1 acceptance run the pessimistic
  production estimate read 44.9 GPU-h where end-to-end implies ~78; the
  SCALE_UP verdict still stands (both are inside the 120 h budget) but the
  budget check is the thing this number gates.

  **Imported from foundry, where it is valid.** There the refold (RF3) IS the
  dominant cost and `sec_per_refold_observed` tracked production within
  10-17%; BoltzGen spends a much larger share off the folding step, and the
  6-step protein protocol more than the 5-step peptide one — which is exactly
  the 1.31x/1.73x split above.

  Fix deferred on purpose: changing it mid-queue would mean the remaining
  acceptance runs test different code from the ones already finished. The
  candidate fix is to measure what the law measures — job start (the registry
  has it) to last refold, or completed-stage wall clock — rather than to add a
  correction factor.

- **The time law over-costs its interior points, and is DELIBERATELY NOT
  refitted.** Operator decision, 2026-09-12: an over-estimate is the right way
  to be wrong. Four at-scale peptide-protocol points now exist —

      tokens   s/design   local exponent to the next point
         99      7.82      ~0.0
        130      7.77      1.39      (PD-L1 macrocycle calibration, 1000 designs)
        229     17.00      1.55
        817    122.30      —

  so cost is nearly FLAT below ~130 tokens and then steepens toward quadratic,
  which a single power law anchored at 99 and 817 cannot represent: it
  over-costs 130 by 44% and 229 by 39%. The 229 discrepancy was filed as
  scatter and is structural. A fixed-overhead-plus-power form (`a + b*N^c`)
  would fit all four.
  **Do not refit it anyway** without asking. It is only an estimate, an
  observed rate supersedes it wherever one exists (>= 50 designs), and where
  the over-estimate is not free it costs only RECALL: a SCALE_UP that reads
  SCALE_UP_PARTIAL, or `calibrate()` declining to raise the adaptive bar
  (which requires the stricter rung to fit the NON-slack budget). It cannot
  produce a false STOP — `_decide` reaches STOP from exactly one branch, zero
  hits with nothing clearing any rung of the bar ladder, which is a pure yield
  condition with no cost term in it. `BUDGET_SLACK = 1.5` exists to keep a
  budget overshoot from flipping a viable plan, and its own comment says so.

- **The multi-site trial path is NOT dispatched, and is refused rather than
  silently swapped.** `_run_site_trials` calls `_stage_binder_spec` and
  `_stage_calibration` unconditionally — the foundry stages — so a BoltzGen
  run reaching it would build an RFD3 contig JSON where a BoltzGen YAML was
  asked for and then wait for RF3 output that never arrives.
  `_refuse_undispatched_site_trials` blocks `--trial-sites > 1` and
  `--stop-after spec|trial` on BoltzGen and points at `--stop-after
  calibration`, which takes the single-site route and IS dispatched. Found by
  reading, not by running; `--stop-after spec` is the documented first command
  for a new user, so it is the one they would have hit first. Wiring it is the
  natural Phase D: it needs the spec call branched and `_backbones_to_batches`
  / `count_rf3` replaced with their BoltzGen equivalents.

- ~~`mini_protein` thresholds are PROVISIONAL~~ **CLOSED 2026-09-12.** The
  994-design mini campaign finished, and `modality.mini_protein` carries no
  overrides *as a measurement*: clearing the shipped gate AND the 0.50 iptm
  bar is **11/994 = 1.11% (95% CI 0.62-1.97)** for mini against cyclic's
  **12/994 = 1.21% (0.69-2.10)** — within noise, so 0.50 is the strictest
  defensible bar for both (0.55 gives 3 hits, under `MIN_HITS_FOR_ESTIMATE`).
  The COMPONENTS differ and the config comment says so: `pass_filters` 20.3%
  vs 8.6%, `ipae <= 10` keeps 83% of mini passers vs 100% of cyclic ones,
  `plddt >= 0.70` 39% vs 3.7%. Its n=24 probe had suggested otherwise (iptm
  0.123-0.448, median 0.215); at scale it is 0.118-0.665, median 0.234.
  Probes size nothing.
- **`EXPOSED_HOTSPOT_CLEARANCE_A = 10.0`** was calibrated on a 19-complex
  MINI-PROTEIN benchmark. A 12–15mer spans ~10–12 Å, so a freshly-exposed
  hydrophobic patch exactly 10 Å from a hotspot is a plausible ALTERNATIVE site
  for a macrocycle rather than part of the same one. Left unchanged: no cyclic
  trim data exists to re-measure against, and guessing at a safety threshold is
  worse than a documented gap.
- **One strict xfail** in `tests/test_design_ranking_regression.py`:
  `hotspot_sasa_delta_min = 30.0` is unreachable for cyclic peptides (0–24.7 Å²
  vs 190–430 for mini-proteins) on the LEGACY `design.thresholds`. It turns
  green when per-modality thresholds reach that path; it is `strict=True` so it
  also fails if someone "fixes" it by loosening the wrong thing.
- **`ipsae_min` is unavailable on BoltzGen** (no PAE matrix). Its own
  `design_ipsae_min` spans 0.0000–0.0289 against an RF3-calibrated bar of 0.5 —
  **zero of 994 clear it** — so it is not a drop-in substitute.
