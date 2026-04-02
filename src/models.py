from enum import Enum
from typing import Optional
from pydantic import BaseModel


class Source(str, Enum):
    pmc = "pmc"
    biorxiv = "biorxiv"
    medrxiv = "medrxiv"
    semantic_scholar = "semantic_scholar"


class DownloadStatus(str, Enum):
    pending = "pending"
    downloaded = "downloaded"
    failed = "failed"


class CurationStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class Paper(BaseModel):
    doi: Optional[str] = None
    pmcid: Optional[str] = None
    pmid: Optional[str] = None
    title: str
    authors: list[str] = []
    journal: Optional[str] = None
    year: Optional[int] = None
    abstract: Optional[str] = None
    source: Optional[Source] = None
    pub_types: list[str] = []          # e.g. ["Journal Article", "Review"]
    pdf_url: Optional[str] = None
    xml_url: Optional[str] = None
    pdf_path: Optional[str] = None
    download_status: DownloadStatus = DownloadStatus.pending
    priority_score: float = 0.5        # 0.0 = exclude (conference abstract), 1.0 = top journal
    citation_count: Optional[int] = None   # from Semantic Scholar; used as ranking signal
    s2_paper_id: Optional[str] = None      # Semantic Scholar paperId for deduplication
    curated: bool = False
    curation_status: CurationStatus = CurationStatus.pending
    curated_at: Optional[str] = None
    fingerprint_path: Optional[str] = None
    curation_model: Optional[str] = None
    curation_tokens: Optional[int] = None
    curation_error: Optional[str] = None

    def unique_key(self) -> str:
        """Return best available unique identifier."""
        return self.doi or self.pmcid or self.pmid or self.title
