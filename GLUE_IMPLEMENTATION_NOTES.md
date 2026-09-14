# Molecular glue (`design_intent: stabilize`) — implementation notes

**Status: NOT functional. Cannot reach a spec.** Written 2026-09-14 to pick up
cold in a new context.

`GLUE_PIPELINE_SCOPE.md` is the 2,000-line design document and stays
authoritative for the *reasoning* (measurements, selection criteria,
evaluation plan, risk register). This file is the shorter thing you need
first: what is already done, what is left, where it lives in the CURRENT
tree, and what will bite. Line numbers here were re-verified on
2026-09-14 after that day's commits moved `pipeline_runner.py` by ~250
lines — the scope's numbers are stale, mine are not.

---

## 1. Where we are

The scope's build order (§8) has 8 stages. **Stages 0, 1 and 2 are done;
Stage 3 is not started.**

| stage | what it was | status |
|---|---|---|
| 0 | domain-source preference, atom-existence check, exposure as an area | **DONE** `6220a2a` |
| 1 | settle the RFD3 two-chain merge empirically | **DONE, PASS** 2026-09-13 |
| 2 | Phase A + Phase B — the GPU trimming benchmark | **DONE** 2026-09-14 |
| **3** | **glue plumbing up to a validated spec, no GPU** | **NOT STARTED** (~6 d) |
| 4 | glue scoring, still no glue GPU campaign | not started (~3 d) |
| 5 | the first glue campaign, 4ZGM | not started (~1 d + 10–20 GPU-h) |
| 6 | two-chain trimming | not started (~6 d) |
| 7 | site selection, reports, second engine | not started (~6 d) |

Stages 0–2 were all *measurement* — they answered whether the merge works and
what trimming costs. **None of them touched the glue path itself.** That is
why a `stabilize` run today still cannot produce a spec.

### The evidence, on disk

`projects/div_standard_diabetes` — prompt *"Design protein therapeutics for
type 2 diabetes."*, `--workflow ppi`, 2026-09-13. It selected
`GLP-1 / GLP-1R` on **5VAI** with `design_intent: stabilize` and its structure
stage wrote a correct `### MODEL-READY HOTSPOTS [STABILIZE — Glue Pocket 1]`
table: 7 residues across BOTH chains (chain A/R = GLP-1R PHE66/ASP67/ALA70,
chain B/P = GLP-1 ALA30/GLY35/ARG36/GLY37 — each verified real on its own
chain). The project has `00_pathway.md`, `01_literature.md`,
`02_structure.md` and **no `binder/` directory at all**. It stopped dead
before the binder track. That is the reproduction case; it needs no GPU and
no API spend to re-trigger.

### The three blockers, re-verified 2026-09-14

All unchanged from the audit. Each was read in the current tree today:

1. **`handoff.parse_hotspot_residues` (`src/handoff.py:108`)** takes
   `target_chain`/`partner_chain` from the HANDOFF and appends every row of
   every section with **no per-row chain attribution**. Its dedup key is
   `(residue, auth_seq_id)`, which additionally collapses the same name+number
   appearing on two chains. Today it returns all 7 of the 5VAI rows stamped
   with one chain, 4 of which belong to the other.
2. **`structure_trim.build_contig` (`src/structure_trim.py:1330`)** takes ONE
   `chain` and emits `f"{chain}{lo}-{hi}"` for every span. **This is the real
   generator-side blocker** — a two-chain glue target cannot be expressed at
   all. Fixing #3 alone changes nothing, because `validate_spec` gates a
   chain-B hotspot on a chain-B contig span.
3. **`foundry_spec.py:120`** — `select[_hotspot_key(target_chain, auth)] =
   atoms` forces every hotspot onto `target_chain` unconditionally.

And a fourth that is worse than a blocker because it *corrupts*:
**`_resolve_unverified_label_seq_ids` / `_correct_label_seq_ids`
(`src/pipeline_runner.py:7635` / `:7614`)** resolve label_seq ids against a
single chain's map. On a two-chain table that silently rewrites the second
chain's ids to the first chain's frame.

---

## 2. Already done — do NOT rebuild these

Several items in the scope's §6 table (items 1–26) have since landed, some as
side effects of unrelated work. Check here before starting any of them.

| item | what | status |
|---|---|---|
| 24 | domain-source preference (prefer RCSB only when its domains cover the hotspots) | **DONE** — `structure_trim._domains_cover_hotspots`, `6220a2a` |
| 23 | early atom-existence / steer-quality check on the hotspot set | **DONE** — `_check_hotspot_atoms_are_buildable` (`pipeline_runner.py:2593`) |
| — | exposure reported as an AREA (§2.2 item 4) | **DONE** — plus a scale-free fraction gate, `MAX_EXPOSED_HYDROPHOBIC_FRACTION`, and four persisted fields |
| 18 | `n_tokens` = both chains + binder | **DONE, and verify it stays done.** `foundry_spec.py:370` is `binder[1] + n_target`, and `n_target` (`:307-340`) iterates `for chain, lo, hi in spans` — already chain-agnostic, so a two-chain contig sums both. This was risk #2 on the scope's ranked list ("silent, under-costs GPU 3.1×"); it is closed by construction, not by intent, so a change to the span loop could reopen it. |
| 25 | benchmark script for the §5 ladder | **DONE differently** — `scripts/benchmark_trimming.py` (real-epitope trim benchmark) plus `scripts/benchmark_trim.py` (the Phase A/B GPU ladders) |

