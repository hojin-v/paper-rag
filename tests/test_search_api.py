import asyncio
import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from paperrag.config import Settings, get_settings
from paperrag.search.api import app, get_service
from paperrag.search.repository import InMemorySearchRepository
from paperrag.search.schemas import KeywordCandidate
from paperrag.search.service import SearchService


if importlib.util.find_spec("httpx2") is None:

    class TestClient:
        __test__ = False

        def __init__(self, asgi_app: Any) -> None:
            self.asgi_app = asgi_app

        def __enter__(self) -> "TestClient":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            return asyncio.run(self._request("GET", url, **kwargs))

        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            return asyncio.run(self._request("POST", url, **kwargs))

        async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            transport = httpx.ASGITransport(app=self.asgi_app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.request(method, url, **kwargs)
                await response.aread()
                return response

else:
    from fastapi.testclient import TestClient


class PromptAwareLLM:
    def generate_json(self, prompt: str, schema_hint: str, operation: str = "") -> dict[str, Any]:
        if "RAG" in prompt:
            return {"keywords": ["RAG"]}
        return {"keywords": ["unknown"]}


class StaticEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = {
            "rag": [1.0, 0.0],
            "unknown": [1.0, 0.0],
            "ocr": [0.9, 0.1],
            "vector search": [0.8, 0.2],
        }
        return [vectors.get(text.lower(), [0.0, 1.0]) for text in texts]


@pytest.fixture
def client_with_service(tmp_path: Path) -> Iterator[tuple[TestClient, SearchService]]:
    service = _service(tmp_path)

    async def override_service() -> SearchService:
        return service

    app.dependency_overrides[get_service] = override_service
    with TestClient(app) as client:
        yield client, service
    app.dependency_overrides.clear()


def test_health(client_with_service: tuple[TestClient, SearchService]) -> None:
    client, _ = client_with_service

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_search_requires_api_key_when_configured(
    client_with_service: tuple[TestClient, SearchService],
) -> None:
    """PAPERRAG_API_KEY가 설정되면 /search가 401을 내고, 올바른 헤더면 통과해야 한다.

    require_api_key가 Depends(get_settings)로 settings를 주입받으므로
    app.dependency_overrides[get_settings]로 API 키가 설정된 상황을 재현할 수 있다
    (실제 라우터에 인증이 제대로 연결됐는지 앱 전체 경로로 확인 — 단위 테스트는
    tests/test_auth.py 참고). /health는 인증 대상이 아니므로 그대로 통과해야 한다.
    """
    client, _ = client_with_service

    async def override_settings() -> Settings:
        return Settings(_env_file=None, api_key="secret")

    app.dependency_overrides[get_settings] = override_settings
    try:
        unauthenticated = client.post("/search", json={"query": "RAG 관련 논문"})
        assert unauthenticated.status_code == 401

        authenticated = client.post(
            "/search",
            json={"query": "RAG 관련 논문"},
            headers={"X-API-Key": "secret"},
        )
        assert authenticated.status_code == 200

        still_open = client.get("/health")
        assert still_open.status_code == 200
    finally:
        del app.dependency_overrides[get_settings]


def test_search_matched_and_excel_download(
    client_with_service: tuple[TestClient, SearchService],
) -> None:
    client, _ = client_with_service

    response = client.post("/search", json={"query": "RAG 관련 논문"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "matched"
    assert body["match_type"] == "exact"
    assert body["primary_paper"]["paper_id"] == 10

    excel_response = client.get(f"/result/{body['result_id']}/excel")
    assert excel_response.status_code == 200
    assert excel_response.headers["content-disposition"].startswith(
        f'attachment; filename="paper-search-{body["result_id"]}.xlsx"'
    )
    assert excel_response.content.startswith(b"PK")


def test_search_response_exposes_available_sections_and_accepts_include_abstract(
    client_with_service: tuple[TestClient, SearchService],
) -> None:
    """UI가 자유 텍스트 대신 드롭다운을 채울 수 있도록 실제 섹션 목록이 응답에 담겨야 한다."""
    client, _ = client_with_service

    response = client.post(
        "/search",
        json={"query": "RAG 관련 논문", "include_abstract": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["available_sections"] == ["Intro", "Related"]


def test_search_suggest_and_select(client_with_service: tuple[TestClient, SearchService]) -> None:
    client, _ = client_with_service

    suggest_response = client.post("/search", json={"query": "unknown"})

    assert suggest_response.status_code == 200
    suggestion = suggest_response.json()
    assert suggestion["status"] == "suggest"
    assert len(suggestion["candidates"]) == 2

    select_response = client.post(
        "/search/select",
        json={
            "session_id": suggestion["session_id"],
            "keyword_id": suggestion["candidates"][0]["keyword_id"],
        },
    )
    assert select_response.status_code == 200
    selected = select_response.json()
    assert selected["status"] == "matched"
    assert selected["match_type"] == "selected"
    assert selected["related_paper"]["paper_id"] == 30


def test_expired_session_returns_404(
    client_with_service: tuple[TestClient, SearchService],
) -> None:
    client, service = client_with_service
    session = service.sessions.create(
        "unknown",
        [KeywordCandidate(keyword_id=1, keyword="RAG", similarity=1.0)],
    )
    service.sessions.expire(session.session_id)

    response = client.post(
        "/search/select",
        json={"session_id": session.session_id, "keyword_id": 1},
    )

    assert response.status_code == 404


def _service(tmp_path: Path) -> SearchService:
    settings = Settings(
        _env_file=None,
        result_dir=tmp_path,
        search_suggestion_limit=3,
        search_similarity_threshold=0.6,
        embed_dim=2,
    )
    return SearchService(_repo(), PromptAwareLLM(), StaticEmbeddingClient(), settings)


def _repo() -> InMemorySearchRepository:
    current_year = datetime.now(UTC).year
    return InMemorySearchRepository(
        keywords=[
            {
                "keyword_id": 1,
                "keyword": "rag",
                "display_form": "RAG",
                "frequency": 10,
                "embedding": [1.0, 0.0],
            },
            {
                "keyword_id": 2,
                "keyword": "ocr",
                "display_form": "OCR",
                "frequency": 5,
                "embedding": [0.9, 0.1],
            },
            {
                "keyword_id": 3,
                "keyword": "vector search",
                "display_form": "Vector Search",
                "frequency": 3,
                "embedding": [0.8, 0.2],
            },
        ],
        papers=[
            {
                "paper_id": 10,
                "title": "RAG Retrieval Study",
                "authors": "Kim; Lee",
                "published_year": current_year,
                "journal": "Journal",
                "abstract": "RAG improves search.",
                "abstract_summary": "RAG 검색 개선.",
                "full_text_link": "https://example.test/rag",
            },
            {
                "paper_id": 30,
                "title": "OCR Related Paper",
                "authors": "Choi",
                "published_year": current_year,
                "journal": "Related",
                "abstract": "OCR is related to RAG.",
                "abstract_summary": "OCR 연관 논문.",
            },
        ],
        paper_keywords=[
            {"paper_id": 10, "keyword_id": 1, "score": 0.9},
            {"paper_id": 30, "keyword_id": 2, "score": 0.8},
        ],
        paragraphs=[
            {
                "paper_id": 10,
                "paragraph_order": 1,
                "section_name": "Intro",
                "original_text": "RAG 원문",
                "cleaned_text": "RAG 정제",
                "summary": "RAG 요약",
                "embedding": [1.0, 0.0],
                "keywords": ["RAG"],
            },
            {
                "paper_id": 30,
                "paragraph_order": 1,
                "section_name": "Related",
                "original_text": "OCR 원문",
                "cleaned_text": "OCR 정제",
                "summary": "OCR 요약",
                "embedding": [0.9, 0.1],
                "keywords": ["OCR"],
            },
        ],
        tables=[
            {
                "paper_id": 10,
                "table_title": "Table 1",
                "table_text": "metric | value",
                "table_summary": "표 요약",
            }
        ],
        relations=[
            {
                "source_paper_id": 10,
                "related_paper_id": 30,
                "relation_score": 0.75,
                "relation_reason": "겹치는 키워드: RAG",
            }
        ],
    )
