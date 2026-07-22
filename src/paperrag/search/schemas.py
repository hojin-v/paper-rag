"""검색 API의 요청/응답 및 내부 결과 번들 Pydantic 스키마.

DESIGN.md §5.1의 2단계 인터랙션(POST /search → matched|suggest, POST /search/select,
GET /result/{result_id}/excel)에서 오가는 데이터 형태를 정의한다. `ResultBundle`은
API 응답 스키마는 아니지만 service.py가 excel.py에 전달하는 내부 계약으로,
엑셀 6개 시트를 만드는 데 필요한 모든 정보를 담는다.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """POST /search 요청 바디.

    query 외 나머지 필드는 전부 선택값이다. 질의 키워드 추출은 항상 LLM(Ollama)로
    이뤄진다 — 형태소 분석 빠른 경로는 사용자가 고르는 옵션이 아니라 LLM 실패
    시의 내부 안전망일 뿐이다(SearchService.extract_keywords 참고). section_query를
    지정하면 결과(엑셀 포함) 단락을 그 문자열을 포함하는 section_name으로만 좁힌다.
    include_related=False면 연관 논문 조회 자체를 생략하고 응답·엑셀에서
    연관 논문 관련 항목을 전부 제외한다. include_tables=False면 표 조회를
    생략하고 엑셀에 표 시트를 만들지 않는다. include_abstract=False면
    대표/연관 논문 정보 시트에서 초록 원문·초록 요약 칸을 비운다 — 셋 다
    "필요 없는 산출물은 아예 만들지 않는다"는 사용자 맞춤 구성을 위한 것이다.
    """

    query: str = Field(min_length=1)
    section_query: str | None = None
    include_related: bool = True
    include_tables: bool = True
    include_abstract: bool = True


class KeywordCandidate(BaseModel):
    """정확 매칭 실패 시 제시하는 유사 키워드 후보 하나.

    similarity는 질의 키워드 임베딩과 keywords.embedding 간 코사인 유사도이며,
    SearchSuggest.candidates에 최대 search_suggestion_limit개(기본 Top 3)만 담긴다.
    """

    keyword_id: int
    keyword: str
    similarity: float


class PaperSummary(BaseModel):
    """대표/연관 논문 각각에 대한 선정 결과 요약.

    score와 reason은 선정 근거를 그대로 노출하기 위한 필드로, 대표 논문이면
    가중합 점수식 계산 내역, 연관 논문이면 paper_relations.relation_score와
    relation_reason(겹치는 키워드 등)이 담긴다. relevance_summary는 이와 별개로
    LLM이 생성하는 자연어 설명(RAG 생성 단계) — 이 논문에서 질의와 가장 유사한
    단락 1개를 근거로 "왜 이 논문인가"를 사람이 읽을 문장으로 답한다. reason이
    "점수가 어떻게 계산됐는지"라면 relevance_summary는 "내용상 왜 관련 있는지"다.
    """

    paper_id: int
    title: str
    authors: str = ""
    published_year: int | None = None
    journal: str | None = None
    full_text_link: str | None = None
    keywords: list[str] = Field(default_factory=list)
    score: float
    reason: str
    relevance_summary: str | None = None


class SearchMatched(BaseModel):
    """POST /search 및 /search/select가 성공(정확 매칭 또는 선택 완료) 시 반환하는 응답.

    match_type으로 "정확 매칭(exact)"인지 "유사 키워드 선택 후 확정(selected)"인지
    구분하고, result_id는 이후 GET /result/{result_id}/excel 다운로드에 사용한다.
    available_sections는 대표(+연관) 논문에 실제로 존재하는 section_name을
    문서 등장 순서로 중복 없이 합친 목록이다 — 사용자가 다음 검색에서
    section_query를 자유 텍스트가 아니라 이 목록에서 골라 넣을 수 있게 하기 위한
    것으로, 대표 논문 선정 결과 자체와는 무관하다(현재 산출물 구성 기준으로
    "어떤 섹션이 있는지"만 알려준다).
    """

    status: Literal["matched"] = "matched"
    matched_keyword: str
    query_keywords: list[str] = Field(default_factory=list)
    match_type: Literal["exact", "selected"]
    explanation: str = ""
    result_id: str
    primary_paper: PaperSummary
    related_paper: PaperSummary | None = None
    available_sections: list[str] = Field(default_factory=list)


class SearchSuggest(BaseModel):
    """POST /search에서 정확 매칭에 실패했을 때 반환하는 응답.

    session_id는 SuggestionSessionStore가 발급한 TTL 30분짜리 세션 키이며,
    사용자가 candidates 중 keyword_id 하나를 골라 POST /search/select로 보내면
    SearchMatched로 이어진다.
    """

    status: Literal["suggest"] = "suggest"
    session_id: str
    query_keywords: list[str] = Field(default_factory=list)
    explanation: str = ""
    candidates: list[KeywordCandidate] = Field(default_factory=list)


class SelectRequest(BaseModel):
    """POST /search/select 요청 바디. suggest 단계의 session_id와 사용자가 고른 keyword_id."""

    session_id: str
    keyword_id: int


class PaperInfo(BaseModel):
    """엑셀 "대표/연관 논문 정보" 시트에 들어가는 논문 상세 메타데이터.

    PaperSummary와 달리 검색 선정 점수(score/reason)는 없고 초록 원문·요약 등
    엑셀 출력에 필요한 전체 필드를 담는다.
    """

    paper_id: int
    title: str
    authors: str = ""
    published_year: int | None = None
    journal: str | None = None
    abstract: str = ""
    abstract_summary: str | None = None
    full_text_link: str | None = None
    keywords: list[str] = Field(default_factory=list)


class ParagraphInfo(BaseModel):
    """엑셀 "대표/연관 논문 단락" 시트의 행 하나. is_topic_relevant=false 단락은 제외된 결과다."""

    paragraph_order: int
    section_name: str = ""
    original_text: str = ""
    cleaned_text: str = ""
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)


class SectionInfo(BaseModel):
    """엑셀 "대표/연관 논문 섹션" 시트의 행 하나.

    ParagraphInfo들을 section_name 기준으로 등장 순서대로 묶어 만든 집계 단위로,
    섹션 내 단락들의 원문/정제문/요약을 이어붙이고 키워드는 중복 없이 합친다.
    """

    section_order: int
    section_name: str
    paragraph_count: int
    original_text: str
    cleaned_text: str
    summary: str
    keywords: list[str] = Field(default_factory=list)


class TableInfo(BaseModel):
    """엑셀 "표 데이터"/"표 셀" 시트에 쓰이는 표 정보. role로 대표/연관 논문 소속을 구분한다."""

    role: Literal["대표", "연관"]
    table_title: str | None = None
    table_text: str = ""
    table_summary: str | None = None


class ResultBundle(BaseModel):
    """검색 1건의 결과를 엑셀로 만드는 데 필요한 모든 데이터를 모은 내부 번들.

    API 응답으로 직접 노출되지 않으며, SearchService.resolve()가 조립해
    search.excel.build_excel()에 넘긴다. created_at은 엑셀 "검색 결과 요약"
    시트의 생성 일시 칼럼에 쓰인다. include_related/include_tables는
    build_excel이 연관 논문 시트(정보/섹션/단락)와 표 시트(표 데이터/표 셀)를
    아예 만들지 말지를 결정하는 플래그다 — related_info가 우연히 None인
    경우(연관 논문이 없어서)와는 별개로, 사용자가 명시적으로 "필요 없음"을
    선택했을 때를 나타낸다. include_abstract=False면 primary_info/related_info의
    초록 원문·초록 요약 필드를 비워서 넘긴다(시트 자체는 유지, 해당 칸만 공백).
    """

    result_id: str
    query: str
    query_keywords: list[str] = Field(default_factory=list)
    matched_keyword: str
    match_type: Literal["exact", "selected"]
    explanation: str = ""
    primary_paper: PaperSummary
    related_paper: PaperSummary | None = None
    primary_info: PaperInfo
    related_info: PaperInfo | None = None
    primary_paragraphs: list[ParagraphInfo] = Field(default_factory=list)
    related_paragraphs: list[ParagraphInfo] = Field(default_factory=list)
    primary_sections: list[SectionInfo] = Field(default_factory=list)
    related_sections: list[SectionInfo] = Field(default_factory=list)
    tables: list[TableInfo] = Field(default_factory=list)
    include_related: bool = True
    include_tables: bool = True
    include_abstract: bool = True
    created_at: datetime
