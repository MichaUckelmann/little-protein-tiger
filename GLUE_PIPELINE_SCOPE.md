# Molecular-glue design (`design_intent: stabilize`) — scope

**Status: measurement complete, nothing implemented, no GPU work launched.**
Date 2026-09-13. Read-only pass over `src/`, `scripts/`, `skills/`, `tests/`,
`projects/`, plus foundry's own installed source.

## How to read this

Every number below carries a provenance label:

- **[measured]** — I ran a command in this session; the command is in the
  appendix and the scratch scripts are in the session scratchpad.
- **[read]** — I read the literal code/artifact at the file:line given.
- **[survey]** — reported by one of two read-only survey agents I ran over the
  repo, and I spot-checked the load-bearing entries myself (marked
  *spot-checked*). Unchecked survey entries are still worth trusting for
  *where* to look, less so for exact line numbers after an edit.
- **[inferred]** — reasoning, not a measurement. Flagged as such everywhere.

Nothing here is an "expected result presented as a fact". Where I could not
settle something I say so and name the single command that would.

Four things the previous scope (`TARGET_SELECTION_AUDIT.md` §4) asserted turned
out to be **wrong or imprecise**, and they matter. They are in §1.3.

---

# 1. The mechanics — what I confirmed, and four corrections

## 1.1 CONFIRMED: RFD3 merges two target chains itself

Read directly out of foundry's own installed parser, not inferred.

`/home/m.uckelmann_cbs-niob.local/code/foundry_source/foundry/models/rfd3/src/rfd3/inference/input_parsing.py:1319-1323` **[read]**:

```python
        if component == "/0":
            # Reset iterators on next chain
            chain = chr(ord(chain) + 1)
            molecule_id += 1
            res_id = 1
            continue
```

and for every non-`/0` component, `input_parsing.py:1367-1393` **[read]**:

```python
        token = set_indices(array=token, chain=chain, res_id_start=res_id,
                            molecule_id=molecule_id, component=component)
        ...
        res_id += n
```

The component itself is fetched chain-aware —
`foundry/utils/components.py:333-346` **[read]**:

```python
def fetch_mask_from_idx(contig_str, *, atom_array):
    chain, res_id = split_contig(contig_str)
    mask = (atom_array.chain_id == chain) & (atom_array.res_id == res_id)
```

So `70-86,/0,R29-128,P7-37` fetches chain **R** residues 29–128 and chain **P**
residues 7–37 from the input file, and writes all 131 of them into output chain
**B** with consecutive `res_id` 1..131. `/0` is the only thing that advances the
output chain letter. (This is also the exact function that raises
`Residue A344 not found in atom array`, the 3KYS HETATM failure already recorded
in `CLAUDE.md`.)

The *multi-segment* half of this is confirmed on shipped artifacts too
**[measured]** — `projects/mesothelioma_showcase/.../calibration/rfd3/yap1_binder_001_yap1_binder_001_0_model_0.json`:

```
contig      : 70-86,/0,A195-229,A239-343,A345-411
num_chains  : 2
diffused_index_map: {'A195':'B1','A196':'B2',...}  n=207
input chains in map : ['A']     output chains in map: ['B']
```

**Caveat, stated plainly:** no artifact on disk has ever had two *different*
input chains in one contig chain-group. The cross-chain merge is proven by
reading the parser, not by a run. See §9 for the 20-minute experiment that
closes that gap before any code is written.

## 1.2 CONFIRMED: the scoring world survives the merge unchanged

Because the RFD3 output is still two chains (binder A, merged target B),
`binder_metrics`' `binder_chain="A"/target_chain="B"` defaults, `_pair`'s
`[0][1]`, `hotspots_from_rfd3(sidecar, "B")`, MPNN's `designed_chains: ["A"]`,
the prefilter and every threshold in `config.yaml design.binder_ranking` are
untouched. **[read]** across `src/binder_metrics.py:256-257, 392-420, 476-497`
and `src/foundry_spec.py:27, 346`.

The input chain also **survives inside the sidecar** — `diffused_index_map`
keys are input-numbered (`"R66"`), values output-numbered (`"B57"`) — so
per-side engagement is recoverable. **[read]** `binder_metrics.py:404-420`.

## 1.3 FOUR CORRECTIONS to the prior scope

### (a) `foundry_spec.py:120` is NOT the only generator-side blocker

There are **three**, and the other two are the ones that actually stop the run:

1. `src/foundry_spec.py:120` **[read]** —
   `select[_hotspot_key(target_chain, auth)] = atoms` forces every hotspot onto
   one chain. Real, but `_hotspot_key` itself (`foundry_spec.py:61-62`) is
   already chain-parameterised, so this is a two-line fix.
2. `src/structure_trim.py:1164-1174` **[read]** — `build_contig(segments, chain, ...)`
   takes ONE chain and emits `f"{chain}{lo}-{hi}"` for every span. **Without a
   chain-B span in the contig, fixing (1) changes nothing**: `validate_spec`
   gates a chain-B hotspot on there being a chain-B contig span
   (`foundry_spec.py:304`), so the spec would be rejected one check later. This
   is the real blocker.
3. `src/foundry_spec.py:309-315` **[read]** — the trim cross-check
   `got = sorted((lo, hi) for _, lo, hi in spans)` **discards the chain**. A
   two-chain contig cannot equal a chain-less `kept_segments` list, and even
   with a chain-aware trim, `(10,20)` on chain A and `(10,20)` on chain B
   collapse to an indistinguishable pair — so it would also *pass* a spec whose
   spans are on the wrong chains.

The prior scope's "`validate_spec` already handles a two-chain target" is
correct *given* a two-chain contig (`parse_contig` at `foundry_spec.py:143-171`
captures a per-span chain letter **[read]**), but (2) and (3) mean the spec
cannot be produced or cross-checked by the current pipeline.

### (b) 5VAI does NOT "model no sidechains" — and a global guard would miss it

The prior scope says 5VAI is backbone+CB only. **Measured** over
`data/structures/5VAI_ba1.cif` with `parent_atom_names`:

| chain | residues with sidechain atoms beyond CB | those atoms present |
|---|---|---|
| R (GLP-1R) | 342 | 1053/1260 = **83.6 %** |
| P (GLP-1) | 23 | 80/86 = **93.0 %** |

For comparison **[measured]**: 3KYS 100.0 %/100.0 %, 7CZD 100.0 %/100.0 %,
6VJJ 98.4 %/89.7 %.

So a "this structure models no sidechains" guard — recommended in the prior
scope — **would not have fired on 5VAI**. What is actually true is narrower and
more interesting: the *specific residues the skill chose* fall in 5VAI's
truncated 16 %. **Measured** per hotspot:

| hotspot | residue | atoms present | spec wants | verdict |
|---|---|---|---|---|
| R66 | PHE | C, CA, CB, N, O | CD2, CZ | `validate_spec` **REFUSES** |
| R67 | ASP | C, CA, CB, N, O | CG, OD1 | **REFUSES** |
| R70 | ALA | C, CA, CB, N, O | CB, CA | accepts |
| P30 | ALA | C, CA, CB, N, O | CB, CA | accepts |
| P35 | GLY | C, CA, N, O | CA, C | accepts |
| P36 | ARG | C, CA, CB, N, O | CZ, NH1 | **REFUSES** |
| P37 | GLY | C, CA, N, O | CA, C | accepts |

3 of 7 refused; the 4 that pass are two ALA (CB is the whole sidechain) and two
GLY (`CA,C` is pure backbone). Net informative sidechain steer: **zero**.

`validate_spec` (`foundry_spec.py:295-302`) already catches this exactly — it is
the only thing that did. The useful additions are therefore (i) run that same
atom-existence check at **interface-stage** time rather than at spec time, and
(ii) a *steer-quality* check: warn when a hotspot set is dominated by
ALA/GLY/backbone-only atoms, because such a set steers RFD3 weakly whatever the
resolution. Not a global "no sidechains" guard.

### (c) There IS no "clean 5VAI trim" — that was an artefact of the blind measure

The prior scope's table says `R29-128 + P7-37` passes at "2 exposed / 0 near".
I reproduce that exactly with the production function **[measured]** — and it is
wrong, for the reason the prior scope itself identified but did not follow
through.

`_exposed_hydrophobic`'s inner `sasa()` (`structure_trim.py:1453-1510`) measures
**one chain in isolation in both files** **[read]**. For a two-chain target that
is structurally blind to a face opened by cutting the *other* chain away. I
implemented the assembly-context measure (both kept chains present, same
parent-atom restriction, same 15 Å² ΔSASA cut) and compared **[measured]**:

| trim | chain | production (isolated) | assembly context |
|---|---|---|---|
| R29-145 + P7-37 | R | 5 res / 407.0 Å² away, 0 near | 4 res / 366.6 Å² |
| | P | **0 / 0** | **7 res / 349.8 Å²** (LEU20 +87.9, PHE12 +73.0, TRP31 +63.9) |
| **R29-128 + P7-37** | R | 2 / 74.8 away, 0 near | 1 / 57.2 |
| | P | **0 / 0** | **8 res / 492.0 Å²** (PHE12 +121.8, LEU20 +87.9, TYR19 +70.4) |
| R29-128 + P26-37 | R | 2 / 74.8 away, 0 near | 2 / 128.8 |
| | P | 0 away, **1 near** (PHE28) | 2 / 95.8 |
| R29-160 + P7-37 | R | 12 / 831.1 away, 0 near | 11 / 790.6 |
| | P | **0 / 0** | 6 / 330.6 |

The "clean" trim opens **492 Å² of hydrophobic surface on chain P** — GLP-1
residues 7–25 thread into the GLP-1R TM bundle that the cut removed, and the
peptide is left as a mostly-naked helix. Under a per-chain
`MAX_EXPOSED_HYDROPHOBIC = 2` every candidate 5VAI glue trim fails. The only
one that comes close is `R29-128 + P26-37` (2/2 per chain) — which is a
12-residue peptide fragment, below any sensible floor.

**Conclusion: 5VAI is the wrong structure for the first glue campaign.** Not
because of sidechains (see (b)) but because a 387-residue class-B GPCR cannot be
cut down to a glue target without stripping the very bundle its peptide is
buried in. §3 names the right entry, and it needs no trim at all.

### (d) An apo-vs-holo readout is ONE extra fold per campaign, not one per design

The prior scope defers apo-vs-holo as "a second fold per design and a new
stage". The apo reference — the two target chains folded *without* the binder —
is **identical for every design in a campaign**, so it is one RF3 run (~10 s at
these sizes), not N. And the per-design holo side is already on disk: the
target-internal interface confidence is recoverable from the raw PAE. See §4.2.

---

# 2. Question 1 — Trimming

## 2.0 The single most important fact about the trim

**No production foundry campaign has ever substantially trimmed anything.**
Every `trim_map.json` in `projects/` **[measured]**:

| project | entry / chain | before → after | removed |
|---|---|---|---|
| mesothelioma_showcase | 3KYS A | 207 → 207 | 0 |
| e2e_foundry, e2e_boltzgen | 3KYS A | 208 → 208 | 0 |
| mash_e2e | 5GN0 A | 222 → 222 | 0 |
| pdl1_e2e, pdl1_macrocycle | 7CZD B | 117 → 117 | 0 |
| pdl1_rc1 | 8ZNL B | 114 → 114 | 0 |
| pain_receptors_rc1, _v3 | 3N7S D | 84 → 84 | 0 |
| validate_bridge_kras | 6VJJ A | 168 → 167 | 1 |
| il7ra_e2e | 3DI2 B | 195 → 186 | 9 |

Maximum ever removed: **9 residues**. So `MAX_EXPOSED_HYDROPHOBIC = 2`,
`EXPOSED_SASA_DELTA_A2 = 15.0` and `EXPOSED_HOTSPOT_CLEARANCE_A = 10.0` have
never gated a real campaign, and there is no GPU evidence about them in either
direction. That is not an argument to loosen them — it is the reason the §5
benchmark is worth running, and it is why the guards are currently *free*: they
cost nothing today, and would start costing the moment a glue campaign forces
real cutting (§2.3).

## 2.1 (a) When does hydrophobic exposure start to hurt? — unknown, and here is the dose ladder to find out

**The rationale is untested.** I could not find any measurement, anywhere in the
repo or in `projects/`, that relates newly-exposed hydrophobic area to design
outcome. `scripts/benchmark_trim.py`'s docstring names the hypothesis
("a freshly exposed hydrophobic slab is what RFD3 preferentially binds") and the
script measures the *exposure*, never the *consequence*. The `MAX_EXPOSED_HYDROPHOBIC = 2`
and `15 Å²` numbers are, on the evidence available, **reasonable judgement
calls, not calibrations**.

What I *can* do without a GPU is establish the dose axis, so the experiment is
designable. I drove the production `trim_target` down a budget ladder on five
real complexes with the epitope held fixed (12 hotspots derived exactly as
`scripts/benchmark_trim.py` derives them — ddG/BSA ranked, spatially clustered,
capped at `MAX_HOTSPOTS`), `max_exposed_hydrophobic=None` so the guard reports
instead of refusing **[measured]**:

**6VJJ chain A (KRAS; partner RAF1) — every rung ONE segment, clean zero baseline:**

| budget | kept | segs | away n / Å² | near n / Å² | total Å² |
|---|---|---|---|---|---|
| 168 (no-op) | 168 | 1 | 0 / 0.0 | 0 / 0.0 | **0** |
| 160 | 153 | 1 | 8 / 240.8 | 5 / 193.9 | 434.7 |
| 140 | 140 | 1 | 9 / 450.3 | 6 / 350.5 | 800.8 |
| 120 | 117 | 1 | 11 / 746.3 | 6 / 350.5 | 1096.8 |
| 100 | 106 | 1 | 11 / 720.7 | 6 / 387.1 | 1107.8 |
| 90 | 90 | 1 | 10 / 743.1 | 7 / 450.6 | 1193.7 |
| 85 | — | — | `TrimError`: 79 residues, below the 80-residue floor | | |

**3KYS chain A (TEAD1; partner YAP1) — monotone in NEAR-epitope exposure:**

| budget | kept | segs | away n / Å² | near n / Å² | total Å² |
|---|---|---|---|---|---|
| 208 (no-op) | 208 | 2 | 0 / 0.0 | 0 / 0.0 | **0** |
| 200 | 200 | 2 | 10 / 353.0 | 0 / 0.0 | 353.0 |
| 180 | 173 | 1 | 14 / 538.0 | 5 / 156.7 | 694.7 |
| 160 | — | — | `TrimBudgetError` (hotspot domains are 173) | | |
| 140 | 140 | 1 | 19 / 1056.9 | 9 / 256.5 | 1313.4 |
| 120 | 120 | 1 | 13 / 995.4 | 14 / 463.1 | 1458.5 |
| 100 | 100 | 1 | 8 / 554.6 | 14 / 515.3 | 1069.9 |
| 90 | 90 | 1 | 5 / 202.4 | 15 / 678.2 | 880.6 |
| 85 | 85 | 1 | 4 / 166.6 | 16 / 723.3 | 889.9 |

Two things to notice, both consequential:

- **`away` is non-monotone; total exposed AREA and the `near` count are the
  usable dose variables.** On 3KYS `away` peaks at budget 140 and then *falls*,
  because the residues that were "away" get removed entirely and the survivors
  reclassify as "near". A study that used the `away` *count* as its dose would
  get a meaningless U-shape. `near` on 3KYS is cleanly monotone
  0 → 0 → 5 → 9 → 14 → 15 → 16 residues / 0 → 0 → 157 → 257 → 463 → 678 → 723 Å².
- **`budget` is a poor experimental knob.** `TrimBudgetError` punched holes in
  both 3DI2 (7 of 7 cut rungs failed) and 5GN0 (7 of 8 failed) **[measured]** —
  the hotspot-bearing domain doesn't fit, and that error fires before any cut is
  attempted. The two ladders above are the two targets on disk where the knob
  actually produces a ladder.

