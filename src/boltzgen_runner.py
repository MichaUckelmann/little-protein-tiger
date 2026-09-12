"""Run a BoltzGen campaign detached, size it, and read its progress off disk.

The BoltzGen counterpart of :mod:`src.foundry_runner`, and deliberately much
smaller: BoltzGen is ONE command that runs its own five- or six-step pipeline
internally, so there is no bash driver to render and no three-stage funnel to
invert. What it needs from LPT is the three things
:mod:`src.design_runner` does not have.

**1. Detachment and a resumable record.** `design_runner.run_design` is a
blocking `subprocess.run` with a 24 h timeout and no state: no pid recorded, no
plan, no checkpoint. If the process dies the orchestrator learns nothing, and a
second invocation would cheerfully start a second BoltzGen on the same GPU.
:mod:`src.job_registry` already solves this generically — it never mentions
RF3 — so this module simply uses it, which is why detachment and resume are
things the BoltzGen path *gains* here rather than costs.

**2. Progress that is not a lie.** Refold count alone reads 0 for the first
stretch of every campaign, because `folding` is step 3 of 5 (peptide) or 6
(protein) and `design` + `inverse_folding` must work through every design
first. On a 1000-design run that is hours of apparent standstill — the same
"this stage produced nothing" misreading `foundry_runner`'s counters were
written to avoid. `progress()` reports per-stage counts and the step BoltzGen
itself last announced.

**3. A cost model.** Fitted below, and honest about its scatter.

## The two laws

Both are anchored on the largest sample available — a 994-design cyclic-peptide
campaign against RAMP1 at 99 tokens — and fitted against two archived
100-design runs at 229 and 817 tokens. Only AT-SCALE slices are used: a
24-design probe of that same campaign measured 16.0 s/design against its
at-scale 7.8, because fixed model loading dominates a small run. That is the
same reason `foundry_runner.sec_per_refold_observed` refuses to report a rate
from fewer than 50 units.

**Time — `(tokens/99)**1.32`, an anchor and not a promise.** Measured 7.82
s/design at 98 tokens, 17.0 at 229, 122.3 at 817. Least squares over the three
gives 1.317, which reproduces the anchor exactly and the large end to +5% but
over-costs the 229-token point by 41%. The scatter is real — three points, two
of them 50-design increments on different targets with different binder sizes
— so `sec_per_design_observed` is the primary path and this law is the
fallback, exactly as on the foundry side. Over-costing is the safe direction for
a budget check and the wrong direction for discouraging an operator, which is
why `plan_campaign` says which of the two it used.

**Disk — `(tokens/99)**0.97`, i.e. essentially LINEAR, and that is
explained.** Measured 0.348 MB/design at 99 tokens, 0.791 at 229, 2.657 at 817;
the implied exponent is 0.978 and 0.963 against the anchor — far tighter than
the time fit. The reason is mechanical: BoltzGen persists coordinates (O(N)) and
SCALAR confidence reductions, and **no PAE matrix at all** (its
`config/folding.yaml` `keys_dict_out` is a 20-name whitelist of scalars; there
is no `.npy` anywhere in a finished run). RF3's `refold_bytes` exponent is 1.49
precisely because half its bytes are the O(N^2) PAE matrix. So the difference
between the two laws is the absence of that matrix, not a fitting artefact —
and it is also why `ipsae_min` cannot be recomputed from a BoltzGen run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from loguru import logger

# ── the laws ────────────────────────────────────────────────────────────────

#: Complex size the two laws are anchored at (target + binder residues).
REF_TOKENS = 99
#: Seconds per design at REF_TOKENS, measured over 970 designs.
SEC_PER_DESIGN_REF = 7.82
#: See the module docstring: least-squares over three at-scale points, with
#: real scatter. Do not "correct" it without re-measuring.
TIME_EXPONENT = 1.32
#: Bytes per design at REF_TOKENS, measured over the same campaign.
BYTES_PER_DESIGN_REF = 0.348e6
#: ~1.0 because BoltzGen writes no PAE matrix — see the module docstring.
DISK_EXPONENT = 0.97
#: `protein-anything` costs MORE per design than `peptide-anything` at the same
#: complex size, because it runs SIX pipeline steps to peptide's five — the
#: extra one being `design_folding`, a refold of the binder ALONE (confirmed
#: live: the cyclic run logs "[Step 3/5]", the mini run "[Step 3/6]").
#:
#: The laws above are fitted on peptide-protocol data only — all three points
#: had 12-15mer binders — so a protein-protocol campaign needs this factor.
#:
#: 1.175 is the ratio at the ONE at-scale protein-protocol point there is: the
#: RAMP1 mini campaign, 970 designs at a 154-167-token complex, END-TO-END
#: 17.32 s/design from its own `campaign_timing.jsonl`, against the peptide
#: law's 14.74 at the same size. The law reproduces that point to within 0.5%
#: with this factor.
#:
#: It replaces a 1.8 derived from that campaign's 24-design PROBE, which was
#: startup-dominated in exactly the way this module documents elsewhere — the
#: probe read 26.52 s/design against the same campaign's at-scale 17.32, a
#: 1.53x inflation, and the cyclic probe read 16.04 against 7.82 (2.05x). The
#: old note called 1.8 a "lower bound"; it was an over-estimate, and the
#: direction mattered less than it looks only because over-costing is the safe
#: way to be wrong (a clamped `n_batches` and a pessimistic verdict, not an
#: over-run). One at-scale point is still one point: treat this as calibrated
#: rather than fitted, and prefer `sec_per_design_observed`, which already
#: takes precedence everywhere it exists.
PROTEIN_PROTOCOL_FACTOR = 1.175

#: Below this many finished designs an observed rate is startup-dominated and
#: is not reported. Same threshold and reasoning as foundry's.
MIN_DESIGNS_FOR_RATE = 50

#: Subdirectories BoltzGen creates under its --output directory.
_DESIGN_SUBDIR = "intermediate_designs"
_INVFOLD_SUBDIR = "intermediate_designs_inverse_folded"
_REFOLD_SUBDIR = "refold_cif"
_FINAL_SUBDIR = "final_ranked_designs"
_METRICS_CSV = "all_designs_metrics.csv"
#: BoltzGen's own stage banner, e.g. "[Step 3/5] folding".
_STEP_RE = re.compile(r"\[Step \d+/\d+\] [a-z_]+")

#: One job id per campaign directory, like foundry's.
JOB_ID = "boltzgen"


def seconds_per_design(n_tokens: int | None,
                       protocol: str | None = None) -> float:
    """Per-design wall clock at a given complex size and protocol.

    `protocol` matters: see `PROTEIN_PROTOCOL_FACTOR`. Omitting it costs the
    campaign at the peptide rate, which UNDER-costs a mini-protein run by
    roughly a factor of two — so callers that know the protocol should pass it.
    """
    base = SEC_PER_DESIGN_REF
    if n_tokens and n_tokens > 0:
        base = SEC_PER_DESIGN_REF * (n_tokens / REF_TOKENS) ** TIME_EXPONENT
    if protocol and protocol.startswith("protein"):
        base *= PROTEIN_PROTOCOL_FACTOR
    return base


def design_bytes(n_tokens: int | None) -> float:
    """Disk per design at a given complex size."""
    if not n_tokens or n_tokens <= 0:
        return BYTES_PER_DESIGN_REF
    return BYTES_PER_DESIGN_REF * (n_tokens / REF_TOKENS) ** DISK_EXPONENT


# ── layout ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BoltzGenPaths:
    campaign_dir: Path
    design_dir: Path
    invfold_dir: Path
    refold_dir: Path
    final_dir: Path
    metrics_csv: Path
    log_path: Path
    registry_path: Path
    plan_path: Path

    @classmethod
    def under(cls, campaign_dir: Path, mode: str | None = None) -> "BoltzGenPaths":
        d = Path(campaign_dir) / mode if mode else Path(campaign_dir)
        inv = d / _INVFOLD_SUBDIR
        return cls(
            campaign_dir=d,
            design_dir=d / _DESIGN_SUBDIR,
            invfold_dir=inv,
            refold_dir=inv / _REFOLD_SUBDIR,
            final_dir=d / _FINAL_SUBDIR,
            metrics_csv=d / _FINAL_SUBDIR / _METRICS_CSV,
            log_path=d / "boltzgen.log",
            registry_path=d / "jobs.json",
            plan_path=d / "plan.json",
        )

    def mkdirs(self) -> None:
        self.campaign_dir.mkdir(parents=True, exist_ok=True)


# ── counting ────────────────────────────────────────────────────────────────

def _count_cifs(d: Path) -> int:
    """`.cif` files directly in `d`, 0 if absent.

    `os.scandir`, never `ls` or a glob: these directories reach tens of
    thousands of entries, and `ls | wc -l` silently reporting 0 reads as "this
    stage produced nothing", which aborts a completed run.
    """
    if not d.is_dir():
        return 0
    try:
        with os.scandir(d) as it:
            return sum(1 for e in it if e.is_file() and e.name.endswith(".cif"))
    except OSError:
        return 0


def count_designs(paths: BoltzGenPaths) -> int:
    return _count_cifs(paths.design_dir)


def count_inverse_folded(paths: BoltzGenPaths) -> int:
    return _count_cifs(paths.invfold_dir)


def count_refolds(paths: BoltzGenPaths) -> int:
    """Completed designs — the campaign's final per-design structure."""
    return _count_cifs(paths.refold_dir)


