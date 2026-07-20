from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paperrag.config import Settings
from paperrag.search.repository import InMemorySearchRepository
from paperrag.search.schemas import SearchMatched, SearchSuggest
from paperrag.search.service import SearchService


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        if self.responses:
            return self.responses.pop(0)
        return {"keywords": ["unknown"]}


class RaisingLLM:
    """호출되면 즉시 실패하는 LLM 더블. 기본 검색 경로가 LLM을 전혀 부르지 않음을 증명하는 데 쓴다."""

    def generate_json(self, prompt: str, schema_hint: str) -> dict[str, Any]:
        raise AssertionError("기본 검색 경로(use_llm=False)는 LLM을 호출하면 안 된다.")


class StaticEmbeddingClient:
    def __init__(self) -> None:
        self.vectors = {
            "rag": [1.0, 0.0],
            "unknown": [1.0, 0.0],
            "orphan": [1.0, 0.0],
            "ocr": [0.9, 0.1],
            "vector search": [0.8, 0.2],
            "low": [0.0, 1.0],
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors.get(text.lower(), [0.0, 1.0]) for text in texts]


def test_exact_match_returns_matched(tmp_path: Path) -> None:
    service = _service(tmp_path, [{"keywords": ["RAG"]}])

    result = service.search("RAG 관련 대표 논문")

    assert isinstance(result, SearchMatched)
    assert result.match_type == "exact"
    assert result.matched_keyword == "RAG"
    assert result.primary_paper.paper_id == 10
    assert result.related_paper is not None
    assert result.related_paper.paper_id == 30


def test_unmatched_query_returns_three_suggestions(tmp_path: Path) -> None:
    service = _service(tmp_path, [{"keywords": ["unknown"]}])

    result = service.search("unknown")

    assert isinstance(result, SearchSuggest)
    assert len(result.candidates) == 3
    assert [candidate.keyword for candidate in result.candidates] == [
        "RAG",
        "OCR",
        "Vector Search",
    ]


def test_select_resolves_primary_and_related_paper(tmp_path: Path) -> None:
    service = _service(tmp_path, [{"keywords": ["unknown"]}])
    suggestion = service.search("unknown")
    assert isinstance(suggestion, SearchSuggest)

    result = service.select(suggestion.session_id, suggestion.candidates[0].keyword_id)

    assert result.match_type == "selected"
    assert result.primary_paper.paper_id == 10
    assert result.related_paper is not None
    assert result.related_paper.title == "OCR Related Paper"


