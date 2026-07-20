"""simple backend — PyMuPDF 텍스트 레이어 기반 휴리스틱 layout 분석기 (진단 전용).

디지털 PDF에서 PyMuPDF가 추출하는 텍스트 블록의 위치(bbox)·폰트 크기만으로 제목/본문/
참고문헌을 대충 나누는 가장 단순한 백엔드다. OCR을 전혀 수행하지 않으므로 스캔 PDF에는
쓸 수 없고, 표·그림·수식 같은 세밀한 블록 타입도 구분하지 못한다. 운영 적재 경로가
아니며(DESIGN.md §2), docling/paddle 백엔드와의 비교 기준선(baseline)이나 가벼운 단위
테스트용으로만 사용한다 (docs/reports/benchmarks/2026-07-04-layout-backends.md 참고 —
같은 rich_sample.pdf에서 docling은 단락 8개·표 1개를 뽑아내지만 simple은 단락 3개·표 0개에
그쳤다).
"""

import re
from pathlib import Path
from typing import Any

from paperrag.ingest.models import DocumentLayout, LayoutBlock

# 참고문헌/부록 시작을 알리는 헤더 줄 패턴. 이 패턴이 매치되는 순간부터 이후 모든 텍스트
# 블록은 block_type="reference"로 분류된다 (STEP 3 filter에서 참고문헌 이후를 제외하기 위한
# 근거 데이터를 여기서 만들어 둔다).
REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)


def _import_pymupdf():
    """PyMuPDF를 지연 임포트한다.

    core 패키지는 무거운 의존성 없이 임포트 가능해야 하므로(CLAUDE.md 코드 규칙),
    실제로 simple backend를 사용하는 시점에만 임포트를 시도하고 미설치 시 설치 안내
    메시지와 함께 명확한 ImportError로 바꿔준다.
    """
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF가 설치되어 있지 않습니다. `pip install -e \".[ingest]\"`로 "
            "수집 선택 의존성을 설치한 뒤 simple backend를 실행하세요."
        ) from exc
    return pymupdf


class SimplePyMuPDFBackend:
    """PyMuPDF의 텍스트 레이어만으로 레이아웃을 추정하는 진단용 백엔드.

    OCR 모델 없이 텍스트 블록의 좌표·폰트 크기 휴리스틱만 사용하므로 처리 속도가
    매우 빠르지만(합성 PDF 기준 0.5s vs docling 12~13s), 단락을 세밀하게 나누지 못하고
    표·그림·수식 등 구조화된 블록 타입도 인식하지 못하는 한계가 있다.
    """

    def analyze(self, pdf_path: str) -> DocumentLayout:
        """PDF의 텍스트 레이어를 페이지별로 읽어 title/reference/text 블록으로 분류한다.

        페이지 내 블록은 y좌표(위→아래) 우선, x좌표(왼→오) 다음 순으로 정렬해 읽기 순서를
        근사한다 — docling처럼 실제 컬럼(2단 조판) 구조를 인식하는 것이 아니라 단순
        좌표 정렬이므로 다단 조판에서는 순서가 어긋날 수 있다.
        """
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

                # (y, x) 순 정렬 = "위에서 아래로, 같은 줄이면 왼쪽에서 오른쪽으로" 라는
                # 단일 컬럼 조판 가정의 읽기 순서 근사치. 2단 조판 논문에서는 이 정렬만으로
                # 왼쪽 컬럼과 오른쪽 컬럼이 뒤섞여 나올 수 있다 (docling 대비 한계).
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

                    # 이 블록의 첫 줄이 "References/참고문헌/Appendix/부록"으로 시작하면
                    # 그 지점부터 문서 끝까지를 참고문헌 구간으로 간주한다. 논문 조판 관례상
                    # 참고문헌은 항상 본문 뒤에 한 번만 나오므로 별도 종료 조건 없이 계속
                    # in_references=True를 유지해도 안전하다.
                    normalized_first_line = text.splitlines()[0].strip()
                    if REFERENCE_HEADER_RE.match(normalized_first_line):
                        in_references = True

                    block_type = "reference" if in_references else "text"
                    # 제목 판정: 1페이지에서만, _find_title_line()이 찾아낸 "폰트 크기가
                    # 가장 큰 줄"의 텍스트가 이 블록 안에 포함돼 있고 아직 제목으로 표시한 적
                    # 없으며 참고문헌 구간이 아닐 때만 title로 승격한다. title_seen으로
                    # 한 문서당 최대 1개 블록만 title이 되도록 막는다.
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

        # "스캔 PDF 여부" 휴리스틱: 텍스트 레이어가 있는 페이지 비율이 80% 미만이면
        # 스캔본(또는 텍스트 레이어가 깨진 PDF)으로 간주한다. simple backend는 OCR을
        # 하지 않으므로 이 값은 어디까지나 진단 정보이며, 실제 스캔 PDF 처리는
        # PaddleBackend(운영 경로)가 담당한다.
        is_scanned = page_count == 0 or pages_with_text / page_count < 0.8
        return DocumentLayout(
            source_path=str(Path(pdf_path)),
            is_scanned=is_scanned,
            blocks=blocks,
        )

    def _find_title_line(self, document: Any) -> str | None:
        """1페이지에서 폰트 크기가 가장 큰 줄을 논문 제목으로 추정해 반환한다.

        "제목은 보통 그 페이지에서 가장 큰 글씨로 조판된다"는 단순한 가정에 기반한
        휴리스틱이다. 별도 레이아웃 모델이 없는 simple backend가 title 블록을 구분할
        수 있는 유일한 단서이며, 후보가 전혀 없으면 None을 반환해 title 승격을 건너뛴다.
        """
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
        """PyMuPDF `get_text("dict")` 블록의 line/span 트리를 줄바꿈으로 이어붙인 평문으로 만든다."""
        lines: list[str] = []
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text:
                lines.append(text)
        return "\n".join(lines)


def _coerce_bbox(value: object) -> tuple[float, float, float, float] | None:
    """PyMuPDF가 주는 bbox(list/tuple 형태)를 `LayoutBlock.bbox` 튜플 타입으로 안전하게 변환한다.

    길이가 4가 아니거나 숫자로 변환할 수 없는 값이면 None을 반환해 이후 로직이
    bbox 없는 블록으로 취급하도록 한다(예외를 던지지 않고 조용히 누락 처리).
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return (values[0], values[1], values[2], values[3])