**Item 16 is partly pre-paid but read the caveat.** The scope's rule is "new
gates ship `null`". Today's `design.binder_ranking.weights` gained
`neg_patch_enrichment: 0.5` — a **weight**, not a threshold, so it cannot
empty a campaign (`filter_records` never sees weights, and a zero-variance
column z-scores to zeros). Survivor counts on every shipped campaign are
unchanged. The rule still stands for anything you add to `thresholds`.

---

## 3. Stage 3 — the critical path, with current locations

Target: **4ZGM**, and this matters. 1.8 Å, two chains, 128 residues, 100%
sidechain completeness, 206 tokens, and **no trim needed at all** — so Stage 3
needs none of the two-chain trimming work (item 7, Stage 6). Do not start on
5VAI: there is **no clean 5VAI trim**, and the scope's §1.3 correction shows
why — `R29-128 + P7-37` opens **492 Å² across 8 hydrophobic residues on chain
P** (PHE12 +122, LEU20 +88, TYR19 +70, TRP31 +64), because GLP-1's N-terminal
half inserts into the TM bundle the cut removes. The earlier "2 exposed, 0
near, passes" reading was an artefact of `_exposed_hydrophobic` comparing each
chain against itself in isolation.

Items 1, 2, 3, 4, 5, 6, 11, 12, 21. Current locations:

| item | change | file:line (verified 2026-09-14) | est |
|---|---|---|---|
| 1 | chain column in the row regex; `(chain, residue, auth)` dedup key; `target_chains: list` in the returned JSON | `src/handoff.py:108` | 1 d |
| 2 | handoff carries a second target chain (`target_chains`) | `src/handoff.py` + `skills/complex-structure-analysis/SKILL.md` | 0.5 d |
| 3 | `_verify_hotspot_grounding` looks each row up on ITS OWN chain | `src/pipeline_runner.py:2350` | 0.5 d |
| 4 | per-chain label_seq maps | `src/pipeline_runner.py:7614`, `:7635` | 0.5 d |
| 5 | `build_contig` over multiple chains | `src/structure_trim.py:1330` | 0.5 d |
| 6 | `kept_by_chain` ALONGSIDE `kept_segments`, written to `trim_map.json` | `src/structure_trim.py:153`, `_write_mapping` | 1 d |
| 11 | `_hotspot_key` fed the hotspot's own chain | `src/foundry_spec.py:120` | 0.25 d |
| 12 | chain-aware trim cross-check | `src/foundry_spec.py` `validate_spec` (`:174`) | 0.25 d |
| 21 | `stabilize` becomes a real branch, reachable from `--workflow structure` | `src/pipeline_runner.py` | 0.5 d |

**Suggested order**, because each step is checkable before the next:
1 → 3 → 4 (the table can then be parsed and grounded correctly, and
`div_standard_diabetes` is the free test) → 5 → 11 → 12 (a spec can then be
built and validated) → 6 → 2 → 21.

### Go/no-go for Stage 3

From the scope, and all four are cheap:

- the eight regression-wall tests green (§5 below);
- `parse_hotspot_residues` on
  `projects/div_standard_diabetes/runs/round-1/02_structure.md` returns
  **7 residues with correct per-chain attribution** (today: 7 rows, all one
  chain, 4 of them wrong);
- grounding passes on that same file;
- `validate_spec` accepts the 4ZGM glue spec including the trim cross-check,
  and `n_tokens` reads **206**, not 178.

**Stop if the wall is not green.**

---

## 4. Why two-chain trimming is deferred to Stage 6, and what that costs

4ZGM needs no trim, so Stage 3 can be built and a first campaign run (Stage 5)
without touching `trim_target`. But **88% of real complexes need a trim**
(§2.3), so glue is not generally usable until Stage 6.

Stage 6 is also the riskiest single change in the whole plan: `trim_target` is
the one function every one of the 13 calibrated campaigns' targets went
through. Two specific hazards:

- **`_exposed_hydrophobic` must stay byte-identical for a one-chain target**
  (item 8, risk R2). Broadening it to assembly context changes a measurement
  those 13 campaigns were checked against, and a previously-passing no-op trim
  that starts refusing is the failure mode.
- **`allowed_auth` is a bare `set[int]`** (item 14), so two chains' author
  numbering spaces collide. It must become chain-keyed.

