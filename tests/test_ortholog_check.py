"""
Ortholog classification, epitope conservation, and the membrane-side inference
that replaced the hardcoded "extracellular" default.

The real case throughout is 5GRS — the *S. pombe* SREBP-SCAP complex a MASH/MASLD
run picked for a human SCAP/SREBF1 target. It is the reason all three exist:
the chain-assignment guard hard-failed it as "the wrong molecule" when it is in
fact a legitimate ortholog whose EPITOPE happens not to be conserved, which is a
different verdict reached for a different reason.
"""

from __future__ import annotations

import pytest

from src.ortholog_check import (MATCH, MISMATCH, ORTHOLOG, ChainVerdict,
                                HotspotConservation, _same_protein_name,
                                classify_chain)


# ---------------------------------------------------------------------------
# classify_chain — the ortholog / mismatch discrimination
# ---------------------------------------------------------------------------

def test_sifts_accession_hit_is_the_protein_whatever_the_identity():
    """A modelled fragment can score low against a full-length reference; if
    SIFTS maps it to the accession itself, that settles it."""
    v = classify_chain(identity=0.31, uniprot="Q12770", gene="SCAP",
                       chain_accessions=["Q12770"])
    assert v.verdict == MATCH


def test_high_identity_is_the_protein():
    v = classify_chain(identity=0.97, uniprot="P46937", gene="YAP1",
                       chain_accessions=[])
    assert v.verdict == MATCH


def test_no_sequence_comparison_is_unknown_not_mismatch():
    v = classify_chain(identity=None, uniprot="Q12770", gene="SCAP")
    assert v.verdict == "unknown"


def test_unrelated_chain_is_a_mismatch(monkeypatch):
    """The PD-L1 incident's shape: a VHH nanobody assigned as the target."""
    monkeypatch.setattr("src.ortholog_check.uniprot_entry", lambda acc, **k: {
        "Q9NZQ7": {"full_name": "Programmed cell death 1 ligand 1",
                   "organism": "Homo sapiens", "taxid": 9606, "gene": "CD274"},
    }.get(acc, {}))
    v = classify_chain(identity=0.21, uniprot="Q9NZQ7", gene="CD274",
                       description="Nanobody KN035", chain_accessions=[])
    assert v.verdict == MISMATCH
    assert "different molecule" in v.reason


def test_ortholog_recognised_by_identical_uniprot_protein_name(monkeypatch):
    """
    The discriminator that makes this work at all: O43043 (*S. pombe* Scp1) and
    Q12770 (human SCAP) carry the byte-identical UniProt recommended name,
    while their gene symbols differ (`scp1` vs `SCAP`) and their sequences are
    only 29% identical — squarely in the same band as an unrelated chain.
    """
    name = "Sterol regulatory element-binding protein cleavage-activating protein"
    monkeypatch.setattr("src.ortholog_check.uniprot_entry", lambda acc, **k: {
        "Q12770": {"full_name": name, "organism": "Homo sapiens",
                   "taxid": 9606, "gene": "SCAP"},
        "O43043": {"full_name": name,
                   "organism": "Schizosaccharomyces pombe",
                   "taxid": 284812, "gene": "scp1"},
    }.get(acc, {}))
    v = classify_chain(identity=0.29, uniprot="Q12770", gene="SCAP",
                       description=name, chain_accessions=["O43043"])
    assert v.verdict == ORTHOLOG
    assert v.is_ortholog
    assert v.chain_uniprot == "O43043"
    assert v.taxid == 284812


def test_ortholog_below_the_identity_floor_is_a_mismatch(monkeypatch):
    """A name match alone is not enough: RCSB descriptions are free text and
    can name a binding partner in passing."""
    name = "Sterol regulatory element-binding protein cleavage-activating protein"
    monkeypatch.setattr("src.ortholog_check.uniprot_entry", lambda acc, **k: {
        "Q12770": {"full_name": name, "taxid": 9606},
        "X99999": {"full_name": name, "taxid": 1234},
    }.get(acc, {}))
    v = classify_chain(identity=0.08, uniprot="Q12770", gene="SCAP",
                       description=name, chain_accessions=["X99999"])
    assert v.verdict == MISMATCH


def test_rcsb_description_is_the_fallback_when_no_accession(monkeypatch):
    name = "Sterol regulatory element-binding protein cleavage-activating protein"
    monkeypatch.setattr("src.ortholog_check.uniprot_entry",
                        lambda acc, **k: {"full_name": name, "taxid": 9606})
    v = classify_chain(identity=0.29, uniprot="Q12770", gene="SCAP",
                       description=name, chain_accessions=[])
    assert v.verdict == ORTHOLOG


