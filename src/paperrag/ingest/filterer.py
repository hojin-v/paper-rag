"""STEP 3 filter — 레이아웃 블록을 메타/본문/표/제외 대상으로 분류한다.

DESIGN.md §3 STEP 3 근거: 그림·그림캡션·독립 수식·참고문헌 이후·부록은 제외하고,
표는 Markdown 등으로 직렬화해 별도로 포함하며, 제목/저자/초록은 메타데이터로만
취급한다(본문 단락에는 포함하지 않음). 이 모듈의 `split_blocks`가 그 정책을
실제로 구현하는 지점이다.
"""

import re
from collections.abc import Sequence

from paperrag.config import Settings, get_settings
from paperrag.ingest.layout.dedup import deduplicate_layout_blocks
from paperrag.ingest.models import LayoutBlock

# 본문/저장 대상에서 완전히 제외하는 블록 유형. 그림·수식·머리말푸리말은 텍스트
# 검색 가치가 낮고, reference는 "참고문헌 이후 전부 제외" 규칙으로 별도 처리된다.
EXCLUDED = {"figure", "figure_caption", "formula", "header_footer", "reference"}
# 본문 단락이 아니라 PaperMeta(제목/저자/초록)로 귀속되는 블록 유형.
META_TYPES = {"title", "author", "abstract"}
# 본문과 별도로 표 목록에 모아 Markdown 직렬화 대상으로 넘기는 블록 유형.
TABLE_TYPES = {"table", "table_caption"}
# "References/Appendix/참고문헌/부록" 절 헤더를 만나면 그 이후 블록은 전부 건너뛴다.
REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)
# 초록 절 헤더는 본문 section_header로 취급하지 않기 위한 판별용 패턴.
ABSTRACT_HEADER_RE = re.compile(r"^\s*(abstract|초록|요약)\b", re.IGNORECASE)
# "Fig. 1", "그림 2"처럼 그림 번호로 시작하는 캡션은 레이아웃 분류가 그림으로
# 못 잡았더라도 텍스트 패턴으로 걸러낸다.
FIGURE_CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?\.?|그림)\s*\d+\b", re.IGNORECASE)
# 저자가 직접 지정한 키워드/CCS 분류 목록의 라벨. 이런 블록은 본문이 아니라
# header_footer로 분류되지만(실제 연구 서술이 아니므로), 목록 자체는 STEP 6에서
# 대표 키워드 후보로 강제 포함시키기 위해 별도로 붙잡아 둔다(pipeline._extract_meta 참고).
AUTHOR_KEYWORDS_LABEL_RE = re.compile(
    r"^\s*(keywords?|key\s*words?|색인어|주요\s*검색어|ccs\s*concepts?)\s*[:：]",
    re.IGNORECASE,
)


def split_blocks(
    blocks: Sequence[LayoutBlock],
    *,
    settings: Settings | None = None,
) -> tuple[dict[str, list[LayoutBlock]], list[LayoutBlock], list[LayoutBlock]]:
    """레이아웃 블록을 (메타 블록 dict, 본문 블록 목록, 표 블록 목록)으로 분류한다.

    처리 순서: ① 중복 블록 제거 및 저자 영역 보정(`_recover_author_regions`)
    ② order 순 순회하며 참고문헌 헤더 이후는 전부 스킵 ③ 그림 캡션 텍스트 패턴·
    각주로 추정되는 블록 제외 ④ EXCLUDED 유형 제외(참고문헌 자체도 여기서 걸림)
    ⑤ 초록 절 헤더는 본문 섹션 시작으로 치지 않음 ⑥ 본문 시작 이후에 나오는
    abstract 블록(초록 재수록 등)은 본문 텍스트로 강등 ⑦ 나머지를 메타/표/본문으로 분배.
    반환된 표 블록은 `pipeline._build_tables`가, 본문 블록은 `paragraphs.build_paragraphs`가
    이어서 처리한다.
    """
    active_settings = settings or get_settings()
    normalized_blocks = _recover_author_regions(
        deduplicate_layout_blocks(list(blocks)),
        active_settings,
    )
    meta_blocks: dict[str, list[LayoutBlock]] = {block_type: [] for block_type in META_TYPES}
    body_blocks: list[LayoutBlock] = []
    table_blocks: list[LayoutBlock] = []
    after_references = False
    body_started = False
    page_extents = _page_extents(normalized_blocks)

    for block in sorted(normalized_blocks, key=lambda item: item.order):
        if after_references:
            continue
        if REFERENCE_HEADER_RE.match(block.text.strip()):
            after_references = True
            continue
        if FIGURE_CAPTION_RE.match(block.text.strip()):
            continue
        if _is_probable_footnote(block, page_extents, active_settings):
            continue
        if block.block_type in EXCLUDED:
            if block.block_type == "reference":
                after_references = True
            elif block.block_type == "header_footer" and AUTHOR_KEYWORDS_LABEL_RE.match(
                block.text.strip()
            ):
                # "Keywords:"/"CCS Concepts:" 블록은 본문에서는 여전히 제외하되,
                # 텍스트만 meta_blocks에 따로 모아 STEP 6에서 대표 키워드 후보로 쓸 수
                # 있게 한다(pipeline._extract_meta/_extract_author_keywords 참고).
                meta_blocks.setdefault("author_keywords", []).append(block)
            continue
        if block.block_type == "section_header":
            if ABSTRACT_HEADER_RE.match(block.text.strip()):
                continue
            body_started = True
        if block.block_type == "abstract" and body_started:
            # 본문 섹션이 이미 시작된 뒤에 나온 abstract 블록은(예: 재수록·오분류)
            # 메타데이터로 중복 취급하지 않고 본문 텍스트로 강등해 저장한다.
            body_blocks.append(block.model_copy(update={"block_type": "text"}))
            continue
        if block.block_type in META_TYPES:
            meta_blocks[block.block_type].append(block)
            continue
        if block.block_type in TABLE_TYPES:
            table_blocks.append(block)
            continue
        if block.block_type in {"section_header", "text"}:
            body_blocks.append(block)

    return meta_blocks, body_blocks, table_blocks


