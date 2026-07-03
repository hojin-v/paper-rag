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
                    text="RAG retrieval improves paper search.",
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


def test_pipeline_e2e_with_fake_components() -> None:
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
        triage_func=lambda path: "digital",
    )

    report = pipeline.run("paper.pdf")

    assert report.paper_id is not None
    assert report.totals["paragraphs"] == 1
    assert report.totals["keywords"] > 0
    assert report.totals["tables"] == 1
    assert report.totals["relations"] == 1
    assert repo.papers[report.paper_id]["title"] == "RAG Retrieval Study 2024"
    assert len(repo.paragraphs) == 1
    assert len(repo.keywords) > 1
    assert len(repo.paper_keywords) > 1
    assert len(repo.tables) == 1
    assert len(repo.relations) == 1
    assert repo.relations[0]["related_paper_id"] == candidate_id

    stages = {row["stage"] for row in repo.job_stages if row["status"] == "done"}
    assert stages == {STAGE_1, STAGE_2, STAGE_3, STAGE_4, STAGE_5, STAGE_6, STAGE_7, STAGE_8}
    assert not [row for row in repo.job_stages if row["status"] == "failed"]
