from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from paperrag.config import get_settings
from paperrag.review.models import BlockCreate, BlockUpdate, IngestedDocument, ReviewDocument
from paperrag.review.service import InvalidPdfError, ReviewService
from paperrag.review.store import DocumentNotFoundError
from paperrag.review.viewer import build_viewer_html

router = APIRouter(tags=["document-review"])


@lru_cache
def get_review_service() -> ReviewService:
    return ReviewService(get_settings())


ReviewDependency = Annotated[ReviewService, Depends(get_review_service)]


@router.post("/documents", response_model=ReviewDocument)
async def upload_document(
    request: Request,
    service: ReviewDependency,
    filename: str = Query(default="uploaded.pdf"),
    backend: str | None = Query(default=None),
) -> ReviewDocument:
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
    return service.list()


@router.get("/documents/{document_id}", response_model=ReviewDocument)
async def get_document(document_id: str, service: ReviewDependency) -> ReviewDocument:
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
    try:
        return HTMLResponse(build_viewer_html(service.get(document_id), editable=editable))
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.get("/documents/{document_id}/pages/{page}/image")
async def page_image(document_id: str, page: int, service: ReviewDependency) -> FileResponse:
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
    try:
        return service.approve_all(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.post("/documents/{document_id}/deduplicate-layout", response_model=ReviewDocument)
async def deduplicate_layout(document_id: str, service: ReviewDependency) -> ReviewDocument:
    try:
        return service.deduplicate_layout(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/run-ocr", response_model=ReviewDocument)
async def run_reviewed_ocr(document_id: str, service: ReviewDependency) -> ReviewDocument:
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
    try:
        return service.reevaluate_automatic_quality(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/confirm-ocr", response_model=ReviewDocument)
async def confirm_ocr(document_id: str, service: ReviewDependency) -> ReviewDocument:
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
    try:
        return service.return_to_layout_review(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents/{document_id}/ingest", response_model=IngestedDocument)
async def ingest_document(document_id: str, service: ReviewDependency) -> IngestedDocument:
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
    content = await run_in_threadpool(
        service.export_training_zip,
        include_unreviewed=include_unreviewed,
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={"content-disposition": 'attachment; filename="paperrag-training-data.zip"'},
    )