def _recover_author_regions(
    blocks: Sequence[LayoutBlock],
    settings: Settings,
) -> list[LayoutBlock]:
    """제목과 초록/섹션 헤더 사이에 낀 "text" 블록을 author로 재분류한다.

    2026-07-12 실측 평가(docs/reports/assessments/2026-07-12-two-paper-ocr-evaluation.md)에서
    저자/소속이 본문(text)으로 오분류되어 DB 저자 컬럼이 비는 사례가 확인됐다.
    이 보정은 첫 페이지에서 title 블록 다음, abstract 또는 다음 section_header
    이전 구간의 text 블록을 author로 승격시켜 완화한다. 실험적 보정이므로
    `Settings.paddle_author_region_recovery` 플래그로 끌 수 있다.
    """
    ordered = sorted(blocks, key=lambda item: item.order)
    if not settings.paddle_author_region_recovery or not ordered:
        return ordered
    first_page = min(block.page for block in ordered)
    page_blocks = [block for block in ordered if block.page == first_page]
    title_orders = [block.order for block in page_blocks if block.block_type == "title"]
    if not title_orders:
        return ordered
    title_end = max(title_orders)
    boundary_orders = [
        block.order
        for block in page_blocks
        if block.order > title_end
        and block.block_type in {"abstract", "section_header"}
    ]
    if not boundary_orders:
        return ordered
    boundary = min(boundary_orders)
    return [
        block.model_copy(update={"block_type": "author"})
        if block.page == first_page
        and title_end < block.order < boundary
        and block.block_type == "text"
        else block
        for block in ordered
    ]


def _page_extents(
    blocks: Sequence[LayoutBlock],
) -> dict[int, tuple[float, float, float]]:
    """페이지별 (최소 x, 최대 x, 최대 y) bbox 경계를 계산한다.

    각주 위치 판정(`_is_probable_footnote`)이 페이지 하단/폭 비율을 기준으로
    동작하려면 페이지의 실제 콘텐츠 영역 크기를 알아야 하므로 미리 집계해 둔다.
    """
    extents: dict[int, tuple[float, float, float]] = {}
    for block in blocks:
        if block.bbox is None:
            continue
        x1, _, x2, y2 = block.bbox
        page_min_x, page_max_x, page_max_y = extents.get(
            block.page,
            (x1, x2, y2),
        )
        extents[block.page] = (
            min(page_min_x, x1),
            max(page_max_x, x2),
            max(page_max_y, y2),
        )
    return extents


def _is_probable_footnote(
    block: LayoutBlock,
    page_extents: dict[int, tuple[float, float, float]],
    settings: Settings,
) -> bool:
    """짧고 페이지 하단 구석에 위치한 text 블록을 각주로 추정해 본문에서 제외한다.

    레이아웃 모델이 각주를 별도 유형으로 분류하지 않는 경우가 있어, 글자 수·
    세로 위치(페이지 하단 비율)·높이·폭 비율 임계값(모두 Settings의
    footnote_* 값)을 함께 만족할 때만 각주로 판정하는 휴리스틱이다.
    """
    if (
        not settings.footnote_filter_enabled
        or block.block_type != "text"
        or block.bbox is None
        or len(block.text.strip()) > settings.footnote_max_chars
    ):
        return False
    extent = page_extents.get(block.page)
    if extent is None:
        return False
    page_min_x, page_max_x, page_max_y = extent
    page_width = page_max_x - page_min_x
    if page_width <= 0.0 or page_max_y <= 0.0:
        return False
    x1, y1, x2, y2 = block.bbox
    return (
        y1 >= page_max_y * settings.footnote_bottom_ratio
        and y2 - y1 <= page_max_y * settings.footnote_max_height_ratio
        and x2 - x1 <= page_width * settings.footnote_max_width_ratio
    )
