"""전체 STEP 1~8 오케스트레이션 — 논문 PDF 1편을 받아 DB 적재까지 실행한다.

IngestPipeline.run()이 source check(1) -> layout(2) -> filter(3) -> paragraph(4)
-> llm_enrich(5) -> keywords(6) -> embed(7) -> relate(8) 순서로 각 단계를
호출하고, 단계마다 processing_jobs에 running/done/failed 상태를 기록한다
(DESIGN.md §3, §4, docs/guide/04-ingest-pipeline.md 7단계). STEP 3 이후
어느 단계에서든 예외가 나면 이미 생성된 papers 행과 종속 데이터를 보상 삭제해
실패한 논문이 DB에 반쪽짜리로 남지 않게 한다(2026-07-12 실측에서 확인된
운영 공백을 메운 조치, docs/reports/assessments/2026-07-12-two-paper-ocr-evaluation.md).
"""

import re
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from paperrag.config import Settings, get_settings
from paperrag.ingest.embeddings import EmbeddingClient
from paperrag.ingest.filterer import AUTHOR_KEYWORDS_LABEL_RE, split_blocks
from paperrag.ingest.keywords import KeywordScore, normalize
from paperrag.ingest.layout.base import LayoutBackend
from paperrag.ingest.llm_enrich import (
    LLMClient,
    PassthroughEnricher,
    enrich_paragraph,
    extract_paper_keywords,
    summarize_abstract,
    summarize_table,
)
from paperrag.ingest.models import (
    EnrichedParagraph,
    IngestReport,
    LayoutBlock,
    PaperMeta,
    ParagraphDraft,
    TableDraft,
)
from paperrag.ingest.paragraphs import build_paragraphs
from paperrag.ingest.relations import build_relations
from paperrag.ingest.repository import IngestRepository, ParagraphRecord

# processing_jobs.stage에 기록되는 단계 이름. docs/guide/04-ingest-pipeline.md
# 7단계 표의 STEP 이름과 그대로 맞춘다.
STAGE_1 = "step1_source_check"
STAGE_2 = "step2_layout"
STAGE_3 = "step3_filter"
STAGE_4 = "step4_paragraph"
STAGE_5 = "step5_llm_enrich"
STAGE_6 = "step6_keywords"
STAGE_7 = "step7_embed"
STAGE_8 = "step8_relate"
STAGE_9 = "step9_precompute_cache"
# 초록 앞에 붙는 "Abstract:"/"초록" 같은 절 제목을 본문에서 떼어내기 위한 패턴.
ABSTRACT_HEADING_RE = re.compile(
    r"^\s*(?:abstract|초록|요약)\s*(?:[:：.\-–—]\s*)?(?:\r?\n|\s+)",
    re.IGNORECASE,
)
# 저자 블록 안에서 "소속(대학/연구소/이메일 등)"이 시작되는 지점을 찾아 그 위쪽만
# 저자명으로 인정하기 위한 패턴.
AFFILIATION_RE = re.compile(
    r"(?:@|\b(?:university|institute|laborator(?:y|ies)|research|department|"
    r"school|college|centre|center|corporation|company|\bco\.|\bltd\.?|\binc\.?)\b)",
    re.IGNORECASE,
)
# 저자명 뒤에 붙는 각주 기호(*, †, ‡)나 소속 번호(위첨자 숫자)를 제거하기 위한 패턴.
AUTHOR_MARKER_RE = re.compile(
    r"\s*[*∗†‡]\s*\d*(?:\s*,\s*\d+)*"
    r"|\d+\s*[*∗†‡](?=\s*(?:,|$))"
    r"|(?<=[A-Za-z])\d+(?=\s*(?:,|$))"
)


