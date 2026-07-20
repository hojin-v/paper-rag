"""PostgresReviewStore를 실제 PostgreSQL(pgserver)에 대해 검증하는 통합 테스트.

InMemoryReviewStore로는 SQL 자체의 오류(파라미터 타입 추론 실패, JSONB 캐스팅 등)를 잡을 수
없다 — 이 프로젝트에서 실제로 그런 버그가 real-Postgres 테스트에서만 드러난 전례가 있다
(search/repository.py의 AmbiguousParameter). 그래서 review_documents 테이블에 대해서도
같은 방식(pgserver 세션 픽스처)으로 upsert·조회·목록 정렬을 직접 검증한다.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from paperrag.review.models import ReviewDocument
from paperrag.review.store import DocumentNotFoundError, PostgresReviewStore


@pytest.fixture()
def engine(pg_dsn: str) -> Iterator[Engine]:
    sqlalchemy_dsn = pg_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    created = create_engine(sqlalchemy_dsn, pool_pre_ping=True)
    try:
        yield created
    finally:
        created.dispose()


def _document(document_id: str, **overrides: object) -> ReviewDocument:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "document_id": document_id,
        "filename": "paper.pdf",
        "source_path": f"/tmp/{document_id}/source.pdf",
        "backend": "paddle",
        "phase": "layout_review",
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return ReviewDocument.model_validate(fields)


def test_save_then_get_round_trips_full_document(tmp_path: Path, engine: Engine) -> None:
    store = PostgresReviewStore(tmp_path, engine=engine)
    document = _document("11111111111111111111111111111111", warnings=["레이아웃 보정함"])

    store.save(document)
    loaded = store.get(document.document_id)

    assert loaded == document


def test_save_twice_upserts_instead_of_duplicating(tmp_path: Path, engine: Engine) -> None:
    store = PostgresReviewStore(tmp_path, engine=engine)
    document = _document("22222222222222222222222222222222")
    store.save(document)

    # paper_id는 실제 papers 행을 가리켜야 한다 — review_documents.paper_id는 papers(paper_id)를
    # 참조하는 FK라, 존재하지 않는 값을 넣으면 이 upsert 자체가 실패한다(적재 완료 문서만
    # paper_id를 갖는다는 불변조건을 DB 레벨에서도 보장).
    with engine.begin() as connection:
        paper_id = connection.execute(
            text("INSERT INTO papers (title) VALUES ('Upsert Test Paper') RETURNING paper_id")
        ).scalar_one()

    updated = document.model_copy(
        update={"phase": "ready_to_ingest", "paper_id": paper_id, "updated_at": datetime.now(UTC)}
    )
    store.save(updated)

    loaded = store.get(document.document_id)
    assert loaded.phase == "ready_to_ingest"
    assert loaded.paper_id == paper_id
    assert len([d for d in store.list() if d.document_id == document.document_id]) == 1


def test_get_missing_document_raises_not_found(tmp_path: Path, engine: Engine) -> None:
    store = PostgresReviewStore(tmp_path, engine=engine)

    with pytest.raises(DocumentNotFoundError):
        store.get("33333333333333333333333333333333")


def test_list_orders_by_created_at_descending(tmp_path: Path, engine: Engine) -> None:
    store = PostgresReviewStore(tmp_path, engine=engine)
    older = _document(
        "44444444444444444444444444444444",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _document(
        "55555555555555555555555555555555",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    store.save(older)
    store.save(newer)

    ids_in_order = [
        document.document_id
        for document in store.list()
        if document.document_id in {older.document_id, newer.document_id}
    ]
    assert ids_in_order == [newer.document_id, older.document_id]