**How large is 1000 Å² of fresh hydrophobic surface?** Comparable to the epitope
itself. 3KYS's target-side interface BSA is **1617.6 Å²** and 6VJJ's is
**627.7 Å²** **[measured]**. So at budget 140 on 3KYS the trim manufactures a
decoy surface ~81 % the area of the site being designed for; on 6VJJ at budget
120 it manufactures one **1.75×** the intended epitope. If the attraction
hypothesis is true at all, it should be visible at these doses. If it is not
visible at 1194 Å² on KRAS, the guard at 2 residues is far too strict.

**What I cannot say:** whether 2 residues / 15 Å² is right. §5 is the experiment.
§5.6 states in advance which result moves which number.

## 2.2 (b) Can trimming be improved programmatically?

### The segmenter question, settled

The prior scope flagged as "unverified and worth one call": does `plan_trim`'s
segmenter land on 5VAI R **128** or on ~145? **[measured]** — it lands on 128,
and how it gets there matters:

```
chain R: 387 residues with CA, auth 29..421;  gaps: [(128, 135)]
geometric partition: 2 part(s) — 29-212, 213-421
RCSB:  5VAI chain R: 2 ECOD domain(s) 96-204, 208-421   (labelled "Sulfatase,SGSH_C")
```

- `method="geometric"` → `plan_trim` keeps 100 residues, **one** segment,
  exactly `29-128`; `trim_target` then reports `contig 68-86,/0,R29-128`,
  2 exposed hydrophobics away (LEU32 +58.9, VAL36 +15.9), 0 near — **passes**.
- `method="auto"` (the production default, which prefers RCSB) → ECOD, 220
  residues, **two** segments `(29,128),(135,254)`, and `trim_target`
  **refuses**: *"newly exposed 33 hydrophobic residues (TYR242, TYR235, LEU141,
  TYR241, LEU231, TYR252), over the 2 tolerated"*.

Three findings from that one measurement:

1. **The answer is 128, and independently corroborated.** 4ZGM — an unrelated
   GLP-1R ECD X-ray structure — deposits its ECD chain as **auth 29–128**
   **[measured]**. The geometric segmenter plus `_largest_component` found the
   biologically correct domain boundary.
2. **`method="auto"` is a live defect, not a glue issue.** RCSB's ECOD
   annotation for 5VAI chain R is `96-204, 208-421` labelled
   "Sulfatase,SGSH_C" — nonsense for a class-B GPCR — and because tier 1 is
   preferred unconditionally (`segment_domains`, `structure_trim.py:565-573`
   **[read]**), it beats the correct geometric answer. Neither hotspot (66/67/70)
   is inside any ECOD domain, so they are force-kept as "orphans" and the trim
   accretes through the TM bundle. **The exposure guard is what caught it** —
   which is one concrete save on the record for a guard the rest of this
   document says is uncalibrated. Worth a separate, non-glue fix: prefer the
   RCSB tier only when its domains actually cover the hotspots.
3. **There is no "disordered-gap-preferring boundary" in the code.** I read
   `plan_trim`, `_prefer_contiguous`, `_bridge_gaps`, `_drop_islands`,
   `_drop_dangles`, `_largest_component` (`structure_trim.py:659-1001`)
   **[read]** — nothing scores a cut by proximity to an unobserved region.
   `_bridge_gaps` *fills* gaps up to `BRIDGE_GAP = 12`; 5VAI's 129–134 hole is
   6 residues and would have been bridged had the two halves been one
   component. The 128 boundary came from `_largest_component`: the ECD and the
   TM bundle are separate components of the 8 Å CA contact graph. The prompt's
   "does the disordered-gap preference generalise?" has no code to generalise —
   **[inferred]** that a real gap-preference term would be a cheap improvement
   (see below), but it does not exist today.

### Would an explicit exposure-minimising objective help? Measured: barely

I scanned **every** same-size contiguous window that contains all 12 hotspots
and measured the real newly-exposed hydrophobic area of each **[measured]**:

**6VJJ chain A, N = 117 residues (22 admissible windows):**

| | window | segs | away Å² | near Å² | total |
|---|---|---|---|---|---|
| best | 7–123 | 1 | 706.3 | 369.7 | **1076.0** |
| 3rd = production's choice | 0–116 | 1 | 746.3 | 350.5 | **1096.8** |
| worst | 19–135 | 1 | 828.2 | 562.5 | **1390.7** |

Production ranks **3rd of 22**; the entire search space spans 1.29×.

**3KYS chain A, N = 140 residues (42 admissible windows):**

| | window | segs | away Å² | near Å² | total |
|---|---|---|---|---|---|
| best | 265–404 | 1 | 789.7 | 256.5 | **1046.2** |
| production's choice | (budget 140) | 1 | 1056.9 | 256.5 | **1313.4** |
| worst | 227–375 | 2 | 1437.8 | 501.1 | **1938.9** |

Search space spans 1.85×; production leaves ~20 % on the table.

**Verdict: an explicit "minimise newly-exposed hydrophobic area subject to
keeping the epitope and fitting the budget" objective buys 2–20 % and is not
worth building.** The reason is physical, and the contrast is stark:

| case | removed | newly exposed hydrophobic |
|---|---|---|
| 5VAI R 387 → 100 (a genuine domain boundary) | **74 %** | **2 residues / 74.8 Å²** |
| 6VJJ A 168 → 117 (shearing one globular fold) | 30 % | 17 residues / 1096.8 Å² |
| 3KYS A 208 → 140 (shearing one globular fold) | 33 % | 28 residues / 1313.4 Å² |

Removing three-quarters of 5VAI along a real domain edge opens **15× less**
hydrophobic surface than removing a third of KRAS — at 2.5× the removal
fraction. **Exposure is governed by whether the cut follows an autonomous
structural unit, not by where within a fold you place it.** Fine boundary
optimisation is optimising the wrong variable.

### What I would actually change, in priority order

**STATUS (2026-09-14): 1, 2 and 4 are IMPLEMENTED; 3 remains a deliberate
non-change.** Item 1 landed as `structure_trim._domains_cover_hotspots`
(`6220a2a`) — it converts the worst 5VAI outcome into the best, as predicted.
Item 2 landed as `MAX_EXPOSED_HYDROPHOBIC_FRACTION = 0.25`, gating on the
fraction wherever a partner chain defines one and falling back to the residue
count where it does not. Item 4 landed as a line in every `22_trim.md`,
stated on clean trims too. The threshold is still a PROPOSAL, and measuring
it more carefully changed what is known about it — see the next subsection.

1. **Fix the domain-source preference** (defect 2 above). Prefer RCSB CATH/SCOP2/ECOD
   only when the returned domains **cover the hotspots**; otherwise fall through to
   Chainsaw/geometric. Cheap, and it converts the worst measured 5VAI outcome
   (refused at 33 exposed) into the best (passes at 2). **~20 lines in
   `segment_domains`**, `structure_trim.py:544-584`. *Estimate: half a day incl. tests.*
2. **Refuse to shear a single structural unit, instead of tolerating 2 residues
   of the result.** The guard currently asks "how much core did this open?"; the
   better question is "did this cut follow a boundary at all?". A measurable
   proxy: total newly-exposed hydrophobic area as a fraction of the target-side
   interface BSA. 5VAI 74.8/1356.0 = 5.5 %; 6VJJ@117 1096.8/627.7 = **175 %**;
   3KYS@140 1313.4/1617.6 = 81 % **[measured]**. A threshold around 20–30 % of
   the epitope's own area separates these cleanly and is *scale-free*, where a
   residue count is not. **This is a proposal, not a calibration** — §5 is what
   would calibrate it.
3. **Do NOT add capping/patching.** RFD3 conditions on the fixed target
   coordinates; a synthetic cap would be new atoms in the conditioning set that
   do not exist in the real protein, and the binder would be designed against
   them. The honest alternatives are: cut elsewhere, keep the target whole
   (which `target_budget_overshoot: 0.15` already permits), or raise the budget.
4. **Report the exposure as an area, and per residue, in the stage report.** The
   current warning names residues and deltas but the *sum* — the number that is
   physically meaningful — is never computed or recorded. One line.

### The exposure threshold, measured over every cut this checkout can build

The three points above (5VAI 5.5%, 3KYS@140 81%, 6VJJ@117 175%) were totals
of `away + near`, and gating on that total turns out to be the wrong
construction: **any near-epitope exposure already raises unconditionally**, so
two of those three are refused before a fraction is ever consulted. The gate
only ever decides cuts with `near == 0`, and re-measuring for exactly those:

| cut | segs | away | area | fraction |
|---|---|---|---|---|
| 5VAI R 387->100, `R29-128`, a real domain boundary | 1 | 2 res | 74.8 Å² | **5.5%** |
| 3KYS A 208->190, shearing the fold | 2 | 13 res | 525.4 Å² | **32.5%** |
| 5VAI R 387->200, accreting into the TM bundle | 2 | 23 res | 1227.5 Å² | **90.5%** |
| 5VAI R 387->150, ditto | 2 | 20 res | 1249.0 Å² | **92.1%** |

Nothing lands between 5.5% and 32.5%, so the whole 10-30% band fits the data
equally well and there is exactly ONE clean cut in it. 0.25 is the middle of
the range proposed above; erring strict is deliberate, since a false refusal
names the knob and costs minutes while a false pass spends GPU-hours designing
against an artificial face and says nothing.

Two things this measurement established that the original three points hid:

- **The production corpus cannot calibrate this and never could.** 22 of the
  25 trims in `projects/` are NO-OPS — the target already fitted the budget —
  and every one measures exactly 0.0%. The guard has essentially never judged
  a real cut in a real campaign. All four rows above had to be forced by
  sweeping the budget below each chain's length.
- **The residue count and the fraction agree on every cut that exists here.**
  The change is therefore not visible in this corpus at all; it is visible in
  the two shapes the corpus does not contain, and those are pinned by test
  rather than claimed: six exposures at +16 Å² (96 Å², ~7% — the count refuses,
  the fraction accepts) and two at +190 Å² against a 462 Å² epitope (380 Å²,
  82% — the count accepts, the fraction refuses).

`TrimResult` and `trim_map.json` now carry `exposed_hydrophobic_A2`,
`n_exposed_hydrophobic`, `exposed_hydrophobic_fraction` and
`exposed_hydrophobic_auth`, because none of it was recorded anywhere before
and a future calibration has to be fitted on something.

## 2.3 (c) Trimming TWO chains

This is genuinely new work and it is where "trimming becomes a real important
part" lands. Four sub-problems.

### How often is a trim even needed? Measured: 88 % of the time

Over all 108 local complexes with ≥2 chains of ≥15 modelled residues
**[measured]**:

- two largest chains **sum ≤ 220**: 13 / 108 = **12 %** → glue needs **no trim**
- sum > 220: 95 / 108 = 88 % → a two-chain trim is required
- smaller chain below `MIN_TARGET_RESIDUES = 80`: 15 / 108 = **14 %**
- median asymmetry (larger/smaller): **1.04**

So: for the *first* campaign, pick from the 12 % and the trim question does not
arise at all (§3 names such an entry). For the feature to be general, the
two-chain trim has to be built.

### "Keep the smaller partner whole" is an accident of 5VAI, not a rule

Median asymmetry is 1.04 — the two chains of a typical complex are the *same
size*. 5VAI's R:P is 387:31 = 12.5×, the extreme tail. **[measured]** A rule
written from it would be wrong for the median case.

`MIN_TARGET_RESIDUES = 80` also has no defensible per-chain reading: 14 % of
real pairs have a sub-80 chain (GLP-1 at 31, the 8ZNL partner at 58, 6JJW's
20-residue chain), and those are legitimate glue partners — a short peptide
locked into a groove is the canonical glue substrate. **Recommendation: apply
the floor to the TOTAL**, and add a separate, much lower per-chain floor
(≥ `MIN_SEGMENT` = 6, i.e. "at least one thing a binder can touch") plus a
hard rule that **every hotspot-bearing chain must retain its hotspots** —
which the existing hotspot-retention check already enforces once it is made
chain-aware.

### Budget allocation between two chains

**[inferred]** — I have no measurement that discriminates between allocation
policies, and I am explicit that this is the weakest-evidenced recommendation
in the document. The reasoning:

The thing being designed against is the **junction**: a contiguous surface
spanning both chains around the existing interface. So the budget should be
spent by distance from the glue site, not split by chain. Concretely:

1. Run `segment_domains` + `plan_trim` **per chain independently** with that
   chain's own hotspots, each given the *full* budget, so each chain's
   hotspot-bearing unit is identified without pre-emptive rationing.
2. If `sum(per-chain hotspot-bearing units) ≤ budget`, keep them and stop.
   (12 % of cases skip even this.)
3. Otherwise accrete/erode by **3D distance to the glue-site centroid across
   both chains jointly**, which is what `_hotspot_centred_crop`
   (`structure_trim.py:843-863`) already does within one chain — it ranks by
   distance to the hotspot centroid, not by sequence position. Generalising it
   to a two-chain residue list is a small change and reuses logic that is
   already the documented answer for "no domain boundary found".
4. `budget` stays ONE number for the whole target, because that is what RF3
   cost depends on. Per-chain budgets would be a second dispatch waiting to
   disagree with the token count.

The refusal that replaces `min_bsa_retention` is the load-bearing part — see
next.

### The guard that does not exist and must: protect the interface being stabilised

`min_bsa_retention` measures target-vs-partner and, for a glue, **both chains
are the target**. It is not merely mis-scaled, it measures the wrong sign: the
R↔P interface is the thing being *preserved*, not discarded.

Evidence that the existing measure is blind to exactly this: on 5VAI, the
geometric trim reports `bsa_retention 1.0031` — a pass — while simultaneously
warning that it *"removed residues carrying 967 Å² (71 %) of the native R-P
interface"* **[measured]**. Retention is computed over the residues that were
kept (`structure_trim.py:1318-1336` **[read]**, and deliberately so, for the
disrupt case), so a glue trim that destroys 71 % of the interface it is meant
to stabilise sails through with a 100 % score.

**Proposed glue-specific guard**, replacing `min_bsa_retention` when both chains
are targets:

> `glue_interface_retention = BSA(chainA↔chainB, trimmed) / BSA(chainA↔chainB, deposited)`,
> measured on solvent-stripped copies of both (the same `write_trimmed`
> desolvation the current code already does at `structure_trim.py:1295-1309`),
> with a floor **≥ 0.90** by analogy to `min_bsa_retention`'s default and a
> hard refusal below it.

This is a whole-interface ratio, not a kept-residue ratio — the opposite base
from the disrupt guard, on purpose. On 5VAI `R29-128 + P7-37` it would read
863.6/2885.4 = 0.30 and refuse **[measured, from the numbers above]**, which is
the correct answer: that trim removes the TM half of the GLP-1 interface.

### The exposure guard must move to assembly context

Established in §1.3(c): `_exposed_hydrophobic`'s isolated-chain SASA cannot see
a face opened by cutting the other chain. The fix is small and I have already
prototyped and measured it (§1.3(c) table): compute SASA with **all kept target
chains present** in both the deposited and trimmed structures, keeping the
existing parent-canonical-atom restriction and the amino-acids-only filter
(both of which exist for documented reasons — the 7CZD water bug and the 3KYS
P1L palmitoyl bug).

**Warning, and it is a real risk:** this changes the measurement for the
**disrupt** path too, if applied there. On a disrupt trim the second chain in
the file is the *partner*, which is retained whole
(`structure_trim.py:1312-1314` **[read]** sets `keep_map[partner_chain] = None`),
so including it would make the interface itself read as buried surface — which
is precisely why the current code excludes it. **The assembly-context measure
must be conditional on the glue path**, taking the set of *target* chains, and
must be `{target_chain}` (a one-element set) on the disrupt path so the numbers
are byte-identical. `tests/test_structure_trim.py:530` parametrises the no-op
retention test over 7CZD/6VJJ/3KYS and would catch a regression here.

---

# 3. Question 2 — Selection criteria for a glue site

## 3.1 What already exists, and what it is worth

