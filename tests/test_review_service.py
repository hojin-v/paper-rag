from io import BytesIO
import json
from datetime import UTC, datetime
from pathlib import Path
import zipfile

from pdf_fixtures import PdfBuilder
import pytest

from paperrag.config import Settings
from paperrag.ingest.models import DocumentLayout, LayoutBlock
from paperrag.review.models import BlockCreate, BlockUpdate, ReviewBlock, ReviewDocument
from paperrag.review.service import ReviewService
from paperrag.review.store import InMemoryReviewStore
from paperrag.review.viewer import build_viewer_html


def _service(settings: Settings) -> ReviewService:
    """실제 Postgres 없이 검수 서비스를 오프라인으로 테스트하기 위한 헬퍼.

    운영 기본값(PostgresReviewStore)을 InMemoryReviewStore로 교체할 뿐, 구조화 메타데이터
    저장 방식만 다르고 나머지(바이너리 자산 디렉터리 등)는 동일하게 동작한다.
    """
    return ReviewService(settings, store=InMemoryReviewStore(settings.review_dir))


def test_upload_review_update_and_training_export(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        review_dir=tmp_path / "review",
        review_render_dpi=72,
        paragraph_min_chars=10,
        allow_diagnostic_backends=True,
    )
    service = _service(settings)

    document = service.upload("sample.pdf", _pdf_bytes(), backend="simple")

    assert document.pdf_kind is None
    assert document.processing_mode == "full_ocr"
    assert document.backend == "simple"
    assert len(document.pages) == 1
    assert document.pages[0].image_width == 400
    assert document.blocks
    assert document.blocks[0].bbox is not None
    assert document.blocks[0].detected_block_type == document.blocks[0].block_type
    assert document.blocks[0].detected_bbox == document.blocks[0].bbox
    assert service.store.page_image_path(document.document_id, 1).is_file()

    block = document.blocks[0]
    updated = service.update_block(
        document.document_id,
        block.block_id,
        BlockUpdate(corrected_text="교정된 제목", review_status="corrected"),
    )
    assert updated.blocks[0].effective_text == "교정된 제목"
    assert updated.blocks[0].detected_block_type == block.block_type

    partial_archive_bytes = service.export_training_zip()
    with zipfile.ZipFile(BytesIO(partial_archive_bytes)) as archive:
        partial_manifest = json.loads(archive.read("manifest.json"))
        assert partial_manifest["layout_pages"] == 0
        assert partial_manifest["ocr_crops"] == 1
        assert partial_manifest["skipped_incomplete_layout_pages"] == 1

    approved = service.approve_all(document.document_id)
    assert all(block.review_status != "unreviewed" for block in approved.blocks)

    archive_bytes = service.export_training_zip()
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        assert "layout/annotations.jsonl" in names
        assert "ocr/labels.jsonl" in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["layout_pages"] == 1
        assert manifest["ocr_crops"] >= 1
        assert manifest["skipped_incomplete_layout_pages"] == 0


def test_viewer_contains_clickable_overlay_and_ocr_text(tmp_path: Path) -> None:
    service = _service(
        Settings(
            _env_file=None,
            review_dir=tmp_path / "review",
            allow_diagnostic_backends=True,
        )
    )
    document = service.upload("sample.pdf", _pdf_bytes(), backend="simple")

    rendered = build_viewer_html(document)

    assert 'onclick="selectBlock' in rendered
    assert "모델 OCR 원문" in rendered
    assert "자동 유형" in rendered
    assert "자동 좌표" in rendered
    assert '<section id="layout-tools" hidden>' in rendered
    assert document.blocks[0].ocr_text in rendered
    assert f'class="block-{document.blocks[0].block_type}"' in rendered
    assert '<div class="legend">' in rendered
    assert "const initialBlock=" in rendered
    assert "grid-template-columns:minmax(0,1fr) minmax(300px,360px)" in rendered
    assert "@media(max-width:720px)" in rendered
    assert "aside{order:-1" in rendered


def test_auto_backend_always_routes_to_full_ocr_paddle(tmp_path: Path) -> None:
    service = _service(
        Settings(
            _env_file=None,
            review_dir=tmp_path / "review",
            paddle_isolate_process=False,
        )
    )

    backend, warnings = service._select_backend("auto")

    assert backend == "paddle"
    assert warnings == []


def test_production_upload_rejects_non_ocr_backend(tmp_path: Path) -> None:
    service = _service(
        Settings(
            _env_file=None,
            review_dir=tmp_path / "review",
            paddle_isolate_process=False,
        )
    )

    with pytest.raises(ValueError, match="진단 전용"):
        service.upload("sample.pdf", _pdf_bytes(), backend="simple")


def test_title_quality_rejects_publisher_logo_not_supported_by_citation() -> None:
    blocks = [
        ReviewBlock(
            block_id="title",
            page=1,
            block_type="title",
            order=0,
            ocr_text="Open Library of Humanities",
        ),
        ReviewBlock(
            block_id="citation",
            page=1,
            block_type="text",
            order=1,
            ocr_text=(
                "How to Cite: Kiessling et al. Advances and Limitations in "
                "Open Source Arabic-Script OCR: A Case Study."
            ),
        ),
    ]

    assert ReviewService._title_consistent(blocks) is False


