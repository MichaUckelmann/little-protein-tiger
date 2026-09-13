# Target-selection audit — 2026-09-13

Why every target LPT had ever picked was also named in a skill prompt, whether
that was cause or coincidence, and what a 16-run varied-prompt sweep found
instead. **Status: measurement complete, fixes NOT implemented.**

---

## The headline answer: the prompt was NOT the driver

The `pain` ablation cell was re-run with CALCRL/RAMP1 removed from every
prompt (commit `f45cba9`). Result — `outputs/ablation/results.json`:

| arm | primary target |
|---|---|
| live (corpus intact) | CALCRL / RAMP1 |
| blank (every corpus tool returns nothing) | CALCRL / RAMP1 |
| decoy (corpus returns off-topic chromatin papers) | CALCRL / RAMP1 |

Unchanged from the pre-removal run. The *supporting cast* moved a lot
(live NTRK1/NGF/SCN9A · blank TRPV1/VSD4/SCN9A · decoy NTRK1/NGF/TRPV1) and
`n_dois` fell 8 → 3 → 4, but the primary never budged.

**So it is training knowledge, not the prompt and not the corpus.** The
operator predicted this; my first reading ("CALCRL was named three times,
therefore the prompt did it") was an over-claim from a design that varied only
one variable. For "pain receptors" CALCRL/RAMP1 is a defensible answer a
well-trained model reaches unaided — erenumab is approved and the CGRP
receptor is the validated pain-receptor PPI of the decade.

The completed 2x2:

| | corpus live | corpus blank/decoy |
|---|---|---|
| prompt names CALCRL | CALCRL | CALCRL |
| prompt silent | CALCRL | **CALCRL** |

---

## Diversity: 16 runs, 14 distinct complexes, zero repeats

`--stop-after spec`, gemini-3.7-flash, $0.44/run, ~$7 total of a $20
allowance. Projects under `projects/div_{standard,wildcard}_<slug>/`, logs in
the session scratchpad as `diversity/{standard,wildcard}_<slug>.log`.

| prompt | coverage | standard (pathway-expert) | wildcard (wildcard-expert) |
|---|---|---|---|
| epigenetic | dense 15.8% | EZH2 / EED (5HYN) | EP400 / TRRAP (9C47) |
| dna_damage | moderate | FANCM / FAAP24 (9HJO) | WDR48 / USP1 (9HNW) |
| tnbc | moderate | YAP1 / TEAD1 (3KYS) | WWC1 / PTPN14 (6JJW) |
| chaperone_nd | mixed | CHIP / MAPT (8FYU) | STUB1 / HSPA8 (4KBQ) |
| rheumatoid | moderate | BRD4 / E2F-1 (3UVW) | TRAF6 / TNFRSF11A (1LB5) |
| pneumonia | sparse | PqsE / RhlR (8B4A) | none — paused, structure_needed |
| diabetes | sparse | GLP-1R / GLP-1 (5VAI) | GPR17 / Gai (7Y89) |
| tuberculosis | sparse | EsxB / p38 (3FLN) | none — REFUSED by gemini |

Only YAP1/TEAD1 had ever appeared in a skill, and no longer does. The two
modes diverged on all 8 prompts. **The named-set correlation was an artefact
of having only ever run two or three prompts.** (CHIP and STUB1 are the same
protein with different partners — related, not a repeat.)

Zero `GENE_A`/`GENE_B` placeholder leaks in 15 reports, and 14/15 emitted a
parseable pathway handoff — so the prompt neutralisation of `08b05f2` /
`f45cba9` cost nothing measurable in format compliance. That was the risk that
justified holding those commits; it did not materialise.

---

## What the sweep actually found — six defects, none implemented

### 1. Advertised size limit != enforced size limit  (cost 2 of 7 runs)

`design.constraints.max_target_residues: 500` and `target_residues_warn: 250`
are shown to the LLM; `design.foundry.target_residue_budget: 220` is what the
trim hard-fails on. They measure different things — 500/250 describe the RAW
chain, 220 the hotspot-bearing DOMAINS after trimming — so a 600-residue
protein with a 150-residue interface domain is fine and a 304-residue
single-domain protein is not, and nothing tells the stage that. PqsE (304) saw
"well under 500", then died at 220.

Fix: guidance, not a number. Tell the structure stage the domains carrying its
chosen hotspots must total <= 220 after trimming. Names no target, so it is
consistent with the neutrality rule.

**220 WAS NEVER MEASURED — answered, agent `a1a289a778a5fa5c6`.** Set
2026-08-21 from ONE complex that ran comfortably at ~175 tokens (a ~97-residue
target), and `config.yaml:341-343` has admitted that verbatim ever since.
`diary.md:2836` and `:3067` list the bisection as an open punch-list item;
no commit closes it. Commit `2996fe8` deleted `gpu_memory_gb: 32` as unread,
so **nothing in the codebase connects the number to any amount of VRAM**, and
`plan_campaign` clamps on disk alone (`foundry_runner.py:320-334`).

**No OOM has ever happened here.** Three greps over `projects/`, `logs/`,
`data/` for `out of memory|CUDA out of memory|torch.cuda.OutOfMemory`,
`CUDA error|uncorrectable ECC|device-side assert`, and `\bOOM\b` returned
ZERO hits. The only `uncorrectable ECC` mentions are prose about the cluster
(`CLAUDE.md:827`, `diary.md:3161`). The only real error class in any campaign
log is the A344 validation incident (`e2e_foundry/.../rfd3.log:13,162,178`).

**What has actually folded on this card** (22 `trim_map.json` files, all
`budget: 220` except `mash_e2e` at 222; token counts are ground truth from
`num_tokens_in` in every RFD3 sidecar, span +-8 because the binder samples
70-86):

| campaign | target res | tokens | RF3 refolds | status |
|---|---|---|---|---|
| mash_e2e r2 (5GN0 A) | **222** | **292-308** | 2,452 | production KILLED at 16/6,104 |
| mesothelioma_showcase (3KYS A) | 207 | 277-293 | 3,620 | complete through binder_summary |
| il7ra_e2e (3DI2 B) | 186 | 256-272 | 9,688 | production finished on disk, UNRECORDED in manifest |
| pdl1_e2e (7CZD B) | 117 | 187-203 | 7,512 | complete |
| pain_receptors_v3 (3N7S D) | 84 | 154-170 | 3,680 | complete |

- **Largest ever folded: 222 residues / up to 308 tokens** (mash_e2e, through
  pilot + calibration). Largest to complete PRODUCTION: 207 residues / 293
  tokens, 1,352 refolds in 8.60 h.
- **Time and disk have each killed a campaign; memory never has.** mash_e2e
  died because its gate costed 22,184 refolds at 63 GPU-h on the stale flat
  8.4 s anchor while `plan_campaign` costed the same work at **138.1 GPU-h** on
  the measured 20.6 s/refold — the one-anchor-one-law bug. `gem_vegf_a` stopped
  a step earlier at 267.4 GPU-h / 217.9 GB against a 120/120 budget.
- The targets 220 is now refusing need **382-484 tokens** (8B4A chain A RhlR =
  304 res; 9HNW chain A WDR48 = 406 res, a single WD40 beta-propeller with
  nothing to cut) — 1.24-1.57x beyond anything folded here, but by the fitted
  runtime law only **27-40 s/refold**, versus the 20.6 s mash_e2e sustained for
  17 GPU-h. Cheap in time. Entirely unknown in memory.
- **RFD3 is the likelier VRAM-binding stage and the worse characterised**:
  `SEC_PER_RFD3_DESIGN = 5.4` has NO size law at all and runs at
  `diffusion_batch_size: 4`.
- Recommend making the gate a **TOKEN** gate in `validate_spec`: tokens are
  what both cost laws and the GPU care about, and the residue gate sums target
  spans only, never the binder (`foundry_spec.py:271-279`) — so it is ~28%
  wrong across modalities by construction.
- **Benchmark, ~30-60 GPU-MINUTES:** 8 token points (195 -> 1000), 3 refolds
  each, driving `src/foundry_stages.py rf3` directly on hand-assembled
  two-chain `*_b0_d0.cif` files — it globs only `"_b" in name and
  name.endswith(".cif")` (`foundry_stages.py:169`), so RFD3 and MPNN can be
  skipped. Extend `scripts/benchmark_trim.py` (already walks a size spectrum,
  already derives hotspots from `analyze_interface`, already CPU-only by
  default). Sample peak VRAM with `nvidia-smi -lms 250` — RF3 runs as a
  subprocess in `.venv-blackwell`, so `torch.cuda.max_memory_allocated` is
  unreachable. Time from `rf3_out` mtimes. Validate each point against the
  sidecar's `num_tokens_in`. **Must run on an IDLE card** — peak VRAM measured
  beside another tenant measures the wrong thing.

Two stale `scripts/doctor.py` checks found in passing, **both now fixed**: the
31 GB warning cited `config.yaml`'s deleted `gpu_memory_gb` and is now worded
as what it actually is — "below the only card every campaign here was run on",
with the real demonstrated ceiling (308 tokens) and the knob to turn
(`target_residue_budget`) — and `check_disk` cited "~2.5 MB per RF3 design"
after `229dae7` replaced that flat constant with the measured 0.6-1.9 MB size
law. The threshold values are unchanged; only the claims about what they mean.
The VRAM BENCHMARK above is still unrun and still needs an idle card.

### 2. Every PPI structure guard is inert for a non-human target, silently

`target_resolve.resolve_target` is human-only by construction (backed by
`data/depmap/HUMAN_9606_idmapping.dat.gz` + HGNC TSV;
`identifier_normalizer.py:21-25` says so). `_select_designable_structure`
(`pipeline_runner.py:2490-2494`) bails at `if len(accs) < 2: return None`
before its partner-presence test at `:2511`. Verified inert:
EsxB, CFP-10, Rv3874, PqsE, RhlR all `ok=False`.

2 of 8 prompts produced non-human targets; both ran with no cross-check.

Traced consequence (`projects/div_standard_tuberculosis`): target_complex
"EsxB / p38", pdb_id 3FLN. The BIOLOGY is real and cited
(doi:10.1038/s41421-024-00653-4). 3FLN is "P38 kinase crystal structure in
complex with R1487" — one polymer entity (MAPK14/Q16539), one ligand, **no
EsxB and no chain A** (its only chain is C). Four guards failed open in
sequence, then the run died in `_resolve_unverified_label_seq_ids` with
"cannot build the auth->label map for chain A" — after three paid LLM stages.

Full design from agent `a42616b42781db28e` (report in this session's history;
key points preserved here):

- **Step 0, do first, needs no database:** assert `target_chain` and
  `partner_chain` are in `_count_chain_residues(analysis_path)`, in
  `_stage_structure` (~`:5908`) and `_stage_binder_interface` (~`:1824`).
  **HARD-FAIL** — a declared chain absent from the file is a fact, not an
  inconclusive check. Catches 3FLN at zero cost, organism-independently.
  There is no chain-existence guard anywhere today.
- Widen `entry_metadata`'s `_ENTRY_QUERY` (`target_resolve.py:202-219`) with
  `rcsb_entity_source_organism { ncbi_taxonomy_id ncbi_scientific_name
  rcsb_gene_name { value } }` and `rcsb_macromolecular_names_combined`. Same
  call, same latency; `skill_runner.py:693-706` already proves the fields
  resolve.
- Widen `ortholog_check.uniprot_entry` (`:137-155`) to return a NAME SET
  (recommended + short + alternative + gene synonyms + locus names) instead of
  one name. This is what makes EsxB/CFP-10/Rv3874 and PqsE resolve — all are
  in their accession's own synonym set. ~10 lines, highest value per line.
- New `resolve_target_in_entry(name, pdb_id)`, tiered and ENTRY-SCOPED (which
  is what kills cross-organism ambiguity): human offline -> entry-scoped
  UniProt name match -> RCSB gene name -> RCSB description; refuse when >=2
  chains claim the name (inherit `identifier_normalizer.resolve`'s ambiguity
  rule at `:696-711`). Tier is returned so callers can gate on 1-2 and only
  warn on 3-4.
- New `find_entries_by_gene(gene, taxid=None)` — RCSB search on
  `rcsb_entity_source_organism.rcsb_gene_name.value`, needs no accession.
  Verified live: `esxB -> ['3FAV','6J19']`, `pqsE -> 12 entries incl. 8B4A`.
- Restructure `_select_designable_structure` into two questions: (1) does the
  CHOSEN entry contain both requested proteins -> **HARD-FAIL** if not, naming
  the chain inventory via `_chain_inventory` (`:2961-2977`); (2) is there a
  cleaner entry for the pair -> existing ranking, WARN if screening could not
  run. Note the partner-absent verdict at `:2546` is currently **unreachable
  when no replacement exists**, which is exactly the 3FLN case.
- Order `_verify_ppi_chain_assignment`'s candidates so resolvable names go
  FIRST (`:3015`, `:3023-3029`). Today the unresolved name "passes" via the
  warn-only path and short-circuits the decisive one — backwards.
- `_check_structure_organism` should change its QUESTION for a deliberately
  non-human target, not just be extended: print RCSB's real source organisms
  beside whatever the handoff claimed. On this run that would have printed
  "3FLN: Homo sapiens only, no Mycobacterium chain" at stage 0. Do not gate on
  the handoff's `structure_organism` — it is an unverified model claim, read by
  no code, and it said "Homo sapiens" for a mycobacterial campaign.
- Say which guards were inactive, on the PPI track too. Copy
  `_structure_first_caveats` (`:1549-1580`) and add a manifest checkpoint
  recording, per protein, which tier resolved it. Today "was this campaign
  actually checked?" has no answer on disk.
- `ortholog_check.classify_chain` hard-codes `non_human = taxid != 9606`
  (`:41`, `:255`); parameterise to the INTENDED organism or a pathogen chain is
  mislabelled ORTHOLOG.
- Tests to extend, not rewrite: `tests/test_audit_fixes.py:918-1000`
  (`_patch_rcsb` seam at `:924-935`), `:495/534/544/597/608/674/684/949-995`,
  `tests/test_structure_switch_persistence.py`.
  `test_an_entry_without_both_proteins_is_not_a_candidate` (`:975-986`) is
  about the REPLACEMENT and stays true; the new hard fail is about the CHOSEN
  entry and needs its own case.

### 3. The corpus-accession rule: an internal contradiction plus an aggregation bug

Agent `ae1a019c4a56b0a0e`'s findings (report in session history; preserved):

- The contradiction is **inside the SKILL.md**, not prompt-vs-tools.
  `pathway-expert/SKILL.md:288` DOES authorise `search_rcsb_pdb` as a
  fallback; `:482`, `:494`, `:500`, `:537-542` restate the rule as corpus-only
  and `:537-542` tells the model to hand RCSB lookup back to the human.
  Mirrored in `wildcard-expert` at `:581-584`, `:879`, `:890-891`, `:897`.
  Both selectors have `search_rcsb_pdb` (allowlist `skill_runner.py:662`).
- **The actual cause of 3FLN is an aggregation bug**: the fallback fires on
  `total_found == 0`, which is the SUM across queried proteins. Reproduced —
  `EsxB -> []` but `p38 -> 8 ids`, so `total_found = 42` and the fallback was
  structurally unreachable for the protein that needed it. Fix: per-protein
  trigger. Highest-value prompt edit.
- `data/pdb_metadata.json` (4.7 MB, 5,019 entries, static, mtime 2026-08-24)
  has **no row for 3FLN**, so `query_named_in_metadata: False` came back with
  no title — "unknown" indistinguishable from "rejected", and the guidance
  "read the title before accepting" had nothing to read. 7CZD and 3N7S are
  also absent.
- The corpus restriction DID cost a real answer: the genuine EsxB/ESAT-6
  structures are **3FAV** (2.15 A) and 6J19, neither in the corpus.
- **"Must exist in the PDB" would have caught nothing** — 3FLN exists. Only
  "must contain both named proteins" catches it, and that is already written at
  `pipeline_runner.py:2511`.
- Downloadable inventories, sizes verified live 2026-09-13:
  - `https://data.rcsb.org/rest/v1/holdings/current/entry_ids` — 1.82 MB,
    259,747 ids, uppercase. Answers existence only.
  - `https://ftp.ebi.ac.uk/pub/databases/msd/sifts/flatfiles/csv/pdb_chain_uniprot.csv.gz`
    — **5.9 MB, 244,406 entries, 33 MB uncompressed, indexes in 0.48 s**,
    carries its own vintage line (`# PDB: 36.26 | UniProt: 2026.04`). Answers
    "does entry X contain proteins A and B" OFFLINE. Spot-checked:
    `3fln -> {Q16539}` only; `3fav -> P9WNK5 + P9WNK7`; `3kys -> P28347 +
    P46937`. Entry ids are lowercase, must `.upper()`.
  - Caveat: a chain with no UniProt has no SIFTS row — 7CZD's anti-PD-L1 VHH is
    absent entirely — so "both present" gives FALSE NEGATIVES for antibodies
    and designed partners. Must stay fail-open and loud.
  - `entries.idx` (54.7 MB) carries only free text, no accessions — not useful.
- `entry_metadata` already works on any organism (verified on Mycobacterium
  entries), so the online path exists today.

### 4. Molecular glue cannot reach a spec — the pipeline is single-chain

`design_intent: stabilize` (8 of 132 recorded stages; `disrupt` is 119) needs
hotspots on BOTH partners. Three places assume one chain:

1. `handoff.py:101-131` `parse_hotspot_residues` — reads target_chain from the
   HANDOFF only, appends every row of every section with **no chain
   attribution**; dedup key `(residue, auth_seq_id)` would also collapse the
   same name+number across chains.
2. `_verify_hotspot_grounding` — looks every residue up on the single
   target_chain.
3. `foundry_spec.py:120` — `select[_hotspot_key(target_chain, auth)] = atoms`,
   every hotspot assigned target_chain unconditionally.

Test case on disk: `projects/div_standard_diabetes/runs/round-1/02_structure.md`
— GLP-1R / GLP-1 on 5VAI, `design_intent: stabilize` (correct strategy for
T2D). Its table has two sub-sections: chain A (GLP-1R) PHE66/ASP67/ALA70 and
chain B (GLP-1) ALA30/GLY35/ARG36/GLY37. **Verified against the file:** 5VAI
chain P (GLP-1, auth 7-37) really has ALA30/GLY35/ARG36/GLY37; chain R
(GLP-1R, auth 29-421) really has VAL30/THR35/VAL36/GLN37. So every residue is
correct on its own chain and the run still hard-failed grounding.

**DESIGNED — agent `a7409bc393a05d960`, report received 2026-09-13.**
Medium change: ~6 files, ~10 functions, 250-350 lines. Not started; the
framing decision below wants a human yes before anyone writes it.

**The recommended framing: merge at the CONTIG, not in the file.** Keep the
trimmed structure a genuine two-chain file with author numbering intact
(`write_trimmed` already writes any number of chains with any residue subsets
— it needs NO change) and emit `70-86,/0,R29-128,P7-37`, with no `/0` between
the two target chains. Read out of foundry's own source
(`rfd3/inference/input_parsing.py:1319-1324`): `/0` is the chain-INCREMENT
token, and components not separated by one accumulate into the same output
chain with a monotonically increasing res_id, whatever input chain they came
from. So RFD3 merges the two target chains ITSELF, and the input->output
mapping stays recorded per residue in the sidecar's `diffused_index_map`.
Confirmed empirically on the shipped 3-segment YAP1/TEAD1 campaign:
`contig: "70-86,/0,A195-229,A239-343,A345-411"`, `extra.num_chains: 2`, map
keys all `A*` and values all `B*`.

That is what makes this affordable: the RFD3 output stays TWO chains, so
`binder_metrics`, `binder_ranking`, the prefilter, MPNN's
`designed_chains: ["A"]`, `_pair`'s `[0][1]`, ipSAE A-vs-B and
`hotspots_from_rfd3(sidecar, "B")` are all **unchanged**. `max_chainbreaks` is
correct for free, since it is derived from `trim.n_segments` — provided that
becomes the total across both chains.

**Verified, not argued: `foundry_spec.validate_spec` already handles a
two-chain target.** The agent built a real glue spec against
`data/structures/5VAI_ba1.cif` and ran the shipped validator unmodified:
`spec OK: glp1r_glue_001 | binder 70-86 | 131 target residues in 2 segment(s)
| 7 hotspots`, `target_spans: [["R",29,128],["P",7,37]]`. `parse_contig`
returns per-chain spans and the hotspot loop already parses the chain out of
the key. **One line in that file is the entire generator-side blocker**
(`foundry_spec.py:120`, `_hotspot_key(target_chain, auth)`). The weak spot is
the trim cross-check at `:309-315`, which discards the chain
(`sorted((lo, hi) for _, lo, hi in spans)`) and would compare equal to the
wrong thing for two chains sharing an author range.

**The real work is `trim_target`.** It takes one `target_chain` and one flat
hotspot list, and needs a per-chain trim plan: `segment_domains` + `plan_trim`
per chain, a budget allocation across them, `kept_by_chain` ADDED ALONGSIDE
`kept_segments` (leaving `_TrimFromDisk`, `trim_map.json` readers and every
disrupt path byte-identical), and `build_contig` over multiple chains. Two
guards change MEANING rather than shape:

- `_exposed_hydrophobic`'s `sasa()` measures one chain in isolation in both
  files, so for a two-chain target it cannot see a face opened by cutting the
  OTHER chain away. Measured: keeping `R29-145 + P7-37` reports 0 newly
  exposed hydrophobics on chain P by construction, though chain P residues
  7-25 thread into the GLP-1R TM bundle that was just removed. A glue needs
  per-chain SASA in the context of the whole kept assembly.
- `min_bsa_retention` measures target-vs-partner, and for a glue both chains
  ARE the target — the R<->P interface is the thing being stabilised, not
  discarded. Measuring that interface before and after is the single most
  important glue-specific trim guard and it does not exist.
- `MIN_TARGET_RESIDUES = 80` must apply to the total; chain P's 31 residues
  fail it individually.

**5VAI is feasible on size and geometry, and blocked on two other things.**
A clean trim exists at **R29-128 + chain P whole = 131 residues** (2 newly
exposed hydrophobics away from the epitope, 0 near — passes). The measured
boundary scan is worth keeping, because the obvious cut fails:

| trim | residues | R exposed (away/near) | P (away/near) |
|---|---|---|---|
| R29-128 + P7-37 | 131 | 2 / 0 PASS | 0 / 0 |
| R29-135 + P7-37 | 138 | 2 / 0 PASS | 0 / 0 |
| R29-145 + P7-37 | 148 | 5 / 0 FAIL (TYR145 +189 A^2) | 0 / 0 |
| R29-128 + P26-37 | 112 | 2 / 0 | 0 / **1** FAIL (PHE28, 5.6 A from a hotspot) |
| R29-160 + P7-37 | 163 | 12 / 0 FAIL | 0 / 0 |

Cutting at 145 — the textbook ECD boundary — fails, and cutting chain P at all
fails. **Unverified and worth one call before writing code**: whether
`plan_trim`'s segmenter lands on 128 or on ~145. CA-CA across the two patches
is 9.9-16.3 A, which a 70-86mer spans trivially.

**Blocker: 5VAI models no sidechains.** It is 3.3 A cryo-EM and every glue
hotspot carries backbone + CB only, so the skill's `CD2,CZ` / `CG,OD1` /
`CZ,NH1` do not exist and `validate_spec` refuses the spec correctly. Re-run
with `CB,CA` passes, but a 5-of-7 backbone-only hotspot set is a weak steer.
The better answer is a better entry — that run's own pathway checkpoint
already lists `human_alternatives: ['27XK','3C59','3C5T','3IOL','4ZGM']`, and
the GLP-1R **ECD-only** X-ray complexes are higher resolution AND already
inside the residue budget. So `_select_designable_structure` should prefer a
small, high-resolution entry containing BOTH partners when
`design_intent: stabilize`. **And a "this structure models no sidechains"
guard is worth adding regardless of glue work** — one pass over the contig
span, and it would have saved this run an LLM stage.

**The scoring gap is scientific, not mechanical, and it is where a glue
campaign silently produces the wrong molecule.** `hotspot_engagement` is a
pooled fraction gated at 0.75, so with 7 hotspots a design engaging one side
heavily and the other barely still passes — **nothing in the gate set
distinguishes a glue from an ordinary competitive binder that happened to grab
one partner.** The fix is cheap because the input chain SURVIVES the merge
inside `diffused_index_map` (keys are input-numbered `"R66"`/`"P30"`, values
output-numbered `"B1"`/`"B57"`): have `hotspots_from_rfd3` also return
output-resid -> input-chain, emit `hotspot_engagement_target` /
`_partner`, and gate on `min(...)` rather than the pool. Also promote
`target_rmsd` to a gate — under the merge it fits on the whole R+P assembly,
so a refold that lets the partner slip out of the groove shows up. **The one
way to break 119 existing runs**: a column absent from a record FAILS its
criterion (`binder_ranking.py:169-173`), so both new gates must ship `null`
and be set per-run.

**BoltzGen expresses a glue more naturally and is still not the first step.**
Its `include` and `binding_types` are both LISTS of per-chain entries
(`boltzgen/data/parse/schema.py:1888-1976`, `:2187-2210`) with chain-relative
1-based indices, which is exactly what `boltzgen_residue_indices` already
returns per chain — ~60 lines, no invented format, and it keeps the two target
chains DISTINCT in the output, so the R<->P chain-pair confidence a glue is
actually about survives. RFD3's merge destroys that (though it is recoverable
from the raw PAE: `ipsae_from_pae_matrix` takes a bare N x N matrix and
synthesises `token_chain_ids`, so given the index map an R-half-vs-P-half
ipSAE is a handful of lines). First campaign still goes on foundry, because
that is where the validated stage machine and every calibrated threshold are.

