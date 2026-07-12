from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from paperrag.collect.models import CollectionReport, DownloadedPaper, PaperCandidate
from paperrag.collect.openalex import OpenAlexClient
from paperrag.config import Settings, get_settings


class PaperDownloadError(RuntimeError):
    """원문 PDF를 검증된 로컬 파일로 저장하지 못함."""


class PaperCollector:
    def __init__(
        self,
        discovery: OpenAlexClient,
        settings: Settings | None = None,
        download_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.discovery = discovery
        self._owns_download_client = download_client is None
        self.download_client = download_client or httpx.Client(
            timeout=self.settings.paper_collection_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "paper-rag/0.1"},
        )

    def close(self) -> None:
        self.discovery.close()
        if self._owns_download_client:
            self.download_client.close()

    def collect_query(
        self,
        query: str,
        limit: int,
        output_dir: Path | None = None,
    ) -> CollectionReport:
        return self.collect_candidates(
            self.discovery.search(query, limit),
            output_dir=output_dir,
        )

    def collect_ids(
        self,
        work_ids: list[str],
        output_dir: Path | None = None,
    ) -> CollectionReport:
        return self.collect_candidates(
            self.discovery.get_works(work_ids),
            output_dir=output_dir,
        )

    def collect_candidates(
        self,
        candidates: list[PaperCandidate],
        output_dir: Path | None = None,
    ) -> CollectionReport:
        destination = output_dir or self.settings.paper_collection_dir
        destination.mkdir(parents=True, exist_ok=True)
        manifest = ManifestStore(
            destination / self.settings.paper_collection_manifest_name
        )
        report = CollectionReport()
        for candidate in candidates:
            try:
                result = self._download(candidate, destination, manifest)
            except Exception as exc:
                report.failures.append((candidate.source_id, str(exc)))
                continue
            if result.status == "downloaded":
                report.downloaded.append(result)
            else:
                report.skipped.append(result)
        return report

    def _download(
        self,
        candidate: PaperCandidate,
        destination: Path,
        manifest: ManifestStore,
    ) -> DownloadedPaper:
        allowed_licenses = {
            item.strip().lower()
            for item in self.settings.paper_collection_allowed_licenses.split(",")
            if item.strip()
        }
        if candidate.license.lower() not in allowed_licenses:
            raise PaperDownloadError(
                f"허용되지 않은 라이선스입니다: {candidate.license or 'unknown'}"
            )
        if not candidate.pdf_url.startswith("https://"):
            raise PaperDownloadError("PDF URL은 HTTPS여야 합니다.")
        existing = manifest.get(candidate.source_id)
        if existing is not None:
            existing_path = Path(str(existing.get("local_path") or ""))
            expected_sha = str(existing.get("sha256") or "")
            if existing_path.is_file() and _sha256(existing_path) == expected_sha:
                return DownloadedPaper(
                    candidate=candidate,
                    local_path=str(existing_path),
                    sha256=expected_sha,
                    byte_size=existing_path.stat().st_size,
                    retrieved_at=str(existing.get("retrieved_at") or ""),
                    status="skipped",
                )
            existing_path.unlink(missing_ok=True)

        filename = f"{candidate.source_id}-{_slug(candidate.title)}.pdf"
        path = destination / filename
        temporary_path = path.with_suffix(".pdf.part")
        maximum_bytes = self.settings.paper_download_max_mb * 1024 * 1024
        digest = hashlib.sha256()
        byte_size = 0
        temporary_path.unlink(missing_ok=True)
        try:
            with self.download_client.stream("GET", candidate.pdf_url) as response:
                response.raise_for_status()
                declared_size = _content_length(response.headers.get("Content-Length"))
                if declared_size is not None and declared_size > maximum_bytes:
                    raise PaperDownloadError(
                        f"PDF가 허용 크기 {self.settings.paper_download_max_mb}MB를 초과합니다."
                    )
                with temporary_path.open("wb") as output:
                    for chunk in response.iter_bytes():
                        byte_size += len(chunk)
                        if byte_size > maximum_bytes:
                            raise PaperDownloadError(
                                f"PDF가 허용 크기 {self.settings.paper_download_max_mb}MB를 초과합니다."
                            )
                        digest.update(chunk)
                        output.write(chunk)
            if byte_size == 0 or not _is_pdf(temporary_path):
                raise PaperDownloadError("응답이 PDF 시그니처로 시작하지 않습니다.")
            temporary_path.replace(path)
        except (httpx.HTTPError, OSError) as exc:
            raise PaperDownloadError(f"PDF 다운로드 실패: {exc}") from exc
        finally:
            temporary_path.unlink(missing_ok=True)

        result = DownloadedPaper(
            candidate=candidate,
            local_path=str(path),
            sha256=digest.hexdigest(),
            byte_size=byte_size,
            retrieved_at=datetime.now(UTC).isoformat(),
            status="downloaded",
        )
        manifest.upsert(result.manifest_record())
        return result


class ManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.records = self._load()

    def get(self, source_id: str) -> dict[str, Any] | None:
        return self.records.get(source_id)

    def upsert(self, record: dict[str, Any]) -> None:
        source_id = str(record["source_id"])
        self.records[source_id] = record
        temporary_path = self.path.with_suffix(self.path.suffix + ".part")
        with temporary_path.open("w", encoding="utf-8") as output:
            for key in sorted(self.records):
                output.write(json.dumps(self.records[key], ensure_ascii=False) + "\n")
        temporary_path.replace(self.path)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        records: dict[str, dict[str, Any]] = {}
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                source_id = str(record["source_id"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise PaperDownloadError(
                    f"manifest {line_number}행이 올바르지 않습니다: {exc}"
                ) from exc
            records[source_id] = record
        return records


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (normalized or "paper")[:80]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_pdf(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(5) == b"%PDF-"


def _content_length(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
