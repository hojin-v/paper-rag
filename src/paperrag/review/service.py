from __future__ import annotations

import json
import multiprocessing
import queue
import re
import shutil
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from paperrag.config import Settings, get_settings
from paperrag.ingest.layout import get_backend
from paperrag.ingest.layout.dedup import deduplicate_layout_blocks
from paperrag.ingest.layout.paddle_backend import _table_structure_quality
from paperrag.ingest.models import DocumentLayout, LayoutBlock
from paperrag.ingest.pipeline import IngestPipeline
from paperrag.review.models import (
    AutomationQuality,
    BlockCreate,
    BlockUpdate,
    IngestedDocument,
    LayoutQuality,
    ReviewBlock,
    ReviewDocument,
    ReviewPage,
)
from paperrag.review.store import FileReviewStore

ALLOWED_BACKENDS = {"auto", "simple", "docling", "paddle"}


class InvalidPdfError(ValueError):
    pass


class StoredLayoutBackend:
    def __init__(self, layout: DocumentLayout) -> None:
        self.layout = layout

    def analyze(self, pdf_path: str) -> DocumentLayout:
        return self.layout.model_copy(update={"source_path": pdf_path})


def _paddle_stage_worker(
    operation: str,
    settings_payload: dict[str, object],
    pdf_path: str,
    blocks_payload: list[dict[str, object]],
    result_queue: object,
) -> None:
    output = result_queue
    try:
        settings = Settings.model_validate(settings_payload)
        backend = get_backend("paddle")
        backend.settings = settings
        if operation == "layout":
            result = backend.analyze_layout(pdf_path)
        else:
            blocks = [LayoutBlock.model_validate(row) for row in blocks_payload]
            result = backend.recognize_layout(pdf_path, blocks)
        output.put(("ok", result.model_dump(mode="python")))
    except Exception as exc:
        output.put(("error", f"{type(exc).__name__}: {exc}"))


