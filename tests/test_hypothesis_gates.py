"""Gates on corpus-derived hypotheses (src/hypothesis_gates.py). Offline: RCSB and the
identifier tables are injected."""
import json

import pytest

from src import hypothesis_gates as G

ALIASES = {"STING": "STING1", "TMEM173": "STING1", "MRAS": "MRAS"}


def resolve(name: str) -> str:
    n = name.strip().upper()
    return ALIASES.get(n, n)


RESP = {
    "get_genetic_codependency": '{"available": true, "r": 0.3024, "n": 1208, "best_pair": ["MALT1","BCL10"]}',
    "novelty_signal": '{"novelty": 0.6484, "classification": "DEPMAP-COUPLED"}',
    "get_fingerprint": '{"doi": "10.1126/science.abj4008", "title": "CRISPRi screen"}',
}
ENTRIES = {
    "6GK2": {"exists": True, "genes": ["MALT1", "BCL10"], "descriptions": ["BCL10", "MALT1"]},
    "1P9M": {"exists": True, "genes": ["IL6", "IL6R"], "descriptions": ["Interleukin-6", "IL-6 receptor"]},
    "9O65": {"exists": True, "genes": ["SHOC2", "MRAS", "PPP1CA"], "descriptions": ["Leucine-rich repeat protein SHOC2", "MRAS"]},
}


def cand(**kw):
    base = {"tier": "HYPOTHESIS", "complex": "MALT1 / BCL10", "track": "corpus_derived", "pdb_ids": ["6GK2"],
            "novelty_score": 0.6484,
            "evidence_chain": [{"kind": "depmap_r", "value": "0.3024", "tool": "get_genetic_codependency"},
                               {"kind": "doi", "value": "10.1126/science.abj4008", "tool": "get_fingerprint"}]}
    base.update(kw)
    return base


CANON = {"tier": "VALIDATED", "complex": "IL6 / IL6R", "track": "canonical", "pdb_ids": ["1P9M"], "evidence_chain": []}


def test_parse_choices_reads_the_handoff_line_and_survives_garbage():
    txt = "x\n- choices_json: " + json.dumps([cand()]) + "\n- other: y"
    assert G.parse_choices(txt)[0]["complex"] == "MALT1 / BCL10"
    assert G.parse_choices("- choices_json: [not json]") == []
    assert G.parse_choices("nothing here") == []


@pytest.mark.parametrize("quoted,expected", [("0.3024", True), ("0.302", True), ("r = 0.3024", True),
                                              ("0.31", False), ("0.4111", False)])
def test_number_grounding_is_format_insensitive_but_not_loose(quoted, expected):
    assert G.number_grounded(quoted, G.numbers_in(RESP["get_genetic_codependency"])) is expected


def test_grounded_chain_passes_and_a_value_found_nowhere_fails():
    ok, _, d = G.check_evidence_chain(cand(), RESP)
    assert ok and d == {"grounded": 2, "total": 2}
    bad = cand(evidence_chain=[{"kind": "depmap_r", "value": "0.2850", "tool": "get_genetic_codependency"}])
    ok, why, _ = G.check_evidence_chain(bad, RESP)
    assert not ok and "0.2850" in why


def test_a_hypothesis_must_state_a_chain_but_a_canonical_candidate_need_not():
    assert G.check_evidence_chain(cand(evidence_chain=[]), RESP)[0] is False
    assert G.check_evidence_chain(CANON, RESP)[0] is True
    stated_but_wrong = dict(CANON, evidence_chain=[{"kind": "depmap_r", "value": "0.99", "tool": "get_genetic_codependency"}])
    assert G.check_evidence_chain(stated_but_wrong, RESP)[0] is False


def test_a_doi_not_in_any_tool_response_fails():
    c = cand(evidence_chain=[{"kind": "doi", "value": "10.9999/made.up", "tool": "get_fingerprint"}])
    assert G.check_evidence_chain(c, RESP)[0] is False


