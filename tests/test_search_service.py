from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paperrag.config import Settings
from paperrag.search.repository import CachedKeywordResult, InMemorySearchRepository
from paperrag.search.schemas import PaperSummary, SearchMatched, SearchSuggest
from paperrag.search.service import SearchService


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.prompts: list[str] = []

    def generate_json(self, prompt: str, schema_hint: str, operation: str = "") -> dict[str, Any]:
        self.prompts.append(prompt)
        if self.responses:
            return self.responses.pop(0)
        return {"keywords": ["unknown"]}


class RaisingLLM:
    """호출되면 즉시 실패하는 LLM 더블. 특정 경로가 LLM을 전혀 부르지 않아야 함을 증명하는 데 쓴다

    (예: 캐시 히트 경로, 이미 생성된 relevance_summary 재사용 경로).
    """

    def generate_json(self, prompt: str, schema_hint: str, operation: str = "") -> dict[str, Any]:
        raise AssertionError("이 경로는 LLM을 호출하면 안 된다.")


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
    # 대표(10)와 연관(30) 논문은 섹션 구성이 다른 별개 논문이라 독립적으로 노출된다.
    assert result.primary_available_sections == ["Introduction"]
    assert result.related_available_sections == ["Related"]


def test_resolve_include_abstract_false_blanks_abstract_and_bypasses_cache(
    tmp_path: Path,
) -> None:
    """include_abstract=False는 기본 뷰가 아니므로 캐시를 타지 않고, 초록 칸을 비워야 한다."""
    service = _service(tmp_path, [{"keywords": ["RAG"]}])

    result = service.search("RAG 관련 대표 논문", include_abstract=False)

    assert isinstance(result, SearchMatched)
    bundle_path = Path(service.settings.result_dir) / f"{result.result_id}.xlsx"
    assert bundle_path.exists()

    from openpyxl import load_workbook

    workbook = load_workbook(bundle_path)
    sheet = workbook["대표 논문 정보"]
    header = [cell.value for cell in sheet[1]]
    row = [cell.value for cell in sheet[2]]
    values = dict(zip(header, row, strict=True))
    assert values["초록 원문"] in (None, "")
    assert values["초록 요약"] in (None, "")


def test_search_generates_relevance_summary_for_primary_paper(tmp_path: Path) -> None:
    """대표 논문에는 LLM이 생성한 관련도 설명(RAG 생성 단계)이 채워져야 한다."""
    service = _service(
        tmp_path,
        [{"keywords": ["RAG"]}, {"summary": "이 논문은 RAG 검색을 다룬다."}],
    )

    result = service.search("RAG 관련 대표 논문")

    assert isinstance(result, SearchMatched)
    assert result.primary_paper.relevance_summary == "이 논문은 RAG 검색을 다룬다."


def test_relevance_summary_reused_from_cache_without_calling_llm_again(tmp_path: Path) -> None:
    """같은 keyword_id로 다시 계산할 때 이미 생성된 관련도 설명은 LLM을 다시 부르지 않고 재사용해야 한다."""
    service = _service(
        tmp_path,
        [{"keywords": ["RAG"]}, {"summary": "이 논문은 RAG 검색을 다룬다."}],
    )
    first = service.search("RAG 관련 대표 논문")
    assert isinstance(first, SearchMatched)
    assert first.primary_paper.relevance_summary == "이 논문은 RAG 검색을 다룬다."

    # 캐시된 값이 있으므로 이후 호출에서 LLM을 다시 부르면 즉시 실패해야 한다.
    service.llm = RaisingLLM()
    second = service.resolve(
        1,
        "RAG 관련 대표 논문",
        "exact",
        matched_keyword="RAG",
        force_refresh=True,
    )

    assert second.primary_paper.relevance_summary == "이 논문은 RAG 검색을 다룬다."


def test_relevance_summary_falls_back_to_paragraph_text_when_llm_fails(tmp_path: Path) -> None:
    """관련도 설명 생성이 실패해도 검색 자체는 막히지 않고 근거 단락 원문으로 대체해야 한다."""
    service = SearchService(_repo(), RaisingLLM(), StaticEmbeddingClient(), _settings(tmp_path))

    result = service.resolve(1, "RAG 관련 논문", "exact", matched_keyword="RAG", include_related=False)

    assert result.primary_paper.relevance_summary == "RAG cleaned paragraph"


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


