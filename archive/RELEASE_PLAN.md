> **ARCHIVED — OUTDATED (2026-08-23).** This plan assumed a community-hosted
> web platform (Hetzner VPS, GitHub OAuth, public corpus contribution flow,
> etc.). Current direction: no hosted frontend for the foreseeable future —
> the project is CLI- and coding-agent-driven. Kept for historical context
> only; do not treat anything below as current planning.

# Open-Science Release Plan — Corpus + Expert Skills

Reference document for turning the literature-corpus pipeline and corpus-explorer
expert skill into a community-driven open-science project. Captures decisions and
open questions from the 2026-05-03 design discussion. Implementation has not
started.

Status as of 2026-05-03: **planning only**. Nothing has shipped.

---

## Strategic shape

**Two products, decoupled.** The corpus-explorer + curator + literature DB has a
~100× larger addressable audience than the protein-design pipeline (any wet-lab
biologist vs. structural-biology specialists), different dependency weight, and
different risk profile. Ship the corpus side first; gate the design pipeline
behind opt-in extras.

- **v0.1**: corpus-explorer skill, curator, identifier normalization, edge
  index + clustering, MCP server, web UI for browse + skill chat. Design
  pipeline present in source but hidden in UI.
- **v0.2**: design pipeline opt-in. Same web UI, separate route.
- **v0.3+**: community contribution flow at scale, schema versioning policy.

Rationale: less surface area to defend on day one; corpus side is what brings
people in.

## Deployment topology (v0.1)

| Layer | Where | Cost |
|---|---|---|
| Frontend (Vite build) | Cloudflare Pages | free |
| API + Celery + Redis + Postgres + LanceDB | Hetzner CX32 (4 vCPU / 8 GB / EU) | €8/mo |
| Data dump releases | Zenodo (DOI) + Cloudflare R2 mirror (zero egress) | free |
| Domain + TLS | Cloudflare DNS + Caddy autocert | ~€10/yr |
| Backups | Hetzner Storage Box 1 TB | €4/mo |
| **Total** | | **~€13/mo** |

Single-tenant VPS gives the cleanest BYOK privacy story (key only on a box you
SSH to), EU jurisdiction throughout (GDPR + open-science narrative), and
predictable cost. Tradeoff: ops burden is yours (~1–2 h/mo with proper
containerisation).

Alternatives considered: Fly.io (better auto-suspend + global latency, more
vendor coupling), Railway/Render (zero ops, ~3× cost), serverless (rejected:
LanceDB + 20-min curation jobs argue against scale-to-zero), academic clouds
(deNBI / ELIXIR — investigate if institutional affiliation appears).

## Capacity expectations for the v0.1 box

| Workload | Realistic | Hard cap | Bottleneck |
|---|---|---|---|
| Search QPS | 20–30 sustained, 80–100 burst | ~150 | CPU + uvicorn workers |
| Concurrent skill sessions | 20–50 active | ~100 | event loop / FDs |
| Concurrent curation jobs | 1–2 | 3 | RAM (sentence-transformers + paper text) |
| Curation throughput | 50–150 papers/day | ~500/day | Celery worker count |
| Daily active users | 200–500 | ~1000 | worker spread |
| Storage runway | ~10 GB → 80 GB | ~50K papers | disk |

The box dies on **concurrency spikes, not volume.** A 60-user simultaneous
search burst degrades latency but recovers; 10 simultaneous curation requests
form a queue (3rd waits ~3 h).

Upgrade path before replatform:
- +€16/mo CCX23 dedicated for Celery (biggest single unlock — 4–6 concurrent curations)
- Cloudflare free tier in front (absorbs ~10× burst search traffic)
- Bump to CX42 (€16/mo, doubles uvicorn workers + RAM headroom)

v0.2 ceiling at ~€40/mo: ~2K DAU, ~500 papers/day curation, ~100 concurrent
skill sessions.

## Authentication model

**Hybrid: anonymous-by-default for browsing, GitHub OAuth required to
contribute or persist anything.**

### Anonymous tier

- BYOK entered per session, encrypted with session-bound key (Fernet via
  existing `web/backend/crypto.py`), held in Redis with TTL = session lifetime,
  scrubbed on close.
- Search results: download button on every result (JSON + markdown). No
  server-side retention.
- IP-based rate limit (Redis token bucket) — non-negotiable, otherwise
  scrapers ruin the day.
