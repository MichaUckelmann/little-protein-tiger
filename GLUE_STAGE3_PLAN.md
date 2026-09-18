# Stage 3 — the executable plan (`design_intent: stabilize`, up to a validated spec)

**Written 2026-09-16 against HEAD `1f8c5dc`.** Successor to
`GLUE_IMPLEMENTATION_NOTES.md`, which stays as the cold-start handover — what
the eight stages are, what Stages 0–2 measured, why 4ZGM and not 5VAI. This
file is the part the notes deliberately did not carry: the ordered edits, the
command that proves each one, and the corrections you need before you trust
either of the older documents.

Every line number, count and measurement below was re-read or re-run in this
tree **today**. Where the four recon reports disagreed with each other I ran
the code and said which reading I took. Nothing here is inherited unverified;
where a number comes from someone else's GPU run it is labelled as such.

---

## 1. What Stage 3 delivers, and what it does not

Stage 3 makes a **two-chain molecular-glue target expressible**, end to end,
from an operator command to a spec `foundry_spec.validate_spec` accepts —
with **no GPU, no API call and no network** beyond structures already in
`data/structures/`. Concretely: a hotspot table whose rows are attributed to
the chain they are actually on; grounding and label_seq resolution that look
each row up on *its own* chain; a contig that names both chains; a
`TrimResult`/`trim_map.json` that records what that contig names; an RFD3
spec whose `select_hotspots` keys carry each hotspot's real chain; a
cross-check that can tell `A29-37` from `B29-37`; and a `--design-intent
stabilize` flag on `--workflow structure` that makes the whole thing
reachable without an LLM. The exit condition is one command producing
`70-86,/0,A29-128,B10-37` on 4ZGM with 8 hotspots across A and B, validated.

**It does not** run a GPU campaign (Stage 5), score one (Stage 4), or trim a
two-chain target (item 7, Stage 6). 4ZGM is chosen precisely because its two
chains are 100 + 28 = 128 residues against a 500 budget, so `plan_trim` keeps
them whole and no cut is ever measured — which is what lets every
single-chain measurement inside `trim_target` (`_exposed_hydrophobic`,
`_per_residue_bsa`, `bsa_retention`, `min_bsa_retention`, `allowed_auth`)
stay untouched and trivially correct. Stage 3 makes that boundary an explicit
**refusal**, not an assumption: a glue target that needs a cut is refused
naming scope item 7, and `trim_target` refuses a co-target unless the trim
came out a no-op. It also does not fix `hotspot_engagement` for a glue (see
§6, risk 4) — that gate becomes a pooled number on the first real campaign
and is Stage 4's problem, flagged here because Stage 3 is what makes it
reachable.

---

## 2. Corrections — read before `GLUE_IMPLEMENTATION_NOTES.md` or `GLUE_PIPELINE_SCOPE.md`

Both older files are wrong in ways that change what you would build. The
notes' own line numbers have drifted; the scope's are stale by up to ~166
lines and three of its claims are contradicted by running the code.

### 2.1 The two claims that would make you build the wrong thing

**(a) `validate_spec` ALREADY accepts a two-chain contig.** Notes §1 blocker
2 ("Fixing #3 alone changes nothing, because `validate_spec` gates a chain-B
hotspot on a chain-B contig span") and scope §1.3(a) item 3 ("A two-chain
contig cannot equal a chain-less `kept_segments` list") are both false.
Measured today on the real 4ZGM file, unmodified code:

    contig 70-86,/0,A29-128,B10-37, hotspots {A113, A120, B30}
      kept_segments=[(29,128),(10,37)]  -> OK   128 res, 214 tok, 2 seg
      kept_segments=[(10,37),(29,128)]  -> OK   (both sides are sorted)
      kept_segments=None                -> OK
      kept_segments=[(29,128)]          -> REFUSED (spurious — this is the caller)

So item 12 is **not** a blocker on the function; it is a *silent-acceptance
hole* in the function (`src/foundry_spec.py:410`, `got = sorted((lo, hi) for
_, lo, hi in spans)` throws the chain away) plus a **wrong argument at the
caller**, which passes `trim.kept_segments` — the target chain only. The fix
direction is "the caller stops lying about what was kept", not "the function
relaxes". Cluster 2, cluster 3 and the risk critique all reached this
independently; I re-ran it rather than take it on trust.

Two measured nuances nobody stated. The chain-blind comparison is only
*reachable* where the wrong chain's residue numbers exist: the obvious
negative-test contig `A29-128,A10-37` is refused by the residue-existence
check first (`residue A10 is not in trimmed.pdb`), so the test that actually
exercises the blindness is the overlap form `A29-128,A29-37`. And that same
form exposes a second defect: `n_target` is `+=` over spans, so it reports
**109 target residues for a 100-residue chain** and 195 tokens for a
186-token complex.

**(b) There are two different `n_tokens`, and the notes' go/no-go names the
one cluster 4 says is wrong.** Measured:

| producer | expression | 4ZGM glue | 4ZGM chain A only |
|---|---|---|---|
| `foundry_spec.py:370` | `binder[1] + n_target` (binder **max** 86) | **214** | 186 |
| `pipeline_runner.py:4796` and `:5102` | `trim.n_residues_after + _binder_midpoint(contig)` (**midpoint** 78) | **206** | 178 |

The notes' `206 / 178` are the **cost-model** numbers and they are the ones
that matter: `foundry_spec`'s `n_tokens` feeds only the `max_complex_tokens`
ceiling and the summary dict, while the pipeline's feeds `plan_campaign`, the
RF3/RFD3 size laws, the disk clamp and `choose_compute()`. Cluster 4's
correction ("reads 214, not 206 … both of the notes' numbers are 8 low")
redirects the go/no-go away from the line that actually under-costs the GPU;
cluster 3's reading is the right one. **Both numbers go in the go/no-go, each
named with its producer** (§5).

Consequence for the notes' §2 table: **item 18 is NOT "DONE, closed by
construction".** It is closed inside `foundry_spec` (verified: `n_target`
iterates `for chain, lo, hi in spans` and summed 128 on the two-chain spec)
and **open** in the cost model, because `trim.n_residues_after` is
`len(keep)` for one chain (`src/structure_trim.py:1612`). Ranked risk #2 is
live, and it lives in `structure_trim`, not in `foundry_spec`.

### 2.2 Line numbers

The notes' §3 table, re-verified today. `handoff.py:108`,
`structure_trim.py:1330`, `foundry_spec.py:120` / `:174` / `:370`,
`pipeline_runner.py:2350` / `:2593` are **exact**. Two have drifted +6:

| symbol | notes say | actually |
|---|---|---|
| `_correct_label_seq_ids` | `pipeline_runner.py:7614` | **`:7620`** |
| `_resolve_unverified_label_seq_ids` | `:7635` | **`:7641`** |

Everything the scope cites for these clusters is stale. Corrected:
`handoff.parse_hotspot_residues` `85-133` → **108–230**;
`_verify_hotspot_grounding` `2268-2362` → **2350–2498**;
`_resolve_unverified_label_seq_ids` `7786-7929` → **7641–7883**;
`build_contig` `structure_trim.py:1164-1174` → **1330–1341**; `TrimResult`
`:130-148` → **150–207**; `_write_mapping` `:1627-1667` → **1853–1897**;
`validate_spec`'s cross-check `foundry_spec.py:309-315` → **409–415**;
item 21's `pipeline_runner.py:1499, 2910-2912` → `_stage_structure_intel` at
**:1481** with the intent ternary at **:1565** (`:2910` is now inside
`_hotspot_numbering_frame` and unrelated). Other symbols you will need:
`_binder_sites` :1294, `_resolve_hotspot_override` :1688,
`_write_override_interface_report` :1776, `_check_ortholog_conservation`
:2049, `_verify_target_chain_assignment` :2171, `_hotspot_numbering_frame`
:2501, `_chain_holding_residues` :2686, `_combine_allowed` :2711,
`_stage_trim` :3791, `_stage_binder_spec` :4097, `_build_label_seq_id_map`
:7572, `_TrimFromDisk` :180, `trim_target` `structure_trim.py:1376`.

### 2.3 Claims that are simply wrong

