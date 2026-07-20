"""STEP 8 relate — 신규 논문과 기존 논문들 사이의 연관도 점수를 계산한다.

DESIGN.md §3/§5.2에 정의된 공식대로 논문 임베딩 코사인 유사도(0.6) + 키워드
자카드 유사도(0.3) + 발행연도 근접도(0.1)를 가중합해 상위 N편을 `paper_relations`에
저장할 후보로 뽑는다. 검색 서비스는 이렇게 미리 계산해 둔 결과를 그대로 조회하므로
실시간 계산 없이 CPU에서도 수 초 내 응답할 수 있다(DESIGN.md §5.2).
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """두 벡터의 코사인 유사도. 길이가 다르거나 비어 있으면 0.0(무관계)으로 처리한다."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(left * right for left, right in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def jaccard(left: set[str], right: set[str]) -> float:
    """두 키워드 집합의 자카드 유사도(교집합/합집합). 한쪽이라도 비어 있으면 0.0."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def year_proximity(y1: int | None, y2: int | None) -> float:
    """발행연도 근접도. 10년 차이가 나면 0에 수렴하도록 선형 감쇠시킨 값(0~1)."""
    if y1 is None or y2 is None:
        return 0.0
    return max(0.0, 1.0 - abs(y1 - y2) / 10.0)


def relation_score(
    embedding_cosine: float,
    keyword_jaccard: float,
    year_score: float,
) -> float:
    """연관 논문 점수 공식(DESIGN.md §3 STEP 8):
    0.6×논문 임베딩 코사인 유사도 + 0.3×키워드 자카드 유사도 + 0.1×연도 근접도.
    임베딩 유사도에 가장 큰 비중을 두어 주제 근접성을 우선하고, 키워드 겹침으로
    보정하며, 연도는 최신성 참고 정도의 약한 가중치만 준다.
    """
    return 0.6 * embedding_cosine + 0.3 * keyword_jaccard + 0.1 * year_score


def build_relations(
    new_paper: Mapping[str, Any] | Any,
    candidates: Iterable[Mapping[str, Any] | Any],
    top_n: int = 20,
) -> list[tuple[int, float, str]]:
    """신규 논문 1편과 기존 후보 논문들의 관계 점수를 계산해 상위 top_n을 반환한다.

    STEP 8에서 호출되며, 반환값은 `repository.save_relations`를 통해
    `paper_relations` 테이블(source_paper_id, related_paper_id, relation_score,
    relation_reason)에 그대로 저장된다. `relation_reason`에는 점수 계산 근거와
    겹치는 키워드를 사람이 읽을 수 있는 문자열로 남겨 검색 결과 엑셀의 "연관 사유"
    컬럼(DESIGN.md §5.3)에 쓰인다.
    """
    new_id = _get(new_paper, "paper_id")
    new_embedding = _get(new_paper, "embedding") or []
    new_keywords = set(_get(new_paper, "keywords") or [])
    new_year = _get(new_paper, "published_year")
    scored: list[tuple[int, float, str]] = []

    for candidate in candidates:
        candidate_id = int(_get(candidate, "paper_id"))
        if new_id is not None and candidate_id == int(new_id):
            continue
        candidate_keywords = set(_get(candidate, "keywords") or [])
        overlap = sorted(new_keywords & candidate_keywords)
        embedding_cosine = cosine(
            new_embedding,
            _get(candidate, "embedding") or [],
        )
        keyword_jaccard = jaccard(new_keywords, candidate_keywords)
        year_score = year_proximity(new_year, _get(candidate, "published_year"))
        score = relation_score(embedding_cosine, keyword_jaccard, year_score)
        overlap_reason = (
            "겹치는 키워드: " + ", ".join(overlap)
            if overlap
            else "겹치는 키워드 없음"
        )
        reason = (
            f"관계 점수={score:.3f} "
            f"(논문 임베딩 cosine {embedding_cosine:.3f}*0.6="
            f"{0.6 * embedding_cosine:.3f}, 키워드 Jaccard {keyword_jaccard:.3f}*0.3="
            f"{0.3 * keyword_jaccard:.3f}, 연도 근접도 {year_score:.3f}*0.1="
            f"{0.1 * year_score:.3f}); {overlap_reason}"
        )
        scored.append((candidate_id, score, reason))

    # 점수 내림차순으로 정렬해 상위 top_n(기본 20편)만 남긴다 — 관계 테이블이
    # 논문 수에 대해 이차적으로 커지지 않도록 하는 제한이다.
    return sorted(scored, key=lambda item: item[1], reverse=True)[:top_n]


def _get(value: Mapping[str, Any] | Any, key: str) -> Any:
    """dict(Mapping)와 객체(ORM row 등) 양쪽 입력을 동일하게 다루기 위한 헬퍼."""
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key)