`src/structure_tools.py:1200-1521` `find_glue_pockets` **[read]**, exposed as
`tool_find_glue_pockets` (`structure_tools_server.py:217`) and in
`skill_runner._TOOL_DEFS` (`skill_runner.py:222, 1400-1402`) **[read]**. **No
deterministic stage in `src/` calls it** — it is reachable only as an LLM tool
**[measured, grep]**. `skills/complex-structure-analysis/SKILL.md:121-123,
293-310` is what invokes it in STABILIZE mode **[survey, spot-checked]**.

Its concept is right: a glue pocket is a pair of **periinterface** patches —
residues *not* in the interface but within 10 Å of it and still
solvent-accessible in the complex — one on each chain, clustered at 8 Å,
scored on hydrophobic fraction / spread / SASA, paired if their centroids are
within `max_bridge_span = 20 Å`. That is the correct molecular-glue geometry:
glues bind at the rim of an existing interface, not inside it.

I ran it on four real complexes **[measured]**. Selected output:

| entry | chains | pocket 1 | centroid sep | nA / nB | union CA–CA diameter |
|---|---|---|---|---|---|
| 5VAI | R/P | Excellent | 12.0 Å | 3 / 4 | 16.3 Å |
| 5VAI | R/P (pocket 2) | Excellent | 13.7 Å | **20** / 4 | **32.2 Å** |
| 3KYS | A/B | Excellent | 16.3 Å | 6 / 4 | 24.8 Å |
| 7CZD | B/A | Excellent | 16.7 Å | 2 / 6 | 23.6 Å |
| 3N7S | D/A | Excellent | 19.2 Å | 3 / 8 | 25.1 Å |

Also: 5VAI reports **153** periinterface residues on chain R and **9** on chain
P **[measured]** — a 31-residue peptide has almost no non-interface surface, so
its "glue patch" is forced to be its own termini. That is an independent,
measured reason 5VAI is a hard glue case.

**Five concrete weaknesses, all measured or read:**

1. **`bridgeable` is vacuous** — `structure_tools.py:1477` sets it to a literal
   `True` for every pocket that survived the `sep > max_bridge_span` filter at
   `:1470` **[read]**. It is a restatement of the filter, not a signal.
2. **It has no notion of whether stabilising is USEFUL.** It returns 3
   Excellent/Good pockets on 7CZD (PD-L1 / an anti-PD-L1 VHH) and on 3N7S
   (CALCRL/RAMP1) — complexes nobody wants to glue **[measured]**. The rating
   is a geometry report, not a selection criterion.
3. **It rates on centroid separation but the binding constraint is the UNION
   diameter**, and the two diverge: 5VAI pocket 2 has a 13.7 Å centroid
   separation and a 32.2 Å union diameter, because one patch is 20 residues
   wide **[measured]**. A large patch pair inside a 20 Å centroid cap can
   easily exceed what one binder covers.
4. **Ratings tie constantly.** `combined_rating` is a sum of two 4-level ranks
   (`structure_tools.py:1471-1481` **[read]**); on three of the four complexes
   all top-3 pockets came out "Excellent" — no usable ordering.
5. **It says nothing about whether the chosen residues can carry an atom-level
   steer** — the 5VAI failure in §1.3(b).

## 3.2 The geometric limit, measured

The prompt asks "how far apart may the two patches be?". I measured the
footprint a real RFD3 mini-protein actually covers: the CA–CA **diameter of the
target residue set it contacts** (all-atom, 8 Å, via `binder_metrics.epitope`),
over 528 real designs from 7 independent campaigns **[measured]**:

| campaign | target | n designs | binder aa | epitope diameter median | p90 | max |
|---|---|---|---|---|---|---|
| pain_receptors_rc1 | 3N7S RAMP1 | 80 | 70–85 | 36.4 Å | 37.5 | 37.5 |
| il7ra_e2e | 3DI2 IL7RA | 80 | 70–86 | 30.0 Å | 31.4 | 37.5 |
| mash_e2e | 5GN0 | 80 | 71–86 | 35.1 Å | 38.9 | 41.9 |
| pdl1_rc1 | 8ZNL PD-L1 | 80 | 71–86 | 29.3 Å | 33.0 | 35.5 |
| mesothelioma_showcase | 3KYS TEAD1 | 80 | 70–86 | 35.9 Å | 37.4 | 41.7 |
| validate_bridge_kras | 6VJJ KRAS | 48 | 70–86 | 31.6 Å | 34.4 | 36.5 |
| pdl1_e2e | 7CZD PD-L1 | 80 | 70–86 | 32.3 Å | 32.5 | 36.5 |

Remarkably consistent across seven unrelated targets. **A 70–86mer covers an
epitope of ~30–36 Å diameter, and never exceeded 41.9 Å in 528 designs.**

So the glue geometric criterion, with a number behind it:

> **The union of the two patches' CA atoms must have a diameter ≤ 35 Å to be
> comfortable and ≤ 40 Å at the outside**, for a 70–86-residue mini-protein.
> Centroid separation is a proxy; compute the union diameter directly.

(No RFD3 cyclic campaign exists to measure — RFD3 has no cyclic path — so I
have **no number** for `cyclic_peptide`. A 12–15mer will be far smaller;
`--modality cyclic_peptide` already auto-selects BoltzGen, and a glue at that
size should be treated as unsized until measured.)

## 3.3 What else makes a glue site good — proposals, with their evidence status

| criterion | how to measure | status |
|---|---|---|
| union CA–CA diameter ≤ 35 Å | direct, ~10 lines over `find_glue_pockets` output | **[measured]** ceiling above |
| both patches accessible from ONE side | angle between each patch's outward normal (patch centroid − chain centroid); reject > 90° | **[inferred]** — cheap, untested |
| continuous groove vs two islands | SASA-weighted path, or simply: does a single 8 Å-radius probe sphere path connect them without leaving the surface | **[inferred]** |
| each patch ≥ 3 residues, hydrophobic fraction ≥ 0.5 | `find_glue_pockets` already reports both | **[read]** — thresholds not calibrated |
| every hotspot carries the atoms the spec will name | `validate_spec`'s existing per-atom check, run early | **[measured]** — §1.3(b) |
| hotspot set is not ALA/GLY-dominated | count hotspots whose named atoms are beyond CB | **[measured]** — 0/7 on 5VAI |
| both chains' sidechains are modelled at the patches | per-residue, not per-structure | **[measured]** — §1.3(b) |
| combined size fits the budget with no trim | sum of the two chains' modelled residues | **[measured]** — 12 % of local complexes |
| the existing complex is worth stabilising | **not automatable** | see below |

**On "is stabilising even useful"**: this cannot be a deterministic pre-pass.
Whether a glue is a therapeutic strategy depends on whether the complex's
formation is the desired outcome (GLP-1R/GLP-1 agonism: yes; PD-L1/VHH: the
question is meaningless) — that is exactly the judgement the pathway/target
LLM stages exist to make, and it is upstream of any structure. The deterministic
pre-pass should *rank the sites in a structure the LLM already chose*, never
choose the structure. `find_glue_pockets`' willingness to rate 7CZD "Excellent"
is the demonstration of why.

## 3.4 What `_select_designable_structure` should prefer for `stabilize`

`_select_designable_structure` (`pipeline_runner.py:2540-2650`) is asymmetric:
`accs[0]` is privileged, the search runs from one accession, and `target_len`
is `min(...)` over that accession's chains only **[survey, spot-checked the
`accs[0]` privileging]**. For a glue it needs both accessions profiled and
**both lengths summed**, because RF3 cost depends on total tokens.

**Concretely, and this is the single highest-value finding in §3**: I tested the
GLP-1R ECD alternatives that `div_standard_diabetes`' own pathway checkpoint
already listed **[measured]** (downloaded to scratch, not to `data/structures/`):

| entry | resolution | chain A | chain B | total | sidechains beyond CB | A↔B BSA | best glue pocket |
|---|---|---|---|---|---|---|---|
| **4ZGM** | **1.8 Å** | 100 res (auth **29–128**) | 28 res (auth 10–37) | **128** | **100.0 % / 100.0 %** | 1535.9 Å² | Excellent, sep 14.6 Å |
| 3IOL | 2.1 Å | 100 res (29–128) | 26 res (10–35) | 126 | 99.1 % / 100.0 % | 1294.0 Å² | Excellent, sep 17.4 Å |
| 3C59 | 2.3 Å | 103 res | 27 res | 130 | 98.3 % / 97.4 % | 1594.7 Å² | Excellent, sep 18.8 Å |
| 5VAI | cryo-EM 3.3 Å | 387 res | 31 res | 418 | 83.6 % / 93.0 % | 2885.4 Å² | Excellent, sep 12.0 Å |

**4ZGM has exactly two chains, 128 total residues, 100 % sidechain completeness
on both, and needs no trim at all** — with a 78-residue binder that is **206
tokens**, inside the 220-residue budget and 11 tokens above the 195-token RF3
anchor (9.95 s/refold, 1.05 MB/refold **[measured]**). Its chain A is deposited
as auth 29–128, independently confirming the boundary the geometric segmenter
found on 5VAI.

So the `stabilize` preference rule:

> When `design_intent: stabilize`, prefer the **smallest, highest-resolution
> entry containing BOTH partners with complete sidechains at the candidate
> patches**, scored on the SUM of both chains' modelled residues against
> `target_residue_budget`. A cryo-EM signalling complex is the best *evidence*
> and the worst *design target*; the small X-ray co-crystal is the right one.

This is the same manoeuvre `_select_designable_structure` already performs for
CALCRL (6E3Y → 3N7S), applied with a two-chain size test instead of a
one-chain one.

**Second candidate already on disk**: `3N7S_ba1.cif` at 94 + 84 = 178 residues,
two chains, and it has a measured *disrupt* baseline on the same file
(`pain_receptors_rc1`: prefilter 0.896-class, 84-residue target, 1 segment)
**[measured]** — a glue campaign there would be directly comparable to a
disrupt campaign on the same coordinates.

---

# 4. Question 3 — How do we evaluate results?

## 4.1 (a) Per-side metrics and gates — and the hazard, verified

### The hazard is real, and I reproduced it on 1,352 real records

`binder_ranking._criteria`'s `num()` closure (`binder_ranking.py:174-178`)
**[read]**:

```python
    def num(col: str, lim: float, cmp: Callable[[float, float], bool]):
        def check(r: DesignRecord) -> bool:
            v = _as_float(r.get(col))
            return v is not None and cmp(v, lim)
        return check
```

A missing column → `_as_float(None)` → `None` → `False` → **fails**. The
docstring says so explicitly (`binder_ranking.py:167-170`).

I patched a per-side criterion into `_criteria` in memory and ran
`filter_records` over `projects/mesothelioma_showcase/.../scoring/refold_scores.csv`
**[measured]**:

```
real records: 1352
survivors with shipped thresholds: 317
  threshold=None : survivors 317   (unchanged)
  threshold=0.5  : survivors   0   dropped: hotspot_engagement_target >= 0.5 -> 317
```

**Confirmed exactly.** With the gate live at 0.5, every one of the 317 previous
survivors fails it as their *first* failing criterion. With the threshold
`null`, the criterion is not built at all (`binder_ranking.py:194-199`:
`if lim is None: continue`) and the result is byte-identical.

Two safety layers, not one:

1. `_criteria`'s `spec` list (`binder_ranking.py:181-190`) is a **fixed table** —
   a new threshold key in config is ignored entirely until it is added to that
   table. I verified this: adding `hotspot_engagement_target_min` to the
   thresholds dict left the criterion count at 8 **[measured]**.
2. Once in the table, `null` disables it.

**There is already a shipped precedent for exactly this pattern**:
`ipsae_min_min: null` in both `DEFAULT_THRESHOLDS` (`binder_ranking.py:68`) and
`config.yaml design.binder_ranking.thresholds` **[read]**, pinned by
`tests/test_design_ranking_regression.py:242`
`test_a_none_threshold_survives_the_merge_because_it_disables_a_gate`
**[survey]**. Follow it: ship the glue gates as `null` and set them per-run
from the glue path only.

### The metrics to add

Computable today from `diffused_index_map` with no new math
(`hotspots_from_rfd3` already parses the input chain out of the key and
currently *drops* off-target-chain entries at `binder_metrics.py:418`
**[read]**):

| new column | definition | goes in |
|---|---|---|
| `hotspot_engagement_a` | fraction of chain-A-origin hotspots contacted by the refold | `binder_metrics.FIELDS` (`binder_metrics.py:451-471`) |
| `hotspot_engagement_b` | same, chain-B-origin | same |
| `hotspot_engagement_min_side` | `min(a, b)` — **this is the gate** | same |
| `glue_ipsae_ab` | target-internal ipSAE between the two target halves | same |
| `glue_ipsae_delta` | `glue_ipsae_ab` − the campaign's single apo value | same |
| `target_rmsd` | **already exists and is already reported** (`binder_metrics.py:458, 577`), and is **not gated** | promote to a gate on the glue path only |

`hotspot_engagement_min_side` is the metric that distinguishes a glue from a
competitive binder that grabbed one partner — which the pooled 0.75 gate
provably cannot. New threshold keys, all shipping `null`:
`hotspot_engagement_min_side_min`, `glue_ipsae_delta_min`, `target_rmsd_max`.

`target_rmsd` deserves emphasis: under the merge it is computed over the
**whole R+P assembly** (superposition is fitted on chain T in
`binder_metrics.py:576-577` **[read]**), so a refold in which the partner slips
out of the groove shows up in it directly. It is already in every existing
record, so promoting it is the one glue gate that does **not** need to ship
`null` for the glue path — though it still must ship `null` globally, because
no disrupt campaign was ever gated on it and 13 calibrated campaigns' survivor
counts would move.

One more constraint: `foundry_spec.MAX_HOTSPOTS = 12` **[read]** is per-region,
and a glue set is two regions. 12 per chain = 24 trips the warning at
`foundry_spec.py:102-108`. `tests/test_binder_ranking.py:156` pins the constant
**[survey]**. **[inferred]** the right answer is a cap of 12 on the *union*, not
12 per chain — more hotspots weaken the engagement gate rather than tightening
it, which `CLAUDE.md` already documents, and a glue steer wants ~6 per side.

## 4.2 (b) What actually proves a glue GLUES

The honest readout is the change in the two target chains' *mutual* interface
confidence caused by the binder. I verified the whole thing is computable with
**existing functions and one extra fold per campaign** — not one per design.

### It is one fold, not N

The apo reference is the two target chains folded **without** the binder. That
input is identical for every design in the campaign, so it is **one RF3 run**.
At these sizes: `rf3_seconds_per_refold(128)` ≈ 7 s **[measured, law]**. Round
it to a minute with startup. Negligible.

### The holo side is already on disk, and needs no new math

`ipsae_from_confidences(conf, binder_chain, target_chain, pae_cutoff)` takes the
chain labels as arguments. I tested whether it will score an arbitrary split of
a merged chain, on a real RF3 `confidences.json` from
`mesothelioma_showcase/.../production/rf3_out/` **[measured]**:

```
pae shape (278, 278);  token counts per chain: {'A_1': 72, 'B_1': 206}
real      A vs B : ipsae_min=0.7251  ipsae_max=0.8115
synthetic R vs P : ipsae_min=0.9184  ipsae_max=0.9215   (chain B split in half)
synthetic A vs R : ipsae_min=0.6762
synthetic A vs P : ipsae_min=0.6975
```

The existing function accepts arbitrary chain labels and computes any pairwise
ipSAE from the raw PAE. (The 0.918 is meaningless — it scores two halves of one
covalently continuous chain — but it proves the machinery.) For a real glue,
synthesise `token_chain_ids` as `["A"]*n_binder + ["R"]*n_R + ["P"]*n_P` using
`diffused_index_map` to find the R/P boundary in output numbering, and call the
function twice.

`ipsae_from_pae_matrix` (`binder_metrics.py:357-385` **[read]**) is the wrong
entry point for this — it hard-codes a **two**-block split at `n_binder`. Use
`ipsae_from_confidences` directly with synthesised labels; that is what
`ipsae_from_pae_matrix` itself does internally, so the two stay identical by
construction.

