"""
Token accounting and dollar budgeting for LLM pipeline stages.

The pipeline builds a fresh `SkillRunner` per stage and discards it, so the
per-call token counters it maintains never reached the orchestrator.  This
module closes that loop: `SkillRunner.usage()` hands up a `Usage`, the
orchestrator hands it to a `TokenLedger`, and the ledger prices it, persists it,
and enforces a hard cap.

Pricing note — the four token buckets are NOT interchangeable:

  * ``input_tokens``            uncached input, full rate
  * ``cache_creation_tokens``   written to the prompt cache, ~1.25x (5m TTL)
  * ``cache_read_tokens``       served from the prompt cache, ~0.10x
  * ``output_tokens``           output rate

The Anthropic API reports these as *separate* fields — ``usage.input_tokens``
already excludes the cached portions.  Summing them into one "input" number
triple-counts a cached prefix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from loguru import logger

__all__ = [
    "Usage", "Rates", "LedgerEntry", "BudgetExceeded", "TokenLedger",
    "rates_for", "price", "load_pricing",
]


# ----------------------------------------------------------------------
# Usage
# ----------------------------------------------------------------------

@dataclass
class Usage:
    """Token counts for one or more API calls, split by billing bucket."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    @property
    def total_input(self) -> int:
        """All input-side tokens, ignoring the price difference between them."""
        return self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        return cls(**{k: int(d.get(k, 0)) for k in
                      ("input_tokens", "output_tokens",
                       "cache_creation_tokens", "cache_read_tokens")})


# ----------------------------------------------------------------------
# Pricing
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Rates:
    """USD per 1M tokens, plus the cache multipliers applied to the input rate."""

    input_per_mtok: float
    output_per_mtok: float
    cache_write_mult: float = 1.25   # 5-minute TTL; use 2.0 for a 1h TTL
    cache_read_mult: float = 0.10


# Fallback table.  config.yaml `models.pricing` overrides this — keep the two
# in sync when rates change, and prefer editing config.
_PRICES: dict[str, Rates] = {
    "claude-opus-5":     Rates(5.00, 25.00),
    "claude-opus-4-8":   Rates(5.00, 25.00),
    "claude-opus-4-7":   Rates(5.00, 25.00),
    "claude-opus-4-6":   Rates(5.00, 25.00),
    "claude-sonnet-5":   Rates(3.00, 15.00),
    "claude-sonnet-4-6": Rates(3.00, 15.00),
    "claude-haiku-4-5":  Rates(1.00,  5.00),
    "claude-fable-5":    Rates(10.00, 50.00),
    # Gemini responses carry no cache buckets; the multipliers are inert.
    "gemini-3.7-flash":              Rates(0.75, 3.75),   # intro to 2026-12-31; 1.50/7.50 after
    "gemini-3.5-flash":              Rates(0.50, 2.50),
    "gemini-3.1-flash-lite":         Rates(0.10, 0.40),
    "gemini-3.1-flash-lite-preview": Rates(0.10, 0.40),
}

_PRICE_OVERRIDES: dict[str, Rates] = {}
_UNPRICED_WARNED: set[str] = set()


