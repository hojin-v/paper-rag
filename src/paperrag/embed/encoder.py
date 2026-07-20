"""
임베딩 인코더 구현체 (hash 폴백 vs sentence-transformers 실제 임베딩).

`embed/server.py`가 HTTP로 노출하는 두 가지 벡터화 전략을 여기서 구현한다. `HashEncoder`는
모델을 전혀 로드하지 않고 텍스트를 해시해 결정적인 단위 벡터를 만든다 — 의미를 담지 않으므로
검색 품질 검증에는 쓸 수 없지만, 차원·API 계약·파이프라인 배선을 모델 다운로드 없이 빠르게
테스트할 수 있다. `SentenceTransformerEncoder`가 실제 운영에 쓰는 BGE-M3 기반 인코더다.
"""

import hashlib
import math
from typing import Any, Protocol

from paperrag.config import Settings


class Encoder(Protocol):
    """인코더 구현체가 공통으로 지켜야 하는 인터페이스.

    `dim`(출력 벡터 차원)과 `encode()`(텍스트 목록 → 벡터 목록)만 있으면 `embed/server.py`가
    어떤 구현체든 동일하게 다룰 수 있다.
    """

    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode texts into vectors."""


class HashEncoder:
    """모델 없이 SHA-256 해시로 결정적 단위 벡터를 생성하는 테스트 전용 인코더.

    같은 텍스트는 항상 같은 벡터를 만들어(결정적) 파이프라인 배선·차원 검증용 단위 테스트에
    쓸 수 있지만, 의미적으로 무관한 텍스트끼리도 벡터 공간상 관계가 무작위이므로 실제 검색 결과로
    사용하면 안 된다(`readiness.py`가 운영에서 이 인코더 사용을 error로 차단한다).
    """

    def __init__(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError("Embedding dimension must be positive.")
        self.dim = dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text: str) -> list[float]:
        values: list[float] = []
        for index in range(self.dim):
            # 텍스트와 차원 인덱스를 함께 해시해, 같은 텍스트라도 차원마다 서로 다른(하지만
            # 재현 가능한) 값을 얻는다. 해시 정수를 [-1, 1] 범위로 선형 변환한다.
            digest = hashlib.sha256(f"{text}\0{index}".encode("utf-8")).digest()
            integer = int.from_bytes(digest[:4], "big", signed=False)
            values.append((integer / 2**32) * 2.0 - 1.0)

        # BGE-M3 등 실제 임베딩과 동일하게 단위 벡터(L2 norm=1)로 정규화해, 코사인 유사도 계산
        # 방식이 hash/st 두 인코더에서 동일하게 동작하도록 맞춘다.
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:
            return values
        return [value / norm for value in values]


class SentenceTransformerEncoder:
    """실제 BGE-M3(sentence-transformers) 모델로 임베딩을 생성하는 운영용 인코더.

    모델은 생성 시점이 아니라 최초 `encode()` 호출 시점에 lazy 로드된다(`_load_model`) — 서버
    기동을 빠르게 하고, `sentence-transformers`가 설치되지 않은 환경에서도 이 클래스를 import는
    할 수 있게 하기 위함(CLAUDE.md의 "무거운 의존성은 optional import" 규칙).
    """

    def __init__(self, model_name: str, dim: int) -> None:
        if dim <= 0:
            raise ValueError("Embedding dimension must be positive.")
        self.model_name = model_name
        self.dim = dim
        self._model: object | None = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            # 빈 입력은 모델을 로드하지 않고 즉시 빈 결과를 반환한다 — 불필요한 모델 로드를
            # 피하고, 호출부가 매번 빈 리스트를 특별 취급하지 않아도 되게 한다.
            return []

        model = self._load_model()
        vectors = model.encode(
            texts,
            normalize_embeddings=True,  # 코사인 유사도 계산을 내적만으로 처리할 수 있도록 정규화.
            convert_to_numpy=False,
        )
        result = [[float(value) for value in vector] for vector in vectors]
        for vector in result:
            if len(vector) != self.dim:
                # 설정한 embed_dim과 실제 모델 출력 차원이 어긋나면 DB VECTOR(1024) 스키마와
                # 호환되지 않으므로, 저장 이전에 여기서 즉시 실패시킨다.
                raise ValueError(
                    f"Embedding model returned {len(vector)} dimensions; expected {self.dim}."
                )
        return result

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                # sentence-transformers는 optional 의존성(.[embed])이라 설치돼 있지 않을 수
                # 있다. 실패 원인과 설치 방법을 바로 알 수 있도록 안내 메시지를 덧붙인다.
                raise ImportError(
                    "sentence-transformers is required for PAPERRAG_EMBED_ENCODER=st. "
                    'Install it with: pip install -e ".[embed]"'
                ) from exc
            # CPU 전용 운영 환경(DESIGN.md §2)이므로 device를 명시적으로 고정한다.
            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model


def get_encoder(settings: Settings) -> Encoder:
    """설정값(`embed_encoder`)에 따라 적절한 인코더 인스턴스를 생성하는 팩토리."""
    encoder = settings.embed_encoder.lower()
    if encoder == "hash":
        return HashEncoder(settings.embed_dim)
    if encoder == "st":
        return SentenceTransformerEncoder(settings.embed_model_name, settings.embed_dim)
    raise ValueError("PAPERRAG_EMBED_ENCODER must be 'hash' or 'st'.")
