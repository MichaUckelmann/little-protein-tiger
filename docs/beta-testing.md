# Beta testing Little Protein Tiger

Everything from a clean machine to your first results, in order. Roughly
**20 minutes of setup**, then a first design run that costs about **$1 of API
credit** and needs no GPU.

> **⚠️ That $1 is a measurement, not a guarantee.** Every reasoning stage is a
> paid API call on your own key, and what it costs depends on which provider
> and model you point at it, what the provider charges this month, and how much
> work you ask for — extending the corpus with your own search terms is a
> different order of spend from designing one binder. **Always pass
> `--budget <USD>`**; it is a hard cap and the run pauses rather than
> overrunning. Set a spend limit in your provider's console as well, since that
> is the only limit that covers everything you do outside LPT. The GPU stages
> cost nothing in API terms.

If you are pointing a coding agent (Claude Code, Cursor, Codex) at this repo
to do the setup for you, give it **[`SETUP_AGENT.md`](../SETUP_AGENT.md)**
instead — same steps, written to be executed rather than read.

---

## What you can test, and what you need for each

There are three independent things to try. **You do not need all of them**, and
the requirements differ sharply — check this table before installing anything.

| | What it does | Needs a GPU? | Needs the corpus? | API key |
|---|---|---|---|---|
| **A. Design from a named target** | "design binders against RING1B" → epitope, spec, campaign | Only past the spec stage | **No** | Gemini |
| **B. Design from a broad prompt** | "inhibitors for pain receptors" → picks the target for you | Only past the spec stage | **Yes** | Gemini |
| **C. Ask the literature** | conversational queries over ~7,000 curated papers | No | **Yes** | Gemini |

Two things to know before you plan your testing:

