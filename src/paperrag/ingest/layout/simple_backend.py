import re
from pathlib import Path
from typing import Any

from paperrag.ingest.models import DocumentLayout, LayoutBlock

REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)


def _import_pymupdf():
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF가 설치되어 있지 않습니다. `pip install -e \".[ingest]\"`로 "
            "수집 선택 의존성을 설치한 뒤 simple backend를 실행하세요."
        ) from exc
    return pymupdf


class SimplePyMuPDFBackend:
    def analyze(self, pdf_path: str) -> DocumentLayout:
        pymupdf = _import_pymupdf()
        blocks: list[LayoutBlock] = []
        order = 0
        pages_with_text = 0
        in_references = False

        with pymupdf.open(pdf_path) as document:
            title_line = self._find_title_line(document)
            title_seen = False

            for page_index, page in enumerate(document):
                page_number = page_index + 1
                page_blocks = page.get_text("dict").get("blocks", [])
                text_blocks = [block for block in page_blocks if block.get("type") == 0]
                if any(self._block_text(block).strip() for block in text_blocks):
                    pages_with_text += 1

                text_blocks.sort(
                    key=lambda block: (
                        block.get("bbox", [0, 0, 0, 0])[1],
                        block.get("bbox", [0, 0, 0, 0])[0],
                    )
                )
                for block in text_blocks:
                    text = self._block_text(block).strip()
                    if not text:
                        continue

                    normalized_first_line = text.splitlines()[0].strip()
                    if REFERENCE_HEADER_RE.match(normalized_first_line):
                        in_references = True

                    block_type = "reference" if in_references else "text"
                    if (
                        page_number == 1
                        and title_line
                        and not title_seen
                        and title_line in text
                        and not in_references
                    ):
                        block_type = "title"
                        title_seen = True

                    blocks.append(
                        LayoutBlock(
                            page=page_number,
                            block_type=block_type,
                            text=text,
                            order=order,
                            bbox=_coerce_bbox(block.get("bbox")),
                            ocr_engine="pymupdf-text",
                        )
                    )
                    order += 1

            page_count = len(document)

        is_scanned = page_count == 0 or pages_with_text / page_count < 0.8
        return DocumentLayout(
            source_path=str(Path(pdf_path)),
            is_scanned=is_scanned,
            blocks=blocks,
        )

    def _find_title_line(self, document: Any) -> str | None:
        if len(document) == 0:
            return None

        best_text: str | None = None
        best_size = -1.0
        first_page = document[0]
        for block in first_page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if not line_text:
                    continue
                line_size = max(
                    (float(span.get("size", 0.0)) for span in line.get("spans", [])),
                    default=0.0,
                )
                if line_size > best_size:
                    best_text = line_text
                    best_size = line_size
        return best_text

    def _block_text(self, block: dict[str, Any]) -> str:
        lines: list[str] = []
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text:
                lines.append(text)
        return "\n".join(lines)


def _coerce_bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return (values[0], values[1], values[2], values[3])