### So the readout is

> `glue_ipsae_delta = ipsae(R,P | binder present, this design) − ipsae(R,P | no binder, once per campaign)`

positive = the binder increased the model's confidence in the R↔P interface.
Cost: one RF3 run per campaign, plus one extra `ipsae_from_confidences` call per
design over a PAE matrix that is already read. **~80 lines, no new stage, no
new GPU work per design.** This is materially cheaper than the prior scope's
"a second fold per design and a new stage" and it should be in the first
implementation, not deferred.

### Cheaper proxies, ranked

1. `target_rmsd` — already computed, already reported, free. Under the merge it
   covers the whole assembly. Gate it. Weakest of the three but zero cost.
2. `glue_ipsae_ab` (absolute, no apo run) — free once the split exists, but
   uninterpretable without a reference, because a well-folded native interface
   scores high with or without a binder.
3. `glue_ipsae_delta` — the real thing, one fold per campaign. Do this one.

**What none of these prove**: that the *physical* complex is stabilised. They
show a folding model became more confident. That caveat must reach the
design-analyst prompt and the report, or a passing glue campaign will be read as
demonstrated stabilisation. The prior scope is right about this and it should
be written into the summary prompt, not just into a document.

## 4.3 (c) Is BoltzGen the better evaluator? Yes for scoring, no for the first build

BoltzGen keeps the two target chains **distinct** in its output — its `include`
and `binding_types` are per-chain lists (`boltzgen/data/parse/schema.py`, per
the prior scope; I did not re-read BoltzGen's source this session, so **[survey
via the prior scope, unverified by me]**). If true, the R↔P chain-pair
confidence a glue is *about* survives natively, where RFD3's merge destroys it.

**It does not change the build order, for three reasons:**

1. §4.2 shows the merge is **recoverable** — the R↔P score is obtainable from
   the ternary PAE with existing code, verified above. The advantage BoltzGen
   offers is convenience, not capability.
2. Every calibrated threshold LPT has is on the foundry path: 13 campaigns,
   `design.binder_ranking` measured on two-chain complexes. BoltzGen's own
   block (`design.boltzgen_ranking`) was measured on cyclic/mini RAMP1 runs and
   its sizing metric is different (`ipsae_min` is refused there outright).
   Sizing a first glue campaign on BoltzGen means sizing it with no calibrated
   bar for a new modality of target.
3. `_refuse_undispatched_site_trials` and the multi-site path already document
   that BoltzGen is *not* dispatched everywhere the binder track goes
   (`CLAUDE.md`), so a BoltzGen-first glue build would ship into a path with
   known holes.

**Recommendation: foundry first, BoltzGen as the second engine** once the
foundry glue campaign has produced a scored set to compare against. Then the
BoltzGen glue spec is a genuinely small change (per-chain `include` lists from
`boltzgen_residue_indices`, which already returns per chain) with a real
reference to validate against.

**MEASURED 2026-09-13, and it narrows point 1's "convenience, not
capability" to something sharper: on BoltzGen it is not even convenience.**
Loading a real per-design `.npz` from the PD-L1 production campaign
(`intermediate_designs_inverse_folded/fold_out_npz/`, 129 tokens, 1,056 atoms,
5 diffusion samples) shows BoltzGen writes coordinates plus **pre-reduced
confidence SCALARS and no matrix at all**. The PAE appears only as
`interaction_pae`, `min_interaction_pae` and `min_design_to_target_pae`, each
shape `(5,)`; the rest is `iptm`/`protein_iptm`/`ligand_iptm`/`design_iptm`/
`design_iiptm`/`design_to_target_iptm`/`design_residue_iptm`, `ptm`/
`design_ptm`/`target_ptm`, `complex_plddt`/`complex_iplddt`/`complex_pde`/
`complex_ipde`, and its own `design_ipsae_min`/`design_to_target_ipsae`/
`target_to_design_ipsae`.

Two consequences:

- **§4.2's recovery route is foundry-only.** `ipsae_from_pae_matrix` needs the
  L x L matrix, which RF3 writes in `confidences.json` and BoltzGen does not
  write anywhere. So an R-vs-P ipSAE over an arbitrary split is computable on
  the foundry path and NOT on BoltzGen, whose reductions are fixed at whatever
  it chose to emit. That is the opposite of the direction §4.3 leans, and it
  reinforces foundry-first rather than qualifying it.
- **Whether BoltzGen emits per-PAIR values for three chains is now an open
  question, not a settled advantage.** The keys above are fixed CATEGORIES —
  `design`, `target`, `ligand`, `complex` — not chain pairs, so a binder plus
  TWO target chains may well collapse into one `design_to_target_*` number
  with the R<->P interface unreported. Settle it before stage 7 leans on it:
  one BoltzGen run on a three-chain YAML, then list the npz keys.

Incidental confirmation of the refusal in CLAUDE.md: `design_ipsae_min` reads
**0.0103-0.0114** on one design and 0.0114-0.0150 on another, inside the
documented 0.0000-0.0289 span, against an RF3-calibrated bar of 0.5 — every
campaign sized on it would verdict STOP.

---

# 5. Question 4 — The GPU trimming benchmark

Designed from the measurements in §2.1. **Nothing launched.** The GPU is busy.

## 5.1 Hypothesis, stated so it can fail

> **H1.** As the newly-exposed hydrophobic area created by a trim increases, an
> increasing fraction of RFD3's binder–target contacts land on that exposed
> patch rather than on the declared epitope.
>
> **H2 (the one the guard is really about).** Newly-exposed hydrophobic surface
> *within `EXPOSED_HOTSPOT_CLEARANCE_A` = 10 Å of a hotspot* costs more per Å²
> than the same area further away.
>
> **H0.** Contact partition is independent of exposure dose. If H0 survives at
> the top of the ladder, `MAX_EXPOSED_HYDROPHOBIC = 2` is far too strict and is
> refusing usable trims (the 5VAI/ECOD case in §2.2 and the 5VAI R29-145 case
> in the prior scope).

## 5.2 Targets, chosen from measurement

**Primary: 6VJJ chain A (KRAS; partner RAF1).** Chosen because every rung is a
**single segment** (no chain-break confound), the no-op rung measures exactly
**0 / 0.0 Å²** **[measured]**, the dose axis is monotone in total area
(0 → 435 → 801 → 1097 → 1108 → 1194 Å²), and a **calibrated baseline already
exists** on the same file: `validate_bridge_kras` measured prefilter 0.896 and
a backbone hit rate of **5/43 = 11.6 % (Wilson 5.1–24.5 %)** at iptm ≥ 0.7 +
gates **[measured]**.

**Replication: 3KYS chain A (TEAD1; partner YAP1).** Chosen because its
**near-epitope** dose is cleanly monotone — 0 → 0 → 157 → 257 → 463 → 678 →
723 Å² **[measured]** — which is the only ladder on disk that tests H2, and
because it has the strongest baseline in the repo: `mesothelioma_showcase`,
**89/481 = 18.5 % (15.3–22.2 %)**, prefilter 0.829, 1,352 production refolds,
best ipTM 0.937 **[measured]**.

Epitope held fixed at all rungs: the 12 hotspots are derived once per target and
`trim_target` raises `TrimError` if any is lost, so every successful rung
retained all 12 by construction **[read + measured]**.

**Rejected after measurement**: 3DI2 (7 of 7 cut rungs die on
`TrimBudgetError`), 5GN0 (7 of 8 die), 7CZD (2 of 3 die), 3N7S (84 residues,
already at the floor) **[measured]**.

## 5.3 The ladders (measured, not planned)

Rungs are the ones the production trimmer actually produces. Full tables in
§2.1.

| | 6VJJ A | 3KYS A |
|---|---|---|
| rungs | 168, 153, 140, 117, 106, 90 | 208, 200, 173, 140, 120, 100, 90 |
| segments | 1 at every rung | 2, 2, 1, 1, 1, 1, 1 |
| total exposed Å² | 0, 435, 801, 1097, 1108, 1194 | 0, 353, 695, 1313, 1459, 1070, 881 |
| near-epitope Å² | 0, 194, 351, 351, 387, 451 | 0, 0, 157, 257, 463, 515, 678 |

The 3KYS segment count changes 2→1 between rungs 2 and 3. **For Phase A this is
not a confound** (Phase A scores every design; the segment-count-derived
`max_chainbreaks` only affects the prefilter, which Phase A does not use). For
Phase B it is, and `rfd3_n_chainbreaks` must be reported per rung so it stays
visible.

## 5.4 The readout — and it needs no refolds, which is the design's key economy

The hypothesis is about **where RFD3 places the binder**. That is decided at
design time. So the primary readout is computable from the RFD3 design CIFs
alone — **no MPNN, no RF3, no refold**.

I verified this end to end on 60 real designs from
`mesothelioma_showcase/.../calibration/rfd3/` using nothing but existing
functions — `binder_metrics.read_structure`, `binder_metrics.epitope`,
`binder_metrics.hotspots_from_rfd3` **[measured]**:

```
design cifs: 580   sidecars: 580
scored 60 designs (no refold, no GPU)
  engagement                   median 0.791  mean 0.814  sd 0.178  min 0.500 max 1.000
  frac_contacts_on_hotspots    median 0.195  mean 0.195  sd 0.018  min 0.150 max 0.242
  n_epitope                    median 45.5   range 33-77
```

Per design, compute:

| quantity | from |
|---|---|
| contacted target residues (output numbering) | `epitope(des, "A", "B", 8.0, binder_backbone_only=False)` — all-atom, as hotspot engagement does (`binder_metrics.py:160-170`) |
| hotspots in output numbering | `hotspots_from_rfd3(sidecar, "B")` |
| exposed-patch residues in output numbering | `_exposed_hydrophobic(deposited, trimmed, chain, hotspots)` → remap via the sidecar's `diffused_index_map` |
| **`patch_contact_fraction`** | \|contacts ∩ patch\| / \|contacts\| |
| **`patch_enrichment`** | `patch_contact_fraction` ÷ (\|patch\| / \|accessible target residues\|) — the size-corrected statistic, and the one to report, because the patch grows down the ladder |
| `hotspot_engagement_design` | \|hotspots ∩ contacts\| / \|hotspots\| — reproduces the existing `hotspots_design` column |

**Nothing new has to be written for the metrics.** What has to be written is
(i) the remap of the exposed-patch residue set through `diffused_index_map` and
(ii) the driver. ~120 lines total, in `scripts/benchmark_trim.py` (§5.7).

Secondary (Phase B, refolds): does the *quality* degrade — `iptm`, `binder_rmsd_dock`,
and the gated backbone hit rate — and does the refold keep the drift the design
had (`epitope_recall`, `epitope_jaccard`, `hotspot_engagement`, all existing
columns).

## 5.5 Statistics and n per rung

**Continuous readout (primary).** Two-sample, α = 0.05, power 0.80,
n ≈ 16σ²/d². Using the **measured** per-design sd of an engagement-type
fraction, σ = 0.178 **[measured, 60 real designs]**:

| effect to detect | designs per rung |
|---|---|
| d = 0.05 | 203 |
| **d = 0.10** | **51** |
| d = 0.15 | 23 |
| d = 0.20 | 13 |
| d = 0.25 | 9 |

**Rate readout (secondary; Wilson, `campaign_calibration.wilson_interval`)**
**[measured]**:

| n | p̂ = 0.10 | p̂ = 0.25 |
|---|---|---|
| 24 | [0.023, 0.258] width 0.235 | [0.120, 0.449] width 0.329 |
| 150 | [0.062, 0.158] width 0.097 | [0.190, 0.328] width 0.138 |
| **300** | **[0.071, 0.139] width 0.068** | **[0.204, 0.302] width 0.098** |
| 580 | [0.078, 0.127] width 0.049 | [0.216, 0.287] width 0.070 |

`rule_of_three(n)` at zero hits: 0.125 at n = 24, 0.030 at n = 100, 0.010 at
n = 300 **[measured]**. `MIN_HITS_FOR_ESTIMATE = 5` **[read]**.

**The prompt is right that n = 24 means nothing** — a 0.235-wide interval cannot
distinguish 8 % from 25 %.

**Choice: 300 designs per rung.** It gives power > 0.99 on d = 0.10 for the
continuous readout, a ±0.07 Wilson interval on the rate readout, and clears
`MIN_HITS_FOR_ESTIMATE` at any plausible rate. 150 would be defensible for
Phase A alone (power 0.80 needs 51) but 300 keeps Phase B interpretable and
the cost difference is under 2 GPU-h.

## 5.6 Cost, computed with the pipeline's own laws

Constants as they actually are in `src/foundry_runner.py` **[read]**:

- `SEC_PER_RFD3_DESIGN = 5.4` (`:49`) — measured "for a ~175-token complex",
  and **it has no size law at all**. `rf3_seconds_per_refold` and `refold_bytes`
  both scale; RFD3 does not. **Flagged as the prompt asks.** For this benchmark
  it is nearly harmless (targets are 90–208 residues, i.e. 168–286 tokens,
  bracketing the 175 it was measured at), but for a two-chain glue at 320+
  tokens it will under-call Phase-A-style costs. **[inferred]** the exponent
  would be milder than RF3's 1.62 because RFD3 is a diffusion loop at fixed
  step count, but that is a guess and the fix is to measure it, not to assume
  one.
- `SEC_PER_MPNN_SEQ = 0.36` (`:50`) — also flat, also unscaled.
- `SEC_PER_RF3_REFOLD = 9.1` at `REF_TOKENS = 195`, `RF3_SIZE_EXPONENT = 1.62`
  (`:67-69`). **Note the prompt's "anchor 9.7 s" is the *measured* value at 195
  tokens; the constant in the code is 9.1**, which reproduces the four fitted
  campaign points to within 7 %.
- `BYTES_PER_REFOLD = 0.97e6`, `REFOLD_DISK_EXPONENT = 1.49` (`:94-95`).

RFD3 output size, **measured** rather than taken from the docstring's "rfd3 ~2 %":
`mesothelioma_showcase/.../calibration/rfd3` = 33 MB for 580 designs =
**56.9 kB/design**.

### Phase A — RFD3 only, no refold

| ladder | rungs | designs/rung | total designs | GPU-h | disk |
|---|---|---|---|---|---|
| 6VJJ A | 6 | 300 | 1,800 | **2.70** | 102 MB |
| 3KYS A | 7 | 300 | 2,100 | **3.15** | 119 MB |
| **both** | 13 | 300 | 3,900 | **5.85** | **222 MB** |

**Phase A fits in one working day on one card, with room to spare.**
(At 150/rung: 1.35 + 1.57 = 2.92 GPU-h, 111 MB.)

### Phase A — COMPLETE (2026-09-14), and the two ladders disagree

Both ladders ran to 300 RFD3 designs per rung, 3,900 designs, ~5.9 GPU-h, and
they answer DIFFERENT questions: 6VJJ's exposure is all far from the epitope
(H1), 3KYS's marches toward it (H2). Patch contacts scored at 8.0 A
heavy-atom; `engage` is the median design-stage `hotspot_engagement` over a
fixed 12-hotspot set, identical at every rung (`derive_hotspots`, top-12 by
BSA — deliberately deterministic, so no LLM variance enters the ladder), and
every rung retained all 12 with BSA retention >= 1.0.

**6VJJ — far exposure. H1 FALSIFIED.**

| rung | tokens | patch A^2 (away/near) | patch % of target | enrichment | engage |
|---|---|---|---|---|---|
| 168 | 246 | 0 / 0 | 0.000 | — (no-trim control) | 1.000 |
| 153 | 231 | 335 / 263 | 0.028 | 0.43 | 1.000 |
| 140 | 218 | 450 / 350 | 0.051 | 0.48 | 1.000 |
| 117 | 195 | 746 / 350 | 0.051 | 0.36 | 1.000 |
| 106 | 178 | 721 / 387 | 0.054 | 0.34 | 1.000 |
| 90  | 168 | 743 / 451 | 0.056 | 0.29 | 1.000 |