- **Every track runs its reasoning stages without a GPU.** `--stop-after spec`
  stops right before the first GPU stage. That is a genuinely useful test — it
  exercises target resolution, structure selection, epitope choice, trimming
  and spec generation, which is where most of the interesting behaviour is.
  **Start here even if you do have a GPU** — going further needs foundry, a
  separate install ([how to point LPT at
  it](#going-past-the-spec-stage-point-lpt-at-foundry)).
- **The literature corpus is a separate download** and gates B and C. See
  [step 4](#4-the-literature-corpus-tracks-b-and-c).

---

## 1. Install

Python **3.12 or 3.13** (3.14 works, 3.15 is refused). No compiler, no conda,
no system packages — every base dependency ships prebuilt wheels.

```bash
git clone <repo-url> little-protein-tiger
cd little-protein-tiger

python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Not a bare `pip` — on a venv built without pip that resolves to the system
# or conda pip and installs LPT outside the project.
.venv/bin/python -m pip install -e ".[dev]"     # ~650 MB, no torch
```

Add the corpus extra **only if you want track B or C** — it pulls torch and
costs about 3 GB:

```bash
.venv/bin/python -m pip install -e ".[corpus,dev]"
```

`./scripts/setup.sh` does all of the above plus steps 2–3 in one shot, if you
prefer.

> **Behind a corporate TLS proxy?** Downloads may fail with `Missing Authority
> Key Identifier` on Python 3.13+. Set `LPT_CA_BUNDLE` to your system CA bundle,
> or `LPT_SSL_RELAX_STRICT=1` in `.env` — that clears one X.509 flag and keeps
> trust-chain, hostname and expiry checking. Both are documented in
> `.env.example`.

## 2. Keys

```bash
cp .env.example .env
```

Now **edit `.env`** — it ships with placeholder values, and until this version
they counted as "set".

| Variable | Needed for | Get it from |
|---|---|---|
| `GEMINI_API_KEY` | every design stage (the default provider) | [aistudio.google.com](https://aistudio.google.com/apikey) |
| `ANTHROPIC_API_KEY` | optional everywhere — `--provider claude`, or `models.claude.stages` to route one stage there | [console.anthropic.com](https://console.anthropic.com/) |
| `NCBI_EMAIL` | only if you extend the corpus yourself | your own address |

Gemini is the default because it is ~4× cheaper on input *and* declines fewer
protein-design prompts. Safety-classifier refusals on interface-analysis stages
are a routine operational fact here, not a bug — but they are **terminal**: if
the model declines a stage, the run records it and stops. LPT does not retry on
another model, deliberately.

If it happens to you during the beta, **the most useful thing you can do is
tell us** — which target, which stage, which category — rather than route
around it. That is the signal that gets a miscalibration fixed for everyone.
You can steer the run onto another provider yourself, and an
`ANTHROPIC_API_KEY` is what makes that possible, but read
[docs/responsible-use.md](responsible-use.md) ("If you override a refusal")
first — it is an override you are accountable for, not a fallback.

## 3. Reference data

52 MB, public, no key, **required before any design run**:

```bash
python scripts/fetch_reference_data.py
```

This is the UniProt ID mapping and the HGNC symbol table. Without them a design
run stops two seconds in.

## 4. The literature corpus (tracks B and C)

```bash
python scripts/fetch_corpus.py
```

~50 MB compressed: **7,072** curated paper fingerprints, the prebuilt vector
index, and the paper database. (`fetch_corpus.py --check` prints the current
asset size; the installed footprint has not been re-measured since the
corpus was licence-filtered, so expect well under the ~451 MB the full
corpus used.) You do **not** rebuild or
re-ingest anything after this.

**~7,000 is the expected number, not a truncated download.** A fingerprint is
a derivative work, so one ships only where the paper's licence permits
derivatives: 7,072 of the maintainer's 14,517 curated papers qualify. The
database still indexes all 57,907 papers found, and the withheld rows are
marked `licence_withheld` rather than dropped — see
[docs/licensing.md](licensing.md).

> **⚠️ Beta note:** if this reports `no lpt-corpus* asset found`, the release
> asset has not been published yet. That is a known repo state, not a problem
> on your machine — ask the maintainer for the archive, and use **track A**
> (which needs no corpus at all) in the meantime.

Then warm the embedding model cache once. The MCP launcher runs offline, so an
uncached model fails with a HuggingFace error that never mentions LPT:

```bash
python -c "from sentence_transformers import SentenceTransformer as S; S('NeuML/pubmedbert-base-embeddings')"
```

## 5. Check it

```bash
python scripts/doctor.py            # or: --track binder / --track literature
```

This is the source of truth for what is and is not ready. Every failing row
names the command that fixes it. Run it again after any step below.

```bash
python scripts/quickstart.py        # ~7s, no keys, no GPU — proves the install is sound
```

---

# Your first runs

## Track A — you already know the target

```bash
python scripts/run_pipeline.py --workflow binder \
    --target RING1B \
    --project ring1b_test \
    --budget 2.00 \
    --stop-after spec
```

| Flag | Why |
|---|---|
| `--workflow binder` | target-name-first; skips literature discovery entirely |
| `--target RING1B` | a gene symbol, UniProt accession, or protein name |
| `--project` | **required** — the campaign is resumable, and the manifest is what makes that work |
| `--budget 2.00` | hard cap on API spend; the run pauses rather than overrunning |
| `--stop-after spec` | stops before the first GPU stage — drop it once foundry is installed, not merely once you have a GPU |

Expect ~2 minutes and well under $1. You get:

```
projects/ring1b_test/runs/round-1/binder/
  20_target_intel.md    which structure and why, with the alternatives it rejected
  21_interface.md       the epitope, hotspot by hotspot, with the energetics
  22_trim.md            what was cut from the target and what that cost
  23_binder_spec.md     the RFD3 spec — the actual design input
```

**What to look at:** `20_target_intel.md` should name a real PDB entry with the
partner you would expect, and `21_interface.md`'s hotspots should be residues
that genuinely sit at that interface. This is the part most worth your
scepticism — tell us when it picks a defensible structure for a bad reason, or
a bad structure for a defensible one.

## Track B — you have a problem, not a target

```bash
python scripts/run_pipeline.py --workflow ppi \
    --query "design novel inhibitors for pain receptors that are promising drug targets" \
    --project pain_test \
    --budget 5.00 \
    --stop-after spec
```

`--project` is required here too: the PPI track hands its discovered target to
the same GPU campaign machinery.

This adds three stages in front of track A — pathway reasoning, a literature
and tractability check against the corpus, then structure and hotspot
selection — writing `00_pathway.md`, `01_literature.md`, `02_structure.md`
before the binder stages above.

Want a target the field has *not* already converged on? Add
`--pathway-mode wildcard`, which triages on graph novelty and DepMap
co-essentiality instead of weight of evidence.

### Writing a good prompt for track B

Every prompt below has actually been run. What works:

| Prompt | Picked |
|---|---|
| `design novel inhibitors for pain receptors that are promising drug targets` | CALCRL / RAMP1 on 3N7S |
| `Design cancer therapeutics to target key nodes in mesothelioma.` | YAP1 / TEAD1 on 3KYS |
| `design binders for treatment of MASH/MASLD` | TEAD4 / TAZ |
| `design PPI inhibitors targeting the KRAS-RAF1 interface` | KRAS / RAF1 |
| `design inhibitors for GPCRs involved in metabolic disease` | GCGR, CB1R |

Rules of thumb, learned from those runs:

- **Name a disease or a process, not a mechanism you have already chosen.** The
  pathway stage is the part being tested; over-specifying skips it.
- **One sentence is enough.** The mesothelioma run went from that single
  sentence to a completed GPU campaign unattended.
- **Say "inhibitors", "binders" or "therapeutics"** so the design intent is
  unambiguous.
- **You can name the interface** (`the KRAS-RAF1 interface`) — it still runs
  discovery, just constrained.
- **Broad prompts are the interesting test.** They are where the failure modes
  live. A prompt that lands on a membrane protein is especially worth
  reporting.

If you want to override the structure it chose, re-run with `--pdb 6E3Y`. The
pipeline sometimes deliberately substitutes a *different* structure than the
literature recommends — it explains itself in a `## STRUCTURE SUBSTITUTION`
section at the bottom of `00_pathway.md`.

## Going past the spec stage: point LPT at foundry

The design engines (RFD3 → solubleMPNN → RF3) come from **foundry**, a separate
project LPT does not ship. Drop `--stop-after spec` once it is set up.

**If you don't have foundry:** install it from
<https://github.com/RosettaCommons/foundry> following their instructions,
including their model weights. Come back here afterwards.

**If you already have it,** LPT needs two paths in `.env`:

```bash
LPT_FOUNDRY_ROOT=/path/to/foundry          # the checkout directory
LPT_FOUNDRY_CKPT_DIR=/path/to/weights      # only if not ~/pip_rcfoundry_ckpt
```

Then confirm:

```bash
python scripts/doctor.py --track binder
```

It reports the checkout, the engine binaries and the weights. LPT finds the
foundry venv itself whatever it is called; if you have more than one it will
ask you to name it, since the wrong one may be built for a different GPU.

**24 GB VRAM** runs small-to-moderate designs. Bigger targets want more.
Budget **~15 GB free disk** for a typical production campaign. Measured over
six real campaigns, a refold directory costs ~0.6-1.9 MB depending on complex
size, and the largest campaign here came to 15 GB across all three stages; a
big target sized at the un-calibrated 3000-batch default could reach ~90 GB.
`plan_campaign` estimates it up front from the complex size and clamps
`n_batches` to fit.

## What a full GPU run costs

Measured on real runs:

| | LLM spend | GPU |
|---|---|---|
| PPI prompt → production (mesothelioma) | **$0.79** | ~22 GPU-h |
| Binder track → production (PD-L1) | $2.12 | — |
| Typical stop-after-spec test | **$0.35 – $1.00** | none |

The pipeline never scales straight to production: it runs a ~300-backbone
trial, *measures* the hit rate, and reports a SCALE_UP / ITERATE / STOP verdict
with a confidence interval before spending real GPU time. That verdict is
always a pause point.

## Reading the results

Every completed trial and campaign writes a self-contained `report.html` — open
it directly in a browser, no server needed. It carries the structure and site
rationale, the confidence distributions, an embedded Mol* viewer over the
top-ranked designs' actual refolds, and an appendix with every stage report in
full.

```bash
python scripts/campaign_status.py projects/<slug>       # progress of a running campaign
python scripts/generate_binder_report.py projects/<slug>/runs/round-1/binder
```

---

# Track C — asking the literature

## From the command line

```bash
python scripts/ask_corpus.py "How does the FACT complex reposition the H2A-H2B dimer?"
```

Runs an interactive session on `gemini-3.7-flash`; each answer cites papers by
title and DOI, and says explicitly what the corpus does *not* contain. It has
the full corpus toolset, not just search — the interaction graph, DepMap
co-essentiality, clusters and quantitative-evidence lookup — so questions like
"what interacts with SPT16, and at what affinity?" return a sourced table.
`--provider claude` switches models.

For literal keyword matching instead of semantic search:

```bash
python scripts/search_fingerprints.py SPT16 SSRP1
```

## Inside Claude Code or Claude Desktop

```bash
python scripts/setup_mcp_json.py
```

This writes `.mcp.json` with absolute paths for *this* checkout (it is
machine-specific and gitignored, so every clone generates its own). It
registers two servers: `literature-db` (15 corpus tools) and `structure-tools`
(8 structure calculations, no API key needed).

- **Claude Code** picks `.mcp.json` up from the project root — just start a new
  session.
- **Claude Desktop** needs the `mcpServers` block copied into
  `claude_desktop_config.json` (`%APPDATA%\Claude\` on Windows,
  `~/Library/Application Support/Claude/` on macOS,
  `~/.config/Claude/` on Linux), **then a restart**. It reads that file only at
  startup, and without the restart the servers simply will not appear.

The servers deliberately **do not auto-trigger**. Ask for the corpus explicitly
— "search the corpus for…", "what does the literature database say about…" —
otherwise you get the model's own knowledge, which for most questions is
broader than this corpus.

## What to ask

The corpus is one lab's reading list, not a survey of biology. It is heavily
weighted toward **chromatin, histone chaperones, and structural/chemical
biology**, and filtered to tier 1–2 journals. Knowing that is the difference
between a fair test and a misleading one.

**Well represented** — cGAS/STING (3,264 papers), chromatin (2,565), cryo-EM
(1,193), TEAD (1,176), nucleosome (765), YAP (537), histone chaperones (256):

```
What does the corpus say about cGAS-STING pathway activation mechanisms?
What structural evidence characterises the YAP-TEAD interface, and are there measured Kd values?
Which papers describe FACT binding to the H2A-H2B dimer, and by what mechanism?
```

**Thin or absent** — GPCRs (394, 3.4%), orphan GPCRs (0.43%), developmental
biology (13 papers, a known gap):

```
What GPCR allosteric modulators exist for chronic pain?      ← will answer badly
```

That last one is worth trying **once**, deliberately, so you can see the
failure mode: the corpus's silence looks like the field's silence. If you ask
about a field outside the corpus's scope and get a thin answer, that is the
corpus, not the tool.

## The tool menu

Ask for these in plain language; the model picks the tool.

**Literature** — semantic search over fingerprints · full detail for one DOI ·
PDB structures cited in the corpus · direct RCSB fallback search.

**Graph and clusters** — what interacts with X · measured Kd/Ki for a pair ·
shortest path between two proteins · the corpus's biggest hubs · how novel a
protein is · export a neighbourhood to Cytoscape · Louvain clusters.

**DepMap co-dependency** — is an interaction supported by CRISPR
co-essentiality · what genes are most co-essential with X.

> *Needs one optional hand download:* **`CRISPRGeneEffect.csv`** (~420 MB)
> from <https://depmap.org/portal/data_page/?tab=allData>, saved as
> `data/depmap/CRISPRGeneEffect.csv` — or set `LPT_DEPMAP_CSV` in `.env` to a
> copy you already have. DepMap's portal 403s a scripted GET,
> so `fetch_reference_data.py --with-depmap` prints these steps rather than
> downloading. Without it these two tools return an error and **the run
> continues** — which you will see as a `WARNING` in a design run too, since
> `find_cocorrelated_genes` is offered to the pathway stage, not just the
> wildcard track. That is not a failed install.

**Structure calculations** (no API key, no corpus) — interface analysis and
buried surface area · residue contacts · mutation clash checks · sequence and
numbering maps · surface-patch scoring · molecular-glue pockets · UniProt
lookup.

---

# Reporting back

Most useful to us, in order:

1. **A stage that was confidently wrong.** A chain assigned to the wrong
   protein, a hotspot on a residue that is not at the interface, a structure
   choice you would not defend. Send the `projects/<slug>/` directory or just
   the stage `.md` file.
2. **A prompt that produced a bad target.** Include the exact prompt.
3. **Setup friction.** Anything where `doctor.py` said you were fine and you
   were not.
4. **Cost surprises.** The per-stage ledger is `projects/<slug>/ledger.jsonl`.

Known rough edges, so you do not spend time on them: the corpus release asset
may not be published yet (step 4); the `web/` directory is unmaintained and
excluded; PyRosetta scoring is optional and off unless installed.
