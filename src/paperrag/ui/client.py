import httpx

from paperrag.search.schemas import SearchMatched, SearchSuggest


class ApiUnavailable(RuntimeError):
    """검색 API에 연결할 수 없을 때 UI에서 표시할 예외."""


class ApiClient:
    def __init__(self, base_url: str, http_client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = http_client or httpx.Client(timeout=30.0)
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
