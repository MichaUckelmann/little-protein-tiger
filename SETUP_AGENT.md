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
4b. **Never type, paste or echo an API key yourself.** Create `.env` from
   `.env.example`, then ask the user to put their own key values in and tell
   you when they have. Verify with `python scripts/doctor.py --track <track>`,
   which reports each key's state without printing it. Do **not** grep for a
   non-empty value: `.env.example` ships `GEMINI_API_KEY=...` and
   `ANTHROPIC_API_KEY=sk-ant-...` as documentation of each key's shape, so
   `grep -c '^GEMINI_API_KEY=.\+' .env` returns 1 on a file nobody has edited.
   A key you handle ends up in your own transcript and in the user's shell
   history.
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
  | `structure` | PDB/interface analysis, epitope choice, trimming, RFD3 spec | Base install only. A `GEMINI_API_KEY` **unless** the operator names the epitope with `--hotspots` |
  | `literature` | Corpus search, discovery workflows | + `corpus` extra (~3 GB downloaded, **~5.8 GB installed**); the corpus itself is a free ~105 MB download. CLI needs `GEMINI_API_KEY`; over MCP it needs **no key** |
  | `ppi` | Discovery → design | + BoltzGen *or* foundry, + `GEMINI_API_KEY` |
  | `binder` | RFD3/MPNN/RF3 campaigns | + foundry, + a CUDA GPU, + ~15 GB disk per campaign (up to ~90 GB for a large target), + `GEMINI_API_KEY` |

  `structure` works everywhere in about 10 minutes and is the only track with a
  genuinely keyless path — see Phase 9 for its command. Say so; many users need
  only that. **Note what it does NOT include:** `report.html` is generated from
  a completed GPU campaign's refold scores, so it belongs to `binder`, not
  here.
- **OS and hardware.** Run `nvidia-smi`, check free disk (`df -h .`), check
  `python3 --version` **and `python3.12 --version`** (LPT needs 3.12-3.14;
  `pyproject.toml` pins `>=3.12,<3.15` — and see Phase 2 on why 3.12 is the
  smoother start). For the literature track also check `command -v zstd`: the
  corpus asset is `.tar.zst`, Python has no stdlib zstd, and
  `fetch_corpus.py` shells out to the binary. It fails legibly
  (`apt install zstd` / `brew install zstd`) but only after the user has
  committed to Phase 6.
- **API keys.** `GEMINI_API_KEY` is the default provider for every pipeline
  stage. `ANTHROPIC_API_KEY` is optional for the pipeline (used by
  `--provider claude` and as the refusal fallback). It is **not** needed to
  curate or to query the corpus: `curation.provider` is `gemini`
  (`gemini-3.1-flash-lite`) and `scripts/ask_corpus.py` defaults to gemini
  too, so a Gemini key alone covers every track. Neither key
  is needed for the `structure` track.
- **Do you already have foundry / PyRosetta / BoltzGen anywhere?** Before
  proposing an install, search: `find ~ -maxdepth 4 \( -name foundry -o -name '*rcfoundry*' \) 2>/dev/null || true`
  (the parentheses matter — `-maxdepth` after a test is a global-option misuse
  that GNU `find` warns about and `bfs` errors on),
  and check conda envs (`conda env list`) for pyrosetta. Many users are in labs
  where a colleague already installed these. If you find a foundry checkout or
  a `pip_rcfoundry_ckpt` weights directory, offer the paths back for
  confirmation rather than asking the user to look them up — Phase 7 needs
  both.

**Platform reality check, state it plainly:**
- Linux + NVIDIA is the only fully supported configuration.
- macOS runs `structure`, `literature`, the reports and the MCP servers, but
  **not** the binder track — foundry/RF3 are CUDA-only.
- Windows: no `setup.sh` (it is bash); follow README's manual steps. The binder
  track is Linux-only by construction — `foundry_runner` generates a bash
  campaign driver launched with `start_new_session=True`.

### Phase 2 — Base install

