---
name: binder-optimizer
description: >
  Analyses a predicted binder-target complex (local CIF/PDB from AF3/RFDiffusion/Boltz),
  maps the binding interface atomically, proposes 4 single-point mutations on the binder
  to improve affinity, validates steric clashes, and outputs 4 AF3 submission JSONs —
  one per mutation for clean, interpretable round-1 validation. Optionally
  cross-validates against molecular-biology-expert or ppi-analysis reports.
  Requires structure-tools MCP server.
  Trigger on: "optimize binder", "improve binding affinity", "suggest mutations",
  "mutate binder", "affinity maturation", "binder optimization", or any request to
  improve a designed binder given a local structure file. Do NOT invoke for target
  selection (use pathway-expert) or first-pass interface analysis without an improvement
  task (use complex-structure-analysis).
---

# Binder Optimizer

Proposes 4 independent single-point binder mutations and emits one AF3 JSON per
mutation. Combined multi-mutation submissions belong in round 2, after singles are
ranked — combining before knowing which ones work produces uninterpretable results.

Output: `## BINDER OPTIMIZATION REPORT` + 4-JSON AF3 array for batch upload.

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

One tool call returns everything needed for Phases 2 and 3:

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

---

## Phase 3: Mutation Candidate Reasoning

No tool calls. Reason over the proximity map and generate 8–12 candidates.

**Rule 1 — Charge complement** (highest priority): uncharged/weakly polar binder near
charged target → mutate to opposite charge (target ARG/LYS → D/E on binder;
target ASP/GLU → R/K on binder, prefer R for longer reach).

**Rule 2 — H-bond formation**: binder residue has `interaction: h_bond_candidate` but
no `h_bond` entry in the H-bond list → mutate to S, T, N, Q, Y, or H.

**Rule 3 — Hydrophobic gap fill**: `gap_flag: true` and target contacts are hydrophobic →
bulkier hydrophobic (A→L/I/F, V→L/I). Avoid W directly — large sidechain, check clash.

**Rule 4 — Desolvation**: `interaction: vdw_contact` or `h_bond_candidate`, binder
residue is polar, target contacts are hydrophobic, no H-bond present → mutate to
non-polar (S/T→A/V; N/Q→L/I).

**Rule 5 — Repulsion removal**: `interaction: electrostatic_repulsive` → mutate binder
to neutral or opposite charge.

**Hard constraints (apply before generating any candidate):**
- Never PRO (destroys backbone H-bond donor, disrupts backbone conformation)
- Never mutate GLY in turns/loops (Gly φ/ψ are inaccessible to any other residue)
- Binder chain only — never mutate the target
- Exactly 4 single-point mutations in final output; no combined submissions in round 1
- No CYS unless disulfide is clearly supported (oxidising environment, proximal Cys)

For each candidate record: `resnum` (auth_seq_id), WT one-letter, proposed one-letter,
shorthand `<WT><resnum><New>` (e.g. `A265E`), one-sentence rationale, rule number.

---

## Phase 4: Steric Clash Validation

For the top 6–8 candidates call one tool per candidate:

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
minor cases. Prune to the best 4–6 passing candidates for Phase 5.

---

## Phase 5: Context Integration

**Skip if no context reports provided.**

**From molecular-biology-expert** (`INTERFACE INSIGHTS FROM LITERATURE`):
- Upgrade if the contacted target residue is a confirmed hotspot (alanine scan > 5× loss)
- Downgrade if the target residue is "tolerant of mutation" (< 2× effect)

**From ppi-analysis report** (`MODEL-READY HOTSPOTS` or `HOTSPOT REGIONS`):
- Upgrade if the contacted target residue appears in the hotspot table
- This provides structural corroboration that the contact is energetically real

---

## Phase 6: Final Selection and AF3 JSON Construction

### Select top 4

Rank by: rule priority (1 > 2 > 3 > 4 > 5) → clash result (none > minor) → context
support (hotspot confirmed > structural > none) → pLDDT at position (≥ 70 preferred).

Do not recommend two mutations at the same position. Prefer candidates targeting
**different interface regions** — spatial independence makes round-2 combining cleaner.

### Get sequences

```
mcp__structure-tools__tool_get_sequence_map
  file_path = "<path>"
  chain     = "<binder_chain>"

mcp__structure-tools__tool_get_sequence_map
  file_path = "<path>"
  chain     = "<target_chain>"
```

Each returns `sequence` (1-letter uppercase string) and `auth_to_string_idx`
mapping. Use `auth_to_string_idx[resnum]` to find the exact 0-based position in the
sequence string — do not use the residue number directly.

