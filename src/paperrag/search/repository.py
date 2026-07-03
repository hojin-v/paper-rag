import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from paperrag.config import Settings
from paperrag.db import get_engine
from paperrag.ingest.keywords import normalize
from paperrag.search.schemas import KeywordCandidate


@dataclass(frozen=True)
class KeywordRow:
    keyword_id: int
    keyword: str
    display_form: str
    frequency: int
    embedding: list[float] | None = None


@dataclass(frozen=True)
class PaperKeywordRow:
    paper_id: int
    kw_score: float


@dataclass(frozen=True)
class PaperMetaRow:
    paper_id: int
    title: str
    authors: str = ""
    published_year: int | None = None
    journal: str | None = None
    abstract: str = ""
    abstract_summary: str | None = None
    full_text_link: str | None = None


@dataclass(frozen=True)
class ParagraphRow:
    paragraph_id: int
    paper_id: int
    paragraph_order: int
    section_name: str = ""
    original_text: str = ""
    cleaned_text: str = ""
    summary: str = ""
    keywords: list[str] | None = None


@dataclass(frozen=True)
class TableRow:
    table_id: int
    paper_id: int
    table_title: str | None
    table_text: str
    table_summary: str | None = None


class SearchRepository(Protocol):
    def keyword_by_id(self, keyword_id: int) -> KeywordRow | None:
        """Return one keyword row by id."""

    def find_keyword_exact(self, normalized: str) -> KeywordRow | None:
        """Find keyword by normalized keyword or alias."""

    def similar_keywords(
        self,
        vec: Sequence[float],
        top_k: int = 3,
        min_sim: float = 0.6,
    ) -> list[KeywordCandidate]:
        """Return vector-nearest keywords."""

    def papers_for_keyword(self, keyword_id: int) -> list[PaperKeywordRow]:
        """Return papers linked to a keyword."""

    def best_paragraph_similarity(self, paper_id: int, vec: Sequence[float]) -> float:
        """Return the best relevant paragraph similarity for a paper."""

    def paper_meta(self, paper_id: int) -> PaperMetaRow | None:
        """Return paper metadata."""

    def paper_keywords(self, paper_id: int) -> list[str]:
        """Return display keywords for a paper."""

    def title_abstract_contains(self, paper_id: int, keyword: str) -> bool:
        """Return whether title or abstract contains the keyword."""

    def top_relation(self, paper_id: int) -> tuple[int, float, str] | None:
        """Return the top precomputed related paper."""

    def paragraphs_of(self, paper_id: int) -> list[ParagraphRow]:
        """Return topic-relevant paragraphs ordered by paragraph order."""

    def tables_of(self, paper_id: int) -> list[TableRow]:
        """Return paper tables."""

    def save_result(
        self,
        result_id: str,
        *,
        query: str,
        match_type: str,
        matched_keyword_id: int,
        primary_paper_id: int,
        related_paper_id: int | None,
        excel_path: str,
    ) -> None:
        """Persist a generated search result."""

    def load_result(self, result_id: str) -> str | None:
        """Return a cached Excel path."""