```bash
python3.12 -m venv .venv || { rm -rf .venv; echo "3.12 cannot build a venv"; }
source .venv/bin/activate                               # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

**Prefer 3.12 if it can build a venv — test that, not whether it exists.** LPT
supports 3.12-3.14, but on 3.13+ behind a TLS-inspecting proxy some downloads
fail (see below), so 3.12 is the smoother start. On Debian/Ubuntu the
interpreter and its `venv` module are separate packages, so `python3.12` can be
present and still fail:

```
The virtual environment was not created successfully because ensurepip is not
available. ... apt install python3.12-venv
```

**That failure leaves a `.venv` with no `pip` in it**, and a later
`source .venv/bin/activate && pip install` then silently uses the SYSTEM pip.
`rm -rf .venv` before retrying, as the command above does. Three exits, in
order of preference:

1. `sudo apt install python3.12-venv`, then retry. Needs root, which a user on
   a managed lab workstation often does not have.
2. **`uv venv --python 3.12 && uv pip install -e ".[dev]"`** — `uv` bundles its
   own bootstrap, so it needs no `python3.12-venv` and no root. `uv.lock` is
   tracked in this repo; this path is supported. Note the venv it creates has
   no `pip` binary: use `uv pip` throughout, or `python -m pip` after
   `uv pip install pip`.
3. `python3 -m venv .venv` on whatever 3.13+ is default — **and then set
   `LPT_SSL_RELAX_STRICT=1` in `.env`**, per the proxy section below. This is
   fine; it just needs that one variable.

~790 MB, ~94 packages, no torch. For the literature track add the `corpus`
extra as well — `pip install -e ".[corpus]"` after the above, or
`pip install -e ".[dev,corpus]"` in one go. It pulls sentence-transformers and,
transitively, torch and the CUDA wheels: **~3 GB downloaded, ~5.8 GB
installed** (`pyproject.toml` states both). Budget the installed figure, not
the download.

**A machine that will never touch a GPU can skip ~2.4 GB of that.** The corpus
extra only needs torch to run the embedding model, which is fine on CPU, but
pip resolves the CUDA build regardless. If the user declined both design
tracks, install the CPU wheel first and the extra will accept it:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[corpus]"
```

**If HTTPS fails with a certificate error**, the user is behind a
TLS-inspecting proxy. There are two distinct failures and they need different
fixes — do not stop at the first:

1. **An untrusted chain** (`unable to get local issuer certificate`) — set
   `LPT_CA_BUNDLE` in `.env` to the system CA bundle
   (`/etc/ssl/certs/ca-certificates.crt` on Debian/Ubuntu). One variable is
   enough for this case: `src/env_config.load_env()` fans it out to the three
   vars the different HTTP clients read.
2. **`Missing Authority Key Identifier`** — Python **3.13+** enforcing stricter
   X.509 rules than 3.12 on a re-signed certificate. **A CA bundle does not fix
   this.** Set `LPT_SSL_RELAX_STRICT=1` in `.env` (it clears that one
   structural check and keeps trust-chain, hostname and expiry validation), or
   rebuild the venv with `python3.12`.

Interception is usually **per-host**, so do not read a passing download as
proof you are clear: on the reference workstation RCSB downloads fine on 3.13
while HGNC (via `storage.googleapis.com`) fails. Test with Phase 3, which
fetches both.

`python scripts/doctor.py` names which of the two you have. Note that both
variables only take effect for code that calls `load_env()` — every LPT entry
point does, a bare `python -c` does not.

