"""검색 핵심 로직: 질의 키워드 추출, 정확 매칭 우선 검색, 대표/연관 논문 선정.

## 2단계 인터랙션 전체 흐름 (DESIGN.md §5.1~5.2)

```
SearchService.search(query)
  1. LLM으로 질의에서 핵심 키워드 1~5개 추출 (extract_keywords)
  2. 정확 매칭 시도 (_best_exact_match): keywords/keyword_aliases 대조
     └─ 매칭 성공 -> resolve(..., match_type="exact") -> SearchMatched 반환 (여기서 종료)
     └─ 매칭 실패 -> 질의 키워드 임베딩으로 유사 키워드 Top-3 조회
                  -> SuggestionSessionStore에 (query, query_keywords, candidates) 저장
                  -> session_id와 candidates를 담은 SearchSuggest 반환

SearchService.select(session_id, keyword_id)   # 사용자가 후보 하나를 고른 뒤 호출
  1. 세션에서 원래 질의/후보 목록 복원 (없거나 만료됐으면 SearchSessionNotFound)
  2. resolve(..., match_type="selected") -> SearchMatched 반환
```

두 경로 모두 최종적으로 `resolve()`에서 대표/연관 논문을 선정하고 엑셀을 생성해
result_id로 캐시한다. 즉 "유사 키워드 제안"은 검색 자체가 아니라, 사용자가
어떤 저장 키워드로 검색을 확정할지 고르게 하는 중간 단계일 뿐이다.

## 정확 매칭 우선 로직 (_best_exact_match)

LLM이 추출한 질의 키워드마다 keywords.keyword 또는 keyword_aliases.alias와
정확히 일치하는 행을 찾는다. 질의에 여러 키워드가 있고 그중 여러 개가 DB에
존재하면(복수 매칭), 다음 가중치로 단 하나만 골라야 한다 — 사용자에게 대표
논문 1편만 보여주는 설계이므로 "가장 그 질의를 대표하는 키워드"를 결정해야
하기 때문이다:

    가중치 = frequency(그 키워드가 전체 코퍼스에서 얼마나 자주 등장했는가)
             × order_weight(질의 문자열에서 그 키워드가 얼마나 앞쪽에 등장했는가)

앞쪽에 나온 키워드일수록 사용자가 강조한 핵심어일 가능성이 높다고 보고
order_weight를 높게 준다(질의 텍스트에서 위치를 찾지 못하면 추출 순서로 대체).
frequency가 높을수록 검색 코퍼스에서 신뢰도 높은(자주 인용되는) 키워드로 보고
가중치를 높인다. 동점이면 인덱스가 앞선(먼저 추출된) 키워드, 그다음 frequency가
높은 키워드 순으로 결정론적으로 하나를 고른다.

## 질의 키워드 추출과 관련도 설명: 항상 LLM (2026-07-22 결정)

검색마다 LLM을 호출하면(Qwen2.5 7B, CPU) 요청 1건에 수십 초가 걸리고 Ollama가
요청을 직렬 처리하므로 동시 사용자가 늘면 대기열이 그대로 쌓인다(실측: 동시 요청
완전 직렬 처리, 각 18~20초 — 이 서버의 실제 하드웨어 사양·동시 사용자별 필요
사양은 `docs/reports/assessments/2026-07-22-llm-search-capacity.md` 참고).
그럼에도 "형태소 분석으로 자원을 아끼는 빠른 경로"를 사용자가 고를 수 있는
선택지로 남겨두지 않기로 했다 — 검색이 실제로 의미를 이해하고, 대표/연관
논문마다 관련도 설명(`_relevance_summary`, RAG 생성 단계)까지 생성해야
"검색 결과를 보여주는 것"을 넘어 "왜 이 논문인지 답하는" 시스템이 되기 때문이다.
따라서 `extract_keywords`는 매번 Ollama LLM을 호출하며, Kiwi 형태소 분석
(`_extract_noun_phrases`)과 정규식 폴백(`_fallback_keywords`)은 더 이상 사용자가
고르는 경로가 아니라 **LLM 호출이 실패했을 때만 쓰는 내부 안전망**으로 격하됐다.
반복 검색의 체감 지연은 `keyword_result_cache`(첫 검색만 LLM 비용을 치르고,
이후 같은 키워드는 캐시 재사용)로 완화한다.

## 섹션 필터: 산출물 커스터마이징

`section_query`를 지정하면 대표/연관 논문의 단락 중 `paragraphs.section_name`이
그 문자열을 포함하는 것만 결과(및 엑셀)에 담는다(`repo.paragraphs_of`의
section_query 인자, 대소문자 무시 부분 일치). 논문 전체를 통짜로 돌려주는 대신
"실험 방법만", "결과만" 같은 사용자 의도를 반영하기 위한 것이며, 어떤 논문이
대표/연관으로 선정되는지에는 영향을 주지 않는다(선정 로직은 항상 전체 단락
기준으로 계산한다) — 이미 확정된 결과의 "무엇을 보여줄지"만 좁히는 필터다.

section_query는 자유 텍스트가 아니라 `SearchMatched.available_sections`(대표+연관
논문에 실제 존재하는 section_name을 문서 등장 순서로 합친 목록, `_available_sections`가
`repo.available_sections`로 조회)에서 고른 값을 그대로 넣는 것을 UI 쪽 사용 방식으로
가정한다 — 사용자가 논문 구조를 미리 몰라도 실제 섹션 제목 중에서 고를 수 있게 하기
위함이다. `include_abstract=False`는 이와 별개로 논문 정보 시트의 초록 원문/요약 칸만
비운다(초록은 paragraphs.section_name 체계 밖의 papers 메타데이터라 섹션 필터로는
빠지지 않는다).

## 매칭 실패 시 유사 키워드 제안

정확 매칭이 하나도 없으면 질의 키워드(또는 원문 질의)를 임베딩해 keywords.embedding과
코사인 유사도를 계산, 상위 search_suggestion_limit(기본 3)개를 후보로 제시한다.
search_similarity_threshold(기본 0.5) 미만인 키워드는 아예 관련이 없다고 보고
후보에서 제외한다 — 임계값을 너무 낮추면 사용자에게 무의미한 후보를 보여주게 되고,
너무 높이면 유효한 후보까지 놓치므로 0.5를 하한으로 잡았다.

## 대표 논문 점수식 vs 연관 논문

**대표 논문**은 확정된 매칭 키워드(keyword_id)에 연결된 논문들 중 아래 가중합
점수가 가장 높은 1편을 실시간으로 계산해서 고른다(_select_primary):

    총점 = 0.5 * paper_keywords.score       (수집 시 산출된 키워드-논문 연관 점수)
         + 0.3 * 단락 최고 유사도            (질의 임베딩 ↔ 논문 단락 임베딩 코사인 유사도 최대값)
         + 0.1 * 제목/초록 등장 여부(0 or 1)  (매칭 키워드가 제목·초록에 직접 등장하는지)
         + 0.1 * 연도 가중치                 (최근 논문일수록 1에 가깝게, 10년 이상 지나면 0)

키워드-논문 연관성(정적 점수)과 실제 질의 의미(단락 임베딩 유사도)를 함께 반영해
"그 키워드로 검색됐을 때 가장 적합한 논문"을 고르는 것이 목적이다.

**연관 논문**은 이와 달리 실시간 계산을 하지 않는다. 수집 파이프라인 STEP 8에서
이미 계산·저장해 둔 `paper_relations` 테이블에서 대표 논문과 연결된 행 중
relation_score가 가장 높은 1편을 그대로 조회할 뿐이다. 검색 시점에 논문 임베딩
유사도나 키워드 자카드를 다시 계산하면 CPU 환경에서 응답이 느려지므로, "미리
계산해 둔 값을 조회만 한다"는 설계로 검색 응답 시간을 짧게 유지한다.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from paperrag.config import Settings, get_settings
from paperrag.ingest.embeddings import EmbeddingClient
from paperrag.ingest.keywords import _kiwi, normalize
from paperrag.ingest.llm_enrich import (
    KOREAN_OUTPUT_RULE,
    LLMClient,
    _coerce_json_dict,
    _validate_korean_output,
)
from paperrag.search.excel import build_excel
from paperrag.search.repository import (
    KeywordRow,
    PaperKeywordRow,
    PaperMetaRow,
    ParagraphRow,
    SearchRepository,
)
from paperrag.search.schemas import (
    PaperInfo,
    PaperSummary,
    ParagraphInfo,
    ResultBundle,
    SectionInfo,
    SearchMatched,
    SearchSuggest,
    TableInfo,
)
from paperrag.search.sessions import SuggestionSessionStore, new_result_id

# LLM(Ollama)에게 질의 키워드 추출을 JSON으로 강제하기 위한 스키마 힌트와 프롬프트.
#
# 2026-07-22 실측 발견: "언어를 바꾸지 마라"는 지시 없이 한국어로만 쓰인 프롬프트를 주면,
# 영어 질의("Structured Document Understanding")조차 LLM이 깨진 한글로 오역해
# ("구조화되ᄂ 문서 이해" — 자모가 음절로 안 붙는 오류) 저장된 정확 키워드와 매칭이
# 실패하고 유사 후보로 빠지는 문제가 재현됐다(2회 확인). 원문 언어를 그대로 유지하라는
# 지시와 few-shot 예시를 추가해 이 오역을 막는다.
QUERY_KEYWORDS_SCHEMA_HINT = '{"keywords":["string","string","string"]}'
QUERY_KEYWORDS_PROMPT = """
너는 한국어/영어 논문 검색 질의에서 핵심 검색 키워드를 추출하는 연구 보조자다.
사용자의 자연어 질의에서 논문 키워드로 대조할 핵심 명사구 1~5개만 JSON으로 반환하라.
**절대 번역하지 마라** — 질의에 등장한 단어의 언어(한국어/영어)를 그대로 유지한 채
키워드를 뽑아라. 영어 질의면 영어 키워드를, 한국어 질의면 한국어 키워드를 반환한다.
반드시 유효한 JSON만 반환하고, 설명 문장은 쓰지 마라.

