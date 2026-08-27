"""Official gene symbols outrank synonyms, and ambiguity is refused.

These pin a rule that is easy to regress into a silent tie-break: before it
existed, a synonym claimed by several genes resolved to whichever symbol came
first ALPHABETICALLY. That is how `p65` meant GORASP1 rather than RELA, `p50`
meant ARHGEF7 rather than NFKB1, and `NAP1` — a yeast name — meant ACOT8, in a
corpus whose own search keywords include "NAP1 AND chaperone".

The rule now has two halves:
  1. a synonym that is some OTHER gene's approved symbol is never indexed, so
     BAP1 always means the deubiquitinase and never RNF2, which lists it;
  2. if several approved genes still claim a synonym, resolve to nothing
     rather than guess — and do not let a weaker tier rescue it.

Needs the reference data (scripts/fetch_reference_data.py); skipped without it.
"""
from __future__ import annotations

import pytest

from src.identifier_normalizer import ReferenceDataMissing, get_normalizer


@pytest.fixture(scope="module")
def norm():
    try:
        return get_normalizer()
    except (ReferenceDataMissing, FileNotFoundError):
        pytest.skip("reference data not installed")


# --- half one: the official symbol wins -----------------------------------

def test_a_synonym_never_beats_another_genes_official_symbol(norm):
    """BAP1 is listed as a synonym of RNF2 and is also its own approved gene."""
    r = norm.resolve("BAP1")
    assert r.human_gene_symbol == "BAP1"
    assert r.match_confidence == "exact_gene"


def test_unambiguous_synonyms_still_resolve(norm):
    for raw, expect in [("RING1B", "RNF2"), ("p53", "TP53"),
                        ("PD-L1", "CD274"), ("MERLIN", "NF2")]:
        assert norm.resolve(raw).human_gene_symbol == expect, raw


def test_official_symbols_are_untouched(norm):
    for sym in ["RNF2", "BAP1", "KRAS", "TEAD1", "NF2", "EZH2", "NAP1L1"]:
        r = norm.resolve(sym)
        assert r.human_gene_symbol == sym
        assert r.match_confidence == "exact_gene"


# --- half two: refuse rather than guess -----------------------------------

@pytest.mark.parametrize("raw", ["NAP1", "p50", "p85", "Ras", "AP-1"])
def test_ambiguous_synonyms_resolve_to_nothing(norm, raw):
    """No curated answer and several claimants — refuse rather than guess.

    NAP1 is here on purpose: it is the yeast name, the human paralogues are
    NAP1L*, and nothing in the name says which. Better absent from a human
    interaction map than present as the wrong protein.
    """
    r = norm.resolve(raw)
    assert r.human_gene_symbol is None, f"{raw} guessed {r.human_gene_symbol}"
    assert r.filtered_reason == "ambiguous_synonym"


@pytest.mark.parametrize("raw,never", [("NAP1", "ACOT8"), ("p65", "GORASP1"),
                                       ("p50", "ARHGEF7"), ("p85", "ARHGEF7"),
                                       ("p62", "DCTN4"), ("KAP1", "KIFAP3"),
                                       ("NRF2", "GABPA"), ("p97", "CFDP1")])
def test_the_specific_alphabetical_accidents_do_not_come_back(norm, raw, never):
    assert norm.resolve(raw).human_gene_symbol != never


# --- curated overrides: where refusing would lose an answer not in doubt ---

@pytest.mark.parametrize("raw,expect", [
    ("PD-1", "PDCD1"), ("p21", "CDKN1A"), ("p27", "CDKN1B"), ("CBP", "CREBBP"),
    ("p62", "SQSTM1"), ("p65", "RELA"), ("p38", "MAPK14"), ("p97", "VCP"),
    ("KAP1", "TRIM28"), ("NRF2", "NFE2L2"), ("TRF2", "TERF2"), ("Drp1", "DNM1L"),
    ("S6", "RPS6"), ("GCN5", "KAT2A"), ("CAF-1", "CHAF1A"), ("CD25", "IL2RA"),
])
def test_curated_biology_beats_both_the_guess_and_the_refusal(norm, raw, expect):
    r = norm.resolve(raw)
    assert r.human_gene_symbol == expect
    assert r.match_confidence == "curated_alias"


def test_a_refusal_is_not_rescued_by_a_weaker_tier(norm):
    """UniProt carries unreviewed entries literally gene-named NAP1 and P65.

    Falling through to them would trade a wrong human gene for a worse one.
    """
    assert norm._by_gene.get("NAP1"), "precondition: UniProt has a NAP1 entry"
    assert norm.resolve("NAP1").human_gene_symbol is None


def test_paralog_shorthand_still_works(norm):
    """YAP must stay YAP1 — HGNC maps YAP to YY1AP1, which is why the
    paralog-default tier runs before the alias tier."""
    assert norm.resolve("YAP").human_gene_symbol == "YAP1"


def test_stats_report_what_the_rule_did(norm):
    s = norm.resolution_stats()
    assert s["approved_symbols"] > 40_000
    assert s["synonyms_dropped"] > 0, "no collisions dropped — is the rule wired?"
