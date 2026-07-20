"""수집 파이프라인 저장소 — papers/paragraphs/keywords/paper_tables/paper_relations/
processing_jobs 테이블에 대한 실제(PostgresIngestRepository) 및 테스트용
(InMemoryIngestRepository) 구현.

각 메서드는 `engine.begin()`으로 커넥션당 하나의 트랜잭션을 열고 그 안에서만
INSERT/UPDATE를 수행한다(자동 커밋/롤백). `pipeline.py`의 STEP 4~8 실패 시
보상 삭제(compensating delete)는 `delete_paper`가 담당하며, 현재는 papers
행 삭제 시 DB의 FK ON DELETE CASCADE에 의존해 종속 데이터가 함께 지워지는
구조다(전역 키워드 frequency까지 원복하는 단일 트랜잭션은 아직 미구현 —
docs/reports/assessments/2026-07-12-two-paper-ocr-evaluation.md의 남은 조치 참고).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from paperrag.config import Settings, get_settings
from paperrag.db import get_engine
from paperrag.ingest.models import PaperMeta, TableDraft


@dataclass(frozen=True)
class ParagraphRecord:
    """STEP 7에서 저장소에 전달하는 단락 저장 단위(원문+정제 결과+임베딩)."""

    section_name: str
    paragraph_order: int
    original_text: str
    cleaned_text: str
    summary: str
    is_topic_relevant: bool
    keywords: list[str] | None = None
    embedding: list[float] | None = None


class IngestRepository(Protocol):
    """수집 파이프라인이 필요로 하는 저장소 인터페이스.

    `PostgresIngestRepository`(운영, PostgreSQL+pgvector)와
    `InMemoryIngestRepository`(dry-run/테스트, DB 없이 메모리 dict)가 이 프로토콜을
    구현하며, `pipeline.IngestPipeline`은 구체 타입에 의존하지 않고 이 인터페이스만
    사용한다. `list_relation_candidates`/`update_paper_embedding`/
    `update_paper_enrichment`는 STEP 7/8에서만 쓰이는 선택적 메서드라 여기 프로토콜에는
    없고 `getattr(..., None)`로 존재 여부를 확인해 호출한다(`pipeline.py` 참고).
    """

    def save_paper(
        self,
        meta: PaperMeta,
        source_path: str,
        embedding: list[float] | None = None,
    ) -> int:
        """Persist paper metadata and return paper_id."""

    def delete_paper(self, paper_id: int) -> None:
        """실패한 적재 실행에서 생성한 논문과 종속 데이터를 제거한다."""

    def save_paragraphs(self, paper_id: int, paragraphs: Sequence[ParagraphRecord]) -> list[int]:
        """Persist paragraphs and return paragraph_ids."""

    def update_paper_enrichment(self, paper_id: int, abstract_summary: str) -> None:
        """Persist paper-level LLM enrichment."""

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
    """PostgreSQL(+pgvector)에 실제로 적재하는 운영용 IngestRepository 구현.

    모든 메서드는 `self.engine.begin()`으로 트랜잭션 하나를 열고 그 블록을 벗어나면
    자동으로 커밋(예외 시 롤백)한다 — 메서드 1회 호출 = 트랜잭션 1개 단위이며,
    STEP 하나 안에서 여러 메서드를 호출하면(예: STEP 7의 단락 저장 + 키워드
    upsert + 표 저장) 각각 별도 트랜잭션으로 커밋된다는 뜻이다. 벡터 컬럼은
    `CAST(:param AS vector)`로 pgvector 타입에 맞춰 넣는다.
    """

    def __init__(self, settings: Settings | None = None, engine: Engine | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or get_engine(self.settings)

    def save_paper(
        self,
        meta: PaperMeta,
        source_path: str,
        embedding: list[float] | None = None,
    ) -> int:
        """STEP 3: papers 행을 status='ingested'로 새로 만들고 paper_id를 반환한다.

        이 시점에는 아직 임베딩이 없을 수 있어(embedding=None 허용) 이후 STEP 7의
        `update_paper_embedding`이 같은 행을 UPDATE한다.
        """
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

    def delete_paper(self, paper_id: int) -> None:
        """실패한 적재 실행의 보상 삭제(compensating delete): papers 행 1개만 지운다.

        paragraphs/paper_keywords/paper_tables/paper_relations은 DB 마이그레이션에서
        papers(paper_id)를 ON DELETE CASCADE로 참조하므로 이 DELETE 한 줄로 함께
        삭제된다(db/migrations/0001_init.sql). 다만 STEP 6에서 이미 증가시킨
        keywords.frequency나 keyword_aliases 병합은 되돌리지 않는다 — 전역 키워드
        통계까지 포함하는 완전한 보상 트랜잭션은 아직 미구현이다
        (2026-07-12 실측 문서의 남은 조치 항목).
        """
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM papers WHERE paper_id = :paper_id"),
                {"paper_id": paper_id},
            )

    def update_paper_embedding(self, paper_id: int, embedding: list[float]) -> None:
        """STEP 7: papers.paper_embedding을 채우고 status를 다시 'ingested'로 확정한다."""
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

    def update_paper_enrichment(self, paper_id: int, abstract_summary: str) -> None:
        """STEP 7: LLM이 생성한 초록 요약(abstract_summary)을 papers 행에 반영한다."""
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE papers SET abstract_summary = :summary WHERE paper_id = :paper_id"),
                {"paper_id": paper_id, "summary": abstract_summary},
            )

    def save_paragraphs(self, paper_id: int, paragraphs: Sequence[ParagraphRecord]) -> list[int]:
        """STEP 7: 단락들을 paragraphs 테이블에 한 트랜잭션으로 순서대로 INSERT한다."""
        statement = text(
            """
            INSERT INTO paragraphs (
                paper_id, section_name, paragraph_order, original_text,
                cleaned_text, summary, keywords, is_topic_relevant, embedding
            )
            VALUES (
                :paper_id, :section_name, :paragraph_order, :original_text,
                :cleaned_text, :summary, :keywords, :is_topic_relevant, CAST(:embedding AS vector)
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
                        "keywords": paragraph.keywords or [],
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
        """STEP 6: 정규화 키워드를 upsert하고 필요하면 동의어(alias)로 기존 키워드에 병합한다.

        판정 순서(모두 한 트랜잭션 안에서 수행):
        ① 정규화 키워드(keywords.keyword) 또는 이미 등록된 별칭(keyword_aliases.alias)과
           정확히 같은 값이 있으면 그 키워드의 frequency만 +1 하고 재사용한다(정확 매칭).
        ② 정확히 같은 값이 없으면 임베딩 코사인 유사도가 가장 높은 기존 키워드를 찾고,
           그 유사도가 `Settings.keyword_alias_similarity_threshold`(설계상 0.95,
           DESIGN.md §3 STEP 6) 이상이면 표기만 다른 동의어로 보고 keyword_aliases에
           등록한 뒤 그 키워드의 frequency를 +1 한다 — 완전히 새 키워드로 중복
           생성하지 않기 위한 근접 매칭 병합이다.
        ③ 그마저도 없으면 완전히 새로운 keywords 행을 생성한다.
        """
        vector_literal = _vector_literal(embedding)
        with self.engine.begin() as connection:
            existing = connection.execute(
                text(
                    """
                    SELECT k.keyword_id
                    FROM keywords k
                    LEFT JOIN keyword_aliases ka ON ka.keyword_id = k.keyword_id
                    WHERE k.keyword = :keyword OR ka.alias = :keyword
                    LIMIT 1
                    """
                ),
                {"keyword": normalized},
            ).scalar_one_or_none()
            if existing is not None:
                connection.execute(
                    text(
                        """
                        UPDATE keywords
                        SET frequency = frequency + 1,
                            embedding = COALESCE(CAST(:embedding AS vector), embedding)
                        WHERE keyword_id = :keyword_id
                        """
                    ),
                    {"keyword_id": existing, "embedding": vector_literal},
                )
                return int(existing)

            nearest = None
            if vector_literal is not None:
                nearest = connection.execute(
                    text(
                        """
                        SELECT keyword_id,
                               1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                        FROM keywords
                        WHERE embedding IS NOT NULL
                        ORDER BY embedding <=> CAST(:embedding AS vector)
                        LIMIT 1
                        """
                    ),
                    {"embedding": vector_literal},
                ).mappings().first()
            # 코사인 유사도가 임계값(기본 0.95) 이상인 기존 키워드가 있으면 완전히
            # 새 키워드를 만들지 않고 그 키워드의 동의어(alias)로 등록한다.
            if nearest is not None and float(nearest["similarity"]) >= (
                self.settings.keyword_alias_similarity_threshold
            ):
                keyword_id = int(nearest["keyword_id"])
                connection.execute(
                    text(
                        """
                        INSERT INTO keyword_aliases (alias, keyword_id)
                        VALUES (:alias, :keyword_id)
                        ON CONFLICT (alias) DO UPDATE SET keyword_id = EXCLUDED.keyword_id
                        """
                    ),
                    {"alias": normalized, "keyword_id": keyword_id},
                )
                connection.execute(
                    text("UPDATE keywords SET frequency = frequency + 1 WHERE keyword_id = :id"),
                    {"id": keyword_id},
                )
                return keyword_id

            keyword_id = connection.execute(
                text(
                    """
                    INSERT INTO keywords (keyword, display_form, embedding)
                    VALUES (:keyword, :display_form, CAST(:embedding AS vector))
                    RETURNING keyword_id
                    """
                ),
                {
                    "keyword": normalized,
                    "display_form": display,
                    "embedding": vector_literal,
                },
            ).scalar_one()
        return int(keyword_id)

    def link_paper_keyword(self, paper_id: int, keyword_id: int, score: float) -> None:
        """STEP 6: paper_keywords에 (논문, 키워드, keywords.KeywordScore 점수)를 upsert한다.

        같은 논문-키워드 조합이 이미 있으면(재처리 등) score만 최신값으로 갱신한다.
        """
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
        """STEP 7: 표 원문+요약+임베딩을 paper_tables에 저장한다."""
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
        """STEP 8: relations.build_relations가 계산한 (연관 논문, 점수, 사유) 목록을 paper_relations에 upsert한다."""
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
        """모든 STEP 실행 전후로 processing_jobs에 상태 로그 행을 남긴다(running/done/failed).

        paper_id는 STAGE_1(source check)처럼 아직 papers 행이 없는 시점에는 None일
        수 있다. started_at/finished_at은 status에 따라 하나만 채워, 단계별 소요
        시간과 실패 stage를 나중에 SQL로 조회할 수 있게 한다
        (docs/guide/04-ingest-pipeline.md 5단계).
        """
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
        """STEP 8: 신규 논문(paper_id)을 제외한, 임베딩이 이미 있는 모든 논문을 연관도 계산 후보로 가져온다.

        논문별 키워드는 paper_keywords/keywords를 조인·집계(array_agg)해 한 번에
        가져와 relations.build_relations가 자카드 유사도를 계산할 수 있게 한다.
        """
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

    def delete_paper(self, paper_id: int) -> None:
        self.papers.pop(paper_id, None)
        self.paragraphs = {
            row_id: row
            for row_id, row in self.paragraphs.items()
            if row["paper_id"] != paper_id
        }
        self.paper_keywords = [
            row for row in self.paper_keywords if row["paper_id"] != paper_id
        ]
        self.tables = {
            row_id: row
            for row_id, row in self.tables.items()
            if row["paper_id"] != paper_id
        }
        self.relations = [
            row
            for row in self.relations
            if row["source_paper_id"] != paper_id and row["related_paper_id"] != paper_id
        ]

    def update_paper_embedding(self, paper_id: int, embedding: list[float]) -> None:
        self.papers[paper_id]["embedding"] = embedding

    def update_paper_enrichment(self, paper_id: int, abstract_summary: str) -> None:
        self.papers[paper_id]["abstract_summary"] = abstract_summary

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
                "keywords": list(paragraph.keywords or []),
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