One thing measured today that makes Stage 6 easier to reason about: the
newly-exposed-hydrophobic fraction tracks **segment count** (1 segment median
7.6%, 2–5 35.6%, ≥6 43.5%) and is **anti-**correlated with how much was
removed (<20% → 16.1%, 20–40% → 71.4%, ≥40% → 7.2%). So exposure is governed
by whether a cut followed a structural unit, not by its size — see
`docs/trim-benchmark.md`. That is the physical basis a two-chain trim's
objective should be built on.

---

## 5. The regression wall — run these on every glue commit

These pin single-chain assumptions and are the ones a glue change breaks. Line
numbers from the scope (§7.2) and not re-verified today, so locate by NAME:

- `tests/test_structure_trim.py` `test_build_contig_shape` — pins
  `"68-86,/0,B42-145"`: one binder range, one `/0`, one chain letter per span
- `tests/test_structure_trim.py` `test_contig_matches_the_kept_segments`
- `tests/test_structure_trim.py` `test_the_floor_is_eighty` —
  `MIN_TARGET_RESIDUES == 80` as one scalar for one chain
- `tests/test_structure_trim.py`
  `test_a_trim_that_removes_nothing_retains_everything` — **the R2 wall**,
  parametrised over 7CZD / 6VJJ / 3KYS
- `tests/test_foundry.py`
  `test_regenerates_the_reference_cd79b_spec_field_for_field` — the golden spec
- `tests/test_foundry.py` `test_validate_cross_checks_against_the_trim` —
  chain-less `(lo, hi)` kept_segments
- `tests/test_foundry.py` `test_mpnn_config_shape` — `designed_chains == ["A"]`
- `tests/test_audit_fixes.py`
  `test_trim_from_disk_has_every_attribute_the_gpu_stages_use` — **the R4
  wall**, by reflection, so a new `TrimResult` field must reach `_TrimFromDisk`

Second tier, also load-bearing:
`tests/test_binder_metrics.py` `test_sidecar_remaps_hotspots_into_output_numbering`
(a single chain argument, a flat int list) and its zero-diff golden over 200
refolds (R5); `tests/test_audit_fixes.py`
`test_a_fusion_partner_is_excluded_from_the_design_target` — **a second chain
in the file is excluded from the design target, which is the exact assumption
a glue inverts**; `test_target_and_partner_follow_the_chain_assignment`
(exactly one chain is "the target");
`tests/test_release_fixes.py` `test_packaged_skill_zips_match_their_source` —
editing `SKILL.md` (item 2) means repackaging the `.zip`.

## 6. The three ranked risks, unchanged

1. **A glue gate shipped with a non-null threshold silently zeroes every
   disrupt campaign.** Measured: 1,352 real 3KYS records, 317 survivors → **0**
   with `hotspot_engagement_target >= 0.5` added. It looks like "bad target",
   not "bad config". Ship `null`, and add a test that re-filters a shipped CSV.
2. **`n_tokens` under-counting a two-chain target ~2×** → GPU 3.1×, disk 2.8×,
   and it feeds the calibration verdict and the local-vs-cluster decision.
   Currently closed by construction (§2) — keep it that way.
3. **Broadening `trim_target`** — see §4.

## 7. First commands to run, cold

    # 1. reproduce the failure, free, no API, no GPU
    python -c "
    import json; from pathlib import Path
    from src.handoff import parse_handoff, parse_hotspot_residues
    t = Path('projects/div_standard_diabetes/runs/round-1/02_structure.md').read_text()
    h = parse_handoff(t); print(json.dumps(json.loads(parse_hotspot_residues(t, h)), indent=1))"
    # VERIFIED output, 2026-09-14 — this is what "broken" looks like:
    #   target_chain: R | partner_chain: P | regions_declared: 1 | 7 residues
    #     PHE 66  CD2,CZ     <- chain R (GLP-1R), correct
    #     ASP 67  CG,OD1     <- chain R, correct
    #     ALA 70  CB,CA      <- chain R, correct
    #     ALA 30  CB,CA      <- chain P (GLP-1). On chain R, auth 30 is VAL.
    #     GLY 35  CA,C       <- chain P. On chain R, auth 35 is THR.
    #     ARG 36  CZ,NH1     <- chain P. On chain R, auth 36 is VAL.
    #     GLY 37  CA,C       <- chain P. On chain R, auth 37 is GLN.
    # Every residue is right on its OWN chain; the parser attributes all seven
    # to R, so grounding hard-fails on the last four. `regions_declared: 1`
    # is also wrong — the table has two chain sub-sections.

    # 2. the wall, before touching anything
    .venv/bin/python -m pytest tests/test_structure_trim.py tests/test_foundry.py \
        tests/test_audit_fixes.py tests/test_binder_metrics.py -q

    # 3. after item 1+3+4, the same snippet must show correct per-chain attribution