반환 형식: {{"keywords":["키워드1","키워드2"]}}

예시 입력: Structured Document Understanding
예시 출력: {{"keywords":["Structured Document Understanding"]}}

예시 입력: 예지보전 시스템
예시 출력: {{"keywords":["예지보전 시스템"]}}

질의:
{query}
""".strip()

# 검색 결과 관련도 설명(RAG 생성 단계)용 스키마 힌트와 프롬프트. 논문 전체가 아니라
# 이 논문에서 질의와 가장 유사한 단락 1개만 근거로 주어, 그 안에 없는 사실은 지어내지
# 말고 짧게(1~2문장) 답하도록 강제한다.
RELEVANCE_SCHEMA_HINT = '{"summary":"string"}'
RELEVANCE_PROMPT = """
너는 논문 검색 결과에서 "왜 이 논문이 검색과 관련 있는지"를 설명하는 연구 보조자다.
아래는 사용자 질의와, 그 질의와 가장 유사하다고 판단된 논문 "{title}"의 단락 1개다.
이 단락 내용만 근거로 삼아 이 논문이 질의와 왜 관련 있는지 1~2문장으로 설명하라.
단락에 없는 사실을 지어내지 말고, 모르면 모른다고 하라. 설명 문장 외 다른 텍스트는
쓰지 말고 반드시 유효한 JSON만 반환하라.

반환 형식: {{"summary":"설명 문장"}}

