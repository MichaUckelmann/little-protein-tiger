# Licensing

Three different things in this project carry three different sets of terms, and
conflating them is the usual way people get this wrong:

| | What it is | Terms |
|---|---|---|
| **The code** | this repository | PolyForm Noncommercial 1.0.0 — see [`LICENSE`](../LICENSE) |
| **The corpus** | the `lpt-corpus-*` release asset | research use; derived from third-party papers — see [The corpus](#the-corpus) |
| **The tools LPT drives** | foundry, BoltzGen, PyRosetta, … | their own licences, several restricted — see [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md) |

The short version: **LPT is free for academic, nonprofit and personal research,
and needs a commercial licence for commercial use.** If you are at a university,
institute, hospital, or government lab, you are covered — including when your
funding comes from industry.

## The code

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/),
a short, plain-English source-available licence. You may use, modify, and
redistribute LPT for any noncommercial purpose. The licence spells out two
categories that cover most readers of this file:

- **Noncommercial organizations** — "any charitable organization, educational
  institution, public research organization, public safety or health
  organization, environmental protection organization, or government
  institution", explicitly **"regardless of the source of funding or
  obligations resulting from the funding."** An academic lab on an
  industry-funded grant is covered.
- **Personal uses** — research, experiment, study, hobby projects, "without any
  anticipated commercial application".

It grants a patent licence alongside the copyright licence, allows modification
and redistribution, and gives you 32 days to cure a first violation rather than
terminating immediately.

**This is not an OSI-approved open-source licence,** and the project does not
claim to be open source. It is source-available. Practical consequences worth
knowing before you depend on it:

- Some companies' policies forbid any non-OSI dependency outright.
- It is not eligible for the `License :: OSI Approved` PyPI classifier
  (`pyproject.toml` declares `License :: Other/Proprietary License`).
- Some journals and funders require an OSI licence for released software.

### Commercial use

Commercial licences are available — contact the copyright holder. "Commercial"
means what PolyForm makes it mean: use by a for-profit entity, or personal use
with an anticipated commercial application.

If you are unsure which side of the line you are on, ask rather than guess. The
grey cases in this field are real: core facilities charging cost recovery,
academic spinouts pre-revenue, CROs running the pipeline as a service. Getting
a one-line answer in writing costs an email.

This project uses the same model as **PyRosetta**, one of its own optional
dependencies: free for academic use, commercial licence on request. If you
already navigate that, nothing here is new.

### Could this go back to fully open source?

**Yes, and that direction is always available.** Relicensing is asymmetric:

- **Restrictive → permissive** works. Everyone holding a copy gains rights,
  nobody's expectations break, and no permission is needed but the copyright
  holder's.
- **Permissive → restrictive** does not. An MIT release is irrevocable for the
  copies already distributed; anyone can fork the last permissive commit and
  use it commercially forever. That is why this was decided *before* the repo
  went public rather than after.

So PolyForm now preserves the choice, and MIT would have spent it. Switching to
MIT or Apache-2.0 later is a one-commit change.

**One condition keeps it that way: the copyright has to stay undivided.** Today
every commit is by one author, so relicensing — in either direction — and
issuing commercial licences are both unilateral decisions. The moment an
outside contribution is merged without a contributor licence agreement or
copyright assignment, that contributor holds copyright in part of LPT, and
neither is possible any more without tracking them down. If you intend to
contribute, expect to be asked to sign a CLA; this is why.

## The corpus

The `lpt-corpus-*` release asset is **not** covered by the code licence, and
LPT's copyright holder does not own most of what it is derived from. It is
offered for research use. If you plan to redistribute it or build a product on
it, read this section rather than assuming.

### What it actually contains

Audited against the shipped database, not assumed:

| | Count | What it is |
|---|---|---|
| Papers indexed | 57,907 | bibliographic metadata: title, authors, journal, year, DOI/PMCID/PMID |
| Papers curated | 14,517 | fingerprint JSONs — the corpus proper |
| Source documents (PDF/XML) | **0** | deliberately excluded; ~95% of the corpus on disk, and nothing downstream reads them |

**Fingerprints are derived work, not excerpts.** Each is structured, model-written
output: paraphrased claims, method lists, normalised numeric values (Kd in
Molar, NCBI taxon ids). The `source_span` field is a *pointer* — `"Section:
Clinical course, Para 1"` — not a quotation. No verbatim passage of any paper is
reproduced.

**Bibliographic metadata is fact, not expression**, and is not what copyright
protects.

**Abstracts are the one exception, and they are stripped from the release.**
`papers.abstract` holds publisher-supplied text — 11,818 rows in the working
database, averaging ~1,750 characters. Those *are* copyrightable, and a PMCID
does not by itself grant redistribution rights (see below). Nothing in LPT ever
reads the column: the curator parses the downloaded PDF/XML, and `search_corpus`
embeds fingerprints. So `scripts/package_corpus.py` blanks it in the packaged
copy of the database — no functional cost, and it removes the only part of the
archive that would have been redistribution of third-party text.

### "It's all PMC open access" — not quite

That assumption is a common one and it is wrong in two ways worth stating,
because the corrections point in the same direction:

1. **Not everything is from PMC.** By source: 49,406 PMC, 7,417 Semantic
   Scholar, 1,006 bioRxiv, 77 medRxiv. About 15% never went through PMC.
2. **PMC is not the same as the PMC Open Access Subset.** A PMCID means an
   article is *free to read* in PMC. It does not mean it carries a CC licence
   or may be redistributed. The OA Subset is a specific, separately identified
   collection; the rest of PMC — publisher deposits, the author-manuscript
   collection — is readable but not redistributable. LPT records no per-paper
   licence field, so the shipped database **cannot** tell you which papers are
   in the OA Subset.

Neither point affects what actually ships, because what ships is derived
fingerprints and bibliographic facts, with abstracts removed. It does mean the
archive should not be described as "open access papers" — it is a derived index
over a reading list, and the papers themselves are not in it.

If you need per-paper licence provenance — for a redistributable dataset, or a
publication that asserts one — it would have to be added: PMC's OA web service
returns the licence for a given PMCID, and that call is not currently made.

## Third-party tools

LPT orchestrates models and tools it does not ship and grants no rights to.
Several are free for academic use and **restricted commercially** — PyRosetta
most notably. Holding a commercial licence for LPT grants you nothing for any
of them. See [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md) and the
licence table in [`README.md`](../README.md#licence-and-third-party-tools).

## Not legal advice

This file describes the project's intent and what was verified about its
contents. It is not legal advice, and it is not a substitute for your own
institution's review — particularly for the corpus, where the underlying
rights belong to publishers and authors rather than to this project.
