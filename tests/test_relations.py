import math

import pytest

from paperrag.ingest.relations import (
    build_relations,
    cosine,
    jaccard,
    relation_score,
    year_proximity,
)


def test_relation_formula_components() -> None:
    assert cosine([1.0, 0.0], [0.5, math.sqrt(3) / 2]) == pytest.approx(0.5)
    assert jaccard({"rag", "pdf"}, {"pdf", "ocr"}) == 1 / 3
    assert year_proximity(2020, 2025) == 0.5
    assert year_proximity(2020, 2030) == 0.0
    assert relation_score(0.5, 1 / 3, 0.5) == pytest.approx(0.45)


def test_build_relations_returns_top_scores_with_overlap_reason() -> None:
    new_paper = {
        "paper_id": 10,
        "embedding": [1.0, 0.0],
        "keywords": {"rag", "pdf"},
        "published_year": 2024,
    }
    candidates = [
        {
            "paper_id": 20,
            "embedding": [1.0, 0.0],
            "keywords": {"rag", "ocr"},
            "published_year": 2023,
        },
        {
            "paper_id": 30,
            "embedding": [0.0, 1.0],
            "keywords": {"other"},
            "published_year": 2000,
        },
    ]

    relations = build_relations(new_paper, candidates, top_n=1)

    assert len(relations) == 1
    paper_id, score, reason = relations[0]
    assert paper_id == 20
    assert score == pytest.approx(0.6 * 1.0 + 0.3 * (1 / 3) + 0.1 * 0.9)
    assert "rag" in reason
    assert "논문 임베딩 cosine 1.000*0.6=0.600" in reason
    assert "키워드 Jaccard 0.333*0.3=0.100" in reason
    assert "연도 근접도 0.900*0.1=0.090" in reason
