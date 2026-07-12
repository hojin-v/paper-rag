import httpx

from paperrag.review.models import IngestedDocument, ReviewDocument
from paperrag.search.schemas import SearchMatched, SearchSuggest


class ApiUnavailable(RuntimeError):
    """검색 API에 연결할 수 없을 때 UI에서 표시할 예외."""


class ApiClient:
    def __init__(
        self,
        base_url: str,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 600.0,
        public_base_url: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._public_base_url = (public_base_url or base_url).rstrip("/")
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = http_client is None

    def search(self, query: str) -> SearchMatched | SearchSuggest:
        response = self._request("POST", "/search", json={"query": query})
        body = response.json()
        status = body.get("status")
        if status == "matched":
            return SearchMatched.model_validate(body)
        if status == "suggest":
            return SearchSuggest.model_validate(body)
        raise ValueError(f"알 수 없는 검색 응답 상태입니다: {status!r}")

    def readiness(self) -> dict[str, object]:
        try:
            response = self._client.get(f"{self._base_url}/ready")
        except httpx.ConnectError as exc:
            raise ApiUnavailable("API 준비 상태를 확인할 수 없습니다.") from exc
        if response.status_code not in {200, 503}:
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("준비 상태 응답은 JSON object여야 합니다.")
        return payload

    def select(self, session_id: str, keyword_id: int) -> SearchMatched:
        response = self._request(
            "POST",
            "/search/select",
            json={"session_id": session_id, "keyword_id": keyword_id},
        )
        return SearchMatched.model_validate(response.json())

    def download_excel(self, result_id: str) -> bytes:
        response = self._request("GET", f"/result/{result_id}/excel")
        return response.content

    def upload_document(
        self,
        filename: str,
        content: bytes,
        backend: str = "paddle",
    ) -> ReviewDocument:
        response = self._request(
            "POST",
            "/documents",
            params={"filename": filename, "backend": backend},
            content=content,
            headers={"content-type": "application/pdf"},
        )
        return ReviewDocument.model_validate(response.json())

    def list_documents(self) -> list[ReviewDocument]:
        response = self._request("GET", "/documents")
        return [ReviewDocument.model_validate(item) for item in response.json()]

    def get_document(self, document_id: str) -> ReviewDocument:
        response = self._request("GET", f"/documents/{document_id}")
        return ReviewDocument.model_validate(response.json())

    def ingest_document(self, document_id: str) -> IngestedDocument:
        response = self._request("POST", f"/documents/{document_id}/ingest")
        return IngestedDocument.model_validate(response.json())

    def approve_all_blocks(self, document_id: str) -> ReviewDocument:
        response = self._request("POST", f"/documents/{document_id}/approve-all")
        return ReviewDocument.model_validate(response.json())

    def deduplicate_layout(self, document_id: str) -> ReviewDocument:
        response = self._request("POST", f"/documents/{document_id}/deduplicate-layout")
        return ReviewDocument.model_validate(response.json())

    def run_document_ocr(self, document_id: str) -> ReviewDocument:
        response = self._request("POST", f"/documents/{document_id}/run-ocr")
        return ReviewDocument.model_validate(response.json())

    def run_automatic_ocr(self, document_id: str) -> ReviewDocument:
        response = self._request("POST", f"/documents/{document_id}/auto-ocr")
        return ReviewDocument.model_validate(response.json())

    def confirm_document_ocr(self, document_id: str) -> ReviewDocument:
        response = self._request("POST", f"/documents/{document_id}/confirm-ocr")
        return ReviewDocument.model_validate(response.json())

    def return_to_layout_review(self, document_id: str) -> ReviewDocument:
        response = self._request("POST", f"/documents/{document_id}/return-to-layout")
        return ReviewDocument.model_validate(response.json())

    def viewer_url(self, document_id: str, *, editable: bool = False) -> str:
        editable_value = "true" if editable else "false"
        return (
            f"{self._public_base_url}/documents/{document_id}/viewer"
            f"?editable={editable_value}"
        )

    def download_training_data(self, *, include_unreviewed: bool = False) -> bytes:
        response = self._request(
            "GET",
            "/training/export",
            params={"include_unreviewed": str(include_unreviewed).lower()},
        )
        return response.content

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.request(method, f"{self._base_url}{path}", **kwargs)
            response.raise_for_status()
            return response
        except httpx.ConnectError as exc:
            raise ApiUnavailable(
                "검색 API 서버에 연결할 수 없습니다. "
                "`uvicorn paperrag.search.api:app` 명령으로 API를 먼저 기동하세요."
            ) from exc
