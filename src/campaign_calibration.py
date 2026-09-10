"""
Size a production campaign from a small calibration run.

A production foundry campaign is a multi-day, ~15-90 GB commitment.  Deciding its
size by guessing is how you spend four days to learn the target was wrong.  This
module answers one question from a ~1 200-refold sample:

    "How large must this campaign be to get N designs at ipSAE_min > 0.7?"

and answers it with error bars, because at realistic hit rates a 1 200-refold
sample sees only a handful of hits.  On the reference CD79b campaign the
all-gates pass rate was 44/28 420 = 0.15 %, so a 1 200-refold calibration expects
**about two hits**.  A point estimate off two observations is not a number you
should spend four days on; the Wilson interval is.

Three things keep this honest:

1. **Interval, not point estimate.**  Required scale is reported as a range and
   the cost is sized against the pessimistic bound.
2. **Zero hits is a real outcome.**  With k = 0 there is no rate to extrapolate
   from, so we report the rule-of-three one-sided bound (p < 3/n) and say
   "at least this big" rather than inventing a finite answer.
3. **The backbone is the scaling unit.**  The `n_seq` MPNN sequences from one
   RFD3 backbone are correlated, so 1 200 refolds is nothing like 1 200
   independent trials.  `n_batches` — the knob you actually turn — controls
   backbones, so the backbone-level rate is what sizes the run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from typing import Any, Sequence

from loguru import logger

from src.binder_ranking import (
    DEFAULT_THRESHOLDS, EXCELLENT_IPSAE_MIN, FilterStats, _as_float, filter_records,
)
# ONE anchor and ONE size law for the whole codebase. `foundry_runner` owns
# both because it is where they were fitted (four campaigns, timed from
# `rf3_out` directory mtimes); importing rather than restating them is what
# stops the gate and the planner drifting apart again — see
# SEC_PER_RF3_REFOLD below. No cycle: `foundry_runner` imports only
# env_config / foundry_spec / job_registry.
from src.foundry_runner import (
    BYTES_PER_REFOLD, REF_TOKENS, SEC_PER_RF3_REFOLD, refold_bytes,
    rf3_seconds_per_refold,
)

# Measured on the RTX PRO 4500 Blackwell (32 GB) for a ~175-token complex.
# Re-measure per target: RF3 attention is O(N^2) in tokens.
SEC_PER_RFD3_DESIGN = 5.4
SEC_PER_MPNN_SEQ = 0.36
#: `SEC_PER_RF3_REFOLD` is re-exported from `foundry_runner` and is an ANCHOR
#: at `REF_TOKENS` (195), not a flat rate: RF3 refold cost scales with complex
#: size as `(tokens/195)**1.62`.
#:
#: This module used to hold its own flat 8.4 while the planner had long since
#: stopped costing that way, and it is the GATE's own budget check, so the
#: error was not cosmetic. On the MASH/TEAD4 campaign the measured rate was
#: 20.6 s at ~300 tokens: the gate costed 22,197 refolds at 63 GPU-h and
#: declared it "inside the 120 h budget" while `plan_campaign` costed the same
#: 22,184 refolds at 138 GPU-h — outside it. SCALE_UP, and an auto-raised
#: excellence bar, were both decided on a number 2.45x too low.
#:
#: `calibrate()` now derives the rate itself when given `n_tokens`, and warns
#: when it has to fall back to the bare anchor. Callers with a MEASURED rate
#: (`foundry_runner.sec_per_refold_observed`, or an earlier stage of the same
#: campaign) should still pass `sec_per_rf3_refold` — it beats both.

Z95 = 1.959963984540054

# Below this many hits the Wilson interval is so wide that extrapolating from it
# carries no information; report a bound and say so rather than a number.
MIN_HITS_FOR_ESTIMATE = 5

# A budget overshoot inside this factor is noise next to the width of the
# intervals involved, and must not flip an otherwise-viable plan to STOP.
BUDGET_SLACK = 1.5


# ----------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------

def wilson_interval(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """
    Wilson score interval for a binomial proportion.

    Preferred over the normal approximation precisely because it stays sane in
    the regime we are in: tiny k, large n, p near 0.  The normal interval would
    return a negative lower bound and a uselessly narrow width at k = 2.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rule_of_three(n: int, confidence: float = 0.95) -> float:
    """
    One-sided upper bound on p when zero events were observed in n trials.

    p < 3/n at 95 % confidence.  This is the ONLY defensible statement after
    k = 0 — it bounds how good the rate could plausibly be, which turns into a
    LOWER bound on the campaign size, never a finite estimate of it.
    """
    if n <= 0:
        return 1.0
    return -math.log(1.0 - confidence) / n


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------

