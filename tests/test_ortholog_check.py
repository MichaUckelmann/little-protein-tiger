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


# ---------------------------------------------------------------------------
# search_corpus recall on a rare category
# ---------------------------------------------------------------------------

def test_a_rare_study_category_is_not_silently_filtered_to_nothing(tmp_path):
    """
    Post-filtering ran the ANN search first and dropped non-matching rows
    afterwards, so a filtered query only ever saw the `limit` globally-nearest
    rows: any category rarer than ~1-in-limit came back empty, and the caller
    could not tell that apart from "the corpus has no such papers". Two
    categories dominate this corpus (90% between them) which is why it
    survived — and the starved case was exactly the one the literature skill
    is told to prefer.

    Built here as a synthetic table so the assertion is about the query, not
    about whatever happens to be in `data/vectors`.
    """
    pytest.importorskip("lancedb")
    import lancedb

    dim = 8
    rows = []
    # 200 rows of the common category clustered at the query vector, and 5 of
    # a rare one further away — the exact shape that starves a post-filter.
    for i in range(200):
        rows.append({"vector": [1.0] + [0.0] * (dim - 1),
                     "study_category": "biochemistry", "paper_key": f"c{i}"})
    for i in range(5):
        rows.append({"vector": [0.9, 0.1] + [0.0] * (dim - 2),
                     "study_category": "structural_biology", "paper_key": f"r{i}"})
    db = lancedb.connect(str(tmp_path / "db"))
    table = db.create_table("t", data=rows)

    query = [1.0] + [0.0] * (dim - 1)
    where = "study_category = 'structural_biology'"
    post = table.search(query).metric("cosine").limit(5).where(
        where, prefilter=False).to_arrow().num_rows
    pre = table.search(query).metric("cosine").limit(5).where(
        where, prefilter=True).to_arrow().num_rows

    assert post == 0, "fixture no longer reproduces the starvation it guards"
    assert pre == 5


def test_vector_store_uses_prefilter():
    """The one-word setting the test above is about, pinned at the call site."""
    import inspect

    from src.vector_store import VectorStore

    calls = [ln.strip() for ln in inspect.getsource(VectorStore.search).splitlines()
             if ".where(" in ln and "prefilter" in ln]
    assert calls, "no prefilter-bearing .where() call in VectorStore.search"
    assert all("prefilter=True" in c for c in calls), calls


# ---------------------------------------------------------------------------
# The trim's marginal-overshoot fallback
# ---------------------------------------------------------------------------

def test_residue_count_matches_the_trim_s_own_definition(tmp_path):
    """Counted by BACKBONE, like structure_trim — so the number compared
    against the budget is the number the trim compares against it."""
    from pathlib import Path

    from src.pipeline_runner import PipelineRunner

    cif = Path("data/structures/5GN0_ba1.cif")
    if not cif.exists():
        pytest.skip("5GN0 not downloaded")
    assert PipelineRunner._target_chain_residue_count(cif, "A") == 222
    assert PipelineRunner._target_chain_residue_count(cif, "Z") == 0


@pytest.mark.parametrize("n_target,budget,fires", [
    (222, 220, True),    # the real TEAD4 case: 2 residues over
    (253, 220, True),    # exactly at the 15% ceiling
    (254, 220, False),   # past it — a real cut is genuinely needed
    (200, 220, False),   # under budget: the trim never runs at all
    (0, 220, False),     # chain unreadable: do not guess
])
def test_overshoot_fallback_band(n_target, budget, fires):
    """
    The band the fallback covers. A campaign died because a 222-residue TEAD4
    had to shed two residues and the only cut that did opened hydrophobic core
    inside 10 A of the epitope. Keeping the target whole is strictly safer for
    the design and costs only GPU time; a target far over budget still has to
    be cut, and that decision stays with the operator.
    """
    ceiling = round(budget * 1.15)
    assert bool(n_target and budget < n_target <= ceiling) is fires


def test_a_close_ortholog_is_still_an_ortholog(monkeypatch):
    """
    Accession before identity. Mouse Tead4 (Q62296) is 96% identical to human
    TEAD4 — past every "same protein" threshold — so an identity-first test
    calls it a match and never checks the epitope against the human sequence.
    It happens to be 12/12 conserved on that target, but that is a
    measurement, and the only way to have it is to take it.
    """
    name = "Transcriptional enhancer factor TEF-3"
    monkeypatch.setattr("src.ortholog_check.uniprot_entry", lambda acc, **k: {
        "Q15561": {"full_name": name, "organism": "Homo sapiens",
                   "taxid": 9606, "gene": "TEAD4"},
        "Q62296": {"full_name": name, "organism": "Mus musculus",
                   "taxid": 10090, "gene": "Tead4"},
    }.get(acc, {}))
    v = classify_chain(identity=0.96, uniprot="Q15561", gene="TEAD4",
                       description=name, chain_accessions=["Q62296"])
    assert v.verdict == ORTHOLOG
    assert v.taxid == 10090


