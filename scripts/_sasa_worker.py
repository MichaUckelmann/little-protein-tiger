#!/usr/bin/env python
"""PyRosetta SASA worker — runs in a Python 3.11 conda env.

Invoked as a subprocess from src/pyrosetta_sasa.py. Reads a JSON spec on stdin
and emits a JSON result on stdout. All errors go to stderr; non-zero exit on
failure. This file deliberately has zero LPT imports — it must stand alone
inside the pyrosetta conda env.

Stdin schema:
    {
      "cif_path":      str,        # absolute path to the boltzgen complex CIF
      "target_chain":  str,        # PDB chain ID, e.g. "A"
      "binder_chain":  str,        # PDB chain ID, e.g. "B"
      "hotspots":      [           # may be empty → totals only
        {"auth_seq_id": int, "residue": str (optional)},
        ...
      ],
      "init_flags":    str (optional)
    }

Stdout schema (on success):
    {
      "status":               "ok",
      "cif_path":             str,
      "target_chain":         str,
      "binder_chain":         str,
      "target_residue_count": int,
      "binder_residue_count": int,
      "target_sasa_bound":    float,      # summed over target chain in complex
      "target_sasa_unbound":  float,      # summed over target chain, binder deleted
      "target_sasa_delta":    float,      # unbound - bound  (≥0; how much the binder buries)
      "hotspot_sasa_bound":   float,      # summed over hotspot residues only
      "hotspot_sasa_unbound": float,
      "hotspot_sasa_delta":   float,
      "per_hotspot": [
        {"auth_seq_id": int, "residue": str, "sasa_bound": float, "sasa_unbound": float, "sasa_delta": float, "found": bool},
        ...
      ],
      "hotspots_missing":     [int, ...]   # auth_seq_ids not found on target chain
    }
"""

from __future__ import annotations

import contextlib
import json
import sys
import traceback
from pathlib import Path

# PyRosetta prints a license/version banner to stdout during init() that would
# otherwise corrupt the JSON we emit on stdout. Importing the module is silent;
# only the init() call below is wrapped in a stdout→stderr redirect.
import pyrosetta
from pyrosetta.rosetta.core.scoring.sasa import SasaCalc


_DEFAULT_INIT_FLAGS = "-mute all -ignore_unrecognized_res -load_PDB_components false"


def _sum_chain_sasa(pose, per_res_sasa, chain_label: str) -> tuple[float, int]:
    """Sum per-residue SASA over residues whose PDB chain matches `chain_label`."""
    info = pose.pdb_info()
    total = 0.0
    n = 0
    for i in range(1, pose.total_residue() + 1):
        if info.chain(i) == chain_label:
            total += float(per_res_sasa[i])
            n += 1
    return total, n


def _index_target_residues(pose, chain_label: str) -> dict[int, int]:
    """Return {auth_seq_id: pose_index} for residues on the given chain."""
    info = pose.pdb_info()
    mapping: dict[int, int] = {}
    for i in range(1, pose.total_residue() + 1):
        if info.chain(i) == chain_label:
            mapping[int(info.number(i))] = i
    return mapping


def _target_only_pose(pose, target_chain: str):
    """Return a Pose containing only the target chain.

    Uses split_by_chain (returns a utility_vector1<PoseOP> indexed 1..n) and
    picks the entry whose first residue's PDB chain matches.
    """
    chains = pose.split_by_chain()
    for idx in range(1, len(chains) + 1):
        sub = chains[idx]
        if sub.pdb_info().chain(1) == target_chain:
            return sub
    raise RuntimeError(
        f"target_chain {target_chain!r} not found in pose; "
        f"present chains: "
        f"{[chains[i].pdb_info().chain(1) for i in range(1, len(chains)+1)]}"
    )


def run(spec: dict) -> dict:
    cif_path = Path(spec["cif_path"])
    if not cif_path.exists():
        raise FileNotFoundError(f"cif_path does not exist: {cif_path}")
    target_chain: str = spec["target_chain"]
    binder_chain: str = spec["binder_chain"]
    hotspots: list[dict] = spec.get("hotspots") or []

    init_flags = spec.get("init_flags") or _DEFAULT_INIT_FLAGS
    with contextlib.redirect_stdout(sys.stderr):
        pyrosetta.init(init_flags)
        complex_pose = pyrosetta.pose_from_file(str(cif_path))

    target_only = _target_only_pose(complex_pose, target_chain)

    sasa_calc = SasaCalc()
    sasa_calc.calculate(complex_pose)
    bound_per_res = sasa_calc.get_residue_sasa()
    sasa_calc.calculate(target_only)
    unbound_per_res = sasa_calc.get_residue_sasa()

    target_bound_total, target_count = _sum_chain_sasa(complex_pose, bound_per_res, target_chain)
    target_unbound_total, _ = _sum_chain_sasa(target_only, unbound_per_res, target_chain)

    binder_count = 0
    info = complex_pose.pdb_info()
    for i in range(1, complex_pose.total_residue() + 1):
        if info.chain(i) == binder_chain:
            binder_count += 1

    bound_index = _index_target_residues(complex_pose, target_chain)
    unbound_index = _index_target_residues(target_only, target_chain)

    per_hotspot: list[dict] = []
    hotspots_missing: list[int] = []
    hotspot_bound_sum = 0.0
    hotspot_unbound_sum = 0.0
    for hs in hotspots:
        auth = int(hs["auth_seq_id"])
        resname = hs.get("residue", "")
        idx_bound = bound_index.get(auth)
        idx_unbound = unbound_index.get(auth)
        if idx_bound is None or idx_unbound is None:
            hotspots_missing.append(auth)
            per_hotspot.append({
                "auth_seq_id": auth,
                "residue": resname,
                "sasa_bound": None,
                "sasa_unbound": None,
                "sasa_delta": None,
                "found": False,
            })
            continue
        sb = float(bound_per_res[idx_bound])
        sub = float(unbound_per_res[idx_unbound])
        per_hotspot.append({
            "auth_seq_id": auth,
            "residue": resname,
            "sasa_bound": sb,
            "sasa_unbound": sub,
            "sasa_delta": sub - sb,
            "found": True,
        })
        hotspot_bound_sum += sb
        hotspot_unbound_sum += sub

    return {
        "status": "ok",
        "cif_path": str(cif_path),
        "target_chain": target_chain,
        "binder_chain": binder_chain,
        "target_residue_count": target_count,
        "binder_residue_count": binder_count,
        "target_sasa_bound": target_bound_total,
        "target_sasa_unbound": target_unbound_total,
        "target_sasa_delta": target_unbound_total - target_bound_total,
        "hotspot_sasa_bound": hotspot_bound_sum,
        "hotspot_sasa_unbound": hotspot_unbound_sum,
        "hotspot_sasa_delta": hotspot_unbound_sum - hotspot_bound_sum,
        "per_hotspot": per_hotspot,
        "hotspots_missing": hotspots_missing,
    }


def main() -> int:
    try:
        spec = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON on stdin: {exc}\n")
        return 2
    try:
        result = run(spec)
    except Exception as exc:
        sys.stderr.write(f"sasa_worker failed: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        json.dump({"status": "error", "error": str(exc)}, sys.stdout)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
