"""레이아웃 검출 결과에서 겹치는/중복된 블록을 자동으로 정리하는 후처리 유틸리티.

PP-StructureV3(및 파생 레이아웃 검출기)는 같은 영역을 여러 박스로 중복 검출하거나,
작은 블록들을 통째로 감싸는 큰 컨테이너 박스를 함께 뱉어내는 경우가 있다. 이 모듈은
그런 잡음을 규칙 기반으로 제거해 STEP 2(layout) 산출물의 블록 수를 실제 논문 구조에
가깝게 맞춘다. 현재는 `paddle_backend.py`(운영 경로)에서 호출된다.
"""

from paperrag.ingest.models import LayoutBlock

# 블록 타입별 "신뢰 우선순위". 두 박스가 사실상 같은 영역을 가리키는 중복 검출로
# 판정됐을 때, 이 값이 더 높은 타입을 가진 블록을 남긴다(같은 값이면 confidence로 재비교).
# title/author/abstract/table처럼 의미가 뚜렷하고 다운스트림(STEP 3 filter, 메타데이터
# 추출)에 직접 쓰이는 타입일수록 높게, 범용 "text"는 가장 낮게 두어 구조화된 타입이
# 일반 텍스트 타입에 밀리지 않도록 한다.
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
    """동일 검출과 큰 컨테이너 박스를 제거하되 원래 객체와 순서는 보존한다.

    입력 순서를 그대로 훑으며 4단계로 정리한다:
    1) 같은 페이지·같은 타입("title")이면서 작은 쪽 면적 기준 겹침 비율이 50% 이상인
       박스는 별개 검출이 아니라 "제목이 여러 줄/여러 박스로 쪼개져 검출된 것"으로 보고
       bbox를 합집합으로 병합한다(title은 쪼개짐 폐해가 크기 때문에 병합, 다른 타입은
       아래 3)/4) 단계에서 별도로 처리).
    2) 같은 페이지에서 IoU(교집합/합집합) 0.85 이상이면 "같은 대상을 중복 검출"한 것으로
       보고 `SPECIALIZED_PRIORITY`(+confidence) 기준으로 더 나은 쪽만 남긴다.
    3) abstract/figure/text 타입 중, 자기 안에 (면적 75% 이하이면서 92% 이상 포함되는)
       자식 블록을 2개 이상 담고 있는 "큰 컨테이너 박스"는 통째로 제거한다 — 실제로는
       개별 자식 블록들이 진짜 콘텐츠이고, 부모 박스는 레이아웃 모델이 잘못 그려낸
       바깥 테두리인 경우가 많기 때문이다.
    4) 컨테이너 제거 후에도 남아있는, 같은 타입(text/section_header)이면서 더 작은 쪽이
       더 큰 쪽에 92% 이상 포함되는 경우는 작은 쪽을 중복으로 보고 제거한다.

    임계값(0.5/0.85/0.75/0.92)은 실측 벤치마크(합성 PDF + PP-StructureV3 결과)로 조정된
    경험적 수치이며, 정답 라벨과의 IoU/TEDS 비교가 아니라 "중복 박스가 사라지는지"를
    보고 튜닝됐다.
    """
    kept: list[LayoutBlock] = []
    for candidate in blocks:
        # 1) title 조각 병합: 같은 페이지의 기존 title과 겹침 비율이 충분히 크면
        #    새 후보를 별도 블록으로 추가하지 않고 bbox만 넓혀서 흡수한다.
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
        # 2) 일반 중복 제거: IoU가 매우 높으면(0.85 이상) 같은 대상을 가리키는 중복
        #    검출로 보고, 타입 우선순위·confidence가 더 나은 블록만 남긴다.
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

    # 3) 컨테이너 박스 제거: abstract/figure/text 타입이면서 자기보다 훨씬 작고(75% 이하)
    #    거의 완전히 포함되는(92% 이상) 자식을 2개 이상 담고 있으면 "잘못 그려진 큰 박스"로
    #    보고 제거한다. text 컨테이너는 안에 figure/formula/table처럼 이질적인 자식이 있으면
    #    (그 자체가 별도 콘텐츠일 가능성이 높으므로) 컨테이너 판정에서 제외한다.
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
                and (
                    parent.block_type != "text"
                    or child.block_type not in {"figure", "formula", "table"}
                )
            ]
        )
        >= 2
    }
    without_containers = [block for block in kept if id(block) not in containers]
    # 4) 같은 타입(text/section_header)끼리 한쪽이 다른 쪽에 거의 포함되는 경우, 더 작은
    #    블록은 같은 내용을 중복으로 담고 있을 가능성이 높아 제거한다.
    contained_duplicates = {
        id(child)
        for child in without_containers
        for parent in without_containers
        if child is not parent
        and child.page == parent.page
        and child.block_type == parent.block_type
        and child.block_type in {"text", "section_header"}
        and _area(child) < _area(parent)
        and _contained_fraction(parent, child) >= 0.92
    }
    return [block for block in without_containers if id(block) not in contained_duplicates]


def _quality_key(block: LayoutBlock) -> tuple[int, float]:
    """중복 후보 중 어느 블록을 남길지 비교하는 정렬 키. (타입 우선순위, confidence) 순으로 비교한다."""
    return (
        SPECIALIZED_PRIORITY.get(block.block_type, 0),
        block.confidence or 0.0,
    )


def _area(block: LayoutBlock) -> float:
    """블록 bbox의 면적. bbox가 없으면 0을 반환한다(면적 비교에서 항상 밀리도록)."""
    if block.bbox is None:
        return 0.0
    x1, y1, x2, y2 = block.bbox
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _intersection(first: LayoutBlock, second: LayoutBlock) -> float:
    """두 블록 bbox의 교집합 면적. 겹치지 않으면(음수 폭/높이) 0으로 클램프한다."""
    if first.bbox is None or second.bbox is None:
        return 0.0
    ax1, ay1, ax2, ay2 = first.bbox
    bx1, by1, bx2, by2 = second.bbox
    return max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(
        0.0, min(ay2, by2) - max(ay1, by1)
    )


def _iou(first: LayoutBlock, second: LayoutBlock) -> float:
    """IoU(Intersection over Union) — 두 박스가 "같은 영역"일 가능성을 재는 표준 지표."""
    intersection = _intersection(first, second)
    union = _area(first) + _area(second) - intersection
    return intersection / union if union > 0 else 0.0


def _contained_fraction(parent: LayoutBlock, child: LayoutBlock) -> float:
    """child 면적 대비 parent와 겹치는 비율. 1에 가까울수록 child가 parent 안에 통째로 들어있다는 뜻."""
    child_area = _area(child)
    return _intersection(parent, child) / child_area if child_area > 0 else 0.0


def _smaller_overlap(first: LayoutBlock, second: LayoutBlock) -> float:
    """두 박스 중 더 작은 쪽의 면적 대비 겹침 비율. title 조각 병합 판정에 쓴다(IoU와 달리
    한쪽이 다른 쪽을 완전히 감싸지 않는 "옆으로 이어진 줄 조각" 케이스도 잡아낸다)."""
    smaller_area = min(_area(first), _area(second))
    return _intersection(first, second) / smaller_area if smaller_area > 0 else 0.0


def _union_bbox(
    first: LayoutBlock,
    second: LayoutBlock,
) -> tuple[float, float, float, float] | None:
    """두 블록 bbox를 감싸는 최소 사각형(합집합 bbox)을 계산한다. 한쪽이 없으면 다른 쪽을 그대로 쓴다."""
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
