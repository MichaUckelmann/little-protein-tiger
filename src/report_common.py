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


def safe_json(obj: Any) -> str:
    r"""Serialize a report payload for embedding in an inline ``<script>``.

    Two sequences would let embedded markup escape the script element and be
    parsed as HTML: ``</script`` ends it outright, and ``<!--`` puts the
    parser into script-data-escaped state, where a later ``</script>`` no
    longer ends it. Both are neutralised with escapes that are valid in JSON
    *and* in a JavaScript string literal: ``\/`` (JSON's solidus escape) and
    ``\u003c`` for the ``<``.

    The earlier ``<\!--`` was neither. JavaScript quietly drops the backslash
    from an unknown escape, so the page rendered fine and the payload simply
    stopped being parseable JSON — which only surfaced once the stage-report
    appendix started carrying a stage's own HTML comment
    (``<!-- MODE: DISRUPT -->``) into the blob, and every test that reads
    REPORT back out of a built report failed at once.
    """
    return (json.dumps(obj, ensure_ascii=False)
            .replace("</script", r"<\/script")
            .replace("<!--", r"\u003c!--"))


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

def parse_provenance(text: str) -> dict | None:
    """Read a ``## MODEL PROVENANCE`` block back out of a stage report.

    Written by ``PipelineRunner._stage_provenance_note``. Returns
    ``{"skill", "written_by", "declined": [{"model", "category"}], "asked"}``,
    or None when the block is absent — which is the normal case for a stage
    report written before this block existed, and for every deterministic
    stage (there is no model to name).
    """
    m = re.search(r"##\s+MODEL PROVENANCE\s*\n(.*?)(?=\n##\s|\Z)",
                  text or "", re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    body = m.group(1)
    skill = re.search(r"-\s*skill:\s*`?([^`\n]+)`?", body)
    who = re.search(r"-\s*written by:\s*\*\*([^*\n]+)\*\*", body)
    declined = [
        {"model": mm.group(1), "category": mm.group(2)}
        for mm in re.finditer(
            r"-\s*`([^`]+)`\s*—\s*safety classifier, category `([^`]+)`", body)
    ]
    return {
        "skill": (skill.group(1).strip() if skill else None),
        "written_by": (who.group(1).strip() if who else None),
        "declined": declined,
        "asked": len(declined) + 1,
    }


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


# ---------------------------------------------------------------------
# full stage-report appendix
# ---------------------------------------------------------------------
#
# The narrative sections above each render ONE slice of a stage's markdown
# (the prose before its handoff, the hotspot rationale, the analyst verdict).
# The appendix renders every stage file WHOLE, so one report file carries the
# complete run record — including the parts no section quotes: the handoff
# blocks themselves, the trim/spec/pilot arithmetic, the citation checks.
# Nothing here re-derives anything; it is the same markdown, rendered.

_HANDOFF_BLOCK_RE = re.compile(
    r"(^#{2,4}[ \t]+PIPELINE HANDOFF[ \t]*$)(.*?)(?=^#|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL)
# Same shape `handoff.parse_handoff` accepts: '- key: value' or bare 'key: value'.
_HANDOFF_KV_RE = re.compile(r"^\s*(?:-\s+)?(\w+):\s*(.+)$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _handoff_block_to_table(text: str) -> str:
    """Rewrite ``### PIPELINE HANDOFF`` bullet blocks as markdown tables.

    A handoff is a field list, and reads like one — two aligned columns beat
    twenty ``- key: value`` bullets when a reader is scanning for the one
    field that explains what the next stage was told. Purely presentational:
    the parse that the PIPELINE consumes is `handoff.parse_handoff` over the
    original file, never this. A block that isn't purely key/value (a model
    wrote prose into it) is left exactly as it was, rather than half-converted.
    """
    def repl(m: re.Match) -> str:
        heading, body = m.group(1), m.group(2)
        rows: list[tuple[str, str]] = []
        for line in body.splitlines():
            if not line.strip() or _FENCE_RE.match(line):
                continue
            kv = _HANDOFF_KV_RE.match(line)
            if not kv:
                return m.group(0)  # not a clean field list — leave it alone
            rows.append((kv.group(1).strip(), kv.group(2).strip()))
        if not rows:
            return m.group(0)
        cells = "\n".join(
            f"| `{k}` | {v.replace('|', chr(92) + '|')} |" for k, v in rows)
        return f"{heading}\n\n| field | value |\n|---|---|\n{cells}\n"

    return _HANDOFF_BLOCK_RE.sub(repl, text)


# A stage report is written to be read in a terminal first, so its
# column-aligned blocks (calibration's "required scale" / "yield at softer
# bars" ladders) are indented TWO spaces, not markdown's four. Markdown reads
# that as a paragraph and collapses every run of spaces, which turns an
# aligned ladder into an unreadable sentence of numbers. Fence them so the
# alignment their author intended survives.
_LIST_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\|)")
_INDENTED_RE = re.compile(r"^ {2,3}\S")
_ALIGNED_RE = re.compile(r"^ {2,3}\S.*?\S {2,}\S")  # ...and an interior column gap


def _preserve_aligned_blocks(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i, in_fence = 0, False
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        prev = out[-1] if out else ""
        starts_block = not prev.strip() or prev.lstrip().startswith("#")
        if (in_fence or not starts_block
                or not _INDENTED_RE.match(line) or _LIST_RE.match(line)):
            out.append(line)
            i += 1
            continue
        # Gather the run, keeping interior blank lines that separate two
        # indented lines — one ladder, visually, is one block.
        j, run = i, []
        while j < len(lines):
            cur = lines[j]
            nxt = lines[j + 1] if j + 1 < len(lines) else ""
            if _FENCE_RE.match(cur):
                break
            if _INDENTED_RE.match(cur) and not _LIST_RE.match(cur):
                run.append(cur)
            elif (not cur.strip() and _INDENTED_RE.match(nxt)
                    and not _LIST_RE.match(nxt)):
                run.append(cur)
            else:
                break
            j += 1
        body = [ln for ln in run if ln.strip()]
        if len(body) >= 2 and any(_ALIGNED_RE.match(ln) for ln in body):
            out += ["```"] + run + ["```"]
        else:
            out += run
        i = j
    return "\n".join(out)


def stage_markdown_html(text: str) -> str:
    """A complete stage report as HTML — prose, tables, handoff and all."""
    return markdown_html(_preserve_aligned_blocks(_handoff_block_to_table(text)))


def stage_documents(entries: list[tuple[str, str, Path]],
                     rel_to: Path | None = None) -> list[dict]:
    """Render `(num, label, path)` stage files into appendix documents.

    Missing and empty files are skipped silently — a run paused after
    calibration simply has no production report to append, which is a fact
    about the run, not an error. `rel_to` shortens the displayed path (a
    reader needs to know WHICH file a section came from; the absolute path
    of a directory they already opened tells them nothing).
    """
    docs: list[dict] = []
    by_text: dict[str, dict] = {}
    for num, label, path in entries:
        path = Path(path)
        text = read_text(path)
        if not text or not text.strip():
            continue
        shown = path
        if rel_to is not None:
            try:
                shown = path.relative_to(rel_to)
            except ValueError:
                pass
        # A PPI-bridged campaign has two stage files with identical bytes:
        # `_bridge_ppi_to_foundry` copies 02_structure.md verbatim as the
        # binder track's 21_interface.md rather than paying for a second,
        # redundant call to the same skill. Rendering both twice reads as
        # the interface stage having repeated the structure stage. Show it
        # once and name the other path — which also documents the copy.
        prior = by_text.get(text)
        if prior is not None:
            prior.setdefault("also", []).append(str(shown))
            continue
        doc = {
            "id": "doc-" + path.stem.replace("_", "-"),
            "num": num,
            "label": label,
            "path": str(shown),
            "html": stage_markdown_html(text),
        }
        by_text[text] = doc
        docs.append(doc)
    return docs


def display_root(start: Path, marker: str = "manifest.json", max_levels: int = 6) -> Path:
    """Nearest ancestor of `start` holding `marker` — the shortest path prefix
    a reader still recognises.

    A binder appendix spans two directories (``binder/2*.md`` plus, for a
    PPI-bridged run, ``0*.md`` one level up), so paths are shown relative to
    the project root rather than to either of them. Falls back to `start`'s
    parent when no marker is found — a legacy ``outputs/<slug>_<date>/`` run
    has no manifest.
    """
    d = Path(start)
    for _ in range(max_levels + 1):
        if (d / marker).exists():
            return d
        if d.parent == d:
            break
        d = d.parent
    return Path(start).parent
