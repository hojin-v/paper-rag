from typing import Protocol

from paperrag.ingest.models import DocumentLayout


class LayoutBackend(Protocol):
    def analyze(self, pdf_path: str) -> DocumentLayout:
        """Analyze a PDF and return normalized layout blocks."""