H1 predicted enrichment **> 1.0 rising with dose**. Measured **0.29-0.48,
falling as the patch grows**, engagement **1.000 everywhere** — cutting
168 -> 90 residues cost no epitope contact at all.

**3KYS — NEAR-epitope exposure. A signal, at the last rung only.**

| rung | tokens | patch A^2 (away/near) | patch % of target | enrichment | engage |
|---|---|---|---|---|---|
| 208 | 286 | 0 / 0 | 0.000 | — (no-trim control) | 0.875 |
| 200 | 278 | 353 / 0 | 0.000 | 0.27 | 0.917 |
| 173 | 251 | 538 / 157 | 0.022 | 0.24 | 0.917 |
| 140 | 218 | 1057 / 256 | 0.074 | 0.37 | 0.917 |
| 120 | 198 | 995 / 463 | 0.135 | 0.60 | 0.917 |
| 100 | 178 | 555 / 515 | 0.139 | 0.63 | 0.917 |
| 90  | 168 | 202 / 678 | 0.283 | **1.27** | **0.750** |

Read the last row against the other six. Enrichment crosses 1.0 exactly where
near-epitope area (678 A^2, 15 residues) overtakes far area (202 A^2, 5) — and
median engagement drops a whole hotspot, from 11/12 to 9/12. The distribution
moves, not just the median: designs BELOW the 0.75 production gate are
**37.7 %** at rung 90 against 2.3-10.0 % at every other rung (mean engagement
0.790 vs 0.879-0.909). That is the first thing in either ladder that looks like
the hazard `EXPOSED_HOTSPOT_CLEARANCE_A = 10` was written for.

**Enrichment is area-normalised** (`frac / patch_share_of_target`), so "a
smaller target has fewer non-patch residues to touch" is already divided out —
that is why it, and not `patch_contact_fraction`, is the statistic to read.
Two confounds remain and neither is resolved at the design stage: rung 90 is
also the smallest target (90 residues, 168 tokens), and its cut necessarily
removed the residues flanking the epitope, so "the groove got shallower" and
"the fresh patch competes" predict the same engagement drop. Distinguishing
them needs the refolds.

**Free measurement: RFD3 per-design cost HAS a size law.** From sidecar mtimes
within each rung (first 10 dropped, so model load is excluded), 13 rungs x 300
designs:

| target | tokens | s/design | | target | tokens | s/design |
|---|---|---|---|---|---|---|
| 3KYS | 286 | 10.81 | | 6VJJ | 246 | 7.94 |
| 3KYS | 278 | 10.50 | | 6VJJ | 231 | 7.35 |
| 3KYS | 251 | 8.47  | | 6VJJ | 218 | 6.94 |
| 3KYS | 218 | 6.96  | | 6VJJ | 195 | 6.17 |
| 3KYS | 198 | 6.25  | | 6VJJ | 184 | 5.82 |
| 3KYS | 178 | 5.66  | | 6VJJ | 168 | 5.29 |
| 3KYS | 168 | 5.40  | | | | |

Log-log fit: **6.24 s at 195 tokens, exponent 1.28** (R^2 0.963 over all 13;
1.33 on 3KYS alone over 168-286, 1.06 on 6VJJ over 168-246 — the 3KYS exponent
is higher because its range is wider, not because the target differs).
**The two targets agree where they overlap** — 6.96 vs 6.94 s at 218 tokens,
5.40 vs 5.29 at 168 — so this is a size law, not a target effect, and the same
shape as RF3's runtime law (1.62) and the disk law (1.49).
`foundry_runner.SEC_PER_RFD3_DESIGN = 5.4` is flat: right at 168 tokens and
**2.0x low at 286**, so a large-complex campaign's RFD3 half is under-costed by
half. Changing it moves `plan_campaign`'s estimate and through it
`campaign_calibration`'s budget check, i.e. SCALE_UP/STOP verdicts — the same
blast radius the RF3 law has, so it is a deliberate change and not a drive-by.

### Phase B — the design, as revised after Phase A (results above)

**Both ladders are now done — see "Phase A — COMPLETE" above for the two
tables.** H1 is falsified on 6VJJ (enrichment 0.29-0.48, falling with dose,
engagement 1.000 at every rung); H2 has a signal on 3KYS, but only at the
bottom rung (enrichment 1.27, median engagement 9/12, 37.7 % of designs below
the production gate). Note the scope of the 6VJJ claim: RFD3 is STEERED by
`select_hotspots`, so it measures whether exposure diverts a CONDITIONED
binder, not whether it would attract an unconditioned one.

**Two facts killed the original two-point dose plan below.** First, the dose
axis has little signal left to find at the design stage on 6VJJ. Second, and
worse, comparing rung 207 against rung 90 varies exposure, target size, token
count AND segment count together, so any yield difference is uninterpretable —
"smaller target is an easier design problem" predicts the same result. That
argument applies with full force to 3KYS rung 90, which is exactly where its
signal is, so the pairing matters more here rather than less.

**The feasibility numbers that shape the arms**, counted from the completed,
freshly-scored ladders as `round(patch_contact_fraction * n_contacts)` —
i.e. how many of a design's target contacts land on the fresh patch:

| ladder | rung | >=1 | >=2 | >=3 | >=4 | >=5 | median | max |
|---|---|---|---|---|---|---|---|---|
| 6VJJ | 153 | 207 | 86 | 10 | 9 | 9 | 1 | 9 |
| 6VJJ | 140 | 283 | 172 | 40 | 1 | 0 | 2 | 4 |
| 6VJJ | 117 | 285 | 190 | 56 | 9 | 5 | 2 | 13 |
| 6VJJ | 106 | 290 | 209 | 75 | 9 | 1 | 2 | 5 |
| 6VJJ | 90  | 291 | 211 | 79 | 10 | 5 | 2 | 14 |
| 3KYS | 200 | 1 | 0 | 0 | 0 | 0 | 0 | 1 |
| 3KYS | 173 | 219 | 61 | 41 | 23 | 6 | 1 | 12 |
| 3KYS | 140 | 297 | 289 | 218 | 49 | 24 | 3 | 20 |
| 3KYS | 120 | 300 | 299 | 291 | 278 | 237 | 5 | 25 |
| 3KYS | 100 | 300 | 300 | 298 | 283 | 244 | 6 | 21 |
| 3KYS | 90  | 300 | 300 | 299 | 298 | 298 | 15 | 19 |

(An earlier interim pass reported 42/40/29/21/9 for 6VJJ's `>=3` column; the
table above is from the completed 300/300 rungs re-scored by the current
`score` subcommand, and is the authoritative one. The medians and enrichments
reproduced the interim numbers to the digit.)

**This changes which rungs can carry a within-rung contrast, and it differs
per ladder.** On 6VJJ every rung splits (79-283 heavy against 89-213 at <= 1),
so the scope's choice of 90 / 106 / 117 stands. On 3KYS the three bottom rungs
are **saturated** — at rung 90, 298 of 300 designs have >= 5 patch contacts,
so there is no patch-light arm to match against and a within-rung contrast is
arithmetically impossible there. 3KYS's usable pairs are **rung 173** (41
heavy at >= 3 against 81 with none) and **rung 140** (49 at >= 4, and a
top-40-vs-bottom-40 split separates ~6 contacts from ~1). Rung 90's
engagement collapse is therefore a DESIGN-STAGE result that is already
measured and needs no refolds; what the refolds add there is whether the
surviving backbones still fold and dock, which is a one-arm quality question
against the rung 208 control, not a contrast.

**Design: within-rung, patch-heavy vs patch-light, matched.** The driver is
`scripts/benchmark_trim.py phaseb-select | phaseb-launch | phaseb-score |
phaseb-analyze`, and `$SP/phaseB.sh` chains them. Four things below CHANGED
when the selector was run against the real ladders — each is marked, because
three of them were pre-registered differently and the reason for changing
them is data, not preference.

- **Unit**: an RFD3 design. Primary analysis is design-level (best refold per
  design by composite, i.e. exactly `max_per_backbone=1`, the way
  `binder_ranking` already picks); refold-level is secondary, because 4
  sequences off one backbone are correlated.
- **Both arms are restricted to PREFILTER SURVIVORS** (new). A real campaign
  never refolds a design the prefilter rejected, so a gate-pass rate measured
  over rejects would not be the production quantity; and letting the
  prefilter drop designs AFTER matching would undo the clash/chainbreak
  balance the matching just bought. Costs 9-33 % of each rung (202-273 of
  300), and 3KYS rung 90 is the biggest loss, which is itself informative.
- **Arms**: heavy = top designs by patch contacts; light = a matched control
  drawn from an ABSOLUTE patch cap (new) — the smallest cap that still fills
  the arm, so zero-contact designs are used whenever enough exist. Defining
  the light arm by covariate matching alone does not work and the first
  version proved it: given the whole sub-floor remainder, the matcher paired
  a 3-contact heavy design with a 2-contact "light" one on 6VJJ. A
  one-contact gap measures nothing.
- **Matching** (nearest-neighbour, weighted z-scores): binder length,
  **total target contacts** (new), RFD3 clash count, chainbreak count —
  **with hard calipers of ±3 contacts and ±3 residues, and a heavy design
  that has no partner inside them is DROPPED** (new).
- **Design-stage `hotspot_engagement` is NO LONGER a matching covariate**
  (changed). It is a MEDIATOR: on 3KYS's bottom rung engagement falls from
  11/12 to 9/12 and 37.7 % of designs land below the production gate, which
  is the effect Phase B exists to measure, so balancing it conditions away
  part of the causal path and biases every result toward null. It is reported
  per arm instead. `--match-engagement` restores the original set.
- **Measured per refold** via `binder_metrics.score_campaign` (not a second
  implementation): `iptm`, `binder_rmsd_dock`, `binder_plddt`, `ipsae_min`,
  `hotspot_engagement`, the gate verdict from `binder_ranking`'s own criteria
  list (with the gate that rejected it, so the funnel is legible), and
  **patch-contact survival** — the fraction of a design's own patch contacts
  still present in its refold, remapped through the sidecar's
  `diffused_index_map` (never the spec). Survival uses
  `binder_backbone_only=True` on BOTH sides, unlike Phase A's design-stage
  count: RFD3's binder sidechains belong to RFD3's sequence, not to the MPNN
  sequence being refolded, so a sidechain-aware comparison would measure the
  sequence change rather than the pose.