@dataclass
class RateEstimate:
    """A hit rate with its interval, at one unit of observation."""

    unit: str                 # "refold" | "backbone"
    k: int
    n: int
    p_hat: float
    p_low: float
    p_high: float
    zero_hits: bool = False

    @property
    def p_pessimistic(self) -> float:
        """The rate to size against: the low end of the interval."""
        return self.p_low


@dataclass
class ScaleEstimate:
    """What it costs to reach `target_designs`, at one rate."""

    basis: str
    required_backbones: float | None
    required_refolds: float | None
    est_gpu_hours: float | None
    est_disk_gb: float | None
    is_lower_bound: bool = False


@dataclass
class CalibrationResult:
    target_designs: int
    excellence_bar: str
    success_metric: str
    n_refolds_scored: int
    n_backbones_scored: int
    prefilter_rate: float
    n_seq: int
    refold_rate: RateEstimate
    backbone_rate: RateEstimate
    central: ScaleEstimate
    pessimistic: ScaleEstimate
    softer_rates: dict[str, int] = field(default_factory=dict)
    filter_stats_text: str = ""
    verdict: str = "ITERATE"
    verdict_reason: str = ""
    affordable_designs: int | None = None
    # How close anything actually got. "0 hits" alone cannot distinguish
    # "narrowly missed the bar" from "nowhere near it", and those imply
    # completely different next moves.
    best_ipsae_min_survivor: float = 0.0
    best_ipsae_min_overall: float = 0.0
    # Strictest bar this sample has enough hits to estimate at, or None.
    suggested_bar: float | None = None
    # The bar actually asked for (SUCCESS_METRICS default, or caller-supplied).
    # Differs from `bar_raised_to` only when adaptive raising fired.
    requested_bar: float = 0.0
    # Set only when adaptive_bar raised the sizing bar above `requested_bar` —
    # the trial supported a stricter bar AND it still fit the budget. None
    # means the campaign is sized at requested_bar, same as before this existed.
    bar_raised_to: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComputeChoice:
    """
    Where a SCALE_UP / SCALE_UP_PARTIAL campaign should actually run.

    `est_gpu_hours` on the pessimistic `ScaleEstimate` is already a
    single-GPU wall-clock estimate (the SEC_PER_* constants above are
    measured on one local GPU) — `local_hours` below IS that number, not a
    recomputation. `cluster_hours` is that same total compute divided across
    `n_gpus_cluster` GPUs running in parallel; it is a rough estimate (a
    cluster refold backend like Protenix has different per-design timing
    than local RF3), not a guarantee — see CLAUDE.md's cluster section for
    what's actually been measured there.
    """

    compute: str  # "local" | "cluster"
    local_hours: float
    cluster_hours: float
    n_gpus_cluster: int
    max_local_hours: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def choose_compute(res: CalibrationResult, *, max_local_hours: float = 48.0,
                   n_gpus_cluster: int = 8) -> ComputeChoice | None:
    """
    Decide local vs. cluster for a SCALE_UP / SCALE_UP_PARTIAL campaign, and
    report the numbers either way.

    Purely a TIME decision: if the pessimistic-bound estimate fits within
    `max_local_hours` on this workstation's one GPU, stay local — that is
    simpler, and cluster GPU-hours are not actually free (compute cost is
    identical, cluster only buys wall-clock parallelism). Otherwise, a
    cluster package should be staged so the campaign completes in a
    reasonable window instead of running for days unattended.

    Returns None when the verdict isn't a scale-up at all (ITERATE/STOP) —
    there is nothing to place on either compute path.
    """
    if res.verdict not in ("SCALE_UP", "SCALE_UP_PARTIAL"):
        return None
    local_hours = res.pessimistic.est_gpu_hours or 0.0
    cluster_hours = local_hours / max(1, n_gpus_cluster)
    compute = "local" if local_hours <= max_local_hours else "cluster"
    return ComputeChoice(
        compute=compute,
        local_hours=round(local_hours, 1),
        cluster_hours=round(cluster_hours, 1),
        n_gpus_cluster=n_gpus_cluster,
        max_local_hours=max_local_hours,
    )


