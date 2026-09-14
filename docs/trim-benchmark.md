# Benchmarking the trim

`scripts/benchmark_trimming.py`. Run `report` for the current numbers; this
page is the method, the selection rules, and the findings that are not
reproduced by simply re-running it.

## Why

The trim's quality guards — the BSA-retention floor, the hotspot-retention
check, the newly-exposed-hydrophobic gate — had never judged a real cut in a
real campaign. **22 of the 25 trims in `projects/` are no-ops**: the target
already fitted `design.foundry.target_residue_budget`, so every one of them
measures 0.0% exposure and 100% retention. Nothing on disk could calibrate a
threshold, and `MAX_EXPOSED_HYDROPHOBIC_FRACTION` was consequently set from
four cuts forced by hand.

## Two phases

**`interface`** costs API money — measured **$0.11–0.26 and ~90 s per
target** — and buys one thing: a REAL epitope. It runs `--workflow structure
--pdb <ID>`, the entry point that skips discovery and target selection
entirely and calls exactly one LLM stage (`complex-structure-analysis`), so
what comes back has grounded residue names, real RFD3 sidechain atoms, and
has passed every chain-assignment and hotspot-grounding guard. It is
idempotent: an entry whose `21_interface.md` exists is skipped, so a restart
after a failure costs nothing for the entries that landed.

**`ladder`** is free, CPU-only and repeatable. It re-reads those epitopes and
trims each target across a descending budget ladder with the exposure gates
and the BSA floor DISABLED — so it measures rather than being refused early —
then derives what production would have decided. The gate is a pure function
of the measurements (`structure_trim.exposure_verdict`), so deriving the
verdict is exactly equivalent to a second gated trim and costs nothing.

### The real hotspot picker is the point, not a convenience

The earlier sweep used `benchmark_trim.derive_hotspots`, a deterministic
ddG/BSA stand-in. On 5VAI chain R it picked residue **205 — inside the
transmembrane bundle** — which makes the hotspot-bearing "domain" the whole
387-residue chain, so every budget below it raises `TrimBudgetError` and the
sweep returned nothing but errors. A stand-in epitope benchmarks a trim
nobody would ever run. If a ladder returns only budget errors, check which
residues were picked before concluding anything about the trim.

## Selecting targets: domain count, not size

**Chain size is the wrong criterion.** The trim cuts on domain boundaries, so
a target has a cut window only between "below the hotspot-bearing domain set,
raises `TrimBudgetError`" and "at or above the whole chain, no-op" — and for a
single-domain target that interval is **empty**. Measured on 7CZD, one
116-residue Ig domain: budget 117 is a no-op, and 105 and below all raise.

A free pre-screen (`segment_domains`, no API) over the 91 distinct entries in
`data/structures/` found only **16** with a multi-domain largest chain and at
least 25 residues outside the largest domain. That screen is a
prioritisation, not a filter — 3KYS yields real cuts at 208→190 despite
failing it, because `plan_trim` also trims segment ends — but spending API
budget by size alone buys uncuttable targets.

Two other selection traps, both confirmed at $0.00 because they fail before
any LLM call:

- **A monomer is refused outright** (see "Defects" below), so the three
  single-chain entries here contribute nothing.
- **A "complex" whose partner is a peptide is not a complex.** 1LB5's
  partner chain is 8 residues; 3UVW's is 12. The structure workflow finds no
  usable interface.

## Reading the report

Three things in it are deliberately framed as NOT evidence:

- **The accept/refuse split is definitional.** `production_verdict`
  classifies a cut BY the threshold, so "accepted cuts reach X,
  exposure-refused start at Y" with X ≤ threshold < Y is guaranteed
  arithmetic. An earlier revision of the report printed exactly that as
  corroboration; it is circular and it is gone. What the split can honestly
  say is whether any observed cut lands NEAR the threshold, because that is
  what decides how much the exact value matters.
- **The groupings are the non-circular part.** Newly-exposed area is reported
  against `n_segments` (pure geometry), against near-epitope exposure (a
  different residue set, gated unconditionally, owing nothing to the
  fraction), and against how much was removed.
- **A no-op rung must measure 0.0%.** It is the control that separates "this
  cut opened core" from "this structure reads as exposed however you treat
  it". If a no-op ever measures above zero, the exposure measure is reporting
  something other than the cut — which it has done before, via waters
  (desolvation) and via a dropped palmitoyl tail.

## The refusal order matters, and is easy to get backwards

`production_verdict` mirrors `trim_target`, which raises on the FIRST failure
and reports only that one. The real order, measured by character offset in
the source: **near-epitope exposure → the away fraction → the residue-count
fallback → the BSA-retention floor.** Retention is LAST, because it sits
after `write_trimmed` rather than with the interface arithmetic that computes
it.

Ordering them differently does not mis-count refusals, it mis-ATTRIBUTES
them, and in one direction: a cut bad enough to open core beside the epitope
has usually damaged the interface too, so both flags are commonly set
together. Putting retention first relabels much of `REFUSED_NEAR` as
`REFUSED_RETENTION` and makes the exposure guard look like it catches far
less than it does. `tests/test_trim_benchmark.py` pins the order against the
implementation.

## Results, 2026-09-14 — 198 rungs, 23 targets, 133 real cuts

