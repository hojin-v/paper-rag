"""검수 문서(`ReviewDocument`)의 구조화 메타데이터 저장소.

이전에는 `review_dir` 아래 `<document_id>/review.json` 파일 하나에 문서 전체 상태를 그대로
저장했다(FileReviewStore). 하지만 파일 읽기-수정-쓰기 사이에는 락이 없어, API를 여러 replica로
동시에 띄우면 같은 문서를 두 요청이 동시에 갱신할 때 경쟁 조건(lost update)이 생길 수 있었다.
`PostgresReviewStore`는 이 구조화 메타데이터를 `review_documents` 테이블 한 행으로 옮겨,
저장을 트랜잭션 UPDATE(SQLAlchemy Engine)로 처리한다.

원본 PDF·페이지 PNG 같은 바이너리 자산은 이 이전 대상이 아니다 — `document_dir`가 관리하는
로컬 디렉터리에 생성 시 한 번만 쓰이고 이후 수정되지 않으므로, 파일 기반 저장의 경쟁 조건
위험이 애초에 없다. 따라서 `document_dir`/`create_dir`/`source_path`/`page_image_path`는
`_ReviewFileAssets`에 공통으로 남겨두고, `save`/`get`/`list`(구조화 메타데이터)만 저장소
구현마다 달라진다 — `ingest/repository.py`·`search/repository.py`의 Postgres/InMemory
이중 구현 패턴과 동일하다.

여전히 남은 한계(문서 삭제 API 부재)는 이전과 같다: `document_dir`가 한 번 생성되면 검수·적재가
끝나도 디스크에 계속 남으며, 정리하려면 파일시스템을 직접 조작해야 한다.
"""

import re
from pathlib import Path
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from paperrag.config import Settings
from paperrag.db import get_engine
from paperrag.review.models import ReviewDocument

# 업로드 시 uuid4().hex로 생성되는 32자리 소문자 16진수 document_id 형식.
# 경로 조작(path traversal) 공격을 막기 위해 document_dir 등 모든 경로 조합 전에
# 이 정규식으로 형식을 검증한다.
DOCUMENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class DocumentNotFoundError(KeyError):
    """존재하지 않는 document_id, 형식이 잘못된 document_id, 또는 그 하위 페이지/이미지를 찾을 때 발생."""

    pass


