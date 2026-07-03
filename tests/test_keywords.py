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

    assert score == 0.4 + 0.3 + 0.3 * 0.5
