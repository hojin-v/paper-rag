from pathlib import Path
from typing import Literal


def _import_pymupdf():
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF가 설치되어 있지 않습니다. `pip install -e \".[ingest]\"`로 "
            "수집 선택 의존성을 설치한 뒤 다시 실행하세요."
        ) from exc
    return pymupdf


def classify_pdf(path: str | Path) -> Literal["digital", "scanned"]:
    pymupdf = _import_pymupdf()
    pdf_path = str(path)

    with pymupdf.open(pdf_path) as document:
        page_count = len(document)
        if page_count == 0:
            return "scanned"

        pages_with_text = 0
        for page in document:
            if page.get_text("text").strip():
                pages_with_text += 1

    return "digital" if pages_with_text / page_count >= 0.8 else "scanned"
