# Release Readiness — Audit & To-Do

Status (as of 2026-08-23): **Tier 0 and Tier 1 both done**, except item 16
(repo-root debris cleanup — not deleted, just gitignored) and item 28
(cosmetic, low priority, deliberately left open). Tier 2 and the open
dashboard design question remain, as does actually committing/pushing this
round of work. This is a living checklist, not a one-shot report — check
items off as they're done and add new findings as they surface. Each item
below is written to be actionable by a fresh Claude Code session reading
just this file + `CLAUDE.md`, without re-deriving context.

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
   code quality. → Maintainer chose **PolyForm Noncommercial 1.0.0** (MIT
   initially; changed before the repo went public, while that was still a
   free choice — see `docs/licensing.md`). `LICENSE` at repo root;
   `pyproject.toml` carries the SPDX id and `readme = "README.md"`.

---

## Tier 1 — should fix before any public release

### Security (web-scoped, still worth fixing even if `web/` isn't a near-term priority)

**All 7 items below are DONE (2026-08-23).** Delegated to a subagent that
completed items 5-8 and 11 before hitting the session's account-level usage
limit; items 9-10 finished directly afterward. 291/291 tests passing
throughout (no test in the suite imports `web.backend`, so `slowapi` not
being installed in the dev venv doesn't block CI).

5. [x] **Cross-tenant BYOK API-key leak in Celery workers.** Fixed via a new
   `_scoped_api_keys(user)` context manager in `web/backend/tasks.py`: snapshots
   the original `ANTHROPIC_API_KEY`/`GEMINI_API_KEY` env values, injects the
   user's decrypted key(s) for the task's duration, and restores (or clears)
   the originals in a `finally`. All 5 call sites (`run_pipeline_task`,
   `resume_pipeline_task`, `retry_run_task`, `run_optimizer_task`,
   `run_binder_optimizer_task`) now route through it — verified by grep, no
   stray `os.environ["ANTHROPIC_API_KEY"] = ...` left outside the helper.
6. [x] **OAuth CSRF.** `web/backend/routers/auth.py`'s `/github` now mints a
   random `state` (`secrets.token_urlsafe(32)`), stashes it in a short-lived
   httponly cookie (no existing session store to reuse — a cookie only needs
   to survive one redirect round trip), and `/callback` verifies it with
   `secrets.compare_digest` before proceeding, rejecting with 400 on
   mismatch/missing.
7. [x] **OAuth callback logging.** The `logger.info(f"... full URL: {request.url}")`
   / query-params log lines (which included GitHub's authorization `code`)
   are gone — replaced with a bare "OAuth callback received".
8. [x] **JWT in URL.** `/callback` now mints a short-lived (60s), single-use
   opaque exchange code (in-memory dict, TTL-cleaned on each mint — the
   backend runs single-process today per `app.py`'s own docstring; a note in
   the code says to move to Redis if that ever changes) and redirects with
   `?code=...` instead of `?token=...`. New `POST /auth/token-exchange`
   trades the code for the real JWT in the response body. Frontend
   (`web/frontend/src/pages/AuthCallback.tsx`, `src/lib/api.ts`) updated to
   POST the code instead of reading `?token=` directly.
9. [x] **No rate limiting.** New `web/backend/rate_limit.py` holds a shared
   `slowapi.Limiter` (split into its own module rather than living in
   `app.py`, to avoid a circular import — routers need to import it too).
   Wired into `app.py` (`app.state.limiter` + the `RateLimitExceeded`
   exception handler) and applied via `@limiter.limit(...)` to
   `POST /auth/github` (`10/minute`; needed adding a `request: Request`
   param the endpoint didn't previously take, since slowapi's decorator
   requires one) and `POST /projects/{id}/runs` (`5/minute`).
10. [x] **`RunCreate.pdb_id` validation.** Now `Field(default=None,
    pattern=r"^[A-Za-z0-9]{4}$")`, matching `structures.py`'s existing
    convention exactly. Verified the pydantic v2 pattern constraint
    correctly allows `None` through while rejecting a malformed string
    (e.g. a traversal-shaped value) with a `ValidationError`.
11. [x] **Subprocess template quoting.** `src/foundry_runner.py`'s
    `_DRIVER_TEMPLATE` now double-quotes every interpolated value
    (`FOUNDRY="{foundry}"`, `--checkpoint "{mpnn_ckpt}"`, etc.) — defense in
    depth, no behavior change for today's operator-trusted/`slugify()`d
    inputs.

### Packaging / portability

12. [x] **Hardcoded machine-specific path defaults in source.**
    - `src/foundry_runner.py`'s `write_campaign_driver` no longer falls back
      to `/home/.../code/foundry` — raises `FoundryValidationError` naming
      the missing `design.foundry.root` config key instead.
    - `src/foundry_stages.py`'s two `--foundry` argparse args are now
      `required=True` (verified the only real call path — the generated
      `run_campaign.sh` — always passes it explicitly; the hardcoded
      default was dead outside manual CLI invocation).
    - `.mcp.json` regenerated for this workstation via new
      `scripts/setup_mcp_json.py` (resolves `.venv/bin/python3` from its own
      `__file__`, not `sys.executable`); original backed up to
      `.mcp.json.bak`. CLAUDE.md's migration section now points to the
      script as the automated alternative to hand-editing.
    - PyRosetta setup notes folded into the repo at `docs/pyrosetta_setup.md`
      (copied faithfully from the external path, genericized for any
      reader); README's two references updated to point there instead.
    - `tests/conftest.py`, `tests/test_foundry.py`,
      `scripts/test_e2e_binder.py`'s BCR reference-campaign path now reads
      `LPT_BCR_REFERENCE_DIR` (env var) with the original hardcoded path as
      fallback default — `skipif` guard behavior unchanged and verified
      both ways (env unset: runs for real; env pointed at a bogus path: all
      13 dependent tests skip with the expected reason).

13. [x] **`pyproject.toml` gaps.** Added `authors` (git author identity),
    `classifiers` (Development Status :: 3 - Alpha, Science/Research, MIT,
    Python 3.12, Bio-Informatics — deliberately not "Production/Stable"),
    `[project.urls] Repository`, and console-script entry points for the
    4 scripts that already exposed a callable `main()`
    (`lpt-run-pipeline`, `lpt-run-skill`, `lpt-fetch-papers`,
    `lpt-curate-papers`, all `scripts.<mod>:main`). Verified these actually
    resolve and run (`--help`) from a `pip install -e .` venv invoked from
    a directory outside the repo — works because hatchling's editable
    install for this project puts the whole repo root on `sys.path` (not
    just `src/`), and each script computes its own root from `__file__`
    rather than cwd. Did not convert the other ~30 `scripts/*.py` files —
    that requires restructuring their `__main__` blocks into importable
    `main()` functions first, out of scope here. The `requires-python`
    3.10-vs-3.12 contradiction with README was already fixed earlier this
    session (README now says 3.12+).

14. [x] **Two redundant, drifting dependency lists.** Deleted
    `requirements.txt` and `requirements-web.txt`; `pyproject.toml` is now
    the single source of truth (`uv.lock` already tracked it). Updated
    every `pip install -r requirements*.txt` mention in README.md
    (Requirements section, Migrating-to-a-new-machine steps, project
    structure tree) to `pip install -e .` / `pip install -e ".[web,dev]"`.
    `diary.md` mentions were left alone (historical log, not living docs).

15. [x] **No CI.** Added `.github/workflows/tests.yml`: checkout@v4,
    setup-python@v5 (3.12), `pip install -e ".[dev]"`, `pytest tests/ -q`,
    on push/PR to `main`. Kept minimal per instructions — no GPU-dependent
    jobs, no extra lint step.

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

17. [x] **No bootstrap/setup script.** Added `scripts/setup.sh`: creates
    `.venv` if missing, `pip install -e ".[dev]"`, copies `.env.example` ->
    `.env` only if `.env` doesn't already exist, runs
    `scripts/fetch_pdb_metadata.py`, then a diagnostic that reads
    `design.workstation.boltzgen_executable`, `design.pyrosetta.python_executable`,
    and `design.foundry.root` from `config.yaml` and reports whether each
    path exists on this machine — never fails the script if one is
    missing. Referenced from README's Requirements section as an optional
    fast-path; manual steps stay documented. Ran it end-to-end on this
    machine: correctly reused the existing venv, left the existing `.env`
    alone, and reported all three GPU tools found.

### Documentation consistency

**All 4 items below are DONE (2026-08-23).**

18. [x] **README skill table.** Added `binder-target-intel` and
    `design-analyst` rows, descriptions drawn from each skill's own
    `SKILL.md` frontmatter.
19. [x] **`skills/enzyme-active-site-modeling/` status — checked, correctly
    left undocumented.** Confirmed it still has no `SKILL.md` (only
    `scripts/__pycache__/*.pyc`, no source even) — not added to the README
    table, per the original instruction to leave incomplete skills out.
20. [x] **Stale example model ID.** `claude-opus-4-6` → `claude-opus-5` in
    both `README.md` and `scripts/run_pipeline.py`'s `--model-id` help text.
    `test_no_stale_model_ids_in_the_config` extended with a sibling test
    (`test_no_stale_model_ids_in_docs_and_cli_help`) that greps README.md
    and run_pipeline.py for the same stale-ID class, so this can't silently
    regress again.
21. [x] **`src/curator.py`'s stale Gemini fallback.** Bumped
    `"gemini-2.0-flash"` → `"gemini-3.7-flash"`, matching
    `config.yaml`'s `models.gemini.default`. Confirmed `curation.gemini_model`
    is set explicitly in all four config files, so this fallback is a true
    dead path in normal operation — bumped for consistency only.

### Code quality / redundancy

**Items 22, 23, 24, 27, 29 are DONE (2026-08-23). Items 25 and 26 are ALSO
DONE (2026-08-23) — real feature engineering, not just cleanup, completed
by a subagent and independently re-verified (diff read + syntax-checked +
full suite re-run). Only item 28 remains open (cosmetic, low priority).**

22. [x] **Stale packaged `skills/*.zip` files.** New `scripts/package_skills.py`
    zips every `skills/<name>/` directory that has a `SKILL.md` (matching
    the existing non-stale zip's exact convention: contents under a
    top-level `<name>/` path, not flattened). Ran it once for real — all 12
    skill dirs with a `SKILL.md` packaged, byte-for-byte verified fresh
    afterward. `enzyme-active-site-modeling` correctly skipped (no
    `SKILL.md`, see item 19). One-line README mention added.
23. [x] **DONE — Duplicated path-resolution helper**, maintainer-flagged as
    deferred debt back in the 2026-05-27 diary entry. Fixed together with
    Tier 0 item 3 (the security fix), as planned — see that item for
    details.
24. [x] **`design-analyst`'s liability rubric — split PPI-only (maintainer's
    decision).** `skills/design-analyst/SKILL.md` now conditions the
    liability-assessment instruction on whether `liability_*` columns are
    actually present in the provided data — mandatory when they are (PPI
    track), explicitly omitted when they aren't (binder track), rather than
    "always mandatory" regardless of track. Conditioned on column
    presence, not a track label, which is more robust to future column
    changes. No changes to `_SUMMARY_CONTEXT_COLS`/`_BINDER_SUMMARY_COLS`
    (no new data added, per the decision — prompt-text fix only).
    `skills/design-analyst.zip` re-packaged after the edit.
25. [x] **`_stage_binder_scoring` cluster_cfg threading.** New
    `_binder_compute_for_mode` (resolves which compute path a given mode's
    `_run_gpu_stage` call actually took, reusing `_resolve_production_plan`
    for the "production" mode specifically — including the fresh-process-resume
    case where `calibration.json` on disk is the only record) and
    `_cluster_paths_for_mode` (reconstructs the `ClusterPaths` needed to
    locate `refold_dir` for scoring, without re-staging). Both call sites of
    `_stage_binder_scoring` updated to pass `calib=calib` through. A
    `--compute auto`/`cluster` production campaign can now be scored the
    same way a local one is. Verified: full diff read, both call sites
    confirmed updated, 291/291 tests still passing.
26. [x] **Per-site `--start-from` resume.** New `PipelineRunner.resume_site_stage`
    is the shared implementation behind both the standalone
    `scripts/resume_cluster_calibration.py` (kept for backward
    compatibility, now a thin caller) and a new `run_pipeline.py --site
    <site_id>` flag (requires `--start-from calibration` and an exact
    `--n-batches` match, forces `--compute cluster` — validated with clear
    CLI errors otherwise). Only `stage="calibration"` is supported (documented
    reasoning: production has no single owning method the same way).
    Verified: full diff read, both callers confirmed working, 291/291 tests
    passing.
27. [x] **Regression tests backfilled** for `iter_protenix_refolds`
    (`tests/test_binder_metrics.py` — synthetic per-design-subdirectory
    tree + a flat-file negative case) and `_clean_atom_list`
    (`tests/test_binder_report.py`'s `TestCleanAtomList` — 7 cases including
    the exact malformed-prose example from the original incident, a clean
    passthrough, and the no-atoms-matched fallback, all checked against the
    real implementation rather than assumed).
29. [x] **Pytest marker registration.** `pyproject.toml` now has
    `[tool.pytest.ini_options]` with `slow` registered (the only custom
    marker in use, confirmed by grepping all of `tests/`). Verified the
    `PytestUnknownMarkWarning` is gone, including under
    `-W error::pytest.PytestUnknownMarkWarning`.

28. **Still open.** `src/ppi_report.py`'s histogram gate-marker lines are
    drawn from live `config.yaml` rather than a frozen per-run threshold
    record (unlike the binder track's `calibration.json`) — cosmetic only,
    the funnel/survivor counts themselves are read from the run's own
    frozen `filter_stats.txt` and can't drift. Low priority; fix by
    persisting the thresholds a PPI run actually scored against if it ever
    becomes confusing in practice.

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
