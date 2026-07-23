from pathlib import Path

import pytest

from paperrag.config import Settings
from paperrag.ingest.embeddings import FakeEmbeddingClient
from paperrag.ingest.llm_enrich import PassthroughEnricher
from paperrag.ingest.models import DocumentLayout, LayoutBlock, PaperMeta
from paperrag.ingest.pipeline import (
    STAGE_1,
    STAGE_2,
    STAGE_3,
    STAGE_4,
    STAGE_5,
    STAGE_6,
    STAGE_7,
    STAGE_8,
    IngestPipeline,
    _extract_author_keywords,
    _extract_meta,
)
from paperrag.ingest.repository import InMemoryIngestRepository


class FakeLayoutBackend:
    def analyze(self, pdf_path: str) -> DocumentLayout:
        return DocumentLayout(
            source_path=pdf_path,
            is_scanned=False,
            blocks=[
                LayoutBlock(page=1, block_type="title", text="RAG Retrieval Study 2024", order=1),
                LayoutBlock(page=1, block_type="author", text="Kim, Lee", order=2),
                LayoutBlock(
                    page=1,
                    block_type="abstract",
                    text="Abstract\nRAG retrieval improves paper search.",
                    order=3,
                ),
                LayoutBlock(page=1, block_type="section_header", text="Introduction", order=4),
                LayoutBlock(
                    page=1,
                    block_type="text",
                    text=(
                        "RAG retrieval connects paper paragraphs with keywords. "
                        "This offline pipeline stores every paragraph for later search."
                    ),
                    order=5,
                ),
                LayoutBlock(page=1, block_type="table_caption", text="Table 1. Scores", order=6),
                LayoutBlock(page=1, block_type="table", text="metric | value\nf1 | 0.9", order=7),
            ],
        )


class FailingLlmClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate_json(
        self, prompt: str, schema_hint: str, operation: str = ""
    ) -> dict[str, object]:
        raise RuntimeError("injected LLM failure")


def test_pipeline_e2e_with_fake_components(tmp_path: Path) -> None:
    repo = InMemoryIngestRepository()
    embedder = FakeEmbeddingClient()
    candidate_id = repo.save_paper(
        PaperMeta(title="Existing RAG Paper", published_year=2023, abstract="RAG retrieval"),
        "candidate.pdf",
        embedding=embedder.embed(["RAG retrieval candidate"])[0],
    )
    keyword_id = repo.upsert_keyword("rag", "RAG", embedder.embed(["RAG"])[0])
    repo.link_paper_keyword(candidate_id, keyword_id, 1.0)

    pipeline = IngestPipeline(
        repo,
        FakeLayoutBackend(),
        PassthroughEnricher(),
        embedder,
        settings=Settings(_env_file=None, paragraph_min_chars=20, paragraph_max_chars=500),
    )

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    report = pipeline.run(str(pdf_path))

    assert report.paper_id is not None
    assert report.totals["paragraphs"] == 1
    assert report.totals["keywords"] > 0
    assert report.totals["tables"] == 1
    assert report.totals["relations"] == 1
    assert repo.papers[report.paper_id]["title"] == "RAG Retrieval Study 2024"
    assert repo.papers[report.paper_id]["authors"] == ["Kim", "Lee"]
    assert repo.papers[report.paper_id]["abstract"] == (
        "RAG retrieval improves paper search."
    )
    assert len(repo.paragraphs) == 1
    assert len(repo.keywords) > 1
    assert len(repo.paper_keywords) > 1
    new_paper_keywords = [
        row for row in repo.paper_keywords if row["paper_id"] == report.paper_id
    ]
    assert 3 <= len(new_paper_keywords) <= 5
    assert len(repo.tables) == 1
    assert len(repo.relations) == 1
    assert repo.relations[0]["related_paper_id"] == candidate_id


def test_pipeline_wires_journal_and_full_text_link_into_paper(tmp_path: Path) -> None:
    repo = InMemoryIngestRepository()
    pipeline = IngestPipeline(
        repo,
        FakeLayoutBackend(),
        PassthroughEnricher(),
        FakeEmbeddingClient(),
        settings=Settings(_env_file=None, paragraph_min_chars=20, paragraph_max_chars=500),
    )
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-test")

    report = pipeline.run(
        str(pdf_path),
        journal="Example Journal",
        full_text_link="https://papers.example/article",
    )

    assert report.paper_id is not None
    # PDF 레이아웃에는 저널명·링크가 없으므로, run()에 넘긴 값이 그대로 papers 행에 채워져야 한다.
    assert repo.papers[report.paper_id]["journal"] == "Example Journal"
    assert repo.papers[report.paper_id]["full_text_link"] == "https://papers.example/article"

    stages = {row["stage"] for row in repo.job_stages if row["status"] == "done"}
    assert stages == {STAGE_1, STAGE_2, STAGE_3, STAGE_4, STAGE_5, STAGE_6, STAGE_7, STAGE_8}
    assert not [row for row in repo.job_stages if row["status"] == "failed"]