- **Statistics**: Mann-Whitney U on iptm / dock-RMSD; gate-pass rate with a
  Wilson interval per arm (`campaign_calibration.wilson_interval`, the
  pipeline's own) and a Katz log interval on the ratio.

**What the ladders actually support, measured.** Pairs surviving the caliper,
against the 40 requested:

| ladder | rung | pairs | heavy patch | light patch | gap |
|---|---|---|---|---|---|
| 6VJJ | 117 | 26 | 3 | 1 | 2 |
| 6VJJ | 106 | 28 | 3 | 1 | 2 |
| 6VJJ | 90  | 30 | 3 | 1 | 2 |
| 3KYS | 173 | 26 | 3 | 0 | 3 |
| 3KYS | 140 | 20 | 5 | 2 | 3 |
| 3KYS | 120 | 13 | 7 | 4 | 3 |
| 3KYS | 100 | **5** | 8 | 3 | 5 |

3KYS rung 100 is **dropped**: five pairs is not an experiment, and the rung
where the exposure dose is most interesting is exactly where patch contact
and total contact count are most tightly coupled, so the caliper has almost
nothing to match with. 3KYS rung 90 is saturated past matching altogether
(298 of 300 designs have >= 5 patch contacts) and contributes ONE arm of 40,
read against the rung 208 control — and note that control carries 2 target
segments where every trimmed rung has 1, so it is a quality baseline and not
a clean comparator; rung 173's light arm (1 segment, zero patch contacts) is
the better reference for it. `PHASEB_MIN_PAIRS = 15` and
`PHASEB_MIN_SEPARATION = 2` make a rung that cannot answer the question say
so before the GPU time is spent.

**Cost, from the pipeline's own size laws.** Note the refold count is
`designs x 4`, with **no prefilter discount** — selection already restricted
both arms to prefilter survivors, so every selected design reaches MPNN:

| set | tokens | designs | refolds | GPU-h |
|---|---|---|---|---|
| 3KYS rung 173 | 251 | 52 | 208 | 0.79 |
| 3KYS rung 140 | 218 | 40 | 160 | 0.48 |
| 3KYS rung 120 | 198 | 26 | 104 | 0.27 |
| 3KYS rung 90 (single arm) | 168 | 40 | 160 | 0.32 |
| 3KYS rung 208 (control, 60) | 286 | 60 | 240 | 1.13 |
| **3KYS total** | | **218** | **872** | **2.99** |
| 6VJJ rungs 90 / 106 / 117 | 168-195 | 168 | 672 | ~1.53 |
| 6VJJ rung 168 (control, 60) | 246 | 60 | 240 | 0.89 |
| **6VJJ total** | | **228** | **912** | **2.42** |
| **both ladders** | | **446** | **1,784** | **5.41** |

**Power, recomputed on the pairs that exist** (two-sided alpha 0.05, power
0.80). 6VJJ pools to 84 pairs/arm and 3KYS to 59, against the ~120 the
original plan assumed. At a 0.18 baseline gate-pass rate, the proportion test
detects a drop to **0.045** (6VJJ) and **0.02** (3KYS) — i.e. on 3KYS the
binary readout can only see a near-total collapse, and a null there means
almost nothing. The CONTINUOUS outcomes are where the power is: Mann-Whitney
at 84/59 per arm detects a shift of **0.43 / 0.52 SD** in iptm or dock-RMSD,
and the refold-level test (336 / 236 per arm, correlated within backbone)
somewhat better. So read the medians and the U tests first, and treat the
gate-pass ratio as corroboration rather than the headline.

**The GPU path is smoke-tested** (2026-09-14): one 3KYS rung-90 design was
taken end to end before queueing 5.4 GPU-h. MPNN produced 2 structures from
the `solublempnn` alias and RF3 refolded both (15 s each at 168 tokens,
contended with the showcase), `score_campaign` scored them and the gate
attributed both failures to `binder_rmsd_dock <= 5`. Those two refolds were
then DELETED rather than kept: they came from a 2-sequence MPNN pass, and
`--skip-existing` would have let the real 4-sequence run inherit refolds of
sequences it never generated.

**What that test did and did not establish.** An earlier revision of this
paragraph, and the commit message at `a418c10`, claimed MPNN had never run on
this workstation because "no campaign under `projects/` has an `mpnn_out`".
**That is false.** MPNN runs in every foundry campaign on both tracks — 31
`mpnn_out` directories, up to 7,288 threaded structures in one campaign
(`il7ra_e2e` production), 11,188 `.fa` files in total, at all three modes and
inside site trials. The claim came from a glob one level too shallow:
`FoundryPaths.under` is handed `campaign/<mode>`, so the directory is
`campaign/production/mpnn_out` and a check of `campaign/mpnn_out` finds zero
every time. So the smoke test verified the PHASE B WIRING — a hand-built
symlink directory of chosen designs, a non-default `--n-seq`, and a per-rung
parent so `.rf3_staging` cannot be shared — on a stage chain that was already
well proven. Cheap insurance, not the first run of MPNN here.

**The dock readout is a FRACTION, not a median, and rung 173 is why.** On
3KYS rung 173's 208 refolds the dock-RMSD distribution is strongly bimodal —
under 5 A or beyond 30 A, with **2 of 104 per arm in between**. The
design-level medians came out **20.94 A (heavy) against 2.26 A (light)**,
which looks like a 9x effect and is not one: the two refold distributions sit
at 32.8 and 30.5 A, a rank test gives p=0.21, and the real contrast is 28 %
vs 37 % of refolds docking at all. The median of a bimodal distribution only
reports which side of the gap the middle record fell on. `phaseb-analyze`
therefore reports `dock<=5` with a Wilson interval as the dock summary, keeps
the median but marks it `~med dock`, and prints a bimodality warning — and
the threshold is read from `design.binder_ranking.thresholds.
binder_rmsd_dock_max`, so the reported fraction and the gate-pass rate cannot
disagree about what "docked" means.

**First rung, for the record, and it settles nothing** (26 pairs, powered to
detect roughly a halving): gate-pass 8/26 heavy vs 6/26 light (ratio 1.33,
95 % CI 0.54-3.31), `dock<=5` 0.46 vs 0.58, median iptm 0.455 vs 0.641,
U-test p=0.49 (iptm) and p=0.21 (dock). Patch survival in the heavy arm is
**1.000** — every design-stage patch contact re-formed in the refold — which
is the one number already pointing at a branch of the decision rule.
Note the heavy arm passes MORE gates while docking LESS often, which is the
mediator effect showing up as predicted: the light arm's designs carry lower
design-stage engagement (median 0.875 vs 1.000 on this rung), so they lose
more refolds to the 0.75 `hotspot_engagement` gate. Had engagement stayed in
the matching, that difference would have been balanced away.

### Phase B — COMPLETE (2026-09-14). No detectable quality cost, on either ladder.

1,784 refolds over 9 rungs and 143 matched pairs, ~5.4 GPU-h. Design-level
(best refold per design by composite, i.e. `max_per_backbone=1`), gate =
every hard gate in `design.binder_ranking.thresholds`:

| ladder | rung | arm | designs | pass | rate | 95% CI | med iptm | dock<=5 | patch surv |
|---|---|---|---|---|---|---|---|---|---|
| 6VJJ | 168 | control | 60 | 25 | 0.417 | 0.301-0.543 | 0.714 | 0.82 | — |
| 6VJJ | 117 | heavy | 26 | 10 | 0.385 | 0.224-0.575 | 0.588 | 0.69 | 1.000 |
| 6VJJ | 117 | light | 26 | 3 | 0.115 | 0.040-0.290 | 0.466 | 0.65 | 0.000 |
| 6VJJ | 106 | heavy | 28 | 6 | 0.214 | 0.102-0.395 | 0.494 | 0.68 | 0.500 |
| 6VJJ | 106 | light | 28 | 8 | 0.286 | 0.153-0.471 | 0.499 | 0.68 | 1.000 |
| 6VJJ | 90 | heavy | 30 | 11 | 0.367 | 0.219-0.545 | 0.644 | 0.77 | 1.000 |
| 6VJJ | 90 | light | 30 | 13 | 0.433 | 0.274-0.608 | 0.615 | 0.70 | 1.000 |
| 3KYS | 208 | control | 60 | 23 | 0.383 | 0.271-0.510 | 0.734 | 0.67 | — |
| 3KYS | 173 | heavy | 26 | 8 | 0.308 | 0.165-0.500 | 0.455 | 0.46 | 1.000 |
| 3KYS | 173 | light | 26 | 6 | 0.231 | 0.110-0.421 | 0.641 | 0.58 | — |
| 3KYS | 140 | heavy | 20 | 4 | 0.200 | 0.081-0.416 | 0.305 | 0.35 | 0.583 |
| 3KYS | 140 | light | 20 | 6 | 0.300 | 0.146-0.519 | 0.283 | 0.45 | 1.000 |
| 3KYS | 120 | heavy | 13 | 3 | 0.231 | 0.082-0.503 | 0.216 | 0.46 | 0.691 |
| 3KYS | 120 | light | 13 | 5 | 0.385 | 0.177-0.645 | 0.622 | 0.62 | 1.000 |
| 3KYS | 90 | heavy | 40 | **0** | 0.000 | 0.000-0.088 | 0.607 | 0.15 | 1.000 |

**Pooled, paired rungs only:**

| ladder | pairs | gate ratio (heavy/light) | 95% CI | iptm U | dock U |
|---|---|---|---|---|---|
| 6VJJ | 84 | 1.125 | 0.711-1.781 | p=0.578 | p=0.508 |
| 3KYS | 59 | 0.882 | 0.488-1.597 | p=0.180 | p=0.146 |

Both intervals span 1.0 and every per-rung U test is null. Per-rung ratios
scatter on both sides (3.33 / 0.75 / 0.85 on 6VJJ, 1.33 / 0.67 / 0.60 on
3KYS) with heavily overlapping intervals, which is what 13-30 pairs buys.
The heavy arms are also not worse than their **no-trim controls** — 0.385,
0.214, 0.367 against 0.417 on 6VJJ; 0.308, 0.200, 0.231 against 0.383 on
3KYS — with one exception, below. And the null is not an artifact of a
target where nothing docks: 6VJJ's arms dock at 0.65-0.77 within 5 A.

**Verdict against the pre-registered rule: branch 3, not branch 1.** Branch 1
required patch-heavy designs to be no worse AND patch contacts NOT to survive
refolding. They are no worse — but **the contacts survive**: patch survival is
1.000 in five of seven heavy arms and 0.58-0.69 in the other two. So the
reading is "contacts survive, quality unaffected", which the rule assigns to
branch 3: a patch contact becomes a scored liability like
`neg_rosetta_vbuns`.

**IMPLEMENTED 2026-09-14, as an addition and not a substitution.**
`binder_metrics` now writes `n_patch` / `patch_contacts` /
`patch_contact_fraction` / `patch_enrichment` per design, and
`design.binder_ranking.weights` carries `neg_patch_enrichment: 0.5`.
`patch_enrichment` is the rankable one: the raw fraction scales with the
patch's SIZE, which is a property of the trim and identical for every design
in a campaign, so ranking on it would shift every design equally and reorder
nothing while looking like it worked. Verified on a real mesothelioma
sidecar — a 12-residue patch placed around the epitope scores 2.95, the same
patch at the far end of the chain 1.17 (chance), and untouched 0.0 — and on a
no-patch campaign (the usual case) the column is all zeros, which
`composite_score` z-scores to no contribution at all.

**The refusals were deliberately NOT removed**, so branch 3's own wording
("rather than a refusal") is only half-honoured, and that is a correction to
this document rather than an omission. Caveat 3 below says it plainly: both
Phase B measurements held total contacts FIXED, so the experiment asks "given
the same number of target contacts, does having more of them on the fresh
patch cost anything" and never "does exposing a patch cost anything" — which
is the only question the trim guards answer. Caveat 2 is the other half: at
the highest near-epitope dose 0 of 40 designs cleared the gates against a
control's 23 of 60. Removing the guards needs the experiment that varies
exposure itself; 0.5 is a tie-breaker weight, which is what a null result
licenses.

**Three things this does NOT establish, stated so the null is not overread.**

1. **"No detectable effect" is not "no effect", and the intervals say how
   much.** 6VJJ's lower bound of 0.711 excludes a ~30% reduction in pass
   rate; 3KYS's lower bound of **0.488 does not exclude a halving**. A real
   effect of that size would need ~4x the pairs to see, which is ~20 GPU-h,
   not 5.
2. **3KYS rung 90 is the one signal pointing the other way, and the pairing
   cannot isolate it.** At the highest near-epitope dose — 678 A^2 over 15
   residues, median 19 patch contacts per design — **0 of 40 designs cleared
   the gates** against the control's 23 of 60, and only 15% docked within
   5 A. It is saturated (298 of 300 designs have >= 5 patch contacts), so no
   patch-light arm exists at that rung and it contributes one unmatched arm.
   It is also the smallest target (168 vs 286 tokens) with a different
   segment count, and "a smaller target is a harder design problem" predicts
   the same result. Its design-stage engagement collapse (37.7% below the
   0.75 gate) is consistent with either story. **Pooling it into the paired
   comparison is what produced a spurious "significant" result** — gate ratio
   0.526 with an interval excluding 1.0, dock p=0.013 — before the pooling
   was fixed to admit only rungs with both arms.
3. **Both measurements are at a matched total contact count.** The contrast
   is "given the same number of target contacts, does having more of them on
   the fresh patch cost anything" — not "does exposing a patch cost
   anything". The trim guards answer the second question and Phase B does not
   test it.

**Pre-registered decision rule** — written before the refolds run, so the
result cannot be rationalised afterwards:

1. **If patch-heavy designs are NOT worse** (no significant iptm / dock
   difference, gate-pass ratio interval containing 1.0) **and patch contacts
   do not survive refolding**: `MAX_EXPOSED_HYDROPHOBIC` rises from 2 to the
   largest patch actually tested without effect (rung 90's 17 residues /
   ~1,190 A^2), and `EXPOSED_HOTSPOT_CLEARANCE_A` drops from 10 A to the
   smallest near-epitope distance tested without effect. Both become
   `trim_target` kwargs with the measurement in the docstring.
2. **If patch-heavy designs ARE worse**: the guards stay, and the glue track
   needs a trim strategy that does not open patches — which is stage 4's
   two-chain trim problem, and it gets harder, not easier.
3. **If contacts survive but quality is unaffected**: the guards stay as a
   RANKING input rather than a refusal — a patch contact becomes a scored
   liability like `neg_rosetta_vbuns`, not a gate.
4. **Either way**, no threshold moves on the 6VJJ ladder alone: 3KYS's
   near-epitope exposure is the case the 10 A clearance exists for.

### Phase B as originally planned — SUPERSEDED, kept for the numbers


At the measured 3KYS prefilter rate of 0.829 and `n_seq = 4`:

| target res | tokens | refolds | s/refold | GPU-h | disk |
|---|---|---|---|---|---|
| 207 | 285 | 992 | 16.83 | 5.19 | 1.69 GB |
| 200 | 278 | 992 | 16.16 | 5.00 | 1.63 GB |
| 173 | 251 | 992 | 13.70 | 4.32 | 1.40 GB |
| 140 | 218 | 992 | 10.90 | 3.55 | 1.14 GB |
| 120 | 198 | 992 | 9.33 | 3.12 | 0.98 GB |
| 100 | 178 | 992 | 7.85 | 2.71 | 0.84 GB |
| 90 | 168 | 992 | 7.15 | 2.52 | 0.77 GB |
| **all 7 rungs** | | 6,944 | | **26.4** | **8.5 GB** |

Full Phase B is ~1.1 days of GPU. **Restricted to the two extreme rungs only
(207 and 90), 300 designs each: 7.7 GPU-h / 2.5 GB** — which is the right shape,
because Phase B's job is to confirm that whatever partition shift Phase A found
does or does not translate into lost yield, and that is a two-point question.

**Recommended total: Phase A (5.85 GPU-h) + Phase B two-point (7.7 GPU-h) =
13.6 GPU-h, ~11 GB.** Phase A alone in a morning; the whole thing in under two
days.

Free disk on the workstation at the time of writing: **114 GB** **[measured]**,
against `disk_budget_gb: 120` and `min_free_gb: 20` **[read]** — so
`plan_campaign`'s clamp will not fire at these sizes.

## 5.7 How it should be run

Extend `scripts/benchmark_trim.py`, which already does three of the four things
needed **[read]**:

- walks a size spectrum of real complexes (`run(path, budget)`)
- derives hotspots deterministically from `analyze_interface`
  (`derive_hotspots`, the same ddG/BSA/cluster/cap rule the skill applies)
- measures newly-exposed hydrophobic area (`newly_exposed_hydrophobic`)
- CPU-only by default

What to add (~120 lines, one new subcommand, **no changes to `src/`**):

1. `--ladder <file>:<chain>/<partner> --budgets 168,153,...` — emit one trim
   directory per rung with `max_exposed_hydrophobic=None`, recording the rung's
   exposed-patch residue set to JSON.
2. `--spec` — build the RFD3 spec per rung with the existing
   `foundry_spec.build_rfd3_spec` + `validate_spec`.
3. `--designs N` — drive Phase A through the existing pilot machinery
   (`foundry_runner.write_campaign_driver` / `job_registry`), one detached job
   per rung, `n_batches = N/4`. **Use `setsid nohup`, per the standing
   workstation note** — a Bash background task's teardown kills descendants.
4. `--score-designs` — the §5.4 readout over each rung's `rfd3/` directory.
   CPU-only, re-runnable, no GPU.

Phase B is then the existing `--stop-after calibration` on the same spec
directories, or `scripts/resume_cluster_calibration.py`-style direct calls to
`_stage_calibration`. **Do not write a bespoke driver.**

## 5.8 What result changes what number

Stated in advance, with the thresholds named.

Let *f(dose)* be `patch_enrichment` (size-corrected, §5.4) at exposed-hydrophobic
dose *dose* Å², and *f(0)* the no-op rung's value (which is ~undefined since the
patch is empty — use the polar size-matched control patch as the reference,
§5.9).

| result | conclusion | action |
|---|---|---|
| `patch_enrichment > 2.0` at any dose ≥ 400 Å², monotone in dose | H1 confirmed; exposure genuinely diverts binders | Keep the guard. **Replace the residue COUNT with an AREA threshold**: set `MAX_EXPOSED_HYDROPHOBIC_A2` to the largest dose where enrichment stays < 1.5, and retire `MAX_EXPOSED_HYDROPHOBIC` (a count cannot express what the ladder measures — §2.1) |
| enrichment > 2.0 for **near**-epitope patches but ≈ 1.0 for **away** patches | H2 confirmed, H1 not | Keep `EXPOSED_HOTSPOT_CLEARANCE_A = 10` and its zero tolerance. **Raise `MAX_EXPOSED_HYDROPHOBIC` (away) from 2 to ≥ 8**, which is what the 3KYS budget-180 rung (14 away / 538 Å², 0 near) and the 5VAI R29-145 case (5 away, 0 near) both need |
| enrichment ≈ 1.0 (CI including 1.0) at **1194 Å²** on 6VJJ | H0 survives at the top of the ladder | The guard is refusing good trims. **`MAX_EXPOSED_HYDROPHOBIC` → `None` by default** (report only), keep the near-epitope zero rule as a warning, and revisit `EXPOSED_SASA_DELTA_A2` |
| Phase A shows no partition shift but Phase B shows the gated hit rate falling with dose | the damage is to fold quality, not to targeting | Wrong guard entirely: replace the exposure check with a **fold-retention** check. `verify_fold_retained` is already defined (`structure_trim.py:1673`) and — **measured by grep** — is called from **nowhere** in `src/`, `scripts/` or `tests/`: dead, untested code that happens to be the right shape for this outcome |
| Phase A and Phase B both flat across the ladder | trimming into a globular fold is simply cheap | Raise `target_residue_budget` is *not* the conclusion — rather, drop the exposure refusal to a warning and let cost decide, since `rf3_seconds_per_refold` already prices size |

**A specific number the prompt asked for**: if `patch_enrichment` exceeds 2.0 at
400 Å² (the 6VJJ budget-160 rung, 8 away + 5 near), then the effective tolerance
is below 8 residues and `MAX_EXPOSED_HYDROPHOBIC = 2` is roughly right. If it
only exceeds 2.0 at 1100 Å² (the budget-120 rung, 11 + 6 = 17 residues), then
the right value is **~8**, not 2, and the 5VAI/ECOD trim (33 residues) is still
correctly refused.

## 5.9 Controls, and the one I could not construct

- **Negative control (essential): a size-matched POLAR patch.** For each rung,
  pick a set of *polar/charged* residues of the same size, at the same distance
  from the epitope, with the same mean SASA, and compute `patch_contact_fraction`
  on it. Within-design and paired, so it is far more powerful than a
  between-rung comparison, and it controls for "the patch is simply where the
  cut face is, and cut faces are convex". **This is the control that makes the
  experiment interpretable; do not run without it.**
- **No-op rung**: both targets measure exactly **0 / 0.0 Å²** **[measured]** —
  a clean zero.
- **Matched-size, different-exposure rung: I could not build a good one.** The
  same-size window scan (§2.2) found only a 1.29× (6VJJ) / 1.85× (3KYS) spread
  between the best and worst same-size contiguous cuts **[measured]**. That is
  too narrow a contrast to serve as a control. The size-matched polar patch
  replaces it.
- **Size confound, acknowledged**: residue count changes down the ladder, so
  tokens change (168 → 286), so `iptm` and `binder_plddt` are not directly
  comparable across rungs. This is unavoidable — trimming *is* changing size —
  and it is why the **primary readout is a contact partition**, which is
  size-corrected by construction, and why the size-matched polar control is
  within-design.

