"""
BGE-M3 HTTP 임베딩 서버 (hash/st 인코더 전환형).

`PAPERRAG_EMBED_ENCODER` 설정값에 따라 실제 sentence-transformers 모델("st", 운영용)이나
모델 없이 결정적 벡터만 만드는 해시 인코더("hash", 단위 테스트 전용)를 로드해 같은 HTTP 계약
(`/embed`, `/health`)으로 노출한다. 호출자(수집 파이프라인, 검색 서비스, `paperrag.readiness`)는
어느 인코더가 떠 있는지 신경 쓰지 않고 동일한 요청/응답 스키마로 통신하며, 실제 운영 여부는
`/health`의 `production_ready` 필드로만 구분한다. docs/guide/08-embedding.md 참고.
"""

from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from paperrag.config import Settings, get_settings
from paperrag.embed.encoder import Encoder, get_encoder


class EmbedRequest(BaseModel):
    """`/embed` 요청 바디: 임베딩할 텍스트 목록."""

    texts: list[str]


class EmbedResponse(BaseModel):
    """`/embed` 응답: `texts`와 같은 순서·개수의 벡터 목록."""

    embeddings: list[list[float]]


class HealthResponse(BaseModel):
    """`/health` 응답. `production_ready`가 이 서버를 운영에 써도 되는지 나타내는 최종 신호다."""

    status: str
    encoder: str
    model: str
    dim: int
    production_ready: bool


@lru_cache
def get_cached_encoder() -> Encoder:
    """설정에 맞는 인코더 인스턴스를 프로세스당 한 번만 생성해 재사용한다.

    `st` 모드는 내부적으로 모델을 lazy 로드하지만, 인코더 객체 자체를 매 요청마다 새로 만들면
    `_model` 캐시가 사라져 요청마다 모델을 다시 로드하게 된다. `lru_cache`로 인스턴스를 고정한다.
    """
    return get_encoder(get_settings())


@asynccontextmanager
async def lifespan(_: FastAPI):
    """서버 기동 시 더미 텍스트로 한 번 인코딩해 모델을 미리 로드(warmup)한다.

    `st` 모드에서 sentence-transformers 모델은 최초 `encode()` 호출 시점에 로드되므로, warmup
    없이는 첫 실제 요청이 모델 로드 시간까지 떠안게 된다. 기동 시 미리 로드해 첫 요청 지연을 없앤다.
    """
    get_cached_encoder().encode(["warmup"])
    yield


app = FastAPI(title="Paper RAG Embedding Server", lifespan=lifespan)


async def get_encoder_dependency() -> Encoder:
    return get_cached_encoder()


async def get_settings_dependency() -> Settings:
    return get_settings()


@app.get("/health", response_model=HealthResponse)
async def health(
    encoder: Annotated[Encoder, Depends(get_encoder_dependency)],
    settings: Annotated[Settings, Depends(get_settings_dependency)],
) -> HealthResponse:
    """현재 로드된 인코더 종류·차원과 운영 가능 여부를 보고한다.

    `readiness.py`의 `_embedding_status`가 이 엔드포인트를 호출해 `encoder == "st"`이고 `dim`이
    설정과 일치하는지로 운영 준비 여부를 판정한다.
    """
    return HealthResponse(
        status="ok",
        encoder=settings.embed_encoder,
        model=settings.embed_model_name,
        dim=encoder.dim,
        production_ready=settings.embed_encoder == "st",
    )


@app.post("/embed", response_model=EmbedResponse)
async def embed(
    request: EmbedRequest,
    encoder: Annotated[Encoder, Depends(get_encoder_dependency)],
) -> EmbedResponse:
    """텍스트 목록을 받아 같은 순서의 임베딩 벡터 목록을 반환한다.

    빈 `texts`가 오면(예: 필터링 후 남은 텍스트가 없는 경우) 인코더가 빈 리스트를 그대로 반환하며
    400 오류를 내지 않는다(`docs/guide/08-embedding.md` 2단계 참고).
    """
    return EmbedResponse(embeddings=encoder.encode(request.texts))
