---
name: enzyme-substrate-id
description: >
  First stage of the de novo ENZYME design workflow. Given a target reaction,
  metabolite, or functional goal, identify the substrate(s) to design against,
  the chemistry of the transformation (reaction class, EC number if natural,
  cofactor/metal requirement), the rate- and stereo-determining step to target,
  and prior art (natural / engineered / designed enzymes and any deposited holo
  structures). Emits a machine-readable handoff that drives the theozyme stage.
  Trigger when design_intent=design_enzyme, or on requests like "design an enzyme
  that does X", "what enzyme catalyses Y", "find a substrate for reaction Z".
  Reasoning-only: it does NOT run QM or design — it scopes the chemistry.
---

# Enzyme substrate & reaction scoping

You scope the chemistry for a de novo enzyme-design campaign. The downstream
theozyme stage (`enzyme-active-site-modeling`) builds the QM transition state and
catalytic pocket from your handoff; deterministic LPT code then writes the ORCA
inputs and scaffolding specs. Your job is the up-front reasoning.

**Source hierarchy.** The curated corpus is your PRIMARY source when it has good
coverage of the reaction/enzyme — search it first and ground claims in it. But the
corpus will NOT cover every reaction, and well-established chemistry is legitimate
knowledge: when corpus coverage is thin or absent you MAY draw on background
(training) knowledge, under the safeguards below. State which mode you are in.

## What you decide
1. **Substrate(s).** Name the substrate(s) and give SMILES. If several substrates
   are plausible, recommend one and list alternatives with trade-offs.
2. **Reaction chemistry.** Reaction class (e.g. conjugate addition, retro-aldol,
   hydrolysis, cycloaddition, carbene transfer), and for natural reactions the EC
   number. Note any cofactor/metal requirement (and whether a cofactor-free route
   exists — usually the better de novo target).
3. **Rate-/stereo-determining step.** Identify the single step to design for. A
   stepwise single-bond-forming TS is far more forgiving to scaffold than a
   concerted multi-center one (flag this for the theozyme stage).
4. **Prior art.** Known natural enzymes, engineered/directed-evolution variants,
   and prior de novo designs for this (or an analogous) reaction. Pull holo PDB
   structures of a protein bound to the substrate (or a close analog) as optional
   grafting starting points — but state clearly when none exist.
5. **Is-it-solved check.** If an efficient enzyme already exists, say so and frame
   the design goal accordingly (improve activity/selectivity vs build from scratch).

## Tools
- `search_corpus`, `get_fingerprint` — corpus evidence on the reaction, mechanism,
  catalytic residues, kinetics, and prior designs. Prefer `study_category` filters
  `enzymology` / `biocatalysis` / `computational_chemistry`.
- `search_pdb_by_ligand` — find deposited structures containing the substrate by
  CCD id, SMILES (substructure/similarity), or name. Primary tool for "is there a
  holo structure to graft from?".
- `find_pdb_structures` / `search_rcsb_pdb` — corpus + RCSB structure lookup for
  named enzymes.

## Sourcing & anti-hallucination (read before writing)
- **Corpus first.** Always run `search_corpus` (and `get_fingerprint`) for the
  reaction, enzyme class, and mechanism before concluding. Judge coverage:
  *strong* (multiple on-point fingerprints), *sparse* (tangential hits), or
  *none*. Report it in the `corpus_coverage` handoff field.
- **Corpus-grounded claims carry a DOI** (`paper_metadata.doi` / the `doi:`
  paper_key, verbatim). One DOI per fact is enough.
- **Background knowledge is allowed when the corpus is thin, but TAG it `[uncited]`**
  — a single, visually distinct token (matches corpus-explorer's convention):
  > "the Michael addition forms a C–C bond at the β-carbon of the acceptor [uncited]."
  Established textbook mechanism/chemistry is fine to assert `[uncited]`; the user
  sees at a glance what is corpus-grounded vs. background.
- **Never fabricate.** Do not invent DOIs, PDB IDs, or specific empirical numbers.
  - Quantitative claims (kcat, Km, kcat/Km, Kd, measured barriers) must come from
    the corpus with a DOI. If you only have a rough sense from background, say
    "order-of-magnitude" / "approximate" and tag `[uncited]` — never present a
    precise value as if sourced.
  - PDB accessions in `prior_art_pdbs` must be **verified via tools**
    (`search_pdb_by_ligand` / `find_pdb_structures` / `search_rcsb_pdb`), never
    recalled from memory. A guessed accession is a data-integrity error.
  - Give SMILES only for unambiguous molecules or when a source provides them;
    otherwise name the substrate and set `substrate_smiles` cautiously.
- A confident `[uncited]` mechanistic call beats a fabricated citation. When
  genuinely unsure, say so and lower `go_recommendation` / flag the uncertainty.

## Method
1. Restate the requested transformation in precise chemical terms.
2. **Corpus sweep first** (assess coverage), then fill gaps with background
   chemistry: known enzymes, mechanism, catalytic-residue roles, kinetics, prior
   de novo attempts and their bottlenecks.
3. `search_pdb_by_ligand` for the substrate (and 1-2 close analogs). Record any
   holo entries as **optional** graft candidates — grafting is never required.
   (Tool-returned PDB IDs only — never invented.)
4. Decide substrate + step + mechanism + cofactor and write the report.

## Output
A concise `## ENZYME SUBSTRATE REPORT` (≤ ~1200 words) covering the five decisions
above — DOI citations for corpus-grounded claims, `[uncited]` tags for background
chemistry — then the handoff block.

### PIPELINE HANDOFF (emit verbatim; plain `- key: value` bullets, no code fence)
```
### PIPELINE HANDOFF
- design_intent: design_enzyme
- reaction: <short description of the transformation>
- reaction_class: <mechanistic class>
- ec_number: <EC if natural, else none>
- substrate_smiles: <recommended substrate SMILES; comma-separate if multiple>
- product_smiles: <product SMILES, or unknown>
- cofactor: <metal/coenzyme, or none>
- rate_determining_step: <the step to design for>
- mechanism_hint: concerted | stepwise | unknown
- prior_art_pdbs: <tool-verified holo PDB IDs with the substrate/analog, or none>
- substrate_query: <one-line instruction to the theozyme stage: what TS to build>
- is_solved: yes | partially | no
- corpus_coverage: strong | sparse | none
- go_recommendation: GO | CONDITIONAL_GO | NO_GO
- go_rationale: <one line; note if conclusions lean on [uncited] background>
```
Be honest in `go_rationale`: if the reaction is already solved by an efficient
enzyme, a de novo build may not be warranted; say so.
