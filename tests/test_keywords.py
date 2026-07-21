import paperrag.ingest.keywords as keywords
from paperrag.ingest.keywords import KeywordScore


def test_normalize_fallback_path(monkeypatch) -> None:
    monkeypatch.setattr(keywords, "_kiwi", lambda: None)

    assert keywords.normalize("  Deep   Learning  ") == "deep learning"


def test_keyword_score_formula() -> None:
    score = KeywordScore().compute(
        "RAG",
        title="RAG based paper retrieval",
        abstract="This paper describes a RAG system.",
        body_frequency=2,
        max_body_frequency=4,
    )

    assert score == 0.3 + 0.2 + 0.2 * 0.5


def test_keyword_score_adds_author_weight_even_without_title_or_abstract_hit() -> None:
    score = KeywordScore().compute(
        "그래프 신경망",
        title="RAG based paper retrieval",
        abstract="This paper describes a RAG system.",
        body_frequency=0,
        max_body_frequency=4,
        is_author_keyword=True,
    )

    assert score == 0.3


def test_keyword_score_author_and_title_hits_stack() -> None:
    score = KeywordScore().compute(
        "RAG",
        title="RAG based paper retrieval",
        abstract="This paper describes a RAG system.",
        body_frequency=2,
        max_body_frequency=4,
        is_author_keyword=True,
    )

    assert score == 0.3 + 0.2 + 0.2 * 0.5 + 0.3
