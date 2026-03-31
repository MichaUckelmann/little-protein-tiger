"""LanceDB-backed semantic search over paper fingerprints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
from loguru import logger

# PubMedBERT-base output dimension. Verified at first ingest against the
# actual model output; raise if there's a mismatch.
_EMBEDDING_DIM = 768


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
                    "description": "Optional filter by study type.",
                    "enum": [
                        "experimental_in_vitro",
                        "experimental_in_vivo",
                        "experimental_structural",
                        "computational",
                        "review",
                        "case_study",
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
            import lancedb
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
            from sentence_transformers import SentenceTransformer
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
        """Concatenate situational_context_hook + all claim values."""
        hook = (fp.get("paper_metadata") or {}).get("situational_context_hook") or ""
        claims = [
            kf.get("claim", "")
            for kf in (fp.get("key_findings") or [])
            if kf.get("claim")
        ]
        if claims:
            findings_str = "Findings: " + " ".join(
                c if c.endswith(".") else c + "." for c in claims
            )
            return f"{hook}\n\n{findings_str}".strip()
        return hook.strip()

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

    def ingest(
        self,
        fingerprint_dir: str | Path,
        rebuild: bool = False,
    ) -> int:
        """
        Embed and index all relevant fingerprint JSONs.

        Parameters
        ----------
        fingerprint_dir : str | Path
            Directory containing ``*.json`` fingerprint files.
        rebuild : bool
            If True, drop and recreate the table before ingesting.

        Returns
        -------
        int
            Number of new records ingested.
        """
        fingerprint_dir = Path(fingerprint_dir)
        db = self._get_db()

        if rebuild and self.TABLE_NAME in db.table_names():
            db.drop_table(self.TABLE_NAME)
            self._table = None
            logger.info("Dropped existing LanceDB table for rebuild.")

        table = self._get_table(create_if_missing=True)

        # Collect already-indexed keys to enable incremental ingestion
        existing_keys: set[str] = set()
        if not rebuild:
            try:
                arrow_tbl = table.to_arrow()
                existing_keys = set(arrow_tbl["paper_key"].to_pylist())
                logger.info(f"{len(existing_keys)} fingerprints already indexed.")
            except Exception as exc:
                logger.warning(f"Could not read existing keys (will re-index all): {exc}")

        encoder = self._get_encoder()

        fp_files = sorted(fingerprint_dir.glob("*.json"))
        logger.info(f"Found {len(fp_files)} JSON files in {fingerprint_dir}")

        batch_texts: list[str] = []
        batch_meta: list[dict] = []

        for fp_file in fp_files:
            try:
                fp = json.loads(fp_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Skipping {fp_file.name}: {exc}")
                continue

            if not fp.get("relevant", True):
                continue

            paper_key = self._derive_paper_key(fp, fp_file.stem)
            if paper_key in existing_keys:
                continue

            embed_text = self._build_embed_text(fp)
            if not embed_text:
                logger.warning(f"No embeddable text for {fp_file.name} — skipping.")
                continue

            batch_texts.append(embed_text)
            batch_meta.append({
                "paper_key": paper_key,
                "doi":        (fp.get("paper_metadata") or {}).get("doi") or "",
                "title":      (fp.get("paper_metadata") or {}).get("title") or "",
                "study_type": (fp.get("paper_metadata") or {}).get("study_type") or "",
                "embed_text": embed_text,
                "fingerprint_json": json.dumps(fp, ensure_ascii=False),
            })

        if not batch_meta:
            logger.info("Nothing new to ingest.")
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
        logger.info(f"Ingested {len(records)} fingerprints.")
        return len(records)

    def search(
        self,
        query: str,
        top_k: int = 5,
        study_type: Optional[str] = None,
    ) -> list[dict]:
        """
        Semantic search over the indexed corpus.

        Returns
        -------
        list[dict]
            Ranked list, each containing: score, paper_key, doi, title,
            study_type, embed_text, fingerprint (full dict).
        """
        table = self._get_table(create_if_missing=False)
        encoder = self._get_encoder()

        query_vec = encoder.encode(query, normalize_embeddings=True).tolist()

        # Over-fetch when filtering by study_type so post-filter has headroom
        fetch_limit = top_k if study_type is None else top_k * 4

        search_builder = (
            table.search(query_vec)
            .metric("cosine")
            .limit(fetch_limit)
        )

        if study_type:
            # prefilter=False → run vector search first, filter after (better recall)
            try:
                search_builder = search_builder.where(
                    f"study_type = '{study_type}'", prefilter=False
                )
            except TypeError:
                # Older LanceDB versions don't have prefilter kwarg
                search_builder = search_builder.where(
                    f"study_type = '{study_type}'"
                )

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
                "score":      score,
                "paper_key":  results_arrow["paper_key"][i].as_py(),
                "doi":        results_arrow["doi"][i].as_py(),
                "title":      results_arrow["title"][i].as_py(),
                "study_type": results_arrow["study_type"][i].as_py(),
                "embed_text": results_arrow["embed_text"][i].as_py(),
                "fingerprint": fp,
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

        results = self.search(query=query, top_k=top_k, study_type=study_type)

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
                f"    DOI: {r['doi'] or 'N/A'} | study_type: {r['study_type']}\n"
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
                "situational_context_hook": pm.get("situational_context_hook", ""),
                "key_findings":            findings,
                "proteins":                proteins,
            })

        return json.dumps(
            {"result_text": "\n".join(lines), "papers": papers_out},
            ensure_ascii=False,
        )
