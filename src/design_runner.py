"""Workstation execution wrapper for BoltzGen.

Blocking subprocess wrapper around ``boltzgen check`` and ``boltzgen run`` plus
a pilot-then-production helper that uses BoltzGen's ``--reuse`` flag so the
production run extends the pilot run in place instead of re-doing it.

Public surface:

* :func:`validate_yaml`              — wrap ``boltzgen check <yaml>``.
* :func:`run_design`                 — wrap ``boltzgen run <yaml> --output ...``.
* :func:`evaluate_pilot`             — read pilot outputs, return gate verdict.
* :func:`run_pilot_then_production`  — chain the above.

The orchestrator in :mod:`src.pipeline_runner` wires these into stage 4 in
chunk 3; this module is intentionally orchestrator-free so it can be called
from scripts, tests, or the web backend.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DesignRunnerError(RuntimeError):
    """Base error for design_runner failures."""


class BoltzGenValidationError(DesignRunnerError):
    """`boltzgen check` rejected the YAML."""


class BoltzGenRunError(DesignRunnerError):
    """`boltzgen run` exited non-zero, timed out, or produced no outputs."""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    output_dir: Path
    status: str                  # "ok" | "skipped"
    runtime_s: float
    num_designs_requested: int
    budget_requested: int
    log_path: Path
    returncode: int


@dataclass
class PilotResult:
    output_dir: Path
    runtime_s: float
    num_designs_requested: int
    budget_requested: int
    total_completed: int         # rows in all_designs_metrics.csv
    final_count: int             # rows in final_designs_metrics_<budget>.csv (or CIFs in final_<budget>_designs/)
    completion_rate: float       # total_completed / num_designs_requested
    final_fill_rate: float       # final_count / budget_requested
    passes_gate: bool
    gate_reason: str
    log_path: Path


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_executable(executable: str | Path) -> Path:
    """Resolve a boltzgen executable spec to an absolute Path.

    Accepts an absolute path (verified to exist) or a bare command name
    (resolved via ``shutil.which``). Raises :class:`DesignRunnerError` if the
    executable cannot be found — the user must set
    ``design.workstation.boltzgen_executable`` to point at the right env.
    """
    p = Path(executable)
    if p.is_absolute():
        if not p.exists():
            raise DesignRunnerError(f"boltzgen executable not found: {p}")
        return p
    found = shutil.which(str(executable))
    if not found:
        raise DesignRunnerError(
            f"boltzgen executable {executable!r} not on PATH. "
            "Set design.workstation.boltzgen_executable in config.yaml to the "
            "absolute path of your boltzgen entry point "
            "(e.g. /home/.../envs/bg/bin/boltzgen)."
        )
    return Path(found)


def _tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return f"(could not read {path})"
    return "\n".join(lines[-n:])


def _csv_row_count(path: Path) -> int:
    """Count data rows (header excluded). Returns 0 if file is missing."""
    if not path.exists():
        return 0
    with path.open() as f:
        return max(0, sum(1 for _ in f) - 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_yaml(
    yaml_path: Path,
    *,
    executable: str | Path = "boltzgen",
    check_output_dir: Path | None = None,
) -> None:
    """Run ``boltzgen check <yaml>`` and raise on non-zero exit.

    Parameters
    ----------
    yaml_path : Path
        Design YAML produced by the ``protein-design-script`` skill.
    executable : str | Path
        ``boltzgen`` entry point — either a bare command on ``PATH`` or an
        absolute path to e.g. ``/home/.../envs/bg/bin/boltzgen``.
    check_output_dir : Path | None
        If provided, ``--output <dir>`` is passed so ``boltzgen check`` also
        writes a colored mmCIF visualisation. Useful for debugging.

    Note: ``boltzgen check`` downloads a small molecule library
    (``mols.zip``) from HuggingFace on first invocation. Subsequent calls
    hit the local cache.
    """
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise BoltzGenValidationError(f"Design YAML not found: {yaml_path}")

    exe = _resolve_executable(executable)
    cmd: list[str] = [str(exe), "check", str(yaml_path)]
    if check_output_dir is not None:
        check_output_dir = Path(check_output_dir)
        check_output_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["--output", str(check_output_dir)]

    logger.info(f"  $ {shlex.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BoltzGenValidationError(
            f"`boltzgen check` failed (rc={proc.returncode}) for {yaml_path}\n"
            f"--- stderr ---\n{proc.stderr.strip() or '(empty)'}\n"
            f"--- stdout (last 1000 chars) ---\n{proc.stdout.strip()[-1000:]}"
        )
    logger.info("  boltzgen check: OK")


def run_design(
    yaml_path: Path,
    output_dir: Path,
    *,
    protocol: str,
    num_designs: int,
    budget: int,
    executable: str | Path = "boltzgen",
    cuda_device: int | None = 0,
    reuse: bool = True,
    timeout_h: float = 24.0,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Invoke ``boltzgen run`` as a blocking subprocess.

    stdout+stderr are streamed to ``<output_dir>/boltzgen.log``. Raises
    :class:`BoltzGenRunError` on non-zero exit or timeout — the log path is
    included in the error message so callers can surface it.

    ``reuse=True`` (the BoltzGen ``--reuse`` flag) lets a second call against
    the same ``output_dir`` extend the previous run rather than redo it. This
    is the basis of :func:`run_pilot_then_production`.

    ``cuda_device`` is exported via ``CUDA_VISIBLE_DEVICES`` only if not
    already set in the inherited environment — explicit user overrides win.
    """
    yaml_path = Path(yaml_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "boltzgen.log"

    exe = _resolve_executable(executable)
    cmd: list[str] = [
        str(exe), "run", str(yaml_path),
        "--output", str(output_dir),
        "--protocol", protocol,
        "--num_designs", str(num_designs),
        "--budget", str(budget),
    ]
    if reuse:
        cmd.append("--reuse")
    if extra_args:
        cmd.extend(extra_args)

    run_env = os.environ.copy()
    if env:
        run_env.update({k: str(v) for k, v in env.items()})
    if cuda_device is not None and "CUDA_VISIBLE_DEVICES" not in run_env:
        run_env["CUDA_VISIBLE_DEVICES"] = str(cuda_device)

    logger.info(
        f"  $ {shlex.join(cmd)}"
        f"  [CUDA_VISIBLE_DEVICES={run_env.get('CUDA_VISIBLE_DEVICES', 'inherit')}]"
    )
    logger.info(f"  streaming log → {log_path}")

    start = time.time()
    with log_path.open("a") as log_f:
        log_f.write(
            f"\n=== run_design @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"num_designs={num_designs} budget={budget} protocol={protocol} ===\n"
        )
        log_f.flush()
        try:
            proc = subprocess.run(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                env=run_env,
                timeout=timeout_h * 3600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            raise BoltzGenRunError(
                f"boltzgen run exceeded timeout_h={timeout_h} ({elapsed:.0f}s). "
                f"Log: {log_path}"
            )

    elapsed = time.time() - start
    if proc.returncode != 0:
        raise BoltzGenRunError(
            f"boltzgen run failed (rc={proc.returncode}, {elapsed:.0f}s).\n"
            f"--- last 30 lines of {log_path} ---\n{_tail(log_path, 30)}"
        )

    logger.info(f"  boltzgen run: OK ({elapsed:.0f}s)")
    return RunResult(
        output_dir=output_dir,
        status="ok",
        runtime_s=elapsed,
        num_designs_requested=num_designs,
        budget_requested=budget,
        log_path=log_path,
        returncode=proc.returncode,
    )


def evaluate_pilot(
    output_dir: Path,
    *,
    num_designs_requested: int,
    budget_requested: int,
    min_completion_rate: float,
    min_final_fill_rate: float,
) -> tuple[int, int, float, float, bool, str]:
    """Read pilot outputs and decide whether to proceed to production.

    The gate is intentionally simple at this stage: it checks that the
    pipeline didn't lose too many designs (``completion_rate``) and that the
    final filter step found enough survivors to fill the requested budget
    (``final_fill_rate``). Quality-based gates (iPTM/iPAE thresholds) live
    in chunk 2's analysis module and run on the *production* output.

    Returns
    -------
    (total_completed, final_count, completion_rate, final_fill_rate,
     passes_gate, reason)
    """
    output_dir = Path(output_dir)
    final_dir = output_dir / "final_ranked_designs"

    all_csv = final_dir / "all_designs_metrics.csv"
    total_completed = _csv_row_count(all_csv)

    final_csv = final_dir / f"final_designs_metrics_{budget_requested}.csv"
    if final_csv.exists():
        final_count = _csv_row_count(final_csv)
    else:
        final_subdir = final_dir / f"final_{budget_requested}_designs"
        final_count = len(list(final_subdir.glob("*.cif"))) if final_subdir.exists() else 0

    completion_rate = total_completed / num_designs_requested if num_designs_requested else 0.0
    final_fill_rate = final_count / budget_requested if budget_requested else 0.0

    if not all_csv.exists():
        return total_completed, final_count, completion_rate, final_fill_rate, False, (
            f"pilot output missing: {all_csv} not found — boltzgen run may have aborted before analysis"
        )
    if completion_rate < min_completion_rate:
        return total_completed, final_count, completion_rate, final_fill_rate, False, (
            f"completion_rate {completion_rate:.1%} < threshold {min_completion_rate:.0%} "
            f"({total_completed}/{num_designs_requested} designs completed the pipeline)"
        )
    if final_fill_rate < min_final_fill_rate:
        return total_completed, final_count, completion_rate, final_fill_rate, False, (
            f"final_fill_rate {final_fill_rate:.1%} < threshold {min_final_fill_rate:.0%} "
            f"({final_count}/{budget_requested} designs survived filtering)"
        )
    return total_completed, final_count, completion_rate, final_fill_rate, True, (
        f"gate passed: completion={completion_rate:.1%}, final_fill={final_fill_rate:.1%}"
    )


def run_pilot_then_production(
    yaml_path: Path,
    output_dir: Path,
    *,
    protocol: str,
    pilot_num_designs: int,
    pilot_budget: int,
    production_num_designs: int,
    production_budget: int,
    min_completion_rate: float = 0.5,
    min_final_fill_rate: float = 0.5,
    executable: str | Path = "boltzgen",
    cuda_device: int | None = 0,
    timeout_h: float = 24.0,
    skip_pilot: bool = False,
    force_production: bool = False,
) -> tuple[PilotResult, RunResult | None]:
    """Pilot → gate → production, using BoltzGen ``--reuse`` to extend in place.

    Returns ``(pilot, prod)``. ``prod`` is ``None`` when:

    * the pilot gate failed and ``force_production=False`` — caller (stage 4
      in :mod:`src.pipeline_runner`) is expected to raise
      ``PipelinePausedError("pilot_failed", ...)`` so the user can decide
      whether to override via ``force_production=True``.

    When ``skip_pilot=True`` the production run executes directly and the
    returned ``pilot`` is a synthetic "skipped" record with ``passes_gate=True``.
    """
    yaml_path = Path(yaml_path)
    output_dir = Path(output_dir)

    if skip_pilot:
        logger.info("  pilot skipped — running production directly")
        prod = run_design(
            yaml_path, output_dir,
            protocol=protocol,
            num_designs=production_num_designs,
            budget=production_budget,
            executable=executable,
            cuda_device=cuda_device,
            timeout_h=timeout_h,
        )
        return (
            PilotResult(
                output_dir=output_dir, runtime_s=0.0,
                num_designs_requested=0, budget_requested=0,
                total_completed=0, final_count=0,
                completion_rate=0.0, final_fill_rate=0.0,
                passes_gate=True, gate_reason="pilot skipped by caller",
                log_path=output_dir / "boltzgen.log",
            ),
            prod,
        )

    pilot_start = time.time()
    pilot_run = run_design(
        yaml_path, output_dir,
        protocol=protocol,
        num_designs=pilot_num_designs,
        budget=pilot_budget,
        executable=executable,
        cuda_device=cuda_device,
        timeout_h=timeout_h,
    )
    pilot_elapsed = time.time() - pilot_start

    total_completed, final_count, completion_rate, final_fill_rate, passes, reason = evaluate_pilot(
        output_dir,
        num_designs_requested=pilot_num_designs,
        budget_requested=pilot_budget,
        min_completion_rate=min_completion_rate,
        min_final_fill_rate=min_final_fill_rate,
    )
    pilot = PilotResult(
        output_dir=output_dir,
        runtime_s=pilot_elapsed,
        num_designs_requested=pilot_num_designs,
        budget_requested=pilot_budget,
        total_completed=total_completed,
        final_count=final_count,
        completion_rate=completion_rate,
        final_fill_rate=final_fill_rate,
        passes_gate=passes,
        gate_reason=reason,
        log_path=pilot_run.log_path,
    )
    logger.info(
        f"  pilot result: completed={total_completed}/{pilot_num_designs} "
        f"({completion_rate:.1%}), final={final_count}/{pilot_budget} "
        f"({final_fill_rate:.1%}) — {'PASS' if passes else 'FAIL'}: {reason}"
    )

    if not passes and not force_production:
        logger.warning("  pilot gate failed — returning without running production")
        return pilot, None

    if not passes and force_production:
        logger.warning("  pilot gate failed but force_production=True — proceeding anyway")

    prod = run_design(
        yaml_path, output_dir,
        protocol=protocol,
        num_designs=production_num_designs,
        budget=production_budget,
        executable=executable,
        cuda_device=cuda_device,
        timeout_h=timeout_h,
    )
    return pilot, prod
