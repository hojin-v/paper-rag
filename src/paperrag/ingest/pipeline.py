import re
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from paperrag.config import Settings, get_settings
from paperrag.ingest.embeddings import EmbeddingClient
from paperrag.ingest.filterer import split_blocks
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

STAGE_1 = "step1_source_check"
STAGE_2 = "step2_layout"
STAGE_3 = "step3_filter"
STAGE_4 = "step4_paragraph"
STAGE_5 = "step5_llm_enrich"
STAGE_6 = "step6_keywords"
STAGE_7 = "step7_embed"
STAGE_8 = "step8_relate"


class IngestPipeline:
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
        path = str(Path(pdf_path))
        report = IngestReport(source_path=path)
        paper_id: int | None = None

        def stage(name: str, action: Callable[[], tuple[object, int]]) -> object:
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
            nonlocal paper_id
            filtered_payload, filtered_count = self._filter_blocks(layout.blocks, path)
            meta_for_save, _, _ = filtered_payload
            paper_id = self.repo.save_paper(meta_for_save, path)
            report.paper_id = paper_id
            return filtered_payload, filtered_count

        filtered = stage(STAGE_3, filter_and_save)
        meta, body_blocks, table_blocks = filtered
        report.stages[STAGE_3].count = len(body_blocks) + len(table_blocks)

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
            paper_embedding, normalized_keywords = persisted_payload

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
            return report
        except Exception as error:
            if paper_id is not None:
                try:
                    self.repo.delete_paper(paper_id)
                    report.paper_id = None
                except Exception as cleanup_error:
                    error.add_note(f"실패 논문 보상 삭제도 실패했습니다: {cleanup_error}")
            raise

    @staticmethod
    def _validate_pdf_source(path: str) -> str:
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
    ) -> tuple[tuple[list[float], set[str]], int]:
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

        for (normalized, display, score), vector in zip(
            keyword_entries,
            keyword_vectors,
            strict=True,
        ):
            keyword_id = self.repo.upsert_keyword(normalized, display, vector)
            self.repo.link_paper_keyword(paper_id, keyword_id, score)

        for table, summary, vector in zip(tables, table_summaries, table_vectors, strict=True):
            self.repo.save_table(paper_id, table, summary, vector)

        saved_count = len(paragraph_records) + len(keyword_entries) + len(tables) + 1
        return (paper_embedding, {normalized for normalized, _, _ in keyword_entries}), saved_count

    def _build_and_save_relations(
        self,
        paper_id: int,
        meta: PaperMeta,
        paper_embedding: list[float],
        normalized_keywords: set[str],
    ) -> tuple[list[tuple[int, float, str]], int]:
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


def _extract_meta(
    meta_blocks: dict[str, list[LayoutBlock]],
    all_blocks: Sequence[LayoutBlock],
    source_path: str,
) -> PaperMeta:
    title = _join_block_text(meta_blocks.get("title", [])) or Path(source_path).stem
    authors_text = _join_block_text(meta_blocks.get("author", []))
    abstract = _join_block_text(meta_blocks.get("abstract", []))
    context = "\n".join(block.text for block in sorted(all_blocks, key=lambda item: item.order)[:20])
    return PaperMeta(
        title=title.strip(),
        authors=_split_authors(authors_text),
        published_year=_extract_year(" ".join([title, abstract, context])),
        journal=None,
        abstract=abstract.strip(),
    )


def _join_block_text(blocks: Sequence[LayoutBlock]) -> str:
    return "\n".join(block.text.strip() for block in sorted(blocks, key=lambda item: item.order) if block.text.strip())


def _split_authors(text: str) -> list[str]:
    if not text.strip():
        return []
    return [
        author.strip()
        for author in re.split(r"[,;\n]+", text)
        if author.strip()
    ]


def _extract_year(text: str) -> int | None:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return int(match.group(1)) if match else None


def _build_tables(table_blocks: Sequence[LayoutBlock]) -> list[TableDraft]:
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