@pytest.mark.parametrize("a,b,same", [
    ("Sterol regulatory element-binding protein cleavage-activating protein",
     "Sterol regulatory element-binding protein cleavage-activating protein", True),
    ("Caspase-2", "Caspase 2", True),
    ("Caspase-2", "Caspase-6", False),
    ("", "Caspase-2", False),
])
def test_protein_name_comparison_is_punctuation_insensitive(a, b, same):
    assert _same_protein_name(a, b) is same


# ---------------------------------------------------------------------------
# HotspotConservation arithmetic
# ---------------------------------------------------------------------------

def test_unaligned_rows_count_against_conservation():
    """A hotspot that could not be placed in the human sequence is not
    evidence of conservation, so it must dilute the fraction rather than be
    dropped from the denominator."""
    c = HotspotConservation(
        rows=[{}, {}, {}, {}], conserved=2, similar=1, different=0, unaligned=1)
    assert c.total == 4
    assert c.fraction_conserved == 0.5
    assert c.fraction_conserved_or_similar == 0.75


def test_empty_conservation_is_zero_not_a_division_error():
    c = HotspotConservation()
    assert c.total == 0
    assert c.fraction_conserved == 0.0


# ---------------------------------------------------------------------------
# The trim stage's membrane-side inference
# ---------------------------------------------------------------------------

class _FakeTopo:
    def __init__(self, kinds: dict[int, str], is_membrane=True, fetched=True):
        self._kinds = kinds
        self.is_membrane = is_membrane
        self.fetched = fetched

    def kind_at(self, pos):
        return self._kinds.get(pos)


def _runner():
    from src.pipeline_runner import PipelineRunner

    return PipelineRunner.__new__(PipelineRunner)


def _patch_alignment(monkeypatch, mapping):
    monkeypatch.setattr("src.membrane_topology.uniprot_to_auth",
                        lambda *a, **k: mapping)


def test_side_is_inferred_from_the_hotspots_not_defaulted(monkeypatch):
    """
    The whole point of the change: SCAP is an ER membrane protein whose
    designable interface faces the CYTOSOL. The old code defaulted to
    "extracellular" and hard-failed exactly this case.
    """
    from src.membrane_topology import CYTOPLASMIC, EXTRACELLULAR

    _patch_alignment(monkeypatch, {10: 110, 11: 111, 12: 112})
    topo = _FakeTopo({10: CYTOPLASMIC, 11: CYTOPLASMIC, 12: EXTRACELLULAR})
    hs = [{"auth_seq_id": 110}, {"auth_seq_id": 111}]
    assert _runner()._infer_membrane_side("1ABC", "A", "Q12770", hs, topo) \
        == CYTOPLASMIC


def test_extracellular_epitope_still_infers_extracellular(monkeypatch):
    from src.membrane_topology import CYTOPLASMIC, EXTRACELLULAR

    _patch_alignment(monkeypatch, {10: 110, 11: 111})
    topo = _FakeTopo({10: EXTRACELLULAR, 11: CYTOPLASMIC})
    assert _runner()._infer_membrane_side(
        "1ABC", "A", "P00000", [{"auth_seq_id": 110}], topo) == EXTRACELLULAR


def test_hotspot_inside_the_membrane_still_fails(monkeypatch):
    """The one topology rule that is not a matter of side: that surface is
    buried in lipid whichever face the rest of the epitope is on."""
    from src.membrane_topology import TRANSMEMBRANE
    from src.pipeline_runner import PipelineError

    _patch_alignment(monkeypatch, {10: 110})
    topo = _FakeTopo({10: TRANSMEMBRANE})
    with pytest.raises(PipelineError, match="INSIDE"):
        _runner()._infer_membrane_side("1ABC", "A", "P00000",
                                       [{"auth_seq_id": 110}], topo)


def test_hotspots_on_both_faces_disable_the_restriction(monkeypatch):
    """No single binder engages an epitope spanning the membrane; restricting
    to either face would silently discard half of it."""
    from src.membrane_topology import CYTOPLASMIC, EXTRACELLULAR

    _patch_alignment(monkeypatch, {10: 110, 11: 111})
    topo = _FakeTopo({10: EXTRACELLULAR, 11: CYTOPLASMIC})
    assert _runner()._infer_membrane_side(
        "1ABC", "A", "P00000",
        [{"auth_seq_id": 110}, {"auth_seq_id": 111}], topo) is None


