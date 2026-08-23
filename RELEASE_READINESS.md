# Release Readiness — Audit & To-Do

Status: **audit complete, fixes not yet started** (as of 2026-08-23). This is a
living checklist, not a one-shot report — check items off as they're done and
add new findings as they surface. Work is expected to span multiple sessions;
each item below is written to be actionable by a fresh Claude Code session
reading just this file + `CLAUDE.md`, without re-deriving context.

**Scope, as directed**: get the codebase into good shape to eventually share
as a **CLI- and coding-agent-driven tool**. The hosted web platform
(`web/backend/`, `web/frontend/`) is explicitly **out of scope for now** — not
deleted, not actively maintained, findings about it are recorded below for
whenever that direction is revisited, but nothing in the CLI path should be
blocked on fixing `web/`.

**Method**: five parallel research agents audited the repo (security, code
quality/redundancy, documentation consistency, packaging/portability, and a
separate local-dashboard design-ideation pass), then findings were
deduplicated, cross-checked against each other, and the highest-severity
security claim was independently re-verified by hand before being written up
here. Effort estimates are rough (trivial < small < medium < large).

---

## Already done this session (2026-08-23)

- [x] Archived `RELEASE_PLAN.md` and `DEPMAP_INTEGRATION_PLAN.md` to
  `archive/`, each with a header noting why it's outdated (both assumed a
  community-hosted web platform / were superseded by later implementation).
- [x] Binder-track compute now defaults to `--compute auto` (was `local`);
  auto-decides local-vs-cluster at the calibration gate via
  `--max-local-hours` (default 48h) and `design.cluster.n_gpus` (now 8).
- [x] Pipeline-wide default LLM provider changed `claude` → `gemini`
  (`gemini-3.7-flash`) across `PipelineRunner`, `run_pipeline.py --provider`,
  `run_skill.py --model`; `models.gemini.refusal_fallbacks` added.
- [x] New `src/ppi_report.py` (+ `scripts/generate_ppi_report.py`) mirrors
  `src/binder_report.py` for the PPI track; both now share
  `src/report_common.py` and `src/report_templates/_shared/{base.css,base.js}`.
- [x] Doc audit confirms all three changes above are already fully and
  correctly reflected in README.md and CLAUDE.md — no follow-up needed there.

---

## Tier 0 — do first (blockers, all trivial-to-small effort)

**All four items below are DONE (2026-08-23).** Delegated to two subagents
(config fixes; the security fix + dedup) plus the LICENSE decision handled
directly with the maintainer. All verified: 281/281 tests passing (269
baseline + 12 new regression tests for the path-confinement fix), and the
exploit was independently reproduced-then-confirmed-closed by hand
(`resolve("/etc/hostname", root=...)` now returns a path under root instead
of the raw outside path).

1. [x] **`requirements.txt` is missing packages the code actually imports.**
   Verified by hand: `gemmi`, `biopython`, `biotite` are in
   `pyproject.toml`'s `dependencies` but **absent from `requirements.txt`**,
   which is the file README.md's setup section actually tells a new user to
   `pip install -r`. A fresh install following the documented path breaks
   the first time any structure-tools code runs.
   → Fix: add the three missing lines to `requirements.txt`, or (better,
   see Tier 1 packaging item) stop maintaining two dependency lists at all.

