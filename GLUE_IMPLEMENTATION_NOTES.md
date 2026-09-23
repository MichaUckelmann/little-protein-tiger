# Molecular glue (`design_intent: stabilize`) — implementation notes

**Status: reaches a validated two-chain spec, and is scored per side. No glue
campaign has ever run on a GPU.** Updated 2026-09-23 against `main` @
`e2d46dc`. Written to pick up cold in a new context.

`GLUE_PIPELINE_SCOPE.md` is the 2,000-line design document and stays
authoritative for the *reasoning* — measurements, selection criteria,
evaluation plan, risk register. `GLUE_STAGE3_PLAN.md` is the executable plan
Stage 3 was built from, and its §2 is the consolidated list of corrections to
both older documents; read that before trusting a line number or a claim in
either. This file is the shorter thing you need first: what works today, what
the next step is, and what will bite.

---

## 1. Where we are

The scope's build order (§8) has 8 stages. **Stages 0–4 are done. Stage 5 —
the first GPU campaign — is not started, and is the next thing.**

| stage | what it is | status |
|---|---|---|
| 0 | domain-source preference, atom-existence check, exposure as an area | **DONE** `6220a2a` |
| 1 | settle the RFD3 two-chain merge empirically | **DONE, PASS** 2026-09-13 |
| 2 | Phase A + Phase B — the GPU trimming benchmark | **DONE** 2026-09-14 |
| 3 | glue plumbing up to a validated spec, no GPU | **DONE** 2026-09-16, 12 commits |
| 4 | glue scoring, still no glue GPU campaign | **DONE** 2026-09-18 |
| **5** | **the first glue campaign, 4ZGM** | **NOT STARTED** (~1 d + 10–20 GPU-h) |
| 6 | two-chain trimming | not started (~6 d) |
| 7 | site selection, reports, second engine | not started (~6 d) |

### What works today

One command, no LLM call, no GPU, no network beyond a structure already on
disk:

    .venv/bin/python scripts/run_pipeline.py --workflow structure --pdb 4ZGM \
        --project glue_4zgm --chains A,B --design-intent stabilize \
        --hotspots A113,A119,A120,B30,B31,B34,B35,B37 --stop-after spec

produces a `validate_spec`-accepted RFD3 spec with contig
`70-86,/0,A29-128,B10-37` and `select_hotspots` keys exactly
`{A113,A119,A120,B30,B31,B34,B35,B37}` — each hotspot under the chain it is
actually on. Both archived `stabilize` reports
(`projects/div_standard_diabetes` 5VAI, `projects/div_wildcard_tnbc` 6JJW)
parse with correct per-chain attribution and ground clean.

Scoring writes six new columns — `hotspot_side_chains`,
`hotspot_engagement_a`/`_b`/`_min_side`, `glue_ipsae_ab`, `glue_ipsae_delta` —
all blank on a non-glue run. Three new thresholds exist and all ship `null`.

### What a glue run refuses, and what lifts each

| refused | why | lifted by |
|---|---|---|
| `--design-engine boltzgen` | `binding:` addresses ONE chain; the co-target would be dropped | item 26, Stage 7 |
| `--modality cyclic_peptide` | selects BoltzGen; also glue+cyclic is unsized (§9) | items 26 + sizing |
| `--trial-sites > 1` | `_run_site_trials` has no two-chain site shape | items 19/20, Stage 7 |
| `--workflow ppi` / `binder` | only `--workflow structure` takes `--design-intent` | item 19, Stage 7 |
| a target whose two chains exceed `target_residue_budget` | the trim must be a NO-OP | item 7, Stage 6 |
| a co-target that is not `partner_chain` | `write_trimmed` puts no other chain in the file | item 7, Stage 6 |
| `allowed_auth` with a co-target | a bare `set[int]` of one chain's author ids | item 14, Stage 6 |

Each refusal names its scope item in the message.

---

## 2. Stage 5 — the next thing

