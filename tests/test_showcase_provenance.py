"""The showcase pages must not drift from the runs they claim to report.

Every showcase page ends with a line saying its figures were extracted from a
run directory or from the corpus. That line used to be false: each builder
carried its numbers as literals in its own source, nothing compared them to
anything, and they rotted. What shipped, on pages about to be the project's
public front door:

  - `ppi_discovery.html` named four of twelve hotspot residues wrong (Lys350,
    Glu353, Leu366, Ile404 for PHE350, LYS353, VAL366, HIS404) in a paragraph
    about grounding hotspot names in the real structure.
  - `campaign_pdl1.html` argued that designs "died on geometry, not on
    confidence" — its own scoring CSV says the opposite.
  - Every corpus statistic on `corpus_explorer.html` was stale by ~25%.

The builders now extract, and commit what they extracted to
`docs/showcase/facts/<page>.json` (see `docs/showcase/_facts.py`) — the source
runs are gitignored, so the snapshot is what makes the pages buildable and
auditable off the maintainer's machine.

These tests pin the two halves of that arrangement: a snapshot exists and is
readable, the committed page actually agrees with it, and no builder has
quietly reverted to typing numbers in by hand. They are deliberately read-only
and need no run data, so they hold in CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SHOWCASE = _ROOT / "docs" / "showcase"
_FACTS = _SHOWCASE / "facts"

# (builder, page, facts name, the fields the page MUST render — dotted paths
# allowed, since the builders nest differently).
#
# A declared list rather than "every number in the snapshot": a facts blob
# legitimately carries values the page uses without printing — gate counts that
# render as percentages, a token total behind a phrase, a comparison run's size.
# Listing them keeps the check meaningful instead of forcing every derivation
# input onto the page. These are the figures a reader would look up, and they
# include the ones that were found wrong.
PAGES = [
    ("build_campaign.py", "campaign_pdl1.html", "campaign_pdl1",
     ["production.n_rfd3", "production.n_mpnn", "n_scored", "bsa_A2",
      "gate_historical.survivors", "gate_current.survivors"]),
    ("build_corpus.py", "corpus_explorer.html", "corpus_explorer",
     ["indexed", "curated", "fingerprints", "clusters", "lit_nodes", "lit_edges",
      "depmap_genes", "depmap_edges"]),
    ("build_ppi.py", "ppi_discovery.html", "ppi_discovery",
     ["n_rfd3", "n_filtered", "n_mpnn", "n_scored", "n_survivors",
      "n_backbones_surviving", "bsa_A2"]),
    ("build_pain.py", "pain_receptors.html", "pain_receptors",
     ["n_rfd3", "n_filtered", "n_mpnn", "n_scored", "n_survivors",
      "n_backbones_surviving", "bsa_A2"]),
]


def _facts(name: str) -> dict:
    path = _FACTS / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"{path.relative_to(_ROOT)} not present")
    return json.loads(path.read_text(encoding="utf-8"))


def _page(name: str) -> str:
    path = _SHOWCASE / name
    if not path.is_file():
        pytest.skip(f"{name} not built in this checkout")
    return path.read_text(encoding="utf-8")


def _numbers(value, out: list[float]) -> list[float]:
    """Every scalar number anywhere in a facts blob."""
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _numbers(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _numbers(v, out)
    return out


@pytest.mark.parametrize("builder,page,facts,keys", PAGES)
def test_each_showcase_page_ships_the_facts_it_was_built_from(builder, page, facts, keys):
    """A page with no snapshot cannot be rebuilt or audited off this machine."""
    assert (_SHOWCASE / builder).is_file(), f"{builder} missing"
    data = _facts(facts)
    assert data, f"{facts}.json is empty"
    assert _numbers(data, []), f"{facts}.json carries no numbers — nothing was extracted"


@pytest.mark.parametrize("builder,page,facts,keys", PAGES)
def test_the_page_agrees_with_its_own_snapshot(builder, page, facts, keys):
    """The counts a reader would look up must match what the builder extracted.

    This is the check that was missing. A number that moved in a re-run, or a
    page left un-rebuilt after its facts changed, surfaces here instead of in
    front of someone who checks it against the paper.
    """
    data, html = _facts(facts), _page(page)
    for key in keys:
        n = data
        for part in key.split("."):
            assert isinstance(n, dict) and part in n, \
                f"facts/{facts}.json has no {key!r}"
            n = n[part]
        assert isinstance(n, int) and not isinstance(n, bool), \
            f"{facts}.{key} is {type(n).__name__}, expected an int count"
        if f"{n:,}" not in html and str(n) not in html:
            pytest.fail(
                f"{page} does not contain {n:,} ({key}) from facts/{facts}.json — "
                f"the page and its extracted facts disagree. Rebuild with "
                f"`python docs/showcase/{builder}`.")


@pytest.mark.parametrize("builder,page,facts,keys", PAGES)
def test_the_page_claims_provenance_it_can_support(builder, page, facts, keys):
    """A page asserting its figures were extracted must have a builder that does.

    The original failure was exactly this mismatch: prose promising the numbers
    came from the run, over a builder that opened nothing.
    """
    src = (_SHOWCASE / builder).read_text(encoding="utf-8")
    assert "_facts.load(" in src, (
        f"{builder} does not go through _facts.load — either it hardcodes its "
        f"figures, or the snapshot it ships is not what built the page")
    assert re.search(r"def extract\(", src), f"{builder} has no extract()"


@pytest.mark.parametrize("builder,page,facts,keys", PAGES)
def test_each_page_unfurls_with_its_own_card(builder, page, facts, keys):
    """`og:image` must be this page's card, and that file must exist.

    The card id is a bare string argument to `_common.head`, so copying a
    builder's header call carries the wrong one silently — `build_pain.py`
    passed "ppi" and the CGRP page unfurled, live, with the mesothelioma
    campaign's card while its own `og_pain.png` was referenced by nothing.
    Nothing in the page or the build output shows it; you only see it in a
    Slack preview or by reading the served HTML.
    """
    html = _page(page)
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    assert m, f"{page} has no og:image — a pasted link renders no card"
    asset = m.group(1).rsplit("/", 1)[-1]
    assert (_SHOWCASE / "assets" / asset).is_file(), (
        f"{page} points og:image at {asset}, which does not exist in assets/")

    # Each showcase page must carry its own card, not a sibling's. index.html
    # advertises the whole set and legitimately reuses one.
    if page != "index.html":
        assert asset != "og_ppi.png" or page == "ppi_discovery.html", (
            f"{page} unfurls with {asset}, another page's card")


def test_every_generated_card_is_referenced_by_some_page():
    """A card nothing points at is a card that was silently replaced."""
    cards = {p.name for p in (_SHOWCASE / "assets").glob("og_*.png")}
    if not cards:
        pytest.skip("no og cards in this checkout")
    referenced = set()
    for _b, page, _f, _k in PAGES:
        path = _SHOWCASE / page
        if path.is_file():
            referenced |= {m.rsplit("/", 1)[-1] for m in re.findall(
                r'content="(https://\S*?/assets/og_\w+\.png)"',
                path.read_text(encoding="utf-8"))}
    orphans = sorted(cards - referenced)
    assert not orphans, (
        f"generated but referenced by no page: {orphans} — either a page points "
        f"at the wrong card, or these are stale")


def test_handoff_blocks_are_read_with_the_pipelines_own_parser():
    """Builders that read stage reports must not re-implement `parse_handoff`.

    `src/handoff.py` is the single reader for `### PIPELINE HANDOFF` blocks
    (CLAUDE.md pins this). A second regex in a builder drifts silently the next
    time a skill prompt changes its output.
    """
    for builder, _, _, _ in PAGES:
        src = (_SHOWCASE / builder).read_text(encoding="utf-8")
        if "PIPELINE HANDOFF" in src and "parse_handoff" not in src:
            pytest.fail(f"{builder} parses handoff blocks without src.handoff")
