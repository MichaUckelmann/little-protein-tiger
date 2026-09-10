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
| **Fingerprints shipped** | **7,072** | the corpus proper — only papers whose licence permits redistributing a derivative |
| Fingerprints withheld | 7,445 | curated in the maintainer's working corpus, but the paper's licence forbids redistributing a derivative or records none. The row is kept and reads `curation_status='licence_withheld'`, so you can see which papers they are and why |
| Source documents (PDF/XML) | **0** | deliberately excluded; ~95% of the corpus on disk, and nothing downstream reads them |

**A fingerprint is treated as a derivative work of its paper, and is published
only when that paper's licence permits redistributing derivatives.** Each one
is structured, model-written output — paraphrased claims, method lists,
normalised numeric values (Kd in Molar, NCBI taxon ids) — and the `source_span`
field is a *pointer* (`"Section: Clinical course, Para 1"`) rather than a
quotation, so no verbatim passage of any paper is reproduced. None of that is
offered as a reason the licence does not apply: the licence is applied, and the
release is filtered accordingly. See
[Per-paper licences](#per-paper-licences-measured) for the counts.

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
   collection — is readable but not redistributable.

The archive should therefore not be described as "open access papers" — it is a
derived index over a reading list, and the papers themselves are not in it.

### Per-paper licences, measured

`scripts/audit_paper_licences.py` resolves a licence for every curated paper
from Europe PMC (by PMCID, falling back to DOI) and caches it in
`data/paper_licences.json`. Measured 2026-09-10 over all 14,517:

| Licence | Papers | Share |
|---|---|---|
| `cc by` | 5,708 | 39.3% |
| *none recorded* | 5,684 | 39.2% |
| `cc by-nc-nd` | 1,501 | 10.3% |
| `cc by-nc` | 1,028 | 7.1% |
| `cc by-nc-sa` | 546 | 3.8% |
| `cc0` | 45 | 0.3% |
| `cc by-nd` | 5 | <0.1% |

Grouped by what they permit: **7,327 (50.5%) allow derivative works, 1,506
(10.4%) forbid them** (`-nd`), and **5,684 (39.2%) record no licence at all** —
which is not permission. A paper with no CC licence in Europe PMC is normally a
publisher deposit that is free to read and not licensed for reuse. Spot-checked
against live records; the classifications reproduce.

The ND set is concentrated in the journals a chromatin/structural corpus would
be expected to draw on — Nat Commun (215), Cell Rep (199), Stem Cell Reports
(123), iScience (105), Nature (94).

### What ships, and what does not

**Only fingerprints of papers whose licence permits redistributing a derivative
work are published.** As of 2026-09-10 that is **7,072 of the 14,517 curated
papers**, and the asset went from 105 MB to 50 MB. A No-Derivatives term
(`cc by-nc-nd`, `cc by-nd`) excludes a paper's fingerprint, and so does no
recorded licence — that is the absence of permission, not permission.

Every count here is reproducible from the archive you downloaded:

```sql
SELECT curation_status, COUNT(*) FROM papers
 WHERE curation_status IN ('completed','licence_withheld') GROUP BY 1;
-- completed         7072     fingerprint shipped
-- licence_withheld  7445     withheld, and papers.licence says why
```

Filtering the JSONs alone would not have been enough, because three other
shipped artifacts are derived from fingerprint text:

| Artifact | What was done |
|---|---|
| `data/fingerprints/` | only licence-permitted papers copied |
| `data/vectors/` | the LanceDB table stores `embed_text` **and** `fingerprint_json` inline, so it is filtered by `paper_key` — an unfiltered index would ship the very text the JSON was withheld to avoid shipping |
| `depmap_edges.parquet`, `clusters.json` | rebuilt from the filtered set, not copied |
| `data/literature.db` | every row kept (bibliographic metadata is fact), but `fingerprint_path` cleared and `curation_status` set to `licence_withheld` for a paper whose fingerprint is not in the archive, so nothing points at a file that is not there |

`package_corpus.py` then extracts its own output and *uses* it — opens the
vector table, opens the database, checks no row dangles — because a local check
cannot see a packaging bug. That check exists because one shipped: a tar filter
meant to drop two scratch directories also stripped LanceDB's `_versions/`, and
the 2026-09-10 asset listed the table in `table_names()` and then failed to
open it, so `search_corpus` was dead for anyone who downloaded it.

### The expansion tools default to the same rule

`fetch_papers.py` resolves each pending paper's licence from Europe PMC and
**does not download** ND or unlicensed papers; they stay indexed, exactly as
the journal-tier gate leaves untiered papers indexed. `curate_papers.py`
re-checks before extracting, since curation is what creates the derivative and
papers fetched before the gate existed are already on disk. Both are governed
by `quality.require_derivative_licence` (default **true**) and both take
`--allow-restricted-licence` to override — for a corpus you keep to yourself.
`package_corpus.py --no-licence-filter` is the separate, deliberate second
switch needed to put such fingerprints in an archive.

Expect the gate to roughly halve a fresh corpus build. That is the cost of
being able to publish the result.

**This is not legal advice** — see the note at the end of this file.

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