**Rejected: merging the two chains in the trimmed FILE.** It breaks author
numbering — which every trim preserves deliberately, and which
`_verify_hotspot_grounding` needs — makes `trim_map.json`'s
`identity_numbering: True` a lie, and for 5VAI the ranges collide (R29-128 vs
P7-37 overlap at 29-37) so chain P would have to be offset, at which point the
skill's own hotspot ids no longer address the structure. **Also rejected for
now: three output chains** (`/0` between the targets). Scientifically the most
honest, but it reworks 8 functions in the one module every threshold in
`config.yaml` was calibrated against on two-chain complexes — that is how you
silently invalidate 13 campaigns of calibration. Get it via BoltzGen instead.

**Deferred, explicitly: apo-vs-holo.** The only real proof a glue glues is
folding R+P WITHOUT the binder and comparing. That is a second fold per design
and a new stage. Until it exists, the summary the design-analyst sees must say
so, so nobody reads a passing glue campaign as demonstrated stabilisation.

**Two findings were NOT glue-specific and are FIXED (`9965bb6`)** — see the
"Related, smaller" list below, which they came out of: the absent-residue and
absent-chain fail-opens in `_verify_hotspot_grounding`, and
`_resolve_unverified_label_seq_ids` rewriting a row's label from another
residue's map. The agent found the second by checking the 5VAI report against
gemmi; I reproduced both before changing anything, and measured that 63 of the
67 stage reports on disk with a local structure are unaffected.

