import hashlib
import sqlite3
from contextlib import contextmanager
import json
from pathlib import Path
from typing import Optional
from loguru import logger

from .models import Paper, DownloadStatus, CurationStatus, Source


def _paper_key(doi: Optional[str], pmcid: Optional[str], pmid: Optional[str], title: str) -> str:
    """Stable unique key: prefer DOI, then PMCID, then title hash."""
    if doi:
        return f"doi:{doi}"
    if pmcid:
        return f"pmcid:{pmcid}"
    return f"title:{hashlib.md5(title.encode()).hexdigest()}"


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate()

    @contextmanager
    def _connect(self):
        """A connection that is COMMITTED and CLOSED.

        `with sqlite3.connect(...) as conn` commits on exit but does NOT close
        — every call leaked a handle, which a long-lived MCP server
        accumulates over a session. Wrapping it keeps every existing
        `with self._connect() as conn:` call site working unchanged.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS papers (
                    paper_key TEXT PRIMARY KEY,
                    doi TEXT,
                    pmcid TEXT,
                    pmid TEXT,
                    title TEXT NOT NULL,
                    authors TEXT,
                    journal TEXT,
                    year INTEGER,
                    abstract TEXT,
                    source TEXT,
                    pub_types TEXT,
                    pdf_url TEXT,
                    xml_url TEXT,
                    pdf_path TEXT,
                    download_status TEXT NOT NULL DEFAULT 'pending',
                    priority_score REAL NOT NULL DEFAULT 0.5,
                    curated INTEGER NOT NULL DEFAULT 0,
                    fail_reason TEXT,
                    curation_status TEXT NOT NULL DEFAULT 'pending',
                    curated_at TEXT,
                    fingerprint_path TEXT,
                    curation_model TEXT,
                    curation_tokens INTEGER,
                    curation_error TEXT,
                    -- Reuse licence of the PAPER, resolved from Europe PMC by
                    -- `scripts/audit_paper_licences.py`. A fingerprint is
                    -- derived from the paper's content, so a No-Derivatives
                    -- term (`cc by-nc-nd`, `cc by-nd`) is what decides whether
                    -- that fingerprint can be redistributed — and an EMPTY
                    -- value is not permission, it is the absence of one.
                    -- NULL means "not yet checked"; the empty string means
                    -- "checked, and the source records no licence".
                    licence TEXT,
                    licence_source TEXT,
                    licence_checked_at TEXT
                )
            """)

    def _migrate(self):
        """Add columns introduced after initial schema creation."""
        with self._connect() as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
            if "xml_url" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN xml_url TEXT")
                logger.debug("Migrated DB: added xml_url column")
            if "pub_types" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN pub_types TEXT")
                logger.debug("Migrated DB: added pub_types column")
            if "priority_score" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN priority_score REAL NOT NULL DEFAULT 0.5")
                logger.debug("Migrated DB: added priority_score column")
            if "curation_status" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN curation_status TEXT NOT NULL DEFAULT 'pending'")
                logger.debug("Migrated DB: added curation_status column")
            if "curated_at" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN curated_at TEXT")
                logger.debug("Migrated DB: added curated_at column")
            if "fingerprint_path" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN fingerprint_path TEXT")
                logger.debug("Migrated DB: added fingerprint_path column")
            if "curation_model" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN curation_model TEXT")
                logger.debug("Migrated DB: added curation_model column")
            if "curation_tokens" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN curation_tokens INTEGER")
                logger.debug("Migrated DB: added curation_tokens column")
            if "curation_error" not in existing:
                conn.execute("ALTER TABLE papers ADD COLUMN curation_error TEXT")
                logger.debug("Migrated DB: added curation_error column")
            # Reuse licence. Added 2026-09-10, after an audit found 1,506
            # curated papers under a No-Derivatives term and 5,684 with no
            # licence recorded at all — neither of which the database could
            # express, so neither could gate anything.
            for col in ("licence", "licence_source", "licence_checked_at"):
                if col not in existing:
                    conn.execute(f"ALTER TABLE papers ADD COLUMN {col} TEXT")
                    logger.debug(f"Migrated DB: added {col} column")

    def set_licence(self, paper_key: str, licence: str | None,
                    source: str | None = None, checked_at: str | None = None):
        """Record a resolved reuse licence.

        `""` is meaningful and different from NULL: it means the lookup ran and
        the source records no licence, which `paper_licence.permits_derivatives`
        reads as restricted. NULL means nobody has looked yet.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE papers SET licence=?, licence_source=?, "
                "licence_checked_at=? WHERE paper_key=?",
                (licence, source, checked_at, paper_key))

    def upsert_paper(self, paper: Paper):
        """Insert or update a paper keyed by DOI > PMCID > title hash."""
        key = _paper_key(paper.doi, paper.pmcid, paper.pmid, paper.title)
        authors_json   = json.dumps(paper.authors)
        pubtypes_json  = json.dumps(paper.pub_types)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO papers
                    (paper_key, doi, pmcid, pmid, title, authors, journal, year, abstract,
                     source, pub_types, pdf_url, xml_url, pdf_path,
                     download_status, priority_score, curated)
                VALUES
                    (:key, :doi, :pmcid, :pmid, :title, :authors, :journal, :year, :abstract,
                     :source, :pub_types, :pdf_url, :xml_url, :pdf_path,
                     :download_status, :priority_score, :curated)
                ON CONFLICT(paper_key) DO UPDATE SET
                    doi = COALESCE(excluded.doi, papers.doi),
                    pmcid = COALESCE(excluded.pmcid, papers.pmcid),
                    pmid = COALESCE(excluded.pmid, papers.pmid),
                    title = excluded.title,
                    authors = excluded.authors,
                    journal = excluded.journal,
                    year = excluded.year,
                    abstract = excluded.abstract,
                    source = excluded.source,
                    pub_types = excluded.pub_types,
                    pdf_url = COALESCE(papers.pdf_url, excluded.pdf_url),
                    xml_url = COALESCE(papers.xml_url, excluded.xml_url),
                    pdf_path = COALESCE(papers.pdf_path, excluded.pdf_path),
                    priority_score = excluded.priority_score,
                    download_status = CASE
                        WHEN papers.download_status = 'downloaded' THEN 'downloaded'
                        ELSE excluded.download_status
                    END
            """, {
                "key": key,
                "doi": paper.doi,
                "pmcid": paper.pmcid,
                "pmid": paper.pmid,
                "title": paper.title,
                "authors": authors_json,
                "journal": paper.journal,
                "year": paper.year,
                "abstract": paper.abstract,
                "source": paper.source.value if paper.source else None,
                "pub_types": pubtypes_json,
                "pdf_url": paper.pdf_url,
                "xml_url": paper.xml_url,
                "pdf_path": paper.pdf_path,
                "download_status": paper.download_status.value,
                "priority_score": paper.priority_score,
                "curated": int(paper.curated),
            })

    def get_papers(self, status: Optional[DownloadStatus] = None) -> list[Paper]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM papers WHERE download_status = ?", (status.value,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM papers").fetchall()
        return [self._row_to_paper(r) for r in rows]

    def mark_downloaded(self, doi: Optional[str], pmcid: Optional[str], path: str):
        # `WHERE pmcid = NULL` matches NOTHING in SQL (NULL != NULL), so a
        # paper with neither identifier updates 0 rows and stays 'pending'
        # forever — re-detected as already-on-disk on every run and never
        # curated. Warn instead of raising: the file IS downloaded, and
        # aborting the whole download loop over 3 unidentifiable rows would be
        # a worse trade than a visible warning.
        key_val, key_col = (doi, "doi") if doi else (pmcid, "pmcid")
        if not key_val:
            logger.warning(
                f"cannot mark downloaded: paper has neither DOI nor PMCID "
                f"(path/reason={path!r}). It will stay 'pending' "
                f"and be re-examined on the next run.")
            return
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE papers SET download_status='downloaded', pdf_path=? WHERE {key_col}=?",
                (path, key_val)
            )
            if cur.rowcount == 0:
                logger.warning(
                    f"marking downloaded matched no row for {key_col}={key_val!r} — "
                    f"the paper stays 'pending'.")

    def get_uncurated(self, limit: int = 0) -> list[Paper]:
        """Papers that are downloaded but not yet curated."""
        query = (
            "SELECT * FROM papers "
            "WHERE download_status = 'downloaded' AND curation_status = 'pending' "
            "ORDER BY priority_score DESC"
        )
        if limit > 0:
            query += f" LIMIT {limit}"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [self._row_to_paper(r) for r in rows]

    def mark_curated(self, paper_key: str, fingerprint_path: str, model: str, tokens: int):
        with self._connect() as conn:
            conn.execute(
                """UPDATE papers SET
                    curation_status = 'completed',
                    curated = 1,
                    curated_at = datetime('now'),
                    fingerprint_path = ?,
                    curation_model = ?,
                    curation_tokens = ?,
                    curation_error = NULL
                WHERE paper_key = ?""",
                (fingerprint_path, model, tokens, paper_key),
            )

    def mark_curation_failed(self, paper_key: str, error: str):
        with self._connect() as conn:
            conn.execute(
                """UPDATE papers SET
                    curation_status = 'failed',
                    curation_error = ?
                WHERE paper_key = ?""",
                (error, paper_key),
            )

    def mark_curation_skipped(self, paper_key: str):
        with self._connect() as conn:
            conn.execute(
                """UPDATE papers SET
                    curation_status = 'skipped',
                    curated_at = datetime('now')
                WHERE paper_key = ?""",
                (paper_key,),
            )

    def reset_curation(self, paper_key: str):
        """Reset curation status to pending (for --reprocess)."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE papers SET
                    curation_status = 'pending',
                    curated = 0,
                    curated_at = NULL,
                    fingerprint_path = NULL,
                    curation_model = NULL,
                    curation_tokens = NULL,
                    curation_error = NULL
                WHERE paper_key = ?""",
                (paper_key,),
            )

    def mark_failed(self, doi: Optional[str], pmcid: Optional[str], reason: str):
        # `WHERE pmcid = NULL` matches NOTHING in SQL (NULL != NULL), so a
        # paper with neither identifier updates 0 rows and stays 'pending'
        # forever — re-detected as already-on-disk on every run and never
        # curated. Warn instead of raising: the file IS downloaded, and
        # aborting the whole download loop over 3 unidentifiable rows would be
        # a worse trade than a visible warning.
        key_val, key_col = (doi, "doi") if doi else (pmcid, "pmcid")
        if not key_val:
            logger.warning(
                f"cannot mark failed: paper has neither DOI nor PMCID "
                f"(path/reason={reason!r}). It will stay 'pending' "
                f"and be re-examined on the next run.")
            return
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE papers SET download_status='failed', fail_reason=? WHERE {key_col}=?",
                (reason, key_val)
            )
            if cur.rowcount == 0:
                logger.warning(
                    f"marking failed matched no row for {key_col}={key_val!r} — "
                    f"the paper stays 'pending'.")

    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            by_status = {
                row["download_status"]: row["cnt"]
                for row in conn.execute(
                    "SELECT download_status, COUNT(*) as cnt FROM papers GROUP BY download_status"
                ).fetchall()
            }
            by_source = {
                row["source"]: row["cnt"]
                for row in conn.execute(
                    "SELECT source, COUNT(*) as cnt FROM papers GROUP BY source"
                ).fetchall()
            }
        return {"total": total, "by_status": by_status, "by_source": by_source}

    @staticmethod
    def _row_to_paper(row: sqlite3.Row) -> Paper:
        keys = row.keys()
        authors   = json.loads(row["authors"])   if row["authors"]   else []
        pub_types = json.loads(row["pub_types"]) if "pub_types" in keys and row["pub_types"] else []
        return Paper(
            doi=row["doi"],
            pmcid=row["pmcid"],
            pmid=row["pmid"],
            title=row["title"],
            authors=authors,
            journal=row["journal"],
            year=row["year"],
            abstract=row["abstract"],
            source=Source(row["source"]) if row["source"] else None,
            pub_types=pub_types,
            pdf_url=row["pdf_url"],
            xml_url=row["xml_url"] if "xml_url" in keys else None,
            pdf_path=row["pdf_path"],
            download_status=DownloadStatus(row["download_status"]),
            priority_score=row["priority_score"] if "priority_score" in keys else 0.5,
            curated=bool(row["curated"]),
            curation_status=CurationStatus(row["curation_status"]) if "curation_status" in keys and row["curation_status"] else CurationStatus.pending,
            curated_at=row["curated_at"] if "curated_at" in keys else None,
            fingerprint_path=row["fingerprint_path"] if "fingerprint_path" in keys else None,
            curation_model=row["curation_model"] if "curation_model" in keys else None,
            curation_tokens=row["curation_tokens"] if "curation_tokens" in keys else None,
            curation_error=row["curation_error"] if "curation_error" in keys else None,
        )
