"""Extract-once, commit-the-facts: how a showcase page gets its numbers.

The problem this solves is specific and had already bitten every page. The
builders used to carry every figure as a literal in their own source while the
pages they generated claimed those figures "were read from" the run directory.
Nothing checked that, so the numbers drifted: `ppi_discovery.html` shipped four
of twelve hotspot residue names wrong, `campaign_pdl1.html` an ipTM/geometry
claim that its own scoring CSV contradicts, and every corpus statistic on
`corpus_explorer.html` was stale by about 25%.

Reading the run directly fixes the drift but breaks rebuilds, because
`projects/`, `outputs/` and `data/` are all gitignored — only the maintainer
has them. So:

  * On a machine that HAS the run, the extractor runs and its result is written
    to `facts/<name>.json`, which IS tracked.
  * Anywhere else, the tracked snapshot is loaded and the page builds identically.

Two things fall out of that. A page's numbers are always machine-derived, so the
provenance line is true; and because the snapshot is version-controlled, a number
that changes shows up as a diff in review instead of silently. That second
property is the one that would have caught all three bugs above.

`SourceMissing` is what an extractor raises when its run is not on this machine
— never a bare exception, or a genuine parse bug would be indistinguishable from
"not the maintainer's laptop" and would silently serve a stale snapshot.
"""
from __future__ import annotations

import json
import pathlib
from typing import Callable

HERE = pathlib.Path(__file__).resolve().parent
FACTS_DIR = HERE / "facts"


class SourceMissing(Exception):
    """The run/corpus this page is built from is not present on this machine."""


def read(path: pathlib.Path) -> str:
    """Read a source artifact, or report it as absent rather than as a crash."""
    if not path.is_file():
        raise SourceMissing(str(path))
    return path.read_text(encoding="utf-8")


def load(name: str, extractor: Callable[[], dict], *, refresh: bool = True) -> dict:
    """Facts for one showcase page: re-extracted where possible, else the snapshot."""
    snapshot = FACTS_DIR / f"{name}.json"
    try:
        facts = extractor()
    except SourceMissing as exc:
        if not snapshot.is_file():
            raise SystemExit(
                f"{name}: source data missing ({exc}) and no tracked snapshot at "
                f"{snapshot.relative_to(HERE.parent.parent)} — this page can only be "
                f"built on a machine with the run.") from exc
        print(f"  [snapshot] {exc} absent — building from facts/{name}.json")
        return json.loads(snapshot.read_text(encoding="utf-8"))

    if refresh:
        FACTS_DIR.mkdir(exist_ok=True)
        new = json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        old = snapshot.read_text(encoding="utf-8") if snapshot.is_file() else None
        snapshot.write_text(new, encoding="utf-8")
        if old is None:
            print(f"  [facts] wrote facts/{name}.json")
        elif old != new:
            print(f"  [facts] facts/{name}.json CHANGED — review the diff before "
                  f"committing; the page's numbers moved")
    return facts
