"""No prompt may pre-answer the question its stage exists to answer.

The pipeline's LLM stages pick a PPI target from a free-text disease query.
An audit found the answer written into the selectors' own few-shot examples:
`skills/pathway-expert/SKILL.md` carried a `choices_json` exemplar whose
top-tier ("VALIDATED") candidate was `YAP1 / TEAD4` with the evidence_basis
"Mesothelioma xenograft regression confirmed upon YAP-TEAD inhibition", and
`skills/wildcard-expert/SKILL.md` asserted as an INSTRUCTION that the
canonical driver of mesothelioma is YAP1. Mesothelioma queries duly returned
YAP1/TEAD1 almost every time. That may well be correct biology -- the point is
that a pre-named answer makes the choice unfalsifiable.

The rule these tests encode:

* Format slots get obviously-synthetic placeholders (`GENE_A`), because a
  leaked placeholder fails LOUDLY -- `identifier_normalizer` will not resolve
  it, `find_pdb_structures` returns nothing -- whereas a leaked real gene is
  indistinguishable from a decision.
* Mechanics examples (synonym adjudication, paralog distinction, residue
  numbering) may keep real gene names, because a concrete example teaches
  those far better than an abstraction. They must simply not name a plausible
  ANSWER.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The target this audit was about. Kept as a regression blocklist rather than
#: a general rule: a future leak of a DIFFERENT target is caught by
#: `test_the_selectors_use_placeholder_genes`, which checks the convention.
_AUDITED = re.compile(
    r"\b(yap1?|tead[1-4]?|hippo|mesothelioma|3kys|verteporfin|wwtr1|vgll4"
    r"|calcrl|ramp1|erenumab|cgrp)\b",
    re.IGNORECASE)

#: Plausible design TARGETS, which a selector prompt must never name. Wider
#: than `_AUDITED` and applied only to the two stages that CHOOSE — every
#: target any run of this pipeline has ever picked was named somewhere in the
#: skills, which is why this list exists and why it is checked at the point of
#: choice rather than everywhere.
#:
#: Mechanics examples elsewhere may still name a real gene, but they now use
#: housekeeping proteins (GAPDH, ACTB/ACTG1) that nobody designs a binder
#: against. An earlier pass used TP53/MDM2 for that role, which MOVED the bias
#: instead of removing it: p53-MDM2 is the textbook PPI drug-discovery
#: example.
_DRUGGABLE = re.compile(
    r"\b(tp53|mdm2|akt[12]|jak[123]|ctnnb1|kras|raf1|braf|egfr|scap|srebp"
    r"|enpp[12]|dpp4|lpar1|cgas|sting|cd79b|sos1|mybpc3|pd-?l1|pd-?1"
    r"|il7ra?|calcrl|ramp1|yap1?|tead[1-4]?)\b",
    re.IGNORECASE)

#: The stages that turn a query into a target. Only these two.
_SELECTORS = ("pathway-expert", "wildcard-expert")

#: Every text that reaches a model: skill system prompts (`skill_runner`
#: `_load_system_prompt` reads the WHOLE SKILL.md, frontmatter included), the
#: reference files a SKILL.md tells the model to read, the curation contract,
#: and the tool descriptions of both transports.
_PROMPT_SURFACES = [
    *sorted((_ROOT / "skills").rglob("*.md")),
    _ROOT / "curation_prompt.md",
]

def _walk_strings(obj):
    """Every string reachable in a tool-definition structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(k)
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _walk_strings(v)


