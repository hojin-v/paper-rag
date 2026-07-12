from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class PaperCandidate:
    source_provider: Literal["openalex"]
    source_id: str
    title: str
    authors: tuple[str, ...]
    publication_year: int | None
    doi: str | None
    landing_page_url: str
    pdf_url: str
    license: str
    language: str | None
    source_name: str | None


@dataclass(frozen=True)
class DownloadedPaper:
    candidate: PaperCandidate
    local_path: str
    sha256: str
    byte_size: int
    retrieved_at: str
    status: Literal["downloaded", "skipped"]

    def manifest_record(self) -> dict[str, Any]:
        record = asdict(self.candidate)
        record.update(
            {
                "local_path": self.local_path,
                "sha256": self.sha256,
                "byte_size": self.byte_size,
                "retrieved_at": self.retrieved_at,
            }
        )
        return record


@dataclass
class CollectionReport:
    downloaded: list[DownloadedPaper] = field(default_factory=list)
    skipped: list[DownloadedPaper] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return len(self.downloaded) + len(self.skipped)
