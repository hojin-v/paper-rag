from pathlib import Path

import pytest

from pdf_fixtures import PdfBuilder
from paperrag.config import Settings
from paperrag.ingest.embeddings import FakeEmbeddingClient
from paperrag.ingest.layout.docling_backend import DoclingBackend
from paperrag.ingest.llm_enrich import PassthroughEnricher
from paperrag.ingest.models import DocumentLayout
from paperrag.ingest.pipeline import IngestPipeline
from paperrag.ingest.repository import InMemoryIngestRepository


class CachedLayoutBackend:
    def __init__(self, layout: DocumentLayout) -> None:
        self.layout = layout

    def analyze(self, pdf_path: str) -> DocumentLayout:
        return self.layout


def test_docling_backend_extracts_tables_and_reference_boundary(tmp_path: Path) -> None:
    pytest.importorskip("docling")
    pdf_path = tmp_path / "docling_table_refs.pdf"
    _write_pdf_with_table_and_references(pdf_path)

    layout = DoclingBackend().analyze(str(pdf_path))

    assert sum(block.block_type == "table" for block in layout.blocks) >= 1
    assert sum(block.block_type == "section_header" for block in layout.blocks) >= 1
    positioned = [block for block in layout.blocks if block.bbox is not None]
    assert positioned
    assert all(
        0 <= block.bbox[0] < block.bbox[2] <= 595
        and 0 <= block.bbox[1] < block.bbox[3] <= 842
        for block in positioned
        if block.bbox is not None
    )

    repo = InMemoryIngestRepository()
    pipeline = IngestPipeline(
        repo,
        CachedLayoutBackend(layout),
        PassthroughEnricher(),
        FakeEmbeddingClient(),
        settings=Settings(_env_file=None, paragraph_min_chars=1, paragraph_max_chars=2000),
    )

    pipeline.run(str(pdf_path))

    paragraph_text = "\n".join(str(row["original_text"]) for row in repo.paragraphs.values())
    assert "Reference item that must not become a paragraph" not in paragraph_text


def _write_pdf_with_table_and_references(pdf_path: Path) -> None:
    builder = (
        PdfBuilder()
        .add_page(595, 842)
        .text(72, 72, "Docling Mapping Study", fontsize=20)
        .text(72, 112, "Abstract", fontsize=15)
        .text(72, 138, "This short abstract describes table extraction.", fontsize=11)
        .text(72, 184, "Introduction", fontsize=15)
        .text(
            72,
            210,
            "The body paragraph should survive the ingest pipeline dry run.",
            fontsize=11,
        )
        .text(72, 266, "Table 1. Metrics", fontsize=11)
    )
    _draw_table(builder)

    builder.add_page(595, 842).text(72, 72, "References", fontsize=16).text(
        72,
        104,
        "Reference item that must not become a paragraph.",
        fontsize=11,
    )

    builder.save(pdf_path)


def _draw_table(builder: PdfBuilder) -> None:
    x0 = 72
    y0 = 294
    cell_width = 120
    cell_height = 32
    rows = [["Metric", "Value"], ["Accuracy", "0.91"], ["Recall", "0.88"]]

    for row_index in range(len(rows) + 1):
        y = y0 + row_index * cell_height
        builder.line(x0, y, x0 + cell_width * 2, y, width=0.8)
    for col_index in range(3):
        x = x0 + col_index * cell_width
        builder.line(x, y0, x, y0 + cell_height * len(rows), width=0.8)

    for row_index, row in enumerate(rows):
        for col_index, text in enumerate(row):
            builder.text(
                x0 + col_index * cell_width + 8,
                y0 + row_index * cell_height + 21,
                text,
                fontsize=10,
            )
