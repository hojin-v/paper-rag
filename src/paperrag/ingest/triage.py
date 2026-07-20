"""STEP 1 source check 보조 — PDF가 디지털 문서인지 스캔본인지 판별.

주의: DESIGN.md §3의 현재 운영 정책은 "모든 PDF를 페이지 이미지로 렌더링해
PP-StructureV3 OCR로 처리"이며 텍스트 레이어는 본문 추출에 쓰지 않는다.
즉 이 모듈의 `classify_pdf` 판정 결과(digital/scanned)는 STEP 2의 backend
선택(paddle 단일 경로)을 바꾸지 않으며, 진단·통계 목적의 보조 정보로만 쓰인다.
"""

from pathlib import Path
from typing import Literal


def _import_pypdfium2():
    try:
        import pypdfium2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "pypdfium2가 설치되어 있지 않습니다. `pip install -e \".[ingest]\"`로 "
            "수집 선택 의존성을 설치한 뒤 다시 실행하세요."
        ) from exc
    return pypdfium2


def classify_pdf(path: str | Path) -> Literal["digital", "scanned"]:
    """PDF의 각 페이지에 추출 가능한 텍스트 레이어가 있는지로 digital/scanned를 판별한다.

    pypdfium2로 페이지별 텍스트를 뽑아 텍스트가 있는 페이지 비율이 임계값 이상이면
    "digital", 아니면(스캔 이미지 위주) "scanned"로 분류한다. 페이지가 0장이면
    안전하게 "scanned"로 취급해 전체 OCR 경로로 넘어가게 한다.
    """
    pdfium = _import_pypdfium2()
    pdf_path = str(path)

    document = pdfium.PdfDocument(pdf_path)
    try:
        page_count = len(document)
        if page_count == 0:
            return "scanned"

        pages_with_text = 0
        for page in document:
            if page.get_textpage().get_text_range().strip():
                pages_with_text += 1
    finally:
        document.close()

    # 80% 이상 페이지에 텍스트 레이어가 있으면 digital로 판정한다. 표지·백지 등
    # 일부 페이지에 텍스트가 없어도 오분류하지 않도록 완화한 임계값이다.
    return "digital" if pages_with_text / page_count >= 0.8 else "scanned"
