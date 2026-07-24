"""검수 상태 기계(state machine)와 자동 품질 판정 로직.

이 파일이 이 프로젝트에서 가장 복잡한 부분이다. `ReviewDocument.phase`는 다음 세 단계를
순서대로(또는 되돌아가며) 거친다.

    layout_review  --run_reviewed_ocr/run_automatic_ocr-->  ocr_review
    ocr_review     --confirm_ocr(사람) 또는 자동 품질 합격-->  ready_to_ingest
    ocr_review     --return_to_layout_review-->              layout_review (되돌아가기)

- `layout_review`: 업로드 직후 상태. PP-DocLayout이 검출한 박스 좌표·유형만 있고 OCR 텍스트는
  비어 있다. 이 단계에서만 좌표·유형 추가/삭제/이동/리사이즈가 허용된다(OCR crop과 좌표가 어긋나면
  안 되기 때문). 모든 블록이 unreviewed가 아니어야 (승인/교정/제외 완료) `run_reviewed_ocr`를
  호출할 수 있다.
- `ocr_review`: 확정 좌표로 crop한 뒤 실제 OCR을 실행한 상태. 이 단계부터는 좌표·유형은 잠기고
  텍스트 교정과 검수 상태만 바꿀 수 있다. 사람이 `confirm_ocr`로 확정하거나, 자동 경로에서
  `_automation_quality`가 합격 판정을 내리면 `ready_to_ingest`로 넘어간다.
- `ready_to_ingest`: DB·pgvector 적재(`ingest`) 가능 상태.

`run_automatic_ocr`는 사람 승인 없이 위 흐름을 한 번에 밀어붙이는 자동 경로다: 레이아웃을 전부
approved 처리 → OCR 실행 → `_automation_quality`로 품질 재판정 → 합격이면 ready_to_ingest,
불합격이면 ocr_review에 남겨 관리자 예외 대기열로 보낸다. `_automation_quality`는 OCR 인식률,
제목/저자 검출 여부, 제목-인용 메타데이터 일치도, 표 구조화 비율 네 가지를 확인한다. 실패 시
"빈 블록(empty_block_ids)"만 unreviewed로 되돌리는데, 이는 알려진 한계다 — 제목/저자 블록 자체가
레이아웃 단계에서 아예 검출되지 않아 실패한 경우 되돌릴 블록이 없으므로 문서는 ocr_review에
머물지만 관리자가 화면에서 볼 검수 대상 블록은 없다(모델의 신뢰 근거가 되는 개별 판정 항목마다
"어떤 블록을 고쳐야 하는지"를 정확히 가리키지 못하는 부분은 아직 구현되어 있지 않다).
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import queue
import re
import shutil
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from paperrag.concurrency import heavy_task_slot
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
from paperrag.review.store import PostgresReviewStore, ReviewStore

logger = logging.getLogger(__name__)

ALLOWED_BACKENDS = {"auto", "simple", "docling", "paddle"}


class InvalidPdfError(ValueError):
    """업로드된 바이트가 PDF 시그니처를 만족하지 않거나 크기 제한을 넘었을 때 발생."""

    pass


class StoredLayoutBackend:
    """검수를 거쳐 이미 확정된 `DocumentLayout`을 그대로 돌려주는 어댑터.

    `IngestPipeline`은 "경로를 분석해 레이아웃을 만드는" 백엔드를 기대하지만, 적재
    시점에는 이미 검수 완료된 레이아웃(사람이 교정한 텍스트 포함)이 있으므로 새로
    분석하지 않고 그 결과를 그대로 반환하도록 백엔드 인터페이스를 흉내낸다.
    """

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
    """별도 프로세스(spawn)에서 실행되는 Paddle 레이아웃/OCR 작업 진입점.

    `multiprocessing.Process`의 target으로 전달되므로 pickle 가능한 인자만 받는다(Settings와
    LayoutBlock을 dict로 직렬화해 넘기고, 결과도 dict로 돌려준다). 성공/실패 여부와 결과를
    `result_queue`에 담아 부모 프로세스(`ReviewService._run_isolated_paddle`)로 전달한다.
    """
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
    """검수 상태 기계의 전이 로직과 자동 품질 판정을 구현하는 핵심 서비스.

    각 메서드는 store(기본값 `PostgresReviewStore`)에서 문서를 읽고, phase 전이 규칙을
    검증한 뒤, 갱신된 문서를 다시 저장한다(요청 사이의 상태는 전부 store가 들고 있고 이
    서비스 자체는 무상태다). API 라우터(`api.py`)는 이 클래스의 예외를 HTTP 상태 코드로만
    변환한다. 테스트는 실제 Postgres 없이 오프라인으로 검증할 수 있도록
    `store=InMemoryReviewStore(...)`를 명시적으로 주입한다.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        store: ReviewStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or PostgresReviewStore(self.settings.review_dir, self.settings)

    def upload(self, filename: str, content: bytes, backend: str = "paddle") -> ReviewDocument:
        """PDF 바이트를 저장하고 레이아웃 검출을 실행해 새 검수 문서를 만든다.

        운영 정책상 `simple`/`docling`처럼 OCR이 없는 진단 전용 backend는
        `allow_diagnostic_backends`가 켜져 있지 않으면 거부한다(운영 업로드 오염 방지).
        `paddle_isolate_process`가 켜져 있으면 레이아웃 검출을 별도 프로세스에서 실행해
        분석이 끝나면 Paddle이 잡고 있던 메모리/CPU를 API 프로세스에서 즉시 회수한다.
        분석에 실패하면 이미 만든 문서 디렉터리를 지우고(rmtree) 예외를 다시 던진다.
        """
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
        """레이아웃 검수 단계에서 사람이 새로 그린 박스를 블록으로 추가한다.

        detected_bbox/detected_block_type을 None으로 두어 "모델이 검출한 게 아니라 사람이
        추가했다"는 사실을 구분해 남기고, review_status는 바로 "corrected"로 표시한다.
        """
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
        """레이아웃 검수 단계에서 잘못 검출된 블록을 목록에서 완전히 제거한다.

        나중에 "왜 이 영역이 없어졌는지" 추적할 수 있도록 삭제한 블록의 ID·유형·페이지·좌표를
        문서의 warnings 이력에 남긴다(삭제 자체를 되돌리는 기능은 없다).
        """
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
        """확정된(검수 완료된) 레이아웃 박스만 crop해 영역별 OCR을 실행하고 ocr_review로 전이한다.

        layout_review 단계에서만 호출 가능하며, 모든 블록이 unreviewed가 아니어야 한다(승인·
        교정·제외 중 하나로 확정되어 있어야 함) — 화면에 표시된 좌표와 실제 OCR 입력 crop이
        어긋나지 않도록 보장하기 위한 조건이다. rejected 블록은 OCR 대상에서 제외하되 문서에는
        그대로 남긴다. 실행 결과 review_status는 다시 "unreviewed"로 초기화되어(OCR 텍스트를
        새로 검수해야 하므로) ocr_review 단계의 검수 대상이 된다.
        """
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
        """사람의 개별 승인 없이 레이아웃 전체를 approved 처리한 뒤 OCR과 자동 품질 판정까지 이어서 실행한다.

        흐름: (1) unreviewed 블록을 모두 approved로 바꿔 `run_reviewed_ocr`의 전제조건(미검수
        없음)을 자동으로 충족시킨다 → (2) `run_reviewed_ocr`로 실제 OCR을 실행한다(phase는
        일단 ocr_review가 됨) → (3) `_automation_quality`로 품질을 판정한다.

        판정 결과 반영 방식이 이 프로젝트에서 가장 까다로운 부분이다.
        - 품질이 "합격(passed)"이면 모든 블록을 approved로 확정하고 phase를 ready_to_ingest로
          올려 바로 적재 가능하게 만든다.
        - "불합격(needs_review)"이면 `quality.empty_block_ids`(OCR 결과가 비어 있는 블록)만
          다시 "unreviewed"로 되돌려 관리자가 검수 화면에서 우선적으로 봐야 할 대상으로
          표시하고, 나머지는 approved로 둔 채 phase는 ocr_review에 머문다.
        - 알려진 한계: 실패 사유가 "제목/저자 블록 자체가 레이아웃 단계에서 검출되지 않음"인
          경우에는 되돌릴 빈 블록이 존재하지 않는다(문제는 블록 부재이지 블록 내용이 아니므로).
          이때는 empty_block_ids가 비어 있는 채로 문서가 ocr_review에 머무르지만, 검수 화면에는
          "다시 봐야 할" unreviewed 블록이 하나도 없어 관리자가 무엇을 고쳐야 하는지 화면만으로는
          알기 어렵다 — reasons 문자열(예: "제목 영역 또는 제목 OCR 누락")을 직접 읽고 레이아웃
          단계로 되돌아가(`return_to_layout_review`) 블록을 추가해야 한다.
        """
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
        """OCR을 다시 실행하지 않고, 현재 블록 내용 그대로 `_automation_quality`만 재계산한다.

        관리자가 예외 대기열 문서에서 텍스트를 직접 교정한 뒤 "이제 통과하는지" 확인하거나,
        이미 ready_to_ingest에 있는 문서를 최신 품질 기준으로 재검증할 때 사용한다.
        합격이면 ready_to_ingest, 아니면 ocr_review로 phase를 맞춰 되돌린다.
        """
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
        """사람이 OCR 결과 검수를 완료했음을 확정해 ready_to_ingest로 전이한다.

        (자동 경로가 아니라) 관리자가 화면에서 직접 하나씩 승인/교정하는 흐름의 마지막 단계다.
        unreviewed 블록이 남아 있으면 거부한다.
        """
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
        """OCR 결과에서 레이아웃 문제(좌표·유형 오류)를 발견했을 때 OCR 결과를 폐기하고 되돌린다.

        rejected가 아닌 모든 블록의 ocr_text/corrected_text/ocr_engine을 지우고 다시
        unreviewed로 만든다 — 좌표를 고친 뒤에는 이전 OCR 텍스트가 더 이상 유효한 crop 결과가
        아니므로 반드시 폐기해야 한다는 원칙을 따른 것이다.
        """
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
        """모델이 자동 검출한 박스 중 IoU 0.85 이상 겹치는 중복과 오검출 컨테이너를 정리한다.

        `deduplicate_layout_blocks`(ingest.layout.dedup)에 실제 중복 판정을 위임한다.
        사람이 직접 그린 블록(detected_bbox가 None)과 이미 제외(rejected)된 블록은 애초에
        정리 대상 목록에 넣지 않아 자동 삭제되지 않도록 보존한다.
        """
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
        """Paddle 레이아웃/OCR 연산을 별도 spawn 프로세스에서 실행하고 결과를 기다린다.

        PaddleOCR/PaddleX 모델은 로드하는 데 상당한 메모리를 쓰고 CPU 스레드를 오래 점유한다.
        API 프로세스 안에서 직접 실행하면 그 메모리가 프로세스 수명 내내 반환되지 않고, 여러
        요청이 겹치면 스레드 경합으로 API 응답성(예: /health)이 떨어진다. 별도 프로세스로
        분리하면 작업이 끝나는 즉시 프로세스를 종료해 메모리/CPU를 OS에 돌려줄 수 있고, 그
        프로세스가 멈추거나 타임아웃되어도 API 프로세스 자체에는 영향을 주지 않는다.
        `paddle_worker_timeout_seconds` 안에 결과가 오지 않으면 프로세스를 강제 종료하고
        TimeoutError를 던진다. 프로세스 스폰부터 결과 대기까지 전부
        `concurrency.heavy_task_slot`로 감싸, LLM 호출과 같은 자원 풀을 공유하는
        동시 실행 개수 제한을 받는다(2026-07-12 실측: 둘이 동시에 상주하면 swap이
        가득 참).

        Celery의 prefork 워커 프로세스는 그 자체가 데몬 프로세스라 Python
        multiprocessing 제약상 자식 프로세스를 만들 수 없다("daemonic processes are
        not allowed to have children" — 2026-07-23 async OCR 경로 실기동 중 재현).
        이미 API와 분리된 워커 컨테이너 안에서 호출된 경우이므로 이 함수의 격리
        목적(API 프로세스 메모리/스레드 보호)은 그 자체로 충족돼 있어, 그럴 땐 추가로
        스폰하지 않고 같은 프로세스에서 직접 실행한다.
        """
        settings_payload = self.settings.model_dump(mode="python")
        blocks_payload = [block.model_dump(mode="python") for block in blocks]
        logger.info("Paddle %s 실행 시작 (pdf_path=%s)", operation, pdf_path)

        if multiprocessing.current_process().daemon:
            with heavy_task_slot(self.settings):
                inline_queue: queue.Queue = queue.Queue()
                _paddle_stage_worker(
                    operation, settings_payload, pdf_path, blocks_payload, inline_queue
                )
                status, payload = inline_queue.get_nowait()
            if status != "ok":
                logger.error("Paddle %s 실패 (pdf_path=%s): %s", operation, pdf_path, payload)
                raise RuntimeError(str(payload))
            logger.info("Paddle %s 완료 (pdf_path=%s)", operation, pdf_path)
            return DocumentLayout.model_validate(payload)

        with heavy_task_slot(self.settings):
            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            process = context.Process(
                target=_paddle_stage_worker,
                args=(operation, settings_payload, pdf_path, blocks_payload, result_queue),
            )
            process.start()
            try:
                status, payload = result_queue.get(
                    timeout=self.settings.paddle_worker_timeout_seconds
                )
            except queue.Empty as exc:
                process.terminate()
                process.join(10)
                logger.error(
                    "Paddle %s 시간 초과 (pdf_path=%s, timeout=%.0fs)",
                    operation,
                    pdf_path,
                    self.settings.paddle_worker_timeout_seconds,
                )
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
            logger.error("Paddle %s 실패 (pdf_path=%s): %s", operation, pdf_path, payload)
            raise RuntimeError(str(payload))
        logger.info("Paddle %s 완료 (pdf_path=%s)", operation, pdf_path)
        return DocumentLayout.model_validate(payload)

    def _automation_quality(self, document: ReviewDocument) -> AutomationQuality:
        """자동 OCR 결과가 사람 개입 없이 적재해도 되는 품질인지 네 가지 기준으로 판정한다.

        1) OCR 인식률(ocr_coverage): 판정 대상 블록 중 텍스트가 비어 있지 않은 비율이
           `automatic_ocr_min_coverage` 미만이면 실패 — 레이아웃은 잡았지만 OCR 자체가 실패한
           경우(흐린 스캔, 폰트 문제 등)를 잡아낸다.
        2) 제목/저자 검출(title_detected/author_detected): 제목·저자 유형 블록이 존재하고
           텍스트도 인식되어야 한다 — 검색·메타데이터 품질에 치명적인 "제목을 통째로 놓친" 경우를
           잡아낸다. 저자 요구 여부는 `automatic_ocr_require_author` 설정으로 끌 수 있다.
        3) 제목-인용 일치도(title_consistent): 논문 페이지 안에 "How to cite:" 형태의 인용
           메타데이터가 있으면 그 문자열과 제목 OCR 텍스트의 토큰 겹침을 비교한다 — 레이아웃이
           엉뚱한 작은 박스를 "제목"으로 잘못 잡아 실제 제목과 다른 텍스트를 인식한 경우를
           잡아낸다(제목 박스는 있지만 내용이 틀린 케이스).
        4) 표 구조화 비율(tables_structured/tables_detected): 표로 분류된 블록이 표 구조화
           엔진(paddle-table-structure-*)으로 인식되고 구조화 품질 임계치를 넘겨야 한다 — 표를
           표로는 검출했지만 셀 구조를 못 살린 경우를 잡아낸다.

        네 기준 중 하나라도 실패하면 status는 "needs_review"가 되고 `reasons`에 실패 사유가
        쌓인다. 어떤 블록을 다시 봐야 하는지는 `empty_block_ids`(OCR 결과가 비어 있는 블록)만
        추려서 반환하는데, 이는 "블록은 있지만 텍스트가 비었다"는 실패만 정확히 짚어줄 뿐
        "블록 자체가 없다"는 실패(예: 제목 블록 검출 실패)는 가리키지 못하는 한계가 있다
        (자세한 내용은 이 파일 상단 모듈 docstring과 `run_automatic_ocr` 참고).
        """
        # figure/formula/header_footer 등은 OCR 인식 여부가 적재 품질에 크게 영향을 주지 않는
        # 유형이라 판정 대상(eligible)에서 제외한다.
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
        author_detected = any(
            block.block_type == "author" and bool(block.effective_text.strip())
            for block in eligible
        )
        title_consistent = self._title_consistent(eligible)
        tables = [block for block in eligible if block.block_type == "table"]
        # 표 구조화 엔진(paddle-table-structure-wired/paddle-table-structure-wireless 등)으로
        # 인식되었고 구조화 품질 점수가 임계치 이상인 표만 "구조화 성공"으로 센다.
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
        if self.settings.automatic_ocr_require_author and not author_detected:
            reasons.append("저자 영역 또는 저자 OCR 누락")
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
            author_detected=author_detected,
            title_consistent=title_consistent,
            tables_detected=len(tables),
            tables_structured=len(structured_tables),
            empty_block_ids=empty_ids,
            reasons=reasons,
        )

    @staticmethod
    def _title_consistent(blocks: list[ReviewBlock]) -> bool:
        """제목 OCR 텍스트가 문서 내 "How to cite:" 인용 메타데이터와 토큰 단위로 겹치는지 확인한다.

        많은 학술지 페이지는 첫 페이지 어딘가에 인용 형식 문구(저자·제목·저널명 포함)를 싣는다.
        그 문구 안의 단어들과 제목 블록 OCR 텍스트의 단어(3자 이상, 영숫자만)가 50% 이상
        겹치면 일치한다고 본다. 인용 문구가 없거나 제목이 비어 있으면 비교할 근거가 없으므로
        보수적으로 True(일치)를 반환한다 — 이 검사가 있다는 이유만으로 멀쩡한 문서를 실패
        처리하지 않기 위함이다.
        """
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
        """document_id로 검수 문서 전체 상태를 조회한다."""
        return self.store.get(document_id)

    def list(self) -> list[ReviewDocument]:
        """모든 검수 문서를 최신순으로 나열한다."""
        return self.store.list()

    def update_block(
        self,
        document_id: str,
        block_id: str,
        update: BlockUpdate,
    ) -> ReviewDocument:
        """블록 하나를 부분 갱신한다. 어떤 필드를 바꿀 수 있는지는 문서 phase에 따라 검증한다.

        좌표(bbox)·유형(block_type) 변경은 layout_review 단계에서만 허용한다(이미 OCR을
        실행했다면 crop 좌표와 실제 OCR 입력이 어긋나게 되므로). 반대로 텍스트 교정
        (corrected_text)은 OCR 실행 전(layout_review)에는 아직 OCR 원문이 없으므로 거부한다.
        """
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
        """검수 상태가 unreviewed인 모든 블록을 일괄 approved로 바꾼다. phase 전이는 없다.

        관리자가 사람 검수 흐름에서 "나머지는 다 괜찮다"고 판단해 한 번에 승인 처리할 때
        쓰는 편의 기능이며, `run_automatic_ocr`가 내부적으로 하는 자동 승인과는 별개다.
        """
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
        """ready_to_ingest 상태의 문서를 실제 DB·pgvector 파이프라인으로 적재한다.

        이미 ingested 상태면 재실행하지 않고 기존 결과를 그대로 반환한다(idempotent 재호출).
        rejected 블록은 적재 대상에서 제외한다. 적재 도중 예외가 발생하면 문서 status를
        "failed"로, error 필드에 원인을 기록해 store에 저장한 뒤 예외를 다시 던진다 —
        실패를 성공으로 위장하지 않기 위함이다(docs/guide/10-production-readiness.md의
        "운영 대체값 차단" 정책과 같은 맥락).
        """
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
            from paperrag.collect.service import lookup_source_metadata
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
            # 저널명·원문 링크는 PDF에서 뽑히지 않으므로, 수집(collect) 단계를 거쳐 검수
            # 큐에 들어온 문서라면 원본 업로드 파일명(document.filename — 검수 저장소는
            # 실제 파일을 source.pdf로 복사·개명하므로 document.source_path에는 이 정보가
            # 없다)에 남은 OpenAlex source_id로 collection-manifest에서 찾아 채운다.
            # manifest에 없으면(직접 업로드 등) best effort로 (None, None) — 적재를 막지 않는다.
            journal, full_text_link = lookup_source_metadata(document.filename, self.settings)
            report = pipeline.run(
                document.source_path, journal=journal, full_text_link=full_text_link
            )
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
        """검수 완료된 데이터를 모아 Colab 학습용 ZIP(PP-DocLayout/PP-OCR 재학습 입력)을 만든다.

        ZIP 구조는 docs/guide/09-upload-review-colab-training.md 3단계에 정의된 형식과 같다.
        - layout/images/<document_id>-p<page>.png: 검수한 페이지 이미지
        - layout/annotations.jsonl: 페이지별 블록 좌표(이미지 픽셀 좌표로 스케일 변환됨)와 라벨
        - ocr/images/<document_id>-<block_id>.png: 블록 단위 OCR crop 이미지
        - ocr/labels.jsonl: 그 crop에 대응하는 최종 텍스트(effective_text, 즉 사람 교정 반영본)
        - manifest.json: 페이지/crop 개수와 포맷 버전

        기본값(`include_unreviewed=False`)은 approved/corrected 블록만 포함해 아직 사람이
        보지 않은 자동 OCR 결과가 학습데이터로 섞여 들어가지 않게 한다. 한 페이지 안에 아직
        unreviewed 블록이 하나라도 남아 있으면 그 페이지 전체를 레이아웃 학습데이터에서
        제외한다(부분적으로만 검수된 페이지의 레이아웃 어노테이션이 불완전한 채로 섞이는 것을
        막기 위함) — 이렇게 건너뛴 페이지 수는 `include_unreviewed=True`로 명시하지 않는 한
        manifest의 skipped_incomplete_layout_pages로만 드러난다.
        """
        output = BytesIO()
        layout_rows: list[str] = []
        ocr_rows: list[str] = []
        skipped_incomplete_layout_pages = 0
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
                    # 이 페이지에 좌표가 있는 모든 블록(승인 여부와 무관) — 아래에서
                    # "이 페이지가 완전히 검수됐는지" 판단하는 데 쓴다.
                    detected_page_blocks = [
                        block
                        for block in document.blocks
                        if block.page == page.page and block.bbox is not None
                    ]
                    if not include_unreviewed and any(
                        block.review_status == "unreviewed"
                        for block in detected_page_blocks
                    ):
                        skipped_incomplete_layout_pages += 1
                        continue
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
                        "skipped_incomplete_layout_pages": (
                            skipped_incomplete_layout_pages
                        ),
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
        """블록별로 원본 PDF에서 2배 확대 crop 이미지를 만들어 OCR 학습데이터로 ZIP에 담는다.

        pypdfium2가 없으면(옵셔널 의존성) 조용히 아무것도 쓰지 않고 돌아간다 — OCR 학습데이터
        없이도 레이아웃 학습데이터만으로 ZIP을 만들 수 있어야 하기 때문이다. pypdfium2는
        PyMuPDF의 `clip=Rect` 같은 임의 영역 렌더링을 지원하지 않으므로, 페이지 전체를 한 번만
        렌더링해(페이지당 캐시) PIL로 필요한 영역만 잘라낸다 — 같은 페이지의 블록이 여러 개여도
        페이지 렌더링은 페이지 수만큼만 일어난다.
        """
        try:
            import pypdfium2  # type: ignore[import-not-found]
        except ImportError:
            return
        pdf = pypdfium2.PdfDocument(document.source_path)
        page_images: dict[int, object] = {}
        try:
            for block in blocks:
                if block.bbox is None or not block.effective_text.strip():
                    continue
                page_index = block.page - 1
                if page_index < 0 or page_index >= len(pdf):
                    continue
                page_image = page_images.get(page_index)
                if page_image is None:
                    bitmap = pdf[page_index].render(scale=2.0, draw_annots=False)
                    page_image = bitmap.to_pil().convert("RGB")
                    page_images[page_index] = page_image
                x1, y1, x2, y2 = block.bbox
                crop = page_image.crop(
                    (round(x1 * 2), round(y1 * 2), round(x2 * 2), round(y2 * 2))
                )
                buffer = BytesIO()
                crop.save(buffer, format="PNG")
                image_name = f"ocr/images/{document.document_id}-{block.block_id}.png"
                archive.writestr(image_name, buffer.getvalue())
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
        finally:
            pdf.close()

    def _select_backend(
        self,
        selected: str,
    ) -> tuple[str, list[str]]:
        """업로드 시 backend="auto"를 실제 backend 이름으로 확정한다(현재는 항상 paddle)."""
        warnings: list[str] = []
        if selected != "auto":
            return selected, warnings
        return "paddle", warnings

    def _analyze(
        self,
        source_path: Path,
        backend: str,
    ) -> DocumentLayout:
        """단계형(staged) OCR을 지원하지 않는 백엔드용 단순 경로: 분석을 한 번에 끝낸다."""
        return get_backend(backend).analyze(str(source_path))

    def _render_pages(self, source_path: Path, directory: Path) -> list[ReviewPage]:
        """검수 화면 배경 이미지로 쓸 PNG를 페이지마다 렌더링해 문서 디렉터리에 저장한다.

        렌더링 해상도는 `review_render_dpi`(72dpi 기준 배율)로 조절한다. 여기서 저장된
        image_width/image_height가 학습데이터 export 시 PDF 좌표 → 이미지 픽셀 좌표 변환의
        기준이 된다.
        """
        try:
            import pypdfium2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError("PDF 검수 화면에는 pypdfium2가 필요합니다.") from exc
        scale = self.settings.review_render_dpi / 72.0
        pages: list[ReviewPage] = []
        document = pypdfium2.PdfDocument(source_path)
        try:
            for index, page in enumerate(document, start=1):
                image_name = f"page-{index:04d}.png"
                width, height = page.get_size()
                bitmap = page.render(scale=scale, draw_annots=False)
                image = bitmap.to_pil().convert("RGB")
                image.save(directory / image_name)
                pages.append(
                    ReviewPage(
                        page=index,
                        width=float(width),
                        height=float(height),
                        image_name=image_name,
                        image_width=image.width,
                        image_height=image.height,
                    )
                )
        finally:
            document.close()
        return pages


def _clip_bbox(
    bbox: tuple[float, float, float, float] | None,
    pages: list[ReviewPage],
    page_number: int,
) -> tuple[float, float, float, float] | None:
    """좌표를 해당 페이지 크기 안으로 clamp하고, 찌그러져 폭/높이가 0 이하가 되면 None을 반환한다.

    모델 검출 결과나 사람이 그린 박스가 페이지 경계를 살짝 벗어나는 경우를 방어하기 위함이다
    (예: 레이아웃 모델이 페이지 여백 밖까지 박스를 그리는 경우).
    """
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
    """PDF 좌표계(pt) bbox를 렌더링된 PNG의 픽셀 좌표로 변환한다.

    학습데이터의 layout/annotations.jsonl은 이미지 파일 기준 좌표를 기대하므로, 저장된 PDF
    좌표(page.width/height 기준)를 실제 이미지 크기(image_width/image_height) 비율만큼
    스케일링해서 내보낸다.
    """
    if bbox is None:
        return None
    scale_x = (page.image_width or page.width) / page.width
    scale_y = (page.image_height or page.height) / page.height
    x1, y1, x2, y2 = bbox
    return (x1 * scale_x, y1 * scale_y, x2 * scale_x, y2 * scale_y)