**4ZGM, single site, `--stop-after calibration`, `--project`.** 300-backbone
trial; `--escalate-to` if the interval is unusable. ~1 d of setup and 10–20
GPU-h.

**Go/no-go:** the calibration verdict read against
`hotspot_engagement_min_side` (set non-null **for this run only**) and
`glue_ipsae_delta`. A SCALE_UP here is the first evidence that glue design
works at all. **Stop and re-scope if the trial produces no design engaging
both sides.**

Three things to do first, none of them large:

1. **Wire the apo fold.** `glue_ipsae_delta` is the interpretable readout and
   it needs one RF3 fold of the target pair with NO binder, once per campaign.
   `PipelineRunner._glue_apo_ipsae` already reads
   `binder/apo/glue_apo.json` (a single `ipsae_ab` key) and returns None when
   it is absent, leaving the column blank. What does not exist is whatever
   *writes* that file. This is the GPU half of scope item 17 and it is the one
   piece of Stage 4 deliberately left for Stage 5, because Stage 4 runs no GPU.
2. **Set `hotspot_engagement_min_side_min` for the run only** — never in
   committed `config.yaml`. See §4, risk 1.
3. **Decide the pooled-gate question**, below.

---

## 3. The one thing Stage 3 built that Stage 5 will trip over

`hotspot_engagement` is a fraction of ALL declared hotspots at a **0.75 gate**,
and a glue pools both chains into one denominator. So a binder engaging one
side well and the other poorly is scored as though the epitope were a single
surface — and on an 8-hotspot 4ZGM set split 3/5, a design touching all of
chain A and none of chain B scores 0.375 and is dropped, while one touching
all of A and 3 of 5 on B scores 0.75 and passes with no statement about
whether it bridges anything.

`hotspot_engagement_min_side` exists precisely to answer that, and ships
`null`. Stage 5 is where it has to be given a value, and **there is no
calibration for it** — the 0.75 on the pooled gate came from measuring RFD3's
own backbone behaviour over a 12-hotspot campaign, and no equivalent
measurement exists per side. Expect the first campaign to produce it rather
than to consume it.

`_note_glue_guards` prints this, and the three other guard caveats, on every
glue run and returns the prose so the report and the log cannot drift.

---

## 4. The three ranked risks

1. **A glue gate shipped with a non-null threshold silently zeroes every
   disrupt campaign.** Measured: 1,352 real 3KYS records, 317 survivors → **0**
   with `hotspot_engagement_min_side >= 0.5`. It looks like "bad target", not
   "bad config". **Mitigated three ways now**: all three thresholds ship
   `null` (in `DEFAULT_THRESHOLDS` and in `config.yaml`, both asserted by
   `tests/test_glue_scoring.py`); `filter_records` **warns by name** when a
   glue gate is active and not one record carries the column; and the survivor
   counts of every shipped campaign run as a test. Still the top risk, because
   the mitigation is a default and a warning, not an impossibility.
2. **`n_tokens` under-counting a two-chain target ~2×** → GPU 3.1×, disk 2.8×,
   and it feeds the calibration verdict and the local-vs-cluster decision.
   **Closed, and no longer "by construction"**: the trim's three counts are
   over `kept_by_chain`, and `validate_spec` takes
   `expected_target_residues` and hard-refuses when the contig and the trim
   disagree. Measured 4ZGM: 128 target residues, 214 tokens from
   `validate_spec` (binder max) and 206 from the cost model (midpoint).
3. **Broadening `trim_target`** — Stage 6, and the reason a glue trim must
   currently be a no-op. `_exposed_hydrophobic` compares each chain against
   itself **in isolation**, so a cut that strips the other chain's buried face
   reads as clean: on 5VAI an `R29-128 + P7-37` trim opens **492 Å² across 8
   hydrophobic residues on chain P** and measures 0. That is why the
   no-op refusal exists rather than a warning.

---

