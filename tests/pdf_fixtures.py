"""테스트 전용 합성 PDF 생성 헬퍼 — reportlab(BSD 라이선스)으로 PyMuPDF 없이 PDF를 만든다.

PyMuPDF의 `page.insert_text((x, y), text)`는 페이지 좌상단을 원점으로 y가 아래로
증가하는 좌표계를 쓰는 반면, reportlab의 `canvas.drawString(x, y, text)`는 PDF
네이티브 좌표계(좌하단 원점, y가 위로 증가)를 그대로 쓴다. `PdfBuilder`는 좌상단
원점 좌표를 받아 내부에서 `canvas_y = height - y`로 변환해, 이 프로젝트 전체가 쓰는
bbox 좌표계(좌상단 원점 — PyMuPDF·pdfplumber와 동일)로 픽스처를 작성할 때 기존
`pymupdf.open() → new_page() → insert_text()` 호출부를 그대로 옮겨 쓸 수 있게 한다.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from reportlab.pdfgen import canvas


class PdfBuilder:
    """페이지 여러 개, 텍스트/선을 좌상단 원점 좌표로 추가해 PDF 바이트를 만드는 빌더."""

    def __init__(self) -> None:
        self._canvas: canvas.Canvas | None = None
        self._buffer = BytesIO()
        self._page_height = 0.0

    def add_page(self, width: float = 400, height: float = 500) -> "PdfBuilder":
        """새 페이지를 추가한다. `pymupdf.Document.new_page(width=, height=)`에 대응."""
        if self._canvas is None:
            self._canvas = canvas.Canvas(self._buffer, pagesize=(width, height))
        else:
            self._canvas.showPage()
            self._canvas.setPageSize((width, height))
        self._page_height = height
        return self

    def text(self, x: float, y: float, value: str, *, fontsize: float = 11) -> "PdfBuilder":
        """(x, y)는 좌상단 원점 좌표. `page.insert_text((x, y), value, fontsize=)`에 대응."""
        assert self._canvas is not None, "add_page()를 먼저 호출해야 합니다."
        self._canvas.setFont("Helvetica", fontsize)
        self._canvas.drawString(x, self._page_height - y, value)
        return self

    def line(
        self, x0: float, y0: float, x1: float, y1: float, *, width: float = 1
    ) -> "PdfBuilder":
        """좌상단 원점 좌표 두 점을 잇는 선을 그린다. `page.draw_line(...)`에 대응."""
        assert self._canvas is not None, "add_page()를 먼저 호출해야 합니다."
        self._canvas.setLineWidth(width)
        self._canvas.line(x0, self._page_height - y0, x1, self._page_height - y1)
        return self

    def build(self) -> bytes:
        """PDF 바이트를 반환한다. `document.tobytes()`에 대응."""
        assert self._canvas is not None, "add_page()를 먼저 호출해야 합니다."
        self._canvas.save()
        return self._buffer.getvalue()

    def save(self, path: Path) -> None:
        """PDF를 파일로 저장한다. `document.save(path)`에 대응."""
        Path(path).write_bytes(self.build())
