---
name: binder-optimizer
description: >
  Invoke ONLY when the user explicitly asks for it by name or clearly
  requests this specific workflow; do not trigger it from a general
  question, which you can answer better from your own knowledge than from
  this narrow corpus.
  Analyses a predicted binder-target complex (local CIF/PDB from AF3/RFDiffusion/Boltz),
  maps the binding interface atomically, proposes 4 single-point mutations on the binder
  to improve affinity, validates steric clashes, and outputs 4 mutated sequences for
  experimental testing. Optionally cross-validates against molecular-biology-expert or
  ppi-analysis reports. Requires structure-tools MCP server.
  Trigger on: "optimize binder", "improve binding affinity", "suggest mutations",
  "mutate binder", "affinity maturation", "binder optimization", or any request to
  improve a designed binder given a local structure file. Do NOT invoke for target
  selection (use pathway-expert) or first-pass interface analysis without an improvement
  task (use complex-structure-analysis).
---

# Binder Optimizer

Proposes 4 independent single-point binder mutations for experimental validation.
Binders will be tested experimentally — no AlphaFold JSON submission needed.
Combined multi-mutation candidates belong in round 2, after singles are ranked
experimentally — combining before knowing which ones work produces uninterpretable results.

Output: `## BINDER OPTIMIZATION REPORT` + machine-readable `### MUTATION OUTPUT` JSON block.

---

## Phase 1: Parse Input

From the user message identify:
- `file_path` — local CIF or PDB of the predicted complex
- `binder_chain` / `target_chain` — chain IDs (**confirm with user if not given** — for
  RFDiffusion outputs the designed chain is commonly A, target B, reversing convention)
- `structure_source` — AF3 / Boltz / RFDiffusion / other (determines pLDDT validity)
- `context_reports` — optional mol-bio-expert or ppi-analysis report

---

## Phase 2: Interface Analysis

### 2a — Interface map

One tool call returns the full contact map:

```
mcp__structure-tools__tool_analyze_interface
  file_path = "<path>"
  chain_a   = "<binder_chain>"
  chain_b   = "<target_chain>"
  cutoff    = 4.5
```

From the result, extract:
- `interface.bsa_total_A2`, `interface.n_hbonds`, `interface.hbonds[]`
- `chain_a_interface_residues[]` — each entry has: `residue`, `resnum`, `one_letter`,
  `type`, `hydrophobicity`, `contacts[]`, `n_contacts`, `gap_flag`
- Each contact in `contacts[]` has: `target_res`, `target_resnum`, `min_dist_A`, `interaction`
- `plddt_at_interface` — `{resnum: plddt_score}`; valid for AF3/Boltz only
- `low_confidence_residues` — pre-flagged list (pLDDT < 70)

For pLDDT: if `structure_source` is RFDiffusion or other non-AF output, B-factors are
placeholders — note "pLDDT not applicable" in the report and ignore confidence flags.

The `contacts[]` field on each binder residue is the proximity map. Build the
PROXIMITY MAP table directly from this data — no additional tool calls needed.

If a specific binder residue needs deeper inspection (e.g. to confirm an H-bond
candidate before reasoning), call:
```
mcp__structure-tools__tool_get_residue_contacts
  file_path     = "<path>"
  chain         = "<binder_chain>"
  resnum        = <auth_seq_id>
  partner_chain = "<target_chain>"
  cutoff        = 4.5
```

### 2b — Target surface patch characterisation

Collect the `target_resnum` values from all contacts in `chain_a_interface_residues[]`
(de-duplicate). Call:

```
mcp__structure-tools__tool_score_surface_patch
  file_path    = "<path>"
  chain        = "<target_chain>"
  residue_list = [<de-duplicated target interface resnums>]
```

Returns: `mean_hydrophobicity`, `hydrophobic_fraction`, `spatial_spread_A`,
`residue_type_breakdown`, `suitability_rating` (Excellent/Good/Marginal/Poor), `rationale`.