## 5. The regression wall — run these on every glue commit

Locate by NAME, not line number.

- `tests/test_structure_trim.py` `test_build_contig_shape` — pins
  `"68-86,/0,B42-145"`: one binder range, one `/0`, one chain letter per span
- `tests/test_structure_trim.py` `test_contig_matches_the_kept_segments`
- `tests/test_structure_trim.py` `test_the_floor_is_eighty`
- `tests/test_structure_trim.py`
  `test_a_trim_that_removes_nothing_retains_everything` — **the R2 wall**,
  parametrised over 7CZD / 6VJJ / 3KYS
- `tests/test_foundry.py`
  `test_regenerates_the_reference_cd79b_spec_field_for_field`
- `tests/test_foundry.py` `test_validate_cross_checks_against_the_trim`
- `tests/test_foundry.py` `test_mpnn_config_shape`
- `tests/test_audit_fixes.py`
  `test_trim_from_disk_has_every_attribute_the_gpu_stages_use` — **the R4
  wall**, by reflection
- `tests/test_binder_metrics.py` — the zero-diff golden over 200 refolds (R5)
- `tests/test_complex_token_ceiling.py` — pins
  `n_tokens == binder_max + n_target`
- `tests/test_release_fixes.py` `test_packaged_skill_zips_match_their_source` —
  editing a `SKILL.md` means re-running `scripts/package_skills.py`

**And the five glue files, which are also the corpus invariants:**
`tests/test_glue_hotspot_parsing.py`, `test_glue_grounding.py`,
`test_glue_label_seq.py`, `test_glue_contig.py`, `test_glue_trim.py`,
`test_glue_spec.py`, `test_glue_branch.py`, `test_glue_scoring.py`. Several of
these re-derive every artifact in `projects/` — 49 specs field-for-field, 53
trim maps, 16 `refold_scores.csv` — so they fail if a change moves anything
that already shipped.

---

## 6. The differential harness, and why the tests are not enough

Green tests do not prove "nothing moved". Every Stage 3/4 step was checked
against snapshots captured BEFORE any code changed, and the final state is:

| differential | scope | result |
|---|---|---|
| derived artifacts | 486 stage reports, 49 specs, 53 trims, 54 contigs | **4 changed** — all glue reports, all a region label that read `""` |
| trim goldens | 10 real trims, 5 structures × 2 budgets | **0 changed** |
| label_seq resolution | 105 reports with a local structure | **101 byte-identical**, 4 glue |
| survivor counts | 16 `refold_scores.csv`, 84,136 rows, 7,080 survivors | **0 changed** |

Rebuild it before any Stage 5/6 work. The scripts are small and the pattern
matters more than the code: snapshot → change → diff, with the additive keys
stripped so equality means "unchanged".

**Four changes are NOT intent-gated**, and each was measured across the whole
corpus rather than argued about:

| change | applies to | measured |
|---|---|---|
| `expected_target_residues` refusal | every foundry run | 54 trims, 0 would newly refuse |
| missing-partner refusal in `_stage_trim` | any `disrupt` run | 112 handoffs, 0 would newly refuse |
| `_REGION_LABEL` gained `]` | every report | 0 labels affected |
| chain-aware trim cross-check | archived trims take the stronger branch | 53 of 53 pass |

The second is the one to remember: a `disrupt` run whose interface stage omits
`partner_chain` now **refuses** where it previously degraded silently into
single-target mode. Nothing on disk triggers it, but it is a real behaviour
change on the non-glue path.

---

## 7. Outstanding work, in dependency order

