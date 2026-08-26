"""
Paper quality scoring.

Score = journal_tier (0-1) × pub_type_weight (0-1) × recency_factor (0.7-1.0)
        with a source penalty for preprints.

Papers with pub_type "Congress" / "Conference" are marked with score 0.0 and
excluded from downloads entirely — they are conference abstract collections
that have PMCIDs but no individual article files on AWS.
"""

import math
import re
from .models import Paper, Source


# ---------------------------------------------------------------------------
# Journal tier lists  (lowercase normalised for matching)
# ---------------------------------------------------------------------------

# Tier 1 — flagship / high-impact journals most relevant to PPI therapeutics
_TIER1_JOURNALS: set[str] = {
    # Multidisciplinary flagships
    "nature", "science", "cell", "pnas",
    "proceedings of the national academy of sciences",
    "new england journal of medicine", "n engl j med", "nejm",
    # Nature family
    "nature chemical biology", "nat chem biol",
    "nature structural & molecular biology", "nat struct mol biol",
    "nature methods", "nat methods",
    "nature communications", "nat commun",
    "nature cell biology", "nat cell biol",
    "nature medicine", "nat med",
    "nature cancer", "nat cancer",
    "nature genetics", "nat genet",
    # Cell Press
    "cell chemical biology","cell chem biol",
    "molecular cell", "mol cell",
    "cancer cell", 
    "cell reports", "cell rep",
    "cell research", "cell res",
    "developmental cell", "dev cell",
    # Other high-impact
    "elife",
    "embo journal", "embo j",
    "journal of the american chemical society", "j am chem soc", "jacs",
    "angewandte chemie", "angew chem int ed",
    "journal of medicinal chemistry", "j med chem",
    "acs chemical biology", "acs chem biol",
    "science advances", "sci adv",
    "science translational medicine", "sci transl med",
    "nucleic acids research", "nucleic acids res",
    "cancer discovery", "cancer discov",
    "nature microbiology", "nat microbiol",
    "cell host & microbe", "cell host microbe",
    "fems microbiology reviews","fems microbiol rev",
    "current opinion microbiology", "curr opin microbiol",
    "the isme journal", "imse j",
    "circulation", 
    "jama the journal of the american medical association", "jama",
    "cardiovascular research", "cardiovasc res",
    "blood", 
    "nature immunology", "nat immunol",
    "cell systems", "cell sys", 
    "genes & development", "genes dev",
    "molecular systems biology", "mol syst biol",
    # Flagship primary-research journals, peers of the entries above.
    # Every spelling the corpus actually contains is listed — matching is
    # exact, so a missing variant is a silent 100% exclusion of that journal.
    "journal of cell biology", "j cell biol",
    "cell stem cell",
    "genome biology", "genome biol",
}

# Tier 2 — solid domain-specific journals
_TIER2_JOURNALS: set[str] = {
    "structure", "struct",
    "biochemistry",
    "journal of biological chemistry", "jbc", "j biol chem",
    "protein science", "protein sci",
    "biophysical journal", "biophys j",
    "febs journal", "febs j",
    "febs letters", "febs lett",
    # Strong specialist venues, peers of JBC / JMB / Biochemical Journal.
    "molecular and cellular biology", "mol cell biol",
    "molecular biology of the cell", "mol biol cell",
    "stem cell reports", "stem cell rep",
    "embo molecular medicine", "embo mol med",
    # "Development (Cambridge, England)" is PubMed's full form; the
    # parenthetical normalises away to "development cambridge england".
    "development", "development cambridge england", "dev camb",
    "plos genetics", "plos genet",
    "chembiochem",
    "bioorganic & medicinal chemistry", "bioorg med chem",
    "european journal of medicinal chemistry", "eur j med chem",
    "molecular pharmacology", "mol pharmacol",
    "biochemical journal", "biochem j",
    "plos biology", "plos biol",
    "plos computational biology", "plos comput biol",
    "communications biology", "commun biol",
    "cell discovery", "cell discov",
    "iscience",
    "chemmedchem",
    "chemical science",
    "organic & biomolecular chemistry",
    "drug discovery today",
    "trends in biochemical sciences", "trends biochem sci", "tibs",
    "current opinion in structural biology", "curr opin struct biol",
    "current opinion in chemical biology", "curr opin chem biol",
    "journal of molecular biology", "jmb",
    "clinical cancer research", "clin cancer res",
    "acta pharmaceutica sinica b", "acta pharm sin b",
}

# Publication types that indicate a conference abstract → exclude entirely
_CONFERENCE_PUB_TYPES: set[str] = {
    "congress",
    "conference paper",
    "meeting abstract",
    "published erratum",  # also not useful
}

# Pub type weights (highest match in the list wins)
_PUB_TYPE_WEIGHTS: dict[str, float] = {
    "journal article": 1.0,
    "research support": 1.0,   # often co-listed with "journal article"
    "review":          0.9,
    "systematic review": 0.9,
    "meta-analysis":   0.85,
    "letter":          0.6,
    "editorial":       0.4,
    "comment":         0.4,
    "news":            0.2,
    "biography":       0.1,
}


# Trailing qualifiers PubMed appends to a journal's name that carry no
# identity: the country/edition suffix on PNAS and Angewandte, chiefly.
_JOURNAL_SUFFIXES = (
    " of the united states of america",
    " engl",
    " international edition in english",
)


