"""검수(review) REST API 라우터.

여기 있는 엔드포인트들은 `ReviewService`가 구현하는 검수 상태 기계
(layout_review → ocr_review → ready_to_ingest)를 그대로 HTTP로 노출한다. 이 라우터 자체는
상태 전이 조건을 검증하지 않으며, 그 책임은 전부 `ReviewService`에 있다 — 여기서는
서비스가 던지는 예외(DocumentNotFoundError/ValueError/ImportError 등)를 적절한 HTTP 상태
코드로 변환하는 역할만 한다. 무거운 작업(레이아웃 검출, OCR, PDF 렌더링, DB 적재)은
`run_in_threadpool`로 감싸 FastAPI의 비동기 이벤트 루프를 막지 않게 한다.
"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from paperrag.auth import require_api_key
from paperrag.config import get_settings
from paperrag.review.models import BlockCreate, BlockUpdate, IngestedDocument, ReviewDocument
from paperrag.review.service import InvalidPdfError, ReviewService
from paperrag.review.store import DocumentNotFoundError
from paperrag.review.viewer import build_viewer_html

# dependencies=[...]를 라우터 전체에 걸어, 검수 관련 엔드포인트 전부(문서 업로드·
# 조회·뷰어·블록 편집·적재 등)를 하나의 지점에서 일괄 인증 대상으로 삼는다.
# PAPERRAG_API_KEY가 설정되지 않은 기본 상태에서는 require_api_key가 즉시
# 통과시키므로 로컬 개발 흐름에는 영향이 없다.
router = APIRouter(tags=["document-review"], dependencies=[Depends(require_api_key)])


@lru_cache
def get_review_service() -> ReviewService:
    """요청마다 새로 만들지 않고 프로세스 안에서 하나의 ReviewService(=하나의 FileReviewStore)를 재사용한다."""
    return ReviewService(get_settings())


ReviewDependency = Annotated[ReviewService, Depends(get_review_service)]


@router.post("/documents", response_model=ReviewDocument)
async def upload_document(
    request: Request,
    service: ReviewDependency,
    filename: str = Query(default="uploaded.pdf"),
    backend: str | None = Query(default=None),
) -> ReviewDocument:
    """PDF를 업로드해 레이아웃 검출까지 실행하고 검수 문서를 생성한다.

    성공하면 문서는 `layout_review` phase로 시작한다(단계형 백엔드가 아니면 바로
    `ready_to_ingest`). 업로드 바이트는 요청 바디에서 직접 읽는다(멀티파트가 아님).
    """
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > service.settings.review_max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF 업로드 크기 제한을 초과했습니다.")
    content = await request.body()
    try:
        selected_backend = backend or service.settings.review_default_backend
        return await run_in_threadpool(service.upload, filename, content, selected_backend)
    except (InvalidPdfError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"레이아웃/OCR 분석 실패: {exc}") from exc


@router.get("/documents", response_model=list[ReviewDocument])
async def list_documents(service: ReviewDependency) -> list[ReviewDocument]:
    """모든 검수 문서를 최신순으로 나열한다(운영자 대시보드/목록 화면용)."""
    return service.list()


@router.get("/documents/{document_id}", response_model=ReviewDocument)
async def get_document(document_id: str, service: ReviewDependency) -> ReviewDocument:
    """검수 문서의 현재 전체 상태(JSON)를 조회한다."""
    try:
        return service.get(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.get("/documents/{document_id}/viewer", response_class=HTMLResponse)
async def document_viewer(
    document_id: str,
    service: ReviewDependency,
    editable: bool = Query(default=False),
) -> HTMLResponse:
    """서버사이드 HTML 검수 뷰어를 반환한다.

    `editable=false`(기본값)는 운영자용 읽기 전용 자동 처리 품질 모니터, `editable=true`는
    관리자가 좌표·유형·텍스트를 직접 교정하는 화면이다. 실제 HTML/SVG/JS 생성은
    `viewer.build_viewer_html`이 담당한다.
    """
    try:
        return HTMLResponse(build_viewer_html(service.get(document_id), editable=editable))
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.get("/documents/{document_id}/pages/{page}/image")
async def page_image(document_id: str, page: int, service: ReviewDependency) -> FileResponse:
    """뷰어가 배경 이미지로 사용하는 페이지 렌더링 PNG를 반환한다."""
    try:
        path = service.store.page_image_path(document_id, page)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="page not found") from exc
    return FileResponse(path, media_type="image/png")


@router.put("/documents/{document_id}/blocks/{block_id}", response_model=ReviewDocument)
async def update_block(
    document_id: str,
    block_id: str,
    update: BlockUpdate,
    service: ReviewDependency,
) -> ReviewDocument:
    """블록 하나의 유형·좌표·교정 텍스트·검수 상태를 갱신한다.

    실제로 어떤 필드를 바꿀 수 있는지는 문서의 현재 phase에 따라 `ReviewService.update_block`이
    검증한다(좌표·유형은 layout_review 단계에서만, 텍스트 교정은 OCR 실행 후에만 허용).
    """
    try:
        return service.update_block(document_id, block_id, update)
    except (DocumentNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="block not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/blocks", response_model=ReviewDocument)
async def add_block(
    document_id: str,
    create: BlockCreate,
    service: ReviewDependency,
) -> ReviewDocument:
    """레이아웃 검수 단계에서 누락된 영역을 사람이 좌표를 그려 새로 추가한다."""
    try:
        return service.add_block(document_id, create)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/documents/{document_id}/blocks/{block_id}",
    response_model=ReviewDocument,
)
async def delete_block(
    document_id: str,
    block_id: str,
    service: ReviewDependency,
) -> ReviewDocument:
    """레이아웃 검수 단계에서 잘못 검출된 영역을 삭제한다(문서 warnings에 이력이 남는다)."""
    try:
        return service.delete_block(document_id, block_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="block not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/approve-all", response_model=ReviewDocument)
async def approve_all(document_id: str, service: ReviewDependency) -> ReviewDocument:
    """검수 상태가 unreviewed인 모든 블록을 일괄 approved로 바꾼다(phase는 바꾸지 않음)."""
    try:
        return service.approve_all(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.post("/documents/{document_id}/deduplicate-layout", response_model=ReviewDocument)
async def deduplicate_layout(document_id: str, service: ReviewDependency) -> ReviewDocument:
    """겹치는(IoU 0.85 이상) 자동 검출 박스와 다른 영역을 감싸는 오검출 컨테이너를 정리한다.

    사람이 직접 그린 박스(detected_bbox가 없는 블록)는 정리 대상에서 제외된다.
    """
    try:
        return service.deduplicate_layout(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/run-ocr", response_model=ReviewDocument)
async def run_reviewed_ocr(document_id: str, service: ReviewDependency) -> ReviewDocument:
    """사람이 승인·교정을 마친 레이아웃 박스만 crop해 영역별 OCR을 실행한다.

    layout_review phase에서만 호출할 수 있고, 성공하면 문서는 ocr_review phase로 전이한다.
    미검수(unreviewed) 블록이 남아 있으면 먼저 승인·교정·제외하라는 400 오류를 반환한다.
    """
    try:
        return await run_in_threadpool(service.run_reviewed_ocr, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"영역별 OCR 실패: {exc}") from exc


@router.post("/documents/{document_id}/auto-ocr", response_model=ReviewDocument)
async def run_automatic_ocr(document_id: str, service: ReviewDependency) -> ReviewDocument:
    """사람의 개별 승인 없이 레이아웃을 전부 승인 처리한 뒤 OCR과 자동 품질 판정까지 연속 실행한다.

    자동 품질이 합격이면 ready_to_ingest로, 불합격이면 ocr_review에 남아 관리자 예외
    대기열로 넘어간다(자세한 판정 기준은 service.ReviewService._automation_quality 참고).
    """
    try:
        return await run_in_threadpool(service.run_automatic_ocr, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"자동 영역별 OCR 실패: {exc}") from exc


@router.post(
    "/documents/{document_id}/reevaluate-quality",
    response_model=ReviewDocument,
)
async def reevaluate_automatic_quality(
    document_id: str,
    service: ReviewDependency,
) -> ReviewDocument:
    """기존 OCR 결과를 유지한 채 자동 품질 기준만 다시 계산한다.

    관리자가 블록을 교정한 뒤, 처음부터 다시 OCR을 돌리지 않고 품질 판정만 갱신하고 싶을 때
    사용한다(예: 빈 제목 블록을 손으로 채운 뒤 재판정).
    """
    try:
        return service.reevaluate_automatic_quality(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/confirm-ocr", response_model=ReviewDocument)
async def confirm_ocr(document_id: str, service: ReviewDependency) -> ReviewDocument:
    """사람이 OCR 검수를 마쳤음을 확정해 ready_to_ingest phase로 전이시킨다.

    ocr_review phase에서만 가능하며 unreviewed 블록이 남아 있으면 거부된다.
    """
    try:
        return service.confirm_ocr(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/return-to-layout", response_model=ReviewDocument)
async def return_to_layout_review(
    document_id: str,
    service: ReviewDependency,
) -> ReviewDocument:
    """OCR 검수 중 레이아웃 문제를 발견했을 때, 기존 OCR 결과를 폐기하고 layout_review로 되돌린다."""
    try:
        return service.return_to_layout_review(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/ingest", response_model=IngestedDocument)
async def ingest_document(document_id: str, service: ReviewDependency) -> IngestedDocument:
    """ready_to_ingest phase의 문서를 실제 PostgreSQL+pgvector 파이프라인으로 적재한다."""
    try:
        return await run_in_threadpool(service.ingest, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/training/export")
async def export_training_data(
    service: ReviewDependency,
    include_unreviewed: bool = Query(default=False),
) -> Response:
    """검수된(기본값) 또는 미검수 포함 전체 데이터를 Colab 학습용 ZIP으로 묶어 반환한다.

    ZIP 구성(layout/annotations.jsonl, ocr/labels.jsonl, manifest.json 등)은
    `service.ReviewService.export_training_zip` docstring 참고.
    """
    content = await run_in_threadpool(
        service.export_training_zip,
        include_unreviewed=include_unreviewed,
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={"content-disposition": 'attachment; filename="paperrag-training-data.zip"'},
    )
