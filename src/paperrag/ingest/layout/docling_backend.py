"""docling backend — Docling(MIT 라이선스) 어댑터 (진단/비교 전용, 운영 적재 경로 아님).

ADR-0002는 원래 "디지털 PDF는 Docling, 스캔 PDF는 PP-StructureV3"라는 이중 트랙으로
설계됐으나, 2026-07-12 사용자 결정으로 디지털 파싱 트랙 자체가 폐기되고 현재 운영
기준은 모든 PDF를 PyMuPDF로 이미지화한 뒤 PP-StructureV3로 전체 OCR하는 단일 경로다
(DESIGN.md §2). 따라서 이 백엔드는 더 이상 운영 수집 파이프라인에서 호출되지 않으며,
docling vs paddle 결과 비교(docs/reports/benchmarks/2026-07-04-layout-backends.md)나
단위 테스트에서만 사용한다.

docling의 `DocumentConverter`가 반환하는 문서 트리를 `iterate_items()`로 순회하며
paper-rag 공통 12개 블록 타입(BlockType)으로 매핑한다. 과거에는
`export_to_markdown()`으로 문서 전체를 통짜 텍스트로 뽑은 뒤 후처리로 쪼개는 방식을
썼으나, 그 방식은 표·참고문헌 같은 구조 정보가 마크다운으로 평탄화되는 과정에서
블록 타입이 통째로 유실되는 버그가 있었다(표 추출 0건, 참고문헌 제외 실패 — 위 벤치마크
리포트 "어댑터 결함 발견" 항목). `iterate_items()` 기반으로 재작성한 뒤에는 각 아이템의
원래 타입(TableItem/PictureItem/label 등)을 유지한 채 매핑하므로 표 추출·참고문헌 제외가
정상 동작한다.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from paperrag.ingest.models import BlockType, DocumentLayout, LayoutBlock

# docling label 값 -> paper-rag 블록 타입 그룹핑에 쓰는 라벨 집합들.
TEXT_LABELS = {"text", "paragraph", "list_item"}
HEADER_FOOTER_LABELS = {"page_header", "page_footer", "footnote"}
PICTURE_LABELS = {"picture", "chart"}
# 텍스트 블록의 첫 줄이 이 패턴과 매치되면 "초록 시작" / "참고문헌 시작" 섹션 헤더로 본다.
ABSTRACT_HEADER_RE = re.compile(r"^\s*(abstract|초록|요약)\b", re.IGNORECASE)
REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)


@dataclass
class _MappingState:
    """`_iter_document_items()` 순회 도중 유지되는 상태 — 문서를 앞에서부터 훑으면서
    "지금 초록/참고문헌 구간을 지나고 있는지", "제목을 이미 봤는지"를 기억해 다음 블록의
    타입을 문맥에 맞게 분류하기 위한 것이다. 아이템 하나하나는 독립적으로 라벨을 갖지만,
    "이게 초록 본문인지 그냥 본문인지"는 앞서 지나온 section_header에 달려있으므로
    상태를 들고 다녀야 한다.
    """

    seen_title: bool = False
    in_abstract: bool = False
    in_references: bool = False


class DoclingBackend:
    """Docling `DocumentConverter` 결과를 공통 `LayoutBlock` 계약으로 변환하는 어댑터.

    운영 백엔드가 아니므로(위 모듈 docstring 참고) `is_scanned`는 항상 False로 고정한다
    — Docling은 디지털 텍스트 레이어가 있는 PDF를 전제로 동작하기 때문이다.
    """

    def analyze(self, pdf_path: str) -> DocumentLayout:
        """PDF를 Docling으로 변환한 뒤 문서 아이템을 순서대로 훑어 `LayoutBlock` 목록을 만든다."""
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
        items = _iter_document_items(document)
        state = _MappingState()
        blocks: list[LayoutBlock] = []

        for index, item in enumerate(items):
            previous_item = items[index - 1] if index > 0 else None
            next_item = items[index + 1] if index + 1 < len(items) else None
            block = _map_item(
                item,
                order=len(blocks),
                state=state,
                document=document,
                previous_item=previous_item,
                next_item=next_item,
            )
            if block is not None:
                blocks.append(block)

        return DocumentLayout(source_path=str(Path(pdf_path)), is_scanned=False, blocks=blocks)


def _iter_document_items(document: Any) -> list[Any]:
    """docling 문서 트리를 평평한 아이템 리스트로 펼친다.

    `iterate_items()`는 docling 버전에 따라 `(item, level)` 튜플 또는 item 자체를 낼 수
    있어 둘 다 방어적으로 처리한다. `iterate_items`가 없는(예상 밖) 문서 객체가 오면
    빈 리스트를 반환해 상위에서 blocks=[]로 안전하게 넘어가도록 한다.

    이 함수가 반환하는 리스트를 순서대로 훑는 것이 곧 "예전 export_to_markdown() 통짜
    파싱" 대신 채택한 12블록 매핑 방식의 핵심이다 — 각 아이템이 원래 타입 정보(TableItem/
    PictureItem/label)를 그대로 유지하고 있어 `_map_item()`이 표/그림/참고문헌을 정확히
    구분할 수 있다.
    """
    iterate_items = getattr(document, "iterate_items", None)
    if not callable(iterate_items):
        return []

    items: list[Any] = []
    for entry in iterate_items():
        item = entry[0] if isinstance(entry, tuple) and entry else entry
        items.append(item)
    return items


def _map_item(
    item: Any,
    *,
    order: int,
    state: _MappingState | None = None,
    document: Any | None = None,
    previous_item: Any | None = None,
    next_item: Any | None = None,
) -> LayoutBlock | None:
    """docling 아이템 하나를 paper-rag의 `LayoutBlock`(12개 타입 중 하나)으로 매핑한다.

    분류 우선순위는: 표/그림 여부(클래스명 기반, label이 비어 있어도 판정 가능) →
    label 값(title/section_header/본문류/caption/formula/reference/header-footer) →
    나머지는 전부 "text"로 폴백. `state`(직전까지의 초록/참고문헌/제목 여부)를 함께 봐야만
    "이 본문 문단이 초록인지 일반 본문인지, 참고문헌 구간에 들어섰는지"를 판단할 수 있어
    순서대로 호출되며 부수효과로 state를 갱신한다.

    label도 없고 표/그림도 아닌 아이템(예: 빈 그룹 노드)은 None을 반환해 블록으로
    만들지 않는다.
    """
    state = state or _MappingState()
    label = _label_value(item)

    if not label and not (_is_table_item(item) or _is_picture_item(item)):
        return None

    block_type: BlockType
    if _is_table_item(item):
        block_type = "table"
        text = _table_text(item, document=document)
    elif _is_picture_item(item):
        block_type = "figure"
        text = ""
    elif label == "title":
        # docling이 title 라벨을 여러 번 내는 경우가 있어(예: 부제목까지 title로 분류),
        # 최초 1개만 실제 "title"로 승격하고 이후 title 라벨은 일반 text로 강등한다.
        block_type = "title" if not state.seen_title else "text"
        state.seen_title = True
        text = _item_text(item, document=document)
    elif label == "section_header":
        block_type = "section_header"
        text = _item_text(item, document=document)
        # 섹션 헤더 텍스트로 "지금부터 참고문헌/초록 구간"인지를 판정해 상태를 갱신한다.
        # 참고문헌 시작이 감지되면 초록 상태는 무조건 해제한다(둘이 동시에 참일 수 없음).
        if REFERENCE_HEADER_RE.match(text.strip()):
            state.in_references = True
            state.in_abstract = False
        else:
            state.in_abstract = bool(ABSTRACT_HEADER_RE.match(text.strip()))
    elif label in TEXT_LABELS:
        text = _item_text(item, document=document)
        # 섹션 헤더 없이 본문 단락 자체가 "References"로 시작하는 경우(헤더가 별도
        # section_header 아이템으로 안 잡히는 문서 포맷 대응)도 참고문헌 진입으로 인정한다.
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        if REFERENCE_HEADER_RE.match(first_line):
            state.in_references = True
            state.in_abstract = False
        if state.in_references:
            block_type = "reference"
        else:
            block_type = "abstract" if state.in_abstract else "text"
    elif label == "caption":
        # caption 아이템 자체에는 "표 캡션인지 그림 캡션인지" 라벨이 없으므로, 문서 순서상
        # 바로 앞/뒤에 table 아이템이 붙어 있는지로 추정한다(표 캡션은 보통 표 바로 위/아래).
        block_type = "table_caption" if _is_adjacent_to_table(previous_item, next_item) else "figure_caption"
        text = _item_text(item, document=document)
    elif label == "formula":
        block_type = "formula"
        text = _item_text(item, document=document)
    elif label == "reference":
        block_type = "reference"
        text = _item_text(item, document=document)
        state.in_references = True
        state.in_abstract = False
    elif label in HEADER_FOOTER_LABELS:
        block_type = "header_footer"
        text = _item_text(item, document=document)
    else:
        # 알려지지 않은 label은 전부 일반 본문(text)으로 폴백해 최소한 내용이 유실되지
        # 않도록 한다.
        block_type = "text"
        text = _item_text(item, document=document)

    return LayoutBlock(
        page=_page_no(item),
        block_type=block_type,
        text=text,
        order=order,
        bbox=_bbox(item, document=document),
        confidence=_confidence(item),
        ocr_engine="docling",
    )


def _label_value(item: Any) -> str:
    """docling 아이템의 label을 소문자 문자열로 정규화한다(Enum이든 str이든 동일하게 처리)."""
    label = getattr(item, "label", "")
    value = getattr(label, "value", label)
    return str(value).strip().lower()


def _page_no(item: Any) -> int:
    """아이템의 출처 정보(`prov`) 첫 항목에서 페이지 번호를 뽑는다. 못 찾으면 1페이지로 폴백한다."""
    prov_items = getattr(item, "prov", None) or []
    first = prov_items[0] if prov_items else None
    page_no = first.get("page_no") if isinstance(first, dict) else getattr(first, "page_no", None)
    try:
        return int(page_no)
    except (TypeError, ValueError):
        return 1


def _bbox(
    item: Any,
    *,
    document: Any | None = None,
) -> tuple[float, float, float, float] | None:
    """아이템의 bbox를 paper-rag 표준 좌표계(원점 좌상단, (x0,y0,x1,y1))로 변환한다.

    docling의 bbox는 dict/list/객체 등 버전에 따라 표현이 달라 셋 다 처리한다. 특히
    docling 좌표는 원점이 좌하단(BOTTOMLEFT)일 수 있는데, 이 경우 페이지 높이를 이용해
    y축을 뒤집어(page_height - y) 좌상단 원점으로 통일한다 — 그렇지 않으면 다른 백엔드
    (paddle 등)와 bbox 좌표계가 어긋나 dedup·후속 처리에서 잘못 비교된다.
    """
    prov_items = getattr(item, "prov", None) or []
    first = prov_items[0] if prov_items else None
    bbox = first.get("bbox") if isinstance(first, dict) else getattr(first, "bbox", None)
    if bbox is None:
        return None
    if isinstance(bbox, dict):
        values = [bbox.get(name) for name in ("l", "t", "r", "b")]
    elif isinstance(bbox, (list, tuple)):
        values = list(bbox)
    else:
        values = [getattr(bbox, name, None) for name in ("l", "t", "r", "b")]
    if len(values) != 4 or any(value is None for value in values):
        return None
    try:
        left, top, right, bottom = (float(value) for value in values)
    except (TypeError, ValueError):
        return None
    x0, x1 = min(left, right), max(left, right)
    y0, y1 = min(top, bottom), max(top, bottom)
    origin = str(getattr(getattr(bbox, "coord_origin", None), "value", "")).upper()
    page_height = _page_height(document, _page_no(item))
    if origin == "BOTTOMLEFT" and page_height is not None:
        return (x0, page_height - y1, x1, page_height - y0)
    return (x0, y0, x1, y1)


def _page_height(document: Any | None, page_no: int) -> float | None:
    """`_bbox()`의 BOTTOMLEFT → 좌상단 변환에 필요한 페이지 높이를 문서 메타데이터에서 조회한다."""
    pages = getattr(document, "pages", None)
    if pages is None:
        return None
    page = pages.get(page_no) if isinstance(pages, dict) else None
    size = getattr(page, "size", None)
    value = getattr(size, "height", None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence(item: Any) -> float | None:
    """아이템에서 confidence/score/conf 중 존재하는 값을 찾아 [0, 1] 범위로 클램프한다."""
    for name in ("confidence", "score", "conf"):
        value = getattr(item, name, None)
        if value is None:
            continue
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue
    return None


def _item_text(item: Any, *, document: Any | None = None) -> str:
    """아이템의 텍스트를 최대한 안전하게 뽑아낸다.

    우선 `text`/`orig` 속성을 직접 읽고, 없으면 `export_to_markdown`/`export_to_text`
    메서드 호출로 폴백한다 — docling 아이템 종류에 따라 텍스트 접근 방식이 다르기
    때문에 여러 경로를 순서대로 시도한다.
    """
    for attr_name in ("text", "orig"):
        value = getattr(item, attr_name, None)
        if value is not None:
            return str(value).strip()

    for method_name in ("export_to_markdown", "export_to_text"):
        text = _call_export(getattr(item, method_name, None), document=document)
        if text:
            return text.strip()

    return ""


def _table_text(item: Any, *, document: Any | None = None) -> str:
    """표 아이템을 Markdown 파이프 테이블 텍스트로 직렬화한다(STEP 3 filter가 표를
    Markdown으로 저장하는 정책, DESIGN.md §3과 일치시키기 위함).

    1순위로 docling이 제공하는 `export_to_markdown()`을 쓰고, 실패하면 표 데이터의
    grid를 직접 순회해 Markdown을 만들며, 그것도 안 되면 일반 텍스트 추출로 폴백한다.
    """
    text = _call_export(getattr(item, "export_to_markdown", None), document=document)
    if text:
        return text.strip()

    text = _markdown_from_grid(getattr(getattr(item, "data", None), "grid", None), document=document)
    if text:
        return text

    return _item_text(item, document=document)


def _call_export(method: Any, *, document: Any | None = None) -> str:
    """docling의 export 메서드를 호출한다. 일부 버전은 `doc=document` 인자를 요구하고
    일부는 인자 없이 호출해야 하므로, `doc` 키워드로 먼저 시도하고 TypeError면
    인자 없이 재시도한다."""
    if not callable(method):
        return ""
    if document is not None:
        try:
            return str(method(doc=document))
        except TypeError:
            pass
    try:
        return str(method())
    except TypeError:
        return ""


def _markdown_from_grid(grid: Any, *, document: Any | None = None) -> str:
    """표의 셀 grid(행×열)를 직접 순회해 Markdown 파이프 테이블 문자열을 만든다
    (`export_to_markdown()`을 쓸 수 없을 때의 폴백 경로)."""
    if grid is None:
        return ""

    rows: list[list[str]] = []
    for row in grid:
        rows.append([_cell_text(cell, document=document) for cell in row])
    if not rows:
        return ""

    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""
    padded_rows = [row + [""] * (width - len(row)) for row in rows]
    header = padded_rows[0]
    body = padded_rows[1:]
    lines = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |" for row in body)
    return "\n".join(lines)


def _cell_text(cell: Any, *, document: Any | None = None) -> str:
    """표 셀 하나의 텍스트를 한 줄로 정규화한다(줄바꿈은 공백으로 치환해 Markdown 표
    한 셀 안에서 줄이 깨지지 않게 한다)."""
    get_text = getattr(cell, "_get_text", None)
    if callable(get_text):
        try:
            return str(get_text(doc=document)).replace("\n", " ").strip()
        except TypeError:
            return str(get_text()).replace("\n", " ").strip()
    return str(getattr(cell, "text", "")).replace("\n", " ").strip()


def _escape_markdown_cell(text: str) -> str:
    """셀 값에 `|`가 있으면 Markdown 파이프 테이블 구분자와 충돌하므로 이스케이프한다."""
    return text.replace("|", r"\|")


def _is_adjacent_to_table(previous_item: Any | None, next_item: Any | None) -> bool:
    """caption 아이템의 문서 순서상 바로 앞/뒤가 표인지 확인한다 — table_caption과
    figure_caption을 구분하는 유일한 단서다(caption 자체는 어느 쪽인지 라벨을 갖지 않음)."""
    return _is_table_item(previous_item) or _is_table_item(next_item)


def _is_table_item(item: Any | None) -> bool:
    """아이템이 표인지 판정한다. 클래스명(`TableItem`)과 label 값("table") 둘 다 확인해
    docling 버전별로 표 표현 방식이 달라도 안정적으로 인식한다."""
    if item is None:
        return False
    return "TableItem" in _class_names(item) or _label_value(item) == "table"


def _is_picture_item(item: Any | None) -> bool:
    """아이템이 그림/차트인지 판정한다(클래스명 `PictureItem` 또는 picture/chart label)."""
    if item is None:
        return False
    return "PictureItem" in _class_names(item) or _label_value(item) in PICTURE_LABELS


def _class_names(item: Any) -> set[str]:
    """아이템 타입의 MRO(상속 체인) 전체 클래스명을 모은다. `isinstance` 대신 이름 문자열로
    비교하는 이유는 docling 패키지를 임포트하지 않고도(타입 힌트만으로) 판정 로직을 쓸 수
    있게 하기 위함이다."""
    return {cast(type, cls).__name__ for cls in type(item).mro()}
