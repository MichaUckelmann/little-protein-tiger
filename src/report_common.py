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

import html as _html
import json
import re
from pathlib import Path
from typing import Any

import markdown as _markdown_lib

_MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists"]

# Raw-HTML neutralisation for LLM-authored prose. `markdown` passes embedded
# HTML through untouched, and a report is a self-contained file explicitly
# meant to be shared, so a prompt-injected paper reaching a stage narrative
# could otherwise land executable script in a distributed artifact. The JSON
# data blobs are already guarded by each report's `safe_json`; this is the
# prose path.
#
# Whole elements whose CONTENT is also dangerous (dropped, content included):
_MD_STRIP_ELEMENTS = ("script", "iframe", "object", "embed", "style", "noscript",
                      "template", "svg", "math", "form", "base", "link", "meta")
_STRIP_RE = re.compile(
    r"<\s*(" + "|".join(_MD_STRIP_ELEMENTS) + r")\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL)
# ...and their self-closing / unterminated forms.
_STRIP_LONE_RE = re.compile(
    r"<\s*/?\s*(" + "|".join(_MD_STRIP_ELEMENTS) + r")\b[^>]*>",
    re.IGNORECASE)
# Inline event handlers: onclick=, onerror=, onload=, ...
_EVENT_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
                            re.IGNORECASE)
# javascript:/vbscript:/data: URLs in href/src.
_BAD_URL_RE = re.compile(
    r"\s(href|src|xlink:href)\s*=\s*(\"|')?\s*(javascript|vbscript|data)\s*:[^\"'>\s]*(\"|')?",
    re.IGNORECASE)


def sanitize_html(rendered: str) -> str:
    """Strip script-bearing constructs from rendered HTML.

    Deliberately a narrow denylist over the constructs that execute, not a full
    sanitiser: the input is markdown from this pipeline's own stages, and the
    goal is that no path from LLM output to a shared file can carry script. If
    reports ever render genuinely untrusted third-party HTML, replace this with
    a real allowlist sanitiser (``bleach`` / ``nh3``) rather than extending it.
    """
    cleaned = _STRIP_RE.sub("", rendered)
    cleaned = _STRIP_LONE_RE.sub("", cleaned)
    cleaned = _EVENT_ATTR_RE.sub("", cleaned)
    cleaned = _BAD_URL_RE.sub(" ", cleaned)
    return cleaned


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
    """Render a stage's markdown narrative to HTML, with raw HTML neutralised.

    Everything passed here is LLM-authored prose, and a report is a
    self-contained file explicitly meant to be shared. `markdown` passes raw
    HTML through untouched by default, so a prompt-injected paper reaching a
    stage narrative would land executable script in a distributed artifact.
    The data blobs are already guarded by `safe_json`; this closes the prose
    path. Markdown's own syntax is unaffected — only literal tags are escaped.
    """
    text = text.strip()
    if not text:
        return ""
    return sanitize_html(
        _markdown_lib.markdown(text, extensions=_MD_EXTENSIONS))


def escape_html(text: str) -> str:
    """Escape a plain-text value for interpolation into an HTML template.

    Used for `@@TITLE@@`, which carries an LLM-derived target name straight
    into `<title>`.
    """
    return _html.escape(str(text), quote=True)


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
