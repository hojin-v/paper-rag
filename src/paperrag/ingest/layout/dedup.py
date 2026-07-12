from paperrag.ingest.models import LayoutBlock

SPECIALIZED_PRIORITY = {
    "title": 9,
    "author": 8,
    "abstract": 8,
    "table": 8,
    "section_header": 7,
    "table_caption": 7,
    "figure_caption": 7,
    "reference": 7,
    "figure": 6,
    "formula": 6,
    "header_footer": 5,
    "text": 1,
}


def deduplicate_layout_blocks(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    """동일 검출과 큰 컨테이너 박스를 제거하되 원래 객체와 순서는 보존한다."""
    kept: list[LayoutBlock] = []
    for candidate in blocks:
        merge_index = next(
            (
                index
                for index, current in enumerate(kept)
                if current.page == candidate.page
                and current.block_type == candidate.block_type == "title"
                and _smaller_overlap(current, candidate) >= 0.5
            ),
            None,
        )
        if merge_index is not None:
            current = kept[merge_index]
            kept[merge_index] = current.model_copy(
                update={
                    "bbox": _union_bbox(current, candidate),
                    "confidence": max(
                        current.confidence or 0.0,
                        candidate.confidence or 0.0,
                    ),
                    "order": min(current.order, candidate.order),
                }
            )
            continue
        duplicate_index = next(
            (
                index
                for index, current in enumerate(kept)
                if current.page == candidate.page and _iou(current, candidate) >= 0.85
            ),
            None,
        )
        if duplicate_index is None:
            kept.append(candidate)
            continue
        current = kept[duplicate_index]
        if _quality_key(candidate) > _quality_key(current):
            kept[duplicate_index] = candidate

    containers = {
        id(parent)
        for parent in kept
        if parent.block_type in {"abstract", "figure", "text"}
        and len(
            [
                child
                for child in kept
                if child is not parent
                and child.page == parent.page
                and _area(child) <= _area(parent) * 0.75
                and _contained_fraction(parent, child) >= 0.92
            ]
        )
        >= 2
    }
    return [block for block in kept if id(block) not in containers]


def _quality_key(block: LayoutBlock) -> tuple[int, float]:
    return (
        SPECIALIZED_PRIORITY.get(block.block_type, 0),
        block.confidence or 0.0,
    )


def _area(block: LayoutBlock) -> float:
    if block.bbox is None:
        return 0.0
    x1, y1, x2, y2 = block.bbox
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _intersection(first: LayoutBlock, second: LayoutBlock) -> float:
    if first.bbox is None or second.bbox is None:
        return 0.0
    ax1, ay1, ax2, ay2 = first.bbox
    bx1, by1, bx2, by2 = second.bbox
    return max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(
        0.0, min(ay2, by2) - max(ay1, by1)
    )


def _iou(first: LayoutBlock, second: LayoutBlock) -> float:
    intersection = _intersection(first, second)
    union = _area(first) + _area(second) - intersection
    return intersection / union if union > 0 else 0.0


def _contained_fraction(parent: LayoutBlock, child: LayoutBlock) -> float:
    child_area = _area(child)
    return _intersection(parent, child) / child_area if child_area > 0 else 0.0


def _smaller_overlap(first: LayoutBlock, second: LayoutBlock) -> float:
    smaller_area = min(_area(first), _area(second))
    return _intersection(first, second) / smaller_area if smaller_area > 0 else 0.0


def _union_bbox(
    first: LayoutBlock,
    second: LayoutBlock,
) -> tuple[float, float, float, float] | None:
    if first.bbox is None:
        return second.bbox
    if second.bbox is None:
        return first.bbox
    return (
        min(first.bbox[0], second.bbox[0]),
        min(first.bbox[1], second.bbox[1]),
        max(first.bbox[2], second.bbox[2]),
        max(first.bbox[3], second.bbox[3]),
    )
