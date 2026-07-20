import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(left * right for left, right in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def year_proximity(y1: int | None, y2: int | None) -> float:
    if y1 is None or y2 is None:
        return 0.0
    return max(0.0, 1.0 - abs(y1 - y2) / 10.0)


def relation_score(
    embedding_cosine: float,
    keyword_jaccard: float,
    year_score: float,
) -> float:
    return 0.6 * embedding_cosine + 0.3 * keyword_jaccard + 0.1 * year_score


def build_relations(
    new_paper: Mapping[str, Any] | Any,
    candidates: Iterable[Mapping[str, Any] | Any],
    top_n: int = 20,
) -> list[tuple[int, float, str]]:
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

    return sorted(scored, key=lambda item: item[1], reverse=True)[:top_n]


def _get(value: Mapping[str, Any] | Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key)