def test_search_extracts_keywords_via_llm(tmp_path: Path) -> None:
    """질의 키워드 추출은 항상 LLM을 거친다 — 질의 문자열에 없는 키워드도 LLM이

    추출한 값을 그대로 써서 매칭한다. 질의에는 "RAG"라는 문자열이 전혀 없으므로
    내부 안전망(형태소 분석/정규식)으로는 절대 "RAG"를 추출할 수 없다 — 정확
    매칭이 성공했다는 것 자체가 LLM 경로가 실제로 쓰였다는 증거다.
    """
    service = _service(tmp_path, [{"keywords": ["RAG"]}])

    result = service.search("이 논문에 대해서 뭔가 알려줘")

    assert isinstance(result, SearchMatched)
    assert result.match_type == "exact"
    assert result.primary_paper.paper_id == 10


def test_query_keywords_prompt_instructs_llm_not_to_translate(tmp_path: Path) -> None:
    """실측 재현된 버그: 영어 질의("Structured Document Understanding")를 LLM이

    깨진 한글로 오역해("구조화되ᄂ 문서 이해") 저장된 정확 키워드와 매칭이 실패했다
    (2026-07-22, 2회 재현). 프롬프트가 원문 언어 유지 지시와 두 언어 few-shot
    예시를 포함해야 이 회귀를 막을 수 있다.
    """
    llm = FakeLLM([{"keywords": ["Structured Document Understanding"]}])
    service = SearchService(
        _repo(),
        llm,
        StaticEmbeddingClient(),
        Settings(_env_file=None, result_dir=tmp_path, embed_dim=2),
    )

    service.extract_keywords("Structured Document Understanding")

    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert "번역하지 마라" in prompt
    assert "Structured Document Understanding" in prompt
    assert "Structured Document Understanding" in prompt.split("질의:")[0]


def test_include_related_false_skips_relation_lookup_and_response(tmp_path: Path) -> None:
    """include_related=False면 top_relation 조회 자체를 건너뛰고 related_paper도 None이어야 한다."""
    repo = _repo()

    def _raise_if_called(paper_id: int) -> tuple[int, float, str] | None:
        raise AssertionError("include_related=False인데 top_relation이 호출됐다.")

    repo.top_relation = _raise_if_called  # type: ignore[method-assign]
    settings = Settings(
        _env_file=None,
        result_dir=tmp_path,
        search_suggestion_limit=3,
        search_similarity_threshold=0.6,
        embed_dim=2,
    )
    service = SearchService(repo, FakeLLM([{"keywords": ["RAG"]}]), StaticEmbeddingClient(), settings)

    result = service.search("RAG 관련 대표 논문", include_related=False)

    assert isinstance(result, SearchMatched)
    assert result.related_paper is None


def test_include_tables_false_skips_table_lookup(tmp_path: Path) -> None:
    """include_tables=False면 tables_of 조회 자체를 건너뛰어야 한다."""
    repo = _repo()

    def _raise_if_called(paper_id: int) -> list[Any]:
        raise AssertionError("include_tables=False인데 tables_of가 호출됐다.")

    repo.tables_of = _raise_if_called  # type: ignore[method-assign]
    settings = Settings(
        _env_file=None,
        result_dir=tmp_path,
        search_suggestion_limit=3,
        search_similarity_threshold=0.6,
        embed_dim=2,
    )
    service = SearchService(repo, FakeLLM([{"keywords": ["RAG"]}]), StaticEmbeddingClient(), settings)

    result = service.search("RAG 관련 대표 논문", include_tables=False)

    assert isinstance(result, SearchMatched)