def last_step(paths: BoltzGenPaths) -> str:
    """The step BoltzGen last announced, or "" if the log says nothing yet."""
    if not paths.log_path.is_file():
        return ""
    try:
        # tqdm writes \r-separated frames, so normalise both.
        text = paths.log_path.read_text(errors="replace").replace("\r", "\n")
    except OSError:
        return ""
    hits = _STEP_RE.findall(text)
    return hits[-1] if hits else ""


def sec_per_design_observed(paths: BoltzGenPaths) -> float:
    """Measured seconds per design, from refold mtimes; 0.0 if not yet usable.

    Mean spacing rather than total/count, so a campaign that was paused or
    resumed does not report the gap as throughput. Returns 0.0 below
    `MIN_DESIGNS_FOR_RATE`, where fixed startup still dominates — measured, a
    24-design slice of the anchor campaign read 16.0 s/design against its
    at-scale 7.8.
    """
    d = paths.refold_dir
    if not d.is_dir():
        return 0.0
    try:
        with os.scandir(d) as it:
            mtimes = sorted(e.stat().st_mtime for e in it
                            if e.is_file() and e.name.endswith(".cif"))
    except OSError:
        return 0.0
    if len(mtimes) < MIN_DESIGNS_FOR_RATE:
        return 0.0
    span = mtimes[-1] - mtimes[0]
    return span / (len(mtimes) - 1) if span > 0 else 0.0