class _ReviewFileAssets:
    """검수 문서의 바이너리 자산(원본 PDF, 페이지 PNG)을 위한 `root/<document_id>/` 디렉터리 관리.

    구조화 메타데이터 저장소(Postgres/InMemory) 구현과 무관하게 공통으로 쓰인다 — 이
    파일들은 생성 시 한 번만 쓰이고 이후 수정되지 않아, 메타데이터와 달리 DB로 옮길
    이유(경쟁 조건 방지)가 없다.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def document_dir(self, document_id: str) -> Path:
        """document_id에 대응하는 디렉터리 경로를 반환한다.

        디렉터리 존재 여부는 확인하지 않고 형식만 검증한다. 실제 존재 여부 확인이 필요한
        호출부(get/source_path/page_image_path)는 별도로 `is_file()` 등을 확인한다.
        """
        if not DOCUMENT_ID_RE.fullmatch(document_id):
            raise DocumentNotFoundError(document_id)
        return self.root / document_id

    def create_dir(self, document_id: str) -> Path:
        """새 문서용 디렉터리를 생성한다. 이미 존재하면 예외를 던져(exist_ok=False) ID 충돌을 감지한다."""
        path = self.document_dir(document_id)
        path.mkdir(parents=True, exist_ok=False)
        return path

    def source_path(self, document_id: str) -> Path:
        """업로드된 원본 PDF 파일 경로를 반환한다."""
        path = self.document_dir(document_id) / "source.pdf"
        if not path.is_file():
            raise DocumentNotFoundError(document_id)
        return path

    def page_image_path(self, document_id: str, page: int) -> Path:
        """검수 화면/학습데이터 export에서 사용하는 특정 페이지의 렌더링된 PNG 경로를 반환한다."""
        document = self.get(document_id)  # type: ignore[attr-defined]
        page_row = next((item for item in document.pages if item.page == page), None)
        if page_row is None:
            raise DocumentNotFoundError(f"{document_id}/page/{page}")
        path = self.document_dir(document_id) / page_row.image_name
        if not path.is_file():
            raise DocumentNotFoundError(f"{document_id}/page/{page}")
        return path


class ReviewStore(Protocol):
    """`ReviewService`가 필요로 하는 검수 문서 저장소의 계약.

    `PostgresReviewStore`(운영)와 `InMemoryReviewStore`(테스트)가 각각 이 Protocol을
    구현한다. 바이너리 자산 메서드(document_dir 등)까지 포함하는 이유는 `ReviewService`와
    `review/api.py`가 이 인터페이스 하나만 보고 두 종류의 자산(구조화 메타데이터, 바이너리
    파일)을 모두 다루기 때문이다.
    """

    root: Path

    def document_dir(self, document_id: str) -> Path: ...

    def create_dir(self, document_id: str) -> Path: ...

    def source_path(self, document_id: str) -> Path: ...

    def page_image_path(self, document_id: str, page: int) -> Path: ...

    def save(self, document: ReviewDocument) -> None: ...

    def get(self, document_id: str) -> ReviewDocument: ...

    def list(self) -> list[ReviewDocument]: ...


class PostgresReviewStore(_ReviewFileAssets):
    """검수 문서 메타데이터의 운영 저장소. `review_documents` 테이블에 문서당 1행을 유지한다.

    `document` 컬럼에 `ReviewDocument` 전체를 JSON으로 저장해 round-trip 원본으로 삼고,
    `phase`/`status`/`paper_id`는 목록·필터링 질의를 위해 함께 저장하는 파생 컬럼이다
    (매 저장마다 함께 갱신되므로 두 값이 어긋나지 않는다).
    """

    def __init__(
        self,
        root: Path,
        settings: Settings | None = None,
        engine: Engine | None = None,
    ) -> None:
        super().__init__(root)
        self.engine = engine or get_engine(settings)

    def save(self, document: ReviewDocument) -> None:
        """문서 전체 상태를 `review_documents`에 upsert한다(있으면 갱신, 없으면 삽입)."""
        statement = text(
            """
            INSERT INTO review_documents (
                document_id, phase, status, paper_id, document, created_at, updated_at
            )
            VALUES (
                :document_id, :phase, :status, :paper_id,
                CAST(:document AS jsonb), :created_at, :updated_at
            )
            ON CONFLICT (document_id) DO UPDATE
            SET
                phase = EXCLUDED.phase,
                status = EXCLUDED.status,
                paper_id = EXCLUDED.paper_id,
                document = EXCLUDED.document,
                updated_at = EXCLUDED.updated_at
            """
        )
        with self.engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "document_id": document.document_id,
                    "phase": document.phase,
                    "status": document.status,
                    "paper_id": document.paper_id,
                    "document": document.model_dump_json(),
                    "created_at": document.created_at,
                    "updated_at": document.updated_at,
                },
            )

    def get(self, document_id: str) -> ReviewDocument:
        """`review_documents`에서 문서를 읽어 ReviewDocument로 역직렬화한다. 없으면 DocumentNotFoundError."""
        statement = text(
            "SELECT document::text AS document FROM review_documents WHERE document_id = :document_id"
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement, {"document_id": document_id}).mappings().first()
        if row is None:
            raise DocumentNotFoundError(document_id)
        return ReviewDocument.model_validate_json(row["document"])

    def list(self) -> list[ReviewDocument]:
        """모든 문서를 최신 생성 순으로 나열한다."""
        statement = text(
            "SELECT document::text AS document FROM review_documents ORDER BY created_at DESC"
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [ReviewDocument.model_validate_json(row["document"]) for row in rows]


class InMemoryReviewStore(_ReviewFileAssets):
    """PostgresReviewStore와 같은 계약을 순수 파이썬 dict로 재현한 테스트용 구현.

    실제 PostgreSQL 없이 검수 서비스를 오프라인으로 검증하기 위한 페이크다(CLAUDE.md 코드
    규칙 — 외부 서비스는 페이크로 대체). 바이너리 자산(원본 PDF/페이지 PNG)은
    `_ReviewFileAssets`가 그대로 실제 디스크(tmp_path 등)에 관리한다.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._documents: dict[str, ReviewDocument] = {}

    def save(self, document: ReviewDocument) -> None:
        self._documents[document.document_id] = document.model_copy(deep=True)

    def get(self, document_id: str) -> ReviewDocument:
        document = self._documents.get(document_id)
        if document is None:
            raise DocumentNotFoundError(document_id)
        return document.model_copy(deep=True)

    def list(self) -> list[ReviewDocument]:
        return sorted(
            (document.model_copy(deep=True) for document in self._documents.values()),
            key=lambda item: item.created_at,
            reverse=True,
        )
