import os
import re
import time

import requests
from loguru import logger

from .models import Paper, Source


EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PMC_AWS_BASE   = "https://pmc-oa-opendata.s3.amazonaws.com"
NCBI_ESEARCH   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_ESUMMARY  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
S2_BULK_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
S2_FIELDS      = (
    "paperId,title,abstract,year,authors,journal,publicationTypes,"
    "externalIds,openAccessPdf,citationCount,isOpenAccess"
)

from .downloader import UNRELIABLE_PDF_HOSTS as _UNRELIABLE_PDF_HOSTS


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _pmc_aws_urls(pmcid: str) -> tuple[str, str]:
    """
    Construct direct HTTPS URLs for a PMC article's PDF and XML from the
    AWS S3 Open Data bucket (world-readable, no API call required).
    Version 1 covers ~99% of articles; the downloader probes v2 on 404.
    """
    prefix = f"{PMC_AWS_BASE}/{pmcid}.1"
    return f"{prefix}/{pmcid}.1.pdf", f"{prefix}/{pmcid}.1.xml"


def _europepmc_pdf_url(result: dict) -> str | None:
    """Extract a usable PDF URL from Europe PMC's fullTextUrlList."""
    for entry in result.get("fullTextUrlList", {}).get("fullTextUrl", []):
        if entry.get("documentStyle") == "pdf":
            url = entry.get("url", "")
            if not any(bad in url for bad in _UNRELIABLE_PDF_HOSTS):
                return url
    return None


def _to_epmc_query(keyword: str) -> str:
    """
    Convert a keyword to EuropePMC TITLE:/ABSTRACT: field syntax.

    Accepts:
      - Plain words:                    "SCAP SREBP2"
      - NCBI [Title/Abstract] notation: "SCAP[Title/Abstract] AND (SREBP[Title/Abstract])"
      - Multi-word NCBI phrases:        "macrocyclic peptide[Title/Abstract]"
      - Already EuropePMC syntax (contains ":"): passed through verbatim
    """
    if ":" in keyword:
        return keyword  # already EuropePMC syntax

    if "[" in keyword:
        # Split on [Title/Abstract] tags, preserving connectors (AND/OR/NOT/parens)
        # between them.  Each segment before a tag ends with the phrase; everything
        # before that phrase is the connector prefix.
        parts = re.split(r'\[Title/Abstract\]', keyword)
        result_parts = []
        for part in parts[:-1]:
            # Separate leading connector/parens from the trailing phrase
            m = re.match(r'^([\s()]*(?:AND|OR|NOT)?[\s()]*)(.*)', part, re.IGNORECASE)
            prefix = m.group(1) if m else ""
            phrase = (m.group(2) if m else part).strip()
            if not phrase:
                result_parts.append(prefix)
                continue
            if " " in phrase:
                epmc = f'(TITLE:"{phrase}" OR ABSTRACT:"{phrase}")'
            else:
                epmc = f"(TITLE:{phrase} OR ABSTRACT:{phrase})"
            result_parts.append(prefix + epmc)
        # Append any trailing connector/close-parens after the last tag
        if parts[-1].strip():
            result_parts.append(parts[-1])
        return "".join(result_parts)

    # Plain words: wrap each token
    return " AND ".join(f"(TITLE:{t} OR ABSTRACT:{t})" for t in keyword.split())


def _biorxiv_pdf_url(doi: str, result: dict) -> str | None:
    """Construct bioRxiv/medRxiv PDF URL from DOI."""
    if not doi or not doi.startswith("10.1101/"):
        return None
    version = result.get("versionNumber", 1)
    return f"https://www.biorxiv.org/content/{doi}v{version}.full.pdf"


# ---------------------------------------------------------------------------
# Europe PMC client  (preprints: bioRxiv / medRxiv)
# ---------------------------------------------------------------------------

