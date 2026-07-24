import json

import httpx
import pytest

from paperrag.search.schemas import SearchMatched, SearchSuggest
from paperrag.ui.client import ApiClient, ApiUnavailable


def test_search_parses_matched_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/search"
        assert json.loads(request.content) == {
            "query": "RAG 논문",
            "primary_section_query": None,
            "related_section_query": None,
            "include_related": True,
            "include_tables": True,
            "include_abstract": True,
        }
        return httpx.Response(200, json=_matched_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ApiClient("http://api.test", http_client=http_client)

        result = client.search("RAG 논문")

    assert isinstance(result, SearchMatched)
    assert result.status == "matched"
    assert result.match_type == "exact"
    assert result.primary_paper.title == "RAG Retrieval Study"


def test_search_suggest_then_select_flow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "status": "suggest",
                    "session_id": "session-1",
                    "candidates": [
                        {"keyword_id": 10, "keyword": "RAG", "similarity": 0.91},
                        {"keyword_id": 20, "keyword": "Vector Search", "similarity": 0.83},
                        {"keyword_id": 30, "keyword": "OCR", "similarity": 0.72},
                    ],
                },
            )
        if request.url.path == "/search/select":
            assert json.loads(request.content) == {
                "session_id": "session-1",
                "keyword_id": 20,
            }
            return httpx.Response(200, json=_matched_body(match_type="selected"))
        raise AssertionError(f"unexpected path: {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ApiClient("http://api.test/", http_client=http_client)

        suggestion = client.search("의미 검색")
        assert isinstance(suggestion, SearchSuggest)
        assert suggestion.session_id == "session-1"
        assert [candidate.keyword_id for candidate in suggestion.candidates] == [10, 20, 30]

        result = client.select(suggestion.session_id, 20)

    assert result.status == "matched"
    assert result.match_type == "selected"


def test_download_excel_returns_bytes() -> None:
    xlsx_bytes = b"PK\x03\x04excel"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/result/result-1/excel"
        return httpx.Response(200, content=xlsx_bytes)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ApiClient("http://api.test", http_client=http_client)

        content = client.download_excel("result-1")

    assert content == xlsx_bytes


def test_readiness_returns_not_ready_payload_without_hiding_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ready"
        return httpx.Response(
            503,
            json={
                "status": "not_ready",
                "errors": ["python_module:paddle"],
                "components": {},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ApiClient("http://api.test", http_client=http_client)

        report = client.readiness()

    assert report["status"] == "not_ready"
    assert report["errors"] == ["python_module:paddle"]


def test_upload_document_and_viewer_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/documents"
        assert request.url.params["backend"] == "simple"
        assert request.content == b"%PDF-test"
        return httpx.Response(200, json=_review_document_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ApiClient("http://api.test/", http_client=http_client)
        document = client.upload_document("테스트.pdf", b"%PDF-test", "simple")

    assert document.document_id == "a" * 32
    assert client.viewer_url(document.document_id).endswith(
        f"/documents/{document.document_id}/viewer?editable=false"
    )
    assert client.viewer_url(document.document_id, editable=True).endswith(
        f"/documents/{document.document_id}/viewer?editable=true"
    )


def test_connect_error_is_converted_to_api_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ApiClient("http://api.test", http_client=http_client)

        with pytest.raises(ApiUnavailable) as exc_info:
            client.search("RAG 논문")

    assert "검색 API 서버에 연결할 수 없습니다" in str(exc_info.value)
    assert "uvicorn paperrag.search.api:app" in str(exc_info.value)


def _matched_body(match_type: str = "exact") -> dict[str, object]:
    return {
        "status": "matched",
        "matched_keyword": "RAG",
        "match_type": match_type,
        "result_id": "result-1",
        "primary_paper": {
            "paper_id": 1,
            "title": "RAG Retrieval Study",
            "authors": "Kim; Lee",
            "published_year": 2025,
            "journal": "Journal of Search",
            "keywords": ["RAG"],
            "score": 0.91,
            "reason": "대표 점수 최고",
        },
        "related_paper": {
            "paper_id": 2,
            "title": "Related Search Study",
            "authors": "Choi",
            "published_year": 2024,
            "journal": "Related Journal",
            "keywords": ["RAG", "OCR"],
            "score": 0.77,
            "reason": "겹치는 키워드: RAG",
        },
    }


def _review_document_body() -> dict[str, object]:
    return {
        "document_id": "a" * 32,
        "filename": "테스트.pdf",
        "source_path": "/tmp/source.pdf",
        "pdf_kind": "digital",
        "backend": "simple",
        "status": "analyzed",
        "pages": [{"page": 1, "width": 400, "height": 500, "image_name": "page.png"}],
        "blocks": [],
        "warnings": [],
        "created_at": "2026-07-12T00:00:00Z",
        "updated_at": "2026-07-12T00:00:00Z",
    }
