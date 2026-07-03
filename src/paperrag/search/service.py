import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from paperrag.config import Settings, get_settings
from paperrag.ingest.embeddings import EmbeddingClient
from paperrag.ingest.keywords import normalize
from paperrag.ingest.llm_enrich import LLMClient
from paperrag.search.excel import build_excel
from paperrag.search.repository import KeywordRow, PaperKeywordRow, PaperMetaRow, SearchRepository
from paperrag.search.schemas import (
    PaperInfo,
    PaperSummary,
    ParagraphInfo,
    ResultBundle,
    SearchMatched,
    SearchSuggest,
    TableInfo,
)
from paperrag.search.sessions import SuggestionSessionStore, new_result_id

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
    """Suggest session was not found or expired."""


class SearchNoPaperFound(Exception):
    """No paper can be selected for the keyword."""


class SearchService:
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
        prompt = QUERY_KEYWORDS_PROMPT.format(query=query)
        try:
            data = self.llm.generate_json(prompt, QUERY_KEYWORDS_SCHEMA_HINT)
            keywords = _clean_keywords(data.get("keywords", []))
        except Exception:
            keywords = []
        if not keywords:
            keywords = _fallback_keywords(query)
        return _normalize_unique(keywords)

    def search(self, query: str) -> SearchMatched | SearchSuggest:
        normalized_query = normalize(query)
        keywords = self.extract_keywords(query)
        exact_match = self._best_exact_match(normalized_query, keywords)
        if exact_match is not None:
            return self.resolve(
                exact_match.keyword_id,
                query,
                "exact",
                matched_keyword=exact_match.display_form,
            )

        vector_text = " ".join(keywords) if keywords else query
        vector = self._embed_one(vector_text)
        candidates = self.repo.similar_keywords(
            vector,
            top_k=self.settings.search_suggestion_limit,
            min_sim=self.settings.search_similarity_threshold,
        )
        session = self.sessions.create(query, candidates)
        return SearchSuggest(session_id=session.session_id, candidates=candidates)

    def select(self, session_id: str, keyword_id: int) -> SearchMatched:
        session = self.sessions.get(session_id)
        if session is None:
            raise SearchSessionNotFound(session_id)
        selected = next(
            (candidate for candidate in session.candidates if candidate.keyword_id == keyword_id),
            None,
        )
        if selected is None:
            raise SearchSessionNotFound(session_id)
        return self.resolve(keyword_id, session.query, "selected", matched_keyword=selected.keyword)

    def resolve(
        self,
        keyword_id: int,
        query: str,
        match_type: Literal["exact", "selected"],
        matched_keyword: str | None = None,
    ) -> SearchMatched:
        keyword = self.repo.keyword_by_id(keyword_id)
        keyword_label = matched_keyword or (keyword.display_form if keyword else str(keyword_id))
        keyword_text = keyword.keyword if keyword is not None else normalize(keyword_label)
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
            primary=primary_summary,
            primary_meta=primary_meta,
            related=related_summary,
            related_meta=related_meta,
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
            match_type=match_type,
            result_id=result_id,
            primary_paper=primary_summary,
            related_paper=related_summary,
        )

    def result_excel_path(self, result_id: str) -> str | None:
        path = self.repo.load_result(result_id)
        if path is None:
            return None
        return path if Path(path).exists() else None

    def _best_exact_match(
        self,
        normalized_query: str,
        keywords: list[str],
    ) -> KeywordRow | None:
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
        primary: PaperSummary,
        primary_meta: PaperMetaRow,
        related: PaperSummary | None,
        related_meta: PaperMetaRow | None,
    ) -> ResultBundle:
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
        return ResultBundle(
            result_id=result_id,
            query=query,
            matched_keyword=matched_keyword,
            match_type=match_type,
            primary_paper=primary,
            related_paper=related,
            primary_info=primary_info,
            related_info=related_info,
            primary_paragraphs=[
                ParagraphInfo(
                    paragraph_order=row.paragraph_order,
                    section_name=row.section_name,
                    original_text=row.original_text,
                    cleaned_text=row.cleaned_text,
                    summary=row.summary,
                    keywords=list(row.keywords or []),
                )
                for row in self.repo.paragraphs_of(primary_meta.paper_id)
            ],
            related_paragraphs=[
                ParagraphInfo(
                    paragraph_order=row.paragraph_order,
                    section_name=row.section_name,
                    original_text=row.original_text,
                    cleaned_text=row.cleaned_text,
                    summary=row.summary,
                    keywords=list(row.keywords or []),
                )
                for row in self.repo.paragraphs_of(related_meta.paper_id)
            ]
            if related_meta is not None
            else [],
            tables=tables,
            created_at=datetime.now(UTC),
        )

    def _required_paper_meta(self, paper_id: int) -> PaperMetaRow:
        meta = self.repo.paper_meta(paper_id)
        if meta is None:
            raise SearchNoPaperFound(f"paper_id={paper_id} was not found")
        return meta

    def _embed_one(self, text: str) -> list[float]:
        vectors = self.embedder.embed([text])
        return vectors[0] if vectors else []


def _paper_info(meta: PaperMetaRow, keywords: list[str]) -> PaperInfo:
    return PaperInfo(
        paper_id=meta.paper_id,
        title=meta.title,
        authors=meta.authors,
        published_year=meta.published_year,
        journal=meta.journal,
        abstract_summary=meta.abstract_summary,
        full_text_link=meta.full_text_link,
        keywords=keywords,
    )


def _clean_keywords(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        keyword = str(item).strip()
        if keyword and keyword not in cleaned:
            cleaned.append(keyword)
    return cleaned


def _fallback_keywords(query: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]+|[가-힣]{2,}", query)
    return [token for token in tokens if len(token.strip()) >= 2]


def _normalize_unique(keywords: list[str]) -> list[str]:
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
    if published_year is None:
        return 0.0
    year = current_year or datetime.now(UTC).year
    age = max(0, year - published_year)
    return max(0.0, min(1.0, 1.0 - age / 10.0))