# ── planning ────────────────────────────────────────────────────────────────

@dataclass
class CampaignPlan:
    num_designs: int
    budget: int
    n_tokens: int | None
    sec_per_design: float
    rate_source: str
    est_gpu_hours: float
    est_disk_gb: float
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "num_designs": self.num_designs, "budget": self.budget,
            "n_tokens": self.n_tokens, "sec_per_design": self.sec_per_design,
            "rate_source": self.rate_source,
            "est_gpu_hours": self.est_gpu_hours,
            "est_disk_gb": self.est_disk_gb, "warnings": list(self.warnings),
        }


def plan_campaign(
    paths: BoltzGenPaths, *, num_designs: int, budget: int,
    n_tokens: int | None = None, protocol: str | None = None,
    sec_per_design: float | None = None,
    disk_budget_gb: float = 120.0, min_free_gb: float = 20.0,
) -> CampaignPlan:
    """Size a campaign, clamping `num_designs` to the disk budget.

    Rate precedence, and it is reported rather than silent: an explicitly
    measured rate, then a rate observed from this campaign's own output, then
    the size law, then the bare anchor WITH A WARNING — because an anchor
    applied to an unknown complex size is the one case where the estimate can
    be wrong by an order of magnitude, and a GPU-hour figure nobody flagged is
    how a 3-hour estimate became an 8.8-hour run on the foundry side.
    """
    warnings: list[str] = []
    observed = sec_per_design_observed(paths)
    if sec_per_design and sec_per_design > 0:
        rate, source = float(sec_per_design), "caller-supplied measurement"
    elif observed > 0:
        rate, source = observed, f"observed in {paths.campaign_dir.name}"
    elif n_tokens:
        rate = seconds_per_design(n_tokens, protocol)
        source = (f"size law at {n_tokens} tokens"
                  + (f", {protocol}" if protocol else ", protocol unknown"))
        if not protocol:
            warnings.append(
                "no protocol given — costed at the peptide rate, which "
                "under-costs a protein-protocol campaign by ~1.8x (it runs an "
                "extra design_folding step).")
    else:
        rate = seconds_per_design(None, protocol)
        source = "bare anchor" + (f", {protocol}" if protocol else "")
        warnings.append(
            f"no measured rate and no complex size — costing every design at "
            f"the {REF_TOKENS}-token anchor ({SEC_PER_DESIGN_REF} s). A larger "
            f"complex can be an order of magnitude slower (122 s/design at 817 "
            f"tokens), so treat the estimate below as a lower bound.")

    per_design_bytes = design_bytes(n_tokens)
    disk_gb = num_designs * per_design_bytes / 1e9
    free_gb = _free_gb(paths.campaign_dir)
    ceiling = min(disk_budget_gb, max(free_gb - min_free_gb, 0.0))
    if disk_gb > ceiling and disk_gb > 0:
        scaled = max(1, int(num_designs * ceiling / disk_gb))
        warnings.append(
            f"disk: {num_designs:,} designs need {disk_gb:.1f} GB but only "
            f"{ceiling:.1f} GB is available (budget {disk_budget_gb:.0f} GB, "
            f"free {free_gb:.1f} GB, reserve {min_free_gb:.0f} GB) — clamped to "
            f"{scaled:,} designs.")
        num_designs = scaled
        disk_gb = num_designs * per_design_bytes / 1e9

    return CampaignPlan(
        num_designs=num_designs, budget=min(budget, num_designs),
        n_tokens=n_tokens, sec_per_design=rate, rate_source=source,
        est_gpu_hours=num_designs * rate / 3600.0, est_disk_gb=disk_gb,
        warnings=warnings,
    )