Extract and record:
- **Patch hydrophobic fraction** — drives the hole-filling strategy in Phase 3
- **Suitability rating** — a Marginal/Poor patch means stacking more hydrophobics on
  the binder is unlikely to help; electrostatic optimisation becomes the priority
- **Spatial spread (Å)** — large spread = diffuse interface; small spread = compact pocket

### 2c — Shape complementarity proxy

True Sc (Lawrence-Colman) requires molecular-dot-surface computation not available here.
Use the following proxies from Phase 2a data:

| Proxy | How to compute | Interpretation |
|---|---|---|
| **Void count** | Count residues where `gap_flag: true` | Each void is a potential fill site |
| **Mean contact distance** | Average `min_dist_A` across all contacts | < 3.8 Å = tight packing; > 4.2 Å = loose |
| **Close-contact fraction** | Fraction of contacts with `min_dist_A` ≤ 3.8 Å | < 0.4 = poor complementarity |
| **Exposed polars** | Polar binder residue with vdw-only contacts and no H-bond entry | Desolvation penalty without reward |

Report a qualitative Sc proxy rating:
- **Good** — void count ≤ 2, mean dist < 3.9 Å, close-contact fraction ≥ 0.5
- **Moderate** — 3–5 voids or mean dist 3.9–4.1 Å
- **Poor** — > 5 voids or mean dist > 4.1 Å or close-contact fraction < 0.3

---

## Phase 2.5: Alanine Scan Classification

No tool calls. Derive from Phase 2a data. For every residue in
`chain_a_interface_residues[]`, classify it into one of three tiers:

| Tier | Criteria | Implication |
|---|---|---|
| **Hotspot** | n_contacts ≥ 3, OR appears in `hbonds[]` as donor/acceptor, OR `interaction: electrostatic` or `aromatic` | Alanine substitution predicted to significantly impair binding — do NOT mutate |
| **Neutral** | n_contacts 1–2, `interaction: vdw_contact` only, no H-bond, not a gap | Alanine substitution predicted neutral — may be optimisable |
| **Target** | `gap_flag: true`, OR polar residue with vdw-only contacts and no H-bond partner | Alanine scan would be neutral-to-slightly-negative — prime for optimisation |

**How to apply in Phase 3**: Hotspot residues are excluded from mutation proposals.
Neutral and Target residues feed the rule engine. Targets are prioritised.

Output the ALANINE SCAN TABLE (see Phase 7) directly from this classification — do not
fabricate ΔΔG numbers; use "Hotspot", "Neutral", or "Target" as the predicted effect.

---

## Phase 3: Mutation Candidate Reasoning

No tool calls. Reason over the proximity map and Phase 2.5 classification. Generate
8–12 candidates. **Only propose mutations for Neutral or Target residues.**

**Pre-filter (before any rule):** Skip PRO, GLY in turns/loops — already excluded by
hard constraints. Skip any Hotspot residue from Phase 2.5.

**Rule 1 — Charge complement** (highest priority): uncharged/weakly polar binder near
charged target → mutate to opposite charge (target ARG/LYS → D/E on binder;
target ASP/GLU → R/K on binder, prefer R for longer reach).

**Rule 2 — H-bond formation**: binder residue has `interaction: h_bond_candidate` but
no `h_bond` entry in the H-bond list → mutate to S, T, N, Q, Y, or H.

**Rule 3a — Hydrophobic gap fill (void)**: `gap_flag: true` and target contacts are
hydrophobic → bulkier hydrophobic (A→L/I/F, V→L/I). Avoid W directly — large
sidechain, check clash first.

**Rule 3b — Pocket fill (no gap, tight contact)**: binder residue is small (A/V/G),
target contacts are hydrophobic, `min_dist_A` ≤ 3.8 Å for all contacts, no gap_flag.
Indicates a tight hydrophobic pocket that a slightly larger residue could pack into
more fully. Prefer A→V, V→L, A→I — one methylene step at a time. Patch hydrophobic
fraction from Phase 2b ≥ 0.4 is a supporting condition; skip if < 0.3.