class PostgresSearchRepository:
    def __init__(self, settings: Settings | None = None, engine: Engine | None = None) -> None:
        self.engine = engine or get_engine(settings)

    def keyword_by_id(self, keyword_id: int) -> KeywordRow | None:
        statement = text(
            """
            SELECT keyword_id, keyword, display_form, frequency, embedding::text AS embedding
            FROM keywords
            WHERE keyword_id = :keyword_id
            """
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement, {"keyword_id": keyword_id}).mappings().first()
        return _keyword_from_mapping(row) if row is not None else None

    def find_keyword_exact(self, normalized: str) -> KeywordRow | None:
        statement = text(
            """
            SELECT
                k.keyword_id,
                k.keyword,
                k.display_form,
                k.frequency,
                k.embedding::text AS embedding
            FROM keywords k
            LEFT JOIN keyword_aliases ka ON ka.keyword_id = k.keyword_id
            WHERE k.keyword = :normalized OR ka.alias = :normalized
            ORDER BY CASE WHEN k.keyword = :normalized THEN 0 ELSE 1 END
            LIMIT 1
            """
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement, {"normalized": normalized}).mappings().first()
        return _keyword_from_mapping(row) if row is not None else None

    def similar_keywords(
        self,
        vec: Sequence[float],
        top_k: int = 3,
        min_sim: float = 0.6,
    ) -> list[KeywordCandidate]:
        if not vec:
            return []
        statement = text(
            """
            SELECT
                keyword_id,
                display_form,
                1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM keywords
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
            """
        )
        with self.engine.begin() as connection:
            rows = connection.execute(
                statement,
                {"embedding": _vector_literal(vec), "top_k": int(top_k)},
            ).mappings().all()
        return [
            KeywordCandidate(
                keyword_id=int(row["keyword_id"]),
                keyword=str(row["display_form"]),
                similarity=float(row["similarity"]),
            )
            for row in rows
            if float(row["similarity"]) >= min_sim
        ]

    def papers_for_keyword(self, keyword_id: int) -> list[PaperKeywordRow]:
        statement = text(
            """
            SELECT paper_id, score
            FROM paper_keywords
            WHERE keyword_id = :keyword_id
            ORDER BY score DESC, paper_id ASC
            """
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement, {"keyword_id": keyword_id}).mappings().all()
        return [
            PaperKeywordRow(paper_id=int(row["paper_id"]), kw_score=float(row["score"]))
            for row in rows
        ]

    def best_paragraph_similarity(self, paper_id: int, vec: Sequence[float]) -> float:
        if not vec:
            return 0.0
        statement = text(
            """
            SELECT 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM paragraphs
            WHERE paper_id = :paper_id
              AND is_topic_relevant = true
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
            """
        )
        with self.engine.begin() as connection:
            similarity = connection.execute(
                statement,
                {"paper_id": paper_id, "embedding": _vector_literal(vec)},
            ).scalar_one_or_none()
        return float(similarity or 0.0)

    def paper_meta(self, paper_id: int) -> PaperMetaRow | None:
        statement = text(
            """
            SELECT
                paper_id,
                title,
                authors,
                published_year,
                journal,
                abstract,
                abstract_summary,
                full_text_link
            FROM papers
            WHERE paper_id = :paper_id
            """
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement, {"paper_id": paper_id}).mappings().first()
        return _paper_from_mapping(row) if row is not None else None

    def paper_keywords(self, paper_id: int) -> list[str]:
        statement = text(
            """
            SELECT k.display_form
            FROM paper_keywords pk
            JOIN keywords k ON k.keyword_id = pk.keyword_id
            WHERE pk.paper_id = :paper_id
            ORDER BY pk.score DESC, k.display_form ASC
            """
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement, {"paper_id": paper_id}).mappings().all()
        return [str(row["display_form"]) for row in rows]

    def title_abstract_contains(self, paper_id: int, keyword: str) -> bool:
        meta = self.paper_meta(paper_id)
        if meta is None:
            return False
        haystack = normalize(" ".join([meta.title, meta.abstract]))
        needle = normalize(keyword)
        return bool(needle and needle in haystack)

    def top_relation(self, paper_id: int) -> tuple[int, float, str] | None:
        statement = text(
            """
            SELECT related_paper_id, relation_score, relation_reason
            FROM paper_relations
            WHERE source_paper_id = :paper_id
            ORDER BY relation_score DESC, related_paper_id ASC
            LIMIT 1
            """
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement, {"paper_id": paper_id}).mappings().first()
        if row is None:
            return None
        return (
            int(row["related_paper_id"]),
            float(row["relation_score"]),
            str(row["relation_reason"] or ""),
        )

    def paragraphs_of(self, paper_id: int) -> list[ParagraphRow]:
        statement = text(
            """
            SELECT
                paragraph_id,
                paper_id,
                paragraph_order,
                section_name,
                original_text,
                cleaned_text,
                summary
            FROM paragraphs
            WHERE paper_id = :paper_id
              AND is_topic_relevant = true
            ORDER BY paragraph_order ASC, paragraph_id ASC
            """
        )
        keywords = self.paper_keywords(paper_id)
        with self.engine.begin() as connection:
            rows = connection.execute(statement, {"paper_id": paper_id}).mappings().all()
        return [
            ParagraphRow(
                paragraph_id=int(row["paragraph_id"]),
                paper_id=int(row["paper_id"]),
                paragraph_order=int(row["paragraph_order"]),
                section_name=str(row["section_name"] or ""),
                original_text=str(row["original_text"] or ""),
                cleaned_text=str(row["cleaned_text"] or ""),
                summary=str(row["summary"] or ""),
                keywords=keywords,
            )
            for row in rows
        ]

    def tables_of(self, paper_id: int) -> list[TableRow]:
        statement = text(
            """
            SELECT table_id, paper_id, table_title, table_text, table_summary
            FROM paper_tables
            WHERE paper_id = :paper_id
            ORDER BY table_id ASC
            """
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement, {"paper_id": paper_id}).mappings().all()
        return [
            TableRow(
                table_id=int(row["table_id"]),
                paper_id=int(row["paper_id"]),
                table_title=row["table_title"],
                table_text=str(row["table_text"] or ""),
                table_summary=row["table_summary"],
            )
            for row in rows
        ]

    def save_result(
        self,
        result_id: str,
        *,
        query: str,
        match_type: str,
        matched_keyword_id: int,
        primary_paper_id: int,
        related_paper_id: int | None,
        excel_path: str,
    ) -> None:
        statement = text(
            """
            INSERT INTO search_results (
                result_id, query, match_type, matched_keyword_id,
                primary_paper_id, related_paper_id, excel_path
            )
            VALUES (
                :result_id, :query, :match_type, :matched_keyword_id,
                :primary_paper_id, :related_paper_id, :excel_path
            )
            ON CONFLICT (result_id) DO UPDATE
            SET
                query = EXCLUDED.query,
                match_type = EXCLUDED.match_type,
                matched_keyword_id = EXCLUDED.matched_keyword_id,
                primary_paper_id = EXCLUDED.primary_paper_id,
                related_paper_id = EXCLUDED.related_paper_id,
                excel_path = EXCLUDED.excel_path
            """
        )
        with self.engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "result_id": result_id,
                    "query": query,
                    "match_type": match_type,
                    "matched_keyword_id": matched_keyword_id,
                    "primary_paper_id": primary_paper_id,
                    "related_paper_id": related_paper_id,
                    "excel_path": excel_path,
                },
            )

    def load_result(self, result_id: str) -> str | None:
        statement = text("SELECT excel_path FROM search_results WHERE result_id = :result_id")
        with self.engine.begin() as connection:
            path = connection.execute(statement, {"result_id": result_id}).scalar_one_or_none()
        return str(path) if path else None