# ----------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------

def _cost(required_refolds: float, *, n_seq: int, prefilter_rate: float,
          sec_per_rf3_refold: float = SEC_PER_RF3_REFOLD,
          bytes_per_refold: float = BYTES_PER_REFOLD
          ) -> tuple[float, float, float]:
    """(required RFD3 designs, GPU hours, disk GB) for a refold count."""
    prefilter_rate = max(prefilter_rate, 1e-6)
    required_designs = required_refolds / max(n_seq, 1) / prefilter_rate
    seconds = (required_designs * SEC_PER_RFD3_DESIGN
               + required_refolds * SEC_PER_MPNN_SEQ
               + required_refolds * (sec_per_rf3_refold or SEC_PER_RF3_REFOLD))
    disk = required_refolds * (bytes_per_refold or BYTES_PER_REFOLD) / 1e9
    return required_designs, seconds / 3600.0, disk


def _scale(rate: RateEstimate, p: float, *, basis: str, target: int,
           n_seq: int, prefilter_rate: float, lower_bound: bool,
           sec_per_rf3_refold: float = SEC_PER_RF3_REFOLD,
           bytes_per_refold: float = BYTES_PER_REFOLD) -> ScaleEstimate:
    if p <= 0:
        return ScaleEstimate(basis, None, None, None, None, is_lower_bound=True)
    if rate.unit == "backbone":
        required_backbones = target / p
        required_refolds = required_backbones * n_seq
    else:
        required_refolds = target / p
        required_backbones = required_refolds / max(n_seq, 1)
    designs, hours, disk = _cost(required_refolds, n_seq=n_seq,
                                 prefilter_rate=prefilter_rate,
                                 sec_per_rf3_refold=sec_per_rf3_refold,
                         bytes_per_refold=bytes_per_refold)
    return ScaleEstimate(
        basis=basis,
        required_backbones=round(designs, 0),
        required_refolds=round(required_refolds, 0),
        est_gpu_hours=round(hours, 1),
        est_disk_gb=round(disk, 1),
        is_lower_bound=lower_bound,
    )


# What counts as an "excellent" design when sizing a campaign.
#
# Measured across two complete campaigns, per 1000 RFD3 backbones:
#
#                                   8TAC    CD79b
#   ipsae_min > 0.5 + gates          2.5      0.1
#   iPTM > 0.7 + gates              13.2      2.0
#
# ipsae_min > 0.5 is a 5-20x rarer event, so a calibration sample large enough to
# measure iPTM is far too small to measure it — the interval blows up and the
# verdict becomes "enlarge the sample" every time. iPTM > 0.7 is the practical
# sizing metric; ipsae_min is still computed on every design and carries the
# heaviest weight in the final ranking.
#
# The gates are NOT optional. Of designs with iPTM > 0.7, only 45% (8TAC) and 8%
# (CD79b) are actually docked on target — sizing on iPTM alone would promise 50
# designs and deliver mostly confident mis-docks.
SUCCESS_METRICS = {
    "iptm": ("iptm", 0.7, 50),
    "ipsae_min": ("ipsae_min", 0.5, 100),
}


def _is_excellent(rec: dict, bar: float, column: str = "ipsae_min") -> bool:
    v = _as_float(rec.get(column))
    return v is not None and v > bar


