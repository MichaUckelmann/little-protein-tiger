"""A trim can carry a co-target, but only when it cuts nothing.

`TrimResult` described one chain. `kept_segments`, the three counts and the
`retained` flag in `trim_map.json` were all target-chain-only, so a
molecular-glue target could not be described even though `write_trimmed`
already writes both chains (`keep_map[partner_chain] = None` keeps it whole).

The no-op refusal is what makes this safe rather than merely additive. Every
remaining measurement in `trim_target` — `_exposed_hydrophobic`,
`_per_residue_bsa`, `bsa_retention`, `min_bsa_retention`, `allowed_auth` — is
written against ONE chain, and broadening any of them to assembly context
would change a measurement the 13 calibrated campaigns were checked against
(risk R2). With the trim a no-op they are all trivially correct because
nothing was cut. A co-target that must actually be CUT is scope item 7,
Stage 6, and refuses here naming it.

`n_residues_after` is the open half of scope item 18, and it is the one that
costs GPU: `pipeline_runner` sizes a campaign from `trim.n_residues_after +
_binder_midpoint(trim.contig)`, so on 4ZGM a target-chain-only count gives
100+78 = 178 tokens against the true 128+78 = 206 — a 22% under-count fed
into a law with exponent 2.56, and through `choose_compute()` into the
local-vs-cluster decision.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from src.pipeline_runner import _TrimFromDisk
from src.structure_trim import TrimError, trim_target

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_4ZGM = _ROOT / "data/structures/4ZGM_ba1.cif"
_3KYS = _ROOT / "data/structures/3KYS_ba1.cif"

_HOTSPOTS = [
    {"residue": "LYS", "auth_seq_id": 113, "rfd3_atoms": "NZ,CE", "chain": "A"},
    {"residue": "TRP", "auth_seq_id": 120, "rfd3_atoms": "CD2,NE1", "chain": "A"},
    {"residue": "ALA", "auth_seq_id": 30, "rfd3_atoms": "CB", "chain": "B"},
    {"residue": "VAL", "auth_seq_id": 33, "rfd3_atoms": "CG1,CG2", "chain": "B"},
]

_needs_4zgm = pytest.mark.skipif(not _4ZGM.exists(), reason="4ZGM not in this checkout")


def _trim(**kw):
    base = dict(structure_path=_4ZGM, target_chain="A", partner_chain="B",
                hotspots=_HOTSPOTS, budget=500,
                out_dir=pathlib.Path(tempfile.mkdtemp()), pdb_id="4ZGM",
                binder_min=70, binder_max=86, co_target_chains=["B"])
    base.update(kw)
    return trim_target(**base)


# ------------------------------------------------------------- the two-chain trim

@_needs_4zgm
def test_a_co_target_reaches_the_contig():
    r = _trim()
    assert r.contig == "70-86,/0,A29-128,B10-37"
    assert r.kept_by_chain == {"A": [(29, 128)], "B": [(10, 37)]}


@_needs_4zgm
def test_kept_segments_keeps_its_exact_current_meaning():
    """NOT deprecated, NOT a flattening — the primary chain's spans."""
    assert _trim().kept_segments == [(29, 128)]


@_needs_4zgm
def test_the_counts_cover_every_chain_the_contig_names():
    """Scope item 18. 128, not 100 — the number that sizes the campaign."""
    r = _trim()
    assert r.n_residues_after == 128
    assert r.n_segments == 2, "two physically separate molecules, two spans"


@_needs_4zgm
def test_a_co_target_hotspot_is_judged_against_its_own_chain():
    """On 4ZGM the collisions are real, not hypothetical.

    Chain B's hotspots at auth 30 and 33 fall inside chain A's kept range
    29-128, so a bare `set[int]` records them retained against the wrong
    molecule; a chain-B residue outside that range would raise "trim lost
    hotspot(s)" for a residue that is present on the chain it belongs to.
    """
    r = _trim()
    assert len(r.hotspots_retained) == 4 and r.hotspots_lost == []

    lost = _trim(hotspots=_HOTSPOTS + [
        {"residue": "GLY", "auth_seq_id": 11, "rfd3_atoms": "CA", "chain": "B"}])
    assert lost.hotspots_lost == [], "B11 is inside B10-37 and is retained"


# ---------------------------------------------------------------- the four refusals

@_needs_4zgm
def test_a_co_target_that_is_not_the_partner_is_refused():
    with pytest.raises(TrimError, match="scope item 7"):
        _trim(co_target_chains=["Z"])


@_needs_4zgm
def test_two_co_targets_are_refused():
    with pytest.raises(TrimError, match="scope item 7"):
        _trim(co_target_chains=["B", "C"])


@_needs_4zgm
def test_allowed_auth_with_a_co_target_is_refused():
    """A bare set of target-chain author ids is meaningless on a second chain."""
    with pytest.raises(TrimError, match="scope item 14"):
        _trim(allowed_auth={29, 30, 31})


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not in this checkout")
def test_a_co_target_on_a_trim_that_would_cut_is_refused():
    """THE guard. Every single-chain measurement below it is then a no-op.

    3KYS chain A is 208 residues; a budget of 150 forces a real cut, which is
    exactly the case Stage 3 must not attempt.
    """
    with pytest.raises(TrimError, match="NO-OP"):
        trim_target(structure_path=_3KYS, target_chain="A", partner_chain="B",
                    hotspots=[{"residue": "MET", "auth_seq_id": 347,
                               "rfd3_atoms": "CB,CA", "chain": "A"}],
                    budget=150, out_dir=pathlib.Path(tempfile.mkdtemp()),
                    pdb_id="3KYS", co_target_chains=["B"],
                    max_exposed_hydrophobic=None)


# ------------------------------------------------------------- the persisted form

@_needs_4zgm
def test_the_trim_map_records_both_chains_and_round_trips():
    r = _trim()
    mapping = json.loads(pathlib.Path(r.mapping_path).read_text())
    assert mapping["kept_by_chain"] == {"A": [[29, 128]], "B": [[10, 37]]}
    assert all(h["retained"] for h in mapping["hotspots"])

    back = _TrimFromDisk(mapping)
    assert back.kept_by_chain == {"A": [(29, 128)], "B": [(10, 37)]}
    assert back.n_residues_after == 128
    assert back.contig == r.contig


def test_a_legacy_trim_map_reconstructs_the_single_chain_mapping():
    """Every one of the 53 trim maps on disk predates the key."""
    back = _TrimFromDisk({"target_chain": "A", "kept_segments": [[27, 110]],
                          "contig": "68-86,/0,A27-110"})
    assert back.kept_by_chain == {"A": [(27, 110)]}


def test_a_mapping_with_no_target_chain_reconstructs_nothing():
    """`{"": [...]}` is truthy, and step 9's cross-check would refuse on it.

    `tests/test_audit_fixes.py` builds exactly this shape, so the fallback has
    to be safe by construction rather than by luck.
    """
    back = _TrimFromDisk({"kept_segments": [[27, 110]]})
    assert back.kept_by_chain == {}
    assert back.kept_segments == [(27, 110)], "the old field is untouched"


def test_a_stored_zero_segment_count_stays_zero():
    """`.get(k, default)`, not `or`."""
    assert _TrimFromDisk({"n_segments": 0, "kept_segments": [[1, 5]]}).n_segments == 0