def test_a_human_chain_at_high_identity_is_a_match_not_an_ortholog(monkeypatch):
    """The accession-first rule must not turn every structure into an
    ortholog: a human accession under a different id is still human."""
    name = "Transcriptional enhancer factor TEF-3"
    monkeypatch.setattr("src.ortholog_check.uniprot_entry", lambda acc, **k: {
        "Q15561": {"full_name": name, "organism": "Homo sapiens",
                   "taxid": 9606, "gene": "TEAD4"},
        "Q15562": {"full_name": name, "organism": "Homo sapiens",
                   "taxid": 9606, "gene": "TEAD2"},
    }.get(acc, {}))
    v = classify_chain(identity=0.96, uniprot="Q15561", gene="TEAD4",
                       description=name, chain_accessions=["Q15562"])
    assert v.verdict == MATCH


def test_the_targets_own_accession_short_circuits_before_any_fetch(monkeypatch):
    """No UniProt round-trip when SIFTS already names the target accession."""
    def boom(*a, **k):
        raise AssertionError("uniprot_entry should not be called here")

    monkeypatch.setattr("src.ortholog_check.uniprot_entry", boom)
    assert classify_chain(identity=0.4, uniprot="Q15561", gene="TEAD4",
                          chain_accessions=["Q15561"]).verdict == MATCH


# ---------------------------------------------------------------------------
# get_fingerprint's LLM-facing view, and transport parity
# ---------------------------------------------------------------------------

def test_the_fields_the_skill_is_told_to_read_survive():
    """
    The view used to strip `methodology` and
    `contradictions_and_negative_results` as "never used by any skill".
    molecular-biology-expert reads both by name, so under the CLI transport it
    saw their absence as "this paper reports no contradictions".
    """
    from pathlib import Path

    from src.skill_runner import _llm_fingerprint

    skill = Path("skills/molecular-biology-expert/SKILL.md").read_text()
    view = _llm_fingerprint({
        "key_findings": [], "methodology": {"experimental_methods_used": ["NMR"]},
        "contradictions_and_negative_results": [{"finding": "x"}],
        "protein_identifiers": {"native_taxon_resolved": 6239, "entries": [1] * 50},
        "curation_metadata": {"model": "x"},
    })
    for field in ("methodology", "contradictions_and_negative_results"):
        assert field in skill, f"skill no longer reads {field}; revisit the view"
        assert field in view


def test_the_sidecar_no_skill_reads_is_dropped_but_its_organism_kept():
    """`protein_identifiers` is 36.5% of a fingerprint and is read only by
    _corpus_graph — except for the one field in it the model wants and has
    never been shown."""
    from src.skill_runner import _llm_fingerprint

    view = _llm_fingerprint(
        {"protein_identifiers": {"native_taxon_resolved": 6239, "entries": [1] * 50}})
    assert "protein_identifiers" not in view
    assert view["native_organism"] == "Caenorhabditis elegans (6239)"

    unknown = _llm_fingerprint({"protein_identifiers": {"native_taxon_resolved": 999999}})
    assert unknown["native_organism"] == "999999"
    assert "native_organism" not in _llm_fingerprint({"key_findings": []})


def test_both_transports_return_the_same_fingerprint_view():
    """They diverged silently: the CLI stripped three blocks, MCP stripped
    none, so one skill behaved differently depending on how it was invoked."""
    import re
    from pathlib import Path

    mcp = Path("src/mcp_server.py").read_text()
    body = re.search(r"def get_fingerprint\(.*?(?=\n@mcp\.tool)", mcp, re.S)
    assert body, "get_fingerprint not found in the MCP server"
    assert "_llm_fingerprint(fp)" in body.group(0)


# ---------------------------------------------------------------------------
# The analyst may not upgrade a deterministic verdict
# ---------------------------------------------------------------------------

class _FakeStats:
    n_records = 1352
    n_survivors = 317

    def render(self):
        return "clash_severe 391\nhotspot_engagement 342\nbinder_rmsd_dock 206"


class _FakeRanking:
    filter_stats = _FakeStats()
    top_k = list(range(20))