def test_automation_quality_requires_recognized_author(tmp_path: Path) -> None:
    service = _service(Settings(_env_file=None, review_dir=tmp_path / "review"))
    now = datetime.now(UTC)
    document = ReviewDocument(
        document_id="missing-author",
        filename="paper.pdf",
        source_path=str(tmp_path / "paper.pdf"),
        backend="paddle",
        blocks=[
            ReviewBlock(
                block_id="title",
                page=1,
                block_type="title",
                order=0,
                ocr_text="Paper title",
            ),
            ReviewBlock(
                block_id="abstract",
                page=1,
                block_type="abstract",
                order=1,
                ocr_text="Abstract text",
            ),
        ],
        created_at=now,
        updated_at=now,
    )

    quality = service._automation_quality(document)

    assert quality.status == "needs_review"
    assert quality.author_detected is False
    assert "저자 영역 또는 저자 OCR 누락" in quality.reasons


def test_staged_layout_then_region_ocr_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StagedBackend:
        def analyze_layout(self, pdf_path: str) -> DocumentLayout:
            return DocumentLayout(
                source_path=pdf_path,
                is_scanned=True,
                blocks=[
                    LayoutBlock(
                        page=1,
                        block_type="title",
                        text="",
                        order=0,
                        bbox=(35, 35, 360, 75),
                        confidence=0.9,
                    ),
                    LayoutBlock(
                        page=1,
                        block_type="table",
                        text="",
                        order=1,
                        bbox=(35, 90, 360, 160),
                        confidence=0.85,
                    ),
                ],
            )

        def recognize_layout(self, pdf_path: str, blocks: list[LayoutBlock]) -> DocumentLayout:
            recognized = [
                block.model_copy(
                    update={
                        "text": "Recognized title"
                        if block.block_type == "title"
                        else "Metric | Value\nF1 | 0.90",
                        "ocr_engine": "pp-ocrv5-region"
                        if block.block_type == "title"
                        else "paddle-table-structure-wireless",
                    }
                )
                for block in blocks
            ]
            return DocumentLayout(
                source_path=pdf_path,
                is_scanned=True,
                blocks=recognized,
            )

    monkeypatch.setattr("paperrag.review.service.get_backend", lambda name: StagedBackend())
    service = _service(
        Settings(
            _env_file=None,
            review_dir=tmp_path / "review",
            paddle_isolate_process=False,
            automatic_ocr_require_author=False,
        )
    )

    document = service.upload("staged.pdf", _pdf_bytes(), backend="paddle")

    assert document.phase == "layout_review"
    assert all(block.ocr_text == "" for block in document.blocks)
    layout_viewer = build_viewer_html(document)
    assert '<section id="layout-tools" >' in layout_viewer
    assert "페이지에서 박스 그리기 시작" in layout_viewer
    assert "선택 박스 이동·크기 조절" in layout_viewer
    assert "resize-handle" in layout_viewer
    assert "선택 영역 삭제" in layout_viewer
    assert "pointerdown" in layout_viewer
    added = service.add_block(
        document.document_id,
        BlockCreate(page=1, block_type="text", bbox=(40, 180, 360, 220)),
    )
    added_block_id = added.blocks[-1].block_id
    deleted = service.delete_block(document.document_id, added_block_id)
    assert all(block.block_id != added_block_id for block in deleted.blocks)
    assert "영역을 삭제했습니다" in deleted.warnings[-1]
    service.approve_all(document.document_id)
    ocr_document = service.run_reviewed_ocr(document.document_id)
    assert ocr_document.phase == "ocr_review"
    assert [block.ocr_text for block in ocr_document.blocks] == [
        "Recognized title",
        "Metric | Value\nF1 | 0.90",
    ]
    assert ocr_document.blocks[1].ocr_engine == "paddle-table-structure-wireless"
    with pytest.raises(ValueError, match="레이아웃 단계"):
        service.update_block(
            document.document_id,
            ocr_document.blocks[0].block_id,
            BlockUpdate(bbox=(35, 35, 300, 75)),
        )
    layout_document = service.return_to_layout_review(document.document_id)
    assert layout_document.phase == "layout_review"
    assert all(
        block.ocr_text == ""
        for block in layout_document.blocks
        if block.review_status != "rejected"
    )
    service.approve_all(document.document_id)
    ocr_document = service.run_reviewed_ocr(document.document_id)
    assert ocr_document.phase == "ocr_review"
    service.approve_all(document.document_id)
    ready = service.confirm_ocr(document.document_id)
    assert ready.phase == "ready_to_ingest"

    automatic = service.upload("automatic.pdf", _pdf_bytes(), backend="paddle")
    automatic = service.run_automatic_ocr(automatic.document_id)
    assert automatic.phase == "ready_to_ingest"
    assert automatic.automation_quality is not None
    assert automatic.automation_quality.status == "passed"
    assert automatic.automation_quality.ocr_coverage == 1.0
    assert automatic.automation_quality.title_detected is True
    assert automatic.automation_quality.tables_structured == 1


def _pdf_bytes() -> bytes:
    return (
        PdfBuilder()
        .add_page(400, 500)
        .text(40, 60, "Paper RAG Layout Review", fontsize=18)
        .text(40, 100, "This paragraph is extracted for OCR review.", fontsize=11)
        .build()
    )
