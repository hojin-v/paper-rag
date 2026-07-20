from datetime import UTC, datetime
from types import SimpleNamespace

from paperrag.review.models import (
    DocumentStatus,
    ReviewBlock,
    ReviewDocument,
    ReviewPage,
    ReviewPhase,
    ReviewStatus,
)
from paperrag.ui.app import (
    _default_document_id,
    _document_label,
    _filter_review_documents,
    _layout_quality_metrics,
    _review_progress,
)


def test_layout_quality_metrics_supports_legacy_model() -> None:
    legacy_quality = SimpleNamespace(
        detected_text_lines=10,
        initial_text_coverage=0.8,
        expanded_blocks=2,
        added_text_blocks=1,
    )

    metrics = _layout_quality_metrics(legacy_quality)

    assert metrics.final_text_coverage == 0.8
    assert metrics.uncovered_text_lines == 2
    assert metrics.split_section_headings == 0
    assert metrics.recovered_title_blocks == 0
    assert metrics.recovered_author_blocks == 0


def test_layout_quality_metrics_repairs_hydrated_legacy_defaults() -> None:
    legacy_quality = SimpleNamespace(
        detected_text_lines=10,
        initial_text_coverage=0.8,
        final_text_coverage=0.0,
        uncovered_text_lines=0,
    )

    metrics = _layout_quality_metrics(legacy_quality)

    assert metrics.final_text_coverage == 0.8
    assert metrics.uncovered_text_lines == 2


def test_default_document_prefers_most_recent_analysis() -> None:
    older_ocr = _review_document("older", pages=11, ocr_text="full OCR", created_day=12)
    recent_layout = _review_document("recent", pages=3, ocr_text="", created_day=13)

    assert _default_document_id([older_ocr, recent_layout]) == "recent"
    assert _document_label(older_ocr) == ("older.pdf · OCR 완료 · 11쪽 · 1영역 · OCR 1 · 미검수 1")


def test_review_queue_filters_and_counts_corrections() -> None:
    pending = _review_document("pending", pages=1, ocr_text="")
    exception = _review_document(
        "exception",
        pages=1,
        ocr_text="OCR",
        phase="ocr_review",
    )
    completed = _review_document(
        "completed",
        pages=1,
        ocr_text="OCR",
        status="ingested",
        review_status="approved",
    )
    changed_block = pending.blocks[0].model_copy(update={"block_type": "abstract"})
    pending = pending.model_copy(update={"blocks": [changed_block]})

    assert _filter_review_documents([pending, exception, completed], "pending") == [
        pending,
        exception,
    ]
    assert _filter_review_documents([pending, exception, completed], "ocr_exception") == [exception]
    assert _filter_review_documents([pending, exception, completed], "completed") == [completed]
    assert _review_progress(pending).changed_from_detection == 1


def _review_document(
    document_id: str,
    *,
    pages: int,
    ocr_text: str,
    created_day: int = 13,
    phase: ReviewPhase | None = None,
    status: DocumentStatus = "analyzed",
    review_status: ReviewStatus = "unreviewed",
) -> ReviewDocument:
    timestamp = datetime(2026, 7, created_day, tzinfo=UTC)
    return ReviewDocument(
        document_id=document_id,
        filename=f"{document_id}.pdf",
        source_path=f"/{document_id}.pdf",
        backend="paddle",
        phase=phase or ("ready_to_ingest" if ocr_text else "layout_review"),
        status=status,
        pages=[
            ReviewPage(
                page=page,
                width=595,
                height=842,
                image_name=f"page-{page}.png",
            )
            for page in range(1, pages + 1)
        ],
        blocks=[
            ReviewBlock(
                block_id=f"{document_id}-block",
                page=1,
                block_type="text",
                detected_block_type="text",
                order=0,
                bbox=(0, 0, 100, 20),
                detected_bbox=(0, 0, 100, 20),
                ocr_text=ocr_text,
                review_status=review_status,
            )
        ],
        created_at=timestamp,
        updated_at=timestamp,
    )
