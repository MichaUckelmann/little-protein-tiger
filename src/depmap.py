"""
DepMap CRISPR gene-effect loader and pairwise / per-gene correlation helpers.

Sourced from ``data/depmap/CRISPRGeneEffect.csv`` (Broad Institute, Chronos
algorithm). Columns follow the pattern ``"GENE_SYMBOL (ENTREZ_ID)"``;
this module strips them to bare gene symbol at load time.

Sign convention reminder for the resolver / skill prompt:
  - More-negative score = gene is more essential in that cell line (knockout
    reduces fitness).
  - Two genes that are co-essential across cell lines correlate POSITIVELY.
  - The correlation is between essentiality patterns, NOT between expression
    levels and NOT a direct measure of physical interaction.

Lazy singleton: the first call to any helper triggers the full CSV read
(~5 s with pyarrow, ~90 MB float32 in numpy). Subsequent calls reuse it.

The implementation deliberately uses pyarrow + numpy and avoids adding a
pandas dependency — pyarrow is already in the venv via lancedb, and
numpy-only correlation is fast enough for interactive MCP queries.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.csv as pacsv
from loguru import logger


_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "depmap" / "CRISPRGeneEffect.csv"

# Module-level singletons. None until first access.
_DATA: np.ndarray | None = None  # shape (n_cell_lines, n_genes), float32, NaN where missing
_GENE_TO_IDX: dict[str, int] | None = None  # uppercase gene symbol → column index
_GENE_NAMES: list[str] | None = None  # ordered list (column index → gene symbol)
_CELL_LINES: list[str] | None = None  # row index → cell-line ID
_DATA_PATH: Path | None = None

# Header column pattern: "ABCD (12345)"
_COL_PATTERN = re.compile(r"^(.+?)\s*\((\d+)\)$")


def _ensure_loaded(path: Path | None = None) -> None:
    """Load the DepMap CSV the first time anything in this module is called.

    Populates the module-level singletons. Subsequent calls are no-ops if
    the same path was already loaded.
    """
    global _DATA, _GENE_TO_IDX, _GENE_NAMES, _CELL_LINES, _DATA_PATH
    target = Path(path) if path else _DEFAULT_PATH
    if _DATA is not None and _DATA_PATH == target:
        return

    logger.info(f"Loading DepMap CRISPR data from {target} ...")
    if not target.exists():
        raise FileNotFoundError(
            f"DepMap CSV not found at {target}. This ~420 MB file is optional "
            f"— only the wildcard-expert DepMap tools need it — and DepMap's "
            f"portal cannot be scripted, so download CRISPRGeneEffect.csv by "
            f"hand from https://depmap.org/portal/data_page/?tab=allData and "
            f"save it there. Run `python scripts/fetch_reference_data.py --check` to see all reference-data status.")

    # pyarrow.csv handles the 440 MB file in ~3 s and respects float→NaN
    # encoding for empty cells out of the box. We materialise the columns
    # into a single contiguous float32 numpy array for fast slicing.
    table = pacsv.read_csv(target)
    schema = table.schema
    col_names = list(schema.names)

    if not col_names:
        raise RuntimeError(f"Empty CSV at {target}")

    # First column is cell-line ID (e.g. ACH-000029). All others are gene scores.
    cell_lines = table.column(0).to_pylist()
    gene_columns_raw = col_names[1:]

    # Strip "GENE (ENTREZ_ID)" → "GENE", uppercase, dedupe.
    gene_names: list[str] = []
    seen: dict[str, int] = {}
    for c in gene_columns_raw:
        m = _COL_PATTERN.match(c)
        sym = (m.group(1) if m else c).strip().upper()
        if sym in seen:
            seen[sym] += 1
            sym = f"{sym}__dup{seen[sym]}"
            logger.warning(f"Duplicate gene symbol in DepMap CSV: {sym}")
        else:
            seen[sym] = 0
        gene_names.append(sym)

    # Build the float32 matrix column-by-column. Pyarrow gives us numpy
    # arrays already; we just stack them. Memory: 18,531 × 1,209 × 4 = ~90 MB.
    n_rows = len(cell_lines)
    n_cols = len(gene_names)
    data = np.empty((n_rows, n_cols), dtype=np.float32)
    for i, raw_col in enumerate(gene_columns_raw):
        # Float arrow column → numpy with NaN for nulls. zero_copy_only=False
        # because pyarrow will need to allocate for the null fill.
        arr = table.column(raw_col).to_numpy(zero_copy_only=False).astype(np.float32, copy=False)
        data[:, i] = arr

    gene_to_idx = {sym: i for i, sym in enumerate(gene_names) if "__dup" not in sym}

    _DATA = data
    _GENE_TO_IDX = gene_to_idx
    _GENE_NAMES = gene_names
    _CELL_LINES = cell_lines
    _DATA_PATH = target

    mb = data.nbytes / 1e6
    logger.info(
        f"DepMap loaded: {n_cols:,} genes × {n_rows:,} cell lines ({mb:.0f} MB float32). "
        f"Distinct symbols indexed: {len(gene_to_idx):,}."
    )


def _column(gene_upper: str) -> np.ndarray | None:
    """Return the float32 column for a gene symbol, or None if absent."""
    assert _DATA is not None and _GENE_TO_IDX is not None
    idx = _GENE_TO_IDX.get(gene_upper)
    if idx is None:
        return None
    return _DATA[:, idx]


# ---------------------------------------------------------------------------
# Pairwise correlation
# ---------------------------------------------------------------------------


def correlation_for_pair(
    gene_a: str,
    gene_b: str,
    min_n: int = 100,
    path: Path | None = None,
) -> dict[str, Any]:
    """Pearson correlation between essentiality profiles of two genes.

    Returns a dict either of:
      - resolved: ``{"available": True, "r": float, "n": int, "gene_a": str, "gene_b": str}``
      - unavailable: ``{"available": False, "reason": str, ...}``

    Reasons covered:
      - ``"missing_a"`` / ``"missing_b"`` — gene not in DepMap.
      - ``"low_overlap"`` — fewer than ``min_n`` cell lines have non-NaN
        scores for both genes; correlation is statistically too noisy.
      - ``"self_pair"`` — caller passed the same gene twice.
    """
    _ensure_loaded(path)
    a = (gene_a or "").strip().upper()
    b = (gene_b or "").strip().upper()

    if not a or not b:
        return {"available": False, "reason": "empty_gene_name", "gene_a": gene_a, "gene_b": gene_b}
    if a == b:
        return {"available": False, "reason": "self_pair", "gene_a": a, "gene_b": b}

    sa = _column(a)
    sb = _column(b)
    if sa is None:
        return {"available": False, "reason": "missing_a", "gene_a": a, "gene_b": b}
    if sb is None:
        return {"available": False, "reason": "missing_b", "gene_a": a, "gene_b": b}

    mask = ~np.isnan(sa) & ~np.isnan(sb)
    n = int(mask.sum())
    if n < min_n:
        return {
            "available": False, "reason": "low_overlap",
            "gene_a": a, "gene_b": b, "n": n, "min_n_required": min_n,
        }

    r = float(np.corrcoef(sa[mask], sb[mask])[0, 1])
    return {"available": True, "r": round(r, 4), "n": n, "gene_a": a, "gene_b": b}


# ---------------------------------------------------------------------------
# Family-aware pairwise: tightest |r| over candidate cross-product
# ---------------------------------------------------------------------------


def correlation_for_pair_family(
    candidates_a: list[str],
    candidates_b: list[str],
    min_n: int = 100,
    path: Path | None = None,
) -> dict[str, Any]:
    """Tightest |r| across all (a × b) candidate pairs.

    For family-head resolutions (e.g. ``AKT`` → AKT1/AKT2/AKT3) we evaluate
    every cross-product against the partner's candidates and report the
    pair with the largest ``|r|`` that meets ``min_n``.

    Returns the same shape as ``correlation_for_pair`` plus:
      - ``evaluated_pairs`` — count of (a, b) pairs that cleared ``min_n``.
      - ``family_ambiguity`` — True if either side had > 1 candidate.
      - ``best_pair`` — ``(gene_a, gene_b)`` of the chosen pair.
      - ``all_results`` — every available pair's r/n, sorted by |r| desc.
    """
    _ensure_loaded(path)

    a_list = [g.strip().upper() for g in (candidates_a or []) if g and g.strip()]
    b_list = [g.strip().upper() for g in (candidates_b or []) if g and g.strip()]
    if not a_list or not b_list:
        return {
            "available": False, "reason": "empty_gene_name",
            "candidates_a": a_list, "candidates_b": b_list,
        }

    family_ambiguity = len(a_list) > 1 or len(b_list) > 1

    # Both inputs resolved to identical gene-symbol sets → degenerate query
    # (e.g. PD-L1 vs CD274 are the same gene under two names). Return a
    # clear reason rather than the misleading "all_missing".
    set_a = set(a_list)
    set_b = set(b_list)
    if set_a == set_b:
        return {
            "available": False,
            "reason": "same_gene_alias",
            "resolved_symbols": sorted(set_a),
            "candidates_a": a_list,
            "candidates_b": b_list,
            "family_ambiguity": family_ambiguity,
        }

    all_results: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    underpowered_best: dict[str, Any] | None = None

    for a in a_list:
        sa = _column(a)
        if sa is None:
            continue
        for b in b_list:
            if a == b:
                continue
            sb = _column(b)
            if sb is None:
                continue
            mask = ~np.isnan(sa) & ~np.isnan(sb)
            n = int(mask.sum())
            if n == 0:
                continue
            r = float(np.corrcoef(sa[mask], sb[mask])[0, 1])
            entry = {"gene_a": a, "gene_b": b, "r": round(r, 4), "n": n}
            if n >= min_n:
                all_results.append(entry)
                if best is None or abs(r) > abs(best["r"]):
                    best = entry
            else:
                if underpowered_best is None or n > underpowered_best["n"]:
                    underpowered_best = entry

    if best is None:
        if underpowered_best is not None:
            return {
                "available": False, "reason": "low_overlap",
                "best_attempted_pair": (underpowered_best["gene_a"], underpowered_best["gene_b"]),
                "best_attempted_n": underpowered_best["n"],
                "min_n_required": min_n,
                "evaluated_pairs": 0,
                "family_ambiguity": family_ambiguity,
                "candidates_a": a_list, "candidates_b": b_list,
            }
        return {
            "available": False, "reason": "all_missing",
            "evaluated_pairs": 0,
            "family_ambiguity": family_ambiguity,
            "candidates_a": a_list, "candidates_b": b_list,
        }

    all_results.sort(key=lambda e: -abs(e["r"]))
    return {
        "available": True,
        "r": best["r"], "n": best["n"],
        "best_pair": (best["gene_a"], best["gene_b"]),
        "evaluated_pairs": len(all_results),
        "family_ambiguity": family_ambiguity,
        "candidates_a": a_list, "candidates_b": b_list,
        "all_results": all_results,
    }


# ---------------------------------------------------------------------------
# Per-gene neighbourhood: top-k co-correlated genes (vectorised)
# ---------------------------------------------------------------------------


def correlations_for_gene(
    gene: str,
    top_k: int = 25,
    min_abs_r: float = 0.2,
    min_n: int = 100,
    path: Path | None = None,
) -> dict[str, Any]:
    """Top-k genes most |Pearson|-correlated with ``gene`` across cell lines.

    Vectorised: ~0.5 s on the full 18k-gene × 1.2k-cell-line matrix.

    Algorithm: replace NaNs with the column mean (zero after centering),
    standardise each column, then dot-product the target column against
    the rest — that's the Pearson correlation as long as the mask of
    valid rows is the same for all gene-pairs. We additionally compute
    a per-pair ``n`` (intersection of non-NaN masks) and reject pairs
    below ``min_n``.
    """
    _ensure_loaded(path)
    g = (gene or "").strip().upper()
    if not g:
        return {"available": False, "reason": "empty_gene_name", "gene": gene}
    if _GENE_TO_IDX is None or _DATA is None:  # narrowing for type checker
        return {"available": False, "reason": "load_failed", "gene": g}
    idx = _GENE_TO_IDX.get(g)
    if idx is None:
        return {"available": False, "reason": "missing_gene", "gene": g}

    target = _DATA[:, idx]
    target_mask = ~np.isnan(target)

    # Standardise the target on its non-NaN subset.
    t_vals = target[target_mask]
    t_mean = t_vals.mean()
    t_std = t_vals.std(ddof=0)
    if t_std == 0 or not np.isfinite(t_std):
        return {"available": False, "reason": "constant_target", "gene": g}

    # For every other gene we need r computed on the pair-wise non-NaN mask.
    # Doing this fully vectorised with per-pair masks is awkward; we instead
    # iterate over genes but compute each pair's stats with vectorised numpy
    # ops. ~18k iterations, each ~10 µs → ~0.5 s total. Acceptable for an
    # interactive tool.
    n_genes = _DATA.shape[1]
    results: list[dict[str, Any]] = []
    target_centered = target - t_mean

    for j in range(n_genes):
        if j == idx:
            continue
        col = _DATA[:, j]
        mask = target_mask & ~np.isnan(col)
        n = int(mask.sum())
        if n < min_n:
            continue
        # Pearson on the masked subset.
        a = target[mask]
        b = col[mask]
        a_mean = a.mean()
        b_mean = b.mean()
        a_dev = a - a_mean
        b_dev = b - b_mean
        denom = np.sqrt((a_dev * a_dev).sum() * (b_dev * b_dev).sum())
        if denom == 0:
            continue
        r = float((a_dev * b_dev).sum() / denom)
        if abs(r) < min_abs_r:
            continue
        results.append({"gene": _GENE_NAMES[j], "r": round(r, 4), "n": n})

    results.sort(key=lambda e: -abs(e["r"]))
    results = results[:top_k]

    return {
        "available": True,
        "gene": g,
        "result_count": len(results),
        "min_abs_r": min_abs_r,
        "min_n": min_n,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Bulk helper used by graph-edge enrichment (sprint 3 export_subgraph extension)
# ---------------------------------------------------------------------------


def has_gene(gene: str) -> bool:
    """Cheap membership test — used by the graph enrichment pass to skip
    edges whose endpoints are not in DepMap before doing anything else."""
    _ensure_loaded()
    if _GENE_TO_IDX is None:
        return False
    return (gene or "").strip().upper() in _GENE_TO_IDX
