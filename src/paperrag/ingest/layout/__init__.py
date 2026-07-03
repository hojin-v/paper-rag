from paperrag.ingest.layout.base import LayoutBackend
from paperrag.ingest.layout.docling_backend import DoclingBackend
from paperrag.ingest.layout.paddle_backend import PaddleBackend
from paperrag.ingest.layout.simple_backend import SimplePyMuPDFBackend


def get_backend(name: str) -> LayoutBackend:
    normalized = name.strip().lower()
    if normalized == "simple":
        return SimplePyMuPDFBackend()
    if normalized == "docling":
        return DoclingBackend()
    if normalized in {"paddle", "pp-structure", "pp-structurev3"}:
        return PaddleBackend()
    raise ValueError(f"알 수 없는 layout backend입니다: {name}")


__all__ = [
    "DoclingBackend",
    "LayoutBackend",
    "PaddleBackend",
    "SimplePyMuPDFBackend",
    "get_backend",
]