def _summary_runner(monkeypatch, handoff, captured):
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner.__new__(PipelineRunner)
    r._BINDER_STAGE_FILES = PipelineRunner._BINDER_STAGE_FILES
    monkeypatch.setattr(r, "_write_binder_fasta", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(r, "_slim_binder_top_k", lambda *a, **k: "name,iptm\nd1,0.9",
                        raising=False)

    def fake_run_stage(skill, q, ctx, out, **kw):
        captured["query"] = q
        return handoff

    monkeypatch.setattr(r, "_run_stage", fake_run_stage, raising=False)
    return r


def _result(go):
    from src.pipeline_runner import PipelineResult

    res = PipelineResult(run_dir=None)
    res.go_recommendation = go
    return res


class _Calib:
    verdict = "STOP"
    verdict_reason = "0 hits in 344 refolds; rule-of-three ceiling 0.9%"


def test_analyst_cannot_upgrade_a_calibration_stop(monkeypatch, tmp_path):
    """
    `_run_binder_track` sets NO_GO from the calibration verdict, then calls
    this stage to report on what the trial DID produce. The assignment used to
    be unconditional, so an LLM looking at a flattering top-20 — which looks
    good by construction, that being what a top-20 is — flipped a measured
    STOP back to GO.
    """
    captured = {}
    dirs = {"binder": tmp_path, "scoring": tmp_path}
    r = _summary_runner(monkeypatch, {"go_recommendation": "GO"}, captured)
    res = _result("NO_GO")
    r._stage_binder_summary(tmp_path / "top_k.csv", {}, dirs, res,
                            ranking=_FakeRanking(), calib={"result": _Calib()})
    assert res.go_recommendation == "NO_GO"


def test_analyst_verdict_still_applies_on_a_healthy_campaign(monkeypatch, tmp_path):
    captured = {}
    dirs = {"binder": tmp_path, "scoring": tmp_path}
    r = _summary_runner(monkeypatch, {"go_recommendation": "conditional-go"},
                        captured)
    res = _result("GO")
    r._stage_binder_summary(tmp_path / "top_k.csv", {}, dirs, res)
    assert res.go_recommendation == "CONDITIONAL_GO"


def test_a_junk_verdict_does_not_overwrite_anything(monkeypatch, tmp_path):
    captured = {}
    dirs = {"binder": tmp_path, "scoring": tmp_path}
    r = _summary_runner(monkeypatch, {"go_recommendation": "looks great!"},
                        captured)
    res = _result("GO")
    r._stage_binder_summary(tmp_path / "top_k.csv", {}, dirs, res)
    assert res.go_recommendation == "GO"


def test_the_analyst_is_told_what_the_run_already_decided(monkeypatch, tmp_path):
    """It used to see only target gene, partner, intent and the top-20 — no
    verdict, no funnel. Both completed campaigns wrote 'No red flags
    identified' over funnels that discarded ~75% of refolds."""
    captured = {}
    dirs = {"binder": tmp_path, "scoring": tmp_path}
    r = _summary_runner(monkeypatch, {"go_recommendation": "GO"}, captured)
    res = _result("NO_GO")
    res.hotspot_residues_json = '{"target_chain":"A","residues":[{"auth_seq_id":1},{"auth_seq_id":2}]}'
    r._stage_binder_summary(tmp_path / "top_k.csv", {}, dirs, res,
                            ranking=_FakeRanking(), calib={"result": _Calib()})
    q = captured["query"]
    assert "Track: foundry" in q
    assert "STOP" in q and "rule-of-three" in q
    assert "may not be\nGO" in q or "may not be GO" in q
    assert "1,352" in q and "317" in q          # the funnel, not just the top-K
    assert "clash_severe 391" in q              # the drop reasons
    assert "2 hotspots" in q and "0.75" in q    # engagement is a fraction


def test_missing_ranking_and_calib_do_not_break_the_stage(monkeypatch, tmp_path):
    """A resume enters this stage with neither in hand."""
    captured = {}
    dirs = {"binder": tmp_path, "scoring": tmp_path}
    r = _summary_runner(monkeypatch, {"go_recommendation": "GO"}, captured)
    res = _result("INCOMPLETE")
    r._stage_binder_summary(tmp_path / "top_k.csv", {}, dirs, res)
    assert res.go_recommendation == "GO"
    assert "Track: foundry" in captured["query"]


# ---------------------------------------------------------------------------
# The LLM-facing view of a structure-tool result
# ---------------------------------------------------------------------------

def _iface_fixture():
    return {
        "interface": {"chain_a": "A", "chain_b": "I", "bsa_total_A2": 5785.1,
                      "n_hbonds": 23,
                      "bsa_per_residue": [
                          {"residue": "GLY", "chain": "A", "resnum": 567,
                           "sasa_free_A2": 92.9, "sasa_complex_A2": 12.0,
                           "bsa_A2": 80.9}]},
        "chain_a_interface_residues": [
            {"residue": "TRP", "chain": "A", "resnum": 568, "one_letter": "W",
             "type": "aromatic", "hydrophobicity": -0.9, "n_contacts": 2,
             "gap_flag": False, "ddg_estimate_kcal_mol": 1.4,
             "contacts": [{"target_res": "SER", "target_chain": "I",
                           "target_resnum": 1004, "min_dist_A": 3.4,
                           "interaction": "h_bond"}]}],
        "chain_b_interface_residues": [],
        "chain_a_categories": {"aromatic": 1},
        "plddt_at_interface": {},
    }


def test_the_view_keeps_every_field_a_skill_names():
    """
    `binder-optimizer` reads `one_letter` and `hydrophobicity` off each
    interface residue by name. `hydrophobicity` is a pure Kyte-Doolittle
    lookup on the residue name and so is information-theoretically redundant —
    it stays anyway, because the alternative is the model recalling the KD
    scale from memory while choosing hydrophobic hotspots.
    """
    from pathlib import Path

    from src._tool_views import llm_view_interface

    skill = Path("skills/binder-optimizer/SKILL.md").read_text()
    row = llm_view_interface(_iface_fixture())["chain_a_interface_residues"][0]
    for field in ("residue", "resnum", "one_letter", "type", "hydrophobicity",
                  "n_contacts", "gap_flag", "contacts"):
        assert field in skill, f"skill stopped naming {field}; revisit the view"
        assert field in row, f"the view dropped {field}, which the skill reads"
    assert row["contacts"][0]["min_dist_A"] == 3.4
    assert row["ddg_estimate_kcal_mol"] == 1.4


def test_the_view_drops_only_constants_and_a_retained_difference():
    from src._tool_views import llm_view_interface

    v = llm_view_interface(_iface_fixture())
    assert "chain" not in v["chain_a_interface_residues"][0]
    assert "target_chain" not in v["chain_a_interface_residues"][0]["contacts"][0]
    per_res = v["interface"]["bsa_per_residue"][0]
    assert "sasa_free_A2" not in per_res and "sasa_complex_A2" not in per_res
    # ...but the value derived from them is what the prompts read, and stays.
    assert per_res["bsa_A2"] == 80.9
    # Chain identity is still available where it is not a per-row constant.
    assert v["interface"]["chain_a"] == "A"


def test_get_sequence_map_is_left_alone():
    """Whitespace only. `residues[]` is the anti-hallucination lookup: models
    have twice produced systematically wrong label_seq_ids by counting."""
    from src._tool_views import llm_view_sequence_map

    r = {"sequence": "ACD", "auth_to_label": {"1": 1},
         "residues": [{"auth_seq_id": 1, "three_letter": "ALA"}]}
    assert llm_view_sequence_map(r) == r


def test_compact_dumps_is_lossless_and_smaller():
    import json

    from src._tool_views import dumps

    obj = _iface_fixture()
    assert json.loads(dumps(obj)) == obj
    assert len(dumps(obj)) < len(json.dumps(obj, indent=2))


def test_an_error_result_passes_through_untouched():
    from src._tool_views import llm_view_interface

    err = {"error": "chain Z not found"}
    assert llm_view_interface(err) == err


def test_both_transports_use_the_same_view():
    """They drifted once already — `get_fingerprint` stripped three blocks on
    one transport and nothing on the other."""
    from pathlib import Path

    for f in ("src/skill_runner.py", "src/structure_tools_server.py"):
        src = Path(f).read_text()
        assert "llm_view_interface(result)" in src, f
        assert "json.dumps(result, indent=2)" not in src, \
            f"{f} still pretty-prints a structure-tool result"


# ---------------------------------------------------------------------------
# label_seq_id: a lookup, never a derivation
# ---------------------------------------------------------------------------

_LSQ_TABLE = (
    "### MODEL-READY HOTSPOTS\n\n"
    "| Residue | auth_seq_id | label_seq_id | atoms |\n|---|---|---|---|\n"
    "| GLU | 256 | 53 | CD,OE1 |\n| VAL | 258 | 55 | CG1,CG2 |\n"
    "| GLN | 262 | 59 | CD,OE1 |\n| ASP | 265 | 62 | CG,OD1 |\n\n"
    "#### BoltzGen binding\nbinding: 53,55,59,62\n"
)


def _lsq_runner():
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner.__new__(PipelineRunner)
    r.config = {"paths": {"structures_dir": "data/structures"}}
    return r


def _cif():
    from pathlib import Path

    p = Path("data/structures/5GN0_ba1.cif")
    if not p.exists():
        pytest.skip("5GN0 not downloaded")
    return p


def test_counted_label_seq_ids_are_replaced_in_table_and_binding_line():
    """BoltzGen reads `binding:` AS label_seq, so a value the model counted
    constrains the binder to the wrong residues."""
    fixed, _ = _lsq_runner()._resolve_unverified_label_seq_ids(
        _LSQ_TABLE, _cif(), "A")
    assert "binding: 54,56,60,63" in fixed
    assert "| 54 |" in fixed and "| 53 |" not in fixed


def test_a_uniform_offset_is_reported_as_confabulation_not_as_typos():
    """Every disagreement identical means the column was DERIVED, not read.
    Worth separating from a scattered single-residue miss, because it says
    something about the rest of the report."""
    _, warns = _lsq_runner()._resolve_unverified_label_seq_ids(
        _LSQ_TABLE, _cif(), "A")
    systematic = [w for w in warns if w.startswith("SYSTEMATIC")]
    assert len(systematic) == 1
    assert "+1" in systematic[0] and "COUNTING" in systematic[0]


def test_a_table_the_parser_cannot_read_raises_instead_of_no_opping():
    """The failure mode that left `_verify_hotspot_grounding` inert for
    months: a guard that runs, matches nothing, and tells no one. The table
    format lives in a SKILL.md that gets edited."""
    from src.pipeline_runner import PipelineError

    drifted = "### MODEL-READY HOTSPOTS\n\nGLU256 -> label 53\nVAL258 -> label 55\n"
    with pytest.raises(PipelineError, match="did not match the expected"):
        _lsq_runner()._resolve_unverified_label_seq_ids(drifted, _cif(), "A")


def test_an_unverifiable_chain_raises_when_there_is_a_table_to_verify():
    from src.pipeline_runner import PipelineError

    with pytest.raises(PipelineError, match="cannot build the auth->label map"):
        _lsq_runner()._resolve_unverified_label_seq_ids(_LSQ_TABLE, _cif(), "ZZZ")


def test_no_table_means_nothing_to_correct_and_nothing_to_raise():
    text = "no hotspots in this report"
    out, warns = _lsq_runner()._resolve_unverified_label_seq_ids(
        text, _cif(), "ZZZ")
    assert out == text and warns == []


def test_both_tracks_correct_the_numbering():
    """Same skill, same table, same failure — the correction lived only on the
    PPI track, which is the track-parity gap shape CLAUDE.md documents for the
    PD-L1 verify guards."""
    import inspect

    from src.pipeline_runner import PipelineRunner

    for fn in (PipelineRunner._stage_structure,
               PipelineRunner._stage_binder_interface):
        assert "_correct_label_seq_ids" in inspect.getsource(fn), fn.__name__


def test_the_correction_is_not_inside_the_swallowing_try():
    """It used to be, so any exception — including one the correction itself
    raised — became 'could not parse hotspot residues' and the counted values
    reached the design spec."""
    import inspect

    from src.pipeline_runner import PipelineRunner

    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(PipelineRunner._stage_structure)))

    def calls_within(node):
        return {n.func.attr for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}

    swallowing = [t for t in ast.walk(tree) if isinstance(t, ast.Try)
                  and any("could not parse hotspot residues" in ast.dump(h)
                          for h in t.handlers)]
    assert swallowing, "the swallowing try/except is gone; revisit this test"
    for t in swallowing:
        assert "_correct_label_seq_ids" not in calls_within(t), \
            "the numbering correction is back inside the try that swallows " \
            "its exceptions as a warning"
    assert "_correct_label_seq_ids" in calls_within(tree)


