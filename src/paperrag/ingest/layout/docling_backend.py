from pathlib import Path
from typing import Any

from paperrag.ingest.models import DocumentLayout, LayoutBlock


class DoclingBackend:
    def analyze(self, pdf_path: str) -> DocumentLayout:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "Docling이 설치되어 있지 않습니다. `pip install -e \".[ingest-full]\"`로 "
                "전체 수집 의존성을 설치하거나 `--backend simple`을 사용하세요."
            ) from exc

        converter = DocumentConverter()
        result = converter.convert(pdf_path)
        document = getattr(result, "document", result)
        text = self._export_text(document)
        blocks = [
            LayoutBlock(page=1, block_type="text", text=chunk, order=index)
            for index, chunk in enumerate(self._split_chunks(text))
        ]
        return DocumentLayout(source_path=str(Path(pdf_path)), is_scanned=False, blocks=blocks)

    def _export_text(self, document: Any) -> str:
        for method_name in ("export_to_markdown", "export_to_text"):
            method = getattr(document, method_name, None)
            if callable(method):
                return str(method())
        return str(document)

    def _split_chunks(self, text: str) -> list[str]:
        chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
        return chunks or [text.strip()] if text.strip() else []
