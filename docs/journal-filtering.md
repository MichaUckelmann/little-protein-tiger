# Journal filtering

**LPT does not download every paper its searches find.** By default it
downloads only papers from journals on a curated tier list, and that decision
shapes everything downstream — the corpus, the fingerprints, the vector search,
the interaction graph, and every target a discovery workflow proposes.

This page is the canonical description of that gate: where it lives, what it
currently excludes, and how to change or switch it off.

## Where it lives

| Thing | Location |
|---|---|
| The switch | `config.yaml` → `quality.require_tiered_journal` (**default `true`**) |
| The gate | `scripts/fetch_papers.py`, in the pending-download filter |
| The tier lists | `src/ranking.py` → `_TIER1_JOURNALS`, `_TIER2_JOURNALS` |
| The matcher | `src/ranking.py` → `is_tiered_journal()` / `_journal_tier()` |
| Per-checkout additions | `config.yaml` → `quality.tier1_extra`, `quality.tier2_extra` |

The gate applies at **download** time only. Search still indexes every hit into
`data/literature.db`, so the record of what was found is complete and you can
widen the filter later and re-run `fetch_papers.py` without re-searching.

## What it does today

Measured against the shipped corpus (55,644 indexed papers):

| | Papers | Share |
|---|---|---|
| Indexed by search | 55,644 | 100% |
| **Pass the tier gate** | **15,472** | **28%** |
| Blocked | 40,172 | 72% |

The gate is deliberately strict. The largest excluded venues are *PLoS One*,
*bioRxiv*, *Scientific Reports*, and *International Journal of Molecular
Sciences* — high-volume outlets whose peer review is variable enough that the
extraction schema's quantitative claims (Kd, Ki, mutational effects) can't be
trusted at the same rate.

## The scoring ladder

`_journal_tier()` returns a 0–1 score; `is_tiered_journal()` is `score >= 0.7`.
The same score also feeds `priority_score`, which orders the download queue.

| Score | Meaning |
|---|---|
| **1.0** | In `_TIER1_JOURNALS` — *Nature*, *Science*, *Cell*, *PNAS*, the Nature and Cell Press families, *NEJM*, *JACS*, *Angewandte*, … |
| **0.85** | Partial match on the Nature / Cell Press / *Science Advances* families — catches new sibling journals not yet listed by name |
| **0.7** | In `_TIER2_JOURNALS` — strong specialist venues: *JBC*, *Biochemical Journal*, *NAR*, *Acta Pharmaceutica Sinica B*, … |
| **0.4** | Unlisted, or journal unknown — decent but unranked. **Blocked by the gate.** |
| **0.25** | Deliberately downweighted: Frontiers titles, and MDPI titles (*IJMS*, *Molecules*, *Cells*, *Cancers*, …) |

Tier 1 holds 77 entries and tier 2 holds 49, but both include abbreviations
alongside full names, so the real count is roughly half that many journals.

## Matching is exact, and that matters

A journal qualifies only if its **normalised** name is in a list. Normalisation
lowercases, blanks punctuation, collapses whitespace, strips a leading `"The"`,
and strips PubMed's trailing country/edition qualifiers.

Those last two rules exist because their absence was silently excluding papers
from journals that *are* listed — 1,721 of them across this corpus:

- `Proceedings of the National Academy of Sciences of the United States of America` (827 papers) — PubMed's full name for PNAS
- `The EMBO Journal`, `The Journal of Biological Chemistry`, `The Biochemical Journal`
- `Angew Chem Int Ed Engl`

**If you add a journal, add every spelling your sources use** — the full name
and the PubMed abbreviation at minimum. A missing variant is not a warning; it
is a silent 100% exclusion of that journal. When adding to `config.yaml`'s
`tier1_extra` / `tier2_extra`, check both:

```bash
python -c "from src.ranking import is_tiered_journal as t; \
           print(t('Journal of Cell Biology'), t('J Cell Biol'))"
```

## Known gaps

Some well-regarded journals are simply not on the list yet — among the more
frequent in this corpus: *Journal of Cell Biology* (462 papers across both
spellings), *Cell Stem Cell*, *Stem Cell Reports*, *Molecular and Cellular
Biology*. These are editorial choices, not bugs; add them via `tier1_extra` /
`tier2_extra` if they matter to your field.

The list also reflects a **molecular / structural / chemical biology** focus.
If your work sits elsewhere — immunology, neuroscience, plant biology — expect
to extend it substantially, or to turn the gate off and rely on
`min_score_to_download` instead.

## Changing it

**Widen for one run**, keeping the config default intact:

```yaml
# config.yaml
quality:
  tier2_extra: ["Journal of Cell Biology", "J Cell Biol", "Cell Stem Cell"]
```

**Turn it off entirely** — every searched paper becomes eligible, ordered by
`priority_score` (which still favours better venues, recent work and research
articles over reviews):

```yaml
quality:
  require_tiered_journal: false
  min_score_to_download: 0.5   # raise to keep the queue selective
```

Expect roughly **3.6× more downloads**, with a corresponding rise in curation
cost and disk. See `docs/environment_setup.md` for install and disk numbers, and
`README.md` for corpus build costs.

## Why a whitelist rather than a metric

Impact factor is proprietary, journal-level, and a poor proxy for whether one
paper's Kd measurement is sound. A short explicit list is auditable, editable,
and honest about being a judgement call — which is why it lives in a reviewable
Python module rather than being fetched from a ranking service.