def _epmc_result_to_paper(result: dict) -> Paper | None:
    """Convert a Europe PMC result dict to a Paper."""
    title = result.get("title", "").strip()
    if not title:
        return None

    doi   = result.get("doi") or None
    pmcid = result.get("pmcid") or None
    pmid  = str(result.get("pmid", "")) or None
    if pmid in (None, "None", ""):
        pmid = None

    authors_list = result.get("authorList", {}).get("author", [])
    authors = [
        a.get("fullName") or f"{a.get('lastName', '')} {a.get('firstName', '')}".strip()
        for a in authors_list
        if a.get("fullName") or a.get("lastName")
    ]

    src_str = result.get("source", "").upper()
    if src_str == "PPR" or (doi and doi.startswith("10.1101/")):
        journal_title = (
            result.get("bookOrReportDetails", {}).get("publisher", "")
            or result.get("journalTitle", "")
        )
        source = Source.medrxiv if "medrxiv" in journal_title.lower() else Source.biorxiv
    else:
        source = Source.pmc

    if source == Source.pmc and pmcid:
        pdf_url, xml_url = _pmc_aws_urls(pmcid)
    else:
        pdf_url = _europepmc_pdf_url(result)
        xml_url = None
        if not pdf_url and doi:
            pdf_url = _biorxiv_pdf_url(doi, result)

    year_raw = result.get("pubYear")
    year = int(year_raw) if year_raw else None

    return Paper(
        doi=doi, pmcid=pmcid, pmid=pmid, title=title, authors=authors,
        journal=result.get("journalTitle") or result.get("bookOrReportDetails", {}).get("publisher"),
        year=year, abstract=result.get("abstractText") or None,
        source=source, pdf_url=pdf_url, xml_url=xml_url,
    )