- Cost tracking: per-session counter, in-session warnings at €1 / €5 / €10.
  Cumulative-across-sessions impossible without identity.

### Authenticated tier (GitHub OAuth, `read:user` scope only)

- Why GitHub: zero password liability, contributor base already has accounts,
  maps cleanly to PR fallback. ORCID would suit academics — add later if
  asked.
- BYOK encrypted at rest with platform key + user-bound salt; decrypt-on-use;
  never logged.
- Persistent search history with delete and "share link" (immutable snapshot,
  citable).
- Cumulative token tracking + user-set hard ceiling.
- Attribution on submitted fingerprints (`contributor: github:<handle>`).

Tradeoff: two paths to maintain, but the anonymous path is a strict subset of
the authenticated one — write the storage layer once, branch on `user_id is None`.

## Corpus expansion via the web

Curation is heavy: ~20 min wall-clock per 100-paper batch with gemini-flash,
worker holds the user's API key in memory throughout.

### Primary flow (in-browser via Celery)

1. User enters keywords → web validates query against PMC OA list.
2. Celery job (existing infra) runs `fetch_papers` + `curate_papers` with the
   user's key decrypted into worker memory for the job's lifetime.
3. Results staged in a per-user **review queue**.
4. User approves individual fingerprints — explicit click per item or per batch.
5. Approved fingerprints enter a **"candidate for shared corpus" pool** that
   the monthly release process merges.

Non-obvious requirements:
- **Never auto-merge to shared corpus.** User explicitly opts each fingerprint
  in. Lets users keep niche/private fingerprints private; creates clean opt-in
  for the open-data commitment.
- **Job key handling**: short-lived Redis ref, not Celery task args. Scrub on
  job completion or failure. Document this in the privacy notice.
- **Quota**: free expansion = 0 (must sign in). Authenticated cap at e.g.
  500 papers/month per user. Prevents queue domination.

### Fallback flow (out-of-browser)

Web emits a `config.yaml` and a one-line `uvx lpt-corpus curate --config <url>`
command. User runs locally on their own machine; uploads result JSON. Useful
for users with policy concerns about cloud-side keys. Worth shipping alongside
the in-browser flow.

## Community contribution model

Trust model: anonymous read; GitHub-OAuth-attributed contribution; periodic
moderator review.

1. Anyone runs `fetch_papers.py + curate_papers.py` (web or CLI) on their own
   keywords with their own BYOK budget.
2. Submission gate: web/PR validates against `extraction_schema.json` + dedups
   against existing corpus + restricts to PMC-OA papers (provenance verifiable).
3. Spot-check sampling: bot re-curates a random 5 % of submissions with the
   reference prompt. Detects adversarial / low-quality submissions.
4. Monthly release: accepted contributions merged → fresh Zenodo snapshot with
   bumped DOI suffix.

Cost of spot-check: 5 % × €0.01 per paper = trivial in practice.

Governance: one-page doc on who merges, who decides schema changes. v0.1 = me.
Plan for delegation in the schema (`reviewer_id`, `reviewed_at`).

Schema migration policy: when v3.0 lands, define whether community
contributions are auto-migrated (preferred) or frozen at submission version.

## Cost & token tracking

BYOK absolves you of LLM bills but transfers the duty of warning users not to
run up their own. Anthropic and Gemini APIs return token counts in every
response — log them per session/user; convert to estimated euros at posted prices.

- In-session warning thresholds: €1, €5, €10.
- Authenticated users: hard ceiling configurable per account. Default off.
- Hash user IDs before logging tokens (GDPR pseudonymisation).
- Never log query content. Aggregate token counts are sufficient.

The `skill_runner.py` `--max-tokens` ceiling already exists — make sure the
web path enforces it (anonymous and authenticated alike).

## Copyright / OA compliance

Must-do before public release.

1. Audit `data/literature.db` against the PMC OA file list
   (`ftp.ncbi.nlm.nih.gov/pub/pmc/oa_file_list.csv`). Tag every paper's OA
   status. Manually-added non-OA papers exist and need to be flagged.
2. Public release ships **only OA-derived fingerprints**. Private deployments
   can keep non-OA fingerprints; the public Zenodo snapshot cannot.
3. License-clean check baked into `curate_papers.py` going forward —
   non-OA papers cannot enter the public corpus by accident.
