"""The hotspot table's numbering FRAME is stated, not inferred. Advisory only.

`_verify_hotspot_grounding` answers "is the residue at auth N what the table
claims". It cannot answer "is auth N the residue the literature means", and
against a uniform frame offset it is only a PROXY: measured over 8ZNL chain
B's modelled span, a +1 offset is caught at 105 of 113 positions and SILENT at
8, where the neighbouring residue happens to share a type. ~93% per hotspot —
a ten-row table slipping through whole is ~1e-11, a single row is one in
fourteen.

`membrane_topology.uniprot_to_auth` answers the second question directly, from
RCSB's deposited entity<->UniProt alignment, and nothing routed a hotspot
through it. Measured live: a uniform `{+1}` for 8ZNL chain B (its
`_struct_ref_seq` maps Q9NZQ7 19-132 onto auth 20-133) and `{0}` for 7CZD.

**Both of those are correct deposits.** A non-zero offset is ordinary and
entirely legal, which is exactly why this must never gate — and why the tests
here are mostly about it staying silent and staying harmless.
"""

from __future__ import annotations

import contextlib

import pytest

from src.pipeline_runner import PipelineRunner


@contextlib.contextmanager
def captured_logs(level: str = "DEBUG"):
    """Collect loguru records emitted inside the block, with their levels.

    Not `caplog`: this project logs through loguru, which does not propagate
    into the stdlib handlers pytest installs, so `caplog.records` is empty no
    matter what was logged. Every assertion here is about text an operator has
    to actually see — and about the LEVEL, since the whole design is that a
    legal deposit must not warn.
    """
    from loguru import logger

    out: list[tuple[str, str]] = []
    sink = logger.add(lambda m: out.append((m.record["level"].name,
                                            m.record["message"])),
                      level=level)
    try:
        yield out
    finally:
        logger.remove(sink)


def _text(records) -> str:
    return "\n".join(m for _, m in records)


def _levels(records) -> set[str]:
    return {lvl for lvl, _ in records}

_FRAME = PipelineRunner._hotspot_numbering_frame
_HOTSPOTS = [{"auth_seq_id": 57, "residue": "TYR"},
             {"auth_seq_id": 67, "residue": "GLN"},
             {"auth_seq_id": 114, "residue": "ARG"}]


@pytest.fixture
def offset_map(monkeypatch):
    """Stand in for the RCSB call with a chosen frame."""
    def _install(mapping):
        import src.membrane_topology as mt
        monkeypatch.setattr(mt, "uniprot_to_auth",
                            lambda *a, **k: mapping)
    return _install


def _uniform(offset: int, lo: int = 19, hi: int = 132) -> dict[int, int]:
    return {u: u + offset for u in range(lo, hi + 1)}


# ── what it says ───────────────────────────────────────────────────────────

def test_a_non_zero_frame_is_reported_with_the_canonical_ids(offset_map):
    """The 8ZNL case. The point of the line is that a reader can compare the
    ids against a paper in the paper's own frame."""
    offset_map(_uniform(+1))
    with captured_logs() as rec:
        _FRAME(_HOTSPOTS, "B", "8ZNL", "Q9NZQ7")
    msg = _text(rec)
    assert "numbering frame" in msg
    assert "+1" in msg
    # auth=canonical for each declared hotspot.
    assert "57=56" in msg and "67=66" in msg and "114=113" in msg
    assert "short" in msg, "must say which way a literature id would be wrong"


def test_an_identical_frame_is_reported_too_and_does_not_warn(offset_map):
    """The 7CZD case. Worth one INFO line: it is the entry where a literature
    number can be used as-is, and a reader cannot otherwise tell it from the
    entry where it cannot. But it is not a warning — nothing is wrong."""
    offset_map(_uniform(0))
    with captured_logs() as rec:
        _FRAME(_HOTSPOTS, "B", "7CZD", "Q9NZQ7")
    assert "author ==" in _text(rec)
    assert "WARNING" not in _levels(rec)


