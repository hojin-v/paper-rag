"""검색·검수 API를 감싸는 얇은 HTTP 클라이언트.

설계 원칙: Streamlit UI(`paperrag.ui.app`)는 이 모듈을 통해서만 백엔드와 통신하며,
PostgreSQL·pgvector·리포지토리 등 데이터 계층에는 직접 접근하지 않는다. 즉 UI 프로세스는
`paperrag.search.api`가 노출하는 FastAPI 엔드포인트만 알면 되고, DB 커넥션 문자열이나
ORM/리포지토리 구현을 알 필요가 없다. 이 클라이언트가 그 경계를 강제하는 유일한 창구다.

각 메서드는 REST 엔드포인트 하나에 대응하며, 응답 JSON을 pydantic 스키마
(`SearchMatched`/`SearchSuggest`/`ReviewDocument`/`IngestedDocument`)로 검증·역직렬화해
호출자(주로 `paperrag.ui.app`)에게 타입이 있는 객체로 돌려준다.
"""

import httpx

from paperrag.review.models import IngestedDocument, ReviewDocument
from paperrag.search.schemas import SearchMatched, SearchSuggest


class ApiUnavailable(RuntimeError):
    """검색 API에 연결할 수 없을 때 UI에서 표시할 예외."""


class ApiClient:
    """검색·검수 API 서버에 대한 httpx 기반 동기 클라이언트.

    `base_url`은 서버 자체를 호출할 때 쓰고, `public_base_url`은 브라우저에서 직접 열어야
    하는 iframe/링크(예: 레이아웃 뷰어) URL을 만들 때 쓴다. 두 URL이 다를 수 있는 이유는
    Streamlit 프로세스가 API에 접근하는 네트워크 경로(예: 컨테이너 내부 주소)와, 사용자의
    브라우저가 접근해야 하는 경로(예: localhost)가 서로 다를 수 있기 때문이다.
    """

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

    def search(
        self,
        query: str,
        *,
        use_llm: bool = False,
        section_query: str | None = None,
        include_related: bool = True,
        include_tables: bool = True,
    ) -> SearchMatched | SearchSuggest:
        """자연어 질의로 `POST /search`를 호출한다.

        DESIGN.md §5.2의 2단계 검색 로직에 대응한다: 서버가 키워드 정확 매칭에 성공하면
        `status="matched"`(대표/연관 논문 확정 결과)를, 실패하면 `status="suggest"`
        (유사 키워드 후보 + 세션 ID, 사용자 선택 대기)를 돌려준다. 이 메서드는 응답의
        `status` 값을 보고 두 스키마 중 어느 쪽으로 역직렬화할지만 결정하며, 실제 매칭
        로직은 API 서버 쪽에 있다.

        기본(use_llm=False)은 서버가 LLM 없이 형태소 분석만으로 키워드를 뽑는 빠른
        경로를 쓴다. use_llm=True면 자연어 이해를 위해 LLM을 호출하지만 훨씬 느리다
        (직렬 처리라 동시 사용자가 있으면 대기 시간이 늘어난다). section_query를
        주면 결과 단락을 그 섹션명을 포함하는 것만으로 좁힌다. include_related=False면
        연관 논문 관련 항목·시트를, include_tables=False면 표 관련 시트를 아예
        만들지 않는다(둘 다 기본은 True — 산출물 구성을 사용자가 좁히는 옵션).
        """
        response = self._request(
            "POST",
            "/search",
            json={
                "query": query,
                "use_llm": use_llm,
                "section_query": section_query,
                "include_related": include_related,
                "include_tables": include_tables,
            },
        )
        body = response.json()
        status = body.get("status")
        if status == "matched":
            return SearchMatched.model_validate(body)
        if status == "suggest":
            return SearchSuggest.model_validate(body)
        raise ValueError(f"알 수 없는 검색 응답 상태입니다: {status!r}")

    def readiness(self) -> dict[str, object]:
        """`GET /ready`를 호출해 실제 OCR·임베딩·LLM·DB 구성요소가 모두 준비됐는지 확인한다.

        FastAPI가 준비 완료(200) 또는 준비되지 않음(503) 상태를 모두 정상적인 JSON
        본문으로 응답하도록 설계돼 있으므로, 이 두 코드는 예외로 취급하지 않고 그대로
        파싱해 호출자(`_render_readiness`)가 `status`/`errors`/`components`를 보고
        화면에 성공·경고 메시지를 그릴 수 있게 한다.
        """
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
        """유사 키워드 제안 화면에서 사용자가 고른 키워드로 `POST /search/select`를 호출한다.

        `search()`가 `suggest` 상태를 반환했을 때 함께 온 `session_id`와, 사용자가 선택한
        `keyword_id`를 그대로 서버에 전달해 확정 검색 결과(대표/연관 논문)를 받는다.
        """
        response = self._request(
            "POST",
            "/search/select",
            json={"session_id": session_id, "keyword_id": keyword_id},
        )
        return SearchMatched.model_validate(response.json())

    def download_excel(self, result_id: str) -> bytes:
        """검색 결과의 `result_id`로 6시트 엑셀(.xlsx) 바이트를 내려받는다."""
        response = self._request("GET", f"/result/{result_id}/excel")
        return response.content

    def upload_document(
        self,
        filename: str,
        content: bytes,
        backend: str = "paddle",
    ) -> ReviewDocument:
        """PDF 원본 바이트를 업로드해 STEP 1~2(이미지화·레이아웃 검출)만 실행시킨다.

        아직 영역별 OCR은 수행하지 않은 `ReviewDocument`(phase="layout_review")를 돌려주며,
        이후 레이아웃을 사람이 확인한 뒤 `run_automatic_ocr`로 다음 단계를 진행한다.
        """
        response = self._request(
            "POST",
            "/documents",
            params={"filename": filename, "backend": backend},
            content=content,
            headers={"content-type": "application/pdf"},
        )
        return ReviewDocument.model_validate(response.json())

    def list_documents(self) -> list[ReviewDocument]:
        """검수 대시보드에 표시할 전체 문서(레이아웃/OCR 검수 대상) 목록을 가져온다."""
        response = self._request("GET", "/documents")
        return [ReviewDocument.model_validate(item) for item in response.json()]

    def get_document(self, document_id: str) -> ReviewDocument:
        """단일 문서의 최신 상태(phase·블록·품질 지표 포함)를 다시 조회한다."""
        response = self._request("GET", f"/documents/{document_id}")
        return ReviewDocument.model_validate(response.json())

    def ingest_document(self, document_id: str) -> IngestedDocument:
        """검수를 통과한 문서를 STEP 4~8(단락화·LLM 정제·임베딩·적재)까지 실행해 DB에 저장한다."""
        response = self._request("POST", f"/documents/{document_id}/ingest")
        return IngestedDocument.model_validate(response.json())

    def approve_all_blocks(self, document_id: str) -> ReviewDocument:
        """관리자가 문서의 모든 블록을 일괄 승인 처리한다(개별 검수를 생략하는 지름길)."""
        response = self._request("POST", f"/documents/{document_id}/approve-all")
        return ReviewDocument.model_validate(response.json())

    def deduplicate_layout(self, document_id: str) -> ReviewDocument:
        """자동 레이아웃 검출에서 중복·컨테이너 박스를 제거해 다시 정리한다."""
        response = self._request("POST", f"/documents/{document_id}/deduplicate-layout")
        return ReviewDocument.model_validate(response.json())

    def run_document_ocr(self, document_id: str) -> ReviewDocument:
        """(관리자용) 현재 레이아웃 박스 기준으로 OCR만 다시 실행한다."""
        response = self._request("POST", f"/documents/{document_id}/run-ocr")
        return ReviewDocument.model_validate(response.json())

    def run_automatic_ocr(self, document_id: str) -> ReviewDocument:
        """레이아웃 검수 완료 문서에 영역별 OCR과 자동 품질 판정을 실행한다.

        결과 문서는 자동 품질 기준을 통과하면 phase="ready_to_ingest"(바로 적재 가능),
        통과하지 못하면 phase="ocr_review"(관리자 검수 필요)로 전환된다.
        """
        response = self._request("POST", f"/documents/{document_id}/auto-ocr")
        return ReviewDocument.model_validate(response.json())

    def confirm_document_ocr(self, document_id: str) -> ReviewDocument:
        """관리자가 OCR 품질 예외를 직접 확인한 뒤 적재 가능 상태로 확정한다."""
        response = self._request("POST", f"/documents/{document_id}/confirm-ocr")
        return ReviewDocument.model_validate(response.json())

    def return_to_layout_review(self, document_id: str) -> ReviewDocument:
        """OCR 품질 예외 문서를 레이아웃 검수 단계(phase="layout_review")로 되돌린다."""
        response = self._request("POST", f"/documents/{document_id}/return-to-layout")
        return ReviewDocument.model_validate(response.json())

    def viewer_url(self, document_id: str, *, editable: bool = False) -> str:
        """레이아웃·OCR 뷰어(iframe 또는 새 창 링크)의 URL을 만든다.

        `editable=True`면 관리자 교정 모드(영역 유형·좌표·원문 편집 가능) URL이 되고,
        `editable=False`면 읽기 전용 오버레이(iframe 표시용) URL이 된다. `public_base_url`을
        기준으로 만들어야 사용자의 브라우저가 실제로 열 수 있는 주소가 된다.
        """
        editable_value = "true" if editable else "false"
        return (
            f"{self._public_base_url}/documents/{document_id}/viewer"
            f"?editable={editable_value}"
        )

    def download_training_data(self, *, include_unreviewed: bool = False) -> bytes:
        """모델 개선용 학습 데이터(검수 완료 블록, 옵션에 따라 미검수 블록도 포함)를 내려받는다."""
        response = self._request(
            "GET",
            "/training/export",
            params={"include_unreviewed": str(include_unreviewed).lower()},
        )
        return response.content

    def close(self) -> None:
        """이 클라이언트가 직접 생성한 httpx.Client만 닫는다.

        외부에서 `http_client`를 주입받은 경우(`_owns_client=False`)는 소유권이 없으므로
        여기서 닫지 않는다(호출자가 그 클라이언트의 생명주기를 관리해야 하기 때문).
        """
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """실제 HTTP 요청을 보내는 공통 헬퍼.

        API 서버가 아예 떠 있지 않아 접속 자체가 실패하는 경우(`httpx.ConnectError`)를
        붙잡아, UI가 이해할 수 있는 `ApiUnavailable`(uvicorn 기동 안내 메시지 포함)로
        바꿔 다시 던진다. 그 외 HTTP 오류(4xx/5xx)는 `raise_for_status()`가 그대로
        `httpx.HTTPError`로 전파하며 이 메서드에서 별도로 감싸지 않는다.
        """
        try:
            response = self._client.request(method, f"{self._base_url}{path}", **kwargs)
            response.raise_for_status()
            return response
        except httpx.ConnectError as exc:
            raise ApiUnavailable(
                "검색 API 서버에 연결할 수 없습니다. "
                "`uvicorn paperrag.search.api:app` 명령으로 API를 먼저 기동하세요."
            ) from exc
