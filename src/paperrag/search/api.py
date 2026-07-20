"""검색 REST API 라우터.

DESIGN.md §5.1이 정의한 2단계 인터랙션을 그대로 노출한다.

```
POST /search          {query}                    -> matched | suggest
POST /search/select   {session_id, keyword_id}   -> matched
GET  /result/{result_id}/excel                    -> xlsx 다운로드
```

SearchService의 예외(SearchSessionNotFound/SearchNoPaperFound/SearchDependencyError)를
HTTP 상태 코드로 변환하는 것 외의 비즈니스 로직은 두지 않는다. 실제 매칭·점수 계산은
search.service.SearchService에, DB 접근은 search.repository에 있다.
"""

import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from paperrag.config import get_settings
from paperrag.review.api import router as review_router
from paperrag.readiness import build_readiness_report
from paperrag.search.repository import PostgresSearchRepository
from paperrag.search.schemas import SearchMatched, SearchRequest, SearchSuggest, SelectRequest
from paperrag.search.service import (
    SearchDependencyError,
    SearchNoPaperFound,
    SearchService,
    SearchSessionNotFound,
)

app = FastAPI(title="Paper RAG Search API")
app.include_router(review_router)

_service: SearchService | None = None


class _InlineFileResponse(FileResponse):
    """FileResponse를 상속해 파일 전체를 메모리로 읽어 한 번에 전송하는 응답.

    starlette 기본 FileResponse는 sendfile/스트리밍 경로를 타는데, 테스트 환경
    (예: TestClient)이나 일부 배포 환경에서 스트리밍 전송이 기대대로 동작하지
    않는 경우가 있어, 엑셀 파일(보통 수 MB 이내)을 통째로 읽어 content-length를
    직접 계산해 보내는 방식으로 우회한다. HEAD 요청은 바디 없이 헤더만 응답한다.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = os.fspath(self.path)
        with open(body, "rb") as file:
            content = file.read()
        self.headers["content-length"] = str(len(content))
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        response_body = b"" if scope["method"].upper() == "HEAD" else content
        await send({"type": "http.response.body", "body": response_body, "more_body": False})
        if self.background is not None:
            await self.background()


async def get_service() -> SearchService:
    """SearchService 싱글턴을 지연 생성해 반환하는 FastAPI 의존성.

    무거운 의존성(Ollama LLM 클라이언트, 임베딩 HTTP 클라이언트, DB 엔진)은
    앱 임포트 시점이 아니라 최초 요청 시점에 한 번만 생성해, 코어 패키지 임포트를
    가볍게 유지하면서도(CLAUDE.md 코드 규칙) 매 요청마다 재생성되는 비용을 없앤다.
    """
    global _service
    if _service is None:
        settings = get_settings()
        from paperrag.ingest.embeddings import HttpEmbeddingClient
        from paperrag.ingest.llm_enrich import OllamaClient

        _service = SearchService(
            PostgresSearchRepository(settings),
            OllamaClient(settings),
            HttpEmbeddingClient(settings),
            settings,
        )
    return _service


@app.get("/health")
async def health() -> dict[str, str]:
    """단순 liveness 체크. 의존 서비스(DB/LLM/임베딩) 상태는 확인하지 않는다."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> JSONResponse:
    """의존 서비스(DB, Ollama, 임베딩 서버 등)가 실제로 준비됐는지 확인하는 readiness 체크.

    docs/guide/05-search-api.md 1단계 사전 조건(스택 기동, 스키마 존재 등)을
    코드로 검증한 결과를 반환하며, 준비되지 않았으면 503을 응답한다.
    """
    report = await run_in_threadpool(build_readiness_report, get_settings())
    status_code = 200 if report["status"] == "ready" else 503
    return JSONResponse(report, status_code=status_code)


@app.post("/search", response_model=SearchMatched | SearchSuggest)
async def search(
    request: SearchRequest,
    service: SearchService = Depends(get_service),
) -> SearchMatched | SearchSuggest:
    """DESIGN.md §5.1의 1단계 진입점: 자연어 질의를 키워드로 정확 매칭 시도한다.

    정확 매칭에 성공하면 대표/연관 논문이 포함된 SearchMatched를, 실패하면
    유사 키워드 후보와 session_id가 담긴 SearchSuggest를 반환한다(2단계로 이어짐).
    request.use_llm(기본 False)이 꺼져 있으면 키워드 추출은 LLM 없이 형태소
    분석만으로 이뤄지고, request.section_query가 있으면 결과 단락을 그 섹션으로
    좁힌다. request.include_related/include_tables(기본 True)를 끄면 연관 논문·
    표 관련 조회와 엑셀 시트를 아예 생략한다. SearchNoPaperFound는 404로,
    LLM/임베딩 응답을 신뢰할 수 없는 SearchDependencyError는 503으로 변환한다
    (임시로 규칙 기반 결과를 조작해 보여주지 않기 위함).
    """
    try:
        return await run_in_threadpool(
            service.search,
            request.query,
            use_llm=request.use_llm,
            section_query=request.section_query,
            include_related=request.include_related,
            include_tables=request.include_tables,
        )
    except SearchNoPaperFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SearchDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/search/select", response_model=SearchMatched)
async def select(
    request: SelectRequest,
    service: SearchService = Depends(get_service),
) -> SearchMatched:
    """suggest 단계에서 사용자가 고른 keyword_id로 검색을 확정하는 2단계 엔드포인트.

    session_id가 만료됐거나 존재하지 않으면(SearchSessionNotFound) 404를 반환한다
    (docs/guide/05-search-api.md 4단계 — suggest 세션 TTL은 30분).
    """
    try:
        return await run_in_threadpool(
            service.select,
            request.session_id,
            request.keyword_id,
        )
    except SearchSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="suggest session expired or not found") from exc
    except SearchNoPaperFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SearchDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/result/{result_id}/excel")
async def result_excel(
    result_id: str,
    service: SearchService = Depends(get_service),
) -> FileResponse:
    """검색 결과(result_id)로 캐시된 6시트 엑셀 파일을 다운로드한다.

    result_id는 search_results 테이블과 PAPERRAG_RESULT_DIR 파일 경로를 함께
    가리키므로, DB 레코드 또는 실제 파일 중 하나라도 없으면(service.result_excel_path
    가 None 반환) 404로 처리한다.
    """
    path = service.result_excel_path(result_id)
    if path is None:
        raise HTTPException(status_code=404, detail="result not found")
    return _InlineFileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"paper-search-{result_id}.xlsx",
        stat_result=os.stat(path),
    )