def test_a_negative_offset_says_long_not_short(offset_map):
    offset_map(_uniform(-3))
    with captured_logs() as rec:
        _FRAME(_HOTSPOTS, "X", "1ABC", "P00001")
    assert "-3" in _text(rec) and "3 residues long" in _text(rec)


def test_a_non_uniform_alignment_states_no_single_frame(offset_map):
    """Several aligned regions, an insertion or a chimera. There is no one
    offset to report, so report the per-hotspot translation and say why."""
    mapping = {**_uniform(0, 19, 60), **_uniform(+5, 61, 132)}
    offset_map(mapping)
    with captured_logs() as rec:
        _FRAME(_HOTSPOTS, "B", "8ZNL", "Q9NZQ7")
    assert "no single frame" in _text(rec)
    assert "57=57" in _text(rec) and "67=62" in _text(rec)


def test_hotspots_outside_the_alignment_are_named_not_dropped(offset_map):
    offset_map(_uniform(+1))
    with captured_logs() as rec:
        _FRAME(_HOTSPOTS + [{"auth_seq_id": 900}], "B", "8ZNL", "Q9NZQ7")
    assert "900" in _text(rec) and "outside the alignment" in _text(rec)


# ── when it stays silent, which is most of the time ────────────────────────

@pytest.mark.parametrize("args", [
    ([], "B", "8ZNL", "Q9NZQ7"),                 # no hotspots
    (_HOTSPOTS, "", "8ZNL", "Q9NZQ7"),           # no chain
    (_HOTSPOTS, "B", "8ZNL", None),              # no accession resolved
    (_HOTSPOTS, "B", "8ZNL", ""),                # ditto
    ([{"residue": "TYR"}], "B", "8ZNL", "Q9NZQ7"),   # no auth ids
])
def test_it_says_nothing_when_it_cannot_answer(args, monkeypatch):
    import src.membrane_topology as mt
    called = []
    monkeypatch.setattr(mt, "uniprot_to_auth",
                        lambda *a, **k: called.append(a) or {})
    with captured_logs() as rec:
        _FRAME(*args)
    assert not rec, f"expected silence, got {rec}"
    assert not called, "must not pay for an RCSB call it cannot use"


def test_no_alignment_for_that_accession_fails_open(offset_map):
    """`uniprot_to_auth` returns {} when the entry has no alignment for the
    accession — which is also what protects the PPI track, where the accession
    handed in may turn out to be the PARTNER's."""
    offset_map({})
    with captured_logs() as rec:
        _FRAME(_HOTSPOTS, "B", "8ZNL", "P01308")
    assert "WARNING" not in _levels(rec)


def test_a_lookup_failure_never_raises(monkeypatch):
    """A network failure must not end a run at a stage that has already
    passed every real check."""
    import src.membrane_topology as mt

    def boom(*a, **k):
        raise RuntimeError("RCSB unreachable")

    monkeypatch.setattr(mt, "uniprot_to_auth", boom)
    with captured_logs() as rec:
        _FRAME(_HOTSPOTS, "B", "8ZNL", "Q9NZQ7")   # must not raise
    assert "WARNING" not in _levels(rec)


def test_it_is_not_a_gate():
    """Source inspection: the advisory must contain no raise, and must run
    only after grounding's own hard checks. A non-zero offset is a legal
    deposit, so a run must never end on one."""
    import inspect

    src = inspect.getsource(PipelineRunner._hotspot_numbering_frame)
    assert "raise" not in src, "the numbering-frame check must never raise"

    grounding = inspect.getsource(PipelineRunner._verify_hotspot_grounding)
    i = grounding.index("_hotspot_numbering_frame(")
    j = grounding.index("is not grounded in")
    assert i > j, (
        "the advisory must run AFTER the grounding mismatch check, so a table "
        "that is wrong about its own residues fails on that rather than being "
        "handed a frame translation of nonsense")
