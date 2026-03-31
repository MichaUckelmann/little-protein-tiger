import time
import hashlib
from pathlib import Path

import requests
from loguru import logger
from tqdm import tqdm

from .models import Paper, DownloadStatus
from .database import Database


PDF_MAGIC = b"%PDF"
XML_SIGNATURES = (b"<?xml", b"<article", b"<pmc-articleset", b"<!DOCTYPE")


def _safe_stem(paper: Paper) -> str:
    """Generate a safe base filename stem (without extension) for a paper."""
    key = paper.doi or paper.pmcid or paper.pmid or paper.title
    # Exclude dots: Path.with_suffix() treats everything after the last dot as the
    # extension, so dots in the stem would cause the filename to be truncated.
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    slug = slug[:120]
    h = hashlib.md5(key.encode()).hexdigest()[:8]
    return f"{slug}_{h}"


def _detect_format(path: Path) -> str | None:
    """Return 'pdf', 'xml', or None if the file is not a recognisable format."""
    with open(path, "rb") as f:
        header = f.read(32)
    if header[:4] == PDF_MAGIC:
        return "pdf"
    if any(header.startswith(sig) for sig in XML_SIGNATURES):
        return "xml"
    # XML files sometimes have a BOM or whitespace before the declaration
    stripped = header.lstrip()
    if any(stripped.startswith(sig) for sig in XML_SIGNATURES):
        return "xml"
    return None


# Sentinel returned when the server gives a definitive 404 — caller should not retry.
NOT_FOUND: Path = Path("__NOT_FOUND__")


def _try_download(url: str, dest_stem: Path, session: requests.Session, timeout: int = 90) -> Path | None:
    """
    Stream-download url, validate format, save with correct extension.
    Returns:
      Path  — success (the saved file)
      NOT_FOUND — server returned 404; no point retrying this URL
      None  — transient failure; caller may retry
    dest_stem should be a path without extension (e.g. data/pdfs/PMC123_abc123).
    """
    try:
        resp = session.get(url, stream=True, timeout=timeout, allow_redirects=True)
        if resp.status_code == 404:
            logger.debug(f"404: {url}")
            return NOT_FOUND
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type.lower():
            logger.debug(f"HTML content-type at {url}")
            return None

        dest_stem.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_stem.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        fmt = _detect_format(tmp)
        if fmt is None:
            with open(tmp, "rb") as f:
                snippet = f.read(32)
            logger.debug(f"Unrecognised format (header {snippet!r}) from {url}")
            tmp.unlink(missing_ok=True)
            return None

        final = dest_stem.with_suffix(f".{fmt}")
        tmp.replace(final)  # replace() is atomic and works on Windows even if dest exists
        return final

    except requests.RequestException as e:
        logger.debug(f"Download error for {url}: {e}")
        dest_stem.with_suffix(".tmp").unlink(missing_ok=True)
        return None


def _aws_version_urls(pmcid: str) -> list[tuple[str, str]]:
    """
    Yield (pdf_url, xml_url) pairs for versions 1 and 2 of a PMC article
    on the AWS S3 Open Data bucket.  Version 1 covers ~99% of articles.
    """
    base = "https://pmc-oa-opendata.s3.amazonaws.com"
    return [
        (f"{base}/{pmcid}.{v}/{pmcid}.{v}.pdf", f"{base}/{pmcid}.{v}/{pmcid}.{v}.xml")
        for v in (1, 2)
    ]