Apply each mutation independently to the binder sequence. Keep target as WT.

### Construct AF3 JSON array

4 objects, one per mutation. All use:
- `useStructureTemplate: false` (both chains — must not bias toward WT geometry)
- `modelSeeds: ["1", "2", "3"]`
- `dialect: "alphafoldserver"`, `version: 1`
- Binder (mutated) first, target (WT) second
- Naming: `<complex_descriptor>_<MutCode>` (e.g. `binder_A265E`)

```json
[
  {
    "name": "<complex>_A265E",
    "modelSeeds": ["1", "2", "3"],
    "sequences": [
      { "proteinChain": { "sequence": "<binder_with_A265E>", "count": 1, "useStructureTemplate": false } },
      { "proteinChain": { "sequence": "<target_WT>",         "count": 1, "useStructureTemplate": false } }
    ],
    "dialect": "alphafoldserver",
    "version": 1
  },
  { "name": "<complex>_V271L", ... },
  { "name": "<complex>_T290N", ... },
  { "name": "<complex>_K312R", ... }
]
```

**Round 2**: rank the 4 predictions by interface quality (interface pLDDT, predicted
contact geometry, or experimental SPR/ITC). Combine the top 2 into a double mutant —
re-invoke binder-optimizer on the best single-mutant AF3 output, or build the JSON
manually from the sequences already extracted above.

---

## Phase 7: Output Report

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

### PROXIMITY MAP
| Binder Residue | Target Contacts (4.5 Å) | Interaction | Gap Flag |
|---|---|---|---|
| <AA><num> | <AA><num> (<dist>Å, <interaction>), ... | <dominant class> | Yes/No |

### MUTATION CANDIDATES (pre-validation)
| Rank | Mutation | Rule | Rationale |
|---|---|---|---|
| 1 | <WT><num><New> | R1 | <one sentence referencing specific target contact and distance> |

### CLASH VALIDATION
| Mutation | Clash | Hard | Soft | Sidechain reach (Å) | Status |
|---|---|---|---|---|---|
| <WT><num><New> | none/minor/major | <N> | <N> | <val> | Pass/Discard |

### CONTEXT INTEGRATION
(Omit if no context reports provided)
- Upgrades: <mutation — reason>
- Downgrades / eliminations: <mutation — reason>

### RECOMMENDED MUTATIONS
| # | Mutation | Rationale | Clash | pLDDT | Context |
|---|---|---|---|---|---|
| 1 | ... | ... | none | <val|N/A> | <hotspot/structural/none> |
| 2–4 | ... |

Caveats: <pLDDT warnings, geometry notes, minor clash notes>
Round 2: rank these 4 AF3 predictions → combine top 2 into a double mutant.

### AF3 JSON OUTPUT
4 single-mutation JSONs — paste array into AF3 server batch upload.
Seeds: [1, 2, 3] | useStructureTemplate: false (both chains)

[full JSON array]
```

---

## Handoff Contract

- **Standalone**: user provides file path + chain IDs + task
- **From orchestrator**: write report to `<run_folder>/05_binder_optimization.md`
- **To wet lab**: `RECOMMENDED MUTATIONS` table + AF3 JSON array
- **To round 2**: re-invoke on best single-mutant AF3 output to build double mutant

---

## Common Pitfalls

- **Chain identity in RFDiffusion outputs**: the designed chain is commonly chain A,
  target chain B — the reverse of convention. Always confirm before Phase 2.

- **auth_to_string_idx, not resnum**: `tool_get_sequence_map` returns the exact string
  index map. Use `auth_to_string_idx[resnum]` — do not use the residue number directly
  as a string index; chains may start above 1 or contain numbering gaps.

- **useStructureTemplate: true suppresses mutant geometry**: AF3 uses the WT structure
  as a template and may correct the mutation back toward WT. Always use `false`.

- **No combined JSONs in round 1**: if a combined mutant underperforms you cannot
  identify the culprit without the singles. Singles first, always.

- **pLDDT < 70 is a caveat, not a veto**: uncertain geometry ≠ wrong position.
  The mutation may stabilise the interface. Flag it and let AF3 decide.

- **Pro/Gly constraints are absolute**: filter these in Phase 3 before reasoning —
  do not pass them to Phase 4 clash checking.

- **Clash tool is a heuristic**: `tool_check_mutation_clash` uses Cβ reach radius,
  not a rotamer library. Minor results are pre-filters; AF3 resolves the final geometry.