def test_extract_meta_keeps_author_names_before_affiliations() -> None:
    author_blocks = [
        LayoutBlock(
            page=1,
            block_type="author",
            text="Jiapeng\nWang",
            order=1,
            bbox=(158, 106, 239, 119),
        ),
        LayoutBlock(
            page=1,
            block_type="author",
            text="Kai Ding\n∗2,3",
            order=2,
            bbox=(373, 103, 440, 121),
        ),
        LayoutBlock(
            page=1,
            block_type="author",
            text="Lianwen Jin*1,3,4",
            order=3,
            bbox=(260, 103, 352, 121),
        ),
        LayoutBlock(
            page=1,
            block_type="author",
            text="1 South China University of Technology, Guangzhou, China",
            order=4,
            bbox=(156, 119, 442, 135),
        ),
        LayoutBlock(
            page=1,
            block_type="author",
            text="author@example.edu",
            order=5,
        ),
    ]
    abstract = LayoutBlock(
        page=1,
        block_type="abstract",
        text="Abstract\nPaper summary",
        order=6,
    )

    meta = _extract_meta(
        {"title": [], "author": author_blocks, "abstract": [abstract]},
        [*author_blocks, abstract],
        "paper.pdf",
    )

    assert meta.authors == ["Jiapeng Wang", "Lianwen Jin", "Kai Ding"]
    assert meta.abstract == "Paper summary"


def test_extract_author_keywords_strips_label_and_splits_items() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="header_footer",
            text="Keywords: RAG, document layout, OCR",
            order=1,
        ),
        LayoutBlock(
            page=1,
            block_type="header_footer",
            text="CCS Concepts: Information systems -> Information retrieval",
            order=2,
        ),
    ]

    assert _extract_author_keywords(blocks) == [
        "RAG",
        "document layout",
        "OCR",
        "Information systems -> Information retrieval",
    ]


def test_extract_meta_wires_author_keywords_from_meta_blocks() -> None:
    keyword_block = LayoutBlock(
        page=1,
        block_type="header_footer",
        text="Keywords: RAG, document layout",
        order=1,
    )

    meta = _extract_meta(
        {"title": [], "author": [], "abstract": [], "author_keywords": [keyword_block]},
        [keyword_block],
        "paper.pdf",
    )

    assert meta.author_keywords == ["RAG", "document layout"]


def test_score_keywords_includes_author_keywords_even_if_llm_missed_them() -> None:
    meta = PaperMeta(
        title="RAG based paper retrieval",
        abstract="This paper describes a RAG system.",
        author_keywords=["그래프 신경망", "RAG"],
    )

    entries, count = IngestPipeline._score_keywords(
        None, meta, enriched_paragraphs=[], paper_keywords=["RAG"]
    )

    assert count == 2
    scores = {display: score for _, display, score in entries}
    assert scores["그래프 신경망"] == pytest.approx(0.3)
    assert scores["RAG"] == pytest.approx(0.3 + 0.2 + 0.3)


def test_pipeline_compensates_created_paper_after_llm_failure(tmp_path: Path) -> None:
    repo = InMemoryIngestRepository()
    settings = Settings(
        _env_file=None,
        allow_degraded_results=False,
        paragraph_min_chars=20,
        paragraph_max_chars=500,
    )
    pipeline = IngestPipeline(
        repo,
        FakeLayoutBackend(),
        FailingLlmClient(settings),
        FakeEmbeddingClient(),
        settings=settings,
    )
    pdf_path = tmp_path / "failure.pdf"
    pdf_path.write_bytes(b"%PDF-test")

    with pytest.raises(RuntimeError, match="두 번 연속"):
        pipeline.run(str(pdf_path))

    assert repo.papers == {}
    assert repo.paragraphs == {}
    assert repo.paper_keywords == []
    assert any(row["stage"] == STAGE_5 and row["status"] == "failed" for row in repo.job_stages)
