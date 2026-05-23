"""LPT-side wrapper around the PyRosetta SASA subprocess worker.

PyRosetta lives in a separate conda env (Python 3.11 ABI), so we can't
``import pyrosetta`` directly from the LPT venv. Instead we send a JSON spec
to ``scripts/_sasa_worker.py`` running under that env's Python and parse the
JSON response.

Public surface:

* :class:`HotspotSasaResult` — typed result with bound/unbound/delta totals.
* :func:`compute_hotspot_sasa` — single-CIF wrapper.

The worker is invoked once per CIF; if you need to batch many designs,
call this in a loop (cheap — pyrosetta init is ~0.6s per call). A
persistent-worker pattern can come later if SASA dominates wall time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_WORKER = _ROOT / "scripts" / "_sasa_worker.py"


class PyRosettaWorkerError(RuntimeError):
    """The pyrosetta subprocess worker exited with an error."""


@dataclass
class HotspotSasaResult:
    cif_path: Path
    target_chain: str
    binder_chain: str
    target_residue_count: int
    binder_residue_count: int
    # Whole-target-chain SASA (sanity-check signal).
    target_sasa_bound: float
    target_sasa_unbound: float
    target_sasa_delta: float
    # Hotspot-restricted SASA (the actual ranking signal — how much the
    # binder occludes the residues we picked in the structure stage).
    hotspot_sasa_bound: float
    hotspot_sasa_unbound: float
    hotspot_sasa_delta: float
    per_hotspot: list[dict] = field(default_factory=list)
    hotspots_missing: list[int] = field(default_factory=list)


def compute_hotspot_sasa(
    cif_path: Path,
    target_chain: str,
    binder_chain: str,
    hotspots: list[dict],
    *,
    python_executable: str | Path,
    worker_script: Path | None = None,
    init_flags: str | None = None,
    timeout_s: float = 120.0,
) -> HotspotSasaResult:
    """Compute hotspot SASA for one boltzgen design CIF.

    Parameters
    ----------
    cif_path : Path
        Path to a complex CIF (target + binder).
    target_chain : str
        PDB chain ID of the target.
    binder_chain : str
        PDB chain ID of the generated binder.
    hotspots : list[dict]
        Entries from the structure-stage handoff's ``hotspot_residues_json``:
        ``{"auth_seq_id": int, "residue": str, ...}``. Extra keys are ignored.
    python_executable : str | Path
        Absolute path to the pyrosetta conda env's python, from
        ``config['design']['pyrosetta']['python_executable']``.
    worker_script : Path | None
        Defaults to ``scripts/_sasa_worker.py`` next to this module.
    init_flags : str | None
        Overrides the worker's default ``pyrosetta.init`` flag string.
    timeout_s : float
        Hard timeout for the subprocess. PyRosetta init + a single SASA pass
        on a small complex takes ~1s; 120s is generous headroom.

    Returns
    -------
    HotspotSasaResult

    Raises
    ------
    PyRosettaWorkerError
        On subprocess exit ≠ 0 or timeout. The original stderr is included.
    """
    cif_path = Path(cif_path)
    if not cif_path.exists():
        raise FileNotFoundError(f"cif_path does not exist: {cif_path}")

    worker = Path(worker_script) if worker_script else _DEFAULT_WORKER
    if not worker.exists():
        raise PyRosettaWorkerError(f"SASA worker script not found: {worker}")

    py = Path(python_executable)
    if not py.is_absolute():
        which = shutil.which(str(python_executable))
        if not which:
            raise PyRosettaWorkerError(
                f"pyrosetta python_executable {python_executable!r} not on PATH. "
                "Set design.pyrosetta.python_executable in config.yaml to the "
                "absolute path of the pyrosetta conda env's python."
            )
        py = Path(which)
    elif not py.exists():
        raise PyRosettaWorkerError(f"pyrosetta python not found: {py}")

    spec: dict = {
        "cif_path": str(cif_path),
        "target_chain": target_chain,
        "binder_chain": binder_chain,
        "hotspots": [
            {"auth_seq_id": int(h["auth_seq_id"]), "residue": h.get("residue", "")}
            for h in hotspots
        ],
    }
    if init_flags is not None:
        spec["init_flags"] = init_flags

    cmd = [str(py), str(worker)]
    logger.debug(f"  sasa worker: {cmd[0]} {cmd[1]} (cif={cif_path.name})")

    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(spec),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PyRosettaWorkerError(
            f"SASA worker timed out after {timeout_s}s on {cif_path}"
        )

    if proc.returncode != 0:
        raise PyRosettaWorkerError(
            f"SASA worker failed (rc={proc.returncode}) on {cif_path}\n"
            f"--- stderr ---\n{proc.stderr.strip() or '(empty)'}\n"
            f"--- stdout ---\n{proc.stdout.strip()[-500:]}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PyRosettaWorkerError(
            f"SASA worker emitted invalid JSON on {cif_path}: {exc}\n"
            f"stdout: {proc.stdout[:500]}"
        )
    if data.get("status") != "ok":
        raise PyRosettaWorkerError(
            f"SASA worker reported failure on {cif_path}: {data.get('error', '(no message)')}"
        )

    return HotspotSasaResult(
        cif_path=cif_path,
        target_chain=data["target_chain"],
        binder_chain=data["binder_chain"],
        target_residue_count=int(data["target_residue_count"]),
        binder_residue_count=int(data["binder_residue_count"]),
        target_sasa_bound=float(data["target_sasa_bound"]),
        target_sasa_unbound=float(data["target_sasa_unbound"]),
        target_sasa_delta=float(data["target_sasa_delta"]),
        hotspot_sasa_bound=float(data["hotspot_sasa_bound"]),
        hotspot_sasa_unbound=float(data["hotspot_sasa_unbound"]),
        hotspot_sasa_delta=float(data["hotspot_sasa_delta"]),
        per_hotspot=list(data.get("per_hotspot", [])),
        hotspots_missing=[int(x) for x in data.get("hotspots_missing", [])],
    )
