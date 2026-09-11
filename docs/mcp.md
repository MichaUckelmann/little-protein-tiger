# Using LPT from Claude Desktop / Claude Code

LPT ships two MCP servers. Registering them lets you use the corpus and
structure tools **conversationally**, driven by your Claude subscription rather
than by an API key.

This is the cheapest way to use LPT, and for exploratory work it is often the
better one: you ask questions and follow them up, instead of committing to a
pipeline run.

## What it gets you

| Server | Tools | Needs |
|---|---|---|
| `literature-db` | 15 — semantic corpus search, fingerprints, the interaction graph, clusters, DepMap co-essentiality, PDB discovery | the `corpus` extra + a corpus |
| `structure-tools` | 8 — interface analysis, residue contacts, mutation clash, sequence maps, surface patches, glue pockets, identifier resolution | base install only |

`structure-tools` works on a bare clone with no corpus and no API key.

## Setup

```bash
python scripts/setup_mcp_json.py      # writes .mcp.json for THIS checkout
```

`.mcp.json` holds absolute paths to your venv and launcher scripts, so it is
gitignored and each checkout generates its own. Claude Code picks it up from the
project root — start a new session and the servers are there.

For Claude **Desktop**, copy the `mcpServers` block into
`claude_desktop_config.json`:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

then **restart Claude Desktop** — it reads that file only at startup, so
without a restart the servers simply will not appear and nothing says why.

If you use the literature server, warm the embedding cache once — the launcher
sets `HF_HUB_OFFLINE=1`, so an uncached model fails with a HuggingFace error
that never mentions LPT:

```bash
python -c "from sentence_transformers import SentenceTransformer as S; S('NeuML/pubmedbert-base-embeddings')"
```

Check it with `python scripts/doctor.py` — the MCP row reports whether
`.mcp.json` points at paths that exist.

## What this does and does not replace

**Does:** everything exploratory. "What's known about the KRAS–RAF1 interface?",
"Which proteins interact with TEAD1 in the corpus?", "Analyse the interface
between chains A and B of 6VJJ", "Find PDB structures for YAP1 and TEAD4."

**Does not:** run a design campaign. The pipeline orchestrates nine stages,
manages a resumable manifest, drives multi-day GPU work and enforces a budget.
That is `scripts/run_pipeline.py`, and it uses an API key.

Worth knowing before you optimise for avoiding API calls: **only 3 of the 9
binder stages call an LLM at all.** Measured from real campaign ledgers, the
entire LLM spend of a full campaign is $0.36–$4.27. The GPU is the expensive
part, and no API is involved in it.

## Running a skill under your subscription

The `skills/*/SKILL.md` files are plain system prompts plus a tool protocol, so
Claude Code can run one directly against the MCP tools — no API key, your
subscription doing the work:

> Read `skills/corpus-explorer/SKILL.md` and follow it to answer:
> what does the corpus say about FACT complex binding to H2A–H2B?

This works well for the analysis skills (`corpus-explorer`,
`complex-structure-analysis`, `pathway-expert`, `wildcard-expert`). It is not a
substitute for the pipeline: a stage's `### PIPELINE HANDOFF` block is parsed by
the next stage, and nothing validates it until then, so a hand-produced handoff
that is subtly malformed fails confusingly two stages later.

## These tools do not auto-trigger — by design

Both servers carry instructions telling the model **not to reach for them on
its own**, and every `SKILL.md` frontmatter begins "Invoke ONLY when the user
explicitly asks…".

That is deliberate. The corpus is ~7,000 papers weighted toward chromatin,
histone chaperones and structural biology. Claude's own knowledge spans all of
biology. If a general question ("what does p53 do?") silently became a corpus
search, you would get a **narrower and worse answer than the model would have
given unaided** — and the corpus's blind spots would look like the state of the
field.

So ask for them explicitly: *"search the corpus for…"*, *"what does the
literature database say about…"*, *"analyse the interface of 6VJJ chains A
and B"*.

**This applies to the MCP transport only.** When `run_pipeline.py` runs a skill,
that skill was explicitly invoked for a corpus task, so its tools SHOULD be used
freely — those descriptions live in a separate table
(`skill_runner._TOOL_DEFS`) and keep their trigger language. The two tables have
opposite requirements and a test keeps them from converging.

## Tool parity between the two transports

The same `SKILL.md` runs under two transports — the MCP servers here, and an
in-process dispatch table in `src/skill_runner.py` for the CLI. **A tool a skill
calls must exist in both**, or the skill silently degrades under one of them.
`tests/test_release_fixes.py` pins this.

One deliberate asymmetry: `protein-design-script` and `orchestrator` reference
`filesystem:write_file`, which is the standard **filesystem** MCP server, not
LPT's. LPT does not expose a file-writing tool — it would be an arbitrary-write
surface for no benefit, and `complex-structure-analysis` explicitly instructs
against using one. If you want those two skills to write files under MCP,
configure the filesystem server yourself with a path allowlist you are happy
with.

## PDB discovery: read the flag

`find_pdb_structures` scans the corpus, and a paper's `pdb_accessions` includes
structures it **cites**, not only ones it deposited. For "YAP1" that is 41 hits,
of which RCSB's own metadata names YAP in exactly one — the rest are methods
references from papers that mention YAP1 in passing.

Results are therefore ranked with confirmed hits first, each flagged
`query_named_in_metadata`. Prefer those. Fall back to `search_rcsb_pdb` (RCSB
full-text, no corpus provenance) only when the corpus has nothing.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Server won't start on Windows | Don't bypass `scripts/launch_mcp.py` — it sets `OMP_NUM_THREADS=1` etc. before importing `sentence_transformers`, without which the import hangs |
| `search_corpus` fails with a HuggingFace error | Embedding model not cached; see the warm-up command above |
| Tools missing after moving the checkout | `.mcp.json` holds absolute paths — re-run `scripts/setup_mcp_json.py` |
| `literature-db` won't import | Needs the `corpus` extra: `.venv/bin/python -m pip install -e ".[corpus]"` |
| Corpus tools return nothing | No corpus installed: `python scripts/fetch_corpus.py` |
