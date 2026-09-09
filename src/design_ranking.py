"""Rank enriched boltzgen design records into a diversity-aware top-K.

The pipeline:

1. **Hard filters** — drop designs failing any of ``iptm_min``,
   ``ipae_max``, ``hotspot_sasa_delta_min``, or (optionally) boltzgen's
   ``pass_filters`` column. Each drop is logged into :class:`FilterStats`
   with a per-reason counter so callers can surface why the funnel
   narrowed.

2. **Composite score** — survivors are z-scored on each weighted column
   (in "higher is better" form; use boltzgen's ``neg_*`` columns for
   metrics where lower is better). Composite = Σ weight × z. Each
   z-scored column is stamped on the record so users can inspect the
   contributions.

3. **MMR diversity** — greedy Maximal Marginal Relevance over
   ``designed_chain_sequence``. Designs ≥ ``seq_identity_cap`` similar to
   an already-picked design are dropped outright (binder-design's
   pattern); the remaining picks balance composite score against the
   best-similarity-to-any-already-picked penalty.

The shape mirrors binder-design's ``SeedScorer`` but is decoupled from
that repo's membrane-geometry context.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from src.design_metrics import DesignRecord


@dataclass
class FilterStats:
    n_input: int = 0
    n_survivors: int = 0
    # Per-reason drop counts. Keys are short tokens (iptm / ipae / hotspot_sasa / boltzgen_pass / missing_column).
    dropped: dict[str, int] = field(default_factory=dict)

    def record_drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


@dataclass
class RankingResult:
    survivors: list[DesignRecord]
    top_k: list[DesignRecord]
    filter_stats: FilterStats
    composite_columns: list[str]  # the weighted columns used


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

_BOOL_TRUE = {"true", "1", "yes"}


def _as_bool(v: Any) -> bool | None:
    """Coerce CSV-flavoured truthy strings to bool. Returns None for missing/unknown."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in {"false", "0", "no"}:
        return False
    return None


