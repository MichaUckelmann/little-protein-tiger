"""LanceDB-backed semantic search over paper fingerprints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Sequence

import pyarrow as pa   # noqa: F401 — schema only; ships in the base install
from loguru import logger

# PubMedBERT-base output dimension. Verified at first ingest against the
# actual model output; raise if there's a mismatch.
_EMBEDDING_DIM = 768



def _sql_quote(value: str) -> str:
    """Quote a string for LanceDB's SQL-ish filter language.

    These values reach us from an LLM tool call. The tool schema declares an
    enum, but that is advisory — nothing enforces it at the API boundary — so
    an embedded quote would otherwise break out of the literal and into the
    predicate. Doubling single quotes is the SQL standard escape; control
    characters are dropped outright since no legitimate enum value has any.
    """
    cleaned = "".join(ch for ch in str(value) if ch.isprintable())
    return "'" + cleaned.replace("'", "''") + "'"



_CORPUS_EXTRA_HINT = (
    "This needs the optional `corpus` extra, which is not installed.\n"
    "    pip install -e \".[corpus]\"\n"
    "It pulls in sentence-transformers and lancedb (and, transitively, torch —\n"
    "about 3 GB). It is optional because semantic corpus search is the only\n"
    "thing that needs it: structure tools, both design tracks and the report\n"
    "generators all run without it."
)


def _require_corpus_extra(module: str, exc: Exception) -> "NoReturn":
    raise ModuleNotFoundError(f"{module} is required for corpus search.\n"
                              f"{_CORPUS_EXTRA_HINT}") from exc


class VectorStore:
    """
    Wraps a LanceDB table of embedded paper fingerprints.

    Parameters
    ----------
    db_path : str | Path
        Directory for the LanceDB database (e.g., "data/vectors").
    embedding_model : str
        Sentence-transformers model name.
    """

    TABLE_NAME = "fingerprints"

    SCHEMA: pa.Schema = pa.schema([
        pa.field("paper_key",        pa.string()),
        pa.field("doi",              pa.string()),
        pa.field("title",            pa.string()),
        pa.field("study_type",       pa.string()),
        pa.field("study_category",   pa.string()),
        pa.field("embed_text",       pa.string()),
        pa.field("vector",           pa.list_(pa.float32(), _EMBEDDING_DIM)),
        pa.field("fingerprint_json", pa.string()),
    ])

    SEARCH_TOOL_DEFINITION: dict[str, Any] = {
        "name": "search_corpus",
        "description": (
            "Semantically search the curated scientific literature corpus. "
            "Returns the top-k most relevant paper fingerprints ranked by "
            "similarity to your query. Each result includes the full "
            "situational context, key quantitative findings, and metadata. "
            "Use this tool whenever the user asks about specific proteins, "
            "mechanisms, assay types, inhibitors, quantitative values (Kd, Ki), "
            "or study designs present in the corpus."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language search query. Be specific: include "
                        "protein names, mechanisms, assay types, or quantitative "
                        "terms for best results "
                        "(e.g., 'STAT5 inhibitor fluorescence polarization Ki')."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (1–20). Default is 5.",
                },
                "study_type": {
                    "type": "string",
                    "description": "Optional filter by study methodology.",
                    "enum": [
                        "experimental_in_vitro",
                        "experimental_in_vivo",
                        "experimental_structural",
                        "computational",
                        "review",
                        "case_study",
                    ],
                },
                "study_category": {
                    "type": "string",
                    "description": (
                        "Optional filter by scientific domain. Use 'pathway_biology' "
                        "to find disease mechanism and target selection papers. "
                        "Use 'biochemistry' for binding assay and inhibitor papers."
                    ),
                    "enum": [
                        "biochemistry",
                        "pathway_biology",
                        "structural_biology",
                        "enzymology",
                        "biocatalysis",
                        "computational_chemistry",
                        "host_pathogen",
                        "clinical",
                        "review",
                    ],
                },
            },
            "required": ["query"],
        },
    }

    def __init__(
        self,
        db_path: str | Path,
        embedding_model: str = "NeuML/pubmedbert-base-embeddings",
    ) -> None:
        self.db_path = Path(db_path)
        self.embedding_model_name = embedding_model
        self._db = None
        self._table = None
        self._encoder = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_db(self):
        if self._db is None:
            try:
                import lancedb
            except ModuleNotFoundError as exc:
                _require_corpus_extra("lancedb", exc)
            self.db_path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.db_path))
        return self._db

    def _get_table(self, create_if_missing: bool = False):
        if self._table is not None:
            return self._table
        db = self._get_db()
        existing = db.table_names()
        if self.TABLE_NAME in existing:
            self._table = db.open_table(self.TABLE_NAME)
        elif create_if_missing:
            self._table = db.create_table(
                self.TABLE_NAME, schema=self.SCHEMA, mode="create"
            )
        else:
            raise RuntimeError(
                f"LanceDB table '{self.TABLE_NAME}' not found at {self.db_path}. "
                "Run `python scripts/ingest_vectors.py` first."
            )
        return self._table

    def _get_encoder(self):
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ModuleNotFoundError as exc:
                _require_corpus_extra("sentence-transformers", exc)
            logger.info(f"Loading embedding model: {self.embedding_model_name}")
            try:
                self._encoder = SentenceTransformer(
                    self.embedding_model_name, local_files_only=True
                )
            except Exception:
                self._encoder = SentenceTransformer(self.embedding_model_name)
            # Verify dimension matches schema
            sample = self._encoder.encode(["dim check"])
            actual_dim = sample.shape[1]
            if actual_dim != _EMBEDDING_DIM:
                raise ValueError(
                    f"Embedding model '{self.embedding_model_name}' produces "
                    f"{actual_dim}-dim vectors but schema expects {_EMBEDDING_DIM}. "
                    "Update _EMBEDDING_DIM or choose a compatible model."
                )
        return self._encoder

    @staticmethod
    def _build_embed_text(fp: dict) -> str:
        """Concatenate protein names + situational_context_hook + pathway context (if present) + claim values."""
        hook = (fp.get("paper_metadata") or {}).get("situational_context_hook") or ""

        # Prepend all protein names so gene symbols are reliably findable by vector search
        protein_names: list[str] = []
        for p in (fp.get("entities") or {}).get("proteins") or []:
            if isinstance(p, str) and p:
                protein_names.append(p)
        for kf in (fp.get("key_findings") or []):
            for p in (kf.get("protein_pair") or []):
                if isinstance(p, str) and p and p not in protein_names:
                    protein_names.append(p)
        proteins_str = f"Proteins: {', '.join(protein_names)}." if protein_names else ""

        # Enrich embed text with pathway context for pathway_biology papers
        pathway_str = ""
        pathway_ctx = fp.get("pathway_context")
        if pathway_ctx:
            parts = []
            pathways = pathway_ctx.get("pathways") or []
            if pathways:
                parts.append(f"Pathways: {', '.join(pathways)}.")
            for d in (pathway_ctx.get("disease_associations") or []):
                disease = d.get("disease", "")
                mechanism = d.get("mechanism", "")
                if disease and mechanism:
                    parts.append(f"{disease}: {mechanism}.")
            for n in (pathway_ctx.get("target_nodes") or []):
                protein = n.get("protein", "")
                position = n.get("pathway_position", "")
                dysreg = n.get("dysregulation", "")
                if protein:
                    parts.append(f"{protein} ({position}, {dysreg}).")
            regs = pathway_ctx.get("upstream_regulators") or []
            effs = pathway_ctx.get("downstream_effectors") or []
            if regs:
                parts.append(f"Upstream regulators: {', '.join(regs)}.")
            if effs:
                parts.append(f"Downstream effectors: {', '.join(effs)}.")
            if parts:
                pathway_str = " ".join(parts)

        claims = [
            kf.get("claim", "")
            for kf in (fp.get("key_findings") or [])
            if kf.get("claim")
        ]
        findings_str = ""
        if claims:
            findings_str = "Findings: " + " ".join(
                c if c.endswith(".") else c + "." for c in claims
            )

        sections = [s for s in [proteins_str, hook, pathway_str, findings_str] if s.strip()]
        return "\n\n".join(sections).strip()

    @staticmethod
    def _derive_paper_key(fp: dict, filename_stem: str) -> str:
        """
        Derive canonical paper_key from the fingerprint JSON.

        Mirrors database.py::_paper_key() precedence:
          doi:<doi>  >  pmcid:<pmcid>  >  filename stem fallback
        """
        doi = (fp.get("paper_metadata") or {}).get("doi")
        if doi:
            return f"doi:{doi}"
        if filename_stem.startswith("pmcid_"):
            return f"pmcid:{filename_stem[len('pmcid_'):]}"
        return filename_stem

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    #: Populated by every `ingest()` call. Keys: added, updated, unchanged,
    #: pruned, skipped_irrelevant, skipped_empty, orphans_seen.
    last_ingest_stats: dict[str, int]

    def ingest(
        self,
        fingerprint_dir: str | Path,
        rebuild: bool = False,
        prune_orphans: bool = False,
    ) -> int:
        """
        Embed and index all relevant fingerprint JSONs.

        Parameters
        ----------
        fingerprint_dir : str | Path
            Directory containing ``*.json`` fingerprint files.
        rebuild : bool
            If True, drop and recreate the table before ingesting.
        prune_orphans : bool
            Delete indexed rows whose fingerprint file no longer exists.
            Off by default because it is destructive; without it those rows
            keep matching searches that a `get_fingerprint` follow-up cannot
            then resolve.

        Returns
        -------
        int
            Rows written (new + re-embedded). `last_ingest_stats` carries the
            full breakdown: added / updated / unchanged / pruned /
            skipped_irrelevant / skipped_empty / orphans_seen.
        """
        fingerprint_dir = Path(fingerprint_dir)
        db = self._get_db()

        if rebuild and self.TABLE_NAME in db.table_names():
            db.drop_table(self.TABLE_NAME)
            self._table = None
            logger.info("Dropped existing LanceDB table for rebuild.")

        table = self._get_table(create_if_missing=True)

        stats = {"added": 0, "updated": 0, "unchanged": 0, "pruned": 0,
                 "skipped_irrelevant": 0, "skipped_empty": 0, "orphans_seen": 0}
        self.last_ingest_stats = stats

        # Existing key -> the embed_text that was actually embedded for it.
        # Keying on the TEXT, not just the key, is what makes a re-curated
        # fingerprint re-embed: ingestion used to be add-only on `paper_key`,
        # so an edited fingerprint kept its stale vector forever and only a
        # full `--rebuild` could fix it.
        existing: dict[str, str] = {}
        if not rebuild:
            try:
                arrow_tbl = table.to_arrow()
                existing = dict(zip(arrow_tbl["paper_key"].to_pylist(),
                                    arrow_tbl["embed_text"].to_pylist()))
                logger.info(f"{len(existing)} fingerprints already indexed.")
            except Exception as exc:
                # Deliberately fatal. The old behaviour logged "will re-index
                # all" and carried on with an EMPTY key set, which does not
                # re-index — `table.add` has no primary key, so it appends a
                # second copy of the entire corpus.
                raise RuntimeError(
                    f"could not read the existing vector index ({exc}). "
                    f"Refusing to continue: ingestion is an append, so running "
                    f"without the current key set would duplicate every row. "
                    f"Re-run with --rebuild to recreate the table from scratch."
                ) from exc

        encoder = self._get_encoder()

        fp_files = sorted(fingerprint_dir.glob("*.json"))
        logger.info(f"Found {len(fp_files)} JSON files in {fingerprint_dir}")

        batch_texts: list[str] = []
        batch_meta: list[dict] = []
        stale_keys: list[str] = []
        seen_keys: set[str] = set()

        for fp_file in fp_files:
            try:
                fp = json.loads(fp_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Skipping {fp_file.name}: {exc}")
                continue

            if not fp.get("relevant", True):
                stats["skipped_irrelevant"] += 1
                continue

            paper_key = self._derive_paper_key(fp, fp_file.stem)
            seen_keys.add(paper_key)

            embed_text = self._build_embed_text(fp)
            if not embed_text:
                logger.warning(f"No embeddable text for {fp_file.name} — skipping.")
                stats["skipped_empty"] += 1
                continue

            if paper_key in existing:
                if existing[paper_key] == embed_text:
                    stats["unchanged"] += 1
                    continue
                # Re-curated (or re-normalised) since it was embedded: drop the
                # old row and embed the new text in this same pass.
                stale_keys.append(paper_key)
                stats["updated"] += 1
            else:
                stats["added"] += 1

            batch_texts.append(embed_text)
            batch_meta.append({
                "paper_key":       paper_key,
                "doi":             (fp.get("paper_metadata") or {}).get("doi") or "",
                "title":           (fp.get("paper_metadata") or {}).get("title") or "",
                "study_type":      (fp.get("paper_metadata") or {}).get("study_type") or "",
                "study_category":  fp.get("study_category") or "",
                "embed_text":      embed_text,
                "fingerprint_json": json.dumps(fp, ensure_ascii=False),
            })

        # Rows whose fingerprint file is gone. Add-only ingestion could never
        # remove them, so the shipped index carries ~1,700 rows that
        # `get_fingerprint` cannot resolve. Destructive, therefore opt-in.
        orphans = sorted(set(existing) - seen_keys)
        stats["orphans_seen"] = len(orphans)
        if orphans and not prune_orphans:
            logger.warning(
                f"{len(orphans)} indexed rows have no fingerprint file on disk "
                f"and will keep returning from search_corpus (a get_fingerprint "
                f"follow-up on them fails). Pass --prune-orphans to delete them.")
        if orphans and prune_orphans:
            stats["pruned"] = self._delete_keys(table, orphans)
            logger.info(f"Pruned {stats['pruned']} orphaned rows.")

        if stale_keys:
            removed = self._delete_keys(table, stale_keys)
            logger.info(f"Re-embedding {removed} changed fingerprints.")

        if not batch_meta:
            logger.info(
                f"Nothing to embed (unchanged {stats['unchanged']}, "
                f"pruned {stats['pruned']}).")
            return 0

        logger.info(f"Encoding {len(batch_texts)} fingerprints...")
        vectors = encoder.encode(
            batch_texts,
            batch_size=32,
            show_progress_bar=True,
            normalize_embeddings=True,
        )

        records = [
            {**meta, "vector": vec.tolist()}
            for meta, vec in zip(batch_meta, vectors)
        ]

        table.add(records)
        logger.info(
            f"Ingested {len(records)} fingerprints "
            f"(new {stats['added']}, re-embedded {stats['updated']}, "
            f"unchanged {stats['unchanged']}, pruned {stats['pruned']}).")
        return len(records)

    @staticmethod
    def _delete_keys(table, keys: Sequence[str], chunk: int = 200) -> int:
        """
        Delete rows by ``paper_key``, in chunks.

        One predicate per key would be thousands of round trips; one predicate
        for all of them overflows LanceDB's filter parser on a full corpus.
        Values go through `_sql_quote` for the same reason search filters do —
        a `paper_key` is a DOI, and DOIs are allowed to contain quotes.
        """
        deleted = 0
        for i in range(0, len(keys), chunk):
            batch = keys[i:i + chunk]
            predicate = "paper_key IN (%s)" % ", ".join(
                _sql_quote(k) for k in batch)
            try:
                table.delete(predicate)
                deleted += len(batch)
            except Exception as exc:  # noqa: BLE001 - one bad chunk must not
                logger.warning(  # abort an otherwise-good ingest
                    f"could not delete {len(batch)} rows: {exc}")
        return deleted

    def search(
        self,
        query: str,
        top_k: int = 5,
        study_type: Optional[str] = None,
        study_category: Optional[str] = None,
    ) -> list[dict]:
        """
        Semantic search over the indexed corpus.

        Returns
        -------
        list[dict]
            Ranked list, each containing: score, paper_key, doi, title,
            study_type, study_category, embed_text, fingerprint (full dict).
        """
        table = self._get_table(create_if_missing=False)
        encoder = self._get_encoder()

        query_vec = encoder.encode(query, normalize_embeddings=True).tolist()

        # No over-fetch: the filter is applied BEFORE the search (see the
        # prefilter note below), so `limit` already means "this many rows that
        # match the filter" rather than "this many rows, some of which may".
        fetch_limit = top_k

        search_builder = (
            table.search(query_vec)
            .metric("cosine")
            .limit(fetch_limit)
        )

        # ONE where() call, not two. LanceDB's builder assigns
        # `self._where = where`, so a second call REPLACES the first rather
        # than ANDing it: asking for study_type + study_category silently
        # applied the category only, and a caller that thought it had
        # constrained methodology got results from every study_type.
        # Verified against lancedb 0.30.2.
        clauses = []
        if study_type:
            clauses.append(f"study_type = {_sql_quote(study_type)}")
        if study_category:
            clauses.append(f"study_category = {_sql_quote(study_category)}")
        if clauses:
            predicate = " AND ".join(clauses)
            # prefilter=True — filter first, THEN search the matching rows.
            #
            # Post-filtering (prefilter=False) runs the ANN search first and
            # drops non-matching rows afterwards, so a filtered query only ever
            # sees the `limit` globally-nearest rows. Any category rarer than
            # ~1-in-limit is filtered to nothing, and the caller cannot tell
            # that apart from "the corpus has no such papers". Measured on this
            # corpus: `study_category='structural_biology'` (327 papers, 2.6%)
            # returned 0 rows at limit=20 and 20 rows with prefilter on. The
            # two categories that DID work — biochemistry and pathway_biology —
            # are 90% of the corpus between them, which is why this survived.
            # It mattered: molecular-biology-expert is told to prefer
            # `experimental_structural`, i.e. exactly the starved case.
            try:
                search_builder = search_builder.where(predicate, prefilter=True)
            except TypeError:      # older lancedb without the kwarg
                search_builder = search_builder.where(predicate)

        results_arrow = search_builder.to_arrow()

        # Distance column name varies by LanceDB version
        col_names = results_arrow.schema.names
        dist_col = "_distance" if "_distance" in col_names else "_score"

        output = []
        for i in range(min(top_k, results_arrow.num_rows)):
            raw = results_arrow[dist_col][i].as_py() if dist_col in col_names else 0.0
            score = round(1.0 - float(raw or 0.0), 4)

            fp = json.loads(results_arrow["fingerprint_json"][i].as_py())
            output.append({
                "score":          score,
                "paper_key":      results_arrow["paper_key"][i].as_py(),
                "doi":            results_arrow["doi"][i].as_py(),
                "title":          results_arrow["title"][i].as_py(),
                "study_type":     results_arrow["study_type"][i].as_py(),
                "study_category": results_arrow["study_category"][i].as_py(),
                "embed_text":     results_arrow["embed_text"][i].as_py(),
                "fingerprint":    fp,
            })

        return output

    def execute_search_tool(self, tool_input: dict) -> str:
        """
        Execute a search_corpus tool call from the Claude API.

        Parameters
        ----------
        tool_input : dict
            The ``input`` dict from a Claude tool_use content block.

        Returns
        -------
        str
            JSON string with keys ``result_text`` (for Claude to read) and
            ``papers`` (structured list for programmatic use).
        """
        query = tool_input.get("query", "")
        top_k = max(1, min(int(tool_input.get("top_k", 5)), 20))
        study_type = tool_input.get("study_type")
        study_category = tool_input.get("study_category")

        results = self.search(
            query=query, top_k=top_k,
            study_type=study_type, study_category=study_category,
        )

        if not results:
            return json.dumps({"result_text": "No results found.", "papers": []})

        lines = [f"Found {len(results)} result(s) for query: \"{query}\"\n"]
        papers_out = []

        for i, r in enumerate(results, start=1):
            fp = r["fingerprint"]
            pm = fp.get("paper_metadata") or {}
            findings = fp.get("key_findings") or []
            proteins = (fp.get("entities") or {}).get("proteins") or []

            # Up to 3 key findings with quantitative values
            finding_lines = []
            for kf in findings[:3]:
                claim = kf.get("claim", "")
                ev = kf.get("evidence_value", "")
                kd = kf.get("affinities_kd_Molar")
                ki = kf.get("inhibitory_constant_Ki")
                line = f"  - {claim}"
                if ev:
                    line += f" [{ev}]"
                if kd is not None:
                    line += f" (Kd={kd:.2e} M)"
                if ki is not None:
                    line += f" (Ki={ki:.2e} M)"
                finding_lines.append(line)

            block = (
                f"[{i}] score={r['score']:.3f} | {r['title']}\n"
                f"    DOI: {r['doi'] or 'N/A'} | study_type: {r['study_type']} | study_category: {r.get('study_category', '')}\n"
                f"    Context: {pm.get('situational_context_hook', '')}\n"
            )
            if finding_lines:
                block += "    Key findings:\n" + "\n".join(finding_lines) + "\n"
            if proteins:
                block += f"    Proteins: {', '.join(proteins[:8])}\n"

            lines.append(block)
            papers_out.append({
                "rank":                    i,
                "score":                   r["score"],
                "paper_key":               r["paper_key"],
                "doi":                     r["doi"],
                "title":                   r["title"],
                "study_type":              r["study_type"],
                "study_category":          r.get("study_category", ""),
                "situational_context_hook": pm.get("situational_context_hook", ""),
                "key_findings":            findings,
                "proteins":                proteins,
            })

        return json.dumps(
            {"result_text": "\n".join(lines), "papers": papers_out},
            ensure_ascii=False,
        )
