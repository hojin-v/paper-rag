"""
수집 오케스트레이션 서비스 — 논문 후보를 실제 로컬 PDF 파일로 검증·저장한다.

`OpenAlexClient`(발견)가 찾아준 `PaperCandidate` 목록을 받아, 라이선스를 다시 한번 확인하고
(방어적 이중 검사), PDF를 스트리밍 다운로드하며 크기 제한·PDF 시그니처를 검증한 뒤 SHA-256과
함께 `collection-manifest.jsonl`에 기록한다. 이미 같은 `source_id`로 동일한 체크섬 파일이 있으면
재다운로드하지 않는다(idempotent 재실행). 실패한 논문 하나가 전체 배치를 막지 않도록, 개별 실패는
`CollectionReport.failures`에 모아 마지막에 보고한다.
"""

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
    """발견된 논문 후보들을 실제로 다운로드·검증·manifest 기록까지 수행하는 오케스트레이터."""

    def __init__(
        self,
        discovery: OpenAlexClient,
        settings: Settings | None = None,
        download_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.discovery = discovery
        # discovery(OpenAlexClient)와 별개로 다운로드 전용 httpx.Client를 둔다 — 검색 API 호출과
        # 대용량 PDF 스트리밍 다운로드는 커넥션 특성이 다르기 때문(스트리밍은 응답을 통째로 메모리에
        # 올리지 않고 청크 단위로 받는다).
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
        *,
        language: str | None = None,
    ) -> CollectionReport:
        """검색어로 후보를 찾아 바로 다운로드까지 수행하는 편의 메서드(발견+다운로드 한 번에)."""
        return self.collect_candidates(
            self.discovery.search(query, limit, language=language),
            output_dir=output_dir,
        )

    def collect_ids(
        self,
        work_ids: list[str],
        output_dir: Path | None = None,
    ) -> CollectionReport:
        """고정 work ID 목록으로 후보를 찾아 바로 다운로드까지 수행하는 편의 메서드."""
        return self.collect_candidates(
            self.discovery.get_works(work_ids),
            output_dir=output_dir,
        )

    def collect_candidates(
        self,
        candidates: list[PaperCandidate],
        output_dir: Path | None = None,
    ) -> CollectionReport:
        """이미 발견된 후보 목록을 순회하며 다운로드하고, 성공/스킵/실패를 집계해 반환한다.

        후보 하나의 다운로드가 예외를 던져도(`PaperDownloadError` 외의 예상 못 한 예외 포함)
        전체를 중단하지 않고 실패로 기록한 뒤 나머지 후보를 계속 처리한다 — 배치 하나의 실패가
        전체 배치를 막지 않도록 하기 위함.
        """
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
        """후보 1편을 검증 후 다운로드(또는 기존 파일 재사용)한다.

        라이선스 확인은 `OpenAlexClient`에서 이미 한 번 필터링됐지만, 이 서비스가 API 응답을
        신뢰하지 않고 저장 직전에 다시 검증하는 방어적 이중 체크다(설정이 검색 이후 바뀌거나
        호출부가 필터를 거치지 않은 후보를 넘길 가능성에 대비).
        """
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
        # 같은 source_id로 이미 받은 기록이 있고, 그 파일이 실제로 존재하며 체크섬까지 일치하면
        # 재다운로드하지 않는다(재실행 시 네트워크 비용 절약 + idempotent 동작 보장).
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
            # 파일이 없거나 손상(체크섬 불일치)됐으면 재다운로드 대상이므로 낡은 파일을 정리한다.
            existing_path.unlink(missing_ok=True)

        filename = f"{candidate.source_id}-{_slug(candidate.title)}.pdf"
        path = destination / filename
        # 다운로드 도중 실패해도 최종 파일명이 반쪽짜리 PDF로 남지 않도록 임시 파일에 먼저 쓴다.
        temporary_path = path.with_suffix(".pdf.part")
        maximum_bytes = self.settings.paper_download_max_mb * 1024 * 1024
        digest = hashlib.sha256()
        byte_size = 0
        temporary_path.unlink(missing_ok=True)
        try:
            with self.download_client.stream("GET", candidate.pdf_url) as response:
                response.raise_for_status()
                # Content-Length 헤더로 미리 크기를 알 수 있으면 다운로드 시작 전에 조기 실패시킨다.
                declared_size = _content_length(response.headers.get("Content-Length"))
                if declared_size is not None and declared_size > maximum_bytes:
                    raise PaperDownloadError(
                        f"PDF가 허용 크기 {self.settings.paper_download_max_mb}MB를 초과합니다."
                    )
                with temporary_path.open("wb") as output:
                    for chunk in response.iter_bytes():
                        byte_size += len(chunk)
                        # 헤더가 없거나 거짓일 수 있으므로, 실제 받은 바이트 수도 매 청크마다 다시
                        # 검사해 스트리밍 도중에도 크기 초과를 확실히 막는다.
                        if byte_size > maximum_bytes:
                            raise PaperDownloadError(
                                f"PDF가 허용 크기 {self.settings.paper_download_max_mb}MB를 초과합니다."
                            )
                        digest.update(chunk)
                        output.write(chunk)
            if byte_size == 0 or not _is_pdf(temporary_path):
                # HTML 오류 페이지나 리다이렉트 안내문이 200 OK로 오는 경우를 PDF로 오인하지 않도록
                # 매직 바이트(%PDF-)를 확인한다(docs/guide/11-paper-collection.md 3단계 참고).
                raise PaperDownloadError("응답이 PDF 시그니처로 시작하지 않습니다.")
            temporary_path.replace(path)
        except (httpx.HTTPError, OSError) as exc:
            raise PaperDownloadError(f"PDF 다운로드 실패: {exc}") from exc
        finally:
            # 정상 완료 시에는 이미 rename됐으므로 존재하지 않고, 실패 시에는 남아있는 임시 파일을
            # 정리한다(missing_ok=True로 어느 경우든 안전).
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
    """`source_id`를 키로 하는 JSONL manifest 파일을 메모리에 올려 읽고 갱신하는 저장소.

    파일 형식은 한 줄에 레코드 하나(JSON)이며, `upsert` 시 전체를 `source_id` 정렬 순서로
    다시 써서 diff가 보기 쉽고 결정적인 파일 내용을 유지한다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records = self._load()

    def get(self, source_id: str) -> dict[str, Any] | None:
        return self.records.get(source_id)

    def upsert(self, record: dict[str, Any]) -> None:
        """레코드를 갱신하고 manifest 파일 전체를 원자적으로 다시 쓴다.

        임시 파일(`.part`)에 먼저 쓴 뒤 `replace()`로 교체해, 쓰는 도중 프로세스가 죽어도 기존
        manifest가 반쪽짜리 내용으로 남지 않게 한다(`collect/smoke.py`의 `_write_manifest`와
        동일한 원자적 쓰기 패턴).
        """
        source_id = str(record["source_id"])
        self.records[source_id] = record
        temporary_path = self.path.with_suffix(self.path.suffix + ".part")
        with temporary_path.open("w", encoding="utf-8") as output:
            for key in sorted(self.records):
                output.write(json.dumps(self.records[key], ensure_ascii=False) + "\n")
        temporary_path.replace(self.path)

    def _load(self) -> dict[str, dict[str, Any]]:
        """manifest 파일이 없으면 빈 상태로 시작하고(최초 수집), 있으면 줄 단위로 파싱한다.

        빈 줄은 건너뛰지만, 내용이 있는 줄이 JSON으로 파싱되지 않거나 `source_id`가 없으면
        manifest 자체가 손상된 것이므로 예외로 알린다 — 조용히 무시하면 중복 다운로드 판단이
        틀어질 수 있다.
        """
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
    """논문 제목을 파일명에 안전하게 쓸 수 있는 짧은 슬러그로 변환한다.

    영숫자가 아닌 문자는 모두 하이픈으로 치환하고, 결과가 비면("paper") 기본값을 쓰며, 파일
    시스템의 파일명 길이 제한을 고려해 80자로 자른다.
    """
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (normalized or "paper")[:80]


def _sha256(path: Path) -> str:
    """파일 전체를 청크 단위(1MB)로 읽어 SHA-256을 계산한다(큰 PDF도 메모리에 한 번에 올리지 않음)."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_pdf(path: Path) -> bool:
    """파일 앞 5바이트가 PDF 매직 넘버(`%PDF-`)인지 확인해, 실제 PDF가 아닌 응답(HTML 오류 페이지
    등)을 논문 파일로 잘못 저장하지 않도록 한다."""
    with path.open("rb") as file:
        return file.read(5) == b"%PDF-"


def _content_length(value: str | None) -> int | None:
    """`Content-Length` 헤더 문자열을 정수로 변환한다. 헤더가 없거나 형식이 이상하면 None을 반환해
    "크기를 미리 알 수 없음"으로 처리하고 스트리밍 중 실측치로만 크기 제한을 검사하게 한다."""
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