2. [x] **`.env.example` does not exist**, referenced by README's setup
   instructions (`cp .env.example .env`) and by the "Migrating to a new
   machine" checklist. Confirmed deleted in git history (commit `d71793c`,
   "Add web platform..."), and currently shows as an uncommitted deletion in
   `git status` even at HEAD in some checks — verify current state with
   `git log -- .env.example` / `git show HEAD:.env.example` before acting.
   → Fix: recreate `.env.example` at repo root with placeholder values for
   every env var the CLI/library code actually reads (grep confirmed list,
   `web/`-only vars excluded since that's out of scope):
   `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `NCBI_EMAIL`, `NCBI_API_KEY`,
   `S2_API_KEY` (Semantic Scholar). Do **not** template
   `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` (that's this workstation's
   TLS-inspection workaround, not a general requirement) — a comment noting
   it may be needed behind a corporate TLS-inspecting proxy is enough.

3. [x] **`_resolve()` path-confinement bypass — verified exploitable.**
   `src/skill_runner.py:50-51` and the equivalent in
   `src/structure_tools_server.py`:
   ```python
   p = Path(file_path)
   if p.exists():
       return str(p)          # <- no _ROOT confinement check at all
   ```
   Any tool-call argument naming a path that already exists anywhere on the
   filesystem is returned unmodified, bypassing the "recover under `_ROOT`"
   logic that exists for every other case. This feeds every read tool
   gated by `_resolve()` (`analyze_interface`, `get_sequence_map`,
   `score_surface_patch`, etc. — arbitrary file read) **and** `write_file`
   (`src/skill_runner.py:1279-1284`, confirmed by direct read: resolves via
   `_resolve()` then unconditionally `dest.write_text(...)` — **arbitrary
   file overwrite** for any existing, writable path, content fully
   LLM/tool-call-controlled). Practical trigger requires an LLM to emit
   such a path — via a compromised/adversarial skill prompt, adversarial
   content in a scraped paper the model is reasoning over, or plain model
   error — but the primitive itself is real and unguarded today.
   **Fixed as**: new shared `src/_path_resolve.py` (`resolve(file_path, *,
   root)`), replacing both original copies. An existing path is only
   fast-returned if `p.resolve().is_relative_to(root.resolve())`; an
   existing-but-outside-root path now falls through into the *same*
   recovery machinery already used for non-existent paths (tail-walk under
   root → canonical `data/structures/` basename check → final rejoin),
   rather than raising — reuses existing, already-tested logic instead of
   inventing new error handling. `skill_runner.py` and
   `structure_tools_server.py` both now thin-wrap this shared function with
   their own `root`. The two original implementations were confirmed
   byte-for-byte identical in logic before unification (no hidden
   behavioral divergence to reconcile). 12 new tests in
   `tests/test_path_resolve.py` cover the fix, the no-regression case, and
   the full existing recovery contract. This also closes Tier 1 item 23
   (the duplicated-helper dedup) as a side effect — done together, not
   separately.

4. [x] **No LICENSE file.** Blocks any legal redistribution regardless of
   code quality. → Maintainer chose **MIT**. `LICENSE` added at repo root;
   `pyproject.toml` now has `license = "MIT"` and `readme = "README.md"`.

---

## Tier 1 — should fix before any public release

### Security (web-scoped, still worth fixing even if `web/` isn't a near-term priority)

5. **Cross-tenant BYOK API-key leak in Celery workers**
   (`web/backend/tasks.py`, ~5 call sites: `run_pipeline_task`,
   `resume_pipeline_task`, `retry_run_task`, `run_optimizer_task`,
   `run_binder_optimizer_task`). Pattern: `if user and user.anthropic_key_enc:
   os.environ["ANTHROPIC_API_KEY"] = decrypt_key(...)` — set-if-present,
   never cleared. A long-lived worker process handling task N for a keyed
   user, then task N+1 for a user with no stored key, silently reuses and
   bills the previous user's key for the second task. The existing code
   comment's safety claim ("worker_prefetch_multiplier=1 makes this safe")
   only addresses *concurrent* access, not this *sequential* leak.
   → Fix: explicitly reset/unset the env var at the start and end of every
   task, not just set-if-present.
6. OAuth CSRF: no `state` parameter on the GitHub OAuth flow
   (`web/backend/routers/auth.py:29-38`). Add and verify one.
7. OAuth callback logs the full URL including the authorization `code`
   (`web/backend/routers/auth.py:51-52`) — remove/redact.
8. JWT handed to frontend via URL query string
   (`.../auth.py:81`, `?token=...`) — logs/history/Referer exposure,
   worse behind a TLS-inspecting proxy (this workstation has one). Swap for
   a one-time exchange code redeemed via POST.
9. `slowapi` is a declared dependency but never wired into `app.py` — no
   rate limiting exists anywhere in the backend. Either wire it up on
   auth/run-creation endpoints or drop the dependency.
10. `RunCreate.pdb_id` (`web/backend/routers/runs.py`) isn't validated
    against the `^[A-Za-z0-9]{4}$` pattern enforced everywhere else in the
    same file — low practical exploitability today, but inconsistent.
    Apply the same regex at creation time.
11. (defense-in-depth, low urgency) `src/foundry_runner.py`'s
    `run_campaign.sh` template interpolates config values into bash text
    unquoted — currently safe (every value is operator-trusted config or
    `slugify()`-sanitized), but quote interpolations as insurance against a
    future freeform field reaching the template.

### Packaging / portability

12. **Hardcoded machine-specific path defaults in source (not just
    config)** — the real blocker-class instance of this, beyond the
    expected/OK config.yaml entries:
    - `src/foundry_runner.py:488` —
      `foundry=f.get("root", "/home/m.uckelmann_cbs-niob.local/code/foundry")`
    - `src/foundry_stages.py:217` and `:232` — same path as an argparse
      `--foundry` default, twice.
    → Fix: these should have no machine-specific fallback at all — require
    the value from config/CLI and error clearly if absent, rather than
    silently defaulting to one person's checkout.
    - `.mcp.json` — currently a Windows path from a *different* machine
      than this checkout (`C:\Users\micha\...`), already flagged as
      known/documented in CLAUDE.md's migration section. No portable fix
      exists in the MCP config format itself; the real fix is a
      `scripts/setup_mcp_json.py` that generates it from the current venv's
      interpreter path (`scripts/launch_mcp.py` already computes `root`
      from `__file__` — reuse that logic).
    - README.md:491 points to `/home/m.uckelmann_cbs-niob.local/pyrosetta/SETUP_NOTES.md`
      — a path **outside this repo**, unreachable by anyone else. Fold
      those setup notes into the repo (e.g. `docs/pyrosetta_setup.md`) so
      the README reference actually resolves for a new user.
    - `tests/conftest.py:16`, `tests/test_foundry.py:26-27`,
      `scripts/test_e2e_binder.py:34` reference a reference-campaign path
      under `/home/m.uckelmann_cbs-niob.local/data/BCR/...` — these are
      already gracefully `skipif`-guarded when absent, low priority, but
      could be moved to an env var for cleanliness.

13. **`pyproject.toml` gaps**: no `license`, `classifiers`, `authors`,
    `readme` field, project URLs, or console-script entry points (despite
    ~15+ primary `scripts/*.py` CLI tools users currently invoke via
    `python scripts/foo.py`). Also a direct contradiction:
    `pyproject.toml:5` says `requires-python = ">=3.12"` but
    `README.md:11` says "Python 3.10+" — pick one and fix the other.

14. **Two redundant, drifting dependency lists** (`requirements.txt` vs
    `pyproject.toml`) — Tier 0 item 1 is a symptom of this structural
    problem. Longer-term fix: pick one source of truth (`pyproject.toml` +
    `uv`/`pip install -e .`, since `uv.lock` already exists and looks
    current) and either delete `requirements.txt`/`requirements-web.txt` or
    generate them from `pyproject.toml` so they can't drift again. Update
    README's setup instructions to match whichever is chosen.

15. **No CI.** No `.github/workflows/` or equivalent. 269 tests exist and
    reportedly pass but nothing runs them automatically on push/PR. Add a
    basic workflow (`pytest tests/ -q`) at minimum; consider also running
    the doc-consistency-style checks (stale model ID grep, etc.) as a cheap
    lint step given how often they've drifted historically per diary.md.

16. **Repo-root debris**: `3kys.cif`, `3KYS_TEAD1_YAP1_region1_boltzgen.cif`,
    `ENPP1_5DLT_cyclic_peptide_boltzgen.cif`, `test_protein_protein.cif`,
    `test_protein_protein_boltzgen.cif`, `mesothelioma_target_network.cyjs`
    — all untracked test-output-looking files sitting at repo root, none
    covered by `.gitignore`. Also `logs/` and `projects/` are untracked and
    uncovered despite `projects/` being a first-class feature directory
    (per-user campaign state — should very likely be gitignored on
    purpose, not accidentally uncovered). → Clean up the stray files
    (confirm with the user before deleting anything that might be wanted —
    they may be intentional test fixtures) and add `*.cif`, `*.cyjs`,
    `logs/`, `projects/` to `.gitignore`.

17. **No bootstrap/setup script.** A `scripts/setup.sh` or `make setup`
    chaining venv creation → deps install → `.env` template copy → PDB
    metadata cache fetch → a sanity-check that reports which of
    BoltzGen/PyRosetta/foundry/protenix are/aren't found would remove most
    of the current manual first-run friction (the GPU-tool installs
    themselves can't be automated, but everything else can).

### Documentation consistency

18. **README skill table is missing 2 real skills**: `binder-target-intel`
    and `design-analyst` exist and are used internally by
    `_STAGE_TO_SKILL` / runnable via `run_skill.py`, but aren't listed in
    README's "Available skills" table (§5). Add rows, or add a one-line
    note that they're pipeline-internal stage skills rather than typical
    standalone-CLI skills, whichever is more accurate to how they're
    actually used.
19. **`skills/enzyme-active-site-modeling/` has no `SKILL.md`** — only a
    `scripts/` subdirectory. Not a functioning skill yet (added in the
    most recent commit, "Add de novo enzyme active-site design workflow" —
    may be genuinely mid-flight on the enzyme branch's own timeline).
    Confirm status with the user; if truly incomplete, leave undocumented
    until it has a `SKILL.md`, don't add it to the table prematurely.
20. **Stale example model ID** `claude-opus-4-6` in two places —
    `README.md:304` and `scripts/run_pipeline.py:309` (a `--model-id`
    help-text example) — should be `claude-opus-5` per current naming
    convention. `tests/test_pipeline_stages.py::test_no_stale_model_ids_in_the_config`
    already bans this pattern in config files but doesn't cover README/CLI
    help text — consider extending that test's grep scope.
21. `src/curator.py:267` falls back to `"gemini-2.0-flash"` (a very old
    generation) when `curation.gemini_model` is absent from config — low
    real-world impact since config.yaml always sets it explicitly, but the
    fallback itself should be bumped to the current `gemini-3.x` family for
    consistency.

### Code quality / redundancy

22. **7 of 9 packaged `skills/*.zip` files are stale** relative to their
    live `SKILL.md` twins (MD5 mismatch confirmed on the zipped
    `SKILL.md` vs the live file for `binder-optimizer`, `complex-expert`,
    `corpus-explorer`, `molecular-biology-expert`, `orchestrator`,
    `pathway-expert`, `protein-design-script`, `complex-structure-analysis`
    — all dated May 21 while their `SKILL.md`s have since been edited up
    to Aug 21). `wildcard-expert`, `binder-target-intel`, `design-analyst`,
    `enzyme-active-site-modeling` have no `.zip` at all. CLAUDE.md
    describes these as "packaged artifacts — regenerate them, don't
    hand-edit," but **no script or README command currently regenerates
    them** — that tooling doesn't exist yet.
    → Fix: write `scripts/package_skills.py` (zip each `skills/<name>/`
    directory), run it once to refresh all zips, document the command in
    README, and optionally add a CI/pre-commit check that fails if a
    `SKILL.md` is newer than its `.zip`.
23. [x] **DONE — Duplicated path-resolution helper**, maintainer-flagged as
    deferred debt back in the 2026-05-27 diary entry. Fixed together with
    Tier 0 item 3 (the security fix), as planned — see that item for
    details.
24. **`design-analyst`'s "liability" rubric has no matching data on the
    binder track.** `skills/design-analyst/SKILL.md:58-150` mandates a
    liability/developability sentence in every summary; `_BINDER_SUMMARY_COLS`
    (`src/pipeline_runner.py:1764-1785`, binder track) has no `liability_*`
    fields — those only exist in the PPI track's `_SUMMARY_CONTEXT_COLS`
    (sourced from BoltzGen). Every binder-track `binder_summary` stage is
    prompted with rubric language it structurally cannot satisfy, risking
    a boilerplate or hallucinated "no liability data" line every run. This
    was flagged as "undecided: real gap or dead prompt language" as far
    back as the 2026-08-22/23 diary entry and is still unresolved — this
    session's audit confirms it's still live. **Needs a decision**, not
    just a fix: either add a binder-track liability signal (if one is
    meaningfully computable from foundry/RF3 output) or split the rubric
    section so the liability request is PPI-only.
25. `_stage_binder_scoring` (`src/pipeline_runner.py:1671`) still only
    knows the local `FoundryPaths` shape — no `cluster_cfg` threading for
    the **production** stage (calibration already got this threading in
    the 2026-08-23 session's compute-auto work). Blocks an
    `auto`-routed cluster production run from being scored end-to-end the
    same way a local run is. Not urgent per diary ("not yet needed") but
    will become one the first time `--compute auto` actually routes a real
    production run to the cluster.
26. `scripts/resume_cluster_calibration.py` remains a standalone
    workaround script because per-site cluster/production resume has no
    path through the normal `--start-from` CLI flag. Extend `--start-from`
    to accept a site-scoped stage argument and retire the script, per the
    plan already sketched in CLAUDE.md's cluster-compute section.
27. Two real bugs fixed live against production data this session/last
    (`src/handoff.py`'s `_clean_atom_list`, `src/binder_metrics.py`'s
    `iter_protenix_refolds`) are still **unguarded by any regression
    test** — confirmed via grep, no `test_*` references either name.
    Backfill tests using the exact failure shapes CLAUDE.md documents
    (malformed RFD3 atom-list prose; Protenix's per-design-subdirectory
    output layout).
28. `src/ppi_report.py`'s histogram gate-marker lines are drawn from
    live `config.yaml` rather than a frozen per-run threshold record
    (unlike the binder track's `calibration.json`) — cosmetic only, the
    funnel/survivor counts themselves are read from the run's own frozen
    `filter_stats.txt` and can't drift. Low priority; fix by persisting
    the thresholds a PPI run actually scored against if it ever becomes
    confusing in practice.
29. `pyproject.toml` has no `[tool.pytest.ini_options]` marker
    registration — `@pytest.mark.slow` in `tests/test_membrane_topology.py`
    triggers a `PytestUnknownMarkWarning` on every collection. Trivial fix,
    register the mark.

---

## Tier 2 — nice to have, no urgency

- `python-jose[cryptography]` (web JWT lib) is less actively maintained
  than `pyjwt`; current usage is safe (algorithm pinned to HS256) but worth
  migrating eventually if `web/` is ever revisited.
- Several standalone utility scripts (`scripts/check_input_pdb.py`,
  `download_ba1_structures.py`, `search_fingerprints.py`,
  `merge_config_keywords.py`, `convert_complex_list.py`) aren't mentioned
  anywhere in README.md. Either document them briefly or move them to a
  `scripts/dev/` or `scripts/maintenance/` subfolder so a new reader can
  tell primary CLI entry points apart from one-off utilities at a glance.
- `config.yaml` vs the three `config_*.yaml` alternates: confirmed **not**
  live drift (the alternates are only ever loaded via `--config` by
  `fetch_papers.py`/`curate_papers.py`, which never reads the `design`/
  `models` blocks `config.yaml` has and the alternates don't) — but worth
  a one-line CLAUDE.md/README note making this explicit so a future editor
  doesn't "fix" it by copying blocks over unnecessarily.
- Gemini's API key is sent as a URL query parameter
  (`src/skill_runner.py:1876`) — this is Google's own documented REST auth
  scheme, not a bug in this repo, but worth remembering if request logging
  is ever added anywhere in the request path.

---

## Open design question — local dashboard / minimal entry point (not scheduled work)

The maintainer asked for exploration, not a decision, on: *"eventually a
minimal entry point... a chat interface in html/dashboard that's hosted
locally on the workstation and can choreograph the whole campaign."* A
dedicated research pass produced four concept directions, summarized here;
see the full proposal (recovered from this session's agent transcript if
needed, or re-run the ideation) for complete reasoning:

1. **Static report gallery + JSON status feed** (minimal). A tiny local
   HTTP server lists projects/runs, links straight to the already-generated
   `report.html` files (binder + PPI tracks both produce these, self-contained,
   with an embedded Mol* viewer), with a live status badge per run read from
   `src/job_registry.py`/`src/foundry_runner.py`'s existing disk-state
   progress objects. No launch button, no chat, no auth. **Effort: small**
   (hours, reuses everything, near-zero risk).
2. **Kickoff + monitor dashboard**. Adds a launch form that shells out to
   the exact `run_pipeline.py` CLI invocation, plus a UI for the
   `PipelinePausedError` choice points (currently resolved by re-invoking
   the CLI). **Effort: medium** — the choice-point UI is the hard part, and
   any launch path must route through `job_registry.py` exactly (not a
   bespoke subprocess call) to avoid two campaigns racing one GPU.
3. **Repurpose the existing `web/` platform, stripped down** to single-tenant
   (drop OAuth, Fernet BYOK-at-rest, Celery/Redis — the GPU long-pole is
   already outside Celery's control via `job_registry`). **Effort: large**
   — auth-scoped queries and task boundaries are baked into most of
   `web/backend/`'s ~2,600 lines; this kind of surgery often costs as much
   as a rewrite. **Recommended against starting here** — it optimizes for
   multi-tenant requirements (OAuth, encrypted-at-rest BYOK, a task queue)
   that the project's own direction has already walked back once
   (see the archived `RELEASE_PLAN.md`).
4. **No new server — a few Claude Code slash commands** wrapping the exact
   CLI calls already typed by hand (`/campaign-status`, `/campaign-launch`,
   `/campaign-report`). **Effort: tiny.**

**Recommendation from the ideation pass**: start with (1), pair with (4),
and only consider (2) after a few weeks confirm the itch is real ("I want
to glance at progress from a browser tab" vs. "I don't want to type CLI
flags"). Do not start with (3).

**Open questions only the maintainer can answer** (from the ideation pass):
- Strictly single-user/single-workstation, or ever LAN/phone-reachable
  during a multi-day campaign? (Changes whether "localhost-only, no auth"
  stays sufficient.)
- What does "spawn coding agents to supervise" concretely mean — literally
  shelling out to the `claude` CLI as a subprocess, or a notification feed
  over existing pause-point state? Very different builds.
- Is retyping `--target`/`--project`/`--budget` into a web form actually
  saving time over the CLI (which already accepts them directly), or is
  the form's real value the pause-point resume UI regardless?
- Any appetite for new persistent state (even SQLite), or should this stay
  a strictly stateless, read-through-disk viewer — matching the philosophy
  already used everywhere else in this pipeline
  (`manifest.json` + `job_registry` as sole sources of truth)?

This section should stay **undecided** until the maintainer weighs in —
don't start building any of it as part of working through the Tier 0-2
checklist above.

---

## What was checked and found clean (don't re-audit these without new reason to)

- No `shell=True`/`os.system` anywhere; every subprocess call uses an argv
  list. No `pickle`/`eval`/`exec`/unsafe `yaml.load` anywhere.
- No committed secrets (`.env` correctly gitignored and never tracked; no
  `sk-`/`AIza`/PEM patterns found in any tracked file).
- `web/backend/crypto.py`'s Fernet key handling: sourced from env only,
  never logged/DB-persisted, raises loudly if unset.
- Web backend: CORS scoped (not wildcard), JWT algorithm pinned (no
  alg-confusion surface), every router checks resource ownership before
  touching a file, path-traversal-safe filename handling on all upload/
  download endpoints except the one gap noted in Tier 1 item 10.
- `report_common.py` / `report_templates/_shared/` confirmed genuinely
  shared (not a false/diverged abstraction) between `binder_report.py` and
  `ppi_report.py`.
- `design_metrics`/`design_ranking` (PPI) vs `binder_metrics`/`binder_ranking`
  (binder track) confirmed still fully decoupled per CLAUDE.md's stated
  rationale — not redundant, don't unify.
- `os.scandir` discipline (never `ls`/glob on large stage directories)
  holds in every hot path checked.
- No dead top-level modules in `src/`.
- Today's provider/compute default changes are fully and correctly
  documented in both README.md and CLAUDE.md already — no doc follow-up
  needed for those specifically.
- Test suite: 269 tests collect cleanly; all `skip`/`xfail` markers are
  legitimate data/fixture-availability guards, not abandoned tests.
