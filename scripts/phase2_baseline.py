"""The 'plain LLM, no tools' baseline for the pathway stage.

Same model, same task, same report template and PIPELINE HANDOFF block as the
pipeline's `pathway-expert` skill; what changes is that the model has NO tools
(no corpus, no graph, no DepMap, no RCSB) and is told so. Built from the live
SKILL.md by explicit edits, each asserted, so a change to the skill that moves an
anchor makes this fail loudly instead of quietly producing a different baseline.

Edits, in order:
  1. Phases 2 to 3.6 (every tool-using search phase) are replaced by one short
     phase saying no tools exist and to work from knowledge.
  2. The CORPUS COVERAGE and PATHWAY SOURCES report sections are removed: they
     report on retrieval that cannot happen here.
  3. An OVERRIDE block is placed first. It says how to read the remaining text
     that mentions the corpus or retrieved fingerprints, and keeps the pipeline's
     citation policy unchanged ("a fact with no citation is acceptable; a fact
     with a fabricated citation is not") so the two arms are told the same thing
     about citing.

The generated prompt is written to outputs/phase2/notools_system_prompt.md so the
exact text a baseline run saw is on disk next to its outputs.
"""
from __future__ import annotations

import re

OVERRIDE = """\
## OPERATING CONDITIONS FOR THIS RUN (these override anything below)

You have NO tools in this run: no literature database, no structure database, no
gene-dependency data, no web search. Work only from your own knowledge.

- Wherever the instructions below refer to "the corpus", "retrieved fingerprints",
  "search hits" or "corpus data", read that as "the published literature you
  actually know". Do not describe retrieval, searches or databases you did not use.
- Citation policy is unchanged: a DOI with a locator may be given only if you are
  certain of it. A fact with no citation is acceptable. A fact with a fabricated
  citation is not. Never cite author names, journals or years in place of a DOI.
- PDB identifiers: give a PDB ID only if you are certain it is the right entry for
  the complex; otherwise write NOT_FOUND. Do not guess accession codes.
- Everything else about the task and the report format is unchanged.

---

"""

NO_TOOLS_PHASE = """\
## Phase 2: Evidence (no tools available)

There is no literature database or other tool in this run. Using the context
extracted in Phase 1, recall what you know about the disease mechanism, the
dysregulated nodes, their dependency evidence and prior therapeutic targeting, and
what is known structurally. Where you are unsure, say so in the report instead of
filling the gap.

---

"""


# Sentences that would send the model to a tool or tell it to read a field it cannot
# see. Each is replaced by its no-tools equivalent; an anchor that no longer matches
# fails the build.
REWRITES = [
    ("`pdb_id` must come from `paper_metadata.pdb_accessions`, `pathway_context.target_nodes[].suggested_pdb_structures` in retrieved fingerprints, **or the `find_pdb_structures` tool result from Phase 3.6**. Write `NOT_FOUND` if nothing found — never guess.",
     "`pdb_id` must be a PDB accession you are certain is the right entry for the complex. Write `NOT_FOUND` if you are not certain — never guess."),
    ("`structure_organism` must be read off the chosen entry's `entities[].organism_name`, not recalled — it is the field",
     "`structure_organism` is the organism of the chosen entry as you know it, stated plainly (write `unknown` if unsure) — it is the field"),
    ("the real complex always wins; check with `search_rcsb_pdb` first and say in\n   the report that you looked.",
     "the real complex always wins; say in the report what you know about\n   experimental co-complexes."),
]


def build(skill_text: str) -> str:
    a = skill_text.index("## Phase 2: Multi-Query Corpus Search")
    b = skill_text.index("## Phase 4: Synthesise and Output Report")
    assert a < b, "skill phases moved: cannot locate the search phases"
    out = skill_text[:a] + NO_TOOLS_PHASE + skill_text[b:]
    for sec, nxt in (("### CORPUS COVERAGE", "### PATHWAY SOURCES"), ("### PATHWAY SOURCES", "### PIPELINE HANDOFF")):
        i, j = out.index(sec), out.index(nxt)
        assert i < j, f"report sections moved: {sec}"
        out = out[:i] + out[j:]
    for old, new in REWRITES:
        assert old in out, f"skill text changed, cannot apply rewrite: {old[:60]!r}"
        out = out.replace(old, new)
    # the override goes after the YAML frontmatter, before the first heading
    m = re.search(r"\n---\n\n(# )", out)
    assert m, "frontmatter end not found"
    return out[:m.end() - len(m.group(1))] + OVERRIDE + out[m.end() - len(m.group(1)):]