---

# 6. New pipeline features, enumerated

Estimates are my own and are **[inferred]**; treat them as order-of-magnitude.
Line numbers are current-tree; several come from the survey agent and are
marked. "Risk" is what could silently break the existing 119-disrupt-run world
(detail in §7).

| # | Feature | Where | Est. | Risk |
|---|---|---|---|---|
| 1 | Two-chain hotspot table: chain column in the row regex, `(chain, residue, auth)` dedup key, `target_chains: list` in the returned JSON | `src/handoff.py:85-133` **[read]** | 1 d | **High** — every existing report parses through here |
| 2 | Handoff carries a second target chain (`target_chain_b` / `target_chains`) | `src/handoff.py:82-83` **[read]** + `skills/complex-structure-analysis/SKILL.md:893-899` **[survey]** | 0.5 d | Medium — skill `.zip` must be repackaged (`tests/test_release_fixes.py:1484`) |
| 3 | `_verify_hotspot_grounding` per-chain: look each row up on **its own** chain | `src/pipeline_runner.py:2268-2362` **[read]** | 0.5 d | Medium — this is the guard that caught the PD-L1 incident |
| 4 | `_correct_label_seq_ids` / `_resolve_unverified_label_seq_ids` per-chain maps | `src/pipeline_runner.py:7786-7929` **[survey]** | 0.5 d | **High** — currently *corrupts* a glue table (documented at `:7921-7929`) |
| 5 | `build_contig` over multiple chains | `src/structure_trim.py:1164-1174` **[read]** | 0.5 d | **High** — `tests/test_structure_trim.py:35, 213` pin the format |
| 6 | `kept_by_chain` added **alongside** `kept_segments`, and written to `trim_map.json` | `src/structure_trim.py:130-148, 1627-1667` **[read]** | 1 d | Medium — `_TrimFromDisk` reflection test at `tests/test_audit_fixes.py:1031` **[survey]** |
| 7 | `trim_target(target_chains=[...])` — per-chain `segment_domains`/`plan_trim`, joint budget, joint 3D accretion (§2.3) | `src/structure_trim.py:1210-1440` **[read]** | 3–4 d | **High** — the whole disrupt path goes through it |
| 8 | Assembly-context `_exposed_hydrophobic`, conditional on the target-chain set | `src/structure_trim.py:1441-1546` **[read]** | 1 d | **High** — changes a measurement 13 campaigns' trims were checked against |
| 9 | `glue_interface_retention` guard replacing `min_bsa_retention` on the glue path | `src/structure_trim.py:1318-1440` **[read]** | 1 d | Low (new code path) |
| 10 | `MIN_TARGET_RESIDUES` applied to the total + low per-chain floor | `src/structure_trim.py:74, 705, 838` **[read]** | 0.5 d | Medium — `tests/test_structure_trim.py:485` asserts the literal 80 **[survey]** |
| 11 | `_hotspot_key` fed the hotspot's own chain | `src/foundry_spec.py:120` **[read]** | 0.25 d | Low |
| 12 | Chain-aware trim cross-check | `src/foundry_spec.py:309-315` **[read]** | 0.25 d | Medium — `tests/test_foundry.py:179` **[survey]** |
| 13 | `_stage_trim` two-chain: per-chain topology restriction, chain-keyed `allowed_auth`, summed budget check | `src/pipeline_runner.py:3424-3617` **[survey, spot-checked :3433-3439]** | 2 d | Medium |
| 14 | `membrane_topology.restriction_for` per chain, chain-keyed `allowed_auth` | `src/membrane_topology.py:266-273` **[survey]** | 1 d | Medium — `allowed_auth` is a bare `set[int]`; two chains' author spaces collide |
| 15 | Per-side engagement + `glue_ipsae_ab`/`_delta` columns | `src/binder_metrics.py:451-471, 392-420, 496-627` **[read]** | 1.5 d | **High** — `tests/test_binder_metrics.py:210` is a zero-diff golden over 200 refolds **[survey]** |
| 16 | New gates in the `spec` table, all shipping `null` | `src/binder_ranking.py:181-190` + `config.yaml` **[read]** | 0.5 d | **High if a threshold ships non-null** — measured: 317 → 0 survivors (§4.1) |
| 17 | Apo fold: one RF3 run per campaign + the delta | `src/pipeline_runner.py` binder-scoring stage | 1 d | Low |
| 18 | `n_tokens` = both chains + binder | `src/pipeline_runner.py:4313, 4580` **[read]** | 0.25 d | **High if missed** — 1.62 exponent means a 2× target under-costs GPU ~3.1× and disk ~2.8× |
| 19 | `_select_designable_structure` two-accession, summed size, `stabilize` preference (§3.4) | `src/pipeline_runner.py:2540-2650` **[survey, spot-checked]** | 2 d | Medium |
| 20 | Deterministic glue-site pre-pass wrapping `find_glue_pockets` + union-diameter/same-side/atom-existence filters (§3.3) | new, ~`src/glue_sites.py`; `find_glue_pockets` at `src/structure_tools.py:1200` **[read]** | 2 d | Low (new) |
| 21 | `design_intent: stabilize` becomes a real branch; reachable from `--workflow structure` | `src/pipeline_runner.py:1499, 2910-2912` **[read]** | 0.5 d | Low |
| 22 | Report: two target chains in the Mol* payload, chain column in the hotspot table, non-`disrupt` hero text | `src/binder_report.py:180-200, 387, 557-569`; `src/ppi_report.py:171-185, 288-299, 471-486` **[survey]** | 1.5 d | Low |
| 23 | Early atom-existence + steer-quality check on the hotspot set (§1.3(b)) | `src/pipeline_runner.py` interface stage, reusing `foundry_spec.py:295-302` | 0.5 d | Low — would have saved the 5VAI run an LLM stage |
| 24 | Domain-source preference fix: prefer RCSB only when its domains cover the hotspots (§2.2) | `src/structure_trim.py:544-584` **[read]** | 0.5 d | Low, **and independently valuable** |
| 25 | Extend `scripts/benchmark_trim.py` with the §5 ladder/spec/score subcommands | `scripts/benchmark_trim.py` **[read]** | 1.5 d | None (script only) |
| 26 | BoltzGen glue spec (per-chain `include`/`binding_types`) | `src/boltzgen_spec.py:306-354` **[survey]** | 1 d | Low — second engine, after foundry |

**Rough total: ~30 working days** for items 1–26, of which items 24, 25, 23 and
16 (~3 days) are independently useful and carry almost no risk.

---

# 7. Risk to the current pipeline

This is the section that matters most. 119 recorded `disrupt` stage runs, 13
calibrated foundry campaigns, and every threshold in
`config.yaml design.binder_ranking` measured on two-chain complexes.

## 7.1 The five ways this silently breaks the existing pipeline

### R1 — A new gate with a non-null threshold empties every existing campaign

**Verified, with numbers** (§4.1): 1,352 real 3KYS records, 317 survivors under
the shipped thresholds. Add `hotspot_engagement_target >= 0.5` and survivors go
to **0**, with the new criterion as the first failing criterion for all 317.
With the threshold `null`, survivors stay at 317 exactly.

*Silent?* Yes — a campaign simply reports 0 survivors and the calibration gate
returns `STOP`, which reads as "this target is bad".

*Pin:* `tests/test_design_ranking_regression.py:242`
`test_a_none_threshold_survives_the_merge_because_it_disables_a_gate` **[survey]**
already pins the mechanism for `ipsae_min_min`. Add the same test for each new
key, plus a test that re-runs `filter_records` over a shipped
`refold_scores.csv` and asserts the survivor count is unchanged by the glue
columns' absence. `tests/test_binder_ranking.py:31, 41` pin the exact criterion
label strings and will catch a reordering of the `spec` table **[survey]**.

### R2 — The assembly-context SASA change silently moves every trim's exposure number