- **Notes §7: "`regions_declared: 1` is also wrong — the table has two chain
  sub-sections."** It is **right**. `regions_declared` counts `###
  MODEL-READY HOTSPOTS` sections (`handoff.py:150-157`), i.e. independent
  epitope regions, and it drives `_primary_section`'s choice and the
  "designing against X and DROPPING the rest" warning. The 5VAI report has
  one pocket with two chain sub-tables. Counting them as 2 would make
  `_primary_section` pick one chain and drop the other — exactly the
  corruption Stage 3 exists to prevent. Cluster 1 and cluster 4 both caught
  this; verified. **Do not "fix" it.**

- **Notes §1 blocker 4: `_resolve_unverified_label_seq_ids` "silently
  rewrites the second chain's ids to the first chain's frame".** Stale by one
  day. Commit `9965bb6` ("A hotspot that is not in the structure is not 'no
  mismatch'", 2026-09-13) added a residue-NAME guard that leaves such a row
  alone and warns; `tests/test_pipeline_stages.py:1312` and `:1342` pin both
  halves. Item 4 is therefore "the second chain's rows cannot be **resolved**",
  not "are corrupted". The residual corruption risk is narrow and real: where
  the other chain carries the same residue NAME at the same auth id, the name
  check passes and the wrong label is written. The 69/74/75/76 values sitting
  in `div_standard_diabetes`'s report on disk are the **pre-`9965bb6`**
  corruption — verified: chain R's real labels at auth 30/35/36/37 are exactly
  69/74/75/76 — not what today's code would produce.

- **Notes §5: "`test_a_fusion_partner_is_excluded_from_the_design_target` — a
  second chain in the file is excluded from the design target, which is the
  exact assumption a glue inverts."** It is not. That test is
  `_target_accession_residues("5TGZ", "A", "P21554")` — **two accessions on
  one chain** (the GCGR–endolysin crystallisation chimera), accession-level
  and per-chain. A glue does not invert it; a glue whose target chain is a
  fusion construct still needs the endolysin excluded. Do not relax it in the
  belief it is blocking you. The assumption a glue actually inverts lives in
  `_binder_sites`/`_stage_trim`'s validators, `trim_target`'s scalar
  `target_chain`, and `_designable_chain_sizes`.

- **Scope §6 rates item 11 "Risk: Low".** The risk of *changing* it is low;
  the risk of leaving it is not. Measured (cluster 3, re-derivable): with two
  hotspots sharing an author number across chains, `build_rfd3_spec` returns
  2 keys for 3 hotspots — one hotspot silently deleted, the survivor carrying
  the *wrong chain's* atoms — and `validate_spec` then **accepts** it. On
  4ZGM the chains overlap at auth 29–37, so this is live on the Stage-3
  target.

- **Scope §6 has no row for two flat author-number sets inside
  `pipeline_runner.py`**, and both are required in Stage 3, not Stage 6:
  `_stage_binder_spec:4104` (`kept = {a for lo, hi in trim.kept_segments ...}`)
  and `_stage_trim:3906` (`hotspots=hs["residues"]`, both chains, into a
  target-chain `by_auth`). On 4ZGM chain A spans 29–128, so **every** chain-B
  hotspot number falls inside `kept` and survives as though it were chain A.
  Cluster 2 ("item 6b"), cluster 3 ("item 11 companion") and cluster 4 all
  found this separately; it is step 8 below.

- **Scope §7.3 / `binder_metrics`: `hotspots_from_rfd3` "drops
  off-target-chain entries".** It filters on `mapped[0]`, the **destination**
  letter (`src/binder_metrics.py:418`). Under the confirmed RFD3 merge (Stage
  1: 100 `A*` + 28 `B*` keys, *all* mapping into `B`), **nothing is dropped** —
  both chains' hotspots become one flat list. That is cheaper than the scope
  assumed and it means `hotspot_engagement` becomes a single **pooled**
  number on the first glue campaign. Stage 4's problem; recorded here because
  Stage 3 is what makes it reachable.

- **Notes §1: "The project has … no `binder/` directory at all."** True of
  `div_standard_diabetes` and not the general shape. There is a **second**
  archived stabilize run nobody's handover mentions:
  `projects/div_wildcard_tnbc` (6JJW, WWC1/PTPN14, chains A and U). It
  *reached* the binder track — `binder/20_target_intel.md`,
  `binder/21_interface.md`, `binder/29_site_comparison.md` — and died one
  stage later in `trim_target`'s hotspot-present check
  (`structure_trim.py:1429-1435`, `hotspot residues [447, 448, …] are not
  present in chain A`). It fails through grounding's **`absent`** branch where
  5VAI fails through **`mismatches`**, and two of its three copies are
  `21_interface.md`, so it exercises the binder-track path. Use both.

- **A fourth generator-side blocker the notes' list of three omits.**
  `structure_trim.py:1429-1435` refuses any hotspot whose auth id is absent
  from the **target** chain, so `trim_target` dies before `build_contig` is
  ever reached. The suggested order hits it at step 5 and no item owns it.
  It is handled in step 8 below (`_stage_trim` filters to the target chain's
  rows before calling `trim_target`).

### 2.4 Counts every plan should quote correctly

Measured today with `find`/`glob`, not from any report:

| thing | correct | who got it wrong |
|---|---|---|
| `trim_map.json` under `projects/` | **53** (0 under `outputs/`) | cluster 2 says 91 |
| specs under `projects/**/spec/` | **49** | — |
| live stage reports (`02_structure.md` + both `21_interface.md` globs) | **111** (108 carry a `target_chain`) | cluster 1 says 61 |
| files anywhere with a `### MODEL-READY HOTSPOTS` section | **138** | — |
| residues parsed from the 6JJW **site** copy | **16**, not 17 | cluster 1 |
| reports whose `partner_chain` is literally `A`/`B` and ≠ `target_chain` | **65 of 108** | (this is objection 4's premise; see §3.1) |

### 2.5 Two guards nobody's cluster owns

Both come from the risk critique and both are confirmed:

- **`_stage_trim:3875` / `:3884`** (`_infer_membrane_side(..., hs["residues"],
  …)` and `bad = [h for h in hs["residues"] if int(h["auth_seq_id"]) not in
  restrict.allowed_auth]`) pool both chains' rows into a **target-chain**
  author-id set and then `raise PipelineError` naming the *target gene's*
  membrane side. Nobody edits these lines. Cluster 2's `trim_target` guard
  (`if allowed_auth: raise TrimError`) sits two frames later and cannot help.
  **But it is inert for the Stage-3 command as designed**: the whole block is
  `if uniprot and side not in (...)`, and `_stage_structure_intel:1584` writes
  `target_uniprot: (uniprot or "").upper()`, so `--workflow structure` without
  `--uniprot` skips it entirely. It fires the moment `--uniprot` is passed, or
  on the PPI-bridged 5VAI path. Step 10.

- **`_check_ortholog_conservation` (`:2049`)** reads `data["target_chain"]`
  for **all** rows (`:2081-2086`) and hard-raises below
  `MIN_HOTSPOT_CONSERVATION` (`:2131`). It is called at `:1933` and `:6696`,
  right after grounding, and is active whenever `self._ortholog` was set —
  which a stabilize run reaches, because both chains are named. Once step 3
  stops grounding from refusing a cross-chain table, this is the next guard to
  score the partner's residues against the *target's* human ortholog
  alignment. CLAUDE.md already records one incident of this exact shape
  (`fraction_conserved: 0.625` over six nonexistent residues). Step 10.

---

## 3. The work plan

### 3.0 Before you start

    # confirm the fixtures (data/ is gitignored; on a fresh checkout, fetch)
    ls data/structures/{4ZGM,5VAI,6JJW}_ba1.cif
    # if absent:
    .venv/bin/python -c "from pathlib import Path; from src.target_resolve import ensure_assembly; \
        [print(ensure_assembly(i, Path('data/structures'))) for i in ('4ZGM','5VAI','6JJW')]"

    # the wall, before touching anything — see §4 for today's measured green
    .venv/bin/python -m pytest tests/test_structure_trim.py tests/test_foundry.py \
        tests/test_audit_fixes.py tests/test_binder_metrics.py -q

Every 4ZGM/5VAI/6JJW test below carries
`@pytest.mark.skipif(not path.exists())`, like the 3KYS ones.

### 3.1 The ordering decision, and the one design choice that is mine

**Order shipped here:** 1 → 2 → 3 → 4 → **21a** → 5 → 6 → 11+6b → 12+18 →
**(membrane + ortholog)** → 21b.

The notes' order is 1 → 3 → 4 → 5 → 11 → 12 → 6 → 2 → 21. Three deviations,
each for a concrete reason a recon report supplied:

- **2 moves from near-last to second.** Item 1's SKILL.md↔parser contract test
  extracts the STABILIZE template out of `SKILL.md` and asserts it parses as
  two chains. Written later, the parser ships for a week against a template it
  does not match. The zip repackage must ride the same commit or
  `tests/test_release_fixes.py::test_packaged_skill_zips_match_their_source`
  goes red for a reason unrelated to glue.
- **6 moves BEFORE 11 and 12.** `kept_by_chain` is the argument both take:
  step 8's `_stage_binder_spec` filter needs it to key on `(chain, auth)`, and
  step 9's cross-check needs it to compare chains. The notes' order builds the
  spec before the trim can describe two chains, so 11's fix has nothing
  correct to filter against and 12 has nothing to cross-check. Cluster 2 and
  cluster 3 found this independently.
- **21 splits, and its first half goes fifth.** Cluster 4's argument stands:
  item 21 last means there is **no way to ask for a glue run without an LLM**,
  so steps 6–9 have no end-to-end proving command and can only be exercised
  against the two archived reports. 21a (flag, resolver, refusals, two-chain
  `--hotspots`, the override report writer) is independent of items 1–12 and
  gives every later step a runnable command. 21b (the glue budget refusal,
  `_note_glue_guards`, docs) stays last because it describes behaviour the
  earlier steps create.

A tenth step exists that is in neither document: the membrane and ortholog
guards from §2.5.

**The chain-anchor contract — my decision, and it settles a conflict between
two recon reports.** Cluster 1 froze

    _CHAIN_HEADING = r"^\s*(?:Target\s+chain|Chain)\s+([A-Za-z0-9]{1,4})\b"

while cluster 4 specified `Chain: <id>` as the anchor its report writer would
emit. **These do not match** — measured: `'Chain: A'` → `[]`, `'Chain A'` →
`['A']`. A glue `--hotspots` override would have written a report whose rows
all fell back to `target_chain`, which is the bug the cluster exists to fix.
I take **cluster 1's regex** and require the writer to emit `Chain <id> — …`
with no colon, because that form already matches every real heading on disk:
`'Chain A (GLP-1R) periinterface patch'` → `A`, `'Chain B / U (PTPN14)'` →
`B`, `'Target chain A — Region 1: operator-specified'` → `A`. Keeping a
legacy-compatible anchor is worth more than a tidier new one, because
`_correct_label_seq_ids` re-parses old reports off disk on every
`--start-from trim`.

**And the precedence hazard both clusters left open is not worth solving —
it is worth making unreachable.** The risk critique's objection 4 is correct
on its premise: `_resolve_heading_chain` tries the literal reading before the
positional one, and **65 of 108** shipped reports have `partner_chain`
literally `A` or `B` and different from `target_chain`, so a legacy positional
`Chain A` heading on one of those would resolve *literally to the partner*.
Cluster 1's mitigation ("no such case exists among the 138 reports") answers a
different shape. The fix is not a better precedence rule. Measured today, per
**section** rather than per document:

    live stage reports:  111 files, 162 hotspot sections,
                           4 sections with >=2 distinct chain headings
    ...and those four are exactly the four glue sections
    (div_standard_diabetes/02_structure.md, and div_wildcard_tnbc x3)

So: **`chain_blocks` splits only when the CHOSEN section carries two or more
distinct chain-heading tokens.** One heading, or none, returns a single block
under `default` and never consults the precedence rule at all. Objection 4
becomes unreachable rather than mitigated, and the single-chain path is
byte-identical by construction instead of by survey.

That discriminator also survives a case no recon report found and which would
have broken a per-**document** rule:
`projects/gem_vegf_a/runs/round-1/binder/sites/vegfa_flt1_primary/binder/21_interface.md`
is a **disrupt** report with two regions in two sections, headed `Chain W`
and `Chain V`, against `target_chain: W` / `partner_chain: X` — two chain
headings in one file, one per section, and `V` is neither declared chain. Per
section it is one heading, so it stays on the default and nothing moves.

---

### Step 1 — item 1: the parser attributes each row to its own chain

**File/symbols:** `src/handoff.py` — new `_CHAIN_HEADING`, `_declared_chains`,
`_resolve_heading_chain`, `chain_blocks` (public); `_REGION_LABEL` (:72),
`_PRIMARY_REGION` (:75), `_primary_section` (:92), `parse_hotspot_residues`
(:108).

**Edit.** Add the four helpers after `_ROW` (:79-80), taking cluster 1's
bodies with **one change**: `chain_blocks` returns `[(default, section)]
unless `len({m.group(1).upper() for m in marks}) > 1`. `chain_blocks` is
public because step 4 must split on the identical rule — the `handoff.py`
pairing in CLAUDE.md's "Common file pairs" is exactly this: the parse logic
lives once.

In `parse_hotspot_residues`, iterate `chain_blocks(chosen, handoff,
target_chain or "A")` instead of the bare section; change the dedup key from
`(residue, auth_id_int)` to `(chain, residue, auth_id_int)`; add
`"chain": chain` to each residue dict; add one key to the returned JSON,
`target_chains: list[str]` in first-appearance order of the chains that
actually carry rows. `target_chain`/`partner_chain` keep their exact current
meaning. A truthy `target_chain` absent from that order logs a warning and
does not raise — `_verify_hotspot_grounding` owns the refusal.

Separately, a glue pocket is a region: `_REGION_LABEL` and `_PRIMARY_REGION`
gain a `(?:Region|Glue\s+Pocket)` alternative, and `_REGION_LABEL`'s negated
class gains `\]` (without it `[STABILIZE — Glue Pocket 1]` yields the label
`Glue Pocket 1]`). `_primary_section`'s inner `rf"Region\s*{n}\b"` gains the
same alternative. This is a defect the notes miss entirely: today a report
saying `Primary target: Glue Pocket 2` silently returns Pocket 1's residues
with `region: ""`. Latent on all four real glue reports (each has one
section), live on the first multi-pocket run.

**How the single-chain path stays byte-identical.** A section with fewer than
two distinct chain headings yields one block under `default`, so the dedup key
is a constant-prefix relabelling of the old one and collapses identically;
`target_chains == [target_chain]`; the extra `chain` key is additive and every
consumer ignores unknown keys (`build_rfd3_spec` reads only
residue/auth/atoms; `binder_report._load_hotspots`, `ppi_report`,
`scripts/benchmark_trimming.py` take `.get("residues")` wholesale). Measured:
**4 of 162** live sections carry ≥2 headings and all four are the glue
reports.

**Proof (no GPU, no API, no network):**

    .venv/bin/python -m pytest tests/test_glue_hotspot_parsing.py \
        tests/test_hotspot_regions.py tests/test_binder_report.py \
        tests/test_ppi_report.py -q

**Estimate: 5 h.**

---

### Step 2 — item 2: the SKILL.md template, and the handoff field

**File/symbols:** `skills/complex-structure-analysis/SKILL.md` — the STABILIZE
MODEL-READY HOTSPOTS template (859–890), its preamble (861–863), the two
cross-references at :603 and :653-654, and the PIPELINE HANDOFF template
(893–909). Then `skills/complex-structure-analysis.zip`.

**Edit.** Three things.

1. Both sub-table headings carry the chain's **literal id**, in the form the
   frozen regex matches: `Chain <chain_a_id> (<ProteinA>) periinterface patch
   — selected <M> residues:`. One sentence above the fence says the
   orchestrator attributes every row that follows a `Chain <id>` line to that
   chain and grounds it against that chain's own residues, and that the
   positional letter is never the answer.
2. The 4-column row shape is **verbatim unchanged**. A leading `| Chain |`
   column would break both `handoff._ROW` (`handoff.py:79`) and
   `_resolve_unverified_label_seq_ids`'s row regex (`pipeline_runner.py:7700`)
   against all 111 shipped reports — and `_correct_label_seq_ids` re-parses
   those on every resume, so this is a live resume property, not history.
3. The handoff gains one bullet after `- partner_chain:`:

       - target_chains: <comma-separated chain IDs that carry MODEL-READY
         HOTSPOTS — one id in DISRUPT / INHIBIT_ACTIVE_SITE mode (same as
         target_chain), BOTH ids in STABILIZE mode, e.g. "R, P".>

   `parse_handoff`'s regex accepts it unchanged, nothing validates the key
   set, and `_declared_chains` reads it first — so it is the authoritative
   disambiguator for every new run, and the positional map is left as a
   legacy-compat path. `_bridge_ppi_to_binder_track` copies `02_structure.md`
   verbatim, so the field crosses the bridge for free.

Then regenerate: `.venv/bin/python scripts/package_skills.py`. Never
hand-edit the zip.

**How the single-chain path stays byte-identical.** The DISRUPT (815–828) and
INHIBIT_ACTIVE_SITE (838–851) templates are not touched, so every
non-stabilize run emits the identical prompt and the identical table.

**Proof:**

    .venv/bin/python scripts/package_skills.py
    .venv/bin/python -m pytest tests/test_release_fixes.py \
        tests/test_glue_hotspot_parsing.py::test_the_skill_template_parses_as_two_chains -q

(Note the ordering: cluster 1's `checkable_by` runs `test_release_fixes.py`
*before* repackaging, which fails on the zip-parity test and reads as a false
negative. Repackage first.)

**Estimate: 2.25 h.**

---

### Step 3 — item 3: grounding looks each row up on its own chain

**File/symbol:** `src/pipeline_runner.py:2350` `_verify_hotspot_grounding`
(body 2350–2498), and the `_hotspot_numbering_frame` call at :2498.

**Edit.** Signature unchanged. After the early exit, group:

    by_chain: dict[str, list[dict]] = {}
    for h in residues:
        by_chain.setdefault(str(h.get("chain") or chain), []).append(h)

Run the existing per-chain body once per key, in insertion order, accumulating
`mismatches` and `absent` as `(chain, text)` and raising **once** at the end.
`get_sequence_map(path, c)` per chain; the fail-open warnings keep their
wording with `{c}` substituted. The empty-`by_auth` refusal keeps its exact
current text when `c == chain` (so `tests/test_pipeline_stages.py`'s 3FLN
`match="does not exist in"` and the message operators know are untouched) and
says "a hotspot chain" when it is not. `_chain_holding_residues(path,
absent_for_c, exclude=c)` takes the row's **own** chain as `exclude`. Both
error strings gain `on chain {c}` per entry only when more than one chain is
involved. When `len(by_chain) > 1`, `logger.info` the split.

`_hotspot_numbering_frame` is called once per chain, with the accession
passed **only** for the chain the caller resolved:

    for c, rows in by_chain.items():
        self._hotspot_numbering_frame(rows, c, pdb_id, uniprot if c == chain else None)

It returns immediately on `not uniprot`, so the second chain is silent rather
than wrong. Today all 7 of 5VAI's rows would be translated through chain R's
UniProt alignment and four printed with a canonical id belonging to a
different protein. Say so in the docstring.

**How the single-chain path stays byte-identical.** A residue dict with no
`chain` key groups under `data["target_chain"]`, so the function collapses to
one pass and every message is produced verbatim. This is what keeps
`test_a_hotspot_that_is_not_in_the_structure_fails_the_guard`,
`test_the_guard_names_the_chain_the_residues_are_actually_on` and
`test_a_target_chain_that_does_not_exist_fails_the_guard` green **unchanged** —
all three build their JSON by hand with no `chain` key. The cross-chain
refusal is preserved for exactly the shape that has not said which chain it
means, and relaxed only for a table that attributed every row.
`tests/test_hotspot_numbering_frame.py:184` asserts by source inspection that
the frame check is invoked from here and contains no `raise`; both hold.

**Reword, do not delete:** `test_the_guard_names_the_chain_the_residues_are_
actually_on`'s docstring ("no stage supports a hotspot set spanning two
chains") becomes false the moment this ships. It must say "a chain-LESS set
spanning two chains", in the same commit.

**Proof:**

    .venv/bin/python -m pytest tests/test_pipeline_stages.py \
        tests/test_hotspot_numbering_frame.py tests/test_glue_grounding.py -q

Ground truth for the new tests, measured today with gemmi:

    5VAI R: 66->(105,PHE) 67->(106,ASP) 70->(109,ALA)
    5VAI P: 30->(24,ALA) 35->(29,GLY) 36->(30,ARG) 37->(31,GLY)
    (on chain R, auth 30/35/36/37 are VAL/THR/VAL/GLN — the mismatch branch)
    6JJW U: 447..453 -> label 24..30    (6JJW A carries none of them)

**Estimate: 2.5 h.**

---

### Step 4 — item 4: per-chain label_seq maps, and the `binding:` line

**File/symbols:** `src/pipeline_runner.py:7620` `_correct_label_seq_ids`
(+ its two call sites, :1910 and :6654) and `:7641`
`_resolve_unverified_label_seq_ids` (body 7641–7883, `_section_sub` at
7869–7906).

**Edit.** Both gain a keyword-only `handoff: dict[str, str] | None = None`,
forwarded; the two call sites pass the `handoff` already bound at :1908 and
:6614. Inside `_resolve_unverified_label_seq_ids`:

1. Build a chain index over character offsets, restricted to `###
   MODEL-READY HOTSPOTS` sections via `chain_blocks` (step 1's function, not
   a second copy). Every offset outside such a section maps to
   `target_chain`, which is today's behaviour exactly. **Write the
   restriction as a comment as well as code** — the `Chain A: Hydrophobic:
   …` line in the COMPLEX OVERVIEW section matches `_CHAIN_HEADING`, and
   dropping the restriction later would start creating blocks over the whole
   document.
2. `_map_for(c)` builds and caches one `_build_label_seq_id_map` per chain
   that carries rows; `_row_sub` calls `_map_for(_chain_at(match.start()))`.
   Same for the BoltzGen `boltzgen_residue_indices` fallback. The existing
   "cannot build the auth->label map for chain {target_chain}" PipelineError
   keeps firing on `target_chain`; raise the same error naming `{c}` when a
   non-default chain that carries rows has an empty map.
3. Every warning that hardcodes `chain {target_chain}` (:7728, :7739) becomes
   `chain {c}`.

**4. `_section_sub` — and this is where the risk critique killed cluster 1's
design, correctly.** Cluster 1 proposed running `_section_sub` per
`chain_blocks` block with a widened line pattern. Measured today on the real
5VAI report, that destroys the very lines it aims to fix:

    section_pat span = 1442 chars, 6 chain headings
    _SEC (hotspot section) = 526 chars, 2 chain headings
    blocks carrying a binding: line -> local_residues == []  (all four of them)

The `binding:` lines live in a `#### BoltzGen binding` subsection that
`section_pat` (`:7857`, terminated by `\n### MODEL.READY HOTSPOTS|\n## `)
includes but that carries no table rows. Split per block, every
binding-carrying block has zero local residues and takes the `:7876-7887`
branch that **replaces** the line with `_LABEL_SEQ_UNAVAILABLE` — deleting
`Chain B binding: 24,29,30,31`, the value cluster 1 itself cites as ground
truth. Today's code is inert here only because its pattern is `^\s*binding:`
and the real lines start with `Chain`.

