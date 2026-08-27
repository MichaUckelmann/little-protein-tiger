"""The shared map renderer: determinism, and that a drawn map stays readable.

The layout is the reason this file exists. It is a physics simulation with no
RNG, and every report that draws a map depends on it producing the SAME picture
for the same graph — otherwise regenerating a report churns the figure.
"""
from __future__ import annotations

import math

import pytest

from src.network_svg import Edge, PALETTE, layout, legend_items, render_svg

NODES = ["A", "B", "C", "D", "E", "F", "G", "H"]
EDGES = [
    Edge("A", "B", 0.55, 12), Edge("A", "C", -0.31, 4), Edge("B", "C", None, 1),
    Edge("C", "D", 0.22, 7), Edge("D", "E", 0.41, 3), Edge("E", "F", None, 2),
    Edge("F", "G", -0.18, 5), Edge("G", "H", 0.33, 9), Edge("A", "H", 0.27, 6),
]
META = {"A": {"is_seed": True, "total_mentions": 40}, "D": {"is_seed": True}}


def test_layout_is_deterministic():
    assert layout(NODES, EDGES) == layout(NODES, EDGES)


def test_layout_stays_inside_the_canvas():
    pos = layout(NODES, EDGES, width=760, height=470)
    for x, y in pos.values():
        assert 46 <= x <= 760 - 46
        assert 26 <= y <= 470 - 26


def test_nodes_do_not_pile_up():
    """A map whose nodes overlap is unreadable, whatever the data says."""
    pos = layout(NODES, EDGES)
    for i, a in enumerate(NODES):
        for b in NODES[i + 1:]:
            assert math.dist(pos[a], pos[b]) > 26, f"{a}/{b} collide"


def test_edge_colour_encodes_the_sign_of_r():
    svg = render_svg(NODES, EDGES, META)
    assert PALETTE["positive"] in svg
    assert PALETTE["negative"] in svg
    assert PALETTE["unknown"] in svg          # the "no DepMap pair" case
    assert 'stroke-dasharray="3 3"' in svg    # ...and it is dashed


def test_every_mark_carries_a_hover_title():
    svg = render_svg(NODES, EDGES, META)
    assert svg.count("<title>") == len(NODES) + len(EDGES)
    assert "DepMap r = +0.550" in svg
    assert "no DepMap pair" in svg


def test_seeds_are_distinguishable_from_pulled_in_nodes():
    svg = render_svg(NODES, EDGES, META)
    assert 'class="nl seed"' in svg
    assert svg.count('class="nl seed"') == 2


def test_empty_graph_does_not_explode():
    assert layout([], []) == {}
    assert "<svg" in render_svg([], [], {})


def test_legend_covers_every_edge_state():
    assert len(legend_items()) == 4
    colours = {c for c, _ in legend_items()}
    assert {PALETTE["positive"], PALETTE["negative"], PALETTE["unknown"]} <= colours


@pytest.mark.parametrize("n", [1, 2, 3])
def test_tiny_graphs_lay_out(n):
    nodes = NODES[:n]
    edges = [e for e in EDGES if e.source in nodes and e.target in nodes]
    assert len(layout(nodes, edges)) == n


# --- alias-aware seeding --------------------------------------------------
# The map is drawn on RAW graph nodes, one per name; the persisted DepMap edge
# index collapses those by gene symbol first. Seeding on the literal string
# therefore saw a smaller graph than the index had — the two components
# disagreed about the same data. These pin the fix.

import networkx as nx  # noqa: E402

from src._corpus_graph import _resolve_seeds, _symbol_to_nodes  # noqa: E402


def _alias_graph() -> nx.Graph:
    """YAP1 scattered over three aliases, as the real graph scatters it."""
    g = nx.Graph()
    for node, sym in [("YAP1", "YAP1"), ("YAP", "YAP1"), ("YAP/TAZ", "YAP1"),
                      ("ILK", "ILK"), ("TEAD3", "TEAD3"), ("TEAD1", "TEAD1"),
                      ("UNRELATED", "SOX2")]:
        g.add_node(node, human_gene_symbol=sym)
    g.add_edge("YAP1", "TEAD1")     # reachable from the literal string
    g.add_edge("YAP", "ILK")        # only via an alias
    g.add_edge("YAP/TAZ", "TEAD3")  # only via an alias
    return g


def test_seeding_collects_every_alias_of_the_gene():
    got, missing = _resolve_seeds(_alias_graph(), ["YAP1"])
    assert set(got) == {"YAP1", "YAP", "YAP/TAZ"}
    assert missing == []


def test_alias_seeding_reaches_partners_the_literal_string_misses():
    g = _alias_graph()
    nb = {n for s in _resolve_seeds(g, ["YAP1"])[0] for n in g.neighbors(s)}
    assert {"TEAD1", "ILK", "TEAD3"} <= nb


def test_opting_out_reproduces_the_old_narrower_behaviour():
    g = _alias_graph()
    got, _ = _resolve_seeds(g, ["YAP1"], alias_aware=False)
    assert got == ["YAP1"]
    nb = {n for s in got for n in g.neighbors(s)}
    assert "ILK" not in nb


def test_unrelated_genes_are_not_swept_in():
    got, _ = _resolve_seeds(_alias_graph(), ["YAP1"])
    assert "UNRELATED" not in got


def test_symbol_index_is_cached_on_the_graph():
    g = _alias_graph()
    assert _symbol_to_nodes(g) is _symbol_to_nodes(g)
    assert set(_symbol_to_nodes(g)["YAP1"]) == {"YAP1", "YAP", "YAP/TAZ"}


def test_a_seed_with_no_symbol_still_resolves_itself():
    g = nx.Graph()
    g.add_node("ORPHAN")  # no human_gene_symbol attribute at all
    assert _resolve_seeds(g, ["ORPHAN"])[0] == ["ORPHAN"]
