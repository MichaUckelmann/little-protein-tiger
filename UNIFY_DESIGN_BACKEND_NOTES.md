# Making foundry (RFD3→MPNN→RF3) the default design engine for PPI-entered runs

Written 2026-08-24, under time pressure (a forced workstation reboot was
~20 min out when this was drafted) — this is a scoping/planning note for a
future session, not a finished design, and several claims below are marked
**VERIFY** rather than confirmed. Read `CLAUDE.md` fully first; it documents
a long list of hard-won, non-obvious facts about the binder/foundry path
that this integration would inherit wholesale and must not silently violate.

## STATUS (2026-08-24, later session)

First implementation pass landed. See `CLAUDE.md`'s "The PPI -> foundry
bridge (opt-in `design_engine`)" section for the shipped architecture and
`diary.md`'s matching dated entry for the full session narrative — both are
now the authoritative descriptions; this file is kept for the original
scoping rationale (the three maintainer decisions below, and the
alternatives considered) rather than duplicated here.

Resolved, as answered by the maintainer:
- BoltzGen stays as a live opt-in escape hatch (`design.backend: boltzgen`,
  the default) — no dead-code removal of `design_metrics`/`design_ranking`.
- `--project` is required for `--design-engine foundry`, matching the
  binder track's own requirement (point 5 below).
- Shipped opt-in (`--design-engine foundry` / `design.backend: foundry`),
  **not** a default flip of `--workflow ppi` — matches point "Before
  starting real implementation"'s explicit recommendation below.
- Finding #3 (the verify-gap) was fixed first, exactly as recommended, and
  turned out to change the integration shape for the better: since PPI's
  `_stage_structure` now runs the same guards `_stage_binder_interface`
  does, the shipped bridge reuses PPI's structure-stage output directly
  instead of the sketch below's `start_from="interface"` (which would have
  re-run `complex-structure-analysis` a second time, redundantly).
- Finding #2 (dead `design.backend` key) is resolved — it is real now.

