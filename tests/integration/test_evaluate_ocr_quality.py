"""scripts/evaluate_ocr_quality.py의 DB 조회·분류 로직을 실제 PostgreSQL(pgserver)로 검증한다.

CER/TEDS 수치 자체의 정확성은 tests/test_eval_metrics.py(오프라인)가 이미 검증하므로,
여기서는 `collect_evaluation`이 phase별로 문서를 올바르게 걸러내고 블록을 CER/TEDS 대상으로
올바르게 분류하는지만 확인한다.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from paperrag.review.models import ReviewBlock, ReviewDocument
from paperrag.review.store import PostgresReviewStore
from scripts.evaluate_ocr_quality import collect_evaluation


@pytest.fixture()
def engine(pg_dsn: str) -> Iterator[Engine]:
    sqlalchemy_dsn = pg_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    created = create_engine(sqlalchemy_dsn, pool_pre_ping=True)
    try:
        yield created
    finally:
        created.dispose()


def _block(block_id: str, block_type: str, ocr_text: str, **overrides: object) -> ReviewBlock:
    fields: dict[str, object] = {
        "block_id": block_id,
        "page": 1,
        "block_type": block_type,
        "detected_block_type": block_type,
        "order": int(block_id.split("-")[-1]),
        "bbox": (0.0, 0.0, 10.0, 10.0),
        "detected_bbox": (0.0, 0.0, 10.0, 10.0),
        "ocr_text": ocr_text,
        "review_status": "approved",
    }
    fields.update(overrides)
    return ReviewBlock.model_validate(fields)


def _document(document_id: str, phase: str, blocks: list[ReviewBlock]) -> ReviewDocument:
    now = datetime.now(UTC)
    return ReviewDocument.model_validate(
        {
            "document_id": document_id,
            "filename": "paper.pdf",
            "source_path": f"/tmp/{document_id}/source.pdf",
            "backend": "paddle",
            "phase": phase,
            "blocks": blocks,
            "created_at": now,
            "updated_at": now,
        }
    )


def test_collect_evaluation_skips_documents_not_ready(tmp_path: Path, engine: Engine) -> None:
    store = PostgresReviewStore(tmp_path, engine=engine)
    ready = _document(
        "eva00000000000000000000000000001",
        "ready_to_ingest",
        [_block("b-1", "text", "정답과 동일")],
    )
    pending = _document(
        "eva00000000000000000000000000002", "layout_review", []
    )
    store.save(ready)
    store.save(pending)

    report = collect_evaluation(store, [ready.document_id, pending.document_id])

    assert [doc.document_id for doc in report.ready_documents] == [ready.document_id]
    assert report.pending == [(pending.document_id, "layout_review")]


def test_collect_evaluation_classifies_blocks_by_type(tmp_path: Path, engine: Engine) -> None:
    store = PostgresReviewStore(tmp_path, engine=engine)
    document = _document(
        "eva00000000000000000000000000003",
        "ready_to_ingest",
        [
            _block("b-1", "text", "본문 텍스트", corrected_text="본문 텍스트"),
            _block("b-2", "table", "a | b", corrected_text="a | b"),
            _block("b-3", "figure", ""),
            _block("b-4", "text", "제외됨", review_status="rejected"),
        ],
    )
    store.save(document)

    report = collect_evaluation(store, [document.document_id])

    assert report.cer_result.block_count == 1  # text만(figure 제외, rejected 제외)
    assert report.teds_result.table_count == 1
    assert report.cer_result.cer == 0.0


def test_collect_evaluation_uses_ocr_text_as_prediction_and_effective_text_as_reference(
    tmp_path: Path, engine: Engine
) -> None:
    store = PostgresReviewStore(tmp_path, engine=engine)
    document = _document(
        "eva00000000000000000000000000004",
        "ready_to_ingest",
        [_block("b-1", "text", "인식 오타", corrected_text="정답 문장")],
    )
    store.save(document)

    report = collect_evaluation(store, [document.document_id])

    assert report.cer_result.blocks[0].reference == "정답 문장"
    assert report.cer_result.blocks[0].hypothesis == "인식 오타"


def test_collect_evaluation_flags_documents_with_zero_genuine_corrections(
    tmp_path: Path, engine: Engine
) -> None:
    """승인만 하고 원문 대조를 안 한 문서(corrected_text가 ocr_text와 늘 같음)는
    CER이 자동으로 0%가 되므로, verification에서 "실제 교정 0건"으로 드러나야 한다
    (2026-07-23 실측: 완료 문서 2편 모두 이 상태였음)."""
    store = PostgresReviewStore(tmp_path, engine=engine)
    unverified = _document(
        "eva00000000000000000000000000005",
        "ready_to_ingest",
        [
            _block("b-1", "text", "승인만 함", corrected_text="승인만 함"),
            _block("b-2", "text", "역시 승인만"),
        ],
    )
    verified = _document(
        "eva00000000000000000000000000006",
        "ready_to_ingest",
        [_block("b-1", "text", "오타있음", corrected_text="정답")],
    )
    store.save(unverified)
    store.save(verified)

    report = collect_evaluation(store, [unverified.document_id, verified.document_id])

    by_id = {item.document_id: item for item in report.verification}
    assert by_id[unverified.document_id].eligible_blocks == 2
    assert by_id[unverified.document_id].verified_blocks == 0
    assert by_id[verified.document_id].verified_blocks == 1
