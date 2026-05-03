# DepMap & Cluster Enrichment of the Literature Graph — Project Plan

Reference document for the multi-sprint effort to enrich the existing
NetworkX literature graph (`src/_corpus_graph.py`) with genetic
co-dependency signal from DepMap CRISPR data and to extend from pairwise
edges to clusters and (eventually) cluster-level semantic neighbourhoods.

Status as of 2026-05-03: planning only. Sprint 1 in progress (naming
coverage spike).

---

## Goals

1. Attach DepMap Pearson correlations to literature-graph edges so the
   reasoning has both a research-attention proxy (literature mentions)
   and an orthogonal cell-line co-essentiality signal.
2. Move from pairwise edges to **clusters** of strongly co-correlated
   proteins, weighted by literature evidence — robustly co-functional
   modules rather than literature-attention hubs.
3. (Stretch) Compare clusters in a semantic embedding space to surface
   biologically-related cluster neighbourhoods.

## Key design decisions

- **Identifier storage**: sidecar block `protein_identifiers` written
  into existing fingerprint JSONs by an idempotent backfill script.
  Same pattern as the canonical-DOI backfill in `297be83`. Curator
  never writes this key.
- **Naming resolution tiers**: exact gene_symbol → UniProtKB-ID stem
  (e.g. `YAP1_HUMAN`) → normalised exact (existing `_normalize_protein`)
  → fuzzy → low-confidence/flagged. Disambiguated by
  `methodology.protein_origin_organism` (already an NCBI taxon int).
- **Non-human handling**: keep `native_uniprot` (original organism) and
  `human_ortholog_uniprot` (for DepMap join). Cheap path first: cross-
  species gene-symbol identity. Audit coverage before paying for
  UniProt orthology API calls.
- **DepMap correlations**: lazy singleton DataFrame in memory
  (`src/depmap.py`), on-demand pairwise `df[a].corr(df[b])`. The full
  N×N matrix is *not* stored. Sparse top-K-neighbours structure is
  built only when we move to clustering (sprint 5).
- **Cluster algorithm**: Leiden (via `python-igraph`/`leidenalg`) on the
  literature graph re-weighted by DepMap correlations. Not pure-DepMap
  clustering — that recovers cell-cycle / ribosome modules
  uninterpretably. MCL is the alternate option if Leiden disappoints.
- **Cluster embeddings (sprint 6, deferred)**: combine per-cluster
  literature embedding (mean of `situational_context_hook` vectors,
  already in LanceDB) with per-cluster ESM2/ProtT5 mean-pool. No model
  training — off-the-shelf only, unless ground-truth surfaces.

## Sprint plan

| # | Sprint | Effort | Output |
|---|---|---|---|
| 1 | Naming spike | 1 day | Coverage metric on representative fingerprint sample. Decide whether HGNC alias / UniProt-secondary download is needed. |
| 2 | Naming backfill | 2 days | Idempotent `scripts/normalize_identifiers.py` over all ~5,547 fingerprints. New `protein_identifiers` block. Ortholog mapping for non-human entries. Coverage target: >85% exact+alias. |
| 3 | DepMap loader + edge enrichment | 1–2 days | `src/depmap.py` lazy singleton; `correlation_for_pair`; one-shot graph enrichment pass attaches `depmap_pearson_r` to each literature-graph edge where both genes resolve. |
| 4 | Skill surface | 1 day | New tools `get_genetic_codependency`, enriched `export_subgraph`. Wired into both `src/mcp_server.py` and `src/skill_runner.py` dispatch. `corpus-explorer/SKILL.md` updated with usage guidance and caveats. |
| 5 | Clusters | 2–3 days | Leiden over the enriched graph with `f(literature_mentions, |depmap_r|)` weights. Cluster registry on disk. Tools `cluster_for_protein`, `cluster_members`, optional resolution-sweep dump. |
| 6 | Cluster neighbourhoods | 3–5 days, **gated** | Per-cluster lit + ESM mean-pool embeddings. Cluster-cluster similarity matrix → second-order graph export. Only after sprint-5 clusters are inspected and the question is well-posed. |

## Risks called out at planning time

1. **Naming will eat the schedule.** Build the coverage metric first
   (sprint 1) so we have concrete numbers before locking sprints 2+.
2. **DepMap correlations ≠ interaction strength.** Co-essentiality
   across cell lines often means same pathway / synthetic-lethal
   partners / co-functional module, *not* physical contact. Skill
   prompts must frame this correctly so the LLM does not conflate
   co-dependency with binding.
3. **Multiple-testing inflation** is not a real issue at scale: |r|>0.25
   over n≈1,150 cell lines is p<10⁻¹⁷ unadjusted. But edges with weak
   r (|r|<0.1) carry essentially no information; threshold at write
   time rather than storing every value.
4. **Graph cache invalidation** — current `_GRAPH_CACHE` requires MCP
   restart. After sprints 2 and 3 add new fields, switch to a
   mtime-based invalidation or stale results will burn debugging time.
5. **Non-human DepMap-ability**: even with ortholog mapping, the
   correlation values are derived from human cell lines. Caveat
   strings on results must surface this — a high `|r|` for a
   mouse-paper edge is the *human ortholog* correlation, not
   validation in the original organism.

## Open questions deferred to later sprints

- Whether to store cluster registry as JSON on disk vs. a new SQLite
  table next to `data/literature.db`.
- Whether to expose multiple Leiden resolutions or a single canonical
  partitioning. Decide after eyeballing sprint-5 output.
- Whether sprint 6 needs a learned embedding combiner (concat → MLP)
  or whether straight cosine-of-concat is enough. Decide empirically.

## Cross-references

- Existing graph: `src/_corpus_graph.py` (`_GRAPH_CACHE`, edge schema).
- Curation contract: `extraction_schema.json` v2.0; `curation_prompt.md`.
- DepMap source: `data/depmap/CRISPRGeneEffect.csv`; UniProt mapping
  `data/depmap/HUMAN_9606_idmapping.dat.gz`.
- Existing backfill pattern to mirror: `297be83` (canonical DOI/PMCID).
