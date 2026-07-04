import hashlib
import math
from typing import Any, Protocol

from paperrag.config import Settings


class Encoder(Protocol):
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode texts into vectors."""


class HashEncoder:
    def __init__(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError("Embedding dimension must be positive.")
        self.dim = dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text: str) -> list[float]:
        values: list[float] = []
        for index in range(self.dim):
            digest = hashlib.sha256(f"{text}\0{index}".encode("utf-8")).digest()
            integer = int.from_bytes(digest[:4], "big", signed=False)
            values.append((integer / 2**32) * 2.0 - 1.0)

        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:
            return values
        return [value / norm for value in values]


class SentenceTransformerEncoder:
    def __init__(self, model_name: str, dim: int) -> None:
        if dim <= 0:
            raise ValueError("Embedding dimension must be positive.")
        self.model_name = model_name
        self.dim = dim
        self._model: object | None = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        model = self._load_model()
        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=False,
        )
        result = [[float(value) for value in vector] for vector in vectors]
        for vector in result:
            if len(vector) != self.dim:
                raise ValueError(
                    f"Embedding model returned {len(vector)} dimensions; expected {self.dim}."
                )
        return result

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "sentence-transformers is required for PAPERRAG_EMBED_ENCODER=st. "
                    'Install it with: pip install -e ".[embed]"'
                ) from exc
            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model


def get_encoder(settings: Settings) -> Encoder:
    encoder = settings.embed_encoder.lower()
    if encoder == "hash":
        return HashEncoder(settings.embed_dim)
    if encoder == "st":
        return SentenceTransformerEncoder(settings.embed_model_name, settings.embed_dim)
    raise ValueError("PAPERRAG_EMBED_ENCODER must be 'hash' or 'st'.")