def test_a_structure_without_label_seq_is_never_given_a_derived_number():
    """
    label_seq is an mmCIF concept: EVERY residue of a PDB-format file has
    none — including `trim/trimmed.pdb`, which this pipeline writes itself and
    hands to the design stage — and a real mmCIF still has none on het rows
    (5 of 227 on 5GN0). The map used to fall back to the residue's 1-indexed
    position in the chain, which is the very counting this guard exists to
    catch, wearing a lab coat.

    The number is still knowable — BoltzGen synthesises one and
    `boltzgen_residue_indices` reproduces that exactly — so the cell is filled
    from THAT, not from whatever the model wrote. The marker is only for the
    case where even that cannot be computed.
    """
    from pathlib import Path

    from src.pipeline_runner import _LABEL_SEQ_UNAVAILABLE

    pdb = Path("projects/mash_e2e/runs/round-2/binder/trim/trimmed.pdb")
    if not pdb.exists():
        pytest.skip("no trimmed.pdb on disk")
    fixed, warns = _lsq_runner()._resolve_unverified_label_seq_ids(
        _LSQ_TABLE, pdb, "A")
    # The model wrote 53,55,59,62 by counting; those happen to be right for
    # THIS file, but they are now the computed values rather than the copied
    # ones, and the report says which file they belong to.
    assert "binding: 53,55,59,62" in fixed
    assert _LABEL_SEQ_UNAVAILABLE not in fixed
    assert any("carries no label_seq" in w for w in warns)
    # auth_seq_id is untouched — it is what RFD3's select_hotspots uses.
    assert "| 256 |" in fixed and "| 265 |" in fixed


