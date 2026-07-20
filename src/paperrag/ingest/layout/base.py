"""layout 백엔드들이 공통으로 따라야 하는 최소 인터페이스 계약.

STEP 2(layout)는 백엔드 교체 가능한 어댑터 구조로 구현한다는 ADR-0002의 결정에 따라,
모든 백엔드(`SimpleTextLayerBackend`, `DoclingBackend`, `PaddleBackend`)는 최소한
`analyze(pdf_path) -> DocumentLayout` 하나만 구현하면 `get_backend()`를 통해 동일하게
호출될 수 있다. `Protocol`을 쓴 이유는 구조적 타이핑(덕 타이핑)으로 각 백엔드 클래스가
이 클래스를 명시적으로 상속하지 않아도 되게 하기 위함이다.
"""

from typing import Protocol

from paperrag.ingest.models import DocumentLayout


class LayoutBackend(Protocol):
    """모든 layout 백엔드가 만족해야 하는 최소 계약(구조적 타입)."""

    def analyze(self, pdf_path: str) -> DocumentLayout:
        """PDF 한 편을 분석해 정규화된 레이아웃 블록 목록(`DocumentLayout`)을 반환한다.

        반환되는 `DocumentLayout.blocks`는 논문 전체를 아우르는 `LayoutBlock` 목록이며,
        각 블록은 12개 클래스(title/author/abstract/section_header/text/table/
        table_caption/figure/figure_caption/formula/reference/header_footer) 중 하나로
        분류되고 읽기 순서(`order`)가 이미 복원돼 있어야 한다 (DESIGN.md §3 STEP 2).

        참고: `analyze`는 이 프로토콜의 최소 요구사항일 뿐이다. 운영 백엔드인
        `PaddleBackend`는 사람이 개입하는 2단계 검수 흐름을 지원하기 위해 이 계약을
        넘어서는 추가 메서드를 노출한다 (이 Protocol에는 포함되지 않음, 해당 파일
        `paddle_backend.py` 참고):
        - `analyze_layout(pdf_path)`: 레이아웃 검출만 수행하고 텍스트 검출 좌표로
          누락·잘림을 보정한 `DocumentLayout`을 반환한다. 아직 OCR 텍스트는 채우지
          않고 사람이 검수할 블록 경계만 만든다.
        - `recognize_layout(pdf_path, reviewed_blocks)`: 사람이 확정한 블록 목록을
          입력받아 각 영역을 crop한 뒤 OCR(표는 표 구조 인식)을 실행해 텍스트가 채워진
          `DocumentLayout`을 반환한다.
        """