질의: {query}
매칭 키워드: {matched_keyword}
논문 제목: {title}
근거 단락(섹션: {section_name}):
{paragraph_text}
""".strip()


class SearchSessionNotFound(Exception):
    """suggest 세션이 없거나(session_id 오타 등) 이미 만료된 경우. API 계층에서 404로 변환된다."""


class SearchNoPaperFound(Exception):
    """확정된 keyword_id에 연결된 논문이 하나도 없는 경우. API 계층에서 404로 변환된다."""


class SearchDependencyError(RuntimeError):
    """검색에 필요한 LLM 또는 임베딩 결과를 만들지 못함.

    allow_degraded_results 설정이 꺼져 있을 때, LLM 키워드 추출 실패나 임베딩
    차원 불일치처럼 신뢰할 수 없는 결과를 규칙 기반 값으로 몰래 대체하지 않고
    명시적으로 실패시키기 위한 예외다. API 계층에서 503으로 변환된다.
    """


class SearchService:
    """검색 요청 한 건의 생애주기(질의 -> 매칭/제안 -> 확정 -> 엑셀 생성)를 담당하는 서비스.

    repo(SearchRepository)로 DB를 조회하고, llm으로 질의 키워드를 추출하고,
    embedder로 벡터 유사도 계산에 쓸 임베딩을 얻는다. sessions는 suggest 단계의
    상태를 보관하는 인메모리 저장소로, 기본값을 넘기지 않으면 서비스 인스턴스마다
    새로 만들어진다(테스트에서는 주입해 세션을 직접 조작할 수 있다).
    """

    def __init__(
        self,
        repo: SearchRepository,
        llm: LLMClient,
        embedder: EmbeddingClient,
        settings: Settings | None = None,
        sessions: SuggestionSessionStore | None = None,
    ) -> None:
        self.repo = repo
        self.llm = llm
        self.embedder = embedder
        self.settings = settings or get_settings()
        self.sessions = sessions or SuggestionSessionStore()

    def extract_keywords(self, query: str) -> list[str]:
        """질의에서 핵심 키워드 1~5개를 LLM으로 추출해 정규화된 유니크 목록으로 반환한다.

        2026-07-22 결정: Kiwi 형태소 분석 빠른 경로를 기본/선택 경로로 두지 않는다
        — 모든 검색이 Ollama LLM 자연어 이해로 키워드를 뽑는다(그래야 대표/연관
        논문에 대한 관련도 설명(RAG 생성 단계, `_relevance_summary`)까지 일관되게
        의미를 이해한 검색이 된다). LLM 호출이 실패하거나 JSON 파싱에 실패하면:
        allow_degraded_results가 꺼져 있으면 SearchDependencyError를 던져 검색을
        중단시키고(신뢰할 수 없는 결과를 조용히 대체하지 않기 위함), 켜져 있으면
        Kiwi 형태소 분석(_extract_noun_phrases) 또는 정규식 최후 폴백
        (_fallback_keywords)으로 대체한다 — 이 둘은 더 이상 "빠른 경로 선택지"가
        아니라 LLM 장애 시에만 쓰는 내부 안전망이다.
        """
        prompt = QUERY_KEYWORDS_PROMPT.format(query=query)
        try:
            data = self.llm.generate_json(
                prompt, QUERY_KEYWORDS_SCHEMA_HINT, operation="query_keywords"
            )
            keywords = _clean_keywords(data.get("keywords", []))
        except Exception as exc:
            if not self.settings.allow_degraded_results:
                raise SearchDependencyError(
                    "질의 키워드 LLM 응답을 검증하지 못했습니다. 규칙 기반 결과로 대체하지 않습니다."
                ) from exc
            keywords = []
        if not keywords:
            fast_keywords = _normalize_unique(_extract_noun_phrases(query))
            keywords = fast_keywords or _fallback_keywords(query)
        return _normalize_unique(keywords)

    def search(
        self,
        query: str,
        *,
        section_query: str | None = None,
        include_related: bool = True,
        include_tables: bool = True,
        include_abstract: bool = True,
    ) -> SearchMatched | SearchSuggest:
        """2단계 인터랙션의 1단계 진입점: 정확 매칭을 우선 시도하고, 실패하면 유사 키워드를 제안한다.

        1) LLM으로 질의 키워드를 추출하고(extract_keywords) keywords/keyword_aliases와
           정확 매칭을 시도한다(_best_exact_match). 매칭되면 바로 resolve()로
           대표/연관 논문을 확정한다.
        2) 정확 매칭이 없으면 질의 키워드(없으면 원문 질의)를 임베딩해 유사 키워드
           Top-N을 조회하고, 세션에 저장한 뒤 SearchSuggest로 후보를 돌려준다.
           사용자는 이 후보 중 하나를 골라 select()를 다시 호출해야 한다.
        section_query가 주어지면 최종 결과(및 엑셀)의 단락을 그 섹션명을 포함하는
        것만으로 좁힌다. include_related=False면 연관 논문 조회 자체를 건너뛰고
        응답·엑셀에서 연관 논문 관련 항목을 전부 제외한다. include_tables=False면
        표 조회를 건너뛰고 엑셀의 표 시트를 만들지 않는다. include_abstract=False면
        논문 정보 시트의 초록 칸을 비운다. 셋 다 세션에 저장돼 select() 이후에도
        그대로 유지된다.
        """
        normalized_query = normalize(query)
        keywords = self.extract_keywords(query)
        exact_match = self._best_exact_match(normalized_query, keywords)
        if exact_match is not None:
            return self.resolve(
                exact_match.keyword_id,
                query,
                "exact",
                matched_keyword=exact_match.display_form,
                query_keywords=keywords,
                section_query=section_query,
                include_related=include_related,
                include_tables=include_tables,
                include_abstract=include_abstract,
            )

        # 정확 매칭 실패: 키워드가 하나도 추출되지 않았으면 원문 질의 전체를 임베딩해
        # 최소한의 의미 기반 후보라도 찾도록 한다.
        vector_text = " ".join(keywords) if keywords else query
        vector = self._embed_one(vector_text)
        candidates = self.repo.similar_keywords(
            vector,
            top_k=self.settings.search_suggestion_limit,
            min_sim=self.settings.search_similarity_threshold,
        )
        session = self.sessions.create(
            query,
            candidates,
            keywords,
            section_query=section_query,
            include_related=include_related,
            include_tables=include_tables,
            include_abstract=include_abstract,
        )
        return SearchSuggest(
            session_id=session.session_id,
            query_keywords=keywords,
            explanation=(
                f"질의에서 {', '.join(keywords) or query} 키워드를 추출했지만 정확히 "
                f"일치하는 저장 키워드가 없어 의미적으로 가까운 {len(candidates)}개 후보를 제시합니다."
            ),
            candidates=candidates,
        )

    def select(self, session_id: str, keyword_id: int) -> SearchMatched:
        """2단계 인터랙션의 2단계: suggest 후보 중 사용자가 고른 keyword_id로 검색을 확정한다.

        세션이 없거나 만료됐거나(sessions.get가 None), 고른 keyword_id가 그 세션의
        후보 목록에 없으면(오래된 세션에 다른 후보 id를 보낸 경우 등) 모두
        SearchSessionNotFound로 취급해 404로 응답하게 한다. 세션에 저장된
        section_query/include_related/include_tables/include_abstract(있었다면
        search() 호출 시점의 옵션)도 그대로 이어받는다.
        """
        session = self.sessions.get(session_id)
        if session is None:
            raise SearchSessionNotFound(session_id)
        selected = next(
            (candidate for candidate in session.candidates if candidate.keyword_id == keyword_id),
            None,
        )
        if selected is None:
            raise SearchSessionNotFound(session_id)
        return self.resolve(
            keyword_id,
            session.query,
            "selected",
            matched_keyword=selected.keyword,
            query_keywords=session.query_keywords,
            section_query=session.section_query,
            include_related=session.include_related,
            include_tables=session.include_tables,
            include_abstract=session.include_abstract,
        )

    def resolve(
        self,
        keyword_id: int,
        query: str,
        match_type: Literal["exact", "selected"],
        matched_keyword: str | None = None,
        query_keywords: list[str] | None = None,
        section_query: str | None = None,
        include_related: bool = True,
        include_tables: bool = True,
        include_abstract: bool = True,
        force_refresh: bool = False,
    ) -> SearchMatched:
        """확정된 keyword_id로 대표/연관 논문을 선정하고 엑셀을 생성해 최종 응답을 만든다.

        **캐시 우선 경로**: section_query가 없고 include_related/include_tables가
        모두 True인 "기본 뷰" 요청이면, 먼저 keyword_result_cache에서 이 keyword_id의
        사전 계산된 결과를 찾는다. 있으면 임베딩·점수 계산·엑셀 생성을 전부 건너뛰고
        캐시된 PaperSummary와 엑셀 경로를 그대로 재사용한다(설명 문장만 이번 질의
        기준으로 새로 조립). 캐시가 없거나 커스텀 옵션이 있으면 아래 순서로 새로
        계산한다: (1) 매칭 키워드 벡터 임베딩 (2) 대표 논문 선정(_select_primary)
        (3) include_related=True면 paper_relations에서 연관 논문 조회(top_relation,
        실시간 계산 없음) — False면 이 조회 자체를 건너뛰어 related_paper가 항상
        None인 응답을 만든다 (4) ResultBundle 조립(section_query로 단락 범위 축소,
        include_tables=False면 표 조회도 건너뜀) (5) 엑셀 생성 및 result_id로 DB
        캐시 저장. 기본 뷰인데 캐시가 없었던 경우, 이번에 계산한 결과를 다음
        검색을 위해 keyword_result_cache에도 저장한다(지연 워밍 — 수집 파이프라인의
        사전 계산을 놓친 키워드도 첫 실제 검색에서 캐시가 채워지게 하기 위함).
        section_query/include_related/include_tables 모두 대표 논문 선정 자체에는
        영향을 주지 않는다 — 이미 확정된 결과에서 "무엇을 보여줄지"만 좁힌다.
        force_refresh=True면 기본 뷰라도 캐시 조회를 건너뛰고 강제로 새로 계산한다
        (precompute_keyword_cache가 새 논문 적재 직후 캐시를 무조건 갱신할 때 사용).
        """
        keyword = self.repo.keyword_by_id(keyword_id)
        keyword_label = matched_keyword or (keyword.display_form if keyword else str(keyword_id))
        extracted_keywords = list(query_keywords or [keyword_label])

        is_default_view = (
            section_query is None and include_related and include_tables and include_abstract
        )
        if is_default_view and not force_refresh:
            cached = self.repo.get_cached_keyword_result(keyword_id)
            if cached is not None:
                return SearchMatched(
                    matched_keyword=keyword_label,
                    query_keywords=extracted_keywords,
                    match_type=match_type,
                    explanation=_search_explanation(
                        extracted_keywords,
                        keyword_label,
                        match_type,
                        cached.primary_paper,
                        cached.related_paper,
                    ),
                    result_id=cached.result_id,
                    primary_paper=cached.primary_paper,
                    related_paper=cached.related_paper,
                    available_sections=self._available_sections(
                        cached.primary_paper.paper_id,
                        cached.related_paper.paper_id if cached.related_paper else None,
                    ),
                )

        keyword_text = keyword.keyword if keyword is not None else normalize(keyword_label)
        vector = self._embed_one(keyword_text or query)
        primary_row, primary_score, primary_reason = self._select_primary(
            keyword_id,
            keyword_text,
            vector,
        )
        primary_meta = self._required_paper_meta(primary_row.paper_id)
        primary_summary = self._paper_summary(primary_meta, primary_score, primary_reason)
        primary_summary = primary_summary.model_copy(
            update={
                "relevance_summary": self._relevance_summary(
                    keyword_id, primary_summary, query, keyword_label, vector
                )
            }
        )

        related_summary: PaperSummary | None = None
        related_meta: PaperMetaRow | None = None
        # 연관 논문은 대표 논문처럼 실시간 계산하지 않고, 수집 STEP 8에서 미리
        # 채워둔 paper_relations 중 relation_score 최고 1건을 그대로 조회한다.
        # include_related=False면 이 조회 자체를 생략해 불필요한 DB 왕복을 없앤다.
        relation = self.repo.top_relation(primary_meta.paper_id) if include_related else None
        if relation is not None:
            related_id, relation_score, relation_reason = relation
            related_meta = self.repo.paper_meta(related_id)
            if related_meta is not None:
                related_summary = self._paper_summary(
                    related_meta,
                    relation_score,
                    relation_reason,
                )
                related_summary = related_summary.model_copy(
                    update={
                        "relevance_summary": self._relevance_summary(
                            keyword_id, related_summary, query, keyword_label, vector
                        )
                    }
                )

        result_id = new_result_id()
        bundle = self._bundle(
            result_id=result_id,
            query=query,
            matched_keyword=keyword_label,
            match_type=match_type,
            query_keywords=extracted_keywords,
            primary=primary_summary,
            primary_meta=primary_meta,
            related=related_summary,
            related_meta=related_meta,
            section_query=section_query,
            include_related=include_related,
            include_tables=include_tables,
            include_abstract=include_abstract,
        )
        out_path = Path(self.settings.result_dir) / f"{result_id}.xlsx"
        excel_path = build_excel(bundle, out_path)
        self.repo.save_result(
            result_id,
            query=query,
            match_type=match_type,
            matched_keyword_id=keyword_id,
            primary_paper_id=primary_meta.paper_id,
            related_paper_id=related_meta.paper_id if related_meta is not None else None,
            excel_path=excel_path,
        )
        if is_default_view:
            self.repo.save_cached_keyword_result(
                keyword_id,
                result_id=result_id,
                excel_path=excel_path,
                primary_paper=primary_summary,
                related_paper=related_summary,
            )
        return SearchMatched(
            matched_keyword=keyword_label,
            query_keywords=extracted_keywords,
            match_type=match_type,
            explanation=bundle.explanation,
            result_id=result_id,
            primary_paper=primary_summary,
            related_paper=related_summary,
            available_sections=self._available_sections(
                primary_meta.paper_id,
                related_meta.paper_id if related_meta is not None else None,
            ),
        )

    def _available_sections(self, primary_paper_id: int, related_paper_id: int | None) -> list[str]:
        """대표(+연관) 논문의 실제 section_name을 문서 등장 순서로 중복 없이 합친다.

        대표 논문 섹션을 먼저 나열하고, 연관 논문에만 있는 섹션은 뒤에 이어붙인다
        (사용자가 다음 검색의 section_query 후보로 그대로 쓸 목록이라 순서가
        읽기 흐름과 맞아야 한다).
        """
        ordered: dict[str, None] = {}
        for name in self.repo.available_sections(primary_paper_id):
            ordered.setdefault(name, None)
        if related_paper_id is not None:
            for name in self.repo.available_sections(related_paper_id):
                ordered.setdefault(name, None)
        return list(ordered)

    def _relevance_summary(
        self,
        keyword_id: int,
        paper: PaperSummary,
        query: str,
        matched_keyword: str,
        vector: list[float],
    ) -> str | None:
        """이 논문이 왜 질의와 관련 있는지 LLM이 생성한 짧은 설명을 반환한다(RAG 생성 단계).

        대표 논문 선정에 실제로 쓴 것과 같은 질의 임베딩으로 이 논문에서 가장
        유사한 단락 1개를 찾아(`repo.top_matching_paragraph`) 그 내용만 근거로
        삼아 짧게 설명하게 한다 — 단락에 없는 사실을 지어내지 않도록 프롬프트에
        그 단락 텍스트만 넣는다. 같은 keyword_id로 이미 생성해
        `keyword_result_cache`에 남아 있고 논문이 그때와 같으면 재사용해 LLM을
        다시 부르지 않는다 — "결과물 구성"에서 표/연관 논문 포함 여부만 바꿔
        재검색할 때마다 매번 다시 생성하는 낭비를 막기 위함이다. 생성이 실패하면
        (LLM 오류·형식 오류) 검색 전체를 막지 않고 단락 원문 앞부분으로 대체한다
        — 이 설명은 부가 정보이지 대표/연관 논문 선정 자체를 좌우하지 않는다.
        """
        cached = self.repo.get_cached_keyword_result(keyword_id)
        cached_candidates: tuple[PaperSummary | None, ...] = ()
        if cached is not None:
            cached_candidates = (cached.primary_paper, cached.related_paper)
        for candidate in cached_candidates:
            if (
                candidate is not None
                and candidate.paper_id == paper.paper_id
                and candidate.relevance_summary
            ):
                return candidate.relevance_summary

        paragraph = self.repo.top_matching_paragraph(paper.paper_id, vector)
        if paragraph is None:
            return None
        paragraph_text = (paragraph.cleaned_text or paragraph.original_text).strip()
        if not paragraph_text:
            return None

        prompt = RELEVANCE_PROMPT.format(
            query=query,
            matched_keyword=matched_keyword,
            title=paper.title,
            section_name=paragraph.section_name or "본문",
            paragraph_text=paragraph_text[:800],
        )
        prompt += "\n" + KOREAN_OUTPUT_RULE
        try:
            data = _coerce_json_dict(
                self.llm.generate_json(
                    prompt, RELEVANCE_SCHEMA_HINT, operation="relevance_explanation"
                )
            )
            summary = str(data.get("summary", "")).strip()
            _validate_korean_output(self.llm, summary)
            if summary:
                return summary
        except Exception:
            pass
        return paragraph_text[:200]

    def precompute_keyword_cache(self, keyword_id: int) -> None:
        """이 키워드의 기본 뷰 결과를 강제로 새로 계산해 keyword_result_cache에 저장한다.

        수집 파이프라인(STEP 9)이 논문을 적재한 직후, 그 논문이 이번에 새로
        연결된 키워드마다 호출한다. 새 논문이 그 키워드의 대표/연관 논문 순위를
        바꿨을 수 있으므로 캐시가 이미 있어도 항상 다시 계산한다(resolve의
        force_refresh=True). 존재하지 않는 keyword_id는 조용히 무시한다(경쟁
        상태로 키워드가 삭제된 경우 등 — 캐시 사전 계산은 최선 노력이지 필수
        경로가 아니다).
        """
        keyword = self.repo.keyword_by_id(keyword_id)
        if keyword is None:
            return
        self.resolve(
            keyword_id,
            query=keyword.display_form,
            match_type="exact",
            matched_keyword=keyword.display_form,
            query_keywords=[keyword.display_form],
            force_refresh=True,
        )

    def result_excel_path(self, result_id: str) -> str | None:
        """result_id로 캐시된 엑셀 파일 경로를 반환한다. DB 레코드나 실제 파일이 없으면 None."""
        path = self.repo.load_result(result_id)
        if path is None:
            return None
        return path if Path(path).exists() else None

    def _best_exact_match(
        self,
        normalized_query: str,
        keywords: list[str],
    ) -> KeywordRow | None:
        """추출된 질의 키워드들 중 DB에 정확히 존재하는(정규화형 또는 별칭 일치) 키워드 하나를 고른다.

        복수 매칭 시 `frequency(코퍼스 내 등장 빈도) × order_weight(질의 내 등장
        위치 가중치)`가 가장 큰 키워드를 채택한다 — 다른 논문에도 폭넓게 쓰이면서
        질의 앞부분에 등장한 키워드일수록 "그 질의를 대표하는 핵심 키워드"일
        가능성이 높다고 보기 때문이다. order_weight는 정규화된 질의 문자열에서
        키워드가 발견된 위치가 앞쪽일수록 1에 가깝고(뒤쪽이면 0에 가까움, 최소
        0.01), 질의 문자열에서 못 찾으면(정규화 차이 등) LLM이 추출한 순서를
        대신 사용한다(첫 번째 키워드가 가장 큰 가중치). 동점이면 먼저 추출된
        키워드, 그다음 frequency가 큰 키워드 순으로 결정론적으로 하나만 남긴다.
        """
        scored: list[tuple[float, int, KeywordRow]] = []
        query_len = max(len(normalized_query), 1)
        for index, keyword in enumerate(keywords):
            row = self.repo.find_keyword_exact(keyword)
            if row is None:
                continue
            position = normalized_query.find(keyword)
            if position >= 0:
                order_weight = max(0.01, 1.0 - (position / query_len))
            else:
                order_weight = 1.0 / (index + 1)
            scored.append((row.frequency * order_weight, index, row))
        if not scored:
            return None
        return max(scored, key=lambda item: (item[0], -item[1], item[2].frequency))[2]

    def _select_primary(
        self,
        keyword_id: int,
        keyword_text: str,
        vector: list[float],
    ) -> tuple[PaperKeywordRow, float, str]:
        """확정된 keyword_id에 연결된 논문들 중 대표 논문 점수식이 가장 높은 1편을 고른다.

        점수식: 0.5*paper_keywords.score(키워드-논문 정적 연관도)
              + 0.3*단락 최고 유사도(질의 임베딩 vs 논문 단락 임베딩 코사인 최댓값)
              + 0.1*제목/초록 등장 여부
              + 0.1*연도 가중치(최근일수록 1에 가까움).
        reason 문자열에 각 항목의 계산 값을 그대로 기록해 엑셀 "선정 사유" 열과
        API 응답에 그대로 노출한다(재현 가능한 근거 제공이 목적). 동점 처리 시
        kw_score가 높은 논문, 그다음 paper_id가 작은(먼저 등록된) 논문을 우선한다.
        """
        rows = self.repo.papers_for_keyword(keyword_id)
        if not rows:
            raise SearchNoPaperFound(f"No papers for keyword_id={keyword_id}")

        scored: list[tuple[float, PaperKeywordRow, str]] = []
        for row in rows:
            meta = self._required_paper_meta(row.paper_id)
            paragraph_similarity = self.repo.best_paragraph_similarity(row.paper_id, vector)
            title_abstract_hit = 1.0 if self.repo.title_abstract_contains(
                row.paper_id,
                keyword_text,
            ) else 0.0
            year_score = _year_weight(meta.published_year)
            total = (
                0.5 * row.kw_score
                + 0.3 * paragraph_similarity
                + 0.1 * title_abstract_hit
                + 0.1 * year_score
            )
            reason = (
                f"대표 점수={total:.3f} "
                f"(키워드 {row.kw_score:.3f}*0.5={0.5 * row.kw_score:.3f}, "
                f"단락 {paragraph_similarity:.3f}*0.3={0.3 * paragraph_similarity:.3f}, "
                f"제목/초록 {title_abstract_hit:.3f}*0.1={0.1 * title_abstract_hit:.3f}, "
                f"연도 {year_score:.3f}*0.1={0.1 * year_score:.3f})"
            )
            scored.append((total, row, reason))
        total, row, reason = max(
            scored,
            key=lambda item: (item[0], item[1].kw_score, -item[1].paper_id),
        )
        return row, total, reason

    def _paper_summary(self, meta: PaperMetaRow, score: float, reason: str) -> PaperSummary:
        """PaperMetaRow + 선정 점수/사유를 API 응답용 PaperSummary로 조립한다."""
        return PaperSummary(
            paper_id=meta.paper_id,
            title=meta.title,
            authors=meta.authors,
            published_year=meta.published_year,
            journal=meta.journal,
            full_text_link=meta.full_text_link,
            keywords=self.repo.paper_keywords(meta.paper_id),
            score=score,
            reason=reason,
        )

    def _bundle(
        self,
        *,
        result_id: str,
        query: str,
        matched_keyword: str,
        match_type: Literal["exact", "selected"],
        query_keywords: list[str],
        primary: PaperSummary,
        primary_meta: PaperMetaRow,
        related: PaperSummary | None,
        related_meta: PaperMetaRow | None,
        section_query: str | None = None,
        include_related: bool = True,
        include_tables: bool = True,
        include_abstract: bool = True,
    ) -> ResultBundle:
        """대표/연관 논문의 상세 정보·단락·섹션·표를 모두 모아 엑셀 생성용 ResultBundle을 만든다.

        section_query가 주어지면 단락 목록을 section_name 부분 일치(대소문자 무시)로
        좁혀서 가져온다(repo.paragraphs_of). 논문 키워드 목록은 필터 대상이 아니다 —
        섹션 필터는 어디까지나 "이번 산출물에 어떤 단락 텍스트를 포함할지"를 사용자가
        고르는 기능이라서다. include_tables=False면 표 조회(repo.tables_of) 자체를
        건너뛰어 불필요한 DB 왕복을 없애고, 결과 ResultBundle.include_tables가
        엑셀에서 표 시트를 아예 만들지 말라는 신호가 된다(related_meta가 None이면
        연관 논문 관련 시트도 자연히 비게 되는 것과 같은 방식). include_abstract=False면
        _paper_info가 초록 원문·요약 필드를 빈 값으로 채운다(시트 자체는 유지).
        """
        primary_info = _paper_info(
            primary_meta,
            self.repo.paper_keywords(primary_meta.paper_id),
            include_abstract=include_abstract,
        )
        related_info = (
            _paper_info(
                related_meta,
                self.repo.paper_keywords(related_meta.paper_id),
                include_abstract=include_abstract,
            )
            if related_meta is not None
            else None
        )
        tables: list[TableInfo] = []
        if include_tables:
            tables.extend(
                TableInfo(
                    role="대표",
                    table_title=row.table_title,
                    table_text=row.table_text,
                    table_summary=row.table_summary,
                )
                for row in self.repo.tables_of(primary_meta.paper_id)
            )
            if related_meta is not None:
                tables.extend(
                    TableInfo(
                        role="연관",
                        table_title=row.table_title,
                        table_text=row.table_text,
                        table_summary=row.table_summary,
                    )
                    for row in self.repo.tables_of(related_meta.paper_id)
                )
        primary_paragraphs = _paragraph_infos(
            self.repo.paragraphs_of(primary_meta.paper_id, section_query=section_query)
        )
        related_paragraphs = (
            _paragraph_infos(
                self.repo.paragraphs_of(related_meta.paper_id, section_query=section_query)
            )
            if related_meta is not None
            else []
        )
        explanation = _search_explanation(
            query_keywords,
            matched_keyword,
            match_type,
            primary,
            related,
        )
        return ResultBundle(
            result_id=result_id,
            query=query,
            query_keywords=query_keywords,
            matched_keyword=matched_keyword,
            match_type=match_type,
            explanation=explanation,
            primary_paper=primary,
            related_paper=related,
            primary_info=primary_info,
            related_info=related_info,
            primary_paragraphs=primary_paragraphs,
            related_paragraphs=related_paragraphs,
            primary_sections=_section_infos(primary_paragraphs),
            related_sections=_section_infos(related_paragraphs),
            tables=tables,
            include_related=include_related,
            include_tables=include_tables,
            include_abstract=include_abstract,
            created_at=datetime.now(UTC),
        )

    def _required_paper_meta(self, paper_id: int) -> PaperMetaRow:
        """paper_meta 조회 결과가 None이면(정합성 깨짐 등) SearchNoPaperFound로 승격시키는 헬퍼."""
        meta = self.repo.paper_meta(paper_id)
        if meta is None:
            raise SearchNoPaperFound(f"paper_id={paper_id} was not found")
        return meta

    def _embed_one(self, text: str) -> list[float]:
        """문자열 1개를 임베딩해 벡터 1개를 반환한다.

        결과가 비어 있거나 설정된 embed_dim(BGE-M3 기준 1024)과 차원이 다르면
        임베딩 서버가 오작동한 것으로 보고 SearchDependencyError를 던져 잘못된
        벡터로 유사도를 계산하지 않게 막는다.
        """
        vectors = self.embedder.embed([text])
        if not vectors or len(vectors[0]) != self.settings.embed_dim:
            raise SearchDependencyError(
                f"임베딩 결과는 {self.settings.embed_dim}차원 벡터 1개여야 합니다."
            )
        return vectors[0]


def _paper_info(
    meta: PaperMetaRow, keywords: list[str], *, include_abstract: bool = True
) -> PaperInfo:
    """PaperMetaRow + 논문 키워드 목록을 엑셀/응답용 PaperInfo로 변환한다.

    include_abstract=False면 초록 원문·요약을 빈 값으로 채운다 — 사용자가
    "초록 제외"를 선택했을 때 엑셀 해당 칸을 비우기 위한 것이다.
    """
    return PaperInfo(
        paper_id=meta.paper_id,
        title=meta.title,
        authors=meta.authors,
        published_year=meta.published_year,
        journal=meta.journal,
        abstract=meta.abstract if include_abstract else "",
        abstract_summary=meta.abstract_summary if include_abstract else None,
        full_text_link=meta.full_text_link,
        keywords=keywords,
    )


def _paragraph_infos(rows: list[ParagraphRow]) -> list[ParagraphInfo]:
    """repository의 ParagraphRow 목록을 엑셀/응답용 ParagraphInfo 목록으로 변환한다."""
    return [
        ParagraphInfo(
            paragraph_order=row.paragraph_order,
            section_name=row.section_name,
            original_text=row.original_text,
            cleaned_text=row.cleaned_text,
            summary=row.summary,
            keywords=list(row.keywords or []),
        )
        for row in rows
    ]


def _section_infos(paragraphs: list[ParagraphInfo]) -> list[SectionInfo]:
    """단락 목록(이미 paragraph_order로 정렬돼 있음)을 연속된 section_name 기준으로 묶어 섹션 목록을 만든다.

    같은 section_name이라도 문서 뒷부분에서 다시 등장하면(예: 다른 섹션을 거쳐
    재등장) 새 그룹으로 취급한다 — 그룹 판단 기준이 "직전 그룹과 이름이 같은가"
    이기 때문에, 논문 원문 상의 등장 순서를 그대로 보존하기 위함이다.
    """
    groups: list[list[ParagraphInfo]] = []
    for paragraph in paragraphs:
        name = paragraph.section_name.strip() or "본문"
        previous_name = (
            groups[-1][0].section_name.strip() or "본문" if groups else None
        )
        if name != previous_name:
            groups.append([paragraph])
        else:
            groups[-1].append(paragraph)

    sections: list[SectionInfo] = []
    for section_order, rows in enumerate(groups, start=1):
        # 섹션 내 단락들의 키워드를 중복 없이(등장 순서 유지) 합친다.
        keywords: list[str] = []
        for row in rows:
            for keyword in row.keywords:
                if keyword not in keywords:
                    keywords.append(keyword)
        sections.append(
            SectionInfo(
                section_order=section_order,
                section_name=rows[0].section_name.strip() or "본문",
                paragraph_count=len(rows),
                original_text="\n\n".join(row.original_text for row in rows if row.original_text),
                cleaned_text="\n\n".join(row.cleaned_text for row in rows if row.cleaned_text),
                summary=" ".join(row.summary for row in rows if row.summary),
                keywords=keywords,
            )
        )
    return sections


def _search_explanation(
    query_keywords: list[str],
    matched_keyword: str,
    match_type: Literal["exact", "selected"],
    primary: PaperSummary,
    related: PaperSummary | None,
) -> str:
    """사용자에게 보여줄 한 줄짜리 검색 설명문을 만든다(SearchMatched.explanation, 엑셀 요약 시트에 사용)."""
    match_text = "정확히 일치" if match_type == "exact" else "유사 키워드로 선택"
    related_text = f" 연관 논문으로 '{related.title}'도 함께 제공합니다." if related else ""
    return (
        f"질의에서 {', '.join(query_keywords)} 키워드를 추출했고 저장 키워드 "
        f"'{matched_keyword}'와 {match_text}했습니다. 점수 근거에 따라 "
        f"'{primary.title}'을 대표 논문으로 선택했습니다.{related_text}"
    )


def _clean_keywords(value: object) -> list[str]:
    """LLM JSON 응답의 keywords 필드를 검증한다. 리스트가 아니면 빈 값, 원소는 문자열화·공백 제거·중복 제거만 한다."""
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        keyword = str(item).strip()
        if keyword and keyword not in cleaned:
            cleaned.append(keyword)
    return cleaned


def _extract_noun_phrases(query: str) -> list[str]:
    """Kiwi 형태소 분석으로 질의에서 명사(구)만 뽑는 LLM 미사용 빠른 키워드 추출 경로.

    ingest.keywords와 같은 캐시된 Kiwi 인스턴스(_kiwi)를 재사용한다. 조사·어미가
    붙은 한국어 질의("스마트팩토리에서")에서도 형태소 태그(NN*: 일반/고유/의존명사,
    SL/SH/SN: 외국어·한자·숫자)를 보고 명사류 토큰만 골라 붙여 하나의 명사구로
    합친다(연속된 명사 태그를 이어 붙임). kiwipiepy가 설치되어 있지 않거나 분석에
    실패하면 빈 목록을 반환하며, 이 경우 호출자(extract_keywords)가 정규식 기반
    _fallback_keywords로 넘어간다.
    """
    kiwi = _kiwi()
    if kiwi is None:
        return []
    try:
        analyses = kiwi.analyze(query)
    except Exception:
        return []
    if not analyses:
        return []

    phrases: list[str] = []
    current = ""
    for token in analyses[0][0]:
        tag = str(getattr(token, "tag", ""))
        form = str(getattr(token, "form", "")).strip()
        if not form:
            continue
        if tag.startswith("NN") or tag in {"SL", "SH", "SN"}:
            current += form
            continue
        if current:
            phrases.append(current)
            current = ""
    if current:
        phrases.append(current)
    return [phrase for phrase in phrases if len(phrase) >= 2]


def _fallback_keywords(query: str) -> list[str]:
    """형태소 분석 경로도 아무것도 못 찾았을 때 쓰는 최후의 정규식 기반 폴백: 영문 토큰과 2글자 이상 한글 토큰을 추출한다."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]+|[가-힣]{2,}", query)
    return [token for token in tokens if len(token.strip()) >= 2]


def _normalize_unique(keywords: list[str]) -> list[str]:
    """키워드 목록을 Kiwi 정규화(normalize)한 뒤 중복을 제거한다(표기 변형 흡수, 등장 순서는 유지)."""
    normalized: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        value = normalize(keyword)
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _year_weight(published_year: int | None, current_year: int | None = None) -> float:
    """대표 논문 점수식의 연도 가중치. 발행연도가 없으면 0, 올해 발행이면 1에 가깝고 10년 이상 지나면 0으로 선형 감쇠한다."""
    if published_year is None:
        return 0.0
    year = current_year or datetime.now(UTC).year
    age = max(0, year - published_year)
    return max(0.0, min(1.0, 1.0 - age / 10.0))