Not yet done (see the diary entry's punch list for details): membrane
topology resolution for a PPI-bridged target, `--trial-sites` support in
the bridge, and a real end-to-end GPU campaign proving output quality
(current verification is unit-level, `_run_binder_track` stubbed).

## The ask

Maintainer's framing: binder-track (`--workflow binder`) campaigns are
producing excellent results (IL7RA campaign mid-flight as of this note:
SCALE_UP verdict, best iptm 0.92, adaptive bar raised to 0.85 — see
`diary.md`'s 2026-08-23/24 entries). Make foundry the default design
mechanism even for PPI-track runs (the literature-driven, generic-prompt
entry point — `"design inhibitors of the Hippo pathway"`), where PPI's own
reasoning (pathway-expert + literature/tractability) picks the target
complex, then hands off to the SAME structure/hotspot analysis and foundry
execution the binder track already uses — instead of continuing into PPI's
current BoltzGen-based design/execution/analysis stages.

## Verdict: medium-to-major, not minor

### What's already shared or track-agnostic (cheap)

- **PPI's `structure` stage and binder's `interface` stage call the
  identical skill** (`complex-structure-analysis`) and produce the
  identical `### MODEL-READY HOTSPOTS` / `### PIPELINE HANDOFF` format,
  parsed by the same `src/handoff.py` code both tracks already use
  (`parse_handoff`/`parse_hotspot_residues`). Confirmed this session while
  building `src/ppi_report.py` — both tracks' handoff blocks round-trip
  through the identical parser with no track-specific branching needed.
- **Domain-aware trimming** (`src/structure_trim.py`) is already
  track-agnostic — takes a structure + hotspots + a residue budget, has no
  awareness of which track called it.
- **`design-analyst`** (final summary) already serves both tracks
  (`_STAGE_TO_SKILL`'s many-to-one mapping, `summary` vs `binder_summary`) —
  the liability-rubric split done 2026-08-23 already conditions on which
  columns are present, so it's track-agnostic in practice already.

### What's real, non-trivial work

1. **GPU execution machinery is binder-track-specific today.**
   `job_registry.py`, `campaign_calibration.py` (scale-up statistics,
   Wilson intervals, adaptive bar), the local/cluster auto-decision
   (`choose_compute`), disk budgeting, `_resolve_production_plan`, cluster
   support (`cluster_runner.py`) — all wired into `_run_binder_track`'s own
   stage machine (`BINDER_STAGE_ORDER`), which currently **requires
   `--project`** (round-based, resumable-by-design for multi-day
   campaigns). PPI supports both `--project` and the legacy one-shot
   `outputs/<slug>_<date>/` layout. Reconciling these is a real design
   decision, not a mechanical change — does a PPI-entered foundry run
   *require* `--project` now too?

2. **`design.backend: foundry` in `config.yaml` (line ~215) is a dead
   config key today — VERIFIED not read anywhere in `pipeline_runner.py`**
   (`grep -n '"backend"' src/pipeline_runner.py` → no hits). This looks
   like a leftover from an earlier plan (see `archive/RELEASE_PLAN.md`-era
   thinking, and this repo's own git history around when `--workflow
   binder` was added) that intended `design.backend: boltzgen|foundry` as
   a toggle *within* the PPI track, but what actually got built instead was
   a fully separate track (`--workflow binder`) with its own stage machine.
   **Do not assume flipping this key does anything today** — it doesn't.
   Either wire it up for real, or remove the dead comment/key so it stops
   implying a feature that doesn't exist.

3. **A correctness gap this investigation surfaced, unprompted — worth
   fixing regardless of the bigger unification question:**
   `_verify_target_chain_assignment` and `_verify_hotspot_grounding` (the
   guards built after the real PD-L1 chain-swap/hotspot-grounding
   incidents documented in CLAUDE.md's binder-track section) are **only
   called from the binder track's `interface`-stage code path**
   (`src/pipeline_runner.py:908-909` inside what's clearly `_stage_interface`
   given the `dirs["binder"]` / `_BINDER_STAGE_FILES["interface"]`
   references, and again at `:1963` in the per-site re-verification path).
   **grep confirms PPI's own `structure` stage never calls either.** A
   PPI-track run today has no protection against the exact failure mode
   that already burned a full campaign once. This alone is worth fixing
   independent of the bigger unification — probably by moving both verify
   calls to a track-agnostic helper called from both `_stage_structure`
   (PPI) and `_stage_interface` (binder), which is a small, safe, isolated
   change and a reasonable first PR before touching anything bigger.

4. **Scoring/ranking are deliberately decoupled and should stay that
   way** (`design_metrics.py`/`design_ranking.py` for BoltzGen's 260-column
   shape vs `binder_metrics.py`/`binder_ranking.py` for RF3's ipSAE/dock-RMSD
   shape — confirmed multiple times this session, including by a dedicated
   code-quality audit, as intentional, not redundant). If PPI-entered runs
   switch to foundry execution, their scoring should naturally become
   `binder_metrics`/`binder_ranking` output — meaning PPI's own
   `design_metrics`/`design_ranking` path becomes dead code for this new
   default flow (presumably kept only for an explicit
   `--design-backend boltzgen` escape hatch, if one is kept at all — a real
   product decision, not a technical one).

5. **Every binder-track "non-obvious fact" in CLAUDE.md now applies to
   PPI-originated targets too**, and needs re-verification in that
   context, not assumed: RFD3 output chain/numbering conventions,
   membrane-topology trimming, the disk-budget clamp, the pilot→calibration
   → production sizing discipline, cluster NB-per-GPU semantics, etc. None
   of these are PDB-selection-method-specific in principle, but "in
   principle" is exactly the kind of claim this codebase's own history
   says to verify against a real run rather than trust — see the "seven/eight
   real bugs" 2026-08-22/23 diary entry and the hotspot-numbering bug fixed
   2026-08-24, both caught only by running real campaigns or reading real
   generated output, not by code review.

## A concrete integration shape (sketch, not a commitment)

The likely lowest-risk path is a **bridge**, not a merge of the two stage
machines:

1. PPI's `pathway` + `literature` stages run unchanged — they already
   produce `pdb_id`, `target_complex`, `design_intent`, `modality`,
   `target_site_hint`, `go_recommendation` in the literature handoff
   (`src/pipeline_runner.py`'s `_stage_literature`).
2. New glue step: translate that literature handoff into the shape
   `_run_binder_track` expects from ITS OWN `target_intel` stage (compare
   field names — binder's target_intel handoff has `target_gene`,
   `pdb_id`, `target_chain`, `partner_chain`, `sites_json`, etc.; PPI's
   literature handoff is close but not identical — **VERIFY** the exact
   field-by-field gap by diffing `_stage_target_intel`'s emitted fields
   against `_stage_literature`'s).
3. Call `_run_binder_track(..., start_from="interface", ...)` (or a new
   thin wrapper) with that translated handoff pre-loaded, so binder's own
   `target_intel`/candidate-search reasoning is skipped entirely (PPI
   already decided the target) and binder's `interface` stage runs next —
   this is the stage that already calls `complex-structure-analysis` AND
   (per finding #3 above) the verify functions, so a PPI-originated run
   would inherit those guards for free once routed through this stage
   rather than PPI's own `structure` stage.
4. Everything from `interface` onward (trim → binder_spec → pilot →
   calibration → production → binder_scoring → binder_summary) runs
   exactly as it does for `--workflow binder` today, unmodified.
5. Requires deciding: does this new mode still need `--pdb`/`--pathway-mode`
   CLI compatibility, does it require `--project` (probably yes, given
   point 1 above), and what does `--workflow` mean now — a third value
   (`hybrid`?) or does `ppi` itself just start doing this by default per
   the maintainer's actual request ("make it the default")?

## Before starting real implementation

- Re-read CLAUDE.md's binder-track section in full; it's the single
  densest source of "this will silently break if you don't know X" facts
  in the repo.
- Confirm with the maintainer: is `--workflow ppi` supposed to KEEP the
  BoltzGen path as an option (config/CLI flag), or is this a full
  replacement with BoltzGen becoming legacy/removed? The "medium vs major"
  estimate changes a lot depending on whether both paths must be
  maintained afterward.
- The IL7RA end-to-end binder-track run (`projects/il7ra_e2e/`, see
  `diary.md`) is the best real evidence of foundry's output quality on a
  fresh target — worth reviewing its final report once production finishes
  before treating "binder track is excellent" as fully proven beyond the
  PD-L1/KRAS/CD79b/8TAC campaigns already on record.
- Don't start with a big-bang merge of the two stage machines. Start with
  finding #3 (the verify-function gap) as an isolated, low-risk first
  change, then the bridge sketch above as a SEPARATE new code path
  (e.g. gated behind an explicit flag) rather than changing `ppi`'s
  existing behavior outright — this repo's own history (the compute-auto
  and provider-default changes earlier this session) shows the pattern of
  "add opt-in, prove it, then flip the default" working well here.
