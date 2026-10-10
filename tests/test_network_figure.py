"""The publication network figure: gene-level collapse, evidence counting, wild-type affinities."""
import json

from src import network_figure as NF


def _cyjs(tmp_path, nodes, edges):
    p = tmp_path / "g.cyjs"
    p.write_text(json.dumps({"elements": {
        "nodes": [{"data": {"id": i, "human_gene_symbol": g}} for i, g in nodes],
        "edges": [{"data": e} for e in edges]}}))
    return p


def test_spellings_collapse_and_papers_are_a_union(tmp_path):
    p = _cyjs(tmp_path, [("KRAS", "KRAS"), ("K-RAS", "KRAS"), ("RAF1", "RAF1")], [
        {"source": "KRAS", "target": "RAF1", "dois": "10.1/a;10.1/b"},
        {"source": "K-RAS", "target": "RAF1", "dois": "10.1/b;10.1/c"}])
    edges, _ = NF.collapse(p, ["KRAS", "RAF1"])
    assert list(edges) == [("KRAS", "RAF1")]
    assert edges[("KRAS", "RAF1")].papers == 3          # not 4: one paper, two spellings


def test_wild_type_affinity_beats_a_tighter_mutant_one(tmp_path):
    p = _cyjs(tmp_path, [("KRAS", "KRAS"), ("KRASG12D", "KRAS"), ("SHOC2", "SHOC2")], [
        {"source": "KRAS", "target": "SHOC2", "tightest_kd_M": 7e-6},
        {"source": "KRASG12D", "target": "SHOC2", "tightest_kd_M": 1.5e-7}])
    g = NF.collapse(p, ["KRAS", "SHOC2"])[0][("KRAS", "SHOC2")]
    assert g.kd == 7e-6 and not g.kd_mut


def test_a_mutant_only_value_is_flagged(tmp_path):
    p = _cyjs(tmp_path, [("KRASQ61R", "KRAS"), ("SHOC2", "SHOC2")],
              [{"source": "KRASQ61R", "target": "SHOC2", "tightest_kd_M": 1.5e-7}])
    g = NF.collapse(p, ["KRAS", "SHOC2"])[0][("KRAS", "SHOC2")]
    assert g.kd == 1.5e-7 and g.kd_mut


def test_name_variants_are_not_mutants():
    assert not NF.is_variant("K-RAS", "KRAS") and not NF.is_variant("MLL1", "KMT2A")
    assert NF.is_variant("KRAS-Q61R", "KRAS") and NF.is_variant("SHOC2M173I", "SHOC2")
    assert not NF.is_variant("RAD51C", "RAD51C")        # a real symbol, not a point mutant


def test_affinity_units_never_scientific():
    assert NF.fmt_k(9.1e-7, "Ki") == "Ki 910 nM"
    assert NF.fmt_k(7e-6, "Kd") == "Kd 7 µM"
    assert "e" not in NF.fmt_k(1.54e-7, "Kd")


def test_render_is_deterministic_and_shows_the_pair_edge(tmp_path):
    p = _cyjs(tmp_path, [("A", "A"), ("B", "B"), ("C", "C")], [
        {"source": "A", "target": "B", "dois": "10.1/a", "depmap_r": 0.4, "depmap_n": 1000},
        {"source": "A", "target": "C", "dois": "10.1/b"},
        {"source": "B", "target": "C", "dois": "10.1/c"}])
    edges, _ = NF.collapse(p, ["A", "B"])
    sel = NF.select(edges, ("A", "B"))
    assert sel["shared"] == ["C"]
    a = NF.render(sel, ("A", "B"), "t", "s", "f")
    assert a == NF.render(sel, ("A", "B"), "t", "s", "f")
    assert "1 paper · r +0.40" in a


def test_an_isolated_seed_is_stated(tmp_path):
    p = _cyjs(tmp_path, [("A", "A"), ("B", "B"), ("C", "C")], [
        {"source": "A", "target": "B", "dois": "10.1/a"}, {"source": "B", "target": "C", "dois": "10.1/b"}])
    sel = NF.select(NF.collapse(p, ["A", "B"])[0], ("A", "B"))
    assert "no other partners in the corpus" in NF.render(sel, ("A", "B"), "t", "s", "f")