def test_the_marker_is_used_when_no_index_can_be_computed_at_all():
    """Belt and braces: if BoltzGen indexing also fails, the cell must not
    carry a number. Anything numeric here reads downstream as measured."""
    from pathlib import Path

    from src.pipeline_runner import _LABEL_SEQ_UNAVAILABLE

    pdb = Path("projects/mash_e2e/runs/round-2/binder/trim/trimmed.pdb")
    if not pdb.exists():
        pytest.skip("no trimmed.pdb on disk")
    import src.pipeline_runner as pr
    real = pr.PipelineRunner._build_label_seq_id_map

    def no_labels(cif, chain):
        return {a: (None, n) for a, (_, n) in real(cif, chain).items()}

    r = _lsq_runner()
    import src.structure_tools as stools
    orig = stools.boltzgen_residue_indices
    stools.boltzgen_residue_indices = lambda *a, **k: {}
    pr.PipelineRunner._build_label_seq_id_map = staticmethod(no_labels)
    try:
        fixed, _ = r._resolve_unverified_label_seq_ids(_LSQ_TABLE, pdb, "A")
    finally:
        stools.boltzgen_residue_indices = orig
        pr.PipelineRunner._build_label_seq_id_map = staticmethod(real)
    assert f"| {_LABEL_SEQ_UNAVAILABLE} |" in fixed
    assert "binding: 53,55,59,62" not in fixed


