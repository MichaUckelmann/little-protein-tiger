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
