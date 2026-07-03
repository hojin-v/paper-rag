from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from paperrag.config import Settings
from paperrag.db import get_engine
from paperrag.ingest.models import PaperMeta, TableDraft


@dataclass(frozen=True)
class ParagraphRecord:
    section_name: str
    paragraph_order: int
    original_text: str
    cleaned_text: str
    summary: str
    is_topic_relevant: bool
    embedding: list[float] | None = None


class IngestRepository(Protocol):
    def save_paper(
        self,
        meta: PaperMeta,
        source_path: str,
        embedding: list[float] | None = None,
    ) -> int:
        """Persist paper metadata and return paper_id."""

    def save_paragraphs(self, paper_id: int, paragraphs: Sequence[ParagraphRecord]) -> list[int]:
        """Persist paragraphs and return paragraph_ids."""

    def upsert_keyword(
        self,
        normalized: str,
        display: str,
        embedding: list[float] | None = None,
    ) -> int:
        """Insert or update a normalized keyword and return keyword_id."""

    def link_paper_keyword(self, paper_id: int, keyword_id: int, score: float) -> None:
        """Link a paper to a keyword with an ingest-time score."""

    def save_table(
        self,
        paper_id: int,
        table: TableDraft,
        summary: str,
        embedding: list[float] | None = None,
    ) -> int:
        """Persist a table and return table_id."""

    def save_relations(self, paper_id: int, relations: Sequence[tuple[int, float, str]]) -> None:
        """Persist related paper scores."""

    def set_job_stage(
        self,
        paper_id: int | None,
        stage: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Persist stage status."""


class PostgresIngestRepository:
    def __init__(self, settings: Settings | None = None, engine: Engine | None = None) -> None:
        self.engine = engine or get_engine(settings)

    def save_paper(
        self,
        meta: PaperMeta,
        source_path: str,
        embedding: list[float] | None = None,
    ) -> int:
        statement = text(
            """
            INSERT INTO papers (
                title, authors, published_year, journal, abstract,
                source_file_path, paper_embedding, status
            )
            VALUES (
                :title, :authors, :published_year, :journal, :abstract,
                :source_file_path, CAST(:paper_embedding AS vector), 'ingested'
            )
            RETURNING paper_id
            """
        )
        with self.engine.begin() as connection:
            paper_id = connection.execute(
                statement,
                {
                    "title": meta.title or "Untitled",
                    "authors": "; ".join(meta.authors),
                    "published_year": meta.published_year,
                    "journal": meta.journal,
                    "abstract": meta.abstract,
                    "source_file_path": source_path,
                    "paper_embedding": _vector_literal(embedding),
                },
            ).scalar_one()
        return int(paper_id)

    def update_paper_embedding(self, paper_id: int, embedding: list[float]) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE papers
                    SET paper_embedding = CAST(:paper_embedding AS vector), status = 'ingested'
                    WHERE paper_id = :paper_id
                    """
                ),
                {"paper_id": paper_id, "paper_embedding": _vector_literal(embedding)},
            )

    def save_paragraphs(self, paper_id: int, paragraphs: Sequence[ParagraphRecord]) -> list[int]:
        statement = text(
            """
            INSERT INTO paragraphs (
                paper_id, section_name, paragraph_order, original_text,
                cleaned_text, summary, is_topic_relevant, embedding
            )
            VALUES (
                :paper_id, :section_name, :paragraph_order, :original_text,
                :cleaned_text, :summary, :is_topic_relevant, CAST(:embedding AS vector)
            )
            RETURNING paragraph_id
            """
        )
        paragraph_ids: list[int] = []
        with self.engine.begin() as connection:
            for paragraph in paragraphs:
                paragraph_id = connection.execute(
                    statement,
                    {
                        "paper_id": paper_id,
                        "section_name": paragraph.section_name,
                        "paragraph_order": paragraph.paragraph_order,
                        "original_text": paragraph.original_text,
                        "cleaned_text": paragraph.cleaned_text,
                        "summary": paragraph.summary,
                        "is_topic_relevant": paragraph.is_topic_relevant,
                        "embedding": _vector_literal(paragraph.embedding),
                    },
                ).scalar_one()
                paragraph_ids.append(int(paragraph_id))
        return paragraph_ids

    def upsert_keyword(
        self,
        normalized: str,
        display: str,
        embedding: list[float] | None = None,
    ) -> int:
        statement = text(
            """
            INSERT INTO keywords (keyword, display_form, embedding)
            VALUES (:keyword, :display_form, CAST(:embedding AS vector))
            ON CONFLICT (keyword) DO UPDATE
            SET
                frequency = keywords.frequency + 1,
                display_form = EXCLUDED.display_form,
                embedding = COALESCE(EXCLUDED.embedding, keywords.embedding)
            RETURNING keyword_id
            """
        )
        with self.engine.begin() as connection:
            keyword_id = connection.execute(
                statement,
                {
                    "keyword": normalized,
                    "display_form": display,
                    "embedding": _vector_literal(embedding),
                },
            ).scalar_one()
        return int(keyword_id)

    def link_paper_keyword(self, paper_id: int, keyword_id: int, score: float) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO paper_keywords (paper_id, keyword_id, score)
                    VALUES (:paper_id, :keyword_id, :score)
                    ON CONFLICT (paper_id, keyword_id) DO UPDATE
                    SET score = EXCLUDED.score
                    """
                ),
                {"paper_id": paper_id, "keyword_id": keyword_id, "score": score},
            )

    def save_table(
        self,
        paper_id: int,
        table: TableDraft,
        summary: str,
        embedding: list[float] | None = None,
    ) -> int:
        statement = text(
            """
            INSERT INTO paper_tables (
                paper_id, table_title, table_text, table_summary, embedding
            )
            VALUES (
                :paper_id, :table_title, :table_text, :table_summary, CAST(:embedding AS vector)
            )
            RETURNING table_id
            """
        )
        with self.engine.begin() as connection:
            table_id = connection.execute(
                statement,
                {
                    "paper_id": paper_id,
                    "table_title": table.table_title,
                    "table_text": table.table_text,
                    "table_summary": summary,
                    "embedding": _vector_literal(embedding),
                },
            ).scalar_one()
        return int(table_id)

    def save_relations(self, paper_id: int, relations: Sequence[tuple[int, float, str]]) -> None:
        statement = text(
            """
            INSERT INTO paper_relations (
                source_paper_id, related_paper_id, relation_score, relation_reason
            )
            VALUES (:source_paper_id, :related_paper_id, :relation_score, :relation_reason)
            ON CONFLICT (source_paper_id, related_paper_id) DO UPDATE
            SET
                relation_score = EXCLUDED.relation_score,
                relation_reason = EXCLUDED.relation_reason
            """
        )
        with self.engine.begin() as connection:
            for related_paper_id, score, reason in relations:
                connection.execute(
                    statement,
                    {
                        "source_paper_id": paper_id,
                        "related_paper_id": related_paper_id,
                        "relation_score": score,
                        "relation_reason": reason,
                    },
                )

    def set_job_stage(
        self,
        paper_id: int | None,
        stage: str,
        status: str,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO processing_jobs (
                        paper_id, stage, status, error, started_at, finished_at
                    )
                    VALUES (
                        :paper_id, :stage, :status, :error, :started_at, :finished_at
                    )
                    """
                ),
                {
                    "paper_id": paper_id,
                    "stage": stage,
                    "status": status,
                    "error": error,
                    "started_at": now if status == "running" else None,
                    "finished_at": now if status in {"done", "failed"} else None,
                },
            )

    def list_relation_candidates(self, paper_id: int) -> list[dict[str, Any]]:
        statement = text(
            """
            SELECT
                p.paper_id,
                p.published_year,
                p.paper_embedding::text AS embedding,
                COALESCE(
                    pk.keywords,
                    ARRAY[]::text[]
                ) AS keywords
            FROM papers p
            LEFT JOIN (
                SELECT
                    paper_keywords.paper_id,
                    array_agg(keywords.keyword) FILTER (WHERE keywords.keyword IS NOT NULL)
                        AS keywords
                FROM paper_keywords
                JOIN keywords ON keywords.keyword_id = paper_keywords.keyword_id
                GROUP BY paper_keywords.paper_id
            ) pk ON pk.paper_id = p.paper_id
            WHERE p.paper_id <> :paper_id
              AND p.paper_embedding IS NOT NULL
            """
        )
        with self.engine.begin() as connection:
            rows = connection.execute(statement, {"paper_id": paper_id}).mappings().all()
        return [
            {
                "paper_id": int(row["paper_id"]),
                "published_year": row["published_year"],
                "embedding": _parse_vector(row["embedding"]),
                "keywords": set(row["keywords"] or []),
            }
            for row in rows
        ]