def test_missing_label_seq_is_not_a_run_ending_failure():
    """A PDB target is a normal input, not an error. Only a chain that is not
    in the file at all is."""
    from pathlib import Path

    pdb = Path("projects/mash_e2e/runs/round-2/binder/trim/trimmed.pdb")
    if not pdb.exists():
        pytest.skip("no trimmed.pdb on disk")
    fixed, _ = _lsq_runner()._resolve_unverified_label_seq_ids(
        _LSQ_TABLE, pdb, "A")
    assert fixed  # returned normally


def test_the_map_reports_absence_as_none_not_as_a_position():
    from pathlib import Path

    from src.pipeline_runner import PipelineRunner

    pdb = Path("projects/mash_e2e/runs/round-2/binder/trim/trimmed.pdb")
    if not pdb.exists():
        pytest.skip("no trimmed.pdb on disk")
    m = PipelineRunner._build_label_seq_id_map(pdb, "A")
    assert m, "chain A should be present in the trimmed target"
    assert all(label is None for label, _ in m.values())
    # ...while a real mmCIF still yields real numbers.
    cif = PipelineRunner._build_label_seq_id_map(_cif(), "A")
    assert cif[256][0] == 54


# ---------------------------------------------------------------------------
# BoltzGen's own residue indexing
# ---------------------------------------------------------------------------

_HS_AUTH = [256, 258, 262, 265, 290, 292, 384, 407, 418, 422]


def test_boltzgen_index_reads_label_seq_from_an_mmcif():
    from src.structure_tools import boltzgen_residue_indices

    idx = boltzgen_residue_indices(str(_cif()), "A", _HS_AUTH)
    assert [idx[a] for a in _HS_AUTH] == [54, 56, 60, 63, 88, 90, 182, 205, 216, 220]


def test_boltzgen_index_synthesises_one_for_a_pdb_the_way_boltzgen_does():
    """A PDB file has no label_seq, so `pdb_parser.py` builds `full_sequence`
    from the polymer subchain, aligns, and assigns `sc[i].label_seq = j + 1`."""
    from pathlib import Path

    from src.structure_tools import boltzgen_residue_indices

    pdb = Path("projects/mash_e2e/runs/round-2/binder/trim/trimmed.pdb")
    if not pdb.exists():
        pytest.skip("no trimmed.pdb on disk")
    idx = boltzgen_residue_indices(str(pdb), "A", _HS_AUTH)
    assert [idx[a] for a in _HS_AUTH] == [53, 55, 59, 62, 87, 89, 181, 204, 215, 219]


def test_the_same_residue_gets_a_different_index_in_a_different_file():
    """The whole reason this function exists: a `binding:` list is meaningful
    relative to ONE file. Copying it from a report written about another is an
    off-by-one straight into the design spec."""
    from pathlib import Path

    from src.structure_tools import boltzgen_residue_indices

    pdb = Path("projects/mash_e2e/runs/round-2/binder/trim/trimmed.pdb")
    if not pdb.exists():
        pytest.skip("no trimmed.pdb on disk")
    assert boltzgen_residue_indices(str(_cif()), "A", [256])[256] == 54
    assert boltzgen_residue_indices(str(pdb), "A", [256])[256] == 53


