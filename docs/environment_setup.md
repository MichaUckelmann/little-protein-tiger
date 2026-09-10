# Environment setup: local GPU tools and cluster staging

LPT's own Python package installs the same way everywhere (`pip install -e .`
into a venv — see the README). This doc covers the part that doesn't: the
binder/design track (`--workflow binder`, or `--workflow ppi` with
`--design-engine foundry`) drives a handful of **external GPU tools**, each
with its own environment, its own install steps, and its own machine-specific
absolute path. This doc is the map of what those tools are, how LPT finds
them, and how to point LPT at your own copies without touching a tracked
file.

If you're only using the literature corpus pipeline (`fetch_papers.py` /
`curate_papers.py` / `ask_corpus.py` / MCP skills), none of this applies —
skip this doc entirely.

## The rule: machine-specific paths go in `.env`, never in `config.yaml`

`config.yaml` is tracked in git. Every `design.*` key that names an absolute
path on somebody's specific machine is set to `null` there on purpose — that
value is real config for the *shape* of the pipeline (thresholds, batch
sizes, weights) but not for *where a binary happens to live on your disk*.

Instead, each of those paths can be set via an env var, read from `.env`
(gitignored, same mechanism this repo already uses for API keys — see
`.env.example`). `src/env_config.py`'s `resolve_env_path(env_var,
config_value)` is the one place this precedence is implemented: **the env var
wins if set; the `config.yaml` value is used only as a fallback** for anyone
who prefers to keep it there instead (e.g. a single-user machine where
`config.yaml` never gets pushed anywhere). Either way, if neither is set,
`LPT_FOUNDRY_ROOT` and the `LPT_CLUSTER_*` vars fail with an error naming
both the env var and the config key — not a stack trace three stages later.
`LPT_BOLTZGEN_EXECUTABLE` and `LPT_PYROSETTA_PYTHON` are the deliberate
exception: with neither set they fall back to a bare `boltzgen` /
`python` on `PATH`, which is a supported way to configure them, and only
fail if that command is missing too.

| Env var | What it points at | Read by |
|---|---|---|
| `LPT_BOLTZGEN_EXECUTABLE` | BoltzGen's entry-point script | `src/pipeline_runner.py` (PPI track, stage 4) |
| `LPT_PYROSETTA_PYTHON` | Python of a conda/venv env with PyRosetta installed | `src/pyrosetta_sasa.py`, `src/rosetta_metrics.py` |
| `LPT_FOUNDRY_ROOT` | A foundry (RFD3 / RF3 / solubleMPNN) checkout | `src/foundry_runner.py` (binder track + foundry-backend PPI bridge) |
| `LPT_CLUSTER_PIPELINE_ROOT` | A `binder_pipeline`-shaped checkout for cluster staging | `src/cluster_runner.py` (`--compute cluster` only) |
| `LPT_CLUSTER_PROTENIX_REPO` | A separate Protenix checkout, for MSA fetching | `src/cluster_runner.py` (cluster path, `msa_source: protenix_hosted`) |
| `LPT_CLUSTER_SUBMIT_INSTRUCTIONS` | Free text shown in the cluster pause message (login node, `cd` path) | `src/cluster_runner.py` |

All six are optional and independently gated — nothing at import time
requires any of them. Each is only consulted when the specific stage that
needs it actually runs, and each is entirely absent from the literature
corpus pipeline.

`scripts/setup.sh` finishes by running `scripts/doctor.py`, which *probes*
rather than path-tests: it runs the binary, calls `nvidia-smi`, and imports
`pyrosetta` under the configured interpreter, then reports readiness per
track with the command that fixes each gap. It covers four of the six vars
below — `LPT_FOUNDRY_ROOT`, `LPT_PYROSETTA_PYTHON`,
`LPT_BOLTZGEN_EXECUTABLE` and `LPT_CLUSTER_PIPELINE_ROOT`; the other two
`LPT_CLUSTER_*` vars are only read when a campaign is actually staged.
Re-run the report on its own any time with:

```bash
source .venv/bin/activate  # if not already active
python scripts/doctor.py   # or: ./scripts/setup.sh --check
```

## What each tool actually is, and how to get one

None of these ship with LPT or get installed by `pip install -e .` — they're
separate, often GPU-generation-specific installs. LPT never imports any of
them into its own process; every integration is a subprocess call to an
absolute path, so a broken or mismatched install in one of these tools can
never break LPT's own venv.

- **BoltzGen** (`LPT_BOLTZGEN_EXECUTABLE`) — install per its own README
  (`pip install boltzgen` or `uv pip install` into its own venv). Point at
  the entry-point script directly; its shebang carries its own venv, so no
  `conda activate` / `uv run` wrapping is needed.
- **PyRosetta** (`LPT_PYROSETTA_PYTHON`) — see `docs/pyrosetta_setup.md` in
  this repo for the full walkthrough, including the Python-ABI trap that
  makes a separate conda env necessary in the first place.
- **foundry (RFD3 / RF3 / solubleMPNN)** (`LPT_FOUNDRY_ROOT`) —
  **https://github.com/RosettaCommons/foundry**. This is the single hard
  dependency of the binder track: without it `--workflow binder` and
  `--design-engine foundry` cannot run at all. LPT neither ships nor installs
  it, and it carries **RosettaCommons' own licence terms, not LPT's MIT** —
  read them before any commercial use.

  Clone it, follow its own setup instructions, and point `LPT_FOUNDRY_ROOT` at
  the checkout directory. Two things to expect:

  - **The CUDA/torch build is tied to your GPU generation.** This repo's
    reference workstation needed a hand-built `.venv-blackwell` because the
    shipped container's torch didn't support that card's `sm_120`
    architecture. Expect something similarly bespoke for whatever GPU you
    have. `config.yaml`'s `design.foundry.launcher` / `rfd3_bin` / `rf3_bin` /
    `mpnn_bin` keys (paths *relative to* `LPT_FOUNDRY_ROOT`) are where that
    gets recorded once your install works.
  - **Model checkpoints are a separate download**, resolved through foundry's
    own checkpoint registry (`~/pip_rcfoundry_ckpt/` by default; set
    `LPT_FOUNDRY_CKPT_DIR` if yours live elsewhere, such as a shared lab
    volume). Note the
    registry aliases `rfd3` and `rf3` work but `solublempnn` does **not** —
    MPNN's config takes a literal path. `src/foundry_stages.py`'s
    `resolve_checkpoint()` globs the registry for the alias so this fails at
    config time rather than after RFD3 has already run for an hour.

  Requirements: a CUDA GPU — **24 GB VRAM** is enough for small-to-moderate
  designs, and larger targets scale from there — plus **~15 GB free disk** for
  a typical production campaign, up to ~90 GB for a large target at the
  un-calibrated default. Measured: a refold directory costs
  ~0.6-1.9 MB depending on complex size (`foundry_runner.refold_bytes`), so
  disk scales with the target the same way GPU time does.
- **Protenix** (`LPT_CLUSTER_PROTENIX_REPO`) — only needed for the cluster
  refold path's MSA fetch; a separate checkout with its own venv, subprocessed
  the same way as everything else here. If you don't have one,
  `design.cluster.msa_source: cluster_colabfold` defers to the cluster
  pipeline's own MSA fetching instead and this var can stay unset.
- **Optional structure tools** (`design.trim.chainsaw_cmd`,
  `design.trim.foldseek_bin` in `config.yaml` — not env vars, since these are
  genuinely optional quality improvements rather than "the stage can't run
  without this") — absent binaries degrade to RCSB annotations and a
  contact-graph partition; nothing breaks.

## The cluster path: staging, not running

`design.cluster.enabled` / `--compute cluster` is not "install a cluster on
your machine" — LPT cannot reach a SLURM scheduler directly from a
workstation. What it does is **stage** a campaign (spec, trimmed structure,
target MSA, a ready-to-run `launch.sh`) onto a shared filesystem path
(`LPT_CLUSTER_PIPELINE_ROOT`) and pause with instructions for a human to run
that script from a login node; resuming later reads results back off the
same filesystem. See `src/cluster_runner.py`'s module docstring and the
"Scale a campaign onto a SLURM cluster" section of the README for the full
mechanics.

This means the cluster path can only ever be as portable as *the target
pipeline it stages onto* — LPT deliberately reuses that pipeline's own
SLURM/apptainer machinery rather than reimplementing it. A new environment
needs its own compatible cluster-side pipeline already set up (or one to
adapt). The shape LPT's staging code assumes:

- A checkout with `bin/run_pipeline.sh` (the actual SLURM submission script)
  and `config/site.sh` (partition/account/container paths — genuinely that
  cluster's own config, LPT never touches it).
- An `examples/<name>/` convention under the checkout root for staged
  campaign inputs (`design.cluster.stage_subdir`).
- RFD3 → solubleMPNN → refold as one array job per GPU, with results landing
  at `<pipeline_root>/outputs/<run_name>/run_*/<backend>/` — one subdirectory
  per array task.
- Its own `NB` (designs-per-array-task) semantics — **NB is per GPU, not a
  campaign total**; see CLAUDE.md's cluster section if you're adapting
  `src/cluster_runner.py` for a differently-shaped pipeline.
- A refold backend that emits a per-design subdirectory with
  `<id>_scores.json` (Protenix's shape) or the RF3-native shape, so
  `src/binder_metrics.py`'s adapters have something to parse.

If your cluster's pipeline doesn't already look like this, `--compute
cluster` isn't a fit yet — `--compute local` (the default fallback, capped by
`design.foundry.max_local_hours`) still works standalone on any single GPU
workstation with no cluster involved at all.

## Containers?

Deliberately not part of this story. Every external tool above is already
isolated as a separate venv/conda env, invoked only by subprocess at a
resolved absolute path — the same isolation a container would buy, without
the added burden of matching a CUDA/torch build to your specific GPU
architecture inside a Dockerfile (which didn't remove that problem for the
one case here that actually hit a GPU-generation mismatch — Blackwell still
needed a hand-built venv). Containerizing the *literature/skills* side (no
GPU, no exotic native deps) would be low-risk if it's ever useful for CI, but
that's a separate, much smaller question from "how do users bring their own
GPU tools" — see `RELEASE_READINESS.md` for that context.
