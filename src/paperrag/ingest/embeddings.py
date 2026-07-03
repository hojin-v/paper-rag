import hashlib
from typing import Any, Protocol

import httpx

from paperrag.config import Settings, get_settings


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into vectors."""


class HttpEmbeddingClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = httpx.post(
            f"{self.settings.embed_base_url.rstrip('/')}/embed",
            json={"texts": texts},
            timeout=self.settings.embed_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        vectors = data.get("embeddings", data)
        if not isinstance(vectors, list):
            raise ValueError("Embedding response must be a list or {'embeddings': list}.")
        return [_coerce_vector(vector) for vector in vectors]


class FakeEmbeddingClient:
    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        values: list[float] = []
        for index in range(self.dim):
            digest = hashlib.sha256(f"{text}\0{index}".encode("utf-8")).digest()
            integer = int.from_bytes(digest[:4], "big", signed=False)
            values.append((integer / 2**32) * 2.0 - 1.0)
        return values


def _coerce_vector(value: Any) -> list[float]:
    if not isinstance(value, list):
        raise ValueError("Embedding vector must be a list.")
    return [float(item) for item in value]