def test_a_pdb_target_gets_real_numbers_not_the_unavailable_marker():
    from pathlib import Path

    from src.pipeline_runner import _LABEL_SEQ_UNAVAILABLE

    pdb = Path("projects/mash_e2e/runs/round-2/binder/trim/trimmed.pdb")
    if not pdb.exists():
        pytest.skip("no trimmed.pdb on disk")
    fixed, warns = _lsq_runner()._resolve_unverified_label_seq_ids(
        _LSQ_TABLE, pdb, "A")
    binding = [ln for ln in fixed.splitlines() if ln.startswith("binding")][0]
    assert binding == "binding: 53,55,59,62"
    assert _LABEL_SEQ_UNAVAILABLE not in fixed
    # ...and the file-relative caveat is still stated.
    assert any("valid for" in w and "ONLY" in w for w in warns)


def test_the_design_skill_no_longer_says_copy_verbatim():
    from pathlib import Path

    md = Path("skills/protein-design-script/SKILL.md").read_text()
    assert "copied\n  verbatim from MODEL-READY HOTSPOTS" not in md
    assert "only valid for the file" in md


# ---------------------------------------------------------------------------
# The measured prefilter rate has to reach the NEXT stage's plan
# ---------------------------------------------------------------------------

def _dirs(root):
    b = root / "binder"
    return {"binder": b, "campaign": b / "campaign",
            "pilot": b / "campaign/pilot",
            "calibration": b / "campaign/calibration",
            "production": b / "campaign/production",
            "trim": b / "trim", "spec": b / "spec", "scoring": b / "scoring"}


def test_calibration_is_planned_on_the_rate_the_pilot_measured(monkeypatch, tmp_path):
    """
    The pilot measured 0.86 and calibration was planned at the 0.59 default:
    580 backbones as int(580*0.59)*4 = 1,368 refolds where the real figure is
    int(580*0.86)*4 = 1,992. A 46% under-count of the work, and with it the
    disk clamp, the GPU-hour estimate, and the local-vs-cluster decision.
    """
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner.__new__(PipelineRunner)
    d = _dirs(tmp_path)
    d["calibration"].mkdir(parents=True)
    monkeypatch.setattr(
        "src.foundry_runner.prefilter_rate_observed",
        lambda paths: 0.86 if "pilot" in str(paths.campaign_dir) else 0.0)
    monkeypatch.setattr(r, "_binder_paths",
                        lambda dirs, m: type("P", (), {
                            "campaign_dir": dirs["campaign"] / m})(),
                        raising=False)
    assert r._persisted_prefilter_rate(d, "calibration") == 0.86


def test_calibration_json_still_wins_for_production(monkeypatch, tmp_path):
    """A resumed `--start-from production` in a fresh process reads the rate
    calibration froze, not a re-measurement."""
    import json as _json

    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner.__new__(PipelineRunner)
    d = _dirs(tmp_path)
    d["calibration"].mkdir(parents=True)
    (d["calibration"] / "calibration.json").write_text(
        _json.dumps({"prefilter_rate": 0.83}))
    monkeypatch.setattr("src.foundry_runner.prefilter_rate_observed",
                        lambda paths: 0.5)
    assert r._persisted_prefilter_rate(d, "production") == 0.83


@pytest.mark.parametrize("bad", [0.0, -1.0, 1.5, None, "x"])
def test_a_nonsense_rate_is_refused_rather_than_propagated(bad, tmp_path):
    import json as _json

    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner.__new__(PipelineRunner)
    d = _dirs(tmp_path)
    d["calibration"].mkdir(parents=True)
    (d["calibration"] / "calibration.json").write_text(
        _json.dumps({"prefilter_rate": bad}))
    r._binder_paths = lambda dirs, m: type("P", (), {
        "campaign_dir": dirs["campaign"] / m})()
    assert r._persisted_prefilter_rate(d, "pilot") == 0.0


def test_the_pilot_has_no_earlier_stage_to_read():
    from src.pipeline_runner import PipelineRunner
    import inspect

    src = inspect.getsource(PipelineRunner._persisted_prefilter_rate)
    assert 'order[:order.index(mode)]' in src, \
        "the earlier-stage walk must be ordered, so pilot reads nothing"


# ---------------------------------------------------------------------------
# The gate must cost a campaign the way the planner does
# ---------------------------------------------------------------------------