def calibrate(
    records: Sequence[dict],
    *,
    target_designs: int | None = None,
    excellence_bar: float | None = None,
    success_metric: str = "iptm",
    thresholds: dict[str, Any] | None = None,
    n_seq: int = 4,
    prefilter_rate: float = 0.59,
    sec_per_rf3_refold: float | None = None,
    n_tokens: int | None = None,
    disk_budget_gb: float = 120.0,
    max_campaign_days: float = 5.0,
    adaptive_bar: bool = True,
) -> CalibrationResult:
    """
    Estimate the production scale needed for `target_designs` excellent designs.

    "Excellent" = passes every hard gate AND `success_metric` > `excellence_bar`.
    The metric only decides how the campaign is SIZED; ranking always uses the
    full composite, with ipsae_min weighted heaviest. See SUCCESS_METRICS.

    `adaptive_bar` (on by default): an unusually good target should not be sized
    to the same fixed bar as a marginal one. Before settling on `excellence_bar`,
    walk the same bar ladder `suggested_bar` is drawn from — strictest first —
    and take the strictest rung that both clears `MIN_HITS_FOR_ESTIMATE` hits
    (Wilson interval is not noise) and whose pessimistic-bound cost still fits
    `disk_budget_gb` / `max_campaign_days` at plain (non-slack) budget. This
    reuses the exact same Wilson-CI machinery the base bar is sized with — an
    easy target naturally gets a tighter bar with a small required scale, a hard
    one falls straight through to the unchanged behaviour below, with no
    separate multiplier table anywhere. Never lowers the bar below what was
    asked for; only ever raises it, and only when the raise is affordable.

    Refold-rate precedence, most trustworthy first:

    1. `sec_per_rf3_refold` — a rate this campaign (or an earlier stage of it)
       actually achieved, via `foundry_runner.sec_per_refold_observed`;
    2. `n_tokens` — the complex size, run through the same
       `rf3_seconds_per_refold` size law the planner uses;
    3. the bare `SEC_PER_RF3_REFOLD` anchor, which is only right near
       `REF_TOKENS` (195) and is warned about, because costing a ~300-token
       target at the anchor under-calls it by ~2x.
    """
    if sec_per_rf3_refold is None and n_tokens:
        sec_per_rf3_refold = rf3_seconds_per_refold(n_tokens)
    if not sec_per_rf3_refold:
        logger.warning(
            f"calibrate(): no measured refold rate and no n_tokens — costing at "
            f"the {SEC_PER_RF3_REFOLD} s anchor, which is only right near "
            f"{REF_TOKENS} tokens. A larger complex will be under-costed and "
            f"the SCALE_UP / budget decision made on it is not trustworthy.")
        sec_per_rf3_refold = SEC_PER_RF3_REFOLD
    # Disk scales with the complex too, and unlike the rate there is nothing
    # measured to prefer: `refold_bytes` is deterministic to within 1.6% across
    # the six fitted campaigns, so the size law IS the best estimate. Falling
    # back to the bare anchor needs no warning of its own — the rate warning
    # above already fires on the same missing `n_tokens`.
    bytes_per_refold = refold_bytes(n_tokens)
    if success_metric not in SUCCESS_METRICS:
        raise ValueError(
            f"success_metric must be one of {sorted(SUCCESS_METRICS)}, "
            f"got {success_metric!r}")
    column, default_bar, default_target = SUCCESS_METRICS[success_metric]
    if excellence_bar is None:
        excellence_bar = default_bar
    if target_designs is None:
        target_designs = default_target
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    survivors, stats = filter_records(records, thresholds)

    scored = [r for r in records if not r.get("error")]
    n_refolds = len(scored)
    backbones = {str(r.get("design_family") or r.get("name")) for r in scored}
    n_backbones = len(backbones)

    excellent = [r for r in survivors if _is_excellent(r, excellence_bar, column)]
    k_refold = len(excellent)
    excellent_backbones = {str(r.get("design_family") or r.get("name"))
                           for r in excellent}
    k_backbone = len(excellent_backbones)

    def make_rate(k: int, n: int, unit: str) -> RateEstimate:
        if k == 0:
            hi = rule_of_three(n)
            return RateEstimate(unit, 0, n, 0.0, 0.0, hi, zero_hits=True)
        lo, hi = wilson_interval(k, n)
        return RateEstimate(unit, k, n, k / n, lo, hi)

    refold_rate = make_rate(k_refold, n_refolds, "refold")
    backbone_rate = make_rate(k_backbone, n_backbones, "backbone")

    # Backbone-level is the sizing basis: n_batches controls backbones, and the
    # n_seq sequences per backbone are correlated, not independent trials.
    if backbone_rate.zero_hits:
        # Nothing to extrapolate from. The rule-of-three bound on the RATE
        # becomes a LOWER bound on the required scale.
        central = _scale(backbone_rate, backbone_rate.p_high, basis="rule-of-three",
                         target=target_designs, n_seq=n_seq,
                         prefilter_rate=prefilter_rate,
                         sec_per_rf3_refold=sec_per_rf3_refold,
                         bytes_per_refold=bytes_per_refold, lower_bound=True)
        pessimistic = central
    else:
        central = _scale(backbone_rate, backbone_rate.p_hat, basis="point estimate",
                         target=target_designs, n_seq=n_seq,
                         prefilter_rate=prefilter_rate,
                         sec_per_rf3_refold=sec_per_rf3_refold,
                         bytes_per_refold=bytes_per_refold, lower_bound=False)
        pessimistic = _scale(backbone_rate, backbone_rate.p_pessimistic,
                             basis="Wilson 95% lower bound", target=target_designs,
                             n_seq=n_seq, prefilter_rate=prefilter_rate,
                         sec_per_rf3_refold=sec_per_rf3_refold,
                         bytes_per_refold=bytes_per_refold,
                             lower_bound=False)

    best_ipsae = max(
        (_as_float(r.get(column)) or 0.0 for r in survivors), default=0.0)
    best_ipsae_any = max(
        (_as_float(r.get(column)) or 0.0 for r in scored), default=0.0)

    # A ladder of bars. When the requested bar returns 0 this is what separates
    # "the sample is too small", "the bar is above anything these designs reach"
    # and "nothing works at all" — three situations with three different next
    # moves.
    # iPTM lives high (a real interface is 0.6-0.9); ipsae_min lives low
    # (0.6 is exceptional). One ladder cannot serve both.
    ladder = ([0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85] if column == "iptm"
              else [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
    by_bar = {b: sum(1 for r in survivors if _is_excellent(r, b, column))
              for b in ladder}
    softer = {"survivors (all hard gates)": len(survivors)}
    softer.update({f"{column} > {b:g}": n for b, n in sorted(by_bar.items())})

    # The strictest bar this sample can actually support an estimate at. Below
    # MIN_HITS_FOR_ESTIMATE the interval is so wide the extrapolation is noise.
    supported = [b for b, n in by_bar.items() if n >= MIN_HITS_FOR_ESTIMATE]
    suggested_bar = max(supported) if supported else None

    requested_bar = excellence_bar
    bar_raised_to: float | None = None
    if adaptive_bar and not backbone_rate.zero_hits:
        budget_hours = max_campaign_days * 24.0

        def _fits_plain(s: ScaleEstimate) -> bool:
            return (s.est_disk_gb is not None and s.est_disk_gb <= disk_budget_gb
                    and s.est_gpu_hours is not None and s.est_gpu_hours <= budget_hours)

        # Strictest first: the first rung that is both estimable (enough hits
        # the interval means something) and affordable (fits budget with no
        # slack — this is an upgrade, not a rescue) wins. Only rungs AT OR
        # ABOVE the requested bar are candidates, so this only ever raises the
        # bar, never lowers it below what was asked for.
        for b in sorted((r for r in ladder if r > requested_bar), reverse=True):
            k_b = sum(1 for fam in {str(r.get("design_family") or r.get("name"))
                                    for r in survivors if _is_excellent(r, b, column)})
            if k_b < MIN_HITS_FOR_ESTIMATE:
                continue
            rate_b = make_rate(k_b, n_backbones, "backbone")
            pessimistic_b = _scale(
                rate_b, rate_b.p_pessimistic, basis="Wilson 95% lower bound",
                target=target_designs, n_seq=n_seq, prefilter_rate=prefilter_rate,
                         sec_per_rf3_refold=sec_per_rf3_refold,
                         bytes_per_refold=bytes_per_refold,
                lower_bound=False)
            if _fits_plain(pessimistic_b):
                bar_raised_to = b
                excellence_bar = b
                backbone_rate = rate_b
                central = _scale(rate_b, rate_b.p_hat, basis="point estimate",
                                 target=target_designs, n_seq=n_seq,
                                 prefilter_rate=prefilter_rate,
                         sec_per_rf3_refold=sec_per_rf3_refold,
                         bytes_per_refold=bytes_per_refold, lower_bound=False)
                pessimistic = pessimistic_b
                break

    result = CalibrationResult(
        target_designs=target_designs,
        excellence_bar=f"all hard gates AND {column} > {excellence_bar:g}",
        success_metric=column,
        n_refolds_scored=n_refolds, n_backbones_scored=n_backbones,
        prefilter_rate=prefilter_rate, n_seq=n_seq,
        refold_rate=refold_rate, backbone_rate=backbone_rate,
        central=central, pessimistic=pessimistic, softer_rates=softer,
        filter_stats_text=stats.render(),
        best_ipsae_min_survivor=round(best_ipsae, 4),
        best_ipsae_min_overall=round(best_ipsae_any, 4),
        suggested_bar=suggested_bar,
        requested_bar=requested_bar,
        bar_raised_to=bar_raised_to,
    )
    _decide(result, disk_budget_gb=disk_budget_gb,
            max_campaign_days=max_campaign_days, stats=stats,
            excellence_bar=excellence_bar, column=column)
    return result


def _decide(res: CalibrationResult, *, disk_budget_gb: float,
            max_campaign_days: float, stats: FilterStats,
            excellence_bar: float, column: str = "ipsae_min") -> None:
    """
    Attach a SCALE_UP / SCALE_UP_PARTIAL / ITERATE / STOP verdict.

    The zero-hit case needs care. A rule-of-three bound is built on an
    OPTIMISTIC rate, so its implied campaign can come out *smaller* than the
    point estimate at a looser bar — which would read as "the stricter bar is
    cheaper", the opposite of the truth. So a zero-hit result is never compared
    against budget as if it were an estimate; it is diagnosed instead, using the
    ladder of softer bars to say which of three situations we are in.
    """
    budget_hours = max_campaign_days * 24.0

    def fits(s: ScaleEstimate, slack: float = 1.0) -> bool:
        return (s.est_disk_gb is not None
                and s.est_disk_gb <= disk_budget_gb * slack
                and s.est_gpu_hours is not None
                and s.est_gpu_hours <= budget_hours * slack)

    # A criterion almost nothing passes ON ITS OWN is a design problem: more
    # sampling cannot fix designs that all miss the epitope.
    systematic = [
        label for label, n in (stats.passing_alone or {}).items()
        if stats.n_input and n / stats.n_input < 0.01
    ]
    sys_note = (f" Also, {', '.join(systematic)} is passed by under 1% of "
                f"designs on its own." if systematic else "")

    if res.backbone_rate.zero_hits:
        nothing_anywhere = all(n == 0 for k, n in res.softer_rates.items()
                               if k.startswith(column))
        best = res.best_ipsae_min_overall

        if nothing_anywhere and systematic:
            res.verdict = "ITERATE"
            res.verdict_reason = (
                f"No design reached even the loosest bar on the ladder (best "
                f"{column} {best:.2f} of any refold). That is a target/hotspot "
                f"problem, not a sampling one — re-tune before spending GPU."
                + sys_note
            )
        elif nothing_anywhere:
            res.verdict = "STOP"
            res.verdict_reason = (
                f"No design reached any bar on the ladder (best {column} "
                f"{best:.2f}) across {res.backbone_rate.n:,} backbones. Nothing "
                f"here is working; scaling would only buy more of the same."
            )
        elif res.suggested_bar is not None:
            res.verdict = "ITERATE"
            res.verdict_reason = (
                f"Nothing cleared {column} > {excellence_bar:g} (best seen: "
                f"{best:.2f}), but softer bars do have hits — so this is the BAR, "
                f"not the target. This sample supports an estimate at "
                f"{column} > {res.suggested_bar:g}; re-run the calibration at "
                f"that bar to size the campaign, or raise the sample if "
                f"{excellence_bar:g} is a hard requirement." + sys_note
            )
        else:
            res.verdict = "ITERATE"
            res.verdict_reason = (
                f"Nothing cleared {column} > {excellence_bar:g} (best seen: "
                f"{best:.2f}) and no bar on the ladder has the "
                f"{MIN_HITS_FOR_ESTIMATE} hits needed for an estimate. The sample "
                f"is too small to size a campaign from — enlarge the calibration "
                f"run before committing." + sys_note
            )
        return

    if res.backbone_rate.k < MIN_HITS_FOR_ESTIMATE:
        # An interval this wide cannot separate viable from hopeless.
        res.verdict = "ITERATE"
        res.verdict_reason = (
            f"Only {res.backbone_rate.k} of {res.backbone_rate.n:,} backbones "
            f"cleared the bar — below the {MIN_HITS_FOR_ESTIMATE} needed for a "
            f"usable estimate, so the 95% interval "
            f"({res.backbone_rate.p_low:.2%}-{res.backbone_rate.p_high:.2%}) spans "
            f"a "
            f"{res.backbone_rate.p_high / max(res.backbone_rate.p_low, 1e-9):.0f}x "
            f"range in campaign size. Enlarge the calibration run or loosen the "
            f"bar" + (f" (this sample supports {column} > {res.suggested_bar:g})"
                      if res.suggested_bar is not None else "") + "." + sys_note
        )
        return

    p = res.pessimistic
    if fits(p):
        res.verdict = "SCALE_UP"
        raised_note = (
            f" This target is unusually good — the trial supports sizing at "
            f"{column} > {res.bar_raised_to:g} (requested was "
            f"{res.requested_bar:g}) within budget, so the bar was raised "
            f"rather than sizing to the loosest workable target."
            if res.bar_raised_to is not None else "")
        res.verdict_reason = (
            f"{res.backbone_rate.k}/{res.backbone_rate.n:,} backbones produced an "
            f"excellent design ({res.backbone_rate.p_hat:.2%}, 95% CI "
            f"{res.backbone_rate.p_low:.2%}-{res.backbone_rate.p_high:.2%}). Even at "
            f"the pessimistic bound the run costs ~{p.est_gpu_hours:,.0f} GPU-h / "
            f"{p.est_disk_gb:,.0f} GB, inside the "
            f"{budget_hours:.0f} h / {disk_budget_gb:.0f} GB budget."
            + raised_note
        )
        return

    # How many designs ARE affordable at the pessimistic rate?
    scale = min(disk_budget_gb / max(p.est_disk_gb, 1e-9),
                budget_hours / max(p.est_gpu_hours, 1e-9))
    affordable = int(res.target_designs * scale)
    res.affordable_designs = affordable
    if fits(p, BUDGET_SLACK) or affordable >= max(1, res.target_designs // 10):
        res.verdict = "SCALE_UP_PARTIAL"
        res.verdict_reason = (
            f"{res.target_designs} excellent designs would cost "
            f"~{p.est_gpu_hours:,.0f} GPU-h / {p.est_disk_gb:,.0f} GB at the "
            f"pessimistic rate, past the {budget_hours:.0f} h / "
            f"{disk_budget_gb:.0f} GB budget. About {affordable} are affordable "
            f"within it."
        )
    else:
        res.verdict = "ITERATE"
        res.verdict_reason = (
            f"At the pessimistic rate the budget buys only ~{affordable} excellent "
            f"designs. Re-tune the interface, hotspots or trim as a new round "
            f"rather than scaling a low-yield campaign." + sys_note
        )


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def render_report(res: CalibrationResult, compute: "ComputeChoice | None" = None) -> str:
    def scale_line(s: ScaleEstimate) -> str:
        if s.required_refolds is None:
            return f"  {s.basis:<26} (no finite estimate)"
        prefix = ">= " if s.is_lower_bound else "~ "
        return (f"  {s.basis:<26} {prefix}{s.required_backbones:,.0f} designs / "
                f"{s.required_refolds:,.0f} refolds  ->  "
                f"{s.est_gpu_hours:,.0f} GPU-h, {s.est_disk_gb:,.0f} GB")

    lines = [
        "## Campaign calibration",
        "",
        f"Goal: **{res.target_designs} designs** meeting {res.excellence_bar}.",
    ]
    if res.bar_raised_to is not None:
        lines.append(
            f"Bar raised from the requested {res.success_metric} > "
            f"{res.requested_bar:g} to {res.success_metric} > "
            f"{res.bar_raised_to:g} — this trial supports it within budget.")
    lines += [
        "",
        f"Sample: {res.n_refolds_scored:,} refolds from {res.n_backbones_scored:,} "
        f"RFD3 backbones ({res.n_seq} sequences each, prefilter kept "
        f"{res.prefilter_rate:.0%}).",
        "",
        "### Hit rate",
        "",
        "| unit | hits | n | rate | 95% interval |",
        "|---|---|---|---|---|",
    ]
    for r in (res.backbone_rate, res.refold_rate):
        if r.zero_hits:
            lines.append(f"| {r.unit} | 0 | {r.n:,} | — | < {r.p_high:.2%} "
                         f"(rule of three) |")
        else:
            lines.append(f"| {r.unit} | {r.k} | {r.n:,} | {r.p_hat:.2%} | "
                         f"{r.p_low:.2%} – {r.p_high:.2%} |")
    lines += [
        "",
        "Backbones are the sizing unit: `n_batches` controls backbones, and the "
        f"{res.n_seq} sequences sharing one are correlated, not independent trials.",
        "",
        "### Required scale",
        "",
    ]
    lines.append(scale_line(res.central))
    if res.pessimistic.basis != res.central.basis:
        lines.append(scale_line(res.pessimistic))
    lines += [
        "",
        "### Yield at softer bars",
        "",
    ]
    for label, n in res.softer_rates.items():
        pct = 100.0 * n / res.n_refolds_scored if res.n_refolds_scored else 0.0
        lines.append(f"  {label:<28} {n:>7,}  ({pct:5.2f}% of refolds)")
    lines += [
        "",
        f"  best {res.success_metric}, any refold:  {res.best_ipsae_min_overall:.4f}",
        f"  best {res.success_metric}, a survivor:  {res.best_ipsae_min_survivor:.4f}",
    ]
    if res.suggested_bar is not None:
        lines.append(
            f"  strictest bar this sample can size a campaign at: "
            f"{res.success_metric} > {res.suggested_bar:g}")
    if res.backbone_rate.zero_hits:
        lines += [
            "",
            "> Nothing cleared the requested bar, so the figure above is a LOWER "
            "BOUND derived from an optimistic rate — it is **not** comparable to a "
            "point estimate at a looser bar, and a stricter bar can make it look "
            "smaller. Read it as \"at least this large\", nothing more.",
        ]
    lines += [
        "",
        "### Gate attribution",
        "",
        "```",
        res.filter_stats_text,
        "```",
        "",
        f"## Verdict: **{res.verdict}**",
        "",
        res.verdict_reason,
    ]
    if compute is not None:
        lines += [
            "",
            "### Where to run it",
            "",
            f"  local  (1 GPU)              ~{compute.local_hours:,.1f} h",
            f"  cluster ({compute.n_gpus_cluster} GPUs, parallel)   "
            f"~{compute.cluster_hours:,.1f} h (rough — different refold "
            f"backend/hardware)",
            "",
            (f"**Decision: {compute.compute}.** "
             + (f"Fits the {compute.max_local_hours:g} h local budget — "
                f"running on this workstation's GPU."
                if compute.compute == "local" else
                f"Exceeds the {compute.max_local_hours:g} h local budget — "
                f"staging a cluster package instead of running for "
                f"{compute.local_hours:,.0f} h unattended.")),
        ]
    return "\n".join(lines)
