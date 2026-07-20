"""로컬 `review.json` 파일(구 FileReviewStore)을 새 `review_documents` 테이블로 1회 이전하는 스크립트.

`PostgresReviewStore`로 전환한 뒤에는 API/워커가 더 이상 `review_dir/<id>/review.json`을 읽지
않으므로, 이미 검수 중이던 기존 문서들은 이 스크립트를 한 번 실행해 DB로 옮겨야 계속 보인다.
원본 PDF·페이지 PNG 같은 바이너리 자산은 옮기지 않는다 — 파일 경로(`review_dir` 기준)가 그대로
유효하므로 손댈 필요가 없다. `save()`가 upsert이므로 여러 번 실행해도 안전하다(idempotent).
손상되었거나 형식이 안 맞는 review.json은 건너뛰고 그 사유를 출력한다.
"""

from pathlib import Path
import sys

from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from paperrag.config import get_settings  # noqa: E402
from paperrag.review.models import ReviewDocument  # noqa: E402
from paperrag.review.store import PostgresReviewStore  # noqa: E402


def main() -> int:
    settings = get_settings()
    store = PostgresReviewStore(settings.review_dir, settings)

    migrated = 0
    skipped: list[str] = []
    for path in sorted(settings.review_dir.glob("*/review.json")):
        try:
            document = ReviewDocument.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            skipped.append(f"{path}: {exc}")
            continue
        try:
            store.save(document)
        except IntegrityError as exc:
            # review_documents.paper_id는 papers(paper_id) FK다 — 이 DB에 해당 논문이 없으면
            # (예: papers 테이블이 별도로 초기화/리셋된 경우) 그 문서만 건너뛰고 계속 진행한다.
            skipped.append(
                f"{document.document_id}: paper_id={document.paper_id}가 papers 테이블에 없음 ({exc.orig})"
            )
            continue
        migrated += 1
        print(f"migrated {document.document_id} (phase={document.phase}, status={document.status})")

    print(f"\n{migrated}개 문서를 review_documents 테이블로 이전했습니다.")
    if skipped:
        print(f"{len(skipped)}개는 건너뛰었습니다:")
        for reason in skipped:
            print(f"  - {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
