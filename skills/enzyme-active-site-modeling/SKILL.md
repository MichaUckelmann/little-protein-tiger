---
name: enzyme-active-site-modeling
description: >
  Use when designing or modeling a de novo enzyme active site (a "theozyme" /
  QM transition-state cluster) for a small-molecule reaction, and when handing
  that motif off to a backbone-generation/scaffolding model (e.g. RFdiffusion /
  RFD3) plus sequence design (e.g. LigandMPNN). Covers: choosing the catalytic
  step to target; deciding concerted vs stepwise from the TS; grafting truncated
  catalytic groups (H-bond donors, oxyanion holes, cations, metals) onto a QM
  transition state with frozen backbone-surrogate anchors; running constrained
  relax -> OptTS -> Freq in ORCA; diagnosing imaginary frequencies; estimating
  and comparing catalyzed vs uncatalyzed barriers; and exporting a trimmed motif
  (substrate/TS + donor residues) for scaffolding. Applies to cofactor-free and
  metal sites for C-C bond formation, conjugate additions, cycloadditions, etc.
---

# Enzyme active-site modeling (theozyme construction + scaffolding handoff)

## What this skill is and is NOT

This skill is a **methodology and troubleshooting playbook** for building QM
models of catalytic active sites and preparing them for protein scaffolding. It
encodes a construction protocol, a catalog of failure modes that are easy to hit
and hard to diagnose, and parameterized geometry helpers.

Be honest with the user about scope. Following this skill end-to-end yields:
- a transition state with a single, verified reaction-coordinate imaginary mode;
- a relaxed catalytic pocket with intact, geometrically sensible contacts;
- an **electronic** barrier estimate and a pocket-vs-bare comparison;
- a trimmed motif file for scaffolding.

It does **NOT** by itself demonstrate catalysis. A favorable cluster barrier is
preliminary evidence, not a rate. Real validation needs matched references and/or
free energies (QM/MM, thermodynamic integration) and ultimately experiment. A
beautiful scaffold around a motif whose barrier was never confirmed is still not
a catalyst. Say so.

Two standing cautions:
- **You cannot install this skill yourself.** You produce/refine the document;
  the user installs it.
- **External-tool syntax drifts.** ORCA keywords, RFD3/RFdiffusion and LigandMPNN
  input formats are version-specific. Treat every template here as a starting
  point and tell the user to verify against the current docs for their build.

Atom indices and geometries that appear in examples are illustrative only. They
are NOT part of the skill — every project re-derives its own.

## Workflow overview

0. **Target & step.** Pick the reaction. Check whether it is already solved
   (engineered/known enzymes). Identify the **rate- and stereo-determining step**
   and design for THAT step, not the overall transformation.
1. **Mechanism first.** Get a clean QM TS for the bare (uncatalyzed) reaction and
   read its imaginary mode. Concerted vs stepwise changes the entire design. Do
   not design a pocket for a mechanism the reaction does not follow.
2. **Theozyme cluster.** Graft truncated catalytic groups onto the TS. Anchor
   each with a frozen backbone-surrogate atom. Relax (forming bond + anchors
   frozen) -> OptTS (anchors frozen) -> Freq.
3. **Diagnose.** Verify a single reaction-coordinate imaginary; measure every
   contact; check for proton transfer / dissociation / over-coordination.
4. **Barrier.** Estimate ΔE‡ for pocket and bare with a consistent reactant
   reference; compare; apply the solvation caveat; build a matched reference if
   rigor is needed.
5. **Scaffold handoff.** Export substrate/TS + donor residues as a trimmed motif;
   decide backbone-N-H vs side-chain donors; fix the whole catalytic
   constellation; generate many and filter hard.

Helper code lives in `scripts/`; ORCA input patterns in `reference/orca_templates.md`;
the full failure-mode catalog in `reference/failure_modes.md`. The most important
items are summarized below — read the references before acting.

## Stage 1 — Mechanism first (do not skip)