`docs/trim-ladder.tsv`. Targets span **112–582 residues**; every one carries a
real epitope from `complex-structure-analysis` (8–12 hotspots each). Interface
phase cost **$2.98** for 23 epitopes; 5 further entries failed at $0.00 (see
"Defects"). Only 2 of 23 targets yielded no real cut at any budget (7CZD,
7OPB — both effectively single-domain).

### The control holds

**24 of 24 no-op rungs measure exactly 0.0%.** A trim that removes nothing
opens nothing, on every target. That is what licenses reading a non-zero
fraction as a property of the cut rather than of the structure — and it has
not always been true of this measure, which has previously reported
desolvation (ordered waters) and a dropped palmitoyl tail as "exposure".

### Exposure is governed by whether the cut followed a structural unit

This is the scope's physical claim (§2.2) and the benchmark confirms it
against two *independent* variables, neither of which the threshold defines:

| grouping | n | median newly-exposed fraction |
|---|---|---|
| **1 segment** | 89 | **7.6%** |
| 2–5 segments | 33 | 35.6% |
| **≥6 segments** | 11 | **43.5%** |

| grouping | n | median newly-exposed fraction |
|---|---|---|
| removed <20% of the chain | 46 | 16.1% |
| removed 20–40% | 27 | **71.4%** |
| removed **≥40%** | 60 | **7.2%** |

Segment count — pure geometry — is monotonic across a **5.7×** range. How
much was removed is **anti**-predictive: the cuts that removed the MOST
opened the LEAST, and the middle band is the worst by an order of magnitude.
A cut that follows a domain edge can discard 40%+ of a chain and open almost
nothing; a cut that shears a fold opens a great deal while removing a third.
So the fraction is not a proxy for "how big was this cut" — it is tracking
the thing that matters, which is why it is the gate and why an
exposure-minimising search over cut positions (§2.2 item 2) was not worth
building.

Near-epitope exposure is **not** a correlate of it (median 14.3% with none
against 16.1% with some), which is the other thing worth knowing: the two
exposure gates measure genuinely different quantities and neither is
redundant.

### The 25% threshold is well placed but MARGINAL

**Correcting an earlier claim.** From the 4 hand-forced cuts the threshold
was first set on, it looked as though "the whole 10–30% band fits the data
equally well". With 133 real cuts it does not: the window in which the
threshold can move **without changing a single verdict here** is
**21.4%–26.2%**, and 27 of 133 cuts land within 0.6–1.6× of it. 0.25 sits
inside that window, near its top.

So the value is consequential rather than arbitrary, and the honest summary
is narrower than before in one direction and firmer in the other: it is not
calibrated against design outcomes (nothing here refolds anything), but it is
no longer a guess between two anecdotes.

## Defects this surfaced

All three were found by running the benchmark, not looked for.

1. **A monomer cannot be designed against through `--workflow structure`.**
   `pipeline_runner.py:1296` requires BOTH `target_chain` and `partner_chain`
   to be valid, so a single chain's empty partner is refused with
   "target-intel did not name usable chains (target='A', partner='')".
   `--chains A` does not rescue it. CLAUDE.md states "A single chain selects
   `design_intent: inhibit_active_site`, which already existed for AlphaFold
   monomers" — that path is unreachable from this entry point. It fails before
   any LLM call, so it costs nothing, but it also means the no-partner branch
   of the exposure gate (where `MAX_EXPOSED_HYDROPHOBIC` is still the gate,
   for want of a BSA denominator) has **no benchmark coverage at all**.
2. **The biological-assembly file can delete the partner chain, and it is the
   file the pipeline prefers.** 8FYU has chains B (128) and A (126) with a
   2,256 Å² interface in `8FYU.cif` — but `8FYU_ba1.cif`, which
   `_ensure_structure` and `_verify_hotspot_grounding` both prefer, contains
   only B and a 10-residue E. So a legitimate two-chain target is reduced to
   a monomer by file selection and then hits defect 1. Which file you read
   changes which chains exist; an inventory built on the asymmetric unit will
   disagree with what the pipeline sees.
3. **The budget is not a hard bound.** 13 of 198 rungs returned MORE residues
   than their budget, by up to **17** (5VAI, budget 170 → 187 residues).
   `plan_trim` validates the hotspot-bearing domains against the budget and
   only then runs `_bridge_gaps`, which ADDS residues back — up to
   `BRIDGE_GAP = 12` per hole, on the deliberate grounds that a 3-residue
   hole is a worse target than no hole. Not silent in production, because
   `validate_spec`'s `max_complex_tokens` is the real ceiling, but "budget"
   reads as a request rather than a limit.

And one thing that is arguably by design but nothing refuses: **12 of 157
rungs produced ≥6 segments, worst 13** (5VAI at budget 170).
`max_chainbreaks` is derived from the segment count, so an N-segment target
spends N−1 chain breaks before the binder is looked at. A 13-segment target
is a shredded one, and only the exposure gate stands in its way.

## What this still does not establish

The benchmark measures **geometry**, not design outcomes. Nothing here folds
or refolds anything, so it cannot say that a cut at 30% exposure produces
worse binders than one at 20% — only that the two are different kinds of cut,
and that the fraction sorts them the way the structural argument predicts.
Calibrating the threshold against measured design yield is a GPU experiment
(the shape of `GLUE_PIPELINE_SCOPE.md` §5), and it is a different and much
more expensive question than the one answered here.