**So: do not widen the pattern by block.** Instead, when the section's table
carries rows on more than one chain, collect the label ids **per chain from
the table** and rewrite each `Chain <id> binding:` / `Chain <id> only:
binding:` line from that chain's own list, matching the line by its own chain
token rather than by position:

    r"^[ \t]*Chain\s+(\S+)\s+(?:only:\s*)?binding:\s*[^\n]+$"

A bare `^\s*binding:` line inside a multi-chain section keeps today's
behaviour (pooled, which is what it means when nothing says otherwise) and
gets a warning. When the table is single-chain, run the existing body
verbatim.

The trailing global `UNVERIFIED_(\d+)` substitution (:7913-7917) stays on the
default chain's map: those tokens carry no chain, `--hotspots` writes
`**UNVERIFIED**` not `UNVERIFIED_NNN`, and no report on disk contains the
token. Say so in the docstring.

**How the single-chain path stays byte-identical.** With `handoff=None` —
every existing caller and every existing test — `chain_blocks` gets an empty
handoff and a section with <2 headings, so the index collapses to one chain
and the function is unchanged. `test_a_label_seq_id_is_not_rewritten_from_
another_residues_map` (:1312) and `test_a_correct_row_is_still_corrected`
(:1342) pass hand-built tables with no heading; the 5GN0 `binding:
54,56,60,63` golden (`tests/test_ortholog_check.py:745`) is single-chain and
takes the unchanged branch.

**Proof:**

    .venv/bin/python -m pytest tests/test_ortholog_check.py \
        tests/test_pipeline_stages.py tests/test_glue_label_seq.py -q

