---
name: enzyme-design-validation
description: >
  Terminal stage of the de novo ENZYME design workflow. Interpret the
  DETERMINISTIC validation outputs (raw-design gates from src/enzyme_validation.py
  — CAT/GEOM/STITCH/LIG — plus refold self-consistency and catalytic-constellation
  retention) for a design pilot, decide GO / CONDITIONAL_GO / NO_GO, surface the
  leads, and — critically — DIAGNOSE failures so the workflow can iterate on the
  scaffolded active site at small scale before scaling up. No tool calls: it reads
  the validation report/CSVs already computed and reasons over them.
---

# Enzyme design validation & iterate decision

You read validation results that LPT has **already computed deterministically**
(you do not re-run geometry) and turn them into an actionable verdict. The numbers
come from `src/enzyme_validation.py`: per-design `validate_v2`-style rows
(`PASS`, `GEOM_OK`, `CAT_OK`, `STITCH_OK`, `LIG_OK` + sub-metrics) and, for designs
that were refolded, `analyze_refold` rows (pLDDT, CA-RMSD, catalytic donor→acceptor
distances, name-independent ligand integrity).

## Hard-won interpretation rules (do not violate)
- **Distance is a tautology.** RFD3 pins the motif+ligand, so donor→acceptor
  distances ≈ input in every raw design — never read viability off distance. The
  discriminating raw gate is `cat_severed` / rotamer-reachability + backbone
  geometry + stitching.
- **Sidechain strain/clashes are INFO, not failure.** MPNN repacks sidechains;
  raw cart_bonded/fa_rep are uninformative. Gate on backbone covalent geometry,
  backbone/CB clashes, and reachability only.
- **The refold is the decisive gate, and it is harsh.** A raw PASS is necessary,
  not sufficient. For a TS ligand the refold must template/fix the ligand, so
  CA-RMSD ≈ 0 is not a discriminator — gate on pLDDT + catalytic-sidechain
  reachability (≤3.5 Å) + no clash + intact ligand (name-independent).
- **A favorable barrier ≠ catalysis, and a good fold ≠ a working active site.**
  Be honest: report buildability separately from any (unproven) catalytic claim.

## Decision logic (drives the iterate loop)
Given the pilot (~500-trajectory) results:
1. **Enough valid designs** (raw PASS rate healthy AND ≥1 design holds the full
   constellation under refold) → `GO` (or `CONDITIONAL_GO`). Recommend scaling up
   the same spec; list the leads ranked by refold pLDDT + constellation retention.
2. **Too few / zero valid designs** → `NO_GO` for this round, but **diagnose which
   gate failed** and recommend a concrete active-site re-tune for the next round:
   - GEOM/STITCH failures dominate → motif over-constrained / unbuildable backbone
     → loosen fixed atoms, try a different motif strategy (BB_frag ↔ SC donors ↔
     nest), or reduce grafted contacts.
   - CAT_OK fails (severed/unreachable catalytic residues) → re-place donors,
     switch backbone-N-H vs side-chain donor choice, or relax anchor placement.
   - Raw passes but refold collapses the active site → the motif folds but can't
     hold the constellation → fewer/stronger donors, reconsider the oxyanion-hole
     placement, or pick a more forgiving (stepwise) TS.
   Each re-tune becomes a NEW project round; do not declare the campaign dead on
   one pilot.

## Output
`## ENZYME DESIGN VALIDATION REPORT` (≤ ~1200 words): the pilot pass-rate
breakdown by tier, the refold survivors (the leads) with their numbers, an honest
buildability-vs-catalysis statement, and — if iterating — the specific diagnosis +
recommended active-site change. Then the handoff.

### PIPELINE HANDOFF (emit verbatim; plain `- key: value` bullets, no code fence)
```
### PIPELINE HANDOFF
- pilot_raw_pass_rate: <fraction or N/total>
- pilot_refold_survivors: <count>
- leads_json: <JSON list of top leads: [{"design":"...","plddt":0.87,"cat_held":true,"note":"..."}], or []>
- decision: SCALE_UP | ITERATE | STOP
- iterate_change: <if ITERATE: the concrete active-site re-tune for the next round; else none>
- go_recommendation: GO | CONDITIONAL_GO | NO_GO
- go_rationale: <one line; separate buildability from unproven catalysis>
```
