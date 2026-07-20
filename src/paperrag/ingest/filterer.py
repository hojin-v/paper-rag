import re
from collections.abc import Sequence

from paperrag.config import Settings, get_settings
from paperrag.ingest.layout.dedup import deduplicate_layout_blocks
from paperrag.ingest.models import LayoutBlock

EXCLUDED = {"figure", "figure_caption", "formula", "header_footer", "reference"}
META_TYPES = {"title", "author", "abstract"}
TABLE_TYPES = {"table", "table_caption"}
REFERENCE_HEADER_RE = re.compile(r"^\s*(references|appendix|참고문헌|부록)\b", re.IGNORECASE)
ABSTRACT_HEADER_RE = re.compile(r"^\s*(abstract|초록|요약)\b", re.IGNORECASE)
FIGURE_CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?\.?|그림)\s*\d+\b", re.IGNORECASE)


def split_blocks(
    blocks: Sequence[LayoutBlock],
    *,
    settings: Settings | None = None,
) -> tuple[dict[str, list[LayoutBlock]], list[LayoutBlock], list[LayoutBlock]]:
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
            continue
        if block.block_type == "section_header":
            if ABSTRACT_HEADER_RE.match(block.text.strip()):
                continue
            body_started = True
        if block.block_type == "abstract" and body_started:
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