### 5. Related, smaller

- **FIXED (`9965bb6`).** `_verify_hotspot_grounding`'s message blamed
  numbering when the real cause was the wrong chain; it now searches the other
  chains and names the one that carries every claimed residue under its
  claimed name. Two fail-opens went with it — an ABSENT residue used to hit a
  `continue` and count as agreement (`div_wildcard_tnbc`, chain U's 447-453
  under `target_chain: A`, which then poisoned `_check_ortholog_conservation`
  into a `fraction_conserved: 0.625` over six residues that do not exist), and
  an absent target CHAIN landed in the "could not read the file" except branch
  because `get_sequence_map` returns `{"error": ...}` rather than raising
  (`div_standard_tuberculosis`, 11 hotspots on a chain A that 3FLN has never
  had — it has one chain, C). Also fixed:
  `_resolve_unverified_label_seq_ids` warned on a residue-name mismatch and
  then overwrote the label anyway, with the label of whatever residue the
  target chain has at that auth id — on 5VAI's glue table it replaced chain
  P's correct 24/29/30/31 with chain R's 69/74/75/76. Measured first: 63 of the
  67 stage reports on disk with a local structure are clean, and the 4
  affected come from the two runs above, both of which already failed.
- For a GENUINE offset, alignment-based remapping is feasible and safe:
  `structure_tools.sequence_identity` (BLOSUM62 local) already exists and
  `_chain_identity_to_uniprot` already aligns modelled residues to a UniProt
  canonical sequence. Safety comes from requiring the residue NAME to
  corroborate the mapping — a wrong mapping fails the name test exactly as
  today. Report the mapping in the stage file.