The single most expensive mistake is designing a pocket for the wrong mechanism.
Before any pocket work:
- Build the bare-reaction TS at a consistent level (see templates) and run a
  frequency. **Animate the imaginary mode** (e.g. `orca_pltvib run.hess 6`) and
  confirm which bonds actually move.
- If two bonds were "supposed" to form but only one moves at the saddle, the
  reaction is **stepwise**, not concerted. Redirect the whole design to the real
  rate-determining step (often a single-bond-forming TS).
- Concerted pericyclic TSs demand rigid multi-center geometric locking (often a
  metal). Stepwise additions are far more forgiving and are usually the better
  cofactor-free target.

## Stage 2 — Theozyme cluster construction

Truncate each catalytic residue to a minimal model and **graft it onto the
converged TS geometry**, not onto a regenerated guess. Standard truncations:
Lys -> methylammonium `C[NH3+]`; backbone amide / Gln -> N-methylacetamide
`CC(=O)NC`; Asn side chain -> acetamide `CC(=O)N`; Arg -> methylguanidinium
`CNC(=[NH2+])N`; His -> 4-methylimidazole `Cc1c[nH]cn1`; Asp/Glu -> acetate
`CC(=O)[O-]`; oxyanion-hole donors -> backbone amide N-H (sequence-agnostic).

**Anchoring is mandatory.** A truncated side chain has no backbone, so unless you
freeze a far "anchor" atom (the carbon standing in for the backbone tether), the
fragment relaxes to ITS preferred geometry, not the one the protein would
enforce. Freeze the anchor as a Cartesian constraint in every stage. Choose the
anchor far enough from the donor to leave H-bond orientation free (a few tenths
of an Angstrom of play) but present enough to stop translation/pivot. For rigid
rings or metals, anchor closer (or restrain two atoms / the metal-ligand
distances) — a single far anchor leaves too much pivot.

**Placement geometry** (helpers in `scripts/theozyme_tools.py`):
- Point a donor N-H at a carbonyl O along its **sp2 lone-pair directions**
  (±~60° from C=O in the carbonyl plane), not just "outward". This is correct
  oxyanion-hole geometry and lands donors in open space.
- After fixing the donor atom, **spin the fragment about the donor-acceptor axis**
  to swing its bulk into open space (keeps the H-bond, removes clashes).
- **Always measure the guess geometry before submitting.** A frozen anchor
  faithfully preserves placement mistakes; one inverted vector once put an
  anchored methyl 1.5 A from the atom it was supposed to donate to.

**Run protocol** (templates in `reference/orca_templates.md`):
1. Constrained relax: freeze forming bond(s) + all anchors. Settles the H-bond
   network without losing the TS arrangement.
2. OptTS: freeze anchors only (release forming bonds). `Calc_Hess true`,
   `Recalc_Hess 5`.
3. Freq: anchors frozen. Closed-shell may use analytic `Freq`; **metals need
   `NumFreq`** (and `UKS`, correct multiplicity).

## Stage 3 — Diagnosis checklist (run every time)

- **One imaginary, and it is the reaction coordinate.** Animate it. A second,
  small imaginary dominated by all-hydrogen motion (methyl/ester rotor) or by the
  truncated caps is a **soft artifact**, not a second pathway — its
  reaction-coordinate component is ~0. It does not affect the TS energy. Clean
  cosmetically (displace along it + finer grid / stagger methyls) only if needed;
  it vanishes with real backbone.
- **Measure every contact.** Real H-bond donor->acceptor ~2.8-3.0 A; real
  M(II)-N/O dative ~1.9-2.2 A. A "bond" drawn by a viewer is just a distance
  cutoff — confirm with numbers.
- **Proton transfer.** A cationic donor next to a basic site (e.g. ammonium next
  to an imine/azaallyl N) will hand over a proton and destroy the species. Check
  for new H on the base; switch to a neutral donor if it happens.
- **Dissociation / wrong-atom coordination** (metals especially). If ligands
  drift out (>2.6 A) or coordinate through the wrong atom, the site collapsed;
  anchor closer or restrain M-L distances, or shelve the metal route.