class InMemorySearchRepository:
    def __init__(
        self,
        *,
        keywords: Sequence[Mapping[str, Any]] | None = None,
        aliases: Mapping[str, int] | None = None,
        papers: Sequence[Mapping[str, Any]] | None = None,
        paper_keywords: Sequence[Mapping[str, Any]] | None = None,
        paragraphs: Sequence[Mapping[str, Any]] | None = None,
        tables: Sequence[Mapping[str, Any]] | None = None,
        relations: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.keywords: dict[int, dict[str, Any]] = {}
        self.aliases: dict[str, int] = dict(aliases or {})
        self.papers: dict[int, dict[str, Any]] = {}
        self.paper_keyword_rows: list[dict[str, Any]] = []
        self.paragraph_rows: list[dict[str, Any]] = []
        self.table_rows: list[dict[str, Any]] = []
        self.relation_rows: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}

        for keyword in keywords or []:
            self.add_keyword(**dict(keyword))
        for paper in papers or []:
            self.add_paper(**dict(paper))
        for row in paper_keywords or []:
            self.link_paper_keyword(**dict(row))
        for row in paragraphs or []:
            self.add_paragraph(**dict(row))
        for row in tables or []:
            self.add_table(**dict(row))
        for row in relations or []:
            self.add_relation(**dict(row))

    def add_keyword(
        self,
        keyword: str,
        display_form: str | None = None,
        frequency: int = 1,
        embedding: Sequence[float] | None = None,
        keyword_id: int | None = None,
        aliases: Sequence[str] | None = None,
    ) -> int:
        next_id = max(self.keywords, default=0) + 1
        actual_id = int(keyword_id or next_id)
        self.keywords[actual_id] = {
            "keyword_id": actual_id,
            "keyword": keyword,
            "display_form": display_form or keyword,
            "frequency": int(frequency),
            "embedding": list(embedding) if embedding is not None else None,
        }
        for alias in aliases or []:
            self.aliases[alias] = actual_id
        return actual_id

    def add_paper(
        self,
        title: str,
        paper_id: int | None = None,
        authors: str | Sequence[str] = "",
        published_year: int | None = None,
        journal: str | None = None,
        abstract: str = "",
        abstract_summary: str | None = None,
        full_text_link: str | None = None,
    ) -> int:
        next_id = max(self.papers, default=0) + 1
        actual_id = int(paper_id or next_id)
        self.papers[actual_id] = {
            "paper_id": actual_id,
            "title": title,
            "authors": _coerce_authors(authors),
            "published_year": published_year,
            "journal": journal,
            "abstract": abstract,
            "abstract_summary": abstract_summary,
            "full_text_link": full_text_link,
        }
        return actual_id

    def link_paper_keyword(self, paper_id: int, keyword_id: int, score: float) -> None:
        self.paper_keyword_rows = [
            row
            for row in self.paper_keyword_rows
            if not (row["paper_id"] == paper_id and row["keyword_id"] == keyword_id)
        ]
        self.paper_keyword_rows.append(
            {"paper_id": int(paper_id), "keyword_id": int(keyword_id), "score": float(score)}
        )

    def add_paragraph(
        self,
        paper_id: int,
        paragraph_order: int,
        original_text: str,
        paragraph_id: int | None = None,
        section_name: str = "",
        cleaned_text: str = "",
        summary: str = "",
        is_topic_relevant: bool = True,
        embedding: Sequence[float] | None = None,
        keywords: Sequence[str] | None = None,
    ) -> int:
        next_id = max((int(row["paragraph_id"]) for row in self.paragraph_rows), default=0) + 1
        actual_id = int(paragraph_id or next_id)
        self.paragraph_rows.append(
            {
                "paragraph_id": actual_id,
                "paper_id": int(paper_id),
                "paragraph_order": int(paragraph_order),
                "section_name": section_name,
                "original_text": original_text,
                "cleaned_text": cleaned_text,
                "summary": summary,
                "is_topic_relevant": bool(is_topic_relevant),
                "embedding": list(embedding) if embedding is not None else None,
                "keywords": list(keywords) if keywords is not None else None,
            }
        )
        return actual_id

    def add_table(
        self,
        paper_id: int,
        table_text: str,
        table_id: int | None = None,
        table_title: str | None = None,
        table_summary: str | None = None,
    ) -> int:
        next_id = max((int(row["table_id"]) for row in self.table_rows), default=0) + 1
        actual_id = int(table_id or next_id)
        self.table_rows.append(
            {
                "table_id": actual_id,
                "paper_id": int(paper_id),
                "table_title": table_title,
                "table_text": table_text,
                "table_summary": table_summary,
            }
        )
        return actual_id

    def add_relation(
        self,
        source_paper_id: int,
        related_paper_id: int,
        relation_score: float,
        relation_reason: str = "",
    ) -> None:
        self.relation_rows.append(
            {
                "source_paper_id": int(source_paper_id),
                "related_paper_id": int(related_paper_id),
                "relation_score": float(relation_score),
                "relation_reason": relation_reason,
            }
        )

    def keyword_by_id(self, keyword_id: int) -> KeywordRow | None:
        row = self.keywords.get(keyword_id)
        return _keyword_from_mapping(row) if row is not None else None

    def find_keyword_exact(self, normalized: str) -> KeywordRow | None:
        for row in self.keywords.values():
            if row["keyword"] == normalized:
                return _keyword_from_mapping(row)
        keyword_id = self.aliases.get(normalized)
        if keyword_id is None:
            return None
        return self.keyword_by_id(keyword_id)

    def similar_keywords(
        self,
        vec: Sequence[float],
        top_k: int = 3,
        min_sim: float = 0.6,
    ) -> list[KeywordCandidate]:
        scored: list[KeywordCandidate] = []
        for row in self.keywords.values():
            embedding = row.get("embedding")
            if embedding is None:
                continue
            similarity = _cosine(vec, embedding)
            if similarity >= min_sim:
                scored.append(
                    KeywordCandidate(
                        keyword_id=int(row["keyword_id"]),
                        keyword=str(row["display_form"]),
                        similarity=similarity,
                    )
                )
        return sorted(scored, key=lambda item: item.similarity, reverse=True)[:top_k]

    def papers_for_keyword(self, keyword_id: int) -> list[PaperKeywordRow]:
        rows = [
            PaperKeywordRow(paper_id=int(row["paper_id"]), kw_score=float(row["score"]))
            for row in self.paper_keyword_rows
            if int(row["keyword_id"]) == keyword_id
        ]
        return sorted(rows, key=lambda row: (-row.kw_score, row.paper_id))

    def best_paragraph_similarity(self, paper_id: int, vec: Sequence[float]) -> float:
        scores = [
            _cosine(vec, row["embedding"])
            for row in self.paragraph_rows
            if int(row["paper_id"]) == paper_id
            and row.get("is_topic_relevant", True)
            and row.get("embedding") is not None
        ]
        return max(scores, default=0.0)

    def paper_meta(self, paper_id: int) -> PaperMetaRow | None:
        row = self.papers.get(paper_id)
        return _paper_from_mapping(row) if row is not None else None

    def paper_keywords(self, paper_id: int) -> list[str]:
        rows = [row for row in self.paper_keyword_rows if int(row["paper_id"]) == paper_id]
        rows.sort(key=lambda row: (-float(row["score"]), int(row["keyword_id"])))
        keywords: list[str] = []
        for row in rows:
            keyword = self.keywords.get(int(row["keyword_id"]))
            if keyword is not None:
                keywords.append(str(keyword["display_form"]))
        return keywords

    def title_abstract_contains(self, paper_id: int, keyword: str) -> bool:
        paper = self.papers.get(paper_id)
        if paper is None:
            return False
        haystack = normalize(" ".join([str(paper["title"]), str(paper.get("abstract", ""))]))
        needle = normalize(keyword)
        return bool(needle and needle in haystack)

    def top_relation(self, paper_id: int) -> tuple[int, float, str] | None:
        rows = [row for row in self.relation_rows if int(row["source_paper_id"]) == paper_id]
        if not rows:
            return None
        row = max(rows, key=lambda item: float(item["relation_score"]))
        return (
            int(row["related_paper_id"]),
            float(row["relation_score"]),
            str(row.get("relation_reason") or ""),
        )

    def paragraphs_of(self, paper_id: int) -> list[ParagraphRow]:
        paper_keywords = self.paper_keywords(paper_id)
        rows = [
            row
            for row in self.paragraph_rows
            if int(row["paper_id"]) == paper_id and row.get("is_topic_relevant", True)
        ]
        rows.sort(key=lambda row: (int(row["paragraph_order"]), int(row["paragraph_id"])))
        return [
            ParagraphRow(
                paragraph_id=int(row["paragraph_id"]),
                paper_id=int(row["paper_id"]),
                paragraph_order=int(row["paragraph_order"]),
                section_name=str(row.get("section_name") or ""),
                original_text=str(row.get("original_text") or ""),
                cleaned_text=str(row.get("cleaned_text") or ""),
                summary=str(row.get("summary") or ""),
                keywords=list(row.get("keywords") or paper_keywords),
            )
            for row in rows
        ]

    def tables_of(self, paper_id: int) -> list[TableRow]:
        rows = [row for row in self.table_rows if int(row["paper_id"]) == paper_id]
        rows.sort(key=lambda row: int(row["table_id"]))
        return [
            TableRow(
                table_id=int(row["table_id"]),
                paper_id=int(row["paper_id"]),
                table_title=row.get("table_title"),
                table_text=str(row.get("table_text") or ""),
                table_summary=row.get("table_summary"),
            )
            for row in rows
        ]

    def save_result(
        self,
        result_id: str,
        *,
        query: str,
        match_type: str,
        matched_keyword_id: int,
        primary_paper_id: int,
        related_paper_id: int | None,
        excel_path: str,
    ) -> None:
        self.results[result_id] = {
            "result_id": result_id,
            "query": query,
            "match_type": match_type,
            "matched_keyword_id": matched_keyword_id,
            "primary_paper_id": primary_paper_id,
            "related_paper_id": related_paper_id,
            "excel_path": excel_path,
        }

    def load_result(self, result_id: str) -> str | None:
        result = self.results.get(result_id)
        if result is None:
            return None
        return str(result["excel_path"])