class IngestPipeline:
    """논문 PDF 1편에 대해 STEP 1~8을 순서대로 실행하는 오케스트레이터.

    repo(저장소)·layout_backend(레이아웃+OCR)·llm(정제/요약/키워드)·embedder(임베딩)
    네 가지 협력 객체를 주입받아 실제 I/O는 위임하고, 이 클래스는 단계 순서·
    실패 처리·리포트 기록만 책임진다. `cli.py`가 dry-run/운영 여부에 따라 다른
    구현체(InMemoryIngestRepository vs PostgresIngestRepository 등)를 주입한다.
    """

    def __init__(
        self,
        repo: IngestRepository,
        layout_backend: LayoutBackend,
        llm: LLMClient | PassthroughEnricher,
        embedder: EmbeddingClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.repo = repo
        self.layout_backend = layout_backend
        self.llm = llm
        self.embedder = embedder
        self.settings = settings or get_settings()

    def run(self, pdf_path: str) -> IngestReport:
        """PDF 한 편을 STEP 1~8 순서로 처리하고 IngestReport를 반환한다.

        내부 `stage()` 헬퍼가 각 단계 실행 전후로 processing_jobs에
        running/done/failed를 기록하고, 실패 시 예외를 다시 던져 파이프라인을
        중단시킨다. STEP 3에서 paper_id가 생성된 이후 어느 단계에서 실패하든
        바깥쪽 try/except가 해당 paper_id와 종속 데이터를 보상 삭제해, 실패한
        논문이 DB에 반쪽 상태로 남지 않게 한다(현재는 부분 삭제 수준이며,
        stage 7에서 이미 갱신된 전역 키워드 frequency까지 되돌리는 완전한
        단일 트랜잭션은 아직 미구현 — 2026-07-12 실측 문서의 남은 조치 항목).
        """
        path = str(Path(pdf_path))
        report = IngestReport(source_path=path)
        paper_id: int | None = None

        def stage(name: str, action: Callable[[], tuple[object, int]]) -> object:
            # 단계 실행 전 running, 성공 시 done, 예외 시 failed를 processing_jobs에
            # 기록한 뒤 예외를 다시 던져 run() 바깥의 보상 삭제 로직으로 넘긴다.
            nonlocal paper_id
            self.repo.set_job_stage(paper_id, name, "running")
            try:
                result, count = action()
            except Exception as exc:
                self.repo.set_job_stage(paper_id, name, "failed", str(exc))
                report.record_stage(name, success=False, error=str(exc))
                raise
            self.repo.set_job_stage(paper_id, name, "done")
            report.record_stage(name, success=True, count=count)
            return result

        stage(STAGE_1, lambda: (self._validate_pdf_source(path), 1))

        layout = stage(STAGE_2, lambda: (self.layout_backend.analyze(path), 0))
        stage_count = len(layout.blocks)
        report.stages[STAGE_2].count = stage_count
        report.is_scanned = layout.is_scanned

        def filter_and_save() -> tuple[tuple[PaperMeta, list[LayoutBlock], list[LayoutBlock]], int]:
            # STEP 3 필터링과 동시에 papers 행을 만든다 — paper_id가 있어야 STEP 4
            # 이후 실패 시 보상 삭제 대상(nonlocal paper_id)을 특정할 수 있기 때문이다.
            nonlocal paper_id
            filtered_payload, filtered_count = self._filter_blocks(layout.blocks, path)
            meta_for_save, _, _ = filtered_payload
            paper_id = self.repo.save_paper(meta_for_save, path)
            report.paper_id = paper_id
            return filtered_payload, filtered_count

        filtered = stage(STAGE_3, filter_and_save)
        meta, body_blocks, table_blocks = filtered
        report.stages[STAGE_3].count = len(body_blocks) + len(table_blocks)

        # STAGE_4~8은 paper_id가 이미 만들어진 뒤이므로, 이 구간에서 실패하면
        # 아래 except에서 papers와 종속 데이터를 보상 삭제해 반쪽짜리 논문이
        # 남지 않게 한다.
        try:
            paragraphs = stage(
                STAGE_4,
                lambda: (
                    build_paragraphs(
                        body_blocks,
                        min_chars=self.settings.paragraph_min_chars,
                        max_chars=self.settings.paragraph_max_chars,
                    ),
                    0,
                ),
            )
            report.stages[STAGE_4].count = len(paragraphs)

            enriched_payload = stage(
                STAGE_5,
                lambda: self._enrich(paragraphs, table_blocks, meta),
            )
            enriched_paragraphs, tables, table_summaries, paper_keywords, abstract_summary = (
                enriched_payload
            )

            keyword_entries = stage(
                STAGE_6,
                lambda: self._score_keywords(meta, enriched_paragraphs, paper_keywords),
            )

            persisted_payload = stage(
                STAGE_7,
                lambda: self._embed_and_persist(
                    paper_id,
                    meta,
                    paragraphs,
                    enriched_paragraphs,
                    keyword_entries,
                    tables,
                    table_summaries,
                    abstract_summary,
                ),
            )
            paper_embedding, normalized_keywords, linked_keyword_ids = persisted_payload

            relations = stage(
                STAGE_8,
                lambda: self._build_and_save_relations(
                    paper_id,
                    meta,
                    paper_embedding,
                    normalized_keywords,
                ),
            )

            report.set_total("paragraphs", len(paragraphs))
            report.set_total("keywords", len(keyword_entries))
            report.set_total("tables", len(tables))
            report.set_total("relations", len(relations))
        except Exception as error:
            # 보상 삭제(compensating delete): STEP 4~8 중 실패하면 이미 저장된
            # paper_id 행과 그 종속 데이터(단락/키워드 연결/표/관계)를 지워
            # 실패한 논문이 "ingested" 상태의 반쪽 데이터로 남지 않게 한다.
            # 삭제 자체가 실패해도 원래 예외를 삼키지 않고 note로 덧붙여 재발생시킨다.
            if paper_id is not None:
                try:
                    self.repo.delete_paper(paper_id)
                    report.paper_id = None
                except Exception as cleanup_error:
                    error.add_note(f"실패 논문 보상 삭제도 실패했습니다: {cleanup_error}")
            raise

        # STEP 9(선택, 최선 노력): 이번에 새로 연결된 키워드들의 검색 결과 캐시를
        # 미리 계산해 둔다. 이 블록은 의도적으로 위 try/except 바깥에 있다 —
        # 여기서 실패해도 이미 STEP 1~8이 성공적으로 끝나 온전히 저장된 논문을
        # 보상 삭제할 이유가 없기 때문이다(캐시 예열 실패는 다음 검색이 조금
        # 느려질 뿐 데이터 정합성 문제가 아니다).
        report.totals["cache_warmed_keywords"] = self._precompute_search_cache(
            paper_id, linked_keyword_ids
        )
        return report

    @staticmethod
    def _validate_pdf_source(path: str) -> str:
        """STEP 1 source check: 확장자·존재 여부·PDF 매직 바이트(%PDF-)를 검증한다.

        운영 정책상 모든 PDF는 전체 페이지 이미지 OCR("full_ocr")로 처리하므로
        이 함수는 digital/scanned를 구분하지 않고 항상 "full_ocr"을 반환한다
        (triage.classify_pdf의 판정은 여기서 쓰이지 않는다).
        """
        source = Path(path)
        if source.suffix.lower() != ".pdf":
            raise ValueError("입력 파일 확장자는 .pdf여야 합니다.")
        if not source.is_file():
            raise FileNotFoundError(path)
        with source.open("rb") as file:
            if file.read(5) != b"%PDF-":
                raise ValueError("PDF 시그니처가 없는 파일입니다.")
        return "full_ocr"

    def _filter_blocks(
        self,
        blocks: Sequence[LayoutBlock],
        source_path: str,
    ) -> tuple[tuple[PaperMeta, list[LayoutBlock], list[LayoutBlock]], int]:
        """STEP 3: filterer.split_blocks로 블록을 분류하고 메타데이터를 추출한다."""
        meta_blocks, body_blocks, table_blocks = split_blocks(
            blocks,
            settings=self.settings,
        )
        meta = _extract_meta(meta_blocks, blocks, source_path)
        return (meta, body_blocks, table_blocks), len(body_blocks) + len(table_blocks)

    def _enrich(
        self,
        paragraphs: Sequence[ParagraphDraft],
        table_blocks: Sequence[LayoutBlock],
        meta: PaperMeta,
    ) -> tuple[
        tuple[list[EnrichedParagraph], list[TableDraft], list[str], list[str], str],
        int,
    ]:
        # STEP 5: 단락별 LLM 정제(1건당 1회 호출), 논문 대표 키워드, 표 요약,
        # 초록 요약을 한 번에 생성한다. LLM 호출 실패는 llm_enrich 내부의
        # 재시도/폴백에서 처리되므로 여기서는 결과만 모은다.
        enriched = [enrich_paragraph(self.llm, paragraph.original_text) for paragraph in paragraphs]
        summaries = [paragraph.summary for paragraph in enriched]
        paper_keywords = extract_paper_keywords(self.llm, meta.title, meta.abstract, summaries)
        tables = _build_tables(table_blocks)
        table_summaries = [summarize_table(self.llm, table.table_text) for table in tables]
        abstract_summary = summarize_abstract(self.llm, meta.abstract)
        return (
            enriched,
            tables,
            table_summaries,
            paper_keywords,
            abstract_summary,
        ), len(enriched) + len(tables)

    def _score_keywords(
        self,
        meta: PaperMeta,
        enriched_paragraphs: Sequence[EnrichedParagraph],
        paper_keywords: Sequence[str],
    ) -> tuple[list[tuple[str, str, float]], int]:
        """STEP 6: 논문 대표 키워드 후보를 정규화하고 keywords.py의 점수 공식으로 순위를 매긴다.

        단락별 키워드(body_keywords)는 이 논문 안에서의 등장 빈도 집계용으로만
        쓰고, 실제로 저장·점수화하는 키워드 표시형(display)은 STEP 5에서 뽑은
        논문 대표 키워드(paper_keywords)를 기준으로 한다 — 2026-07-12 실측에서
        지적된 "모든 단락 키워드를 대표 키워드로 승격하지 않는다"는 정책을 그대로
        반영한 것이다. 다만 저자가 직접 지정한 키워드(meta.author_keywords,
        "Keywords:"/"CCS Concepts:" 블록에서 뽑음)는 LLM이 title/abstract/요약만
        보고 독립적으로 제안하지 않았어도 후보로 강제 포함시킨다 — 저자 스스로 고른
        용어는 그 자체로 신뢰할 만한 신호이기 때문이다(KeywordScore.author_weight).
        """
        body_keywords = [
            keyword
            for paragraph in enriched_paragraphs
            for keyword in paragraph.keywords
            if keyword.strip()
        ]
        body_counter: Counter[str] = Counter()

        for keyword in body_keywords:
            normalized = normalize(keyword)
            if not normalized:
                continue
            body_counter[normalized] += 1

        displays_by_normalized: dict[str, str] = {}
        for keyword in paper_keywords:
            normalized = normalize(keyword)
            if normalized:
                displays_by_normalized.setdefault(normalized, keyword.strip())
        author_normalized: set[str] = set()
        for keyword in meta.author_keywords:
            normalized = normalize(keyword)
            if normalized:
                displays_by_normalized.setdefault(normalized, keyword.strip())
                author_normalized.add(normalized)

        max_body_frequency = max(body_counter.values(), default=0)
        scorer = KeywordScore()
        entries = [
            (
                normalized,
                display,
                scorer.compute(
                    display,
                    title=meta.title,
                    abstract=meta.abstract,
                    body_frequency=body_counter.get(normalized, 0),
                    max_body_frequency=max_body_frequency,
                    is_author_keyword=normalized in author_normalized,
                ),
            )
            for normalized, display in displays_by_normalized.items()
        ]
        entries.sort(key=lambda item: item[2], reverse=True)
        return entries, len(entries)

    def _embed_and_persist(
        self,
        paper_id: int,
        meta: PaperMeta,
        paragraphs: Sequence[ParagraphDraft],
        enriched_paragraphs: Sequence[EnrichedParagraph],
        keyword_entries: Sequence[tuple[str, str, float]],
        tables: Sequence[TableDraft],
        table_summaries: Sequence[str],
        abstract_summary: str,
    ) -> tuple[tuple[list[float], set[str], list[int]], int]:
        """STEP 7: 단락/키워드/표/논문 텍스트를 임베딩하고 전부 DB에 저장한다.

        임베딩 대상은 DESIGN.md §3 STEP 7 그대로다: 단락은 cleaned_text(원문이
        아니라 LLM 정제 결과), 키워드는 표시형(display), 표는 제목+요약, 논문은
        제목+초록+대표 키워드를 이어붙인 텍스트. 논문 임베딩/요약은 STEP 3에서
        이미 만든 papers 행을 UPDATE(update_paper_embedding/update_paper_enrichment)
        하고, 단락·키워드·표는 새로 INSERT한다. 반환값의 세 번째 요소(linked_keyword_ids)는
        이번에 upsert_keyword가 돌려준 keyword_id 목록으로, STEP 9가 "이번 논문이
        새로 연결된 키워드"만 골라 캐시를 다시 계산하는 데 그대로 쓰인다.
        """
        paragraph_vectors = self.embedder.embed(
            [paragraph.cleaned_text for paragraph in enriched_paragraphs]
        )
        keyword_vectors = self.embedder.embed([display for _, display, _ in keyword_entries])
        table_vectors = self.embedder.embed(
            [
                "\n".join(filter(None, [table.table_title or "", summary]))
                for table, summary in zip(tables, table_summaries, strict=True)
            ]
        )
        paper_text = "\n".join(
            [
                meta.title,
                meta.abstract,
                ", ".join(display for _, display, _ in keyword_entries),
            ]
        )
        paper_embedding = self.embedder.embed([paper_text])[0]

        update_embedding = getattr(self.repo, "update_paper_embedding", None)
        if callable(update_embedding):
            update_embedding(paper_id, paper_embedding)
        update_enrichment = getattr(self.repo, "update_paper_enrichment", None)
        if callable(update_enrichment):
            update_enrichment(paper_id, abstract_summary)

        paragraph_records = [
            ParagraphRecord(
                section_name=draft.section_name,
                paragraph_order=draft.paragraph_order,
                original_text=draft.original_text,
                cleaned_text=enriched.cleaned_text,
                summary=enriched.summary,
                keywords=list(enriched.keywords),
                is_topic_relevant=enriched.is_topic_relevant,
                embedding=vector,
            )
            for draft, enriched, vector in zip(
                paragraphs,
                enriched_paragraphs,
                paragraph_vectors,
                strict=True,
            )
        ]
        self.repo.save_paragraphs(paper_id, paragraph_records)

        linked_keyword_ids: list[int] = []
        for (normalized, display, score), vector in zip(
            keyword_entries,
            keyword_vectors,
            strict=True,
        ):
            keyword_id = self.repo.upsert_keyword(normalized, display, vector)
            self.repo.link_paper_keyword(paper_id, keyword_id, score)
            linked_keyword_ids.append(keyword_id)

        for table, summary, vector in zip(tables, table_summaries, table_vectors, strict=True):
            self.repo.save_table(paper_id, table, summary, vector)

        saved_count = len(paragraph_records) + len(keyword_entries) + len(tables) + 1
        return (
            (paper_embedding, {normalized for normalized, _, _ in keyword_entries}, linked_keyword_ids),
            saved_count,
        )

    def _build_and_save_relations(
        self,
        paper_id: int,
        meta: PaperMeta,
        paper_embedding: list[float],
        normalized_keywords: set[str],
    ) -> tuple[list[tuple[int, float, str]], int]:
        """STEP 8: 저장소에서 연관도 계산 후보 논문들을 가져와 relations.build_relations로 점수를 매기고 저장한다.

        저장소가 list_relation_candidates를 지원하지 않으면(프로토콜 선택적 메서드)
        빈 후보 목록으로 동작해 연관 논문 없이도 파이프라인이 실패하지 않게 한다.
        """
        list_candidates = getattr(self.repo, "list_relation_candidates", None)
        candidates = list_candidates(paper_id) if callable(list_candidates) else []
        relations = build_relations(
            {
                "paper_id": paper_id,
                "published_year": meta.published_year,
                "embedding": paper_embedding,
                "keywords": normalized_keywords,
            },
            candidates,
            top_n=self.settings.relation_top_k,
        )
        self.repo.save_relations(paper_id, relations)
        return relations, len(relations)

    def _precompute_search_cache(self, paper_id: int, keyword_ids: Sequence[int]) -> int:
        """STEP 9(선택, 최선 노력): 이번 논문이 새로 연결된 키워드마다 검색 결과
        캐시(keyword_result_cache)를 강제로 다시 계산해 채운다.

        새 논문이 어떤 키워드의 대표/연관 논문 순위를 바꿨을 수 있으므로, 그
        키워드에 한해서만(전체 재계산이 아니라) 갱신한다 — 이 논문과 무관한
        키워드는 데이터가 바뀌지 않았으니 건드릴 필요가 없다. `self.repo`가
        `engine` 속성을 갖지 않으면(InMemoryIngestRepository, 즉 dry-run) 캐시
        테이블 자체가 없는 것과 같으므로 조용히 건너뛴다. 개별 키워드 예열이
        실패해도(임베딩 서버 일시 오류 등) 나머지 키워드는 계속 처리한다 — 이미
        run()의 보상 삭제 대상 밖(try/except 바깥)에서 호출되므로 여기서 예외를
        다시 던지지 않는 것이 원칙과 일치한다.

        알려진 한계: STEP 8은 "이번 논문 기준" 연관 논문만 계산·저장하므로, 이번
        논문이 기존 다른 논문의 새 연관 논문으로 등재되는 경우(양방향 조회이므로
        가능) 그 기존 논문이 대표인 키워드의 캐시는 여기서 갱신되지 않는다.
        (docs/reports/assessments 캐시 설계 논의에서 "간단 버전"으로 남겨둔
        잔여 위험 — 필요성이 확인되면 역방향 연관 논문까지 갱신하는 "꼼꼼한
        버전"으로 확장한다.)
        """
        engine = getattr(self.repo, "engine", None)
        if engine is None or not keyword_ids:
            return 0

        from paperrag.search.repository import PostgresSearchRepository
        from paperrag.search.service import SearchService

        # SearchService 생성자는 llm 인자를 요구하지만, precompute_keyword_cache가
        # 부르는 resolve()는 LLM을 전혀 호출하지 않는다(임베딩만 씀) — 그래서
        # self.llm(PassthroughEnricher일 수도 있음)을 그대로 넘겨도 안전하다.
        search_service = SearchService(
            PostgresSearchRepository(self.settings, engine),
            self.llm,
            self.embedder,
            self.settings,
        )
        self.repo.set_job_stage(paper_id, STAGE_9, "running")
        warmed = 0
        try:
            for keyword_id in keyword_ids:
                try:
                    search_service.precompute_keyword_cache(keyword_id)
                    warmed += 1
                except Exception:
                    continue
        finally:
            self.repo.set_job_stage(
                paper_id, STAGE_9, "done" if warmed == len(keyword_ids) else "failed"
            )
        return warmed


def _extract_meta(
    meta_blocks: dict[str, list[LayoutBlock]],
    all_blocks: Sequence[LayoutBlock],
    source_path: str,
) -> PaperMeta:
    """STEP 3 산출물: title/author/abstract 메타 블록으로 PaperMeta를 조립한다.

    제목이 비어 있으면(레이아웃이 제목을 못 찾은 경우) 소스 파일명을 대신
    사용해 최소한의 식별자를 남긴다. 발행연도는 제목+초록+앞부분 20개 블록
    텍스트에서 4자리 연도 패턴으로 추출한다(별도 메타데이터 소스가 없으므로
    본문에서 추정하는 근사치).
    """
    title = _join_block_text(meta_blocks.get("title", [])) or Path(source_path).stem
    authors = _extract_authors(meta_blocks.get("author", []))
    abstract = _strip_abstract_heading(
        _join_block_text(meta_blocks.get("abstract", []))
    )
    author_keywords = _extract_author_keywords(meta_blocks.get("author_keywords", []))
    context = "\n".join(block.text for block in sorted(all_blocks, key=lambda item: item.order)[:20])
    return PaperMeta(
        title=title.strip(),
        authors=authors,
        published_year=_extract_year(" ".join([title, abstract, context])),
        journal=None,
        abstract=abstract.strip(),
        author_keywords=author_keywords,
    )


def _strip_abstract_heading(text: str) -> str:
    """초록 텍스트 맨 앞의 "Abstract"/"초록" 같은 절 제목 한 번만 제거한다."""
    return ABSTRACT_HEADING_RE.sub("", text, count=1).strip()


def _extract_authors(blocks: Sequence[LayoutBlock]) -> list[str]:
    """저자 블록에서 소속·이메일이 시작되기 전까지의 줄만 저자명으로 모아 분리한다.

    2026-07-12 실측에서 저자 블록이 이름+소속+이메일이 한 블록에 섞여 오는
    사례가 확인되었으므로, 블록을 시각적 순서(_order_author_blocks)로 훑다가
    AFFILIATION_RE에 처음 매치되는 줄을 만나면 그 블록에서 이후 줄은 버리고
    이후 블록 처리도 중단한다.
    """
    author_texts: list[str] = []
    for block in _order_author_blocks(blocks):
        name_lines: list[str] = []
        affiliation_started = False
        for line in block.text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if AFFILIATION_RE.search(stripped):
                affiliation_started = True
                break
            name_lines.append(stripped)
        if name_lines:
            author_texts.append(" ".join(name_lines))
        if affiliation_started:
            break
    cleaned = AUTHOR_MARKER_RE.sub("", ", ".join(author_texts))
    return _split_authors(cleaned)


def _order_author_blocks(blocks: Sequence[LayoutBlock]) -> list[LayoutBlock]:
    """저자 블록을 좌표(bbox) 기준으로 위→아래, 같은 줄이면 왼→오른쪽 시각 순서로 정렬한다.

    OCR/레이아웃의 order 필드는 항상 시각적 배치와 일치하지 않을 수 있어(예:
    여러 열로 나열된 저자명), 세로 중심 좌표가 비슷한(반높이 이내) 블록들을
    같은 "행"으로 묶은 뒤 행 단위로 정렬한다. bbox가 없는 블록은 order로만
    정렬해 뒤에 붙인다.
    """
    positioned = [block for block in blocks if block.bbox is not None]
    unpositioned = [block for block in blocks if block.bbox is None]
    if not positioned:
        return sorted(unpositioned, key=lambda item: item.order)
    rows: list[tuple[float, list[LayoutBlock]]] = []
    for block in sorted(
        positioned,
        key=lambda item: (
            (item.bbox[1] + item.bbox[3]) / 2.0,
            item.bbox[0],
        )
        if item.bbox is not None
        else (float("inf"), float("inf")),
    ):
        if block.bbox is None:
            continue
        center = (block.bbox[1] + block.bbox[3]) / 2.0
        height = max(1.0, block.bbox[3] - block.bbox[1])
        row_index = next(
            (
                index
                for index, (row_center, _) in enumerate(rows)
                if abs(center - row_center) <= height * 0.5
            ),
            None,
        )
        if row_index is None:
            rows.append((center, [block]))
            continue
        row_center, row_blocks = rows[row_index]
        rows[row_index] = (
            (row_center * len(row_blocks) + center) / (len(row_blocks) + 1),
            [*row_blocks, block],
        )
    visual_order = [
        block
        for _, row_blocks in sorted(rows, key=lambda row: row[0])
        for block in sorted(
            row_blocks,
            key=lambda item: item.bbox[0] if item.bbox is not None else float("inf"),
        )
    ]
    return [*visual_order, *sorted(unpositioned, key=lambda item: item.order)]


def _join_block_text(blocks: Sequence[LayoutBlock]) -> str:
    """같은 유형의 블록 여러 개를 order 순서대로 줄바꿈으로 이어붙인다(제목/초록 등)."""
    return "\n".join(block.text.strip() for block in sorted(blocks, key=lambda item: item.order) if block.text.strip())


def _extract_author_keywords(blocks: Sequence[LayoutBlock]) -> list[str]:
    """"Keywords:"/"CCS Concepts:" 라벨이 붙은 머리말/꼬리말 블록에서 저자가 직접
    지정한 키워드 목록을 뽑는다. 라벨을 떼어낸 뒤 쉼표/세미콜론/가운뎃점(•)/줄바꿈
    기준으로 나눠 각 항목을 하나의 후보로 본다 — CCS Concepts처럼 계층형 문구
    ("Information systems → Information retrieval")는 화살표를 포함한 구문 전체를
    항목 하나로 취급한다(세분화하지 않음).
    """
    keywords: list[str] = []
    for block in sorted(blocks, key=lambda item: item.order):
        text = AUTHOR_KEYWORDS_LABEL_RE.sub("", block.text.strip(), count=1).strip()
        for keyword in re.split(r"[,;•\n]+", text):
            cleaned = keyword.strip(" .")
            if cleaned:
                keywords.append(cleaned)
    return keywords


def _split_authors(text: str) -> list[str]:
    """정리된 저자 문자열을 쉼표/세미콜론/줄바꿈 기준으로 저자 목록으로 분리한다."""
    if not text.strip():
        return []
    return [
        author.strip()
        for author in re.split(r"[,;\n]+", text)
        if author.strip()
    ]


def _extract_year(text: str) -> int | None:
    """텍스트에서 19xx/20xx 형태의 첫 4자리 연도를 찾아 발행연도로 추정한다."""
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return int(match.group(1)) if match else None


def _build_tables(table_blocks: Sequence[LayoutBlock]) -> list[TableDraft]:
    """표 블록들을 order 순서로 훑어 직전 table_caption을 table의 제목으로 짝짓는다.

    캡션이 표 바로 앞에 나온다는 레이아웃 관례를 가정한 것으로, 캡션이 없으면
    table_title은 None으로 남는다.
    """
    tables: list[TableDraft] = []
    pending_caption: str | None = None
    for block in sorted(table_blocks, key=lambda item: item.order):
        if block.block_type == "table_caption":
            pending_caption = block.text.strip()
            continue
        if block.block_type == "table":
            tables.append(
                TableDraft(
                    table_title=pending_caption,
                    table_text=block.text.strip(),
                )
            )
            pending_caption = None
    return tables