## Stage 4 — Barrier estimate and comparison

ΔE‡ = E(TS) − E(reactant), computed the SAME way for pocket and bare so the
difference is meaningful. Get the reactant via a **relaxed scan** of the forming
bond out to the pre-reaction complex (robust; avoids "do the fragments drift
apart"). Take E(TS) from the OptTS `FINAL SINGLE POINT ENERGY`; take E(reactant)
from the lowest scan point. ×627.5 for kcal/mol.

- **The chemistry is in the decimals.** Totals of −900 to −1650 Hartree make a
  0.01 Ha (6 kcal/mol) barrier look like noise. Compare the TS to the *lowest*
  scan point, not to the near-TS point.
- **Solvation bookkeeping caveat.** A bare anion fully solvated by implicit
  solvent vs a pocket that swaps bulk solvation for discrete H-bonds is not a
  clean comparison — part of any difference is accounting, not catalysis. The
  rigorous fix is a **matched reference**: the same donors present but translated
  away from the reacting atoms, keeping donor count and reactant reference
  consistent. Offer to build it.
- This is electronic ΔE‡, not ΔG‡ (no entropy, no bimolecular association). State
  that. A pocket that also produces a bound pre-reaction complex (a real minimum
  where the bare reactants just separate) is showing substrate pre-organization —
  report it as a separate, genuine result.

## Stage 5 — Scaffolding handoff (RFD3 / RFdiffusion + LigandMPNN)

- **Fix the whole catalytic constellation** (TS reacting atoms + ALL donors) as
  one rigid block — not just one atom. Scaffold around the **TS** pose, not the
  reactant/relaxed minimum: that is where the donors do catalytic work.
- **Strip the QM caps**; rebuild each donor as a real residue. Oxyanion-hole
  donors should be **sequence-agnostic backbone N-H** (let design assign
  residues). A side-chain donor (e.g. Asn) must enter as that side chain in its
  proper geometry — do NOT "fix a backbone N-H and relabel it as a side chain";
  backbone-amide vs side-chain-carboxamide attach differently and the geometry
  will not be reachable.
- The "ligand" handed to the scaffolder is a **transition state** (partial bonds);
  pass it as rigid coordinates and prevent any small-molecule step from
  "correcting" partial bonds or re-perceiving bond orders.
- **Generate many, filter hard.** A thin motif yields many nominal backbones; the
  real work is filtering (fold/self-consistency via AF3/Chai-1, then **donor
  retention in the TS pose**, not just fold quality).
- Scaffolding feasibility is independent of unproven catalysis — run the cheap
  barrier check in parallel, not after.

## Decision patterns (chemistry)

- **Design target = transition-state stabilization** (Pauling): stabilize the TS
  MORE than the reactant. Do not over-stabilize the ground state (a donor/cation
  that mainly stabilizes the reactant anion raises the barrier).
- **Put the oxyanion hole where negative charge actually develops** at the TS
  (e.g. on the electrophile's carbonyl in a conjugate addition, where the enolate
  forms — which can be a different carbonyl than the nucleophile's).
- **Match donor/acceptor and charge at every contact.** A deprotonated imine N is
  an acceptor with no N-H to donate and carries negative charge — it wants a
  neutral donor or a cation that won't transfer a proton (the metal analog), not
  another anion/acceptor.
- **Metal vs H-bonds.** A metal gives bidentate, charge-without-proton, geometry-
  locking organization that 1-2 H-bonds cannot reproduce; reserve it for cases
  (concerted pericyclic, tight chelation) that genuinely need it. Cofactor-free
  H-bond/cation pockets work well for stepwise additions.

## Using the helpers

`scripts/theozyme_tools.py` — parameterized placement/alignment (embed fragment,
Kabsch, vector/rotation utilities, lone-pair directions, spin-to-relieve-clash,
anchor selection, full `place_donor` and `graft_residue`). No substrate-specific
indices.

`scripts/mode_tools.py` — read an ORCA `pltvib` animation, recover the
equilibrium geometry and eigenvector, list top movers, compute per-bond
closing components, and emit a mode-displaced geometry to clean a spurious mode.

`reference/orca_templates.md` — relax / OptTS / Freq / relaxed-scan / IRC inputs
with the standard method line and constraint syntax, for closed-shell and metal
(UKS) cases.

`reference/failure_modes.md` — the full catalog with how each was caught and
fixed.

## Pipeline mode (little-protein-tiger integration)

When run as the **theozyme stage** of the LPT enzyme workflow you are handed a
substrate/reaction handoff and a persistent project directory. Your job is the
*reasoning*: pick the rate-determining step, decide concerted vs stepwise,
choose the catalytic donors and their truncations/charges, and specify the
catalytic constellation. **Deterministic LPT code does the mechanical work** —
it writes the ORCA inputs (relax → OptTS → Freq → scan) from your spec, the user
runs ORCA externally, then LPT ingests the results (single-imaginary check,
`classify_ts`, barrier estimate) and gates on the barrier before scaffolding.
You do **not** run ORCA, RFD3, or LigandMPNN; you produce the design they need.

### Tools available in pipeline mode
- `search_corpus`, `get_fingerprint` — prior art: known/engineered enzymes for
  this reaction, mechanism precedents, catalytic-residue roles, QM methods.
- `search_pdb_by_ligand` (by CCD id / SMILES / name) — find a deposited holo
  structure of a protein bound to the substrate (or a close analog).
- `tool_extract_ligand_contacts` — from such a holo structure, read the
  first-shell residues + the ligand atoms they contact, as **optional** graft
  candidates. **Grafting is optional**: there is frequently no holo structure —
  if so, build the active site purely from the QM model and let the diffusion
  model generate the surrounding residues. Never block on grafting.
- `tool_analyze_active_site_geometry` — measure distances/angles in a guessed or
  grafted constellation before committing to a QM run (catch the inverted-vector
  / anchor-1.5 Å-off mistakes from the failure-mode catalog cheaply).
- `find_pdb_structures` / `search_rcsb_pdb` — corpus + RCSB structure lookup.
- `write_file` — write the trimmed motif / TS geometry artifacts into the
  project run dir's `enzyme/` folder.

### Sourcing & anti-hallucination
The mechanism/QM reasoning in this skill is established chemistry — assert it
directly. But **specific empirical prior-art claims** (a particular enzyme's
measured barrier or kinetics, "this exact pocket was used before") follow the
corpus rules: search the corpus first; cite a DOI when it's there; tag background
recollection `[uncited]`; and **never fabricate a DOI, a PDB accession, or a
measured number**. PDB IDs you mention must come from the tools, not memory. A
candid `[uncited]` belief beats a fake citation.

### PIPELINE HANDOFF (emit at the very end, verbatim format)
End your report with a `### PIPELINE HANDOFF` block of plain `- key: value`
bullets (no code fence) so the orchestrator can drive the deterministic ORCA-prep
and scaffolding stages. Keys (single-token), values one line; pack structured
data as a JSON string:

```
### PIPELINE HANDOFF
- substrate_smiles: <SMILES of the substrate(s), comma-separated>
- reaction_step: <the rate/stereo-determining step you designed for>
- mechanism: concerted | stepwise
- forming_bond: <ligand atom pair, e.g. C0-C18>
- acceptors: <ligand atoms where negative charge develops, e.g. O15,N5>
- donors_json: <JSON: {"A1":{"donor":"ND2","acc":"O15","mode":"sc","smiles":"CC(=O)N"}, ...}>
- charge: <int>
- mult: <int>
- ts_xyz_path: <project-relative path to the TS .xyz, or NEEDS_QM if not yet built>
- grafting: used <PDBID> | skipped (no holo structure)
- go_recommendation: GO | CONDITIONAL_GO | NO_GO
- go_rationale: <one line: is the mechanism/target sound enough to spend QM+GPU?>
```

Be honest in `go_rationale`: a favorable cluster barrier is preliminary evidence,
not a rate, and a beautiful scaffold around an unconfirmed motif is still not a
catalyst.