def _keyword_from_mapping(row: Mapping[str, Any] | None) -> KeywordRow:
    if row is None:
        raise ValueError("Keyword row is required.")
    return KeywordRow(
        keyword_id=int(row["keyword_id"]),
        keyword=str(row["keyword"]),
        display_form=str(row.get("display_form") or row["keyword"]),
        frequency=int(row.get("frequency") or 0),
        embedding=_parse_vector(row.get("embedding")),
    )


def _paper_from_mapping(row: Mapping[str, Any] | None) -> PaperMetaRow:
    if row is None:
        raise ValueError("Paper row is required.")
    return PaperMetaRow(
        paper_id=int(row["paper_id"]),
        title=str(row.get("title") or "Untitled"),
        authors=_coerce_authors(row.get("authors") or ""),
        published_year=row.get("published_year"),
        journal=row.get("journal"),
        abstract=str(row.get("abstract") or ""),
        abstract_summary=row.get("abstract_summary"),
        full_text_link=row.get("full_text_link"),
    )


def _coerce_authors(value: str | Sequence[str] | Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return "; ".join(str(item) for item in value)
    return str(value or "")


def _vector_literal(vector: Sequence[float] | None) -> str | None:
    if vector is None:
        return None
    return "[" + ",".join(f"{float(value):.9g}" for value in vector) + "]"


def _parse_vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [float(item) for item in value]
    text_value = str(value).strip().strip("[]")
    if not text_value:
        return []
    return [float(item) for item in text_value.split(",")]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(left * right for left, right in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)