4. Fingerprints of non-OA papers are likely fair use (factual extraction) but
   exclude them anyway. Don't pick that fight.

## Packaging & distribution

- **PyPI**: `lpt-corpus` (light), `lpt-corpus[design]` (heavy GPU/MCP deps).
  Console scripts: `lpt fetch-corpus`, `lpt curate`, `lpt mcp`.
- **Data files NOT in the wheel.** Wheel ships logic only; first-run bootstrap
  pulls the current Zenodo snapshot.
- **MCP distribution**: `uvx lpt-corpus-mcp` for any MCP-compatible client +
  `.mcpb` bundle for Claude Desktop one-click install. Snippet for
  `claude_desktop_config.json` in README for users on other clients.
- **Zenodo deposit per release**: gives a citable DOI per snapshot — single
  biggest driver of academic adoption.
- **CITATION.cff**: required so users can cite the corpus and the tool
  separately.
- **Reproducibility**: pin specific gemini-flash snapshot, not the floating
  alias. Re-extractions must match.

## Pre-release checklist (v0.1)

Things to do before public announcement, in rough dependency order:

- [ ] **OA audit**: cross-reference `data/literature.db` against PMC OA list;
      tag every paper. Add OA status to `extraction_schema.json` if missing.
- [ ] **Curator OA gate**: refuse to curate non-OA papers in public mode;
      configurable for private deployments.
- [ ] **Curation provenance**: every fingerprint records `curator_model +
      prompt_version + curated_date`. Audit existing fingerprints; backfill if
      missing.
- [ ] **Schema versioning** decision: how does community contribution handle a
      v3.0 bump? Document.
- [ ] **PyPI packaging skeleton**: split `lpt-corpus` from design extras;
      bootstrap script for data dump.
- [ ] **Web auth**: GitHub OAuth, anonymous session encryption, key TTL,
      rate limiting.
- [ ] **Web review queue**: schema (`users`, `api_keys`, `queries`,
      `contribution_queue`), moderator view, approve/reject UI.
- [ ] **Token tracking**: per-session counter, threshold warnings, optional
      hard ceiling for authenticated users.
- [ ] **Privacy notice**: anonymous queries not logged beyond aggregates;
      BYOK never written unencrypted; account deletion clears history but
      preserves anonymized contributions.
- [ ] **Account deletion**: GDPR right-to-erase. Removes query history;
      strips attribution from submitted fingerprints (which remain).
- [ ] **CITATION.cff** + first **Zenodo deposit** with DOI.
- [ ] **README rewrite** for an external audience: what it is, install in
      30 s, run a query in 60 s, contribute in 5 min.
- [ ] **Friendly-tester pass**: 2–3 outside users stress-test contribution flow
      before public announcement.
- [ ] **Discoverability launch**: PyPI alone won't drive adoption — post to
      biostars, r/bioinformatics, ISMB, awesome-mcp list, AI-for-biology
      Slack/Discord channels at launch.

## Open questions

- **Hosted MCP** — should the public Hetzner box also expose the MCP server
  for users who'd rather not install? Cleaner privacy if not (no shared
  process); friction win if yes. Defer until first user asks.
- **ORCID** alongside GitHub OAuth — adds friction for non-academic users but
  improves credibility for academic ones. Wait for explicit demand.
- **Per-cluster top-DOIs** in shared corpus — would need a fingerprint walk
  per cluster. Cheap to add when consumers ask; not in v0.1.
- **Cluster stability across corpus growth** — when monthly releases add
  papers, clusters shift. Worth tracking diff over time; not blocking.
- **Federation** — should other groups be able to host their own private
  corpus that contributes opt-in to the public one? Interesting but outside
  v0.1.

## Cross-references

- Existing web platform: `web/backend/` (FastAPI + Celery), `web/frontend/`
  (Vite + React 19 + TypeScript), `web/backend/crypto.py` (Fernet for BYOK).
- Skill execution model: `src/skill_runner.py` (CLI / web transport) +
  `src/mcp_server.py` (MCP transport). Tool surfaces must stay in sync —
  see `CLAUDE.md`.
- Cost-per-paper baseline: ~€0.01 with gemini-flash (used for sizing the
  community spot-check budget).
- Curation contract: `extraction_schema.json` v2.0, `curation_prompt.md`.
  Schema migration policy needs a decision before community contribution opens.