**Rule 4 — Desolvation**: `interaction: vdw_contact` or `h_bond_candidate`, binder
residue is polar, target contacts are hydrophobic, no H-bond present → mutate to
non-polar (S/T→A/V; N/Q→L/I).

**Rule 5 — Repulsion removal**: `interaction: electrostatic_repulsive` → mutate binder
to neutral or opposite charge.

**Rule 6 — Second-shell support**: Look for binder residues that are NOT in the
interface (absent from `chain_a_interface_residues[]`) but neighbour an interface
residue whose sidechain is floppy or solvent-exposed. If the interface residue is a
Hotspot, locking it in the correct rotamer via a second-shell contact can contribute
significantly to binding. Flag candidates where a second-shell residue could form a
new intra-binder H-bond or hydrophobic contact to pre-organise the hotspot sidechain.
These are lower-confidence (no direct structural data) — mark as Rule 6 and note
"second-shell, requires AF3 confirmation."

**Hard constraints (apply before generating any candidate):**
- Never PRO (destroys backbone H-bond donor, disrupts backbone conformation)
- Never mutate GLY in turns/loops (Gly φ/ψ are inaccessible to any other residue)
- Binder chain only — never mutate the target
- Exactly 4 single-point mutations in final output; no combined submissions in round 1
- No CYS unless disulfide is clearly supported (oxidising environment, proximal Cys)

For each candidate record: `resnum` (auth_seq_id), WT one-letter, proposed one-letter,
shorthand `<WT><resnum><New>` (e.g. `A265E`), one-sentence rationale, rule number.

---

## Phase 4: Red Flag Filter

No tool calls. Apply before clash validation — discard or downgrade candidates that
fail. Record the flag in the MUTATION CANDIDATES table.

**Flag A — Buried polar (desolvation penalty)**: The proposed new residue is polar
(S, T, N, Q, H) AND none of the target contacts in `contacts[]` are capable of
forming an H-bond (all interactions are `vdw_contact` or `hydrophobic`). Without a
partner, burying a polar residue costs ~1–2 kcal/mol. **Discard unless Rule 2 intent.**

**Flag B — Aggregation patch**: The proposed mutation introduces a large hydrophobic
(W, F, L, I) AND the binder residue is on a solvent-exposed face (few contacts, low
n_contacts). A new exposed hydrophobic can drive self-aggregation. **Downgrade; note
in report.**

**Flag C — Steric over-reach (Rule 3b heuristic fail)**: Pocket-fill candidate (Rule 3b)
where all contacts already have `min_dist_A` ≤ 3.5 Å — space is already occupied.
Bulking up here guarantees a hard clash. **Discard; do not send to Phase 5.**

**Flag D — Removes a hotspot-proximal backbone donor**: The WT residue is the only
non-Gly residue providing a backbone NH toward the target (visible in `hbonds[]` as
backbone H-bond). Even a conservative substitution changes the sidechain environment.
**Retain but flag; note AF3 repack required.**

Candidates with Flag A or C are removed from the pool before Phase 5. Flags B and D
are retained with a note. If the pool drops below 6 after flagging, go back to Phase 3
and generate additional candidates from lower-priority rules.

---

## Phase 5: Steric Clash Validation

For the top 6–8 candidates (after red-flag filter) call one tool per candidate:

```
mcp__structure-tools__tool_check_mutation_clash
  file_path     = "<path>"
  chain         = "<binder_chain>"
  resnum        = <auth_seq_id>
  new_aa        = "<1-letter or 3-letter code>"
  partner_chain = "<target_chain>"
```

Returns: `clash` (none / minor / major), `hard_clashes`, `soft_clashes`,
`sidechain_reach_proposed_A`.

| Result | Action |
|---|---|
| `none` | Pass — retain |
| `minor` | Retain with note: "minor clash, may resolve by AF3 repacking" |
| `major` | Discard |

