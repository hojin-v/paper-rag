import asyncio
from pathlib import Path
from typing import Any

import httpx
from pdf_fixtures import PdfBuilder
import pytest

from paperrag.config import Settings
from paperrag.review.api import get_review_service
from paperrag.review.service import ReviewService
from paperrag.review.store import InMemoryReviewStore
from paperrag.search.api import app


def _service(settings: Settings) -> ReviewService:
    """실제 Postgres 없이 검수 API를 오프라인으로 테스트하기 위한 헬퍼(test_review_service.py와 동일)."""
    return ReviewService(settings, store=InMemoryReviewStore(settings.review_dir))


def test_upload_viewer_and_block_update_api(tmp_path: Path) -> None:
    service = _service(
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


class _FakeAsyncTask:
    """Celery 태스크의 `.delay(...)`만 흉내 내는 페이크. 브로커(Redis) 없이도 테스트 가능하게 한다."""

    def __init__(self, task_id: str = "fake-task-123") -> None:
        self.id = task_id
        self.calls: list[tuple[Any, ...]] = []

    def delay(self, *args: Any) -> "_FakeAsyncTask":
        self.calls.append(args)
        return self


def test_submit_automatic_ocr_async_returns_task_id_without_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto-ocr/async는 실제 Celery 브로커 연결 없이도 task_id를 즉시 반환해야 한다."""
    import paperrag.worker.app as worker_app

    service = _service(
        Settings(
            _env_file=None,
            review_dir=tmp_path / "review",
            review_render_dpi=72,
            allow_diagnostic_backends=True,
        )
    )
    app.dependency_overrides[get_review_service] = lambda: service
    fake_task = _FakeAsyncTask()
    monkeypatch.setattr(worker_app, "run_automatic_ocr_task", fake_task)
    try:
        upload = _request(
            "POST",
            "/documents",
            params={"filename": "sample.pdf", "backend": "simple"},
            content=_pdf_bytes(),
            headers={"content-type": "application/pdf"},
        )
        document_id = upload.json()["document_id"]

        response = _request("POST", f"/documents/{document_id}/auto-ocr/async")

        assert response.status_code == 200
        assert response.json() == {"task_id": "fake-task-123"}
        assert fake_task.calls == [(document_id,)]

        missing = _request("POST", "/documents/does-not-exist/auto-ocr/async")
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("celery_state", "celery_result", "expected_status", "expected_key"),
    [
        ("PENDING", None, "pending", None),
        ("STARTED", None, "started", None),
        ("SUCCESS", {"document_id": "doc-1", "phase": "ready_to_ingest"}, "success", "result"),
        ("FAILURE", "boom", "failure", "error"),
    ],
)
def test_job_status_reports_celery_states(
    monkeypatch: pytest.MonkeyPatch,
    celery_state: str,
    celery_result: Any,
    expected_status: str,
    expected_key: str | None,
) -> None:
    """GET /jobs/{task_id}가 Celery의 네 가지 상태를 JobStatus로 올바르게 옮기는지 확인한다."""

    class _FakeAsyncResult:
        def __init__(self, task_id: str, app: Any = None) -> None:
            self.state = celery_state
            self.result = celery_result

    monkeypatch.setattr("celery.result.AsyncResult", _FakeAsyncResult)

    response = _request("GET", "/jobs/some-task-id")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected_status
    if expected_key is not None:
        assert body[expected_key] == celery_result


def test_llm_calls_viewer_renders_and_forwards_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(engine: Any, **kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [
            {
                "id": 1,
                "created_at": "2026-07-23T10:00:00+00:00",
                "operation": "paragraph_enrich",
                "model": "qwen2.5:7b-instruct-q4_K_M",
                "prompt": "요약해줘",
                "response": '{"summary":"ok"}',
                "success": True,
                "error": None,
                "latency_ms": 123.0,
                "cache_hit": False,
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "context": None,
            }
        ]

    monkeypatch.setattr("paperrag.review.api.fetch_llm_calls", fake_fetch)

    response = _request(
        "GET",
        "/observability/llm-calls",
        params={"operation": "paragraph_enrich", "success": "true", "limit": "50"},
    )

    assert response.status_code == 200
    assert "paragraph_enrich" in response.text
    assert "성공" in response.text
    assert captured == {"operation": "paragraph_enrich", "success": True, "limit": 50}


def test_llm_calls_viewer_handles_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("paperrag.review.api.fetch_llm_calls", lambda engine, **kwargs: [])

    response = _request("GET", "/observability/llm-calls")

    assert response.status_code == 200
    assert "기록된 호출이 없습니다" in response.text


def _request(method: str, path: str, **kwargs: object) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def _pdf_bytes() -> bytes:
    return (
        PdfBuilder()
        .add_page(400, 500)
        .text(40, 60, "Clickable Layout", fontsize=18)
        .text(40, 100, "OCR result", fontsize=11)
        .build()
    )
