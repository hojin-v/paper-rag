"""simple backend — pdfplumber 텍스트 레이어 기반 휴리스틱 layout 분석기 (진단 전용).

디지털 PDF에서 pdfplumber가 추출하는 텍스트 줄의 위치(bbox)·폰트 크기만으로 제목/본문/
참고문헌을 대충 나누는 가장 단순한 백엔드다. OCR을 전혀 수행하지 않으므로 스캔 PDF에는
쓸 수 없고, 표·그림·수식 같은 세밀한 블록 타입도 구분하지 못한다. 운영 적재 경로가
아니며(DESIGN.md §2), docling/paddle 백엔드와의 비교 기준선(baseline)이나 가벼운 단위
테스트용으로만 사용한다 (docs/reports/benchmarks/2026-07-04-layout-backends.md 참고 —
같은 rich_sample.pdf에서 docling은 단락 8개·표 1개를 뽑아내지만 simple은 단락 3개·표 0개에
그쳤다).

이전에는 PyMuPDF의 `get_text("dict")`가 주는 블록(여러 줄을 문단 단위로 묶은 결과)을
LayoutBlock 1개로 그대로 매핑했다. pdfplumber는 문단 그룹핑 없이 줄(line) 단위로만
텍스트를 준다(AGPL-3.0인 PyMuPDF를 MIT 라이선스 pdfplumber로 교체하며 발생한 차이) —
그래서 이 백엔드는 이제 줄 하나당 LayoutBlock 하나를 만든다. 진단 전용 baseline의
"단락을 세밀하게 나누지 못한다"는 이미 알려진 한계를 벗어나지 않으며, 참고문헌 판정은
줄 단위로 오히려 더 정확해지고, 실제 단락 병합은 뒤따르는 STEP 3~4(paragraph builder)가
담당하므로 최종 산출물에는 영향이 없다.
"""

import re
from pathlib import Path
from typing import Any

from paperrag.ingest.models import DocumentLayout, LayoutBlock

# 참고문헌/부록 시작을 알리는 헤더 줄 패턴. 이 패턴이 매치되는 순간부터 이후 모든 텍스트
# 블록은 block_type="reference"로 분류된다 (STEP 3 filter에서 참고문헌 이후를 제외하기 위한
# 근거 데이터를 여기서 만들어 둔다).
REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)


def _import_pdfplumber():
    """pdfplumber를 지연 임포트한다.

    core 패키지는 무거운 의존성 없이 임포트 가능해야 하므로(CLAUDE.md 코드 규칙),
    실제로 simple backend를 사용하는 시점에만 임포트를 시도하고 미설치 시 설치 안내
    메시지와 함께 명확한 ImportError로 바꿔준다.
    """
    try:
        import pdfplumber  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "pdfplumber가 설치되어 있지 않습니다. `pip install -e \".[ingest]\"`로 "
            "수집 선택 의존성을 설치한 뒤 simple backend를 실행하세요."
        ) from exc
    return pdfplumber


class SimpleTextLayerBackend:
    """pdfplumber의 텍스트 레이어만으로 레이아웃을 추정하는 진단용 백엔드.

    OCR 모델 없이 텍스트 줄의 좌표·폰트 크기 휴리스틱만 사용하므로 처리 속도가
    매우 빠르지만, 줄 단위로만 블록을 만들고 표·그림·수식 등 구조화된 블록 타입도
    인식하지 못하는 한계가 있다.
    """

    def analyze(self, pdf_path: str) -> DocumentLayout:
        """PDF의 텍스트 레이어를 페이지별로 읽어 title/reference/text 블록으로 분류한다.

        페이지 내 줄은 pdfplumber가 이미 위→아래 순으로 반환하므로 별도 정렬 없이 그대로
        읽기 순서로 쓴다 — docling처럼 실제 컬럼(2단 조판) 구조를 인식하는 것이 아니므로
        다단 조판에서는 순서가 어긋날 수 있다.
        """
        pdfplumber = _import_pdfplumber()
        blocks: list[LayoutBlock] = []
        order = 0
        pages_with_text = 0
        in_references = False

        with pdfplumber.open(pdf_path) as document:
            title_line = self._find_title_line(document)
            title_seen = False

            for page_index, page in enumerate(document.pages):
                page_number = page_index + 1
                lines = page.extract_text_lines(strip=True)
                if any(line.get("text", "").strip() for line in lines):
                    pages_with_text += 1

                for line in lines:
                    text = line.get("text", "").strip()
                    if not text:
                        continue

                    # 이 줄이 "References/참고문헌/Appendix/부록"으로 시작하면 그 지점부터
                    # 문서 끝까지를 참고문헌 구간으로 간주한다. 논문 조판 관례상 참고문헌은
                    # 항상 본문 뒤에 한 번만 나오므로 별도 종료 조건 없이 계속
                    # in_references=True를 유지해도 안전하다.
                    if REFERENCE_HEADER_RE.match(text):
                        in_references = True

                    block_type = "reference" if in_references else "text"
                    # 제목 판정: 1페이지에서만, _find_title_line()이 찾아낸 "폰트 크기가
                    # 가장 큰 줄"의 텍스트가 이 줄과 같고 아직 제목으로 표시한 적 없으며
                    # 참고문헌 구간이 아닐 때만 title로 승격한다. title_seen으로 한 문서당
                    # 최대 1개 블록만 title이 되도록 막는다.
                    if (
                        page_number == 1
                        and title_line
                        and not title_seen
                        and title_line == text
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
                            bbox=_line_bbox(line),
                            ocr_engine="pdfplumber-text",
                        )
                    )
                    order += 1

            page_count = len(document.pages)

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
        if not document.pages:
            return None

        best_text: str | None = None
        best_size = -1.0
        first_page = document.pages[0]
        for line in first_page.extract_text_lines(strip=True):
            line_text = line.get("text", "").strip()
            if not line_text:
                continue
            chars = line.get("chars") or []
            line_size = max((float(char.get("size", 0.0)) for char in chars), default=0.0)
            if line_size > best_size:
                best_text = line_text
                best_size = line_size
        return best_text


def _line_bbox(line: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """pdfplumber `extract_text_lines()`의 한 줄에서 (x0, top, x1, bottom) bbox를 뽑는다.

    pdfplumber는 top/bottom을 이미 페이지 좌상단 원점(y 아래로 증가) 기준으로 주므로,
    이 프로젝트 전체가 쓰는 `LayoutBlock.bbox` 좌표계(PyMuPDF와 동일한 좌상단 원점)와
    변환 없이 그대로 호환된다.
    """
    try:
        return (
            float(line["x0"]),
            float(line["top"]),
            float(line["x1"]),
            float(line["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
