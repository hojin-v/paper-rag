from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from paperrag.config import Settings, get_settings
from paperrag.embed.encoder import Encoder, get_encoder


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]


class HealthResponse(BaseModel):
    status: str
    encoder: str
    model: str
    dim: int
    production_ready: bool


@lru_cache
def get_cached_encoder() -> Encoder:
    return get_encoder(get_settings())


@asynccontextmanager
async def lifespan(_: FastAPI):
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
    return EmbedResponse(embeddings=encoder.encode(request.texts))