def load_pricing(config: dict | None) -> None:
    """
    Install `models.pricing` from config as an override layer.

    Shape:  models: {pricing: {"<model-id>": {input: 3.0, output: 15.0,
                                              cache_write_mult: 1.25,
                                              cache_read_mult: 0.1}}}
    """
    _PRICE_OVERRIDES.clear()
    pricing = ((config or {}).get("models") or {}).get("pricing") or {}
    for model, spec in pricing.items():
        try:
            _PRICE_OVERRIDES[model] = Rates(
                input_per_mtok=float(spec["input"]),
                output_per_mtok=float(spec["output"]),
                cache_write_mult=float(spec.get("cache_write_mult", 1.25)),
                cache_read_mult=float(spec.get("cache_read_mult", 0.10)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(f"models.pricing[{model!r}] is malformed, ignoring: {exc}")


def rates_for(model: str) -> Rates | None:
    """
    Look up rates for a model id: exact match, then longest known prefix.

    Prefix matching lets a dated snapshot (`claude-haiku-4-5-20251001`) price
    off its undated base.  Returns None for a genuinely unknown model.
    """
    if not model:
        return None
    for table in (_PRICE_OVERRIDES, _PRICES):
        if model in table:
            return table[model]
    candidates = [k for k in (*_PRICE_OVERRIDES, *_PRICES) if model.startswith(k)]
    if candidates:
        return (_PRICE_OVERRIDES.get(max(candidates, key=len))
                or _PRICES[max(candidates, key=len)])
    return None


def price(model: str, usage: Usage) -> float:
    """
    USD cost of `usage` on `model`.

    Unknown models price at $0.00 with a one-time warning rather than raising —
    a missing rate must never abort a run that is otherwise fine.  The ledger
    records `unpriced=True` on those entries so the total is honestly qualified.
    """
    r = rates_for(model)
    if r is None:
        if model not in _UNPRICED_WARNED:
            _UNPRICED_WARNED.add(model)
            logger.warning(
                f"No price table entry for model {model!r} — its spend will be "
                f"counted as $0.00 and the ledger total will be an UNDERESTIMATE. "
                f"Add it to config.yaml models.pricing."
            )
        return 0.0
    billable_input = (
        usage.input_tokens
        + usage.cache_creation_tokens * r.cache_write_mult
        + usage.cache_read_tokens * r.cache_read_mult
    )
    return (billable_input / 1e6) * r.input_per_mtok + \
           (usage.output_tokens / 1e6) * r.output_per_mtok


# ----------------------------------------------------------------------
# Ledger
# ----------------------------------------------------------------------

@dataclass
class LedgerEntry:
    ts: str
    stage: str
    skill: str
    provider: str
    model: str
    usage: Usage
    usd: float
    unpriced: bool = False
    kind: str = "actual"      # "actual" | "estimate"
    note: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        d["usage"] = self.usage.as_dict()
        return json.dumps(d, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerEntry":
        return cls(
            ts=d.get("ts", ""), stage=d.get("stage", ""), skill=d.get("skill", ""),
            provider=d.get("provider", ""), model=d.get("model", ""),
            usage=Usage.from_dict(d.get("usage", {})),
            usd=float(d.get("usd", 0.0)), unpriced=bool(d.get("unpriced", False)),
            kind=d.get("kind", "actual"), note=d.get("note", ""),
        )


class BudgetExceeded(RuntimeError):
    """Raised by `TokenLedger.preflight` / `check_cap` when the cap would be passed."""

    def __init__(self, *, spent_usd: float, projected_usd: float,
                 cap_usd: float, stage: str):
        self.spent_usd = spent_usd
        self.projected_usd = projected_usd
        self.cap_usd = cap_usd
        self.stage = stage
        super().__init__(
            f"API budget exceeded before stage {stage!r}: "
            f"${spent_usd:.4f} spent + ${projected_usd:.4f} projected "
            f"> ${cap_usd:.2f} cap"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TokenLedger:
    """
    Append-only spend ledger with an optional hard dollar cap.

    The JSONL file is authoritative: one line per recorded call-group, appended
    and flushed immediately.  It survives a crash mid-write and tolerates
    concurrent writers (CLI + Celery), neither of which a rewritten JSON
    document would.  An optional `manifest_sink` mirrors a rolled-up summary
    into the project manifest after each stage.
    """

    def __init__(
        self,
        path: Path,
        *,
        cap_usd: float | None = None,
        mode: str = "hard",
        manifest_sink: Callable[[dict], None] | None = None,
    ):
        if mode not in ("hard", "warn"):
            raise ValueError(f"budget mode must be 'hard' or 'warn', got {mode!r}")
        self.path = Path(path)
        self.cap_usd = cap_usd
        self.mode = mode
        self._sink = manifest_sink
        self.entries: list[LedgerEntry] = []
        self._replay()

    # -- persistence ----------------------------------------------------

    def _replay(self) -> None:
        """Load prior spend so a resumed run's cap covers the whole project."""
        if not self.path.exists():
            return
        for lineno, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                self.entries.append(LedgerEntry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                # A torn final line from a hard kill is expected; skip it.
                logger.warning(f"{self.path}:{lineno} unreadable ledger line: {exc}")
        if self.entries:
            logger.info(
                f"Budget ledger: replayed {len(self.entries)} prior entries "
                f"(${self.spent_usd:.4f} already spent)"
            )

    def _append(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")
            fh.flush()

    # -- recording ------------------------------------------------------

    def record(
        self,
        *,
        stage: str,
        skill: str,
        provider: str,
        model: str,
        usage: Usage,
        note: str = "",
    ) -> LedgerEntry:
        """Price and persist one stage's actual usage."""
        entry = LedgerEntry(
            ts=_now(), stage=stage, skill=skill, provider=provider, model=model,
            usage=usage, usd=price(model, usage),
            unpriced=rates_for(model) is None, note=note,
        )
        self._append(entry)
        logger.info(
            f"  [{stage}] spend ${entry.usd:.4f} "
            f"({usage.input_tokens:,} in / {usage.output_tokens:,} out / "
            f"{usage.cache_read_tokens:,} cache-read) — "
            f"project total ${self.spent_usd:.4f}"
            + (f" / ${self.cap_usd:.2f} cap" if self.cap_usd is not None else "")
        )
        self._flush_manifest()
        return entry

    # -- enforcement ----------------------------------------------------

    def preflight(self, *, stage: str, model: str, estimated: Usage) -> None:
        """
        Refuse to start a stage whose projected cost would pass the cap.

        The estimate is a *guard*, not an accounting number — the recorded
        actuals are the truth.  Raises BudgetExceeded in "hard" mode.
        """
        if self.cap_usd is None:
            return
        projected = price(model, estimated)
        if self.spent_usd + projected <= self.cap_usd:
            return
        if self.mode == "warn":
            logger.warning(
                f"[{stage}] projected ${projected:.4f} would take spend past the "
                f"${self.cap_usd:.2f} cap (${self.spent_usd:.4f} used) — "
                f"continuing because budget mode is 'warn'"
            )
            return
        raise BudgetExceeded(spent_usd=self.spent_usd, projected_usd=projected,
                             cap_usd=self.cap_usd, stage=stage)

    def check_cap(self, *, next_stage: str) -> None:
        """
        Post-stage guard: a stage that overshot its own estimate should pause
        cleanly here rather than mid-flight in the next one.
        """
        if self.cap_usd is None or self.spent_usd <= self.cap_usd:
            return
        if self.mode == "warn":
            logger.warning(
                f"Spend ${self.spent_usd:.4f} is over the ${self.cap_usd:.2f} cap "
                f"before {next_stage!r} — continuing (budget mode 'warn')"
            )
            return
        raise BudgetExceeded(spent_usd=self.spent_usd, projected_usd=0.0,
                             cap_usd=self.cap_usd, stage=next_stage)

    # -- reporting ------------------------------------------------------

    @property
    def spent_usd(self) -> float:
        return sum(e.usd for e in self.entries if e.kind == "actual")

    @property
    def remaining_usd(self) -> float | None:
        return None if self.cap_usd is None else max(0.0, self.cap_usd - self.spent_usd)

    @property
    def has_unpriced(self) -> bool:
        return any(e.unpriced for e in self.entries if e.kind == "actual")

    def _group(self, key: Callable[[LedgerEntry], str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.entries:
            if e.kind != "actual":
                continue
            out[key(e)] = out.get(key(e), 0.0) + e.usd
        return out

    def by_stage(self) -> dict[str, float]:
        return self._group(lambda e: e.stage)

    def by_model(self) -> dict[str, float]:
        return self._group(lambda e: e.model)

    def totals(self) -> Usage:
        total = Usage()
        for e in self.entries:
            if e.kind == "actual":
                total = total + e.usage
        return total

    def as_manifest_block(self) -> dict:
        return {
            "cap_usd": self.cap_usd,
            "mode": self.mode,
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": (None if self.remaining_usd is None
                              else round(self.remaining_usd, 6)),
            "by_stage": {k: round(v, 6) for k, v in self.by_stage().items()},
            "by_model": {k: round(v, 6) for k, v in self.by_model().items()},
            "tokens": self.totals().as_dict(),
            "has_unpriced": self.has_unpriced,
            "ledger_path": str(self.path),
            "updated_at": _now(),
        }

    def _flush_manifest(self) -> None:
        if self._sink is None:
            return
        try:
            self._sink(self.as_manifest_block())
        except Exception as exc:   # never let bookkeeping fail a run
            logger.warning(f"budget manifest mirror failed: {exc}")

    def summary_markdown(self) -> str:
        lines = ["| stage | USD |", "|---|---|"]
        for stage, usd in sorted(self.by_stage().items(), key=lambda kv: -kv[1]):
            lines.append(f"| {stage} | {usd:.4f} |")
        lines.append(f"| **total** | **{self.spent_usd:.4f}** |")
        if self.cap_usd is not None:
            lines.append(f"| _cap_ | _{self.cap_usd:.2f}_ |")
        t = self.totals()
        lines.append("")
        lines.append(
            f"Tokens: {t.input_tokens:,} in · {t.output_tokens:,} out · "
            f"{t.cache_creation_tokens:,} cache-write · {t.cache_read_tokens:,} cache-read"
        )
        if self.has_unpriced:
            lines.append("")
            lines.append("> **Underestimate** — at least one model has no price "
                         "table entry and was counted as $0.00.")
        return "\n".join(lines)
