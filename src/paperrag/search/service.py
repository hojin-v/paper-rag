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

## 질의 키워드 추출: 빠른 경로(기본) vs AI 경로(선택)

검색마다 LLM을 호출하면(Qwen2.5 7B, CPU) 요청 1건에 수십 초가 걸리고 Ollama가
요청을 직렬 처리하므로 동시 사용자가 늘면 대기열이 그대로 쌓인다(실측: 동시 3건
요청 시 완전 직렬 처리, 각 18~20초). 이를 피하기 위해 `extract_keywords`는
기본적으로 **Kiwi 형태소 분석으로 명사(구)만 뽑는 빠른 경로**(`_extract_noun_phrases`,
LLM 미호출)를 쓰고, 호출자가 `use_llm=True`로 명시적으로 요청했을 때만 LLM 경로로
넘어간다. 빠른 경로가 아무 키워드도 못 찾으면(형태소 분석 실패·미설치 등) 정규식
기반 최후 폴백(`_fallback_keywords`)으로 넘어간다 — 이 경로 역시 LLM을 호출하지
않는다. 즉 기본 검색 흐름은 완전히 LLM 없이 동작하며, "AI 자연어 검색"은 사용자가
명시적으로 켠 경우에만 그 비용(지연·직렬화)을 감수하는 선택적 기능이다.

## 섹션 필터: 산출물 커스터마이징

