#!/usr/bin/env python3
"""Check what this machine can actually run, and say how to fix what it can't.

LPT has four largely independent tracks with almost disjoint requirements:

    structure   PDB/interface analysis, trimming, reports   — base install only
    literature  corpus search, the discovery workflows      — + corpus extra + data
    ppi         discovery + design; its engine is set by
                design.backend (boltzgen or foundry)
    binder      RFD3/MPNN/RF3 campaigns                     — + foundry + a GPU

Three of the four are usable without the fourth, so a single pass/fail verdict
would be useless. This reports per track.

Every check PROBES rather than testing for a path: it runs the binary, calls
nvidia-smi, imports pyrosetta under the configured interpreter, measures free
disk. `setup.sh`'s old diagnostic used `Path.exists()` on a value from
config.yaml, which is why it happily printed "[x] Foundry ... found" on a
machine that had none — the path it checked belonged to the maintainer.

    python scripts/doctor.py                 # everything
    python scripts/doctor.py --track binder  # just one track
    python scripts/doctor.py --json          # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.env_config import load_env, resolve_env_path  # noqa: E402

load_env(_ROOT / ".env")

OK, WARN, FAIL, NA = "ok", "warn", "fail", "n/a"

_MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL", NA: "--  "}
_COLOR = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", NA: "\033[90m"}
_RESET = "\033[0m"
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

TRACKS = ("structure", "literature", "ppi", "binder", "cluster")


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    tracks: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix,
                "tracks": list(self.tracks)}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *a, **kw) -> None:
        self.checks.append(Check(*a, **kw))

    def for_track(self, track: str) -> list[Check]:
        return [c for c in self.checks if track in c.tracks]

    def verdict(self, track: str) -> str:
        states = {c.status for c in self.for_track(track)}
        if FAIL in states:
            return "NOT READY"
        if WARN in states:
            return "READY (with caveats)"
        return "READY"


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _run(argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run a command, returning (rc, first line of output). rc=-1 if it
    could not be launched at all."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    out = (p.stdout or p.stderr or "").strip().splitlines()
    return p.returncode, (out[0] if out else "")


def check_interpreter(rep: Report) -> None:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 12)
    # 3.13 works, but its stricter X.509 validation rejects certificates
    # re-signed by TLS-inspecting corporate proxies that 3.12 accepts — and no
    # CA bundle fixes that. Worth flagging before a download fails confusingly.
    newer = ok and (v.major, v.minor) > (3, 12)
    # ...but not if the operator has already applied the fix. Nagging about a
    # setting that is set means the track can never report clean, and the last
    # thing `SETUP_AGENT.md` Phase 9 shows the user is this row.
    if newer and os.environ.get("LPT_SSL_RELAX_STRICT", "").strip().lower() in (
            "1", "true", "yes", "on"):
        rep.add("Python", OK,
                f"{v.major}.{v.minor}.{v.micro}  (LPT_SSL_RELAX_STRICT set)",
                tracks=TRACKS)
        return
    rep.add("Python", FAIL if not ok else (WARN if newer else OK),
            f"{v.major}.{v.minor}.{v.micro}"
            + ("" if sys.prefix != sys.base_prefix else "  (not in a venv)"),
            "LPT requires Python 3.12-3.14 (pyproject requires-python)." if not ok
            else ("Tested in CI on 3.12 and 3.13. On 3.13+ behind a "
                  "TLS-inspecting proxy, "
                  "downloads can fail with 'Missing Authority Key Identifier' — "
                  "3.13 enforces stricter certificate rules and a CA bundle does "
                  "not help. Either set LPT_SSL_RELAX_STRICT=1 in .env "
                  "(see .env.example — it clears that one flag and keeps "
                  "trust-chain, hostname and expiry checks) or use "
                  "python3.12." if newer else ""),
            tracks=TRACKS)


def check_package(rep: Report) -> None:
    try:
        import src.pipeline_runner  # noqa: F401
        rep.add("Package imports", OK, "src.pipeline_runner", tracks=TRACKS)
    except Exception as exc:                      # pragma: no cover - env dependent
        rep.add("Package imports", FAIL, str(exc)[:90],
                'pip install -e ".[dev]"', tracks=TRACKS)


def check_corpus_extra(rep: Report) -> None:
    missing = [m for m in ("lancedb", "sentence_transformers")
               if not _module_available(m)]
    if missing:
        rep.add("Corpus extra", FAIL, f"missing: {', '.join(missing)}",
                'pip install -e ".[corpus]"   (~3 GB: pulls torch)',
                tracks=("literature",))
    else:
        rep.add("Corpus extra", OK, "lancedb + sentence-transformers",
                tracks=("literature",))


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# The literal values shipped in .env.example. `cp .env.example .env` and
# forgetting to edit it leaves every one of these non-empty, so a bare
# `bool(os.environ.get(...))` reported a fully-unconfigured checkout as green
# and the user found out several stages later from a provider-side 403.
# The list moved to `src.env_config` so `skill_runner`'s own preflight uses the
# same one — it used to accept the placeholder and fail at the first API call.
from src.env_config import PLACEHOLDERS as _PLACEHOLDERS  # noqa: E402,F401
from src.env_config import key_is_usable as _key_state    # noqa: E402


def _curation_provider() -> str:
    """Which provider curation is CONFIGURED to use, not which one it once used.

    This was hardcoded as "claude", and told every reader of `doctor.py` that
    extending the corpus needs an Anthropic key. `curation.provider` has since
    moved to gemini, so the advice was backwards for exactly the person most
    likely to follow it — someone setting up for the first time and deciding
    which keys to pay for.
    """
    import yaml
    try:
        cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        return str((cfg.get("curation") or {}).get("provider") or "").lower()
    except (OSError, yaml.YAMLError):
        return ""


def check_api_keys(rep: Report) -> None:
    # Gemini is the default provider for every pipeline stage, for
    # `scripts/ask_corpus.py`, and (since it moved off claude) for CURATION too.
    # Read the config rather than restating it — see `_curation_provider`.
    curation = _curation_provider()
    gem, gem_why = _key_state("GEMINI_API_KEY")
    ant, ant_why = _key_state("ANTHROPIC_API_KEY")
    rep.add("GEMINI_API_KEY", OK if gem else FAIL,
            "set" if gem else f"{gem_why} — the default provider for every stage",
            "" if gem else "Add a real GEMINI_API_KEY to .env (see .env.example).",
            tracks=("ppi", "binder"))
    # Literature is a WARN, not a FAIL, and deliberately differs from the two
    # design tracks: `search_corpus` runs entirely locally against the
    # downloaded index, so the MCP route README advertises to Claude
    # subscription users needs NO key at all. Reporting the track NOT READY on
    # this row alone told a user their corpus was broken while it was serving
    # real results — and SETUP_AGENT.md Phase 9 only suggests next commands for
    # tracks that read READY.
    rep.add("GEMINI_API_KEY", OK if gem else WARN,
            "set" if gem else f"{gem_why} — needed for the CLI "
                              "(ask_corpus.py, curation); the MCP route needs none",
            "" if gem else ("Add a real GEMINI_API_KEY to .env for the CLI. "
                            "search_corpus over MCP works without it."),
            tracks=("literature",))
    # The STRUCTURE track needs it too, and this row was missing: `--workflow
    # structure` picks the epitope with an LLM at its `interface` stage, so a
    # keyless run died there having already downloaded and analysed the
    # structure. It is a WARN rather than a FAIL because `--hotspots` makes
    # that stage deterministic and the track genuinely keyless.
    rep.add("GEMINI_API_KEY", OK if gem else WARN,
            "set" if gem else f"{gem_why} — needed unless you pass --hotspots",
            "" if gem else ("Add a real GEMINI_API_KEY to .env, or name the "
                            "epitope yourself with --hotspots B56,B66 and the "
                            "interface stage makes no model call."),
            tracks=("structure",))
    # Design tracks: a nice-to-have (refusal fallback, --provider claude).
    rep.add("ANTHROPIC_API_KEY", OK if ant else WARN,
            "set" if ant else f"{ant_why} — needed for --provider claude "
                              "and as the refusal fallback",
            "" if ant else "Optional here. Add ANTHROPIC_API_KEY to .env to enable it.",
            tracks=("ppi", "binder"))
    # Literature track. Whether an Anthropic key is actually needed depends on
    # what curation is configured to use, so say which and why.
    if curation == "claude":
        detail = (f"{ant_why} — needed to EXTEND the corpus")
        hint = ("Optional for querying (ask_corpus.py defaults to gemini). "
                "Needed to curate new papers: curation.provider in config.yaml "
                "is 'claude'.")
    else:
        detail = (f"{ant_why} — not needed: curation runs on "
                  f"{curation or 'the configured provider'}")
        hint = (f"Optional. Querying and curation both run on "
                f"{curation or 'the configured provider'}; an Anthropic key only "
                f"adds --provider claude and the refusal fallback.")
    rep.add("ANTHROPIC_API_KEY", OK if ant else WARN,
            "set" if ant else detail, "" if ant else hint,
            tracks=("literature",))
    email, email_why = _key_state("NCBI_EMAIL")
    rep.add("NCBI_EMAIL", OK if email else WARN,
            "set" if email else f"{email_why} — PubMed will rate-limit harder",
            "" if email else "Add your real NCBI_EMAIL to .env.",
            tracks=("literature",))


def check_reference_data(rep: Report) -> None:
    depmap = _ROOT / "data" / "depmap"
    required = {"HUMAN_9606_idmapping.dat.gz": "gene symbol -> UniProt",
                "hgnc_complete_set.tsv": "approved symbols and aliases"}
    missing = [n for n in required if not (depmap / n).is_file()]
    if missing:
        rep.add("Reference data", FAIL,
                f"missing: {', '.join(missing)}",
                "python scripts/fetch_reference_data.py   (~52 MB, public)",
                tracks=("binder", "ppi"))
    else:
        rep.add("Reference data", OK, "UniProt id-mapping + HGNC",
                tracks=("binder", "ppi"))

    crispr = depmap / "CRISPRGeneEffect.csv"
    rep.add("DepMap CRISPR matrix", OK if crispr.is_file() else NA,
            "present" if crispr.is_file()
            else "absent — only the wildcard-expert DepMap tools need it",
            "" if crispr.is_file()
            else "Optional, ~420 MB: https://depmap.org/portal/data_page/?tab=allData",
            tracks=("literature",))


def check_corpus(rep: Report) -> None:
    db = _ROOT / "data" / "literature.db"
    fps = _ROOT / "data" / "fingerprints"
    vec = _ROOT / "data" / "vectors"
    n_fp = len(list(fps.glob("*.json"))) if fps.is_dir() else 0

    if not db.is_file():
        rep.add("Corpus database", FAIL, "data/literature.db absent",
                "python scripts/fetch_corpus.py   "
                "(~105 MB, free — the curated corpus ships pre-built)",
                tracks=("literature",))
    else:
        rep.add("Corpus database", OK, f"{db.stat().st_size/2**20:.0f} MB",
                tracks=("literature",))

    rep.add("Fingerprints", OK if n_fp else FAIL,
            f"{n_fp:,} curated" if n_fp else "none — nothing to search",
            "" if n_fp else "python scripts/fetch_corpus.py",
            tracks=("literature",))

    rep.add("Vector index", OK if vec.is_dir() else FAIL,
            "present" if vec.is_dir() else "absent — search_corpus will fail",
            "" if vec.is_dir() else
            "python scripts/fetch_corpus.py   (or scripts/ingest_vectors.py "
            "if you built your own fingerprints)",
            tracks=("literature",))


def check_embedding_cache(rep: Report) -> None:
    """launch_mcp.py sets HF_HUB_OFFLINE=1, so an uncached model fails with a
    HuggingFace error that names nothing about LPT."""
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    hits = list(home.rglob("*pubmedbert*")) if home.exists() else []
    rep.add("Embedding model cached", OK if hits else WARN,
            "NeuML/pubmedbert-base-embeddings" if hits
            else "not in the HuggingFace cache",
            # NOT a bare `python -c`: that skips `env_config.load_env()`, so
            # neither LPT_CA_BUNDLE nor LPT_SSL_RELAX_STRICT applies and the
            # download fails verification behind a TLS-inspecting proxy —
            # measured at 73 s to fail versus 13 s for the script. This string
            # matters because SETUP_AGENT.md Phase 5 tells an agent to trust
            # doctor's remediation over its own judgement.
            "" if hits else "python scripts/warm_embedding_cache.py",
            tracks=("literature",))


def check_gpu(rep: Report) -> None:
    # Same tuple check_foundry makes: with design.backend: foundry a PPI run
    # bridges into the very same GPU stages the binder track runs.
    tracks = ("binder",) if _ppi_engine() != "foundry" else ("binder", "ppi")
    exe = shutil.which("nvidia-smi")
    if not exe:
        rep.add("GPU", FAIL, "nvidia-smi not found",
                "The design stages need a CUDA GPU (>=32 GB recommended); "
                "with design.backend: foundry that includes the ppi track. "
                "Corpus and literature work runs CPU-only.",
                tracks=tracks)
        return
    rc, line = _run([exe, "--query-gpu=name,memory.total",
                     "--format=csv,noheader"])
    if rc != 0 or not line:
        rep.add("GPU", WARN, "nvidia-smi present but returned nothing",
                "Driver problem? Try running nvidia-smi by hand.",
                tracks=("binder",))
        return
    name, _, mem = line.partition(",")
    try:
        gb = int("".join(ch for ch in mem if ch.isdigit())) / 1024
    except ValueError:
        gb = 0.0
    rep.add("GPU", OK if gb >= 31.0 else WARN,
            f"{name.strip()}, {gb:.0f} GB",
            "" if gb >= 31.0 else
            "config.yaml assumes ~32 GB; smaller cards may OOM on large targets.",
            tracks=tracks)


def check_disk(rep: Report) -> None:
    tracks = ("binder",) if _ppi_engine() != "foundry" else ("binder", "ppi")
    free_gb = shutil.disk_usage(_ROOT).free / 2**30
    rep.add("Free disk", OK if free_gb >= 120 else WARN,
            f"{free_gb:.0f} GB at {_ROOT}",
            "" if free_gb >= 120 else
            "A full production campaign needs ~120 GB (~2.5 MB per RF3 design). "
            "plan_campaign will clamp the campaign to fit.",
            tracks=tracks)


def check_foundry(rep: Report) -> None:
    # A PPI run configured for foundry needs all of this too.
    tracks = ("binder",) if _ppi_engine() != "foundry" else ("binder", "ppi")
    root = resolve_env_path("LPT_FOUNDRY_ROOT", None)
    if not root:
        rep.add("foundry", FAIL, "LPT_FOUNDRY_ROOT not set",
                "Install from https://github.com/RosettaCommons/foundry, then set "
                "LPT_FOUNDRY_ROOT in .env. See docs/environment_setup.md.",
                tracks=tracks)
        return
    path = Path(root)
    if not path.is_dir():
        rep.add("foundry", FAIL, f"{root} is not a directory",
                "LPT_FOUNDRY_ROOT must point at your foundry checkout.",
                tracks=tracks)
        return
    venvs = [p for p in path.glob(".venv*") if p.is_dir()]
    rep.add("foundry", OK if venvs else WARN, f"{root}"
            + (f"  ({', '.join(p.name for p in venvs)})" if venvs else
               "  — no .venv* found inside"),
            "" if venvs else
            "foundry needs its own venv with a torch built for your GPU "
            "generation; see docs/environment_setup.md.",
            tracks=tracks)

    ckpt = Path(os.environ.get("LPT_FOUNDRY_CKPT_DIR")
                or Path.home() / "pip_rcfoundry_ckpt")
    rep.add("foundry checkpoints", OK if ckpt.is_dir() else WARN,
            str(ckpt) if ckpt.is_dir() else "checkpoint registry not found",
            "" if ckpt.is_dir() else
            "RFD3/RF3 resolve checkpoints through foundry's registry "
            "Set LPT_FOUNDRY_CKPT_DIR in .env if your weights are "
            "elsewhere, or fetch them per foundry's own docs.",
            tracks=tracks)


def check_pyrosetta(rep: Report) -> None:
    """PyRosetta is optional and scoring-only — report it as such."""
    import yaml

    try:
        cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
        design = (cfg or {}).get("design") or {}
    except OSError:
        design = {}

    from src.pyrosetta_sasa import check_available, pyrosetta_mode

    mode = pyrosetta_mode(design)
    try:
        usable, reason = check_available(design)
    except Exception as exc:                       # enabled: true, not installed
        rep.add("PyRosetta", FAIL, str(exc)[:110],
                "Set LPT_PYROSETTA_PYTHON, or use design.pyrosetta.enabled: auto.",
                tracks=("ppi", "binder"))
        return

    if not usable:
        rep.add("PyRosetta", NA if mode == "off" else WARN, reason,
                "" if mode == "off" else
                "Optional (scoring only): hotspot SASA and the Rosetta "
                "composite terms are skipped without it. "
                "See docs/pyrosetta_setup.md.",
                tracks=("ppi", "binder"))
        return

    # Probe it: does `import pyrosetta` actually work under that interpreter?
    from src.pyrosetta_sasa import resolve_interpreter

    exe = resolve_interpreter(design)
    rc, line = _run([exe, "-c", "import pyrosetta; print('ok')"], timeout=90)
    rep.add("PyRosetta", OK if rc == 0 else FAIL,
            exe if rc == 0 else f"{exe} cannot import pyrosetta",
            "" if rc == 0 else
            "The interpreter exists but has no working pyrosetta — "
            "see docs/pyrosetta_setup.md (the Python-ABI trap).",
            tracks=("ppi", "binder"))


def _ppi_engine() -> str:
    """Which design engine --workflow ppi will actually use.

    `design.backend` decides it (CLI --design-engine overrides per-run). Report
    against the CONFIGURED engine rather than assuming BoltzGen — otherwise a
    checkout switched to foundry gets told to install a tool it never calls,
    and is not told its foundry install is the one that matters.
    """
    import yaml

    try:
        cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    except OSError:
        return "boltzgen"
    return ((cfg or {}).get("design") or {}).get("backend") or "boltzgen"


def check_boltzgen(rep: Report) -> None:
    engine = _ppi_engine()
    if engine == "foundry":
        rep.add("PPI design engine", OK, "foundry (design.backend) — "
                "the same RFD3/MPNN/RF3 machine as --workflow binder",
                "PPI runs through the foundry bridge, so the binder-track "
                "checks above are the ones that matter; BoltzGen is unused.",
                tracks=("ppi",))
        return
    rep.add("PPI design engine", OK, "boltzgen (design.backend)",
            tracks=("ppi",))

    exe = resolve_env_path("LPT_BOLTZGEN_EXECUTABLE", None)
    if not exe:
        rep.add("BoltzGen", FAIL, "LPT_BOLTZGEN_EXECUTABLE not set",
                "Needed for --workflow ppi's design/execution stages. "
                "Set it in .env; see docs/environment_setup.md.",
                tracks=("ppi",))
        return
    if not Path(exe).exists():
        rep.add("BoltzGen", FAIL, f"{exe} does not exist",
                "Point LPT_BOLTZGEN_EXECUTABLE at the boltzgen binary.",
                tracks=("ppi",))
        return
    rc, _ = _run([exe, "--help"], timeout=60)
    rep.add("BoltzGen", OK if rc == 0 else WARN, exe,
            "" if rc == 0 else "The binary exists but `--help` failed.",
            tracks=("ppi",))


def check_cluster(rep: Report) -> None:
    root = resolve_env_path("LPT_CLUSTER_PIPELINE_ROOT", None)
    rep.add("Cluster pipeline_root", OK if root and Path(root).is_dir() else NA,
            root if root else "not set — --compute cluster unavailable",
            "" if root else
            "Only needed for --compute cluster. See docs/environment_setup.md.",
            tracks=("cluster",))


def check_mcp(rep: Report) -> None:
    mcp = _ROOT / ".mcp.json"
    if not mcp.is_file():
        rep.add("MCP registration", NA, ".mcp.json absent",
                "Only needed for Claude Desktop/Code: "
                "python scripts/setup_mcp_json.py",
                tracks=("literature", "structure"))
        return
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
        cmds = [s.get("command", "") for s in (data.get("mcpServers") or {}).values()]
        stale = [c for c in cmds if c and not Path(c).exists()]
    except (OSError, json.JSONDecodeError):
        stale, cmds = ["unreadable"], []
    rep.add("MCP registration", WARN if stale else OK,
            f"{len(cmds)} server(s)" + (" — stale paths" if stale else ""),
            "python scripts/setup_mcp_json.py" if stale else "",
            tracks=("literature", "structure"))


def check_structures(rep: Report) -> None:
    d = _ROOT / "data" / "structures"
    n = len(list(d.glob("*.cif"))) if d.is_dir() else 0
    rep.add("Structure cache", OK, f"{n} file(s) — downloaded on demand",
            tracks=("structure", "ppi", "binder"))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt(status: str) -> str:
    mark = _MARK[status]
    return f"{_COLOR[status]}[{mark}]{_RESET}" if _USE_COLOR else f"[{mark}]"


def render(rep: Report, tracks: list[str]) -> int:
    print(f"\nLPT environment check — {_ROOT}\n")
    seen: set[int] = set()
    for track in tracks:
        checks = rep.for_track(track)
        if not checks:
            continue
        print(f"  {track.upper()}")
        for c in checks:
            key = id(c)
            dup = "  (as above)" if key in seen else ""
            seen.add(key)
            print(f"    {_fmt(c.status)} {c.name:24s} {c.detail}{dup}")
            if c.fix and c.status in (FAIL, WARN) and not dup:
                for line in c.fix.splitlines():
                    print(f"           -> {line}")
        print()

    print("  VERDICT")
    worst_ok = True
    for track in tracks:
        if not rep.for_track(track):
            continue
        v = rep.verdict(track)
        if v == "NOT READY":
            worst_ok = False
        colour = OK if v == "READY" else (WARN if "caveats" in v else FAIL)
        print(f"    {_fmt(colour)} {track:12s} {v}")
    print()
    return 0 if worst_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check what this machine can run, and how to fix what it can't.")
    ap.add_argument("--track", choices=(*TRACKS, "all"), default="all",
                    help="Report on one track only (default: all).")
    ap.add_argument("--json", action="store_true",
                    help="Machine-readable output.")
    args = ap.parse_args()

    rep = Report()
    for probe in (check_interpreter, check_package, check_corpus_extra,
                  check_api_keys, check_reference_data, check_corpus,
                  check_embedding_cache, check_structures, check_gpu,
                  check_disk, check_foundry, check_pyrosetta,
                  check_boltzgen, check_cluster, check_mcp):
        try:
            probe(rep)
        except Exception as exc:                   # a broken probe must not
            rep.add(probe.__name__, WARN,          # sink the whole report
                    f"check itself failed: {exc}", tracks=TRACKS)

    tracks = list(TRACKS) if args.track == "all" else [args.track]

    if args.json:
        print(json.dumps({
            "root": str(_ROOT),
            "checks": [c.as_dict() for c in rep.checks],
            "verdicts": {t: rep.verdict(t) for t in tracks if rep.for_track(t)},
        }, indent=2))
        return 0 if all(rep.verdict(t) != "NOT READY"
                        for t in tracks if rep.for_track(t)) else 1

    return render(rep, tracks)


if __name__ == "__main__":
    raise SystemExit(main())
