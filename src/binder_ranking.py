"""
Filter, score and diversify foundry binder designs.

A sibling of :mod:`src.design_ranking` (which ranks BoltzGen-native columns) for
the foundry track's metric set.  Same shapes — first-failure drop attribution,
z-scored weighted composite, greedy MMR — different gate, and one extra
diversity level.

## The gate is geometric first, confidence second

RF3 templating provably cannot convey a docked pose, so iPTM and iPAE only say
how sure the model is about the interface it *chose*.  On the CD79b campaign the
highest-iPTM refold (0.72) had docked ~30 residues from its design epitope.
Filtering on confidence alone therefore selects confidently mis-docked binders.
``binder_rmsd_dock`` runs first and does the most work: 28 420 refolds -> 877
pass iPTM >= 0.5 -> 116 also pass dock RMSD <= 5 -> 44 pass everything.

ipSAE is a better confidence metric than iPTM (its d0 scales with the number of
confidently-aligned partner residues rather than total complex length) but it is
still a confidence metric and is ranked alongside the geometry, not instead of it.

## Three levels of diversity

Designs arrive in families: `n_seq` MPNN sequences threaded onto one RFD3
backbone.  Sequence MMR alone leaves a top-20 drawn from a handful of backbones,
so a per-backbone cap runs first.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
from loguru import logger

from src.design_ranking import _seq_identity, mmr_select

DesignRecord = dict[str, Any]

_BOOL_TRUE = {"true", "1", "yes"}


# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------

# Hard filters, applied IN ORDER — the order decides which gate a design is
# attributed to when it fails several.  `None` disables a criterion.
DEFAULT_THRESHOLDS: dict[str, Any] = {
    "binder_rmsd_dock_max": 5.0,
    "epitope_recall_min": 0.5,
    # Fraction of declared hotspots contacted; see config.yaml for the
    # calibration behind 0.75 (1.0 rejects designs for hotspots RFD3
    # itself missed — only 49% of backbones contact all 12 of a 12-set).
    "hotspot_engagement_min": 0.75,
    "binder_rmsd_fold_max": 2.0,
    "binder_plddt_min": 0.75,
    # Deliberately OFF, even though EXCELLENT_IPSAE_MIN is now calibrated.
    # ipsae_min is high-precision but low-recall: on 8TAC a > 0.5 cut is 70%
    # precise against the Rosetta-validated set but recovers only 4.2% of it.
    # As a hard gate it would discard ~95% of designs Rosetta likes, so it earns
    # its keep as a ranking weight (the heaviest one) and at most a soft gate
    # around 0.3. Set it explicitly if you want the strict behaviour.
    "ipsae_min_min": None,
    "iptm_min": 0.5,
    "iface_pae_max": 15.0,
    "require_no_clash": True,
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "ipsae_min": 2.0,
    "neg_binder_rmsd_dock": 1.5,
    "binder_plddt": 1.5,
    "iptm": 1.0,
    "neg_iface_pae": 1.0,
    "epitope_recall": 1.0,
    "hotspot_engagement": 0.5,
}

# Columns the weights refer to in "higher is better" form. `neg_` prefixes are
# synthesised from the underlying column at scoring time.
_NEG_PREFIX = "neg_"

DEFAULT_MMR = {"lambda_": 0.5, "seq_identity_cap": 0.7}
DEFAULT_TOP_K = 20
DEFAULT_MAX_PER_BACKBONE = 1

# The "excellent design" bar used by the calibration stage to size a campaign.
#
# Calibrated, not guessed. Measured across two complete campaigns (8TAC: 32,000
# refolds; CD79b: 28,420): the best ipsae_min ever produced was 0.640 and 0.684
# respectively, and the 8TAC campaign's own Rosetta rank-1 design (ddg -40.4,
# dock RMSD 0.57 A) scores 0.615. A 0.7 bar is therefore above anything this
# pipeline has ever made and yields zero hits on both campaigns.
#
# At 0.5, `ipsae_min` selects 8TAC refolds that are Rosetta-validated survivors
# with 70% precision against a 1.41% base rate -- a 50x enrichment -- so the bar
# is meaningful rather than merely reachable.
EXCELLENT_IPSAE_MIN = 0.5


# ----------------------------------------------------------------------
# Coercion
# ----------------------------------------------------------------------

def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _as_bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _BOOL_TRUE


# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------

@dataclass
class FilterStats:
    n_input: int = 0
    n_survivors: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    # How many records pass each criterion ON ITS OWN. This is what tells you
    # whether to scale a campaign up or re-tune the target — a criterion that
    # almost nothing passes alone is a design problem, not a sampling problem.
    passing_alone: dict[str, int] = field(default_factory=dict)

    def record_drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def render(self) -> str:
        lines = [f"input:     {self.n_input:,}", f"survivors: {self.n_survivors:,}", ""]
        if self.dropped:
            lines.append("dropped by first failing criterion:")
            for reason, n in sorted(self.dropped.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {reason:<26} {n:>8,}")
        if self.passing_alone:
            lines += ["", "passing each criterion alone:"]
            for reason, n in self.passing_alone.items():
                pct = 100.0 * n / self.n_input if self.n_input else 0.0
                lines.append(f"  {reason:<26} {n:>8,}  ({pct:5.1f}%)")
        return "\n".join(lines)


def _criteria(thresholds: dict[str, Any]) -> list[tuple[str, Callable[[DesignRecord], bool]]]:
    """
    Build the ordered criterion list.  Order is the attribution order.

    A record missing the column a criterion needs FAILS it — a missing metric is
    not a pass.  The one exception is `require_no_clash`, where a missing flag
    means RF3 did not report one.
    """
    t = thresholds

    def num(col: str, lim: float, cmp: Callable[[float, float], bool]):
        def check(r: DesignRecord) -> bool:
            v = _as_float(r.get(col))
            return v is not None and cmp(v, lim)
        return check

    le = lambda v, l: v <= l   # noqa: E731
    ge = lambda v, l: v >= l   # noqa: E731

    spec: list[tuple[str, str | None, Any, Any]] = [
        ("binder_rmsd_dock", "binder_rmsd_dock_max", le, None),
        ("epitope_recall", "epitope_recall_min", ge, None),
        ("hotspot_engagement", "hotspot_engagement_min", ge, None),
        ("binder_rmsd_fold", "binder_rmsd_fold_max", le, None),
        ("binder_plddt", "binder_plddt_min", ge, None),
        ("ipsae_min", "ipsae_min_min", ge, None),
        ("iptm", "iptm_min", ge, None),
        ("iface_pae", "iface_pae_max", le, None),
    ]
    out: list[tuple[str, Callable[[DesignRecord], bool]]] = []
    for col, key, cmp, _ in spec:
        lim = t.get(key)
        if lim is None:
            continue
        label = f"{col} {'<=' if cmp is le else '>='} {lim:g}"
        out.append((label, num(col, float(lim), cmp)))
    if t.get("require_no_clash", True):
        def no_clash(r: DesignRecord) -> bool:
            severe = _as_float(r.get("clash_severe"))
            return (_as_bool(r.get("has_clash")) is not True
                    and (severe is None or severe == 0))
        out.append(("no clash", no_clash))
    return out


def filter_records(
    records: Sequence[DesignRecord],
    thresholds: dict[str, Any] | None = None,
) -> tuple[list[DesignRecord], FilterStats]:
    """Apply the hard gate, attributing each drop to its FIRST failing criterion."""
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    crit = _criteria(thresholds)
    stats = FilterStats(n_input=len(records))
    stats.passing_alone = {label: 0 for label, _ in crit}

    survivors: list[DesignRecord] = []
    for r in records:
        if r.get("error"):
            stats.record_drop("scoring error")
            continue
        first_fail = None
        for label, check in crit:
            ok = check(r)
            if ok:
                stats.passing_alone[label] += 1
            elif first_fail is None:
                first_fail = label
        if first_fail is None:
            survivors.append(r)
        else:
            stats.record_drop(first_fail)
    stats.n_survivors = len(survivors)
    return survivors, stats


# ----------------------------------------------------------------------
# Composite score
# ----------------------------------------------------------------------

def composite_score(
    survivors: list[DesignRecord],
    weights: dict[str, float] | None = None,
) -> list[str]:
    """
    Stamp ``<col>_z``, ``composite_score`` and ``composite_rank`` on survivors.

    Every weighted column is z-scored across the survivor set, so the weights
    express relative importance rather than depending on each metric's natural
    scale.  A column with no variance contributes 0 rather than a division by
    zero, and a column absent from the data is skipped with a warning instead of
    silently counting as 0 for everyone.
    """
    weights = weights or DEFAULT_WEIGHTS
    if not survivors:
        return []

    used: list[str] = []
    z_by_col: dict[str, np.ndarray] = {}
    for col, w in weights.items():
        src = col[len(_NEG_PREFIX):] if col.startswith(_NEG_PREFIX) else col
        vals = np.array([_as_float(r.get(src)) for r in survivors], dtype=object)
        if any(v is None for v in vals):
            n_missing = sum(1 for v in vals if v is None)
            if n_missing == len(vals):
                logger.warning(
                    f"ranking weight {col!r} refers to column {src!r} which is "
                    f"absent from every record — dropping it from the composite"
                )
                continue
            logger.warning(
                f"{n_missing}/{len(vals)} records missing {src!r}; "
                f"substituting the column mean for those"
            )
        numeric = np.array([np.nan if v is None else float(v) for v in vals])
        mean = np.nanmean(numeric)
        numeric = np.where(np.isnan(numeric), mean, numeric)
        if col.startswith(_NEG_PREFIX):
            numeric = -numeric
        std = numeric.std()
        z = np.zeros_like(numeric) if std < 1e-12 else (numeric - numeric.mean()) / std
        z_by_col[col] = z
        used.append(col)

    total = np.zeros(len(survivors))
    for col in used:
        total += weights[col] * z_by_col[col]
        for rec, zv in zip(survivors, z_by_col[col]):
            rec[f"{col}_z"] = round(float(zv), 4)
    for rec, sc in zip(survivors, total):
        rec["composite_score"] = round(float(sc), 4)
    for rank, rec in enumerate(sorted(survivors, key=lambda r: -r["composite_score"]), 1):
        rec["composite_rank"] = rank
    return used


# ----------------------------------------------------------------------
# Diversity
# ----------------------------------------------------------------------

def cap_per_backbone(
    survivors: list[DesignRecord],
    max_per_backbone: int = DEFAULT_MAX_PER_BACKBONE,
    family_column: str = "design_family",
) -> list[DesignRecord]:
    """
    Keep at most `max_per_backbone` designs per RFD3 backbone.

    The `n_seq` MPNN sequences threaded onto one backbone are highly correlated:
    same fold, same pose, near-identical geometry columns.  Without this cap a
    top-20 is typically drawn from ~5 distinct backbones, and sequence MMR cannot
    fix it because those sequences genuinely differ.
    """
    if max_per_backbone <= 0:
        return survivors
    seen: dict[str, int] = {}
    kept: list[DesignRecord] = []
    for rec in sorted(survivors, key=lambda r: r.get("composite_rank", 10**9)):
        fam = str(rec.get(family_column) or rec.get("name", ""))
        n = seen.get(fam, 0)
        if n >= max_per_backbone:
            continue
        seen[fam] = n + 1
        rec["family_rank"] = n + 1
        kept.append(rec)
    logger.info(
        f"backbone cap: {len(kept):,} of {len(survivors):,} survivors kept "
        f"({len(seen):,} distinct backbones, max {max_per_backbone} each)"
    )
    return kept


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

@dataclass
class RankingResult:
    survivors: list[DesignRecord]
    top_k: list[DesignRecord]
    filter_stats: FilterStats
    composite_columns: list[str]
    n_backbones: int


def rank_designs(
    records: Sequence[DesignRecord],
    *,
    thresholds: dict[str, Any] | None = None,
    weights: dict[str, float] | None = None,
    mmr: dict[str, float] | None = None,
    top_k: int = DEFAULT_TOP_K,
    max_per_backbone: int = DEFAULT_MAX_PER_BACKBONE,
) -> RankingResult:
    """filter -> composite -> backbone cap -> sequence MMR."""
    mmr = {**DEFAULT_MMR, **(mmr or {})}
    survivors, stats = filter_records(records, thresholds)
    logger.info(f"filter: {stats.n_survivors:,}/{stats.n_input:,} survive")
    if not survivors:
        return RankingResult([], [], stats, [], 0)

    used = composite_score(survivors, weights)
    capped = cap_per_backbone(survivors, max_per_backbone)
    n_backbones = len({str(r.get("design_family")) for r in survivors})

    # Re-rank within the capped set so MMR's rank-1 seed is the capped best.
    for rank, rec in enumerate(sorted(capped, key=lambda r: -r["composite_score"]), 1):
        rec["composite_rank"] = rank

    picks = mmr_select(
        capped, top_k=top_k, lambda_=float(mmr["lambda_"]),
        seq_identity_cap=float(mmr["seq_identity_cap"]),
        sequence_column="binder_seq",
    )
    return RankingResult(survivors, picks, stats, used, n_backbones)


def read_scores(path: Path) -> list[DesignRecord]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_ranking_outputs(result: RankingResult, out_dir: Path) -> dict[str, Path]:
    """Write ranked.csv / top_k.csv / filter_stats.txt."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def _dump(rows: list[DesignRecord], name: str) -> Path:
        p = out_dir / name
        if not rows:
            p.write_text("", encoding="utf-8")
            return p
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return p

    paths["ranked"] = _dump(
        sorted(result.survivors, key=lambda r: r.get("composite_rank", 10**9)),
        "ranked.csv")
    paths["top_k"] = _dump(result.top_k, "top_k.csv")
    stats_path = out_dir / "filter_stats.txt"
    stats_path.write_text(
        result.filter_stats.render()
        + f"\n\ndistinct backbones among survivors: {result.n_backbones:,}"
        + f"\ncomposite columns: {', '.join(result.composite_columns)}\n",
        encoding="utf-8")
    paths["filter_stats"] = stats_path
    logger.info(f"Ranking outputs -> {out_dir}")
    return paths


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scores_csv", type=Path)
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("."))
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--max-per-backbone", type=int, default=DEFAULT_MAX_PER_BACKBONE)
    ap.add_argument("--ipsae-min", type=float, default=None,
                    help="Enable the ipsae_min gate at this value (off by default; "
                         "it is uncalibrated until you have a calibration run).")
    args = ap.parse_args(argv)

    thresholds = dict(DEFAULT_THRESHOLDS)
    if args.ipsae_min is not None:
        thresholds["ipsae_min_min"] = args.ipsae_min
    result = rank_designs(read_scores(args.scores_csv), thresholds=thresholds,
                          top_k=args.top_k, max_per_backbone=args.max_per_backbone)
    write_ranking_outputs(result, args.out_dir)
    print(result.filter_stats.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
