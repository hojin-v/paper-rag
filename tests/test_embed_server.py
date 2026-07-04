import asyncio
import importlib.util
import math
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from paperrag.config import Settings
from paperrag.embed.encoder import Encoder, get_encoder
from paperrag.embed.server import (
    app,
    get_cached_encoder,
    get_encoder_dependency,
    get_settings_dependency,
)
from paperrag.ingest import embeddings as embedding_module
from paperrag.ingest.embeddings import HttpEmbeddingClient


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


@pytest.fixture
def embed_client() -> Iterator[TestClient]:
    settings = Settings(_env_file=None, embed_encoder="hash", embed_dim=1024)
    encoder = get_encoder(settings)

    async def override_settings() -> Settings:
        return settings

    async def override_encoder() -> Encoder:
        return encoder

    app.dependency_overrides[get_settings_dependency] = override_settings
    app.dependency_overrides[get_encoder_dependency] = override_encoder
    get_cached_encoder.cache_clear()

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    get_cached_encoder.cache_clear()


def test_health(embed_client: TestClient) -> None:
    response = embed_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "encoder": "hash",
        "model": "BAAI/bge-m3",
        "dim": 1024,
    }


def test_embed_hash_response_shape_determinism_and_normalization(
    embed_client: TestClient,
) -> None:
    response = embed_client.post("/embed", json={"texts": ["same text", "same text", "other"]})

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["embeddings"]

    vectors = body["embeddings"]
    assert len(vectors) == 3
    assert len(vectors[0]) == 1024
    assert vectors[0] == vectors[1]
    assert vectors[0] != vectors[2]
    assert math.sqrt(sum(value * value for value in vectors[0])) == pytest.approx(1.0)


def test_embed_empty_texts_returns_empty_vectors(embed_client: TestClient) -> None:
    response = embed_client.post("/embed", json={"texts": []})

    assert response.status_code == 200
    assert response.json() == {"embeddings": []}


def test_http_embedding_client_payload_contract(
    embed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def post(url: str, json: dict[str, Any], timeout: int) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return embed_client.post("/embed", json=json)

    monkeypatch.setattr(embedding_module.httpx, "post", post)
    settings = Settings(
        _env_file=None,
        embed_base_url="http://testserver",
        embed_timeout_seconds=7,
    )

    vectors = HttpEmbeddingClient(settings).embed(["contract"])

    assert captured == {
        "url": "http://testserver/embed",
        "json": {"texts": ["contract"]},
        "timeout": 7,
    }
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024