def download_papers(
    papers: list[Paper],
    db: Database,
    pdf_dir: Path,
    delay_s: float = 1.0,
    max_retries: int = 3,
    dry_run: bool = False,
):
    """
    Download content for all pending papers, updating DB status.

    Priority order per paper:
      1. AWS S3 PDF (for PMC papers with pmcid, tries v1 then v2)
      2. pdf_url from search metadata (preprints, Europe PMC fulltextRepo)
      3. AWS S3 XML fallback (PMC papers where PDF is unavailable on AWS)
      4. xml_url from search metadata
    """
    actionable = [
        p for p in papers
        if p.download_status != DownloadStatus.downloaded
        and (p.pdf_url or p.xml_url or p.pmcid)
    ]
    skipped = len(papers) - len(actionable) - sum(
        1 for p in papers if p.download_status == DownloadStatus.downloaded
    )
    if skipped > 0:
        logger.warning(f"{skipped} papers have no URL and no PMCID — skipping")

    if dry_run:
        pdf_count = sum(1 for p in actionable if p.pdf_url)
        xml_only = sum(1 for p in actionable if not p.pdf_url and p.xml_url)
        logger.info(f"[DRY RUN] Would attempt {len(actionable)} papers "
                    f"({pdf_count} with PDF URL, {xml_only} XML-only)")
        return

    logger.info(f"Downloading {len(actionable)} papers to {pdf_dir}")
    session = requests.Session()
    session.headers.update({"User-Agent": "LiteratureSearchAgent/1.0"})

    for paper in tqdm(actionable, desc="Downloading"):
        stem = _safe_stem(paper)
        dest_stem = pdf_dir / stem

        # Skip if a completed file (.pdf or .xml) already exists on disk.
        # Filter explicitly — glob may return stale .tmp files first.
        existing = next(
            (f for f in pdf_dir.glob(f"{stem}.*") if f.suffix in (".pdf", ".xml")),
            None,
        )
        if existing:
            logger.debug(f"File already exists, marking downloaded: {existing.name}")
            db.mark_downloaded(paper.doi, paper.pmcid, str(existing))
            continue

        # Clean up any stale .tmp left by a previous interrupted run
        stale_tmp = dest_stem.with_suffix(".tmp")
        stale_tmp.unlink(missing_ok=True)

        final_path: Path | None = None

        # --- Build ordered list of (hint, url) to try ---
        urls_to_try: list[tuple[str, str]] = []

        # 1. AWS PDF v1, v2 for PMC papers
        aws_xml_urls: list[str] = []
        if paper.pmcid:
            for aws_pdf, aws_xml in _aws_version_urls(paper.pmcid):
                urls_to_try.append(("pdf", aws_pdf))
                aws_xml_urls.append(aws_xml)

        # 2. pdf_url from search (preprints / Europe PMC fulltextRepo)
        if paper.pdf_url and not paper.pdf_url.startswith("https://pmc-oa-opendata"):
            urls_to_try.append(("pdf", paper.pdf_url))

        # 3. AWS XML fallback (v1, v2)
        for xml_url in aws_xml_urls:
            urls_to_try.append(("xml", xml_url))

        # 4. xml_url from search metadata
        if paper.xml_url and not paper.xml_url.startswith("https://pmc-oa-opendata"):
            urls_to_try.append(("xml", paper.xml_url))

        # --- Attempt downloads ---
        for hint, url in urls_to_try:
            for attempt in range(1, max_retries + 1):
                logger.debug(f"Attempt {attempt}/{max_retries} [{hint}]: {url}")
                result = _try_download(url, dest_stem, session)
                if result is NOT_FOUND:
                    break  # 404 — file doesn't exist, skip immediately to next URL
                if result is not None:
                    final_path = result
                    break
                if attempt < max_retries:
                    wait = delay_s * (2 ** (attempt - 1))
                    logger.debug(f"Retrying in {wait:.1f}s")
                    time.sleep(wait)
            if final_path is not None:
                break

        if final_path is not None:
            fmt = "XML" if final_path.suffix == ".xml" else "PDF"
            logger.debug(f"Downloaded [{fmt}]: {final_path.name}")
            db.mark_downloaded(paper.doi, paper.pmcid, str(final_path))
        else:
            db.mark_failed(paper.doi, paper.pmcid, "All URL attempts failed")
            logger.warning(f"Failed: {paper.title[:70]}")

        time.sleep(delay_s)
