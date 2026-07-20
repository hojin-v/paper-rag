"""
논문 수집 파이프라인이 주고받는 데이터 모델.

`openalex.py`(발견/검색) → `service.py`(다운로드) → CLI 출력 순서로 흘러가는 세 단계 데이터를
표현한다: 아직 다운로드하지 않은 후보(`PaperCandidate`), 실제로 내려받은 결과(`DownloadedPaper`),
여러 후보를 처리한 뒤의 집계 결과(`CollectionReport`). 모든 값 객체는 불변(`frozen=True`)으로
만들어, 다운로드 중 후보 메타데이터가 실수로 바뀌는 것을 막는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class PaperCandidate:
    """OpenAlex 검색/조회로 찾은, 아직 다운로드하지 않은 논문 후보 1편의 메타데이터.

    `license`는 이미 `openalex.py`에서 허용 라이선스(cc-by/cc-by-sa/cc0) 필터를 통과한 값만
    들어온다 — 이 시점에는 "다운로드해도 되는 논문"이라는 뜻이다.
    """

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
    """다운로드(또는 기존 파일 재사용으로 스킵) 처리가 끝난 논문 1편의 결과.

    `status`가 "skipped"인 경우는 실패가 아니라, 이미 같은 `source_id`로 동일한 SHA-256 파일이
    로컬에 있어 재다운로드를 생략했다는 뜻이다(`service.py`의 중복 방지 로직).
    """

    candidate: PaperCandidate
    local_path: str
    sha256: str
    byte_size: int
    retrieved_at: str
    status: Literal["downloaded", "skipped"]

    def manifest_record(self) -> dict[str, Any]:
        """`collection-manifest.jsonl`에 한 줄로 기록할 dict 표현을 만든다.

        후보 메타데이터(`candidate`의 모든 필드)에 다운로드 결과(로컬 경로·체크섬·크기·시각)를
        더해, 나중에 어떤 출처에서 어떤 라이선스로 언제 받았는지(provenance)를 재구성할 수 있게
        한다(docs/guide/11-paper-collection.md 5단계 "보존하는 출처 정보" 참고).
        """
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
    """한 번의 수집 실행(검색 또는 work-id 지정) 결과를 모은 집계 객체.

    `failures`는 개별 논문 다운로드가 실패해도 나머지 논문 처리를 막지 않기 위해, 예외를 즉시
    올리지 않고 `(source_id, 에러 메시지)` 튜플로 모아두는 용도다(`service.py`의
    `collect_candidates` 참고).
    """

    downloaded: list[DownloadedPaper] = field(default_factory=list)
    skipped: list[DownloadedPaper] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        """성공적으로 처리된(새로 받았거나 이미 있어 스킵한) 논문 수."""
        return len(self.downloaded) + len(self.skipped)
