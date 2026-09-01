# Setting up LPT with a coding agent

Paste this file into Claude Code (or any capable coding agent) from a fresh
clone and it will set up Little Protein Tiger on your machine.

> **To the human:** you only need to read the next paragraph. Everything after
> it is addressed to the agent.
>
> The agent will ask you a handful of questions first — which parts of LPT you
> want, what hardware you have, which API keys you hold — because the four
> tracks have almost disjoint requirements and the difference between them is a
> 650 MB install and a multi-day GPU stack. It will not spend money without
> asking. If you would rather do it by hand, follow `README.md` and
> `docs/environment_setup.md` instead; this file automates those, it does not
> replace them.

---

## Agent instructions

You are setting up LPT on the user's machine. Work through the phases in order.
**Do not skip the interview.** Requirements differ enormously between tracks,
and installing everything by default wastes gigabytes and hours.

### Ground rules

1. **Write `.env`, never `config.yaml`.** `config.yaml` is tracked in git;
   machine paths in it show up in the user's next `git diff` and eventually in
   a pull request. Every machine-specific value has an `LPT_*` env var — see
   `.env.example` and `docs/environment_setup.md`.
2. **Never spend the user's money without explicit confirmation.** Setup itself
   is free — the corpus ships pre-built. Only *extending* it with new search
   terms costs API spend. If that comes up, state the estimate and wait for a
   yes.
3. **Do not modify** `config.yaml`, `CLAUDE.md`, `.mcp.json` (except by running
   `scripts/setup_mcp_json.py`), or anything under `src/`. If setup seems to
   need a source change, stop and tell the user why.
4. **Do not commit anything.**
5. **Never attempt to obtain licensed software from unofficial sources**, and
   never work around a licence check. foundry and PyRosetta both carry terms;
   if the user does not have them, say which tracks are unavailable and move on.
6. **Verify by running, not by reading.** After each phase, run the relevant
   check and show the user its real output.

### Phase 1 — Interview

Ask, and wait for answers:

- **Which tracks do you want?** Describe them honestly:
  | Track | What it does | Needs |
  |---|---|---|
  | `structure` | PDB/interface analysis, trimming, reports | Base install only |
  | `literature` | Corpus search, discovery workflows | + `corpus` extra (~3 GB); the corpus itself is a free ~83 MB download |
  | `ppi` | Discovery → design | + BoltzGen *or* foundry |
  | `binder` | RFD3/MPNN/RF3 campaigns | + foundry, + a CUDA GPU, + ~120 GB disk |

  `structure` works everywhere in about 10 minutes. Say so — many users need
  only that.
- **OS and hardware.** Run `nvidia-smi`, check free disk (`df -h .`), check
  `python3 --version` (LPT needs 3.12-3.14; `pyproject.toml` pins
  `>=3.12,<3.15`).
- **API keys.** `GEMINI_API_KEY` is the default provider for every pipeline
  stage. `ANTHROPIC_API_KEY` is optional for the pipeline (used by
  `--provider claude` and as the refusal fallback) but **required by
  `scripts/ask_corpus.py`**, which talks to Claude directly and has no Gemini
  path — so a literature-track user who wants that REPL needs it. Neither key
  is needed for the `structure` track.
- **Do you already have foundry / PyRosetta / BoltzGen anywhere?** Before
  proposing an install, search: `find ~ -maxdepth 4 -name "foundry" -o -maxdepth 4 -name "*rcfoundry*" 2>/dev/null`,
  and check conda envs (`conda env list`) for pyrosetta. Many users are in labs
  where a colleague already installed these.

**Platform reality check, state it plainly:**
- Linux + NVIDIA is the only fully supported configuration.
- macOS runs `structure`, `literature`, the reports and the MCP servers, but
  **not** the binder track — foundry/RF3 are CUDA-only.
- Windows: no `setup.sh` (it is bash); follow README's manual steps. The binder
  track is Linux-only by construction — `foundry_runner` generates a bash
  campaign driver launched with `start_new_session=True`.

### Phase 2 — Base install

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

~650 MB, ~90 packages, no torch. Add `.[corpus]` **only** if the user chose the
literature track — it pulls sentence-transformers and, transitively, torch and
the CUDA wheels: ~3 GB more.

If HTTPS fails with a certificate error, the user is likely behind a
TLS-inspecting proxy. Set `LPT_CA_BUNDLE` in `.env` to the system CA bundle
(`/etc/ssl/certs/ca-certificates.crt` on Debian/Ubuntu). One variable is
enough — `src/env_config.load_env()` fans it out to the three vars the
different HTTP clients read.

### Phase 3 — Reference data

```bash
python scripts/fetch_reference_data.py
```

~52 MB, public, no credentials. **Required** for the ppi and binder tracks —
without it they die two seconds in. Add `--with-depmap` only if the user wants
the wildcard-expert DepMap tools (that file is ~420 MB and must be fetched by
hand from the DepMap portal; the script prints the URL).

### Phase 4 — Prove it works

```bash
python scripts/quickstart.py
```

~7 seconds, no keys, no GPU, no corpus. It downloads a real structure,
analyses a real interface, and resolves a gene to ranked candidate epitopes.
**Show the user this output.** It is the moment they see the tool work, and it
establishes a baseline: if something later fails, the install itself was fine.

### Phase 5 — Environment check

```bash
python scripts/doctor.py
```

