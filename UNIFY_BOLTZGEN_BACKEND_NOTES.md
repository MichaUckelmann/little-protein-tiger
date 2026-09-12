# Making BoltzGen a plug-in backend — working notes

Sibling of `UNIFY_DESIGN_BACKEND_NOTES.md`, which scoped the other direction
(PPI → foundry). This one scopes BoltzGen as an alternative backend for the
**binder** track, and records what was measured getting there.

**Status: phases A and B complete and committed (unpushed). Phase C is next.
The acceptance runs have NOT been done yet — they are deliberately deferred so
they test the final implementation rather than an intermediate one.**

---

## Operator decisions already taken

1. **BoltzGen keeps its own scoring**, with `binder_metrics` re-scoring as a
   later opt-in. Measured reason: BoltzGen's ranker is a MAXIMIN over six
   per-metric ranks and is a strong dock selector (numbers below).
2. **The legacy PPI BoltzGen stages stay alive** until the new path is proven
   unnecessary. So `design/execution/analysis` and the
   `protein-design-script` skill are not retired yet.
3. **`--project` should be required everywhere**, for consistency. **NOT YET
   IMPLEMENTED** — see Phase C.
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

## Phase C — what is left to build

**C1. Route PPI + BoltzGen through the bridge into the new backend.**
Today `--workflow ppi --design-engine boltzgen` still reaches the LEGACY
stages: the PPI dispatch at `src/pipeline_runner.py:891` returns
`_bridge_ppi_to_foundry` only for `foundry`, and everything else falls through
to `_stage_design`. The bridge already composes a synthetic `20_target_intel.md`
and copies `02_structure.md` to `21_interface.md`, then enters
`_run_binder_track(start_from="trim")` — where the B4 dispatch is waiting. So
C1 is mostly: make the bridge backend-agnostic (rename intent, not behaviour)
and let BoltzGen take the same route.

**C2. `--project` required everywhere** (decision 3). Currently
`pipeline_runner.py:567` requires it for `ppi + foundry` only; the binder track
requires it separately; `ppi + boltzgen` requires nothing. Making it universal
is a small CLI/constructor change plus a test.

**C3. Decide what `--stop-after` means off the binder track.** It is honoured
only there (4 call sites, all binder-track). Once C1 lands, PPI + BoltzGen
inherits it for free — which is most of the reason C1 is worth doing before the
acceptance runs.

**C4. Legacy retirement is NOT in scope** (decision 2). Keep both paths; the
e2e driver exists to regression-check the old one.

---

## The acceptance runs (deferred until after Phase C)

All three verified to parse. `auto_mode` defaults to `True`; there is no
`--auto` flag.

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

**Do not replace run 3 with a plain CLI invocation.** `--stop-after` is
binder-track only and the legacy path cannot be sized from the CLI, so
`run_pipeline.py --workflow ppi --design-engine boltzgen --stop-after
calibration` would run to completion at the shipped 1,000 / 20,000 counts:
**332 GPU-h (~13 days)** at YAP1/TEAD1 size. I nearly launched that.

After C1, run 3 should probably become a *fourth* run (PPI + BoltzGen through
the new backend), with the driver kept as the legacy regression check.

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
- `PROTEIN_PROTOCOL_FACTOR = 1.8` — **a LOWER BOUND, still provisional.**
  `protein-anything` runs 6 steps to `peptide-anything`'s 5 (extra =
  `design_folding`, a binder-alone refold; confirmed from `[Step 3/5]` vs
  `[Step 3/6]`). Derived from the RAMP1 mini campaign's 26.45 s/design of
  *refold spacing* vs the peptide law's 14.86 — and that spacing excludes the
  extra step, so the true factor is higher. **Firm it up from that campaign's
  finished ledger** (`outputs/bz_calib_ramp1_mini/campaign_timing.jsonl`).

### The RAMP1 994-design cyclic campaign (`outputs/bz_calib_ramp1_cyclic`)

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

- **`mini_protein` thresholds in `design.boltzgen_ranking` are PROVISIONAL**
  and inherit the base. The matching 994-design mini campaign
  (`outputs/bz_calib_ramp1_mini`) is what sets them. Its n=24 probe showed iptm
  0.123–0.448, median 0.215, **zero above 0.60** — so the cyclic numbers must
  not be assumed to transfer. Run
  `scripts/calibrate_boltzgen_thresholds.py outputs/bz_calib_ramp1_mini` when
  it finishes.
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
