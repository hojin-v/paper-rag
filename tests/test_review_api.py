import asyncio
from pathlib import Path

import httpx
import pymupdf

from paperrag.config import Settings
from paperrag.review.api import get_review_service
from paperrag.review.service import ReviewService
from paperrag.search.api import app


def test_upload_viewer_and_block_update_api(tmp_path: Path) -> None:
    service = ReviewService(
        Settings(
            _env_file=None,
            review_dir=tmp_path / "review",
            review_render_dpi=72,
            allow_diagnostic_backends=True,
        )
    )
    app.dependency_overrides[get_review_service] = lambda: service
    try:
        upload = _request(
            "POST",
            "/documents",
            params={"filename": "sample.pdf", "backend": "simple"},
            content=_pdf_bytes(),
            headers={"content-type": "application/pdf"},
        )
        assert upload.status_code == 200
        document = upload.json()
        document_id = document["document_id"]
        block_id = document["blocks"][0]["block_id"]

        viewer = _request("GET", f"/documents/{document_id}/viewer")
        assert viewer.status_code == 200
        assert "모델 OCR 원문" in viewer.text
        assert "읽기 전용 모니터" in viewer.text
        assert 'class="read-only"' in viewer.text
        assert "관리자 교정 모드" in viewer.text

        editable_viewer = _request(
            "GET",
            f"/documents/{document_id}/viewer?editable=true",
        )
        assert editable_viewer.status_code == 200
        assert 'class="editable"' in editable_viewer.text
        assert "OCR 이후 단계: OCR 결과와 검수 상태만 수정" in editable_viewer.text

        update = _request(
            "PUT",
            f"/documents/{document_id}/blocks/{block_id}",
            json={"corrected_text": "교정 결과", "review_status": "corrected"},
        )
        assert update.status_code == 200
        assert update.json()["blocks"][0]["corrected_text"] == "교정 결과"
    finally:
        app.dependency_overrides.clear()


def _request(method: str, path: str, **kwargs: object) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def _pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=400, height=500)
    page.insert_text((40, 60), "Clickable Layout", fontsize=18)
    page.insert_text((40, 100), "OCR result", fontsize=11)
    content = document.tobytes()
    document.close()
    return content