The tool uses a Cβ-heuristic (sidechain reach radius, not a rotamer library).
It is a pre-filter, not a definitive verdict — AF3 re-prediction resolves ambiguous
minor cases. Prune to the best 4–6 passing candidates for Phase 6.

---

## Phase 6: Context Integration

**Skip if no context reports provided.**

**From molecular-biology-expert** (`INTERFACE INSIGHTS FROM LITERATURE`):
- Upgrade if the contacted target residue is a confirmed hotspot (alanine scan > 5× loss)
- Downgrade if the target residue is "tolerant of mutation" (< 2× effect)

**From ppi-analysis report** (`MODEL-READY HOTSPOTS` or `HOTSPOT REGIONS`):
- Upgrade if the contacted target residue appears in the hotspot table
- This provides structural corroboration that the contact is energetically real

---

## Phase 7: Final Selection and Sequence Construction

### Select top 4

Rank by: rule priority (1 > 2 > 3a/3b > 4 > 5 > 6) → red-flag status (no flag > Flag B/D
> discarded) → clash result (none > minor) → context support (hotspot confirmed >
structural > none) → pLDDT at position (≥ 70 preferred).

Do not recommend two mutations at the same position. Prefer candidates targeting
**different interface regions** — spatial independence makes round-2 combining cleaner.

### Get binder sequence

```
mcp__structure-tools__tool_get_sequence_map
  file_path = "<path>"
  chain     = "<binder_chain>"
```

Returns `sequence` (1-letter uppercase string) and `auth_to_string_idx` mapping.
Use `auth_to_string_idx[resnum]` to find the exact 0-based position in the sequence
string — do not use the residue number directly.

Apply each mutation independently to the binder sequence to produce 4 mutated sequences.

### Emit MUTATION OUTPUT block

After selecting the 4 mutations, output a machine-readable JSON block exactly as shown:

```
### MUTATION OUTPUT
```json
[
  {"mutation": "A265E", "mutated_sequence": "<full binder sequence with A265E applied>"},
  {"mutation": "V271L", "mutated_sequence": "<full binder sequence with V271L applied>"},
  {"mutation": "T290N", "mutated_sequence": "<full binder sequence with T290N applied>"},
  {"mutation": "K312R", "mutated_sequence": "<full binder sequence with K312R applied>"}
]
```
```

Each `mutated_sequence` must be the complete binder sequence (same length as WT except
for the single substitution). The block must appear verbatim so it can be parsed
programmatically.

**Round 2**: after experimental results are in, re-invoke binder-optimizer on the
uploaded CIF of the best-performing mutant to propose a double mutant.

---

## Phase 8: Output Report

Populate all sections from tool outputs. Do not fabricate distances, BSA values, or
interaction types. Write "Not determined" where tool data is absent.

