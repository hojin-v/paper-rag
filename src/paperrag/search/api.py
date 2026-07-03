import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.types import Receive, Scope, Send

from paperrag.config import get_settings
from paperrag.search.repository import PostgresSearchRepository
from paperrag.search.schemas import SearchMatched, SearchRequest, SearchSuggest, SelectRequest
from paperrag.search.service import SearchNoPaperFound, SearchService, SearchSessionNotFound

app = FastAPI(title="Paper RAG Search API")

_service: SearchService | None = None


class _InlineFileResponse(FileResponse):
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
    return {"status": "ok"}


@app.post("/search", response_model=SearchMatched | SearchSuggest)
async def search(
    request: SearchRequest,
    service: SearchService = Depends(get_service),
) -> SearchMatched | SearchSuggest:
    try:
        return service.search(request.query)
    except SearchNoPaperFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/search/select", response_model=SearchMatched)
async def select(
    request: SelectRequest,
    service: SearchService = Depends(get_service),
) -> SearchMatched:
    try:
        return service.select(request.session_id, request.keyword_id)
    except SearchSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="suggest session expired or not found") from exc
    except SearchNoPaperFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/result/{result_id}/excel")
async def result_excel(
    result_id: str,
    service: SearchService = Depends(get_service),
) -> FileResponse:
    path = service.result_excel_path(result_id)
    if path is None:
        raise HTTPException(status_code=404, detail="result not found")
    return _InlineFileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"paper-search-{result_id}.xlsx",
        stat_result=os.stat(path),
    )