def _free_gb(path: Path) -> float:
    import shutil

    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return float("inf")


# ── progress ────────────────────────────────────────────────────────────────

@dataclass
class CampaignProgress:
    n_designs: int
    n_inverse_folded: int
    n_refolds: int
    expected: int
    step: str
    metrics_written: bool
    #: Rows in BoltzGen's own metrics table — designs it has fully scored.
    n_metrics: int
    job_status: str | None

    @property
    def complete(self) -> bool:
        """Done means the metrics table exists AND covers this campaign.

        Two halves, and both are needed:

        * Not `n_refolds >= expected` alone: `analysis` and `filtering` run
          AFTER every refold is on disk and are CPU-bound, so a refold-count
          check calls a campaign finished while it still has hours of work.
        * Not "the metrics CSV exists" alone either, which is the trap this
          caught in testing. `--reuse` means a campaign is normally EXTENDED
          from a smaller earlier invocation, and that invocation left a
          complete metrics CSV behind — so a 994-design run sitting at 572
          refolds reported COMPLETE off its own 24-design probe's table, and an
          orchestrator would have moved on mid-campaign.

        So the table must also account for the designs asked for. `n_metrics`
        is its row count, read cheaply.
        """
        return self.metrics_written and self.n_metrics >= self.expected > 0

    def render(self) -> str:
        pct = 100.0 * self.n_refolds / self.expected if self.expected else 0.0
        return (f"{self.step or 'starting'}: {self.n_designs:,} designed, "
                f"{self.n_inverse_folded:,} inverse-folded, "
                f"{self.n_refolds:,}/{self.expected:,} refolded ({pct:.0f}%)"
                + f", {self.n_metrics:,} scored"
                + (f" [job {self.job_status}]" if self.job_status else "")
                + ("  COMPLETE" if self.complete else ""))