Item 8. `_exposed_hydrophobic` currently excludes the partner chain *on
purpose*: including it makes the interface read as buried surface, and the
docstring records two real bugs that came from getting this wrong (7CZD's
waters, 3KYS's P1L tail) **[read]**.

*Silent?* Yes, in the worst direction — a previously-passing no-op trim starts
refusing. That is exactly the 6VJJ/7CZD class of bug already fixed once.

*Pin:* `tests/test_structure_trim.py:530`
`test_a_trim_that_removes_nothing_retains_everything`, parametrised over 7CZD /
6VJJ / 3KYS (list at `:525`), asserts `bsa_retention == 1.0 ± 0.005` and
`bsa_dropped_A2 == 0 ± 1.0` **[survey]**. Plus `:438`
`test_the_exposure_guard_compares_the_same_atoms_on_both_sides` and `:490`
`test_two_exposed_hydrophobics_are_tolerated_but_three_are_not` (which asserts
the literal `MAX_EXPOSED_HYDROPHOBIC == 2`) **[survey]**. **Mitigation: make the
chain set a parameter defaulting to `{target_chain}`**, so the disrupt path is
byte-identical by construction, and add a test asserting the two paths agree on
a one-chain target.

### R3 — `n_tokens` computed from one chain under-costs a two-chain campaign ~3×

`pipeline_runner.py:4313` and `:4580` **[read]**:

```python
        n_tokens = trim.n_residues_after + _binder_midpoint(trim.contig)
```

`trim.n_residues_after` sums flat, chain-less `kept_segments`
(`pipeline_runner.py:199-201` **[survey]**). If `kept_segments` becomes
chain-keyed and `n_residues_after` is not updated, the token count halves. With
`RF3_SIZE_EXPONENT = 1.62` and `REFOLD_DISK_EXPONENT = 1.49`, a 2× token
underestimate under-calls GPU-hours by 2^1.62 = **3.1×** and disk by
2^1.49 = **2.8×** **[measured, laws]**. That feeds `est_gpu_hours`, the
`choose_compute()` local-vs-cluster decision, `plan_campaign`'s disk clamp, and
— through `campaign_calibration.calibrate` — the SCALE_UP/STOP verdict.

*Silent?* Completely. This is the same shape as the MASH/TEAD4 incident already
in `CLAUDE.md`, where the gate costed 22,197 refolds at 63 GPU-h against the
planner's 138.

*Pin:* `tests/test_audit_fixes.py:1056`
`test_trim_from_disk_falls_back_to_the_segments` asserts
`_TrimFromDisk({"kept_segments": [[27,110]], ...}).n_residues_after == 84` and
`[[1,10],[21,30]] → 20` **[survey]** — it pins the flat-sum behaviour, so a
chain-keyed `kept_segments` breaks it loudly, which is the right outcome. Also
`tests/test_foundry.py:536` `test_refold_estimate_grows_with_complex_size` and
`tests/test_audit_fixes.py:219`
`test_disk_is_sized_from_the_complex_not_a_flat_constant`, whose measured points
top out at 300 tokens — **a two-chain glue at 320+ tokens is outside the fitted
range of both laws**, which is worth saying in the campaign log.

### R4 — `_TrimFromDisk` reflection: any new `trim.<attr>` must be reconstructible from `trim_map.json`

`tests/test_audit_fixes.py:1031`
`test_trim_from_disk_has_every_attribute_the_gpu_stages_use` regexes
`src/pipeline_runner.py` for `\btrim\.([a-z_][a-z0-9_]*)` and asserts every name
found is an attribute of a `_TrimFromDisk` built from a **4-key** stub
**[survey]**. So `trim.kept_by_chain`, `trim.target_chains`, `trim.glue_retention`
each need a `trim_map.json` field and a `_TrimFromDisk` fallback, or the test
fails immediately.

*This is a feature, not a hazard* — it is the test that caught the 10-day
`n_residues_after` outage documented in `CLAUDE.md`. Treat it as the design
constraint it is: **every new trim attribute must survive a process restart.**

### R5 — The golden specs and the zero-diff scorer

Three tests are literal goldens and will fail on any shape change **[survey]**:

- `tests/test_foundry.py:71` `test_regenerates_the_reference_cd79b_spec_field_for_field`
  — reproduces the shipped `CD79b_binder_001.json` field-for-field, with a
  single `target_chain="C"` kwarg and `select_hotspots` keys `"<chain><auth>"`.
- `tests/test_binder_metrics.py:210` `test_reproduces_the_reference_scorer_row_for_row`
  — `score_campaign` vs `data/BCR/refold_scores.csv` over 200 real refolds on
  all 25 metric columns (`_GT_COLS`, `:178-187`).
- `tests/test_structure_trim.py:227` `test_reproduces_the_hand_made_cd79_extracellular_trim`
  — asserts `n_segments == 1`, `hi == 145`, `abs(lo - 44) <= 4`,
  `100 <= n_residues_after <= 110`, 6 hotspots retained.

*Mitigation:* every new signature must be **additive with a default that
reproduces today's behaviour** — `target_chains: list | None = None` falling
back to `[target_chain]`, new FIELDS appended (never reordered — `write_scores`
uses `csv.DictWriter(fieldnames=FIELDS, extrasaction="ignore")`,
`binder_metrics.py:947` **[read]**, so appending is safe and reordering is not).

## 7.2 The regression wall — the eight tests to run on every commit

Total suite: **1,217 collected, 21 deselected** (network-marked;
`pyproject.toml:99`) **[survey, agent ran the collection]**.

| test | pins |
|---|---|
`tests/test_structure_trim.py:35` `test_build_contig_shape` | `"68-86,/0,B42-145"` and `"70-86,/0,A1-10,A20-30"` — one binder range, one `/0`, one chain letter per span |
`tests/test_structure_trim.py:213` `test_contig_matches_the_kept_segments` | the kept_segments↔contig invariant, single-chain-shaped |
`tests/test_structure_trim.py:485` `test_the_floor_is_eighty` (+ `:475`, `:463`) | `MIN_TARGET_RESIDUES == 80` as one scalar for one chain |
`tests/test_structure_trim.py:530` `test_a_trim_that_removes_nothing_retains_everything` | no-op retention on 7CZD / 6VJJ / 3KYS — **the R2 wall** |
`tests/test_foundry.py:71` `test_regenerates_the_reference_cd79b_spec_field_for_field` | the golden RFD3 spec |
`tests/test_foundry.py:179` `test_validate_cross_checks_against_the_trim` | chain-less `(lo, hi)` kept_segments |
`tests/test_foundry.py:208` `test_mpnn_config_shape` | `designed_chains == ["A"]` |
`tests/test_audit_fixes.py:1031` `test_trim_from_disk_has_every_attribute_the_gpu_stages_use` | **the R4 wall** |

Second tier, noisier but load-bearing: `tests/test_binder_metrics.py:116`
`test_sidecar_remaps_hotspots_into_output_numbering` (asserts
`hotspots_from_rfd3(sidecar, "B") == [33,34,35,46,48,89]`, a single chain
argument and a flat int list); `tests/test_binder_metrics.py:210` (R5);
`tests/test_binder_ranking.py:31/41` (exact criterion label strings);
`tests/test_audit_fixes.py:1070` `test_target_and_partner_follow_the_chain_assignment`
(`_order_names_by_chain(...) == ("RAMP1","CALCRL")` — exactly one chain is "the
target"); `tests/test_audit_fixes.py:825`
`test_a_fusion_partner_is_excluded_from_the_design_target` (**a second chain in
the file is excluded from the design target — the exact assumption a glue
inverts**); `tests/test_audit_fixes.py:561`
`test_an_af_model_defaults_to_the_single_chain_mode`; `tests/test_docs_match_code.py:171`
`test_claude_md_thresholds_match_the_config_they_cite` (any change to
`target_residue_budget` forces a `CLAUDE.md` edit);
`tests/test_release_fixes.py:1484` `test_packaged_skill_zips_match_their_source`
and `:1517` `test_every_skill_referenced_tool_exists_in_the_mcp_transport`.
All **[survey]**.

## 7.3 The three biggest risks, ranked

1. **A glue gate shipped with a non-null threshold silently zeroes every
   disrupt campaign.** Measured: 317 → 0 survivors. The failure looks like "bad
   target", not "bad config". Mitigated by shipping `null` (precedent exists)
   and by a test that re-filters a shipped CSV.
2. **`n_tokens` under-counting a two-chain target by ~2×, hence GPU by 3.1× and
   disk by 2.8×.** Feeds the calibration verdict and the cluster decision.
   Completely silent. Same shape as an incident already in `CLAUDE.md`.
3. **Broadening `trim_target` to two chains changes the one function all 13
   calibrated campaigns' targets went through.** Especially the exposure
   measurement (R2), which must stay byte-identical for a one-chain target or
   previously-passing no-op trims start refusing.

---

# 8. Build order, with stop/go checkpoints

Cheapest and most informative first. Each stage ends with a decision, and
**nothing GPU-shaped happens before stage 3**.

### Stage 0 — DONE (`6220a2a`, 2026-09-13)

Items 24 (domain-source preference), 23 (early atom-existence + steer-quality
check), and the exposure **area** reported alongside the count (§2.2.4).

**Every go/no-go met.** Full suite green (1222). `method="auto"` on 5VAI now
returns `geometric`, `[(29, 128)]`, 1 segment, 100 residues, BSA retention
100.3%, 2 exposed hydrophobics — where it previously refused at 33. The 3
unbuildable 5VAI hotspot atoms (PHE66 `CD2,CZ`, ASP67 `CG,OD1`, ARG36
`CZ,NH1`) and the 4 weak-steer ones (ALA70, ALA30, GLY35, GLY37) are both
reported at interface-stage time. The exposure warning reads "75 A^2, 6% of
the target-side interface area", matching §2.2's measured 74.8/1356.0.

Two decisions made during the build, both narrower than the scope proposed:

- **The atom check WARNS, it does not refuse.** Whether `rfd3_atoms` matters
  is engine-dependent — BoltzGen steers from `binding:` label_seq entries and
  never reads the column — so a refusal would end a legitimate BoltzGen
  campaign over something its generator ignores. `validate_spec` still
  hard-fails for the runs that build an RFD3 contig; only the diagnosis moved
  earlier.
- **The exposure AREA is reported, not gated,** and no `TrimResult` field was
  added for it. Every threshold in `structure_trim.py` is expressed in
  residues; adding a field creates the `_TrimFromDisk` reflection obligation
  (`test_audit_fixes.py`) for a number no consumer reads yet. The area travels
  in `warnings`, which is what reaches the stage report. §5 is what would
  calibrate a fraction-based gate, and this is the measurement a campaign
  needs recorded first.

Foundry regression net clean: 43 artifact hashes identical, 13/13
re-derivations bit-identical, zero survivor-count changes — `trim_target` is
the function all 13 calibrated campaigns went through.

### Stage 1 — Settle the merge empirically · **DONE, PASS (2026-09-13)**

**Verdict: RFD3 merges two input chains into one output chain.** Measured on
one 4-sample RFD3 design against 4ZGM with the hand-built spec
`70-86,/0,A29-128,B10-37` (7 hotspots straddling the A/B interface, taken from
the pipeline's own `analyze_interface`, validated by the UNMODIFIED
`validate_spec`: 128 target residues, 2 segments):

| check | result |
|---|---|
| output chains | **2** — `A` 79 residues (the diffused binder), `B` 128 |
| `diffused_index_map` keys | span **both** input chains: 100 `A*` + 28 `B*` |
| `diffused_index_map` values | **all `B*`** |
| output res_ids | **1..128, consecutive** |

So a molecular-glue target reaches scoring in exactly the shape the existing
code assumes — one binder chain against one target chain — and stages 3-7 rest
on a measurement rather than a reading of foundry's source. `check_merge.py`
asserts all four mechanically.

**Two things the probe cost that the plan did not anticipate**, both now fixed
in the repo rather than in the probe:

1. **RFD3 addresses residues by its loader's `res_id` — author numbering from
   a PDB, `label_seq_id` from an mmCIF.** The first two attempts fed it
   `4ZGM_ba1.cif` and died with `[component=A106] Residue A106 not found in
   atom array` for a residue plainly present as `ATOM ... ALA A ... 106` —
   4ZGM chain A is auth 29-128 / label 6-105, and A106 is the first id past
   the LABEL range. The pipeline is safe only because `_stage_trim` passes
   `trimmed.pdb`; `validate_spec` now refuses an mmCIF whose two numberings
   disagree, and **that guard immediately caught all 13 Phase A rung specs**,
   which pointed at `trimmed.cif`. Spans falling inside the label range would
   have mis-modelled silently.
2. `extra.num_chains` **does not exist** in this foundry build's sidecar (keys
   are `ckpt_path`, `diffused_index_map`, `metrics`, `seed`, `specification`),
   so the go/no-go below was unsatisfiable as written. The chain count comes
   from the output CIF.

#### As originally specified

Build a two-chain RFD3 spec by hand on `4ZGM` (`70-86,/0,A29-128,B10-37`),
validate it with the **unmodified** `validate_spec`, and run **one** RFD3 design
(4 samples, ~22 s). Inspect the sidecar.

**Go/no-go:** `extra.num_chains == 2`, `diffused_index_map` keys span **both**
`A*` and `B*` and all values are `B*`, `sampled_contig` shows 128 consecutive
target res_ids. **If this fails, the entire "merge at the contig" framing is
wrong and everything below is void.** This is the single cheapest, most
decisive experiment in the plan and it must come first. (`validate_spec` may
refuse the trim cross-check — pass `kept_segments=None` for this probe, and note
that the refusal is item 12, not a surprise.)

### Stage 2 — Phase A and Phase B: DONE (2026-09-14). Both hypotheses answered.

`scripts/benchmark_trim.py` gained four subcommands — `ladder`, `spec`,
`designs`, `score` — and everything that does not need the GPU has been run.
Findings, all reproducible with the commands in §5.7:

**Both ladders build, and 3KYS reproduces §5.3 EXACTLY**: total exposed 0, 353,
695, 1313, 1459, 1070, 881 A^2 and near-epitope 0, 0, 157, 257, 463, 515, 678
A^2 — the monotone near-epitope dose that makes it the only ladder testing H2.
All 13 rungs build a spec that the unmodified `validate_spec` accepts, each
retaining all 12 hotspots, at 168-286 tokens.

**One rung does not reproduce and mine is the authoritative number.** 6VJJ at
budget 153 measures 335 away + 263 near = **598 A^2** against §5.3's 435. Rungs
140/117/106/90 match to the digit (801, 1097, 1108, 1194), so the difference is
specific to that rung. `benchmark_trim.py`'s own older
`newly_exposed_hydrophobic` gives a third answer (897 A^2) — it does not
restrict to parent-canonical atoms and does not split near from away — which is
the likeliest origin of the disagreement. The ladder now calls the PRODUCTION
`_exposed_hydrophobic`, which is the function whose thresholds are being
calibrated, so that is the number to use.

**Phase A costs 5.85 GPU-h only if it runs RFD3 ALONE — and §5.7's
"drive it through `write_campaign_driver`" does not.** That driver runs the
full pipeline (design -> prefilter -> MPNN -> RF3), and `plan.est_gpu_hours`
over the same 13 rungs reads **34.8 GPU-h** (15.0 for 6VJJ's six, 19.8 for
3KYS's seven) — six times the budget, almost all of it refolds the readout
never looks at. `launch_designs` therefore uses `_rfd3_command` rather than the
driver: same command, same sampler settings, same checkpoint alias, same
binary resolution, no refolds. That reads **2.70 + 3.15 = 5.85 GPU-h**, which
is where §5.6's figure comes from.

**The scorer is validated against the pipeline's own column, not against
itself.** `hotspot_engagement_design` was compared per design to the
`hotspots_design` that `mesothelioma_showcase` recorded at calibration time:
**1,924 matched refold rows, max |delta| 3.3e-5** (mine rounds to 4 dp, the CSV
to 3), median 0.917 on both sides. This matters because §5.4's own figure
(median 0.791 over 60 designs) is neither — it is a 60-design sample, and the
whole-set answer differs by DESIGN SET rather than by computation: all 580 raw
RFD3 outputs give median 1.000, the 481 that pass the prefilter give 0.917. A
scorer that was wrong would compress exactly the difference the experiment
measures (diary 2026-09-10), so it is checked against a number the pipeline
produced independently.

**Remaining: the GPU half.** 5.85 GPU-h, queued behind the showcase, `queue3`
and stage 1. `SEC_PER_RFD3_DESIGN` is a flat constant with no size law, and
Phase A measures one for free — the rung directories span 168-286 tokens.

#### Original stage-2 plan, for reference · ~1.5 d + 5.85 GPU-h

Item 25, then the §5 Phase A ladders on 6VJJ and 3KYS, 300 designs/rung,
including the size-matched polar control (§5.9).

**Go/no-go:** the §5.8 table decides `MAX_EXPOSED_HYDROPHOBIC` /
`EXPOSED_HOTSPOT_CLEARANCE_A`. Run Phase B (7.7 GPU-h, two extreme rungs) only
if Phase A shows a partition shift, or if it shows none and you want to confirm
the guards are too strict before loosening them. **Whatever the result, the
guards stop being uncalibrated** — which is the point, and it is worth doing
even if glue is abandoned.

### Stage 3 — Glue plumbing up to a validated spec, no GPU · ~6 d

Items 1, 2, 3, 4, 5, 6, 11, 12, 21, plus 18 (`n_tokens`). Target: `4ZGM`, where
**no trim is needed** (128 residues, item 7 not yet required).

**Go/no-go:** the eight wall tests green; `parse_hotspot_residues` on
`projects/div_standard_diabetes/.../02_structure.md` returns 7 residues with
**correct per-chain attribution** (today it returns all 7 stamped `target_chain: R`,
of which 4 are chain P — **[measured]**, §1.3 and appendix); grounding passes;
`validate_spec` accepts the 4ZGM glue spec including the trim cross-check;
`n_tokens` reads 206, not 178. **Stop here if the wall is not green.**

### Stage 4 — Glue scoring, still no glue GPU campaign · ~3 d

Items 15, 16, 17, plus the per-side split of `hotspots_from_rfd3`.

**Go/no-go:** re-filter every shipped `refold_scores.csv` in `projects/` and
assert **byte-identical survivor counts** (317 on mesothelioma_showcase, etc.);
`glue_ipsae_ab` reproduces the §4.2 synthetic-split numbers on a real
`confidences.json`; all new thresholds `null` in `config.yaml`.

### Stage 5 — The first glue campaign · ~1 d setup + ~10–20 GPU-h

4ZGM, single site, `--stop-after calibration`, `--project`. 300-backbone trial;
`--escalate-to` if the interval is unusable.

**Go/no-go:** the calibration verdict, read against
`hotspot_engagement_min_side` (set non-null **for this run only**) and
`glue_ipsae_delta`. A SCALE_UP here is the first evidence that glue design works
at all. **Stop and re-scope if the trial produces no design engaging both sides.**

### Stage 6 — Two-chain trimming · ~6 d

Items 7, 8, 9, 10, 13, 14. Only now, and only because 88 % of real complexes
need it (§2.3). Calibrated by Stage 2's result.

**Go/no-go:** `test_a_trim_that_removes_nothing_retains_everything` green on all
three parametrised targets; every `trim_map.json` in `projects/` reproducible
byte-for-byte by re-running the trim; the `glue_interface_retention` guard
refuses `5VAI R29-128 + P7-37` at 0.30.

### Stage 7 — Site selection, reports, second engine · ~6 d

Items 19, 20, 22, 26.

---

# 9. Open questions, and the one command that settles each

| Question | Command / experiment |
|---|---|
| Does RFD3 really merge two **different** input chains? | Stage 1: one RFD3 design on a hand-built `70-86,/0,A29-128,B10-37` 4ZGM spec, inspect `num_chains` + `diffused_index_map`. **2 GPU-min.** The most decisive open question in the document. |
| Does exposure actually divert binders, and at what dose? | §5 Phase A. 5.85 GPU-h. |
| Does `SEC_PER_RFD3_DESIGN = 5.4` have a size law? | Time RFD3 per design at 168 and 286 tokens — **Phase A measures this for free**, since it spans both. |
| Would `_select_designable_structure` find 4ZGM for GLP-1R? | `.venv/bin/python -c "from src.target_resolve import find_complex_structures, resolve_target; print(find_complex_structures(resolve_target('GLP-1R').uniprot, rows=40))"` — one network call. |
| Does a merged two-chain target cost an extra `rfd3_n_chainbreaks`? | Read `rfd3_n_chainbreaks` off Stage 1's sidecar. Expected 1 for two segments **[inferred]**, matching any 2-segment trim. |
| Is the ≤ 35 Å union-diameter ceiling right for `cyclic_peptide`? | **Unmeasurable today** — no RFD3 cyclic campaign exists. Treat glue + cyclic as unsized. |
| Does the exposure *area*-as-fraction-of-epitope-BSA threshold (§2.2.2) separate good from bad cuts? | Falls out of Phase A: regress `patch_enrichment` on `exposed_Å² / target_side_BSA` instead of on raw area. No extra GPU. |
| Is `glue_ipsae_delta` a real signal or noise? | Needs Stage 5. There is no proxy; it is the one thing only a campaign can answer. |

---

# Appendix — commands run

All under `.venv/bin/python` from the repo root; scratch scripts in the session
scratchpad (`.../76d1526a-.../scratchpad/`). Nothing under `src/`, `scripts/`,
`skills/`, `tests/` or `config.yaml` was modified; no GPU work was launched; no
commit was made. Three PDB entries (3IOL, 4ZGM, 3C59) were downloaded to
`/tmp/cl_scr/`, **not** into `data/structures/`.

| script | what it measured |
|---|---|
| `seg5vai.py` | `segment_domains` on 5VAI chain R: geometric `29-212, 213-421`; ECOD `96-204, 208-421`; chain R gap at `(128, 135)` |
| `plan5vai.py` | `plan_trim` → geometric `[(29,128)]` 100 res; auto/ECOD `[(29,128),(135,254)]` 220 res |
| `trim5vai.py` | `trim_target` end-to-end: geometric passes (2 exposed away), auto refuses (33 exposed) |
| `ladder.py` | the budget ladders on 3KYS / 3DI2 / 5GN0 / 6VJJ / 7CZD (§2.1) |
| `spanscan.py` | exhaustive same-size contiguous-window scan, 6VJJ N=117 (22 windows) and 3KYS N=140 (42 windows) |
| `glue_sasa.py` | production isolated-chain vs assembly-context exposure on four 5VAI two-chain trims |
| `glue_budget.py` | 108 local complexes: 12 % fit 220 as a pair, median asymmetry 1.04, 14 % have a sub-80 chain |
| `sidechains.py` | sidechain completeness on 5VAI / 3KYS / 7CZD / 6VJJ |
| (inline) | the exact atoms present at the 7 5VAI glue hotspots vs what the spec names |
| `footprint.py` | epitope CA–CA diameter over 528 real designs from 7 campaigns |
| `gluepockets.py` | `find_glue_pockets` on 5VAI / 3KYS / 7CZD / 3N7S, with union diameters |
| `alt_entries.py` | 3IOL / 4ZGM / 3C59: resolution, chain sizes, sidechain completeness, BSA, glue pockets |
| `design_only.py` | the §5.4 readout on 60 real 3KYS designs — no refold, existing functions only |
| `power.py`, `bench_cost.py` | power / Wilson / rule-of-three; Phase A and Phase B GPU-h and GB |
| `three_way.py` | `ipsae_from_confidences` with synthesised 3-way `token_chain_ids` on a real RF3 `confidences.json` |
| (inline) | `filter_records` over 1,352 real records with a patched glue criterion: 317 → 0 at 0.5, 317 at `null` |
| (inline) | `parse_hotspot_residues` on the real 5VAI glue report: 7 residues, all stamped chain R, 4 of them chain P |
| (inline) | foundry's own `input_parsing.py:1319-1323` and `components.py:333-346` |