class EuropePMCClient:
    """Searches Europe PMC — used for preprints (SRC:PPR)."""

    def __init__(self, delay_s: float = 0.5):
        self.delay_s = delay_s
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "LiteratureSearchAgent/1.0"})

    def _fetch_page(self, query: str, cursor: str, page_size: int = 1000) -> dict:
        params = {
            "query": query, "resultType": "core",
            "pageSize": page_size, "cursorMark": cursor, "format": "json",
        }
        resp = self.session.get(EUROPEPMC_BASE, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search_keyword(self, keyword: str, max_results: int, sources: list[str]) -> list[Paper]:
        papers: list[Paper] = []
        seen_keys: set[str] = set()
        src_filter = " OR ".join(f"SRC:{s.upper()}" for s in sources)
        kw_query = _to_epmc_query(keyword)
        query = f"(HAS_FT:Y) AND ({kw_query}) AND ({src_filter})"
        logger.debug(f"[EuropePMC] Query: {query}")

        cursor = "*"
        while len(papers) < max_results:
            try:
                data = self._fetch_page(query, cursor, page_size=min(1000, max_results - len(papers)))
            except requests.RequestException as e:
                logger.warning(f"EuropePMC search failed: {e}")
                break
            results = data.get("resultList", {}).get("result", [])
            if not results:
                break
            for r in results:
                paper = _epmc_result_to_paper(r)
                if paper is None:
                    continue
                key = paper.doi or paper.pmcid or paper.title
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                papers.append(paper)
                if len(papers) >= max_results:
                    break
            next_cursor = data.get("nextCursorMark")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(self.delay_s)
        return papers

    def search(self, keywords: list[str], max_results: int, sources: list[str]) -> list[Paper]:
        all_papers: dict[str, Paper] = {}
        for kw in keywords:
            logger.info(f"[EuropePMC] Searching: '{kw}'")
            results = self.search_keyword(kw, max_results, sources)
            logger.info(f"  -> {len(results)} results")
            for p in results:
                key = p.doi or p.pmcid or p.title
                if key not in all_papers:
                    all_papers[key] = p
            time.sleep(self.delay_s)
        logger.info(f"[EuropePMC] {len(all_papers)} unique papers")
        return list(all_papers.values())


# ---------------------------------------------------------------------------
# NCBI eSearch + eSummary client  (PMC full-text articles)
# ---------------------------------------------------------------------------

def _ncbi_summary_to_paper(uid: str, summary: dict) -> Paper | None:
    """Convert an NCBI eSummary record to a Paper."""
    title = summary.get("title", "").strip()
    if not title:
        return None

    # Extract IDs from articleids list
    article_ids = summary.get("articleids", [])
    doi  = next((a["value"] for a in article_ids if a.get("idtype") == "doi"), None) or None
    pmid = next((a["value"] for a in article_ids if a.get("idtype") == "pubmed"), None) or None
    # PMCID from articleids, or construct from uid
    pmcid_raw = next((a["value"] for a in article_ids if a.get("idtype") == "pmc"), None)
    pmcid = pmcid_raw if pmcid_raw else f"PMC{uid}"

    authors = [a["name"] for a in summary.get("authors", []) if a.get("authtype") == "Author"]

    # Parse year from pubdate "2025 Oct" → 2025
    pubdate = summary.get("pubdate", "")
    year: int | None = None
    if pubdate:
        try:
            year = int(pubdate.split()[0])
        except (ValueError, IndexError):
            pass

    # Publication types list (e.g. ["Journal Article", "Review"])
    pub_types: list[str] = summary.get("pubtype", [])

    pdf_url, xml_url = _pmc_aws_urls(pmcid)

    return Paper(
        doi=doi, pmcid=pmcid, pmid=pmid, title=title,
        authors=authors,
        journal=summary.get("source") or None,
        year=year,
        abstract=None,  # eSummary doesn't include abstract; available in Sprint 2
        source=Source.pmc,
        pub_types=pub_types,
        pdf_url=pdf_url,
        xml_url=xml_url,
    )


class NCBIPMCClient:
    """
    Searches NCBI PMC via eSearch + eSummary.

    Finds open-access and author-manuscript articles that may not appear in
    Europe PMC's SRC:PMC index (e.g. articles indexed as SRC:MED in Europe PMC
    but still available in the PMC AWS S3 Open Data bucket).
    """

    BATCH_SIZE = 200  # eSummary IDs per request

    def __init__(
        self,
        email: str | None = None,
        api_key: str | None = None,
        delay_s: float = 0.34,   # ~3 req/s without key; 0.1 with key
    ):
        self.email   = email or os.environ.get("NCBI_EMAIL")
        self.api_key = api_key or os.environ.get("NCBI_API_KEY")
        self.delay_s = 0.1 if self.api_key else delay_s
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "LiteratureSearchAgent/1.0"})

    def _base_params(self) -> dict:
        p: dict = {}
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    def _esearch(self, keyword: str, max_results: int) -> list[str]:
        """Return list of PMC integer IDs (as strings) for the keyword."""
        ids: list[str] = []
        retstart = 0
        # Build a Title/Abstract-restricted query from the keyword.
        # Splitting on whitespace and tagging each token with [Title/Abstract]
        # avoids full-text matches where terms appear incidentally in methods or refs.
        # Multi-word quoted phrases (already containing "[") are passed through as-is.
        if "[" in keyword:
            # User has written an explicit field-tagged query — use verbatim
            kw_query = keyword
        else:
            tokens = keyword.split()
            kw_query = " AND ".join(f"{t}[Title/Abstract]" for t in tokens)
        term = f"({kw_query}) AND (open_access[filter] OR author_manuscript[filter])"
        logger.debug(f"[NCBI] eSearch term: {term}")

        while len(ids) < max_results:
            batch = min(self.BATCH_SIZE, max_results - len(ids))
            params = {
                **self._base_params(),
                "db": "pmc", "term": term, "retmode": "json",
                "retmax": batch, "retstart": retstart,
                "sort": "relevance",
            }
            try:
                resp = self.session.get(NCBI_ESEARCH, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json().get("esearchresult", {})
            except requests.RequestException as e:
                logger.warning(f"NCBI eSearch failed: {e}")
                break

            batch_ids = data.get("idlist", [])
            if not batch_ids:
                break
            ids.extend(batch_ids)
            total_available = int(data.get("count", 0))
            if len(ids) >= total_available:
                break
            retstart += len(batch_ids)
            time.sleep(self.delay_s)

        return ids

    def _esummary(self, ids: list[str]) -> list[Paper]:
        """Fetch eSummary metadata for a list of PMC integer IDs."""
        papers: list[Paper] = []
        for i in range(0, len(ids), self.BATCH_SIZE):
            batch = ids[i : i + self.BATCH_SIZE]
            params = {
                **self._base_params(),
                "db": "pmc", "id": ",".join(batch), "retmode": "json",
            }
            try:
                resp = self.session.get(NCBI_ESUMMARY, params=params, timeout=30)
                resp.raise_for_status()
                result = resp.json().get("result", {})
            except requests.RequestException as e:
                logger.warning(f"NCBI eSummary failed: {e}")
                continue

            for uid in batch:
                summary = result.get(uid)
                if not summary:
                    continue
                paper = _ncbi_summary_to_paper(uid, summary)
                if paper:
                    papers.append(paper)
            time.sleep(self.delay_s)
        return papers

    def search_keyword(self, keyword: str, max_results: int) -> list[Paper]:
        ids = self._esearch(keyword, max_results)
        logger.debug(f"[NCBI] eSearch returned {len(ids)} IDs")
        if not ids:
            return []
        return self._esummary(ids)

    def search(self, keywords: list[str], max_results: int) -> list[Paper]:
        all_papers: dict[str, Paper] = {}
        for kw in keywords:
            logger.info(f"[NCBI]     Searching: '{kw}'")
            results = self.search_keyword(kw, max_results)
            logger.info(f"  -> {len(results)} results")
            for p in results:
                key = p.doi or p.pmcid or p.title
                if key not in all_papers:
                    all_papers[key] = p
            time.sleep(self.delay_s)
        logger.info(f"[NCBI] {len(all_papers)} unique PMC papers")
        return list(all_papers.values())


# ---------------------------------------------------------------------------
# Semantic Scholar bulk search client
# ---------------------------------------------------------------------------

def _to_s2_query(keyword: str) -> str:
    """
    Convert an NCBI-style keyword (with [Title/Abstract] tags) to
    Semantic Scholar bulk-search query syntax.

    S2 uses: space = AND, | = OR, - = NOT, "phrase" = exact phrase.
    """
    # Strip field tags
    q = re.sub(r"\[Title/Abstract\]", "", keyword, flags=re.IGNORECASE)
    # OR → |  (must come before AND removal)
    q = re.sub(r"\bOR\b", "|", q, flags=re.IGNORECASE)
    # AND → space (all terms required by default)
    q = re.sub(r"\bAND\b", " ", q, flags=re.IGNORECASE)
    # NOT → -
    q = re.sub(r"\bNOT\b", "-", q, flags=re.IGNORECASE)
    # Tidy whitespace around |
    q = re.sub(r"\s*\|\s*", " | ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


class SemanticScholarClient:
    """
    Searches Semantic Scholar via the bulk-search endpoint.

    Discovers papers that have open-access PDFs outside the PMC Open Access
    subset, and provides citation counts to improve ranking for all papers.

    Set S2_API_KEY in the environment for higher rate limits (10 req/s vs
    ~1 req/s unauthenticated).
    """

    def __init__(self, api_key: str | None = None, delay_s: float = 1.1):
        self.api_key = api_key or os.getenv("S2_API_KEY")
        # With a key the burst limit is much higher; stay conservative
        self.delay_s = 0.2 if self.api_key else delay_s
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "LiteratureSearchAgent/1.0"})
        if self.api_key:
            self.session.headers["x-api-key"] = self.api_key

    def _fetch_bulk(self, query: str, token: str | None, limit: int) -> dict:
        params: dict = {
            "query": query,
            "fields": S2_FIELDS,
            "limit": min(limit, 1000),
            "publicationTypes": "JournalArticle,Review",
            "sort": "citationCount:desc",
        }
        if token:
            params["token"] = token
        resp = self.session.get(S2_BULK_SEARCH, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _to_paper(self, record: dict) -> Paper | None:
        title = (record.get("title") or "").strip()
        if not title:
            return None

        ext   = record.get("externalIds") or {}
        doi   = ext.get("DOI") or None
        pmcid_raw = ext.get("PubMedCentral") or None
        pmcid = (f"PMC{pmcid_raw}" if pmcid_raw and not str(pmcid_raw).startswith("PMC")
                 else pmcid_raw) or None
        pmid  = str(ext.get("PubMed") or "") or None

        authors = [
            a.get("name", "") for a in (record.get("authors") or [])
            if a.get("name")
        ]
        journal_info = record.get("journal") or {}
        journal  = journal_info.get("name") or None
        year     = record.get("year") or None
        abstract = record.get("abstract") or None
        pub_types = record.get("publicationTypes") or []
        citation_count = record.get("citationCount")
        s2_id = record.get("paperId")

        # Determine source and PDF URL
        oa_pdf    = record.get("openAccessPdf") or {}
        oa_status = (oa_pdf.get("status") or "").upper()
        green_url = oa_pdf.get("url") if oa_status == "GREEN" else None
        is_oa     = bool(record.get("isOpenAccess"))

        if pmcid:
            # Only include PMC papers that are actually open-access — non-OA
            # PMC IDs exist but their files aren't in the S3 OA bucket (→ 404).
            if not is_oa:
                return None
            source   = Source.pmc
            pdf_url, xml_url = _pmc_aws_urls(pmcid)
            # Store a GREEN repository URL as fallback: downloader tries it
            # if the AWS PDF/XML both 404 (paper in PMC but not yet on S3).
            # Exclude known-unreliable hosts that return HTML instead of PDFs.
            if green_url and not green_url.startswith("https://pmc-oa-opendata") \
                    and not any(bad in green_url for bad in _UNRELIABLE_PDF_HOSTS):
                pdf_url = green_url
                xml_url = None
        else:
            # No PMC ID — only usable if there's a GREEN (repository) PDF.
            # GOLD/HYBRID/BRONZE are publisher-hosted and return 403.
            if not green_url:
                return None
            source   = Source.semantic_scholar
            pdf_url  = green_url
            xml_url  = None

        return Paper(
            doi=doi, pmcid=pmcid, pmid=pmid, title=title,
            authors=authors, journal=journal, year=year, abstract=abstract,
            source=source, pub_types=pub_types,
            pdf_url=pdf_url, xml_url=xml_url,
            citation_count=citation_count,
            s2_paper_id=s2_id,
        )

    def search_keyword(self, keyword: str, max_results: int) -> list[Paper]:
        query = _to_s2_query(keyword)
        logger.debug(f"[S2] Query: {query}")
        papers: list[Paper] = []
        seen_keys: set[str] = set()
        token: str | None = None

        while len(papers) < max_results:
            try:
                data = self._fetch_bulk(query, token, max_results - len(papers))
            except requests.RequestException as e:
                logger.warning(f"S2 search failed for '{keyword}': {e}")
                break

            for record in data.get("data") or []:
                paper = self._to_paper(record)
                if paper is None:
                    continue
                key = paper.doi or paper.pmcid or paper.title
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                papers.append(paper)
                if len(papers) >= max_results:
                    break

            token = data.get("token")
            if not token:
                break
            time.sleep(self.delay_s)

        return papers

    def search(self, keywords: list[str], max_results: int) -> list[Paper]:
        all_papers: dict[str, Paper] = {}
        for kw in keywords:
            logger.info(f"[S2]       Searching: '{kw}'")
            results = self.search_keyword(kw, max_results)
            logger.info(f"  -> {len(results)} results")
            for p in results:
                key = p.doi or p.pmcid or p.title
                if key not in all_papers:
                    all_papers[key] = p
            time.sleep(self.delay_s)
        logger.info(f"[S2] {len(all_papers)} unique papers")
        return list(all_papers.values())