`section_query`를 지정하면 대표/연관 논문의 단락 중 `paragraphs.section_name`이
그 문자열을 포함하는 것만 결과(및 엑셀)에 담는다(`repo.paragraphs_of`의
section_query 인자, 대소문자 무시 부분 일치). 논문 전체를 통짜로 돌려주는 대신
"실험 방법만", "결과만" 같은 사용자 의도를 반영하기 위한 것이며, 어떤 논문이
대표/연관으로 선정되는지에는 영향을 주지 않는다(선정 로직은 항상 전체 단락
기준으로 계산한다) — 이미 확정된 결과의 "무엇을 보여줄지"만 좁히는 필터다.

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
from paperrag.ingest.llm_enrich import LLMClient
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
QUERY_KEYWORDS_SCHEMA_HINT = '{"keywords":["string","string","string"]}'
QUERY_KEYWORDS_PROMPT = """
너는 한국어/영어 논문 검색 질의에서 핵심 검색 키워드를 추출하는 연구 보조자다.
사용자의 자연어 질의에서 논문 키워드로 대조할 핵심 명사구 1~5개만 JSON으로 반환하라.
반드시 유효한 JSON만 반환하고, 설명 문장은 쓰지 마라.

반환 형식: {{"keywords":["키워드1","키워드2"]}}

질의:
{query}
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

    def extract_keywords(self, query: str, *, use_llm: bool = False) -> list[str]:
        """질의에서 핵심 키워드 1~5개를 뽑아 정규화된 유니크 목록으로 반환한다.

        기본(use_llm=False)은 Kiwi 형태소 분석으로 명사(구)만 추출하는 빠른 경로
        (_extract_noun_phrases)를 쓴다 — LLM을 호출하지 않으므로 지연이 거의 없고
        동시 요청에도 직렬화되지 않는다. 이 경로가 아무것도 못 찾으면(형태소 분석기
        미설치 등) 정규식 기반 최후 폴백(_fallback_keywords)으로 넘어가는데, 이
        폴백도 LLM을 쓰지 않는다.

        use_llm=True로 명시적으로 요청한 경우에만 Ollama LLM 자연어 이해 경로를
        탄다. LLM 호출이 실패하거나 JSON 파싱에 실패하면: allow_degraded_results가
        꺼져 있으면 SearchDependencyError를 던져 검색을 중단시키고(신뢰할 수
        없는 결과를 조용히 대체하지 않기 위함), 켜져 있으면 빠른 경로 결과 또는
        정규식 폴백으로 대체한다.
        """
        fast_keywords = _normalize_unique(_extract_noun_phrases(query))
        if not use_llm:
            return fast_keywords or _normalize_unique(_fallback_keywords(query))

        prompt = QUERY_KEYWORDS_PROMPT.format(query=query)
        try:
            data = self.llm.generate_json(prompt, QUERY_KEYWORDS_SCHEMA_HINT)
            keywords = _clean_keywords(data.get("keywords", []))
        except Exception as exc:
            if not self.settings.allow_degraded_results:
                raise SearchDependencyError(
                    "질의 키워드 LLM 응답을 검증하지 못했습니다. 규칙 기반 결과로 대체하지 않습니다."
                ) from exc
            keywords = []
        if not keywords:
            keywords = fast_keywords or _fallback_keywords(query)
        return _normalize_unique(keywords)

    def search(
        self,
        query: str,
        *,
        use_llm: bool = False,
        section_query: str | None = None,
    ) -> SearchMatched | SearchSuggest:
        """2단계 인터랙션의 1단계 진입점: 정확 매칭을 우선 시도하고, 실패하면 유사 키워드를 제안한다.

        1) 질의 키워드를 추출하고(기본은 LLM 미호출 빠른 경로, use_llm=True면 AI 경로)
           keywords/keyword_aliases와 정확 매칭을 시도한다(_best_exact_match).
           매칭되면 바로 resolve()로 대표/연관 논문을 확정한다.
        2) 정확 매칭이 없으면 질의 키워드(없으면 원문 질의)를 임베딩해 유사 키워드
           Top-N을 조회하고, 세션에 저장한 뒤 SearchSuggest로 후보를 돌려준다.
           사용자는 이 후보 중 하나를 골라 select()를 다시 호출해야 한다.
        section_query가 주어지면 최종 결과(및 엑셀)의 단락을 그 섹션명을 포함하는
        것만으로 좁힌다(세션에 저장돼 select() 이후에도 유지된다).
        """
        normalized_query = normalize(query)
        keywords = self.extract_keywords(query, use_llm=use_llm)
        exact_match = self._best_exact_match(normalized_query, keywords)
        if exact_match is not None:
            return self.resolve(
                exact_match.keyword_id,
                query,
                "exact",
                matched_keyword=exact_match.display_form,
                query_keywords=keywords,
                section_query=section_query,
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
        session = self.sessions.create(query, candidates, keywords, section_query=section_query)
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
        section_query(있었다면 search() 호출 시점의 필터)도 그대로 이어받는다.
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
        )

    def resolve(
        self,
        keyword_id: int,
        query: str,
        match_type: Literal["exact", "selected"],
        matched_keyword: str | None = None,
        query_keywords: list[str] | None = None,
        section_query: str | None = None,
    ) -> SearchMatched:
        """확정된 keyword_id로 대표/연관 논문을 선정하고 엑셀을 생성해 최종 응답을 만든다.

        exact/selected 두 경로 모두 마지막에 이 메서드로 합류한다. 순서는
        (1) 매칭 키워드 벡터 임베딩 (2) 대표 논문 선정(_select_primary)
        (3) paper_relations에서 연관 논문 조회(top_relation, 실시간 계산 없음)
        (4) ResultBundle 조립(section_query로 단락 범위 축소 가능) (5) 엑셀 생성
        및 result_id로 DB 캐시 저장, 순. section_query는 대표 논문 선정 자체에는
        영향을 주지 않는다 — 이미 확정된 결과에서 "무엇을 보여줄지"만 좁힌다.
        """
        keyword = self.repo.keyword_by_id(keyword_id)
        keyword_label = matched_keyword or (keyword.display_form if keyword else str(keyword_id))
        keyword_text = keyword.keyword if keyword is not None else normalize(keyword_label)
        extracted_keywords = list(query_keywords or [keyword_label])
        vector = self._embed_one(keyword_text or query)
        primary_row, primary_score, primary_reason = self._select_primary(
            keyword_id,
            keyword_text,
            vector,
        )
        primary_meta = self._required_paper_meta(primary_row.paper_id)
        primary_summary = self._paper_summary(primary_meta, primary_score, primary_reason)

        related_summary: PaperSummary | None = None
        related_meta: PaperMetaRow | None = None
        # 연관 논문은 대표 논문처럼 실시간 계산하지 않고, 수집 STEP 8에서 미리
        # 채워둔 paper_relations 중 relation_score 최고 1건을 그대로 조회한다.
        relation = self.repo.top_relation(primary_meta.paper_id)
        if relation is not None:
            related_id, relation_score, relation_reason = relation
            related_meta = self.repo.paper_meta(related_id)
            if related_meta is not None:
                related_summary = self._paper_summary(
                    related_meta,
                    relation_score,
                    relation_reason,
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
        return SearchMatched(
            matched_keyword=keyword_label,
            query_keywords=extracted_keywords,
            match_type=match_type,
            explanation=bundle.explanation,
            result_id=result_id,
            primary_paper=primary_summary,
            related_paper=related_summary,
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
    ) -> ResultBundle:
        """대표/연관 논문의 상세 정보·단락·섹션·표를 모두 모아 엑셀 생성용 ResultBundle을 만든다.

        section_query가 주어지면 단락 목록을 section_name 부분 일치(대소문자 무시)로
        좁혀서 가져온다(repo.paragraphs_of). 표/논문 키워드 목록은 필터 대상이 아니다 —
        섹션 필터는 어디까지나 "이번 산출물에 어떤 단락 텍스트를 포함할지"를 사용자가
        고르는 기능이라서다.
        """
        primary_info = _paper_info(primary_meta, self.repo.paper_keywords(primary_meta.paper_id))
        related_info = (
            _paper_info(related_meta, self.repo.paper_keywords(related_meta.paper_id))
            if related_meta is not None
            else None
        )
        tables: list[TableInfo] = [
            TableInfo(
                role="대표",
                table_title=row.table_title,
                table_text=row.table_text,
                table_summary=row.table_summary,
            )
            for row in self.repo.tables_of(primary_meta.paper_id)
        ]
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


def _paper_info(meta: PaperMetaRow, keywords: list[str]) -> PaperInfo:
    """PaperMetaRow + 논문 키워드 목록을 엑셀/응답용 PaperInfo로 변환한다."""
    return PaperInfo(
        paper_id=meta.paper_id,
        title=meta.title,
        authors=meta.authors,
        published_year=meta.published_year,
        journal=meta.journal,
        abstract=meta.abstract,
        abstract_summary=meta.abstract_summary,
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
