"""A contig can name more than one target chain.

`build_contig` took ONE chain and emitted `f"{chain}{lo}-{hi}"` for every
span, so a two-chain molecular-glue target could not be expressed at all —
the generator-side blocker that made fixing anything upstream of it moot.

What makes the two-chain form work without touching scoring is RFD3's own
merge rule: `/0` is the chain-INCREMENT token, so every span after it lands in
ONE output chain whatever input chains they came from. Measured on 4ZGM
(Stage 1 of the scope): `70-86,/0,A29-128,B10-37` yields exactly two output
chains, binder A and target B numbered 1..128, with all 128
`diffused_index_map` entries — 100 keyed `A*`, 28 keyed `B*` — mapping into
`B`. So `binder_metrics`, `binder_ranking`, the `chain_pair_*` `[0][1]` read
and ipSAE all still see one binder against one target.
"""

from __future__ import annotations

import json
import pathlib

from src.foundry_spec import parse_contig
from src.structure_trim import build_contig, build_contig_multi

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_single_chain_spelling_is_unchanged():
    """`test_build_contig_shape` pins this literal; it must not move."""
    assert build_contig([(42, 145)], "B", 68, 86) == "68-86,/0,B42-145"


def test_a_two_chain_contig_names_both():
    assert build_contig_multi({"A": [(29, 128)], "B": [(10, 37)]}, 70, 86) \
        == "70-86,/0,A29-128,B10-37"


def test_there_is_exactly_one_chain_break_token_however_many_chains():
    """`/0` increments the output chain; a second one would split the target.

    A target split across two output chains breaks every consumer that reads
    `chain_pair_*[0][1]` as the one interface.
    """
    for mapping in ({"A": [(29, 128)]},
                    {"A": [(29, 128)], "B": [(10, 37)]},
                    {"A": [(195, 229), (239, 411)], "B": [(10, 37)], "C": [(1, 9)]}):
        assert build_contig_multi(mapping, 70, 86).count("/0") == 1


def test_mapping_order_is_the_contig_order():
    """Load-bearing: it fixes the output 1..N numbering, and
    `_run_cluster_stage` takes `spans[0][0]` as the target chain.

    `dict` preserves insertion order in CPython but nothing in the code says
    so, which is why this is asserted rather than assumed.
    """
    assert build_contig_multi({"A": [(29, 128)], "B": [(10, 37)]}, 70, 86) \
        == "70-86,/0,A29-128,B10-37"
    assert build_contig_multi({"B": [(10, 37)], "A": [(29, 128)]}, 70, 86) \
        == "70-86,/0,B10-37,A29-128"


def test_multiple_segments_per_chain_stay_with_their_chain():
    assert build_contig_multi({"A": [(195, 229), (239, 411)], "B": [(10, 37)]},
                              70, 86) == "70-86,/0,A195-229,A239-411,B10-37"


def test_the_spec_parser_reads_a_two_chain_contig():
    """`foundry_spec.parse_contig` is the consumer; it already handles this."""
    binder, spans = parse_contig("70-86,/0,A29-128,B10-37")
    assert binder == (70, 86)
    assert spans == [("A", 29, 128), ("B", 10, 37)]


def test_every_shipped_contig_is_a_single_chain_round_trip():
    """The corpus proof that the refactor changed nothing that already exists.

    All 53 `trim_map.json` contigs on disk parse back to exactly
    `{target_chain: kept_segments}` and rebuild byte-identically.
    """
    maps = sorted(set(list(_ROOT.glob("projects/**/trim_map.json"))
                      + list(_ROOT.glob("outputs/**/trim_map.json"))))
    checked, bad = 0, []
    for p in maps:
        contig = json.loads(p.read_text()).get("contig")
        if not contig:
            continue
        checked += 1
        binder, spans = parse_contig(contig)
        chains = {c for c, _, _ in spans}
        if len(chains) != 1:
            bad.append((str(p), contig, "multi-chain"))
            continue
        rebuilt = build_contig([(lo, hi) for _, lo, hi in spans],
                               chains.pop(), *binder)
        if rebuilt != contig:
            bad.append((str(p), contig, rebuilt))
    if not checked:
        import pytest
        pytest.skip("no shipped trim maps in this checkout")
    assert bad == [], f"{len(bad)} of {checked} contigs changed: {bad[:3]}"