**Estimate: 4.75 h** (cluster 1's 3.25 h plus the `_section_sub` redesign).

---

### Step 5 — item 21a: make a glue run askable, with no LLM

**Files/symbols:** `src/pipeline_runner.py` — new module-level
`_DESIGN_INTENTS`, `is_glue_intent`, `waives_partner_chain` (beside
`chain_id_or_blank` :253); `PipelineRunner.__init__` (:430-449, :625);
new `_refuse_unbuilt_glue_paths` (beside `_refuse_undispatched_site_trials`);
new `_resolve_design_intent` (beside `_resolve_modality` :1212);
`_stage_structure_intel` (:1481, intent at :1565);
`_resolve_hotspot_override` (:1688, refusal at :1719-1724);
`_write_override_interface_report` (:1776); `_stage_binder_interface`
(:1822, the `--hotspots` branch); `_binder_sites` (:1327); `_stage_trim`
(:3830). Plus `scripts/run_pipeline.py` `build_parser` and `main`.

**Edit.** Take cluster 4's design as written, with the anchor correction from
§3.1 and one addition:

- `--design-intent {disrupt,stabilize,inhibit_active_site}`, default None,
  `--workflow structure` only (the CLI refuses it elsewhere with a message
  naming the working alternative — same posture as `--hotspots` on the PPI
  track). Validated in `__init__` like `modality`.
- `_resolve_design_intent(measured, *, source, has_partner)` — the
  `_resolve_modality` pattern: returns `measured` unchanged and logs nothing
  when the flag is absent; refuses `stabilize` with one chain. Called only
  from `_stage_structure_intel`, whose `design_intent = "disrupt" if
  partner_chain else "inhibit_active_site"` ternary at :1565 becomes
  `measured` + a resolve.
- `_refuse_unbuilt_glue_paths(intent, *, source)` — self-contained, like
  `_refuse_undispatched_site_trials`, refusing glue on `boltzgen`, on
  `cyclic_peptide`, on `--trial-sites > 1`, and on `--workflow ppi|binder`,
  each naming its scope item. Called from three places: end of `__init__`
  (flag-level), `run()` right after `_stage_pathway` (**as its own `if
  start_idx <= 1:` block, placed before the existing `if start_idx <= 1 and
  result.pdb_id:` at :843** — a stabilize handoff with no `pdb_id` must not
  slip past), and `_run_binder_track` after `intel = H["target_intel"]`.
  Read the intent from the **literature** handoff first, then the pathway's:
  `_bridge_ppi_to_binder_track:3682` and `_stage_structure:6581` both prefer
  the literature value, and refusing on a pathway intent the run was never
  going to use would be wrong. (Inert today — both archived stabilize runs
  agree across stages — but the precedence mismatch is real.) This refusal is
  a strict improvement: `div_standard_diabetes` spent three LLM stages and
  then died at grounding blaming "textbook/literature numbering".
- `_resolve_hotspot_override(spec, pdb_id, target_chain, *, partner_chain="")`
  — `allowed = {target_chain} | ({partner_chain} if partner_chain else set())`,
  a per-chain residue map, and **every** returned dict gains `"chain"`,
  including the single-chain path (`_check_hotspot_atoms_are_buildable:2654`
  already reads `h.get("chain") or chain`). Non-glue callers pass nothing and
  get the identical refusal string.
- `_write_override_interface_report` gains a two-chain form: one 4-column
  table per chain, each preceded by `Chain <id> — Region 1: operator-specified
  — selected N residues:` (**no colon after `Chain`** — §3.1), and a
  `select_hotspots` block prefixing each residue with its **own** chain
  (`A113: NZ,CE` / `B30: CB,CA`). Cluster 4's writer still builds `picks` as
  `f"{target_chain}{r['auth_seq_id']}"` (:1798-1800); that must read
  `r["chain"]`. Gate the whole new form on `len({r["chain"] for r in
  residues}) > 1` so the single-chain output is byte-identical.
- `waives_partner_chain(intel)` replaces the inline predicate at
  `_binder_sites:1327` (behaviour identical — `stabilize` already requires a
  partner there, and `tests/test_single_target_mode.py::test_only_single_
  target_intent_waives_the_partner` pins it), and is **added** to
  `_stage_trim` before the `if not partner:` at :3830, where no intent check
  exists at all today: a `disrupt` or `stabilize` run whose partner went
  missing currently degrades silently to single-target mode.

**How the single-chain path stays byte-identical.** Every new predicate is
gated on `is_glue_intent` or on an absent flag. `_resolve_design_intent`
returns `measured` and logs nothing without `--design-intent`.
`_write_override_interface_report`'s gate is on the residues it was handed.
The `_stage_trim` refusal is measured-unreachable for every shipped run: over
all 119 stage handoffs on disk, **every** blank/`none`-partner case carries
`inhibit_active_site`, which the predicate waives, and all 72
`20_target_intel.md` handoffs carry a `design_intent` at all. It is a
behaviour change in principle on the disrupt path, and that change is the
rule CLAUDE.md already states.

**Proof:**

    .venv/bin/python scripts/run_pipeline.py --help | grep -A6 -- --design-intent
    .venv/bin/python -m pytest tests/test_glue_branch.py tests/test_hotspot_override.py \
        tests/test_single_target_mode.py tests/test_structure_first_track.py \
        tests/test_boltzgen_backend.py tests/test_ppi_backend_routing.py \
        tests/test_docs_match_code.py -q
    # and the survey that proves the _stage_trim refusal is unreachable today:
    .venv/bin/python -c "import glob;from pathlib import Path;from src.handoff import parse_handoff;\
from src.pipeline_runner import chain_id_or_blank, waives_partner_chain;\
print([p for p in glob.glob('projects/*/runs/*/**/21_interface.md',recursive=True)+glob.glob('projects/*/runs/*/02_structure.md') \
if (h:=parse_handoff(Path(p).read_text(errors='replace'))) and not chain_id_or_blank(h.get('partner_chain')) \
and h.get('target_chain') and not waives_partner_chain(h)])"   # -> []

**Estimate: 7.5 h.**

---

### Step 6 — item 5: `build_contig` over multiple chains

**File/symbol:** `src/structure_trim.py:1330` `build_contig`, plus the single
production call inside the `TrimResult(...)` constructor at :1610.

**Edit.** Add

    def build_contig_multi(kept_by_chain, binder_min, binder_max) -> str

joining `f"{c}{lo}-{hi}"` over `kept_by_chain.items()` behind one
`{binder_min}-{binder_max},/0,`. Reduce `build_contig(segments, chain,
binder_min, binder_max)` to `build_contig_multi({chain: segments}, ...)`,
keeping its signature and name. No empty-spans raise — `parse_contig` already
raises "contig names no target span" and `trim_target` raises "trim planning
kept no residues" first.

Two facts the docstring must carry, because both are load-bearing and neither
is obvious. **There is exactly ONE `/0`**: it is RFD3's chain-INCREMENT token,
and every span after it merges into one output target chain (Stage 1's
measured 4ZGM result: 2 output chains, binder A + target B numbered 1..128).
And **mapping order is load-bearing**: it fixes the output 1..N numbering that
`diffused_index_map` and `binder_metrics` read, and
`pipeline_runner._run_cluster_stage:4165` takes `spans[0][0]` as the target
chain — so the primary target chain must be inserted first. A `dict` preserves
insertion order in CPython, but nothing in the code says so; assert it in the
test.

**How the single-chain path stays byte-identical.** A single-key mapping
produces the same join in the same order, so both existing assertions in
`test_build_contig_shape` hold literally. Proven over the corpus: all **53**
shipped `trim_map.json` contigs parse to exactly `{target_chain:
kept_segments}`.

**Proof:**

    .venv/bin/python -m pytest tests/test_structure_trim.py -q -k \
      "build_contig or contig_matches or removes_nothing or trims_tead1 or exposure_guard_compares"

**Estimate: 1.5 h.**

---

### Step 7 — item 6: `kept_by_chain` on `TrimResult`, `trim_map.json`, `_TrimFromDisk`

**Files/symbols:** `src/structure_trim.py` — `TrimResult` (150–207),
`trim_target` (1376; hotspot check 1429-1435, `plan_trim` call 1443-1446,
retained/lost 1494-1495, `TrimResult(...)` 1605-1631, counts 1611-1613),
`_write_mapping` (1853–1897); `src/pipeline_runner.py:180` `_TrimFromDisk`.

**Edit.** Take cluster 2's design, which is the most carefully bounded of the
four.

- `TrimResult` gains `kept_by_chain: dict[str, list[tuple[int,int]]] =
  field(default_factory=dict)` as the **first defaulted field**, immediately
  after `hotspots_lost` (:161). The comment must say all three things:
  it is the complete set of author-numbered spans the contig names, primary
  chain first; `kept_segments` is **not** deprecated and **not** a flattening
  of it (it keeps its exact current meaning, the primary target chain's
  spans); and a consumer that means "everything RFD3 is conditioned on" must
  read the new field, because `(lo, hi)` pairs from two chains share one
  author-number space and cannot be distinguished once flattened.
- `trim_target` gains keyword-only `co_target_chains: Sequence[str] = ()` and
  **four refusals**, each naming its deferred scope item so the line reads as
  deliberate: the co-target must be `partner_chain` (the only other chain
  `write_trimmed`'s `keep_map` puts in the file — item 7, Stage 6);
  `allowed_auth` must be None (a bare `set[int]` of target-chain author ids,
  meaningless on a second chain — item 14, Stage 6); the trim must be a
  **no-op** (`len(keep) == len(residues)`, checked after `plan_trim`) — this
  is the one guard that makes every remaining single-chain measurement in the
  function trivially correct; and the co chain must have polymer residues in
  the written output.
- Chain-aware hotspot routing: a module-level `_row_chain(h, default)`; a
  `hotspots_target` list that **is the same object** when `co` is empty
  (identity, not a copy — this is what keeps `_exposed_hydrophobic`'s
  10 Å clearance list bit-identical); `kept_by_chain` built by reading the
  co chain's residues back out of the file the trim just wrote; and
  `retained`/`lost` keyed on `(chain, auth)` pairs rather than a bare
  `set[int]`. On 4ZGM, chain-B hotspots at auth 29/36 fall inside chain A's
  kept range 29-128 and would otherwise be recorded `retained: True` against
  the wrong molecule, while B26 would raise `TrimError: trim lost hotspot(s)
  [26]`.
- The three counts are redefined over `kept_by_chain`:
  `n_residues_after`, `n_residues_before`, `n_segments`. **This is the open
  half of item 18** (§2.1b). The comment must carry the numbers: the two cost
  sites `pipeline_runner.py:4796` and `:5102` compute `trim.n_residues_after
  + _binder_midpoint(trim.contig)`, so target-chain-only gives 100+78 = 178
  against the true 128+78 = 206 — a 22% under-count on a law with exponent
  2.56. And `n_segments` feeds `write_campaign_driver`'s `max_cb`: a glue
  contig's two spans are two physically separate molecules, so a hardcoded 1
  rejects every design.
- `_write_mapping` keys its `retained` flag on the same `(chain, auth)` pair
  set, with an explicit fallback to `{target_chain: result.kept_segments}`
  when `kept_by_chain` is empty (three things assert `all(h["retained"])` —
  `tests/test_structure_trim.py:199`, `tests/test_binder_report.py:75`,
  `scripts/test_e2e_binder.py:228` — and a fallback to `{}` turns all of them
  False at once), and adds one payload key, `kept_by_chain`. No separate
  `co_target_chains` key: `[c for c in kept_by_chain if c != target_chain]`
  is the same information with no second source to disagree.
- `_TrimFromDisk` reads `kept_by_chain` back, reconstructing
  `{target_chain: kept_segments}` when the key is absent — every
  `trim_map.json` ever written — and both count fallbacks derive from it.
  **Keep the `.get(k, default)` form for `n_segments`, not `or`**, so a
  stored 0 stays 0.

  One trap both cluster 2 and cluster 3 half-saw and neither resolved:
  `tests/test_audit_fixes.py:1066` constructs `_TrimFromDisk` from
  `{"kept_segments": [[27,110]]}` with **no `target_chain`**, so the
  reconstruction key is `""`. Cluster 2 calls that harmless; cluster 3's
  `trim_cross_check` (step 9) does `if by_chain:` and `{"": [...]}` is
  truthy, which would send `want = {"": [(27,110)]}` against `got = {"A":
  ...}` and refuse in front of a GPU launch. **Reconstruct only when
  `self.target_chain` is truthy**, and leave `kept_by_chain` empty otherwise.
  All 53 shipped `trim_map.json` carry `target_chain`, so nothing archived
  changes — but the fallback must be safe by construction, not by luck.

**How the single-chain path stays byte-identical.** With `co == ()`,
`hotspots_target is hotspots`, `kept_by_chain` has one key, `(target_chain,
a) in kept_pairs` is exactly `a in kept_set`, and all three counts reduce to
`len(keep)` / `len(residues)` / `len(segments)`. Proven two ways: the existing
literal assertions (`n_residues_after == 208`, `before == after`,
`n_segments == 1`), and a before/after `trim_map.json` diff over the three R2
structures.

**Proof:**

    # capture goldens BEFORE the edit, diff AFTER (7CZD B/A, 6VJJ A/B, 3KYS A/B,
    # budget=220, max_exposed_hydrophobic=None), minus the new key:
    diff /tmp/trim_before.json /tmp/trim_after.json      # must be empty
    .venv/bin/python -m pytest tests/test_structure_trim.py tests/test_binder_report.py \
        tests/test_audit_fixes.py -q
    # the corpus wall — run today, prints "53 trim_maps, 0 mismatches":
    .venv/bin/python - <<'EOF'
    import json, glob
    from src.foundry_spec import parse_contig
    n = bad = 0
    for p in sorted(glob.glob('projects/**/trim_map.json', recursive=True)):
        m = json.load(open(p)); n += 1
        _, s = parse_contig(m['contig'])
        want = sorted((m['target_chain'], lo, hi) for lo, hi in m['kept_segments'])
        bad += (sorted(s) != want)
    print(n, 'trim_maps,', bad, 'mismatches')
    EOF

**Estimate: 7.5 h.**

---

### Step 8 — items 11 + 6b: the hotspot's own chain reaches the spec

**These three edits land in ONE commit.** Cluster 2 names this "the likeliest
merge accident in Stage 3, and the failure is quiet", and it is worse than
that: step 3 removes grounding's *accidental* protection against a cross-chain
table, so if item 11 lands without the two filters, partner hotspots are
stamped onto the target chain and `validate_spec` confirms they are real
residues with real atoms. Write
`test_a_partner_hotspot_is_never_stamped_onto_the_target_chain` **first**.

**Files/symbols:** `src/foundry_spec.py:65-140` `build_rfd3_spec` (the defect
at :120) and `RFD3Spec`; `src/pipeline_runner.py:4104` `_stage_binder_spec`'s
`kept` filter; `src/pipeline_runner.py:3906` `_stage_trim`'s `hotspots=`
argument.

**Edit.**

1. `build_rfd3_spec`: signature unchanged. Replace
   `select[_hotspot_key(target_chain, auth)] = atoms` with a per-row
   `chain = str(h.get("chain") or "").strip() or target_chain`, a
   one-letter-alpha validation that raises `SpecError`, and a
   `logger.warning` (**not** a raise) on a duplicate key with different
   atoms, keeping the overwrite so the written file is unchanged. Do **not**
   parse the contig here — `validate_spec:404` already refuses a hotspot
   whose chain has no contig span, and adding a parse executes new code on
   the single-chain path. `RFD3Spec` gains a trailing
   `target_chains: list[str] = field(default_factory=list)`, populated as
   `sorted({k[0] for k in select})`.
2. `_stage_binder_spec`: replace the flat `kept` set with a `(chain, auth)`
   membership test against `trim.kept_by_chain`, falling back to
   `{hs["target_chain"]: kept}` when the attribute is absent (a
   `_TrimFromDisk` rebuilt from a pre-step-7 `trim_map.json`). Measured
   consequence of skipping this: on 4ZGM, B26 is dropped and B29/B36 survive
   as though they were chain A, because chain A spans 29–128.
3. `_stage_trim`: pass `hotspots=[h for h in hs["residues"] if
   (h.get("chain") or hs["target_chain"]) == hs["target_chain"]]` to
   `trim_target`. This is the fourth generator-side blocker from §2.3 — the
   trim cuts the target chain and has no business force-keeping a partner
   residue number that happens to collide.

**How the single-chain path stays byte-identical.** Every existing hotspot
producer emits no `chain` key (`handoff.parse_hotspot_residues` before step 1,
`scripts/benchmark_trim.py:1437`, `tests/test_foundry.py:77`), so `chain`
falls back to `target_chain`; `kept_by_chain` is `{target_chain: ...}`, making
the pair test identical to the flat one; and the `_stage_trim` filter is an
identity operation. The proof is the **49-spec field-for-field
re-derivation**, not the single CD79b golden.

**Proof:**

    .venv/bin/python -m pytest tests/test_foundry.py tests/test_complex_token_ceiling.py \
        tests/test_structure_trim.py tests/test_audit_fixes.py tests/test_glue_spec.py -q

Two errors in cluster 3's `checkable_by` that must not be copied: it names
`tests/test_pipeline_runner.py` and `tests/test_binder_track.py`, **neither of
which exists** (pytest exits on a usage error and asserts nothing — use
`tests/test_pipeline_stages.py`); and its re-derivation harness writes
`target_chain=next(iter(hotspots))[0]` where `hotspots` is a list of dicts,
which is a `KeyError` — it needs `next(iter(entry["select_hotspots"]))[0]`.
That harness also feeds chain-less dicts, so it proves the **default** is
unchanged and is not by itself a test of the new branch; the glue assertions
below are.

**Estimate: 4.5 h.**

---

### Step 9 — items 12 + 18: the cross-check learns the chain, and the trim and spec must agree

**Files/symbols:** `src/foundry_spec.py:174` `validate_spec` (signature, the
`n_target` accumulation at :340, the cross-check at :409-415, the summary at
:417-424); new module-level `trim_cross_check`; `src/foundry_runner.py:868`
/`:891` `run_design`; `src/pipeline_runner.py:4806` and `:5962`.

**Edit.**

1. `validate_spec` gains `kept_by_chain: Mapping[str, Sequence[tuple[int,int]]]
   | None = None` **beside** `kept_segments`, refusing both at once. The
   existing `kept_segments` body moves under an `elif`, character for
   character. The new branch compares `{chain: sorted(spans)}` dicts. The
   summary gains `"target_chains"`. **Watch the `elif` edit** — dropping the
   `is not None` would make an empty list skip the check, where today `[]`
   refuses any non-empty contig; keep that.
2. `n_target` becomes a set union over `(chain, auth)` rather than `+=` over
   spans. Measured today: the overlap contig `A29-128,A29-37` reports **109
   target residues for a 100-residue chain** and 195 tokens for a 186-token
   complex. Identical on all 49 archived specs (disjoint spans) — verify by
   diffing `n_target_residues` before and after over all 49.
3. `trim_cross_check(trim)` — one helper so three call sites cannot drift,
   returning `{"kept_by_chain": ...}` when the trim has a **non-empty** one
   and `{"kept_segments": ...}` otherwise. Rewire
   `pipeline_runner.py:4806` (via `run_design`, which gains the kwarg),
   `pipeline_runner.py:5962` (`_prepared_site` — today it would log "unusable;
   rebuilding it" on a glue site and re-pay for the LLM stages), and
   `foundry_runner.py:891`. Leave `scripts/test_e2e_binder.py:173` and
   `scripts/benchmark_trim.py:1454` alone; both hold genuinely single-chain
   trims.
4. **Item 18's mechanical closure.** `validate_spec` gains
   `expected_target_residues: int | None = None` and refuses when the contig's
   `n_target` disagrees with the trim's recorded count, with a message that
   says which side is authoritative for what (the contig is what RFD3
   templates; the trim's count is what the campaign is costed on) and that a
   disagreement means a dropped chain. `pipeline_runner.py:4806` passes
   `getattr(trim, "n_residues_after", None)`. This turns risk #2 from "closed
   by construction" into "closed by a check". It is a hard refusal because the
   invariant is exact, not approximate: measured **44 of 44** archived
   trim/spec pairs agree, and **53 of 53** trim_maps satisfy
   `sum(hi-lo+1 for kept_segments) == n_residues_after`.

**How the single-chain path stays byte-identical.** `kept_by_chain` and
`expected_target_residues` both default to None; `trim_cross_check` returns
`{"kept_segments": [...]}` for every trim in existence today (no
`TrimResult`/`_TrimFromDisk` carried the attribute before step 7, and no
archived `trim_map.json` has the key). The chain-aware branch is strictly
*stronger* on the disrupt path — it also checks the chain letter — and rejects
nothing already shipped: over all 53 trim_maps, every contig's spans name
exactly `{target_chain}` and equal `kept_segments`, 0 mismatches.

**Proof:**

    .venv/bin/python -m pytest tests/test_foundry.py tests/test_complex_token_ceiling.py \
        tests/test_audit_fixes.py tests/test_glue_spec.py -q
    # the helper is inert on an archived trim:
    .venv/bin/python -c "from pathlib import Path;from src.pipeline_runner import _TrimFromDisk;\
from src.structure_trim import load_mapping;from src.foundry_spec import trim_cross_check;\
print(sorted(trim_cross_check(_TrimFromDisk(load_mapping(Path('projects/pdl1_rc1/runs/round-1/binder/trim/trim_map.json')))).keys()))"
    # -> ['kept_segments']

**Estimate: 5 h.**

---

### Step 10 — the two unowned guards (membrane topology, ortholog conservation)

Not in the notes, not in the scope's item table, and in none of the four
recon reports' change lists. Both come from the risk critique; both confirmed
in §2.5.

**Files/symbols:** `src/pipeline_runner.py:3868-3898` (`_infer_membrane_side`
call and the `allowed_auth` refusal inside `_stage_trim`);
`src/pipeline_runner.py:2049` `_check_ortholog_conservation`, called at :1933
and :6696.

**Edit.** Same shape as step 3, and the cheap answer is right for one and not
the other.

- **Membrane topology.** Group by chain. `_infer_membrane_side` and
  `restriction_for` are both keyed to one chain's accession, and the bridge
  writes no accession for a co-target, so the honest behaviour is: infer and
  restrict on the **target chain's** rows only; for any other chain, **skip
  with a warning** naming the chain and saying no topology restriction was
  applied to it. A glue co-target whose TM residues are not stripped is a real
  hazard (item 14, Stage 6) — say so in the warning rather than fail open
  quietly, which is the posture `_note_single_target_guards` already
  established. `_combine_allowed` (:2711) already returns None when neither a
  topology nor a chimera restriction applies, so the common case is untouched.
- **Ortholog conservation.** `hotspot_conservation(pdb_id, chain, hotspots,
  …)` takes one chain and one accession, and a glue's second chain is a
  different protein with a different human ortholog. Resolving a second
  accession here is not Stage-3 work. **Skip and warn** when
  `len({h.get("chain") or chain for h in hotspots}) > 1`, scoring the target
  chain's rows only — and say in the warning that the co-target's epitope
  conservation is unchecked. Do **not** leave it silently scoring the
  partner's residues against the target's alignment: CLAUDE.md records that
  exact failure (`fraction_conserved: 0.625` over six residues that did not
  exist on the chain).

**How the single-chain path stays byte-identical.** Both are gated on a
hotspot set that names more than one chain, which no shipped run produces.

**Proof:**

    .venv/bin/python -m pytest tests/test_ortholog_check.py tests/test_membrane_topology.py \
        tests/test_pipeline_stages.py tests/test_glue_branch.py -q

**Estimate: 2.5 h.**

---

### Step 11 — item 21b: the glue budget refusal, the guard note, the docs

**Files/symbols:** `src/pipeline_runner.py` — a refusal in `_stage_trim` after
the budget is read (:3836) and before the topology block; new
`_note_glue_guards` beside `_note_single_target_guards` (:3751), called from
`_stage_trim` and `_structure_first_caveats` (:1610); `README.md`.

**Edit.** Take cluster 4's design.

- Refuse a glue target whose two chains together exceed
  `design.foundry.target_residue_budget`, naming scope item 7 and stating why
  two-chain trimming is not merely unimplemented but *unmeasured*:
  `_exposed_hydrophobic` compares each chain against itself in isolation, so a
  cut that strips the other chain's buried face reads as clean. The number to
  quote is the scope's own 5VAI measurement (**492 Å² across 8 hydrophobic
  residues on chain P** for an `R29-128 + P7-37` trim, versus 0 measured in
  isolation) — that is the scope's measurement, not mine. 4ZGM is 100+28=128.
- `_note_glue_guards(intel) -> str` returns the prose it logs, so
  `_structure_first_caveats` can embed it and the report and the log cannot
  drift. Four points: `min_bsa_retention` (0.90) measures the very interface
  a glue is stabilising and is uncalibrated for that (item 9, Stage 6) — safe
  in Stage 3 only because a no-op trim reports exactly 1.0;
  `_exposed_hydrophobic`'s isolation blindness; `hotspot_engagement` becomes a
  **pooled** fraction over both chains at a 0.75 gate (§2.3 — this is the one
  that bites first, in Stage 4); and that
  `_verify_target_chain_assignment`/`_verify_partner_chain_is_requested`/
  `_verify_hotspot_grounding` stay fully **active** and are *more*
  load-bearing here, because both chains are design targets.
- README: document `--design-intent` beside `--modality` and `--hotspots` as
  the third instance of the same posture, list what a glue run currently
  refuses and which scope item lifts each, and give the working command
  verbatim.

**Proof:**

    .venv/bin/python -m pytest tests/test_glue_branch.py tests/test_docs_match_code.py -q

**Estimate: 3 h.**

---

### Estimate

| step | item(s) | hours |
|---|---|---|
| 1 | 1 (parser + regions) | 5.0 |
| 2 | 2 (SKILL.md + handoff + zip) | 2.25 |
| 3 | 3 (grounding) | 2.5 |
| 4 | 4 (label_seq + `_section_sub`) | 4.75 |
| 5 | 21a (flag, refusals, two-chain `--hotspots`, writer) | 7.5 |
| 6 | 5 (`build_contig_multi`) | 1.5 |
| 7 | 6 (`kept_by_chain`, counts, `_TrimFromDisk`) | 7.5 |
| 8 | 11 + 6b + `_stage_trim` filter (one commit) | 4.5 |
| 9 | 12 + 18 | 5.0 |
| 10 | membrane + ortholog (unowned) | 2.5 |
| 11 | 21b (budget refusal, guard note, docs) | 3.0 |
| | **total** | **46 h ≈ 6 working days** |

The notes' "~6 d" happens to land in the same place, but its per-item table
omits three of these steps (the `_stage_binder_spec`/`_stage_trim` filters,
the two unowned guards, and the `_section_sub` redesign) and prices item 21 at
0.5 d against a measured 10.5 h across steps 5 and 11.

---

## 4. Test plan

### 4.1 Today's measured green baseline

Run by me, this tree, today. **These are the numbers to diff against**, not
the ones in any recon report:

    .venv/bin/python -m pytest tests/test_structure_trim.py tests/test_foundry.py \
        tests/test_audit_fixes.py tests/test_binder_metrics.py -q
      -> 299 passed, 1 deselected, 31 warnings in 34.8 s

    .venv/bin/python -m pytest tests/test_hotspot_regions.py tests/test_hotspot_override.py \
        tests/test_pipeline_stages.py tests/test_ortholog_check.py tests/test_single_target_mode.py \
        tests/test_structure_first_track.py tests/test_complex_token_ceiling.py \
        tests/test_binder_report.py tests/test_ppi_report.py tests/test_release_fixes.py \
        tests/test_docs_match_code.py tests/test_hotspot_numbering_frame.py \
        tests/test_patch_liability.py -q
      -> 542 passed, 1 skipped, 13 deselected in 43.1 s

**Combined: 841 passed, 1 skipped, 14 deselected, ~78 s.** Nothing is
failing. The deselections are the `network`-marked provider-contract tests;
the single skip is
`test_the_real_two_region_report_comes_back_within_the_cap`, which wants
`projects/a344_fix_check/.../21_interface.md` (absent in this checkout) — note
that this means one of `test_hotspot_regions.py`'s guards does **not** fire
here, so a column-layout change would lose a tripwire silently.

### 4.2 Wall tests that stay green, untouched

Every one of these keeps its assertions exactly as written. The
single-chain-path discipline in §3 is what makes that possible, and if any of
them goes red the change is wrong, not the test.

- `test_foundry.py::test_regenerates_the_reference_cd79b_spec_field_for_field` —
  a frozen artifact from a real GPU campaign. Do not alter it to accommodate a
  new signature; give the signature a default instead. A glue golden is Stage
  5 work, after a spec has actually run.
- `test_foundry.py::test_mpnn_config_shape` — `designed_chains == ["A"]`. It
  becomes a **tripwire proving the RFD3 merge is intact**: if it goes red
  during glue work, the contig has grown a second `/0`.
- `test_binder_metrics.py::test_sidecar_remaps_hotspots_into_output_numbering`
  and the 25-column zero-diff golden over 200 refolds (R5) — glue-neutral by
  the merge. `hotspots_from_rfd3` filters on the destination letter, so both
  chains' hotspots survive and land in one output namespace.
- `test_audit_fixes.py::test_trim_from_disk_has_every_attribute_the_gpu_stages_use`
  (R4) — reflection; it **must be allowed to fail** the moment
  `_stage_binder_spec` reads `trim.kept_by_chain` without `_TrimFromDisk`
  having it. That is why steps 7 and 8 land in that order and 8 is one commit.
- `test_audit_fixes.py::test_a_fusion_partner_is_excluded_from_the_design_target` —
  accession-level, not chain-level (§2.3). Untouched.
- `test_single_target_mode.py` — all of it, including
  `test_only_single_target_intent_waives_the_partner`, which is parametrised
  over `"stabilize"` and **requires it to refuse a missing partner**. Do not
  drop `"stabilize"` from that list to make a glue run: it is the assertion
  stopping a glue that lost half its pocket from quietly becoming a
  single-surface campaign with the wrong-molecule and ortholog guards off.
  `test_there_is_one_predicate_not_two` is what forces step 5's predicate into
  **both** validators.
- `test_hotspot_regions.py` — all five. One region per campaign survives the
  glue unchanged and for the same reason: a glue pocket is **one** region
  spanning two chains, and two glue pockets are two campaigns.
- `test_pipeline_stages.py`'s three grounding tests and two label_seq tests —
  all pass chain-less JSON and take the collapsed path.
- `test_release_fixes.py::test_packaged_skill_zips_match_their_source` —
  repackage in the same commit as the `SKILL.md` edit (step 2).

### 4.3 Wall tests that get a two-chain case added

- `test_structure_trim.py::test_build_contig_shape` — keep both assertions
  literally; add `build_contig_multi({"A":[(29,128)],"B":[(10,37)]},70,86) ==
  "70-86,/0,A29-128,B10-37"`, that it contains **exactly one** `/0`, and that
  mapping order is honoured (reversed mapping → reversed spans).
- `test_structure_trim.py::test_contig_matches_the_kept_segments` — keep the
  3KYS assertions; add `res.kept_by_chain == {"A": res.kept_segments}` and the
  general invariant `spans == join over kept_by_chain`. The first is what
  proves the general form degenerates to the old one.
- `test_structure_trim.py::test_a_trim_that_removes_nothing_retains_everything`
  (**the R2 wall**) — keep all three rungs at the exact-1.0 bar; add a 4ZGM
  two-chain no-op rung at the same bar. "Nothing removed ⇒ exactly 100%" is
  true of a glue too, and if it is not, the new measurement is wrong.
- `test_foundry.py::test_validate_cross_checks_against_the_trim` — keep the
  accept and the refuse; add an `kept_segments=[]` case (today `[]` refuses a
  non-empty contig, and the `elif` edit could silently lose that), and the
  chain-swap negative beside it (§4.4).
- `test_audit_fixes.py::test_trim_from_disk_falls_back_to_the_segments` — the
  no-`target_chain` case; assert `kept_by_chain == {}` (not `{"": ...}`) and
  the counts unchanged at 84 and 20.
- `test_audit_fixes.py::test_target_and_partner_follow_the_chain_assignment` —
  keep the three existing cases; add a `stabilize` case, and a separate test
  that `restriction_for` runs per **kept** chain with that chain's own
  accession (or, under step 10's decision, that it is skipped with a warning
  for a co-target). The single-accession path is what the existing test
  protects; the glue's exposure is the second, unprotected call.

### 4.4 New tests

**`tests/test_glue_hotspot_parsing.py`** (step 1–2)
- `test_the_5vai_glue_table_attributes_every_row_to_its_own_chain` — the real
  `div_standard_diabetes` report: 7 residues, chains `[R,R,R,P,P,P,P]`,
  `target_chains == ["R","P"]`, `target_chain`/`partner_chain` still `R`/`P`.
- `test_the_6jjw_glue_table_splits_eleven_and_six` — the real
  `div_wildcard_tnbc` `02_structure.md` and `binder/21_interface.md`: 17
  residues, 11 on `A` / 6 on `U` (auth 447,448,449,450,451,453). **Do not
  parametrise the site copy into this test** — it parses to **16**, measured,
  not 17; give it its own case or a computed expectation.
- `test_every_shipped_report_still_parses_to_one_chain` — the byte-identity
  wall. Glob `projects/*/runs/*/02_structure.md`,
  `projects/*/runs/*/binder/21_interface.md` and
  `projects/*/runs/*/binder/sites/*/binder/21_interface.md` — **111 files
  today**, not 61 — and assert every residue's `chain` equals the handoff's
  `target_chain` with an explicit four-file allow-list. Glob, do not
  `grep -r`.
- `test_a_section_with_one_chain_heading_is_never_split` — the `gem_vegf_a`
  report (`Chain W` / `Chain V` in two sections, target `W`, partner `X`):
  every row stays on `W` and the `V` heading changes nothing. This is the test
  that pins the ≥2-headings-per-section rule and makes the precedence hazard
  unreachable.
- `test_target_chains_in_the_handoff_wins_over_the_positional_fallback` —
  chains literally named `A` and `R`, heading `Chain A`, handoff
  `target_chains: A, R` → literal `A`.
- `test_a_glue_pocket_is_a_region_and_the_named_primary_wins` — synthetic
  two-pocket report with `Primary target: Glue Pocket 2`: `regions_declared ==
  2`, `region == "Glue Pocket 2"` (no trailing bracket), Pocket 2's residues
  returned. Today this silently returns Pocket 1 with `region: ""`.
- `test_the_skill_template_parses_as_two_chains` — extract the STABILIZE fence
  from `SKILL.md`, substitute `R`/`P` and two rows per table, assert
  `target_chains == ["R","P"]`. The SKILL.md↔parser contract, pinned the way
  `test_a_table_the_parser_cannot_read_raises_instead_of_no_opping` pins the
  DISRUPT one. No API call.

**`tests/test_glue_grounding.py`** (step 3)
- `test_per_chain_grounding_passes_on_both_real_glue_reports` — parametrised
  over (5VAI, `div_standard_diabetes`) and (6JJW, `div_wildcard_tnbc`); must
  not raise. Both CIFs are on disk; no network, no API, no GPU.
- `test_a_chainless_two_chain_table_still_refuses` — the same 6JJW residues
  with the `chain` key **stripped** must still raise "present under those
  names in chain U". This is the test that stops a future refactor from
  defaulting the chain silently.
- `test_a_row_on_neither_chain_still_fails` — mutate one chain-P auth id to a
  number absent from both R and P; must raise naming chain P.

**`tests/test_glue_label_seq.py`** (step 4)
- `test_per_chain_label_seq_ids_resolve_on_their_own_chain` — the real 5VAI
  text with the chain-P cells restored: chain-R rows → 105/106/109, chain-P
  rows → 24/29/30/31, and **zero** "NAME mismatch" warnings (today that input
  produces four and leaves the rows unresolved).
- `test_the_second_chains_counted_labels_are_corrected` — the real 6JJW text:
  auth 447–453 come out 24,25,26,27,28,30 rather than the model's 14–20.
- `test_the_binding_lines_are_rewritten_per_chain_and_never_deleted` — the
  real 5VAI text: `Chain A binding:` keeps chain R's ids, `Chain B binding:`
  gets chain P's, and **neither line is replaced** by the
  `_LABEL_SEQ_UNAVAILABLE` statement. This is the regression the naive
  per-block design would have shipped (§ step 4).
- `test_a_single_chain_section_takes_the_unchanged_binding_rewrite` — the
  5GN0 `binding: 54,56,60,63` golden through the new path with `handoff={}`
  and with `handoff={"target_chain":"A"}`: identical both times.

**`tests/test_glue_branch.py`** (steps 5, 10, 11)
- `test_the_structure_track_can_now_state_stabilize`;
  `test_the_resolver_is_a_no_op_without_the_flag`;
  `test_stabilize_without_a_partner_chain_is_refused`;
  `test_an_unknown_intent_is_refused_at_construction`.
- `test_glue_is_refused_on_every_unbuilt_path` — parametrised over
  `boltzgen`, `cyclic_peptide`, `trial_sites=2`, `workflow in ("ppi","binder")`;
  each names its scope item, and `design_intent=None` still constructs.
- `test_a_stabilize_handoff_is_refused_before_the_structure_stage` — assert
  the stage runner is never invoked, and that the **literature** handoff's
  intent is the one read.
- `test_a_glue_run_accepts_hotspots_on_both_chains`;
  `test_a_third_chain_is_still_refused_on_a_glue_run`.
- `test_a_single_chain_override_report_is_unchanged` — a **golden-string**
  compare, not a substring test. This file is what `--start-from trim`
  re-parses off disk.
- `test_a_glue_override_report_carries_both_chains` — two `Chain <id>`
  headings (no colon), two 4-column tables whose rows still match **both**
  `handoff._ROW` and `_resolve_unverified_label_seq_ids`'s row regex, and a
  `select_hotspots` block with per-residue chain prefixes.
- `test_a_glue_run_that_lost_its_partner_refuses_in_the_trim_too`.
- `test_a_glue_target_that_needs_a_trim_is_refused` (5VAI R/P) and
  `test_4zgm_fits_and_is_not_refused`.
- `test_a_glue_run_says_which_guards_change_meaning` — mirrors
  `test_single_target_mode_says_which_guards_it_disabled`; must name
  retention, isolation, pooled engagement, and the three guards that stay
  ACTIVE.
- `test_the_membrane_and_ortholog_guards_skip_a_co_target_chain_loudly`
  (step 10) — a two-chain table with a `target_uniprot` set must warn per
  skipped chain and must not raise.

**`tests/test_glue_spec.py`** (steps 8–9)
- `test_a_hotspot_carries_its_own_chain_into_the_key` — 4ZGM, 3 on A + 4 on B,
  `target_chain="A"`: exactly the 7 expected keys.
- `test_two_chains_sharing_an_auth_number_no_longer_collide` — the
  A32/B32/A39 case: 3 keys out, A32 keeping its own atoms. Measured today:
  2 keys out, A32 carrying B's atoms, and `validate_spec` **accepts** it.
- `test_a_chainless_hotspot_still_lands_on_the_target_chain`.
- `test_every_archived_spec_re_derives_field_for_field` — walk the **49**
  specs under `projects/**/spec/`, rebuild, compare
  dialect/contig/infer_ori_strategy/is_non_loopy/select_hotspots. Baseline
  today: 49 re-derived, 0 diffs. Skip
  `projects/trimbench_6yye/.../6yye_chain_c_binder_001.json` **by name with a
  comment** — it raises `SpecError: hotspot A56 names atom(s) …` today, a
  pre-existing failure unrelated to glue.
- `test_the_cross_check_refuses_spans_on_the_wrong_chain` — use the **overlap**
  contig `70-86,/0,A29-128,A29-37`, not `A10-37`: measured today, `A10-37` is
  refused by the residue-existence check (`residue A10 is not in
  trimmed.pdb`) and so does not exercise the cross-check at all. With the
  overlap form, `kept_by_chain={"A":[(29,128)],"B":[(29,37)]}` must raise
  while `kept_segments=[(29,128),(29,37)]` still passes — that asymmetry is
  the whole point and it holds today.
- `test_passing_both_cross_check_forms_is_refused`.
- `test_an_overlapping_span_is_not_double_counted` — 100, not the 109
  measured today.
- `test_a_target_count_that_disagrees_with_the_trim_is_refused` —
  `expected_target_residues=100` against the 128-residue glue spec.
- `test_trim_and_spec_agree_on_the_target_size` — all **44** archived
  trim/spec pairs pass `expected_target_residues`; 0 refusals.
- `test_every_shipped_trim_map_is_a_single_chain_kept_by_chain` — all **53**
  trim_maps (not 91): contig spans equal `[(target_chain, lo, hi)]`, and
  `_TrimFromDisk(m).kept_by_chain == {target_chain: kept_segments}` with the
  counts unchanged.
- `test_the_4zgm_glue_spec_validates_end_to_end` — the go/no-go as a test
  (§5).
- `test_the_mmcif_refusal_still_fires_for_a_two_chain_contig` — the same spec
  pointing at the `.cif` must still raise "label_seq_id numbering differs".

**`tests/test_glue_branch.py::test_the_glue_smoke_reaches_a_validated_spec`** —
the whole `--workflow structure --pdb 4ZGM --chains A,B --design-intent
stabilize --hotspots … --stop-after spec` path in-process against a
`tmp_path` project, seeding the tmp structures dir from `data/structures/`.
Mark it `xfail(strict=True)` from step 5 until step 9 lands, so the wall
records the gap rather than hiding it, and it flips to a failure the moment
someone forgets to un-xfail it. (Note `scripts/download_pdb_structures.py:84`
reads `paths.structures_dir` from `config.yaml` on disk rather than the
caller's in-memory config, so a non-default structures dir downloads into the
repo's `data/structures/` and then fails its own existence check — seed the
fixture instead of relying on a fetch.)

---

## 5. Go/no-go for Stage 3 — exact commands and expected output

The notes' four criteria, made runnable. **Stop if any fails.**

**(1) The wall is green, with no regression against §4.1.**

    .venv/bin/python -m pytest tests/test_structure_trim.py tests/test_foundry.py \
        tests/test_audit_fixes.py tests/test_binder_metrics.py -q
    # expect: >= 299 passed, 1 deselected  (new tests only ADD)

    .venv/bin/python -m pytest tests/test_hotspot_regions.py tests/test_hotspot_override.py \
        tests/test_pipeline_stages.py tests/test_ortholog_check.py tests/test_single_target_mode.py \
        tests/test_structure_first_track.py tests/test_complex_token_ceiling.py \
        tests/test_binder_report.py tests/test_ppi_report.py tests/test_release_fixes.py \
        tests/test_docs_match_code.py tests/test_hotspot_numbering_frame.py \
        tests/test_patch_liability.py -q
    # expect: >= 542 passed, 1 skipped, 13 deselected

**(2) `parse_hotspot_residues` attributes every row to its own chain, on both
archived reports.**

    .venv/bin/python -c "
import json; from pathlib import Path
from src.handoff import parse_handoff, parse_hotspot_residues
for p,exp in (('projects/div_standard_diabetes/runs/round-1/02_structure.md',['R','P']),
              ('projects/div_wildcard_tnbc/runs/round-1/02_structure.md',['A','U'])):
    t=Path(p).read_text(); d=json.loads(parse_hotspot_residues(t,parse_handoff(t)))
    print(d['target_chains'], len(d['residues']), [r['chain'] for r in d['residues']])
    assert d['target_chains']==exp"
    # expect:
    #   ['R', 'P'] 7  ['R','R','R','P','P','P','P']
    #   ['A', 'U'] 17 ['A'x11, 'U'x6]
    # today: target_chains absent; all rows stamped 'R' / 'A'

**(3) Grounding passes per chain on both, and still refuses a chain-less
two-chain table.**

    .venv/bin/python -m pytest tests/test_glue_grounding.py -q
    # expect: 3 passed
    #   - 5VAI 7/7 and 6JJW 17/17 ground clean (today: PipelineError via the
    #     `mismatches` and `absent` branches respectively)
    #   - the chain-STRIPPED 6JJW table still raises "present under those names
    #     in chain U"

**(4) `validate_spec` accepts the 4ZGM glue spec including the chain-aware
cross-check, and BOTH token numbers read correctly.** Two numbers, two
producers — see §2.1(b); a reviewer checking "206" against `validate_spec`
will see 214 and wrongly conclude the fix failed.

    .venv/bin/python -m pytest tests/test_glue_spec.py::test_the_4zgm_glue_spec_validates_end_to_end \
        tests/test_glue_spec.py::test_the_cost_model_number_for_that_spec_is_206 -q

    # expected, and all four verified by me today on a hand-built spec:
    #   contig                                  70-86,/0,A29-128,B10-37
    #   validate_spec n_target_residues          128      (chain A 100 + chain B 28)
    #   validate_spec n_tokens                   214      (binder MAX 86 + 128)
    #   validate_spec n_segments                 2
    #   trim.n_residues_after                    128
    #   _binder_midpoint(contig)                 78
    #   cost-model n_tokens (pipeline:4796)      206      <- the notes' number
    #   single-chain baseline, for contrast:     100 res / 186 tok / 178 cost
    #   kept_by_chain={"A":[(29,128)],"B":[(10,37)]}  -> ACCEPTED
    #   kept_by_chain={"B":[(29,128)],"A":[(10,37)]}  -> SpecError

**(5) One additional criterion the notes do not have, and should:** the
end-to-end command runs with no LLM and no GPU.

    .venv/bin/python scripts/run_pipeline.py --workflow structure --pdb 4ZGM \
        --project glue_4zgm --chains A,B --design-intent stabilize \
        --hotspots A113,A119,A120,B30,B31,B34,B35,B37 --stop-after spec
    # expect: a validated spec at projects/glue_4zgm/runs/round-1/binder/spec/*.json
    #   with select_hotspots keys exactly {A113,A119,A120,B30,B31,B34,B35,B37}
    #   and NO key of the form A30/A31/A34/A35/A37

That last clause is the one that matters most. Measured today: 4ZGM chain A
spans auth 29–128, so **every** chain-B hotspot number also exists on chain A
under a different residue name. Before step 8 those five hotspots are stamped
onto chain A and the spec validates, because `validate_spec` confirms they
are real residues carrying the stated atoms. That is a wrong campaign that
looks exactly like a right one.

---

## 6. Risks and open questions, ranked

### Risk 1 — step 3 removes the accidental guard, and steps 8's three edits are what replace it

`_verify_hotspot_grounding` is today the *only* thing stopping a cross-chain
hotspot table, and it stops it by accident. The moment it looks each row up on
its own chain, three flat author-number sets — `_stage_trim:3906`,
`_stage_binder_spec:4104`, `foundry_spec.py:120` — silently merge the two
chains, and **nothing downstream catches it**. On 4ZGM all five partner
hotspot numbers exist on chain A. Mitigation: steps 8's three edits land in
one commit, with `test_a_partner_hotspot_is_never_stamped_onto_the_target_
chain` written first; and `test_a_chainless_two_chain_table_still_refuses`
pins that the relaxation applies only to a table that said which chain it
meant. Three separate recon reports found one third of this problem each,
which is itself the evidence that it is easy to half-fix.

### Risk 2 — the cost model still under-counts, and 4ZGM is the mildest possible test of it

Ranked risk #2 in the notes is **open**, not closed (§2.1b). Step 7 fixes
`n_residues_after` and step 9 makes the disagreement a refusal. But 4ZGM's
100/28 split under-costs by only 1.27× on RF3 time, 1.18× on RFD3 time and
1.24× on disk (cluster 3's computation from `foundry_runner`'s own laws at 206
vs 178 tokens) — the scope's "3.1×" is a 5VAI-shaped split. **So a 4ZGM number
that looks right proves very little about risk #2.** What closes it is the
mechanical `expected_target_residues` guard, not the measurement. Say that in
the commit message, because the first person to run a real glue campaign will
see the refusal and must know it means "the trim recorded one chain", not
"the spec is wrong".

### Risk 3 — `_write_override_interface_report` is a resume artifact, and the anchor was nearly wrong

`--start-from trim` re-parses `21_interface.md` off disk, and
`_correct_label_seq_ids` re-runs on it. A shape change that is not
byte-identical for the single-chain case breaks resume for every existing
`--hotspots` campaign; a leading `| Chain |` column breaks both row regexes
against all 111 shipped reports at once; and cluster 4's `Chain: <id>` anchor
does not match cluster 1's frozen regex, which would have shipped a glue
override whose rows all fell back to `target_chain`. All three are addressed
in §3.1 and step 5, and the golden-string test is what holds it. Note that
`test_hotspot_regions.py`'s real-report guard is **skipped in this checkout**
(the fixture is absent), so one tripwire for the column layout does not fire
here.

### Risk 4 — `hotspot_engagement` becomes a pooled number, and Stage 3 creates that

Contradicting the scope (§2.3): `hotspots_from_rfd3` filters on the
**destination** chain, and under the merge everything maps into `B`, so
nothing is dropped. The first glue campaign will report one pooled engagement
fraction gated at 0.75 — which the scope itself says "provably cannot"
distinguish a glue from a competitive binder. Stage 3 cannot fix this (it is a
new column, items 15/16, Stage 4) and must not pretend to; `_note_glue_guards`
says so out loud, which is the `_note_single_target_guards` posture.

### Risk 5 — `MAX_HOTSPOTS` is decided three different ways and one of them refuses a real artifact

`foundry_spec.py:102-108` warns above 12 and never truncates; cluster 4 makes
it an **error** on the operator path; `div_wildcard_tnbc`'s real table declares
11 + 6 = 17, which cluster 4's rule refuses and cluster 1 proposes as a
fixture; cluster 3 leaves it open; the scope calls it `[inferred]`, not
measured. **Decision for Stage 3: keep it a TOTAL of 12 and keep it an error
only on the operator path** (an operator can choose which to drop; the builder
cannot), and do **not** apply it to parsing — the 6JJW fixture must keep
parsing to 17. Whether a glue gets 12 per side, 12 total, or a per-side
minimum is genuinely unmeasured and belongs with the per-side engagement gate
in Stage 4. Note the teeth: engagement is a fraction of whatever was declared,
so a 17-residue declared set weakens the one gate CLAUDE.md says carries the
epitope check.

### Risk 6 — three clusters each claimed `_stage_binder_spec:4104`

Cluster 2 ("item 6b, owned by NO scope item"), cluster 3 ("item 11 companion")
and cluster 4 ("cross-cluster") wrote three different bodies for the same two
lines, four lines from step 8's other edit, and cluster 4 also edits `:3906`
in the same commit. Resolved by putting all three in step 8 as one commit with
one body (cluster 3's, which is the only one that spells the `_TrimFromDisk`
fallback correctly). The failure if this is got wrong is quiet: chain-correct
keys built over a row set that was already filtered wrongly, producing a spec
that is short two hotspots and still validates.

### Risk 7 — the two unowned guards fire the first time anyone passes `--uniprot`

Step 10 handles them, and their priority is lower than the critique implies
for one measured reason: `_stage_structure_intel:1584` writes `target_uniprot:
(uniprot or "").upper()`, and the membrane block is `if uniprot and …`, so the
Stage-3 command as written skips both. They fire on `--uniprot`, and on the
PPI-bridged 5VAI path. Do not skip step 10 on the strength of a green 4ZGM
run: the guard being inert is a property of the command, not of the code.

### Open questions the critics could not settle

1. **Where a 4ZGM glue *hotspot table* comes from.** There is none anywhere in
   the tree — only `data/structures/4ZGM_ba1.cif`. Steps 1–4 can therefore be
   exercised only on 5VAI and 6JJW; the Stage-3 target reaches the parser only
   through step 5's two-chain `--hotspots` path. That is why 21a moved forward,
   and it is also why the notes' own go/no-go criterion ("grounding passes on
   `div_standard_diabetes`") is the one that can be met without step 5 while
   criterion 4 cannot. `structure_tools.find_glue_pockets('4ZGM_ba1.cif','A','B')`
   returns a usable 8-residue set (cluster 4's measurement:
   A TRP120/PRO119/LYS113, B ARG34/GLY37/TRP31/GLY35/ALA30), which is where the
   `--hotspots` string in §5(5) comes from. Nobody verified it against the
   structure a second time; do that before quoting it as an epitope.
2. **Whether `co_target_chains` or `design_intent` is the right parameter on
   `trim_target`.** Cluster 2 chose the explicit chain list so the refusals are
   mechanical and `trim_target` stays ignorant of pipeline vocabulary (it knows
   nothing about `design_intent` today). Step 11's author will have a
   `design_intent` in hand and may prefer to pass it. **They must not both
   exist.** Unresolved; decide before step 11.
3. **Which chain a glue picks as `target_chain`.** Stage 3 silently requires
   the **larger** one: `plan_trim:852` applies `MIN_TARGET_RESIDUES = 80` to
   the target chain alone, so 4ZGM chain B (28 residues) as primary raises
   "below the 80-residue floor" — a true statement about the wrong question.
   Scope item 10 (a combined-surface floor, or a separate smaller per-chain
   one) is deferred, so this is an undocumented one-line precondition. The
   regression-wall review is right that `test_the_floor_is_eighty` pins only
   the constant and **nothing pins the per-chain application**; if anyone
   relaxes the floor for a glue, the "19-residue target passed every other
   check" hole reopens with no test to catch it. A new test is owed here and is
   not in Stage 3's scope as written.
4. **Whether step 7's G3 (no-op only) should refuse or fall back.** Cluster 2
   chose refusal because the overshoot policy lives in `_stage_trim` (item 13,
   Stage 6). The cost is that a 4ZGM-like pair five residues over budget cannot
   run at all; the cheapest unblock is raising
   `design.foundry.target_residue_budget`, not relaxing G3. Unsettled, but
   refusal is the safe default and is what this plan takes.
5. **Whether the `absent`-branch and `mismatches`-branch diagnoses should be
   reworded once a two-chain table is legal.** With per-row chains,
   `_chain_holding_residues`'s hint ("the table is attributing another chain's
   residues to the target") becomes a *different* diagnosis — "the heading is
   wrong". And the `mismatches` branch's "likely reported textbook/literature
   numbering" is the message an operator actually saw on the 5VAI run, where it
   misdiagnosed a cross-chain attribution as a numbering-frame error. Cluster 1
   specified the code change and not the new sentences, and noted that adding
   the hint to the `mismatches` branch changes a string two tests match on.
   Still open.
6. **`_hotspot_numbering_frame` is silent about the co-target's frame**,
   because there is no accession for it on this code path.
   `_verify_ppi_chain_assignment` already computes both candidates and could
   stash the pair. Cheap and correct, but it touches a guard the PD-L1 work
   built, and nobody measured whether `uniprot_to_auth` returns anything useful
   for a 31-residue peptide chain like 5VAI P. Deliberately left out.
7. **Whether anything outside this tree reads `hotspot_residues_json`.**
   Cluster 1 checked `src/`, `scripts/` and `tests/` and called it exhaustive,
   but missed `scripts/measure_boltzgen_binder_gates.py:201`, which calls
   `parse_hotspot_residues(report.read_text(), {})` with an **empty handoff** —
   so under the new shape it would report `target_chains: ["A"]` for a report
   whose real target chain is `R`, fabricating a value where today the field is
   simply absent. Fix it in step 1 (pass the parsed handoff) or make
   `target_chains` omit itself when the handoff is empty. `web/` is untracked
   and excluded by CLAUDE.md and was not opened by anyone.
8. **Nobody has verified on GPU that a glue contig's merged output target
   chain scores `rfd3_n_chainbreaks == 1` at the A/B junction.** Stage 1
   measured the merge (2 output chains, 1..128 consecutive) but reported no
   chainbreak count, so `max_chainbreaks = n_segments = 2` is loose-or-required
   and nobody knows which. Free to settle at Stage 5's first prefilter report;
   it costs no extra GPU time. Until then, step 7's `n_segments` redefinition
   is right either way (loose beats rejecting every design).
