"""STEP 7 embed — 단락·키워드·표·논문 텍스트를 벡터로 변환하는 임베딩 클라이언트.

운영에서는 `HttpEmbeddingClient`가 BGE-M3 임베딩 서버(`Settings.embed_base_url`,
1024차원, DESIGN.md §2)에 HTTP로 요청하고, dry-run/테스트에서는 외부 서비스 없이
결정적인 벡터를 만드는 `FakeEmbeddingClient`를 사용한다(`cli.py`의 `--dry-run` 분기 참고).
"""

import hashlib
from typing import Any, Protocol

import httpx

from paperrag.config import Settings, get_settings


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into vectors."""


class HttpEmbeddingClient:
    """BGE-M3 임베딩 서버의 `/embed` 엔드포인트를 호출하는 실제 운영 클라이언트."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 목록을 한 번의 HTTP 요청으로 임베딩 벡터 목록으로 변환한다.

        빈 입력은 요청 없이 빈 리스트를 반환한다(STEP 7에서 표가 없는 논문 등
        불필요한 호출을 피하기 위함). 응답은 `{"embeddings": [...]}` 형태이거나
        벡터 리스트 자체일 수 있어 두 형식을 모두 허용한다.
        """
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
    """DB/BGE-M3 서버 없이 dry-run·테스트를 돌리기 위한 결정적 가짜 임베딩 클라이언트.

    같은 텍스트는 항상 같은 벡터를 만들어(sha256 해시 기반) 코사인 유사도 계산 등을
    오프라인에서도 검증 가능하게 한다. 실제 의미를 담지 않으므로 운영에는 쓰지 않는다.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        # 텍스트+인덱스를 해시해 각 차원 값을 [-1, 1] 범위로 결정론적으로 생성한다.
        values: list[float] = []
        for index in range(self.dim):
            digest = hashlib.sha256(f"{text}\0{index}".encode("utf-8")).digest()
            integer = int.from_bytes(digest[:4], "big", signed=False)
            values.append((integer / 2**32) * 2.0 - 1.0)
        return values


def _coerce_vector(value: Any) -> list[float]:
    """HTTP 응답의 벡터 원소를 float 리스트로 강제 변환(타입이 섞여 와도 방어)."""
    if not isinstance(value, list):
        raise ValueError("Embedding vector must be a list.")
    return [float(item) for item in value]
