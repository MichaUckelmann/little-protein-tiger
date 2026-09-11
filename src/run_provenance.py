"""One machine-readable record of what a campaign was, per run.

LPT already writes everything in here somewhere — the manifest knows the
project and the budget, each stage report knows its own handoff and which
model wrote it, the calibration knows the verdict. Spread across eight files
and two directory layouts, though, "what was this campaign, and who decided
what" is a question you answer by reading, not by querying.

For a dual-use tool, **auditability is the control that is actually
available**. Prevention is not: the generative models are public, and
`--workflow structure` accepts any local file. What a release can reasonably
offer is that every campaign leaves a complete, mechanical account of what it
targeted, which model chose the epitope, whether any model declined the work
first, and whether a select-agent name was flagged — in one file, in a shape
a script can read.

So this is deliberately a *collector*, not a new source of truth. Every field
is copied from an artifact that already existed; nothing here re-derives a
number or makes a judgement. `provenance.json` next to `report.html` is
regenerated whenever a report is, which also means an old campaign gains one
the next time `scripts/generate_binder_report.py` runs over it.

**It is a record, not a clearance.** A clean `select_agent_screen` means no
listed name appeared in the text that was screened, nothing more.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from src.handoff import parse_handoff
from src.report_common import parse_provenance, read_json, read_text

SCHEMA_VERSION = 2

#: Handoff fields worth lifting into the record: the identity of the thing
#: designed against, and the choices that identity rests on. Deliberately not
#: every field — a provenance record that mirrors the whole handoff is just
#: the handoff again, and the point is that this is short enough to read.
_KEEP = (
    "target_gene", "target_complex", "partner_name", "uniprot", "pdb_id",
    "target_chain", "partner_chain", "design_intent", "structure_organism",
    "contig", "n_segments", "modality",
)


def _stage_files(start: Path, max_levels: int = 3) -> list[Path]:
    """Every ``NN_*.md`` stage report for this run, nearest first.

    Walks up from `start` because the two layouts put them in different
    places: a binder campaign's own reports live in ``<run>/binder/``, while a
    PPI-bridged campaign's ``00_pathway.md``/``01_literature.md``/
    ``02_structure.md`` — where the "why this target" reasoning actually is —
    sit one level up. Same reasoning as `binder_report`'s own ancestor walk.
    """
    seen: dict[str, Path] = {}
    node = start
    for _ in range(max_levels + 1):
        if node.is_dir():
            for path in sorted(node.glob("[0-9][0-9]_*.md")):
                seen.setdefault(path.name, path)
        if node.parent == node:
            break
        node = node.parent
    return [seen[k] for k in sorted(seen)]


def _manifest(start: Path, max_levels: int = 5) -> tuple[dict, Path | None]:
    """The project manifest for this run, found by walking up to it."""
    node = start
    for _ in range(max_levels + 1):
        candidate = node / "manifest.json"
        if candidate.is_file():
            return (read_json(candidate) or {}), candidate
        if node.parent == node:
            break
        node = node.parent
    return {}, None


def collect(run_dir: Path) -> dict[str, Any]:
    """Assemble the provenance record for one run or campaign directory."""
    run_dir = Path(run_dir)
    manifest, manifest_path = _manifest(run_dir)

    stages: list[dict] = []
    target: dict[str, Any] = {}
    for path in _stage_files(run_dir):
        text = read_text(path) or ""
        handoff = parse_handoff(text)
        entry: dict[str, Any] = {"report": path.name}
        prov = parse_provenance(text)
        if prov:
            entry["written_by"] = prov.get("written_by")
            entry["skill"] = prov.get("skill")
            if prov.get("declined"):
                entry["declined"] = prov["declined"]
        else:
            # No block: either a deterministic stage (no model to name) or a
            # report written before provenance blocks existed. Say which,
            # rather than leaving a silent gap.
            entry["written_by"] = None
            entry["note"] = "no model provenance recorded"
        stages.append(entry)
        # Later stages refine the target; a stage that names a field wins over
        # an earlier one that named it differently, which is the same
        # precedence the pipeline itself applies.
        for key in _KEEP:
            if handoff.get(key):
                target[key] = handoff[key]

    declined_any = [
        {"stage": s["report"], **d}
        for s in stages for d in s.get("declined", [])
    ]

    checkpoints = manifest.get("checkpoints") or []
    screen = next((c for c in reversed(checkpoints)
                   if c.get("id") == "select_agent_screen"), None)

    calib = read_json(run_dir / "calibration" / "calibration.json") or {}

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
                            .isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "project": {
            "slug": manifest.get("slug"),
            "workflow": manifest.get("workflow"),
            "query": manifest.get("query"),
            "created_at": manifest.get("created_at"),
            "manifest": (str(manifest_path) if manifest_path else None),
        },
        "target": target,
        "stages": stages,
        "models": {
            # The question an auditor actually asks: did any model decline
            # this work, and did the content then come from another one?
            "declined": declined_any,
            "any_refusal": bool(declined_any),
        },
        "select_agent_screen": (
            {"flagged": True,
             "hits": (screen.get("payload") or {}).get("hits") or [],
             "list_source": (screen.get("payload") or {}).get("list_source"),
             "list_reviewed": (screen.get("payload") or {}).get("list_reviewed"),
             "blocking": False}
            if screen else
            {"flagged": False,
             "note": ("No listed select-agent name appeared in the screened "
                      "text. This is not a clearance — see "
                      "src/select_agents.py.")}),
        "calibration": {
            "verdict": calib.get("verdict"),
            "success_metric": calib.get("success_metric"),
            "bar": calib.get("bar_raised_to") or calib.get("requested_bar"),
        } if calib else {},
        "budget_usd": (manifest.get("budget") or {}).get("spent_usd"),
        "disclaimer": (
            "Every design this run produced is an unvalidated computational "
            "hypothesis: a sequence and a predicted pose, never a measurement "
            "of binding. Screening any synthesised sequence is the user's "
            "responsibility. See docs/responsible-use.md."),
    }
    return record


def write(run_dir: Path, out_path: Path | None = None) -> Path | None:
    """Write `provenance.json`. Best-effort: returns None on any failure.

    Called as a side effect of report generation, which is itself wrapped so
    it can never fail a campaign — so this must not raise either.
    """
    try:
        run_dir = Path(run_dir)
        out = Path(out_path) if out_path else (run_dir / "provenance.json")
        out.write_text(json.dumps(collect(run_dir), indent=2) + "\n",
                       encoding="utf-8")
        return out
    except Exception:                                         # noqa: BLE001
        return None


def footer_html(record: dict[str, Any]) -> str:
    """The "who wrote this" table for a report footer.

    A refusal that only reaches a log line is one nobody reviewing the
    campaign can see, and the report is the artifact that circulates. So the
    model behind each LLM stage is named in the report itself, and a stage
    that another model declined first says so.
    """
    rows = [s for s in record.get("stages", []) if s.get("written_by")]
    if not rows:
        return ""
    out = ["<p style=\"margin-top:10px;\"><b>Which model wrote which stage.</b> "
           "Deterministic stages are absent from this list because there is no "
           "model to name.</p>",
           "<table class=\"data\" style=\"margin-top:8px; min-width:0;\">",
           "<tr><th>Stage report</th><th>Written by</th><th>Declined first</th></tr>"]
    for s in rows:
        declined = s.get("declined") or []
        note = ", ".join(
            f"{_esc(d.get('model'))} ({_esc(d.get('category') or '?')})"
            for d in declined) or "—"
        out.append(
            f"<tr><td><code>{_esc(s.get('report'))}</code></td>"
            f"<td><code>{_esc(s.get('written_by'))}</code></td>"
            f"<td>{note}</td></tr>")
    out.append("</table>")
    if record.get("models", {}).get("any_refusal"):
        out.append(
            "<p style=\"margin-top:10px;\">A provider safety classifier "
            "declined at least one stage above, and it was retried on a "
            "different model. LPT stops after two models decline rather than "
            "trying a smaller one — see <code>docs/responsible-use.md</code>.</p>")
    return "\n".join(out)


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"[<>&\"]", lambda m: {
        "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;"}[m.group()], text)