class InMemoryIngestRepository:
    def __init__(self) -> None:
        self.papers: dict[int, dict[str, Any]] = {}
        self.paragraphs: dict[int, dict[str, Any]] = {}
        self.keywords: dict[int, dict[str, Any]] = {}
        self.paper_keywords: list[dict[str, Any]] = []
        self.tables: dict[int, dict[str, Any]] = {}
        self.relations: list[dict[str, Any]] = []
        self.job_stages: list[dict[str, Any]] = []
        self._keyword_by_normalized: dict[str, int] = {}
        self._paper_id = 1
        self._paragraph_id = 1
        self._keyword_id = 1
        self._table_id = 1

    def save_paper(
        self,
        meta: PaperMeta,
        source_path: str,
        embedding: list[float] | None = None,
    ) -> int:
        paper_id = self._paper_id
        self._paper_id += 1
        self.papers[paper_id] = {
            "paper_id": paper_id,
            "title": meta.title or "Untitled",
            "authors": list(meta.authors),
            "published_year": meta.published_year,
            "journal": meta.journal,
            "abstract": meta.abstract,
            "source_file_path": source_path,
            "embedding": embedding,
            "status": "ingested",
        }
        return paper_id

    def update_paper_embedding(self, paper_id: int, embedding: list[float]) -> None:
        self.papers[paper_id]["embedding"] = embedding

    def save_paragraphs(self, paper_id: int, paragraphs: Sequence[ParagraphRecord]) -> list[int]:
        paragraph_ids: list[int] = []
        for paragraph in paragraphs:
            paragraph_id = self._paragraph_id
            self._paragraph_id += 1
            self.paragraphs[paragraph_id] = {
                "paragraph_id": paragraph_id,
                "paper_id": paper_id,
                "section_name": paragraph.section_name,
                "paragraph_order": paragraph.paragraph_order,
                "original_text": paragraph.original_text,
                "cleaned_text": paragraph.cleaned_text,
                "summary": paragraph.summary,
                "is_topic_relevant": paragraph.is_topic_relevant,
                "embedding": paragraph.embedding,
            }
            paragraph_ids.append(paragraph_id)
        return paragraph_ids

    def upsert_keyword(
        self,
        normalized: str,
        display: str,
        embedding: list[float] | None = None,
    ) -> int:
        if normalized in self._keyword_by_normalized:
            keyword_id = self._keyword_by_normalized[normalized]
            keyword = self.keywords[keyword_id]
            keyword["frequency"] += 1
            keyword["display_form"] = display
            if embedding is not None:
                keyword["embedding"] = embedding
            return keyword_id

        keyword_id = self._keyword_id
        self._keyword_id += 1
        self._keyword_by_normalized[normalized] = keyword_id
        self.keywords[keyword_id] = {
            "keyword_id": keyword_id,
            "keyword": normalized,
            "display_form": display,
            "frequency": 1,
            "embedding": embedding,
        }
        return keyword_id

    def link_paper_keyword(self, paper_id: int, keyword_id: int, score: float) -> None:
        self.paper_keywords = [
            row
            for row in self.paper_keywords
            if not (row["paper_id"] == paper_id and row["keyword_id"] == keyword_id)
        ]
        self.paper_keywords.append(
            {"paper_id": paper_id, "keyword_id": keyword_id, "score": score}
        )

    def save_table(
        self,
        paper_id: int,
        table: TableDraft,
        summary: str,
        embedding: list[float] | None = None,
    ) -> int:
        table_id = self._table_id
        self._table_id += 1
        self.tables[table_id] = {
            "table_id": table_id,
            "paper_id": paper_id,
            "table_title": table.table_title,
            "table_text": table.table_text,
            "table_summary": summary,
            "embedding": embedding,
        }
        return table_id

    def save_relations(self, paper_id: int, relations: Sequence[tuple[int, float, str]]) -> None:
        self.relations = [
            row for row in self.relations if row["source_paper_id"] != paper_id
        ]
        for related_paper_id, score, reason in relations:
            self.relations.append(
                {
                    "source_paper_id": paper_id,
                    "related_paper_id": related_paper_id,
                    "relation_score": score,
                    "relation_reason": reason,
                }
            )

    def set_job_stage(
        self,
        paper_id: int | None,
        stage: str,
        status: str,
        error: str | None = None,
    ) -> None:
        self.job_stages.append(
            {
                "paper_id": paper_id,
                "stage": stage,
                "status": status,
                "error": error,
                "timestamp": datetime.now(UTC),
            }
        )

    def list_relation_candidates(self, paper_id: int) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for candidate_id, paper in self.papers.items():
            if candidate_id == paper_id or paper.get("embedding") is None:
                continue
            keyword_ids = [
                row["keyword_id"] for row in self.paper_keywords if row["paper_id"] == candidate_id
            ]
            keywords = {self.keywords[keyword_id]["keyword"] for keyword_id in keyword_ids}
            candidates.append(
                {
                    "paper_id": candidate_id,
                    "published_year": paper.get("published_year"),
                    "embedding": paper.get("embedding") or [],
                    "keywords": keywords,
                }
            )
        return candidates


def _vector_literal(vector: Sequence[float] | None) -> str | None:
    if vector is None:
        return None
    return "[" + ",".join(f"{float(value):.9g}" for value in vector) + "]"


def _parse_vector(value: Any) -> list[float]:
    if value is None:
        return []
    if isinstance(value, list):
        return [float(item) for item in value]
    text_value = str(value).strip().strip("[]")
    if not text_value:
        return []
    return [float(item) for item in text_value.split(",")]