This is your shared source of truth with the user for the rest of the session.
It reports readiness per track and prints the exact command to fix each failing
row. **Show its full output**, then work only on the rows relevant to the
tracks the user chose. Re-run it after each subsequent phase.

Do not duplicate its logic in your own checks — call it.

### Phase 6 — The corpus (literature track only)

**The curated corpus ships pre-built. Downloading it is free and takes about a
minute.** A new user does not rebuild it: no LLM spend, no days of downloading,
no PubMed rate limits.

```bash
python scripts/fetch_corpus.py
```

~83 MB compressed, ~337 MB installed: **10,981 curated papers**, the vector
index, and a database indexing all ~55,000 papers the maintainer's searches
found. `search_corpus` works immediately afterwards.

Source documents (PDFs/XMLs) are deliberately excluded — they are ~95% of the
corpus on disk and nothing downstream reads them.

If `fetch_corpus.py` reports no release asset, none has been published yet;
say so rather than silently falling through to a paid rebuild.

**Cost only enters if the user wants to EXTEND the corpus** with their own
search terms — a different field, or newer papers than the shipped snapshot.
Do not raise this unprompted; mention it only if they ask, or if their field is
clearly outside the shipped corpus's scope (it is chromatin / histone chaperone
/ structural biology focused — check with `scripts/ask_corpus.py` before
assuming it does not cover them).

If they do want to extend it, then and only then:

1. **Read `docs/journal-filtering.md` with them.** LPT downloads only tier 1/2
   journals by default — 32% of search hits — and the tier lists are
   molecular/structural/chemical-biology focused. A user in another field needs
   `quality.tier1_extra` / `tier2_extra` or the gate off, or they will quietly
   get a corpus missing most of their literature.
2. **Edit the keywords** in `config.yaml`. This is the one expected
   `config.yaml` edit; confirm it with them first.
3. `python scripts/fetch_papers.py --prefer-xml --dry-run` — show them what
   would be fetched before fetching it. XML is ~47x smaller than publisher PDFs
   for the same paper.
4. `python scripts/curate_papers.py --limit N --provider gemini` — **state the
   estimate and wait for a yes.** Roughly **$0.03 per paper** on the default
   `gemini-3.7-flash` (~28k tokens each, measured), so ~$15 for 500. Setting
   `curation.gemini_model: gemini-3.1-flash-lite` is ~8x cheaper. Add
   `--discard-documents` if they will not re-curate.

Curation self-runs identifier normalisation, the graph rebuild and vector
ingest afterwards; do not run those by hand.

### Phase 7 — GPU tools (ppi / binder tracks only)

Each is a separate install with its own licence. LPT ships none of them.

- **foundry** (RFD3 / solubleMPNN / RF3) — https://github.com/RosettaCommons/foundry.
  **The hard dependency of the binder track.** RosettaCommons terms, not MIT —
  tell the user to read them before any commercial use. Expect a
  GPU-generation-specific torch build (this repo's reference machine needed a
  hand-built `.venv-blackwell` for `sm_120`). Checkpoints are a separate
  download through foundry's own registry. Then set `LPT_FOUNDRY_ROOT`.
- **BoltzGen** — needed for `--workflow ppi` only when `design.backend` is
  `boltzgen`. Check the configured value; `doctor.py` reports which engine the
  PPI track will actually use.
- **PyRosetta** — **optional and scoring-only**. Used for hotspot SASA and the
  Rosetta composite terms, both *after* designs exist. With
  `design.pyrosetta.enabled: auto` (the default) a machine without it runs both
  tracks and skips those metrics. **Free for academic use only**; commercial
  use needs a licence. See `docs/pyrosetta_setup.md` for the Python-ABI trap
  that makes a separate conda env necessary.

### Phase 8 — MCP servers (optional)

Only if the user wants LPT's tools inside Claude Desktop or Claude Code:

```bash
python scripts/setup_mcp_json.py
```

If they chose the literature track, warm the embedding cache too — the MCP
launcher sets `HF_HUB_OFFLINE=1`, so an uncached model fails with a
HuggingFace error that names nothing about LPT:

```bash
python -c "from sentence_transformers import SentenceTransformer as S; S('NeuML/pubmedbert-base-embeddings')"
```

### Phase 9 — Finish

1. Re-run `python scripts/doctor.py` and show the final state.
2. Suggest one real next command for the tracks that are READY:
   - structure: `python scripts/quickstart.py --pdb <their PDB> --chains A B`
   - binder: `python scripts/run_pipeline.py --workflow binder --target <GENE> --project test --stop-after spec`
     (`--stop-after spec` validates everything up to the GPU without spending
     GPU time — the right first run.)
   - literature: `python scripts/ask_corpus.py "<a question in their field>"`
3. Point them at `docs/responsible-use.md`. Every design this pipeline emits is
   an unvalidated computational hypothesis, and anyone synthesising a sequence
   is responsible for screening it.
4. Summarise: what is installed, what is not, what it would take to add the
   rest, and what you spent.

### If something fails

- Show the actual error, not a paraphrase.
- Check `doctor.py` first — it probably already names the fix.
- `docs/environment_setup.md` covers the `LPT_*` variables and every external
  tool; `docs/pyrosetta_setup.md` covers PyRosetta specifically;
  `docs/journal-filtering.md` covers corpus scope.
- Do not work around a failure by editing source. Report it — a fresh-clone
  failure is a bug worth reporting upstream.