def count_metrics_rows(paths: BoltzGenPaths) -> int:
    """Designs in BoltzGen's metrics table, or 0 if it has not been written.

    Counted by reading lines rather than parsing the CSV: the table is ~200-250
    columns wide and this is called on every progress poll.
    """
    if not paths.metrics_csv.is_file():
        return 0
    try:
        with paths.metrics_csv.open("rb") as fh:
            return max(sum(1 for _ in fh) - 1, 0)     # minus the header
    except OSError:
        return 0


def progress(paths: BoltzGenPaths, expected: int) -> CampaignProgress:
    status = None
    if paths.registry_path.is_file():
        try:
            from src.job_registry import JobRegistry

            status = JobRegistry(paths.registry_path).refresh(JOB_ID).status
        except Exception as exc:      # a registry problem must not hide progress
            logger.debug(f"  job status unavailable: {exc}")
    return CampaignProgress(
        n_designs=count_designs(paths),
        n_inverse_folded=count_inverse_folded(paths),
        n_refolds=count_refolds(paths),
        expected=expected,
        step=last_step(paths),
        metrics_written=paths.metrics_csv.is_file(),
        n_metrics=count_metrics_rows(paths),
        job_status=status,
    )


# ── launch / resume ─────────────────────────────────────────────────────────

def build_argv(executable: str | Path, yaml_path: Path, paths: BoltzGenPaths, *,
               protocol: str, num_designs: int, budget: int,
               reuse: bool = True) -> list[str]:
    """The `boltzgen run` command line.

    `--reuse` is on by default, and it is what makes a campaign resumable at
    all: re-invoking with a larger `--num_designs` EXTENDS what is on disk
    rather than repeating it, so a probe becomes the first slice of the real
    run and a crash costs only the unfinished designs.
    """
    argv = [str(executable), "run", str(yaml_path),
            "--output", str(paths.campaign_dir),
            "--protocol", protocol,
            "--num_designs", str(num_designs),
            "--budget", str(budget)]
    if reuse:
        argv.append("--reuse")
    return argv


def launch(
    paths: BoltzGenPaths, argv: Sequence[str], *,
    cuda_device: int | None = 0, note: str = "",
):
    """Start the campaign DETACHED and return its job record.

    Detached because a production campaign runs for hours: a blocking call
    inside a Celery task would hit the visibility timeout and be redelivered,
    which is two BoltzGens on one GPU. The environment is resolved HERE and
    baked into the launch, for the same reason `foundry_runner` bakes it into
    its driver — the child outlives this process and must not depend on it.
    """
    from src.job_registry import JobRegistry

    paths.mkdirs()
    env = dict(os.environ)
    if cuda_device is not None and "CUDA_VISIBLE_DEVICES" not in env:
        env["CUDA_VISIBLE_DEVICES"] = str(cuda_device)
    reg = JobRegistry(paths.registry_path)
    rec = reg.launch(JOB_ID, list(argv), cwd=paths.campaign_dir,
                     log_path=paths.log_path, env=env, note=note)
    logger.info(f"  boltzgen launched detached (pid {rec.pid}) -> "
                f"{paths.log_path}")
    return rec


def resume(paths: BoltzGenPaths, argv: Sequence[str], *,
           cuda_device: int | None = 0, note: str = ""):
    """Attach to a live campaign, or relaunch it.

    Relaunching is safe because every BoltzGen step skips what is already on
    disk under `--reuse`. The one thing that is NOT safe is relaunching over a
    corrupt design: the equal-length crash leaves a design whose CIF lost its
    binder chain, and `folding` will die on it again. `boltzgen_spec` prevents
    that case at source; anything else surfaces in the log.
    """
    from src.job_registry import JobRegistry, STATUS_RUNNING

    if paths.registry_path.is_file():
        reg = JobRegistry(paths.registry_path)
        try:
            rec = reg.refresh(JOB_ID)
        except Exception:
            rec = None
        if rec is not None and rec.status == STATUS_RUNNING:
            logger.info(f"  boltzgen already running (pid {rec.pid}) — "
                        f"attaching rather than starting a second one")
            return rec
    return launch(paths, argv, cuda_device=cuda_device, note=note)


def stop(paths: BoltzGenPaths) -> bool:
    from src.job_registry import JobRegistry

    if not paths.registry_path.is_file():
        return False
    return JobRegistry(paths.registry_path).kill(JOB_ID)