def _normalise(s: str) -> str:
    """Canonical form for journal-name matching.

    Matching is exact against the tier lists, so every cosmetic difference in
    how a source spells a journal is a silent exclusion. Measured against the
    real corpus, three such differences were dropping ~1,300 papers from
    journals that ARE listed:
      * a leading "The"  — "The EMBO Journal", "The Journal of Biological
        Chemistry", "The Biochemical Journal"
      * PubMed's country suffix — "Proceedings of the National Academy of
        Sciences of the United States of America" (827 papers alone)
      * double spaces left behind when punctuation is blanked out
    """
    out = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    out = re.sub(r"\s+", " ", out).strip()
    if out.startswith("the "):
        out = out[4:]
    for suffix in _JOURNAL_SUFFIXES:
        if out.endswith(suffix):
            out = out[: -len(suffix)].strip()
            break
    return out


# Run the lists through the SAME normalisation as the lookup key.
#
# The lists are hand-written, so entries carry natural punctuation ("Genes &
# Development", "Cell Host & Microbe", "Nature Structural & Molecular
# Biology", "The ISME Journal"). Lookups normalise; the lists did not — so
# those six entries could never match anything, and four well-known tier 1
# journals were being silently excluded from the corpus despite being listed.
# Normalising at import makes the whole class of mistake impossible: an entry
# can now be written in whatever form reads naturally.
_TIER1_JOURNALS = {_normalise(j) for j in _TIER1_JOURNALS}
_TIER2_JOURNALS = {_normalise(j) for j in _TIER2_JOURNALS}


def _journal_tier(journal: str | None, tier1_extra: set[str], tier2_extra: set[str]) -> float:
    if not journal:
        return 0.4  # unknown journal — neutral
    j = _normalise(journal)
    tier1 = _TIER1_JOURNALS | {_normalise(x) for x in tier1_extra}
    tier2 = _TIER2_JOURNALS | {_normalise(x) for x in tier2_extra}
    if j in tier1:
        return 1.0
    if j in tier2:
        return 0.7
    # Partial match for Nature/Science/Cell family
    if any(t in j for t in ("nature", "cell press", "science adv")):
        return 0.85
    # Downweight high-volume open-access publishers with variable peer review
    if "frontiers" in j or j.startswith("front "):
        return 0.25
    if any(t in j for t in ("mdpi", "molecules", "ijms", "int j mol sci",
                             "biomolecules", "pharmaceutics", "cancers",
                             "cells ", "antioxidants", "nutrients")):
        return 0.25
    return 0.4   # everything else: decent but unranked


def _pub_type_weight(pub_types: list[str]) -> float:
    """Return the highest weight among the article's listed pub types."""
    if not pub_types:
        return 0.8   # no type info — assume journal article
    types_lower = {t.lower() for t in pub_types}
    best = 0.5
    for pt, w in _PUB_TYPE_WEIGHTS.items():
        if any(pt in t for t in types_lower):
            best = max(best, w)
    return best


def _recency(year: int | None) -> float:
    """Linear decay: 1.0 for 2025+, 0.7 for 2015, floor at 0.5."""
    if not year:
        return 0.75
    return max(0.5, min(1.0, 0.7 + (year - 2015) * 0.03))


def _citation_boost(citation_count: int | None) -> float:
    """
    Log-normalised citation signal: 0.0 (no cites) → 1.0 (≥10 000 cites).
    log10(10 000) / 4 = 1.0, so papers with >10k citations are capped at 1.0.
    Returned as a small additive boost (≤0.08) so it never dominates journal tier.
    """
    if not citation_count:
        return 0.0
    return min(1.0, math.log10(max(1, citation_count)) / 4.0)


def is_conference_abstract(pub_types: list[str]) -> bool:
    types_lower = {t.lower() for t in pub_types}
    return bool(types_lower & _CONFERENCE_PUB_TYPES)


def score_paper(
    paper: Paper,
    tier1_extra: list[str] | None = None,
    tier2_extra: list[str] | None = None,
) -> float:
    """
    Compute priority score in [0, 1].
    Returns 0.0 for conference abstracts (caller should exclude from downloads).
    """
    if is_conference_abstract(paper.pub_types):
        return 0.0

    t1 = set(tier1_extra or [])
    t2 = set(tier2_extra or [])

    if paper.source in (Source.biorxiv, Source.medrxiv):
        # Preprints: capped at 0.55 regardless of other factors
        type_w = _pub_type_weight(paper.pub_types)
        rec    = _recency(paper.year)
        base   = min(0.55, 0.55 * type_w * rec)
        boost  = 0.08 * _citation_boost(paper.citation_count)
        return round(min(0.55, base + boost), 3)

    j_tier  = _journal_tier(paper.journal, t1, t2)
    type_w  = _pub_type_weight(paper.pub_types)
    rec     = _recency(paper.year)

    # Weighted combination: journal tier dominates; citation count adds a small boost
    score = 0.55 * j_tier + 0.30 * type_w + 0.15 * rec
    score = min(1.0, score + 0.08 * _citation_boost(paper.citation_count))
    return round(score, 3)


def is_tiered_journal(
    journal: str | None,
    tier1_extra: list[str] | None = None,
    tier2_extra: list[str] | None = None,
) -> bool:
    """Return True if the journal is in tier 1 or tier 2 (i.e. known high-quality venue)."""
    t1 = set(tier1_extra or [])
    t2 = set(tier2_extra or [])
    return _journal_tier(journal, t1, t2) >= 0.7
