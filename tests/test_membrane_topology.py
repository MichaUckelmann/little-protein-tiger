"""Membrane topology: fetching, mapping, and what it excludes."""

from __future__ import annotations

import pytest

from src.membrane_topology import (
    CYTOPLASMIC, EXTRACELLULAR, SIGNAL, TRANSMEMBRANE, Topology,
    TopologyRestriction, TopologySegment, _classify, check_hotspots,
    restriction_for,
)

# Requires network; skipped automatically when unavailable.
requires_net = pytest.mark.skipif(
    not pytest.importorskip("requests"), reason="requests unavailable")


def _topo(*segs) -> Topology:
    return Topology(uniprot="TEST",
                    segments=[TopologySegment(*s) for s in segs], fetched=True)


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------

@pytest.mark.parametrize("ftype,desc,expect", [
    ("Transmembrane", "Helical", TRANSMEMBRANE),
    ("Topological domain", "Extracellular", EXTRACELLULAR),
    ("Topological domain", "Cytoplasmic", CYTOPLASMIC),
    ("Topological domain", "Lumenal", EXTRACELLULAR),   # topologically outside
    ("Topological domain", "Periplasmic", EXTRACELLULAR),
    ("Signal", "", SIGNAL),
    ("Chain", "whatever", None),
])
def test_feature_classification(ftype, desc, expect):
    assert _classify(ftype, desc) == expect


def test_membrane_detection_needs_a_transmembrane_segment():
    assert not _topo((EXTRACELLULAR, 1, 100, "")).is_membrane
    assert _topo((TRANSMEMBRANE, 101, 121, "Helical")).is_membrane


def test_soluble_topology_describes_itself():
    assert "soluble" in _topo((EXTRACELLULAR, 1, 50, "")).describe()


# ----------------------------------------------------------------------
# What gets excluded
# ----------------------------------------------------------------------

def _restriction(side=EXTRACELLULAR, mapping=None, topo=None, monkeypatch=None):
    import src.membrane_topology as mt

    topo = topo or _topo((SIGNAL, 1, 18, ""), (EXTRACELLULAR, 19, 100, ""),
                         (TRANSMEMBRANE, 101, 121, "Helical"),
                         (CYTOPLASMIC, 122, 160, ""))
    mapping = mapping if mapping is not None else {i: i for i in range(1, 161)}
    monkeypatch.setattr(mt, "uniprot_to_auth", lambda *a, **k: mapping)
    return restriction_for("1ABC", "A", "P00000", side=side, topology=topo)


def test_transmembrane_is_excluded_on_the_extracellular_side(monkeypatch):
    r = _restriction(monkeypatch=monkeypatch)
    assert r.applies
    assert 50 in r.allowed_auth                 # extracellular
    assert 110 not in r.allowed_auth            # transmembrane
    assert 130 not in r.allowed_auth            # cytoplasmic
    assert r.n_transmembrane_excluded == 21


def test_transmembrane_is_excluded_on_the_cytoplasmic_side_too(monkeypatch):
    """
    An exposed TM helix is a hydrophobic slab that attracts binders which cannot
    work in a membrane — that is true whichever side you are designing against.
    """
    r = _restriction(side=CYTOPLASMIC, monkeypatch=monkeypatch)
    assert r.applies
    assert 130 in r.allowed_auth
    assert 110 not in r.allowed_auth
    assert 50 not in r.allowed_auth
    assert r.n_transmembrane_excluded == 21


def test_a_signal_peptide_does_not_trigger_a_restriction(monkeypatch):
    """
    A cleaved signal peptide is not unreachable, and treating it as an exclusion
    would fire the restriction on ectodomain constructs that need none.
    """
    # A membrane protein (so the soluble short-circuit does not fire) whose
    # crystallised construct is the ectodomain plus its signal peptide — the
    # common case for PD-L1 and TREM2 structures.
    r = _restriction(mapping={i: i for i in range(1, 101)},
                     monkeypatch=monkeypatch)
    assert not r.applies
    assert "entirely extracellular" in r.note


def test_side_any_disables_the_restriction():
    r = restriction_for("1ABC", "A", "P00000", side="any")
    assert not r.applies


def test_a_soluble_protein_is_never_restricted():
    r = restriction_for("1ABC", "A", "P00000", side=EXTRACELLULAR,
                        topology=_topo((EXTRACELLULAR, 1, 100, "")))
    assert not r.applies
    assert "soluble" in r.note


def test_unavailable_topology_imposes_nothing():
    """Not knowing must never silently shrink the target."""
    r = restriction_for("1ABC", "A", "P00000", topology=Topology("P00000"))
    assert not r.applies
    assert "unavailable" in r.note


def test_a_restriction_that_would_empty_the_target_is_refused(monkeypatch):
    topo = _topo((TRANSMEMBRANE, 1, 20, "Helical"), (CYTOPLASMIC, 21, 60, ""))
    r = _restriction(topo=topo, mapping={i: i for i in range(1, 61)},
                     monkeypatch=monkeypatch)
    assert not r.applies
    assert "WARNING" in r.note


def test_filter_is_a_no_op_when_the_restriction_does_not_apply():
    r = TopologyRestriction(False, EXTRACELLULAR)
    assert r.filter([5, 1, 3]) == [1, 3, 5]


# ----------------------------------------------------------------------
# Hotspots
# ----------------------------------------------------------------------

def test_hotspots_in_a_transmembrane_helix_are_flagged(monkeypatch):
    r = _restriction(monkeypatch=monkeypatch)
    topo = _topo((SIGNAL, 1, 18, ""), (EXTRACELLULAR, 19, 100, ""),
                 (TRANSMEMBRANE, 101, 121, "Helical"),
                 (CYTOPLASMIC, 122, 160, ""))
    mapping = {i: i for i in range(1, 161)}
    bad, reasons = check_hotspots(
        [{"residue": "LEU", "auth_seq_id": 50},
         {"residue": "ILE", "auth_seq_id": 110}], r, topo, mapping)
    assert [h["auth_seq_id"] for h in bad] == [110]
    assert "transmembrane" in reasons[0]
    assert "cannot work in a membrane" in reasons[0]


def test_no_hotspots_flagged_without_a_restriction():
    bad, reasons = check_hotspots(
        [{"auth_seq_id": 999}], TopologyRestriction(False, EXTRACELLULAR),
        Topology("X"))
    assert bad == [] and reasons == []


# ----------------------------------------------------------------------
# Live lookups
# ----------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.network
def test_real_uniprot_topology_for_the_test_targets():
    from src.membrane_topology import fetch_topology

    pdl1 = fetch_topology("Q9NZQ7")
    assert pdl1.is_membrane
    assert any(s.kind == EXTRACELLULAR and s.start == 19 for s in pdl1.segments)

    kras = fetch_topology("P01116")
    assert kras.fetched and not kras.is_membrane