def test_structure_gate_requires_both_proteins_in_one_entry():
    assert G.check_structure(cand(), ENTRIES, resolve)[0] is True
    # the SHOC2 / KRAS case: the entry holds SHOC2 and MRAS, not KRAS
    shoc2 = cand(complex="SHOC2 / KRAS", pdb_ids=["9O65"])
    ok, why = G.check_structure(shoc2, ENTRIES, resolve)
    assert not ok and "KRAS" in why
    assert G.check_structure(cand(pdb_ids=[]), ENTRIES, resolve)[0] is False
    assert G.check_structure(cand(complex="ONLYONE"), ENTRIES, resolve)[0] is False


def test_novel_gate_rejects_a_hypothesis_that_is_the_canonical_pick():
    dup = cand(complex="IL6R / IL6")
    res = G.evaluate([CANON, dup], RESP, ENTRIES, resolve)
    assert res[1].checks["novel"][0] is False and not res[1].eligible


def test_supported_gate_needs_a_grounded_doi_or_strong_depmap():
    weak = cand(evidence_chain=[{"kind": "depmap_r", "value": "0.3024", "tool": "get_genetic_codependency"}])
    assert G.evaluate([CANON, weak], RESP, ENTRIES, resolve)[1].checks["supported"][0] is True      # 0.3024 >= 0.3
    low = {"get_genetic_codependency": '{"r": 0.12}'}
    weak2 = cand(evidence_chain=[{"kind": "depmap_r", "value": "0.12", "tool": "get_genetic_codependency"}])
    assert G.evaluate([CANON, weak2], low, ENTRIES, resolve)[1].checks["supported"][0] is False


def test_policy_forwards_the_eligible_corpus_derived_candidate():
    cands = [CANON, cand()]
    out = G.select_forward(cands, G.evaluate(cands, RESP, ENTRIES, resolve))
    assert out["chosen"] == "MALT1 / BCL10" and out["track"] == "corpus_derived" and out["gated"]


def test_policy_falls_back_to_canonical_when_no_hypothesis_is_eligible():
    bad = cand(complex="SHOC2 / KRAS", pdb_ids=["9O65"])
    cands = [CANON, bad]
    out = G.select_forward(cands, G.evaluate(cands, RESP, ENTRIES, resolve))
    assert out["chosen"] == "IL6 / IL6R" and out["track"] == "canonical"
    assert out["rejected"]["SHOC2 / KRAS"] == ["structure"] or "structure" in out["rejected"]["SHOC2 / KRAS"]


def test_canonical_policy_overrides_the_novel_preference():
    cands = [CANON, cand()]
    out = G.select_forward(cands, G.evaluate(cands, RESP, ENTRIES, resolve), policy="canonical")
    assert out["chosen"] == "IL6 / IL6R"


def test_nothing_eligible_forwards_the_skill_preference_but_says_it_is_ungated():
    bad = cand(complex="SHOC2 / KRAS", pdb_ids=["9O65"])
    cands = [bad]
    out = G.select_forward(cands, G.evaluate(cands, RESP, ENTRIES, resolve), skill_preference="SHOC2 / KRAS")
    assert out["chosen"] == "SHOC2 / KRAS" and out["gated"] is False and "UNGATED" in out["reason"]


def test_unknown_policy_is_refused():
    with pytest.raises(ValueError):
        G.select_forward([CANON], G.evaluate([CANON], RESP, ENTRIES, resolve), policy="bold")


def test_a_graph_path_written_as_a_list_or_as_arrows_is_checked_against_tool_output():
    resp = {"shortest_interaction_path": '{"paths": [["KRAS", "SHOC2"]], "hops": 1}'}
    as_list = cand(evidence_chain=[{"kind": "graph_path", "value": ["KRAS", "SHOC2"], "tool": "shortest_interaction_path"}])
    as_text = cand(evidence_chain=[{"kind": "graph_path", "value": "KRAS -> SHOC2", "tool": "shortest_interaction_path"}])
    absent = cand(evidence_chain=[{"kind": "graph_path", "value": ["KRAS", "NOSUCHGENE"], "tool": "shortest_interaction_path"}])
    assert G.check_evidence_chain(as_list, resp)[0] is True
    assert G.check_evidence_chain(as_text, resp)[0] is True
    assert G.check_evidence_chain(absent, resp)[0] is False