class RaisingEmbedder:
    """임베딩 호출 시 즉시 실패하는 더블. 캐시 히트가 임베딩 호출조차 안 하는지 증명하는 데 쓴다."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("캐시 히트인데 임베딩이 호출됐다.")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        result_dir=tmp_path,
        search_suggestion_limit=3,
        search_similarity_threshold=0.6,
        embed_dim=2,
    )


def test_resolve_default_view_uses_cache_and_skips_scoring(tmp_path: Path) -> None:
    """기본 뷰(섹션 필터 없음, 연관·표 포함)에서 캐시가 있으면 점수 계산·임베딩을 건너뛴다."""
    repo = _repo()
    repo.keyword_result_cache[1] = CachedKeywordResult(
        result_id="r-cached-0001",
        excel_path="/tmp/cached.xlsx",
        primary_paper=PaperSummary(
            paper_id=999, title="Cached Paper", score=0.42, reason="캐시된 대표 사유"
        ),
        related_paper=None,
    )

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("캐시 히트인데 점수 계산 경로가 호출됐다.")

    repo.papers_for_keyword = _raise  # type: ignore[method-assign]
    repo.top_relation = _raise  # type: ignore[method-assign]
    service = SearchService(repo, RaisingLLM(), RaisingEmbedder(), _settings(tmp_path))

    result = service.resolve(1, "RAG 관련 논문", "exact", matched_keyword="RAG")

    assert result.result_id == "r-cached-0001"
    assert result.primary_paper.paper_id == 999
    assert result.primary_paper.title == "Cached Paper"


def test_resolve_section_query_bypasses_cache_even_if_present(tmp_path: Path) -> None:
    """primary_section_query가 있으면 캐시가 있어도 쓰지 않고 새로 계산해야 한다."""
    repo = _repo()
    repo.keyword_result_cache[1] = CachedKeywordResult(
        result_id="r-cached-0001",
        excel_path="/tmp/cached.xlsx",
        primary_paper=PaperSummary(
            paper_id=999, title="Cached Paper", score=0.42, reason="캐시된 대표 사유"
        ),
        related_paper=None,
    )
    service = SearchService(repo, RaisingLLM(), StaticEmbeddingClient(), _settings(tmp_path))

    result = service.resolve(
        1, "RAG 관련 논문", "exact", matched_keyword="RAG", primary_section_query=["실험"]
    )

    assert result.result_id != "r-cached-0001"
    assert result.primary_paper.paper_id == 10  # _repo()의 실제 계산 결과(캐시 무시)


def test_resolve_warms_cache_on_miss_for_default_view(tmp_path: Path) -> None:
    """기본 뷰인데 캐시가 없으면, 계산 후 다음 검색을 위해 캐시에 저장해야 한다(지연 워밍)."""
    repo = _repo()
    assert repo.get_cached_keyword_result(1) is None
    service = SearchService(repo, RaisingLLM(), StaticEmbeddingClient(), _settings(tmp_path))

    result = service.resolve(1, "RAG 관련 논문", "exact", matched_keyword="RAG")

    cached = repo.get_cached_keyword_result(1)
    assert cached is not None
    assert cached.result_id == result.result_id
    assert cached.primary_paper.paper_id == result.primary_paper.paper_id == 10


def test_precompute_keyword_cache_forces_refresh_when_ranking_changes(tmp_path: Path) -> None:
    """새 논문이 더 높은 점수로 연결되면, precompute_keyword_cache가 캐시를 강제로 갱신해야 한다."""
    repo = _repo()
    service = SearchService(repo, RaisingLLM(), StaticEmbeddingClient(), _settings(tmp_path))

    service.resolve(1, "RAG", "exact", matched_keyword="RAG")
    original_cached = repo.get_cached_keyword_result(1)
    assert original_cached is not None
    assert original_cached.primary_paper.paper_id == 10

    # 새 논문이 기존보다 훨씬 높은 키워드 점수·단락 유사도·제목 일치로 "RAG"에 연결된
    # 상황을 시뮬레이션한다(=적재 파이프라인이 STEP 9에서 마주치는 상황과 동일).
    current_year = datetime.now(UTC).year
    repo.add_paper(paper_id=99, title="New RAG Paper", published_year=current_year, abstract="")
    repo.add_paragraph(paper_id=99, paragraph_order=1, original_text="RAG", embedding=[1.0, 0.0])
    repo.link_paper_keyword(paper_id=99, keyword_id=1, score=0.99)

    stale_cached = repo.get_cached_keyword_result(1)
    assert stale_cached is not None
    assert stale_cached.primary_paper.paper_id == 10  # 아직 갱신 전

    service.precompute_keyword_cache(1)

    refreshed = repo.get_cached_keyword_result(1)
    assert refreshed is not None
    assert refreshed.primary_paper.paper_id == 99


def test_precompute_keyword_cache_ignores_unknown_keyword(tmp_path: Path) -> None:
    """존재하지 않는 keyword_id로 호출해도 예외 없이 조용히 무시해야 한다."""
    repo = _repo()
    service = SearchService(repo, RaisingLLM(), StaticEmbeddingClient(), _settings(tmp_path))

    service.precompute_keyword_cache(999999)

    assert repo.get_cached_keyword_result(999999) is None


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