def _as_float(v: Any) -> float | None:
    """Coerce to float, None on missing/unparseable."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def filter_records(
    records: list[DesignRecord],
    *,
    iptm_min: float,
    ipae_max: float,
    hotspot_sasa_delta_min: float,
    require_boltzgen_pass: bool,
    hotspot_sasa_available: bool = True,
) -> tuple[list[DesignRecord], FilterStats]:
    """Apply hard filters. Drops a record on the **first** failure encountered,
    so per-reason counts attribute each drop to one cause (the first one
    tested) rather than double-counting.

    ``hotspot_sasa_available`` says whether the SASA enrichment step actually
    ran. It is False when PyRosetta is unavailable or disabled, and then the
    SASA gate is SKIPPED rather than failed — see the note at its use below.
    """
    stats = FilterStats(n_input=len(records))
    survivors: list[DesignRecord] = []

    for rec in records:
        if require_boltzgen_pass:
            passed = _as_bool(rec.get("pass_filters"))
            if passed is not True:
                stats.record_drop("boltzgen_pass")
                continue

        iptm = _as_float(rec.get("design_to_target_iptm"))
        if iptm is None:
            stats.record_drop("missing_iptm")
            continue
        if iptm < iptm_min:
            stats.record_drop("iptm")
            continue

        ipae = _as_float(rec.get("min_design_to_target_pae"))
        if ipae is None:
            stats.record_drop("missing_ipae")
            continue
        if ipae > ipae_max:
            stats.record_drop("ipae")
            continue

        # The SASA gate applies only if enrichment actually ran. When
        # PyRosetta is absent or disabled, NOTHING has a SASA value, and
        # treating that as a failure silently dropped every single design and
        # then blamed the designs in 05_analysis.md. Skipping the gate is the
        # honest behaviour: one fewer filter, reported as such.
        if hotspot_sasa_available:
            sasa_delta = _as_float(rec.get("lpt_hotspot_sasa_delta"))
            if sasa_delta is None:
                # Enrichment ran but capped to top-K: a design it never scored
                # has not passed this filter, so it is still a drop.
                stats.record_drop("missing_hotspot_sasa")
                continue
            if sasa_delta < hotspot_sasa_delta_min:
                stats.record_drop("hotspot_sasa")
                continue

        survivors.append(rec)

    stats.n_survivors = len(survivors)
    return survivors, stats


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def composite_score(
    survivors: list[DesignRecord],
    weights: dict[str, float],
) -> list[str]:
    """Compute z-scored composite score per record. Mutates records in place.

    Each weight key must exist on the record (boltzgen-native or ``lpt_*``)
    and be in "higher is better" form. Missing values on individual records
    are replaced with that column's mean (z = 0), so a design with one
    missing metric gets penalised only via the metrics it has.

    Returns the list of columns actually used (subset of ``weights.keys()``
    whose values could be parsed on at least one record).

    On each record, adds:
      * ``<col>_z`` for each metric — the z-score contribution.
      * ``composite_score`` — Σ weight × z.
      * ``composite_rank`` — 1-indexed rank by descending ``composite_score``.
    """
    if not survivors:
        return []

    used_cols: list[str] = []
    for col in weights:
        values = [_as_float(r.get(col)) for r in survivors]
        if all(v is None for v in values):
            logger.warning(
                f"  ranking: column {col!r} has no parseable values across "
                f"{len(survivors)} survivors — skipping (weight={weights[col]})"
            )
            continue
        used_cols.append(col)

    if not used_cols:
        raise ValueError(
            "composite_score: none of the configured ranking weights have "
            "parseable values on the survivor set"
        )

    # Build matrix; impute missing with column mean (post-mean computation).
    n = len(survivors)
    matrix = np.zeros((n, len(used_cols)))
    for j, col in enumerate(used_cols):
        raw = np.array([_as_float(r.get(col)) for r in survivors], dtype=object)
        mask = np.array([v is None for v in raw])
        nums = np.array([0.0 if v is None else float(v) for v in raw], dtype=float)
        if mask.all():
            continue  # column dropped above, shouldn't reach here
        mean_present = float(nums[~mask].mean())
        nums[mask] = mean_present
        matrix[:, j] = nums

    means = matrix.mean(axis=0)
    stds = matrix.std(axis=0)
    # Guard against zero variance — keep z=0 for those columns rather than
    # dividing by zero. A degenerate column contributes nothing to ranking.
    safe_stds = np.where(stds < 1e-9, 1.0, stds)
    z = (matrix - means) / safe_stds
    z = np.where(stds < 1e-9, 0.0, z)

    weight_vec = np.array([float(weights[c]) for c in used_cols])
    composite = z @ weight_vec

    # Stamp per-column z + composite onto each record.
    for i, rec in enumerate(survivors):
        for j, col in enumerate(used_cols):
            rec[f"{col}_z"] = float(z[i, j])
        rec["composite_score"] = float(composite[i])

    # Rank by composite, descending. Ties broken by input order.
    sorted_idx = sorted(range(n), key=lambda i: -survivors[i]["composite_score"])
    for new_rank, old_i in enumerate(sorted_idx, start=1):
        survivors[old_i]["composite_rank"] = new_rank

    return used_cols


# ---------------------------------------------------------------------------
# MMR diversity selection
# ---------------------------------------------------------------------------

def _seq_identity(a: str, b: str) -> float:
    """SequenceMatcher.ratio() — robust to length differences, [0, 1]."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def mmr_select(
    survivors: list[DesignRecord],
    *,
    top_k: int,
    lambda_: float,
    seq_identity_cap: float,
    sequence_column: str = "designed_chain_sequence",
) -> list[DesignRecord]:
    """Greedy MMR over composite-ranked survivors.

    Survivors must already have ``composite_score`` and ``composite_rank``
    stamped on them (i.e. :func:`composite_score` was called first).

    Algorithm:
      1. Sort by ``composite_rank``.
      2. Seed picks with the rank-1 design.
      3. For each remaining candidate, compute max similarity to any
         already-picked design. Drop candidates with similarity ≥
         ``seq_identity_cap``.
      4. Score remaining candidates with
         ``lambda_ * z_composite - (1 - lambda_) * max_similarity``,
         where ``z_composite`` is the composite score linearly rescaled to
         [0, 1] across survivors.
      5. Pick the best, repeat until ``top_k`` or candidates exhausted.

    Each pick gets ``mmr_rank`` (1-indexed) and ``mmr_max_similarity`` stamped.
    """
    if not survivors or top_k <= 0:
        return []

    ordered = sorted(survivors, key=lambda r: r["composite_rank"])

    # Rescale composite to [0, 1] for stable interplay with similarity term.
    scores = np.array([r["composite_score"] for r in ordered])
    score_range = scores.max() - scores.min()
    if score_range < 1e-9:
        score_normalised = np.zeros_like(scores)
    else:
        score_normalised = (scores - scores.min()) / score_range

    sequences = [str(r.get(sequence_column, "") or "") for r in ordered]

    picked: list[int] = [0]
    ordered[0]["mmr_rank"] = 1
    ordered[0]["mmr_max_similarity"] = 0.0

    while len(picked) < top_k:
        best_mmr = -np.inf
        best_idx = -1
        best_sim = 0.0
        for i in range(len(ordered)):
            if i in picked:
                continue
            seq_i = sequences[i]
            max_sim = 0.0
            for j in picked:
                sim = _seq_identity(seq_i, sequences[j])
                if sim > max_sim:
                    max_sim = sim
            if max_sim >= seq_identity_cap:
                continue  # too similar to an already-picked design — drop
            mmr = lambda_ * score_normalised[i] - (1.0 - lambda_) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
                best_sim = max_sim
        if best_idx < 0:
            break  # no eligible candidates remain
        picked.append(best_idx)
        ordered[best_idx]["mmr_rank"] = len(picked)
        ordered[best_idx]["mmr_max_similarity"] = float(best_sim)

    return [ordered[i] for i in picked]


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def rank_designs(
    records: list[DesignRecord],
    *,
    thresholds: dict[str, Any],
    weights: dict[str, float],
    mmr: dict[str, Any],
    top_k: int,
    hotspot_sasa_available: bool = True,
) -> RankingResult:
    """Apply filters → composite score → MMR. Single entry point for stage 5.

    Expected ``thresholds`` keys: ``iptm_min``, ``ipae_max``,
    ``hotspot_sasa_delta_min``, ``require_boltzgen_pass``.

    Expected ``mmr`` keys: ``lambda_``, ``seq_identity_cap``.
    """
    survivors, stats = filter_records(
        records,
        iptm_min=float(thresholds["iptm_min"]),
        ipae_max=float(thresholds["ipae_max"]),
        hotspot_sasa_delta_min=float(thresholds["hotspot_sasa_delta_min"]),
        require_boltzgen_pass=bool(thresholds.get("require_boltzgen_pass", True)),
        hotspot_sasa_available=hotspot_sasa_available,
    )
    logger.info(
        f"  ranking: {stats.n_survivors}/{stats.n_input} survived hard filters "
        f"({dict(sorted(stats.dropped.items()))})"
    )

    if not survivors:
        logger.warning("  ranking: no survivors — top-K is empty")
        return RankingResult(survivors=[], top_k=[], filter_stats=stats, composite_columns=[])

    used_cols = composite_score(survivors, weights)

    selected = mmr_select(
        survivors,
        top_k=top_k,
        lambda_=float(mmr["lambda_"]),
        seq_identity_cap=float(mmr["seq_identity_cap"]),
    )
    logger.info(f"  ranking: MMR picked top {len(selected)} (target {top_k})")

    return RankingResult(
        survivors=survivors,
        top_k=selected,
        filter_stats=stats,
        composite_columns=used_cols,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_ranking_outputs(
    result: RankingResult,
    output_dir: Path,
    *,
    ranked_filename: str = "ranked.csv",
    top_k_filename: str = "top_k.csv",
    stats_filename: str = "filter_stats.txt",
) -> tuple[Path, Path, Path]:
    """Write three artifacts: all survivors (ranked), MMR top-K, filter stats.

    Returns ``(ranked_path, top_k_path, stats_path)``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ranked_path = output_dir / ranked_filename
    top_k_path = output_dir / top_k_filename
    stats_path = output_dir / stats_filename

    _write_records_csv(sorted(result.survivors, key=lambda r: r["composite_rank"]), ranked_path)
    _write_records_csv(result.top_k, top_k_path)

    stats = result.filter_stats
    lines = [
        "# Filter stats (chunk 3 / stage 5)",
        f"input designs:     {stats.n_input}",
        f"survivors:         {stats.n_survivors}",
        f"top_k selected:    {len(result.top_k)}",
        "",
        "## Drop reasons",
    ]
    if stats.dropped:
        for reason, count in sorted(stats.dropped.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason}: {count}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"## Composite columns used: {result.composite_columns}")
    stats_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    logger.info(f"  wrote ranking outputs to {output_dir}/")
    return ranked_path, top_k_path, stats_path


def _write_records_csv(records: list[DesignRecord], path: Path) -> None:
    """CSV writer mirroring design_metrics.write_enriched_csv conventions.

    Column order: id cols → ranking cols (composite/mmr) → boltzgen-native →
    lpt_* → per-metric z-scores. Ranking outputs lead with the score columns
    so the file is human-skimmable.
    """
    if not records:
        path.write_text("")
        return
    seen: dict[str, None] = {}
    for rec in records:
        for k in rec.keys():
            seen.setdefault(k, None)
    all_cols = list(seen.keys())

    id_cols = [c for c in ("composite_rank", "mmr_rank", "design_id", "cif_path") if c in seen]
    rank_cols = [c for c in ("composite_score", "mmr_max_similarity") if c in seen]
    z_cols = [c for c in all_cols if c.endswith("_z")]
    lpt_cols = [c for c in all_cols if c.startswith("lpt_")]
    used_already = set(id_cols + rank_cols + z_cols + lpt_cols)
    native_cols = [c for c in all_cols if c not in used_already]

    final_cols = id_cols + rank_cols + native_cols + lpt_cols + z_cols

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=final_cols, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = {}
            for col in final_cols:
                v = rec.get(col)
                if isinstance(v, Path):
                    row[col] = str(v)
                else:
                    row[col] = v
            writer.writerow(row)