def test_primary_score_formula_is_numeric(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.resolve(1, "RAG 관련 논문", "exact")

    assert result.primary_paper.score == pytest.approx(0.89)
    assert "키워드 0.800*0.5=0.400" in result.primary_paper.reason
    assert "단락 1.000*0.3=0.300" in result.primary_paper.reason
    assert "제목/초록 1.000*0.1=0.100" in result.primary_paper.reason
    assert "연도 0.900*0.1=0.090" in result.primary_paper.reason


def test_similarity_threshold_filters_low_candidates(tmp_path: Path) -> None:
    service = _service(tmp_path, [{"keywords": ["unknown"]}])

    result = service.search("unknown")

    assert isinstance(result, SearchSuggest)
    assert all(candidate.similarity >= 0.6 for candidate in result.candidates)
    assert {candidate.keyword_id for candidate in result.candidates} == {1, 2, 3}


def test_orphan_keyword_is_not_exact_match_or_suggestion(tmp_path: Path) -> None:
    service = _service(tmp_path, [{"keywords": ["orphan"]}])

    result = service.search("orphan")

    assert isinstance(result, SearchSuggest)
    assert all(candidate.keyword_id != 5 for candidate in result.candidates)


def test_default_search_never_calls_llm(tmp_path: Path) -> None:
    """use_llm 기본값(False)에서는 RaisingLLM이 주입돼도 절대 호출되지 않아야 한다.

    빠른 경로(형태소 분석 또는 정규식 폴백)만으로 정확 매칭까지 완료되는지 확인한다.
    """
    settings = Settings(
        _env_file=None,
        result_dir=tmp_path,
        search_suggestion_limit=3,
        search_similarity_threshold=0.6,
        embed_dim=2,
    )
    service = SearchService(_repo(), RaisingLLM(), StaticEmbeddingClient(), settings)

    result = service.search("RAG 관련 대표 논문")

    assert isinstance(result, SearchMatched)
    assert result.match_type == "exact"
    assert result.primary_paper.paper_id == 10


def test_use_llm_true_invokes_llm_path(tmp_path: Path) -> None:
    """use_llm=True면 질의 문자열에 없는 키워드도 LLM이 추출한 값을 그대로 써서 매칭한다.

    질의에는 "RAG"라는 문자열이 전혀 없으므로 빠른 경로(형태소 분석/정규식)로는 절대
    "RAG"를 추출할 수 없다 — 정확 매칭이 성공했다는 것 자체가 LLM 경로가 실제로
    쓰였다는 증거다.
    """
    service = _service(tmp_path, [{"keywords": ["RAG"]}])

    result = service.search("이 논문에 대해서 뭔가 알려줘", use_llm=True)

    assert isinstance(result, SearchMatched)
    assert result.match_type == "exact"
    assert result.primary_paper.paper_id == 10


def test_extract_noun_phrases_strips_korean_particles_with_kiwi() -> None:
    """Kiwi가 설치된 환경에서는 조사가 붙은 질의에서도 명사(구)만 뽑혀야 한다.

    kiwipiepy가 없는 개발 환경(이 저장소의 기본 .venv 등)에서는 스킵된다 —
    ingest-full/ocr extra를 설치한 환경에서만 실제로 검증된다.
    """
    pytest.importorskip("kiwipiepy")
    from paperrag.search.service import _extract_noun_phrases

    phrases = _extract_noun_phrases("스마트팩토리에서 이상탐지 연구")

    assert "스마트팩토리" in phrases


def _service(tmp_path: Path, llm_responses: list[dict[str, Any]] | None = None) -> SearchService:
    settings = Settings(
        _env_file=None,
        result_dir=tmp_path,
        search_suggestion_limit=3,
        search_similarity_threshold=0.6,
        embed_dim=2,
    )
    return SearchService(
        _repo(),
        FakeLLM(llm_responses),
        StaticEmbeddingClient(),
        settings,
    )


def _repo() -> InMemorySearchRepository:
    current_year = datetime.now(UTC).year
    return InMemorySearchRepository(
        keywords=[
            {
                "keyword_id": 1,
                "keyword": "rag",
                "display_form": "RAG",
                "frequency": 10,
                "embedding": [1.0, 0.0],
            },
            {
                "keyword_id": 2,
                "keyword": "ocr",
                "display_form": "OCR",
                "frequency": 5,
                "embedding": [0.9, 0.1],
            },
            {
                "keyword_id": 3,
                "keyword": "vector search",
                "display_form": "Vector Search",
                "frequency": 3,
                "embedding": [0.8, 0.2],
            },
            {
                "keyword_id": 4,
                "keyword": "low",
                "display_form": "Low",
                "frequency": 1,
                "embedding": [0.0, 1.0],
            },
            {
                "keyword_id": 5,
                "keyword": "orphan",
                "display_form": "Orphan",
                "frequency": 20,
                "embedding": [1.0, 0.0],
            },
        ],
        papers=[
            {
                "paper_id": 10,
                "title": "RAG Retrieval Study",
                "authors": "Kim; Lee",
                "published_year": current_year - 1,
                "journal": "Journal of Search",
                "abstract": "RAG improves paper search.",
                "abstract_summary": "RAG 검색을 개선한다.",
                "full_text_link": "https://example.test/rag",
            },
            {
                "paper_id": 20,
                "title": "Older RAG Study",
                "authors": "Park",
                "published_year": current_year - 5,
                "journal": "Archive",
                "abstract": "RAG baseline.",
                "abstract_summary": "이전 RAG 기준선.",
            },
            {
                "paper_id": 30,
                "title": "OCR Related Paper",
                "authors": "Choi",
                "published_year": current_year,
                "journal": "Related Journal",
                "abstract": "OCR and RAG are combined.",
                "abstract_summary": "OCR와 RAG를 결합한다.",
            },
        ],
        paper_keywords=[
            {"paper_id": 10, "keyword_id": 1, "score": 0.8},
            {"paper_id": 20, "keyword_id": 1, "score": 0.7},
            {"paper_id": 30, "keyword_id": 2, "score": 0.9},
            {"paper_id": 30, "keyword_id": 3, "score": 0.4},
        ],
        paragraphs=[
            {
                "paper_id": 10,
                "paragraph_order": 1,
                "section_name": "Introduction",
                "original_text": "RAG 원문 단락",
                "cleaned_text": "RAG cleaned paragraph",
                "summary": "RAG summary",
                "embedding": [1.0, 0.0],
                "keywords": ["RAG"],
            },
            {
                "paper_id": 20,
                "paragraph_order": 1,
                "original_text": "Older RAG paragraph",
                "cleaned_text": "Older cleaned paragraph",
                "summary": "Older summary",
                "embedding": [0.5, 0.8660254038],
                "keywords": ["RAG"],
            },
            {
                "paper_id": 30,
                "paragraph_order": 1,
                "section_name": "Related",
                "original_text": "OCR 원문 단락",
                "cleaned_text": "OCR cleaned paragraph",
                "summary": "OCR summary",
                "embedding": [0.9, 0.1],
                "keywords": ["OCR"],
            },
        ],
        tables=[
            {
                "paper_id": 10,
                "table_title": "Table 1. Scores",
                "table_text": "metric | value\nf1 | 0.90",
                "table_summary": "RAG 점수 표",
            }
        ],
        relations=[
            {
                "source_paper_id": 10,
                "related_paper_id": 30,
                "relation_score": 0.77,
                "relation_reason": "겹치는 키워드: RAG",
            }
        ],
    )
