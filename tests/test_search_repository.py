from paperrag.search.repository import InMemorySearchRepository


def test_paragraphs_of_without_section_query_returns_all() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1)

    assert {row.section_name for row in rows} == {"Introduction", "Methods"}


def test_paragraphs_of_filters_by_section_query_case_insensitive() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1, section_query="method")

    assert len(rows) == 1
    assert rows[0].section_name == "Methods"


def test_paragraphs_of_blank_section_query_is_ignored() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1, section_query="   ")

    assert len(rows) == 2


def test_paragraphs_of_no_match_returns_empty() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1, section_query="존재하지않는섹션")

    assert rows == []


def _repo() -> InMemorySearchRepository:
    return InMemorySearchRepository(
        paragraphs=[
            {
                "paper_id": 1,
                "paragraph_order": 1,
                "section_name": "Introduction",
                "original_text": "서론 원문",
                "cleaned_text": "서론 정제",
                "summary": "서론 요약",
                "is_topic_relevant": True,
            },
            {
                "paper_id": 1,
                "paragraph_order": 2,
                "section_name": "Methods",
                "original_text": "방법론 원문",
                "cleaned_text": "방법론 정제",
                "summary": "방법론 요약",
                "is_topic_relevant": True,
            },
        ],
    )
