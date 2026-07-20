"""검수 문서(`ReviewDocument`)를 위한 파일시스템 기반 저장소.

DB가 아니라 `review_dir` 아래 `<document_id>/review.json` 파일 하나에 문서 전체 상태를
JSON으로 직렬화해 저장한다. 구현이 단순하고 별도 인프라(DB 마이그레이션 등)가 필요 없다는
장점이 있지만 다음과 같은 한계를 그대로 가진다(docs/guide/10-production-readiness.md 참고).

- 파일 읽기-수정-쓰기 사이에 락이 없어, API를 여러 replica로 동시에 띄우면 같은 문서를 두
  요청이 동시에 갱신할 때 경쟁 조건(lost update)이 생길 수 있다. 현재는 API를 단일 프로세스로
  운영하는 것을 전제로 한다.
- 문서를 삭제하는 API/메서드가 없다. `document_dir`가 한 번 생성되면 검수·적재가 끝나도
  디스크에 계속 남으며, 정리하려면 파일시스템을 직접 조작해야 한다.
"""

import re
from pathlib import Path

from paperrag.review.models import ReviewDocument

# 업로드 시 uuid4().hex로 생성되는 32자리 소문자 16진수 document_id 형식.
# 경로 조작(path traversal) 공격을 막기 위해 document_dir 등 모든 경로 조합 전에
# 이 정규식으로 형식을 검증한다.
DOCUMENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class DocumentNotFoundError(KeyError):
    """존재하지 않는 document_id, 형식이 잘못된 document_id, 또는 그 하위 페이지/이미지를 찾을 때 발생."""

    pass


class FileReviewStore:
    """검수 문서 하나당 `root/<document_id>/` 디렉터리를 사용하는 JSON 파일 저장소."""

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

    def save(self, document: ReviewDocument) -> None:
        """문서 전체 상태를 review.json으로 저장한다.

        임시 파일(.tmp)에 먼저 쓴 뒤 `replace`로 원자적으로 교체해, 저장 도중 프로세스가
        죽어도 review.json 자체가 반쯤 쓰인 상태로 깨지지 않도록 한다(다만 여러 요청이
        동시에 저장하는 경쟁 조건까지 막아주지는 않는다 — 모듈 docstring 참고).
        """
        directory = self.document_dir(document.document_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "review.json"
        temporary = directory / "review.json.tmp"
        temporary.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def get(self, document_id: str) -> ReviewDocument:
        """review.json을 읽어 ReviewDocument로 역직렬화한다. 없으면 DocumentNotFoundError."""
        path = self.document_dir(document_id) / "review.json"
        if not path.is_file():
            raise DocumentNotFoundError(document_id)
        return ReviewDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[ReviewDocument]:
        """모든 문서를 최신 생성 순으로 나열한다.

        손상되었거나(잘린 JSON 등) 읽는 도중 사라진 review.json은 조용히 건너뛴다 —
        전체 목록 조회가 파일 하나의 손상 때문에 실패하지 않게 하기 위함이다.
        """
        documents: list[ReviewDocument] = []
        for path in sorted(self.root.glob("*/review.json"), reverse=True):
            try:
                documents.append(
                    ReviewDocument.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                continue
        return sorted(documents, key=lambda item: item.created_at, reverse=True)

    def source_path(self, document_id: str) -> Path:
        """업로드된 원본 PDF 파일 경로를 반환한다."""
        path = self.document_dir(document_id) / "source.pdf"
        if not path.is_file():
            raise DocumentNotFoundError(document_id)
        return path

    def page_image_path(self, document_id: str, page: int) -> Path:
        """검수 화면/학습데이터 export에서 사용하는 특정 페이지의 렌더링된 PNG 경로를 반환한다."""
        document = self.get(document_id)
        page_row = next((item for item in document.pages if item.page == page), None)
        if page_row is None:
            raise DocumentNotFoundError(f"{document_id}/page/{page}")
        path = self.document_dir(document_id) / page_row.image_name
        if not path.is_file():
            raise DocumentNotFoundError(f"{document_id}/page/{page}")
        return path