```
## BINDER OPTIMIZATION REPORT

### INPUT SUMMARY
- Structure: <path>
- Binder chain: <id> (<N> res) — <confirmed by user | inferred>
- Target chain: <id> (<name if known>, <N> res)
- Task: <user task>
- Structure source: <AF3/RFDiffusion/Boltz/other>
- pLDDT: <applicable | not applicable — reason>
- Context reports: <Yes: [mol-bio | ppi-analysis] | No>

### INTERFACE OVERVIEW
- BSA: <bsa_total_A2> Å²
- H-bonds: <n_hbonds>
- Interface residues: <n_contacts_chain_a> binder, <n_contacts_chain_b> target
- Binder residues by type: Hydrophobic: [...] Aromatic: [...] Charged: [...] Polar: [...]
- Low-confidence binder residues (pLDDT < 70): <list | none | not applicable>
- Target patch: suitability <rating>, hydrophobic fraction <val>, spatial spread <val> Å
- Shape complementarity (proxy): <Good/Moderate/Poor> — voids: <N>, mean contact dist: <val> Å,
  close-contact fraction: <val>

### ALANINE SCAN TABLE
| Binder Residue | n_contacts | H-bond? | Dominant interaction | Tier | Implication |
|---|---|---|---|---|---|
| <AA><num> | <N> | Yes/No | <type> | Hotspot/Neutral/Target | Leave alone / Monitor / Optimise |

### PROXIMITY MAP
| Binder Residue | Target Contacts (4.5 Å) | Interaction | Gap Flag |
|---|---|---|---|
| <AA><num> | <AA><num> (<dist>Å, <interaction>), ... | <dominant class> | Yes/No |

### MUTATION CANDIDATES (pre-validation)
| Rank | Mutation | Rule | Rationale | Red Flag |
|---|---|---|---|---|
| 1 | <WT><num><New> | R1 | <one sentence referencing specific target contact and distance> | None/A/B/C/D |

### CLASH VALIDATION
| Mutation | Clash | Hard | Soft | Sidechain reach (Å) | Status |
|---|---|---|---|---|---|
| <WT><num><New> | none/minor/major | <N> | <N> | <val> | Pass/Discard |

### CONTEXT INTEGRATION
(Omit if no context reports provided)
- Upgrades: <mutation — reason>
- Downgrades / eliminations: <mutation — reason>

### RECOMMENDED MUTATIONS
| # | Mutation | Rationale | Clash | Red Flag | pLDDT | Context |
|---|---|---|---|---|---|---|
| 1 | ... | ... | none | None | <val|N/A> | <hotspot/structural/none> |
| 2–4 | ... |

Caveats: <pLDDT warnings, geometry notes, minor clash notes>
Round 2: after experimental affinity results, upload the best mutant CIF and re-invoke.

### MUTATION OUTPUT
```json
[full MUTATION OUTPUT JSON array — see Phase 6]
```
```

---

## Handoff Contract

- **Standalone**: user provides file path + chain IDs + task
- **From orchestrator**: write report to `<run_folder>/05_binder_optimization.md`
- **To wet lab**: `RECOMMENDED MUTATIONS` table + `MUTATION OUTPUT` sequences
- **To round 2**: re-invoke on uploaded CIF of best experimentally-validated mutant

---

## Common Pitfalls

- **Chain identity in RFDiffusion outputs**: the designed chain is commonly chain A,
  target chain B — the reverse of convention. Always confirm before Phase 2.

- **auth_to_string_idx, not resnum**: `tool_get_sequence_map` returns the exact string
  index map. Use `auth_to_string_idx[resnum]` — do not use the residue number directly
  as a string index; chains may start above 1 or contain numbering gaps.

- **useStructureTemplate suppresses mutant geometry**: if you ever submit to AF3 in
  round 2, always use `useStructureTemplate: false` — the WT template would correct
  the mutation back toward WT geometry.

- **No combined mutants in round 1**: if a combined mutant underperforms you cannot
  identify the culprit without the singles. Singles first, always.

- **pLDDT < 70 is a caveat, not a veto**: uncertain geometry ≠ wrong position.
  The mutation may stabilise the interface. Flag it and let AF3 decide.

- **Pro/Gly constraints are absolute**: filter these in Phase 3 before reasoning —
  do not pass them to Phase 5 clash checking.

- **Clash tool is a heuristic**: `tool_check_mutation_clash` uses Cβ reach radius,
  not a rotamer library. Minor results are pre-filters; AF3 resolves the final geometry.

- **Alanine scan is contact-based, not energetic**: the Phase 2.5 classification
  uses structural proxies (n_contacts, H-bonds, interaction type). Do not state
  ΔΔG numbers — say "Hotspot", "Neutral", or "Target" only.

- **tool_score_surface_patch scores the target patch, not the binder**: use it to
  characterise what the binder is docking onto, not the binder itself. A Marginal/Poor
  target patch means the binding site is inherently difficult — temper expectations
  accordingly.

- **Sc proxy is not Lawrence-Colman Sc**: gap_flag + contact distance is a heuristic.
  Report it as "proxy" — do not equate to published Sc values.
