"""
PyRosetta interface metrics, run only on designs that already look right.

## Why gating comes first

Rosetta does not know whether a predicted complex is real. Relax and
InterfaceAnalyzer will happily return a −40 kcal/mol ddG for a binder docked
30 Å from its intended epitope, or for a 0.6-pLDDT model that folded into
something plausible but wrong: those are still physical poses, so the physics
terms are well-defined and completely uninformative. Ranking on them would
promote confident nonsense.

So the order is fixed: **confidence and geometry gates first, Rosetta second, and
Rosetta terms enter only the final composite.** On the reference campaign that
takes ~28,000 refolds down to a few hundred before any PyRosetta call — which is
also the only reason this is affordable, at ~10-30 s per design single-threaded.

PyRosetta lives in its own conda env (ABI-incompatible with LPT's venv), so this
module drives `scripts/_rosetta_worker.py` as a subprocess — the same pattern as
`src/pyrosetta_sasa.py`.
"""

from __future__ import annotations

import csv
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from loguru import logger


_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _ROOT / "scripts" / "_rosetta_worker.py"

# Columns the worker emits. Kept explicit so a rename shows up as a KeyError
# here rather than as a silently-missing ranking term.
ROSETTA_COLUMNS = (
    "ddg", "dsasa", "cms", "hbonds_int", "unsat_hbonds", "vbuns", "sbuns",
    "packstat", "ddg_per_dsasa", "ddg_per_res", "iface_score", "total_score",
)


class RosettaUnavailable(RuntimeError):
    """PyRosetta is not installed or not configured; scoring is skipped."""


@dataclass
class RosettaResult:
    rows: list[dict]
    csv_path: Path | None
    n_scored: int
    n_failed: int
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.n_scored > 0

    def by_design(self) -> dict[str, dict]:
        return {r["design"]: r for r in self.rows if not r.get("error")}


def _interpreter(cfg: dict) -> str:
    """The pyrosetta env's python: LPT_PYROSETTA_PYTHON env var, then config.

    Resolution is shared with the PPI track's SASA path (`pyrosetta_sasa`) so
    the two cannot disagree about whether PyRosetta is usable. Note there is
    deliberately no `shutil.which("python")` fallback: that resolved the LPT
    venv's own interpreter, which has no pyrosetta, so `available()` said True
    and the worker then died on `import pyrosetta` — surfaced to the user as
    the baffling "worker produced no output (rc=1)".
    """
    from src.pyrosetta_sasa import resolve_interpreter

    exe = resolve_interpreter(cfg)
    if not exe:
        raise RosettaUnavailable(
            "no pyrosetta interpreter configured — set the LPT_PYROSETTA_PYTHON "
            "env var (see .env.example) or design.pyrosetta.python_executable "
            "in config.yaml. PyRosetta is optional: with "
            "design.pyrosetta.enabled: auto (the default) the pipeline simply "
            "skips the Rosetta scoring terms.")
    return exe


def available(cfg: dict) -> bool:
    """True when Rosetta scoring can run — honours design.pyrosetta.enabled."""
    from src.pyrosetta_sasa import check_available

    usable, _reason = check_available(cfg)
    return usable and _WORKER.exists()


def score_designs(
    structures: Sequence[Path],
    out_csv: Path,
    *,
    cfg: dict,
    binder_chain: str = "A",
    target_chain: str = "B",
    relax: bool = True,
    workers: int | None = None,
    timeout_s: float = 7200.0,
) -> RosettaResult:
    """
    Score a SHORTLIST of predicted complexes.

    `structures` must already have passed the confidence and geometry gates —
    see the module docstring. Passing a whole campaign here is both meaningless
    and unaffordable.

    Never raises for a missing PyRosetta: it returns a result with
    `skipped_reason` set, because Rosetta terms are an enrichment of the ranking,
    not a prerequisite for it.
    """
    structures = [Path(s) for s in structures]
    out_csv = Path(out_csv)
    if not structures:
        return RosettaResult([], None, 0, 0, "no designs passed the gates")
    try:
        exe = _interpreter(cfg)
    except RosettaUnavailable as exc:
        logger.warning(f"skipping Rosetta metrics: {exc}")
        return RosettaResult([], None, 0, 0, str(exc))
    if not _WORKER.exists():
        return RosettaResult([], None, 0, 0, f"worker missing at {_WORKER}")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    listing = out_csv.parent / "rosetta_inputs.txt"
    listing.write_text("\n".join(str(s.resolve()) for s in structures),
                       encoding="utf-8")

    workers = workers or max(1, (os.cpu_count() or 4) - 2)
    argv = [exe, str(_WORKER), "--list", str(listing),
            "--out", str(out_csv),
            "--binder-chain", binder_chain, "--target-chain", target_chain,
            "--nproc", str(workers)]
    if not relax:
        argv.append("--no-relax")

    logger.info(
        f"Rosetta: scoring {len(structures):,} gated design(s) on {workers} "
        f"worker(s) — this is the expensive step, which is why it runs last")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"Rosetta scoring failed to run: {exc}")
        return RosettaResult([], None, 0, 0, str(exc))
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        logger.warning(f"Rosetta worker exited {proc.returncode}: {' | '.join(tail)}")
    if not out_csv.exists():
        return RosettaResult([], None, 0, 0,
                             f"worker produced no output (rc={proc.returncode})")

    rows = list(csv.DictReader(out_csv.open(encoding="utf-8")))
    failed = [r for r in rows if r.get("error")]
    logger.info(f"Rosetta: {len(rows) - len(failed):,} scored, {len(failed)} failed")
    return RosettaResult(rows, out_csv, len(rows) - len(failed), len(failed))


def merge_into(records: Sequence[dict], result: RosettaResult,
               key: str = "name") -> int:
    """
    Attach Rosetta columns to already-scored designs.

    Designs without a Rosetta row keep their confidence metrics and simply have
    no Rosetta terms — `binder_ranking.composite_score` substitutes the column
    mean for those, so an ungated design is neither rewarded nor punished for
    the absence.
    """
    by_design = result.by_design()
    if not by_design:
        return 0
    n = 0
    for rec in records:
        row = by_design.get(str(rec.get(key, "")))
        if not row:
            continue
        for col in ROSETTA_COLUMNS:
            val = row.get(col)
            if val not in ("", None):
                rec[f"rosetta_{col}"] = val
        n += 1
    logger.info(f"merged Rosetta metrics into {n:,} design record(s)")
    return n


def select_for_rosetta(records: Sequence[dict], *, limit: int = 300,
                       cif_column: str = "refold_cif") -> list[dict]:
    """
    The shortlist worth spending PyRosetta on: survivors, best first.

    Capped because the cost is linear and the tail is not worth it — if 2000
    designs survive the gates, the top few hundred by composite are the ones a
    human will ever look at.
    """
    ranked = sorted(
        (r for r in records if r.get(cif_column)),
        key=lambda r: -(float(r["composite_score"])
                        if r.get("composite_score") not in ("", None) else 0.0))
    return ranked[:limit]