def test_soluble_protein_is_never_restricted(monkeypatch):
    _patch_alignment(monkeypatch, {})
    topo = _FakeTopo({}, is_membrane=False)
    assert _runner()._infer_membrane_side(
        "1ABC", "A", "P01116", [{"auth_seq_id": 12}], topo) is None


def test_no_sifts_alignment_does_not_restrict(monkeypatch):
    """Fail-open: an unmappable structure must not silently shrink the target."""
    _patch_alignment(monkeypatch, {})
    topo = _FakeTopo({10: "cytoplasmic"})
    assert _runner()._infer_membrane_side(
        "1ABC", "A", "P00000", [{"auth_seq_id": 110}], topo) is None


# ---------------------------------------------------------------------------
# The conservation gate
# ---------------------------------------------------------------------------

def _gate_runner(verdict, human_acc="Q12770"):
    r = _runner()
    r.config = {"paths": {"structures_dir": "data/structures"}}
    r._ortholog = verdict
    r._ortholog_human_acc = human_acc
    r._project = None
    r._round_id = None
    return r


_HOTSPOTS = ('{"target_chain": "A", "residues": '
             '[{"auth_seq_id": 567, "residue": "GLY"}]}')


def test_gate_is_a_no_op_when_the_chain_was_not_an_ortholog():
    from src.pipeline_runner import PipelineResult

    res = PipelineResult(run_dir=None)
    _gate_runner(ChainVerdict(verdict=MATCH))._check_ortholog_conservation(
        _HOTSPOTS, "5GRS", res)
    assert res.ortholog_conservation is None


def test_gate_raises_when_the_epitope_is_not_conserved(monkeypatch):
    from src.pipeline_runner import PipelineError, PipelineResult

    cons = HotspotConservation(rows=[{"auth_seq_id": 567, "residue": "GLY",
                                      "human_aa": "R", "human_auth": 670,
                                      "status": "different"}],
                               conserved=0, different=1, method="test")
    monkeypatch.setattr("src.ortholog_check.hotspot_conservation",
                        lambda *a, **k: cons)
    monkeypatch.setattr("src.ortholog_check.fetch_alphafold_model",
                        lambda *a, **k: None)
    res = PipelineResult(run_dir=None)
    v = ChainVerdict(verdict=ORTHOLOG, chain_uniprot="O43043",
                     organism="Schizosaccharomyces pombe", identity=0.29)
    with pytest.raises(PipelineError, match="NOT conserved"):
        _gate_runner(v)._check_ortholog_conservation(_HOTSPOTS, "5GRS", res)
    # The table is recorded even on the failure path — the operator needs the
    # human-numbered equivalents to pick a different site.
    assert res.ortholog_conservation["fraction_conserved"] == 0.0


def test_gate_passes_a_conserved_epitope_and_records_the_alphafold_model(
        monkeypatch, tmp_path):
    from src.pipeline_runner import PipelineResult

    cons = HotspotConservation(
        rows=[{"auth_seq_id": i, "residue": "GLY", "human_aa": "G",
               "human_auth": i, "status": "conserved"} for i in range(10)],
        conserved=9, different=1, method="test")
    monkeypatch.setattr("src.ortholog_check.hotspot_conservation",
                        lambda *a, **k: cons)
    af = tmp_path / "AF-Q12770-F1.cif"
    af.write_text("data_AF")
    monkeypatch.setattr("src.ortholog_check.fetch_alphafold_model",
                        lambda *a, **k: af)
    res = PipelineResult(run_dir=None)
    v = ChainVerdict(verdict=ORTHOLOG, chain_uniprot="O43043", identity=0.29)
    _gate_runner(v)._check_ortholog_conservation(_HOTSPOTS, "5GRS", res)
    assert res.ortholog_conservation["fraction_conserved"] == 0.9
    assert res.ortholog_conservation["human_alphafold_model"] == str(af)


def test_gate_fails_open_when_conservation_cannot_be_computed(monkeypatch):
    """A UniProt outage must not halt a run — only a MEASURED shortfall does."""
    from src.pipeline_runner import PipelineResult

    def boom(*a, **k):
        raise RuntimeError("uniprot down")

    monkeypatch.setattr("src.ortholog_check.hotspot_conservation", boom)
    res = PipelineResult(run_dir=None)
    v = ChainVerdict(verdict=ORTHOLOG, chain_uniprot="O43043")
    _gate_runner(v)._check_ortholog_conservation(_HOTSPOTS, "5GRS", res)
    assert res.ortholog_conservation is None