def _fake_rows(n=2000, hits=10):
    """
    Records shaped like binder_metrics output, with `hits` EXCELLENT ones on
    `hits` DISTINCT backbones — backbones are the sizing unit, and the four
    sequences sharing one are correlated rather than independent trials, so
    clustering the hits would fall under MIN_HITS_FOR_ESTIMATE and the verdict
    would be ITERATE for a reason that has nothing to do with cost.
    """
    rows = []
    for i in range(n):
        good = i % 4 == 0 and i // 4 < hits
        rows.append({
            "name": f"d_{i}", "design_family": f"bb_{i // 4}",
            "iptm": 0.95 if good else 0.30,
            "ipsae_min": 0.7 if good else 0.1,
            "iface_pae": 3.0 if good else 20.0,
            "binder_plddt": 0.90 if good else 0.50,
            "binder_rmsd_dock": 1.0 if good else 30.0,
            "binder_rmsd_fold": 1.0 if good else 10.0,
            "epitope_recall": 0.9 if good else 0.1,
            "hotspot_engagement": 1.0 if good else 0.1,
            "clash_severe": 0,
        })
    return rows


def test_the_refold_rate_actually_moves_the_cost():
    from src.campaign_calibration import calibrate

    rows = _fake_rows()
    cheap = calibrate(rows, n_seq=4, prefilter_rate=0.9, sec_per_rf3_refold=8.4)
    dear = calibrate(rows, n_seq=4, prefilter_rate=0.9, sec_per_rf3_refold=20.6)
    assert cheap.pessimistic.required_refolds == dear.pessimistic.required_refolds
    assert dear.pessimistic.est_gpu_hours > cheap.pessimistic.est_gpu_hours * 1.8


def test_a_realistic_refold_rate_can_flip_the_verdict():
    """
    The MASH/TEAD4 shape: the gate costed 22,197 refolds at the flat 8.4 s
    anchor as 63 GPU-h and called it "inside the 120 h budget", while
    `plan_campaign` costed the same refolds at the measured 20.6 s as 138 —
    outside it. SCALE_UP and an auto-raised excellence bar were both decided
    on a number 2.45x too low.
    """
    from src.campaign_calibration import calibrate

    rows = _fake_rows(n=2108, hits=10)
    kw = dict(n_seq=4, prefilter_rate=0.909, disk_budget_gb=120.0,
              max_campaign_days=5.0, target_designs=50)
    cheap = calibrate(rows, sec_per_rf3_refold=8.4, **kw)
    dear = calibrate(rows, sec_per_rf3_refold=20.6, **kw)
    assert cheap.verdict == "SCALE_UP"
    assert dear.verdict != "SCALE_UP", (
        "a 2.45x more expensive refold must not still read as fully affordable")


def test_omitting_the_rate_keeps_the_old_anchor():
    """Back-compat: a caller that cannot measure gets the documented anchor."""
    from src.campaign_calibration import SEC_PER_RF3_REFOLD, calibrate

    rows = _fake_rows()
    a = calibrate(rows, n_seq=4, prefilter_rate=0.9)
    b = calibrate(rows, n_seq=4, prefilter_rate=0.9,
                  sec_per_rf3_refold=SEC_PER_RF3_REFOLD)
    assert a.pessimistic.est_gpu_hours == b.pessimistic.est_gpu_hours


def test_the_gate_is_given_the_planners_rate():
    """
    The two cost models drifted because only one of them was ever fixed.

    The size-law fallback now lives inside `calibrate()` rather than being
    spelled out at this one call site, so the guarantee is stronger than it
    was: ANY caller that supplies `n_tokens` gets the planner's law, and one
    that supplies neither a rate nor a size is warned rather than silently
    costed at the 195-token anchor. The call site's job is reduced to handing
    over the two things only it knows — a measured rate if one exists, and the
    complex size.
    """
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner._stage_calibration)
    assert "sec_per_rf3_refold=" in src
    # ...preferring, in order, the same rates plan_campaign prefers.
    assert "sec_per_refold_observed(paths)" in src
    assert "_earlier_refold_rate" in src
    # `or None`, not `or 0.0`: sec_per_refold_observed returns 0.0 when it has
    # too few timestamps, and calibrate() must see None to reach the size law.
    assert "or None" in src
    assert "n_tokens=" in src


def test_calibrate_owns_the_size_law_so_it_cannot_drift_again():
    """One anchor and one law, shared with the planner by import."""
    import src.campaign_calibration as cc
    import src.foundry_runner as fr
    from src.campaign_calibration import calibrate

    assert cc.SEC_PER_RF3_REFOLD == fr.SEC_PER_RF3_REFOLD

    rows = _fake_rows()
    anchor = calibrate(rows, n_seq=4, prefilter_rate=0.9, n_tokens=fr.REF_TOKENS)
    big = calibrate(rows, n_seq=4, prefilter_rate=0.9, n_tokens=300)
    assert big.pessimistic.est_gpu_hours > anchor.pessimistic.est_gpu_hours
