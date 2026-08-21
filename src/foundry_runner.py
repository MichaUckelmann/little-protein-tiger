"""
Launch, monitor and resume a foundry binder campaign on the local GPU.

RFD3 -> prefilter -> solubleMPNN -> RF3 refold. Mirrors the public shape of
`src/design_runner.py` (the BoltzGen backend) so the two are interchangeable
behind `design.backend`.

## Subprocess, not the importable engines

foundry's inference engines *are* importable, but they live in a separate uv venv
with its own torch build (`.venv-blackwell`, required because the workstation's
Blackwell card is sm_120 and the container's torch is not built for it). Making
LPT import them would make a literature pipeline depend on the entire foundry
stack. Crash isolation matters too: over a multi-day run an OOM or a driver
hiccup should kill a child, not the orchestrator. Idempotency is already an
on-disk property, so process boundaries cost nothing.

`cwd` must be the foundry root for `uv run` to resolve `.venv-blackwell`.

## Progress comes from disk, never from the log

Every count uses `os.scandir`. A production stage directory holds 50-100k
entries, where a shell glob is `Argument list too long` and `ls | wc -l` inside a
pipeline silently reports 0 — which reads as "this stage produced nothing" and
aborts a run that had in fact completed. This is the single most important
operational lesson from the reference campaign and it is not negotiable.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from loguru import logger

from src.foundry_spec import (
    RFD3_BINDER_CHAIN, RFD3_TARGET_CHAIN, build_mpnn_configs, parse_contig,
    strip_design_suffixes, validate_spec,
)
from src.job_registry import JobRegistry, JobRecord, STATUS_RUNNING

# Measured on the RTX PRO 4500 Blackwell (32 GB) for a ~175-token complex.
SEC_PER_RFD3_DESIGN = 5.4
SEC_PER_MPNN_SEQ = 0.36
SEC_PER_RF3_REFOLD = 8.4
BYTES_PER_RF3_DIR = 2.5e6


class FoundryError(RuntimeError):
    """A campaign could not be prepared or run."""


class FoundryValidationError(FoundryError):
    """The inputs are wrong; running would waste GPU days."""


@dataclass(frozen=True)
class FoundryPaths:
    campaign_dir: Path
    rfd3_dir: Path
    filtered_dir: Path
    mpnn_dir: Path
    mpnn_config_dir: Path
    rf3_dir: Path
    logs_dir: Path
    driver_path: Path
    registry_path: Path

    @classmethod
    def under(cls, campaign_dir: Path) -> "FoundryPaths":
        d = Path(campaign_dir)
        return cls(
            campaign_dir=d,
            rfd3_dir=d / "rfd3",
            filtered_dir=d / "designs_filtered",
            mpnn_dir=d / "mpnn_out",
            mpnn_config_dir=d / "mpnn_out" / "configs",
            rf3_dir=d / "rf3_out",
            logs_dir=d / "logs",
            driver_path=d / "run_campaign.sh",
            registry_path=d / "jobs.json",
        )

    def mkdirs(self) -> None:
        for p in (self.campaign_dir, self.rfd3_dir, self.filtered_dir,
                  self.mpnn_dir, self.mpnn_config_dir, self.rf3_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class CampaignPlan:
    mode: str
    n_batches: int
    diffusion_batch_size: int
    n_seq: int
    expected_rfd3: int
    prefilter_rate: float
    expected_mpnn: int
    expected_rf3: int
    est_gpu_hours: float
    est_disk_gb: float
    free_disk_gb: float
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CampaignProgress:
    stage: str
    n_rfd3: int
    n_filtered: int
    n_mpnn: int
    n_rf3: int
    expected_rfd3: int
    expected_rf3: int
    free_gb: float
    job: JobRecord | None = None
    eta_s: float | None = None

    @property
    def complete(self) -> bool:
        return self.n_rf3 >= self.expected_rf3 > 0


# ----------------------------------------------------------------------
# Disk counters
# ----------------------------------------------------------------------

def _scan(d: Path, predicate: Callable[[os.DirEntry], bool]) -> int:
    if not Path(d).is_dir():
        return 0
    n = 0
    with os.scandir(d) as it:
        for entry in it:
            try:
                if predicate(entry):
                    n += 1
            except OSError:
                continue
    return n


def count_rfd3(d: Path) -> int:
    return _scan(d, lambda e: e.is_file() and e.name.endswith(".cif.gz"))


def count_filtered(d: Path) -> int:
    return _scan(d, lambda e: e.name.endswith(".cif.gz"))


def count_mpnn(d: Path) -> int:
    """Threaded structures, i.e. sequences — `<design>_b<b>_d<d>.cif`."""
    return _scan(d, lambda e: e.is_file() and "_b" in e.name
                 and e.name.endswith(".cif"))


def count_rf3(d: Path) -> int:
    """
    One completed refold per subdirectory holding `_summary_confidences.json`.

    RF3's own `skip_existing` keys on `_metrics.csv`, which it writes only when
    early stopping triggers — so trusting it silently re-folds finished designs.
    """
    if not Path(d).is_dir():
        return 0
    n = 0
    with os.scandir(d) as it:
        for entry in it:
            if not entry.is_dir():
                continue
            if (Path(entry.path) / f"{entry.name}_summary_confidences.json").exists():
                n += 1
    return n


def free_gb(path: Path) -> float:
    try:
        usage = shutil.disk_usage(str(Path(path).resolve()))
    except OSError:
        return float("inf")
    return usage.free / 1e9


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------

def plan_campaign(
    cfg: dict,
    paths: FoundryPaths,
    *,
    mode: str = "production",
    n_batches: int | None = None,
    prefilter_rate: float = 0.59,
) -> CampaignPlan:
    """
    Size a campaign and check it against the disk budget.

    The disk clamp is not theoretical: at the reference production settings
    (3000 batches x 4 designs x 4 sequences = 48,000 refolds) RF3 output is
    ~120 GB, about half the free space on this workstation.
    """
    f = cfg.get("foundry") or {}
    stage_cfg = f.get(mode) or {}
    n_batches = int(n_batches if n_batches is not None
                    else stage_cfg.get("n_batches", 100))
    dbs = int((f.get("rfd3") or {}).get("diffusion_batch_size", 4))
    n_seq = int((f.get("mpnn") or {}).get("n_seq", 4))

    expected_rfd3 = n_batches * dbs
    expected_mpnn = int(expected_rfd3 * prefilter_rate) * n_seq
    expected_rf3 = expected_mpnn

    seconds = (expected_rfd3 * SEC_PER_RFD3_DESIGN
               + expected_mpnn * SEC_PER_MPNN_SEQ
               + expected_rf3 * SEC_PER_RF3_REFOLD)
    disk = expected_rf3 * BYTES_PER_RF3_DIR / 1e9
    have = free_gb(paths.campaign_dir)
    budget_gb = float(f.get("disk_budget_gb", 120))
    min_free = float(f.get("min_free_gb", 20))

    warnings: list[str] = []
    ceiling = min(budget_gb, max(have - min_free, 0.0))
    if disk > ceiling and expected_rf3 > 0:
        scale = ceiling / disk
        capped = max(1, int(n_batches * scale))
        warnings.append(
            f"{expected_rf3:,} refolds would need {disk:,.0f} GB but only "
            f"{ceiling:,.0f} GB is available under the budget "
            f"({budget_gb:.0f} GB cap, {have:.0f} GB free, {min_free:.0f} GB "
            f"reserve) — n_batches clamped {n_batches:,} -> {capped:,}")
        n_batches = capped
        expected_rfd3 = n_batches * dbs
        expected_mpnn = int(expected_rfd3 * prefilter_rate) * n_seq
        expected_rf3 = expected_mpnn
        seconds = (expected_rfd3 * SEC_PER_RFD3_DESIGN
                   + expected_mpnn * SEC_PER_MPNN_SEQ
                   + expected_rf3 * SEC_PER_RF3_REFOLD)
        disk = expected_rf3 * BYTES_PER_RF3_DIR / 1e9

    plan = CampaignPlan(
        mode=mode, n_batches=n_batches, diffusion_batch_size=dbs, n_seq=n_seq,
        expected_rfd3=expected_rfd3, prefilter_rate=prefilter_rate,
        expected_mpnn=expected_mpnn, expected_rf3=expected_rf3,
        est_gpu_hours=round(seconds / 3600.0, 1), est_disk_gb=round(disk, 1),
        free_disk_gb=round(have, 1), warnings=warnings)
    logger.info(
        f"{mode} plan: {expected_rfd3:,} designs -> ~{expected_mpnn:,} sequences "
        f"-> {expected_rf3:,} refolds | ~{plan.est_gpu_hours:,.0f} GPU-h, "
        f"~{plan.est_disk_gb:,.0f} GB (free {have:,.0f} GB)")
    for w in warnings:
        logger.warning(w)
    return plan


# ----------------------------------------------------------------------
# Prefilter
# ----------------------------------------------------------------------

def prefilter_designs(
    rfd3_dir: Path,
    out_dir: Path,
    *,
    max_chainbreaks: int,
    max_sidechain_clashes: int = 0,
    max_backbone_clashes: int = 0,
    min_non_loop: float = 0.6,
    report_path: Path | None = None,
) -> tuple[int, int]:
    """
    Symlink the designs worth refolding into `out_dir`. Returns (kept, total).

    RF3 refolding is the entire cost of a campaign (~8.4 s per sequence, `n_seq`
    per design), so every design dropped here saves ~34 s of GPU. On the
    reference campaign this kept 59% of designs.

    Symlinks, not copies — an RFD3 output tree is tens of GB. Stale links are
    cleared first so re-running at different thresholds cannot leave survivors of
    the previous ones behind.

    `max_chainbreaks` must be DERIVED from the number of target segments, not
    hard-coded: a single-segment target scores exactly 1, and a two-segment trim
    scores 2. Passing the constant 1 against a multi-segment target rejects every
    design.
    """
    rfd3_dir, out_dir = Path(rfd3_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for entry in out_dir.iterdir():
        if entry.is_symlink():
            entry.unlink()

    rows: list[dict] = []
    kept = total = 0
    with os.scandir(rfd3_dir) as it:
        entries = [e for e in it if e.is_file() and e.name.endswith(".cif.gz")]
    for e in sorted(entries, key=lambda x: x.name):
        total += 1
        name = e.name[: -len(".cif.gz")]
        sidecar = rfd3_dir / f"{name}.json"
        reason = ""
        m: dict = {}
        if sidecar.exists():
            try:
                m = (json.loads(sidecar.read_text(encoding="utf-8"))
                     .get("metrics") or {})
            except (OSError, json.JSONDecodeError):
                reason = "unreadable sidecar"
        else:
            reason = "missing sidecar"
        if not reason:
            # RFD3 writes the clash counts as FLAT, dotted keys —
            # "n_clashing.interresidue_clashes_w_sidechain" is one string, not a
            # nested object. Reading it as nested returns nothing and silently
            # disables both clash gates.
            sc = m.get("n_clashing.interresidue_clashes_w_sidechain", 0) or 0
            bb = m.get("n_clashing.interresidue_clashes_w_backbone", 0) or 0
            if m.get("n_chainbreaks", 0) > max_chainbreaks:
                reason = f"chainbreaks={m.get('n_chainbreaks')}"
            elif sc > max_sidechain_clashes:
                reason = f"sc_clashes={sc}"
            elif bb > max_backbone_clashes:
                reason = f"bb_clashes={bb}"
            elif m.get("non_loop_fraction", 1.0) < min_non_loop:
                reason = f"non_loop={m.get('non_loop_fraction'):.2f}"
        rows.append({"name": name, "kept": not reason, "reason": reason})
        if reason:
            continue
        link = out_dir / e.name
        if not link.exists():
            link.symlink_to(Path(e.path).resolve())
        kept += 1

    if report_path:
        import csv

        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(report_path).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "kept", "reason"])
            w.writeheader()
            w.writerows(rows)

    rate = kept / total if total else 0.0
    logger.info(f"prefilter kept {kept:,}/{total:,} designs ({rate:.1%})")
    if total and kept == 0:
        logger.warning(
            "prefilter kept nothing — check max_chainbreaks against the number "
            "of target segments before concluding the designs are bad")
    return kept, total


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------

_DRIVER_TEMPLATE = '''#!/usr/bin/env bash
# Generated by src/foundry_runner.py — do not edit; regenerate instead.
#
# A direct port of the reference campaign driver. The retry loops, find-based
# counters and disk abort are load-bearing: they survived a real 3.7-day run.
# The science lives in Python (src/foundry_stages.py); only orchestration is here.
set -uo pipefail

FOUNDRY={foundry}
CAMPAIGN={campaign}
LPT={lpt}
PY={py}

RFD3_DIR="$CAMPAIGN/rfd3"
FILTERED="$CAMPAIGN/designs_filtered"
MPNN_OUT="$CAMPAIGN/mpnn_out"
RF3_OUT="$CAMPAIGN/rf3_out"
LOGS="$CAMPAIGN/logs"

EXP_RFD3={expected_rfd3}
MAX_RFD3_ATTEMPTS={max_rfd3_attempts}
MAX_RF3_ATTEMPTS={max_rf3_attempts}
MIN_FREE_GB={min_free_gb}

log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }}
# `find`, never `ls`: these directories hold 50-100k entries, where `ls | wc -l`
# in a pipeline silently reports 0 and reads as "the stage produced nothing".
count_rfd3() {{ find "$RFD3_DIR" -maxdepth 1 -name '*.cif.gz' 2>/dev/null | wc -l; }}
count_mpnn() {{ find "$MPNN_OUT" -maxdepth 1 -name '*_b*_d*.cif' 2>/dev/null | wc -l; }}
count_rf3()  {{ find "$RF3_OUT" -mindepth 2 -maxdepth 2 -name '*_summary_confidences.json' 2>/dev/null | wc -l; }}
free_gb()    {{ df -BG --output=avail "$CAMPAIGN" | tail -1 | tr -dc '0-9'; }}

log "campaign start: {expected_rfd3} designs, n_seq={n_seq}, mode={mode}"

# ---- Stage 1: RFD3 ---------------------------------------------------------
for attempt in $(seq 1 $MAX_RFD3_ATTEMPTS); do
  n=$(count_rfd3)
  if [ "$n" -ge "$EXP_RFD3" ]; then log "RFD3 complete: $n/$EXP_RFD3"; break; fi
  log "RFD3 attempt $attempt ($n/$EXP_RFD3 so far)"
  ( cd "$FOUNDRY" && {rfd3_cmd} ) >> "$LOGS/rfd3.log" 2>&1
  log "RFD3 attempt $attempt exited $?"
  sleep 30
done
[ "$(count_rfd3)" -eq 0 ] && {{ log "ABORT: RFD3 produced nothing"; exit 1; }}

# ---- Stage 1.5: prefilter --------------------------------------------------
log "prefiltering"
$PY "$LPT/src/foundry_stages.py" prefilter "$RFD3_DIR" "$FILTERED" \\
    --max-chainbreaks {max_chainbreaks} --min-non-loop {min_non_loop} \\
    --report "$CAMPAIGN/filter_report.csv" >> "$LOGS/filter.log" 2>&1
log "prefilter kept $(find "$FILTERED" -maxdepth 1 -name '*.cif.gz' | wc -l)"

# ---- Stage 2: solubleMPNN --------------------------------------------------
log "MPNN"
$PY "$LPT/src/foundry_stages.py" mpnn "$FILTERED" "$MPNN_OUT" \\
    --checkpoint {mpnn_ckpt} --n-seq {n_seq} --chunk-size {mpnn_chunk} \\
    --foundry "$FOUNDRY" --mpnn-bin {mpnn_bin} --skip-existing \\
    >> "$LOGS/mpnn.log" 2>&1
EXPECTED_RF3=$(count_mpnn)
log "MPNN produced $EXPECTED_RF3 sequences"
[ "$EXPECTED_RF3" -eq 0 ] && {{ log "ABORT: MPNN produced nothing"; exit 1; }}

# ---- Stage 3: RF3 refold ---------------------------------------------------
for attempt in $(seq 1 $MAX_RF3_ATTEMPTS); do
  done_n=$(count_rf3)
  if [ "$done_n" -ge "$EXPECTED_RF3" ]; then log "RF3 complete: $done_n/$EXPECTED_RF3"; break; fi
  if [ "$(free_gb)" -lt "$MIN_FREE_GB" ]; then
    log "ABORT: under ${{MIN_FREE_GB}} GiB free — stopping before the disk fills"; exit 1
  fi
  log "RF3 attempt $attempt ($done_n/$EXPECTED_RF3 so far)"
  $PY "$LPT/src/foundry_stages.py" rf3 "$MPNN_OUT" "$RF3_OUT" \\
      --checkpoint {rf3_ckpt} --template {rf3_template} \\
      --diffusion-batch-size {rf3_dbs} --seed {rf3_seed} \\
      --foundry "$FOUNDRY" --rf3-bin {rf3_bin} --skip-existing \\
      >> "$LOGS/rf3.log" 2>&1
  log "RF3 attempt $attempt exited $?"
  sleep 30
done

log "campaign finished: rfd3=$(count_rfd3) mpnn=$(count_mpnn) rf3=$(count_rf3)"
'''


def _rfd3_command(cfg: dict, spec_path: Path, paths: FoundryPaths,
                  plan: CampaignPlan) -> str:
    f = cfg.get("foundry") or {}
    rfd3 = f.get("rfd3") or {}
    sampler = rfd3.get("inference_sampler") or {}
    bin_ = f.get("rfd3_bin", ".venv-blackwell/bin/rfd3")
    launcher = " ".join(f.get("launcher") or ["uv", "run"])
    parts = [
        f"{launcher} {bin_}",
        f"out_dir={paths.rfd3_dir}",
        f"inputs={spec_path}",
        f"n_batches={plan.n_batches}",
        f"diffusion_batch_size={plan.diffusion_batch_size}",
        f"ckpt_path={f.get('ckpt', {}).get('rfd3', 'rfd3')}",
    ]
    # IPD's documented PPI-designability settings. The reference campaign did not
    # use them; its hit rate was 44/28,420.
    for key in ("step_scale", "gamma_0"):
        if key in sampler:
            parts.append(f"inference_sampler.{key}={sampler[key]}")
    return " \\\n      ".join(parts)


def write_campaign_driver(
    cfg: dict,
    spec_path: Path,
    paths: FoundryPaths,
    plan: CampaignPlan,
    *,
    n_target_segments: int = 1,
) -> Path:
    """Render `run_campaign.sh`. One PID to track; every stage idempotent."""
    import sys

    f = cfg.get("foundry") or {}
    prefilter = f.get("prefilter") or {}
    mpnn = f.get("mpnn") or {}
    rf3 = f.get("rf3") or {}
    max_cb = prefilter.get("max_chainbreaks")
    if max_cb is None:
        max_cb = n_target_segments

    text = _DRIVER_TEMPLATE.format(
        foundry=f.get("root", "/home/m.uckelmann_cbs-niob.local/code/foundry"),
        campaign=paths.campaign_dir,
        lpt=Path(__file__).resolve().parents[1],
        py=sys.executable,
        expected_rfd3=plan.expected_rfd3,
        n_seq=plan.n_seq,
        mode=plan.mode,
        max_rfd3_attempts=int(f.get("max_rfd3_attempts", 10)),
        max_rf3_attempts=int(f.get("max_rf3_attempts", 20)),
        min_free_gb=int(f.get("min_free_gb", 20)),
        rfd3_cmd=_rfd3_command(cfg, spec_path, paths, plan),
        max_chainbreaks=int(max_cb),
        min_non_loop=float(prefilter.get("min_non_loop", 0.6)),
        mpnn_ckpt=(f.get("ckpt") or {}).get("mpnn", "solublempnn"),
        mpnn_bin=f.get("mpnn_bin", ".venv-blackwell/bin/mpnn"),
        mpnn_chunk=int(mpnn.get("chunk_size", 250)),
        rf3_ckpt=(f.get("ckpt") or {}).get("rf3", "rf3"),
        rf3_bin=f.get("rf3_bin", ".venv-blackwell/bin/rf3"),
        rf3_template=rf3.get("template", "target"),
        rf3_dbs=int(rf3.get("diffusion_batch_size", 1)),
        rf3_seed=int(rf3.get("seed", 0)),
    )
    paths.driver_path.parent.mkdir(parents=True, exist_ok=True)
    paths.driver_path.write_text(text, encoding="utf-8")
    paths.driver_path.chmod(0o755)
    logger.info(f"campaign driver -> {paths.driver_path}")
    return paths.driver_path


# ----------------------------------------------------------------------
# Run / monitor / resume
# ----------------------------------------------------------------------

JOB_ID = "campaign"


def progress(paths: FoundryPaths, plan: CampaignPlan,
             registry: JobRegistry | None = None) -> CampaignProgress:
    """Where the campaign is, read from disk."""
    n_rfd3 = count_rfd3(paths.rfd3_dir)
    n_filt = count_filtered(paths.filtered_dir)
    n_mpnn = count_mpnn(paths.mpnn_dir)
    n_rf3 = count_rf3(paths.rf3_dir)
    expected_rf3 = n_mpnn or plan.expected_rf3

    if n_rf3 >= expected_rf3 > 0:
        stage = "done"
    elif n_mpnn:
        stage = "rf3"
    elif n_filt:
        stage = "mpnn"
    elif n_rfd3 >= plan.expected_rfd3 > 0:
        stage = "filter"
    else:
        stage = "rfd3"

    remaining = max(expected_rf3 - n_rf3, 0)
    eta = remaining * SEC_PER_RF3_REFOLD if stage == "rf3" else None
    if stage == "rfd3":
        eta = max(plan.expected_rfd3 - n_rfd3, 0) * SEC_PER_RFD3_DESIGN

    job = None
    if registry is not None:
        try:
            job = registry.refresh(JOB_ID)
        except KeyError:
            job = None
    return CampaignProgress(
        stage=stage, n_rfd3=n_rfd3, n_filtered=n_filt, n_mpnn=n_mpnn, n_rf3=n_rf3,
        expected_rfd3=plan.expected_rfd3, expected_rf3=expected_rf3,
        free_gb=free_gb(paths.campaign_dir), job=job, eta_s=eta)


def render_progress(p: CampaignProgress) -> str:
    def bar(n: int, total: int) -> str:
        pct = (100.0 * n / total) if total else 0.0
        return f"{n:>7,}/{total:<7,} ({pct:5.1f}%)"

    lines = [
        f"stage      {p.stage}",
        f"RFD3       {bar(p.n_rfd3, p.expected_rfd3)}",
        f"prefilter  {p.n_filtered:>7,}",
        f"MPNN       {p.n_mpnn:>7,}",
        f"RF3        {bar(p.n_rf3, p.expected_rf3)}",
        f"free disk  {p.free_gb:,.0f} GB",
    ]
    if p.eta_s:
        lines.append(f"ETA        ~{p.eta_s / 3600:.1f} h at the measured rate")
    if p.job is not None:
        lines.append(f"job        pid {p.job.pid} · {p.job.status} · "
                     f"{p.job.elapsed_s / 3600:.2f} h elapsed")
    return "\n".join(lines)


def run_design(
    spec_path: Path,
    paths: FoundryPaths,
    *,
    cfg: dict,
    plan: CampaignPlan,
    n_target_segments: int = 1,
    kept_segments: Sequence[tuple[int, int]] | None = None,
) -> JobRecord:
    """
    Validate, write the driver, and launch the campaign detached.

    Returns immediately with the job record; the campaign keeps running after
    this process exits. Re-calling attaches to a live job rather than starting a
    second one on the same GPU.
    """
    paths.mkdirs()
    max_target = ((cfg.get("foundry") or {}).get("target_residue_budget"))
    try:
        validate_spec(spec_path, kept_segments=kept_segments,
                      max_target_residues=max_target)
    except Exception as exc:
        raise FoundryValidationError(str(exc)) from exc

    write_campaign_driver(cfg, spec_path, paths, plan,
                          n_target_segments=n_target_segments)
    (paths.campaign_dir / "plan.json").write_text(
        json.dumps(plan.as_dict(), indent=2), encoding="utf-8")

    registry = JobRegistry(paths.registry_path)
    return registry.launch(
        JOB_ID, ["bash", str(paths.driver_path)],
        cwd=str(paths.campaign_dir),
        log_path=paths.logs_dir / "driver.log",
        note=f"{plan.mode}: {plan.expected_rfd3} designs",
    )


def wait_for_campaign(
    paths: FoundryPaths,
    plan: CampaignPlan,
    *,
    poll_s: float = 120.0,
    timeout_s: float | None = None,
    on_tick: Callable[[CampaignProgress], None] | None = None,
) -> CampaignProgress:
    """
    Block until the campaign's OUTPUT is complete, or its driver stops.

    The completion test is a disk count, not the process exiting: the driver
    restarts RFD3 and RF3 several times through its own retry loops, so "the
    process is gone" and "the work is done" are independent facts.
    """
    registry = JobRegistry(paths.registry_path)

    def done() -> bool:
        return progress(paths, plan).complete

    def tick(_rec: JobRecord) -> None:
        p = progress(paths, plan, registry)
        logger.info(
            f"[campaign] {p.stage}: rfd3 {p.n_rfd3:,}/{p.expected_rfd3:,} · "
            f"mpnn {p.n_mpnn:,} · rf3 {p.n_rf3:,}/{p.expected_rf3:,} · "
            f"{p.free_gb:,.0f} GB free")
        if on_tick:
            on_tick(p)

    try:
        registry.poll_until(JOB_ID, until=done, tick_s=poll_s, on_tick=tick,
                            timeout_s=timeout_s)
    except KeyError:
        logger.warning(f"no campaign job recorded in {paths.registry_path}")
    return progress(paths, plan, registry)


def resume(paths: FoundryPaths, cfg: dict, plan: CampaignPlan) -> JobRecord:
    """
    Re-attach to a running campaign, or relaunch it where it stopped.

    Every stage is skip-existing, so relaunching costs only the work that was
    genuinely unfinished.
    """
    registry = JobRegistry(paths.registry_path)
    rec = registry.get(JOB_ID)
    if rec is not None:
        rec = registry.refresh(JOB_ID)
        if rec.status == STATUS_RUNNING:
            logger.info(f"campaign already running (pid {rec.pid}); attaching")
            return rec
        logger.info(f"previous campaign ended {rec.status}; relaunching from disk")
    if not paths.driver_path.exists():
        raise FoundryError(
            f"no driver at {paths.driver_path} — run the design stage first")
    return registry.launch(
        JOB_ID, ["bash", str(paths.driver_path)], cwd=str(paths.campaign_dir),
        log_path=paths.logs_dir / "driver.log", note="resume")


def stop(paths: FoundryPaths) -> bool:
    return JobRegistry(paths.registry_path).kill(JOB_ID)


def prefilter_rate_observed(paths: FoundryPaths) -> float:
    """Measured prefilter survival, for sizing the next stage honestly."""
    total = count_rfd3(paths.rfd3_dir)
    kept = count_filtered(paths.filtered_dir)
    return (kept / total) if total else 0.0


def collect(paths: FoundryPaths) -> dict:
    """A summary of what is on disk, for the stage report."""
    return {
        "campaign_dir": str(paths.campaign_dir),
        "n_rfd3": count_rfd3(paths.rfd3_dir),
        "n_filtered": count_filtered(paths.filtered_dir),
        "n_mpnn": count_mpnn(paths.mpnn_dir),
        "n_rf3": count_rf3(paths.rf3_dir),
        "prefilter_rate": round(prefilter_rate_observed(paths), 4),
        "free_gb": round(free_gb(paths.campaign_dir), 1),
    }


def find_design_sidecar(paths: FoundryPaths) -> Path | None:
    """
    Any RFD3 design sidecar — scoring needs one for its `diffused_index_map`.

    Must NOT be the input spec: only a sidecar carries the input->output
    renumbering, and passing the spec silently scores the wrong residues.
    """
    with os.scandir(paths.rfd3_dir) as it:
        for entry in it:
            if entry.is_file() and entry.name.endswith(".json"):
                return Path(entry.path)
    return None
