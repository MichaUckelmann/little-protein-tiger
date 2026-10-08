"""The blank/decoy arms of the corpus ablation must not leak corpus content.

A corpus-reading tool left out of CORPUS_TOOLS silently keeps feeding the model
corpus data in an arm described as blank (`find_pdb_structures` did, until
2026-10-07). These tests make that a failure instead of a surprise in a trace.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ablate_corpus as ac  # noqa: E402
from src.skill_runner import _TOOL_DEFS  # noqa: E402


def test_every_tool_is_classified_exactly_once():
    names = {t["name"] for t in _TOOL_DEFS}
    assert not (ac.CORPUS_TOOLS & ac.LIVE_BY_DESIGN), "a tool is both blanked and live"
    unclassified = names - ac.CORPUS_TOOLS - ac.LIVE_BY_DESIGN
    assert not unclassified, (
        f"{sorted(unclassified)} are neither blanked nor declared live by design; decide "
        "whether each reads the corpus, then add it to CORPUS_TOOLS or LIVE_BY_DESIGN")
    stale = (ac.CORPUS_TOOLS | ac.LIVE_BY_DESIGN) - names
    assert not stale, f"classified tools that no longer exist: {sorted(stale)}"


def test_find_pdb_structures_is_blanked():
    # Its implementation scans fingerprint_dir; see the comment on CORPUS_TOOLS.
    assert "find_pdb_structures" in ac.CORPUS_TOOLS


class _FakeRunner:
    def __init__(self):
        self.inner_calls = []
        self._execute_tool = self._inner

    def _inner(self, name, args):
        self.inner_calls.append(name)
        return "REAL CORPUS CONTENT"


def test_blank_arm_never_calls_a_corpus_tool_and_passes_others_through():
    r = _FakeRunner()
    ac.patch(r, "blank", {})
    for name in sorted(ac.CORPUS_TOOLS):
        assert r._execute_tool(name, {"query": "x", "proteins": ["TP53"]}) == ac.NOTHING, name
    assert r.inner_calls == [], "a blanked tool reached the real implementation"
    for name in sorted(ac.LIVE_BY_DESIGN):
        assert r._execute_tool(name, {}) == "REAL CORPUS CONTENT"


def test_live_arm_is_untouched():
    r = _FakeRunner()
    before = r._execute_tool
    ac.patch(r, "live", {})
    assert r._execute_tool == before