class ReviewService:
    def __init__(
        self,
        settings: Settings | None = None,
        store: FileReviewStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or FileReviewStore(self.settings.review_dir)

    def upload(self, filename: str, content: bytes, backend: str = "paddle") -> ReviewDocument:
        selected = backend.strip().lower()
        if selected not in ALLOWED_BACKENDS:
            raise ValueError(f"지원하지 않는 backend입니다: {backend}")
        if selected not in {"auto", "paddle"} and not self.settings.allow_diagnostic_backends:
            raise ValueError(
                "OCR 없는 simple/docling backend는 진단 전용입니다. "
                "운영 업로드는 paddle만 허용합니다."
            )
        if not content.startswith(b"%PDF-"):
            raise InvalidPdfError("PDF 시그니처가 없는 파일입니다.")
        if len(content) > self.settings.review_max_upload_mb * 1024 * 1024:
            raise InvalidPdfError(
                f"PDF는 {self.settings.review_max_upload_mb}MB 이하여야 합니다."
            )

        document_id = uuid4().hex
        directory = self.store.create_dir(document_id)
        source_path = directory / "source.pdf"
        source_path.write_bytes(content)

        actual_backend, warnings = self._select_backend(selected)
        try:
            backend_instance = get_backend(actual_backend)
            analyze_layout = getattr(backend_instance, "analyze_layout", None)
            staged_layout = actual_backend == "paddle" and callable(analyze_layout)
            if staged_layout and self.settings.paddle_isolate_process:
                layout = self._run_isolated_paddle("layout", str(source_path), [])
            else:
                layout = (
                    analyze_layout(str(source_path))
                    if staged_layout
                    else backend_instance.analyze(str(source_path))
                )
            pages = self._render_pages(source_path, directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        blocks: list[ReviewBlock] = []
        for index, block in enumerate(layout.blocks, start=1):
            detected_bbox = _clip_bbox(block.bbox, pages, block.page)
            blocks.append(
                ReviewBlock(
                    block_id=f"b-{index:06d}",
                    page=block.page,
                    block_type=block.block_type,
                    detected_block_type=block.block_type,
                    order=block.order,
                    bbox=detected_bbox,
                    detected_bbox=detected_bbox,
                    confidence=block.confidence,
                    ocr_engine=block.ocr_engine,
                    ocr_text=block.text,
                )
            )
        fallback_count = sum(
            block.ocr_engine == "pp-ocrv5-unassigned" for block in layout.blocks
        )
        if fallback_count:
            warnings.append(
                f"레이아웃 영역에 포함되지 않은 전체 OCR {fallback_count}개를 "
                "누락 방지 본문 후보로 보존했습니다."
            )
        now = datetime.now(UTC)
        document = ReviewDocument(
            document_id=document_id,
            filename=Path(filename).name or "uploaded.pdf",
            source_path=str(source_path),
            backend=actual_backend,
            phase="layout_review" if staged_layout else "ready_to_ingest",
            pages=pages,
            blocks=blocks,
            warnings=warnings,
            layout_quality=(
                LayoutQuality.model_validate(layout.metrics)
                if layout.metrics
                else None
            ),
            created_at=now,
            updated_at=now,
        )
        self.store.save(document)
        return document

    def add_block(self, document_id: str, create: BlockCreate) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase != "layout_review":
            raise ValueError("영역 추가는 레이아웃 검수 단계에서만 가능합니다.")
        bbox = _clip_bbox(create.bbox, document.pages, create.page)
        if bbox is None:
            raise ValueError("페이지 안의 유효한 영역 좌표가 필요합니다.")
        next_number = max(
            (int(block.block_id.removeprefix("b-")) for block in document.blocks),
            default=0,
        ) + 1
        document.blocks.append(
            ReviewBlock(
                block_id=f"b-{next_number:06d}",
                page=create.page,
                block_type=create.block_type,
                detected_block_type=None,
                order=max((block.order for block in document.blocks), default=-1) + 1,
                bbox=bbox,
                detected_bbox=None,
                review_status="corrected",
            )
        )
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def delete_block(self, document_id: str, block_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase != "layout_review":
            raise ValueError("영역 삭제는 레이아웃 검수 단계에서만 가능합니다.")
        block = next((item for item in document.blocks if item.block_id == block_id), None)
        if block is None:
            raise KeyError(block_id)
        document.blocks = [item for item in document.blocks if item.block_id != block_id]
        document.warnings.append(
            f"레이아웃 검수에서 {block.block_id}({block.block_type}, page={block.page}, "
            f"bbox={block.bbox}) 영역을 삭제했습니다."
        )
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def run_reviewed_ocr(self, document_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase != "layout_review":
            raise ValueError("레이아웃 검수 단계의 문서만 OCR을 실행할 수 있습니다.")
        unreviewed = [
            block.block_id
            for block in document.blocks
            if block.review_status == "unreviewed"
        ]
        if unreviewed:
            raise ValueError(
                f"레이아웃 미검수 영역 {len(unreviewed)}개를 승인·교정·제외해야 합니다."
            )
        source_blocks = [
            LayoutBlock(
                page=block.page,
                block_type=block.block_type,
                text="",
                order=block.order,
                bbox=block.bbox,
                confidence=block.confidence,
                ocr_engine=None,
            )
            for block in document.blocks
            if block.review_status != "rejected"
        ]
        if document.backend == "paddle" and self.settings.paddle_isolate_process:
            result = self._run_isolated_paddle(
                "ocr",
                document.source_path,
                source_blocks,
            )
        else:
            backend = get_backend(document.backend)
            recognize_layout = getattr(backend, "recognize_layout", None)
            if not callable(recognize_layout):
                raise ValueError(
                    f"{document.backend} backend는 단계형 OCR을 지원하지 않습니다."
                )
            result = recognize_layout(document.source_path, source_blocks)
        recognized_by_order = {block.order: block for block in result.blocks}
        updated_blocks: list[ReviewBlock] = []
        for block in document.blocks:
            recognized = recognized_by_order.get(block.order)
            if recognized is None or block.review_status == "rejected":
                updated_blocks.append(block)
                continue
            updated_blocks.append(
                block.model_copy(
                    update={
                        "ocr_text": recognized.text,
                        "corrected_text": None,
                        "ocr_engine": recognized.ocr_engine,
                        "block_type": recognized.block_type,
                        "review_status": "unreviewed",
                    }
                )
            )
        document.blocks = updated_blocks
        document.phase = "ocr_review"
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def run_automatic_ocr(self, document_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase != "layout_review":
            raise ValueError("레이아웃 분석 단계의 문서만 자동 OCR을 실행할 수 있습니다.")
        document.blocks = [
            block.model_copy(update={"review_status": "approved"})
            if block.review_status == "unreviewed"
            else block
            for block in document.blocks
        ]
        self.store.save(document)
        document = self.run_reviewed_ocr(document_id)
        quality = self._automation_quality(document)
        document.automation_quality = quality
        empty_ids = set(quality.empty_block_ids)
        document.blocks = [
            block.model_copy(
                update={
                    "review_status": (
                        "unreviewed"
                        if quality.status == "needs_review"
                        and block.block_id in empty_ids
                        else "approved"
                    )
                }
            )
            if block.review_status != "rejected"
            else block
            for block in document.blocks
        ]
        if quality.status == "passed":
            document.phase = "ready_to_ingest"
        else:
            document.warnings.append(
                "자동 품질 기준 미달로 관리자 예외 대기열에 보냈습니다: "
                + "; ".join(quality.reasons)
            )
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def reevaluate_automatic_quality(self, document_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase not in {"ocr_review", "ready_to_ingest"}:
            raise ValueError("OCR이 완료된 문서만 자동 품질을 다시 판정할 수 있습니다.")
        quality = self._automation_quality(document)
        document.automation_quality = quality
        document.phase = (
            "ready_to_ingest" if quality.status == "passed" else "ocr_review"
        )
        if quality.status == "needs_review":
            message = "자동 품질 재판정 미달: " + "; ".join(quality.reasons)
            if message not in document.warnings:
                document.warnings.append(message)
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def confirm_ocr(self, document_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase != "ocr_review":
            raise ValueError("OCR 검수 단계의 문서만 최종 확정할 수 있습니다.")
        unreviewed = [
            block.block_id
            for block in document.blocks
            if block.review_status == "unreviewed"
        ]
        if unreviewed:
            raise ValueError(
                f"OCR 미검수 영역 {len(unreviewed)}개를 승인·교정·제외해야 합니다."
            )
        document.phase = "ready_to_ingest"
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def return_to_layout_review(self, document_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase != "ocr_review":
            raise ValueError("OCR 검수 단계의 문서만 레이아웃 검수로 되돌릴 수 있습니다.")
        document.blocks = [
            block.model_copy(
                update={
                    "ocr_text": "",
                    "corrected_text": None,
                    "ocr_engine": None,
                    "review_status": "unreviewed",
                }
            )
            if block.review_status != "rejected"
            else block
            for block in document.blocks
        ]
        document.phase = "layout_review"
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def deduplicate_layout(self, document_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        if document.phase != "layout_review":
            raise ValueError("자동 중복 정리는 레이아웃 검수 단계에서만 가능합니다.")
        automatic = [
            LayoutBlock(
                page=block.page,
                block_type=block.block_type,
                text="",
                order=block.order,
                bbox=block.bbox,
                confidence=block.confidence,
            )
            for block in document.blocks
            if block.detected_bbox is not None and block.review_status != "rejected"
        ]
        retained_orders = {
            block.order for block in deduplicate_layout_blocks(automatic)
        }
        before = len(automatic)
        document.blocks = [
            block
            for block in document.blocks
            if block.detected_bbox is None
            or block.review_status == "rejected"
            or block.order in retained_orders
        ]
        removed = before - len(retained_orders)
        if removed:
            document.warnings.append(f"겹친 자동 레이아웃 영역 {removed}개를 정리했습니다.")
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def _run_isolated_paddle(
        self,
        operation: str,
        pdf_path: str,
        blocks: list[LayoutBlock],
    ) -> DocumentLayout:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(
            target=_paddle_stage_worker,
            args=(
                operation,
                self.settings.model_dump(mode="python"),
                pdf_path,
                [block.model_dump(mode="python") for block in blocks],
                result_queue,
            ),
        )
        process.start()
        try:
            status, payload = result_queue.get(
                timeout=self.settings.paddle_worker_timeout_seconds
            )
        except queue.Empty as exc:
            process.terminate()
            process.join(10)
            raise TimeoutError(
                f"Paddle {operation} 작업이 제한 시간을 초과했습니다."
            ) from exc
        finally:
            result_queue.close()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        if status != "ok":
            raise RuntimeError(str(payload))
        return DocumentLayout.model_validate(payload)

    def _automation_quality(self, document: ReviewDocument) -> AutomationQuality:
        eligible_types = {
            "title",
            "author",
            "abstract",
            "section_header",
            "text",
            "table",
            "table_caption",
            "reference",
        }
        eligible = [
            block
            for block in document.blocks
            if block.review_status != "rejected" and block.block_type in eligible_types
        ]
        recognized = [block for block in eligible if block.effective_text.strip()]
        empty_ids = [block.block_id for block in eligible if not block.effective_text.strip()]
        coverage = len(recognized) / len(eligible) if eligible else 0.0
        title_detected = any(
            block.block_type == "title" and bool(block.effective_text.strip())
            for block in eligible
        )
        title_consistent = self._title_consistent(eligible)
        tables = [block for block in eligible if block.block_type == "table"]
        structured_tables = [
            block
            for block in tables
            if (block.ocr_engine or "").startswith("paddle-table-structure-")
            and _table_structure_quality(block.effective_text)
            >= self.settings.paddle_table_min_structure_quality
        ]
        reasons: list[str] = []
        if coverage < self.settings.automatic_ocr_min_coverage:
            reasons.append(
                f"OCR 영역 인식률 {coverage:.1%} < "
                f"{self.settings.automatic_ocr_min_coverage:.1%}"
            )
        if not title_detected:
            reasons.append("제목 영역 또는 제목 OCR 누락")
        elif not title_consistent:
            reasons.append("제목 OCR이 문서의 인용 메타데이터와 일치하지 않음")
        if len(structured_tables) != len(tables):
            reasons.append(
                f"표 구조화 {len(structured_tables)}/{len(tables)}개"
            )
        return AutomationQuality(
            status="needs_review" if reasons else "passed",
            eligible_blocks=len(eligible),
            recognized_blocks=len(recognized),
            ocr_coverage=coverage,
            title_detected=title_detected,
            title_consistent=title_consistent,
            tables_detected=len(tables),
            tables_structured=len(structured_tables),
            empty_block_ids=empty_ids,
            reasons=reasons,
        )

    @staticmethod
    def _title_consistent(blocks: list[ReviewBlock]) -> bool:
        title_text = " ".join(
            block.effective_text for block in blocks if block.block_type == "title"
        )
        citation_texts = [
            block.effective_text
            for block in blocks
            if re.search(r"\bhow\s+to\s+cite\s*:", block.effective_text, re.IGNORECASE)
        ]
        if not citation_texts or not title_text.strip():
            return True
        title_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", title_text.lower())
            if len(token) >= 3
        }
        citation_tokens = set(
            re.findall(r"[a-z0-9]+", " ".join(citation_texts).lower())
        )
        if not title_tokens:
            return False
        return len(title_tokens & citation_tokens) / len(title_tokens) >= 0.5

    def get(self, document_id: str) -> ReviewDocument:
        return self.store.get(document_id)

    def list(self) -> list[ReviewDocument]:
        return self.store.list()

    def update_block(
        self,
        document_id: str,
        block_id: str,
        update: BlockUpdate,
    ) -> ReviewDocument:
        document = self.store.get(document_id)
        block = next((item for item in document.blocks if item.block_id == block_id), None)
        if block is None:
            raise KeyError(block_id)
        changes = update.model_dump(exclude_none=True)
        geometry_changes = {"bbox", "block_type"}.intersection(changes)
        if geometry_changes and document.phase != "layout_review":
            raise ValueError(
                "OCR 입력 영역과 결과의 일치를 위해 레이아웃 단계에서만 "
                "영역 좌표와 유형을 바꿀 수 있습니다."
            )
        if "corrected_text" in changes and document.phase == "layout_review":
            raise ValueError("OCR 텍스트 교정은 OCR 실행 후에 가능합니다.")
        if "bbox" in changes:
            changes["bbox"] = _clip_bbox(changes["bbox"], document.pages, block.page)
        updated_block = block.model_copy(update=changes)
        document.blocks = [
            updated_block if item.block_id == block_id else item for item in document.blocks
        ]
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def approve_all(self, document_id: str) -> ReviewDocument:
        document = self.store.get(document_id)
        document.blocks = [
            block.model_copy(update={"review_status": "approved"})
            if block.review_status == "unreviewed"
            else block
            for block in document.blocks
        ]
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return document

    def ingest(self, document_id: str) -> IngestedDocument:
        document = self.store.get(document_id)
        if document.status == "ingested" and document.paper_id is not None:
            return IngestedDocument(document_id=document_id, paper_id=document.paper_id)
        if document.phase != "ready_to_ingest":
            raise ValueError("레이아웃 검수와 OCR 검수를 완료한 뒤 적재할 수 있습니다.")
        document.status = "ingesting"
        document.error = None
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        try:
            from paperrag.ingest.embeddings import HttpEmbeddingClient
            from paperrag.ingest.llm_enrich import OllamaClient
            from paperrag.ingest.repository import PostgresIngestRepository

            layout = DocumentLayout(
                source_path=document.source_path,
                is_scanned=True,
                blocks=[
                    LayoutBlock(
                        page=block.page,
                        block_type=block.block_type,
                        text=block.effective_text,
                        order=block.order,
                        bbox=block.bbox,
                        confidence=block.confidence,
                        ocr_engine=block.ocr_engine,
                    )
                    for block in document.blocks
                    if block.review_status != "rejected"
                ],
            )
            pipeline = IngestPipeline(
                PostgresIngestRepository(self.settings),
                StoredLayoutBackend(layout),
                OllamaClient(self.settings),
                HttpEmbeddingClient(self.settings),
                settings=self.settings,
            )
            report = pipeline.run(document.source_path)
        except Exception as exc:
            document.status = "failed"
            document.error = str(exc)
            document.updated_at = datetime.now(UTC)
            self.store.save(document)
            raise

        if report.paper_id is None:
            raise RuntimeError("수집 파이프라인이 paper_id를 반환하지 않았습니다.")
        document.status = "ingested"
        document.paper_id = report.paper_id
        document.updated_at = datetime.now(UTC)
        self.store.save(document)
        return IngestedDocument(
            document_id=document_id,
            paper_id=report.paper_id,
            totals=report.totals,
        )

    def export_training_zip(self, include_unreviewed: bool = False) -> bytes:
        output = BytesIO()
        layout_rows: list[str] = []
        ocr_rows: list[str] = []
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for document in self.store.list():
                accepted = [
                    block
                    for block in document.blocks
                    if block.bbox is not None
                    and block.review_status != "rejected"
                    and (include_unreviewed or block.review_status in {"approved", "corrected"})
                ]
                if not accepted:
                    continue
                for page in document.pages:
                    page_blocks = [block for block in accepted if block.page == page.page]
                    if not page_blocks:
                        continue
                    image_path = self.store.page_image_path(document.document_id, page.page)
                    image_name = f"layout/images/{document.document_id}-p{page.page:04d}.png"
                    archive.write(image_path, image_name)
                    layout_rows.append(
                        json.dumps(
                            {
                                "image": image_name,
                                "width": page.image_width or page.width,
                                "height": page.image_height or page.height,
                                "document_id": document.document_id,
                                "page": page.page,
                                "blocks": [
                                    {
                                        "block_id": block.block_id,
                                        "label": block.block_type,
                                        "bbox": _scale_bbox_for_image(block.bbox, page),
                                        "text": block.effective_text,
                                    }
                                    for block in page_blocks
                                ],
                            },
                            ensure_ascii=False,
                        )
                    )
                self._write_ocr_crops(archive, document, accepted, ocr_rows)
            archive.writestr("layout/annotations.jsonl", "\n".join(layout_rows) + "\n")
            archive.writestr("ocr/labels.jsonl", "\n".join(ocr_rows) + "\n")
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": "paperrag-training-v1",
                        "layout_pages": len(layout_rows),
                        "ocr_crops": len(ocr_rows),
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return output.getvalue()

    def _write_ocr_crops(
        self,
        archive: zipfile.ZipFile,
        document: ReviewDocument,
        blocks: list[ReviewBlock],
        rows: list[str],
    ) -> None:
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:
            return
        with pymupdf.open(document.source_path) as pdf:
            for block in blocks:
                if block.bbox is None or not block.effective_text.strip():
                    continue
                page_index = block.page - 1
                if page_index < 0 or page_index >= len(pdf):
                    continue
                clip = pymupdf.Rect(*block.bbox)
                pixmap = pdf[page_index].get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip)
                image_name = f"ocr/images/{document.document_id}-{block.block_id}.png"
                archive.writestr(image_name, pixmap.tobytes("png"))
                rows.append(
                    json.dumps(
                        {
                            "image": image_name,
                            "text": block.effective_text,
                            "document_id": document.document_id,
                            "block_id": block.block_id,
                        },
                        ensure_ascii=False,
                    )
                )

    def _select_backend(
        self,
        selected: str,
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []
        if selected != "auto":
            return selected, warnings
        return "paddle", warnings

    def _analyze(
        self,
        source_path: Path,
        backend: str,
    ) -> DocumentLayout:
        return get_backend(backend).analyze(str(source_path))

    def _render_pages(self, source_path: Path, directory: Path) -> list[ReviewPage]:
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError("PDF 검수 화면에는 PyMuPDF가 필요합니다.") from exc
        scale = self.settings.review_render_dpi / 72.0
        pages: list[ReviewPage] = []
        with pymupdf.open(source_path) as pdf:
            for index, page in enumerate(pdf, start=1):
                image_name = f"page-{index:04d}.png"
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                pixmap.save(directory / image_name)
                pages.append(
                    ReviewPage(
                        page=index,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        image_name=image_name,
                        image_width=int(pixmap.width),
                        image_height=int(pixmap.height),
                    )
                )
        return pages


def _clip_bbox(
    bbox: tuple[float, float, float, float] | None,
    pages: list[ReviewPage],
    page_number: int,
) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    page = next((item for item in pages if item.page == page_number), None)
    if page is None:
        return None
    x1, y1, x2, y2 = bbox
    clipped = (
        max(0.0, min(page.width, x1)),
        max(0.0, min(page.height, y1)),
        max(0.0, min(page.width, x2)),
        max(0.0, min(page.height, y2)),
    )
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        return None
    return clipped


def _scale_bbox_for_image(
    bbox: tuple[float, float, float, float] | None,
    page: ReviewPage,
) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    scale_x = (page.image_width or page.width) / page.width
    scale_y = (page.image_height or page.height) / page.height
    x1, y1, x2, y2 = bbox
    return (x1 * scale_x, y1 * scale_y, x2 * scale_x, y2 * scale_y)