def test_no_tool_description_names_the_audited_target():
    """`find_pdb_structures` and `search_rcsb_pdb` are the tools that PRODUCE
    the PDB id the pipeline designs against, and their parameter examples used
    to be `['YAP1', 'TEAD4', 'NF2']`.

    Introspects the real objects rather than grepping the file: a tool
    description IS what the model receives, while the same module's internal
    function docstrings and comments are not model-facing and legitimately
    still discuss real genes (the substring-matching lesson, for one).
    """
    from src.skill_runner import _TOOL_DEFS
    from src.vector_store import VectorStore

    offenders = []
    for label, defs in (("_TOOL_DEFS", _TOOL_DEFS),
                        ("VectorStore.SEARCH_TOOL_DEFINITION",
                         VectorStore.SEARCH_TOOL_DEFINITION)):
        for text in _walk_strings(defs):
            m = _AUDITED.search(text)
            if m:
                offenders.append(f"{label}: ...{text[max(0, m.start() - 40):m.end() + 40]}...")
    assert not offenders, (
        "tool descriptions are part of the model's context:\n"
        + "\n".join(offenders))


def test_the_mcp_transport_is_checked_too():
    """FastMCP uses a tool's docstring as its description, so the MCP server's
    docstrings are a second, independent prompt surface — and the two
    transports are deliberately NOT kept identical (see CLAUDE.md's
    "Two transports, opposite trigger rules"), so neither covers the other."""
    import inspect

    from src import mcp_server

    offenders = []
    for name, fn in vars(mcp_server).items():
        # Only the tool WRAPPERS, which are defined in this module. Imported
        # implementation functions (`export_subgraph as _export_subgraph`) are
        # the same object as `_corpus_graph`'s and their docstrings are not
        # what FastMCP publishes — the wrapper's own docstring is.
        if not callable(fn) or getattr(fn, "__module__", "") != mcp_server.__name__:
            continue
        doc = inspect.getdoc(fn)
        if doc and _AUDITED.search(doc):
            m = _AUDITED.search(doc)
            offenders.append(f"mcp_server.{name}: ...{doc[max(0, m.start() - 40):m.end() + 40]}...")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("skill", ["pathway-expert", "wildcard-expert"])
def test_the_selectors_use_placeholder_genes_in_their_choices_exemplar(skill):
    """The general rule, not just this target's blocklist.

    `choices_json` is the field the pipeline PARSES to get its candidate list,
    so its exemplar is the most load-bearing text in either prompt. It must
    demonstrate the SHAPE with names that cannot be mistaken for candidates.
    """
    text = (_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    example = [l for l in text.splitlines() if "- choices_json: [" in l]
    assert example, f"{skill}: no choices_json exemplar found"
    for line in example:
        assert "GENE_A" in line, (
            f"{skill}: the choices_json exemplar must use placeholder gene "
            f"names, so that a copied example fails loudly rather than "
            f"silently naming a target:\n  {line.strip()[:160]}")


def test_the_placeholder_convention_is_explained_where_it_is_used():
    """A placeholder with no explanation invites the model to fill it in with
    something plausible, or to echo it verbatim. The exemplar has to say which
    it is."""
    text = (_ROOT / "skills" / "pathway-expert" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "DELIBERATE PLACEHOLDERS" in text
    assert "an example was copied instead of answered" in text


@pytest.mark.parametrize("skill", _SELECTORS)
def test_a_selector_names_no_plausible_target_at_all(skill):
    """The empirical reason this is stricter than `_AUDITED`: every target any
    run of this pipeline has picked — YAP1/TEAD1, PD-L1, CALCRL/RAMP1,
    KRAS/RAF1 — was named somewhere in the skills. Whether that is cause or
    coincidence is measurable (see scripts/ablate_corpus.py and the
    target-diversity sweep), but a stage that CHOOSES should not be carrying a
    menu while we find out.

    Mechanics examples in the evaluator skills are exempt by design: they use
    housekeeping proteins (GAPDH, ACTB/ACTG1) that are not candidate answers.
    """
    text = (_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    hits = [f"{skill}:{i}: {line.strip()[:90]}"
            for i, line in enumerate(text.splitlines(), 1)
            if _DRUGGABLE.search(line)]
    assert not hits, (
        "a target-selecting prompt must name no plausible target:\n"
        + "\n".join(hits))
