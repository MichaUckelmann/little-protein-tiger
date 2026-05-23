"""Parse boltzgen design outputs and enrich them with per-hotspot SASA.

Boltzgen already emits a fat metrics CSV per run
(``<run_dir>/final_ranked_designs/all_designs_metrics.csv``, ~260 columns
including ``design_to_target_iptm``, ``min_design_to_target_pae``,
``complex_plddt``, ``design_sasa_bound_refolded``, ``quality_score``,
``pass_filters``, ``final_rank``…). What it does **not** know about is the
user-specified hotspots from the structure stage — i.e. whether the binder
actually landed on those residues.

This module:

1. :func:`parse_boltzgen_outputs` — load the CSV as ``list[dict]`` records
   and resolve each design's CIF path under ``intermediate_designs/``.
2. :func:`enrich_with_hotspot_sasa` — call
   :func:`src.pyrosetta_sasa.compute_hotspot_sasa` per design and stamp
   ``lpt_*`` columns onto each record. Failures are logged and the columns
   set to ``None`` (configurable via ``on_error``).
3. :func:`write_enriched_csv` — write the enriched records back out as a
   single CSV. Boltzgen-native columns first, ``lpt_*`` columns last.

Chunk 3 (ranking) consumes the enriched records list directly.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from loguru import logger

from src.pyrosetta_sasa import PyRosettaWorkerError, compute_hotspot_sasa


DesignRecord = dict[str, Any]


# Columns we'll convert from CSV strings to floats where possible.
# We don't enumerate them — instead we just try-float every non-empty cell.
# This keeps the parser robust against new boltzgen columns showing up.


def _coerce_value(s: str) -> Any:
    """Best-effort string→numeric coercion for CSV cells.

    Empty strings become ``None``. Strings that parse as int return ``int``;
    strings that parse as float return ``float``. Everything else stays as
    ``str``. This preserves boltzgen's native types without us having to
    enumerate ~260 column names.
    """
    if s == "" or s is None:
        return None
    # Try int first so "23" doesn't become 23.0.
    try:
        if "." not in s and "e" not in s and "E" not in s:
            return int(s)
    except (TypeError, ValueError):
        pass
    try:
        return float(s)
    except (TypeError, ValueError):
        return s


def parse_boltzgen_outputs(
    run_dir: Path,
    *,
    metrics_csv_name: str = "all_designs_metrics.csv",
    intermediate_dir_name: str = "intermediate_designs",
) -> list[DesignRecord]:
    """Read a boltzgen run's metrics CSV and resolve per-design CIF paths.

    Parameters
    ----------
    run_dir : Path
        The directory passed as ``--output`` to ``boltzgen run`` — same dir
        :func:`src.design_runner.run_design` writes into.
    metrics_csv_name : str
        Name of the metrics CSV inside ``final_ranked_designs/``. Use
        ``f"final_designs_metrics_{budget}.csv"`` to read only the top-K.
    intermediate_dir_name : str
        Where the per-design CIFs live, relative to ``run_dir``.

    Returns
    -------
    list[DesignRecord]
        One dict per design. All boltzgen-native columns are preserved,
        type-coerced to int/float/str. Two extra keys are added:

        * ``design_id`` — alias for ``id`` (string).
        * ``cif_path`` — absolute ``pathlib.Path`` to the CIF, resolved
          under ``run_dir / intermediate_dir_name``. ``None`` if the file
          is missing (logged as a warning).

    Raises
    ------
    FileNotFoundError
        If the metrics CSV itself is missing — typically means boltzgen
        aborted before the analysis step finished.
    """
    run_dir = Path(run_dir)
    csv_path = run_dir / "final_ranked_designs" / metrics_csv_name
    if not csv_path.exists():
        raise FileNotFoundError(
            f"boltzgen metrics CSV not found: {csv_path} "
            "(was the run interrupted before analysis?)"
        )
    cif_dir = run_dir / intermediate_dir_name

    records: list[DesignRecord] = []
    missing_cifs = 0
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            record: DesignRecord = {k: _coerce_value(v) for k, v in row.items()}
            design_id = str(record.get("id", "")) or None
            record["design_id"] = design_id
            file_name = row.get("file_name") or ""
            cif_path = cif_dir / file_name if file_name else None
            if cif_path is not None and not cif_path.exists():
                missing_cifs += 1
                cif_path = None
            record["cif_path"] = cif_path
            records.append(record)

    logger.info(
        f"  parsed {len(records)} designs from {csv_path.name}"
        + (f" ({missing_cifs} CIFs missing under {intermediate_dir_name}/)" if missing_cifs else "")
    )
    return records


# Columns added by :func:`enrich_with_hotspot_sasa`. Kept in one place so
# the ranking stage (chunk 3) and the analyst skill (chunk 4) reference a
# single source of truth.
LPT_SASA_COLUMNS = (
    "lpt_target_sasa_bound",
    "lpt_target_sasa_unbound",
    "lpt_target_sasa_delta",
    "lpt_hotspot_sasa_bound",
    "lpt_hotspot_sasa_unbound",
    "lpt_hotspot_sasa_delta",
    "lpt_hotspots_missing",
)


def enrich_with_hotspot_sasa(
    records: list[DesignRecord],
    *,
    target_chain: str,
    binder_chain: str,
    hotspots: list[dict],
    python_executable: str | Path,
    init_flags: str | None = None,
    on_error: str = "skip",
    max_designs: int | None = None,
    progress_every: int = 25,
) -> list[DesignRecord]:
    """Compute per-design hotspot SASA via :mod:`src.pyrosetta_sasa`.

    Each call subprocesses to the pyrosetta worker (one ``pyrosetta.init``
    per design, ~0.6s overhead). For 100-design top-K runs this is ~1 min;
    for full 20k production runs you want to enrich only the top-K from
    boltzgen's ``final_rank`` ordering — chunk 3 will gate on that.

    Records are mutated in place; the list is also returned for chaining.

    Parameters
    ----------
    on_error : str
        ``"skip"`` (default) — log a warning, set the lpt_ columns to
        ``None``, continue. ``"raise"`` — let
        :class:`~src.pyrosetta_sasa.PyRosettaWorkerError` propagate.
    max_designs : int | None
        Cap on how many records to enrich. ``None`` = all. Records past the
        cap have the lpt_ columns set to ``None`` so the schema stays
        consistent.
    """
    if on_error not in ("skip", "raise"):
        raise ValueError(f"on_error must be 'skip' or 'raise', got {on_error!r}")

    n_total = len(records)
    n_to_enrich = n_total if max_designs is None else min(max_designs, n_total)
    n_ok = 0
    n_skipped_no_cif = 0
    n_failed = 0

    for idx, record in enumerate(records):
        if idx >= n_to_enrich:
            for col in LPT_SASA_COLUMNS:
                record.setdefault(col, None)
            continue

        cif_path: Path | None = record.get("cif_path")
        if cif_path is None:
            n_skipped_no_cif += 1
            for col in LPT_SASA_COLUMNS:
                record[col] = None
            continue

        try:
            result = compute_hotspot_sasa(
                cif_path=cif_path,
                target_chain=target_chain,
                binder_chain=binder_chain,
                hotspots=hotspots,
                python_executable=python_executable,
                init_flags=init_flags,
            )
        except (PyRosettaWorkerError, FileNotFoundError) as exc:
            n_failed += 1
            if on_error == "raise":
                raise
            logger.warning(
                f"  SASA failed on design {record.get('design_id')} "
                f"({cif_path.name if cif_path else '?'}): {exc}"
            )
            for col in LPT_SASA_COLUMNS:
                record[col] = None
            continue

        record["lpt_target_sasa_bound"] = result.target_sasa_bound
        record["lpt_target_sasa_unbound"] = result.target_sasa_unbound
        record["lpt_target_sasa_delta"] = result.target_sasa_delta
        record["lpt_hotspot_sasa_bound"] = result.hotspot_sasa_bound
        record["lpt_hotspot_sasa_unbound"] = result.hotspot_sasa_unbound
        record["lpt_hotspot_sasa_delta"] = result.hotspot_sasa_delta
        record["lpt_hotspots_missing"] = ",".join(str(x) for x in result.hotspots_missing)
        n_ok += 1

        if progress_every and (idx + 1) % progress_every == 0:
            logger.info(f"  SASA progress: {idx + 1}/{n_to_enrich} designs")

    logger.info(
        f"  SASA enrichment done: ok={n_ok} skipped_no_cif={n_skipped_no_cif} "
        f"failed={n_failed} not_enriched={n_total - n_to_enrich}"
    )
    return records


def write_enriched_csv(records: list[DesignRecord], output_path: Path) -> Path:
    """Write enriched records back to CSV.

    Column order: union of all keys across records, with ``design_id`` and
    ``cif_path`` first, boltzgen-native columns next (preserving their
    original order from the first record), and ``lpt_*`` columns last.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not records:
        output_path.write_text("")
        return output_path

    # Preserve first record's column order for boltzgen-native cols; union
    # the rest in order of first appearance.
    seen: dict[str, None] = {}
    for rec in records:
        for k in rec.keys():
            seen.setdefault(k, None)
    all_cols = list(seen.keys())

    # Reorder: id_cols, then boltzgen-native (non-lpt_, non-id), then lpt_*.
    id_cols = [c for c in ("design_id", "cif_path") if c in seen]
    lpt_cols = [c for c in all_cols if c.startswith("lpt_")]
    native_cols = [c for c in all_cols if c not in id_cols and c not in lpt_cols]
    final_cols = id_cols + native_cols + lpt_cols

    with output_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=final_cols, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            # Convert Path → str at write time; None stays empty.
            out_row = {}
            for col in final_cols:
                val = rec.get(col)
                if isinstance(val, Path):
                    out_row[col] = str(val)
                else:
                    out_row[col] = val
            writer.writerow(out_row)
    logger.info(f"  wrote enriched metrics to {output_path} ({len(records)} rows × {len(final_cols)} cols)")
    return output_path
