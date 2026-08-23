"""
Shared helpers for LPT's deterministic HTML run reports (no LLM in the loop).

Both :mod:`src.binder_report` (binder-track campaign reports) and
:mod:`src.ppi_report` (PPI-track run reports) read the same shape of thing
off disk — a stage's own markdown report, a ``### PIPELINE HANDOFF`` block,
a ``## CITATION VERIFICATION`` block — and render it the same way (LLM
prose through the ``markdown`` package, never re-derived). This module is
that shared reading/rendering layer so the two report generators can't
silently drift on how a handoff or a citation block is parsed.

Track-specific assembly (which sections exist, what the hero says, which
metrics get charted) stays in each report's own module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import markdown as _markdown_lib

_MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists"]


class ReportError(RuntimeError):
    """Report generation failed for a reason worth surfacing, not swallowing."""


# ---------------------------------------------------------------------
# small file helpers
# ---------------------------------------------------------------------

def read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def read_json(path: Path) -> Any | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def markdown_html(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    return _markdown_lib.markdown(text, extensions=_MD_EXTENSIONS)


def section_before_handoff(text: str) -> str:
    """Everything before the first ``### PIPELINE HANDOFF`` — the stage's
    own narrative, meant to be rendered as prose."""
    idx = text.find("### PIPELINE HANDOFF")
    return text[:idx] if idx >= 0 else text


def extract_citation_section(text: str) -> str | None:
    """The ``## CITATION VERIFICATION`` block, rendered as HTML — but only
    when something actually failed verification. A clean citation check is
    not worth a reader's attention; a failed one is."""
    m = re.search(r"##\s+CITATION VERIFICATION\s*\n(.*)\Z", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    body = m.group(1)
    if re.search(r"NOT IN CORPUS\s*\(\s*0\s*\)", body, re.IGNORECASE):
        return None
    if "NOT IN CORPUS" not in body.upper() and "citations checked: 0" in body.lower():
        return None
    return markdown_html(body)


# ---------------------------------------------------------------------
# numeric helpers
# ---------------------------------------------------------------------

def as_float(row: dict, key: str) -> float | None:
    v = row.get(key)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN check without importing math for one use


def histogram(values: list[float], lo: float, hi: float, nbins: int) -> dict:
    width = (hi - lo) / nbins
    counts = [0] * nbins
    for v in values:
        idx = int((v - lo) / width)
        idx = max(0, min(nbins - 1, idx))
        counts[idx] += 1
    return {"edges": [round(lo + i * width, 4) for i in range(nbins + 1)], "counts": counts}
