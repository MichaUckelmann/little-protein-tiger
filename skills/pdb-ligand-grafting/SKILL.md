---
name: pdb-ligand-grafting
description: >
  OPTIONAL enrichment stage of the de novo ENZYME design workflow. When a
  deposited holo structure of a protein bound to the substrate (or a close
  analog) exists, extract the first-shell interacting residues and propose which
  to graft into the de novo active site so the scaffolder/design model starts
  from real, productive substrate contacts. This is NEVER a hard requirement:
  there is frequently no holo structure, in which case the active site is built
  purely from the chemistry/QM model and the diffusion model generates the
  surrounding residues. Trigger after the theozyme stage when prior_art_pdbs is
  non-empty. Reasoning-only: it proposes grafts, it does not run design.
---

# PDB-ligand grafting (optional active-site enrichment)

A holo structure of a natural/engineered protein bound to your substrate encodes
interactions evolution already found. When one exists, grafting a few of those
contacts onto the de novo theozyme can sharpen specificity and improve
buildability. This stage is **optional and best-effort** — its absence must never
block the workflow.

## Decision first: is there anything to graft?
- If the handoff's `prior_art_pdbs` is empty (or `search_pdb_by_ligand` returns
  nothing relevant), **stop and emit `grafting: skipped (no holo structure)`**.
  Carry the theozyme motif forward unchanged; the diffusion model will generate
  the surrounding residues. Do not invent contacts.
- If one or more holo structures exist, proceed.

## Method
1. For each candidate holo PDB, `tool_extract_ligand_contacts` to list the
   first-shell residues + the ligand atoms each contacts (and any catalytic
   metals, reported separately).
2. Keep only contacts that are **transferable** to the de novo design: contacts
   to the substrate moiety that is preserved in your TS, especially around the
   atoms where charge develops at the transition state (cross-check against the
   theozyme handoff's `acceptors` / `forming_bond`). Discard contacts to parts of
   the natural substrate your reaction doesn't involve.
3. For each kept contact, decide the graft mode the way the theozyme/scaffolding
   rules require: a **backbone N-H** oxyanion donor is sequence-agnostic (let
   design assign the residue); a **side-chain** donor must be grafted as that
   specific side chain in its proper geometry (never "fix a backbone N-H and
   relabel it as a side chain" — they attach differently).
4. `tool_analyze_active_site_geometry` to confirm the proposed graft geometry is
   sane (donor→acceptor ~2.8–3.0 Å, sensible angles) before recommending it.
5. Recommend the minimal productive set — graft sparingly; over-constraining the
   motif kills diffusion yield.

**Sourcing & anti-hallucination.** Every contact you cite must come from a
`tool_extract_ligand_contacts` / `tool_analyze_active_site_geometry` result on a
real structure — never assert a residue–ligand contact from memory, and never
invent a PDB accession (use only tool-returned IDs). Chemical rationale for *why*
a contact is transferable may use background knowledge; tag it `[uncited]`.

## Output
`## GRAFTING REPORT` (≤ ~900 words): per candidate structure, the transferable
contacts kept/rejected with reasons, the proposed graft residues + modes, and any
catalytic metal to carry over. Then the handoff.

### PIPELINE HANDOFF (emit verbatim; plain `- key: value` bullets, no code fence)
```
### PIPELINE HANDOFF
- grafting: used <PDBID> | skipped (no holo structure)
- graft_residues_json: <JSON list: [{"from_pdb":"5DLT","residue":"TYR214","mode":"sc","contacts":"O15","note":"..."}], or []>
- carry_metal: <metal + coordinating atoms to preserve, or none>
- motif_changes: <one line: how the theozyme constellation should be augmented, or "none">
- go_recommendation: GO | CONDITIONAL_GO | NO_GO
- go_rationale: <one line>
```
A `skipped` grafting result is a normal, frequent, fully-valid outcome — set
`go_recommendation: GO` and proceed to design.
