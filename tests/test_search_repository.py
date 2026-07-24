from paperrag.search.repository import InMemorySearchRepository


def test_paragraphs_of_without_section_query_returns_all() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1)

    assert {row.section_name for row in rows} == {"Introduction", "Methods"}


def test_paragraphs_of_filters_by_section_query_case_insensitive() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1, section_query=["method"])

    assert len(rows) == 1
    assert rows[0].section_name == "Methods"


def test_paragraphs_of_matches_any_of_multiple_section_names() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1, section_query=["method", "intro"])

    assert {row.section_name for row in rows} == {"Introduction", "Methods"}


def test_paragraphs_of_blank_section_query_is_ignored() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1, section_query=["   "])

    assert len(rows) == 2


def test_paragraphs_of_no_match_returns_empty() -> None:
    repo = _repo()

    rows = repo.paragraphs_of(1, section_query=["존재하지않는섹션"])

    assert rows == []


def test_available_sections_orders_by_first_appearance_and_dedupes() -> None:
    repo = InMemorySearchRepository(
        paragraphs=[
            {
                "paper_id": 1,
                "paragraph_order": 1,
                "section_name": "Introduction",
                "original_text": "a",
            },
            {
                "paper_id": 1,
                "paragraph_order": 2,
                "section_name": "Methods",
                "original_text": "b",
            },
            {
                # 같은 섹션이 문서 뒷부분에서 다시 등장 — 중복 없이 첫 등장 위치만 남아야 한다.
                "paper_id": 1,
                "paragraph_order": 3,
                "section_name": "Introduction",
                "original_text": "c",
            },
        ],
    )

    assert repo.available_sections(1) == ["Introduction", "Methods"]


def test_available_sections_excludes_irrelevant_and_blank_sections() -> None:
    repo = InMemorySearchRepository(
        paragraphs=[
            {
                "paper_id": 1,
                "paragraph_order": 1,
                "section_name": "Introduction",
                "original_text": "a",
            },
            {
                "paper_id": 1,
                "paragraph_order": 2,
                "section_name": "Excluded",
                "original_text": "b",
                "is_topic_relevant": False,
            },
            {
                "paper_id": 1,
                "paragraph_order": 3,
                "section_name": "",
                "original_text": "c",
            },
        ],
    )

    assert repo.available_sections(1) == ["Introduction"]


def test_available_sections_scoped_to_paper_id() -> None:
    repo = _repo()

    assert repo.available_sections(1) == ["Introduction", "Methods"]
    assert repo.available_sections(999) == []


def test_top_matching_paragraph_returns_closest_by_cosine_similarity() -> None:
    repo = InMemorySearchRepository(
        paragraphs=[
            {
                "paper_id": 1,
                "paragraph_order": 1,
                "section_name": "Introduction",
                "original_text": "서론 원문",
                "cleaned_text": "서론 정제",
                "embedding": [0.0, 1.0],
            },
            {
                "paper_id": 1,
                "paragraph_order": 2,
                "section_name": "Methods",
                "original_text": "방법론 원문",
                "cleaned_text": "방법론 정제",
                "embedding": [1.0, 0.0],
            },
        ],
    )

    result = repo.top_matching_paragraph(1, [1.0, 0.0])

    assert result is not None
    assert result.section_name == "Methods"
    assert result.cleaned_text == "방법론 정제"


def test_top_matching_paragraph_excludes_irrelevant_and_missing_embedding() -> None:
    repo = InMemorySearchRepository(
        paragraphs=[
            {
                "paper_id": 1,
                "paragraph_order": 1,
                "section_name": "Excluded",
                "original_text": "a",
                "embedding": [1.0, 0.0],
                "is_topic_relevant": False,
            },
            {
                "paper_id": 1,
                "paragraph_order": 2,
                "section_name": "NoEmbedding",
                "original_text": "b",
            },
        ],
    )

    assert repo.top_matching_paragraph(1, [1.0, 0.0]) is None


def test_top_matching_paragraph_returns_none_for_empty_vector_or_no_paragraphs() -> None:
    repo = _repo()

    assert repo.top_matching_paragraph(1, []) is None
    assert repo.top_matching_paragraph(999, [1.0, 0.0]) is None


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