- `structure_needed` pause names no target (`target_complex:` empty), so the
  operator is asked for a PDB accession without being told what for.
  `projects/div_wildcard_pneumonia` has four prioritised strategies in
  `00_pathway.md` and none reaches the pause message. Its skill should also
  have written `pdb_id: NOT_FOUND` rather than omitting the handoff block
  (the only such omission in 15 runs).
- **Intracellular interfaces are unmentioned by either selector, and that is
  a REPORTING gap, not a missing prohibition.** Verified: `grep -c
  intracellular|cytoplasmic` = 0 in both selectors; the "Not reachable" list
  (`wildcard-expert:613`) covers lipid-ligand orthosteric pockets, deep
  aminergic pockets and TM surfaces only. `wildcard_diabetes` picked
  **GPR17 / Gai (7Y89)**, a GPCR-G-protein cytoplasmic coupling interface, and
  called tractability "Excellent".

  **OPERATOR DECISION (2026-09-13): do NOT make this a prohibition.** An
  intracellular domain is harder to reach, not impossible — intrabodies,
  intracellularly expressed nanobodies and cell-penetrating scaffolds are all
  real routes — and for some targets the therapeutic effect genuinely lives
  inside the cell. My earlier "a protein binder cannot reach it" was wrong.

  The guidance wants THREE tiers, not the current two:
  1. **Preferred** — extracellular, *when the extracellular part carries the
     therapeutic effect*. For a membrane receptor it usually does, and that is
     the case to prioritise.
  2. **Possible, with the delivery burden STATED** — cytoplasmic and
     intracellular domains. The defect in the GPR17 run is not the choice; it
     is calling tractability "Excellent" without naming the delivery
     requirement, so a downstream reader cannot see the cost.
  3. **Not reachable** — TM surfaces, and pockets entered laterally from the
     bilayer. This is the existing list and it is correct: those are
     unreachable by any binder, not merely hard.

  So the edit is to make tier 2 explicit and require the delivery route to be
  named in `tractability`/`go_rationale` — not to add GPR17-class targets to
  the refusal list. Pre-existing gap either way; not caused by the
  neutralisation (my edits to that section removed only the CALCRL example).

  **IMPLEMENTED.** Tier 2 written into all four target-selecting skills
  (`pathway-expert`, `wildcard-expert`, `molecular-biology-expert`,
  `complex-structure-analysis`; `binder-target-intel` already deferred to the
  operator's ask). `molecular-biology-expert` carried the actual prohibition —
  "**Never propose**: ... any intracellular residue" — and that clause is gone.
  Two things changed shape rather than being appended:

  - **The antibody test was load-bearing and wrong for tier 2.** "Would an
    antibody work here?" is a good test of whether a site is inside the bundle
    and a bad test of whether it is reachable at all — an antibody's own
    inability to cross a membrane says nothing about geometry. It is now scoped
    to the OUTER face explicitly, with the general test restated as "is this
    site solvent-exposed on one face, or inside the bundle?".
  - **The pipeline was already correct and two docs said otherwise.**
    `membrane_side` has defaulted to `"auto"` since `_infer_membrane_side`
    landed — the side is read off where the declared hotspots sit, so a
    coherently cytoplasmic epitope is trimmed to the cytoplasmic face and
    designed against normally. `CLAUDE.md:579` still claimed "membrane targets
    are designed against the extracellular side" and `CLAUDE.md:291` still
    named a "`_stage_trim` 'extracellular' default" that no longer exists. Both
    corrected. The prohibition was only ever in the prompts.

  What still fails, unchanged: a hotspot INSIDE the membrane, and hotspots
  split across both faces. Neither is one epitope, and the second is the one
  case inference genuinely cannot resolve — a straddling set leaves the trim no
  face to restrict to. The skills now say that is why.
- `wildcard_tuberculosis` was REFUSED by gemini (category OTHER, call #1) and
  the run stopped — the refusal contract working, no automatic retry. Standard
  mode on the same disease was not refused.

---

## Commits — all pushed as of `fe7f387` (2026-09-13)

    fe7f387  Intracellular is a delivery burden - say so in the prompts that said otherwise
    c2b3d49  Intracellular is a delivery burden, not a prohibition   (the decision)
    de18abb  Record that the 220-residue budget was never measured
    c9caaaa  Write down the target-selection audit before compaction
    5f25759  Record that the PPI structure guards are inert for a non-human target
    c4a488f  A cofactor is not a modified residue, and label_seq is what knows the difference
    f45cba9  Neither selector prompt names a plausible target any more
    08b05f2  No prompt may pre-answer the question its stage exists to answer

1209 tests pass. Foundry regression net clean (0 of 41 artifacts altered,
13/13 re-derivations bit-identical) as of `c4a488f`.

`08b05f2`/`f45cba9` were held pending evidence that placeholder exemplars did
not hurt handoff compliance; that evidence exists (14/15 handoffs, 0 leaks), so
the hold was lifted before pushing.

---

## Still in flight

- **PD-L1 macrocycle showcase**, GPU, `projects/pdl1_macrocycle/runs/round-1`,
  production 4,605 designs sized from its own calibration (SCALE_UP, bar
  auto-raised iptm 0.50 -> 0.65). All designed + inverse-folded; was at
  ~85/4,605 refolds mid-morning, ~10 GPU-h total. Log:
  scratchpad `run5_pdl1_production.log`. Its parent `queue.sh` writes
  `queue done` to `diversity/../queue_status.txt` when finished.
- **`queue3.sh` armed** behind the showcase (scratchpad), running in order:
  (1) A344 fix check — `--workflow structure --pdb 3KYS --chains A,B
  --design-engine foundry --project a344_fix_check --stop-after calibration`
  (pinned so the fix is tested regardless of what selection now picks);
  (2) `div`-style PPI+foundry retry on mesothelioma, project `e2e_foundry_r2`;
  (3) legacy BoltzGen driver retry, project `e2e_ppi_legacy_r2`.
  Both retries exist because runs 2 and 3 failed on bugs fixed in `2ce8ae4`.
- **Two agent reports not yet seen**: `a1a289a778a5fa5c6` (220-residue budget
  / OOM benchmark) and `a7409bc393a05d960` (multi-chain glue design).

## How to run things here

Launch any multi-hour campaign with `setsid nohup ... & disown`, NOT the Bash
tool's `run_in_background` — harness task teardown kills descendants even
across `start_new_session`, which killed a run and its detached BoltzGen child
on 2026-09-13. Watch the log with a Monitor instead. Memory:
`long-gpu-runs-need-setsid`.