### Phase 3 — Reference data (ppi / binder tracks; optional for structure-only)

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
python scripts/doctor.py --track <the track they chose>    # once per track
```

This is your shared source of truth with the user for the rest of the session.
It reports readiness per track and prints the exact command to fix each failing
row. **Show its full output** for the tracks they chose, and re-run it after
each subsequent phase.

Use `--track`. The unfiltered `doctor.py` prints every row for all five tracks
and **exits non-zero if any of them is unready** — so a structure-only user
sees a wall of failures for tracks they declined, and an agent checking the
exit code reads breakage where there is none.

Do not duplicate its logic in your own checks — call it.

### Phase 6 — The corpus (literature track only)

**The curated corpus ships pre-built. Downloading it is free and takes about a
minute.** A new user does not rebuild it: no LLM spend, no days of downloading,
no PubMed rate limits.

> **Check, do not assume.** Run `python scripts/fetch_corpus.py --check`
> first and trust only what it prints — not any prose in this repo, which has
> been stale in both directions. If it reports no `lpt-corpus*` asset, that is
> a **repo state, not a fault on this machine**: say exactly that, and tell the
> user the binder track (`--workflow binder`) needs no corpus and is fully
> usable meanwhile. As of v0.1.0 the asset is published and the download works
> anonymously.

```bash
python scripts/fetch_corpus.py
```

~105 MB compressed. Afterwards `du -sh data/` reports **~480 MB** — about
410 MB of that is corpus artifacts (`vectors/` 215 MB, `fingerprints/`
158 MB, `literature.db` 35 MB) and the rest is Phase 3's reference data. The
figures here are decimal MB; `du` shows MiB, so it reads ~7% smaller. **14,517 curated papers**, of
which 14,514 have fingerprint files on disk — `doctor.py` reports the file
count and the two figures differ by three. Plus the vector index and a database
indexing all 57,907 papers the maintainer's searches found. `search_corpus`
works immediately afterwards **once the embedding model is cached** — that is
Phase 8, and on a cold machine `doctor.py` will show it failing until then.

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
4. `python scripts/curate_papers.py --limit N` — **state the estimate and wait
   for a yes.** Both figures below are measured, not estimated: a 3,078-paper
   run averaged 23.5k tokens per paper at a 92/8 input/output split.
   - `gemini-3.1-flash-lite` (the default): **~$0.008 per paper** — ~$4 for
     500, ~$25 for 3,000.
   - `gemini-3.7-flash`: **~$0.024 per paper**, about 3x that, and its price
     doubles in January 2027.
   Add `--discard-documents` if they will not re-curate.

Curation self-runs identifier normalisation, the graph rebuild and vector
ingest afterwards; do not run those by hand.

### Phase 7 — GPU tools (ppi / binder tracks only)

LPT ships none of these. **Most users here already have foundry** — ask before
assuming anything.

#### foundry (RFD3 / solubleMPNN / RF3) — the binder track's hard dependency

**Ask the user two questions and wait:**

1. *"Do you have foundry installed? If so, what is the path to the checkout?"*
2. *"Where are the model weights?"* — foundry's default is
   `~/pip_rcfoundry_ckpt`; if that directory exists, offer it as the answer
   rather than making them look it up.

**If they have it**, write both into `.env` (never `config.yaml` — see ground
rule 1) and verify:

```bash
LPT_FOUNDRY_ROOT=/their/path/to/foundry
LPT_FOUNDRY_CKPT_DIR=/their/path/to/weights   # omit if ~/pip_rcfoundry_ckpt
```

```bash
python scripts/doctor.py --track binder
```

The `foundry` rows must be `[ok]`. If the checkout row passes but the binaries
row does not, the venv inside their checkout is named something LPT could not
resolve unambiguously — ask which one is built for their GPU and set
`design.foundry.rfd3_bin` / `mpnn_bin` / `rf3_bin` (paths relative to the
checkout). Do **not** guess: the wrong venv may be a build for a different card.

**If they do not have it**, do not attempt the install. Point them at
<https://github.com/RosettaCommons/foundry> to follow its own instructions
(including its weights), and say clearly that setup is **not blocked** on it:
`--stop-after spec` runs both design tracks' full reasoning path — target
resolution, epitope choice, trimming, spec generation — with an API key alone,
no GPU. That is the right first run either way; `docs/beta-testing.md` has the
command.

> **STOP AND ASK before attempting a foundry install yourself.** It needs
> licence acceptance only the user can give (RosettaCommons terms, not LPT's),
> and a torch build matched to their GPU generation. It is not a scripted step.

#### The other two

- **BoltzGen** (`LPT_BOLTZGEN_EXECUTABLE`) — only for `--workflow ppi` with
  `design.backend: boltzgen`, or `--modality cyclic_peptide`. Ask for the path
  the same way; `doctor.py` reports which engine the PPI track will use.
- **PyRosetta** (`LPT_PYROSETTA_PYTHON`) — **optional, scoring only.** With
  `design.pyrosetta.enabled: auto` (the default) a machine without it runs both
  tracks and skips those metrics. Free for academic use; commercial needs a
  licence. `docs/pyrosetta_setup.md` covers the Python-ABI trap that makes a
  separate conda env necessary.

### Phase 8 — MCP servers (optional)

Only if the user wants LPT's tools inside Claude Desktop or Claude Code:

```bash
python scripts/setup_mcp_json.py
```

Then verify both servers actually start — this phase is the one place ground
rule 6 was routinely skipped, and `setup_mcp_json.py` registers **both**
servers regardless of what is installed:

```bash
./.venv/bin/python scripts/launch_structure_tools.py < /dev/null   # base install
./.venv/bin/python scripts/launch_mcp.py < /dev/null               # needs [corpus]
```

`launch_mcp.py` exits with `ModuleNotFoundError: ... needs the optional
`corpus` extra` on a base install. That is expected and harmless — Claude will
show that server as failed — but **tell the user** rather than leaving them to
discover a permanently-red server. `setup_mcp_json.py --dry-run` prints the
config without writing it.

Claude **Code** picks `.mcp.json` up from the project root on its next session.
Claude **Desktop** needs the printed `mcpServers` block copied into
`claude_desktop_config.json` (`%APPDATA%\Claude\` on Windows,
`~/Library/Application Support/Claude/` on macOS, `~/.config/Claude/` on Linux)
— and then **STOP AND TELL THE USER TO RESTART CLAUDE DESKTOP.** It reads that
file only at startup; without a restart the servers silently do not appear, and
you cannot restart it for them.

If they chose the literature track, warm the embedding cache too — the MCP
launcher sets `HF_HUB_OFFLINE=1`, so an uncached model fails with a
HuggingFace error that names nothing about LPT:

```bash
python scripts/warm_embedding_cache.py          # ~439 MB, one time
python scripts/warm_embedding_cache.py --check  # is it already there?
```

**Use the script, not a bare `python -c`.** A one-liner does not call
`src.env_config.load_env()`, so neither `LPT_CA_BUNDLE` nor
`LPT_SSL_RELAX_STRICT` applies and the download fails verification behind a
TLS-inspecting proxy — after five silent retries, with the *same* unhelpful
`OSError` the missing-cache case produces. Measured: 73 s to fail that way
versus 12 s to succeed.

### Phase 9 — Finish

1. Re-run `python scripts/doctor.py --track <their tracks>` and show the final
   state. Use `--track` here too — bare `doctor.py` exits non-zero whenever any
   of the five tracks is unready, so ending a successful setup on it reports
   failure.
2. Suggest one real next command for the tracks that are READY:
   - structure, **no key needed** — the operator names the epitope, so the
     `interface` stage makes no model call at all:
     ```bash
     python scripts/run_pipeline.py --workflow structure --pdb 3KYS \
       --chains A,B --hotspots 276,314,318,322 --project my_target \
       --stop-after spec
     ```
     Swap in their own entry and residues, or `--structure /path/to/file.cif`
     for a local file. Drop `--hotspots` and a model chooses the epitope from
     the coordinates — that needs `GEMINI_API_KEY`. `--project` is required
     either way. `quickstart.py --pdb <theirs> --chains A B` is a *demo*, not
     this track: it analyses an interface and stops, with no trim and no spec.

     **Tell them where the output is** — this track's whole product is a file,
     and the path is not guessable (`binder/` appears twice):
     `projects/<name>/runs/round-1/binder/sites/primary/binder/` holds
     `spec/*.json` (the RFD3 input), `trim/trimmed.cif` (the prepared target)
     and `2*.md` (the reports). **Point them at `20_target_intel.md` first.**

     **Two warnings on this track are expected and harmless**: `could not
     confirm target_chain=...` and `could not resolve '<id> chain A' to a human
     UniProt accession`. A structure-first run has no gene name to check
     against, so three identity guards fail open — `20_target_intel.md` lists
     exactly which, and says transmembrane residues will NOT be stripped.
     Add `--uniprot <ACC>` to switch them back on if the accession is known;
     the partner-chain one can never clear here and is noise.

     **How to tell success from failure**: a successful run leaves a
     `spec/*.json`. `GO/NO-GO: INCOMPLETE` is normal for `--stop-after spec`,
     but "PIPELINE COMPLETE" is printed even when every site failed — check for
     the spec file, and check `29_site_comparison.md` for a `FAILED:` line.
   - binder: `python scripts/run_pipeline.py --workflow binder --target <GENE> --project test --stop-after spec`
     (`--stop-after spec` validates everything up to the GPU without spending
     GPU time — the right first run.)
   - literature, CLI: `python scripts/ask_corpus.py "<a question in their field>"`
     (needs `GEMINI_API_KEY`).
   - literature, **no key needed**: the MCP route. `search_corpus` runs locally
     against the downloaded index, so a user on a Claude subscription needs no
     API key at all — `doctor.py` still reports the track NOT READY on the
     missing key, which is about the CLI path only. Say which one they have.
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