### Stage 5 — the first glue campaign (~1 d + 10–20 GPU-h)
See §2. Blocked on nothing; needs the apo-fold writer (item 17's GPU half).

### Stage 6 — two-chain trimming (~6 d), items 7, 8, 9, 10, 13, 14
**88% of real complexes need a trim**, so glue is not generally usable until
this lands — 4ZGM was chosen precisely because it is one of the 12% that fits.
The riskiest change in the whole plan: `trim_target` is the one function every
one of the 13 calibrated campaigns' targets went through.

- item 7 — per-chain `segment_domains`/`plan_trim`, joint budget
- item 8 — assembly-context `_exposed_hydrophobic` (§4 risk 3). **Must stay
  byte-identical for a one-chain target**; the R2 wall is what proves it
- item 9 — `glue_interface_retention` replacing `min_bsa_retention` on the
  glue path, which today measures the retention of the very interface a glue
  is stabilising and is calibrated for campaigns that cut into one
- item 10 — `MIN_TARGET_RESIDUES` on the total plus a per-chain floor.
  Today's consequence: **4ZGM chain B (28 residues) can never be the primary
  `target_chain`**, because `plan_trim` raises below 80. Undocumented
  precondition, currently satisfied by picking the larger chain
- item 13 — `_stage_trim` two-chain topology + chain-keyed `allowed_auth`
- item 14 — `membrane_topology.restriction_for` per chain. Today the topology
  and ortholog guards run on the target chain only and **say so in a warning**;
  a membrane co-target's TM residues are not stripped

### Stage 7 — site selection, reports, second engine (~6 d), items 19, 20, 22, 26
- item 19 — `_select_designable_structure` two-accession, `stabilize`
  preference. Until this lands, `--workflow ppi` cannot ask for a glue at all
- item 20 — deterministic glue-site pre-pass over `find_glue_pockets`
- item 22 — both reports: two target chains in the Mol* payload, a chain
  column in the hotspot table, non-`disrupt` hero text. **`binder_report`
  currently renders a glue campaign with no idea the epitope has two sides**
- item 26 — BoltzGen glue spec, per-chain `include`/`binding_types`

### Smaller, unowned by any stage
- **`foundry_spec.MAX_HOTSPOTS = 12` is per-region and a glue set is two
  regions.** 12 per chain = 24 trips the warning. The scope's inferred answer
  is a cap of 12 on the UNION; nothing implements it yet, and
  `--hotspots` already errors above the cap rather than warning
- **glue + `cyclic_peptide` is unsized** — no RFD3 cyclic campaign exists, so
  the ≤35 Å union-diameter ceiling is unmeasurable. Currently refused
- **the design-analyst caveat** is in the prompt; it is not yet in either
  HTML report's rendered narrative

---

## 8. First commands to run, cold

    # 1. the wall, before touching anything
    .venv/bin/python -m pytest tests/ -q
    # 1,598 passed on 2026-09-23 @ e2d46dc. The count moves as the repo
    # grows; ZERO failures is the check, not the number.

    # 2. the end-to-end glue command — no LLM, no GPU, ~3 s
    .venv/bin/python scripts/run_pipeline.py --workflow structure --pdb 4ZGM \
        --project glue_check --chains A,B --design-intent stabilize \
        --hotspots A113,A119,A120,B30,B31,B34,B35,B37 --stop-after spec
    # expect contig 70-86,/0,A29-128,B10-37 and 8 hotspots across A and B

    # 3. both archived glue reports, parsed and grounded
    .venv/bin/python -m pytest tests/test_glue_hotspot_parsing.py \
        tests/test_glue_grounding.py tests/test_glue_label_seq.py -q

    # 4. the survivor wall — the one that protects every disrupt campaign
    .venv/bin/python -m pytest \
        tests/test_glue_scoring.py::test_the_survivor_counts_of_every_shipped_campaign_are_unchanged -q

    # 5. fixtures (data/ is gitignored; fetch on a fresh checkout)
    ls data/structures/{4ZGM,5VAI,6JJW}_ba1.cif || \
    .venv/bin/python -c "from pathlib import Path; from src.target_resolve import ensure_assembly; \
        [print(ensure_assembly(i, Path('data/structures'))) for i in ('4ZGM','5VAI','6JJW')]"
