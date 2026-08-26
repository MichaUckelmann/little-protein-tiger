"""
Stage a binder campaign for the CLUSTER compute path and read its results back.

Compute model = "LPT stages, a human submits, LPT reads results off shared
storage" — a third variant alongside foundry's "LPT drives the GPU" (this
workstation) and the old enzyme track's "LPT preps + validates" (external
ORCA). This machine has no SLURM login-node access, so unlike foundry_runner
this module never launches a job; it prepares inputs in the exact shape
g-groups/.../binder_pipeline already expects, writes a
ready-to-run launch script, and pauses. `collect_campaign()` on resume reads
back whatever that pipeline produced.

REUSES that pipeline rather than reimplementing it: its own `config/site.sh`
stays the single source of truth for partition/account/container paths, its
own `bin/run_pipeline.sh` + `slurm/*.slurm` stay the only place SLURM
directives live. This module's job is narrower — get a spec, a trimmed
structure and (for Protenix) a target MSA onto shared storage in the shapes
that pipeline's own scripts read, and get its output CSVs back into LPT's own
scoring path (src/binder_metrics.score_campaign_protenix).

## Why Protenix needs an MSA and RF3 doesn't

RF3 templates the target chain directly from its input structure. Protenix (and
every other folding-repo backend on the cluster) co-folds the target from
SEQUENCE ALONE with no template — measured on CD79b, an un-MSA'd target
refolds ~11 A wrong (vs RF3's 0.4 A), which lands straight in binder_rmsd_bb
and makes every design-recapitulation metric meaningless. Fetching a real
target MSA is therefore not an accuracy nice-to-have here, it is the
difference between usable and unusable output.

## Two-view paths

The submit host and compute nodes mount the same share at different prefixes
(see pipeline_root/lib/paths.sh `repo_paths()`); everything this module writes
INTO a generated script for the cluster to read must be expressed in whatever
form that pipeline's own `config/site.sh` documents for its `$NODE_PKG` /
`SHARE_NODE_PREFIX` — this module never assumes it knows that prefix itself,
it only ever writes paths relative to `pipeline_root` and lets the existing
`bin/run_pipeline.sh` do its own node-path translation, exactly as it already
does for a human running it directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from src.env_config import resolve_env_path


class ClusterError(RuntimeError):
    """A cluster campaign could not be staged."""


class MsaFetchError(ClusterError):
    """The target MSA could not be fetched or reused from cache."""


@dataclass(frozen=True)
class ClusterConfig:
    enabled: bool
    # None means "not configured" — deliberately NOT Path(""), which
    # pathlib normalises to Path(".") and would make an unconfigured root
    # silently pass an `is_dir()` check against the current working
    # directory instead of failing loudly.
    pipeline_root: "Path | None"
    stage_subdir: str
    submit_instructions: str
    refold_backend: str
    use_msa: bool
    msa_source: str
    protenix_repo: "Path | None"
    protenix_venv: str
    n_gpus: int
    pilot: dict
    calibration: dict
    production: dict
    diffusion_batch_size: int
    n_seq: int
    poll_hint_s: int

    @classmethod
    def from_cfg(cls, cfg: dict) -> "ClusterConfig":
        c = ((cfg or {}).get("design") or {}).get("cluster") or {}
        pipeline_root = resolve_env_path("LPT_CLUSTER_PIPELINE_ROOT", c.get("pipeline_root"))
        protenix_repo = resolve_env_path("LPT_CLUSTER_PROTENIX_REPO", c.get("protenix_repo"))
        return cls(
            enabled=bool(c.get("enabled", False)),
            pipeline_root=Path(pipeline_root).expanduser() if pipeline_root else None,
            stage_subdir=c.get("stage_subdir", "examples"),
            submit_instructions=resolve_env_path(
                "LPT_CLUSTER_SUBMIT_INSTRUCTIONS", c.get("submit_instructions")
            ) or "",
            refold_backend=c.get("refold_backend", "protenix"),
            use_msa=bool(c.get("use_msa", True)),
            msa_source=c.get("msa_source", "protenix_hosted"),
            protenix_repo=Path(protenix_repo).expanduser() if protenix_repo else None,
            protenix_venv=c.get("protenix_venv", ".venv"),
            # 8, matching config.yaml design.cluster.n_gpus and
            # campaign_calibration.choose_compute's own default. A 4 here
            # silently sized cluster campaigns for half the GPUs whenever
            # design.cluster was absent from a config.
            n_gpus=max(1, int(c.get("n_gpus", 8))),
            pilot=c.get("pilot") or {"n_batches": 25},
            calibration=c.get("calibration") or {"n_batches": 145},
            production=c.get("production") or {"n_batches": 3000},
            diffusion_batch_size=int(c.get("diffusion_batch_size", 4)),
            n_seq=int(c.get("n_seq", 4)),
            poll_hint_s=int(c.get("poll_hint_s", 1800)),
        )


@dataclass(frozen=True)
class ClusterPaths:
    run_name: str
    run_dir: Path             # <pipeline_root>/<stage_subdir>/<run_name>/
    spec_path: Path
    structure_path: Path
    msa_path: "Path | None"
    launch_script: Path

    @property
    def refold_dir(self) -> Path:
        """
        Where a completed run's refolds land, per bin/run_pipeline.sh's own
        <pipeline_root>/outputs/<run_name>/run_*/<backend>/ layout — one
        run_* per array task (NARRAY), each independently scored.
        """
        return self.run_dir.parents[1] / "outputs" / self.run_name


@dataclass
class ClusterPlan:
    mode: str
    n_gpus: int
    n_batches: int
    diffusion_batch_size: int
    n_seq: int
    shards: int
    expected_rfd3: int
    expected_rf3: int
    prefilter_rate: float = 0.59

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ----------------------------------------------------------------------
# Planning — mirrors foundry_runner.plan_campaign's shape, sized by GPU
# count instead of a fixed local-GPU assumption.
# ----------------------------------------------------------------------

def plan_campaign(cluster_cfg: ClusterConfig, *, mode: str,
                  n_batches: int | None = None,
                  prefilter_rate: float = 0.59) -> ClusterPlan:
    stage_cfg = getattr(cluster_cfg, mode)
    nb = int(n_batches if n_batches is not None else stage_cfg.get("n_batches", 100))
    dbs = cluster_cfg.diffusion_batch_size
    n_seq = cluster_cfg.n_seq
    # NB is PER GPU ARRAY TASK, not a campaign total to divide across GPUs —
    # confirmed against the cluster pipeline's own diagnostic line
    # (`NARRAY x NB x DBS = ... designs`, bin/run_pipeline.sh). Missing this
    # factor undercounted a real campaign's own expected/actual totals by
    # exactly n_gpus (reported ~8.6k refolds for a campaign that actually
    # ran, and completed, at ~59k).
    expected_rfd3 = nb * dbs * cluster_cfg.n_gpus
    expected_rf3 = int(expected_rfd3 * prefilter_rate) * n_seq
    # NARRAY = n_gpus keeps stages 01/02 at one array task per GPU; refold
    # SHARDS subdivides the (much larger) refold workload across the same
    # n_gpus so every stage keeps all GPUs busy rather than idling most of
    # them during the cheap diffuse/mpnn stages and bottlenecking on refold.
    shards = max(1, round(expected_rf3 / max(expected_rfd3, 1)))
    plan = ClusterPlan(mode=mode, n_gpus=cluster_cfg.n_gpus, n_batches=nb,
                       diffusion_batch_size=dbs, n_seq=n_seq, shards=shards,
                       expected_rfd3=expected_rfd3, expected_rf3=expected_rf3,
                       prefilter_rate=prefilter_rate)
    logger.info(
        f"cluster {mode} plan: {expected_rfd3:,} designs -> {expected_rf3:,} "
        f"refolds | NARRAY={cluster_cfg.n_gpus} SHARDS={shards}")
    return plan


# ----------------------------------------------------------------------
# MSA
# ----------------------------------------------------------------------

def _seq_hash(seq: str) -> str:
    return hashlib.sha256(seq.encode("utf-8")).hexdigest()[:16]


def fetch_target_msa(seq: str, name: str, cluster_cfg: ClusterConfig) -> Path:
    """
    Real target MSA via a SEPARATE, machine-specific Protenix checkout's own
    hosted-MMseqs2 search (protenix-server.com, free, no local ColabFold DB) —
    subprocessed, never imported, same pattern as every other foreign-venv
    tool in this codebase (PyRosetta, BoltzGen, foundry). Cached forever by
    sequence hash, so repeat trials against the same target are free.

    Returns the path to a standard `.a3m` file — the same format the cluster
    pipeline's own `build_fold_yaml.py --target-msa` accepts for any folding
    backend, Protenix included.
    """
    if cluster_cfg.msa_source != "protenix_hosted":
        raise MsaFetchError(
            f"msa_source={cluster_cfg.msa_source!r} has no automatic fetch — "
            f"use bin/make_msa.sh manually (see the pause instructions).")
    if cluster_cfg.protenix_repo is None:
        raise MsaFetchError(
            "no Protenix checkout configured — set the LPT_CLUSTER_PROTENIX_REPO "
            "env var (see .env.example) or design.cluster.protenix_repo in "
            "config.yaml to a working checkout, or switch msa_source to "
            "cluster_colabfold.")
    venv_py = cluster_cfg.protenix_repo / cluster_cfg.protenix_venv / "bin" / "python3"
    if not venv_py.exists():
        raise MsaFetchError(
            f"no Protenix venv at {venv_py} — set the LPT_CLUSTER_PROTENIX_REPO "
            f"env var (see .env.example) or design.cluster.protenix_repo in "
            f"config.yaml to a working checkout, or switch msa_source to "
            f"cluster_colabfold.")

    script = (
        "import sys; sys.path.insert(0, 'lpt_scripts')\n"
        "from build_target_msa import build_or_reuse_target_msa\n"
        f"p = build_or_reuse_target_msa({seq!r}, name={name!r})\n"
        "print(str(p))\n"
    )
    proc = subprocess.run(
        [str(venv_py), "-c", script], cwd=str(cluster_cfg.protenix_repo),
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise MsaFetchError(
            f"target MSA fetch failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}")
    cache_dir = Path(proc.stdout.strip().splitlines()[-1])
    a3m = cache_dir / "non_pairing.a3m"
    if not a3m.exists():
        raise MsaFetchError(f"MSA fetch reported success but {a3m} is missing")
    n_hits = sum(1 for line in a3m.read_text().splitlines() if line.startswith(">"))
    logger.info(f"target MSA for {name}: {n_hits} hits, cached at {a3m}")
    return a3m



def _require_pipeline_root(cluster_cfg) -> None:
    """Fail with the configured-path message, not `NoneType / str`.

    Reachable on a RESUME: a campaign staged with LPT_CLUSTER_PIPELINE_ROOT set
    is later resumed (`--start-from binder_scoring`) from a shell without it,
    and every path build raised an opaque TypeError instead of the message
    `from_cfg` was written to give.
    """
    if cluster_cfg.pipeline_root is None:
        raise ClusterError(
            "cluster pipeline_root is not set — set the "
            "LPT_CLUSTER_PIPELINE_ROOT env var (see .env.example) or "
            "design.cluster.pipeline_root in config.yaml to this machine's "
            "mount of the shared binder_pipeline checkout.")


def node_path(path: Path, cluster_cfg: ClusterConfig) -> str:
    """
    Submit-host absolute path -> compute-node absolute path.

    Mirrors pipeline_root/lib/paths.sh's own `repo_paths()` rule exactly:
    swap everything up to and including `SHARE_NODE_PREFIX` for
    `SHARE_NODE_PREFIX` itself. Only `TARGET_MSA` needs this — every other
    path this module hands the cluster (`SPEC=...`) is relative to
    pipeline_root and bin/run_pipeline.sh does its own translation for those.
    Read directly from that pipeline's config/site.sh so this stays correct
    if a future user's cluster mounts the share at a different node prefix,
    without this module needing its own copy of that value.
    """
    prefix = "/g-groups"
    _require_pipeline_root(cluster_cfg)
    site_sh = cluster_cfg.pipeline_root / "config" / "site.sh"
    try:
        text = site_sh.read_text(encoding="utf-8")
        m = re.search(r'SHARE_NODE_PREFIX:?=["\']?([^"\'\s}]+)', text)
        if m:
            prefix = m.group(1)
    except OSError:
        pass
    s = str(path)
    idx = s.find(prefix + "/")
    return prefix + s[idx + len(prefix):] if idx != -1 else s


# ----------------------------------------------------------------------
# Staging
# ----------------------------------------------------------------------

def stage_campaign(
    spec_path: Path, trim, dirs: dict, cluster_cfg: ClusterConfig, *,
    mode: str, slug: str, target_chain: str = "B", n_batches: int | None = None,
) -> tuple[ClusterPaths, ClusterPlan]:
    """
    Write everything g-groups/.../binder_pipeline needs onto shared storage
    for one campaign stage, plus a launch script ready for a human to run.

    Does NOT submit anything — this machine cannot reach the scheduler.
    """
    if cluster_cfg.pipeline_root is None or not cluster_cfg.pipeline_root.is_dir():
        raise ClusterError(
            f"cluster pipeline_root does not exist: "
            f"{cluster_cfg.pipeline_root!r} — set the LPT_CLUSTER_PIPELINE_ROOT "
            f"env var (see .env.example) or design.cluster.pipeline_root in "
            f"config.yaml to a binder_pipeline checkout reachable from this "
            f"machine.")

    run_name = f"{slug}_{mode}"
    _require_pipeline_root(cluster_cfg)
    run_dir = cluster_cfg.pipeline_root / cluster_cfg.stage_subdir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    spec_path = Path(spec_path)
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    design_name = next(iter(payload))
    src_structure = Path(payload[design_name]["input"])
    staged_structure = run_dir / src_structure.name
    shutil.copy(src_structure, staged_structure)
    # Rewrite `input` to a bare filename co-located with the spec — this
    # machine's absolute path means nothing on the cluster; matches the
    # convention pipeline_root/examples/CD79/CD79b_binder_001.json already
    # uses (`"input": "CD79_A_B_extr.pdb"`).
    payload[design_name]["input"] = staged_structure.name
    staged_spec = run_dir / spec_path.name
    staged_spec.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    msa_path = None
    if cluster_cfg.use_msa and cluster_cfg.refold_backend != "rf3":
        from src.structure_tools import get_sequence_map
        seq = get_sequence_map(str(staged_structure), target_chain)["sequence"]
        try:
            fetched = fetch_target_msa(seq, name=f"{slug}_{target_chain}", cluster_cfg=cluster_cfg)
            # fetch_target_msa's cache lives on THIS machine's local disk
            # (the protenix checkout), not on shared storage — a cluster
            # compute node cannot see it. Copy it alongside the spec/
            # structure so the launch script's TARGET_MSA path is, like
            # everything else it references, relative to run_dir.
            msa_path = run_dir / "target.a3m"
            shutil.copy(fetched, msa_path)
        except MsaFetchError as exc:
            logger.warning(
                f"automatic MSA fetch failed, falling back to manual "
                f"bin/make_msa.sh instructions: {exc}")
            msa_path = None

    plan = plan_campaign(cluster_cfg, mode=mode, n_batches=n_batches)
    launch_script = _write_launch_script(
        run_dir, run_name, staged_spec, plan, cluster_cfg, msa_path)

    return ClusterPaths(
        run_name=run_name, run_dir=run_dir, spec_path=staged_spec,
        structure_path=staged_structure, msa_path=msa_path,
        launch_script=launch_script,
    ), plan


def _write_launch_script(run_dir: Path, run_name: str, spec_path: Path,
                         plan: ClusterPlan, cluster_cfg: ClusterConfig,
                         msa_path: "Path | None") -> Path:
    """
    A thin env-var wrapper around the existing bin/run_pipeline.sh — every
    cluster-specific default (partition, account, container paths) stays in
    that pipeline's own config/site.sh, untouched. This script only sets the
    per-campaign values LPT computed: which spec, how many GPUs, which
    backend, where the MSA is.
    """
    lines = [
        "#!/bin/bash",
        f"# Generated by LPT (src/cluster_runner.py) for run '{run_name}'.",
        "# Run this FROM THE CLUSTER (see submit_instructions in config.yaml's",
        "# design.cluster block) — this machine cannot submit SLURM jobs.",
        "set -euo pipefail",
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        f'cd "$HERE/../.."   # pipeline_root, siblings of {cluster_cfg.stage_subdir}/',
        "",
        # bin/run_pipeline.sh's own SPEC handling already prepends
        # "$PKG/<stage_subdir>/" to a relative SPEC (its case statement is
        # hardcoded to "examples/", which is why stage_subdir must stay
        # "examples" for this to resolve) — including it here too produced
        # ".../examples/examples/..." and a "spec not found" on first real
        # use. SPEC is run_name-relative-to-stage_subdir only.
        f"export SPEC={run_name}/{spec_path.name}",
        f"export NARRAY={plan.n_gpus}",
        f"export NB={plan.n_batches}",
        f"export DBS={plan.diffusion_batch_size}",
        f"export NSEQ={plan.n_seq}",
        f"export SHARDS={plan.shards}",
        f"export REFOLD={cluster_cfg.refold_backend}",
    ]
    if msa_path is not None:
        lines.append(
            "# TARGET_MSA is passed through verbatim (unlike SPEC, it is NOT"
        )
        lines.append(
            "# resolved relative to pipeline_root), so this is already"
        )
        lines.append(
            "# rewritten to the compute NODE's view (config/site.sh's"
        )
        lines.append("# SHARE_NODE_PREFIX), not this submit-host's own path.")
        lines.append(f"export TARGET_MSA={node_path(msa_path, cluster_cfg)}")
    elif cluster_cfg.use_msa and cluster_cfg.refold_backend != "rf3":
        lines += [
            "# Automatic MSA fetch failed or was not configured — run these",
            "# manually first (see pipeline_root/bin/make_msa.sh), then set",
            "# TARGET_MSA to the resulting .a3m before running this script:",
            f"#   bin/extract_target_seq.sh {cluster_cfg.stage_subdir}/{run_name}/rfd3 "
            f"{cluster_cfg.stage_subdir}/{run_name}/target.fasta",
            f"#   bin/make_msa.sh {cluster_cfg.stage_subdir}/{run_name}/target.fasta",
        ]
    lines += [
        "",
        f"bash bin/run_pipeline.sh {run_name}",
        "",
        f'echo "Track with: squeue -u \\$USER  |  results land in outputs/{run_name}/"',
    ]
    script = run_dir / "launch.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


# ----------------------------------------------------------------------
# Progress / collection
# ----------------------------------------------------------------------

def refold_counts(paths: ClusterPaths, refold_backend: str) -> dict:
    """
    Disk-based progress, same discipline as foundry_runner: os.scandir, never
    ls/glob, since a production run_*/ tree can hold tens of thousands of
    entries.

    Both backends use a per-design SUBDIRECTORY (`<id>/<id>_scores.json` for
    Protenix/folding-repo backends, `<id>/<id>_summary_confidences.json` for
    rf3) — confirmed by direct inspection of a real cluster campaign
    (2026-08-23); an earlier revision scanned for files directly inside the
    backend dir and always counted zero against real output.
    """
    import os

    suffix = "_summary_confidences.json" if refold_backend == "rf3" else "_scores.json"
    out = paths.refold_dir
    n_runs = 0
    n_refolds = 0
    if out.is_dir():
        with os.scandir(out) as it:
            for entry in it:
                if not entry.is_dir() or not entry.name.startswith("run_"):
                    continue
                n_runs += 1
                backend_dir = Path(entry.path) / refold_backend
                if not backend_dir.is_dir():
                    continue
                with os.scandir(backend_dir) as inner:
                    for design in inner:
                        if design.is_dir() and (Path(design.path) / f"{design.name}{suffix}").exists():
                            n_refolds += 1
    return {"n_runs": n_runs, "n_refolds": n_refolds, "out_dir": str(out)}


def is_complete(paths: ClusterPaths, plan: ClusterPlan, refold_backend: str) -> bool:
    return refold_counts(paths, refold_backend)["n_refolds"] >= plan.expected_rf3 > 0


def collect_campaign(paths: ClusterPaths, dirs: dict, hotspots, sidecar_dir: Path,
                     cluster_cfg: ClusterConfig, *, limit: int = 0,
                     workers: int = 1) -> list[dict]:
    """
    Score every Protenix (or RF3) refold the cluster produced, using LPT's own
    scorer — not the cluster pipeline's own narrower `score_designs.py` — so
    ipSAE/epitope_recall/hotspot_engagement and the full ranking composite
    work identically to a local foundry campaign.
    """
    from src.binder_metrics import ScoreConfig, score_campaign, score_campaign_protenix

    out = paths.refold_dir
    if not out.is_dir():
        raise ClusterError(
            f"no cluster output at {out} yet — the SLURM jobs likely haven't "
            f"finished (or haven't been submitted); nothing to collect.")

    rows: list[dict] = []
    scorer = (score_campaign_protenix if cluster_cfg.refold_backend != "rf3"
             else score_campaign)
    import os
    with os.scandir(out) as it:
        run_dirs = sorted(e.path for e in it if e.is_dir() and e.name.startswith("run_"))
    for run_path in run_dirs:
        backend_dir = Path(run_path) / cluster_cfg.refold_backend
        design_dir = Path(run_path) / "mpnn"
        if not backend_dir.is_dir() or not design_dir.is_dir():
            continue
        rows.extend(scorer(
            backend_dir, design_dir, hotspots=hotspots,
            cfg=ScoreConfig(), workers=workers,
            limit=(limit - len(rows)) if limit else 0,
        ))
        if limit and len(rows) >= limit:
            break
    return rows
